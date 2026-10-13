"""position.py - 用户持仓管理与仓位配置建议（2026-10-12 新增）

回答用户四个问题：
  1. 实际看哪个 Tab？——智能推荐做主决策，股票筛选做发现，本模块管已买入的票
  2. 什么时候挂单？——四时段规则见 buyTiming（竞价/早盘/回踩/尾盘）
  3. 买卖点是什么？——买点=买入参考价低吸；卖点=目标止盈/止损价/移动止盈三线
  4. 仓位怎么配？——本模块按大盘环境给总仓位建议，按个股状态给单票动作

诊断动作优先级（从高到低，命中即停）：
  stop_loss   现价已跌破止损价（基于成本、按当日振幅缩放）→ 立即止损，不抱有幻想
  take_profit 现价已达目标止盈价且当日冲高回落/烂板 → 落袋为安
  reduce      连跌≥3天 / 近30天推荐亏损黑名单 / 今日炸板 → 减仓回避
  trailing    浮盈≥5%/10% → 止损线被动上移（保本/锁盈），只输出新止损线不改动作
  hold        未触任何风控线 → 持有，守住止损价
  watch       浮亏但未触止损 → 持有观察

组合层：总仓位建议随大盘涨跌幅分档（弱市低仓、强市高档），单票超配/过度分散告警。
"""
import asyncio
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional

from app.config import KLINE_HISTORY_ENABLED, KLINE_HISTORY_DB_PATH, TREND_LIMIT_UP_THRESHOLD
from db.database import load_user_positions, load_trend_scan_results, load_daily_recommendations, load_daily_market_reviews
from services.stock import fetch_with_retry, parse_tencent_batch_data
from services.strategy import calc_stock_score_v2
from services.risk_control import calc_risk_control
from services.recommend import calc_intraday_trajectory

# 止损幅度按当日振幅缩放（与 risk_control 同思路）：振幅越大容忍越宽
_STOP_PCT_BY_AMPLITUDE = [(8, 0.12), (5, 0.10), (3, 0.08), (0, 0.06)]
# 目标止盈幅度按评分分档（大赚小亏：止盈目标 ≥ 2倍止损空间）
_TARGET_PCT_BY_SCORE = [(75, 0.12), (65, 0.09), (0, 0.06)]


def _stop_pct(amplitude: float) -> float:
    for thr, pct in _STOP_PCT_BY_AMPLITUDE:
        if amplitude >= thr:
            return pct
    return 0.06


def _target_pct(score: float) -> float:
    for thr, pct in _TARGET_PCT_BY_SCORE:
        if score >= thr:
            return pct
    return 0.06


def calc_trailing_stop(cost: float, price: float) -> Dict:
    """移动止盈线（大赚小亏核心：让盈利单的止损线只上不下）"""
    if cost <= 0 or price <= 0:
        return {'enabled': False}
    pnl = (price - cost) / cost * 100
    if pnl >= 10:
        return {'enabled': True, 'line': round(cost * 1.05, 2), 'level': 'lock',
                'msg': f'浮盈{pnl:.1f}%≥10%，止损上移至成本+5%锁定利润'}
    if pnl >= 5:
        return {'enabled': True, 'line': round(cost * 1.00, 2), 'level': 'breakeven',
                'msg': f'浮盈{pnl:.1f}%≥5%，止损上移至成本价保本'}
    return {'enabled': False, 'pnl': pnl}


def _consecutive_down_local(symbol: str, today_price: Optional[float] = None) -> int:
    """本地K线库算连跌天数（与趋势徽章同口径，数据延迟时补实时价）"""
    if not KLINE_HISTORY_ENABLED:
        return 0
    code = symbol.lower().replace('sh', '').replace('sz', '').replace('bj', '')
    try:
        conn = sqlite3.connect(f'file:{KLINE_HISTORY_DB_PATH}?mode=ro', uri=True)
        cur = conn.cursor()
        cur.execute("SELECT date, close FROM stock_daily WHERE symbol=? ORDER BY date DESC LIMIT 10", (code,))
        rows = cur.fetchall()
        conn.close()
        if len(rows) < 2:
            return 0
        # 追加/刷新今天bar
        today_str = datetime.now().strftime('%Y-%m-%d')
        closes = [r[1] for r in rows]
        dates = [r[0] for r in rows]
        if today_price and today_price > 0:
            if dates[0] == today_str:
                closes[0] = today_price
            else:
                closes.insert(0, today_price)
        closes = closes[::-1]  # 升序
        cd = 0
        for i in range(len(closes) - 1, 0, -1):
            if closes[i] < closes[i - 1]:
                cd += 1
            else:
                break
        return cd
    except Exception:
        return 0


