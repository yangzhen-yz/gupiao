"""trend.py - split from main.py"""
import json, asyncio, time
from typing import List, Dict, Optional
from datetime import date, timedelta
from app.config import (TREND_MIN_SCORE, TREND_MIN_KLINE_DAYS, TREND_LIMIT_UP_THRESHOLD, TREND_LIMIT_DOWN_THRESHOLD,
    TREND_IS_UP_MIN_SCORE, USE_INTRADAY_BREAK_CHECK, KLINE_DATA_LIMIT, KLINE_DISPLAY_MA_POINTS,
    SCAN_CONCURRENCY, SCAN_CACHE_TTL, HOT_STOCK_POOL, DELISTING_KEYWORDS, FORBIDDEN_KEYWORDS,
    STRATEGY_PREDICT_BULL_MIN_SCORE, SCAN_KLINE_TIMEOUT,
    TREND_PRICE_ABOVE_MA20_SCORE, TREND_MA20_SLOPE_WINDOW, TREND_MA20_SLOPE_SCORE,
    TREND_LIMIT_UP_20D_BONUS_1, TREND_LIMIT_UP_20D_BONUS_2, TREND_CONSECUTIVE_BOARD_SCORE,
    TREND_VOLUME_RATIO_20V60_THRESHOLD,
    TREND_DRAWDOWN_20D_TIERS,
    TREND_DEDUCT_MA20_BROKEN, TREND_DEDUCT_LIMIT_DOWN, TREND_DEDUCT_SHRINK_VOL,
    TREND_RANGE_20D_FLAT_MIN)
