"""limit_pool.py - 涨停/炸板/跌停池逐票日表（2026-10-22，P3 数据底座）

情绪快照只有聚合数（几家涨停、几家炸板），回答不了"今天首板/弱转强的票
第二天到底怎样"。把东财三个池子逐票存下来，是"首板次日溢价""炸板后修复"
这类因子的最小前提 —— 没有历史名单，这些策略只能凭印象。

两个写入点：
  · 盘中情绪刷新（`sentiment._sync_collect`）：约 3 分钟一轮，调用方自己节流；
  · 16:10 收评（`review._sync_fetch_stocks`）：收盘定版，**最重要的一份**。
表按 (trade_date, pool, symbol) 覆盖写，同一天重复采集不会重复计数；
盘中先写的会被收盘定版覆盖，正好反映"最后谁封住了"。

`raw` 保留东财原始 JSON：以后要补字段（几天几板 zttj、封单占比、流通市值）
不用重抓历史。价格 `p` 在东财池接口里是**毫元**，这里按 review 既有口径 /1000 转元。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Dict, List, Optional

from db.database import get_db_conn

POOLS = {'zt': '涨停池', 'zb': '炸板池', 'dt': '跌停池'}

_DDL = '''CREATE TABLE IF NOT EXISTS limit_pool_daily (
    trade_date TEXT NOT NULL,
    pool       TEXT NOT NULL,          -- zt / zb / dt
    symbol     TEXT NOT NULL,
    name       TEXT,
    change_pct REAL,
    price      REAL,                   -- 元（东财原始 p 为毫元，已 /1000）
    seal_fund  REAL,                   -- 封板资金，元
    turnover   REAL,                   -- 换手率，%
    lianban    INTEGER,                -- 连板数（首板=1）
    first_seal TEXT,                   -- 首次封板时间，保留原始格式
    last_seal  TEXT,
    zhaban     INTEGER,                -- 当日炸板次数
    raw        TEXT,                   -- 东财原始 JSON
    created_at TEXT,
    PRIMARY KEY (trade_date, pool, symbol)
)'''


def _num(v) -> Optional[float]:
    try:
        x = float(v)
        return x if x == x else None          # 挡 NaN
    except (TypeError, ValueError):
        return None


def _int(v) -> Optional[int]:
    x = _num(v)
    return None if x is None else int(x)


def _txt(v) -> Optional[str]:
    if v is None or v == '':
        return None
    return str(v)


def ensure_table(conn=None) -> None:
    own = conn is None
    c = conn or get_db_conn()
    try:
        c.execute(_DDL)
        if own:
            c.commit()
    finally:
        if own:
            c.close()


def save_pools(day: str, zt: Optional[List[Dict]] = None,
               zb: Optional[List[Dict]] = None, dt: Optional[List[Dict]] = None,
               conn=None) -> int:
    """落一天的三个池子，返回写入行数。池子为空不写，失败只打印、不抛。

    失败不抛是刻意的：这是情绪的附带采集，接口挂了不应该让温度计整块没了。
    但打印必须显式（`[涨停池] 落库失败`），否则表一直是空的还没人知道。
    """
    groups = {'zt': zt or [], 'zb': zb or [], 'dt': dt or []}
    if not any(groups.values()):
        return 0
    stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    rows = []
    for kind, items in groups.items():
        for it in items:
            code = str(it.get('c') or '').strip()
            if not code:
                continue
            price = _num(it.get('p'))
            rows.append((
                day, kind, code, it.get('n'),
                _num(it.get('zdp')),
                round(price / 1000.0, 3) if price is not None else None,
                _num(it.get('fund')), _num(it.get('hs')), _int(it.get('lbc')),
                _txt(it.get('fbt')), _txt(it.get('lbt')), _int(it.get('zbc')),
                json.dumps(it, ensure_ascii=False), stamp,
            ))
    if not rows:
        return 0
    own = conn is None
    c = conn or get_db_conn()
    try:
        c.execute(_DDL)
        c.executemany(
            'INSERT OR REPLACE INTO limit_pool_daily (trade_date,pool,symbol,name,'
            'change_pct,price,seal_fund,turnover,lianban,first_seal,last_seal,'
            'zhaban,raw,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)
        if own:
            c.commit()
        return len(rows)
    except Exception as error:
        print(f'[涨停池] 落库失败 day={day}: {error}')
        return 0
    finally:
        if own:
            c.close()


def load_day(day: str, kind: Optional[str] = None) -> List[Dict]:
    """读某天的池子明细，按 连板数降序、涨跌幅降序。没表/没数据返回 []。"""
    conn = get_db_conn()
    try:
        sql = ('SELECT trade_date,pool,symbol,name,change_pct,price,seal_fund,turnover,'
               'lianban,first_seal,last_seal,zhaban FROM limit_pool_daily WHERE trade_date=?')
        args: tuple = (day,)
        if kind:
            sql += ' AND pool=?'
            args += (kind,)
        sql += ' ORDER BY lianban DESC, change_pct DESC'
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def stats() -> Dict:
    """自检用：库里有几天、每类池子多少行、最后一天是哪天。"""
    conn = get_db_conn()
    try:
        rows = conn.execute(
            'SELECT trade_date, pool, COUNT(*) n FROM limit_pool_daily '
            'GROUP BY trade_date, pool ORDER BY trade_date DESC').fetchall()
        days = sorted({r['trade_date'] for r in rows})
        by: Dict[str, int] = {}
        for r in rows:
            by[r['pool']] = by.get(r['pool'], 0) + r['n']
        return {'days': len(days), 'first': days[0] if days else '',
                'last': days[-1] if days else '', 'rowsByPool': by,
                'recent': [dict(r) for r in rows[:12]]}
    except Exception as error:
        return {'days': 0, 'error': str(error)[:120]}
    finally:
        conn.close()
