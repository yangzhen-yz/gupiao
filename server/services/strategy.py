"""strategy.py - split from main.py"""
import asyncio
from typing import List, Dict, Optional
from datetime import date, timedelta
from app.config import STRATEGY_BULL_THRESHOLD, STRATEGY_BEAR_THRESHOLD, STRATEGY_PREDICT_BULL_MIN_SCORE, STRATEGY_BACKTEST_PREDICTION_LIMIT, STRATEGY_BACKTEST_HISTORY_DAYS, STRATEGY_SCORE_THRESHOLDS, STRATEGY_SCORE_VALUES, STRATEGY_WEBI_VERIFY, STRATEGY_PE_FILTER, STRATEGY_LABEL_THRESHOLDS, DEFAULT_WEIGHTS, MARKET_CRASH_THRESHOLD, MARKET_SEVERE_CRASH_THRESHOLD, STRATEGY_INDUSTRY_NEUTRALIZATION_ENABLED, STRATEGY_INDUSTRY_NEUTRAL_MIN_SAMPLE, STRATEGY_INDUSTRY_P80_BONUS, STRATEGY_INDUSTRY_P20_PENALTY, TREND_LIMIT_UP_THRESHOLD
from db.database import get_db_conn, load_strategy_factor_weights, save_strategy_factor_weights, save_predictions
from services.stock import fetch_stock_data, fetch_with_retry, parse_tencent_batch_data
from services.ai import load_ai_config, DEEPSEEK_API_KEY, _call_deepseek_api


def _score_main_inflow(main_inflow_ratio: Optional[float]) -> float:
    """P1-1 主力净流入因子评分（2026-06-30）

    基于主力净流入/流通市值占比(%)评分：
    - ≥3%（强力吸筹）→ 15 分
    - ≥1%（持续流入）→ 10 分
    - ≥0%（小幅流入）→ 7 分
    - ≥-1%（小幅流出）→ 4 分
    - <-1%（主力出逃）→ 2 分
    - None（无数据）→ 7 分（no_data 中档，避免无数据股票总分被拖累）

    与外盘占比、量比形成数据层防护：避免"价涨但主力出货"的诱多票被高分推荐。
    """
    vals = STRATEGY_SCORE_VALUES['main_capital_inflow']
    if main_inflow_ratio is None:
        return vals['no_data']
    thr = STRATEGY_SCORE_THRESHOLDS['main_capital_inflow']
    if main_inflow_ratio >= thr['high']:
        return vals['high']
    if main_inflow_ratio >= thr['mid']:
        return vals['mid']
    if main_inflow_ratio >= thr['low']:
        return vals['low']
    if main_inflow_ratio >= thr['negative']:
        return vals['low']  # 0~-1% 小幅流出，仍给 low 分
    return vals['negative']


