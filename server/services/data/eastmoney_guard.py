"""eastmoney_guard.py - 东财 API 共享熔断器（2026-10-17，2026-10-19 加域名级熔断与错误分级）

背景：东财 push2/push2delay/datacenter 的 API 路径会做 IP 级封锁（403 / 连接重置），
一锁数小时。此前只有资金流向模块有自己的熔断（fundflow._CLIST_*），而情绪温度计、
AI 诊断的热点板块、龙虎榜、估值分位等热路径各自带 2 次重试 × 6~10 秒超时硬打，
封锁期间每个冷请求都要白等 2~10 秒 —— 行动台（默认首页）实测 9.2 秒就是这么来的。

本模块把熔断状态收敛成一处，所有东财调用方共用：
  · 连续失败达阈值即进入冷却窗口，期间 `skip()` 为真，调用方直接走兜底/缓存；
  · 一次成功即复位（解封后首轮恢复，不需要等冷却结束）；
  · 按 scope 分开计数（clist / ulist / datacenter / 池接口…），互不影响。

2026-10-19 两处新增，都是当天实测逼出来的：

1. **域名级熔断（`kind='blocked'`）**。东财封的是 IP，不是某个接口。当天 08:51 起
   `push2delay/…/clist` 全断（四个镜像 + 明文 http + POST + 换参数 + 换 UA 一律
   `RemoteDisconnected`），而 `push2/…/stock/get` 同时还能 200 —— 于是按 scope 分开的
   熔断各自为政：clist 熔断了，涨停池(push2ex)、龙虎榜(datacenter-web)、公告(np-anotice)
   照打不误，全天日志里这三家反复失败。封禁期间继续戳任何东财路径都是在给 IP 续封。
   现在连接级失败会同时打开**全域闸门**：`skip()` 对任何 scope 都返回真，
   直到某一次东财请求成功（`ok()`）或冷却到期。
2. **错误分级**。`classify()` 把异常分成 `blocked`（连不上/连上被重置/DNS 失败 = 风控信号，
   2 次即熔断、冷却 30 分钟起）与 `flaky`（超时/空 body/rc!=0/HTTP 5xx = 抖动，
   3 次才熔断、冷却 15 分钟起）。不分级就会把"东财偶尔回空包"当成封禁处理，
   也会把真封禁当成抖动反复重试。

线程安全：调用方有的在事件循环里、有的在 asyncio.to_thread 的工作线程里，
状态更新用 threading.Lock 保护（持有时间极短，不会成为新瓶颈）。

2026-11-06 两处改造（起因：当天 09:15:04 一次 clist 连接级失败 → 全域闸门开 4 小时，
把 14 天失败率只有 1.0% 的 push2ex 三池一起关到 13:16，情绪快照 33 行退潮/冰点全部
`stale=1`，转弱推送/修复推送/减仓影子全线静音——见 docs/系统更新日志.md 11-06 条）：

1. **按主机家族分闸**（`EASTMONEY_FAMILY_GATE_ENABLED`）。"封的是 IP"这个前提只在
   **多个家族同时**连接级失败时才成立。实测（`api_usage_daily` 2026-10-23~11-06）：
   `clist` 47.5%、`ulist` 18.7%（都在 push2delay/push2 = 家族 main），而
   `pools`（push2ex）1.0%、`datacenter`（datacenter-web）0.4%、`appdata` 1.9%、
   `ann`（np-anotice）0%。所以 blocked 只关**出事的那个家族**；只有当
   `EASTMONEY_IP_GATE_FAMILIES` 个家族同时 blocked，才升级成全域闸门（保留 10-19
   那条教训：真封 IP 时别各家族轮着去敲）。关掉这个开关＝逐字节回到改造前。
2. **blocked 即当日停调**（`EASTMONEY_DAY_STOP_ENABLED`）。冷却上限从 4 小时改成
   **抬到当日 23:59:59**，次日自然过期（零定时器、零 cron）。停调期间每
   `EASTMONEY_DAY_STOP_PROBE_SEC`（默认 1800s）放行**一次**探测请求，成功即由 `ok()`
   解除——不留这个窗口的话，"停到 23:59"就变成"09:15 抖一下、10:00 明明通了也照样瞎
   一整天"（11-06 现场 13:16 那次 ulist 成功就是把下午 16 个快照救回 fresh 的那一次）。
   把 `EASTMONEY_DAY_STOP_PROBE_SEC` 配成 0 = 严格的"当天不再调"。
   另加**人工暂停**：`pause(hours, scope)` 写进状态文件，优先级高于自动复位
   （`ok()` 不得清掉它），`resume()` 或到期自动解除；用于"东财不让用就停几天"。
"""
import json
import os
import threading
import time
from datetime import date, datetime, time as hhmmss

