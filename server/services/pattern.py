"""pattern.py - 量价6形态分类（P2-2，2026-06-30）

识别6种量价形态用于评分修正 + P3-1动态阈值触发。
形态优先级互斥：突破类 > 滞涨/背离类 > 回调类 > 横盘类

形态说明：
  ① 放量突破(volume_breakout)：近3日倍量+涨幅≥5%，最强进攻信号
  ② 倍量反包(volume_reversal)：当日倍量+阳包阴+涨≥3%，止跌反转信号
  ③ 缩量回调(shrink_pullback)：近5日持续缩量+阴跌，健康回调/洗盘特征
  ④ 放量滞涨(volume_stagnation)：量创20日新高+涨幅∈[-1%,1%)，顶部滞涨风险
  ⑤ 量价背离(volume_divergence)：价微涨+20日均量放大超30%，顶背离风险
  ⑥ 缩量横盘(shrink_sideways)：振幅<5%+量能萎缩，动能不足
"""
from typing import List, Dict, Optional
from app.config import (
    TREND_PATTERN_FACTOR,
    TREND_PATTERN_VOLUME_BREAKOUT_RATIO, TREND_PATTERN_VOLUME_BREAKOUT_CHANGE,
    TREND_PATTERN_REVERSAL_CHANGE, TREND_PATTERN_SHRINK_RATIO,
    TREND_PATTERN_SIDEWAYS_AMPLITUDE, TREND_PATTERN_DIVERGENCE_VOLUME_EXPAND,
)


