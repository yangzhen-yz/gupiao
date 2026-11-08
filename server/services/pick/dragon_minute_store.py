# -*- coding: utf-8 -*-
"""
龙头通道·原始分钟 bar 持久化（影子安全）。

目的：scan_once 每轮已现拉当日分时 bars（parse_minute_rows 产物），但只存在内存里随
进程丢弃，dragon_backtest.simulate() 需要的「原始分钟 bar + 触发时刻 + 次日开盘」无法 replay。
本模块把候选票的**当日完整分钟 bar** 落库（UPSERT，每 (trade_date,symbol) 一行，最新一轮覆盖），
供日后按 trigger_hm 用 bar_index() 切出信号时刻、结合次日开盘结算真实收益。

设计要点（对齐 dragon_shadow 的容错风格）：
- 落库失败只打印，绝不打断扫描主链路；
- ensure_tables 幂等（CREATE IF NOT EXISTS），首次 save 时自建；
- bars 为 list[dict]，JSON 序列化即可（key: t/price/vol/cum_vol/cum_amt/vwap/vol_ratio_run）。
"""
import json
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from db.database import get_db_conn

TABLE = 'intraday_dragon_minute'

_DDL = '''CREATE TABLE IF NOT EXISTS intraday_dragon_minute (
    trade_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    bars_json TEXT,
    bar_count INTEGER,
    updated_at TEXT,
    PRIMARY KEY (trade_date, symbol)
)'''

_ensured = False


def ensure_tables(conn=None) -> None:
    own = conn is None
    c = conn or get_db_conn()
    try:
        c.execute(_DDL)
        c.commit()
    finally:
        if own:
            try:
                c.close()
            except Exception:
                pass


def save_minute(trade_date: str, symbol: str, bars: Sequence[Dict], conn=None) -> int:
    """UPSERT 一只票的当日完整分钟 bar。返回 1=写入 / 0=跳过。落库失败抛给调用方容错。"""
    global _ensured
    if not bars:
        return 0
    if not _ensured:
        ensure_tables()
        _ensured = True
    own = conn is None
    c = conn or get_db_conn()
    try:
        payload = json.dumps(list(bars), ensure_ascii=False)
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        c.execute(
            f'INSERT OR REPLACE INTO {TABLE} '
            f'(trade_date, symbol, bars_json, bar_count, updated_at) '
            f'VALUES (?, ?, ?, ?, ?)',
            (trade_date, symbol, payload, len(bars), now))
        c.commit()
        return 1
    finally:
        if own:
            try:
                c.close()
            except Exception:
                pass


def load_minute(trade_date: str, symbol: str, conn=None) -> Optional[List[Dict]]:
    """读回某票某交易日原始分钟 bar；无则返回 None。"""
    global _ensured
    if not _ensured:
        ensure_tables()
        _ensured = True
    own = conn is None
    c = conn or get_db_conn()
    try:
        row = c.execute(
            f'SELECT bars_json FROM {TABLE} WHERE trade_date=? AND symbol=?',
            (trade_date, symbol)).fetchone()
        if not row or not row[0]:
            return None
        return json.loads(row[0])
    except Exception:
        return None
    finally:
        if own:
            try:
                c.close()
            except Exception:
                pass


def scan_existing(trade_date: Optional[str] = None, conn=None) -> List[str]:
    """返回已落库 (trade_date,symbol) 清单，供回测枚举样本。trade_date 为空则全量。"""
    global _ensured
    if not _ensured:
        ensure_tables()
        _ensured = True
    own = conn is None
    c = conn or get_db_conn()
    try:
        if trade_date:
            rows = c.execute(
                f'SELECT trade_date, symbol FROM {TABLE} WHERE trade_date=?',
                (trade_date,)).fetchall()
        else:
            rows = c.execute(
                f'SELECT trade_date, symbol FROM {TABLE}').fetchall()
        return [f'{d}|{s}' for d, s in rows]
    except Exception:
        return []
    finally:
        if own:
            try:
                c.close()
            except Exception:
                pass
