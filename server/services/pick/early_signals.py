"""early_signals.py - 早期信号层（第5维度，2026-08-17 新增）

在 is_up_trend V4.0 评分系统上叠加"早期信号加成层"，识别"趋势形成前"的转折信号，
降低整体滞后性（均线系统内在滞后 2-5 天）。

5 个纯函数检测器 + 1 个聚合函数。所有函数仅依赖 OHLCV 序列（复用 is_up_trend 已提取的
closes/opens/highs/lows/vols），无 IO/无状态。

性能 gating（避免给所有票都跑 5 个检测器）：
- 全局开关 TREND_EARLY_SIGNAL_ENABLED
- 仅 base_score ∈ [40, 92] 时运行（边界股才有早期信号价值）
- rsi_value >= 88 时不运行（避免给超买股加分）
- 单检测器异常不影响其他（容错 try/except）

封顶：bonus = min(命中数 × PER_HIT_SCORE, MAX_BONUS)（默认最多 +10 分）
"""
from typing import Dict, List, Optional

from app.config import (
    TREND_EARLY_SIGNAL_ENABLED, TREND_EARLY_SIGNAL_PER_HIT_SCORE,
    TREND_EARLY_SIGNAL_MAX_BONUS, TREND_EARLY_SIGNAL_POSITION_GATE,
    TREND_EARLY_SIGNAL_BASE_SCORE_MIN, TREND_EARLY_SIGNAL_BASE_SCORE_MAX,
    TREND_EARLY_SIGNAL_NR7_PERIOD, TREND_EARLY_SIGNAL_NR7_RANGE_RATIO,
    TREND_EARLY_SIGNAL_VCP_MIN_CONTRACTIONS, TREND_EARLY_SIGNAL_VCP_VOL_DECAY,
    TREND_EARLY_SIGNAL_RSI_BOTTOM_LOOKBACK, TREND_EARLY_SIGNAL_RSI_BOTTOM_PERIOD,
    TREND_EARLY_SIGNAL_FIRST_LIMIT_UP_POSITION_MAX,
    TREND_EARLY_SIGNAL_FIRST_LIMIT_UP_LOOKBACK,
    TREND_EARLY_SIGNAL_CONSOLIDATION_BREAKOUT_VOL_RATIO,
    TREND_LIMIT_UP_THRESHOLD,
)


def _calc_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """简单 RSI 计算（与 trend._calc_rsi 逻辑一致，独立实现避免循环依赖）。"""
    n = len(closes)
    if n < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(n - period, n):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses += -diff
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100.0 - 100.0 / (1.0 + rs)


def detect_nr7_inside_bar(closes: List[float], highs: List[float], lows: List[float],
                          vols: List[float], latest_idx: int) -> Optional[Dict]:
    """检测器1: NR7 / Inside Bar 缩量变盘。

    NR7 = 当前 N 日（默认7）区间是近 30 日最小（波动率收缩到极致，变盘在即）。
    叠加 Inside Bar（今日高低点完全包含在昨日内）或缩量确认。
    命中加分 +3。
    """
    n = latest_idx + 1
    period = TREND_EARLY_SIGNAL_NR7_PERIOD
    if n < 30 or latest_idx < period:
        return None
    # 当前 period 日区间
    cur_high = max(highs[latest_idx - period + 1:latest_idx + 1])
    cur_low = min(lows[latest_idx - period + 1:latest_idx + 1])
    cur_range = cur_high - cur_low
    if cur_range <= 0:
        return None
    # 近 30 日每个 period 日窗口的区间，取最小比较
    lookback = 30
    min_hist_range = float('inf')
    for end in range(latest_idx - period, max(period - 1, latest_idx - lookback), -1):
        h = max(highs[end - period + 1:end + 1])
        l = min(lows[end - period + 1:end + 1])
        r = h - l
        if r > 0 and r < min_hist_range:
            min_hist_range = r
    if min_hist_range == float('inf'):
        return None
    is_nr7 = cur_range <= min_hist_range * TREND_EARLY_SIGNAL_NR7_RANGE_RATIO
    if not is_nr7:
        return None
    # Inside Bar 或缩量确认
    inside_bar = (highs[latest_idx] <= highs[latest_idx - 1] and
                  lows[latest_idx] >= lows[latest_idx - 1])
    avg_vol_20 = sum(vols[max(0, latest_idx - 19):latest_idx + 1]) / min(20, latest_idx + 1)
    shrink = vols[latest_idx] < avg_vol_20 * 0.7 if avg_vol_20 > 0 else False
    if not (inside_bar or shrink):
        return None
    return {
        'name': 'nr7_inside_bar',
        'desc': f'NR7缩量变盘：{period}日区间为近30日最小' + ('，Inside Bar确认' if inside_bar else '，缩量确认'),
        'score': TREND_EARLY_SIGNAL_PER_HIT_SCORE,
    }


