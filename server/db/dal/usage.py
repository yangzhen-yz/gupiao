"""DAL domain: third-party API usage accounting."""

from ..dal_state import *

def save_api_usage_daily(rows) -> int:
    """批量 upsert 当日接口调用计数。rows = [(date, provider, interface, count, ok, fail), ...]"""
    if not rows:
        return 0
    try:
        # 2026-11-03 从 fast_fail 摘回 30 秒（昨天分类分错了）：这不是"可重拉的缓存"，
        # 而是接口调用量的**审计留痕**——它是"东财 fail 比率为什么暴涨""哪个调用方没接闸门"
        # 这些判断的唯一依据，丢一天就永久补不回来（计数是累加值，没有源头可重算）。
        conn = get_db_conn(wait_for_lock=True)
        cursor = conn.cursor()
        cursor.executemany(
            '''INSERT INTO api_usage_daily(date, provider, interface, count, ok, fail)
               VALUES(?, ?, ?, ?, ?, ?)
               ON CONFLICT(date, provider, interface) DO UPDATE SET
                 count=excluded.count, ok=excluded.ok, fail=excluded.fail''',
            rows,
        )
        conn.commit()
        conn.close()
        return len(rows)
    except Exception as error:
        print(f'保存接口用量失败: {str(error)}')
        return 0


def load_api_usage_today(date_str: str) -> List[Dict]:
    """读取某一天的接口用量计数（install 时回填内存，避免重启丢当天累计）。"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        rows = cursor.execute(
            'SELECT * FROM api_usage_daily WHERE date = ?', (date_str,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as error:
        print(f'读取当日接口用量失败: {str(error)}')
        return []


def load_api_usage_history(days: int = 14) -> List[Dict]:
    """读取近 N 天接口用量（按 date 升序），供趋势图。"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        rows = cursor.execute(
            '''SELECT * FROM api_usage_daily
               WHERE date >= date('now', ?)
               ORDER BY date ASC''',
            (f'-{days - 1} days',),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as error:
        print(f'读取接口用量历史失败: {str(error)}')
        return []


def save_api_usage_limit(provider: str, interface: str, limit: int, updated_by: str = '') -> bool:
    """保存（upsert）某接口的每日上限。"""
    try:
        conn = get_db_conn(wait_for_lock=True)
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO api_usage_limits(provider, interface, daily_limit, updated_by, updated_at)
               VALUES(?, ?, ?, ?, datetime('now', '+8 hours'))
               ON CONFLICT(provider, interface) DO UPDATE SET
                 daily_limit=excluded.daily_limit, updated_by=excluded.updated_by,
                 updated_at=datetime('now', '+8 hours')''',
            (provider, interface, int(limit), updated_by),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存接口上限失败: {str(error)}')
        return False


def load_api_usage_limits() -> List[Dict]:
    """读取所有已持久化的接口上限。"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        rows = cursor.execute('SELECT * FROM api_usage_limits').fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as error:
        print(f'读取接口上限失败: {str(error)}')
        return []
