"""action_desk.py - 「今日行动台」聚合数据源（2026-10-13）

背景：后端调度器本来就按时段产出数据（9:25 竞价预览、9:45/13:30 推荐、14:45 尾盘快照、
15:10 收评、15:20 回测），但前端把这一切平铺成 9 个功能 Tab，用户得自己拼"现在该做什么"。
本模块把**同一个答案在服务端算好**：按当前时段决定该露出哪些区块、每块排什么，
前端只负责渲染。

硬约束（务必保持）：本模块是热路径，每次轮询都会跑，因此
  1. 绝不调用 /api/hot-stocks-scan（注释里记着单次 167s）、/api/scan-trend-stocks、
     /api/ai-diagnose（DeepSeek）、/api/daily_recommendations（GET 会重算约 30 天历史价）；
  2. 行情一律走 services.stock.fetch_with_retry 的 5 秒缓存 —— IndexBar 每 5 秒就在打
     同一批 symbol，所以这里基本是命中缓存的免费调用；
  3. 所有 DB 读取都用"只取需要的那一行/那一组"的专用查询（load_recommendation_group /
     load_latest_focus_plan / load_trend_scan_freshness），不复用整表加载函数。
"""
import asyncio
import time
from datetime import datetime
from typing import Dict, List, Optional

from db.database import (load_recommendation_group, load_latest_focus_plan,
                         load_trend_scan_freshness, load_hot_stock_buttons,
                         load_user_scan_pool)
from services.trading_calendar import get_session_phase, PHASE_LABELS
from services.market import load_market_alert, check_market_crash
from services.position import get_portfolio_diagnosis
from services.stock import fetch_with_retry, parse_tencent_batch_data
from services.risk_control import calc_risk_control

# 需要实时行情的时段（休市/盘后不取指数，省 RTT）
_LIVE_PHASES = {'auction', 'morning', 'lunch', 'mid', 'tail'}

# 各时段"现在可执行"的买入时机（buyTiming.type）
# 语义：推荐在当日盘中生成，'auction' 指的是"今日已涨停 → 次日竞价参与"，
# 所以它只在次日 9:15-9:25 可执行，盘中出现时属于"明日计划"而非"现在买"。
_PHASE_TIMINGS = {
    'auction': ('auction',),
    'morning': ('early', 'pullback', 'intraday'),
    'lunch':   ('pullback', 'intraday'),
    'mid':     ('pullback', 'intraday'),
    'tail':    ('tail', 'intraday'),
    'post':    (),
    'closed':  (),
    'pre':     (),
    'unknown': (),
}

# 时段内还要再看分钟：进 buyNow 的唯一标准是"此刻下单不违反它自己徽章上的窗口"，
# 否则区块标题（此时机可介入）和卡片徽章（尾盘买入）会互相打脸。
# 2026-10-14 用户就是这么问的：11:57 打开行动台，午休时段里出现了两张"尾盘买入"的卡。
_MORNING_EARLY_CUTOFF = 10 * 60   # 早盘买入只到 10:00（收评文案里同样写着"10 点后不再新开早盘仓"）


def _allowed_timings(phase: str, now: Optional[datetime] = None) -> tuple:
    allowed = _PHASE_TIMINGS.get(phase, ())
    if phase == 'morning' and allowed:
        d = now or datetime.now()
        if d.hour * 60 + d.minute >= _MORNING_EARLY_CUTOFF:
            allowed = tuple(t for t in allowed if t != 'early')
    return allowed

# 前端轮询间隔建议（毫秒）：越靠近决策窗口越密
_PHASE_POLL_MS = {
    'pre': 120000, 'auction': 20000, 'morning': 60000, 'lunch': 300000,
    'mid': 60000, 'tail': 30000, 'post': 600000, 'closed': 1800000,
    'unknown': 600000,
}

# 区块顺序：行动台按此顺序渲染（服务端说了算，前端不再各自判断时段）
_PHASE_SECTIONS = {
    'pre':     ['tradeWindow', 'tomorrowPlan', 'positionActions', 'watchlist'],
    'auction': ['tomorrowPlan', 'tradeWindow', 'buyNow', 'watchlist'],
    'morning': ['positionActions', 'buyNow', 'tradeWindow', 'watchlist'],
    'lunch':   ['positionActions', 'reviewDigest', 'buyNow', 'watchlist'],
    'mid':     ['positionActions', 'buyNow', 'tradeWindow', 'watchlist'],
    'tail':    ['positionActions', 'buyNow', 'tradeWindow', 'watchlist'],
    'post':    ['reviewDigest', 'tomorrowPlan', 'positionActions', 'watchlist'],
    'closed':  ['tomorrowPlan', 'reviewDigest', 'watchlist'],
    'unknown': ['reviewDigest', 'watchlist'],
}