def detect_vcp(closes: List[float], highs: List[float], lows: List[float],
               vols: List[float], latest_idx: int) -> Optional[Dict]:
    """检测器2: VCP 波动率收缩（Volatility Contraction Pattern）。

    近 30 日找 2-3 次峰谷，振幅 + 量能严格递减（每次收缩比上一次更窄更缩量）。
    这是 Mark Minervini 的 SEPA 体系核心买入形态：收缩到极致后突破概率高。
    命中加分 +3。
    """
    n = latest_idx + 1
    if n < 30:
        return None
    lookback = 30
    start = max(1, latest_idx - lookback + 1)
    # 找局部高点/低点（枢轴点）
    pivots_high = []
    pivots_low = []
    for i in range(start, latest_idx):
        if i < 2 or i > n - 2:
            continue
        if highs[i] == max(highs[i - 2:i + 3]):
            pivots_high.append(i)
        if lows[i] == min(lows[i - 2:i + 3]):
            pivots_low.append(i)
    # 至少需要 MIN_CONTRACTIONS 次收缩（即 N+1 个枢轴形成 N 段）
    min_pivots = TREND_EARLY_SIGNAL_VCP_MIN_CONTRACTIONS + 1
    pivots = sorted(set(pivots_high + pivots_low))
    if len(pivots) < min_pivots:
        return None
    # 计算相邻枢轴间的振幅（用高点-低点占价格比例）
    amplitudes = []
    vol_avgs = []
    for i in range(len(pivots) - 1):
        p1, p2 = pivots[i], pivots[i + 1]
        seg_high = max(highs[p1:p2 + 1])
        seg_low = min(lows[p1:p2 + 1])
        ref = (seg_high + seg_low) / 2
        if ref <= 0:
            return None
        amplitudes.append((seg_high - seg_low) / ref)
        seg_vol = sum(vols[p1:p2 + 1]) / max(1, p2 - p1 + 1)
        vol_avgs.append(seg_vol)
    # 振幅严格递减 + 量能递减（最后一次量能 ≤ 第一次 × VOL_DECAY）
    contractions = sum(1 for i in range(len(amplitudes) - 1)
                       if amplitudes[i + 1] < amplitudes[i])
    if contractions < TREND_EARLY_SIGNAL_VCP_MIN_CONTRACTIONS:
        return None
    vol_decay_ok = (len(vol_avgs) >= 2 and vol_avgs[-1] <= vol_avgs[0] * (1 + TREND_EARLY_SIGNAL_VCP_VOL_DECAY))
    if not vol_decay_ok:
        return None
    return {
        'name': 'vcp',
        'desc': f'VCP波动率收缩：{contractions + 1}次收缩，振幅与量能逐级递减',
        'score': TREND_EARLY_SIGNAL_PER_HIT_SCORE,
    }


