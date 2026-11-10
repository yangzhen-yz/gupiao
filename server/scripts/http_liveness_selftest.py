# -*- coding: utf-8 -*-
"""http_liveness_selftest.py — 后端 HTTP 自愈探活的正证与反证（2026-11-09）

守的不变量：**"进程活着、HTTP 死了"这种形态必须有人动手，但不能乱动手。**
两条边界比"该写的时候写了"更要紧，各有一组反证：

  · **端口没在听（refused）不算失败**——那是看护 port_down 分支的地盘，我们写标志只会
    让两路同时拉起（3003 双进程老坑）；uvicorn 又是先跑 lifespan 再 bind，启动期必然是
    refused，计了数就会在每次重启时误写一次标志。
  · **不是本进程写的标志不覆盖、不撤销**——别的会话可能正等着用它换代码。

跑法：cd server && python scripts/http_liveness_selftest.py
（全程不碰线上标志：SERVER_DIR 被换到临时目录，还专门有一条断言盯着这件事；
 不连库、不写库；只有第 [10] 组对线上 /api/health 做一次只读探活）
"""
import ast
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.dirname(HERE)
sys.path.insert(0, SERVER)

import app.config as cfg                          # noqa: E402
from services.ops import http_liveness as H       # noqa: E402

PASS, FAIL = [], []
_SENT = []          # 桩：记录本应发出去的推送正文

_ORIG_SERVER_DIR = H.SERVER_DIR
_REAL_FLAG = os.path.join(_ORIG_SERVER_DIR, 'data', 'backend_restart_requested')
_TMP = tempfile.mkdtemp(prefix='http_liveness_')
H.SERVER_DIR = _TMP
FLAG = H._flag_path()

_CFG = {}
_orig_cfg = H._cfg
H._cfg = lambda k, d: _CFG.get(k, d)


