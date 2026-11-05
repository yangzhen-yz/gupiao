"""position_watch.py - 持仓快反推送（2026-10-14 新增）

补的是哪一块
------------
改造前，"跌破止损线"这件事要等到用户自己打开页面才看得见：前端 60 秒轮询
（午休 300 秒、盘后 600 秒）+ 行动台 8 秒缓存 + 行情 5 秒缓存 ≈ 最快 73 秒，
而 push.py 只推"买入候选"，全文不引用持仓。用户在办公室窄屏里看盘、经常不在电脑前，
止损信号却是唯一一个"晚几分钟就等于多亏几个点"的信号。

本模块把持仓的紧急动作变成主动推送：每 POSITION_WATCH_INTERVAL_SEC 秒扫一次持仓，
复用 services/position.py 的 diagnose_position 判定（**与持仓页、行动台同一口径**，
不另建一套止损算法，避免"同一只票两个页面两个止损价"），命中即推企业微信。

纪律（宁可少发，也不能发到用户关推送）
--------------------------------------
  1. 每只票每种动作每天最多推一次；唯一例外是止损后继续跌
     （较上次推送价再跌 POSITION_WATCH_REPUSH_DROP_PCT%）才允许重推，并标“续跌”；
  2. 一轮扫描里的多只票合并成**一条**消息，不逐只发；
  3. 市场级提示双向对称（2026-10-23 用户指出“昨天转弱推了、今天由跌反攻却没推”）：
     转弱（退潮/冰点）当天每周期推一条；转弱之后情绪第一次走出坏周期（修复/回暖向）
     再推一条“情绪修复”——只在近期推过转弱时才触发，常态回暖日不会天天吵；
  4. 市场级消息与账号无关，去重标记记在全局账号 0 上——多账号绑同一群时只发一条
     （2026-10-22 实锤：按账号去重导致群里 10:36:19/:20 连收两条一模一样的转弱）；
  5. 去重落在 DB（position_alert_log），重启服务不会把已经推过的再推一遍。
"""
import json
import time
from datetime import datetime, timedelta
from typing import Dict, List

from app.config import (POSITION_WATCH_ENABLED, POSITION_WATCH_INTERVAL_SEC,
                        POSITION_WATCH_START, POSITION_WATCH_END,
                        POSITION_WATCH_REPUSH_DROP_PCT, POSITION_WATCH_USERS,
                        PUSH_DAILY_LIMIT_PER_USER, WECOM_PUSH_ENABLED)
from db.database import get_db_conn
from services.portfolio.position import get_portfolio_diagnosis
# 按账号发送：谁的持仓提醒发给谁，不再共用一个全局群（多用户下这是串台+隐私事故）
from services.support.push_channel import send_to_user
import services.support.push_channel as pc
from services.strategy.sentiment import get_sentiment
from services.data.trading_calendar import get_session_phase, is_trading_day

# 需要主动叫醒的动作（与 action_desk._URGENT_ACTIONS 同一集合；giveback 是"还能救"的早提醒）
_ALERT_ACTIONS = {
    'stop_loss': ('🛑', '立即止损'),
    'reduce': ('⚠️', '减仓回避'),
    'take_profit': ('🎯', '止盈落袋'),
    'giveback': ('📉', '冲高回落'),
}
_MARKET_SYMBOL_KEY = '__market__'
# 市场级消息（情绪转弱/修复）与账号无关，去重记录统一记在虚拟账号 0 上：
# 多账号绑同一企微群时按各自账号去重会发两条一模一样的（10-22 实锤），全局记一次即发一次。
_MARKET_USER_ID = 0
# 情绪周期的“坏方向”：进入即推转弱；走出即可能触发修复推送
_BAD_CYCLES = ('退潮', '冰点')
# 按账号节流：调度器主循环已按 POSITION_WATCH_INTERVAL_SEC 控频，这里只防单账号"一轮内重复扫"。
# 注意不能用模块级单个时间戳——watch_all_users 同一轮会顺序扫所有账号，全局时间戳会让
# 第一个账号之后的账号全部命中节流被跳过（多账号静默漏推）。
_last_scan_ts_by_user: dict = {}