def _in_active_pools(symbol: str) -> Dict:
    """是否仍在今日推荐/趋势池（拿着推荐过的票，看它还"红"不"红"）"""
    sym = symbol.lower()
    result = {'in_today_rec': False, 'in_trend_pool': False}
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        daily = load_daily_recommendations()
        for rec in daily:
            if rec.get('date') == today:
                for s in rec.get('daily_recommendations', []):
                    if (s.get('symbol') or '').lower() == sym:
                        result['in_today_rec'] = True
        trend = load_trend_scan_results()
        if trend.get('date') == today:
            for s in trend.get('stocks', []):
                if (s.get('symbol') or '').lower() == sym:
                    result['in_trend_pool'] = True
    except Exception:
        pass
    return result


def diagnose_position(pos: Dict, quote: Optional[Dict], market_change: float,
                      loser_symbols: set) -> Dict:
    """单票诊断：输出动作、止损/止盈价、原因"""
    sym = (pos.get('symbol') or '').lower()
    name = pos.get('name') or sym
    cost = float(pos.get('cost_price') or 0)
    shares = int(pos.get('shares') or 0)
    out = {'symbol': sym, 'name': name, 'cost': cost, 'shares': shares,
           'price': 0, 'pnl_pct': None, 'pnl_amt': None, 'market_value': 0,
           'action': 'watch', 'action_label': '持有观察', 'stop_price': None,
           'stop_pct': None, 'target_price': None, 'trailing': {'enabled': False},
           'detail': '', 'flags': []}
    if cost <= 0 or shares <= 0:
        out['detail'] = '持仓信息不完整（成本价/股数缺失）'
        return out

    if not quote:
        out['detail'] = '行情获取失败，无法诊断（稍后重试）'
        return out

    price = float(quote.get('priceRaw') or quote.get('price') or 0)
    if price <= 0:
        out['detail'] = '行情获取失败，无法诊断（稍后重试）'
        return out

    amplitude = float(quote.get('amplitude', 0) or 0)
    out['price'] = price
    out['pnl_pct'] = round((price - cost) / cost * 100, 2)
    out['pnl_amt'] = round((price - cost) * shares, 2)
    out['market_value'] = round(price * shares, 2)

    # 简版评分 + 风控止损（基于成本价）；2026-10-13 起补齐 trend_result 走死猫反弹门控
    try:
        from services.trend import compute_trend_result
        tr = compute_trend_result(sym, price)
        score_result = calc_stock_score_v2(quote, market_change=market_change, trend_result=tr)
        score = score_result.get('total', 0)
        rc = calc_risk_control(cost, score_result=score_result, stock=quote)
        stop_price = float(rc.get('stop_loss') or 0) or round(cost * (1 - _stop_pct(amplitude)), 2)
        risk_level = rc.get('risk_level', '')
    except Exception:
        score, stop_price, risk_level = 0, round(cost * (1 - _stop_pct(amplitude)), 2), ''
    out['score'] = score
    out['stop_price'] = round(stop_price, 2)
    out['stop_pct'] = round((stop_price - cost) / cost * 100, 1)
    out['target_price'] = round(cost * (1 + _target_pct(score)), 2)

    # 信号收集
    flags = []
    traj = calc_intraday_trajectory(quote)
    if traj['valid']:
        if traj['board_break']:
            flags.append('今日炸板')
        if traj['big_drawdown']:
            flags.append('冲高回落')
    cd = _consecutive_down_local(sym, price)
    if cd >= 2:
        flags.append(f'连跌{cd}天')
    if sym in loser_symbols:
        flags.append('近30天推荐亏损')
    pools = _in_active_pools(sym)
    if pools['in_today_rec']:
        flags.append('今日仍在推荐')
    if pools['in_trend_pool']:
        flags.append('趋势池在列')
    out['flags'] = flags

    # 动作判定（优先级从高到低）
    trailing = calc_trailing_stop(cost, price)
    out['trailing'] = trailing

    if price <= stop_price:
        out['action'] = 'stop_loss'
        out['action_label'] = '立即止损'
        out['detail'] = (f'现价{price:.2f}已跌破止损价{stop_price:.2f}'
                         f'（成本{cost:.2f}，{out["stop_pct"]}%），纪律优先：先走再说，不抢反弹')
    elif price >= out['target_price'] and (traj['board_break'] or traj['big_drawdown'] or risk_level == 'high'):
        out['action'] = 'take_profit'
        out['action_label'] = '止盈落袋'
        out['detail'] = (f'浮盈{out["pnl_pct"]:.1f}%已达目标{out["target_price"]:.2f}，'
                         f'且{"炸板/冲高回落" if (traj["board_break"] or traj["big_drawdown"]) else "高风险"}，建议兑现')
    elif traj['board_break'] or sym in loser_symbols or cd >= 3:
        reasons = []
        if traj['board_break']:
            reasons.append('今日炸板')
        if sym in loser_symbols:
            reasons.append('近30天推荐亏损')
        if cd >= 3:
            reasons.append(f'连跌{cd}天')
        out['action'] = 'reduce'
        out['action_label'] = '减仓回避'
        out['detail'] = f'{"、".join(reasons)}，形态已坏，建议至少减半，剩余部分严格止损{stop_price:.2f}'
    elif trailing['enabled']:
        out['action'] = 'hold_trailing'
        out['action_label'] = '持有（止损上移）'
        out['detail'] = f'{trailing["msg"]}，新止损线{trailing["line"]:.2f}'
    elif out['pnl_pct'] < 0:
        out['action'] = 'watch'
        out['action_label'] = '持有观察'
        out['detail'] = f'浮亏{out["pnl_pct"]:.1f}%未触止损{stop_price:.2f}，可持有观察；若大盘继续走弱，止损线上移保本'
    else:
        out['action'] = 'hold'
        out['action_label'] = '持有'
        detail = f'未触止损{stop_price:.2f}，目标{out["target_price"]:.2f}，让利润奔跑'
        if pools['in_today_rec'] or pools['in_trend_pool']:
            detail += '；该股今日仍在推荐/趋势池，可多一分耐心'
        out['detail'] = detail
    return out