def detect_rsi_bottom_divergence(closes: List[float], latest_idx: int,
                                 rsi_value: Optional[float] = None) -> Optional[Dict]:
    """检测器3: RSI 底背离。

    价格创新低但 RSI 抬高（下跌动能衰竭），且当前 RSI < 50（仍在弱势区，尚未过热）。
    命中加分 +3。
    """
    n = latest_idx + 1
    lookback = TREND_EARLY_SIGNAL_RSI_BOTTOM_LOOKBACK
    period = TREND_EARLY_SIGNAL_RSI_BOTTOM_PERIOD
    if n < lookback or n < period + 1:
        return None
    if rsi_value is None:
        rsi_value = _calc_rsi(closes, period)
    if rsi_value is None or rsi_value >= 50:
        return None
    # 找近 lookback 日的两个价格低点
    start = max(period, latest_idx - lookback + 1)
    # 近期低点（最后 1/3 区间）
    recent_start = start + (latest_idx - start) * 2 // 3
    recent_low_idx = recent_start
    for i in range(recent_start, latest_idx + 1):
        if closes[i] < closes[recent_low_idx]:
            recent_low_idx = i
    # 前期低点（前 2/3 区间）
    prev_low_idx = start
    for i in range(start, recent_start):
        if closes[i] < closes[prev_low_idx]:
            prev_low_idx = i
    # 价格创新低（近期低 < 前期低）
    if closes[recent_low_idx] >= closes[prev_low_idx]:
        return None
    # RSI 抬高（近期低点 RSI > 前期低点 RSI）
    rsi_recent = _calc_rsi(closes[:recent_low_idx + 1], period)
    rsi_prev = _calc_rsi(closes[:prev_low_idx + 1], period)
    if rsi_recent is None or rsi_prev is None:
        return None
    if rsi_recent <= rsi_prev:
        return None
    return {
        'name': 'rsi_bottom_div',
        'desc': f'RSI底背离：价格创新低但RSI抬高({rsi_prev:.0f}→{rsi_recent:.0f})，下跌动能衰竭',
        'score': TREND_EARLY_SIGNAL_PER_HIT_SCORE,
    }


def detect_bottom_first_limit_up(closes: List[float], opens: List[float],
                                 latest_idx: int, position_pct: Optional[float]) -> Optional[Dict]:
    """检测器4: 底部首板。

    今日涨停 + 近 60 日无其他涨停 + 当前价格分位 < 50%（低位首板，趋势启动信号）。
    命中加分 +3。
    """
    n = latest_idx + 1
    lookback = TREND_EARLY_SIGNAL_FIRST_LIMIT_UP_LOOKBACK
    pos_max = TREND_EARLY_SIGNAL_FIRST_LIMIT_UP_POSITION_MAX
    if n < lookback or latest_idx < 1:
        return None
    # 今日涨停
    change_today = (closes[latest_idx] - closes[latest_idx - 1]) / closes[latest_idx - 1] * 100
    if change_today < TREND_LIMIT_UP_THRESHOLD:
        return None
    # 位置门控：分位过高不算"底部"
    if position_pct is not None and position_pct > pos_max:
        return None
    # 近 60 日（排除今日）无其他涨停
    start = max(1, latest_idx - lookback + 1)
    for i in range(start, latest_idx):
        prev_c = closes[i - 1] if i > 0 else opens[i]
        if prev_c <= 0:
            continue
        chg = (closes[i] - prev_c) / prev_c * 100
        if chg >= TREND_LIMIT_UP_THRESHOLD:
            return None  # 60日内有其他涨停，不是首板
    return {
        'name': 'bottom_first_limit',
        'desc': f'底部首板：低位涨停且近{lookback}日无其他涨停，趋势启动信号',
        'score': TREND_EARLY_SIGNAL_PER_HIT_SCORE,
    }


def detect_volume_breakout_from_consolidation(closes: List[float], opens: List[float],
                                              highs: List[float], lows: List[float],
                                              vols: List[float], latest_idx: int,
                                              avg_vol_20: float) -> Optional[Dict]:
    """检测器5: 缩量整理后首放。

    前 7 日区间收敛 + 缩量，当日倍量阳线打破（成交量 ≥ 20日均量 × VOL_RATIO）。
    命中加分 +3。与 pattern.py 的 volume_breakout 互斥（由聚合函数处理去重）。
    """
    n = latest_idx + 1
    if n < 10 or latest_idx < 7 or avg_vol_20 <= 0:
        return None
    # 前 7 日区间收敛：振幅 < 8%（相对中值）
    seg_start = latest_idx - 7
    seg_high = max(highs[seg_start:latest_idx])
    seg_low = min(lows[seg_start:latest_idx])
    seg_mid = (seg_high + seg_low) / 2
    if seg_mid <= 0:
        return None
    seg_amplitude = (seg_high - seg_low) / seg_mid * 100
    if seg_amplitude >= 8:
        return None
    # 前 7 日缩量：均量 < 20日均量 × 0.8
    seg_avg_vol = sum(vols[seg_start:latest_idx]) / 7
    if seg_avg_vol >= avg_vol_20 * 0.8:
        return None
    # 当日倍量阳线：成交量 ≥ 20日均量 × VOL_RATIO + 收阳
    vol_ratio_req = TREND_EARLY_SIGNAL_CONSOLIDATION_BREAKOUT_VOL_RATIO
    if vols[latest_idx] < avg_vol_20 * vol_ratio_req:
        return None
    if closes[latest_idx] <= opens[latest_idx]:
        return None
    return {
        'name': 'consolidation_break',
        'desc': f'缩量整理后首放：前7日收敛缩量，当日倍量({vols[latest_idx] / avg_vol_20:.1f}倍)阳线突破',
        'score': TREND_EARLY_SIGNAL_PER_HIT_SCORE,
    }