_URGENCY = {'stop_loss': 0, 'reduce': 1, 'take_profit': 2, 'giveback': 3, 'trailing': 4}
# 需立刻执行的（卡片染红）vs 该出现在待办里的（多一条"冲高回落"，但不抢紧急的视觉权重）
_URGENT_ACTIONS = ('stop_loss', 'reduce', 'take_profit')
_ACTIONABLE_ACTIONS = _URGENT_ACTIONS + ('giveback',)

_DESK_CACHE: Dict[str, Dict] = {}
_DESK_CACHE_TTL = 8  # 秒：合并两个账号多标签页的连续请求


def _norm(sym: str) -> str:
    return (sym or '').strip().lower()


def _today_str() -> str:
    return datetime.now().strftime('%Y-%m-%d')


def _quote_map_to_dict(quotes: List[Dict]) -> Dict[str, Dict]:
    return {_norm(q.get('symbol')): q for q in quotes if q.get('symbol')}


async def _batch_quotes(symbols: List[str]) -> Dict[str, Dict]:
    """一次批量行情；失败返回空表（行动台降级为"无实时价"而不是报错）"""
    syms = [s for s in dict.fromkeys(_norm(x) for x in symbols) if s]
    if not syms:
        return {}
    try:
        data = await fetch_with_retry(','.join(syms))
        return _quote_map_to_dict(parse_tencent_batch_data(data))
    except Exception as e:
        print(f'[行动台] 批量行情失败: {e}')
        return {}


def _build_position_actions(diag: Dict) -> List[Dict]:
    """持仓 → 待办清单（到止损/该减仓/到止盈优先，其余归入"持有中"计数）"""
    out = []
    for p in diag.get('positions') or []:
        action = p.get('action')
        if action not in _ACTIONABLE_ACTIONS and not (p.get('trailing') or {}).get('enabled'):
            continue
        out.append({
            'symbol': p.get('symbol'),
            'name': p.get('name'),
            'price': p.get('price'),
            'pnlPct': p.get('pnl_pct'),
            'pnlAmt': p.get('pnl_amt'),
            'action': action,
            'actionLabel': p.get('action_label'),
            'urgent': action in _URGENT_ACTIONS,
            'stop': p.get('stop_price'),
            'target': p.get('target_price'),
            'trailingLine': (p.get('trailing') or {}).get('line'),
            'detail': p.get('detail'),
            'flags': p.get('flags') or [],
        })
    out.sort(key=lambda x: (_URGENCY.get(x['action'], 9), x['pnlPct'] if x['pnlPct'] is not None else 0))
    return out


def _build_buy_items(group: Dict, quote_map: Dict[str, Dict], allowed: tuple) -> tuple:
    """推荐 → (现在可买 buyNow, 稍后再看 deferred)

    止损价复用 services.risk_control.calc_risk_control（纯计算，无 I/O），
    与持仓页/个股决策卡同一口径，避免"同一只票两个页面两个止损"。
    """
    buy_now, deferred = [], []
    for rec in group.get('stocks') or []:
        bsp = rec.get('buySellPoints') or {}
        timing = bsp.get('buyTiming') or {}
        ttype = timing.get('type') or 'intraday'
        sym = _norm(rec.get('symbol'))
        q = quote_map.get(sym) or {}
        ref_buy = bsp.get('buy')
        try:
            ref_price = float(ref_buy) if ref_buy and str(ref_buy).replace('.', '').isdigit() else 0.0
        except (TypeError, ValueError):
            ref_price = 0.0
        live_price = float(q.get('priceRaw') or 0) or _to_float(rec.get('price'))
        base = ref_price or live_price
        rc = calc_risk_control(base, stock=q) if base > 0 else {}
        item = {
            'symbol': sym,
            'name': rec.get('name'),
            'price': live_price,
            'changePercent': float(q.get('changePercent') or 0) if q else _to_float(rec.get('changePercent')),
            'score': rec.get('score') or 0,
            'timingType': ttype,
            'timingLabel': timing.get('label') or '',
            'timingNote': timing.get('note') or '',
            'execHint': timing.get('execHint') or '',
            'buy': ref_price or None,
            'target': _to_float(bsp.get('sell')) or None,
            'stop': round(float(rc.get('stop_loss') or 0), 2) or None,
            'riskLevel': rc.get('risk_level') or '',
            'positionSuggest': rc.get('position_suggest') or '',
            'logic': (rec.get('reason') or '')[:180],
            'recDate': group.get('date'),
        }
        (buy_now if ttype in allowed else deferred).append(item)
    buy_now.sort(key=lambda x: -(x['score'] or 0))
    deferred.sort(key=lambda x: -(x['score'] or 0))
    return buy_now, deferred