from app.config import (EASTMONEY_DAY_STOP_ENABLED, EASTMONEY_DAY_STOP_PROBE_SEC,
                        EASTMONEY_FAMILY_GATE_ENABLED, EASTMONEY_IP_GATE_FAMILIES)

# —— flaky（业务级抖动）——
_FAIL_THRESHOLD = 3          # 连续失败 3 次即熔断
_COOLDOWN_BASE_SEC = 900     # 基础冷却 15 分钟（2026-10-19 300→900：东财封锁以小时计，
                             # 5 分钟冷却等于每 5 分钟戳一次东财，反而延长封禁——10-19 实测封了近 5 小时）
_COOLDOWN_MAX_SEC = 3600     # 指数退避上限 1 小时

# —— blocked（连接级 = IP 风控）——
_BLOCKED_THRESHOLD = 2       # 两次"连上就被重置"就够说明问题了，第三次纯属续封
_DOMAIN_COOLDOWN_BASE_SEC = 1800   # 全域冷却 30 分钟起（当天实测封了近 5 小时，5 分钟一戳只会一直吃闭门羹）
_DOMAIN_COOLDOWN_MAX_SEC = 4 * 3600

_lock = threading.Lock()
_state: dict = {}            # scope -> {'streak': int, 'until': float, 'trips': int}
_domain = {'streak': 0, 'until': 0.0, 'trips': 0, 'probeAt': 0.0}   # 东财全域（IP 级封禁）状态

# —— 主机家族（2026-11-06）——
# 同一家族 = 同一个域名（同一批 IP 风控的粒度）。scope 到家族的映射要覆盖
# 所有已接入的 scope；没登记的按"自己一家"处理（宁可多敲一次，也不误伤别的链路）。
_FAMILY_OF_SCOPE = {
    # push2delay：clist 榜单（板块/资金流排行/估值翻页/RPS 代码表）+ ulist 批量行情
    'clist': 'main', 'ulist': 'main', 'rps': 'main', 'valuation': 'main',
    # push2 单票与北向：10-19 实测 push2delay 全断时它还能 200，属另一条链路
    'push2': 'push2', 'kamt': 'push2', 'marketcap': 'push2',
    'pools': 'pools',                                           # push2ex 三池
    'datacenter': 'datacenter', 'holder': 'datacenter',         # datacenter-web
    'dragon': 'datacenter', 'fundamental': 'datacenter',
    'appdata': 'appdata',                                       # emappdata 边缘节点
    'ann': 'notice',                                            # np-anotice 公告
}
_FAMILIES: dict = {}         # family -> {'streak': 连续 blocked 次数, 'until': float, 'trips': int}
_manual: dict = {}           # scope 或 'all' -> {'until': float, 'reason': str}

# 人工暂停的固定键：'all' 表示整个东财一起停（含 ignore_domain 的 scope）
_ALL = 'all'


def _family_of(scope: str) -> str:
    return _FAMILY_OF_SCOPE.get(scope, scope or 'default')


def _eod_ts(now: float = None) -> float:
    """当日 23:59:59 的 epoch（本地时区）。当天停调就是把 until 抬到这里。"""
    d = date.fromtimestamp(now if now is not None else time.time())
    return datetime.combine(d, hhmmss(23, 59, 59)).timestamp()


