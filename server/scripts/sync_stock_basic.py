"""sync_stock_basic.py - 全市场股票名录（代码→名称、上市/退市状态）落到本地库

为什么需要：要把"只做沪深主板、剔除 ST/*ST"变成可执行的规则，就得有一份**离线可查的名称表**。
之前 `history_kline.db` 里的 stock_basic 表是空的（只有建表语句，从没填过），
名字只能靠运行时腾讯行情逐只捞 —— 于是"这只票现在是不是 ST"这种判断没有数据支撑，
而 ST 的涨跌停是 5%，混在主板的 10% 口径里连涨停判定都会算错。

数据源：baostock `query_stock_basic()`（免费、无需 token、一次返回全市场含退市）。
字段：type='1' 是股票，status='1' 是在市（'0' 已退市）。

用法：
  python scripts/sync_stock_basic.py            # 拉一次并入库（幂等，重复执行覆盖）
  python scripts/sync_stock_basic.py --stats    # 只看当前库里的统计
"""
import argparse
import datetime
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import KLINE_HISTORY_DB_PATH                    # noqa: E402
from services.data.kline_history import _to_baostock_code, _to_pure_code  # noqa: E402

TABLE = 'stock_basic_full'

MAIN_BOARD_PREFIX = ('600', '601', '603', '605',       # 沪市主板
                     '000', '001', '002', '003')       # 深市主板（002/003 由原中小板并入）


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(f'CREATE TABLE IF NOT EXISTS {TABLE} ('
                 '  symbol TEXT PRIMARY KEY, name TEXT, ipo_date TEXT, out_date TEXT,'
                 '  type TEXT, status TEXT, fetched_at TEXT)')
    # 引擎自己那张 stock_basic(symbol,name) 也一并填上，别再留一张空表
    conn.execute('CREATE TABLE IF NOT EXISTS stock_basic (symbol TEXT PRIMARY KEY, name TEXT)')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=10000')


def is_st(name: str) -> bool:
    """ST / *ST / 退市整理期（名称带"退"）都算进去 —— 一律排除。"""
    n = (name or '').upper()
    return ('ST' in n) or ('退' in n)


def is_main_board(symbol: str) -> bool:
    """沪深主板：600/601/603/605、000/001/002/003。
    创业板(300/301)、科创板(688/689)、北交所(4xx/8xx/920) 都不在里面。"""
    return symbol.startswith(MAIN_BOARD_PREFIX)


def sync() -> int:
    import baostock as bs
    bs.login()
    try:
        rs = bs.query_stock_basic()
        if rs.error_code != '0':
            print(f'[名录] baostock 返回错误: {rs.error_msg}')
            return -1
        rows = []
        while rs.next():
            r = rs.get_row_data()          # code, code_name, ipoDate, outDate, type, status
            if len(r) < 6:
                continue
            rows.append((_to_pure_code(r[0]), r[1], r[2], r[3], r[4], r[5]))
    finally:
        bs.logout()
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=60)
    try:
        ensure_schema(conn)
        conn.executemany(f'INSERT OR REPLACE INTO {TABLE} VALUES (?,?,?,?,?,?,?)',
                         [r + (now,) for r in rows])
        conn.executemany('INSERT OR REPLACE INTO stock_basic(symbol,name) VALUES (?,?)',
                         [(r[0], r[1]) for r in rows if r[4] == '1' and r[5] == '1'])
        conn.commit()
    finally:
        conn.close()
    print(f'[名录] 入库 {len(rows)} 条（含退市），取自 baostock query_stock_basic')
    report()
    return len(rows)


def report() -> None:
    conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=30)
    try:
        ensure_schema(conn)
        total = conn.execute(f'SELECT COUNT(1) FROM {TABLE}').fetchone()[0]
        listed = conn.execute(f"SELECT COUNT(1) FROM {TABLE} WHERE type='1' AND status='1'").fetchone()[0]
        main = conn.execute(f"SELECT COUNT(1) FROM {TABLE} WHERE type='1' AND status='1'").fetchall()
        rows = conn.execute(f'SELECT symbol,name FROM {TABLE} WHERE type=\'1\' AND status=\'1\'').fetchall()
        main_all = [r for r in rows if is_main_board(r[0])]
        main_clean = [r for r in main_all if not is_st(r[1])]
        st = [r for r in main_all if is_st(r[1])]
        have_kline = {r[0] for r in conn.execute('SELECT DISTINCT symbol FROM stock_daily')}
        print(f'  名录总数 {total:,}（含退市/非股票）｜在市股票 {listed:,}')
        print(f'  沪深主板 {len(main_all):,} 只，其中 ST/退市整理 {len(st):,} 只 → '
              f'可做 universe {len(main_clean):,} 只')
        print(f'  本地日K已覆盖 {len([r for r in main_clean if r[0] in have_kline]):,} 只 '
              f'（缺的通常是新股或当天没同步到）')
    finally:
        conn.close()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser()
    ap.add_argument('--stats', action='store_true', help='只打印当前库里的统计，不拉数据')
    a = ap.parse_args()
    if not a.stats:
        if sync() < 0:
            sys.exit(1)
    else:
        report()
