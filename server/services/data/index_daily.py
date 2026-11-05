# -*- coding: utf-8 -*-
"""index_daily.py - 大盘指数日线本地化（2026-11-03，#12 抗跌因子的前置）

为什么要有这张表
    #12 要算"个股当日相对大盘的回撤"，但本地**一个指数都没有**：
    `history_kline.db.stock_daily` 的 symbol 全是 6 位个股纯代码（60/00/30/68 四段），
    上证指数只能靠主库 `kline_cache` 里 200 根、每次现拉外部源的 JSON。
    没有本地指数日线，"相对大盘"就只能每次请求现取，收盘后也在走网络——这正是
    资金流那个 9 秒的同款毛病。

数据源与回填
    腾讯 `web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=<sym>,day,,,N,qfq`，
    2026-11-03 实测可用（一次取 8 根成功；同域名的日K路径此前被 WAF 拦过，
    用的是 `appstock/app/fqkline/get` 这条，与题材链回填同一条路）。
    返回行序是 **[日期, 开, 收, 高, 低, 量]**——收盘在第 3 位，不是第 2 位，别搞错。
    因为一次就能拿 700 根，所以**首日即可回填全历史**，不必像板块表那样干等攒数。

边界
    · 只存 OHLC + 成交量，不存成交额（腾讯这条接口该字段为空，不猜单位）；
    · 回填失败不覆盖已有好数据（按 (symbol,date) UPSERT，且只在拿到有效行时写）；
    · 开关 INDEX_DAILY_ENABLED=False 时既不回填也不日更，且**不改动任何现有读取路径**
      （现在读指数的地方仍读它的 kline_cache，切过来是下一步的事）。
"""
import json
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional

# 要本地化的指数。先只放抗跌因子真正要用的那几个，不铺摊子。
INDEXES = {
    'sh000001': '上证指数',
    'sz399001': '深证成指',
    'sz399006': '创业板指',
    'sh000300': '沪深300',
}
FQKLINE_URL = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
DDL = '''CREATE TABLE IF NOT EXISTS index_daily (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    name TEXT DEFAULT '',
    open REAL, high REAL, low REAL, close REAL,
    volume REAL,
    source TEXT DEFAULT 'tencent_fqkline',
    created_at DATETIME DEFAULT (datetime('now', '+8 hours')),
    PRIMARY KEY (symbol, date)
)'''


def ensure_table() -> bool:
    try:
        from db.database import get_db_conn
        conn = get_db_conn()
        conn.execute(DDL)
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f'[指数日线] 建表失败: {e}')
        return False


def fetch_kline(symbol: str, bars: int = 700) -> List[List]:
    """取该指数最近 bars 根日K。返回 [[date, open, close, high, low, volume], ...]。"""
    import requests
    url = '%s?param=%s,day,,,%d,qfq' % (FQKLINE_URL, symbol, int(bars))
    r = requests.get(url, timeout=(3.0, 12.0), headers={'User-Agent': 'Mozilla/5.0'})
    r.raise_for_status()
    j = r.json()
    node = (j.get('data') or {}).get(symbol) or {}
    rows = node.get('qfqday') or node.get('day') or []
    return [x for x in rows if isinstance(x, (list, tuple)) and len(x) >= 5]


def upsert_rows(symbol: str, rows: List[List]) -> int:
    """按 (symbol,date) UPSERT。腾讯行序是 [日期, 开, 收, 高, 低, 量]，**收盘在第 3 位**。"""
    if not rows:
        return 0
    from db.database import get_db_conn
    name = INDEXES.get(symbol, '')
    recs = []
    for x in rows:
        try:
            d = str(x[0])[:10]
            o, cl, h, l = float(x[1]), float(x[2]), float(x[3]), float(x[4])
            vol = float(x[5]) if len(x) > 5 and x[5] not in (None, '') else None
        except (TypeError, ValueError, IndexError):
            continue
        if not d or cl <= 0:
            continue
        recs.append((symbol, d, name, o, h, l, cl, vol))
    if not recs:
        return 0
    conn = get_db_conn()
    conn.executemany(
        'INSERT INTO index_daily (symbol, date, name, open, high, low, close, volume) '
        'VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(symbol, date) DO UPDATE SET '
        'name=excluded.name, open=excluded.open, high=excluded.high, low=excluded.low, '
        'close=excluded.close, volume=excluded.volume', recs)
    conn.commit()
    conn.close()
    return len(recs)


def sync_all(bars: Optional[int] = None) -> Dict:
    """同步所有已登记指数。返回 {symbol: 写入行数}，单只失败不影响其他。"""
    from app.config import INDEX_DAILY_ENABLED, INDEX_DAILY_BARS
    out: Dict[str, int] = {}
    if not INDEX_DAILY_ENABLED:
        return {'disabled': True}
    if not ensure_table():
        return {'error': 'table_missing'}
    n = int(bars or INDEX_DAILY_BARS)
    for sym, cn in INDEXES.items():
        try:
            rows = fetch_kline(sym, n)
            written = upsert_rows(sym, rows)
            out[sym] = written
            print(f'[指数日线] {sym} {cn} 写入 {written} 行'
                  + (f'（首 {rows[0][0]} 末 {rows[-1][0]}）' if rows else '（无数据）'))
        except Exception as e:
            out[sym] = 0
            print(f'[指数日线] {sym} {cn} 同步失败: {str(e)[:90]}')
    return out


def stats() -> Dict:
    """本地已有哪些指数、各多少天、覆盖到哪一天。"""
    try:
        from db.database import get_db_conn
        conn = get_db_conn()
        rows = conn.execute('SELECT symbol, count(*) n, min(date) lo, max(date) hi '
                            'FROM index_daily GROUP BY symbol ORDER BY symbol').fetchall()
        conn.close()
        return {'rows': [dict(r) for r in rows], 'indexes': len(rows)}
    except Exception as e:
        return {'error': str(e)[:80]}