def _fam(family: str) -> dict:
    return _FAMILIES.setdefault(family, {'streak': 0, 'until': 0.0, 'trips': 0, 'probeAt': 0.0})


# 冷却状态跨进程持久化（2026-10-23）：原先只存在内存里，重启一次就等于把"正在封 IP"
# 忘掉，下一轮又去敲东财；而重启后的首个情绪请求恰好在 2s 预算里等不到结果，
# 行动台只能显示空壳。落盘后重启仍直接走兜底，既不续封也能立刻显示最近快照。
_STATE_FILE = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', '..', 'data', 'eastmoney_guard_state.json'))
_STATE_MAX_AGE_SEC = 86400  # 超过一天的旧状态不再恢复，避免把陈年退避次数带进来


def _save_state_locked() -> None:
    """把当前冷却状态原子写盘（调用方已持 _lock）。失败只打印，不影响请求。"""
    try:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        doc = {'saved_at': time.time(),
               'scopes': {k: dict(v) for k, v in _state.items()},
               'domain': dict(_domain),
               'families': {k: dict(v) for k, v in _FAMILIES.items()},
               'manual': {k: dict(v) for k, v in _manual.items()}}
        tmp = _STATE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(doc, f, ensure_ascii=False)
        os.replace(tmp, _STATE_FILE)
    except Exception as e:
        print(f'[东财熔断] 状态写盘失败(忽略): {e}')


def _load_state() -> None:
    """进程启动时恢复仍在冷却期内的状态；过期状态直接丢弃。

    例外：**人工暂停不受 `_STATE_MAX_AGE_SEC` 约束**——"停三天"这种指令不该因为
    进程恰好重启过就被忘掉（11-06 加）。只按自己的 until 判过期。
    """
    try:
        with open(_STATE_FILE, encoding='utf-8') as f:
            doc = json.load(f)
        now = time.time()
        with _lock:
            # 人工暂停先恢复（不受整份文件年龄限制）
            for key, m in (doc.get('manual') or {}).items():
                if isinstance(m, dict) and float(m.get('until') or 0) > now:
                    _manual[key] = {'until': float(m['until']),
                                    'reason': str(m.get('reason') or '')}
                    print(f'[东财熔断] 恢复人工暂停：{key} 剩余 {int((m["until"] - now) / 3600)}h'
                          f'（{m.get("reason", "")}）')
            if time.time() - float(doc.get('saved_at') or 0) > _STATE_MAX_AGE_SEC:
                return
            for scope, st in (doc.get('scopes') or {}).items():
                if not isinstance(st, dict):
                    continue
                until = float(st.get('until') or 0)
                # 冷却中，或失败计数还没到阈值：都恢复，避免重启后从 0 重新累积、
                # 让封禁期多挨一轮探测。
                if until > now or int(st.get('streak') or 0) or int(st.get('bstreak') or 0):
                    _state[scope] = {'streak': int(st.get('streak') or 0),
                                     'until': until,
                                     'trips': int(st.get('trips') or 0),
                                     'bstreak': int(st.get('bstreak') or 0)}
            for fam, fs in (doc.get('families') or {}).items():
                if not isinstance(fs, dict):
                    continue
                until = float(fs.get('until') or 0)
                if until > now or int(fs.get('streak') or fs.get('bstreak') or 0):
                    _FAMILIES[fam] = {'streak': int(fs.get('streak') or fs.get('bstreak') or 0),
                                      'until': until,
                                      'trips': int(fs.get('trips') or 0),
                                      # 重启后重新计时：原 probeAt 多半是过去时刻，
                                      # 直接沿用会让恢复后的第一轮变成"试探"
                                      'probeAt': (now + EASTMONEY_DAY_STOP_PROBE_SEC
                                                  if EASTMONEY_DAY_STOP_PROBE_SEC > 0 else until)}
                    if until > now:
                        print(f'[东财熔断] 从磁盘恢复冷却：家族 {fam} 剩余 {int(until - now)}s')
            dom = doc.get('domain') or {}
            until = float(dom.get('until') or 0)
            if until > now or int(dom.get('streak') or 0):
                _domain.update({'streak': int(dom.get('streak') or 0),
                                'until': until,
                                'trips': int(dom.get('trips') or 0),
                                'probeAt': (now + EASTMONEY_DAY_STOP_PROBE_SEC
                                            if EASTMONEY_DAY_STOP_PROBE_SEC > 0 else until)})
                if until > now:
                    print(f'[东财熔断] 从磁盘恢复冷却：全域剩余 {int(until - now)}s')
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f'[东财熔断] 状态恢复失败(忽略): {e}')


