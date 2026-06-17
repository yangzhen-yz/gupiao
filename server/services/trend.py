"""trend.py - split from main.py"""
import json, asyncio, time
from typing import List, Dict, Optional
from datetime import date, timedelta
from fastapi import HTTPException
from app.config import (TREND_MIN_SCORE, TREND_MIN_KLINE_DAYS, TREND_LIMIT_UP_THRESHOLD, TREND_LIMIT_DOWN_THRESHOLD,
    TREND_IS_UP_MIN_SCORE, USE_INTRADAY_BREAK_CHECK, KLINE_DATA_LIMIT, KLINE_DISPLAY_MA_POINTS,
    SCAN_CONCURRENCY, SCAN_CACHE_TTL, HOT_STOCK_POOL, DELISTING_KEYWORDS, FORBIDDEN_KEYWORDS,
    STRATEGY_PREDICT_BULL_MIN_SCORE, SCAN_KLINE_TIMEOUT,
    TREND_D1_TREND_FULL, TREND_D1_TREND_PARTIAL, TREND_D1_TREND_BARE,
    TREND_D1_DIVERGENCE_PENALTY_TIERS,
    TREND_D2_LIMIT_UP_TIERS, TREND_D2_YANG_LINE_TIERS,
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
    TREND_BONUS_POCKET_PIVOT,
    TREND_SECTOR_MOMENTUM_WEIGHT, TREND_SECTOR_HOT_RANK_MIN,
    TREND_EXCLUDE_CONSECUTIVE_LIMIT_DOWN, TREND_EXCLUDE_MARKET_CAP_MIN,
    TREND_RANGE_20D_FLAT_MIN)
from db.database import get_db_conn, load_trend_scan_results, save_trend_scan_results, save_predictions, load_user_scan_pool, save_user_scan_pool, load_hot_search_ranking, load_stock_basic_info
from services.stock import (get_http_client, has_delisting_risk, is_main_board, get_kline_data, parse_realtime_fields,
    calculate_ma, get_cached_stock_name, fetch_with_retry, fetch_stock_data, get_stock_name,
    parse_tencent_batch_data, has_negative_announcement)
from services.strategy import calc_stock_score_v2, load_strategy_factor_weights

_last_scan_result = None
_last_scan_date = ''
_last_scan_ts = 0.0
_scan_trend_lock = asyncio.Lock()
SCAN_SEMAPHORE = asyncio.Semaphore(SCAN_CONCURRENCY)

def _ensure_today_date(data: Dict) -> Dict:
    if data:
        data['scanDate'] = data.get('date', '')
        data['date'] = date.today().isoformat()
    return data

