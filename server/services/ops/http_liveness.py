# -*- coding: utf-8 -*-
"""http_liveness.py - 后端 HTTP 自愈探活（2026-11-09）

要补的是哪一格
--------------
11-06 那次补了「调度链失活告警」（services/ops/sched_liveness.py），但它判的是
**进程内调度还在不在跑**。11-07 与 11-09 两次事故暴露的是相反的一面：
**进程活着、调度还在跑，HTTP 却已经不应答。**

  · 2026-11-07：一条公开 URL 触发撮合死循环，事件循环被占 14 分钟。端口一直 LISTENING，
    看护的探活是 `socket.create_connection(('127.0.0.1', 3003))`——内核把连接收进
    backlog 就算「活着」，所以那 14 分钟看护一直以为后端正常。
  · 2026-11-09：日志里只出现**一条** `Accept failed on a socket`
    （OSError [WinError 64] 指定的网络名不再可用）。这是 CPython 3.12 IOCP 监听循环的
    已知行为（`asyncio/proactor_events.py:_start_serving.loop()` 的 except 分支会
    `sock.close()`，且不再把 accept 任务挂回去）——**一次 OSError 就永久关掉监听 socket**。
    进程其余线程照跑（16:16:14 还在落接口用量、16:16:35 还有预热冷算完成），
    页面与接口全挂，直到 16:17:42 由新进程（PID 4220）重新监听。

本模块只做一件事
----------------
独立 daemon 线程，每 30 秒用标准库 `http.client` 探一次 `127.0.0.1:3003/api/health`；
**连续 3 次失败**就写 `server/data/backend_restart_requested`，交给仓库外的 SYSTEM 看护
（`backend_watchdog.py`，每 5 分钟一轮、有权限 taskkill）去重启。会话没有权限杀 SYSTEM
进程，所以「写标志」是本仓唯一合法的自愈出口。

为什么必须是独立线程
--------------------
判据和动作都不能跑在被判的对象上。事件循环卡死时，循环里的探活根本不会执行；
放进 `run_scheduler()` 主循环也不行——那和 sched_liveness 是同一个被观察者。
所以与 `sched_liveness.start_watchdog()` 同一写法：独立线程、整个 tick 包在 try 里
（一个坏 tick 不能打死观察者，这是 11-06 的教训反过来用）。

「端口没在听」为什么**不算**失败
--------------------------------
这是本模块最容易写错的一处，也是它与看护分工的边界：

  · uvicorn 的顺序是**先跑 lifespan.startup()、后 bind 端口**（uvicorn/server.py:94）。
    本仓启动时 lifespan 要拉热搜、预热缓存，实测启动耗时 190~751 秒；这段时间端口
    根本不 LISTENING，任何探活都不可能成功。
  · 端口没在听时，看护的 `port_down()` 分支本来就会拉起后端。我们再写一个标志，
    只会让「看护拉起」和「标志重启」两路同时动手，正是 3003 双进程的老坑。
  · 所以必须把「端口没在听」和「端口在听但不应答」分开：前者清零不计数，后者才计数。
    这才是看护看不见、也是 11-07 那种死法。

**怎么分开：不能靠异常类型，得问操作系统。** 本机实测（2026-11-09）：向未监听的
回环端口 `socket.create_connection(timeout=1)` 不返回 RST，而是**等满超时**
（39991/39992 两个空端口各 1.02s / 1.01s，升 TimeoutError）。按「refused=端口没听、
timeout=坏了」写，会在每次启动的预热期里判死并写标志，正好触发上面那个双进程老坑。
所以探活失败时再跑一次 `netstat -ano -p TCP` 看有没有人 LISTENING 在目标端口：
  · 有人在听 + 连不上/超时  ⇒ 计数（应用层死了）
  · 没人听               ⇒ port_down，清零、不写标志（看护负责拉起）
  · netstat 都读不到      ⇒ unknown，不猜也不写（宁可漏一次自愈，不制造双进程）
`netstat` 只在失败路径上跑，正常时每轮就一次本机 HTTP 请求。

写标志之后的两条收敛
--------------------
  · **冷却**：`HTTP_LIVENESS_HEAL_COOLDOWN_MIN`（默认 30 分钟）内不重复写；标志已存在
    且不是我们写的，也不覆盖（别人可能正等着用这个标志换代码）。
  · **恢复即撤销**：若探活又通了，而磁盘上的标志**确实是我们这个进程写的**（正文首行
    带 token，按 token 认领），就删掉它——重启很贵（启动 190~751 秒），一次几十秒的
    抖动不该留下一次重启请求。不是我们写的标志一律不删。

边界（如实）
------------
  · 不管「进程整个没了 / 端口没在听」——那是看护的地盘。
  · 不管「HTTP 正常、调度不动」——那是 sched_liveness 的地盘。
  · 盘中（工作日 09:10~15:10）看护会**推迟**执行重启、标志保留到 15:10 之后；本模块照写
    不误，推送正文里写明这一点，不假装「已经重启好了」。
  · 关掉开关（配置页 `http_liveness_enabled`）时连探活都不做、不起线程，逐字节等于没这项。
"""
from __future__ import annotations