_load_state()


def classify(err) -> str:
    """异常 → 'blocked'（风控信号）| 'flaky'（抖动）。

    判据是"连接层有没有被对方断掉"：
      · RemoteDisconnected / ConnectionResetError / ConnectionAborted → blocked
      · DNS 解析失败（getaddrinfo / ConnectionError 包住的 NameResolutionError）→ blocked
      · requests 的 ConnectionError 基本都属连接层 → blocked
      · Timeout / HTTP 4xx·5xx / 空 body / rc!=0（这些是 ValueError 抛上来的）→ flaky
    """
    name = type(err).__name__
    if name in ('ConnectionError', 'ConnectTimeout', 'ProtocolError', 'RemoteDisconnected',
                'ConnectionResetError', 'ConnectionAbortedError', 'ChunkedEncodingError'):
        return 'blocked'
    s = f'{name}: {err}'
    low = s.lower()
    if ('remote end closed' in low or 'connection reset' in low or 'aborted' in low
            or 'getaddrinfo' in low or 'name or service not known' in low
            or 'max retries exceeded' in low):
        return 'blocked'
    return 'flaky'


def should_retry(err) -> bool:
    """连接级失败不值得重试：东财要么一次就通，要么在封这个 IP，重试只是给它续封。

    各重试循环（收评 clist、涨停池、资金流向、RPS）统一用它，
    避免"重试 3 次 × 12s 超时"在封禁日把一个接口拖到 30 秒以上。
    """
    return classify(err) != 'blocked'


def _manual_until(scope: str, now: float) -> float:
    """人工暂停的到期时刻：'all' ≥ 家族名 ≥ scope 名，取还在生效里最晚的那个（0=没暂停）。"""
    until = 0.0
    for key in (_ALL, _family_of(scope), scope):
        m = _manual.get(key)
        if m and now < float(m.get('until') or 0):
            until = max(until, float(m['until']))
    return until


def _skip_locked(scope: str, now: float, ignore_domain: bool = False,
                 consume_probe: bool = True) -> bool:
    """skip() 的实现体（调用方必须已持 _lock）。

    `consume_probe=False` 只给 status() 用：**查询状态不能把探测窗口吃掉**，
    否则"看一眼配置页"就等于少探一次东财。
    """
    if _manual_until(scope, now) > 0:
        return True
    st = _state.get(scope)
    if st and now < st['until']:
        return True
    if ignore_domain:
        return False
    if now < _domain['until']:
        # probeAt 缺省取 until：没显式安排过探测就等于老实冷却（老行为），
        # 自测/恢复流程手挑一个 until 时不会被当成"立即放行"而误打东财
        _pa = _domain.get('probeAt', 0.0)
        if (EASTMONEY_DAY_STOP_ENABLED and EASTMONEY_DAY_STOP_PROBE_SEC > 0
                and _pa > 0 and now >= _pa):
            if consume_probe:
                _domain['probeAt'] = now + EASTMONEY_DAY_STOP_PROBE_SEC
            return False
        return True
    if EASTMONEY_FAMILY_GATE_ENABLED:
        f = _FAMILIES.get(_family_of(scope))
        if f and now < f['until']:
            # 当日停调留一个**探测窗口**：每隔 EASTMONEY_DAY_STOP_PROBE_SEC 放行一次，
            # 成功即由 ok() 解除停调。不留窗口的话"停到 23:59"就变成"东财 09:15 抖一下，
            # 10:00 明明通了也照样瞎一整天"（11-06 现场 13:16 那次 ulist 成功就是把
            # 下午 16 个快照救回 fresh 的那一次）。
            _pa = f.get('probeAt', 0.0)
            if (EASTMONEY_DAY_STOP_ENABLED and EASTMONEY_DAY_STOP_PROBE_SEC > 0
                    and _pa > 0 and now >= _pa):
                if consume_probe:
                    f['probeAt'] = now + EASTMONEY_DAY_STOP_PROBE_SEC
                return False
            return True
    return False


