"""strategy.py - split from main.py"""
import asyncio
from typing import List, Dict, Optional
from datetime import date, timedelta
from app.config import (STRATEGY_BULL_THRESHOLD, STRATEGY_BEAR_THRESHOLD, STRATEGY_BACKTEST_PREDICTION_LIMIT, STRATEGY_SCORE_THRESHOLDS, STRATEGY_SCORE_VALUES, STRATEGY_WEBI_VERIFY, STRATEGY_MAIN_FORCE_SWEEP, STRATEGY_MAIN_FORCE_CONTROL, STRATEGY_PE_FILTER, STRATEGY_LABEL_THRESHOLDS, DEFAULT_WEIGHTS, STRATEGY_INDUSTRY_NEUTRALIZATION_ENABLED, STRATEGY_INDUSTRY_NEUTRAL_MIN_SAMPLE, STRATEGY_INDUSTRY_P80_BONUS, STRATEGY_INDUSTRY_P20_PENALTY, TREND_LIMIT_UP_THRESHOLD, RECOMMEND_TREND_STALE_DRIFT, RECOMMEND_TREND_STALE_FACTOR, TREND_GATE_ENABLED, TREND_GATE_LOW_SCORE, TREND_GATE_MIN_CHANGE, DEAD_CAT_BOUNCE_PENALTY)
from db.database import (get_db_conn, load_strategy_factor_weights, save_strategy_factor_weights, save_predictions)
# 2026-10-14：IC 权重学习开关（bug 修复后默认先影子，见 config 注释）
from app.config import FACTOR_WEIGHT_LEARNING_ENABLED
from services.data.stock import fetch_stock_data, fetch_with_retry, parse_tencent_batch_data, get_kline_data