def is_up_trend(kline_data: List[List], realtime_price: Optional[float] = None,
                negative_announcement: Optional[Dict] = None,
                stock_name: str = '',
                market_cap: Optional[float] = None,
                is_mainboard: bool = True) -> Dict:
    """
    中短线波段趋势判断 V4.0

    评分结构：基础分(100分) - 扣分项(含互斥衰减) + 加分项 + 板块动量修正 = 最终总分

    维度 1 短期均线趋势    30 分（趋势方向20 + 乖离惩罚0~-10）
    维度 2 短期资金异动    30 分（涨停次数/连阳数取高分）
    维度 3 量能配合        20 分（量比10 + 量能趋势斜率5 + 量能稳定性5）
    维度 4 风险回撤        20 分（当前距10日高点回撤，5档）

    扣分项（12项，分3组互斥/衰减）：高位追高/双空头/破位/跌停(可豁免)/缩量/放量滞涨/量价背离等
    加分项（5项）：多头排列/倍量突破/连续阳线/口袋支点/低位蓄力
    板块动量修正：叠加 ±20%（占位接口，当前 0）
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

    # ========== 计算额外均线（MA60、MA120 用于扣分/加分） ==========
    ma60 = calculate_ma(kline_data, 60)
    ma120 = calculate_ma(kline_data, 120)

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

    # ========== 维度 2：短期资金异动（30 分，涨停分+连阳分取高） ==========
    # ③ 近 20 日涨停次数
    limit_up_20d = 0
    for i in range(win20_start_idx, n):
        prev_c = closes[i - 1] if i > 0 else opens[i]
        change_pct = (closes[i] - prev_c) / prev_c * 100 if prev_c > 0 else 0
        if change_pct >= TREND_LIMIT_UP_THRESHOLD:
            limit_up_20d += 1
    s_limit_up = 0
    for cnt, sc in TREND_D2_LIMIT_UP_TIERS:
        if limit_up_20d >= cnt:
            s_limit_up = sc
            break

    # ④ 2 连板 或 连续阳线得分
    has_2_consecutive_board = _check_2_consecutive_board(closes, win20_start_idx, n)
    consecutive_yang_count = _count_consecutive_yang(closes, win20_start_idx, n)
    s_yang = 0
    if has_2_consecutive_board:
        s_yang = 30
    else:
        for cnt, sc in TREND_D2_YANG_LINE_TIERS:
            if consecutive_yang_count >= cnt:
                s_yang = sc
                break
    s_d2 = max(s_limit_up, s_yang)

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
    for thr, sc in TREND_D4_DRAWDOWN_TIERS:
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
        base_score -= TREND_DEDUCT_VOLUME_SPIKE
        deducts.append(('放量滞涨', TREND_DEDUCT_VOLUME_SPIKE))

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

    # ⑤ 横盘位置判断（移到此处，bonus_score 已定义）
    if amplitude_20d < TREND_RANGE_20D_FLAT_MIN:
        if position_pct is not None and position_pct < TREND_SIDEWAYS_LOW_PCT:
            bonus_score += TREND_SIDEWAYS_BONUS
            bonuses.append(('低位蓄力(横盘)', TREND_SIDEWAYS_BONUS))
        elif position_pct is not None and position_pct < TREND_SIDEWAYS_MID_PCT:
            base_score -= TREND_DEDUCT_AMPLITUDE_FLAT // 2
            deducts.append(('中位横盘(<5%)', TREND_DEDUCT_AMPLITUDE_FLAT // 2))
        else:
            base_score -= TREND_DEDUCT_AMPLITUDE_FLAT
            deducts.append(('高位横盘(<5%)', TREND_DEDUCT_AMPLITUDE_FLAT))

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

    # ========== 板块动量因子（环境加成/折价 ±20%） ==========
    sector_factor = 0  # 默认无修正
    # TODO: 从收评板块数据中获取该股所属板块的动量，实际运行时填充
    # 目前留接口：该因子在扫描阶段由 scan_trend_scan_results 注入

    # 总分（下界 0，含板块动量修正）
    raw_score = max(0, base_score + bonus_score)
    total_score = round(raw_score * (1 + sector_factor))

    # ========== 入选判断 ==========
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
        # 板块动量因子
        'sectorFactor': round(sector_factor, 2),
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


def _check_2_consecutive_board(closes: List[float], start: int, end: int) -> bool:
    """区间内是否出现 2 连板（连续 2 个交易日涨幅 ≥ 9.8%）"""
    n = end - start
    if n < 2:
        return False
    board_run = 0
    for i in range(start + 1, end):
        prev_c = closes[i - 1]
        change_pct = (closes[i] - prev_c) / prev_c * 100 if prev_c > 0 else 0
        if change_pct >= TREND_LIMIT_UP_THRESHOLD:
            board_run += 1
            if board_run >= 2:
                return True
        else:
            board_run = 0
    return False


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


def compute_sector_momentum(symbol: str, name: str) -> float:
    """
    计算板块动量因子（0~1之间的修正系数）

    基于个股概念标签与当日收评板块数据的匹配度，对个股评分做环境修正：
    - 处于热门板块 → 正修正（bonus multiplier > 0）
    - 处于冷门板块 → 负修正（penalty multiplier < 0）
    - 无法判断 → 0（不做修正）

    当前阶段：占位实现，返回 0.0。
    后续在 scan_trend_scan_results 中传入收评板块数据后填充实际逻辑。
    """
    return 0.0


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
async def scan_trend_scan_results(force: bool = False):
    global _last_scan_result, _last_scan_date, _last_scan_ts
    
    today = date.today().isoformat()
    now_ts = time.time()
    
    if not force and _last_scan_result and _last_scan_date == today and (now_ts - _last_scan_ts) < SCAN_CACHE_TTL:
        print(f'[趋势扫描] 使用内存缓存（{_last_scan_date}，{now_ts - _last_scan_ts:.0f}s前扫描）')
        return {"success": True, "data": _ensure_today_date(_last_scan_result.copy())}
    
    if _scan_trend_lock.locked():
        if _last_scan_result and _last_scan_date == today:
            return {"success": True, "data": _ensure_today_date(_last_scan_result.copy())}
        return {"success": True, "data": _ensure_today_date(load_trend_scan_results())}
    
    async with _scan_trend_lock:
        if not force and _last_scan_result and _last_scan_date == today and (time.time() - _last_scan_ts) < SCAN_CACHE_TTL:
            return {"success": True, "data": _ensure_today_date(_last_scan_result.copy())}
        
        t0 = time.time()
        hot_search_ranking_data = load_hot_search_ranking()
        import services.stock
        services.stock._hot_search_ranking_cache = hot_search_ranking_data
        services.stock._stock_basic_info_cache = None
        
        scan_pool = []
        source = ''
        
        if hot_search_ranking_data.get('stocks') and len(hot_search_ranking_data['stocks']) > 0:
            scan_pool = [s['symbol'] for s in hot_search_ranking_data['stocks']]
            source = 'eastmoney-hot'
            print(f'[趋势扫描] 使用热搜榜数据，共 {len(scan_pool)} 只股票 (更新时间: {hot_search_ranking_data.get("updateTime")})')
        else:
            local_map = load_stock_basic_info()
            map_symbols = list(local_map.keys())
            scan_pool = list(set(HOT_STOCK_POOL + map_symbols))
            source = 'fallback'
            print(f'[趋势扫描] 热搜榜数据为空，使用备选池，共 {len(scan_pool)} 只股票')

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
        except Exception:
            pass
        
        async def process_one_stock(symbol: str) -> Optional[Dict]:
            async with SCAN_SEMAPHORE:
                try:
                    kline = await asyncio.wait_for(get_kline_data(symbol, client), timeout=SCAN_KLINE_TIMEOUT)
                    if not kline:
                        return None

                    # 获取实时数据（同时用于趋势判断的盘中跌破校准 + 策略评分）
                    realtime_data = None
                    try:
                        realtime_raw = await fetch_with_retry(symbol, client)
                        if realtime_raw:
                            parsed_list = parse_tencent_batch_data(realtime_raw)
                            if parsed_list:
                                realtime_data = parsed_list[0]
                    except Exception:
                        pass

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
                    )
                    if not trend_result['isUp']:
                        return None

                    if has_delisting_risk(name, symbol):
                        return None

                    strategy_score = None
                    if realtime_data:
                        # 传入 market_change 以便应用 AI 优化规则2
                        strategy_score = calc_stock_score_v2(realtime_data, weights, market_change=market_change)

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
            
            result = {
                'date': today,
                'totalScanned': len(scan_pool),
                'found': len(trend_scan_results),
                'stocks': cleaned_results,
                'scanTime': f'{(time.time() - t0):.1f}s',
                'source': source
            }
            
            save_trend_scan_results(result)
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