def skip(scope: str = 'default', ignore_domain: bool = False) -> bool:
    """该不该跳过东财？人工暂停 > scope 自己冷却 > 家族闸门 > 全域闸门。

    `ignore_domain=True` 只给**独立边缘节点**用（2026-10-23 人气榜 appdata）：
    它和 push2/push2delay 不是同一条链路——10-23 当天 clist 只有 17/134 成功、
    全域在冷却，而 appdata 仍 16/16 成功。把 appdata 一起停掉，热搜榜在整个封禁期
    就只能空着；所以它只服从自己的 scope 冷却，失败也只关自己（见 fail(kind='flaky')）。
    注意：**人工暂停连 ignore_domain 也照样生效**——人工说停就是停，不分主次。
    """
    with _lock:
        return _skip_locked(scope, time.time(), ignore_domain)


def pause(hours: float = 0, reason: str = '', target: str = _ALL,
          actor: str = '') -> dict:
    """人工暂停东财（11-06 用户要求"先暂停一段时间"）。

    `target` 可以是 'all'（默认，整东财）、家族名（main/pool/dc/notice/appdata）
    或具体 scope（clist/ulist/pools/…）。`hours<=0` 等于不暂停（直接解除该 target）。
    写进状态文件，所以**看护重启后仍然是停的**；`ok()` 不会清掉人工暂停。
    """
    with _lock:
        now = time.time()
        if hours <= 0:
            _manual.pop(target, None)
        else:
            _manual[target] = {'until': now + hours * 3600, 'reason': reason,
                               'set_at': now, 'actor': actor}
        _save_state_locked()
        return {'target': target,
                'paused': target in _manual,
                'remainHours': round((_manual[target]['until'] - now) / 3600, 2)
                if target in _manual else 0.0,
                'reason': _manual.get(target, {}).get('reason', ''),
                'actor': actor}


def resume(target: str = _ALL) -> dict:
    """解除人工暂停（只解人工，自动冷却仍要自己到期或等一次成功）。"""
    with _lock:
        had = _manual.pop(target, None)
        _save_state_locked()
        return {'target': target, 'wasPaused': bool(had)}


def ok(scope: str = 'default', clear_domain: bool = True) -> None:
    """一次成功：默认清掉本 scope 计数、本家族闸门与全域闸门。

    `clear_domain=False` 供**独立边缘节点**使用（2026-10-23 人气榜 appdata）：
    它和 push2/push2delay 走的不是同一条链路——10-23 当天 clist 只有 17/134 成功、
    全域在冷却，而 appdata 仍 16/16 成功。此时若用 appdata 的成功去关全域闸门，
    会让 clist/ulist 立刻提前探测、给封禁续命；所以它只复位自己的 scope。

    "当日停调"在这里会被一次成功解除（11-06 现场就是这么恢复的：13:16 一次 ulist
    成功 → 全域闸门解除 → 下午 16 个快照全是 fresh）。保留这个行为是有意的：
    判据是"它真的通了"，比"今天曾经不通"更强；否则误判一次 blocked 就要白停一整天。
    人工暂停不受影响。
    """
    with _lock:
        had = bool(_state.pop(scope, None))
        fam = _family_of(scope)
        # 本家族的闸门只认"本家族真的通了"：pools(push2ex) 的成功不该替 main(push2delay)
        # 解除停调 —— 11-06 的教训正好是反过来（main 的失败把 pools 一起关了）。
        f = _FAMILIES.get(fam)
        if f and (f['until'] or f.get('streak')):
            print(f'[东财熔断] {scope} 请求成功 → 家族 {fam} 闸门解除'
                  f'（此前累计 blocked {f.get("streak", 0)} 次）')
            _FAMILIES[fam] = {'streak': 0, 'until': 0.0, 'trips': 0, 'probeAt': 0.0}
            had = True
        if clear_domain and (_domain['until'] or _domain['streak']):
            print(f'[东财熔断] {scope} 请求成功 → 全域闸门解除'
                  f'（此前累计 blocked {_domain["streak"]} 次）')
            had = True
        if clear_domain:
            _domain.update({'streak': 0, 'until': 0.0, 'trips': 0, 'probeAt': 0.0})
        if had:
            _save_state_locked()