def _to_float(v) -> float:
    try:
        f = float(v)
        return f if f == f else 0.0
    except (TypeError, ValueError):
        return 0.0


def _advice_text(advice) -> str:
    """portfolio.advice 是 {advice_pct,label,note} 结构体 —— 行动台只印一句话，
    直接把 dict 交给前端会把 JSON 显示在卡片上（2026-10-13 暗色截图里出现过）。"""
    if not advice:
        return ''
    if isinstance(advice, str):
        return advice
    pct = advice.get('advice_pct')
    label = advice.get('label') or ''
    if label and pct is not None:
        return f'建议总仓位 {label}（约 {pct}% 以内）'
    if label:
        return f'建议总仓位 {label}'
    return f'建议总仓位 {pct}%' if pct is not None else ''


def _build_watchlist(watch_symbols: List[Dict], quote_map: Dict[str, Dict]) -> List[Dict]:
    """自选异动：只保留"值得看一眼"的（|涨幅|≥3% 或放量或触均线），按波动幅度排序"""
    out = []
    for w in watch_symbols:
        sym = _norm(w.get('symbol'))
        q = quote_map.get(sym)
        if not q:
            continue
        pct = _to_float(q.get('changePercent'))
        vol_ratio = _to_float(q.get('volumeRatio'))
        flags = []
        if pct >= 5:
            flags.append('急拉')
        elif pct <= -5:
            flags.append('急跌')
        if vol_ratio >= 2:
            flags.append(f'量比{vol_ratio:.1f}')
        if abs(pct) < 3 and not flags:
            continue
        out.append({
            'symbol': sym,
            'name': q.get('name') or w.get('name'),
            'price': _to_float(q.get('priceRaw')),
            'changePercent': round(pct, 2),
            'volumeRatio': round(vol_ratio, 2),
            'flags': flags,
        })
    out.sort(key=lambda x: -abs(x['changePercent']))
    return out