def _get_log(date_str: str, user_id: int, symbol: str, kind: str) -> Dict:
    """去重记录按 (日期, 账号, 股票, 动作) —— 主键必须含 user_id，
    否则 A 绑过的那只票会把 B 的同票提醒一起挡掉。表结构由 push_channel 统一负责。"""
    try:
        pc._ensure_table()
        conn = get_db_conn()
        row = conn.execute('SELECT pushed_at, price, note FROM position_alert_log '
                           'WHERE alert_date=? AND user_id=? AND symbol=? AND kind=?',
                           (date_str, user_id, symbol, kind)).fetchone()
        conn.close()
        return dict(row) if row else {}
    except Exception:
        return {}


def _mark(date_str: str, user_id: int, symbol: str, kind: str, price: float, note: str = ''):
    try:
        pc._ensure_table()
        conn = get_db_conn()
        conn.execute('INSERT OR REPLACE INTO position_alert_log '
                     '(alert_date, user_id, symbol, kind, pushed_at, price, note) '
                     'VALUES (?,?,?,?,?,?,?)',
                     (date_str, user_id, symbol, kind,
                      datetime.now().strftime('%Y-%m-%d %H:%M:%S'), price, note))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'[持仓快反] 记录去重失败: {e}')


def _unmark(date_str: str, user_id: int, symbol: str, kind: str):
    """发送失败时撤回去重标记 —— 否则这条止损信号当天就永久丢了"""
    try:
        conn = get_db_conn()
        conn.execute('DELETE FROM position_alert_log '
                     'WHERE alert_date=? AND user_id=? AND symbol=? AND kind=?',
                     (date_str, user_id, symbol, kind))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _in_window(now: datetime) -> bool:
    if not is_trading_day(now.date()):
        return False
    hm = now.strftime('%H:%M')
    if not (POSITION_WATCH_START <= hm <= POSITION_WATCH_END):
        return False
    # 集合竞价/午休不推（午休的回落要等开盘才执行，推了只是吵人）
    return get_session_phase(now).get('phase') in ('morning', 'mid', 'tail')


def _fmt_position_alert(items: List[Dict], sentiment: Dict) -> str:
    lines = [f'**🔔 持仓紧急信号 {datetime.now():%H:%M:%S}**', '']
    cyc = sentiment.get('cycle') or '未知'
    if cyc != '未知':
        lines.append(f'短线情绪：**{cyc}**（{sentiment.get("cycleScore")}分）'
                     f'{sentiment.get("advice") or ""}')
        lines.append('')
    for i, p in enumerate(items, 1):
        emoji, label = _ALERT_ACTIONS.get(p['action'], ('⚠️', p.get('action_label') or ''))
        lines.append(f'{i}. {emoji} **{p["name"]}**（{p.get("symbol", "")}）—— {label}')
        lines.append(f'   现价 {p["price"]}｜成本 {p.get("cost")}｜'
                     f'盈亏 {p.get("pnl_pct", 0):+}%（{p.get("pnl_amt", 0):+}元）')
        lines.append(f'   止损 {p.get("stop_price")}｜目标 {p.get("target_price")}')
        lines.append(f'   {p.get("detail") or ""}')
        if p.get('_repeat_tag'):
            lines.append(f'   （{p["_repeat_tag"]}）')
        flags = p.get('flags') or []
        if flags:
            lines.append(f'   信号：{"、".join(flags)}')
        lines.append('')
    if cyc in ('退潮', '冰点'):
        lines.append('纪律：亏钱效应扩散期，反弹先兑现、不补仓、不接力高位。')
    return '\n'.join(lines)


def _fmt_market_alert(sentiment: Dict) -> str:
    ind = sentiment.get('indicators') or {}
    lines = [f'**📉 短线情绪转弱 {datetime.now():%H:%M:%S}**', '',
             f'周期：**{sentiment.get("cycle")}**（{sentiment.get("cycleScore")}分）',
             f'{sentiment.get("detail") or ""}', '']
    for w in (sentiment.get('weakSignals') or [])[:4]:
        lines.append(f'· {w}')
    top = (sentiment.get('leadSectors') or [])[:1]
    if top:
        lines.append(f'· 当日涨停最集中：{top[0]["sector"]}（{top[0]["limitUp"]}只，'
                     f'最高{top[0]["maxLianban"]}板）')
    lines += ['', '行动：停新开仓、持仓反弹减仓、不追高不接力。']
    _ = ind  # 指标已写进 detail，这里只是留个字段引用方便以后扩展
    return '\n'.join(lines)