def fail(scope: str = 'default', err=None, kind: str = None, now: float = None) -> None:
    """一次失败。传 `err`（异常对象）就自动分级；显式 `kind` 优先。

    调用方绝大多数是在 `except Exception as e:` 里报失败，所以默认走 err 分级；
    只传 scope 时按 'flaky' 处理（保守：不把没拿到异常上下文的失败当成封禁）。
    kind='blocked' 会额外打开**该 scope 所属主机家族的闸门**（11-06 之前是打开全域闸门，
    把 push2ex 三池、datacenter 龙虎榜这些当天明明还能用的链路一起关 4 小时）。

    两类失败**分开计数**（`streak` / `bstreak`）：混成一个计数的话，
    "一次网络抖动 + 一次连接被重置"就会按被封的档位冷却 30 分钟，
    把本来 15 分钟就能重试的接口白白关死半小时。

    `now` 只给自测用（注入假时间，不 sleep）。
    """
    if kind is None:
        kind = classify(err) if err is not None else 'flaky'
    if now is None:
        now = time.time()
    with _lock:
        st = _state.setdefault(scope, {'streak': 0, 'until': 0.0, 'trips': 0, 'bstreak': 0})
        st['streak'] += 1
        if kind == 'blocked':
            st['bstreak'] = st.get('bstreak', 0) + 1
        threshold = _BLOCKED_THRESHOLD if kind == 'blocked' else _FAIL_THRESHOLD
        base = _DOMAIN_COOLDOWN_BASE_SEC if kind == 'blocked' else _COOLDOWN_BASE_SEC
        cap = _DOMAIN_COOLDOWN_MAX_SEC if kind == 'blocked' else _COOLDOWN_MAX_SEC
        counted = st['bstreak'] if kind == 'blocked' else st['streak']
        if counted >= threshold and now >= st['until']:
            st['trips'] += 1
            cooldown = min(base * (2 ** (st['trips'] - 1)), cap)
            st['until'] = now + cooldown
            print(f'[东财熔断] {scope} 连续 {kind} 失败 {counted} 次'
                  f'（第 {st["trips"]} 次熔断），本接口冷却 {cooldown}s'
                  f'（上限 {cap}s；期间走兜底，不再请求东财）')
        if kind == 'blocked':
            _domain['streak'] += 1
            if EASTMONEY_FAMILY_GATE_ENABLED:
                fam = _family_of(scope)
                f = _fam(fam)
                f['streak'] = f.get('streak', 0) + 1
                if f['streak'] >= _BLOCKED_THRESHOLD and now >= f['until']:
                    f['trips'] += 1
                    if EASTMONEY_DAY_STOP_ENABLED:
                        # 抬到当日 23:59:59：次日自然过期，不需要任何定时器
                        f['until'] = _eod_ts(now)
                        f['probeAt'] = (now + EASTMONEY_DAY_STOP_PROBE_SEC
                                        if EASTMONEY_DAY_STOP_PROBE_SEC > 0 else f['until'])
                        _rest = (f['until'] - now) / 3600
                        print(f'[东财熔断] 家族 {fam} 连接级失败累计 {f["streak"]} 次'
                              f'（第 {f["trips"]} 次熔断）→ **当日停调**，剩余 {_rest:.1f}h'
                              f'（次日 00:00 自动恢复；期间该家族走备源/旧值，'
                              f'其它家族照常用）')
                    else:
                        cool = min(_DOMAIN_COOLDOWN_BASE_SEC * (2 ** (f['trips'] - 1)),
                                   _DOMAIN_COOLDOWN_MAX_SEC)
                        f['until'] = now + cool
                        f['probeAt'] = 0.0        # 不开探测：退避到期才放行（= 老行为节奏）
                        print(f'[东财熔断] 家族 {fam} 连续 blocked {f["streak"]} 次'
                              f'（第 {f["trips"]} 次熔断），本家族冷却 {cool}s')
                # 升级为 IP 级判据：不止一个家族同时在熔断，才说明封的是 IP
                hot = [k for k, v in _FAMILIES.items() if v['until'] > now]
                if (len(hot) >= max(2, EASTMONEY_IP_GATE_FAMILIES)
                        and now >= _domain['until']):
                    _domain['trips'] += 1
                    dcool = (_eod_ts(now) - now) if EASTMONEY_DAY_STOP_ENABLED else min(
                        _DOMAIN_COOLDOWN_BASE_SEC * (2 ** (_domain['trips'] - 1)),
                        _DOMAIN_COOLDOWN_MAX_SEC)
                    _domain['until'] = now + dcool
                    _domain['probeAt'] = (now + EASTMONEY_DAY_STOP_PROBE_SEC
                                          if EASTMONEY_DAY_STOP_PROBE_SEC > 0 else _domain['until'])
                    print(f'[东财熔断] {len(hot)} 个家族同时连接级失败（{"/".join(sorted(hot))}）'
                          f'→ 判定为 IP 级封禁，**全域闸门开启 {int(dcool)}s**：'
                          f'所有东财路径一起停手，直到某次请求成功或闸门到期')
            elif _domain['streak'] >= _BLOCKED_THRESHOLD and now >= _domain['until']:
                # 关掉分家族开关 = 逐字节回到 11-06 之前的老行为
                _domain['trips'] += 1
                dcool = min(_DOMAIN_COOLDOWN_BASE_SEC * (2 ** (_domain['trips'] - 1)),
                            _DOMAIN_COOLDOWN_MAX_SEC)
                _domain['until'] = now + dcool
                _domain['probeAt'] = 0.0       # 关掉分家族＝连探测一起关掉，逐字节等于改造前
                print(f'[东财熔断] 连接级失败累计 {_domain["streak"]} 次 → **全域闸门开启 {dcool}s**：'
                      f'封的是 IP，涨停池/龙虎榜/公告这些路径同期也不再请求东财，'
                      f'直到某次请求成功或闸门到期')
        _save_state_locked()