def _build_headline(phase: str, diag: Dict, actions: List[Dict], buy_now: List[Dict],
                    alert: Optional[Dict], focus: Dict, minutes_to_next: Optional[int],
                    covered: bool) -> Dict:
    """一句话结论：打开页面第一眼要看到的东西"""
    urgent = [a for a in actions if a['urgent']]
    stop_hits = [a for a in urgent if a['action'] == 'stop_loss']
    risk = _to_float((alert or {}).get('riskScore'))

    if not covered:
        return {'level': 'warning',
                'text': '交易日历未覆盖本年度，时段判断可能不准',
                'sub': '请更新 services/trading_calendar.py 的休市安排后再依赖行动建议'}
    if stop_hits:
        return {'level': 'error',
                'text': f'{len(stop_hits)} 只持仓已跌破止损线，纪律优先',
                'sub': '先按建议处理持仓，再考虑任何新仓；不抢反弹、不摊平'}
    if risk >= 60:
        pred = (alert or {}).get('prediction') or {}
        return {'level': 'error',
                'text': pred.get('title') or '大盘高风险',
                'sub': pred.get('action') or '建议空仓观望'}
    if risk >= 40 and urgent:
        return {'level': 'warning',
                'text': f'大盘偏弱 + {len(urgent)} 只持仓触发信号，先守后攻',
                'sub': (urgent[0]['name'] or '') + '：' + (urgent[0]['detail'] or '')}

    if phase == 'auction':
        n = len(buy_now)
        return {'level': 'info',
                'text': f'竞价窗口：{n} 只昨日涨停票待竞价' if n else '竞价窗口：无待竞价标的',
                'sub': '9:20 后挂竞价单；高开 5% 以上不追，防一字/T字诱多'}
    if phase == 'tail':
        left = f'，还剩约 {minutes_to_next} 分钟' if minutes_to_next else ''
        return {'level': 'info' if buy_now else 'muted',
                'text': f'尾盘确认窗口{left}：今日量价形态已定，该进的进',
                'sub': '只买标记"尾盘买入"的票，避开盘中诱多；14:57 后不再新开仓'}
    if phase == 'morning':
        return {'level': 'info' if buy_now else 'muted',
                'text': f'早盘：{len(buy_now)} 只到时机' if buy_now else '早盘：暂无到时机的新仓',
                'sub': '强势票 10:00 前决策，回踩不破均价线再买；10 点后不再新开早盘仓'}
    if phase == 'mid':
        return {'level': 'muted',
                'text': '盘中观察期：按计划等回踩，不追高',
                'sub': '高波动票留到 14:30 后确认；先处理持仓信号'}
    if phase == 'lunch':
        return {'level': 'muted', 'text': '午间休市：13:00 恢复交易',
                'sub': '趁现在过一遍下午要盯的票，别在下午开盘才想起来'}
    if phase in ('post', 'closed'):
        n = len(focus.get('items') or [])
        return {'level': 'info' if n else 'muted',
                'text': f'盘后：明日计划已列出 {n} 只' if n else '盘后：今日收评尚未生成',
                'sub': '收评 15:10 左右产出；先处理持仓，再定明天的挂单'}
    if phase == 'pre':
        return {'level': 'muted', 'text': '盘前准备：核对持仓与今日观察名单',
                'sub': '9:15 进入集合竞价窗口'}
    return {'level': 'muted', 'text': '当前无待办', 'sub': ''}