def _fmt_market_recover(sentiment: Dict) -> str:
    """与转弱对称的“情绪修复”文案（2026-10-23）：冰点/退潮走出后的第一次告知。

    由跌反攻的当天，用户需要知道“可以恢复观察/轻仓试错了”，错过修复日第一段
    与错过止损同样贵 —— 只报坏消息的好人风控是半个风控。
    """
    lines = [f'**📈 短线情绪修复 {datetime.now():%H:%M:%S}**', '',
             f'周期：**{sentiment.get("cycle")}**（{sentiment.get("cycleScore")}分）',
             f'{sentiment.get("detail") or ""}', '']
    top = (sentiment.get('leadSectors') or [])[:1]
    if top:
        lines.append(f'· 当日涨停最集中：{top[0]["sector"]}（{top[0]["limitUp"]}只，'
                     f'最高{top[0]["maxLianban"]}板）')
    advice = sentiment.get('advice') or '情绪修复，轻仓试错主流题材低位股'
    lines += ['', f'行动：{advice}。']
    return '\n'.join(lines)


def _recent_down_pushed(date_str: str) -> bool:
    """近 4 天（含今天）有没有推过“转弱”。修复推送只在转弱之后触发：
    常态回暖日绝不每天报“情绪很好”（那是噪音），它只回答“昨天叫你别动手，
    现在可以重新看了吗”。4 天窗口是给跨周末兜的：周五冰点 → 周一回暖要能推
    （自测第 6 项钉死，3 天窗口在周五→周一场景会漏）。
    """
    d0 = datetime.strptime(date_str, '%Y-%m-%d')
    for i in range(4):
        d = (d0 - timedelta(days=i)).strftime('%Y-%m-%d')
        for cyc in _BAD_CYCLES:
            if _get_log(d, _MARKET_USER_ID, _MARKET_SYMBOL_KEY, cyc):
                return True
    return False


