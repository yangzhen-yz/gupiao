# -*- coding: utf-8 -*-
"""sched_liveness.py - 调度链失活告警（2026-11-07，把 `docs/后续计划.md` 那条"观测兜底"落地）

要回答的问题只有一个：**"今天怎么没推票"不再需要人来问。**
11-06 那次调度链被冻 12 小时（03:30~15:52），五张决策表全天 0 行，而**没有任何一条通知**——
发现方式是用户下午问"大盘这么好为什么没推"，再翻日志、再数表行数。放大器（逐 job 隔离）
已经消掉了，按"消除放大器 → 自愈 → 告警"的顺序，这一步补的就是最后那一格。

两条判据，缺一不可
------------------
**A 心跳陈旧（抓"循环整个不转了"）**
`core/scheduler._run_pending_guarded()` 每跑完一轮就 `mark_loop_beat()`；本模块的观察者
线程每 60 秒看一次"距最后一次 beat 多少分钟"，超过 `SCHED_HEARTBEAT_STALE_MIN` 就告警。
关键设计：**观察者不能是被观察者本身**。判据如果写在主循环里，循环一死它就跟着死，
等于没有——所以它跑在独立的 daemon 线程，而且整个 tick 包在 try 里（一个坏 tick 不能打死观察者，
这正是 11-06 那个 bug 的教训反向用一次）。启动有 `SCHED_LIVENESS_START_GRACE_MIN` 宽限期，
否则每次重启都会先报一条假警。

**B 决策链空档（抓"循环还在转、但决策不产出"）**
只有 A 不够：11-06 那天主循环**一直在转**（日志里每 10 秒一条 run_pending 异常），
心跳根本不陈旧，可决策链就是全天 0 行。所以在 `SCHED_CHAIN_CHECK_TIMES`（默认 10:00 / 14:10）
查一次当天的产出行数。

判据的形态是**拿历史数据校出来的**，不是拍脑袋写的。第一版想的是"交易日 + 推荐表 0 行 = 告警"，
实测把它否掉了：2026-09-21/22/23、10-08 这四个官方交易日推荐表也全是 0 行——那天系统还没上线
盘中情绪快循环/候选池，不是故障。按第一版写，这四天每天两条假警。真正区分"放假/没上线"
与"进程活着但决策链死了"的是**数据链有没有在产**：

    2026-09-21  推荐0 批次0 候选池0 竞价0 情绪快照0     ← 整天没在跑（不告警）
    2026-11-06  推荐0 批次0 候选池0 竞价0 情绪快照53    ← 数据链活着、决策链死了（告警）
    2026-11-05  推荐3 批次6 候选池0 竞价2464 情绪快照48  ← 正常（不告警）

所以 B 的成立条件是：交易日 **且** 当天 `market_sentiment_snapshots` 有行 **且**
`daily_recommendations`/`strategy_snapshot*`/`daily_candidate_pool` 全 0 行。
`daily_candidate_pool` 只能进"全 0"那一侧、不能当必要条件，真实原因 11-09 才量准：
它的 `pool_date` **恒滞后一个交易日**（09:00 那班 RPS 用上一交易日的日K算，写的是昨天），
真库 548 行里 `pool_date == date(created_at)` 的有 **0 行**。所以按"当天"去数它永远是 0，
是纯冗余成员，不提供任何"活着"的证据。**也不许把它换成 `created_at`**：那样 11-06 会被判成
活着（池子 09:00 就写完了，证明不了 09:45 之后循环还在跑），恰好漏掉本模块要抓的那次真事故。
（早先此处写的理由是"该表 10-14 才起、有独立开关"，那不是主因，11-09 更正。）

去重与限频
----------
A 类同因冷却 `SCHED_LIVENESS_COOLDOWN_MIN` 分钟；B 类每天每档只发一次。
**发送失败不写去重标记**（否则那条告警当天永久丢，这是过夜出口提醒那次定下的规矩）。
留痕只在进程内存（重启即重置）——这是运维告警不是业务数据，为它建表得不偿失；
真要审计，`server_supervised.log` 里每条都有对应打印。

关掉开关（`SCHED_LIVENESS_ENABLED=False`，配置页 `sched_liveness_enabled`）时
连数据库都不查、一条通知都不发，逐字节等于没这项；`start_watchdog()` 也不会起线程。
"""
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from app.config import (SCHED_CHAIN_CHECK_TIMES, SCHED_HEARTBEAT_STALE_MIN,
                        SCHED_LIVENESS_COOLDOWN_MIN, SCHED_LIVENESS_ENABLED,
                        SCHED_LIVENESS_START_GRACE_MIN, SCHED_LIVENESS_WINDOW)

