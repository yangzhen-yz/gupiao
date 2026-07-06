"""trend.py - split from main.py"""
import json, asyncio, time
from typing import List, Dict, Optional
from datetime import date, timedelta
from fastapi import HTTPException
from app.config import (TREND_MIN_SCORE, TREND_MIN_KLINE_DAYS, TREND_LIMIT_UP_THRESHOLD, TREND_LIMIT_DOWN_THRESHOLD,
    TREND_IS_UP_MIN_SCORE, USE_INTRADAY_BREAK_CHECK, KLINE_DATA_LIMIT, KLINE_DISPLAY_MA_POINTS,
    SCAN_CONCURRENCY, SCAN_CACHE_TTL, HOT_STOCK_POOL, DELISTING_KEYWORDS, FORBIDDEN_KEYWORDS,
    STRATEGY_PREDICT_BULL_MIN_SCORE, SCAN_KLINE_TIMEOUT, TREND_AUTO_ADD_POOL_THRESHOLD,
    TREND_D1_TREND_FULL, TREND_D1_TREND_PARTIAL, TREND_D1_TREND_BARE,
    TREND_D1_DIVERGENCE_PENALTY_TIERS,
    TREND_D2_LIMIT_UP_TIERS, TREND_D2_YANG_LINE_TIERS, TREND_D2_CONSECUTIVE_BOARD_TIERS,
    TREND_D3_RATIO_TIERS, TREND_D3_SLOPE_TIERS, TREND_D3_STABILITY_TIERS,
    TREND_D4_DRAWDOWN_TIERS,
    TREND_DEDUCT_POSITION_HIGH, TREND_DEDUCT_MA60_MA120_BEAR, TREND_DEDUCT_POSITION_MID,
    TREND_DEDUCT_MA20_BROKEN, TREND_DEDUCT_LIMIT_DOWN, TREND_DEDUCT_SHRINK_VOL,
    TREND_DEDUCT_VOLUME_SPIKE, TREND_DEDUCT_VOLUME_DIVERGENCE,
    TREND_DEDUCT_EXTREME_VOLATILITY, TREND_DEDUCT_SINGLE_DAY_CRASH,
    TREND_DEDUCT_AMPLITUDE_FLAT, TREND_DEDUCT_NON_MAIN_BOARD,
    TREND_DEDUCT_GROUPS,
    TREND_POSITION_BREAKTHROUGH_GAIN,
    TREND_SIDEWAYS_LOW_PCT, TREND_SIDEWAYS_MID_PCT, TREND_SIDEWAYS_BONUS,
    TREND_BONUS_BULL_ALIGNMENT, TREND_BONUS_VOLUME_BREAKOUT, TREND_BONUS_CONSECUTIVE_YANG,
    TREND_BONUS_POCKET_PIVOT, TREND_BONUS_STEADY_UPTREND, TREND_BONUS_ACCELERATING,
    TREND_SECTOR_MOMENTUM_WEIGHT, TREND_SECTOR_HOT_RANK_MIN,
    TREND_SECTOR_FACTOR_TIERS, TREND_SECTOR_FACTOR_COLD,
    TREND_EXCLUDE_CONSECUTIVE_LIMIT_DOWN, TREND_EXCLUDE_MARKET_CAP_MIN,
    TREND_RANGE_20D_FLAT_MIN,
    # P2-1 筹码结构修正
    TREND_CHIP_FACTOR_MAX, TREND_CHIP_FACTOR_MIN,
    TREND_CHIP_COST_ABOVE_BOTH, TREND_CHIP_COST_BELOW_BOTH, TREND_CHIP_COST_BETWEEN,
    TREND_CHIP_TURNOVER_LOCKED, TREND_CHIP_TURNOVER_LOOSE, TREND_CHIP_TURNOVER_NORMAL,
    TREND_CHIP_MAIN_ACCUMULATE, TREND_CHIP_MAIN_DISTRIBUTE, TREND_CHIP_MAIN_NEUTRAL,
    TREND_CHIP_CV_STABLE, TREND_CHIP_CV_VOLATILE, TREND_CHIP_CV_NORMAL,
    # P3-1 动态阈值
    TREND_DYNAMIC_MARKET_BULL_CHANGE, TREND_DYNAMIC_MARKET_BEAR_CHANGE,
    TREND_TOTAL_FACTOR_CAP, TREND_DYNAMIC_THRESHOLDS, TREND_DYNAMIC_DEFAULTS,
    # P3-2 行业中性化
    STRATEGY_INDUSTRY_NEUTRALIZATION_ENABLED)
from db.database import get_db_conn, load_trend_scan_results, save_trend_scan_results, save_predictions, load_user_scan_pool, save_user_scan_pool, load_hot_search_ranking, load_stock_basic_info, load_hot_stock_buttons, load_stock_concept_tags, save_stock_industry
from services.stock import (get_http_client, has_delisting_risk, is_main_board, get_kline_data,
    calculate_ma, get_cached_stock_name, fetch_with_retry, fetch_stock_data, get_stock_name,
    parse_tencent_batch_data, has_negative_announcement)
from services.strategy import calc_stock_score_v2, load_strategy_factor_weights, apply_industry_neutralization
from services.pattern import detect_volume_price_pattern

_last_scan_result = None
_last_scan_date = ''
_last_scan_ts = 0.0
# 2026-07-06 修复：模块级 asyncio.Lock / Semaphore 会绑定到 import 时的事件循环，
# Uvicorn reload 后新进程的新循环会触发 "bound to a different event loop" 错误，
# 导致 /api/scan-trend-stocks?force=true 返回 500。改为函数内懒初始化。
_scan_trend_lock = None

def _get_scan_lock() -> asyncio.Lock:
    """获取趋势扫描锁（函数内懒初始化，绑定当前事件循环）"""
    global _scan_trend_lock
    if _scan_trend_lock is None:
        _scan_trend_lock = asyncio.Lock()
    return _scan_trend_lock

def get_scan_semaphore() -> asyncio.Semaphore:
    """获取趋势扫描并发信号量（每次返回新实例，绑定当前事件循环）"""
    return asyncio.Semaphore(SCAN_CONCURRENCY)

def _ensure_today_date(data: Dict) -> Dict:
    # 2026-07-06 C-5: 已合并到 services.utils.ensure_today_date，此处保留薄壳透传以最小化改动
    from services.utils import ensure_today_date
    return ensure_today_date(data)

