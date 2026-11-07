# -*- coding: utf-8 -*-
"""sched_liveness_selftest.py — 修⑤「调度链失活告警」的反证与正证（2026-11-07）

守的不变量：**"今天怎么没推票"不能再靠人来问。** 两条判据都得测到要害，
而这份自测的重点不是"该告警时告了"，而是**不该告警时别响**——
第一次响就把人训练成不看告警，比没有告警更糟。

三组关键反证（都是拿真库历史校准出来的，见模块文件头）：
  · 11-06 形态（情绪快照 53 行 + 决策链四张表全 0）⇒ 必须告警；
  · 2026-09-21 形态（六张表全 0，功能还没上线）⇒ 必须**不**告警；
  · 心跳正常但决策链 0 行（循环在转、今天真的没票达标）⇒ B 只认"数据链活着"那一侧。

跑法：cd server && python scripts/sched_liveness_selftest.py
（写库的用例全部走 `dbmod.use_db_path(临时库)`；第 [9] 组真库只读，不发通知）
"""
import ast
import datetime as dt
import io
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.dirname(HERE)
sys.path.insert(0, SERVER)

import app.config as cfg                       # noqa: E402
import db.database as dbmod                    # noqa: E402
from services.ops import sched_liveness as L   # noqa: E402

PASS, FAIL = [], []
_SENT = []          # 桩：记录本应发出去的告警正文