def reset(scope: str = 'default') -> None:
    """手动复位（自测/管理用）：只清本 scope。全域用 reset_domain()。"""
    with _lock:
        _state.pop(scope, None)
        _save_state_locked()


def reset_domain() -> None:
    """全开闸：全域 + 所有家族一起复位（管理/自测用）。

    11-06 之前这里只动 `_domain` 一处就等价于"所有东财路径放行"；分家族之后闸门分散在
    `_FAMILIES` 里，只清 `_domain` 会留下"以为全开了、其实 main 家族还在停调"的假象——
    `analysis_out/guard_coverage_selftest.py` 今天就正好踩在这上面。人工暂停仍然不动：
    那是"人说要停"，只能由 `resume()` 解。
    """
    with _lock:
        _domain.update({'streak': 0, 'until': 0.0, 'trips': 0, 'probeAt': 0.0})
        _FAMILIES.clear()
        _save_state_locked()


def status(scope: str = 'default') -> dict:
    """给配置页/诊断接口看的实时状态（"为什么这页是旧的"要能一句话答出来）。

    11-06 起要能分清三种"停"：人工暂停（manual）、本家族当日停调（family）、
    IP 级全域闸门（domain）。`blocksRequest` 是这一条 scope 此刻到底会不会发请求的答案，
    免得前端/排查时对着三个字段自己猜。
    """
    with _lock:
        now = time.time()
        st = _state.get(scope) or {'streak': 0, 'until': 0.0, 'trips': 0, 'bstreak': 0}
        fam = _family_of(scope)
        f = _FAMILIES.get(fam) or {'streak': 0, 'until': 0.0, 'trips': 0}
        mo = _manual_until(scope, now)
        scope_cooling = bool(st.get('until', 0) > now)
        fam_cooling = EASTMONEY_FAMILY_GATE_ENABLED and f['until'] > now
        dom_cooling = _domain['until'] > now
        return {'scope': scope, 'family': fam,
                'streak': st['streak'], 'blockedStreak': st.get('bstreak', 0),
                'trips': st.get('trips', 0),
                'cooling': scope_cooling,
                'cooldownRemainSec': max(0, int(st['until'] - now)),
                # —— 家族闸门（11-06 新增）——
                'familyBlockedStreak': f.get('streak', 0),
                'familyTrips': f.get('trips', 0),
                'familyCooling': fam_cooling,
                'familyRemainSec': max(0, int(f['until'] - now)),
                'familyStoppedToday': bool(fam_cooling and EASTMONEY_DAY_STOP_ENABLED
                                          and f['until'] >= _eod_ts(now) - 1),
                'familyNextProbeSec': max(0, int(f.get('probeAt', 0.0) - now)) if fam_cooling else 0,
                'familyRecoverAt': (datetime.fromtimestamp(f['until']).strftime('%m-%d %H:%M:%S')
                                    if f['until'] > now else None),
                # —— 全域（IP 级）闸门 ——
                'domainBlockedStreak': _domain['streak'],
                'domainCooling': dom_cooling,
                'domainRemainSec': max(0, int(_domain['until'] - now)),
                'domainTrips': _domain['trips'],
                'domainNextProbeSec': (max(0, int(_domain.get('probeAt', 0.0) - now))
                                       if dom_cooling else 0),
                # —— 人工暂停 ——
                'manualPaused': mo > 0,
                'manualRemainSec': max(0, int(mo - now)) if mo else 0,
                'manualUntil': (datetime.fromtimestamp(mo).strftime('%m-%d %H:%M:%S') if mo else None),
                'manualReason': next((_manual[k]['reason'] for k in (_ALL, fam, scope)
                                      if k in _manual and now < _manual[k]['until']), ''),
                # 和 skip() 同一个判据（含探测窗口），前端不用再自己拼三个字段
                'blocksRequest': _skip_locked(scope, now, False, consume_probe=False)}


