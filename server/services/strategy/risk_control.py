"""risk_control.py - 风控止损输出（P2a，2026-07-16 新增）

为每只被推荐的/被查看的股票输出一套"可执行的风控结论"：
- stop_loss：基于当日振幅(波动率代理)动态缩放的止损价，低波动股止损窄、高波动股止损宽。
- risk_level：low / mid / high，综合 综合评分 + 估值高估 + 聪明钱净卖出 + 筹码散户化 判定。
- position_suggest：轻仓/半仓/重仓 建议（对应 ≤30% / ≤50% / ≤80%）。
- risk_note：一句话风险说明（前端直接展示）。

设计为纯函数（无 IO），可在 recommend / routes 扫描 / 单股分析中同步调用，
不增加任何网络请求。输入字段全部来自已有的 stock 行情 + score_result + valuation。

2026-10-28（V4.6）：角色体系整体下线，止损只按 **位置分位** 修正，
不再有 情绪先锋/板块龙头/中军/跟风/杂毛 的角色分类与角色止损倍率。
"""
from typing import Dict, Optional

from app.config import (RISK_CONTROL_STOP_LOSS_MIN, RISK_CONTROL_STOP_LOSS_MAX,
                        RISK_CONTROL_STOP_LOSS_VOL_MULT, RISK_CONTROL_SCORE_HIGH_RISK,
                        RISK_CONTROL_SCORE_MID_RISK,
                        BUY_SELL_POSITION_LOW_GATE, BUY_SELL_POSITION_HIGH_GATE,
                        BUY_SELL_STOP_POSITION_LOW_MULT, BUY_SELL_STOP_POSITION_HIGH_MULT)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _risk_level_from_signals(score_total: float, valuation: Optional[Dict],
                             smart_money: Optional[Dict], holder_ratio: Optional[float]) -> str:
    """综合信号判定风险等级。

    基础等级由综合评分决定；叠加"高危信号"会升级等级（高风险不可被降级）。
    高危信号：估值极端高估 / 龙虎榜净卖出 / 股东户数环比大幅增加(散户化)。
    """
    if score_total < RISK_CONTROL_SCORE_HIGH_RISK:
        level = 'high'
    elif score_total < RISK_CONTROL_SCORE_MID_RISK:
        level = 'mid'
    else:
        level = 'low'

    danger = []
    if valuation and valuation.get('overvalued'):
        danger.append('估值极端高估')
    if smart_money and isinstance(smart_money, dict):
        net = float(smart_money.get('net_amt') or 0)
        on_board = bool(smart_money.get('on_board'))
        if on_board and net < 0:
            danger.append('龙虎榜净卖出')
    if holder_ratio is not None and holder_ratio >= 10:
        danger.append('筹码散户化')

    # 出现任一高危信号，至少升到 mid；若本就 high 则保持
    if danger and level == 'low':
        level = 'mid'
    return level, danger


def calc_risk_control(price: float, score_result: Optional[Dict] = None,
                      stock: Optional[Dict] = None,
                      valuation: Optional[Dict] = None,
                      smart_money: Optional[Dict] = None,
                      holder_ratio: Optional[float] = None) -> Dict:
    """计算风控止损结论。

    Args:
        price: 当前价 / 买入参考价（元）
        score_result: calc_stock_score_v2 返回值（含 'total'）
        stock: 行情 dict（含 'amplitude' 当日振幅%，用于波动率缩放止损）
        valuation: 估值分位 dict（含 'overvalued'），来自 valuation.get_valuation_batch
        smart_money: 聪明钱 dict（含 'on_board'/'net_amt'）
        holder_ratio: 股东户数环比变化率(%)，>0 散户化

    Returns:
        {price, stop_loss, stop_loss_pct, risk_level, position_suggest, risk_note}
    """
    price = float(price or 0)
    if price <= 0:
        return {
            'price': 0, 'stop_loss': 0, 'stop_loss_pct': 0.0,
            'risk_level': 'high', 'position_suggest': '轻仓(≤30%)',
            'risk_note': '无效价格，无法计算止损',
        }

    score_total = 0.0
    if isinstance(score_result, dict):
        score_total = float(score_result.get('total', 0) or 0)

    # ---- 1. 动态止损幅度（振幅波动率缩放 + 位置修正） ----
    amplitude = 0.0
    if isinstance(stock, dict):
        try:
            amplitude = float(stock.get('amplitude') or 0)
        except (TypeError, ValueError):
            amplitude = 0.0
    # 振幅为 0（停牌/无数据）时给一个中性默认 6%；否则 振幅×系数 再 clamp
    if amplitude <= 0:
        base_stop_pct = 0.06
    else:
        base_stop_pct = _clamp(amplitude / 100.0 * RISK_CONTROL_STOP_LOSS_VOL_MULT,
                               RISK_CONTROL_STOP_LOSS_MIN, RISK_CONTROL_STOP_LOSS_MAX)

    # V4.6: 只剩位置止损修正（角色倍率已随角色体系删除）
    stop_mult = 1.0
    position_pct = 50
    pos_label = ''
    if isinstance(score_result, dict):
        base_scores = score_result.get('base_scores', {}) or {}

        # 位置分位：fallback base_scores['position']
        pos_score = base_scores.get('position', 10)
        if pos_score <= 5:
            position_pct = 80   # 高位
        elif pos_score >= 15:
            position_pct = 20   # 低位
        else:
            position_pct = 50   # 中位

        # 位置止损倍率
        if position_pct <= BUY_SELL_POSITION_LOW_GATE:
            stop_mult *= BUY_SELL_STOP_POSITION_LOW_MULT      # 低位：放宽
            pos_label = '低位'
        elif position_pct >= BUY_SELL_POSITION_HIGH_GATE:
            stop_mult *= BUY_SELL_STOP_POSITION_HIGH_MULT      # 高位：收紧
            pos_label = '高位'

    stop_pct = _clamp(base_stop_pct * stop_mult, RISK_CONTROL_STOP_LOSS_MIN, RISK_CONTROL_STOP_LOSS_MAX)
    stop_loss = round(price * (1 - stop_pct), 2)
    stop_pct = round((price - stop_loss) / price * 100, 2)

    # ---- 2. 风险等级 ----
    level, danger = _risk_level_from_signals(score_total, valuation, smart_money, holder_ratio)

    # ---- 3. 仓位建议 ----
    position_map = {
        'low': '可重仓(≤80%)',
        'mid': '半仓(≤50%)',
        'high': '轻仓(≤30%)',
    }
    position_suggest = position_map.get(level, '半仓(≤50%)')

    # ---- 4. 风险说明 ----
    note_parts = []
    note_parts.append(f"综合评分{score_total:.0f}分→{level_map_cn(level)}")
    if danger:
        note_parts.append('；'.join(danger))
    # 只写位置与止损幅度
    if isinstance(score_result, dict):
        note_parts.append(f"{pos_label}止损{stop_pct:.1f}%" if pos_label
                          else f"止损{stop_pct:.1f}%")
    elif amplitude > 0:
        note_parts.append(f"止损≈现价-{stop_pct:.1f}%（基于振幅波动率）")
    risk_note = '，'.join(note_parts)

    return {
        'price': round(price, 2),
        'stop_loss': stop_loss,
        'stop_loss_pct': stop_pct,
        'risk_level': level,
        'position_suggest': position_suggest,
        'risk_note': risk_note,
    }


def level_map_cn(level: str) -> str:
    return {'low': '低风险', 'mid': '中风险', 'high': '高风险'}.get(level, '中风险')
