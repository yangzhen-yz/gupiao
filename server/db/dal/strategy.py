"""DAL domain: strategy."""

from ..dal_state import *

def load_strategy_factor_weights() -> Dict:
    """加载动态权重，如果数据库无数据则返回默认权重"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT factor_name, weight, accuracy, sample_count, correct_count FROM strategy_factor_weights')
        rows = cursor.fetchall()
        conn.close()
        if not rows:
            return {k: {'weight': v, 'accuracy': 50.0, 'sample_count': 0, 'correct_count': 0}
                    for k, v in DEFAULT_WEIGHTS.items()}
        result = {}
        for row in rows:
            factor_name = row['factor_name']
            # 过滤掉数据库中的孤儿因子（不再使用的旧 key），避免前端显示英文 key
            if factor_name not in DEFAULT_WEIGHTS:
                continue
            result[factor_name] = {
                'weight': float(row['weight']),
                'accuracy': float(row['accuracy']),
                'sample_count': int(row['sample_count']),
                'correct_count': int(row['correct_count']),
            }
        # 补充数据库中可能缺少的因子
        for k, v in DEFAULT_WEIGHTS.items():
            if k not in result:
                result[k] = {'weight': v, 'accuracy': 50.0, 'sample_count': 0, 'correct_count': 0}
        return result
    except Exception as error:
        print(f'加载策略权重失败: {str(error)}')
        return {k: {'weight': v, 'accuracy': 50.0, 'sample_count': 0, 'correct_count': 0}
                for k, v in DEFAULT_WEIGHTS.items()}


def save_strategy_factor_weights(weights_data: Dict) -> bool:
    """保存动态权重"""
    try:
        conn = get_db_conn(wait_for_lock=True)
        cursor = conn.cursor()
        for factor_name, data in weights_data.items():
            # 跳过非法因子 key（防止前端传入未识别的英文 key 写入数据库）
            if factor_name not in DEFAULT_WEIGHTS:
                continue
            weight = data.get('weight', 1.0)
            accuracy = data.get('accuracy', 50.0)
            sample_count = data.get('sample_count', 0)
            correct_count = data.get('correct_count', 0)
            cursor.execute(
                '''INSERT OR REPLACE INTO strategy_factor_weights (factor_name, weight, accuracy, sample_count, correct_count)
                   VALUES (?, ?, ?, ?, ?)''',
                (factor_name, weight, accuracy, sample_count, correct_count)
            )
        # 清理数据库中已不存在的孤儿因子（防止历史脏数据残留）
        valid_keys = tuple(DEFAULT_WEIGHTS.keys())
        if valid_keys:
            placeholders = ','.join(['?'] * len(valid_keys))
            cursor.execute(
                f'DELETE FROM strategy_factor_weights WHERE factor_name NOT IN ({placeholders})',
                valid_keys
            )
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存策略权重失败: {str(error)}')
        return False


def save_predictions(predictions: List[Dict]) -> bool:
    """保存扫描预测记录"""
    try:
        conn = get_db_conn(wait_for_lock=True)
        cursor = conn.cursor()
        for p in predictions:
            cursor.execute(
                '''INSERT OR REPLACE INTO strategy_predict_records
                   (predict_date, symbol, name, score, label, predict_direction,
                    outer_ratio, volume_ratio, turnover_rate, change_percent,
                    weibi, avg_price_deviation, amplitude, market_change,
                    position, main_capital_inflow, holder_structure)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (p['predict_date'], p['symbol'], p['name'], p['score'], p['label'],
                 p['predict_direction'], p.get('outer_ratio', 0), p.get('volume_ratio', 0),
                 p.get('turnover_rate', 0), p.get('change_percent', 0),
                 p.get('weibi', 0), p.get('avg_price_deviation', 0),
                 p.get('amplitude', 0), p.get('market_change', 0),
                 p.get('position', 0), p.get('main_capital_inflow', 0),
                 p.get('holder_structure', 0))
            )
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存预测记录失败: {str(error)}')
        return False

