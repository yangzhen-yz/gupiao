"""strategy.py - split from main.py"""
from typing import List, Dict, Optional
from datetime import date, timedelta
from app.config import STRATEGY_BULL_THRESHOLD, STRATEGY_BEAR_THRESHOLD, STRATEGY_PREDICT_BULL_MIN_SCORE, STRATEGY_BACKTEST_PREDICTION_LIMIT, STRATEGY_BACKTEST_HISTORY_DAYS, STRATEGY_SCORE_THRESHOLDS, STRATEGY_SCORE_VALUES, STRATEGY_WEBI_VERIFY, STRATEGY_PE_FILTER, STRATEGY_LABEL_THRESHOLDS, DEFAULT_WEIGHTS, MARKET_CRASH_THRESHOLD, MARKET_SEVERE_CRASH_THRESHOLD
from db.database import get_db_conn, load_strategy_factor_weights, save_strategy_factor_weights, save_predictions
from services.stock import fetch_stock_data
from services.ai import load_ai_config, DEEPSEEK_API_KEY, _call_deepseek_api

def calc_stock_score_v2(data: Dict, weights: Dict = None, market_change: float = 0.0,
                        trend_result: Dict = None) -> Dict:
    """使用动态权重的评分函数 V2 (V4.1 融合趋势评分)
    
    Args:
        data: 股票特征数据（实时行情/日内因子）
        weights: 策略因子权重
        market_change: 大盘涨跌幅，用于根据市场环境动态调整评分阈值
        trend_result: 趋势评分结果（来自 is_up_trend），包含 score(0-100) 等字段。
                      提供后，最终评分 = 50% 日内因子 + 50% 趋势因子
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

    # 基础得分（与原逻辑一致）
    base_scores = {
        'outer_ratio': STRATEGY_SCORE_VALUES['outer_ratio']['high'] if outer_ratio >= STRATEGY_SCORE_THRESHOLDS['outer_ratio']['high'] else STRATEGY_SCORE_VALUES['outer_ratio']['mid'] if outer_ratio >= STRATEGY_SCORE_THRESHOLDS['outer_ratio']['mid'] else STRATEGY_SCORE_VALUES['outer_ratio']['low'],
        'volume_ratio': STRATEGY_SCORE_VALUES['volume_ratio']['high'] if volume_ratio >= STRATEGY_SCORE_THRESHOLDS['volume_ratio']['high'] else STRATEGY_SCORE_VALUES['volume_ratio']['mid'] if volume_ratio >= STRATEGY_SCORE_THRESHOLDS['volume_ratio']['mid'] else STRATEGY_SCORE_VALUES['volume_ratio']['normal'] if volume_ratio >= STRATEGY_SCORE_THRESHOLDS['volume_ratio']['low'] else STRATEGY_SCORE_VALUES['volume_ratio']['low'],
        'turnover_rate': STRATEGY_SCORE_VALUES['turnover_rate']['high'] if (STRATEGY_SCORE_THRESHOLDS['turnover_rate']['high'][0] <= turnover_rate <= STRATEGY_SCORE_THRESHOLDS['turnover_rate']['high'][1]) else STRATEGY_SCORE_VALUES['turnover_rate']['mid'] if (STRATEGY_SCORE_THRESHOLDS['turnover_rate']['mid'][0] <= turnover_rate < STRATEGY_SCORE_THRESHOLDS['turnover_rate']['mid'][1]) else STRATEGY_SCORE_VALUES['turnover_rate']['normal'] if turnover_rate > STRATEGY_SCORE_THRESHOLDS['turnover_rate']['high_penalty'] else STRATEGY_SCORE_VALUES['turnover_rate']['low'],
        'change_percent': STRATEGY_SCORE_VALUES['change_percent']['high'] if change_percent > STRATEGY_SCORE_THRESHOLDS['change_percent']['high'] else STRATEGY_SCORE_VALUES['change_percent']['mid'] if change_percent > STRATEGY_SCORE_THRESHOLDS['change_percent']['mid'] else STRATEGY_SCORE_VALUES['change_percent']['normal'] if change_percent > STRATEGY_SCORE_THRESHOLDS['change_percent']['low'] else STRATEGY_SCORE_VALUES['change_percent']['negative'] if change_percent > STRATEGY_SCORE_THRESHOLDS['change_percent']['negative'] else 0,
        'weibi': STRATEGY_SCORE_VALUES['weibi']['high'] if weibi > STRATEGY_SCORE_THRESHOLDS['weibi']['high'] else STRATEGY_SCORE_VALUES['weibi']['mid'] if weibi > STRATEGY_SCORE_THRESHOLDS['weibi']['mid'] else STRATEGY_SCORE_VALUES['weibi']['low'] if weibi > STRATEGY_SCORE_THRESHOLDS['weibi']['low'] else STRATEGY_SCORE_VALUES['weibi']['negative'],
        'avg_price_deviation': STRATEGY_SCORE_VALUES['avg_price_deviation']['high'] if avg_price_deviation > STRATEGY_SCORE_THRESHOLDS['avg_price_deviation']['high'] else STRATEGY_SCORE_VALUES['avg_price_deviation']['mid'] if avg_price_deviation > STRATEGY_SCORE_THRESHOLDS['avg_price_deviation']['mid'] else STRATEGY_SCORE_VALUES['avg_price_deviation']['normal'] if avg_price_deviation > STRATEGY_SCORE_THRESHOLDS['avg_price_deviation']['low'] else STRATEGY_SCORE_VALUES['avg_price_deviation']['low'],
        'amplitude': STRATEGY_SCORE_VALUES['amplitude']['high'] if (STRATEGY_SCORE_THRESHOLDS['amplitude']['high'][0] < amplitude <= STRATEGY_SCORE_THRESHOLDS['amplitude']['high'][1]) else STRATEGY_SCORE_VALUES['amplitude']['mid'] if (STRATEGY_SCORE_THRESHOLDS['amplitude']['mid'][0] < amplitude <= STRATEGY_SCORE_THRESHOLDS['amplitude']['mid'][1]) else STRATEGY_SCORE_VALUES['amplitude']['low'],
    }
    if weibi > STRATEGY_WEBI_VERIFY['weibi_high'] and change_percent < 0:
        base_scores['weibi'] = 3
    elif weibi > STRATEGY_WEBI_VERIFY['weibi_high'] and change_percent < STRATEGY_SCORE_THRESHOLDS['change_percent']['negative']:
        base_scores['weibi'] = 2
    elif weibi > STRATEGY_WEBI_VERIFY['weibi_high'] and outer_ratio < STRATEGY_WEBI_VERIFY['outer_ratio_low']:
        base_scores['weibi'] = 4
    elif weibi > STRATEGY_WEBI_VERIFY['weibi_high'] and volume_ratio < STRATEGY_WEBI_VERIFY['volume_ratio_low']:
        base_scores['weibi'] = 4
        
    # AI回测优化建议规则1: 结合分时图量价关系，当外盘占比低于50%且量比>3时，降低看涨权重 (惩罚分数)
    if outer_ratio < 50 and volume_ratio > 3:
        # 如果是这种典型的诱多出货形态，直接扣除部分外盘和量比的分数
        base_scores['outer_ratio'] = max(0, base_scores['outer_ratio'] - 5)
        base_scores['volume_ratio'] = max(0, base_scores['volume_ratio'] - 5)

    # 2026-06-11 策略优化建议：增加放量出货检测（骗线过滤）
    # 当量比显著（>1.5）且外盘占比并未显著超过内盘（<55%）时，可能存在主力利用对倒放量吸引关注后悄悄出货。
    if volume_ratio > 1.5 and outer_ratio < 55:
        base_scores['volume_ratio'] = max(0, base_scores['volume_ratio'] - 5)
        base_scores['outer_ratio'] = max(0, base_scores['outer_ratio'] - 5)
        
    # 2026-06-11 策略优化建议：不再盲目惩罚涨幅，而是区分“强势启动”与“力竭赶顶”
    # 2026-06-11 策略优化建议：拥抱强势龙头股。涨停不代表风险，往往代表次日的高溢价。
    if change_percent > 9.5:
        # 涨停股评分逻辑：如果委比极高且换手率适中，说明封单坚决，给予高分
        if weibi > 80 and 2 <= turnover_rate <= 15:
            base_scores['change_percent'] = 25 # 龙头股奖励分，鼓励次日竞价关注
        else:
            base_scores['change_percent'] = 15 # 普通封板
    elif change_percent > 7.0:
        # 高位强势，如果不放量滞涨，给予高分奖励
        if volume_ratio < 2.5:
            base_scores['change_percent'] = 20
        else:
            base_scores['change_percent'] = 12
    elif change_percent > 3.0:
        # 强势区间：量价齐升则给满分
        if 1.0 <= volume_ratio <= 2.2 and outer_ratio > 56:
            base_scores['change_percent'] = 20
        else:
            base_scores['change_percent'] = 15
    elif change_percent > 0:
        # 稳健启动区
        base_scores['change_percent'] = 18
    else:
        # 下跌股保持原逻辑
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

    # 归一化到100分（8个因子满分总和为95，乘以平均权重后需要归一化）
    max_possible = sum([
        15, 10, 10, 20, 10, 15, 5, 15  # 各因子满分
    ]) * sum(weights.get(f, {}).get('weight', 1.0) for f in base_scores) / len(base_scores)
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
        }
    }
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

        # 逐个验证
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
                try:
                    stock_data = await fetch_stock_data(symbol)
                    if not stock_data:
                        continue
                    actual_change = stock_data.get('parsed', {}).get('changePercent', 0)
                except Exception:
                    continue

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