__all__ = ['mark_loop_beat', 'beat_state', 'check_once', 'start_watchdog',
           'stop_watchdog', 'is_enabled']

# 观察者看到的"世界现状"。全部只读，本模块不写任何业务表。
_BEAT = {'ts': 0.0, 'since': None, 'loop_errors': 0}
_STATE = {
    'started_ts': 0.0,
    'stop': False,
    'last_alert': {'A': 0.0, 'B': '', 'E': ''},
    'last_skip_reason': '',      # 上一轮"为什么不发"的原因，变了才打印
    'last_chain_error': '',      # 上一轮 B 判据查库的异常文本，变了才报（修⑩-5）
    'thread': None,
}
_LOCK = threading.Lock()

# B 判据里"活着"的证据表与"该产出"的表（表名/列名实测于 11-07）
_ALIVE_TABLE = ('market_sentiment_snapshots', 'sent_date')
_DECISION_TABLES = (('daily_recommendations', 'rec_date'),
                    ('strategy_snapshot_batch', 'snap_date'),
                    ('strategy_snapshot', 'snap_date'),
                    ('daily_candidate_pool', 'pool_date'))


def _cfg(key: str, default: Any) -> Any:
    """先热读配置页覆盖表，读不到再退回 config 现值（与海选影子同一写法）。"""
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
    return bool(_cfg('sched_liveness_enabled', SCHED_LIVENESS_ENABLED))


def mark_loop_beat(ran: int = 0, errors: int = 0) -> None:
    """主循环每跑完一轮调一次。`ran`=本轮真正执行的 job 数，`errors`=其中抛异常的个数。"""
    now = time.time()
    with _LOCK:
        _BEAT['ts'] = now
        _BEAT['since'] = datetime.now()
        _BEAT['loop_errors'] = int(_BEAT.get('loop_errors', 0)) + int(errors or 0)
        _BEAT['last_ran'] = int(ran or 0)


def beat_state() -> Dict:
    """给告警文本与自测用的心跳快照（不改动状态）。"""
    with _LOCK:
        ts = float(_BEAT.get('ts') or 0.0)
    return {'ts': ts,
            'since': _BEAT.get('since'),
            'age_min': (round((time.time() - ts) / 60.0, 1) if ts else None),
            'last_ran': _BEAT.get('last_ran'),
            'loop_errors': _BEAT.get('loop_errors'),
            'started': _STATE['started_ts']}


def _rows(conn, table: str, date_col: str, day: str) -> int:
    try:
        return int(conn.execute(f'SELECT COUNT(*) FROM {table} WHERE {date_col} LIKE ?',
                                (day + '%',)).fetchone()[0])
    except Exception:
        return -1                      # 表还不存在（全新库/功能未上线）：按"没数"处理，不参与必要条件


def _count_chain(day: str) -> Dict:
    from db.dal_state import get_db_conn
    out = {'alive': 0, 'decision': {}, 'errors': ''}
    conn = None
    # 11-07 修⑩-5 顺带抓到的第二处：**取连接这一步原来在 try 外面**。
    # 而"库被锁死/打不开"恰恰是最需要它活着的那一种失败——原来异常直接从 `_count_chain`
    # 逃出去，B 判据一条没判，观察线程只留下一行"本轮异常"。现在连取连接一起包进来，
    # 失败就如实写进 `errors`，由 `alert_and_record` 的 E 类出声。
    try:
        conn = get_db_conn()
        out['alive'] = max(0, _rows(conn, _ALIVE_TABLE[0], _ALIVE_TABLE[1], day))
        for t, col in _DECISION_TABLES:
            out['decision'][t] = max(0, _rows(conn, t, col, day))
    except Exception as e:
        out['errors'] = f'{type(e).__name__}: {e}'
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out


def _hm_now(now: datetime) -> str:
    return now.strftime('%H:%M')


