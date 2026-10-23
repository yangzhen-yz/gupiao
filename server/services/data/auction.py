"""auction.py - 09:25 集合竞价快照（2026-10-22）

为什么要有它
------------
现在所有"买入时机"都只能在开盘后用，因为系统没有 9:25 的竞价数据：
  · 腾讯实时行情在 9:25 那一分钟里，字段 3 就是**竞价撮合价**、字段 36 是竞价成交量；
  · 过了 9:30 同一字段就变成连续竞价的最新价，**再取就已经不是竞价了**。
所以这张表必须由定时任务在 9:25 当场采，事后补不回来。它是以后做
"弱转强 / 首板竞价高开 / 竞价爆量"这些策略的最小数据前提。

数据口径（腾讯 qt.gtimg.cn，2026-10-22 实测字段）
------------------------------------------------
  price=3  竞价匹配价     prev_close=4 昨收       open=5 今开
  volume=36 成交量(手)    amount=37 成交额(万元)
  bid1=9  ask1=19         stamp=30 行情时间戳(YYYYMMDDHHMMSS)
落库时：volume ×100 → 股，amount ×10000 → 元，gap_pct = price/prev_close-1。

两条硬规矩
----------
1. **只写竞价窗口**（09:24~09:31）。晚于这个窗口宁可不写，也不要把 9:45 的现价
   冒充竞价价 —— 一张错表比没有表更坏。
2. 只采 `universe.eligible_symbols()`（主板非ST+市值≥35亿，≈2462 只），
   分批 60 只，失败批次只记数不阻塞；09:45 推荐绝不能因为竞价采集而延迟。
"""
from __future__ import annotations

import sqlite3
import sys
import time
from datetime import date, datetime
from typing import Dict, List, Optional

import requests

from app.config import AUCTION_SNAPSHOT_BATCH, EASTMONEY_HEADERS, SQLITE_DB_PATH

_URL = 'https://qt.gtimg.cn/q='
_UA = {'User-Agent': EASTMONEY_HEADERS.get('User-Agent', 'Mozilla/5.0')}

# 腾讯字段位置（~ 分隔，2026-10-22 实测；与 eod_snapshot 同源）
_F_NAME, _F_PRICE, _F_PREV, _F_OPEN = 1, 3, 4, 5
_F_BID1, _F_ASK1 = 9, 19
_F_STAMP, _F_VOL_HANDS, _F_AMT_WAN = 30, 36, 37

# 竞价窗口：9:25 撮合，9:30 转连续竞价。给调度排队留 1 分钟余量，
# 但绝不放到 9:30 之后（那时字段已是盘中现价）。
_AUCTION_START = '09:24'
_AUCTION_END = '09:31'

_DDL = '''CREATE TABLE IF NOT EXISTS auction_snapshot (
    trade_date     TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    name           TEXT,
    auction_price  REAL,
    prev_close     REAL,
    gap_pct        REAL,
    volume         REAL,
    amount         REAL,
    bid1           REAL,
    ask1           REAL,
    quote_time     TEXT,
    fetched_at     TEXT,
    PRIMARY KEY (trade_date, symbol)
)'''


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(SQLITE_DB_PATH, timeout=30)
    conn.execute('PRAGMA busy_timeout=10000')
    return conn


def _pure(symbol: str) -> str:
    s = (symbol or '').strip().lower()
    return s[2:] if s[:2] in ('sh', 'sz', 'bj') else s


def _prefixed(code: str) -> str:
    code = _pure(code)
    return ('sh' + code) if code.startswith('6') else ('sz' + code) if code[:2] in ('00', '30') else ''


def _num(f: List[str], i: int) -> Optional[float]:
    try:
        return float(f[i])
    except (TypeError, ValueError, IndexError):
        return None


def _parse_line(ln: str) -> Optional[Dict]:
    try:
        key = ln.split('=')[0].replace('v_', '').strip()
        body = ln.split('"', 1)[1].rsplit('"', 1)[0]
    except (IndexError, ValueError):
        return None
    f = body.split('~')
    if len(f) <= _F_AMT_WAN:
        return None
    price, prev = _num(f, _F_PRICE), _num(f, _F_PREV)
    stamp = (f[_F_STAMP] or '').strip()
    if not price or not prev or price <= 0 or prev <= 0 or len(stamp) < 12:
        return None
    vol_hands = _num(f, _F_VOL_HANDS) or 0.0
    amt_wan = _num(f, _F_AMT_WAN) or 0.0
    return {
        'symbol': _pure(key),
        'name': f[_F_NAME],
        'price': price,
        'prev_close': prev,
        'open': _num(f, _F_OPEN),
        'gap_pct': (price / prev - 1.0) * 100.0,
        'volume': vol_hands * 100.0,          # 手 → 股
        'amount': amt_wan * 1e4,              # 万元 → 元
        'bid1': _num(f, _F_BID1) or 0.0,
        'ask1': _num(f, _F_ASK1) or 0.0,
        'quote_time': stamp,
    }


def _in_auction_window(now: Optional[datetime] = None) -> bool:
    hm = (now or datetime.now()).strftime('%H:%M')
    return _AUCTION_START <= hm <= _AUCTION_END