def calc_stock_score_v2(data: Dict, weights: Dict = None, market_change: float = 0.0,
                        trend_result: Dict = None,
                        fund_flow_data: Optional[Dict] = None) -> Dict:
    """使用动态权重的评分函数 V2 (V4.1 融合趋势评分 + P1-1 新增主力净流入因子)

    Args:
        data: 股票特征数据（实时行情/日内因子）
        weights: 策略因子权重
        market_change: 大盘涨跌幅，用于根据市场环境动态调整评分阈值
        trend_result: 趋势评分结果（来自 is_up_trend），包含 score(0-100) 等字段。
                      提供后，最终评分 = 50% 日内因子 + 50% 趋势因子
        fund_flow_data: 主力资金流向数据（P1-1 新增），格式：
                        {'main_net_inflow': float(元), 'main_inflow_ratio': float(%),
                         'circ_mv': float(元)}
                        提供后，新增第9因子 main_capital_inflow 参与评分
    """
    if weights is None:
        weights = load_strategy_factor_weights()

    # 趋势评分（若提供，后续与日内因子混合）
    trend_score = trend_result.get('score', 0) if trend_result else None

    outer_ratio = data.get('outerRatio', 50)
    volume_ratio = data.get('volumeRatio', 0)
    turnover_rate = data.get('turnoverRate', 0)
    change_percent = data.get('changePercent', 0)
    weibi = data.get('weibi', 0)
    avg_price_deviation = data.get('avgPriceDeviation', 0)
    amplitude = data.get('amplitude', 0)
    pe = data.get('pe', 0)

    # P1-1 主力净流入占比（%）— 无数据时为 None，评分时给 no_data 中档分
    main_inflow_ratio = None
    if fund_flow_data:
        # 优先用已计算好的占比，否则用 main_net_inflow / circ_mv 现算
        if fund_flow_data.get('main_inflow_ratio') is not None:
            main_inflow_ratio = float(fund_flow_data['main_inflow_ratio'])
        elif fund_flow_data.get('main_net_inflow') is not None and fund_flow_data.get('circ_mv'):
            mv = float(fund_flow_data['circ_mv'])
            if mv > 0:
                main_inflow_ratio = float(fund_flow_data['main_net_inflow']) / mv * 100

    # 基础得分（与原逻辑一致）
    base_scores = {
        'outer_ratio': STRATEGY_SCORE_VALUES['outer_ratio']['high'] if outer_ratio >= STRATEGY_SCORE_THRESHOLDS['outer_ratio']['high'] else STRATEGY_SCORE_VALUES['outer_ratio']['mid'] if outer_ratio >= STRATEGY_SCORE_THRESHOLDS['outer_ratio']['mid'] else STRATEGY_SCORE_VALUES['outer_ratio']['low'],
        'volume_ratio': STRATEGY_SCORE_VALUES['volume_ratio']['high'] if volume_ratio >= STRATEGY_SCORE_THRESHOLDS['volume_ratio']['high'] else STRATEGY_SCORE_VALUES['volume_ratio']['mid'] if volume_ratio >= STRATEGY_SCORE_THRESHOLDS['volume_ratio']['mid'] else STRATEGY_SCORE_VALUES['volume_ratio']['normal'] if volume_ratio >= STRATEGY_SCORE_THRESHOLDS['volume_ratio']['low'] else STRATEGY_SCORE_VALUES['volume_ratio']['low'],
        'turnover_rate': STRATEGY_SCORE_VALUES['turnover_rate']['high'] if (STRATEGY_SCORE_THRESHOLDS['turnover_rate']['high'][0] <= turnover_rate <= STRATEGY_SCORE_THRESHOLDS['turnover_rate']['high'][1]) else STRATEGY_SCORE_VALUES['turnover_rate']['mid'] if (STRATEGY_SCORE_THRESHOLDS['turnover_rate']['mid'][0] <= turnover_rate < STRATEGY_SCORE_THRESHOLDS['turnover_rate']['mid'][1]) else STRATEGY_SCORE_VALUES['turnover_rate']['normal'] if turnover_rate > STRATEGY_SCORE_THRESHOLDS['turnover_rate']['high_penalty'] else STRATEGY_SCORE_VALUES['turnover_rate']['low'],
        'change_percent': STRATEGY_SCORE_VALUES['change_percent']['high'] if change_percent > STRATEGY_SCORE_THRESHOLDS['change_percent']['high'] else STRATEGY_SCORE_VALUES['change_percent']['mid'] if change_percent > STRATEGY_SCORE_THRESHOLDS['change_percent']['mid'] else STRATEGY_SCORE_VALUES['change_percent']['normal'] if change_percent > STRATEGY_SCORE_THRESHOLDS['change_percent']['low'] else STRATEGY_SCORE_VALUES['change_percent']['negative'] if change_percent > STRATEGY_SCORE_THRESHOLDS['change_percent']['negative'] else 0,
        'weibi': STRATEGY_SCORE_VALUES['weibi']['high'] if weibi > STRATEGY_SCORE_THRESHOLDS['weibi']['high'] else STRATEGY_SCORE_VALUES['weibi']['mid'] if weibi > STRATEGY_SCORE_THRESHOLDS['weibi']['mid'] else STRATEGY_SCORE_VALUES['weibi']['low'] if weibi > STRATEGY_SCORE_THRESHOLDS['weibi']['low'] else STRATEGY_SCORE_VALUES['weibi']['negative'],
        'avg_price_deviation': STRATEGY_SCORE_VALUES['avg_price_deviation']['high'] if avg_price_deviation > STRATEGY_SCORE_THRESHOLDS['avg_price_deviation']['high'] else STRATEGY_SCORE_VALUES['avg_price_deviation']['mid'] if avg_price_deviation > STRATEGY_SCORE_THRESHOLDS['avg_price_deviation']['mid'] else STRATEGY_SCORE_VALUES['avg_price_deviation']['normal'] if avg_price_deviation > STRATEGY_SCORE_THRESHOLDS['avg_price_deviation']['low'] else STRATEGY_SCORE_VALUES['avg_price_deviation']['low'],
        'amplitude': STRATEGY_SCORE_VALUES['amplitude']['high'] if (STRATEGY_SCORE_THRESHOLDS['amplitude']['high'][0] < amplitude <= STRATEGY_SCORE_THRESHOLDS['amplitude']['high'][1]) else STRATEGY_SCORE_VALUES['amplitude']['mid'] if (STRATEGY_SCORE_THRESHOLDS['amplitude']['mid'][0] < amplitude <= STRATEGY_SCORE_THRESHOLDS['amplitude']['mid'][1]) else STRATEGY_SCORE_VALUES['amplitude']['low'],
        # P1-1 主力净流入因子（2026-06-30）：无数据给 no_data 中档分，避免拖累总分
        'main_capital_inflow': _score_main_inflow(main_inflow_ratio),
    }
    if weibi > STRATEGY_WEBI_VERIFY['weibi_high'] and change_percent < 0:
        base_scores['weibi'] = 3
    elif weibi > STRATEGY_WEBI_VERIFY['weibi_high'] and change_percent < STRATEGY_SCORE_THRESHOLDS['change_percent']['negative']:
        base_scores['weibi'] = 2
    elif weibi > STRATEGY_WEBI_VERIFY['weibi_high'] and outer_ratio < STRATEGY_WEBI_VERIFY['outer_ratio_low']:
        base_scores['weibi'] = 4
    elif weibi > STRATEGY_WEBI_VERIFY['weibi_high'] and volume_ratio < STRATEGY_WEBI_VERIFY['volume_ratio_low']:
        base_scores['weibi'] = 4
        
    # ====== 放量出货 / 诱多检测（分级：涨停/非涨停用不同阈值） ======
    # 
    # 核心原则：涨停板本身也可能是诱多出货（尾盘拉板、反复开板对倒、虚假封单），
    # 不能简单豁免。但涨停时的正常封板交易也会产生高量比+外盘≈50%，
    # 所以涨停股需要更高的"异常阈值"来区分真龙头 vs 涨停出货。
    #
    # 涨停股阈值：
    #   - 天量开板（vol>8x 且 outer<45%）→ 出货嫌疑，重罚 -5
    #   - 放量偏弱（vol>5x 且 outer<48%）→ 封板质量欠缺，轻罚 -3
    #   - 温和放量（vol≤5x）→ 正常封板，不罚
    # 非涨停股阈值：
    #   - 放量但外盘不占优（vol>3x 且 outer<50%）→ 诱多出货
    #   - 量比偏高 + 外盘不突出（vol>1.5x 且 outer<55%）→ 对倒嫌疑

    is_limit_up = change_percent >= TREND_LIMIT_UP_THRESHOLD

    if is_limit_up:
        # --- 涨停股：用更高的异常阈值区分封板质量 ---
        if volume_ratio > 8 and outer_ratio < 45:
            # 天量换手 + 卖盘占优：典型的涨停开板出货
            base_scores['outer_ratio'] = max(0, base_scores['outer_ratio'] - 5)
            base_scores['volume_ratio'] = max(0, base_scores['volume_ratio'] - 5)
        elif volume_ratio > 5 and outer_ratio < 48:
            # 放量封板但卖压偏大：封板质量存疑，减半惩罚
            base_scores['outer_ratio'] = max(0, base_scores['outer_ratio'] - 3)
            base_scores['volume_ratio'] = max(0, base_scores['volume_ratio'] - 3)
        # vol ≤ 5x 或 outer ≥ 48%：正常涨停交易特征，不罚
    else:
        # --- 非涨停股：保持原有规则 ---
        if outer_ratio < 50 and volume_ratio > 3:
            base_scores['outer_ratio'] = max(0, base_scores['outer_ratio'] - 5)
            base_scores['volume_ratio'] = max(0, base_scores['volume_ratio'] - 5)
        if volume_ratio > 1.5 and outer_ratio < 55:
            base_scores['volume_ratio'] = max(0, base_scores['volume_ratio'] - 5)
            base_scores['outer_ratio'] = max(0, base_scores['outer_ratio'] - 5)
        
    # 2026-06-11 策略优化建议：不再盲目惩罚涨幅，而是区分"强势启动"与"力竭赶顶"
    # 2026-06-11 策略优化建议：拥抱强势龙头股。涨停不代表风险，往往代表次日的高溢价。
    # P1-2 涨停质量增强（2026-06-30）：用委比+换手+量比组合代理封板质量
    # 业界共识：封板质量是次日溢价的关键——强力封板次日高开概率高，烂板次日低开概率高
    # 分档：强力封板25 / 优质封板22 / 普通封板18 / 弱封板14 / 烂板8
    if change_percent > 9.5:
        if weibi > 95 and 2 <= turnover_rate <= 8 and volume_ratio < 3:
            base_scores['change_percent'] = 25  # 强力封板（一字/秒板，委比极高压单小，次日高溢价）
        elif weibi > 80 and 2 <= turnover_rate <= 15:
            base_scores['change_percent'] = 22  # 优质封板（原"龙头股高分"逻辑保留）
        elif weibi > 50 and turnover_rate <= 20:
            base_scores['change_percent'] = 18  # 普通封板（封单一般，有换手但未爆量）
        elif weibi > 0:
            base_scores['change_percent'] = 14  # 弱封板（封单被砸过，委比偏低）
        else:
            base_scores['change_percent'] = 8   # 烂板/开板（委比为负，封单被砸开）
    elif change_percent > 7.0:
        if volume_ratio < 2.5:
            base_scores['change_percent'] = 18
        else:
            base_scores['change_percent'] = 11
    elif change_percent > 3.0:
        if 1.0 <= volume_ratio <= 2.2 and outer_ratio > 56:
            base_scores['change_percent'] = 18
        else:
            base_scores['change_percent'] = 14
    elif change_percent > 0:
        base_scores['change_percent'] = 16
    else:
        pass

    # 2026-06-11 策略优化建议：大盘上涨时的优选策略
    # 当大盘上涨（market_change > 0）时，优先选择“外盘占比 > 60% 且 量比 < 1.5”的股票（缩量稳步上涨，非对倒放量）。
    if market_change > 0 and outer_ratio > 60 and volume_ratio < 1.5:
        base_scores['outer_ratio'] += 5
        base_scores['volume_ratio'] += 5

    # 2026-06-11 策略优化建议：虚假封单过滤 (委比 100% 陷阱)
    # 误判样本如旭光电子、昊华科技在委比 100% 时实际下跌，说明封单可能是诱多虚假单。
    if weibi > 99:
        # 如果委比接近 100% 但外盘占比并未同步处于高位（>60%），则该委比可信度极低。
        if outer_ratio < 60:
            base_scores['weibi'] = 0
            base_scores['outer_ratio'] = max(0, base_scores['outer_ratio'] - 5)

    cp = abs(change_percent)
    if cp > STRATEGY_PE_FILTER['pe_high'] or (pe > 0 and pe > STRATEGY_PE_FILTER['pe_max']):
        base_scores['position'] = 5
    elif cp < 1 and pe > 0 and pe < 50:
        base_scores['position'] = 15
    else:
        base_scores['position'] = 10

    # 应用动态权重
    total = 0
    for factor, base_score in base_scores.items():
        w = weights.get(factor, {}).get('weight', 1.0)
        total += base_score * w

    # 归一化到100分（动态计算各因子满分总和，支持因子扩展）
    # P1-1：从 8 因子扩展到 9 因子（新增 main_capital_inflow），硬编码改为动态计算
    def _factor_max(f: str) -> float:
        """从 STRATEGY_SCORE_VALUES 提取该因子的最大档位分值"""
        vals = STRATEGY_SCORE_VALUES.get(f, {})
        # 过滤掉非数值的 key（如 'no_data'）后取最大
        numeric_vals = [v for v in vals.values() if isinstance(v, (int, float))]
        return max(numeric_vals) if numeric_vals else 0
    max_possible = sum(_factor_max(f) for f in base_scores) * \
                   sum(weights.get(f, {}).get('weight', 1.0) for f in base_scores) / len(base_scores)
    if max_possible > 0:
        total = round(total / max_possible * 100)
    total = min(100, max(0, total))

    label = ''
    
    # AI回测优化建议规则2: 考虑大盘环境因素，当大盘下跌时降低策略的看涨阈值或增加惩罚
    bull_threshold = STRATEGY_BULL_THRESHOLD
    strong_bull_threshold = STRATEGY_LABEL_THRESHOLDS['strong_bull']
    label_bull_threshold = STRATEGY_LABEL_THRESHOLDS['bull']
    
    if market_change < 0:
        # 大盘下跌时，提高看涨门槛（更难被判定为看涨）
        penalty = min(10, abs(int(market_change * 5))) # 跌幅越大，门槛提高越多，最多提高10分
        bull_threshold += penalty
        strong_bull_threshold += penalty
        label_bull_threshold += penalty

    if total >= strong_bull_threshold:
        label = '强烈看涨'
    elif total >= label_bull_threshold:
        label = '偏多看涨'
    elif total >= 45:
        label = '震荡观望'
    elif total >= 30:
        label = '偏空看跌'
    else:
        label = '强烈看跌'

    predict_direction = 'bull' if total >= bull_threshold else 'bear' if total < STRATEGY_BEAR_THRESHOLD else 'neutral'

    # V4.1: 融合趋势评分（50% 日内 + 50% 趋势），仅当 trend_result 提供时生效
    intraday_score = total
    if trend_score is not None:
        total = round(intraday_score * 0.5 + trend_score * 0.5)
        total = min(100, max(0, total))
        # 融合后重新判定标签和方向
        if total >= strong_bull_threshold:
            label = '强烈看涨'
        elif total >= label_bull_threshold:
            label = '偏多看涨'
        elif total >= 45:
            label = '震荡观望'
        elif total >= 30:
            label = '偏空看跌'
        else:
            label = '强烈看跌'
        predict_direction = 'bull' if total >= bull_threshold else 'bear' if total < STRATEGY_BEAR_THRESHOLD else 'neutral'

    return {
        'total': total,
        'label': label,
        'predict_direction': predict_direction,
        'base_scores': base_scores,
        'intraday_score': intraday_score,  # V4.1: 日内原始分
        'trend_score': trend_score,         # V4.1: 趋势分
        'factor_values': {
            'outer_ratio': outer_ratio,
            'volume_ratio': volume_ratio,
            'turnover_rate': turnover_rate,
            'change_percent': change_percent,
            'weibi': weibi,
            'avg_price_deviation': avg_price_deviation,
            'amplitude': amplitude,
            'main_capital_inflow': main_inflow_ratio,  # P1-1: 主力净流入占比(%)，None=无数据
        }
    }