async def watch_positions(user_id: int = 1) -> Dict:
    """一轮持仓快反扫描：诊断持仓 → 找紧急动作 → 去重 → 合并推送。

    返回 {'pushed': N, 'alerts': [...]}；任何异常都由 scheduler 侧兜底，不影响其他任务。
    """
    global _last_scan_ts_by_user
    if not POSITION_WATCH_ENABLED or not WECOM_PUSH_ENABLED:
        return {'pushed': 0, 'disabled': True}

    now = datetime.now()
    if not _in_window(now):
        return {'pushed': 0, 'off_window': True}
    # 按账号节流：避免多账号下只有第一个账号被扫到（每个账号独立记时）。
    last = _last_scan_ts_by_user.get(user_id, 0.0)
    if time.time() - last < POSITION_WATCH_INTERVAL_SEC - 1:
        return {'pushed': 0, 'throttled': True}
    _last_scan_ts_by_user[user_id] = time.time()

    date_str = now.strftime('%Y-%m-%d')
    pc._ensure_table()

    diag = await get_portfolio_diagnosis(user_id=user_id)
    positions = diag.get('positions') or []
    sentiment = await get_sentiment()

    # 每日上限：异常行情下一轮可能同时扫出多只止损票，逐轮累加会把人轰炸到关掉推送。
    # 到了上限就只记行动台、不再发消息，并把本轮信号标成 capped（不反复重试）。
    capped = pc.over_daily_limit(user_id)
    alerts: List[Dict] = []
    locked_today: List[Dict] = []
    for p in positions:
        act = p.get('action')
        if act not in _ALERT_ACTIONS:
            continue
        sym = (p.get('symbol') or '').lower()
        # T+1 闸门：今天买的票今天卖不掉，推"立即止损"是看着不能动的噪音（2026-10-15 用户
        # 就是被这条骂的：闽东电力当天建仓、当天收到两条"该卖"）。改成：当天不推，
        # 只记成 t1_pending，第二天 9:15 集合竞价一开始再推"昨日冻结、今日可卖"预案。
        if not p.get('sellable', True):
            if not _get_log(date_str, user_id, sym, act):
                _mark(date_str, user_id, sym, act, float(p.get('price') or 0), 't1_pending')
                locked_today.append(dict(p, symbol=sym))
                print(f"[持仓快反 {datetime.now():%H:%M:%S}] {p.get('name')} 触发 {act} "
                      f"但今日买入不可卖，已转入明日竞价预案")
            continue
        log = _get_log(date_str, user_id, sym, act)
        if log:
            # 止损后继续下跌才允许重推：这类票"没走"是事实，用户需要被第二次提醒
            if act == 'stop_loss':
                base_price = float(log.get('price') or 0)
                cur_price = float(p.get('price') or 0)
                if base_price > 0 and cur_price <= base_price * (1 - POSITION_WATCH_REPUSH_DROP_PCT / 100):
                    item = dict(p)
                    item['_repeat_tag'] = (f'较上次推送（{log.get("pushed_at", "")[-8:]} '
                                           f'{base_price}）再跌，累计跌破止损'
                                           f'{(1 - cur_price / float(p.get("stop_price") or cur_price)) * 100:.1f}%')
                    alerts.append(item)
                    _mark(date_str, user_id, sym, act, cur_price,
                          'capped' if capped else 'repeat')
            continue
        item = dict(p)
        item['symbol'] = sym
        alerts.append(item)
        _mark(date_str, user_id, sym, act, float(p.get('price') or 0),
              'capped' if capped else '')

    # 市场级：情绪转弱全局每天每周期只推一次（退潮→冰点算升级、可再推一次）；
    # 去重记在全局账号 0 —— 多账号绑同一群时不再各发一条（10-22 重复推送实锤）。
    market_lines = None
    if sentiment.get('cycle') in _BAD_CYCLES and not sentiment.get('stale'):
        if not _get_log(date_str, _MARKET_USER_ID, _MARKET_SYMBOL_KEY,
                        str(sentiment.get('cycle'))):
            market_lines = _fmt_market_alert(sentiment)
            _mark(date_str, _MARKET_USER_ID, _MARKET_SYMBOL_KEY,
                  str(sentiment.get('cycle')), 0.0,
                  json.dumps(sentiment.get('weakSignals') or [], ensure_ascii=False))

    # 方向对称（2026-10-23）：转弱之后情绪第一次走出坏周期 → 推“情绪修复”。
    # 口径：近 3 天推过转弱 + 今天还没推过修复 + 数据新鲜。常态回暖日不触发。
    recover_lines = None
    cycle = str(sentiment.get('cycle') or '')
    if cycle and cycle not in _BAD_CYCLES and not sentiment.get('stale'):
        if (_recent_down_pushed(date_str)
                and not _get_log(date_str, _MARKET_USER_ID, _MARKET_SYMBOL_KEY, f'up:{cycle}')):
            recover_lines = _fmt_market_recover(sentiment)
            _mark(date_str, _MARKET_USER_ID, _MARKET_SYMBOL_KEY, f'up:{cycle}', 0.0,
                  f'修复自转弱（{sentiment.get("cycleScore")}分）')

    # 情绪减仓影子（2026-11-04，#11）：情绪进入坏周期时，给该账号每个持仓算一条
    # "该减多少"落 exit_signal_log。**不执行、不改推送、不参与任何现有判定**，只攒对照数据
    # （减仓比例是先验值、未做寻优，≥20 个交易日后再裁决要不要接进动作链）。
    # 与上面的市场级推送是两件事：那条回答"要不要提醒你"（每周期只推一次、记在账号 0），
    # 这条回答"具体减多少股"（按账号按持仓，一天一票一条，UNIQUE 幂等）。
    # T+1 当天卖不掉的持仓不记——记了也执行不了，只会虚增信号量。
    # 关掉 EXIT_SENTIMENT_SHADOW_ENABLED 时一个信号都不落，逐字节等于改造前。
    shadow_written = 0
    if sentiment.get('cycle') in _BAD_CYCLES and not sentiment.get('stale'):
        try:
            from services.strategy.exit_shadow import record_sentiment_reduce
            for p in positions:
                if not p.get('sellable', True):
                    continue
                if record_sentiment_reduce(user_id=user_id, pos=p, sentiment=sentiment,
                                           date_str=date_str):
                    shadow_written += 1
            if shadow_written:
                # 只打汇总、不改返回值结构（前端读的是 pushed/symbols，加 key 是风险）；
                # 每轮一行便于事后数"这个周期一共给了多少条建议"
                print(f'[减仓影子] 账号{user_id} 本轮新落 {shadow_written} 条减仓建议'
                      f'（{sentiment.get("cycle")}，影子·不执行）')
        except Exception as e:
            print(f'[减仓影子] 本轮异常（不影响推送）: {e}')

    pushed = 0
    if capped:
        print(f'[持仓快反 {datetime.now():%H:%M:%S}] 账号{user_id} 今日已发 '
              f'{pc.sent_today(user_id)} 条，达上限 {PUSH_DAILY_LIMIT_PER_USER}，'
              f'本轮 {len(alerts)} 条信号只记进行动台，不再发消息')
    elif alerts:
        res = send_to_user(user_id, _fmt_position_alert(alerts, sentiment))
        if res.get('ok'):
            pushed += 1
        else:
            for p in alerts:
                _unmark(date_str, user_id, (p.get('symbol') or '').lower(), p.get('action'))
    if market_lines:
        # 市场级提醒对所有人内容一样，允许回落到全局出口（personal=False）；
        # 上面那条持仓消息则严格只走本人通道，绝不把别人的持仓明细发进公共群。
        res = send_to_user(user_id, market_lines, personal=False)
        if res.get('ok'):
            pushed += 1
        else:
            # 发送失败撤掉全局去重标记，否则这条市场消息当天就永久丢了
            _unmark(date_str, _MARKET_USER_ID, _MARKET_SYMBOL_KEY,
                    str(sentiment.get('cycle')))
    if recover_lines:
        res = send_to_user(user_id, recover_lines, personal=False)
        if res.get('ok'):
            pushed += 1
        else:
            _unmark(date_str, _MARKET_USER_ID, _MARKET_SYMBOL_KEY, f'up:{cycle}')
    if pushed:
        print(f'[持仓快反 {datetime.now():%H:%M:%S}] 推送 {pushed} 条，'
              f'涉及动作 {len(alerts)} 只 + 情绪{sentiment.get("cycle")}')
    return {'pushed': pushed, 'alerts': [a.get('symbol') for a in alerts],
            'cycle': sentiment.get('cycle')}