def detect_volume_price_pattern(closes: List[float], opens: List[float],
                                 highs: List[float], lows: List[float],
                                 vols: List[float], avg_vol_20: float,
                                 avg_vol_60: float, latest_idx: int,
                                 amplitude_20d: float) -> Dict:
    """识别量价6形态，返回互斥的单个形态 + 修正系数

    Args:
        closes/opens/highs/lows/vols: OHLCV 序列
        avg_vol_20/avg_vol_60: 20/60日均量
        latest_idx: 最新K线索引
        amplitude_20d: 近20日振幅(%)

    Returns:
        {
            'pattern': str,    # 形态名（volume_breakout/volume_reversal/...）
            'factor': float,   # 修正系数（来自 TREND_PATTERN_FACTOR）
            'desc': str,       # 中文描述
            'details': Dict,   # 形态识别的中间数据
        }
    """
    n = latest_idx + 1
    if n < 5 or avg_vol_20 <= 0:
        return {'pattern': 'none', 'factor': 0.0, 'desc': '数据不足', 'details': {}}

    today_close = closes[latest_idx]
    today_vol = vols[latest_idx]
    prev_close = closes[latest_idx - 1] if latest_idx > 0 else today_close

    # ========== 优先级1：突破类 ==========
    # 1a. 放量突破：近3日任一日 倍量 + 涨幅≥5%
    for i in range(max(0, latest_idx - 2), latest_idx + 1):
        if vols[i] >= avg_vol_20 * TREND_PATTERN_VOLUME_BREAKOUT_RATIO:
            pc = closes[i - 1] if i > 0 else closes[i]
            change = (closes[i] - pc) / pc * 100 if pc > 0 else 0
            if change >= TREND_PATTERN_VOLUME_BREAKOUT_CHANGE:
                return {
                    'pattern': 'volume_breakout',
                    'factor': TREND_PATTERN_FACTOR['volume_breakout'],
                    'desc': f'放量突破（{latest_idx - i + 1}日前 倍量+涨{change:.1f}%）',
                    'details': {'breakoutDay': i, 'volumeRatio': round(vols[i] / avg_vol_20, 2), 'change': round(change, 2)}
                }

    # 1b. 倍量反包：当日倍量 + 阳包阴（今日阳线 + 收盘>昨日最高 + 昨日阴线）
    if latest_idx > 0:
        today_is_yang = today_close > opens[latest_idx]
        yesterday_is_yin = closes[latest_idx - 1] < opens[latest_idx - 1]
        engulf = today_close > highs[latest_idx - 1]
        if (today_vol >= avg_vol_20 * TREND_PATTERN_VOLUME_BREAKOUT_RATIO
                and today_is_yang and yesterday_is_yin and engulf):
            change = (today_close - prev_close) / prev_close * 100 if prev_close > 0 else 0
            if change >= TREND_PATTERN_REVERSAL_CHANGE:
                return {
                    'pattern': 'volume_reversal',
                    'factor': TREND_PATTERN_FACTOR['volume_reversal'],
                    'desc': f'倍量反包（阳包阴+涨{change:.1f}%）',
                    'details': {'volumeRatio': round(today_vol / avg_vol_20, 2), 'change': round(change, 2)}
                }

    # ========== 优先级2：滞涨/背离类（风险形态）==========
    # 2a. 放量滞涨：单日量创20日新高 + 涨幅在 [-1%, 1%]
    win20_vols = vols[max(0, latest_idx - 19):latest_idx + 1]
    max_vol_20 = max(win20_vols) if win20_vols else 0
    if today_vol == max_vol_20 and today_vol > 0:
        change = (today_close - prev_close) / prev_close * 100 if prev_close > 0 else 0
        if -1.0 <= change < 1.0:
            return {
                'pattern': 'volume_stagnation',
                'factor': TREND_PATTERN_FACTOR['volume_stagnation'],
                'desc': f'放量滞涨（量比{today_vol / avg_vol_20:.1f}x+涨{change:.1f}%）',
                'details': {'volumeRatio': round(today_vol / avg_vol_20, 2), 'change': round(change, 2)}
            }

    # 2b. 量价背离：近5日股价微涨 + 20日均量放大超30%
    if n >= 25:
        price_rise = (closes[latest_idx] - closes[latest_idx - 4]) / closes[latest_idx - 4] * 100 if closes[latest_idx - 4] > 0 else 0
        if price_rise >= 1.0:
            avg_vol_first = sum(vols[latest_idx - 24:latest_idx - 19]) / 5
            avg_vol_last = sum(vols[latest_idx - 4:latest_idx + 1]) / 5
            if avg_vol_first > 0 and avg_vol_last > avg_vol_first * TREND_PATTERN_DIVERGENCE_VOLUME_EXPAND:
                return {
                    'pattern': 'volume_divergence',
                    'factor': TREND_PATTERN_FACTOR['volume_divergence'],
                    'desc': f'量价背离（价涨{price_rise:.1f}%+量扩{(avg_vol_last / avg_vol_first - 1) * 100:.0f}%）',
                    'details': {'priceRise': round(price_rise, 2), 'volumeExpand': round(avg_vol_last / avg_vol_first, 2)}
                }

    # ========== 优先级3：回调类 ==========
    # 3a. 缩量回调：近5日持续缩量（<60日均量×0.5）+ 阴跌
    if n >= 5 and avg_vol_60 > 0:
        all_shrink = all(vols[latest_idx - i] < avg_vol_60 * TREND_PATTERN_SHRINK_RATIO for i in range(5))
        price_drop = closes[latest_idx] < closes[latest_idx - 4]
        if all_shrink and price_drop:
            drop_pct = (closes[latest_idx - 4] - closes[latest_idx]) / closes[latest_idx - 4] * 100 if closes[latest_idx - 4] > 0 else 0
            return {
                'pattern': 'shrink_pullback',
                'factor': TREND_PATTERN_FACTOR['shrink_pullback'],
                'desc': f'缩量回调（量比{vols[latest_idx] / avg_vol_60:.2f}+跌{drop_pct:.1f}%）',
                'details': {'volumeRatio': round(vols[latest_idx] / avg_vol_60, 2), 'dropPct': round(drop_pct, 2)}
            }

    # ========== 优先级4：横盘类 ==========
    # 4a. 缩量横盘：振幅<5% + 量能萎缩（今日量<20日均量×0.8）
    if amplitude_20d < TREND_PATTERN_SIDEWAYS_AMPLITUDE and today_vol < avg_vol_20 * 0.8:
        return {
            'pattern': 'shrink_sideways',
            'factor': TREND_PATTERN_FACTOR['shrink_sideways'],
            'desc': f'缩量横盘（振幅{amplitude_20d:.1f}%+量比{today_vol / avg_vol_20:.2f}）',
            'details': {'amplitude': round(amplitude_20d, 2), 'volumeRatio': round(today_vol / avg_vol_20, 2)}
        }

    return {'pattern': 'none', 'factor': 0.0, 'desc': '无显著形态', 'details': {}}