def apply_industry_neutralization(stocks: List[Dict], industry_map: Dict[str, str]) -> None:
    """P3-2 行业中性化：按行业分位数调整 strategy_score.total（原地修改，2026-06-30）

    对 calc_stock_score_v2 输出的 total 做后处理，避免热门行业股票因整体高
    volume_ratio/turnover/change 而集体虚高。trend_score 已有 sector_factor 做环境修正，
    不再叠加，避免双重修正。

    Args:
        stocks: 待评分股票列表，每个元素须含 stock['score']['total'] 和 stock['symbol']。
                score 须是 dict（calc_stock_score_v2 的返回值）。
        industry_map: {symbol_lower: industry_name}，来自 fetch_batch_fund_flow 的 f100 字段。
                      industry 为空字符串归入 '其他'。

    原则：
      - 单行业样本 < STRATEGY_INDUSTRY_NEUTRAL_MIN_SAMPLE(5) → 跳过（统计无意义）
      - 行业内总分 ≥ P80 → +STRATEGY_INDUSTRY_P80_BONUS(2)
      - 行业内总分 ≤ P20 → +STRATEGY_INDUSTRY_P20_PENALTY(-2)
      - 中间不动
      - 原地写回 stock['score']['total']（钳制 0-100），并记录 stock['score']['industryAdjust']
    """
    if not STRATEGY_INDUSTRY_NEUTRALIZATION_ENABLED or not stocks or not industry_map:
        # 未启用或无行业数据：所有 stock 标记 industryAdjust=0，保持一致性
        for s in stocks:
            if isinstance(s.get('score'), dict):
                s['score']['industryAdjust'] = 0
        return

    # 1. 按 industry 分组收集 (stock_ref, total)
    groups: Dict[str, List[Dict]] = {}
    for s in stocks:
        if not isinstance(s.get('score'), dict):
            continue
        sym = (s.get('symbol') or '').lower()
        ind = industry_map.get(sym, '') or '其他'
        if not ind:
            ind = '其他'
        groups.setdefault(ind, []).append(s)

    # 2. 每个行业计算 p20/p80 并调整
    for ind, members in groups.items():
        # 默认 industryAdjust=0
        for m in members:
            m['score']['industryAdjust'] = 0
        if len(members) < STRATEGY_INDUSTRY_NEUTRAL_MIN_SAMPLE:
            continue  # 样本不足，不调整
        totals = sorted(m['score']['total'] for m in members)
        # 分位数：用线性插值近似（index = pct × (n-1)）
        n = len(totals)
        def _quantile(p: float) -> float:
            if n == 1:
                return totals[0]
            pos = p * (n - 1)
            lo = int(pos)
            hi = min(lo + 1, n - 1)
            frac = pos - lo
            return totals[lo] * (1 - frac) + totals[hi] * frac
        p20 = _quantile(0.20)
        p80 = _quantile(0.80)
        for m in members:
            t = m['score']['total']
            if t >= p80:
                adj = STRATEGY_INDUSTRY_P80_BONUS
            elif t <= p20:
                adj = STRATEGY_INDUSTRY_P20_PENALTY
            else:
                adj = 0
            m['score']['industryAdjust'] = adj
            m['score']['total'] = max(0, min(100, t + adj))