def _fmt_t1_plan(items: List[Dict], diag: Dict) -> str:
    lines = [f'**📋 昨日冻结 · 今日可卖预案 {datetime.now():%H:%M}**', '',
             '这些票昨天买入时触发了信号但 T+1 卖不掉，今天开盘可以执行：', '']
    for i, p in enumerate(items, 1):
        emoji, label = _ALERT_ACTIONS.get(p.get('_t1_signal') or 'stop_loss', ('⚠️', '处理'))
        lines.append(f'{i}. {emoji} **{p["name"]}**（{p.get("symbol","")}）—— {label}')
        lines.append(f'   昨日信号价 {p.get("_t1_price")} → 现价 {p.get("price")}'
                     f'｜成本 {p.get("cost")}｜盈亏 {p.get("pnl_pct", 0):+}%')
        lines.append(f'   止损 {p.get("stop_price")}｜目标 {p.get("target_price")}')
        lines.append(f'   {p.get("detail") or ""}')
        lines.append('')
    lines.append('竞价纪律：高开 5% 以上不追买、只执行卖出；低开破止损位直接走，不等反抽。')
    _ = diag  # 预留：后面可带账户级建议
    return '\n'.join(lines)


async def push_t1_plan(user_id: int = 1) -> Dict:
    """次日 9:15 竞价窗口：把昨天"买了却卖不掉"的信号变成今天可执行的预案。

    T+1 是硬约束 —— 当天建仓的票当天推止损毫无意义（2026-10-15 用户就是被这条骂的）。
    正确的时点是第二天集合竞价：那时既能挂单，也还有昨天的盘面记忆。
    """
    now = datetime.now()
    if not is_trading_day(now.date()) or get_session_phase(now).get('phase') != 'auction':
        return {'pushed': 0, 'skipped': '非竞价时段'}
    date_str = now.strftime('%Y-%m-%d')
    pc._ensure_table()
    conn = get_db_conn()
    rows = conn.execute("SELECT alert_date, symbol, kind, price FROM position_alert_log "
                        "WHERE user_id=? AND note='t1_pending' AND alert_date<? "
                        "ORDER BY alert_date DESC", (user_id, date_str)).fetchall()
    conn.close()
    if not rows:
        return {'pushed': 0, 'empty': True}
    latest: Dict[str, Dict] = {}
    for r in rows:
        latest.setdefault(str(r[1]).lower(), dict(r))

    diag = await get_portfolio_diagnosis(user_id=user_id)
    pos_map = {(p.get('symbol') or '').lower(): p for p in (diag.get('positions') or [])}
    items = []
    for sym, r in latest.items():
        p = pos_map.get(sym)
        if not p:
            continue                       # 已不在持仓，说明他自己处理过了
        if not p.get('sellable', True):
            continue                       # 今天又加仓，仍然卖不掉，明天再说
        items.append(dict(p, _t1_from=r.get('alert_date'), _t1_signal=r.get('kind'),
                          _t1_price=r.get('price')))
    if not items:
        return {'pushed': 0, 'note': '昨日预案涉及的票已不在持仓或仍不可卖'}
    if _get_log(date_str, user_id, _MARKET_SYMBOL_KEY, 't1_plan'):
        return {'pushed': 0, 'deduped': True}

    res = send_to_user(user_id, _fmt_t1_plan(items, diag))
    if res.get('ok'):
        _mark(date_str, user_id, _MARKET_SYMBOL_KEY, 't1_plan', 0.0,
              json.dumps([i['symbol'] for i in items], ensure_ascii=False))
        conn = get_db_conn()
        conn.execute("DELETE FROM position_alert_log WHERE user_id=? AND note='t1_pending' "
                     "AND alert_date<?", (user_id, date_str))
        conn.commit()
        conn.close()
        print(f'[持仓快反] 已推送 T+1 预案：{[i["name"] for i in items]}')
        return {'pushed': 1, 'symbols': [i['symbol'] for i in items]}
    return {'pushed': 0, 'errors': res.get('errors')}


