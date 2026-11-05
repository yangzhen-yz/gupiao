"""DAL domain: fundamental."""

from ..dal_state import *

def save_holder_num_batch(records: List[Dict]) -> bool:
    """批量保存股东人数数据（UPSERT 语义，2026-07-06）

    Args:
        records: [{'symbol', 'end_date', 'holder_total_num', 'num_change',
                   'total_num_ratio', 'avg_free_shares', 'avg_hold_amt',
                   'notice_date', 'updated_at'}]
    """
    try:
        conn = get_db_conn(fast_fail=True)
        cursor = conn.cursor()
        for r in records:
            cursor.execute('''INSERT OR REPLACE INTO stock_holder_num
                (symbol, end_date, holder_total_num, num_change, total_num_ratio,
                 avg_free_shares, avg_hold_amt, notice_date, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (r.get('symbol', ''), r.get('end_date', ''),
                 r.get('holder_total_num'), r.get('num_change'),
                 r.get('total_num_ratio'), r.get('avg_free_shares'),
                 r.get('avg_hold_amt'), r.get('notice_date'),
                 r.get('updated_at', '')))
        conn.commit()
        conn.close()
        return True
    except Exception as _e:
        print(f'[数据库] 保存股东人数数据失败: {_e}')
        return False


def load_holder_num_batch(symbols: List[str]) -> Dict[str, Dict]:
    """批量查询每只股票最新一期股东人数（2026-07-06）

    Args:
        symbols: 股票代码列表（小写带前缀 sh600519）
    Returns:
        {symbol: {holder_total_num, num_change, total_num_ratio,
                  avg_free_shares, avg_hold_amt, end_date}}
    """
    if not symbols:
        return {}
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        placeholders = ','.join('?' * len(symbols))
        # 子查询取每只股票最新一期 end_date
        cursor.execute(f'''SELECT symbol, end_date, holder_total_num, num_change,
            total_num_ratio, avg_free_shares, avg_hold_amt
            FROM stock_holder_num
            WHERE symbol IN ({placeholders})
            AND end_date = (
                SELECT MAX(end_date) FROM stock_holder_num h2
                WHERE h2.symbol = stock_holder_num.symbol)''', symbols)
        rows = cursor.fetchall()
        conn.close()
        result = {}
        for row in rows:
            result[row['symbol']] = {
                'holder_total_num': row['holder_total_num'],
                'num_change': row['num_change'],
                'total_num_ratio': row['total_num_ratio'],
                'avg_free_shares': row['avg_free_shares'],
                'avg_hold_amt': row['avg_hold_amt'],
                'end_date': row['end_date'],
            }
        return result
    except Exception as _e:
        print(f'[数据库] 查询股东人数数据失败: {_e}')
        return {}


def save_fundamental_batch(records: List[Dict]) -> bool:
    """批量保存基本面质量数据（UPSERT 语义，2026-07-16 P1a）

    Args:
        records: [{'symbol', 'report_date', 'roe', 'net_profit_yoy', 'eps', 'updated_at'}]
    """
    try:
        conn = get_db_conn(fast_fail=True)
        cursor = conn.cursor()
        for r in records:
            cursor.execute('''INSERT OR REPLACE INTO stock_fundamental
                (symbol, report_date, roe, net_profit_yoy, eps, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)''',
                (r.get('symbol', ''), r.get('report_date', ''),
                 r.get('roe'), r.get('net_profit_yoy'), r.get('eps'),
                 r.get('updated_at', '')))
        conn.commit()
        conn.close()
        return True
    except Exception as _e:
        print(f'[数据库] 保存基本面数据失败: {_e}')
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def load_fundamental_batch(symbols: List[str]) -> Dict[str, Dict]:
    """批量查询每只股票最新基本面质量数据（2026-07-16 P1a）

    Args:
        symbols: 股票代码列表（小写带前缀 sh600519）
    Returns:
        {symbol: {report_date, roe, net_profit_yoy, eps}}
    """
    if not symbols:
        return {}
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        placeholders = ','.join('?' * len(symbols))
        cursor.execute(f'''SELECT symbol, report_date, roe, net_profit_yoy, eps
            FROM stock_fundamental WHERE symbol IN ({placeholders})''', symbols)
        rows = cursor.fetchall()
        conn.close()
        result = {}
        for row in rows:
            result[row['symbol']] = {
                'report_date': row['report_date'],
                'roe': row['roe'],
                'net_profit_yoy': row['net_profit_yoy'],
                'eps': row['eps'],
            }
        return result
    except Exception as _e:
        print(f'[数据库] 查询基本面数据失败: {_e}')
        return {}


def save_valuation_batch(records: List[Dict]) -> int:
    """批量保存估值分位数据（UPSERT 语义，2026-07-16 P2c）

    Args:
        records: [{'symbol', 'pe', 'pb', 'pe_pct', 'pb_pct', 'updated_at'}]
    Returns:
        成功保存的条数
    """
    if not records:
        return 0
    try:
        conn = get_db_conn(fast_fail=True)
        cursor = conn.cursor()
        for r in records:
            cursor.execute('''INSERT OR REPLACE INTO stock_valuation
                (symbol, pe, pb, pe_pct, pb_pct, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)''',
                (r.get('symbol', ''), r.get('pe'), r.get('pb'),
                 r.get('pe_pct'), r.get('pb_pct'), r.get('updated_at', '')))
        conn.commit()
        n = len(records)
        conn.close()
        return n
    except Exception as _e:
        print(f'[数据库] 保存估值数据失败: {_e}')
        return 0


def load_valuation_batch(symbols: List[str]) -> List[Dict]:
    """批量查询每只股票估值分位（2026-07-16 P2c）

    Args:
        symbols: 股票代码列表（小写带前缀 sh600519）
    Returns:
        [{'symbol', 'pe', 'pb', 'pe_pct', 'pb_pct'}]
    """
    if not symbols:
        return []
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        placeholders = ','.join('?' * len(symbols))
        cursor.execute(f'''SELECT symbol, pe, pb, pe_pct, pb_pct
            FROM stock_valuation WHERE symbol IN ({placeholders})''', symbols)
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as _e:
        print(f'[数据库] 查询估值数据失败: {_e}')
        return []