def ck(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(f"  [{'✓' if cond else '✗'}] {msg}")


def _mk_db(tag):
    p = os.path.join(tempfile.gettempdir(), f'sched_live_{tag}_{os.getpid()}.db')
    if os.path.exists(p):
        os.remove(p)
    c = sqlite3.connect(p)
    c.execute('CREATE TABLE market_sentiment_snapshots (sent_date TEXT, ts TEXT, payload TEXT)')
    c.execute('CREATE TABLE daily_recommendations (rec_date TEXT, symbol TEXT)')
    c.execute('CREATE TABLE strategy_snapshot_batch (snap_date TEXT, batch TEXT)')
    c.execute('CREATE TABLE strategy_snapshot (snap_date TEXT, symbol TEXT)')
    c.execute('CREATE TABLE daily_candidate_pool (pool_date TEXT, symbol TEXT)')
    c.commit()
    return p, c


def _ins(c, table, col, day, n):
    for i in range(n):
        c.execute(f'INSERT INTO {table} ({col}) VALUES (?)', (f'{day} 09:4{i%5}:00',))
    c.commit()


def _stub_push(ok=True):
    """把发送口换成桩：ok=False 用来验"发送失败不写去重标记"。"""
    import services.support.push as P
    orig = P.push_wecom_webhook
    _SENT.clear()

    def fake(content):
        _SENT.append(content)
        return ok
    P.push_wecom_webhook = fake
    return orig


def _reset_state():
    L._STATE['last_alert'] = {'A': 0.0, 'B': '', 'E': ''}
    L._STATE['last_skip_reason'] = ''
    L._STATE['last_chain_error'] = ''
    L._STATE['started_ts'] = time.time()
    L._BEAT.update({'ts': time.time(), 'since': dt.datetime.now(), 'last_ran': 1, 'loop_errors': 0})


# ========== [1] A 心跳陈旧 ==========
print('[1] A 类：主循环不转了必须告警')
_ORIGIN_DB = dbmod.current_db_path()
_now = dt.datetime(2026, 11, 9, 10, 1)        # 周一，交易日，在窗口内
_reset_state()
r = L.check_once(now=_now, force=True)
ck(r['alerts'] == [], f"心跳新鲜 → 不该有告警（实得 {r['alerts']}）")
L._BEAT['ts'] = time.time() - 20 * 60
L._BEAT['since'] = dt.datetime.now() - dt.timedelta(minutes=20)
r = L.check_once(now=_now, force=True)
ck(any(a['kind'] == 'A' for a in r['alerts']), f'心跳 20 分钟没更新（阈值 15）→ A 告警（实得 {len(r["alerts"])} 条）')
L._BEAT['ts'] = time.time() - 3 * 60
r = L.check_once(now=_now, force=True)
ck(not any(a['kind'] == 'A' for a in r['alerts']), '心跳 3 分钟 → 不告警（阈值不是 0，别把正常间隙报出去）')

print('[2] A 类边界：从没记过心跳')
L._BEAT['ts'] = 0.0
L._BEAT['since'] = None
L._STATE['started_ts'] = time.time() - 60 * 60          # 进程已起来 1 小时
r = L.check_once(now=_now, force=True)
ck(any(a['kind'] == 'A' for a in r['alerts']), '超宽限还没有心跳 ⇒ 必须告警（不能因为"没数据"就当正常）')
L._STATE['started_ts'] = time.time() - 60                 # 刚起来 1 分钟
r = L.check_once(now=_now, force=True)
ck(r['alerts'] == [] and '宽限' in r.get('skipped', ''),
   f'启动宽限期内不告警（实得 skipped={r.get("skipped")}）—— 否则每次重启都先报一条假警')

# ========== [3] B 决策链空档：真库历史校准出来的三种形态 ==========
print('[3] B 类：11-06 形态告警、9 月形态不告警、正常日不告警')
tmp1106, c1 = _mk_db('1106')
_ins(c1, 'market_sentiment_snapshots', 'sent_date', '2026-11-06', 53)
dbmod.use_db_path(tmp1106)
try:
    r = L.check_once(now=dt.datetime(2026, 11, 6, 14, 11), force=True)
    b = [a for a in r['alerts'] if a['kind'] == 'B']
    ck(len(b) == 1, f'情绪快照 53 行 + 决策链四张表全 0 ⇒ 一条 B 告警（实得 {len(b)}）')
    ck(b and '情绪快照 53' in b[0]['text'], '告警正文带上"活着"的证据行数')
    ck(b and '14:10' in b[0]['why'], '正文点名是哪一档（10:00/14:10），便于复盘谁该跑没跑')
finally:
    c1.close(); dbmod.use_db_path(_ORIGIN_DB); os.remove(tmp1106)

tmp0921, c2 = _mk_db('0921')          # 六张表全 0（功能还没上线的那四天）
dbmod.use_db_path(tmp0921)
try:
    r = L.check_once(now=dt.datetime(2026, 9, 21, 14, 11), force=True)
    ck(r['alerts'] == [], f'整天六张表全 0 ⇒ **不告警**（实得 {r["alerts"]}）——第一版就死在这儿')
    ck('不告警' in r.get('chain_note', '') or '不告警' in r.get('chain_note', ''),
       f'但要在结果里留一句解释（实得 chain_note={r.get("chain_note","")[:40]}）')
    # 反证：把"数据链活着"这道必要条件人为抬高（等于去掉它），这天就会假告警
    _orig_cnt = L._count_chain
    L._count_chain = lambda day: {'alive': 1, 'decision': {k: 0 for k, _ in L._DECISION_TABLES},
                                  'errors': ''}
    try:
        r2 = L.check_once(now=dt.datetime(2026, 9, 21, 14, 11), force=True)
        ck(len([a for a in r2['alerts'] if a['kind'] == 'B']) == 1,
           '反证：只要"活着"的证据非零，同一批 0 行就从"不告警"翻成"告警" ⇒ 必要条件真的在管事')
    finally:
        L._count_chain = _orig_cnt
finally:
    c2.close(); dbmod.use_db_path(_ORIGIN_DB); os.remove(tmp0921)

tmpok, c3 = _mk_db('normal')
_ins(c3, 'market_sentiment_snapshots', 'sent_date', '2026-11-05', 48)
_ins(c3, 'daily_recommendations', 'rec_date', '2026-11-05', 3)
dbmod.use_db_path(tmpok)
try:
    r = L.check_once(now=dt.datetime(2026, 11, 5, 14, 11), force=True)
    ck(r['alerts'] == [], f'正常日不告警（实得 {r["alerts"]}）')
finally:
    c3.close(); dbmod.use_db_path(_ORIGIN_DB); os.remove(tmpok)

tmpempty, c4 = _mk_db('empty')        # 表存在但一条没有（全新库）
dbmod.use_db_path(tmpempty)
try:
    r = L.check_once(now=dt.datetime(2026, 11, 9, 14, 11), force=True)
    ck(r['alerts'] == [], '全新库（全 0）⇒ 不告警，与 9 月同处理')
finally:
    c4.close(); dbmod.use_db_path(_ORIGIN_DB); os.remove(tmpempty)

# ========== [4] 闸门：非交易日与观测窗口 ==========
print('[4] 闸门：非交易日/窗口外一律不判（深夜与周末不该有告警）')
tmpg, c5 = _mk_db('gate')
_ins(c5, 'market_sentiment_snapshots', 'sent_date', '2026-11-07', 20)
dbmod.use_db_path(tmpg)
try:
    r = L.check_once(now=dt.datetime(2026, 11, 7, 10, 30), force=True)      # 周六
    ck('非交易日' in r.get('skipped', ''), f'周六跳过（实得 {r.get("skipped")}）')
    _reset_state()
    r = L.check_once(now=dt.datetime(2026, 11, 9, 21, 30), force=True)      # 周一但窗口外
    ck('不在观测窗口' in r.get('skipped', ''), f'21:30 跳过（实得 {r.get("skipped")}）')
    _reset_state()
    L._BEAT['ts'] = time.time() - 60 * 60
    r = L.check_once(now=dt.datetime(2026, 11, 9, 21, 30), force=True)
    ck(r['alerts'] == [], '窗口外即使心跳停了一小时也不发（夜间停轮是常态，不是事故）')
finally:
    c5.close(); dbmod.use_db_path(_ORIGIN_DB); os.remove(tmpg)

# ========== [5] 开关关掉 = 零副作用 ==========
print('[5] sched_liveness_enabled=False 必须连库都不查、线程都不起')
_orig_cfg = L._cfg
L._cfg = lambda k, d: (False if k == 'sched_liveness_enabled' else _orig_cfg(k, d))
_orig_count = L._count_chain
L._count_chain = lambda day: (_ for _ in ()).throw(AssertionError('关掉后不该查库'))
try:
    r = L.check_once(now=dt.datetime(2026, 11, 9, 14, 11), force=True)
    ck(r['enabled'] is False and r['alerts'] == [], 'check_once 直接短路返回')
    ck('开关' in r.get('skipped', ''), f'并说明为什么（实得 {r.get("skipped")}）')
    t = L.start_watchdog()
    ck(t is None, 'start_watchdog 关掉时不起线程（零副作用）')
finally:
    L._cfg = _orig_cfg
    L._count_chain = _orig_count

# ========== [6] 去重与"发送失败不写标记" ==========
print('[6] 去重：同因冷却；发送失败必须还能重试')
tmpd, c6 = _mk_db('dedupe')
_ins(c6, 'market_sentiment_snapshots', 'sent_date', '2026-11-09', 30)
dbmod.use_db_path(tmpd)
orig_push = _stub_push(ok=False)
try:
    _reset_state()
    now = dt.datetime(2026, 11, 9, 14, 11)
    r = L.check_once(now=now)
    out = L.alert_and_record(r)
    ck(out['sent'] and out['sent'][0]['pushed'] is False, '发送失败如实记 False')
    r2 = L.check_once(now=now)
    ck(len(r2['alerts']) == len(r['alerts']), '失败不写去重标记 ⇒ 下一轮还会再判（否则这条告警当天永久丢）')
    P = _stub_push(ok=True)
    L.alert_and_record(L.check_once(now=now))
    r3 = L.check_once(now=now)
    ck(all(a['kind'] != 'B' for a in r3['alerts']), '发送成功后同一档当天不再重发')
finally:
    import services.support.push as _P
    _P.push_wecom_webhook = orig_push
    c6.close(); dbmod.use_db_path(_ORIGIN_DB); os.remove(tmpd)

# ========== [7] 观察者线程不能被一个坏 tick 打死 ==========
print('[7] 观察线程：check 与 sleep 都在 try 内；停止用标志，不往线程里抛异常')
_ticks = {'n': 0}
_orig_check, _orig_sleep = L.check_once, time.sleep
_orig_alert = L.alert_and_record


def _boom(*a, **k):
    _ticks['n'] += 1
    raise RuntimeError('故意的：模拟一次读库炸裂')


L.check_once = _boom
L.alert_and_record = lambda r: None
time.sleep = lambda sec: _orig_sleep(0.05)
try:
    _STATE0 = dict(L._STATE)
    L._STATE['thread'] = None
    L._STATE['stop'] = False
    th = L.start_watchdog(interval_sec=0.05)
    ck(th is not None, '开关开着时 start_watchdog 起了线程')
    th2 = L.start_watchdog(interval_sec=0.05)
    ck(th2 is th, '重复调用不叠加线程（重启路径上起两个哨兵=双倍告警，这条必须钉住）')
    _orig_sleep(0.6)
    ck(_ticks['n'] >= 3, f'连续抛错仍跑了 {_ticks["n"]} 个 tick（一个坏 tick 打不死观察者）')
    ck(th.is_alive(), '此时线程还活着')
    ck(L.stop_watchdog() is True, 'stop_watchdog() 让它自然退出（返回 True=已退出）')
    ck(not th.is_alive(), '线程确实停了，不是靠往它里面抛异常')
    # 反证：把 try 拆掉的话，一次异常就会带走哨兵——用同样注入验证"哨兵不会死于事故"
    L._STATE['thread'] = None
    L._STATE['stop'] = False
    th3 = L.start_watchdog(interval_sec=0.05)
    _orig_sleep(0.3)
    ck(th3.is_alive() and _ticks['n'] >= 6, f'再启一次仍活（累计 tick {_ticks["n"]}）')
    L.stop_watchdog()
finally:
    L.check_once = _orig_check
    L.alert_and_record = _orig_alert
    time.sleep = _orig_sleep
    L._STATE['stop'] = False

# ========== [8] 告警正文的硬性内容 ==========
print('[8] 正文：数字来自判定、处置可执行、且不含 GBK 编不出的符号')
_b = {'ts': time.time() - 20 * 60, 'since': dt.datetime(2026, 11, 9, 13, 50),
      'age_min': 20.0, 'last_ran': 0, 'loop_errors': 0, 'started': 0.0}
txt = L._render({'kind': 'B', 'why': '到 14:10 当天决策链表全 0 行，但情绪快照已有 53 行',
                 'rows': {'alive': 53, 'decision': {'daily_recommendations': 0,
                                                    'strategy_snapshot_batch': 0,
                                                    'strategy_snapshot': 0,
                                                    'daily_candidate_pool': 0}}},
                _b, dt.datetime(2026, 11, 9, 14, 11), True)
ck('53' in txt and 'backend_restart_requested' in txt, '正文带行数与"下一步做什么"')
ck('09:45' in txt, '点名会错过哪几个批次（11-06 实拍的那几个时点）')
try:
    txt.encode('gbk')
    ck(True, '告警正文 GBK 可编码（新日志规约：不往运行期输出里放 ⚠/✓）')
except UnicodeEncodeError as e:
    ck(False, f'正文含 GBK 编不出的字符：{e}')

# ========== [9] 接线层 AST ==========
print('[9] 接线层：心跳与观察线程真的挂上了，不是写了个没人调的模块')
sch = io.open(os.path.join(SERVER, 'core', 'scheduler.py'), encoding='utf-8').read()
t = ast.parse(sch)
funcs = {n.name: ast.dump(n) for n in ast.walk(t) if isinstance(n, ast.FunctionDef)}
ck('mark_loop_beat' in funcs.get('_run_pending_guarded', ''),
   '_run_pending_guarded 真的记心跳（摘掉这一行自测就该报红）')
ck('start_watchdog' in funcs.get('run_scheduler', ''),
   'run_scheduler 在主循环之前起观察线程（放循环里等于让被观察者判自己的死）')
ck('sched_liveness_enabled' in io.open(os.path.join(SERVER, 'services/support/app_settings.py'),
                                       encoding='utf-8').read(),
   '配置页注册了总开关（可热改，不必重启就能停）')
cfgr = io.open(os.path.join(SERVER, 'app/cfg_schedule.py'), encoding='utf-8').read()
for k in ('SCHED_LIVENESS_ENABLED', 'SCHED_HEARTBEAT_STALE_MIN', 'SCHED_CHAIN_CHECK_TIMES',
          'SCHED_LIVENESS_WINDOW', 'SCHED_LIVENESS_COOLDOWN_MIN', 'SCHED_LIVENESS_START_GRACE_MIN'):
    ck(k in cfgr, f'配置项 {k} 在位')

# ========== [10] 真库只读 smoke：两条判据在真历史上的表现 ==========
print('[10] 真库只读：11-06 与 11-05 两天各判出什么')
real = os.path.join(SERVER, 'gupiao.db')
try:
    conn = sqlite3.connect(f'file:{real}?mode=ro', uri=True)
    for day, hh, mm, expect in [('2026-11-06', 14, 11, 'B'), ('2026-11-05', 14, 11, None),
                                ('2026-09-21', 14, 11, None)]:
        alive = conn.execute('SELECT COUNT(*) FROM market_sentiment_snapshots WHERE sent_date LIKE ?',
                             (day + '%',)).fetchone()[0]
        dec = [conn.execute(f'SELECT COUNT(*) FROM {tb} WHERE {col} LIKE ?', (day + '%',)).fetchone()[0]
               for tb, col in L._DECISION_TABLES]
        all_zero = all(x == 0 for x in dec)
        got = 'B' if (alive > 0 and all_zero) else None
        ck(got == expect, f'{day}：情绪快照 {alive} 行、决策链 {dec} ⇒ 判据给 {got}（期望 {expect}）')
    conn.close()
except sqlite3.OperationalError as e:
    print('     真库读不了（跳过本组）：', e)

# ========== [11] 线上看到的两个小毛病（正文分型 + 不打刷屏） ==========
print('[11] 正文分型与"本轮不发"只打一次（11-07 合并后线上核验抓出来的）')
_b11 = {'ts': time.time() - 20 * 60, 'since': dt.datetime.now() - dt.timedelta(minutes=20),
        'age_min': 20.0, 'last_ran': 0, 'loop_errors': 0, 'started': 0.0}
_a_txt = L._render({'kind': 'A', 'why': '距最后一次调度心跳 20.0 分钟（阈值 15 分钟）'},
                   _b11, dt.datetime(2026, 11, 9, 14, 11), True)
_b_txt = L._render({'kind': 'B', 'why': '到 14:10 决策链全 0 行',
                    'rows': {'alive': 53, 'decision': {k: 0 for k, _ in L._DECISION_TABLES}}},
                   _b11, dt.datetime(2026, 11, 9, 14, 11), True)
ck('09:45/10:00/13:30/14:05 批次' in _b_txt, 'B 的正文保留"错过哪几个批次"（这才是 B 的含义）')
ck('09:45/10:00/13:30/14:05 批次' not in _a_txt, 'A 的正文**不**写"错过批次"——那是 B 的话（原来共用一段）')
ck('所有定时任务都不执行' in _a_txt, 'A 说的是"循环没跑完、期间所有任务都停"')
ck(_a_txt != _b_txt, '两类正文不同文（同文就等于有一类在误导人）')

import builtins
tmp11, c11 = _mk_db('quiet')
dbmod.use_db_path(tmp11)
_buf, _old_print = [], builtins.print


def _cap(*a, **k):
    line = ' '.join(str(x) for x in a)
    if '本轮不发' in line:          # 只截这一类，其它照原样输出（否则 ck 的回显也被吞）
        _buf.append(line)
    else:
        _old_print(line, **k)


try:
    L._STATE.pop('last_skip_reason', None)
    _reset_state()
    builtins.print = _cap
    try:
        for _ in range(3):           # 周六连查 3 轮：改造前会打 3 行
            L.alert_and_record(L.check_once(now=dt.datetime(2026, 11, 7, 10, 0)))
    finally:
        builtins.print = _old_print
    ck(len(_buf) == 1, f'同一原因连查 3 轮只打 1 行（实得 {len(_buf)} 行）——改造前每 60 秒一行 = 1440 行/天')

    _buf.clear()
    L._STATE['last_skip_reason'] = ''
    builtins.print = _cap
    try:
        L.alert_and_record(L.check_once(now=dt.datetime(2026, 11, 7, 10, 0)))    # 第一次：非交易日
        L.alert_and_record(L.check_once(now=dt.datetime(2026, 11, 9, 21, 0)))    # 换原因：窗口外
    finally:
        builtins.print = _old_print
    ck(len(_buf) == 2, f'原因变了就重新打一行（实得 {len(_buf)} 行：{[x[-16:] for x in _buf]}）')

    _buf.clear()
    L._STATE['last_skip_reason'] = '故意预置成别的原因'
    builtins.print = _cap
    try:
        L.alert_and_record(L.check_once(now=dt.datetime(2026, 11, 7, 10, 0)))
    finally:
        builtins.print = _old_print
    ck(len(_buf) == 1, '原因与上次不同 ⇒ 必须打（证明判据真在比对，不是一律沉默）')
finally:
    c11.close()
    dbmod.use_db_path(_ORIGIN_DB)
    os.remove(tmp11)


# ========== [12] E 类：判据自己查不动时必须出声（修⑩-5） ==========
print('\n[12] E 类：B 判据查库失败时，"看不见"必须被说出来（原来是静默）')
import builtins
import db.dal_state as _ds

tmp12, c12 = _mk_db('blind')
_ORIGIN_DB12 = dbmod.current_db_path()
dbmod.use_db_path(tmp12)
_reset_state()
L.mark_loop_beat(ran=3)
_EBUF, _EOLD = [], builtins.print
_orig_get = _ds.get_db_conn


def _ecap(*a, **k):
    line = ' '.join(str(x) for x in a)
    if '判据无法评估' in line:        # 只截这一类，其余照原样输出（不吞 ck 的回显）
        _EBUF.append(line)
    else:
        _EOLD(line, **k)


def _failing_conn(*a, **k):
    raise RuntimeError('模拟写锁：database is locked')


def _failing_conn2(*a, **k):
    raise OSError('模拟另一只脚：no such table app_settings')


_orig_push12 = _stub_push(ok=True)
try:
    _now12 = dt.datetime(2026, 11, 9, 14, 11)      # 周一交易日、14:10 那档已过
    _ds.get_db_conn = _failing_conn
    r_err = L.check_once(now=_now12)
    ck(bool(r_err.get('chain_errors')),
       f'查询异常被如实记进 chain_errors（实得 {r_err.get("chain_errors")!r}）')
    ck(r_err['alerts'] == [], '库查不动时 B 不误发（但也不能假装"一切正常"）')

    builtins.print = _ecap
    try:
        L.alert_and_record(r_err, force=False)
    finally:
        builtins.print = _EOLD
    ck(len(_EBUF) == 1, f'这一轮有一行出声（实得 {len(_EBUF)} 行）——改造前一行都没有')
    ck(len(_SENT) == 1, f'异常首次出现 ⇒ 推送一次（实得 {len(_SENT)} 条）')
    ck('看不见' in _SENT[0] and 'database is locked' in _SENT[0],
       '正文说清"不是没事，是我看不见"，并带上异常原文')
    try:
        _SENT[0].encode('gbk')
        ck(True, 'E 类正文 GBK 编得出（B8 那条规矩）')
    except UnicodeEncodeError as e:
        ck(False, f'E 类正文编不进 GBK：{e}')

    _EBUF.clear()
    _n0 = len(_SENT)
    builtins.print = _ecap
    try:
        for _ in range(2):
            L.alert_and_record(L.check_once(now=_now12), force=False)
    finally:
        builtins.print = _EOLD
    ck(len(_SENT) == _n0, f'同一异常当天不重推（再多查两轮仍 {len(_SENT)} 条）')
    ck(len(_EBUF) == 0, f'同一异常文本不重复打日志（实得 {len(_EBUF)} 行）')

    _ds.get_db_conn = _failing_conn2
    _EBUF.clear()
    builtins.print = _ecap
    try:
        L.alert_and_record(L.check_once(now=_now12), force=False)
    finally:
        builtins.print = _EOLD
    ck(len(_SENT) == _n0, f'异常文本变了但仍是同一天 ⇒ 不重推（按天去重，实得 {len(_SENT)} 条）')
    ck(len(_EBUF) == 1, f'换了新异常必须再打一行日志（实得 {len(_EBUF)} 行）')
    ck('no such table' in (_EBUF[0] if _EBUF else ''), '日志里带的是新的异常原文，不是旧的那条')

    # 取连接失败也必须进 errors（原来 `conn = get_db_conn()` 写在 try 外面，
    # "库打不开"这一类会直接从 _count_chain 逃出去，B 一条不判、还只剩一行线程异常）
    _ds.get_db_conn = _failing_conn2
    r_conn = L._count_chain(_now12.strftime('%Y-%m-%d'))
    ck(r_conn['errors'].startswith('OSError'),
       f'取连接异常同样落在 errors 里（实得 {r_conn["errors"]!r}）')
    _ds.get_db_conn = _orig_get

    _EBUF.clear()
    _n1 = len(_SENT)
    builtins.print = _ecap
    try:
        # 连接已恢复：这一轮判据里没有 chain_errors ⇒ 一条都不该推
        L.alert_and_record(L.check_once(now=_now12), force=False)
        ck(len(_SENT) == _n1, '库恢复正常 ⇒ E 类不再推（实得仍 ' + str(len(_SENT)) + ' 条）')
        # 再喂一个**当天没出现过的新文本**（去掉连接桩之后已经查不到异常了，所以手动构造 res）
        L.alert_and_record({'enabled': True, 'day': _now12.strftime('%Y-%m-%d'),
                            'alerts': [], 'skipped': '',
                            'chain_errors': 'RuntimeError: 又一种新异常 text xyz'}, force=False)
        ck(any('又一种新异常 text xyz' in x for x in _EBUF),
           '异常文本换了 ⇒ 日志必须再打一行（去重只按"文本变没变"，不是一律沉默）')
        r_ok = L.check_once(now=_now12)
    finally:
        builtins.print = _EOLD
    ck(len(_SENT) == _n1, '库恢复后不再推 E 类')
    ck('chain_errors' not in r_ok, '恢复正常时判据不留异常痕迹')
finally:
    _ds.get_db_conn = _orig_get
    import services.support.push as _P
    _P.push_wecom_webhook = _orig_push12
    builtins.print = _EOLD
    c12.close()
    dbmod.use_db_path(_ORIGIN_DB12)
    os.remove(tmp12)


print(f'\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败')
if FAIL:
    for m in FAIL:
        print('  失败：', m)
    sys.exit(1)
