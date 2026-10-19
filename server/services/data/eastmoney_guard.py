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
"""
import threading
import time

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
_domain = {'streak': 0, 'until': 0.0, 'trips': 0}   # 东财全域（IP 级封禁）状态


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


def skip(scope: str = 'default') -> bool:
    """该不该跳过东财？scope 自己在冷却，**或全域闸门开着**，都算该跳过。"""
    with _lock:
        now = time.time()
        st = _state.get(scope)
        if st and now < st['until']:
            return True
        return now < _domain['until']


def ok(scope: str = 'default') -> None:
    """一次成功：说明 IP 没被封 —— 清掉本 scope 计数，并关掉全域闸门。"""
    with _lock:
        _state.pop(scope, None)
        if _domain['until'] or _domain['streak']:
            print(f'[东财熔断] {scope} 请求成功 → 全域闸门解除'
                  f'（此前累计 blocked {_domain["streak"]} 次）')
        _domain.update({'streak': 0, 'until': 0.0, 'trips': 0})


def fail(scope: str = 'default', err=None, kind: str = None) -> None:
    """一次失败。传 `err`（异常对象）就自动分级；显式 `kind` 优先。

    调用方绝大多数是在 `except Exception as e:` 里报失败，所以默认走 err 分级；
    只传 scope 时按 'flaky' 处理（保守：不把没拿到异常上下文的失败当成封禁）。
    kind='blocked' 会额外打开全域闸门（所有东财路径一起停手）。

    两类失败**分开计数**（`streak` / `bstreak`）：混成一个计数的话，
    "一次网络抖动 + 一次连接被重置"就会按被封的档位冷却 30 分钟，
    把本来 15 分钟就能重试的接口白白关死半小时。
    """
    if kind is None:
        kind = classify(err) if err is not None else 'flaky'
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
            if _domain['streak'] >= _BLOCKED_THRESHOLD and now >= _domain['until']:
                _domain['trips'] += 1
                dcool = min(_DOMAIN_COOLDOWN_BASE_SEC * (2 ** (_domain['trips'] - 1)),
                            _DOMAIN_COOLDOWN_MAX_SEC)
                _domain['until'] = now + dcool
                print(f'[东财熔断] 连接级失败累计 {_domain["streak"]} 次 → **全域闸门开启 {dcool}s**：'
                      f'封的是 IP，涨停池/龙虎榜/公告这些路径同期也不再请求东财，'
                      f'直到某次请求成功或闸门到期')


def reset(scope: str = 'default') -> None:
    """手动复位（自测/管理用）：只清本 scope。全域用 reset_domain()。"""
    with _lock:
        _state.pop(scope, None)


def reset_domain() -> None:
    with _lock:
        _domain.update({'streak': 0, 'until': 0.0, 'trips': 0})


def status(scope: str = 'default') -> dict:
    """给配置页/诊断接口看的实时状态（"为什么这页是旧的"要能一句话答出来）。"""
    with _lock:
        now = time.time()
        st = _state.get(scope) or {'streak': 0, 'until': 0.0, 'trips': 0, 'bstreak': 0}
        return {'scope': scope, 'streak': st['streak'], 'blockedStreak': st.get('bstreak', 0),
                'trips': st.get('trips', 0),
                'cooling': now < st['until'],
                'cooldownRemainSec': max(0, int(st['until'] - now)),
                'domainBlockedStreak': _domain['streak'],
                'domainCooling': now < _domain['until'],
                'domainRemainSec': max(0, int(_domain['until'] - now)),
                'domainTrips': _domain['trips']}