def _score_main_inflow(main_inflow_ratio: Optional[float]) -> float:
    """主力净流入因子评分（2026-06-30）

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


def _score_holder_structure(holder_num: Optional[int], ratio: Optional[float]) -> float:
    """筹码集中度因子评分（2026-07-06，第 10 因子）

    基于股东人数绝对值 + 环比变化率(%)评分：
    - 户数少 + 环比减少 → 筹码集中，主力吸筹，看涨
    - 户数多 → 抛压重，难拉升
    - 环比增加 → 散户化，看跌

    判断优先级：无数据 → 散户化(≥+10%) → 抛压重(>20万) → 户数+环比组合分档
    与外盘占比、主力净流入形成筹码层防护：避免"散户扎堆+筹码分散"的票被高分推荐。
    """
    vals = STRATEGY_SCORE_VALUES['holder_structure']
    if holder_num is None or ratio is None:
        return vals['no_data']

    thr = STRATEGY_SCORE_THRESHOLDS['holder_structure']

    # 1. 散户化强烈信号（环比增加≥10%，不论户数，最优先）
    if ratio >= thr['dispersion_ratio']:
        return vals['negative']  # 2

    # 2. 大盘抛压重（户数>20万）：结合环比再细分
    if holder_num > thr['large_hold']:
        if ratio > 0:  # 大盘 + 散户化（0~10%）
            return vals['negative']  # 2
        return vals['low']  # 5：大盘但筹码趋稳/集中

    # 3. 小盘高度集中（户数<3万 且 环比减少≥10%）
    if holder_num < thr['tiny_hold'] and ratio <= thr['concentrate_ratio']:
        return vals['high']  # 15

    # 4. 小盘筹码趋集中（户数<3万 且 环比减少）
    if holder_num < thr['tiny_hold'] and ratio < 0:
        return vals['mid']  # 12

    # 5. 中小盘筹码集中（户数 3-8万 且 环比减少≥5%）
    if holder_num < thr['small_hold'] and ratio <= -thr['stable_range']:
        return vals['mid']  # 12

    # 6. 筹码稳定（户数<8万 且 环比±5%内）
    if holder_num < thr['small_hold'] and abs(ratio) <= thr['stable_range']:
        return vals['normal']  # 8

    # 7. 中大盘散户化（户数 8-20万 且 环比增加）
    if holder_num <= thr['large_hold'] and ratio > 0:
        return vals['low']  # 5

    # 8. 默认中性（修复：原缩进错误导致此行嵌套在 #7 的 if 块内，永不执行，
    #    函数对未命中任何条件的股票隐式返回 None，在 sum() 时抛 TypeError）
    return vals['normal']  # 8


def _score_rps(rps: Optional[float]) -> float:
    """RPS 相对强度因子评分（2026-07-16 新增，欧奈尔核心因子）

    基于个股区间涨幅在全市场的百分位排名(0~100)：
    - ≥90（强于90%个股，龙头候选）→ 15 分（high）
    - ≥80 → 10 分（mid）
    - ≥70 → 7 分（low）
    - ≥50 → 3 分（weak，中位附近）
    - <50 → 0 分（弱势，RPS 不及一半股票，明确低配）
    - None（无数据）→ 7 分（no_data 中档，避免无数据股票总分被拖累）

    与涨幅因子(change_percent)区别：change_percent 是"当日/短期涨幅"，
    RPS 是"相对全市场的区间强度排名"，是趋势市选龙头的关键维度。
    """
    vals = STRATEGY_SCORE_VALUES['rps']
    if rps is None:
        return vals['no_data']
    thr = STRATEGY_SCORE_THRESHOLDS['rps']
    if rps >= thr['high']:
        return vals['high']
    if rps >= thr['mid']:
        return vals['mid']
    if rps >= thr['low']:
        return vals['low']
    if rps >= thr['weak']:
        return vals['weak']
    return 0  # <50 偏弱，明确低配（不拖累也无虚高）


def _score_fundamental(roe: Optional[float], yoy: Optional[float]) -> float:
    """基本面质量因子评分（2026-07-16 P1a）

    基于 ROE(加权净资产收益率%) + 归母净利润同比%(SJLTZ)：
    - ROE≥15% 且 净利同比≥30% → 15 分（high，高质量成长）
    - ROE≥10% 且 净利同比≥15% → 11 分（mid，稳健优质）
    - ROE≥5% 且 净利同比≥0% → 8 分（low，及格）
    - ROE 在 0~5% 或 净利同比 -20%~0% → 4 分（weak，平庸/放缓）
    - ROE<0（亏损）或 净利同比<-20%（大幅下滑）→ 3 分（negative，质量差）
    - None（无数据）→ 8 分（no_data 中档，避免无数据股票总分被拖累）

    与主力净流入、筹码集中度形成"质量防护"：避免垃圾股仅靠资金面被高分推荐。
    """
    vals = STRATEGY_SCORE_VALUES['fundamental']
    if roe is None or yoy is None:
        return vals['no_data']
    thr = STRATEGY_SCORE_THRESHOLDS['fundamental']
    if roe < 0:
        return vals['negative']  # 亏损
    if yoy < -20:
        return vals['negative']  # 盈利大幅下滑
    if roe >= thr['roe_high'] and yoy >= thr['yoy_high']:
        return vals['high']
    if roe >= thr['roe_mid'] and yoy >= thr['yoy_mid']:
        return vals['mid']
    if roe >= 5 and yoy >= 0:
        return vals['low']
    return vals['weak']


def _score_smart_money(info: Optional[Dict]) -> float:
    """聪明钱因子评分（2026-07-16 P1b，纯加分信号）

    info: {'on_board': bool, 'net_amt': float(龙虎榜净买入元), 'appearances': int}
    - 上榜且净买入 ≥5000万 → 10 分（high，机构/游资强力介入）
    - 上榜且净买入 ≥1000万 → 6 分（mid，游资介入）
    - 上榜且净买入 >0 → 4 分（low，有资金关注）
    - 未上榜/净卖出 → 0 分（none，纯加分不扣分，无信号不虚高）

    设计核心：这是"Bonus"因子，无数据时给 0 而非中档分，避免拖累普通股票。
    """
    vals = STRATEGY_SCORE_VALUES['smart_money']
    if not info or not info.get('on_board'):
        return vals['none']
    net = float(info.get('net_amt') or 0)
    thr = STRATEGY_SCORE_THRESHOLDS['smart_money']
    if net >= thr['net_amt_high']:
        return vals['high']
    if net >= thr['net_amt_mid']:
        return vals['mid']
    if net > 0:
        return vals['low']
    return vals['none']


def calc_stock_score_v2(data: Dict, weights: Dict = None, market_change: float = 0.0,
                        trend_result: Dict = None,
                        fund_flow_data: Optional[Dict] = None,
                        holder_data: Optional[Dict] = None,
                        rps_data: Optional[Dict] = None,
                        fundamental_data: Optional[Dict] = None,
                        smart_money_data: Optional[Dict] = None) -> Dict:
    """使用动态权重的评分函数 V2 (V4.1 融合趋势评分 + 新增主力净流入因子)

    Args:
        data: 股票特征数据（实时行情/日内因子）
        weights: 策略因子权重
        market_change: 大盘涨跌幅，用于根据市场环境动态调整评分阈值
        trend_result: 趋势评分结果（来自 is_up_trend），包含 score(0-100) 等字段。
                      提供后，最终评分 = 50% 日内因子 + 50% 趋势因子
        fund_flow_data: 主力资金流向数据（新增），格式：
                        {'main_net_inflow': float(元), 'main_inflow_ratio': float(%),
                         'circ_mv': float(元)}
                        提供后，新增第9因子 main_capital_inflow 参与评分
        holder_data: 股东人数筹码面数据（新增），格式：
                     {'holder_total_num': int, 'total_num_ratio': float(%),
                      'avg_free_shares': float, 'avg_hold_amt': float, 'end_date': str}
                     提供后，新增第10因子 holder_structure 参与评分（月度数据，从 DB 缓存读取）
    """
    if weights is None:
        weights = load_strategy_factor_weights()

    # 趋势评分（若提供，后续与日内因子混合）
    trend_score = trend_result.get('score', 0) if trend_result else None
    # 趋势分新鲜度校验（2026-10-12）：趋势分来自"扫描时刻"的快照（checkPrice），
    # 若当前价已较扫描时明显下跌，说明趋势信号失真（典型案例：万向德农 9:35 触涨停时
    # 被扫描捕捉到 97.6 分，10:00 已炸板下跌，趋势分不再代表当下），按系数打折，
    # 防止"最强瞬间的快照分"给已走弱的股票背书。
    trend_stale = False
    if trend_score and trend_result:
        try:
            details = trend_result.get('details') or {}
            scan_price = float(details.get('checkPrice') or details.get('latestClose') or 0)
            cur_price = float(data.get('priceRaw') or data.get('price') or 0)
            if scan_price > 0 and cur_price > 0 and cur_price < scan_price * (1 - RECOMMEND_TREND_STALE_DRIFT):
                trend_score = round(trend_score * RECOMMEND_TREND_STALE_FACTOR, 1)
                trend_stale = True
        except (TypeError, ValueError):
            pass

    outer_ratio = data.get('outerRatio', 50)
    volume_ratio = data.get('volumeRatio', 0)
    turnover_rate = data.get('turnoverRate', 0)
    change_percent = data.get('changePercent', 0)
    weibi = data.get('weibi', 0)
    avg_price_deviation = data.get('avgPriceDeviation', 0)
    amplitude = data.get('amplitude', 0)
    pe = data.get('pe', 0)

    # 主力净流入占比（%）— 无数据时为 None，评分时给 no_data 中档分
    main_inflow_ratio = None
    if fund_flow_data:
        # 优先用已计算好的占比，否则用 main_net_inflow / circ_mv 现算
        if fund_flow_data.get('main_inflow_ratio') is not None:
            main_inflow_ratio = float(fund_flow_data['main_inflow_ratio'])
        elif fund_flow_data.get('main_net_inflow') is not None and fund_flow_data.get('circ_mv'):
            mv = float(fund_flow_data['circ_mv'])
            if mv > 0:
                main_inflow_ratio = float(fund_flow_data['main_net_inflow']) / mv * 100

    # 股东人数筹码面（户数 + 环比变化率%）— 无数据时给 no_data 中档分
    holder_num = None
    holder_ratio = None
    if holder_data:
        if holder_data.get('holder_total_num') is not None:
            holder_num = int(holder_data['holder_total_num'])
        if holder_data.get('total_num_ratio') is not None:
            holder_ratio = float(holder_data['total_num_ratio'])

    # RPS 相对强度（2026-07-16 新增）：个股 RPS 百分位(0~100)，无数据给 no_data 中档分
    rps_value = None
    if rps_data:
        if rps_data.get('rps') is not None:
            try:
                rps_value = float(rps_data['rps'])
            except (TypeError, ValueError):
                rps_value = None

    # 基本面质量（2026-07-16 P1a）：ROE(%) + 归母净利润同比(%)，无数据给 no_data 中档分
    fund_roe = None
    fund_yoy = None
    if fundamental_data:
        if fundamental_data.get('roe') is not None:
            try:
                fund_roe = float(fundamental_data['roe'])
            except (TypeError, ValueError):
                fund_roe = None
        if fundamental_data.get('net_profit_yoy') is not None:
            try:
                fund_yoy = float(fundamental_data['net_profit_yoy'])
            except (TypeError, ValueError):
                fund_yoy = None

    # 聪明钱（2026-07-16 P1b）：龙虎榜净买入等，无信号给 none(0 分，纯加分不拖累)
    sm_info = None
    if smart_money_data:
        sm_info = {
            'on_board': bool(smart_money_data.get('on_board', False)),
            'net_amt': float(smart_money_data.get('net_amt') or 0),
            'appearances': int(smart_money_data.get('appearances') or 0),
        }

    # 基础得分（与原逻辑一致）
    base_scores = {
        'outer_ratio': STRATEGY_SCORE_VALUES['outer_ratio']['high'] if outer_ratio >= STRATEGY_SCORE_THRESHOLDS['outer_ratio']['high'] else STRATEGY_SCORE_VALUES['outer_ratio']['mid'] if outer_ratio >= STRATEGY_SCORE_THRESHOLDS['outer_ratio']['mid'] else STRATEGY_SCORE_VALUES['outer_ratio']['low'],
        'volume_ratio': STRATEGY_SCORE_VALUES['volume_ratio']['high'] if volume_ratio >= STRATEGY_SCORE_THRESHOLDS['volume_ratio']['high'] else STRATEGY_SCORE_VALUES['volume_ratio']['mid'] if volume_ratio >= STRATEGY_SCORE_THRESHOLDS['volume_ratio']['mid'] else STRATEGY_SCORE_VALUES['volume_ratio']['normal'] if volume_ratio >= STRATEGY_SCORE_THRESHOLDS['volume_ratio']['low'] else STRATEGY_SCORE_VALUES['volume_ratio']['low'],
        'turnover_rate': STRATEGY_SCORE_VALUES['turnover_rate']['high'] if (STRATEGY_SCORE_THRESHOLDS['turnover_rate']['high'][0] <= turnover_rate <= STRATEGY_SCORE_THRESHOLDS['turnover_rate']['high'][1]) else STRATEGY_SCORE_VALUES['turnover_rate']['mid'] if (STRATEGY_SCORE_THRESHOLDS['turnover_rate']['mid'][0] <= turnover_rate < STRATEGY_SCORE_THRESHOLDS['turnover_rate']['mid'][1]) else STRATEGY_SCORE_VALUES['turnover_rate']['normal'] if turnover_rate > STRATEGY_SCORE_THRESHOLDS['turnover_rate']['high_penalty'] else STRATEGY_SCORE_VALUES['turnover_rate']['low'],
        'change_percent': STRATEGY_SCORE_VALUES['change_percent']['high'] if change_percent > STRATEGY_SCORE_THRESHOLDS['change_percent']['high'] else STRATEGY_SCORE_VALUES['change_percent']['mid'] if change_percent > STRATEGY_SCORE_THRESHOLDS['change_percent']['mid'] else STRATEGY_SCORE_VALUES['change_percent']['normal'] if change_percent > STRATEGY_SCORE_THRESHOLDS['change_percent']['low'] else STRATEGY_SCORE_VALUES['change_percent']['negative'] if change_percent > STRATEGY_SCORE_THRESHOLDS['change_percent']['negative'] else 0,
        'weibi': STRATEGY_SCORE_VALUES['weibi']['high'] if weibi > STRATEGY_SCORE_THRESHOLDS['weibi']['high'] else STRATEGY_SCORE_VALUES['weibi']['mid'] if weibi >= STRATEGY_SCORE_THRESHOLDS['weibi']['mid'] else STRATEGY_SCORE_VALUES['weibi']['low'] if weibi > STRATEGY_SCORE_THRESHOLDS['weibi']['low'] else STRATEGY_SCORE_VALUES['weibi']['negative'],
        'avg_price_deviation': STRATEGY_SCORE_VALUES['avg_price_deviation']['high'] if avg_price_deviation > STRATEGY_SCORE_THRESHOLDS['avg_price_deviation']['high'] else STRATEGY_SCORE_VALUES['avg_price_deviation']['mid'] if avg_price_deviation > STRATEGY_SCORE_THRESHOLDS['avg_price_deviation']['mid'] else STRATEGY_SCORE_VALUES['avg_price_deviation']['normal'] if avg_price_deviation > STRATEGY_SCORE_THRESHOLDS['avg_price_deviation']['low'] else STRATEGY_SCORE_VALUES['avg_price_deviation']['low'],
        'amplitude': STRATEGY_SCORE_VALUES['amplitude']['high'] if (STRATEGY_SCORE_THRESHOLDS['amplitude']['high'][0] < amplitude <= STRATEGY_SCORE_THRESHOLDS['amplitude']['high'][1]) else STRATEGY_SCORE_VALUES['amplitude']['mid'] if (STRATEGY_SCORE_THRESHOLDS['amplitude']['mid'][0] < amplitude <= STRATEGY_SCORE_THRESHOLDS['amplitude']['mid'][1]) else STRATEGY_SCORE_VALUES['amplitude']['low'],
        # 主力净流入因子（2026-06-30）：无数据给 no_data 中档分，避免拖累总分
        'main_capital_inflow': _score_main_inflow(main_inflow_ratio),
        # 股东人数筹码面因子（2026-07-06）：无数据给 no_data 中档分
        'holder_structure': _score_holder_structure(holder_num, holder_ratio),
        # RPS 相对强度因子（2026-07-16 新增）：无数据给 no_data 中档分
        'rps': _score_rps(rps_value),
        # 基本面质量因子（2026-07-16 P1a）：无数据给 no_data 中档分
        'fundamental': _score_fundamental(fund_roe, fund_yoy),
        # 聪明钱因子（2026-07-16 P1b）：纯加分，无信号给 none(0 分)
        'smart_money': _score_smart_money(sm_info),
    }
    # 2026-07-08 修复：涨停股振幅豁免。涨停股振幅必然>8%（开盘价~昨收→涨停价振幅10%），
    # 当前会被 amplitude > 8 扣到 low=2分，不合理。涨停时振幅≥5%按 high=5 给分。
    if change_percent >= TREND_LIMIT_UP_THRESHOLD and amplitude >= 5:
        base_scores['amplitude'] = STRATEGY_SCORE_VALUES['amplitude']['high']
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
        # 2026-07-08 优化：放量惩罚阈值从1.5提高到2.0，避免正常活跃交易(1.0-2.0)被误判为对倒
        if outer_ratio < 50 and volume_ratio > 3:
            base_scores['outer_ratio'] = max(0, base_scores['outer_ratio'] - 5)
            base_scores['volume_ratio'] = max(0, base_scores['volume_ratio'] - 5)
        if volume_ratio > 2.0 and outer_ratio < 55:
            base_scores['volume_ratio'] = max(0, base_scores['volume_ratio'] - 5)
            base_scores['outer_ratio'] = max(0, base_scores['outer_ratio'] - 5)
        
    # 2026-06-11 策略优化建议：不再盲目惩罚涨幅，而是区分"强势启动"与"力竭赶顶"
    # 2026-06-11 策略优化建议：拥抱强势龙头股。涨停不代表风险，往往代表次日的高溢价。
    # 涨停质量增强（2026-06-30）：用委比+换手+量比组合代理封板质量
    # 业界共识：封板质量是次日溢价的关键——强力封板次日高开概率高，烂板次日低开概率高
    # 分档：强力封板25 / 优质封板22 / 普通封板18 / 弱封板14 / 烂板8
    # 2026-07-14 修复：收盘后委比数据常归0或失真，涨停股不能简单判为烂板
    # 涨停本身是强势信号，需结合量能判断封板质量
    if change_percent > 9.5:
        if weibi > 95 and 2 <= turnover_rate <= 8 and volume_ratio < 3:
            base_scores['change_percent'] = STRATEGY_SCORE_VALUES['change_percent']['high']  # 强力封板（28分：一字/秒板委比极高压单小，次日高溢价）
        elif weibi > 80 and 2 <= turnover_rate <= 15:
            base_scores['change_percent'] = 25  # 优质封板（2026-07-08 22→25：真强势封板次日高溢价）
        elif weibi > 50 and turnover_rate <= 20:
            base_scores['change_percent'] = 20  # 普通封板（2026-07-08 18→20：封单一般，有换手但未爆量）
        elif weibi >= 0:
            base_scores['change_percent'] = 18  # 收盘委比归0或弱封板，仍给基础涨停分（2026-07-14 15→18）
        else:
            base_scores['change_percent'] = 12  # 烂板/开板（委比为负，封单被砸开；2026-07-14 8→12）
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
        # 2026-08-31 评分优化：缩量上涨扣分（量价背离）。
        # 0<涨幅≤3% 且量比<0.6（明显缩量）= 无量推升，做多动能不足、持续性差，
        # 不给满额涨幅分，避免"无量微涨"混入强势梯队。
        if volume_ratio < 0.6:
            base_scores['change_percent'] = 12
        else:
            base_scores['change_percent'] = 16
    else:
        pass

    # ====== 2026-07-14 修复：涨停股反转惩罚不应因收盘委比归0而误触发 ======
    # 只有真实负委比或天量（量比>8）/低外盘才视为弱封板/烂板，需要额外惩罚
    if change_percent > 9.5:
        # 2026-08-31 评分优化：换手率>20%（天量换手）视为弱板信号。
        # 涨停当日 20% 以上筹码换手 = 大量获利盘/解套盘兑现，次日抛压显著，
        # 在大盘走弱时反转风险高，应与烂板同档处理。
        weak_board = (weibi < 0) or (volume_ratio > 8) or (outer_ratio < 45) or (turnover_rate > 20)
        if weak_board:
            if market_change < -0.5:
                # 大盘显著下跌：涨停股次日反转风险极高，重罚
                reversal_penalty = 10
            elif market_change < 0:
                # 大盘微跌：中等反转风险
                reversal_penalty = 6
            elif market_change < 0.3:
                # 大盘平盘：轻度反转风险
                reversal_penalty = 3
            else:
                # 大盘上涨：涨停股可能延续强势，不罚
                reversal_penalty = 0
            # 真烂板（委比为负且天量）额外加重惩罚
            if weibi < 0 and volume_ratio > 5:
                reversal_penalty += 3
            base_scores['change_percent'] = max(0, base_scores['change_percent'] - reversal_penalty)
        else:
            # 非弱封板：收盘委比归0或温和量能，保留基础涨停分，避免被反转惩罚直接抹零
            base_scores['change_percent'] = max(15, base_scores['change_percent'])

    # 2026-06-11 策略优化建议：大盘上涨时的优选策略
    # 当大盘上涨（market_change > 0）时，优先选择“外盘占比 > 60% 且 量比 < 1.5”的股票（缩量稳步上涨，非对倒放量）。
    if market_change > 0 and outer_ratio > 60 and volume_ratio < 1.5:
        base_scores['outer_ratio'] += 5
        base_scores['volume_ratio'] += 5

    # 2026-06-11 策略优化建议：虚假封单过滤 (委比 100% 陷阱)
    # 误判样本如旭光电子、昊华科技在委比 100% 时实际下跌，说明封单可能是诱多虚假单。
    # 2026-07-08 修复：涨停股豁免此规则——涨停+100%委比恰恰是真强势封板的特征，
    # 不应触发诱多过滤。涨停股已经在 line 185-195 用更严格的"天量开板/封板质量"判定出货嫌疑。
    if weibi > 99 and not is_limit_up:
        # 如果委比接近 100% 但外盘占比并未同步处于高位（>60%），则该委比可信度极低。
        if outer_ratio < 60:
            base_scores['weibi'] = 0
            base_scores['outer_ratio'] = max(0, base_scores['outer_ratio'] - 5)

    # ====== P1-4 主力扫货识别（2026-07-07）======
    # 核心场景：急拉 + 负委比（卖盘被吃光）+ 高外盘 + 温和量能 = 主力扫货
    # 时间无关：上午/午后/尾盘急拉扫货均可命中，关键是组合特征而非发生时刻
    # 例：002384 东山精密 急拉6.79% / 委比-26.67% / 外盘55.7% / 量比1.17 / 换手4.9%
    # 原逻辑仅按委比阈值给 4 分（low），完全漏掉"主力吃光卖盘"的强看涨信号
    if (change_percent >= STRATEGY_MAIN_FORCE_SWEEP['change_percent_min']
        and weibi <= STRATEGY_MAIN_FORCE_SWEEP['weibi_negative_max']
        and outer_ratio >= STRATEGY_MAIN_FORCE_SWEEP['outer_ratio_min']
        and volume_ratio <= STRATEGY_MAIN_FORCE_SWEEP['volume_ratio_max']
        and turnover_rate >= STRATEGY_MAIN_FORCE_SWEEP['turnover_rate_min']):
        base_scores['weibi'] = max(base_scores['weibi'], STRATEGY_MAIN_FORCE_SWEEP['weibi_bonus_score'])
        base_scores['volume_ratio'] = max(base_scores['volume_ratio'], STRATEGY_MAIN_FORCE_SWEEP['volume_ratio_bonus_score'])

    # ====== P1-4 主力控盘识别（2026-07-07）======
    # 核心场景：低量比 + 急拉 + 高换手 + 高外盘 = 主力控盘锁仓拉升
    # 时间无关：任何时段的控盘锁仓拉升均可命中
    # 低量比不代表弱势，可能是主力高度控盘后用少量资金快速拉升
    if (change_percent >= STRATEGY_MAIN_FORCE_CONTROL['change_percent_min']
        and volume_ratio <= STRATEGY_MAIN_FORCE_CONTROL['volume_ratio_max']
        and turnover_rate >= STRATEGY_MAIN_FORCE_CONTROL['turnover_rate_min']
        and outer_ratio >= STRATEGY_MAIN_FORCE_CONTROL['outer_ratio_min']):
        base_scores['volume_ratio'] = max(base_scores['volume_ratio'], STRATEGY_MAIN_FORCE_CONTROL['volume_ratio_bonus_score'])

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
    # ：从 8 因子扩展到 9 因子（新增 main_capital_inflow），硬编码改为动态计算
    # 2026-07-08 修复：归一化公式从 sum(max)*avg(w) 改为正确的 sum(max[f]*w[f])
    # 原公式当高权重因子满分值低时会高估 max_possible，导致所有评分被系统性压低
    def _factor_max(f: str) -> float:
        """从 STRATEGY_SCORE_VALUES 提取该因子的最大档位分值"""
        vals = STRATEGY_SCORE_VALUES.get(f, {})
        # 过滤掉非数值的 key（如 'no_data'）后取最大
        numeric_vals = [v for v in vals.values() if isinstance(v, (int, float))]
        return max(numeric_vals) if numeric_vals else 0
    max_possible = sum(_factor_max(f) * weights.get(f, {}).get('weight', 1.0) for f in base_scores)
    if max_possible > 0:
        total = round(total / max_possible * 100)
    total = min(100, max(0, total))

    label = ''
    
    # AI回测优化建议规则2: 考虑大盘环境因素，当大盘下跌时降低策略的看涨阈值或增加惩罚
    bull_threshold = STRATEGY_BULL_THRESHOLD
    strong_bull_threshold = STRATEGY_LABEL_THRESHOLDS['strong_bull']
    label_bull_threshold = STRATEGY_LABEL_THRESHOLDS['bull']
    
    if market_change < 0:
        # 2026-07-14 修复：降低大盘下跌惩罚，避免涨停股等强势信号被误标为观望
        # 原公式 min(25, |change*15|) 过于严苛，导致收盘后强势票被系统性压低
        # 新公式：只影响方向预测阈值，标签阈值保持固定，保证用户看到稳定的信号强度
        penalty = min(15, int(abs(market_change) * 8))
        bull_threshold += penalty
        # 标签阈值保持固定（≥70 偏多看涨 / ≥80 强烈看涨），不再随大盘下跌而上调
        # strong_bull_threshold 和 label_bull_threshold 不再 += penalty

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

    # V4.2: 自适应融合趋势评分（权重随大盘状态动态调整），仅当 trend_result 提供时生效
    # 优化（2026-08-03）：原 V4.1 固定 50/50 融合，不适应不同市场状态：
    #   - 牛市（market_change > 0.5%）：趋势延续性强，趋势分权重更高 → 40% 日内 + 60% 趋势
    #   - 熊市（market_change < -0.5%）：趋势信号易被熊市反弹误导，日内分权重更高 → 60% 日内 + 40% 趋势
    #   - 震荡市（-0.5% ~ 0.5%）：两者均重要 → 50/50
    # 另修复：趋势分=0（被强制排除的股票）不应直接 50% 融合拖累日内分，
    #         改为仅取日内分的 70%（轻度折价但不过度惩罚）
    intraday_score = total
    # 死猫反弹门控（2026-10-13）：趋势处于下行区(<TREND_GATE_LOW_SCORE)且当日大涨(≥3%)，
    # 属"下跌中继反弹"，日内动量分(change_percent/outer_ratio等)不可信，融合前打折，
    # 避免反弹股凭单日暴涨在纯日内口径下反超趋势良好股。
    dead_cat_bounce = False
    if trend_score is not None:
        if TREND_GATE_ENABLED and 0 < trend_score < TREND_GATE_LOW_SCORE and change_percent >= TREND_GATE_MIN_CHANGE:
            dead_cat_bounce = True
            intraday_score = round(intraday_score * (1 - DEAD_CAT_BOUNCE_PENALTY))
        if trend_score == 0:
            # 趋势评分=0 意味着股票被强制排除（ST/利空/RSI超买等），
            # 轻度折价而非直接拉到 0
            total = round(intraday_score * 0.7)
        else:
            # 自适应权重：根据大盘涨跌幅动态调整
            if market_change > 0.5:
                trend_weight = 0.6   # 牛市：趋势分更可信
            elif market_change < -0.5:
                trend_weight = 0.4   # 熊市：日内分更敏感
            else:
                trend_weight = 0.5   # 震荡：均衡
            total = round(intraday_score * (1 - trend_weight) + trend_score * trend_weight)
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
        'trend_score': trend_score,         # V4.1: 趋势分（2026-10-12 起为新鲜度校验后的值）
        'trend_stale': trend_stale,         # 2026-10-12: 趋势分是否因现价低于扫描价被打折
        'dead_cat_bounce': dead_cat_bounce, # 2026-10-13: 是否触发死猫反弹门控（下跌趋势单日大涨被抑制）
        'factor_values': {
            'outer_ratio': outer_ratio,
            'volume_ratio': volume_ratio,
            'turnover_rate': turnover_rate,
            'change_percent': change_percent,
            'weibi': weibi,
            'avg_price_deviation': avg_price_deviation,
            'amplitude': amplitude,
            'main_capital_inflow': main_inflow_ratio,  # : 主力净流入占比(%)，None=无数据
            'holder_structure': {'holder_num': holder_num, 'ratio': holder_ratio},  # : 股东人数+环比%，None=无数据
            'rps': rps_value,  # : RPS 相对强度百分位(0~100)，None=无数据
            'fundamental': {'roe': fund_roe, 'net_profit_yoy': fund_yoy},  # : ROE% + 净利同比%，None=无数据
            'smart_money': sm_info,  # : 聪明钱 {on_board,net_amt,appearances}，None=无信号
        }
    }


def apply_industry_neutralization(stocks: List[Dict], industry_map: Dict[str, str]) -> None:
    """行业中性化：按行业分位数调整 strategy_score.total（原地修改，2026-06-30）

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