async def backtest_yesterday_predictions() -> Dict:
    """回溯验证未验证的预测，计算准确率，调整权重
    
    优先回测最近一个交易日的预测，同时补验所有历史未验证的预测。
    """
    try:
        today = date.today()
        # 确定最近一个交易日（跳过周末）
        if today.weekday() == 0:  # 周一 → 回测上周五
            latest_trading_day = (today - timedelta(days=3))
        elif today.weekday() == 6:  # 周日 → 回测上周五
            latest_trading_day = (today - timedelta(days=2))
        elif today.weekday() == 5:  # 周六 → 回测上周五
            latest_trading_day = (today - timedelta(days=1))
        else:
            latest_trading_day = (today - timedelta(days=1))
        
        yesterday = latest_trading_day.isoformat()

        conn = get_db_conn()
        cursor = conn.cursor()

        # 先补验所有历史未验证的预测（非今日）
        from datetime import datetime as _dt
        today_str = today.isoformat()
        cursor.execute(
            'SELECT DISTINCT predict_date FROM strategy_predict_records WHERE verified = 0 AND predict_date < ? ORDER BY predict_date',
            (today_str,)
        )
        unverified_dates = [row['predict_date'] for row in cursor.fetchall()]
        
        backfilled_count = 0
        for ud in unverified_dates:
            if ud == yesterday:
                continue  # 昨天的在主流程中处理
            # 跳过周末：周六/周日不生成回测报告（但仍验证预测记录）
            try:
                ud_weekday = _dt.strptime(ud, '%Y-%m-%d').weekday()
            except Exception:
                ud_weekday = 0
            is_weekend = ud_weekday >= 5
            
            cursor.execute(
                'SELECT * FROM strategy_predict_records WHERE predict_date = ? AND verified = 0',
                (ud,)
            )
            old_preds = cursor.fetchall()
            for pred in old_preds:
                try:
                    stock_data = await fetch_stock_data(pred['symbol'])
                    if not stock_data:
                        continue
                    actual_change = stock_data.get('parsed', {}).get('changePercent', 0)
                    actual_dir = 'bull' if actual_change > 0 else 'bear' if actual_change < 0 else 'neutral'
                    is_correct = pred['predict_direction'] == actual_dir
                    cursor.execute(
                        '''UPDATE strategy_predict_records
                           SET verified=1, actual_change=?, actual_direction=?, is_correct=?
                           WHERE predict_date=? AND symbol=?''',
                        (actual_change, actual_dir, 1 if is_correct else 0, ud, pred['symbol'])
                    )
                    backfilled_count += 1
                except Exception:
                    continue
            
            if is_weekend:
                # 周末不生成回测报告
                print(f'[回测] 跳过周末日期 {ud}（不生成回测报告）')
                continue
            
            # 为补验日期生成回测报告（如果不存在）
            cursor.execute(
                'SELECT id FROM strategy_backtest_reports WHERE backtest_date = ?', (ud,)
            )
            if not cursor.fetchone():
                cursor.execute(
                    'SELECT * FROM strategy_predict_records WHERE predict_date = ? AND verified = 1', (ud,)
                )
                all_verified = cursor.fetchall()
                if all_verified:
                    correct = sum(1 for r in all_verified if r['is_correct'] == 1)
                    # 统计看涨/看跌误判
                    bull_misjudge = []
                    bear_misjudge = []
                    for r in all_verified:
                        if r['is_correct'] == 1:
                            continue
                        item = {
                            'symbol': r['symbol'],
                            'name': r['name'],
                            'score': r['score'],
                            'predict_change': float(r['change_percent']),
                            'actual_change': float(r['actual_change']),
                            'outer_ratio': float(r['outer_ratio']),
                            'volume_ratio': float(r['volume_ratio']),
                            'weibi': float(r['weibi']),
                        }
                        if r['predict_direction'] == 'bull':
                            bull_misjudge.append(item)
                        else:
                            bear_misjudge.append(item)
                    # 调用分析函数生成误判分析
                    ud_market_change = 0.0
                    try:
                        idx_data = await fetch_stock_data('sh000001')
                        if idx_data:
                            ud_market_change = idx_data.get('parsed', {}).get('changePercent', 0)
                    except Exception:
                        pass
                    weights_bk = load_strategy_factor_weights()
                    misjudge_analysis = await _analyze_misjudgments(bull_misjudge, bear_misjudge, ud_market_change, weights_bk)
                    import json
                    cursor.execute(
                        '''INSERT OR REPLACE INTO strategy_backtest_reports
                           (backtest_date, market_change, total_predictions, correct_count, accuracy,
                            bull_accuracy, bear_accuracy, bull_misjudge_count, bear_misjudge_count,
                            misjudge_analysis, weight_adjustments)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                        (ud, ud_market_change, len(all_verified), correct,
                         round(correct / len(all_verified) * 100, 2) if all_verified else 0,
                         round(sum(1 for r in all_verified if r['predict_direction'] == 'bull' and r['is_correct'] == 1) / max(1, sum(1 for r in all_verified if r['predict_direction'] == 'bull')) * 100, 2),
                         round(sum(1 for r in all_verified if r['predict_direction'] == 'bear' and r['is_correct'] == 1) / max(1, sum(1 for r in all_verified if r['predict_direction'] == 'bear')) * 100, 2),
                         len(bull_misjudge), len(bear_misjudge),
                         json.dumps(misjudge_analysis, ensure_ascii=False),
                         json.dumps([], ensure_ascii=False))
                    )
        
        if backfilled_count > 0:
            print(f'[回测] 补验了 {backfilled_count} 条历史未验证预测（日期：{", ".join(unverified_dates)}）')
            conn.commit()

        # 获取最近交易日未验证的预测
        cursor.execute(
            'SELECT * FROM strategy_predict_records WHERE predict_date = ? AND verified = 0',
            (yesterday,)
        )
        predictions = cursor.fetchall()

        if not predictions:
            # 没有未验证的预测，检查是否已有回测报告
            cursor.execute(
                'SELECT id FROM strategy_backtest_reports WHERE backtest_date = ?',
                (yesterday,)
            )
            existing = cursor.fetchone()
            if existing:
                conn.close()
                return {'date': yesterday, 'message': '回测报告已存在', 'backfilled': backfilled_count}
            
            # 没有回测报告但预测已验证，从已验证预测生成报告
            cursor.execute(
                'SELECT * FROM strategy_predict_records WHERE predict_date = ? AND verified = 1',
                (yesterday,)
            )
            predictions = cursor.fetchall()
            if not predictions:
                conn.close()
                return {'date': yesterday, 'message': '无待验证的预测记录', 'backfilled': backfilled_count}

        # 获取昨日大盘涨跌
        market_change = 0.0
        try:
            idx_data = await fetch_stock_data('sh000001')
            if idx_data:
                market_change = idx_data.get('parsed', {}).get('changePercent', 0)
        except Exception:
            pass

        # 性能优化（2026-07-02）：收集所有未验证的 symbols，一次性批量拉取行情（消除 N+1）
        unverified_preds = [p for p in predictions if not (p['verified'] and p['actual_change'] is not None)]
        unverified_symbols = list({p['symbol'] for p in unverified_preds})
        actual_change_map: Dict[str, float] = {}
        if unverified_symbols:
            # 分批拉取（每批最多100只）
            _b_size = 100
            async def _fetch_batch(syms):
                try:
                    _raw = await fetch_with_retry(','.join(syms))
                    return parse_tencent_batch_data(_raw)
                except Exception as e:
                    print(f'  [回测] 批量拉取失败({len(syms)}只): {e}')
                    return []
            _b_tasks = [
                _fetch_batch(unverified_symbols[i:i + _b_size])
                for i in range(0, len(unverified_symbols), _b_size)
            ]
            _b_results = await asyncio.gather(*_b_tasks, return_exceptions=True)
            for r in _b_results:
                if isinstance(r, Exception) or not r:
                    continue
                for s in r:
                    sym = s.get('symbol', '').lower()
                    if sym:
                        actual_change_map[sym] = s.get('changePercent', 0)

            # 兜底（2026-07-03 修复）：如果批量拉取后 map 为空（如非交易时间被 except 吞掉），
            # 降级为单只串行拉取，确保 0% 准确率 bug 不再出现
            if not actual_change_map and unverified_symbols:
                print(f'  [回测] 批量拉取返回空，降级为单只串行拉取({len(unverified_symbols)}只)...')
                for sym in unverified_symbols:
                    try:
                        _raw = await fetch_with_retry(sym)
                        _parsed = parse_tencent_batch_data(_raw)
                        if _parsed:
                            actual_change_map[_parsed[0]['symbol'].lower()] = _parsed[0].get('changePercent', 0)
                    except Exception:
                        pass
                print(f'  [回测] 降级拉取完成，actual_change_map 填充 {len(actual_change_map)} 条')

        # 逐个验证（用 actual_change_map 替代 N+1 await）
        correct_count = 0
        total_count = len(predictions)
        bull_correct = 0
        bull_total = 0
        bull_misjudge = []

        for pred in predictions:
            symbol = pred['symbol']
            predict_dir = pred['predict_direction']
            
            if pred['verified'] and pred['actual_change'] is not None:
                actual_change = float(pred['actual_change'])
                actual_dir = pred['actual_direction'] if pred['actual_direction'] else ('bull' if actual_change > 0 else 'bear' if actual_change < 0 else 'neutral')
                is_correct = bool(pred['is_correct'])
            else:
                # 性能优化（2026-07-02）：从 actual_change_map 取值，不再 N+1 await
                actual_change = actual_change_map.get(symbol.lower(), 0)
                if actual_change == 0 and symbol.lower() not in actual_change_map:
                    continue  # 拉取失败的跳过

                actual_dir = 'bull' if actual_change > 0 else 'bear' if actual_change < 0 else 'neutral'
                is_correct = predict_dir == actual_dir

                cursor.execute(
                    '''UPDATE strategy_predict_records
                       SET verified=1, actual_change=?, actual_direction=?, is_correct=?
                       WHERE predict_date=? AND symbol=?''',
                    (actual_change, actual_dir, 1 if is_correct else 0, yesterday, symbol)
                )

            if is_correct:
                correct_count += 1

            bull_total += 1
            if is_correct:
                bull_correct += 1
            else:
                bull_misjudge.append({
                    'symbol': symbol, 'name': pred['name'],
                    'score': pred['score'], 'predict_change': float(pred['change_percent']),
                    'actual_change': actual_change,
                    'outer_ratio': float(pred['outer_ratio']),
                    'volume_ratio': float(pred['volume_ratio']),
                    'weibi': float(pred['weibi']),
                })

        conn.commit()

        # 计算各因子准确率并调整权重
        weights = load_strategy_factor_weights()
        weight_adjustments = _adjust_weights(predictions, weights, cursor)

        # 保存更新后的权重
        save_strategy_factor_weights(weights)

        # 生成误判分析
        misjudge_analysis = await _analyze_misjudgments(bull_misjudge, [], market_change, weights)

        # 计算准确率
        accuracy = round(correct_count / total_count * 100, 2) if total_count > 0 else 0
        bull_accuracy = round(bull_correct / bull_total * 100, 2) if bull_total > 0 else 0

        # 保存回测报告
        import json
        cursor.execute(
            '''INSERT OR REPLACE INTO strategy_backtest_reports
               (backtest_date, market_change, total_predictions, correct_count, accuracy,
                bull_accuracy, bear_accuracy, bull_misjudge_count, bear_misjudge_count,
                misjudge_analysis, weight_adjustments)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
            (yesterday, market_change, total_count, correct_count, accuracy,
             bull_accuracy, 0, len(bull_misjudge), 0,
             json.dumps(misjudge_analysis, ensure_ascii=False),
             json.dumps(weight_adjustments, ensure_ascii=False))
        )
        conn.commit()
        conn.close()

        return {
            'date': yesterday,
            'market_change': market_change,
            'total': total_count,
            'correct': correct_count,
            'accuracy': accuracy,
            'bull_accuracy': bull_accuracy,
            'bull_misjudge': bull_misjudge,
            'misjudge_analysis': misjudge_analysis,
            'weight_adjustments': weight_adjustments,
        }
    except Exception as error:
        print(f'回测验证失败: {str(error)}')
        import traceback
        traceback.print_exc()
        return {'error': str(error)}