def detect_early_signals(kline_data: List[List], closes: List[float], opens: List[float],
                         highs: List[float], lows: List[float], vols: List[float],
                         latest_idx: int, avg_vol_20: float, position_pct: Optional[float],
                         all_time_high: float, rsi_value: Optional[float],
                         pattern_name: str, base_score: float) -> Dict:
    """聚合所有早期信号检测器，返回 {bonus, signals, hitCount}。

    性能 gating：
    - 全局开关关闭 → 直接返回 0
    - base_score 不在 [MIN, MAX] 区间 → 跳过（边界股才有早期信号价值）
    - rsi_value >= 88 → 跳过（避免给超买股加分）
    - position_pct > POSITION_GATE → 大部分信号价值低（仍允许底背离/首板检测）
    - 单检测器异常不影响其他（容错 try/except）

    去重：若 pattern 已识别为 volume_breakout，则 consolidation_break 不再加分。
    """
    result = {'bonus': 0, 'signals': [], 'hitCount': 0}
    if not TREND_EARLY_SIGNAL_ENABLED:
        return result
    # base_score gating
    if base_score < TREND_EARLY_SIGNAL_BASE_SCORE_MIN or base_score > TREND_EARLY_SIGNAL_BASE_SCORE_MAX:
        return result
    # RSI 超买 gating
    if rsi_value is not None and rsi_value >= 88:
        return result

    signals = []
    high_position = position_pct is not None and position_pct > TREND_EARLY_SIGNAL_POSITION_GATE

    # 检测器1: NR7 / Inside Bar（高位时价值低，跳过）
    if not high_position:
        try:
            r = detect_nr7_inside_bar(closes, highs, lows, vols, latest_idx)
            if r:
                signals.append(r)
        except Exception as _e:
            print(f'[早期信号] NR7检测异常: {_e}')

    # 检测器2: VCP（高位时价值低，跳过）
    if not high_position:
        try:
            r = detect_vcp(closes, highs, lows, vols, latest_idx)
            if r:
                signals.append(r)
        except Exception as _e:
            print(f'[早期信号] VCP检测异常: {_e}')

    # 检测器3: RSI 底背离
    try:
        r = detect_rsi_bottom_divergence(closes, latest_idx, rsi_value)
        if r:
            signals.append(r)
    except Exception as _e:
        print(f'[早期信号] RSI底背离检测异常: {_e}')

    # 检测器4: 底部首板
    try:
        r = detect_bottom_first_limit_up(closes, opens, latest_idx, position_pct)
        if r:
            signals.append(r)
    except Exception as _e:
        print(f'[早期信号] 底部首板检测异常: {_e}')

    # 检测器5: 缩量整理后首放（与 pattern.py 的 volume_breakout 互斥）
    if pattern_name != 'volume_breakout':
        try:
            r = detect_volume_breakout_from_consolidation(
                closes, opens, highs, lows, vols, latest_idx, avg_vol_20)
            if r:
                signals.append(r)
        except Exception as _e:
            print(f'[早期信号] 缩量后首放检测异常: {_e}')

    hit_count = len(signals)
    bonus = min(hit_count * TREND_EARLY_SIGNAL_PER_HIT_SCORE, TREND_EARLY_SIGNAL_MAX_BONUS)
    return {'bonus': bonus, 'signals': signals, 'hitCount': hit_count}