import http.client
import os
import subprocess
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from app.config import (HTTP_LIVENESS_ENABLED, HTTP_LIVENESS_FAIL_STREAK,
                        HTTP_LIVENESS_HEAL_COOLDOWN_MIN, HTTP_LIVENESS_HOST,
                        HTTP_LIVENESS_INTERVAL_SEC, HTTP_LIVENESS_PATH,
                        HTTP_LIVENESS_PORT, HTTP_LIVENESS_START_GRACE_SEC,
                        HTTP_LIVENESS_TIMEOUT_SEC)

__all__ = ['probe', 'check_once', 'handle', 'start_watchdog', 'stop_watchdog',
           'is_enabled', 'state_snapshot']

# 本文件在 server/services/ops/ 下，到 server 根要往上走三层（ops→services→server）。
SERVER_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FLAG_REL = os.path.join('data', 'backend_restart_requested')

# 进程内状态：重启即清零（运维告警不建表，审计看 server_supervised.log）
_STATE: Dict[str, Any] = {
    'started_ts': 0.0,
    'stop': False,
    'thread': None,
    'streak': 0,             # 连续失败计数（refused 与宽限期会清零）
    'last_fail_streak': 0,   # 恢复时用来说「此前连续失败几次」
    'last_ok_ts': 0.0,
    'last_heal_ts': 0.0,     # 上次写标志的时刻（冷却用）
    'flag_by_us': False,     # 磁盘上那份标志是不是本进程写的
    'flag_token': '',        # 写入时放在正文首行的认领凭据
    'last_log_key': '',      # 上一轮打印过的状态，变了才打（防止刷屏）
    'last_probe': None,
}
_LOCK = threading.Lock()


def _cfg(key: str, default: Any) -> Any:
    """先热读配置页覆盖表，读不到再退回 config 现值（与海选影子/sched_liveness 同一写法）。"""
    try:
        from services.support import app_settings
        if any(i['key'] == key for i in app_settings.SPEC):
            v = app_settings.value(key)
            if v is not None:
                return v
    except Exception:
        pass
    import app.config as _cfg_mod
    return getattr(_cfg_mod, key.upper(), default)


def is_enabled() -> bool:
    return bool(_cfg('http_liveness_enabled', HTTP_LIVENESS_ENABLED))


def _flag_path() -> str:
    """重启标志的绝对路径。做成函数是为了自测能把 SERVER_DIR 换到临时目录，
    绝不碰线上那份标志（自测里有一条断言专门盯这件事）。"""
    return os.path.join(SERVER_DIR, FLAG_REL)


def state_snapshot() -> Dict[str, Any]:
    """给自测/日志用的状态快照（不改动状态）。"""
    with _LOCK:
        out = {k: v for k, v in _STATE.items() if k != 'thread'}
    out['thread_alive'] = bool(_STATE.get('thread') and _STATE['thread'].is_alive())
    return out