def _total_position_advice(market_change: float) -> Dict:
    """总仓位建议（随大盘环境分档）"""
    if market_change <= -1.0:
        pct, label, note = 30, '三成以下', '大盘明显走弱，防守优先，只留最强票'
    elif market_change <= -0.3:
        pct, label, note = 50, '五成以下', '大盘偏弱，降低仓位，等尾盘或明日再说'
    elif market_change < 0.5:
        pct, label, note = 70, '七成以下', '大盘震荡，结构性参与，单票不超两成'
    else:
        pct, label, note = 90, '九成以下', '大盘强势，可积极但留一成现金应对波动'
    return {'advice_pct': pct, 'label': label, 'note': note}


# ========== 交易环境提示（2026-10-12，用户要求放进持仓页及时可见） ==========
# 回答"今天适不适合交易"：大盘涨跌幅定基础档 → 开盘形态修正（低开高走=韧性升档/
# 高开低走=诱多降档）→ 情绪周期修正 → 持仓紧急信号联动。
# 数据依据：132条推荐复盘显示大跌日推荐T+1胜率反而最高(82%)——所以不是"跌就不买"，
# 而是分级指引：弱市降仓+只低吸+持仓反弹减仓，极端环境才空仓。
_ENV_LEVELS = {
    'aggressive': {'title': '积极做多', 'emoji': '🔴', 'pct': 80,
                   'detail': '大盘强势、情绪配合，可积极交易；新仓按推荐票的时机徽章执行，单票≤20%'},
    'normal': {'title': '正常参与', 'emoji': '🔵', 'pct': 60,
               'detail': '大盘平稳，正常参与；低吸为主不追高，单票≤20%，总仓位≤60%'},
    'cautious': {'title': '降低仓位', 'emoji': '🟠', 'pct': 40,
                 'detail': '大盘偏弱，降低仓位；只低吸不追高，优先尾盘票，总仓位≤40%'},
    'defensive': {'title': '防守为主', 'emoji': '🟤', 'pct': 20,
                  'detail': '大盘走弱，防守优先；持仓有反弹减仓、不急于新开仓，新仓≤20%'},
    'avoid': {'title': '空仓观望', 'emoji': '⛔', 'pct': 0,
              'detail': '大盘明显弱势/情绪冰点，不适合买入；持仓反弹减仓，空仓者耐心等待'},
}
_ENV_ORDER = ['avoid', 'defensive', 'cautious', 'normal', 'aggressive']