async def _get_next_day_change_from_kline(symbol: str, pred_date: str) -> Optional[float]:
    """从K线数据获取预测日次日的涨跌幅
    
    2026-07-08 修复：历史补验原来用 fetch_stock_data 获取今天的实时涨跌，
    而非预测日次日的涨跌。改用 get_kline_data 从K线中找到预测日次日的收盘价，
    计算正确的涨跌幅。
    
    Args:
        symbol: 股票代码 (如 sh603380)
        pred_date: 预测日期 (如 2026-07-04)
    
    Returns:
        次日涨跌幅(%)，如无法获取返回 None
    """
    try:
        kline = await get_kline_data(symbol.lower())
        if not kline or len(kline) < 2:
            return None
        # 在K线中找到预测日的位置
        for i, k in enumerate(kline):
            if k[0] == pred_date:
                if i + 1 < len(kline):
                    pred_close = float(k[2])
                    next_close = float(kline[i + 1][2])
                    if pred_close > 0:
                        return round((next_close - pred_close) / pred_close * 100, 2)
                break  # 找到预测日但没有次日数据（可能是今天）
        # 预测日不在K线中（可能是非交易日生成的预测），找最近的前一个交易日
        pred_date_dt = None
        from datetime import datetime as _dt
        try:
            pred_date_dt = _dt.strptime(pred_date, '%Y-%m-%d').date()
        except Exception:
            return None
        best_idx = -1
        for i, k in enumerate(kline):
            try:
                kline_date = _dt.strptime(k[0], '%Y-%m-%d').date()
                if kline_date <= pred_date_dt:
                    best_idx = i
                else:
                    break
            except Exception:
                continue
        if best_idx >= 0 and best_idx + 1 < len(kline):
            pred_close = float(kline[best_idx][2])
            next_close = float(kline[best_idx + 1][2])
            if pred_close > 0:
                return round((next_close - pred_close) / pred_close * 100, 2)
        return None
    except Exception as e:
        print(f'  [补验] K线获取失败 {symbol}: {e}')
        return None


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
                    # 2026-07-08 修复：用K线数据获取预测日次日的涨跌幅
                    # 原代码用 fetch_stock_data 获取今天的实时涨跌，对历史预测完全错误
                    actual_change = await _get_next_day_change_from_kline(pred['symbol'], ud)
                    if actual_change is None:
                        continue
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
                    # 2026-07-08 修复：用K线获取补验日次日的大盘涨跌，而非今天的实时数据
                    ud_market_change = 0.0
                    try:
                        idx_change = await _get_next_day_change_from_kline('sh000001', ud)
                        if idx_change is not None:
                            ud_market_change = idx_change
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
        # 2026-07-08 修复：accuracy 分母使用实际验证数(bull_total)而非全部预测数(total_count)
        # 原代码 total_count = len(predictions) 包含行情拉取失败被 continue 跳过的记录
        # 导致 accuracy 被系统性拉低
        correct_count = 0
        verified_count = 0  # 实际验证的预测数（替代 total_count 作为分母）
        bull_correct = 0
        bull_total = 0
        bull_misjudge = []
        # 2026-07-14：补充 bear 计数（保持与原硬编码 0 一致 — 暂未按 predict_dir 分桶，避免改动既有 bull_accuracy 语义）
        bear_correct = 0
        bear_total = 0
        bear_misjudge = []

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
                # 2026-07-08 修复：停牌/零涨跌的股票也跳过（changePercent=0 几乎一定是数据异常）
                if symbol.lower() not in actual_change_map or actual_change == 0:
                    continue  # 拉取失败或停牌的跳过

                actual_dir = 'bull' if actual_change > 0 else 'bear' if actual_change < 0 else 'neutral'
                is_correct = predict_dir == actual_dir

                cursor.execute(
                    '''UPDATE strategy_predict_records
                       SET verified=1, actual_change=?, actual_direction=?, is_correct=?
                       WHERE predict_date=? AND symbol=?''',
                    (actual_change, actual_dir, 1 if is_correct else 0, yesterday, symbol)
                )

            verified_count += 1
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
        # 2026-10-14：权重 bug 修好后，学习回路默认先"影子运行"——
        # 调整明细照样算、照样写进回测报告供对照，但不落 strategy_factor_weights，
        # 因为线上评分此刻仍冻结在 2026-09-04 的权重上；直接放开会让"今天胜率变了"
        # 再次无法归因（行情 or 权重漂移）。确认要放开把
        # config.FACTOR_WEIGHT_LEARNING_ENABLED 改成 True 即可。
        if FACTOR_WEIGHT_LEARNING_ENABLED:
            save_strategy_factor_weights(weights)
        else:
            for _a in weight_adjustments:
                _a['applied'] = False
            _moved = sum(1 for _a in weight_adjustments if _a.get('action') != 'keep')
            print(f'[回测] 权重学习=影子：{_moved}/{len(weight_adjustments)} 个因子本应调整，'
                  f'未写入 strategy_factor_weights（FACTOR_WEIGHT_LEARNING_ENABLED=False）')

        # 生成误判分析
        misjudge_analysis = await _analyze_misjudgments(bull_misjudge, [], market_change, weights)

        # 计算准确率
        # 2026-07-08 修复：分母使用 verified_count（实际验证数），不再使用 total_count（含跳过记录）
        accuracy = round(correct_count / verified_count * 100, 2) if verified_count > 0 else 0
        bull_accuracy = round(bull_correct / bull_total * 100, 2) if bull_total > 0 else 0
        # 2026-07-14 修复：补全 bear_accuracy 计算（之前硬编码 0，导致 scheduler 访问 result['bear_accuracy'] KeyError）
        bear_accuracy = round(bear_correct / bear_total * 100, 2) if bear_total > 0 else 0

        # 保存回测报告
        import json
        cursor.execute(
            '''INSERT OR REPLACE INTO strategy_backtest_reports
               (backtest_date, market_change, total_predictions, correct_count, accuracy,
                bull_accuracy, bear_accuracy, bull_misjudge_count, bear_misjudge_count,
                misjudge_analysis, weight_adjustments)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
            (yesterday, market_change, verified_count, correct_count, accuracy,
             bull_accuracy, bear_accuracy, len(bull_misjudge), len(bear_misjudge),
             json.dumps(misjudge_analysis, ensure_ascii=False),
             json.dumps(weight_adjustments, ensure_ascii=False))
        )
        conn.commit()
        conn.close()

        return {
            'date': yesterday,
            'market_change': market_change,
            'total': verified_count,
            'correct': correct_count,
            'accuracy': accuracy,
            'bull_accuracy': bull_accuracy,
            'bear_accuracy': bear_accuracy,  # 2026-07-14 修复：补齐字段，scheduler 调度需要
            'bull_misjudge': bull_misjudge,
            'bear_misjudge': bear_misjudge,  # 2026-07-14 修复：补齐字段，便于前端展示
            'misjudge_analysis': misjudge_analysis,
            'weight_adjustments': weight_adjustments,
        }
    except Exception as error:
        print(f'回测验证失败: {str(error)}')
        import traceback
        traceback.print_exc()
        return {'error': str(error)}
def _adjust_weights(predictions: List[Dict], weights: Dict, cursor) -> List[Dict]:
    """根据预测准确性调整各因子权重（2026-09-04 升级：IC/IC_IR 引擎优先，中位数区分度法兜底）

    V2（IC 引擎，对标华泰金工/米筐多因子体系）：
      - 对每个截面日计算"因子值 vs 次日超额收益"的 Spearman Rank IC
      - IC_IR = IC均值/IC标准差 作为因子质量核心指标
      - IC_IR > +0.30 → 增权（×1.05，上限2.0）；< -0.30 → 降权（×0.95，下限0.3）
      - 方向一致占比 < 50% 的因子不增权（避免噪声驱动），反向且占比 < 40% 额外压低
      - 截面日 < 5 个时该因子回退"中位数区分度"旧逻辑（冷启动兼容）

    旧逻辑（V1 区分度）保留为兜底：高因子值组 vs 低因子值组的预测准确率差异。
    """
    from services.strategy.factor_ic import FACTOR_COLUMNS, compute_factor_ic_stats, adjust_weights_by_ic_ir

    adjustments = []

    db_columns = set(FACTOR_COLUMNS)
    # rps/fundamental/smart_money 尚未持久化到 strategy_predict_records，跳过权重调整

    # 一次性拉取近30天已验证记录（所有因子列共用，消除逐因子 N 次查询）
    rows = []
    ic_error = ''
    try:
        cursor.execute(
            f'''SELECT predict_date, predict_direction, actual_change, is_correct, market_change,
                        {', '.join(FACTOR_COLUMNS)}
           FROM strategy_predict_records
           WHERE verified = 1 AND predict_date >= date('now', '-30 days')
           ORDER BY predict_date DESC LIMIT {STRATEGY_BACKTEST_PREDICTION_LIMIT}'''
        )
        # 2026-10-14 修复（P0）：必须转成 dict。sqlite3.Row 没有 .get()，
        # 而 compute_factor_ic_stats 第一行就 r.get('predict_date') —— 2026-09-04
        # IC 引擎上线当天起，这里每天都抛 AttributeError，异常一路冒泡到
        # backtest_yesterday_predictions 的外层 try，导致：
        #   ① 回测报告 INSERT 永远跑不到（strategy_backtest_reports 只剩零星几天）；
        #   ② save_strategy_factor_weights 永远跑不到，**因子权重冻结在 2026-09-04 15:20:12**；
        #   ③ 连"IC 样本不足回退 legacy 区分度"这条兜底路径都没机会执行。
        # 下面再把 IC 计算单独包一层 try：一个可选子步骤不该废掉整条回测。
        rows = [dict(r) for r in cursor.fetchall()]
    except Exception:
        rows = []

    # ---- 主路径：IC/IC_IR 引擎 ----
    try:
        ic_stats = compute_factor_ic_stats(rows) if rows else {}
    except Exception as e:
        ic_stats = {}
        ic_error = f'{type(e).__name__}: {e}'
        print(f'[回测] IC 引擎异常，本轮回退区分度兜底: {ic_error}')
    if ic_stats:
        adjustments.extend(adjust_weights_by_ic_ir(weights, ic_stats))
        # 记录引擎标识，便于回测报告区分新旧逻辑
        for a in adjustments:
            a['engine'] = 'ic_ir'

    # ---- 兜底 + 补齐：IC 截面不足或无数据的因子走旧区分度逻辑 ----
    for factor_name in DEFAULT_WEIGHTS.keys():
        if factor_name in ic_stats:
            # 权重元数据沿用 IC 口径
            weights[factor_name]['sample_count'] = ic_stats[factor_name]['n_total']
            continue
        if factor_name not in db_columns:
            adjustments.append({'factor': factor_name, 'action': 'keep', 'reason': '非数据库因子', 'engine': 'ic_ir' if ic_stats else 'legacy'})
            continue

        recent = rows
        if len(recent) < 10:
            adjustments.append({'factor': factor_name, 'action': 'keep', 'reason': f'样本不足({len(recent)})', 'engine': 'ic_ir' if ic_stats else 'legacy'})
            continue

        # 计算该因子在预测正确和错误样本中的分布差异
        correct_samples = [r for r in recent if r['is_correct'] == 1]
        wrong_samples = [r for r in recent if r['is_correct'] == 0]

        if not correct_samples or not wrong_samples:
            adjustments.append({'factor': factor_name, 'action': 'keep', 'reason': '无错误样本', 'engine': 'ic_ir' if ic_stats else 'legacy'})
            continue

        # 中位数区分度——高因子值样本组 vs 低因子值样本组的准确率差异
        factor_vals = [r[factor_name] for r in recent if r[factor_name] is not None and r[factor_name] != 0]
        if len(factor_vals) < 5:
            adjustments.append({'factor': factor_name, 'action': 'keep', 'reason': '因子值有效样本不足', 'engine': 'ic_ir' if ic_stats else 'legacy'})
            continue

        median_val = sorted(factor_vals)[len(factor_vals) // 2]
        high_group = [r for r in recent if r[factor_name] is not None and r[factor_name] > median_val]
        low_group = [r for r in recent if r[factor_name] is not None and r[factor_name] <= median_val]

        if not high_group or not low_group:
            adjustments.append({'factor': factor_name, 'action': 'keep', 'reason': '分组后样本为空', 'engine': 'ic_ir' if ic_stats else 'legacy'})
            continue

        high_correct = sum(1 for r in high_group if r['is_correct'] == 1)
        low_correct = sum(1 for r in low_group if r['is_correct'] == 1)
        high_accuracy = high_correct / len(high_group) * 100
        low_accuracy = low_correct / len(low_group) * 100

        discrimination = high_accuracy - low_accuracy
        factor_accuracy = (high_accuracy + low_accuracy) / 2

        current_weight = weights[factor_name]['weight']
        sample_count = len(recent)
        correct_count = len(correct_samples)

        # 更新权重元数据
        weights[factor_name]['accuracy'] = round(factor_accuracy, 2)
        weights[factor_name]['sample_count'] = sample_count
        weights[factor_name]['correct_count'] = correct_count
        weights[factor_name]['discrimination'] = round(discrimination, 2)

        old_weight = current_weight
        if discrimination > 10:
            new_weight = min(2.0, current_weight * 1.05)
            action = 'increase'
            reason = f'区分度{discrimination:.1f}%(高组{high_accuracy:.0f}%>低组{low_accuracy:.0f}%)，权重{old_weight:.3f}→{new_weight:.3f}'
        elif discrimination < -10:
            new_weight = max(0.3, current_weight * 0.95)
            action = 'decrease'
            reason = f'区分度{discrimination:.1f}%(高组{high_accuracy:.0f}%<低组{low_accuracy:.0f}%)，权重{old_weight:.3f}→{new_weight:.3f}'
        else:
            new_weight = current_weight
            action = 'keep'
            reason = f'区分度{discrimination:.1f}%在±10%内，权重保持{old_weight:.3f}'

        weights[factor_name]['weight'] = round(new_weight, 4)
        adjustments.append({'factor': factor_name, 'action': action, 'old_weight': round(old_weight, 4),
                            'new_weight': round(new_weight, 4), 'reason': reason,
                            'discrimination': round(discrimination, 2),
                            'high_accuracy': round(high_accuracy, 2), 'low_accuracy': round(low_accuracy, 2),
                            'engine': 'ic_ir' if ic_stats else 'legacy'})

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
        
        # 规则分析（2026-10-20：原"先试 AI 再回退规则"的 AI 分支随豆包/ARK 一起下线，
        # 规则路径原样保留——顺带修掉旧结构里"没配 key 时 try 正常走完、except 不触发、
        # 误判分析整段为空"的隐性 bug：现在规则分析无条件执行）
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