_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def _port_listening(port: Optional[int] = None) -> Optional[bool]:
    """目标端口上有没有进程在 LISTENING。True/False；读不到返回 None（不猜）。

    为什么不靠 connect 异常：本机实测未监听的回环端口是**超时**而不是 RST
    （见文件头），异常类型分不开「没人听」和「有人听但死了」。
    """
    if port is None:
        port = int(_cfg('http_liveness_port', HTTP_LIVENESS_PORT))
    try:
        out = subprocess.run(['netstat', '-ano', '-p', 'TCP'], capture_output=True,
                             text=True, timeout=10, creationflags=_NO_WINDOW).stdout
    except Exception:
        return None
    want = ':' + str(port)
    for line in out.splitlines():
        cols = line.split()
        # netstat 列序：TCP 本地地址 外部地址 状态 PID
        if len(cols) >= 5 and cols[0] == 'TCP' and cols[3] == 'LISTENING' \
                and cols[1].endswith(want):
            return True
    return False


def probe(timeout_sec: Optional[float] = None) -> Dict[str, Any]:
    """探一次本机 /api/health。只读，不发业务请求、不查库。

    返回 `{'ok', 'state', 'status', 'elapsed', 'detail', 'host', 'port', 'path'}`。
    `state` 取值：ok / port_down / unknown / timeout / io_error / http_error / bad_body / error
    （`port_down` = 没人 LISTENING，交给看护；`unknown` = 连不上且 netstat 也读不到，不计数）
    """
    timeout = float(timeout_sec if timeout_sec is not None
                    else _cfg('http_liveness_timeout_sec', HTTP_LIVENESS_TIMEOUT_SEC))
    host = str(_cfg('http_liveness_host', HTTP_LIVENESS_HOST))
    port = int(_cfg('http_liveness_port', HTTP_LIVENESS_PORT))
    path = str(_cfg('http_liveness_path', HTTP_LIVENESS_PATH))
    t0 = time.time()
    out: Dict[str, Any] = {'ok': False, 'state': 'error', 'status': None, 'elapsed': 0.0,
                           'detail': '', 'host': host, 'port': port, 'path': path}
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        # 先单独建连，才能把「端口没在听」与「连上了但不应答」分开——
        # 前者交给看护（见文件头），后者才是本模块要抓的。连不上时问一次 netstat。
        try:
            conn.connect()
        except OSError as e:
            listed = _port_listening(port)
            if isinstance(e, ConnectionRefusedError):
                out['state'] = 'port_down'      # 收到 RST = 确实没人听
            elif listed is False:
                out['state'] = 'port_down'      # netstat 明确说没人听
            elif listed is None:
                out['state'] = 'unknown'        # 读不到：不猜、不计数
            else:
                out['state'] = 'timeout' if isinstance(e, TimeoutError) else 'io_error'
            out['detail'] = f'{type(e).__name__}: {e}; listening={listed}'[:200]
            return _finish(out, t0)

        conn.request('GET', path, headers={'Connection': 'close'})
        resp = conn.getresponse()
        body = resp.read(2048)
        out['status'] = int(resp.status)
        text = body.decode('utf-8', 'ignore')
        if resp.status == 200 and '"ok"' in text:
            out['ok'] = True
            out['state'] = 'ok'
        elif resp.status == 200:
            out['state'] = 'bad_body'
            out['detail'] = 'HTTP 200 但正文不含 "ok"：%s' % text[:80]
        else:
            out['state'] = 'http_error'
            out['detail'] = 'HTTP %s' % resp.status
    except TimeoutError as e:
        out['state'] = 'timeout'
        out['detail'] = f'{type(e).__name__}: {e}'[:160]
    except OSError as e:
        out['state'] = 'io_error'
        out['detail'] = f'{type(e).__name__}: {e}'[:160]
    except Exception as e:          # BadStatusLine / RemoteDisconnected 兜底
        out['state'] = 'error'
        out['detail'] = f'{type(e).__name__}: {e}'[:160]
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return _finish(out, t0)


def _finish(out: Dict[str, Any], t0: float) -> Dict[str, Any]:
    out['elapsed'] = round(time.time() - t0, 3)
    return out