def families_status() -> dict:
    """一页看全：每个家族此刻停不停、停到什么时候、为什么。"""
    with _lock:
        now = time.time()
        out = {}
        for fam in set(list(_FAMILIES) + list({_family_of(s) for s in _FAMILY_OF_SCOPE})):
            f = _FAMILIES.get(fam) or {'streak': 0, 'until': 0.0, 'trips': 0}
            mo = _manual_until(fam, now) or _manual_until(_ALL, now)
            out[fam] = {'blockedStreak': f.get('streak', 0), 'trips': f.get('trips', 0),
                        'cooling': f['until'] > now,
                        'remainSec': max(0, int(f['until'] - now)),
                        'recoverAt': (datetime.fromtimestamp(f['until']).strftime('%m-%d %H:%M:%S')
                                      if f['until'] > now else None),
                        'manualPaused': mo > 0}
        out['_domain'] = {'blockedStreak': _domain['streak'], 'trips': _domain['trips'],
                          'cooling': _domain['until'] > now,
                          'remainSec': max(0, int(_domain['until'] - now))}
        out['_manual'] = {k: {'until': datetime.fromtimestamp(v['until']).strftime('%m-%d %H:%M:%S'),
                              'reason': v.get('reason', ''),
                              'remainSec': max(0, int(v['until'] - now))}
                          for k, v in _manual.items() if v['until'] > now}
        return out


def reset_all() -> None:
    """全清（自测/管理用）：scope 计数、家族闸门、全域闸门；**不动人工暂停**。"""
    with _lock:
        _state.clear()
        _FAMILIES.clear()
        _domain.update({'streak': 0, 'until': 0.0, 'trips': 0, 'probeAt': 0.0})
        _save_state_locked()