from db.database import get_db_conn, load_trend_scan_results, save_trend_scan_results, save_predictions, load_user_scan_pool, load_hot_search_ranking, load_stock_basic_info
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
                stock_name: str = '') -> Dict:
    """
    中短线波段趋势判断（满分 100 分）

    关注周期：未来 ~1 个月
    评分结构（6 项基础分 = 100 分 + 扣分 + 强制排除）：
      维度 1 短期均线趋势（核心，40 分）
        ① 价格站稳 MA20：              20 分
        ② MA20 斜率向上（3 日区间）：   20 分
      维度 2 短期资金异动（活跃度，30 分）
        ③ 近 20 日涨停次数：           0 / 12 / 20 分
        ④ 2 连板或连续阳线 ≥4 根：      0 / 10 分
      维度 3 量能配合（短线灵魂，15 分）
        ⑤ 20日均量 > 60日均量 × 阈值： 0 / 15 分
      维度 4 风险回撤（风控，15 分）
        ⑥ 近 20 日最大回撤：           0 / 8 / 15 分

    扣分项（实盘必用，总分直接扣减，可叠加）：
      - 近 5 日内收盘价跌破 MA20 且 3 日内未收回：  -15
      - 近 20 日出现单日跌停：                      -12
      - 持续缩量阴跌（量 < 60日均量 50%）：          -10

    强制排除（无论得分多少，直接剔除）：
      - 近 20 日有重大利空 / 减持 / 立案 / 调查 公告
      - ST / *ST / 退市风险警示
      - 非沪深主板
      - 近 20 日振幅 < 5%（完全横盘，无短线机会）

    入选门槛：总分 ≥ 60 为 isUp=true，≥ 80 进入趋势发现列表。

    :param kline_data: K线数据（按日期升序），每项 [日期, 开, 收, 高, 低, 量, ...]
    :param realtime_price: 实时价（盘中校准，可选）
    :param negative_announcement: 预拉的利空检测结果 {hasNegative, hits}，由调用方在循环外并发拉取
    :param stock_name: 股票名称（用于 ST 风险检查）
    :return: 趋势判断结果 dict
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

    # ========== 强制排除项（在评分前先过） ==========
    exclusions = []
    # 1. ST / *ST 风险
    if stock_name and (any(kw in stock_name for kw in DELISTING_KEYWORDS) or any(kw in stock_name for kw in FORBIDDEN_KEYWORDS)):
        exclusions.append(f'ST/退市风险股（{stock_name}）')
    # 2. 重大利空公告
    if negative_announcement and negative_announcement.get('hasNegative'):
        hits = negative_announcement.get('hits', [])[:3]
        exclusions.append(f'近20日有重大利空公告：{"；".join(hits)[:120]}')
    # 3. 近 20 日振幅 < TREND_RANGE_20D_FLAT_MIN%（横盘）
    win20_start = max(0, n - 20)
    last20_highs = highs[win20_start:]
    last20_lows = lows[win20_start:]
    if last20_highs and last20_lows:
        win20_high = max(last20_highs)
        win20_low = min(last20_lows)
        amplitude_20d = (win20_high - win20_low) / win20_low * 100 if win20_low > 0 else 0
    else:
        amplitude_20d = 0
    if amplitude_20d < TREND_RANGE_20D_FLAT_MIN:
        exclusions.append(f'近20日振幅仅{amplitude_20d:.1f}%，完全横盘')

    if exclusions:
        return {
            'isUp': False, 'score': 0, 'reason': '强制排除：' + '；'.join(exclusions),
            'details': {'amplitude20d': round(amplitude_20d, 2), 'checkPrice': check_price},
            'deducts': [], 'exclusions': exclusions
        }

    # ========== 维度 1：MA20 趋势（40 分） ==========
    # ① 价格站稳 MA20
    if ma20_val and check_price > ma20_val:
        s_price_ma20 = TREND_PRICE_ABOVE_MA20_SCORE
    else:
        s_price_ma20 = 0
    # ② MA20 斜率向上（近 N 日均值 > 前 N 日均值）
    slope_w = TREND_MA20_SLOPE_WINDOW
    s_ma20_slope = 0
    if (latest_idx >= 2 * slope_w and ma20[latest_idx]
            and all(ma20[latest_idx - k] is not None for k in range(2 * slope_w + 1))):
        recent_avg = sum(ma20[latest_idx - k] for k in range(slope_w)) / slope_w
        prev_avg = sum(ma20[latest_idx - slope_w - k] for k in range(slope_w)) / slope_w
        if recent_avg > prev_avg:
            s_ma20_slope = TREND_MA20_SLOPE_SCORE

    # ========== 维度 2：资金异动（30 分） ==========
    win20_start_idx = max(0, n - 20)
    # ③ 近 20 日涨停次数
    limit_up_20d = 0
    for i in range(win20_start_idx, n):
        prev_c = closes[i - 1] if i > 0 else opens[i]
        change_pct = (closes[i] - prev_c) / prev_c * 100 if prev_c > 0 else 0
        if change_pct >= TREND_LIMIT_UP_THRESHOLD:
            limit_up_20d += 1
    if limit_up_20d >= 2:
        s_limit_up_20d = TREND_LIMIT_UP_20D_BONUS_2
    elif limit_up_20d == 1:
        s_limit_up_20d = TREND_LIMIT_UP_20D_BONUS_1
    else:
        s_limit_up_20d = 0
    # ④ 2 连板 或 连续阳线 ≥4 根
    consecutive_board_or_yang = _check_consecutive_board_or_yang(closes, win20_start_idx, n)
    s_consecutive = TREND_CONSECUTIVE_BOARD_SCORE if consecutive_board_or_yang else 0

    # ========== 维度 3：量能配合（15 分） ==========
    win60_start_idx = max(0, n - 60)
    avg_vol_20 = sum(vols[max(0, n - 20):]) / min(20, n) if n > 0 else 0
    avg_vol_60 = sum(vols[win60_start_idx:]) / max(1, n - win60_start_idx) if n - win60_start_idx > 0 else 0
    # 仅在 20 日均量 > 60 日均量且 20 日均量 > 0 时给分（温和放大 + 量价同步）
    if avg_vol_60 > 0 and avg_vol_20 > avg_vol_60 * TREND_VOLUME_RATIO_20V60_THRESHOLD:
        s_volume = 15
    else:
        s_volume = 0

    # ========== 维度 4：风险回撤（15 分） ==========
    # 近 20 日最大回撤：(峰值 - 谷值) / 峰值 × 100%
    peak = closes[win20_start_idx] if win20_start_idx < n else closes[0]
    max_dd_20 = 0.0
    for i in range(win20_start_idx, n):
        if closes[i] > peak:
            peak = closes[i]
        dd = (peak - closes[i]) / peak * 100 if peak > 0 else 0
        if dd > max_dd_20:
            max_dd_20 = dd
    # 分档
    s_drawdown = 0
    for thr, sc in TREND_DRAWDOWN_20D_TIERS:
        if max_dd_20 < thr:
            s_drawdown = sc
            break
    if max_dd_20 >= TREND_DRAWDOWN_20D_TIERS[-1][0]:
        s_drawdown = 0

    # ========== 基础分合计 ==========
    base_score = s_price_ma20 + s_ma20_slope + s_limit_up_20d + s_consecutive + s_volume + s_drawdown

    # ========== 扣分项 ==========
    deducts = []  # (name, points)
    # (1) 近 5 日内跌破 MA20 且 3 日内未收回
    #    解读：最近 5 个交易日中，最新一个交易日往前数 3 日内有跌破 MA20，且最新一个交易日未收回
    ma20_broken = _check_ma20_recently_broken(closes, ma20, latest_idx, window=5, recovery_days=3)
    if ma20_broken:
        base_score -= TREND_DEDUCT_MA20_BROKEN
        deducts.append(('近5日跌破MA20且3日内未收回', TREND_DEDUCT_MA20_BROKEN))

    # (2) 近 20 日出现单日跌停
    has_limit_down_20d = _check_limit_down_in_window(opens, closes, win20_start_idx, n)
    if has_limit_down_20d:
        base_score -= TREND_DEDUCT_LIMIT_DOWN
        deducts.append(('近20日单日跌停', TREND_DEDUCT_LIMIT_DOWN))

    # (3) 持续缩量阴跌：最近 5 日成交量持续 < 60日均量 50%
    shrink = _check_shrink_volume(vols, avg_vol_60, n, days=5, ratio=0.5)
    if shrink:
        base_score -= TREND_DEDUCT_SHRINK_VOL
        deducts.append(('近5日缩量阴跌（<60日均量50%）', TREND_DEDUCT_SHRINK_VOL))

    # 总分（下界 0）
    total_score = max(0, base_score)

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
        'sPriceAboveMa20': s_price_ma20,
        'ma20': round(ma20_val, 2) if ma20_val else None,
        'sMa20SlopeUp': s_ma20_slope,
        'ma20SlopeWindow': slope_w,
        # 维度 2
        'sLimitUp20d': s_limit_up_20d,
        'limitUpCount20d': limit_up_20d,
        'sConsecutiveBoardOrYang': s_consecutive,
        # 维度 3
        'sVolume': s_volume,
        'avgVol20': round(avg_vol_20, 0),
        'avgVol60': round(avg_vol_60, 0),
        # 维度 4
        'sDrawdown20d': s_drawdown,
        'maxDrawdown20d': round(max_dd_20, 2),
        'amplitude20d': round(amplitude_20d, 2),
        # 通用
        'latestClose': latest_close,
        'checkPrice': check_price,
        'isRealtime': is_realtime,
        'limitDown20d': has_limit_down_20d,
        # 展示用：连涨/连跌天数（与评分无关）
        'consecutiveUpDays': consecutive_up_days,
        'consecutiveDownDays': consecutive_down_days,
    }

    return {
        'isUp': is_trend_up,
        'score': total_score,
        'details': details,
        'deducts': [{'name': d[0], 'points': d[1]} for d in deducts],
        'ma20': ma20[-10:] if len(ma20) >= 10 else ma20,
        'latestPrice': latest_close,
        'checkPrice': check_price,
        'isRealtime': is_realtime,
        'recent5Days': closes[-5:] if len(closes) >= 5 else closes,
    }


def _check_consecutive_board_or_yang(closes: List[float], start: int, end: int) -> bool:
    """
    在 [start, end) 区间内，是否出现 2 连板 或 连续阳线 ≥4 根。
    2 连板 = 连续 2 个交易日涨幅 ≥ 9.8%。
    连续阳线 ≥4 = 连续 4 个交易日 closes[i] > closes[i-1]。
    """
    n = end - start
    if n < 2:
        return False
    # 2 连板
    board_run = 0
    for i in range(start + 1, end):
        prev_c = closes[i - 1] if i - 1 > 0 else closes[i]
        change_pct = (closes[i] - prev_c) / prev_c * 100 if prev_c > 0 else 0
        if change_pct >= TREND_LIMIT_UP_THRESHOLD:
            board_run += 1
            if board_run >= 2:
                return True
        else:
            board_run = 0
    # 连续阳线 ≥4
    yang_run = 0
    for i in range(start + 1, end):
        if closes[i] > closes[i - 1]:
            yang_run += 1
            if yang_run >= 4:
                return True
        else:
            yang_run = 0
    return False


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

                    # 非沪深主板 → 直接排除（中短线不参与）
                    if not is_main_board(symbol):
                        return None

                    # 并发拉取近 20 日公告用于利空检测（失败不阻塞，仅视为无利空）
                    try:
                        neg_ann = await asyncio.wait_for(has_negative_announcement(symbol), timeout=4)
                    except Exception:
                        neg_ann = {'hasNegative': False, 'hits': []}

                    # 趋势判断（新版中短线 100 分制）
                    trend_result = is_up_trend(
                        kline,
                        realtime_price=realtime_price,
                        negative_announcement=neg_ann,
                        stock_name=name,
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