def check_once(now: Optional[datetime] = None, force: bool = False,
               probe_fn: Optional[Callable[[], Dict[str, Any]]] = None) -> Dict[str, Any]:
    """跑一次判定，**不写文件、不推送**（动作在 `handle()` 里，能不能做与做没做分开）。

    `force=True` 只给自测/人工点验用：跳过自愈冷却，仍然照实判据。
    `probe_fn` 只给自测注入假探针用。
    """
    now = now or datetime.now()
    res: Dict[str, Any] = {'enabled': is_enabled(), 'now': now.strftime('%H:%M:%S'),
                           'day': now.strftime('%Y-%m-%d'), 'verdict': 'disabled',
                           'skipped': '', 'streak': 0, 'need': None, 'heal': False,
                           'probe': None}
    if not res['enabled']:
        res['skipped'] = '开关关闭：连探活都不做'
        return res

    p = (probe_fn or probe)()
    res['probe'] = p
    _STATE['last_probe'] = p
    with _LOCK:
        if p.get('ok'):
            had = int(_STATE.get('streak') or 0)
            _STATE['streak'] = 0
            _STATE['last_fail_streak'] = had
            _STATE['last_ok_ts'] = time.time()
            res['verdict'] = 'recover' if had else 'ok'
            return res

        if p.get('state') in ('refused', 'port_down', 'unknown'):
            # 端口没在听：看护的 port_down 分支本来就负责拉起；我们再写标志会让两路同时
            # 动手（3003 双进程老坑）。所以清零、不计数，只记一行。
            # unknown = 连不上且 netstat 读不到：同样不猜（宁可漏一次自愈，不制造双进程）。
            _STATE['streak'] = 0
            res['verdict'] = 'unknown' if p.get('state') == 'unknown' else 'port_down'
            return res

        grace = float(_cfg('http_liveness_start_grace_sec', HTTP_LIVENESS_START_GRACE_SEC))
        if _STATE.get('started_ts') and (time.time() - _STATE['started_ts']) < grace:
            _STATE['streak'] = 0
            res['verdict'] = 'grace'
            res['skipped'] = f'启动宽限期内（{grace:g} 秒）不判死'
            return res

        need = max(1, int(_cfg('http_liveness_fail_streak', HTTP_LIVENESS_FAIL_STREAK)))
        _STATE['streak'] = int(_STATE.get('streak') or 0) + 1
        res['streak'] = _STATE['streak']
        res['need'] = need
        if _STATE['streak'] < need:
            res['verdict'] = 'fail'
            return res

        flag = _flag_path()
        if os.path.exists(flag) and not _STATE.get('flag_by_us'):
            # 已有别人（含上一个进程）写的标志在等看护：不覆盖、不重复写
            res['verdict'] = 'flag_pending'
            return res
        cooldown = float(_cfg('http_liveness_heal_cooldown_min',
                              HTTP_LIVENESS_HEAL_COOLDOWN_MIN)) * 60.0
        if not force and (time.time() - float(_STATE.get('last_heal_ts') or 0.0)) < cooldown:
            res['verdict'] = 'cooldown'
            return res
        res['verdict'] = 'heal'
        res['heal'] = True
        return res


def _render(res: Dict[str, Any], now: datetime) -> str:
    """推送正文。数字全部来自本次判定，不写「可能/大概」。"""
    p = res.get('probe') or {}
    timeout = _cfg('http_liveness_timeout_sec', HTTP_LIVENESS_TIMEOUT_SEC)
    return '\n'.join([
        '[后端自愈] %s HTTP 探活连续 %s 次失败，已写重启标志'
        % (now.strftime('%m-%d %H:%M'), res.get('streak')),
        '探活：GET http://%s:%s%s，超时 %ss；最近一次 state=%s，耗时 %ss'
        % (p.get('host'), p.get('port'), p.get('path'), timeout,
           p.get('state'), p.get('elapsed')),
        '含义：进程还在（调度照跑），但 HTTP 已经不应答。看护探的是 TCP 端口，'
        '端口在听时它看不见这种死法（11-07 事件循环卡死 14 分钟就是这样漏掉的）。',
        '处置：看护每 5 分钟一轮，读到 server/data/backend_restart_requested 会重启后端；'
        '工作日盘中 09:10~15:10 它会推迟到 15:10 之后执行（标志保留）。'
        '若下一次探活就恢复，已写的这份标志会自动撤销（只撤本进程写的那份）。',
    ])


