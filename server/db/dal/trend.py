"""DAL domain: trend."""

from ..dal_state import *

def load_trend_scan_freshness() -> Dict:
    """趋势扫描的"数据到哪天了"（行动台用，不解析任何 JSON）。

    load_trend_scan_results 会把最近若干行的 ma5/ma10/details JSON 全部解开，
    行动台只需要日期与只数，故单开一条聚合查询。
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT scan_date, MAX(created_at) AS scan_at, "
            "       SUM(CASE WHEN symbol = '__SCAN_EMPTY__' THEN 0 ELSE 1 END) AS found, "
            "       MAX(total_scanned) AS total_scanned "
            "FROM trend_scan_results "
            "WHERE scan_date = (SELECT MAX(scan_date) FROM trend_scan_results)"
        )
        row = cursor.fetchone()
        conn.close()
        if not row or not row['scan_date']:
            return {'date': '', 'scanTime': '', 'found': 0, 'totalScanned': 0}
        return {
            'date': row['scan_date'],
            # 注意：表里的 scan_time 存的是"本次扫描耗时"（如 '446.7s'），
            # 数据时点要用 created_at，别拿 scan_time 当时间戳显示。
            'scanTime': str(row['scan_at'] or ''),
            'found': int(row['found'] or 0),
            'totalScanned': int(row['total_scanned'] or 0),
        }
    except Exception as error:
        print(f'加载趋势扫描新鲜度失败: {str(error)}')
        return {'date': '', 'scanTime': '', 'found': 0, 'totalScanned': 0}


def load_trend_scan_results() -> Dict:
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute(f'SELECT * FROM trend_scan_results ORDER BY scan_date DESC, score DESC LIMIT {QUERY_TREND_RESULTS_LIMIT}')
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            return {'date': '', 'stocks': []}

        first_row = rows[0]
        stocks = []
        for row in rows:
            if row['scan_date'] != first_row['scan_date']:
                continue
            # 跳过占位行（0只扫描结果的元数据行）
            if row['symbol'] == '__SCAN_EMPTY__':
                continue
            stocks.append({
                'symbol': row['symbol'],
                'name': row['name'],
                'score': row['score'],
                'details': json.loads(row['details_json']) if row['details_json'] else {},
                'latestPrice': float(row['latest_price']) if row['latest_price'] else 0,
                'ma5': json.loads(row['ma5_json']) if row['ma5_json'] else [],
                'ma10': json.loads(row['ma10_json']) if row['ma10_json'] else [],
                'ma20': json.loads(row['ma20_json']) if row['ma20_json'] else [],
                'recent5Days': json.loads(row['recent5_json']) if row['recent5_json'] else [],
            })

        return {
            'date': first_row['scan_date'],
            'totalScanned': first_row['total_scanned'] or 0,
            'found': len(stocks),
            'stocks': stocks,
            'scanTime': first_row['scan_time'] or '',
            'source': first_row['source'] or ''
        }
    except Exception as error:
        print(f'加载趋势股票失败: {str(error)}')
        return {'date': '', 'stocks': []}


def save_trend_scan_results(data: Dict) -> bool:
    conn = None
    try:
        scan_date = data.get('date', date.today().isoformat())
        stocks = data.get('stocks', [])
        total_scanned = data.get('totalScanned', 0)
        source = data.get('source', '')
        scan_time = data.get('scanTime', '')

        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM trend_scan_results WHERE scan_date = ?', (scan_date,))
        for stock in stocks:
            cursor.execute('''
                INSERT INTO trend_scan_results (scan_date, symbol, name, score, details_json, latest_price, ma5_json, ma10_json, ma20_json, recent5_json, total_scanned, source, scan_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                scan_date,
                stock.get('symbol', ''),
                stock.get('name', ''),
                stock.get('score', 0),
                json.dumps(stock.get('details', {}), ensure_ascii=False),
                stock.get('latestPrice', 0),
                json.dumps(stock.get('ma5', []), ensure_ascii=False),
                json.dumps(stock.get('ma10', []), ensure_ascii=False),
                json.dumps(stock.get('ma20', []), ensure_ascii=False),
                json.dumps(stock.get('recent5Days', []), ensure_ascii=False),
                total_scanned,
                source,
                scan_time
            ))
        # 当扫描结果为0只时，仍插入一条占位行保留扫描元数据（total_scanned/scan_time等）
        # 占位行 symbol='__SCAN_EMPTY__'，history-counts 查询会排除它不计入符合数
        if not stocks:
            cursor.execute('''
                INSERT INTO trend_scan_results (scan_date, symbol, name, score, details_json, latest_price, ma5_json, ma10_json, ma20_json, recent5_json, total_scanned, source, scan_time)
                VALUES (?, '__SCAN_EMPTY__', '', 0, '{}', 0, '[]', '[]', '[]', '[]', ?, ?, ?)
            ''', (scan_date, total_scanned, source, scan_time))
        conn.commit()
        return True
    except Exception as error:
        print(f'保存趋势股票失败: {str(error)}')
        return False
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


def save_trend_backtest_records(predict_date: str, results: List[Dict]) -> bool:
    """保存趋势回测预测记录（每次趋势扫描后调用）

    Args:
        predict_date: 预测日期（即趋势扫描日期）
        results: cleaned_results 列表，每项含 symbol/name/score/details
                 details 子字典内含 sectorFactor/chipFactor/pattern/patternFactor/marketRegime/totalFactor
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        for r in results:
            details = r.get('details', {}) or {}
            cursor.execute('''
                INSERT OR REPLACE INTO trend_backtest_records
                    (predict_date, symbol, name, score, predict_direction,
                     sector_factor, chip_factor, pattern, pattern_factor, market_regime, total_factor,
                     s_d1, s_d2, s_d3, s_d4)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                predict_date,
                r.get('symbol', ''),
                r.get('name', ''),
                int(r.get('score', 0) or 0),
                'bull',
                float(details.get('sectorFactor', 0) or 0),
                float(details.get('chipFactor', 0) or 0),
                str(details.get('pattern', '') or ''),
                float(details.get('patternFactor', 0) or 0),
                str(details.get('marketRegime', 'range') or 'range'),
                float(details.get('totalFactor', 0) or 0),
                details.get('sD1'),
                details.get('sD2'),
                details.get('sD3'),
                details.get('sD4'),
            ))
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'[趋势回测] 保存预测记录失败: {str(error)}')
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def load_trend_backtest_records(predict_date: str = None, symbol: str = None, limit: int = 100) -> List[Dict]:
    """加载趋势回测明细记录（支持按 predict_date / symbol 过滤）"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        sql = 'SELECT * FROM trend_backtest_records'
        conditions = []
        params = []
        if predict_date:
            conditions.append('predict_date = ?')
            params.append(predict_date)
        if symbol:
            conditions.append('symbol = ?')
            params.append(symbol)
        if conditions:
            sql += ' WHERE ' + ' AND '.join(conditions)
        sql += ' ORDER BY predict_date DESC, score DESC LIMIT ?'
        params.append(limit)
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as error:
        print(f'[趋势回测] 加载明细失败: {str(error)}')
        return []


def load_trend_backtest_records_pending_verification() -> List[Dict]:
    """加载所有未完全验证的记录（verified_1d=0 OR verified_3d=0 OR verified_5d=0）

    返回所有待补验的记录，按 predict_date 分组由调用方处理。
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM trend_backtest_records
            WHERE verified_1d = 0 OR verified_3d = 0 OR verified_5d = 0
            ORDER BY predict_date ASC, score DESC
        ''')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as error:
        print(f'[趋势回测] 加载待验证记录失败: {str(error)}')
        return []


