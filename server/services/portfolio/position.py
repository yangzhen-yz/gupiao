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

from app.config import (KLINE_HISTORY_ENABLED, KLINE_HISTORY_DB_PATH,
                        TREND_LIMIT_UP_THRESHOLD, CYCLE_RULES)
from db.database import load_user_positions, load_trend_scan_results, load_daily_recommendations, load_daily_market_reviews, get_user_capital, get_user_realized_pnl
from services.data.stock import fetch_with_retry, parse_tencent_batch_data
from services.strategy.strategy import calc_stock_score_v2
from services.strategy.risk_control import calc_risk_control
from services.pick.recommend import calc_intraday_trajectory

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


# ========== 冲高回落告警（2026-10-14，用户实盘暴露的盲区） ==========
# 背景：yz 账户的吉华集团当日从 +5.3% 砸到 -5.3%（自高点回落 10.1%、现价在均价线下方 4%），
# 但因为没跌破"成本 -5%"那条止损线，diagnose_position 一路落到 watch 分支；
# 而行动台只收 stop_loss / reduce / take_profit —— 于是一只把全天利润吐光的票在系统里一声不响。
# recommend 侧其实早就算着 big_drawdown（回落≥8%），但那个只用来"不推荐买入"，从没接到持仓侧。
GIVEBACK_MIN_HIGH_PCT = 3.0    # 当日确实冲高过（相对昨收），否则只是平盘震荡
GIVEBACK_MIN_DRAWDOWN = 5.0    # 自日内高点回落幅度(%)，比买入侧的 8% 提前一档提醒
GIVEBACK_KEEP_RATIO = 0.5      # 涨幅保住不到高点的一半 → 视为"没守住"