def check_once(now: Optional[datetime] = None, force: bool = False) -> Dict:
    """跑一次两条判据。返回结果 dict（含 `alerts` 列表，空=一切正常）。

    `force=True` 只用于自测/人工点验：跳过冷却与限频，仍然照实判据。
    """
    now = now or datetime.now()
    ds = now.strftime('%Y-%m-%d')          # 先算出来：连"开关关闭"那条返回也要带得上同一天
    res = {'enabled': is_enabled(), 'now': now.strftime('%H:%M:%S'),
           'day': ds, 'alerts': [], 'skipped': ''}
    if not res['enabled']:
        res['skipped'] = '开关关闭：连数据库都不查'
        return res

    from services.data.trading_calendar import is_calendar_covered, is_trading_day
    if not is_trading_day(ds):
        res['skipped'] = '非交易日（官方日历），两条判据都不判'
        return res
    w_start, w_end = (SCHED_LIVENESS_WINDOW[0], SCHED_LIVENESS_WINDOW[1])
    hm = _hm_now(now)
    if not (w_start <= hm <= w_end):
        # 原因串里**不许拼当前时刻**：下游 alert_and_record() 拿这条串当去重键
        # （_STATE['last_skip_reason'] != why 才打一行），拼了 hh:mm 就每分钟都算
        # "原因变了"，去重当场失效。11-09 线上实测抓到：日志里"本轮不发"579 行，
        # 其中 560 行是同一句"不在观测窗口…（现在 hh:mm）"，正是 PR #13 要治的刷屏。
        # 时刻信息没丢——打印那行前缀本来就带 [调度失活 HH:MM:SS]。
        res['skipped'] = f'不在观测窗口 {w_start}~{w_end} 内'
        return res

    beat = beat_state()
    grace = float(_cfg('sched_liveness_start_grace_min', SCHED_LIVENESS_START_GRACE_MIN))
    stale = float(_cfg('sched_liveness_heartbeat_stale_min', SCHED_HEARTBEAT_STALE_MIN))
    cooldown = float(_cfg('sched_liveness_cooldown_min', SCHED_LIVENESS_COOLDOWN_MIN)) * 60.0

    # ---- A 心跳陈旧 ----
    if not beat['ts']:
        # 从没记过 beat：可能是启动早期还没进循环，给宽限期
        if _STATE['started_ts'] and (time.time() - _STATE['started_ts']) > grace * 60.0:
            res['alerts'].append({
                'kind': 'A', 'why': f'进程起来 {round((time.time() - _STATE["started_ts"]) / 60.0)} 分钟'
                                    f'了，一次都没记到调度心跳（宽限 {grace:g} 分钟）',
                'stale_min': None})
        else:
            res['skipped'] = '还没有心跳，但在启动宽限期内'
    elif beat['age_min'] is not None and beat['age_min'] > stale:
        if force or (time.time() - _STATE['last_alert']['A']) >= cooldown:
            res['alerts'].append({
                'kind': 'A', 'why': f'距最后一次调度心跳 {beat["age_min"]} 分钟（阈值 {stale:g} 分钟）',
                'stale_min': beat['age_min']})

    # ---- B 决策链空档 ----
    # B 只查**最近已过的那一档**（默认 10:00 / 14:10），不是所有已过的档都查一遍——
    # 第一版就是这么写的，14:11 那次一口气发两条同因告警，被自测 [3]/[6] 当场抓住。
    passed = [x for x in SCHED_CHAIN_CHECK_TIMES if hm >= x]
    if not passed:
        res.setdefault('chain_note', f'还没到第一个检查时点（{SCHED_CHAIN_CHECK_TIMES[0]}）')
    else:
        slot = max(passed)
        if _STATE['last_alert'].get('B') == f'{ds}#{slot}' and not force:
            res.setdefault('chain_note', f'{slot} 这一档今天已经报过，不重复发')
        else:
            cnt = _count_chain(ds)
            if cnt['errors']:
                res.setdefault('chain_errors', cnt['errors'])
            else:
                all_zero = all(v == 0 for v in cnt['decision'].values())
                if cnt['alive'] > 0 and all_zero:
                    res['alerts'].append({
                        'kind': 'B', 'slot': slot,
                        'why': (f'到 {slot} 当天决策链表全 0 行，但情绪快照已有 {cnt["alive"]} 行'
                                f'（数据链活着、决策链没产出）'),
                        'rows': cnt})
                elif cnt['alive'] == 0 and all_zero:
                    # 整天一条都没产（放假/未上线/进程压根没起）——按 9 月那四天校准出来的这条不告警
                    res.setdefault('chain_note',
                                  '决策链与情绪快照全 0：整天没在跑，不是"跑着但卡住"，不告警')

    for a in res['alerts']:
        a['text'] = _render(a, beat, now, is_calendar_covered(ds))
    return res