def fetch_auction(codes: List[str], batch: int = AUCTION_SNAPSHOT_BATCH,
                  pause: float = 0.15) -> Dict[str, Dict]:
    """批量拉竞价快照。返回 {裸码: 快照}，以及失败批次数挂在 '_failedBatches'。"""
    out: Dict[str, Dict] = {}
    failed = 0
    todo = [t for t in (_prefixed(c) for c in codes) if t]
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        ok = False
        for attempt in range(3):
            try:
                r = requests.get(_URL + ','.join(chunk), headers=_UA, timeout=12)
                r.encoding = 'gbk'
                for ln in r.text.strip().split(';'):
                    ln = ln.strip()
                    if ln.startswith('v_'):
                        d = _parse_line(ln)
                        if d:
                            out[d['symbol']] = d
                ok = True
                break
            except Exception as e:
                if attempt == 2:
                    print(f'[竞价快照] 第 {i // batch + 1} 批失败: {type(e).__name__}: {e}')
                else:
                    time.sleep(1 + attempt)
        if not ok:
            failed += 1
        time.sleep(pause)
    out['_failedBatches'] = failed  # type: ignore[assignment]
    return out


def snapshot_universe(batch: Optional[int] = None, codes: Optional[List[str]] = None,
                      dry_run: bool = False, now: Optional[datetime] = None) -> Dict:
    """采一次全范围竞价快照并落 auction_snapshot。

    dry_run=True：不做时间窗校验、不写库，只回报"拿到了多少只/字段样例"，
    用于盘后核对接口字段有没有改版。
    返回 {ok, date, rows, scope, failedBatches, why?}
    """
    now = now or datetime.now()
    trade_date = now.strftime('%Y-%m-%d')
    if not dry_run and not _in_auction_window(now):
        return {'ok': False, 'date': trade_date, 'rows': 0, 'scope': 0,
                'failedBatches': 0,
                'why': f'当前 {now:%H:%M} 不在竞价窗口 {_AUCTION_START}~{_AUCTION_END}，拒写'}
    if codes is None:
        from services.data import universe
        codes = universe.eligible_symbols()
    batch = int(batch or AUCTION_SNAPSHOT_BATCH)
    snap = fetch_auction(codes, batch=batch)
    failed = int(snap.pop('_failedBatches', 0) or 0)
    fetched_at = now.strftime('%Y-%m-%d %H:%M:%S')
    rows = []
    stale = 0
    for s, d in snap.items():
        if d['quote_time'][:8] != now.strftime('%Y%m%d'):
            stale += 1                       # 停牌/行情时间停在旧交易日，宁缺毋错
            continue
        rows.append((trade_date, s, d['name'], d['price'], d['prev_close'], d['gap_pct'],
                     d['volume'], d['amount'], d['bid1'], d['ask1'], d['quote_time'], fetched_at))
    if not dry_run and rows:
        conn = _conn()
        try:
            conn.execute(_DDL)
            conn.executemany(
                'INSERT OR REPLACE INTO auction_snapshot '
                '(trade_date,symbol,name,auction_price,prev_close,gap_pct,volume,amount,'
                ' bid1,ask1,quote_time,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', rows)
            conn.commit()
        finally:
            conn.close()
    return {'ok': bool(rows), 'date': trade_date, 'rows': len(rows), 'scope': len(codes),
            'failedBatches': failed, 'stale': stale,
            'sample': snap.get(rows[0][1]) if (rows and dry_run) else None}


def load_day(trade_date: Optional[str] = None) -> List[Dict]:
    """读某天竞价快照（默认今天），按 gap_pct 降序。给以后的首板/弱转强筛选用。"""
    d = trade_date or date.today().strftime('%Y-%m-%d')
    conn = _conn()
    try:
        conn.execute(_DDL)
        cur = conn.execute(
            'SELECT trade_date,symbol,name,auction_price,prev_close,gap_pct,volume,amount,'
            'bid1,ask1,quote_time,fetched_at FROM auction_snapshot '
            'WHERE trade_date=? ORDER BY gap_pct DESC', (d,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def stats() -> Dict:
    conn = _conn()
    try:
        conn.execute(_DDL)
        n = conn.execute('SELECT COUNT(*) FROM auction_snapshot').fetchone()[0]
        days = conn.execute('SELECT COUNT(DISTINCT trade_date) FROM auction_snapshot').fetchone()[0]
        lo, hi = conn.execute('SELECT MIN(trade_date), MAX(trade_date) FROM auction_snapshot').fetchone()
    finally:
        conn.close()
    return {'rows': n, 'days': days, 'from': lo, 'to': hi, 'db': SQLITE_DB_PATH}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description='09:25 集合竞价快照')
    ap.add_argument('--dry-run', action='store_true', help='只拉不写（盘后核对字段用）')
    ap.add_argument('--limit', type=int, default=0, help='只拉前 N 只（调试用）')
    ap.add_argument('--batch', type=int, default=AUCTION_SNAPSHOT_BATCH)
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if a.dry_run and not a.limit:
        ap.error('--dry-run 请配 --limit（避免盘后对全市场打 41 次请求）')
    codes = None
    if a.limit:
        from services.data import universe
        codes = universe.eligible_symbols()[:a.limit]
    r = snapshot_universe(batch=a.batch, codes=codes, dry_run=a.dry_run)
    print(r)
    print(stats())
    return 0 if r.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())
