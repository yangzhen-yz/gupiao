"""数据库维护：定期清理历史缓存与快照，防止 SQLite 无限膨胀。

2026-09-18 新增。329MB 中 kline_cache(23749行×14KB≈330MB) 是主因。
策略：
  - kline_cache：只保留最近 30 天（趋势扫描用近20日，30天足够安全）
  - hot_search_daily_snapshot：只保留最近 60 天
  - 清理后 VACUUM 回收空间
"""
from datetime import datetime, timedelta
from db.database import get_db_conn


def cleanup_old_cache(days_kline: int = 30, days_hot: int = 60) -> dict:
    """清理过期缓存，返回 {表: 删除行数}"""
    result = {}
    conn = get_db_conn()
    cur = conn.cursor()

    cutoff_kline = (datetime.now() - timedelta(days=days_kline)).strftime('%Y-%m-%d')
    try:
        cur.execute("DELETE FROM kline_cache WHERE trade_date < ?", (cutoff_kline,))
        result['kline_cache'] = cur.rowcount
    except Exception as e:
        print(f'[DB维护] 清理 kline_cache 失败: {e}')
        result['kline_cache'] = 0

    cutoff_hot = (datetime.now() - timedelta(days=days_hot)).strftime('%Y-%m-%d')
    try:
        cur.execute("DELETE FROM hot_search_daily_snapshot WHERE snapshot_date < ?", (cutoff_hot,))
        result['hot_search_daily_snapshot'] = cur.rowcount
    except Exception as e:
        print(f'[DB维护] 清理 hot_search_daily_snapshot 失败: {e}')
        result['hot_search_daily_snapshot'] = 0

    conn.commit()

    # VACUUM 回收空间（需要独占，无活动事务时）
    try:
        conn.commit()
        conn.isolation_level = None  # autocommit，VACUUM 要求
        cur.execute("VACUUM")
        print('[DB维护] VACUUM 完成')
    except Exception as e:
        print(f'[DB维护] VACUUM 跳过: {e}')
    finally:
        conn.close()

    total = sum(result.values())
    print(f'[DB维护] 清理完成: {result}，共删除 {total} 行')
    return result


if __name__ == '__main__':
    cleanup_old_cache()