def _render(a: Dict, beat: Dict, now: datetime, covered: bool) -> str:
    """告警正文。数字全部来自本次判定，不写"可能/大概"；缺日历覆盖时提醒去看年度表。"""
    head = '[调度失活告警] %s %s' % (now.strftime('%m-%d %H:%M'), a['kind'])
    body = a['why']
    _since = beat.get('since')
    anchor = ('最后心跳 %s 分钟前（%s），本轮执行 job 数 %s，累计异常计数 %s'
              % (beat.get('age_min'), (_since.strftime('%H:%M:%S') if _since else '无'),
                 beat.get('last_ran'), beat.get('loop_errors')))
    rows = ''
    if a.get('rows'):
        rows = ('\n当天行数：情绪快照 %d（活着）｜推荐 %d｜批次快照 %d｜逐票快照 %d｜候选池 %d'
                % (a['rows']['alive'], a['rows']['decision'].get('daily_recommendations', -1),
                   a['rows']['decision'].get('strategy_snapshot_batch', -1),
                   a['rows']['decision'].get('strategy_snapshot', -1),
                   a['rows']['decision'].get('daily_candidate_pool', -1)))
    if a['kind'] == 'A':
        # A 是"循环不转了"，B 是"循环在转但决策不出数"——两句话原来共用，结果 A 的正文
        # 也写着"错过 09:45 批次"，那是 B 的含义（11-07 合并后线上点验时看到的）。
        # 处置动作也不一样：A 先找卡在哪个 job / 线程还在不在，B 先看是哪一环断了产。
        tail = ('\n含义：调度主循环这一段时间没跑完（线程可能退了，或卡在某个 job 里出不来）。'
                '期间所有定时任务都不执行——海选、批次推荐、竞价与收盘快照、落库全停。')
        act = ('处置：先在 server/server_supervised.log 找最后一条 "[定时任务" 与 "[预热"，'
               '看卡在谁身上；确认循环停了就写 server/data/backend_restart_requested '
               '让看护重启（不要 taskkill）。')
    else:
        tail = ('\n含义：循环还在转、心跳也新鲜，但决策链今天一条都没落。盘中这会错过 '
                '09:45/10:00/13:30/14:05 批次，页面看得到行情但推荐是空的。')
        act = ('处置：看上面那份行数——情绪快照有行说明进程活着，问题出在推荐/批次这一环；'
               '先翻日志里 "[定时任务" 有没有 job 抛异常（11-06 就是一个坏 job 卡死整条链），'
               '必要时写 server/data/backend_restart_requested 让看护重启。')
    yr = ('' if covered else '\n[警告] 交易日历没覆盖当前年份（trading_calendar.CALENDAR_YEARS），判据按星期几走，先补年度休市表。')
    return '\n'.join([head, body, anchor + rows, tail, act]) + yr


def _render_blind(err: str, day: str, now: datetime) -> str:
    """E 类正文：说的不是"今天没事"，而是"今天我没能力判断"。"""
    return '\n'.join([
        '[调度链看护] 判据无法评估 %s' % now.strftime('%m-%d %H:%M'),
        f'查 {day} 当天决策链行数时抛异常：{err}',
        '含义：B（决策链空档）这一档今天不会告警——不是"没事"，是"看不见"。'
        '心跳那条（A）不依赖库，仍然有效。',
        '处置：优先看是不是写锁长占（日志 database is locked）、`app_settings` 之类'
        '的表还没建、或库文件异常；推送按天去重（一天最多一条），异常文本每次变化'
        '都会在日志里再打一行，不至于因为去重而漏掉新情况。',
    ])


def _send(text: str) -> bool:
    try:
        from services.support.push import push_wecom_webhook
        return bool(push_wecom_webhook(text))
    except Exception as e:
        print(f'[调度失活] 推送失败（下次重试，不写去重标记）: {e}')
        return False