def _adjust_weights(predictions: List[Dict], weights: Dict, cursor) -> List[Dict]:
    """根据预测准确性调整各因子权重"""
    adjustments = []
    
    db_columns = {'outer_ratio', 'volume_ratio', 'turnover_rate', 'change_percent',
                  'weibi', 'avg_price_deviation', 'amplitude'}

    for factor_name in DEFAULT_WEIGHTS.keys():
        if factor_name not in db_columns:
            adjustments.append({'factor': factor_name, 'action': 'keep', 'reason': '非数据库因子'})
            continue
        # 统计该因子高分时预测的准确率
        cursor.execute(
            f'''SELECT predict_direction, actual_change, is_correct, {factor_name} as factor_val
               FROM strategy_predict_records
               WHERE verified = 1 AND predict_date >= date('now', '-30 days')
               ORDER BY predict_date DESC LIMIT {STRATEGY_BACKTEST_PREDICTION_LIMIT}'''
        )
        recent = cursor.fetchall()

        if len(recent) < 10:
            adjustments.append({'factor': factor_name, 'action': 'keep', 'reason': '样本不足'})
            continue

        # 计算该因子在预测正确和错误样本中的分布差异
        correct_samples = [r for r in recent if r['is_correct'] == 1]
        wrong_samples = [r for r in recent if r['is_correct'] == 0]

        if not correct_samples or not wrong_samples:
            adjustments.append({'factor': factor_name, 'action': 'keep', 'reason': '无错误样本'})
            continue

        # 因子准确率
        factor_accuracy = len(correct_samples) / len(recent) * 100
        current_weight = weights[factor_name]['weight']
        sample_count = len(recent)
        correct_count = len(correct_samples)

        # 更新权重数据
        weights[factor_name]['accuracy'] = round(factor_accuracy, 2)
        weights[factor_name]['sample_count'] = sample_count
        weights[factor_name]['correct_count'] = correct_count

        # 权重调整逻辑：
        # 准确率 > 60%: 增强权重（最高2.0）
        # 准确率 < 40%: 减弱权重（最低0.3）
        # 40%-60%: 保持不变
        old_weight = current_weight
        if factor_accuracy > 60:
            new_weight = min(2.0, current_weight * 1.05)
            action = 'increase'
            reason = f'准确率{factor_accuracy:.1f}%>60%，权重从{old_weight:.3f}提升到{new_weight:.3f}'
        elif factor_accuracy < 40:
            new_weight = max(0.3, current_weight * 0.95)
            action = 'decrease'
            reason = f'准确率{factor_accuracy:.1f}%<40%，权重从{old_weight:.3f}降低到{new_weight:.3f}'
        else:
            new_weight = current_weight
            action = 'keep'
            reason = f'准确率{factor_accuracy:.1f}%在40%-60%之间，权重保持{old_weight:.3f}'

        weights[factor_name]['weight'] = round(new_weight, 4)
        adjustments.append({'factor': factor_name, 'action': action, 'old_weight': round(old_weight, 4), 'new_weight': round(new_weight, 4), 'reason': reason})

    return adjustments