def _shift_level(level: str, step: int) -> str:
    idx = _ENV_ORDER.index(level)
    return _ENV_ORDER[max(0, min(len(_ENV_ORDER) - 1, idx + step))]


def _load_today_sentiment() -> Optional[Dict]:
    """今日收评的短线情绪周期（cycle/cycleScore/advice），未生成收评则 None"""
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        daily = load_daily_market_reviews()
        for rec in daily:
            if rec.get('date') == today:
                return rec.get('sentimentCycle') or None
    except Exception:
        pass
    return None


def calc_trading_advice(market_change: float, open_change: Optional[float] = None,
                        sentiment: Optional[Dict] = None,
                        urgent_sell_count: int = 0) -> Dict:
    """今日/明日交易环境建议（持仓页横幅，60s 刷新及时可见）

    Args:
        market_change: 大盘涨跌幅%（上证实时）
        open_change: 大盘开盘涨跌幅%（今开/昨收-1），无则 None
        sentiment: 今日收评情绪周期 dict（cycle/cycleScore），无则 None
        urgent_sell_count: 持仓中触发止损/减仓信号的只数

    Returns:
        {'level','title','emoji','pct','detail','marketChange','openChange',
         'notes':[...], 'urgentSells':N, 'sentiment':{...}|None}
    """
    # 1. 基础档：大盘涨跌幅
    if market_change >= 0.8:
        level = 'aggressive'
    elif market_change >= -0.5:
        level = 'normal'
    elif market_change >= -1.0:
        level = 'cautious'
    elif market_change >= -2.5:
        level = 'defensive'
    else:
        level = 'avoid'

    notes = []
    # 2. 开盘形态修正（结合"当天开盘情况"）
    if open_change is not None:
        if open_change <= -0.5 and market_change >= open_change + 0.5:
            level = _shift_level(level, 1)
            notes.append(f'低开高走（开盘{open_change:+.1f}%→现{market_change:+.1f}%），日内有韧性')
        elif open_change >= 0.5 and market_change <= open_change - 0.5:
            level = _shift_level(level, -1)
            notes.append(f'高开低走（开盘{open_change:+.1f}%→现{market_change:+.1f}%），谨防诱多')

    # 3. 情绪周期修正（今日收评五维情绪分）
    if sentiment:
        cs = sentiment.get('cycleScore')
        if isinstance(cs, (int, float)):
            if cs < 30:
                level = _shift_level(level, -1)
                notes.append(f"短线情绪{sentiment.get('cycle', '冰点')}期（{cs:.0f}分），亏钱效应扩散")
            elif cs >= 70:
                level = _shift_level(level, 1)
                notes.append(f"短线情绪{sentiment.get('cycle', '高潮')}期（{cs:.0f}分），赚钱效应好")

    cfg = _ENV_LEVELS[level]
    detail = cfg['detail']
    # 4. 持仓联动：有紧急卖出信号时，环境提示让位于持仓处理（不再附加"无异常信号"）
    if urgent_sell_count > 0:
        detail += f'；你有 {urgent_sell_count} 只持仓触发止损/减仓信号，优先处理持仓再考虑新仓'
    elif not notes and level in ('normal', 'aggressive'):
        detail += '；无异常信号，按计划执行'

    return {
        'level': level,
        'title': cfg['title'],
        'emoji': cfg['emoji'],
        'pct': cfg['pct'],
        'detail': detail,
        'marketChange': round(market_change, 2),
        'openChange': round(open_change, 2) if open_change is not None else None,
        'notes': notes,
        'urgentSells': urgent_sell_count,
        'sentiment': {'cycle': sentiment.get('cycle'), 'cycleScore': sentiment.get('cycleScore')} if sentiment else None,
    }