def update_trend_backtest_verification(predict_date: str, symbol: str, period: str,
                                       actual_change: float, market_change: float,
                                       is_correct: bool) -> bool:
    """补验单条记录的指定周期

    Args:
        period: '1d' / '3d' / '5d'
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        from datetime import datetime
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        sql = f'''UPDATE trend_backtest_records
                  SET verified_{period} = 1,
                      actual_change_{period} = ?,
                      market_change_{period} = ?,
                      is_correct_{period} = ?,
                      verified_at_{period} = ?
                  WHERE predict_date = ? AND symbol = ?'''
        cursor.execute(sql, (
            float(actual_change), float(market_change),
            1 if is_correct else 0, now,
            predict_date, symbol
        ))
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'[趋势回测] 更新验证记录失败: {str(error)}')
        return False


def upsert_trend_backtest_report(predict_date: str, total_count: int,
                                 period_stats: Dict, score_segment_stats: Dict) -> bool:
    """UPSERT 趋势回测聚合报告

    Args:
        predict_date: 预测日期
        total_count: 该日入选趋势股总数
        period_stats: {'1d': {'verified': n, 'win': n, 'win_rate': r}, '3d': {...}, '5d': {...}}
        score_segment_stats: {'60_70': {'total': n, 'win_1d': n, 'win_3d': n, 'win_5d': n},
                              '70_80': {...}, '80_plus': {...}}
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        from datetime import datetime
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        p1 = period_stats.get('1d', {})
        p3 = period_stats.get('3d', {})
        p5 = period_stats.get('5d', {})
        s60 = score_segment_stats.get('60_70', {})
        s70 = score_segment_stats.get('70_80', {})
        s80 = score_segment_stats.get('80_plus', {})
        cursor.execute('''
            INSERT INTO trend_backtest_reports (
                predict_date, total_count,
                verified_1d_count, win_1d_count, win_rate_1d,
                verified_3d_count, win_3d_count, win_rate_3d,
                verified_5d_count, win_5d_count, win_rate_5d,
                score_60_70_total, score_60_70_win_1d, score_60_70_win_3d, score_60_70_win_5d,
                score_70_80_total, score_70_80_win_1d, score_70_80_win_3d, score_70_80_win_5d,
                score_80_plus_total, score_80_plus_win_1d, score_80_plus_win_3d, score_80_plus_win_5d,
                updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(predict_date) DO UPDATE SET
                total_count = excluded.total_count,
                verified_1d_count = excluded.verified_1d_count, win_1d_count = excluded.win_1d_count, win_rate_1d = excluded.win_rate_1d,
                verified_3d_count = excluded.verified_3d_count, win_3d_count = excluded.win_3d_count, win_rate_3d = excluded.win_rate_3d,
                verified_5d_count = excluded.verified_5d_count, win_5d_count = excluded.win_5d_count, win_rate_5d = excluded.win_rate_5d,
                score_60_70_total = excluded.score_60_70_total, score_60_70_win_1d = excluded.score_60_70_win_1d, score_60_70_win_3d = excluded.score_60_70_win_3d, score_60_70_win_5d = excluded.score_60_70_win_5d,
                score_70_80_total = excluded.score_70_80_total, score_70_80_win_1d = excluded.score_70_80_win_1d, score_70_80_win_3d = excluded.score_70_80_win_3d, score_70_80_win_5d = excluded.score_70_80_win_5d,
                score_80_plus_total = excluded.score_80_plus_total, score_80_plus_win_1d = excluded.score_80_plus_win_1d, score_80_plus_win_3d = excluded.score_80_plus_win_3d, score_80_plus_win_5d = excluded.score_80_plus_win_5d,
                updated_at = excluded.updated_at
        ''', (
            predict_date, total_count,
            p1.get('verified', 0), p1.get('win', 0), p1.get('win_rate', 0),
            p3.get('verified', 0), p3.get('win', 0), p3.get('win_rate', 0),
            p5.get('verified', 0), p5.get('win', 0), p5.get('win_rate', 0),
            s60.get('total', 0), s60.get('win_1d', 0), s60.get('win_3d', 0), s60.get('win_5d', 0),
            s70.get('total', 0), s70.get('win_1d', 0), s70.get('win_3d', 0), s70.get('win_5d', 0),
            s80.get('total', 0), s80.get('win_1d', 0), s80.get('win_3d', 0), s80.get('win_5d', 0),
            now,
        ))
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'[趋势回测] 保存报告失败: {str(error)}')
        return False