def is_up_trend(kline_data: List[List], realtime_price: Optional[float] = None,
                negative_announcement: Optional[Dict] = None,
                stock_name: str = '',
                market_cap: Optional[float] = None,
                is_mainboard: bool = True,
                sector_change: Optional[float] = None,
                turnover_rate: float = 0.0,
                main_inflow_ratio: Optional[float] = None,
                market_regime: str = 'range',
                pattern_info: Optional[Dict] = None) -> Dict:
    """
    中短线波段趋势判断 V4.0

    评分结构：基础分(100分) - 扣分项(含互斥衰减) + 加分项 + 板块动量修正 = 最终总分

    维度 1 短期均线趋势    30 分（趋势方向20 + 乖离惩罚0~-10）
    维度 2 短期资金异动    30 分（连板高度分层/连阳数取高分）
    维度 3 量能配合        20 分（量比10 + 量能趋势斜率5 + 量能稳定性5）
    维度 4 风险回撤        20 分（当前距10日高点回撤，5档）

    Args:
        sector_change: 个股所属题材/板块当日平均涨幅(%)，用于板块动量修正。
                       None=无数据不修正；≥3%热门+5%；≤-3%冷门-5%。

    扣分项（12项，分3组互斥/衰减）：高位追高/双空头/破位/跌停(可豁免)/缩量/放量滞涨/量价背离等
    加分项（5项）：多头排列/倍量突破/连续阳线/口袋支点/低位蓄力
    板块动量修正：sector_change 经 _calc_sector_factor 分档映射为 ±5% 修正系数，
                  与 chip_factor / pattern_factor 叠加后钳制在 TREND_TOTAL_FACTOR_CAP=0.15
                  （由 scan_trend_scan_results 基于 stock_concept_tags 预计算注入）
    """
    # ========== 数据完整性检查 ==========
    if not kline_data or len(kline_data) < TREND_MIN_KLINE_DAYS:
        return {
            'isUp': False, 'score': 0, 'reason': f'数据不足，需要至少{TREND_MIN_KLINE_DAYS}日K线',
            'details': {}, 'deducts': [], 'exclusions': []
        }

    closes = [float(k[2]) for k in kline_data]
    opens = [float(k[1]) for k in kline_data]
    highs = [float(k[3]) for k in kline_data]
    lows = [float(k[4]) for k in kline_data]
    vols = [float(k[5]) if len(k) > 5 and k[5] else 0.0 for k in kline_data]
    n = len(kline_data)
    latest_idx = n - 1
    latest_close = closes[latest_idx]
    cv = None  # P2-1: 量能稳定性CV，D3子维度3计算后覆盖（预初始化避免NameError）

    # 优先使用实时价（盘中校准）
    if USE_INTRADAY_BREAK_CHECK and realtime_price and realtime_price > 0:
        check_price = realtime_price
        is_realtime = True
    else:
        check_price = latest_close
        is_realtime = False

    ma20 = calculate_ma(kline_data, 20)
    ma20_val = ma20[latest_idx] if ma20[latest_idx] else None

    # 近 20 日振幅（用于扣分项）
    win20_start_idx = max(0, n - 20)
    last20_highs = highs[win20_start_idx:]
    last20_lows = lows[win20_start_idx:]
    if last20_highs and last20_lows:
        win20_high = max(last20_highs)
        win20_low = min(last20_lows)
        amplitude_20d = (win20_high - win20_low) / win20_low * 100 if win20_low > 0 else 0
    else:
        amplitude_20d = 0

    # ========== 强制排除项（在评分前先过） ==========
    exclusions = []
    # 1. ST / *ST 风险
    if stock_name and (any(kw in stock_name for kw in DELISTING_KEYWORDS) or any(kw in stock_name for kw in FORBIDDEN_KEYWORDS)):
        exclusions.append(f'ST/退市风险股（{stock_name}）')
    # 2. 重大利空公告
    if negative_announcement and negative_announcement.get('hasNegative'):
        hits = negative_announcement.get('hits', [])[:3]
        exclusions.append(f'近20日有重大利空公告：{"；".join(hits)[:120]}')
    # 3. 近 20 日出现连续 3 个跌停
    consecutive_limit_down = _check_consecutive_limit_down(opens, closes, win20_start_idx, n)
    if consecutive_limit_down:
        exclusions.append('近20日出现连续3个跌停')
    # 4. 总市值 < 20 亿（小微盘庄股）
    if market_cap is not None and market_cap < TREND_EXCLUDE_MARKET_CAP_MIN:
        exclusions.append(f'总市值仅{market_cap:.1f}亿（<{TREND_EXCLUDE_MARKET_CAP_MIN}亿）')

    if exclusions:
        return {
            'isUp': False, 'score': 0, 'reason': '强制排除：' + '；'.join(exclusions),
            'details': {'amplitude20d': round(amplitude_20d, 2), 'checkPrice': check_price},
            'deducts': [], 'exclusions': exclusions
        }

    # ========== P2-2 量价形态识别 + P3-1 动态阈值预计算（2026-06-30）==========
    # 提前计算均量用于形态识别（D3 会重新计算相同值，不影响正确性）
    _pat_avg_vol_20 = sum(vols[max(0, n - 20):]) / min(20, n) if n > 0 else 0
    _pat_win60_start = max(0, n - 60)
    _pat_avg_vol_60 = sum(vols[_pat_win60_start:]) / max(1, n - _pat_win60_start) if n - _pat_win60_start > 0 else 0
    pattern_result = pattern_info or detect_volume_price_pattern(
        closes, opens, highs, lows, vols, _pat_avg_vol_20, _pat_avg_vol_60, latest_idx, amplitude_20d)
    pattern_factor = pattern_result['factor']
    dyn_thresh = _get_dynamic_thresholds(market_regime, pattern_result['pattern'])

    # ========== 计算额外均线（MA60、MA120 用于扣分/加分） ==========
    ma60 = calculate_ma(kline_data, 60)
    ma120 = calculate_ma(kline_data, 120)
    # P2-1: 提前取 MA60/MA120 最新值，供筹码修正系数 _calc_chip_factor 引用
    # （原 line 498 处的赋值已移至此处，ma60_down/ma120_down 在 line 335 用数组计算，不受影响）
    ma60_val = ma60[latest_idx] if latest_idx < len(ma60) and ma60[latest_idx] else None
    ma120_val = ma120[latest_idx] if latest_idx < len(ma120) and ma120[latest_idx] else None

    # 计算 250 日分位（现价在近250日收盘价中的百分位）
    position_pct = None
    if n >= 250:
        win250_closes = closes[-250:]
        lower = sum(1 for c in win250_closes if c <= check_price)
        position_pct = lower / 250 * 100
    elif n >= 60:
        win_all = closes[-min(60, n):]
        lower = sum(1 for c in win_all if c <= check_price)
        position_pct = lower / len(win_all) * 100

    # ========== 维度 1：短期均线趋势（满分 30 分 = 趋势方向 20 + 乖离惩罚 0~-10） ==========
    # 趋势方向分：近10日 vs MA20 的位置关系 + MA20 斜率
    s_d1_trend = 0
    if ma20_val:
        above_count_10 = sum(1 for i in range(max(0, latest_idx - 9), latest_idx + 1) if closes[i] > ma20[i]) if latest_idx >= 0 else 0
        ma20_rising_5d = True
        if latest_idx >= 5 and all(ma20[i] is not None for i in range(latest_idx - 4, latest_idx + 1)):
            for i in range(latest_idx - 3, latest_idx + 1):
                if ma20[i] <= ma20[i - 1]:
                    ma20_rising_5d = False
                    break
        else:
            ma20_rising_5d = False
        if above_count_10 >= 10 and ma20_rising_5d:
            s_d1_trend = TREND_D1_TREND_FULL  # 20
        else:
            brief_break_count = 0
            brief_break_recovered = True
            for i in range(max(0, latest_idx - 6), latest_idx + 1):
                if closes[i] <= ma20[i]:
                    brief_break_count += 1
                    if i + 1 <= latest_idx and closes[i + 1] <= ma20[i + 1]:
                        brief_break_recovered = False
            ma20_flat_or_up = True
            if latest_idx >= 2 and all(ma20[i] is not None for i in range(latest_idx - 2, latest_idx + 1)):
                if ma20[latest_idx] < ma20[latest_idx - 2]:
                    ma20_flat_or_up = False
            if brief_break_count <= 1 and brief_break_recovered and ma20_flat_or_up:
                s_d1_trend = TREND_D1_TREND_PARTIAL  # 15
            elif check_price > ma20_val:
                s_d1_trend = TREND_D1_TREND_BARE  # 8

    # 乖离率惩罚：当前价偏离MA20过远 → 追高风险
    d1_divergence_penalty = 0
    if ma20_val and ma20_val > 0:
        divergence = abs(check_price - ma20_val) / ma20_val * 100
        for thr, penalty in TREND_D1_DIVERGENCE_PENALTY_TIERS:
            if divergence >= thr:
                d1_divergence_penalty = penalty
                break
    s_d1 = max(0, s_d1_trend + d1_divergence_penalty)

    # ========== 维度 2：短期资金异动（30 分，连板高度分层 + 连阳分取高） ==========
    # P0-2 升级（2026-06-30）：业界龙头战法核心是"龙一溢价 > 龙二 > 龙三"，
    # 用连板高度（最大连续涨停数）替代单纯涨停次数，识别题材内龙头位置
    # ③ 近 20 日涨停次数 + 最大连板数
    limit_up_20d = 0
    for i in range(win20_start_idx, n):
        prev_c = closes[i - 1] if i > 0 else opens[i]
        change_pct = (closes[i] - prev_c) / prev_c * 100 if prev_c > 0 else 0
        if change_pct >= TREND_LIMIT_UP_THRESHOLD:
            limit_up_20d += 1
    max_consecutive_boards = _count_max_consecutive_boards(closes, win20_start_idx, n)

    # 连板高度分层评分：5连板+=市场总龙头30；3-4连板=板块龙头25；2连板=替补龙20；首板=12
    s_board = 0
    for cnt, sc in TREND_D2_CONSECUTIVE_BOARD_TIERS:
        if max_consecutive_boards >= cnt:
            s_board = sc
            break
    # 无连板时 fallback 到涨停次数评分（旧逻辑保留，覆盖"多次涨停但非连板"场景）
    if s_board == 0 and limit_up_20d > 0:
        for cnt, sc in TREND_D2_LIMIT_UP_TIERS:
            if limit_up_20d >= cnt:
                s_board = sc
                break

    # ④ 连续阳线得分（与连板分取高）
    has_2_consecutive_board = max_consecutive_boards >= 2
    consecutive_yang_count = _count_consecutive_yang(closes, win20_start_idx, n)
    s_yang = 0
    if has_2_consecutive_board:
        s_yang = 30
    else:
        for cnt, sc in TREND_D2_YANG_LINE_TIERS:
            if consecutive_yang_count >= cnt:
                s_yang = sc
                break
    s_d2 = max(s_board, s_yang)

    # ========== 维度 3：量能配合（满分 20 分 = 量比 10 + 量能趋势 5 + 稳定性 5） ==========
    win60_start_idx = max(0, n - 60)
    avg_vol_20 = sum(vols[max(0, n - 20):]) / min(20, n) if n > 0 else 0
    avg_vol_60 = sum(vols[win60_start_idx:]) / max(1, n - win60_start_idx) if n - win60_start_idx > 0 else 0
    vol_ratio = avg_vol_20 / avg_vol_60 if avg_vol_60 > 0 else 0

    # 子维度 1：量比得分（0~10）
    s_d3_ratio = 0
    for ratio, sc in TREND_D3_RATIO_TIERS:
        if vol_ratio >= ratio:
            s_d3_ratio = sc
            break

    # 子维度 2：量能趋势斜率（0~5）— 近20日5日滚动均量的线性回归斜率
    s_d3_slope = 0
    if n >= 25:
        rolling_avgs = []
        for i in range(max(0, n - 20), n):
            start_i = max(0, i - 4)
            window_vols = vols[start_i:i + 1]
            rolling_avgs.append(sum(window_vols) / len(window_vols))
        if len(rolling_avgs) >= 5 and sum(rolling_avgs) > 0:
            mean_vol = sum(rolling_avgs) / len(rolling_avgs)
            x_mean = (len(rolling_avgs) - 1) / 2
            num = sum((i - x_mean) * (rolling_avgs[i] - mean_vol) for i in range(len(rolling_avgs)))
            den = sum((i - x_mean) ** 2 for i in range(len(rolling_avgs)))
            if den > 0:
                raw_slope = num / den
                norm_slope = raw_slope / mean_vol if mean_vol > 0 else 0  # 归一化斜率
                for thr, sc in TREND_D3_SLOPE_TIERS:
                    if norm_slope >= thr:
                        s_d3_slope = sc
                        break

    # 子维度 3：量能稳定性（0~5）— 近20日 CV 越小越健康
    s_d3_stability = 0
    recent_vols = vols[max(0, n - 20):]
    if len(recent_vols) >= 5:
        mean_v = sum(recent_vols) / len(recent_vols)
        if mean_v > 0:
            std_v = (sum((v - mean_v) ** 2 for v in recent_vols) / len(recent_vols)) ** 0.5
            cv = std_v / mean_v
            for thr, sc in TREND_D3_STABILITY_TIERS:
                if cv <= thr:
                    s_d3_stability = sc
                    break

    # 检查"价涨量增"：近5日收盘价整体上涨（量比满分但价不涨降一档）
    price_rising = False
    if latest_idx >= 4:
        price_rising = closes[latest_idx] > closes[latest_idx - 4]
    if s_d3_ratio >= 10 and not price_rising:
        s_d3_ratio = 8  # 量够但价不涨，降一档

    s_d3 = s_d3_ratio + s_d3_slope + s_d3_stability

    # ========== 维度 4：风险回撤（20 分，5 档） ==========
    # 使用"当前距近10日高点回撤"替代"20日历史最大回撤"
    # V型反转/已创新高的股票当前回撤≈0得满分，避免被历史回撤误伤
    recent_peak_10d = max(closes[max(0, latest_idx - 9):latest_idx + 1])
    current_dd = (recent_peak_10d - check_price) / recent_peak_10d * 100 if recent_peak_10d > 0 else 0
    s_d4 = 0
    for thr, sc in dyn_thresh['D4_DRAWDOWN_TIERS']:
        if current_dd < thr:
            s_d4 = sc
            break

    # ========== 基础分合计 ==========
    base_score = s_d1 + s_d2 + s_d3 + s_d4

    # ========== 扣分项（12 项，可叠加） ==========
    deducts = []

    # (1) 现价位于250日分位 ≥ 80%（年度高位）
    # 豁免：近20日涨幅≥20%视为"新高突破"，强势信号不扣分
    if position_pct is not None and position_pct >= 80:
        gain_20d = (check_price - closes[win20_start_idx]) / closes[win20_start_idx] * 100 if win20_start_idx < n and closes[win20_start_idx] > 0 else 0
        if gain_20d < TREND_POSITION_BREAKTHROUGH_GAIN:
            base_score -= TREND_DEDUCT_POSITION_HIGH
            deducts.append(('年度高位(分位{:d}%)'.format(int(position_pct)), TREND_DEDUCT_POSITION_HIGH))

    # (2) MA60 与 MA120 同步向下（中长期双空头）
    ma60_down = _ma_slope_down(ma60, latest_idx, window=5)
    ma120_down = _ma_slope_down(ma120, latest_idx, window=5)
    if ma60_down and ma120_down:
        base_score -= TREND_DEDUCT_MA60_MA120_BEAR
        deducts.append(('MA60+MA120双空头', TREND_DEDUCT_MA60_MA120_BEAR))
    elif position_pct is not None and 60 <= position_pct < 80:
        base_score -= TREND_DEDUCT_POSITION_MID
        deducts.append(('中高位(分位{:d}%)'.format(int(position_pct)), TREND_DEDUCT_POSITION_MID))
    elif ma60_down and not ma120_down:
        base_score -= TREND_DEDUCT_POSITION_MID
        deducts.append(('MA60向下/MA120向上', TREND_DEDUCT_POSITION_MID))

    # (3) 近 5 日跌破 MA20 且 3 日未收回
    ma20_broken = _check_ma20_recently_broken(closes, ma20, latest_idx, window=5, recovery_days=3)
    if ma20_broken:
        base_score -= TREND_DEDUCT_MA20_BROKEN
        deducts.append(('近5日跌破MA20未收回', TREND_DEDUCT_MA20_BROKEN))

    # (4) 近 20 日出现单日跌停（豁免：近3日≥2次涨停 → 洗盘/利空出尽后的强势修复）
    recent_boards = 0
    for i in range(max(0, latest_idx - 2), latest_idx + 1):
        if i > 0:
            prev_c = closes[i - 1]
            change_pct = (closes[i] - prev_c) / prev_c * 100 if prev_c > 0 else 0
            if change_pct >= TREND_LIMIT_UP_THRESHOLD:
                recent_boards += 1
    if recent_boards < 2 and _check_limit_down_in_window(opens, closes, win20_start_idx, n):
        base_score -= TREND_DEDUCT_LIMIT_DOWN
        deducts.append(('近20日单日跌停', TREND_DEDUCT_LIMIT_DOWN))

    # (5) 持续缩量阴跌
    if _check_shrink_volume(vols, avg_vol_60, n, days=5, ratio=0.5):
        base_score -= TREND_DEDUCT_SHRINK_VOL
        deducts.append(('近5日缩量阴跌', TREND_DEDUCT_SHRINK_VOL))

    # (6) 放量滞涨（单日成交量创20日新高 + 股价无力上涨 -1%~1%，排除大跌恐慌盘和涨停进攻）
    if _check_volume_spike(vols, closes, win20_start_idx, n):
        base_score -= dyn_thresh['DEDUCT_VOLUME_SPIKE']
        deducts.append(('放量滞涨', dyn_thresh['DEDUCT_VOLUME_SPIKE']))

    # (7) 量价顶背离（近5日股价微涨 + 20日均量放大超30%）
    if _check_volume_divergence(closes, vols, n):
        base_score -= TREND_DEDUCT_VOLUME_DIVERGENCE
        deducts.append(('量价顶背离', TREND_DEDUCT_VOLUME_DIVERGENCE))

    # (8) 近20日振幅≥15%且收跌（天地板/炸板）
    if amplitude_20d >= 15 and closes[latest_idx] < closes[latest_idx - 1] if latest_idx > 0 else False:
        base_score -= TREND_DEDUCT_EXTREME_VOLATILITY
        deducts.append(('天地板/炸板', TREND_DEDUCT_EXTREME_VOLATILITY))

    # (9) 单日大跌超-7%且次日未修复
    if _check_single_day_crash(opens, closes, latest_idx):
        base_score -= TREND_DEDUCT_SINGLE_DAY_CRASH
        deducts.append(('单日大跌未修复', TREND_DEDUCT_SINGLE_DAY_CRASH))

    # (10) 近20日振幅<5% — 稍后结合位置判断（移到加分后处理）

    # (11) 非主板（创业板/科创板）— 不强制排除，扣分
    if not is_mainboard:
        base_score -= TREND_DEDUCT_NON_MAIN_BOARD
        deducts.append(('非主板', TREND_DEDUCT_NON_MAIN_BOARD))

    # ========== 加分项（4 项 + 横盘位置判断，额外奖励） ==========
    bonus_score = 0
    bonuses = []

    # ① MA20 > MA60 > MA120 标准多头排列 + 现价分位 < 60%
    if _check_bull_alignment(ma20, ma60, ma120, latest_idx) and (position_pct is None or position_pct < 60):
        bonus_score += TREND_BONUS_BULL_ALIGNMENT
        bonuses.append(('多头排列+低位', TREND_BONUS_BULL_ALIGNMENT))

    # ② 近3日倍量突破（日成交量 ≥ 20日均量×2 + 涨幅≥5%）
    if _check_volume_breakout(vols, avg_vol_20, closes, n, days=3):
        bonus_score += TREND_BONUS_VOLUME_BREAKOUT
        bonuses.append(('倍量突破', TREND_BONUS_VOLUME_BREAKOUT))

    # ③ 近5日连续阳线 + 每日涨幅 ≥ 1%
    if _check_consecutive_yang_bonus(closes, latest_idx):
        bonus_score += TREND_BONUS_CONSECUTIVE_YANG
        bonuses.append(('连续阳线(≥1%/日)', TREND_BONUS_CONSECUTIVE_YANG))

    # ④ 口袋支点：倍量 + 收于当日高点附近(>80%) + 收盘价>前日高点
    if _check_pocket_pivot(opens, closes, highs, vols, avg_vol_20, latest_idx):
        bonus_score += TREND_BONUS_POCKET_PIVOT
        bonuses.append(('口袋支点', TREND_BONUS_POCKET_PIVOT))

    # ⑤ 稳步上行 vs 加速拉升（2026-07-02 B-4 新增，参考市面策略）
    # 市面经验："稳步震荡上行趋势延续周期更长，加速拉升易深度调整"
    # 稳步上行：近20日涨幅5%-15% + 阳线占比≥60% + 最大单日涨幅<5%
    # 加速拉升：近5日涨幅≥15% + 量比≥2（警示透支风险，低加分）
    if n >= 20:
        change_20d = ((closes[latest_idx] - closes[latest_idx - 19]) / closes[latest_idx - 19] * 100) if latest_idx >= 19 and closes[latest_idx - 19] > 0 else 0
        change_5d = ((closes[latest_idx] - closes[latest_idx - 4]) / closes[latest_idx - 4] * 100) if latest_idx >= 4 and closes[latest_idx - 4] > 0 else 0
        # 阳线占比（近20日）
        yang_count_20d = sum(1 for i in range(max(0, latest_idx - 19), latest_idx + 1) if closes[i] > opens[i])
        yang_ratio_20d = (yang_count_20d / 20) if latest_idx >= 19 else 0
        # 最大单日涨幅（近20日）
        max_daily_gain = 0
        for i in range(max(1, latest_idx - 19), latest_idx + 1):
            if closes[i - 1] > 0:
                dg = (closes[i] - closes[i - 1]) / closes[i - 1] * 100
                if dg > max_daily_gain:
                    max_daily_gain = dg
        # 量比（近5日均值 vs 20日均量）
        vol_ratio_5d = (sum(vols[max(0, latest_idx - 4):latest_idx + 1]) / 5 / avg_vol_20) if avg_vol_20 > 0 and latest_idx >= 4 else 0

        # 稳步上行优先（互斥，稳步上行加分更高）
        if 5 <= change_20d <= 15 and yang_ratio_20d >= 0.6 and max_daily_gain < 5:
            bonus_score += TREND_BONUS_STEADY_UPTREND
            bonuses.append(('稳步上行', TREND_BONUS_STEADY_UPTREND))
        elif change_5d >= 15 and vol_ratio_5d >= 2:
            bonus_score += TREND_BONUS_ACCELERATING
            bonuses.append(('加速拉升(警示)', TREND_BONUS_ACCELERATING))

    # ⑥ 横盘位置判断（移到此处，bonus_score 已定义）
    if amplitude_20d < TREND_RANGE_20D_FLAT_MIN:
        if position_pct is not None and position_pct < TREND_SIDEWAYS_LOW_PCT:
            bonus_score += TREND_SIDEWAYS_BONUS
            bonuses.append(('低位蓄力(横盘)', TREND_SIDEWAYS_BONUS))
        elif position_pct is not None and position_pct < TREND_SIDEWAYS_MID_PCT:
            base_score -= dyn_thresh['DEDUCT_AMPLITUDE_FLAT'] // 2
            deducts.append(('中位横盘(<5%)', dyn_thresh['DEDUCT_AMPLITUDE_FLAT'] // 2))
        else:
            base_score -= dyn_thresh['DEDUCT_AMPLITUDE_FLAT']
            deducts.append(('高位横盘(<5%)', dyn_thresh['DEDUCT_AMPLITUDE_FLAT']))

    # ========== 扣分项互斥/衰减（同类风险只取扣分最大的一项） ==========
    deduct_names = {d[0]: d[1] for d in deducts}
    for group_name, keys in TREND_DEDUCT_GROUPS.items():
        triggered = [(k, deduct_names.get(k, 0)) for k in keys if k in deduct_names]
        if len(triggered) >= 2:
            # 取扣分最大的保留，其余退还
            triggered.sort(key=lambda x: x[1], reverse=True)
            for k, pts in triggered[1:]:
                base_score += pts  # 退还扣分
                deducts = [d for d in deducts if d[0] != k]  # 移除非最大扣分项

    # ========== 板块动量因子（环境加成/折价 ±5%） ==========
    # P0-1 实现（2026-06-30）：基于个股概念标签的同题材平均涨幅做环境修正
    # sector_change 由 scan_trend_scan_results 预计算并注入（基于 stock_concept_tags）
    # None=无数据不修正；≥3%热门+5%；≥1%偏热+3%；≤-3%冷门-5%；≤-1%偏冷-3%
    sector_factor = _calc_sector_factor(sector_change)

    # ========== P2-1 筹码结构修正（代理指标，2026-06-30）==========
    # 用 MA60/MA120 成本压力位 + 换手率筹码松动度 + 主力净流入 + 量能稳定性CV 组合
    # 作为乘性修正系数叠加（与板块动量同族），钳制在 [-0.08, 0.08]
    chip_factor = _calc_chip_factor(check_price, ma60_val, ma120_val,
                                    turnover_rate, main_inflow_ratio, cv)

    # 总分（下界 0，封顶 100，含板块动量修正）
    # 修复：原本 raw_score = max(0, base + bonus) 无上限，涨停龙头可拿到 135+，融合后 6 只全 100
    # 新版：基础分满分 100 不变；加分项最大 15 → 缩放为 0-5 的"附加奖励"叠加在基础分上，
    # 板块动量 + 筹码 + 形态三因子叠加钳制在 TREND_TOTAL_FACTOR_CAP=0.15 后做乘性修正。这样：
    # - 基础分非顶格的票 trend_score < 100（区分明显）
    # - 基础分顶格的票 trend_score = 100 + 加分缩放(0~5) → 仍封顶 100，但内部用附加分区分
    # - 三因子修正后封顶 100（不再溢出）
    base_score_capped = min(100, base_score)
    # 稳步上行与加速拉升互斥（line 451-455 if/else），取稳步上行作为上限代表；
    # 加速拉升 +1 不会同时出现，故 max_bonus 不含 TREND_BONUS_ACCELERATING
    max_bonus = (TREND_BONUS_BULL_ALIGNMENT + TREND_BONUS_VOLUME_BREAKOUT +
                 TREND_BONUS_CONSECUTIVE_YANG + TREND_BONUS_POCKET_PIVOT +
                 TREND_BONUS_STEADY_UPTREND + TREND_SIDEWAYS_BONUS)  # = 23（2026-07-02 B-2 调整后）
    bonus_contrib = (bonus_score / max_bonus) * 5 if max_bonus > 0 else 0
    raw_score = base_score_capped + bonus_contrib  # 100 + (0~5) = 100~105
    raw_score = min(100, raw_score)  # 封顶
    # P2-1/P2-2/P0-1 三修正系数叠加（封顶 TREND_TOTAL_FACTOR_CAP=0.15，防溢出）
    # total = raw × (1 + min(0.15, sector_factor + chip_factor + pattern_factor))
    total_factor = min(TREND_TOTAL_FACTOR_CAP, sector_factor + chip_factor + pattern_factor)
    total_score = round(raw_score * (1 + total_factor), 1)
    total_score = min(100, max(0, total_score))

    # ========== 入选判断 ==========
    # 过滤规则 1：现价在 MA20 下方 + 60 日累计跌幅 > 15% → 强制排除（原有逻辑保留）
    if ma20_val and check_price < ma20_val:
        recent60_close = closes[max(0, n - 60)]
        if recent60_close > 0:
            drop_60d = (check_price - recent60_close) / recent60_close * 100
            if drop_60d < -15:
                return {
                    'isUp': False, 'score': total_score,
                    'reason': f'中期下跌过滤：60日累计跌幅{drop_60d:.1f}%，且现价位于MA20下方',
                    'details': {}, 'deducts': [{'name': d[0], 'points': d[1]} for d in deducts],
                    'bonuses': [{'name': b[0], 'points': b[1]} for b in bonuses],
                    'ma20': ma20[-10:] if len(ma20) >= 10 else ma20,
                    'latestPrice': latest_close, 'checkPrice': check_price,
                }
    
    # 过滤规则 2：下跌趋势中的短暂反弹过滤（v5.3 增强）
    # 条件：① 中长期均线结构偏空（满足任意一项即视为偏空）：
    #          a. MA60 + MA120 双空头（5日斜率均向下）
    #          b. MA60 < MA120 死亡交叉未修复
    #          c. 现价 < MA120（股价尚未收复半年线，中长期趋势未逆转）
    #        ② 现价距全量历史最高点回撤 > 20%
    # → 即使近期反弹至 MA20 甚至 MA60 上方，中长期趋势仍偏空，
    #   属于"下跌中继反弹"而非趋势反转，强制排除
    # 
    # v5.2→v5.3 改动说明：
    #   和邦生物 (sh603077)：MA60(2.725) > MA120(2.642) 形成伪金叉
    #   + MA120 已走平，双重检测全部返回 False，过滤被完全绕过。
    #   实测 score=62 isUp=True，但该股距历史高点 3.54 回撤 29.4%，
    #   且现价 2.5 仍在 MA120(2.642) 之下，属于典型下跌中继反弹。
    # v5.3 新增条件 c：股价 < MA120 也是中长期偏空的有效信号。
    # 注：ma60_val/ma120_val 已在 line 163-164 提前赋值（P2-1 筹码修正需要）

    ma_long_bearish = ma60_down and ma120_down
    if not ma_long_bearish:
        # 死亡交叉（MA60 < MA120）是稳健的中长期熊市信号
        if ma60_val is not None and ma120_val is not None and ma60_val < ma120_val:
            ma_long_bearish = True
    if not ma_long_bearish:
        # 股价尚未收复 MA120（半年线）：中长期趋势仍未逆转
        # 典型案例：和邦生物 MA60 > MA120 伪金叉 + MA120 走平，
        # 但现价 2.5 仍在 MA120(2.642) 下方，说明中长期均线组整体
        # 对股价形成压制，反弹只是短暂的
        if ma120_val is not None and check_price < ma120_val:
            ma_long_bearish = True

    if ma_long_bearish:
        # 使用全量历史数据找最高点，而非仅限 60 日内
        all_time_high = max(highs) if highs else 0
        peak_drawdown = (all_time_high - check_price) / all_time_high * 100 if all_time_high > 0 else 0
        if peak_drawdown > 20:
            signal_desc = []
            if ma60_down and ma120_down:
                signal_desc.append('双空头')
            elif ma60_val is not None and ma120_val is not None and ma60_val < ma120_val:
                signal_desc.append('死亡交叉')
            elif ma120_val is not None and check_price < ma120_val:
                signal_desc.append(f'股价仍低于MA120({ma120_val:.2f})')
            return {
                'isUp': False, 'score': total_score,
                'reason': (f'下跌趋势中的短暂反弹：{",".join(signal_desc)}，'
                           f'距全量历史高点{all_time_high:.2f}回撤{peak_drawdown:.1f}%'),
                'details': {}, 'deducts': [{'name': d[0], 'points': d[1]} for d in deducts],
                'bonuses': [{'name': b[0], 'points': b[1]} for b in bonuses],
                'latestPrice': latest_close, 'checkPrice': check_price,
            }

    is_trend_up = total_score >= TREND_IS_UP_MIN_SCORE

    # ========== 连涨/连跌天数（仅供前端展示，与评分无关） ==========
    # 用 check_price 替换 K 线最后一天的 close 作为"今天"，再从最新一天往前数
    # 这样盘中时不会因为 K 线接口滞后导致连涨/连跌误算
    closes_for_count = list(closes)
    if is_realtime:
        closes_for_count[-1] = check_price
    consecutive_up_days = 0
    consecutive_down_days = 0
    for i in range(len(closes_for_count) - 1, 0, -1):
        if closes_for_count[i] > closes_for_count[i - 1]:
            consecutive_up_days += 1
        else:
            break
    for i in range(len(closes_for_count) - 1, 0, -1):
        if closes_for_count[i] < closes_for_count[i - 1]:
            consecutive_down_days += 1
        else:
            break

    details = {
        # 维度 1
        'sD1': s_d1,
        'sD1Trend': s_d1_trend,
        'd1DivergencePenalty': d1_divergence_penalty,
        'ma20': round(ma20_val, 2) if ma20_val else None,
        # 维度 2
        'sD2': s_d2,
        'limitUpCount20d': limit_up_20d,
        'maxConsecutiveBoards': max_consecutive_boards,  # P0-2: 最大连板数（龙头辨识）
        'consecutiveYangCount': consecutive_yang_count,
        'has2ConsecutiveBoard': has_2_consecutive_board,
        # 维度 3
        'sD3': s_d3,
        'sD3Ratio': s_d3_ratio,
        'sD3Slope': s_d3_slope,
        'sD3Stability': s_d3_stability,
        'avgVol20': round(avg_vol_20, 0),
        'avgVol60': round(avg_vol_60, 0),
        'volRatio': round(vol_ratio, 2),
        # 维度 4
        'sD4': s_d4,
        'currentDrawdown': round(current_dd, 2),
        'amplitude20d': round(amplitude_20d, 2),
        # 250日分位
        'positionPct': round(position_pct, 1) if position_pct is not None else None,
        # MA60/MA120
        'ma60Down': ma60_down,
        'ma120Down': ma120_down,
        # 通用
        'latestClose': latest_close,
        'checkPrice': check_price,
        'isRealtime': is_realtime,
        # 展示用：连涨/连跌天数
        'consecutiveUpDays': consecutive_up_days,
        'consecutiveDownDays': consecutive_down_days,
        # 加分
        'bonusScore': bonus_score,
        # 板块动量因子（P0-1）
        'sectorChange': round(sector_change, 2) if sector_change is not None else None,
        'sectorFactor': round(sector_factor, 2),
        # P2-1 筹码结构修正系数
        'chipFactor': round(chip_factor, 3),
        # P2-2 量价形态
        'pattern': pattern_result['pattern'],
        'patternDesc': pattern_result['desc'],
        'patternFactor': round(pattern_factor, 3),
        # P3-1 动态阈值市场状态
        'marketRegime': market_regime,
        # 三修正系数叠加总值（封顶 0.15）
        'totalFactor': round(min(TREND_TOTAL_FACTOR_CAP, sector_factor + chip_factor + pattern_factor), 3),
    }

    return {
        'isUp': is_trend_up,
        'score': total_score,
        'details': details,
        'deducts': [{'name': d[0], 'points': d[1]} for d in deducts],
        'bonuses': [{'name': b[0], 'points': b[1]} for b in bonuses],
        'ma20': ma20[-10:] if len(ma20) >= 10 else ma20,
        'latestPrice': latest_close,
        'checkPrice': check_price,
        'isRealtime': is_realtime,
        'recent5Days': closes[-5:] if len(closes) >= 5 else closes,
    }


def _count_max_consecutive_boards(closes: List[float], start: int, end: int) -> int:
    """区间内最大连续涨停数（连板高度）。
    P0-2 新增（2026-06-30）：用于龙头辨识——5连板+=市场总龙头，3-4连板=板块龙头。
    涨停判定：当日涨幅 ≥ TREND_LIMIT_UP_THRESHOLD（默认 9.8%）
    """
    n = end - start
    if n < 1:
        return 0
    max_run = 0
    run = 0
    for i in range(start + 1, end):
        prev_c = closes[i - 1]
        change_pct = (closes[i] - prev_c) / prev_c * 100 if prev_c > 0 else 0
        if change_pct >= TREND_LIMIT_UP_THRESHOLD:
            run += 1
            if run > max_run:
                max_run = run
        else:
            run = 0
    return max_run


def _count_consecutive_yang(closes: List[float], start: int, end: int) -> int:
    """区间内最长连续阳线天数（closes[i] > closes[i-1]）"""
    n = end - start
    if n < 1:
        return 0
    max_run = 0
    run = 0
    for i in range(start + 1, end):
        if closes[i] > closes[i - 1]:
            run += 1
            if run > max_run:
                max_run = run
        else:
            run = 0
    return max_run


def _check_ma20_recently_broken(closes: List[float], ma20: List[Optional[float]],
                                 latest_idx: int, window: int = 5, recovery_days: int = 3) -> bool:
    """
    近 window 个交易日内：是否有"跌破 MA20 且 recovery_days 日内未收回"。
    实现：在 [latest_idx-window+1, latest_idx] 区间内
      找跌破点（closes[i] < ma20[i]），如果存在跌破点且距 latest_idx < recovery_days，则视为未收回。
    """
    if latest_idx < 1 or window < 1:
        return False
    lo = max(1, latest_idx - window + 1)
    for i in range(lo, latest_idx + 1):
        if ma20[i] and closes[i] < ma20[i]:
            # 跌破点距当前 < recovery_days → 未收回
            if (latest_idx - i) < recovery_days:
                return True
    return False


def _check_limit_down_in_window(opens: List[float], closes: List[float], start: int, end: int) -> bool:
    """在区间内是否有单日跌停（涨幅 ≤ -9.8%）"""
    for i in range(start, end):
        prev_c = closes[i - 1] if i > 0 else opens[i]
        if prev_c <= 0:
            continue
        change_pct = (closes[i] - prev_c) / prev_c * 100
        if change_pct <= TREND_LIMIT_DOWN_THRESHOLD:
            return True
    return False


def _check_shrink_volume(vols: List[float], avg_vol_60: float, n: int,
                         days: int = 5, ratio: float = 0.5) -> bool:
    """最近 days 日成交量持续 < 60日均量 × ratio"""
    if n < days or avg_vol_60 <= 0:
        return False
    for i in range(n - days, n):
        if vols[i] >= avg_vol_60 * ratio:
            return False
    return True


def _ma_slope_down(ma: List[Optional[float]], idx: int, window: int = 5) -> bool:
    """检查均线在窗口内是否向下（近期均值 < 远期均值）"""
    if idx < 2 * window:
        return False
    if any(ma[idx - k] is None for k in range(2 * window)):
        return False
    recent = sum(ma[idx - k] for k in range(window)) / window
    older = sum(ma[idx - window - k] for k in range(window)) / window
    return recent < older


def _check_volume_spike(vols: List[float], closes: List[float], start: int, end: int) -> bool:
    """单日成交量创20日新高 + 股价无力上涨（涨幅<1%且跌幅≤1%）→ 放量滞涨
    排除条件：大跌放量（跌幅>1%）是恐慌盘/筹码交换，不是滞涨；涨停放量是进攻信号"""
    if end - start < 5:
        return False
    max_vol_20 = max(vols[start:end])
    if max_vol_20 <= 0:
        return False
    for i in range(start + 1, end):
        if vols[i] == max_vol_20:
            prev_c = closes[i - 1] if i > 0 else closes[i]
            change = (closes[i] - prev_c) / prev_c * 100 if prev_c > 0 else 0
            # 仅当涨幅<1%且跌幅≤1%时判定为滞涨（微涨+巨量或微跌+巨量）
            # 大跌放量是恐慌抛售/筹码交换，涨停放量是进攻，均不在此列
            if -1.0 <= change < 1.0:
                return True
    return False


def _check_volume_divergence(closes: List[float], vols: List[float], n: int) -> bool:
    """近5日股价小幅抬升 + 20日均量逐级放大超30% → 量价顶背离"""
    if n < 25:
        return False
    if closes[n - 1] <= closes[n - 5]:
        return False
    price_rise = (closes[n - 1] - closes[n - 5]) / closes[n - 5] * 100 if closes[n - 5] > 0 else 0
    if price_rise < 1.0:
        return False
    # 20日均量近5日变化
    if n >= 25:
        avg_vol_first = sum(vols[n - 25:n - 20]) / 5
        avg_vol_last = sum(vols[n - 5:n]) / 5
        if avg_vol_first > 0 and avg_vol_last > avg_vol_first * 1.3:
            return True
    return False


def _check_single_day_crash(opens: List[float], closes: List[float], idx: int) -> bool:
    """单日大跌超-7%且次日未修复"""
    if idx < 1:
        return False
    for i in [idx, idx - 1]:
        if i > 0 and opens[i] > 0:
            prev_c = closes[i - 1]
            change = (closes[i] - prev_c) / prev_c * 100 if prev_c > 0 else 0
            if change <= -7.0:
                # 次日是否修复
                if i + 1 <= idx:
                    next_prev = closes[i] if closes[i] > 0 else prev_c
                    next_change = (closes[i + 1] - next_prev) / next_prev * 100 if next_prev > 0 else 0
                    if next_change > 3.0:
                        return False
                return True
    return False


def _check_consecutive_limit_down(opens: List[float], closes: List[float], start: int, end: int) -> bool:
    """近20日是否出现连续3个跌停"""
    n = end - start
    if n < 3:
        return False
    run = 0
    for i in range(start, end):
        prev_c = closes[i - 1] if i > 0 else opens[i]
        if prev_c <= 0:
            continue
        change = (closes[i] - prev_c) / prev_c * 100
        if change <= TREND_LIMIT_DOWN_THRESHOLD:
            run += 1
            if run >= 3:
                return True
        else:
            run = 0
    return False


def _check_bull_alignment(ma20: list, ma60: list, ma120: list, idx: int) -> bool:
    """MA20 > MA60 > MA120 标准多头排列"""
    if idx < 0:
        return False
    v20 = ma20[idx] if idx < len(ma20) and ma20[idx] is not None else None
    v60 = ma60[idx] if idx < len(ma60) and ma60[idx] is not None else None
    v120 = ma120[idx] if idx < len(ma120) and ma120[idx] is not None else None
    if v20 and v60 and v120:
        return v20 > v60 > v120
    return False


def _check_volume_breakout(vols: List[float], avg_vol_20: float, closes: List[float],
                           n: int, days: int = 3) -> bool:
    """近N日出现倍量突破（日成交量≥20日均量×2 + 涨幅≥5%）"""
    if n < days or avg_vol_20 <= 0:
        return False
    for i in range(max(0, n - days), n):
        if vols[i] >= avg_vol_20 * 2:
            prev_c = closes[i - 1] if i > 0 else closes[0]
            if prev_c > 0:
                change = (closes[i] - prev_c) / prev_c * 100
                if change >= 5.0:
                    return True
    return False


def _check_consecutive_yang_bonus(closes: List[float], idx: int) -> bool:
    """近5日连续阳线 + 每日涨幅≥1%"""
    if idx < 5:
        return False
    for i in range(idx - 4, idx + 1):
        if i <= 0:
            continue
        prev_c = closes[i - 1]
        if prev_c <= 0:
            return False
        change = (closes[i] - prev_c) / prev_c * 100
        if change < 1.0:
            return False
    return True


def _check_pocket_pivot(opens: List[float], closes: List[float], highs: List[float],
                         vols: List[float], avg_vol_20: float, idx: int) -> bool:
    """口袋支点：倍量突破 + 收于当日高点附近(>80%位置) + 收盘价>前日高点"""
    if idx < 1 or avg_vol_20 <= 0:
        return False
    if vols[idx] < avg_vol_20 * 1.5:
        return False
    today_range = highs[idx] - opens[idx] if opens[idx] > 0 else highs[idx] - closes[idx]
    if today_range <= 0:
        return False
    close_position = (closes[idx] - opens[idx]) / today_range if opens[idx] > 0 else 0
    if close_position < 0.8:
        return False
    if closes[idx] <= highs[idx - 1]:
        return False
    return True


def _calc_sector_factor(sector_change: Optional[float]) -> float:
    """
    计算板块动量修正系数（P0-1 实现，2026-06-30）

    基于个股所属题材/板块当日平均涨幅，对个股评分做环境修正：
    - 热门题材（≥3%）→ +5%
    - 偏热（≥1%）→ +3%
    - 中性（-1%~1%）→ 0%
    - 偏冷（≤-1%）→ -3%
    - 冷门（≤-3%）→ -5%
    - None（无数据）→ 0%（不修正）

    Args:
        sector_change: 个股所属题材当日平均涨幅(%)，由 scan_trend_scan_results
                       基于 stock_concept_tags 预计算并注入。
    """
    if sector_change is None:
        return 0.0
    for thr, factor in TREND_SECTOR_FACTOR_TIERS:
        if sector_change >= thr:
            return factor
    return TREND_SECTOR_FACTOR_COLD  # 所有阈值都未命中 → 最冷档


def _classify_market_regime(market_change: float, index_kline: Optional[List[List]]) -> str:
    """P3-1 大盘状态分类（2026-06-30）

    结合大盘指数(sh000001) 现价 vs MA20 + 当日涨幅，判定 bull/bear/range：
    - bull: 大盘在 MA20 之上 且 当日涨幅 ≥ +0.5%
    - bear: 大盘在 MA20 之下 且 当日涨幅 ≤ -0.5%
    - range: 其他情况（含数据不足）

    Args:
        market_change: 大盘当日涨跌幅(%)
        index_kline: 大盘指数 K 线序列（[date,open,close,high,low,volume]）
    Returns:
        'bull' / 'bear' / 'range'
    """
    try:
        index_above_ma20 = None
        if index_kline and len(index_kline) >= 20:
            ma20_arr = calculate_ma(index_kline, 20)
            latest_idx = len(index_kline) - 1
            ma20_val = ma20_arr[latest_idx] if latest_idx < len(ma20_arr) and ma20_arr[latest_idx] else None
            latest_close = float(index_kline[latest_idx][2])
            if ma20_val and ma20_val > 0:
                index_above_ma20 = latest_close > ma20_val

        if index_above_ma20 is True and market_change >= TREND_DYNAMIC_MARKET_BULL_CHANGE:
            return 'bull'
        if index_above_ma20 is False and market_change <= TREND_DYNAMIC_MARKET_BEAR_CHANGE:
            return 'bear'
        # MA20 不可用时退化为仅看当日涨跌
        if index_above_ma20 is None:
            if market_change >= TREND_DYNAMIC_MARKET_BULL_CHANGE:
                return 'bull'
            if market_change <= TREND_DYNAMIC_MARKET_BEAR_CHANGE:
                return 'bear'
        return 'range'
    except Exception:
        return 'range'


def _get_dynamic_thresholds(market_regime: str, pattern: str) -> Dict:
    """P3-1 三级优先合并动态阈值（2026-06-30）

    优先级：精确匹配(regime, pattern) > regime 默认 > 全局默认
    保证返回的 dict 一定含 6 个 key，供 is_up_trend 内 6 处引用：
      D1_DIVERGENCE_PENALTY_TIERS / DEDUCT_VOLUME_SPIKE / DEDUCT_MA20_BROKEN /
      D4_DRAWDOWN_TIERS / DEDUCT_VOLUME_DIVERGENCE / DEDUCT_AMPLITUDE_FLAT
    """
    # 1. 全局默认基底（拷贝，避免污染常量）
    merged = dict(TREND_DYNAMIC_DEFAULTS)
    # 2. 精确匹配覆盖（命中则 update）
    override = TREND_DYNAMIC_THRESHOLDS.get((market_regime, pattern))
    if override:
        merged.update(override)
    return merged


def _calc_chip_factor(check_price: float, ma60_val: Optional[float], ma120_val: Optional[float],
                      turnover_rate: float, main_inflow_ratio: Optional[float],
                      cv: Optional[float]) -> float:
    """P2-1 筹码结构修正系数（代理指标方案，2026-06-30）

    无真实 CYQ 筹码数据，用 4 个代理指标组合刻画筹码结构：
      子项1 成本压力位：现价 vs MA60/MA120（双线上方=无套牢盘 / 双线下方=双重套牢）
      子项2 换手率松动度：1-3%=筹码锁定 / >15%=筹码松动
      子项3 主力筹码动向：主力净流入占比 ≥3%=吸筹 / ≤-3%=派发
      子项4 量能稳定性 CV：≤0.3=沉淀 / >0.8=紊乱
    4 子项相加后钳制到 [TREND_CHIP_FACTOR_MIN, TREND_CHIP_FACTOR_MAX] = [-0.08, 0.08]
    """
    # 子项1：成本压力位
    if ma60_val is not None and ma120_val is not None:
        if check_price > ma60_val and check_price > ma120_val:
            sub_cost = TREND_CHIP_COST_ABOVE_BOTH
        elif check_price < ma60_val and check_price < ma120_val:
            sub_cost = TREND_CHIP_COST_BELOW_BOTH
        else:
            sub_cost = TREND_CHIP_COST_BETWEEN
    else:
        sub_cost = TREND_CHIP_COST_BETWEEN  # MA 数据不足，按中性处理

    # 子项2：换手率筹码松动度
    if 1.0 <= turnover_rate <= 3.0:
        sub_turnover = TREND_CHIP_TURNOVER_LOCKED
    elif turnover_rate > 15.0:
        sub_turnover = TREND_CHIP_TURNOVER_LOOSE
    else:
        sub_turnover = TREND_CHIP_TURNOVER_NORMAL

    # 子项3：主力筹码动向
    if main_inflow_ratio is None:
        sub_main = TREND_CHIP_MAIN_NEUTRAL
    elif main_inflow_ratio >= 3.0:
        sub_main = TREND_CHIP_MAIN_ACCUMULATE
    elif main_inflow_ratio <= -3.0:
        sub_main = TREND_CHIP_MAIN_DISTRIBUTE
    else:
        sub_main = TREND_CHIP_MAIN_NEUTRAL

    # 子项4：量能稳定性 CV（D3 子维度3 计算，可能为 None）
    if cv is None:
        sub_cv = TREND_CHIP_CV_NORMAL
    elif cv <= 0.3:
        sub_cv = TREND_CHIP_CV_STABLE
    elif cv > 0.8:
        sub_cv = TREND_CHIP_CV_VOLATILE
    else:
        sub_cv = TREND_CHIP_CV_NORMAL

    raw = sub_cost + sub_turnover + sub_main + sub_cv
    return max(TREND_CHIP_FACTOR_MIN, min(TREND_CHIP_FACTOR_MAX, raw))


def calc_pool_stock_indicators(kline_data: List[List], realtime_price: float = 0) -> Dict:
    """
    计算股票池展示所需的轻量级指标：连涨/连跌天数、是否在5日线上方、最新价。
    不进行趋势综合评分，只返回核心展示数据。
    """
    if not kline_data or len(kline_data) < 10:
        return {'latestPrice': realtime_price, 'consecutiveUp': 0, 'consecutiveDown': 0, 'aboveMa5': False, 'ma5': None}

    closes = [float(k[2]) for k in kline_data]
    ma5_arr = calculate_ma(kline_data, 5)
    latest_idx = len(kline_data) - 1
    latest_close = closes[latest_idx]

    if realtime_price and realtime_price > 0:
        check_price = realtime_price
        # 用实时价替换 K 线最后一天 close 作为"今天"的代表价
        # （盘中时 K 线 API 返回的"最后一天"可能仍是昨收，会导致连涨/连跌误算）
        closes[-1] = realtime_price
    else:
        check_price = latest_close

    ma5_val = ma5_arr[latest_idx] if ma5_arr[latest_idx] else None

    # 连涨天数（从最新一天开始往前数，实时价已替换最后一天）
    consecutive_up = 0
    for i in range(latest_idx, 0, -1):
        if closes[i] > closes[i - 1]:
            consecutive_up += 1
        else:
            break

    # 连跌天数（从最新一天开始往前数，实时价已替换最后一天）
    consecutive_down = 0
    for i in range(latest_idx, 0, -1):
        if closes[i] < closes[i - 1]:
            consecutive_down += 1
        else:
            break

    above_ma5 = (ma5_val is not None and check_price > ma5_val)

    return {
        'latestPrice': check_price,
        'latestClose': latest_close,
        'consecutiveUp': consecutive_up,
        'consecutiveDown': consecutive_down,
        'aboveMa5': above_ma5,
        'ma5': round(ma5_val, 2) if ma5_val else None,
    }


def check_below_ma10(kline_data: List[List]) -> Dict:
    """
    检查股票是否跌破10日线且当天没有收回

    注意：本函数由每天 15:05 的定时任务调用，作用对象是**用户股票池**（非趋势池）。
    趋势池通过 `scan_trend_scan_results` 全量替换机制自行维护跌破移除逻辑；
    本函数是用户自选股的兜底检查，避免用户股票池中长期保留已破位股票。

    判定标准：
    - 当天最低价 < MA10 → 跌破
    - 且当天收盘价 < MA10 → 未收回，应该删除（shouldRemove=True）
    - 若最低价 < MA10 但收盘价 >= MA10 → 视为盘中刺破已收复，不删除

    返回: {'shouldRemove': True/False, 'reason': '原因', 'latestClose': 收盘价, 'ma10': 10日线}
    """
    if not kline_data or len(kline_data) < 10:
        return {
            'shouldRemove': False,
            'reason': 'K线数据不足，需要至少10日数据',
            'latestClose': None,
            'ma10': None
        }
    
    # 计算MA10
    ma10_list = calculate_ma(kline_data, 10)
    latest_idx = len(kline_data) - 1
    latest_kline = kline_data[latest_idx]
    latest_close = float(latest_kline[2])
    latest_low = float(latest_kline[4])
    ma10 = ma10_list[latest_idx]
    
    if ma10 is None:
        return {
            'shouldRemove': False,
            'reason': 'MA10数据不足',
            'latestClose': latest_close,
            'ma10': None
        }
    
    # 检查是否跌破10日线（最低价 < MA10）
    if latest_low < ma10:
        # 检查当天是否收回（收盘价 >= MA10）
        if latest_close < ma10:
            # 跌破且没有收回，应该删除
            return {
                'shouldRemove': True,
                'reason': f'跌破10日线且未收回（收盘价{latest_close:.2f} < MA10{ma10:.2f}）',
                'latestClose': latest_close,
                'ma10': ma10
            }
        else:
            # 跌破但收回了，不需要删除
            return {
                'shouldRemove': False,
                'reason': f'曾跌破10日线但已收回（收盘价{latest_close:.2f} >= MA10{ma10:.2f}）',
                'latestClose': latest_close,
                'ma10': ma10
            }
    else:
        # 没有跌破，不需要删除
        return {
            'shouldRemove': False,
            'reason': f'未跌破10日线（收盘价{latest_close:.2f}，MA10{ma10:.2f}）',
            'latestClose': latest_close,
            'ma10': ma10
        }
async def scan_and_remove_below_ma10():
    """
    扫描股票池，删除跌破10日线且未收回的股票

    同时检查两个池：
    1. user_scan_pool（用户股票池）
    2. trend_scan_results（趋势股票池，当日扫描结果）
    """
    try:
        print('[10日线检查] 开始扫描股票池...')
        
        # 加载用户股票池
        symbols = load_user_scan_pool()
        if not symbols:
            print('[10日线检查] 用户股票池为空')
        else:
            print(f'[10日线检查] 用户股票池共{len(symbols)}只股票需要检查')
        
        # 加载今日趋势股票池（用于在用户股票池之外，额外检查趋势列表）
        trend_symbols = []
        try:
            trend_data = load_trend_scan_results()
            today = date.today().isoformat()
            # 只检查当天的趋势扫描结果
            if trend_data and trend_data.get('date') == today and trend_data.get('stocks'):
                trend_symbols = [s['symbol'] for s in trend_data['stocks'] if s.get('symbol')]
                print(f'[10日线检查] 当日趋势股票池共{len(trend_symbols)}只股票需要检查')
        except Exception as e:
            print(f'[10日线检查] 加载趋势股票池失败: {str(e)}')
        
        # 合并两个池（去重）
        all_symbols = list(dict.fromkeys((symbols or []) + trend_symbols))
        if not all_symbols:
            print('[10日线检查] 股票池为空，无需检查')
            return
        
        # 区分用户池 vs 趋势池中的股票（用于决定从哪个池删除）
        user_set = set(symbols or [])
        trend_set = set(trend_symbols)
        
        removed_user_symbols = []
        kept_user_symbols = []
        removed_trend_symbols = []
        kept_trend_symbols = []
        
        # 逐个检查股票
        for symbol in all_symbols:
            try:
                # 获取K线数据
                kline_data = await get_kline_data(symbol.lower())
                if not kline_data:
                    print(f'[10日线检查] {symbol} 获取K线数据失败，跳过')
                    if symbol in user_set:
                        kept_user_symbols.append(symbol)
                    if symbol in trend_set:
                        kept_trend_symbols.append(symbol)
                    continue
                
                # 检查是否跌破10日线
                result = check_below_ma10(kline_data)
                stock_name = get_cached_stock_name(symbol) or symbol
                in_user = symbol in user_set
                in_trend = symbol in trend_set
                
                if result['shouldRemove']:
                    pool_tags = []
                    if in_user:
                        pool_tags.append('用户池')
                    if in_trend:
                        pool_tags.append('趋势池')
                    tag_str = '/'.join(pool_tags) if pool_tags else '股票池'
                    print(f'[10日线检查] {stock_name}({symbol}): {result["reason"]} - 从{tag_str}删除')
                    if in_user:
                        removed_user_symbols.append(symbol)
                    else:
                        kept_user_symbols.append(symbol)
                    if in_trend:
                        removed_trend_symbols.append(symbol)
                    else:
                        kept_trend_symbols.append(symbol)
                else:
                    print(f'[10日线检查] {stock_name}({symbol}): {result["reason"]}')
                    if in_user:
                        kept_user_symbols.append(symbol)
                    if in_trend:
                        kept_trend_symbols.append(symbol)
                
            except Exception as e:
                print(f'[10日线检查] {symbol} 检查失败: {str(e)}')
                if symbol in user_set:
                    kept_user_symbols.append(symbol)
                if symbol in trend_set:
                    kept_trend_symbols.append(symbol)
                continue
        
        # 1. 保存更新后的用户股票池
        if symbols:  # 用户池有数据时（即使没有删除也要保持原状）
            if removed_user_symbols:
                save_user_scan_pool(kept_user_symbols)
                print(f'[10日线检查] 用户池扫描完成！共删除{len(removed_user_symbols)}只，保留{len(kept_user_symbols)}只')
                print(f'[10日线检查] 用户池已删除: {removed_user_symbols}')
            else:
                print(f'[10日线检查] 用户池扫描完成！没有需要删除的股票')
        
        # 2. 处理趋势股票池 - 从 trend_scan_results 中删除跌破 MA10 的股票
        if removed_trend_symbols:
            # 从趋势扫描结果中删除跌破 MA10 的股票
            current_trend = load_trend_scan_results()
            current_stocks = current_trend.get('stocks', [])
            original_count = len(current_stocks)
            kept_stocks = [s for s in current_stocks if s.get('symbol') not in set(removed_trend_symbols)]
            new_count = len(kept_stocks)
            
            if new_count < original_count:
                # 重新保存趋势扫描结果
                updated_trend = dict(current_trend)
                updated_trend['stocks'] = kept_stocks
                updated_trend['found'] = new_count
                save_trend_scan_results(updated_trend)
                # 同步更新内存缓存
                global _last_scan_result
                if _last_scan_result and _last_scan_result.get('date') == updated_trend.get('date'):
                    _last_scan_result = updated_trend
                print(f'[10日线检查] 趋势池扫描完成！共删除{original_count - new_count}只，保留{new_count}只')
                print(f'[10日线检查] 趋势池已删除: {[s for s in removed_trend_symbols]}')
            else:
                print(f'[10日线检查] 趋势池扫描完成！没有需要删除的股票')
        else:
            print(f'[10日线检查] 趋势池扫描完成！没有需要删除的股票')
            
    except Exception as error:
        print(f'[10日线检查] 扫描失败: {str(error)}')
def auto_add_high_score_to_pool(trend_results: List[Dict]) -> List[str]:
    """
    将评分 ≥ TREND_AUTO_ADD_POOL_THRESHOLD 的趋势股自动加入用户股票池。
    阈值 ≤ 0 时关闭自动加入。
    返回实际新增的股票代码列表（已在池中的会自动去重）。
    """
    threshold = TREND_AUTO_ADD_POOL_THRESHOLD
    if threshold <= 0:
        return []

    current_pool = [s.lower() for s in load_user_scan_pool()]
    pool_set = set(current_pool)
    to_add = []

    for r in trend_results:
        score = r.get('score', 0)
        symbol = (r.get('symbol') or '').lower()
        if score >= threshold and symbol and symbol not in pool_set:
            to_add.append(symbol)
            pool_set.add(symbol)

    if to_add:
        new_pool = current_pool + to_add
        save_user_scan_pool(new_pool)
        print(f'[趋势扫描] 自动加入股票池 {len(to_add)} 只（≥{threshold}分）: {to_add}')
    else:
        print(f'[趋势扫描] 无高分股达到自动加入阈值（≥{threshold}分）')

    return to_add


async def scan_trend_scan_results(force: bool = False):
    global _last_scan_result, _last_scan_date, _last_scan_ts
    
    today = date.today().isoformat()
    now_ts = time.time()
    
    if not force and _last_scan_result and _last_scan_date == today and (now_ts - _last_scan_ts) < SCAN_CACHE_TTL:
        print(f'[趋势扫描] 使用内存缓存（{_last_scan_date}，{now_ts - _last_scan_ts:.0f}s前扫描）')
        return {"success": True, "data": _ensure_today_date(_last_scan_result.copy())}
    
    if _get_scan_lock().locked():
        if _last_scan_result and _last_scan_date == today:
            return {"success": True, "data": _ensure_today_date(_last_scan_result.copy())}
        return {"success": True, "data": _ensure_today_date(load_trend_scan_results())}
    
    async with _get_scan_lock():
        if not force and _last_scan_result and _last_scan_date == today and (time.time() - _last_scan_ts) < SCAN_CACHE_TTL:
            return {"success": True, "data": _ensure_today_date(_last_scan_result.copy())}
        
        t0 = time.time()
        hot_search_ranking_data = load_hot_search_ranking()
        import services.stock
        services.stock._hot_search_ranking_cache = hot_search_ranking_data
        services.stock._stock_basic_info_cache = None
        
        scan_pool = []
        source = ''

        # ===== 固定底仓：HOT_STOCK_POOL（config.py 配置）+ hot_stock_buttons（用户快捷查询）=====
        # 两者始终并入扫描池，与热搜榜来源无关，保证用户关注的票不被遗漏
        fixed_pool: List[str] = list(HOT_STOCK_POOL)  # config 配置池
        try:
            buttons = load_hot_stock_buttons()
            btn_symbols = [b['code'].lower() for b in buttons if b.get('code')]
            btn_symbols = [s for s in btn_symbols if s not in fixed_pool]
            fixed_pool = list(dict.fromkeys(fixed_pool + btn_symbols))
        except Exception as e:
            print(f'[趋势扫描] 加载快捷查询股票池失败: {str(e)}')

        if hot_search_ranking_data.get('stocks') and len(hot_search_ranking_data['stocks']) > 0:
            hot_symbols = [s['symbol'] for s in hot_search_ranking_data['stocks']]
            # 合并：热搜榜 ∪ 固定底仓（去重保持热搜榜顺序在前）
            scan_pool = list(dict.fromkeys(hot_symbols + fixed_pool))
            source = 'eastmoney-hot+fixed'
            added_fixed = len(scan_pool) - len(hot_symbols)
            print(f'[趋势扫描] 热搜榜 {len(hot_symbols)} 只 + 固定底仓新增 {added_fixed} 只，共 {len(scan_pool)} 只 (更新时间: {hot_search_ranking_data.get("updateTime")})')
        else:
            local_map = load_stock_basic_info()
            map_symbols = list(local_map.keys())
            scan_pool = list(dict.fromkeys(fixed_pool + map_symbols))
            source = 'fallback'
            print(f'[趋势扫描] 热搜榜数据为空，使用备选池（固定底仓 + 本地基础信息），共 {len(scan_pool)} 只股票')

        # 把"上一次趋势列表"也并入扫描池，确保昨天在趋势列表里、今天从热搜榜掉出的股票也会被重新评估
        try:
            prev_trend = load_trend_scan_results()
            if prev_trend and prev_trend.get('stocks'):
                prev_symbols = [s['symbol'] for s in prev_trend['stocks'] if s.get('symbol')]
                if prev_symbols:
                    before_count = len(scan_pool)
                    scan_pool = list(dict.fromkeys(scan_pool + prev_symbols))
                    added = len(scan_pool) - before_count
                    if added > 0:
                        print(f'[趋势扫描] 已合并上一轮趋势列表 {len(prev_symbols)} 只（新增 {added} 只不在热搜榜中的），共 {len(scan_pool)} 只')
        except Exception as e:
            print(f'[趋势扫描] 合并上轮趋势列表失败: {str(e)}')

        # 排除"用户股票池"中已存在的股票 — 已被用户加入股票池的股票不应再出现在趋势发现列表中
        try:
            user_pool = set(s.lower() for s in load_user_scan_pool())
            if user_pool:
                before = len(scan_pool)
                scan_pool = [s for s in scan_pool if s.lower() not in user_pool]
                removed = before - len(scan_pool)
                if removed > 0:
                    print(f'[趋势扫描] 已排除用户股票池中 {removed} 只股票')
        except Exception as e:
            print(f'[趋势扫描] 排除用户股票池失败: {str(e)}')
        
        client = get_http_client()
        weights = load_strategy_factor_weights()
        
        # 提前获取大盘涨跌幅，传递给评分函数
        market_change = 0.0
        try:
            idx_data = await fetch_stock_data('sh000001')
            if idx_data:
                market_change = idx_data.get('parsed', {}).get('changePercent', 0)
        except Exception as _e:
            print(f'[趋势扫描] 大盘涨跌幅获取失败，使用 0: {_e}')

        # ===== P3-1 大盘状态预计算（2026-06-30，用于 is_up_trend 动态阈值）=====
        # 结合大盘现价 vs MA20 + 当日涨跌，判定 bull/bear/range
        # 传入 is_up_trend 后，D1乖离/D3放量滞涨/D4回撤等阈值会按状态动态调整
        market_regime = 'range'
        try:
            idx_kline = await get_kline_data('sh000001', client)
            market_regime = _classify_market_regime(market_change, idx_kline)
        except Exception as e:
            print(f'[趋势扫描] 大盘状态分类失败（降级为range）: {e}')
        print(f'[趋势扫描] 大盘状态: {market_regime} (大盘涨跌{market_change:.2f}%)')

        # ===== P0-1 板块动量预计算（2026-06-30）=====
        # 基于个股概念标签(stock_concept_tags)的同题材平均涨幅，构建 symbol → sector_change 映射
        # 后续 is_up_trend 调用时注入，实现"热门题材+5%/冷门题材-5%"的环境修正
        sector_change_map: Dict[str, float] = {}
        try:
            concept_tags_raw = load_stock_concept_tags()
            concept_tags = {k.lower(): v for k, v in concept_tags_raw.items()}
            if concept_tags and scan_pool:
                # 性能优化（2026-07-02）：串行批量改 asyncio.gather 并发，5 RTT → 1 RTT
                symbol_change_map: Dict[str, float] = {}
                _batch_sz = 100

                async def _fetch_change_batch(_batch):
                    try:
                        _raw = await fetch_with_retry(','.join(_batch), client)
                        return parse_tencent_batch_data(_raw)
                    except Exception:
                        return []

                _change_tasks = [
                    _fetch_change_batch(scan_pool[i:i + _batch_sz])
                    for i in range(0, len(scan_pool), _batch_sz)
                ]
                _change_results = await asyncio.gather(*_change_tasks, return_exceptions=True)
                for r in _change_results:
                    if isinstance(r, Exception) or not r:
                        continue
                    for s in r:
                        sym = s.get('symbol', '').lower()
                        if sym:
                            symbol_change_map[sym] = s.get('changePercent', 0)
                # 按题材标签分组求平均涨幅
                tag_changes: Dict[str, List[float]] = {}
                for sym, tags in concept_tags.items():
                    cp = symbol_change_map.get(sym)
                    if cp is None:
                        continue
                    for tag in tags:
                        tag_changes.setdefault(tag, []).append(cp)
                tag_avg = {t: sum(cps) / len(cps) for t, cps in tag_changes.items() if cps}
                # 个股 sector_change = 其所有标签平均涨幅的均值
                for sym in scan_pool:
                    tags = concept_tags.get(sym.lower())
                    if tags:
                        avg_list = [tag_avg[t] for t in tags if t in tag_avg]
                        if avg_list:
                            sector_change_map[sym.lower()] = sum(avg_list) / len(avg_list)
                print(f'[趋势扫描] 板块动量预计算：{len(sector_change_map)} 只有板块数据，{len(tag_avg)} 个题材')
        except Exception as e:
            print(f'[趋势扫描] 板块动量预计算失败（不影响扫描）: {e}')

        # ===== P1-1 主力资金流向预取（2026-06-30）=====
        # 批量获取扫描池股票的主力净流入，注入 strategy_score 的第9因子
        # 局部 import 避免与 recommend.py 循环导入
        fund_flow_map: Dict[str, Dict] = {}
        try:
            from services.recommend import fetch_batch_fund_flow
            fund_flow_map = await fetch_batch_fund_flow(scan_pool) or {}
        except Exception as e:
            print(f'[趋势扫描] 资金流向预取失败（不影响扫描）: {e}')

        async def process_one_stock(symbol: str) -> Optional[Dict]:
            async with get_scan_semaphore():
                try:
                    # 性能优化（2026-07-02）：get_kline_data 内部已拉实时行情校准最新 K 线
                    # 通过 return_realtime=True 复用 realtime_raw，避免二次 fetch_with_retry 冗余请求
                    kline, realtime_raw = await asyncio.wait_for(
                        get_kline_data(symbol, client, return_realtime=True),
                        timeout=SCAN_KLINE_TIMEOUT
                    )
                    if not kline:
                        return None

                    # 复用 get_kline_data 内部已拉的实时行情原始数据
                    realtime_data = None
                    if realtime_raw:
                        parsed_list = parse_tencent_batch_data(realtime_raw)
                        if parsed_list:
                            realtime_data = parsed_list[0]

                    # 优先使用实时价进行趋势判断（解决盘中跌破数据滞后问题）
                    realtime_price = None
                    if realtime_data and realtime_data.get('priceRaw'):
                        realtime_price = realtime_data['priceRaw']

                    # 提前获取名称（用于 ST 风险检查 + 后续展示）
                    name = get_cached_stock_name(symbol)
                    if not name:
                        name = await get_stock_name(symbol)
                    if not name or name == symbol:
                        return None

                    # 沪深主板标记（非主板改为扣 -5 分，不再直接排除）
                    _is_mainboard = is_main_board(symbol)

                    # 并发拉取近 20 日公告用于利空检测（失败不阻塞，仅视为无利空）
                    try:
                        neg_ann = await asyncio.wait_for(has_negative_announcement(symbol), timeout=4)
                    except Exception:
                        neg_ann = {'hasNegative': False, 'hits': []}

                    # 市值（概算：从实时数据取，若不可用则为 None=不触发小微盘排除）
                    _market_cap = None
                    if realtime_data:
                        # 腾讯 API 返回的 marketCap 可能在某些字段中，取不到则留 None
                        _market_cap = realtime_data.get('marketCap')

                    # 趋势判断（V3.0 综合评分）
                    trend_result = is_up_trend(
                        kline,
                        realtime_price=realtime_price,
                        negative_announcement=neg_ann,
                        stock_name=name,
                        market_cap=_market_cap,
                        is_mainboard=_is_mainboard,
                        sector_change=sector_change_map.get(symbol.lower()),
                        turnover_rate=realtime_data.get('turnoverRate', 0) if realtime_data else 0.0,
                        main_inflow_ratio=(fund_flow_map.get(symbol.lower(), {}) or {}).get('main_inflow_ratio'),
                        market_regime=market_regime,
                    )
                    if not trend_result['isUp']:
                        return None

                    if has_delisting_risk(name, symbol):
                        return None

                    strategy_score = None
                    if realtime_data:
                        # 传入 market_change + fund_flow_data（P1-1），与 recommend/routes 路径对齐
                        ff = fund_flow_map.get(symbol.lower())
                        strategy_score = calc_stock_score_v2(realtime_data, weights,
                                                             market_change=market_change,
                                                             fund_flow_data=ff)

                    return {
                        'symbol': symbol,
                        'name': name,
                        'score': trend_result['score'],
                        'details': trend_result['details'],
                        'latestPrice': trend_result['latestPrice'],
                        'ma5': trend_result.get('ma5'),
                        'ma10': trend_result.get('ma10'),
                        'ma20': trend_result.get('ma20'),
                        'recent5Days': trend_result.get('recent5Days'),
                        'deducts': trend_result.get('deducts', []),
                        'bonuses': trend_result.get('bonuses', []),
                        'strategyScore': strategy_score,
                        'realtimeData': realtime_data
                    }
                except Exception as e:
                    return None
        
        try:
            tasks = [process_one_stock(s) for s in scan_pool]
            results = await asyncio.gather(*tasks)

            # P3-2 行业中性化：对 strategy_score 做后处理（三层对齐 routes/recommend/trend）
            # 注意：trend 列表按 trend_score 排序，此处仅调整 strategyScore 字段，不改变趋势入选/排序
            # helper 原地修改 r['strategyScore'] dict（dict 引用共享），industry_map 复用已预取的 fund_flow_map
            if STRATEGY_INDUSTRY_NEUTRALIZATION_ENABLED and fund_flow_map:
                industry_map = {sym: (ff or {}).get('industry', '') for sym, ff in fund_flow_map.items()}
                neutral_list = [{'symbol': r['symbol'], 'score': r['strategyScore']}
                                for r in results if r and r.get('strategyScore')]
                apply_industry_neutralization(neutral_list, industry_map)
                try:
                    save_stock_industry(industry_map)
                except Exception as e:
                    print(f'[趋势扫描] 行业映射持久化失败（不影响扫描）: {e}')

            trend_scan_results = [r for r in results if r is not None and r['score'] >= TREND_MIN_SCORE]
            
            trend_scan_results.sort(key=lambda x: x['score'], reverse=True)
            
            # 保存策略预测记录 (复用上面已获取的 market_change)
            predictions = []
            cleaned_results = []
            for r in trend_scan_results:
                if r.get('strategyScore') and r.get('realtimeData'):
                    score_data = r['strategyScore']
                    realtime_data = r['realtimeData']
                    if score_data['predict_direction'] == 'bull':
                        predictions.append({
                            'predict_date': today,
                            'symbol': r['symbol'],
                            'name': r['name'],
                            'score': score_data['total'],
                            'label': score_data['label'],
                            'predict_direction': score_data['predict_direction'],
                            'outer_ratio': realtime_data.get('outerRatio', 0),
                            'volume_ratio': realtime_data.get('volumeRatio', 0),
                            'turnover_rate': realtime_data.get('turnoverRate', 0),
                            'change_percent': realtime_data.get('changePercent', 0),
                            'weibi': realtime_data.get('weibi', 0),
                            'avg_price_deviation': realtime_data.get('avgPriceDeviation', 0),
                            'amplitude': realtime_data.get('amplitude', 0),
                            'market_change': market_change,
                        })
                # 清理不需要返回给前端的字段
                cleaned_r = {
                    'symbol': r['symbol'],
                    'name': r['name'],
                    'score': r['score'],
                    'details': r['details'],
                    'latestPrice': r['latestPrice'],
                    'ma5': r['ma5'],
                    'ma10': r['ma10'],
                    'ma20': r['ma20'],
                    'recent5Days': r['recent5Days']
                }
                cleaned_results.append(cleaned_r)
            
            if predictions:
                save_predictions(predictions)
                print(f'[趋势扫描] 保存了 {len(predictions)} 条策略预测记录')
            
            # 自动将高分趋势股加入用户股票池（阈值由 config.py 控制）
            auto_added = auto_add_high_score_to_pool(trend_scan_results)

            result = {
                'date': today,
                'totalScanned': len(scan_pool),
                'found': len(trend_scan_results),
                'stocks': cleaned_results,
                'scanTime': f'{(time.time() - t0):.1f}s',
                'source': source,
                'autoAdded': auto_added,
                'autoAddThreshold': TREND_AUTO_ADD_POOL_THRESHOLD,
            }
            
            save_trend_scan_results(result)
            # P4-1: 同步落库趋势回测预测记录（用于后续 N 日回测验证）
            try:
                from db.database import save_trend_backtest_records
                save_trend_backtest_records(today, cleaned_results)
            except Exception as e:
                print(f'[趋势回测] 预测记录落库失败（不影响扫描）: {e}')
            _last_scan_result = result
            _last_scan_date = today
            _last_scan_ts = time.time()
            print(f'[趋势扫描] 完成，发现 {len(trend_scan_results)} 只趋势股，耗时 {result["scanTime"]}')
            
            return {"success": True, "data": _ensure_today_date(result)}
        except Exception as error:
            print(f'趋势扫描失败: {str(error)}')
            raise HTTPException(status_code=500, detail="趋势扫描失败")

# ========== AI 诊断功能 ==========
async def scan_trend_task(force: bool = False):
    try:
        print('[趋势扫描] 定时任务开始...')
        await scan_trend_scan_results(force=force)
    except Exception as e:
        print(f'[趋势扫描] 定时任务失败: {e}')