async def build_action_desk(user_id: int = 1, phase_override: str = '') -> Dict:
    """组装行动台数据。user_id 决定看到谁的持仓/自选（2026-10-13 多用户）。"""
    info = get_session_phase()
    phase = info['phase']
    if phase_override and phase_override in PHASE_LABELS:
        phase = phase_override          # 仅 dev 截图/联调用：?phase=tail

    cache_key = f'{user_id}:{phase}'
    now_ts = time.time()
    hit = _DESK_CACHE.get(cache_key)
    if hit and (now_ts - hit['ts']) < _DESK_CACHE_TTL:
        return hit['data']

    # 1) 纯本地/文件读取（无网络）
    alert = load_market_alert()
    group = load_recommendation_group()
    focus = load_latest_focus_plan()
    trend_fresh = load_trend_scan_freshness()
    watch_symbols = _load_watch_symbols(user_id)

    # 2) 网络读取：持仓诊断（内部自带 5s 缓存行情）与指数/个股批量行情并行。
    #    每路都有独立超时：任何一路慢/挂都不能把整个首页拖住，降级为"该区块暂缺"。
    live = phase in _LIVE_PHASES
    symbols_needed = ([_norm(s.get('symbol')) for s in (group.get('stocks') or [])]
                      + [_norm(w.get('symbol')) for w in watch_symbols])
    degraded: List[str] = []

    async def _immediate(value=None):
        return value

    async def _guarded(task, budget: float, name: str, default):
        try:
            return await asyncio.wait_for(task, budget)
        except asyncio.TimeoutError:
            print(f'[行动台] {name} 超时 {budget}s，本区块降级')
            degraded.append(name)
            return default
        except Exception as e:
            print(f'[行动台] {name} 失败: {e}')
            degraded.append(name)
            return default

    # 三路并发发起（ensure_future 立即排程），再各自带超时地取回，互不拖累
    diag_task = asyncio.ensure_future(get_portfolio_diagnosis(user_id=user_id))
    quotes_task = asyncio.ensure_future(_batch_quotes(symbols_needed) if live else _immediate({}))
    crash_task = asyncio.ensure_future(check_market_crash() if live else _immediate(None))

    diag = await _guarded(diag_task, 3.0, 'positions',
                          {'positions': [], 'envAdvice': None, 'has_positions': False})
    quote_map = await _guarded(quotes_task, 2.0, 'quotes', {}) if live else {}
    crash = await _guarded(crash_task, 2.0, 'indexes', None) if live else None
    if not isinstance(quote_map, dict):
        quote_map = {}

    actions = _build_position_actions(diag)
    allowed = _allowed_timings(phase)
    # 竞价/盘前看的是上一次生成的推荐（对标记"次日竞价"的票而言即昨日），盘中看当日
    buy_now, deferred = _build_buy_items(group, quote_map, allowed)
    watch_alerts = _build_watchlist(watch_symbols, quote_map) if live else []
    headline = _build_headline(phase, diag, actions, buy_now, alert, focus,
                               info.get('minutesToNext'), info.get('calendarCovered', True))

    env = diag.get('envAdvice') or {}
    data = {
        'asOf': datetime.now().isoformat(timespec='seconds'),
        'phase': phase,
        'phaseLabel': PHASE_LABELS.get(phase, phase),
        'isTradingDay': info['isTradingDay'],
        'calendarCovered': info['calendarCovered'],
        'nextPhaseAt': info['nextPhaseAt'],
        'minutesToNext': info['minutesToNext'],
        'nextPollMs': _PHASE_POLL_MS.get(phase, 120000),
        'sections': _PHASE_SECTIONS.get(phase, ['watchlist']),
        'degraded': degraded,
        'headline': headline,
        'market': {
            'riskScore': _to_float((alert or {}).get('riskScore')),
            'savedAt': (alert or {}).get('savedAt') or '',
            'prediction': (alert or {}).get('prediction') or {},
            'status': (crash or {}).get('marketStatus') or '',
            'suggestion': (crash or {}).get('suggestion') or {},
            'indexes': [{'name': i.get('name'), 'price': i.get('price'),
                         'changePercent': i.get('changePercent')}
                        for i in ((crash or {}).get('indexes') or [])],
        },
        'tradeWindow': {
            'level': env.get('level'), 'title': env.get('title'), 'detail': env.get('detail'),
            'pct': env.get('pct'), 'marketChange': env.get('marketChange'),
            'urgentSells': env.get('urgentSells'),
            'suggestedTotal': _advice_text((diag.get('portfolio') or {}).get('advice')),
            'totalPnlPct': (diag.get('portfolio') or {}).get('total_pnl_pct'),
            'warnings': (diag.get('portfolio') or {}).get('warnings') or [],
        } if env else {},
        'positionActions': actions,
        'holdCount': sum(1 for p in (diag.get('positions') or [])
                         if p.get('action') not in _ACTIONABLE_ACTIONS),
        'buyNow': buy_now,
        'deferredBuys': [{'symbol': d['symbol'], 'name': d['name'],
                          'timingLabel': d['timingLabel'], 'score': d['score']}
                         for d in deferred],
        'tomorrowPlan': [{'symbol': _norm(f.get('code') or f.get('symbol')),
                          'name': f.get('name'),
                          'score': f.get('score'),
                          'timingLabel': (f.get('buyTiming') or {}).get('label'),
                          'timingType': (f.get('buyTiming') or {}).get('type'),
                          'plan': f.get('tomorrowPlan') or {}}
                         for f in (focus.get('items') or [])],
        'watchlistAlerts': watch_alerts,
        'watchlistCount': len(watch_symbols),
        'freshness': {
            'recommendation': group.get('date') or '',
            'recommendationToday': group.get('date') == _today_str(),
            'trend': trend_fresh.get('date') or '',
            'trendScanTime': trend_fresh.get('scanTime') or '',
            'review': focus.get('date') or '',
            'reviewToday': focus.get('date') == _today_str(),
        },
    }
    _DESK_CACHE[cache_key] = {'ts': now_ts, 'data': data}
    return data


def _load_watch_symbols(user_id: int = 1) -> List[Dict]:
    """自选清单 = 热门股 chips + 趋势自选池（按账号隔离，2026-10-13）"""
    out, seen = [], set()
    try:
        for row in load_hot_stock_buttons(user_id=user_id):
            sym = _norm(row.get('code'))
            if sym and sym not in seen:
                seen.add(sym)
                out.append({'symbol': sym, 'name': row.get('name'), 'source': 'hot'})
    except Exception as e:
        print(f'[行动台] 读取自选股失败: {e}')
    try:
        for sym in load_user_scan_pool(user_id=user_id):
            sym = _norm(sym)
            if sym and sym not in seen:
                seen.add(sym)
                out.append({'symbol': sym, 'name': '', 'source': 'pool'})
    except Exception as e:
        print(f'[行动台] 读取自选池失败: {e}')
    return out