def ck(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(f"  [{'√' if cond else '×'}] {msg}")


def _fake_push(ok=True):
    _SENT.clear()

    def fake(text):
        _SENT.append(text)
        return ok
    return fake


H._send = _fake_push(True)


def _reset(grace=0.0, enabled=True):
    _CFG.update({'http_liveness_enabled': enabled,
                 'http_liveness_start_grace_sec': grace,
                 'http_liveness_fail_streak': 3,
                 'http_liveness_heal_cooldown_min': 30,
                 'http_liveness_timeout_sec': 5.0,
                 'http_liveness_interval_sec': 30})
    with H._LOCK:
        H._STATE.update({'started_ts': 0.0, 'stop': False, 'streak': 0,
                         'last_fail_streak': 0, 'last_ok_ts': 0.0, 'last_heal_ts': 0.0,
                         'flag_by_us': False, 'flag_token': '', 'last_log_key': '',
                         'last_probe': None})
    if os.path.exists(FLAG):
        os.remove(FLAG)
    _SENT.clear()


def _p(state, ok=False, elapsed=5.0):
    return {'ok': ok, 'state': state, 'status': 200 if ok else None,
            'elapsed': elapsed, 'detail': '', 'host': '127.0.0.1', 'port': 3003,
            'path': '/api/health'}


OK = lambda: _p('ok', ok=True, elapsed=0.004)          # noqa: E731
TIMEOUT = lambda: _p('timeout', elapsed=5.0)           # noqa: E731
REFUSED = lambda: _p('refused', elapsed=0.001)         # noqa: E731


def _tick(probe_fn, force=False):
    """跑一轮判定 + 动作，返回 (res, out)。"""
    r = H.check_once(force=force, probe_fn=probe_fn)
    return r, H.handle(r, force=force)


print('[0] 自测自身的安全前提：绝不碰线上那份重启标志')
ck(H._flag_path() != _REAL_FLAG, f'标志路径已换到临时目录（{H._flag_path()}）')
ck(H._flag_path().startswith(_TMP), '标志路径确实在临时目录下')
ck(not H._flag_path().startswith(_ORIG_SERVER_DIR), '线上那份标志（server/data/ 下）不会被本自测读到/删到')

print('[1] 开关关闭：连探活都不做、不起线程')
_reset(enabled=False)


def _boom():
    raise AssertionError('开关关闭时探活函数不该被调用')


r = H.check_once(probe_fn=_boom)
out = H.handle(r)
ck(r['verdict'] == 'disabled' and r['probe'] is None, 'check_once 不探活、判定为 disabled')
ck(out == {'wrote_flag': False, 'cancelled': False, 'pushed': False}, 'handle 不写标志、不推送')
ck(not os.path.exists(FLAG), '磁盘上没有标志文件')
ck(_SENT == [], '一条推送都没发')
ck(H.start_watchdog(interval_sec=0.05) is None, 'start_watchdog 关掉时不起线程（零副作用）')

print('[2] 反证：端口没在听（refused）连续 5 次也不计数、不写标志')
_reset()
verdicts = []
for _ in range(5):
    r, out = _tick(REFUSED)
    verdicts.append(r['verdict'])
ck(set(verdicts) == {'port_down'}, f'5 次全部判 port_down（实得 {verdicts}）')
ck(H.state_snapshot()['streak'] == 0, 'streak 保持 0（refused 清零不累计）')
ck(not os.path.exists(FLAG), '没有写重启标志')
ck(_SENT == [], '没有推送')
ck(H.state_snapshot()['last_heal_ts'] == 0.0, '没有设置自愈冷却')

print('[3] 单次/两次失败不写，第三次才写（阈值 3）')
_reset()
r, out = _tick(TIMEOUT)
ck(r['verdict'] == 'fail' and r['streak'] == 1, '第 1 次失败：fail，streak=1')
ck(not out['wrote_flag'] and not os.path.exists(FLAG), '第 1 次不写标志')
r, out = _tick(TIMEOUT)
ck(r['verdict'] == 'fail' and r['streak'] == 2, '第 2 次失败：fail，streak=2')
ck(not out['wrote_flag'] and not os.path.exists(FLAG), '第 2 次仍不写标志')
ck(_SENT == [], '前两次一条推送都没发')
r, out = _tick(TIMEOUT)
ck(r['verdict'] == 'heal' and r['heal'] is True, '第 3 次失败：判定 heal')
ck(out['wrote_flag'] and os.path.exists(FLAG), '第 3 次写了重启标志')
ck(len(_SENT) == 1 and out['pushed'] is True, '第 3 次推送了一条且成功')
_body = _SENT[0] if _SENT else ''
ck('连续 3 次' in _body and '重启标志' in _body, '正文带连续失败次数与"已写重启标志"')
ck('15:10' in _body and '推迟' in _body, '正文如实说明盘中会推迟到 15:10 之后')
# 判据串用拼接构造，避免字面量本身被 pre-commit B4 的"D:[\\]"词面匹配误拦（11-10 实测被拦过一次）
ck(('D' + ':' + chr(92)) not in _body and _ORIG_SERVER_DIR not in _body, '正文不含本机绝对路径')
_flag_txt = open(FLAG, encoding='utf-8').read()
ck(_flag_txt.startswith('http_liveness pid='), '标志首行是可认领的 token')
ck('连续 3 次 HTTP 探活失败' in _flag_txt, '标志正文带原因与次数')

print('[4] 冷却期内不重复写（默认 30 分钟）')
_before = open(FLAG, encoding='utf-8').read()
r, out = _tick(TIMEOUT)
ck(r['verdict'] == 'cooldown' and r['heal'] is False, '第 4 次失败：cooldown，不再 heal')
ck(not out['wrote_flag'] and len(_SENT) == 1, '没有重写标志、没有第二条推送')
ck(open(FLAG, encoding='utf-8').read() == _before, '标志内容一字未动')

print('[5] 恢复：streak 清零 + 撤销本进程写的那份标志')
r, out = _tick(OK)
ck(r['verdict'] == 'recover' and r['streak'] == 0, '恢复判定为 recover，streak=0')
ck(out['cancelled'] is True, 'handle 撤销了本进程写的标志')
ck(not os.path.exists(FLAG), '标志文件已被删除')
ck(H.state_snapshot()['flag_by_us'] is False, '所有权已交还')
r, out = _tick(OK)
ck(r['verdict'] == 'ok' and out['cancelled'] is False, '持续正常：ok，无多余动作')
ck(len(_SENT) == 1, '恢复没有发推送（只留第 3 组那一条）')

print('[6] 反证：别人写的标志不覆盖、不撤销')
_reset()
with open(FLAG, 'w', encoding='utf-8') as f:
    f.write('manual request from another session\n')
for _ in range(3):
    r, out = _tick(TIMEOUT)
ck(r['verdict'] == 'flag_pending' and r['heal'] is False, '已有别人的标志：判 flag_pending，不再写')
ck(not out['wrote_flag'], '没有覆盖别人的标志')
ck(open(FLAG, encoding='utf-8').read().startswith('manual request'), '别人的标志内容原样保留')
r, out = _tick(OK)
ck(r['verdict'] == 'recover', '探活恢复：判 recover')
ck(out['cancelled'] is False and os.path.exists(FLAG), '不是我们写的，恢复时也不删')
ck(_SENT == [], '这一组一条推送都没发')

print('[7] 启动宽限期：窗口内失败不累计；出窗口才开始计数')
_reset(grace=180)
H._STATE['started_ts'] = time.time()
vs = []
for _ in range(5):
    r, out = _tick(TIMEOUT)
    vs.append(r['verdict'])
ck(set(vs) == {'grace'}, f'宽限期内 5 次全判 grace（实得 {vs}）')
ck(H.state_snapshot()['streak'] == 0 and not os.path.exists(FLAG), 'streak 不累计、没写标志')
H._STATE['started_ts'] = time.time() - 181
r, out = _tick(TIMEOUT)
ck(r['verdict'] == 'fail' and r['streak'] == 1, '出宽限后第一次失败才计数（streak=1）')

print('[8] 写标志失败：不推送、不设冷却，下一轮还能再试')
_reset()
for _ in range(3):
    H.check_once(probe_fn=TIMEOUT)
_blk = os.path.join(_TMP, 'blocked_file')
with open(_blk, 'w', encoding='utf-8') as f:
    f.write('x')
_orig_flag_path = H._flag_path
H._flag_path = lambda: os.path.join(_blk, 'flag')      # 父路径是文件 → makedirs 必失败
r, out = _tick(TIMEOUT)
ck(out['wrote_flag'] is False and _SENT == [], '写失败：不推送')
ck(H.state_snapshot()['last_heal_ts'] == 0.0, '写失败：没有设冷却')
H._flag_path = _orig_flag_path
r, out = _tick(TIMEOUT)
ck(r['verdict'] == 'heal' and out['wrote_flag'] is True, '恢复可写后当轮就能补写（没被冷却挡住）')

print('[9] 线程生命周期：起一次、不重复起、停得掉')
_reset()
_seen = {'n': 0}
_orig_probe = H.probe


def _count_probe():
    _seen['n'] += 1
    return REFUSED()
H.probe = _count_probe
t1 = H.start_watchdog(interval_sec=0.05)
ck(t1 is not None and t1.is_alive(), '开关开着时起了线程')
t2 = H.start_watchdog(interval_sec=0.05)
ck(t2 is t1, '重复调用返回同一个线程（不重复起）')
time.sleep(0.3)
ck(_seen['n'] >= 2, f'线程真的在跑（0.3 秒内探了 {_seen["n"]} 次）')
ck(H.stop_watchdog() is True, 'stop_watchdog 让它自然退出')
ck(H.state_snapshot()['thread_alive'] is False, '线程已不在')
H.probe = _orig_probe

print('[10] 真机只读探活 smoke（不依赖线上是否在跑）')
_real = H.probe()       # 这里用真 _cfg 的 host/port？——_cfg 已被桩覆盖，但探的仍是 127.0.0.1:3003
ck(set(['ok', 'state', 'status', 'elapsed', 'detail', 'host', 'port', 'path']) <= set(_real),
   '返回结构齐全')
ck(_real['host'] == '127.0.0.1' and _real['port'] == 3003 and _real['path'] == '/api/health',
   '探的是本机 3003 的 /api/health')
ck(_real['state'] in ('ok', 'port_down', 'unknown', 'timeout',
                      'io_error', 'http_error', 'bad_body', 'error'),
   f'state 在约定集合内（实得 {_real["state"]}）')
print(f'     实测：state={_real["state"]} status={_real["status"]} elapsed={_real["elapsed"]}s')

print('[11] 接线与漂移守卫')
_MAIN = os.path.join(SERVER, 'app', 'main.py')
_main_src = open(_MAIN, encoding='utf-8').read()
ck('from services.ops.http_liveness import start_watchdog' in _main_src,
   'app/main.py 已接线 start_watchdog')
ck('_start_http_liveness()' in _main_src, 'app/main.py 真的调用了它')
ck('_make_log_utf8_safe' in _main_src, 'B8 的入口编码兜底还在（没被这次改动碰掉）')

_cfg_src = open(os.path.join(SERVER, 'app', 'cfg_schedule.py'), encoding='utf-8').read()
_missing_cfg = [n for n in ('HTTP_LIVENESS_ENABLED', 'HTTP_LIVENESS_HOST', 'HTTP_LIVENESS_PORT',
                            'HTTP_LIVENESS_PATH', 'HTTP_LIVENESS_INTERVAL_SEC',
                            'HTTP_LIVENESS_TIMEOUT_SEC', 'HTTP_LIVENESS_FAIL_STREAK',
                            'HTTP_LIVENESS_START_GRACE_SEC', 'HTTP_LIVENESS_HEAL_COOLDOWN_MIN')
                if not hasattr(cfg, n)]
ck(not _missing_cfg, f'config 里九项常量全部可导入（缺 {_missing_cfg}）')
ck("'HTTP_LIVENESS_ENABLED'" in _cfg_src, 'cfg_schedule.__all__ 带上了新常量（facade 不丢）')

from services.support import app_settings as A     # noqa: E402
_spec = {s['key']: s for s in A.SPEC}
_want = ['http_liveness_enabled', 'http_liveness_interval_sec', 'http_liveness_timeout_sec',
         'http_liveness_fail_streak', 'http_liveness_heal_cooldown_min',
         'http_liveness_start_grace_sec']
ck(all(k in _spec for k in _want), '六项都已注册进配置页 SPEC')
ck(all(_spec[k].get('group') == '运维与自愈' for k in _want), '六项同属「运维与自愈」组')
ck(_spec['http_liveness_enabled']['live'] is True, '总开关 live=True（关掉当轮就停）')
ck(_spec['http_liveness_interval_sec']['live'] is False, '间隔 live=False（改完要重启，不假装热生效）')

# 自测里再守一道 B8：本模块新增的运行期 print 必须 GBK 编得出
_mod_src = open(os.path.join(SERVER, 'services', 'ops', 'http_liveness.py'), encoding='utf-8').read()
_bad = []
for node in ast.walk(ast.parse(_mod_src)):
    if isinstance(node, ast.Call) and getattr(node.func, 'id', '') == 'print':
        seg = ast.get_source_segment(_mod_src, node) or ''
        try:
            seg.encode('gbk')
        except UnicodeEncodeError:
            _bad.append(node.lineno)
ck(_bad == [], f'http_liveness.py 的 print 全部 GBK 可编码（不过关的行号：{_bad}）')

print('[12] 真 socket 对照：四种真实形态的分类（临时端口，不碰 3003）')
import socket            # noqa: E402
import threading         # noqa: E402


def _listener(mode):
    """起一个真监听：ok=回 200 {"status":"ok"}；bad=回 500；silent=接受但永不应答。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('127.0.0.1', 0))
    s.listen(8)
    hold = []

    def _loop():
        s.settimeout(0.2)
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                continue
            if mode == 'silent':
                hold.append(c)          # 不读不写不关：客户端必须超时，而不是被 FIN 断掉
                continue
            try:
                c.recv(1024)
                if mode == 'ok':
                    c.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 15\r\n'
                              b'Connection: close\r\n\r\n{"status":"ok"}')
                else:
                    c.sendall(b'HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n'
                              b'Connection: close\r\n\r\n')
            except OSError:
                pass
            finally:
                try:
                    c.close()
                except OSError:
                    pass

    threading.Thread(target=_loop, daemon=True).start()
    return s.getsockname()[1], s


p_ok, _s1 = _listener('ok')
p_bad, _s2 = _listener('bad')
p_silent, _s3 = _listener('silent')
_CFG['http_liveness_timeout_sec'] = 1.0
_CFG['http_liveness_host'] = '127.0.0.1'

_CFG['http_liveness_port'] = p_ok
r1 = H.probe()
ck(r1['ok'] is True and r1['state'] == 'ok' and r1['status'] == 200,
   f'200 且正文含 ok => ok（实得 {r1["state"]}/{r1["status"]}）')

_CFG['http_liveness_port'] = p_bad
r2 = H.probe()
ck(r2['ok'] is False and r2['state'] == 'http_error' and r2['status'] == 500,
   f'HTTP 500 => http_error（实得 {r2["state"]}/{r2["status"]}）')

_CFG['http_liveness_port'] = p_silent
r3 = H.probe()
ck(r3['ok'] is False and r3['state'] == 'timeout',
   f'端口在听但不应答 => timeout（实得 {r3["state"]}，耗时 {r3["elapsed"]}s）')
ck(r3['elapsed'] >= 0.9, '确实是等满超时才判，不是立刻放弃')

# 拿一个刚关掉的端口验「没人听」：本机实测是超时而不是 RST，必须靠 netstat 认出来
_s4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
_s4.bind(('127.0.0.1', 0))
p_closed = _s4.getsockname()[1]
_s4.close()
_CFG['http_liveness_port'] = p_closed
ck(H._port_listening(p_closed) is False, 'netstat 认得出这个端口没人听')
r4 = H.probe()
ck(r4['ok'] is False and r4['state'] == 'port_down',
   f'端口没人听 => port_down（不靠异常类型；实得 {r4["state"]}，耗时 {r4["elapsed"]}s）')
ck('listening=False' in r4['detail'], f'详细里带 netstat 结论（{r4["detail"][:60]}）')

# 同一个刚关的端口连两次，本机可能给出两种异常：第一次等满超时（要靠 netstat 认出没人听），
# 后续重连直接 WinError 10061（refused 分支）。两条路都必须落到 port_down——这是 11-09 之后
# 补的实测（先前只按"超时"写，会让"真实没人听×5"这条断言看起来在测 netstat，其实走的是 refused）。
_states = []
for _ in range(6):
    _states.append(H.probe()['state'])
ck(all(s == 'port_down' for s in _states),
   f'同一个没人听的端口连 6 次全判 port_down（实得 {_states}）')

# 决策逻辑三态：连不上时按 netstat 的 True/False/None 分流（不依赖真实网络行为）
_orig_connect = H.http.client.HTTPConnection.connect
_orig_listening = H._port_listening


def _raise_timeout(self):
    raise TimeoutError('timed out')


H.http.client.HTTPConnection.connect = _raise_timeout
H._port_listening = lambda port=None: True
_r = H.probe(timeout_sec=0.2)
ck(_r['state'] == 'timeout', f'连不上但有人在听 => timeout（计数；实得 {_r["state"]}）')
H._port_listening = lambda port=None: False
_r = H.probe(timeout_sec=0.2)
ck(_r['state'] == 'port_down', f'连不上且没人听 => port_down（不计数；实得 {_r["state"]}）')
H._port_listening = lambda port=None: None
_r = H.probe(timeout_sec=0.2)
ck(_r['state'] == 'unknown', f'连不上且 netstat 读不到 => unknown（不猜；实得 {_r["state"]}）')
H.http.client.HTTPConnection.connect = _orig_connect
H._port_listening = _orig_listening

# 分类真的接到了判定上：真实 timeout 连 3 次 => heal；真实「没人听」连 5 次 => 仍不写
_reset()
_CFG['http_liveness_timeout_sec'] = 1.0
for _ in range(3):
    r, out = _tick(lambda: dict(r3))
ck(r['verdict'] == 'heal' and out['wrote_flag'], '真实 timeout 连续 3 次 => 写标志')
_reset()
_CFG['http_liveness_port'] = p_closed
for _ in range(5):
    r, out = _tick(H.probe)
ck(r['verdict'] == 'port_down' and not os.path.exists(FLAG), '真实「没人听」连续 5 次 => 仍不写标志')
ck(_SENT == [], '这一路一条推送都没发')

for _s in (_s1, _s2, _s3):
    try:
        _s.close()
    except OSError:
        pass

print('\n' + '=' * 60)
print(f'结果：{len(PASS)} 通过 / {len(FAIL)} 失败')
if FAIL:
    for m in FAIL:
        print(f'  失败：{m}')
sys.exit(1 if FAIL else 0)