def alert_and_record(res: Dict, force: bool = False) -> Dict:
    """把 check_once 的结论变成通知；**发送成功才写去重标记**。"""
    now = time.time()
    sent = []
    day = res.get('day') or datetime.now().strftime('%Y-%m-%d')   # 与判定用同一天，别用墙钟另取一个
    for a in res.get('alerts', []):
        pushed = _send(a['text'])
        if pushed:                       # 失败就不记，下一轮还会再判
            if a['kind'] == 'A':
                _STATE['last_alert']['A'] = now
            else:
                _STATE['last_alert']['B'] = '%s#%s' % (day, a['slot'])
        sent.append({'kind': a['kind'], 'pushed': pushed, 'why': a['why']})
    if sent:
        print('[调度失活 %s] %s' % (datetime.now().strftime('%H:%M:%S'),
                                    '；'.join(f"{s['kind']} 推送={'成功' if s['pushed'] else '失败'}"
                                              f"（{s['why']}）" for s in sent)))
    elif res.get('skipped'):
        # 只在**原因变了**的那一次打一行。原来无条件打，11-07 合并后实测周六每 60 秒
        # 一条"本轮不发：非交易日"= 1440 行/天，等于用一种刷屏换掉另一种刷屏；
        # 日志里塞满"本轮不发"之后，真出事那一行反而更难找。
        why = res['skipped']
        if _STATE.get('last_skip_reason') != why:
            _STATE['last_skip_reason'] = why
            print(f'[调度失活 {datetime.now():%H:%M:%S}] 本轮不发：{why}')

    # E「判据自己瞎了」——修⑩-5：这一档原来只把异常塞进 res['chain_errors'] 就再没人看，
    # 结果恰恰在**库查不动**（写锁、表缺、库损坏）的时候 B 不会告警，也没人知道它没判。
    # 哨兵最该出声的两种情况是"事情坏了"和"我看不见了"，第二种原来完全沉默。
    err = res.get('chain_errors') or ''
    if err and _STATE.get('last_chain_error') != err:
        _STATE['last_chain_error'] = err
        day = res.get('day') or datetime.now().strftime('%Y-%m-%d')
        text = _render_blind(err, day, datetime.now())
        print(f'[调度失活 {datetime.now():%H:%M:%S}] 判据无法评估（B 这一档今天不会告警）：{err}')
        if force or _STATE['last_alert'].get('E') != day:
            if _send(text):
                _STATE['last_alert']['E'] = day
                print(f'[调度失活 {datetime.now():%H:%M:%S}] E 类（看不见）已推送，当天不再重复')
    return {'sent': sent, 'alerts': len(res.get('alerts', []))}


def _watchdog_loop(interval_sec: int = 60) -> None:
    """观察者线程：整个 tick（含 sleep）都包在 try 里——一次异常不能打死观察者。

    这就是 11-06 的教训反过来用：那次是"一个坏 job 打断整条循环"，这里如果只包
    `check_once()` 而让 `time.sleep` 露在外面，一样会因为一个异常把观察线程带走，
    于是"负责发现事故的哨兵自己死于事故"。停止用 `_STATE['stop']` 标志（自测与
    将来的干净退出都走这里），不靠往线程里抛异常。
    """
    while not _STATE.get('stop'):
        try:
            alert_and_record(check_once())
            time.sleep(interval_sec)
        except Exception as e:
            try:
                print(f'[调度失活] 观察线程本轮异常（不影响下一轮）: {type(e).__name__}: {e}')
                time.sleep(interval_sec)
            except Exception:
                pass


def stop_watchdog() -> bool:
    """让观察线程在下一个 tick 自然退出（自测收尾用；返回是否请求成功）。"""
    _STATE['stop'] = True
    t = _STATE.get('thread')
    if t and t.is_alive():
        t.join(timeout=3)
    alive = bool(_STATE.get('thread') and _STATE['thread'].is_alive())
    if not alive:
        _STATE['thread'] = None
    return not alive


def start_watchdog(interval_sec: int = 60) -> Optional[threading.Thread]:
    """由 `run_scheduler()` 在进主循环之前调用；开关关掉时不起线程（零副作用）。"""
    _STATE['started_ts'] = time.time()
    if not is_enabled():
        print('[调度失活] 开关关闭，不起观察线程（逐字节等于没这项）')
        return None
    _STATE['stop'] = False
    if _STATE['thread'] and _STATE['thread'].is_alive():
        return _STATE['thread']
    t = threading.Thread(target=_watchdog_loop, args=(interval_sec,),
                         daemon=True, name='sched-liveness')
    t.start()
    _STATE['thread'] = t
    print(f'[调度失活] 观察线程已启动（每 {interval_sec}s 一次；心跳阈值 '
          f'{SCHED_HEARTBEAT_STALE_MIN} 分钟、检查时点 {"/".join(SCHED_CHAIN_CHECK_TIMES)}）')
    return t