def _watch_targets() -> List[int]:
    """要扫哪些账号：配置留空 = users 表里的全部账号。

    为什么不能写死 id=1：本项目 2026-10-12 起多账号隔离，而 id=1 恰好是调试账号
    sim_dbg，真实用户 yz 是 id=2 —— 写死就会"扫没人看的仓、漏掉真在跑的仓"，
    而且这种漏法在日志里完全看不出异常。
    """
    if POSITION_WATCH_USERS:
        return list(POSITION_WATCH_USERS)
    try:
        conn = get_db_conn()
        ids = [r[0] for r in conn.execute('SELECT id FROM users ORDER BY id')]
        conn.close()
        return ids or [1]
    except Exception as e:
        print(f'[持仓快反] 读取账号列表失败，退回 id=1: {e}')
        return [1]


def watch_targets() -> List[int]:
    """对外暴露要扫描的账号列表（调度器 9:15 推 T+1 预案时也要用同一份）"""
    return _watch_targets()


async def watch_all_users() -> Dict:
    """按目标账号逐个扫描持仓（多用户账号，持仓彼此隔离）"""
    result = {'pushed': 0, 'users': []}
    for uid in _watch_targets():
        try:
            r = await watch_positions(user_id=uid)
            result['pushed'] += r.get('pushed') or 0
            result['users'].append({'user_id': uid, **r})
        except Exception as e:
            print(f'[持仓快反] 账号{uid}扫描失败: {e}')
            result['users'].append({'user_id': uid, 'error': str(e)})
    return result
