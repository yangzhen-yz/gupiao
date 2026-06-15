"""MySQL -> SQLite 数据迁移脚本"""
import pymysql
import sqlite3
import json
from decimal import Decimal

MYSQL_CONFIG = {
    'host': 'localhost',
    'port': 3306,
    'user': 'root',
    'password': '123456',
    'database': 'gupiao',
    'charset': 'utf8mb4',
}

SQLITE_PATH = 'gupiao.db'

# 表名列表（按依赖顺序）
TABLES = [
    'daily_market_reviews',
    'trend_scan_results',
    'hot_stock_buttons',
    'stock_alias_map',
    'strategy_predict_records',
    'strategy_factor_weights',
    'strategy_backtest_reports',
    'hot_search_ranking',
    'hot_search_meta',
    'hot_search_daily_snapshot',
    'daily_recommendations',
    'stock_basic_info',
    'user_scan_pool',
    'stock_concept_tags',
]


def get_mysql_columns(cursor, table):
    """获取 MySQL 表的列名列表"""
    cursor.execute(f"SHOW COLUMNS FROM `{table}`")
    return [row[0] for row in cursor.fetchall()]


def get_sqlite_columns(cursor, table):
    """获取 SQLite 表的列名列表"""
    cursor.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cursor.fetchall()]


def migrate():
    mysql_conn = pymysql.connect(**MYSQL_CONFIG)
    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_conn.execute("PRAGMA journal_mode=WAL")

    mysql_cur = mysql_conn.cursor()
    sqlite_cur = sqlite_conn.cursor()

    for table in TABLES:
        # 获取两边的列名
        mysql_cols = get_mysql_columns(mysql_cur, table)
        sqlite_cols = get_sqlite_columns(sqlite_cur, table)

        # 取交集（只迁移两边都有的列）
        common_cols = [c for c in mysql_cols if c in sqlite_cols]
        if not common_cols:
            print(f"  [SKIP] {table}: 无公共列")
            continue

        # 检查 MySQL 数据量
        mysql_cur.execute(f"SELECT COUNT(*) FROM `{table}`")
        mysql_count = mysql_cur.fetchone()[0]
        if mysql_count == 0:
            print(f"  [SKIP] {table}: MySQL 无数据")
            continue

        # 检查 SQLite 已有数据量
        sqlite_cur.execute(f"SELECT COUNT(*) FROM {table}")
        sqlite_count = sqlite_cur.fetchone()[0]

        # 清空 SQLite 表再导入
        sqlite_cur.execute(f"DELETE FROM {table}")
        print(f"  {table}: MySQL {mysql_count} rows -> SQLite (清空原 {sqlite_count} rows)")

        # 分批读取 MySQL 数据
        cols_str = ', '.join(f'`{c}`' for c in common_cols)
        mysql_cur.execute(f"SELECT {cols_str} FROM `{table}`")

        placeholders = ', '.join(['?'] * len(common_cols))
        insert_sql = f"INSERT INTO {table} ({', '.join(common_cols)}) VALUES ({placeholders})"

        batch_size = 500
        batch = []
        total = 0
        while True:
            rows = mysql_cur.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                # 处理 datetime/date 对象转字符串
                converted = []
                for val in row:
                    if hasattr(val, 'isoformat'):
                        converted.append(val.isoformat())
                    elif isinstance(val, Decimal):
                        converted.append(float(val))
                    elif isinstance(val, bytes):
                        converted.append(val.decode('utf-8', errors='replace'))
                    else:
                        converted.append(val)
                batch.append(tuple(converted))
            sqlite_cur.executemany(insert_sql, batch)
            total += len(batch)
            batch = []

        sqlite_conn.commit()
        print(f"  {table}: 迁移完成 {total} rows")

    mysql_conn.close()
    sqlite_conn.close()
    print("\n迁移完成！")


if __name__ == '__main__':
    migrate()