def _send(text: str) -> bool:
    try:
        from services.support.push import push_wecom_webhook
        return bool(push_wecom_webhook(text))
    except Exception as e:
        print(f'[后端自愈] 推送失败（不影响标志）: {e}')
        return False


def _write_flag(res: Dict[str, Any]) -> bool:
    """写重启标志。首行 token 是「这份是我写的」唯一凭据，恢复时按它认领。"""
    p = res.get('probe') or {}
    flag = _flag_path()
    token = 'http_liveness pid=%d ts=%.3f' % (os.getpid(), time.time())
    try:
        parent = os.path.dirname(flag)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(flag, 'w', encoding='utf-8', newline='\n') as f:
            f.write(token + '\n')
            f.write('原因：连续 %s 次 HTTP 探活失败（最近一次 state=%s，耗时 %ss）\n'
                    % (res.get('streak'), p.get('state'), p.get('elapsed')))
            f.write('写入时间：%s\n' % datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            f.write('由 services/ops/http_liveness.py 自动写入；探活恢复时会自行撤销。\n')
        with _LOCK:
            _STATE['flag_by_us'] = True
            _STATE['flag_token'] = token
        return True
    except Exception as e:
        print(f'[后端自愈] 写重启标志失败: {type(e).__name__}: {e}')
        return False


def _cancel_own_flag() -> bool:
    """撤销本进程写的那份标志；认不出 token 就一律不删（可能是别的会话的请求）。"""
    if not _STATE.get('flag_by_us'):
        return False
    flag = _flag_path()
    token = str(_STATE.get('flag_token') or '')
    try:
        with open(flag, 'r', encoding='utf-8', errors='replace') as f:
            head = f.readline().strip()
        if token and head == token:
            os.remove(flag)
            print('[后端自愈] HTTP 已恢复，已撤销本进程写的那份重启请求（%s）' % FLAG_REL)
            return True
        print('[后端自愈] 重启标志的内容已被别的写入方改过，不撤销（保留对方意图）')
    except FileNotFoundError:
        pass                    # 看护已经消费掉了
    except Exception as e:
        print(f'[后端自愈] 撤销标志时读文件失败（保留不删）: {type(e).__name__}: {e}')
    finally:
        with _LOCK:
            _STATE['flag_by_us'] = False
            _STATE['flag_token'] = ''
    return False


def _log_once(key: str, msg: str) -> None:
    """同一状态只打一行。key 里**不许拼当前时刻**——那会让去重每轮都失效（修2 的教训）。"""
    if _STATE.get('last_log_key') == key:
        return
    _STATE['last_log_key'] = key
    print(msg)


def handle(res: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
    """把判定变成动作：写标志 / 恢复时撤销 / 按状态变化打日志。推送失败不写去重标记。"""
    out = {'wrote_flag': False, 'cancelled': False, 'pushed': False}
    now = datetime.now()
    if not res.get('enabled'):
        _log_once('disabled', f'[后端自愈 {now:%H:%M:%S}] 本轮不发：{res.get("skipped")}')
        return out

    p = res.get('probe') or {}
    if p.get('ok') and _STATE.get('flag_by_us'):
        out['cancelled'] = _cancel_own_flag()

    if res.get('heal'):
        if _write_flag(res):
            out['wrote_flag'] = True
            with _LOCK:
                _STATE['last_heal_ts'] = time.time()
            out['pushed'] = _send(_render(res, now))
            _log_once('heal', '[后端自愈 %s] 连续 %s 次探活失败（%s），已写重启标志，推送=%s'
                      % (now.strftime('%H:%M:%S'), res.get('streak'), p.get('state'),
                         '成功' if out['pushed'] else '失败'))
        else:
            _log_once('write_failed', f'[后端自愈 {now:%H:%M:%S}] 写重启标志失败（下一轮重试）')
        return out

    verdict = res.get('verdict')
    if verdict == 'port_down':
        _log_once('port_down', '[后端自愈] 端口没在听：交给看护的 port_down 分支拉起，不写标志')
    elif verdict == 'unknown':
        _log_once('unknown', '[后端自愈] 探活连不上且读不到 netstat（不知道有没有人在听）：'
                             '本类不计数、不写标志')
    elif verdict == 'flag_pending':
        _log_once('flag_pending', f'[后端自愈 {now:%H:%M:%S}] 已有重启标志在等看护处理，不重复写')
    elif verdict == 'cooldown':
        _log_once('cooldown', f'[后端自愈 {now:%H:%M:%S}] 探活仍失败，但在自愈冷却期内，不重复写标志')
    elif verdict == 'recover':
        _log_once('recover', '[后端自愈 %s] HTTP 已恢复（此前连续失败 %s 次）'
                  % (now.strftime('%H:%M:%S'), _STATE.get('last_fail_streak')))
    elif verdict == 'fail':
        _log_once('fail', '[后端自愈 %s] 探活失败 %s/%s（%s，%ss），未到阈值'
                  % (now.strftime('%H:%M:%S'), res.get('streak'), res.get('need'),
                     p.get('state'), p.get('elapsed')))
    elif verdict == 'grace':
        _log_once('grace', f'[后端自愈] 本轮不发：{res.get("skipped")}')
    return out


def _watchdog_loop(interval_sec: float) -> None:
    """观察者线程：整个 tick（含 sleep）都包在 try 里——一次异常不能打死观察者。"""
    while not _STATE.get('stop'):
        try:
            handle(check_once())
            time.sleep(interval_sec)
        except Exception as e:
            try:
                print(f'[后端自愈] 观察线程本轮异常（不影响下一轮）: {type(e).__name__}: {e}')
                time.sleep(interval_sec)
            except Exception:
                pass


def stop_watchdog() -> bool:
    """让观察线程在下一个 tick 自然退出（自测收尾用）；返回是否已退出。"""
    _STATE['stop'] = True
    t = _STATE.get('thread')
    if t and t.is_alive():
        t.join(timeout=3)
    alive = bool(_STATE.get('thread') and _STATE['thread'].is_alive())
    if not alive:
        _STATE['thread'] = None
    return not alive


def start_watchdog(interval_sec: Optional[int] = None) -> Optional[threading.Thread]:
    """由 `app/main.py` 在 lifespan 里、调度线程起来之后调用；开关关掉时不起线程（零副作用）。"""
    _STATE['started_ts'] = time.time()
    if not is_enabled():
        print('[后端自愈] 开关关闭，不起探活线程（逐字节等于没这项）')
        return None
    if interval_sec is None:
        # 配置值有下限 5 秒（写标志是重动作，不能被配成每秒一轮）；显式传参的（自测）按原值。
        interval_sec = max(5, int(_cfg('http_liveness_interval_sec', HTTP_LIVENESS_INTERVAL_SEC)))
    else:
        interval_sec = max(0.05, float(interval_sec))
    _STATE['stop'] = False
    if _STATE['thread'] and _STATE['thread'].is_alive():
        return _STATE['thread']
    t = threading.Thread(target=_watchdog_loop, args=(interval_sec,),
                         daemon=True, name='http-liveness')
    t.start()
    _STATE['thread'] = t
    print('[后端自愈] HTTP 探活线程已启动（每 %ss 一次 GET %s:%s%s，超时 %ss；'
          '连续 %s 次失败写重启标志）'
          % (interval_sec, HTTP_LIVENESS_HOST, HTTP_LIVENESS_PORT, HTTP_LIVENESS_PATH,
             _cfg('http_liveness_timeout_sec', HTTP_LIVENESS_TIMEOUT_SEC),
             _cfg('http_liveness_fail_streak', HTTP_LIVENESS_FAIL_STREAK)))
    return t