async def _analyze_misjudgments(bull_misjudge: List[Dict], bear_misjudge: List[Dict],
                                market_change: float, weights: Dict) -> Dict:
    """分析误判原因"""
    analysis = {
        'market_context': '',
        'bull_misjudge_analysis': '',
        'bear_misjudge_analysis': '',
        'suggestions': [],
    }

    # 大盘环境分析
    if market_change > 0:
        analysis['market_context'] = f'大盘上涨{market_change:.2f}%'
    elif market_change < 0:
        analysis['market_context'] = f'大盘下跌{market_change:.2f}%'
    else:
        analysis['market_context'] = '大盘平盘'

    # 看涨误判基础数据统计
    if bull_misjudge:
        avg_predict_change = sum(s['predict_change'] for s in bull_misjudge) / len(bull_misjudge)
        avg_actual_change = sum(s['actual_change'] for s in bull_misjudge) / len(bull_misjudge)
        avg_outer = sum(s.get('outer_ratio', 50) for s in bull_misjudge) / len(bull_misjudge)
        avg_weibi = sum(s.get('weibi', 0) for s in bull_misjudge) / len(bull_misjudge)
        avg_vol = sum(s.get('volume_ratio', 1) for s in bull_misjudge) / len(bull_misjudge)
        
        # 尝试使用AI进行深度诊断
        try:
            load_ai_config()
            if DEEPSEEK_API_KEY:
                import json
                prompt = f"""
作为专业的A股量化分析师，请对昨日选股策略的"看涨误判"进行深度分析。
【大盘环境】
{analysis['market_context']}

【误判数据统计】
- 误判数量: {len(bull_misjudge)}只 (策略预测看涨，但实际下跌)
- 预测时平均涨幅: {avg_predict_change:.2f}%
- 实际平均跌幅: {avg_actual_change:.2f}%
- 误判样本平均特征:
  * 外盘占比: {avg_outer:.1f}%
  * 委比: {avg_weibi:.1f}%
  * 量比: {avg_vol:.2f}

【部分误判样本详情】
{json.dumps(bull_misjudge[:5], ensure_ascii=False)}

请分析误判的核心原因，并给出下一步优化选股策略的建议。
返回格式必须是JSON:
{{
  "bull_misjudge_analysis": "总结误判的核心原因，例如指出哪些技术指标存在失效或被主力骗线的可能（约100字）",
  "suggestions": ["优化建议1", "优化建议2"]
}}
"""
                ai_response = await _call_deepseek_api(prompt)
                ai_result = json.loads(ai_response)
                
                analysis['bull_misjudge_analysis'] = f"共{len(bull_misjudge)}只看涨误判，预测时平均涨幅{avg_predict_change:.2f}%，实际平均跌幅{avg_actual_change:.2f}%。" + ai_result.get('bull_misjudge_analysis', '')
                analysis['suggestions'] = ai_result.get('suggestions', [])
                
        except Exception as e:
            print(f"AI误判分析失败，回退到规则分析: {str(e)}")
            # 回退到基于规则的分析
            patterns = []
            if market_change > 0 and bull_misjudge:
                patterns.append('大盘上涨但个股下跌，可能是个股基本面问题或行业轮动')
            if avg_outer > STRATEGY_SCORE_THRESHOLDS['outer_ratio']['high']:
                patterns.append('外盘占比偏高但下跌，可能存在主力对倒出货')
            if avg_weibi > 20:
                patterns.append('委比偏高但下跌，可能是虚假委托（挂大买单不成交）')

            analysis['bull_misjudge_analysis'] = (
                f'共{len(bull_misjudge)}只看涨误判，'
                f'预测时平均涨幅{avg_predict_change:.2f}%，实际平均跌幅{avg_actual_change:.2f}%。'
                + '；'.join(patterns) if patterns else ''
            )

            if avg_outer > STRATEGY_SCORE_THRESHOLDS['outer_ratio']['high']:
                analysis['suggestions'].append('外盘占比指标的可靠性下降，建议降低该因子权重')
            if avg_weibi > 20:
                analysis['suggestions'].append(f'委比指标可能存在虚假信号，已增加交叉验证：委比>{STRATEGY_WEBI_VERIFY["weibi_high"]}但股价下跌时降分，委比高但外盘占比<{STRATEGY_WEBI_VERIFY["outer_ratio_low"]}%或量比<{STRATEGY_WEBI_VERIFY["volume_ratio_low"]}时降分')

    # 看跌误判分析（预测跌但实际涨）
    if bear_misjudge:
        avg_actual_change = sum(s['actual_change'] for s in bear_misjudge) / len(bear_misjudge)
        analysis['bear_misjudge_analysis'] = (
            f'共{len(bear_misjudge)}只看跌误判，实际平均涨幅{avg_actual_change:.2f}%。'
            f'{"大盘上涨带动反弹，属于系统性机会" if market_change > 1 else "可能低估了超跌反弹动能"}'
        )

    if not analysis['suggestions']:
        if bull_misjudge or bear_misjudge:
            analysis['suggestions'].append('建议持续观察，积累更多样本后优化权重')
        else:
            analysis['suggestions'].append('当前策略表现良好，维持现有权重')

    return analysis