def load_trend_backtest_reports(limit: int = 30) -> List[Dict]:
    """加载历史回测报告列表（按 predict_date DESC）"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM trend_backtest_reports ORDER BY predict_date DESC LIMIT ?',
            (limit,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as error:
        print(f'[趋势回测] 加载报告列表失败: {str(error)}')
        return []


def load_latest_trend_backtest_report() -> Dict:
    """加载最新一条回测报告（前端徽章用）"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM trend_backtest_reports ORDER BY predict_date DESC LIMIT 1')
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else {}
    except Exception as error:
        print(f'[趋势回测] 加载最新报告失败: {str(error)}')
        return {}


def load_trend_dimension_weights() -> Dict:
    """加载 D1-D4 趋势维度权重（无数据时返回 1.0 默认值）"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        _ensure_optimizer_v2_columns(cursor)
        conn.commit()
        cursor.execute('SELECT * FROM trend_dimension_weights')
        rows = cursor.fetchall()
        conn.close()
        result = {
            'd1': {'weight': 1.0, 'accuracy': 50.0, 'discrimination': 0, 'sample_count': 0, 'correct_count': 0, 'valid_neg_streak': 0},
            'd2': {'weight': 1.0, 'accuracy': 50.0, 'discrimination': 0, 'sample_count': 0, 'correct_count': 0, 'valid_neg_streak': 0},
            'd3': {'weight': 1.0, 'accuracy': 50.0, 'discrimination': 0, 'sample_count': 0, 'correct_count': 0, 'valid_neg_streak': 0},
            'd4': {'weight': 1.0, 'accuracy': 50.0, 'discrimination': 0, 'sample_count': 0, 'correct_count': 0, 'valid_neg_streak': 0},
        }
        for row in rows:
            d = row['dimension'].lower()
            if d in result:
                raw = dict(row)
                result[d] = {
                    'weight': float(row['weight']),
                    'accuracy': float(row['accuracy']),
                    'discrimination': float(raw.get('discrimination') or 0),
                    'sample_count': int(row['sample_count']),
                    'correct_count': int(row['correct_count']),
                    'valid_neg_streak': int(raw.get('valid_neg_streak') or 0),
                }
        return result
    except Exception as error:
        print(f'加载趋势维度权重失败: {str(error)}')
        return {'d1': {'weight': 1.0}, 'd2': {'weight': 1.0}, 'd3': {'weight': 1.0}, 'd4': {'weight': 1.0}}


def save_trend_dimension_weights(weights_data: Dict) -> bool:
    """保存 D1-D4 趋势维度权重"""
    try:
        conn = get_db_conn(wait_for_lock=True)
        cursor = conn.cursor()
        _ensure_optimizer_v2_columns(cursor)
        for dim_key, data in weights_data.items():
            weight = data.get('weight', 1.0)
            accuracy = data.get('accuracy', 0)
            discrimination = data.get('discrimination', 0)
            sample_count = data.get('sample_count', 0)
            correct_count = data.get('correct_count', 0)
            valid_neg_streak = int(data.get('valid_neg_streak', 0) or 0)
            cursor.execute('''
                INSERT OR REPLACE INTO trend_dimension_weights
                    (dimension, weight, accuracy, discrimination, sample_count, correct_count,
                     valid_neg_streak, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now', '+8 hours'))
            ''', (dim_key, weight, accuracy, discrimination, sample_count, correct_count, valid_neg_streak))
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存趋势维度权重失败: {str(error)}')
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass

