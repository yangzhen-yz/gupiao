"""DAL domain: stock."""

from ..dal_state import *

def load_stock_basic_info() -> Dict:
    _ck = 'stock_basic_info'
    _hit = _db_cache_get(_ck)
    if _hit is not None:
        return _hit
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT symbol, name FROM stock_basic_info')
        rows = cursor.fetchall()
        conn.close()
        _result = {row['symbol']: row['name'] for row in rows}
        _db_cache_set(_ck, _result, 60.0)
        return _result
    except Exception as error:
        print(f'加载股票映射失败: {str(error)}')
        return {}


def save_stock_basic_info(stock_basic_info: Dict) -> bool:
    try:
        conn = get_db_conn(fast_fail=True)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM stock_basic_info')
        for symbol, name in stock_basic_info.items():
            cursor.execute(
                'INSERT INTO stock_basic_info (symbol, name) VALUES (?, ?)',
                (symbol, name)
            )
        conn.commit()
        conn.close()
        _db_cache_invalidate('stock_basic_info')
        return True
    except Exception as error:
        print(f'保存股票映射失败: {str(error)}')
        return False


def save_stock_industry(industry_map: Dict[str, str]) -> bool:
    """批量更新 symbol → industry（增量更新，不删除已有名称）（2026-06-30）"""
    if not industry_map:
        return False
    try:
        conn = get_db_conn(fast_fail=True)
        cursor = conn.cursor()
        for symbol, industry in industry_map.items():
            if not industry:
                continue
            cursor.execute(
                "UPDATE stock_basic_info SET industry = ?, updated_at = datetime('now', '+8 hours') WHERE symbol = ?",
                (industry, symbol.lower())
            )
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存股票行业映射失败: {str(error)}')
        return False


def add_stock_basic_info(symbol: str, name: str) -> bool:
    """增量写入单只股票基础信息（远程搜索回写用，2026-10-12）

    与 save_stock_basic_info（全表替换）区分：避免回写时重写整表。
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('''INSERT INTO stock_basic_info (symbol, name) VALUES (?, ?)
            ON CONFLICT(symbol) DO UPDATE SET name=excluded.name''', (symbol.lower(), name))
        conn.commit()
        conn.close()
        _db_cache_invalidate('stock_basic_info')
        return True
    except Exception as error:
        print(f'增量写入股票信息失败: {str(error)}')
        return False


def load_stock_alias_map() -> Dict:
    """加载名称搜索映射（名称 -> 代码）"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT name, symbol FROM stock_alias_map')
        rows = cursor.fetchall()
        conn.close()
        return {row['name']: row['symbol'] for row in rows}
    except Exception as error:
        print(f'加载名称搜索映射失败: {str(error)}')
        return {}