def calc_giveback(traj: Dict, quote: Dict) -> Dict:
    """冲高回落判定：涨上去过 + 掉回均价线下方 + 涨幅基本没保住。

    四个条件全满足才算，避免把强势票的正常整理误报成风险（比如从 +12% 整理到 +9% 的票，
    回落幅度够但仍在均价线上方，不该报警）。
    """
    empty = {'hit': False, 'high_pct': 0.0, 'drawdown': 0.0, 'avg_dev': 0.0, 'now_pct': 0.0}
    if not traj or not traj.get('valid'):
        return empty

    def _f(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    high_pct = _f(traj.get('high_pct'))
    drawdown = _f(traj.get('drawdown'))
    avg_dev = _f(quote.get('avgPriceDeviation'))
    now_pct = _f(quote.get('changePercent'))
    hit = (high_pct >= GIVEBACK_MIN_HIGH_PCT
           and drawdown >= GIVEBACK_MIN_DRAWDOWN
           and avg_dev < 0
           and now_pct <= high_pct * GIVEBACK_KEEP_RATIO)
    return {'hit': hit, 'high_pct': round(high_pct, 2), 'drawdown': round(drawdown, 2),
            'avg_dev': round(avg_dev, 2), 'now_pct': round(now_pct, 2)}


def diagnose_position(pos: Dict, quote: Optional[Dict], market_change: float,
                      loser_symbols: set) -> Dict:
    """单票诊断：输出动作、止损/止盈价、原因"""
    sym = (pos.get('symbol') or '').lower()
    name = pos.get('name') or sym
    cost = float(pos.get('cost_price') or 0)
    shares = int(pos.get('shares') or 0)
    # T+1：今天买入的票今天卖不掉。买入日期缺失的老记录按"可卖"处理，
    # 免得因为一条空字段把该发的止损提醒吞掉。
    buy_date = str(pos.get('buy_date') or '')[:10]
    sellable = (not buy_date) or buy_date < datetime.now().strftime('%Y-%m-%d')
    out = {'symbol': sym, 'name': name, 'cost': cost, 'shares': shares,
           'price': 0, 'pnl_pct': None, 'pnl_amt': None, 'market_value': 0,
           'action': 'watch', 'action_label': '持有观察', 'stop_price': None,
           'stop_pct': None, 'target_price': None, 'trailing': {'enabled': False},
           'detail': '', 'flags': [], 'buy_date': buy_date, 'sellable': sellable,
           'locked_reason': '' if sellable else f'今日（{buy_date}）买入，T+1 明日才可卖'}
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
    tr = None
    try:
        from services.strategy.trend import compute_trend_result
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
    gb = calc_giveback(traj, quote)
    out['giveback'] = gb

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
    elif gb['hit']:
        # 排在炸板之后：同样是回落，炸板更严重，那条走"减仓回避"
        out['action'] = 'giveback'
        out['action_label'] = '冲高回落'
        out['detail'] = (f'日内最高{gb["high_pct"]:+.1f}%已回吐至{gb["now_pct"]:+.1f}%'
                         f'（自高点回落{gb["drawdown"]:.1f}%，现价在均价线下方{abs(gb["avg_dev"]):.1f}%），'
                         f'浮盈{out["pnl_pct"]:+.1f}%、未破止损{stop_price:.2f}；'
                         f'尾盘收不回均价线就先减半，别等它变成止损')
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

    # 统一卖出计划（2026-10-22）：三机制并列。默认只挂 exit_plan 展示，
    # 不改变上述任何动作；只有 exit_rules_live_enabled=True 才接管。
    _qchg = float(quote.get('changePercent')) if quote and quote.get('changePercent') is not None else None
    try:
        from services.portfolio.exit_rules import plan_exit, gather_sell_context
        # 用户4条卖点所需真实数据（best-effort；缺数据由 exit_rules 侧标记 unavailable，不误触发）
        _ctx = gather_sell_context(sym, quote)
        plan = plan_exit(symbol=sym, cost=cost, price=price, buy_date=buy_date,
                         stop_price=stop_price, target_price=out['target_price'],
                         trailing=trailing, trend_result=tr,
                         day_gain_pct=_qchg,
                         now_hm=datetime.now().strftime('%H:%M'),
                         market_down=(market_change < 0),
                         yest_limit_up=_ctx.get('yest_limit_up', False),
                         vol_today=_ctx.get('vol_today'),
                         vol_yest=_ctx.get('vol_yest'),
                         limit_down=(_qchg is not None and _qchg <= -9.5),
                         sector_down=_ctx.get('sector_down'))
        out['exit_plan'] = plan
        if plan.get('enabled') and plan.get('live') and plan.get('action') == 'exit' \
                and out.get('action') != 'stop_loss':
            # 映射到现有推送动作集合：固定止损=stop_loss，逻辑/时间止损=reduce；
            # 不新增动作 key，避免 position_watch / action_desk 的集合漏接。
            out['action'] = 'stop_loss' if plan.get('hit') == 'fixed' else 'reduce'
            out['action_label'] = plan.get('action_label') or '离场'
            out['detail'] = f"{plan.get('reason')}（exit_rules 实盘接管）"
    except Exception as e:
        out['exit_plan'] = {'enabled': False, 'why': f'{type(e).__name__}: {e}'}
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


async def _resolve_sentiment() -> Optional[Dict]:
    """环境档位用的情绪源（2026-10-14）：盘中五维优先，收评值降级兜底。

    改造前这里只有收评值 —— 也就是 15:10 之前拿的是**昨天**的情绪，
    今天开盘后情绪怎么变，持仓页的"今天适不适合交易"完全不知道。
    """
    try:
        from services.strategy.sentiment import get_sentiment
        s = await get_sentiment()
        if s.get('cycle') and s.get('cycle') != '未知':
            return s
    except Exception as e:
        print(f'[持仓诊断] 盘中情绪不可用，退回收评口径: {e}')
    return _load_today_sentiment()


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

    # 3. 情绪周期定档（2026-10-14：从"±1 档修正"升级为"周期直接定档 + 结构信号再降档"）
    #    旧逻辑只看情绪分（<30 降一档 / ≥70 升一档），问题是分数相同的两天结构可能完全相反：
    #    情绪分 55 但昨日高位股全断板，和情绪分 55 但梯队完整，操作强度不该一样。
    if sentiment:
        cycle = sentiment.get('cycle') or ''
        stale = bool(sentiment.get('stale'))
        rule = CYCLE_RULES.get(cycle) or {}
        try:
            from services.strategy.sentiment import cycle_gate
            gate = cycle_gate(sentiment)
        except Exception:
            gate = {'envShift': 0, 'vetoEnabled': False}
        if not stale and cycle and cycle != '未知':
            # 降档只在"确认成立"后执行（过 10:00 + ≥2 个弱信号同向）；
            # 未确认时文案照说，档位不动 —— 9:35 的假冰点不该把一天的仓位上限打掉。
            shift = int(gate.get('envShift') or 0)
            if shift:
                level = _shift_level(level, shift)
                notes.append(f'短线情绪{cycle}期（{sentiment.get("cycleScore")}分）：'
                             f'{rule.get("note") or sentiment.get("advice") or ""}')
            struct = [w for w in (sentiment.get('weakSignals') or [])
                      if '梯队崩塌' in w or '龙头失势' in w]
            if struct:
                if gate.get('vetoEnabled'):
                    level = _shift_level(level, -1)
                notes.append(struct[0])
            # 大盘涨但题材亏钱 —— 散户最常见的亏法就是"看大盘买个股"
            if cycle in ('退潮', '冰点') and market_change >= 0:
                notes.append(f'大盘{market_change:+.1f}%但短线情绪{cycle}：'
                             f'指数涨不等于个股有钱赚，今天只减不加')
        else:
            # 收评口径/降级值：沿用旧的情绪分修正，只降不升（数据不是今天的，不给加仓理由）
            cs = sentiment.get('cycleScore')
            if isinstance(cs, (int, float)):
                if cs < 30:
                    level = _shift_level(level, -1)
                    notes.append(f"短线情绪{sentiment.get('cycle', '冰点')}期（{cs:.0f}分），亏钱效应扩散")
                elif cs >= 70:
                    notes.append(f"情绪分{cs:.0f}为昨日口径，今日未复核，不作为加仓理由")

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
    # 初始本金：仓位口径的分母。没设本金时下面一律回退成"市值/持仓市值"，
    # 也就是老行为（单票必然接近 100%），并在 warnings 里提醒用户去设。
    principal = get_user_capital(user_id=user_id)
    # 累计已实现盈亏（2026-10-23）：减仓/清仓落账的那部分。没有它，"当前资金"
    # 只能永远等于初始本金，用户清仓后账面对不上账。
    realized_pnl = get_user_realized_pnl(user_id=user_id)

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
                                         sentiment=await _resolve_sentiment())
        # 已实现盈亏 + 当前资金（2026-10-23）：清仓后本金口径必须跟着已实现的钱走，
        # 否则用户落袋 +890 元后页面还显示 3.25 万，"总盈亏"对不上账。
        realized = get_user_realized_pnl(user_id=user_id)
        # 空仓：现金 = 本金 + 全部已实现（无在途成本），当前资金同值
        current_capital = round(principal + realized, 2) if principal > 0 else None
        # 空仓也要把本金带出去：用户往往是先设好本金、再买第一笔，
        # 若这里返回 portfolio=None，前端就没有地方显示和保存本金。
        return {'has_positions': False, 'market_change': round(market_change, 2),
                'positions': [], 'envAdvice': env_advice,
                'portfolio': {'total_cost': 0, 'total_value': 0, 'total_pnl': 0,
                              'total_pnl_pct': 0, 'principal': round(principal, 2),
                              'capital_set': principal > 0,
                              'base_label': '本金' if principal > 0 else '总成本',
                              'position_pct': 0.0,
                              'free_cash': round(principal + realized, 2) if principal > 0 else None,
                              'realized_pnl': round(realized, 2),
                              'current_capital': current_capital,
                              'total_pnl_all': round(realized, 2),
                              'capital_hint': ('先去持仓页设一次初始本金，之后买入就能按本金算仓位、看整体收益'
                                               if principal <= 0 else ''),
                              'advice': _total_position_advice(market_change),
                              'warnings': [], 'action_counts': {}}}

    symbols = [p['symbol'].lower() for p in positions]

    async def _fetch_all():
        """大盘 + 持仓行情**一次批量拉取**（2026-10-17：原为两次并行 RTT，
        且 combined 串会进 fetch_with_retry 的 10s 实时缓存，行动台 8s 轮询/
        持仓页 60s 轮询大部分直接命中）。market 串与持仓串去重后一次请求，
        再按 symbol 拆回两用；任一失败都不影响另一份（解析不到就退回 0）。"""
        combined = list(dict.fromkeys(['sh000001'] + symbols))
        try:
            data = await fetch_with_retry(','.join(combined))
            parsed = parse_tencent_batch_data(data)
        except Exception as e:
            print(f'[持仓诊断] 行情拉取失败: {e}')
            return 0.0, None, {}
        by_sym = {p['symbol'].lower(): p for p in parsed}
        market_change, market_open_change = 0.0, None
        mq = by_sym.get('sh000001')
        if mq:
            market_change = float(mq.get('changePercent', 0) or 0)
            try:
                open_px = float(mq.get('open') or 0)
                prev_close = float(mq.get('prevCloseRaw') or 0)
                if open_px > 0 and prev_close > 0:
                    market_open_change = (open_px / prev_close - 1) * 100
            except (TypeError, ValueError):
                pass
        quote_map = {s: by_sym[s] for s in symbols if s in by_sym}
        return market_change, market_open_change, quote_map

    market_change, market_open_change, quote_map = await _fetch_all()

    # 黑名单（复用推荐复盘）
    try:
        from services.pick.recommend_review import get_recent_loser_symbols
        loser_symbols = get_recent_loser_symbols()
    except Exception:
        loser_symbols = set()

    diag_list = [diagnose_position(p, quote_map.get(p['symbol'].lower()),
                                   market_change, loser_symbols)
                 for p in positions]

    # 交易环境提示（持仓页横幅，及时可见；无持仓时也给出）
    urgent_sell_count = sum(1 for d in diag_list if d.get('action') in ('stop_loss', 'reduce', 'take_profit'))
    env_advice = calc_trading_advice(market_change, open_change=market_open_change,
                                     sentiment=await _resolve_sentiment(),
                                     urgent_sell_count=urgent_sell_count)

    total_cost = sum(d['cost'] * d['shares'] for d in diag_list)
    total_value = sum(d['market_value'] for d in diag_list)
    total_pnl = total_value - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0
    # 总盈亏 = 已实现（减仓/清仓落账）+ 浮动（持仓市值-成本）。
    # 当前资金 = 初始本金 + 总盈亏；可用现金 = 初始本金 + 已实现 - 在途成本
    # （买入扣现金、卖出按 成本+已实现 回现金，恒等式见 database.py 表注释）。
    # 注意 free_cash 老口径是 principal - total_value（把浮盈也当现金），
    # 浮盈没落袋就不是钱——用户清仓前看到的"可用"会虚高，这里按真实现金算。
    total_pnl_all = total_pnl + realized_pnl
    current_capital = round(principal + total_pnl_all, 2) if principal > 0 else None
    free_cash = round(principal + realized_pnl - total_cost, 2) if principal > 0 else None

    # 仓位基数：设了本金就按本金算（"这票占我全部资金的几成"才是用户想看的），
    # 没设就退回总成本 —— 那是老口径，只持一只票时必然接近 100%，超配告警等于废话，
    # 所以这种情况必须显式提示用户去设本金，而不是悄悄给一个误导性的百分比。
    base = principal if principal > 0 else total_cost
    base_label = '本金' if principal > 0 else '总成本'
    for d in diag_list:
        d['pos_pct'] = round(d['market_value'] / base * 100, 1) if base > 0 else 0.0
    position_pct = round(total_value / base * 100, 1) if base > 0 else 0.0

    advice = _total_position_advice(market_change)

    warnings = []
    # 超配判断只在"有本金"时做：分母换成持仓自己的成本时，单票必然是 ~100%，
    # 那条告警等于天天喊狼来了，反而把真正的问题淹掉。
    # 未设本金这件事单独用 capital_hint 说清楚（前端渲染成引导，而不是警告）。
    capital_hint = ''
    if principal <= 0:
        capital_hint = '未设初始本金：仓位是按「总成本」算的，只持一只票必然接近100%（去持仓页设一次即可）'
    elif base > 0:
        for d in diag_list:
            if d['pos_pct'] > 30:
                warnings.append(f'{d["name"]}占{base_label}{d["pos_pct"]:.0f}%，单票超配（建议≤25%）')
    if len(diag_list) > 5:
        warnings.append(f'持仓{len(diag_list)}只较多，过度分散会稀释精力与收益')
    # 实际仓位 vs 大盘建议档位：只有按本金算出来的仓位才配和"建议总仓位"比
    if principal > 0 and position_pct > advice['advice_pct'] + 5:
        warnings.append(f'实际仓位{position_pct:.0f}%已高出当前大盘建议的{advice["advice_pct"]}%（{advice["label"]}），先减再加')

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
            'principal': round(principal, 2),
            'capital_set': principal > 0,
            'base_label': base_label,
            'position_pct': position_pct,
            'free_cash': free_cash,
            # 2026-10-23：已实现盈亏 / 当前资金 / 总盈亏（已实现+浮动）。
            # 本金未设时 current_capital 为 None（前端隐藏），realized/total_pnl_all 仍给。
            'realized_pnl': round(realized_pnl, 2),
            'current_capital': current_capital,
            'total_pnl_all': round(total_pnl_all, 2),
            'total_pnl_all_pct': round(total_pnl_all / principal * 100, 2) if principal > 0 else None,
            'capital_hint': capital_hint,
            'advice': advice,
            'warnings': warnings,
            'action_counts': action_counts,
        },
        'envAdvice': env_advice,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