async def get_portfolio_diagnosis(user_id: int = 1) -> Dict:
    """持仓组合诊断：行情+盈亏+单票动作+组合建议+交易环境提示（前端持仓Tab主数据源）

    user_id：按用户隔离持仓（2026-10-12 多用户账号），缺省 1 兼容旧调用
    """
    positions = load_user_positions(user_id=user_id)

    async def _fetch_market():
        """大盘实时涨跌 + 开盘涨跌（今开/昨收，用于开盘形态修正）"""
        try:
            data = await fetch_with_retry('sh000001')
            parsed = parse_tencent_batch_data(data)
            if parsed:
                q = parsed[0]
                change = float(q.get('changePercent', 0) or 0)
                open_change = None
                try:
                    open_px = float(q.get('open') or 0)
                    prev_close = float(q.get('prevCloseRaw') or 0)
                    if open_px > 0 and prev_close > 0:
                        open_change = (open_px / prev_close - 1) * 100
                except (TypeError, ValueError):
                    pass
                return change, open_change
        except Exception:
            pass
        return 0.0, None

    # 无持仓也给出交易环境提示（用户核心诉求：及时看到今天适不适合交易）
    if not positions:
        market_change, market_open_change = await _fetch_market()
        env_advice = calc_trading_advice(market_change, open_change=market_open_change,
                                         sentiment=_load_today_sentiment())
        return {'has_positions': False, 'market_change': round(market_change, 2),
                'positions': [], 'portfolio': None, 'envAdvice': env_advice}

    symbols = [p['symbol'].lower() for p in positions]

    async def _fetch_quotes():
        try:
            data = await fetch_with_retry(','.join(symbols))
            return {p['symbol'].lower(): p for p in parse_tencent_batch_data(data)}
        except Exception as e:
            print(f'[持仓诊断] 行情拉取失败: {e}')
            return {}

    (market_change, market_open_change), quote_map = await asyncio.gather(_fetch_market(), _fetch_quotes())

    # 黑名单（复用推荐复盘）
    try:
        from services.recommend_review import get_recent_loser_symbols
        loser_symbols = get_recent_loser_symbols()
    except Exception:
        loser_symbols = set()

    diag_list = [diagnose_position(p, quote_map.get(p['symbol'].lower()),
                                   market_change, loser_symbols)
                 for p in positions]

    # 交易环境提示（持仓页横幅，及时可见；无持仓时也给出）
    urgent_sell_count = sum(1 for d in diag_list if d.get('action') in ('stop_loss', 'reduce', 'take_profit'))
    env_advice = calc_trading_advice(market_change, open_change=market_open_change,
                                     sentiment=_load_today_sentiment(),
                                     urgent_sell_count=urgent_sell_count)

    total_cost = sum(d['cost'] * d['shares'] for d in diag_list)
    total_value = sum(d['market_value'] for d in diag_list)
    total_pnl = total_value - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0

    warnings = []
    for d in diag_list:
        if total_value > 0 and d['market_value'] / total_value > 0.3:
            warnings.append(f'{d["name"]}占总市值{d["market_value"]/total_value*100:.0f}%，单票超配（建议≤25%）')
    if len(diag_list) > 5:
        warnings.append(f'持仓{len(diag_list)}只较多，过度分散会稀释精力与收益')

    advice = _total_position_advice(market_change)
    action_counts = {}
    for d in diag_list:
        action_counts[d['action']] = action_counts.get(d['action'], 0) + 1

    return {
        'has_positions': True,
        'market_change': round(market_change, 2),
        'positions': diag_list,
        'portfolio': {
            'total_cost': round(total_cost, 2),
            'total_value': round(total_value, 2),
            'total_pnl': round(total_pnl, 2),
            'total_pnl_pct': round(total_pnl_pct, 2),
            'advice': advice,
            'warnings': warnings,
            'action_counts': action_counts,
        },
        'envAdvice': env_advice,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
