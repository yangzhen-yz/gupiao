"""minute_forward.py - 腾讯 5 分钟线前向采集（2026-10-22）

为什么不用 baostock
------------------
`minute_history.py` 走 baostock，逐只逐天拉，登录频率一高就被判黑（10001011），
10-16 已经整晚停摆过一次。5 分钟线只用来回答"今天早盘/尾盘买到底值多少钱"，
不值得再把整条链路押在需要登录的源上。腾讯 `mkline` 接口零登录、单只一次请求，
收盘后对**活跃票**（当天推荐 + 持仓 + 海选池）各拉一次即可。

口径（2026-10-22 实测 sh600000）
-------------------------------
  m5 每根 = [时间(YYYYMMDDHHMM，**段结束时刻**), open, close, high, low, 成交量(手), {}, ?]
  · 时间补 '00000' 成 baostock 的 17 位格式，`session_price` 可以无缝复用；
  · 成交量 ×100 → 股；
  · 接口**不返成交额**，amount 落 NULL —— 不拿 volume×price 估一个数冒充真实成交额。
  · 价格是**未复权实时价**：只适合近几日分析；跨除权日的历史比较要回到 baostock 前复权库。

只采活跃票、不全市场，原因是请求数与调度线程：100 只 ≈ 30~40 秒，
15:05 跑完不耽误 15:10 收评；全市场 2462 只会把收评和后续任务一起拖后。
这只样本本身有选择偏差，做统计时要写明"活跃票样本"，别当成全市场结论。
"""
from __future__ import annotations

import sqlite3
import sys
import time
from datetime import date
from typing import Dict, List, Optional

import requests

from app.config import EASTMONEY_HEADERS, SQLITE_DB_PATH

_URL = 'https://ifzq.gtimg.cn/appstock/app/kline/mkline'
_UA = {'User-Agent': EASTMONEY_HEADERS.get('User-Agent', 'Mozilla/5.0'),
       'Referer': 'https://gu.qq.com/'}
_DEFAULT_LIMIT = 100
_MAX_BARS = 60          # 约 1.25 个交易日，足够覆盖当天 48 根


def _pure(symbol: str) -> str:
    s = (symbol or '').strip().lower()
    return s[2:] if s[:2] in ('sh', 'sz', 'bj') else s


def _prefixed(code: str) -> str:
    code = _pure(code)
    return ('sh' + code) if code.startswith('6') else ('sz' + code) if code[:2] in ('00', '30') else ''


def _bars_to_rows(arr: List, trade_date: str) -> List[tuple]:
    rows = []
    for b in arr:
        if not isinstance(b, list) or len(b) < 6:
            continue
        t = str(b[0])
        if len(t) < 12 or t[:8] != trade_date.replace('-', ''):
            continue
        try:
            o, c, h, low = float(b[1]), float(b[2]), float(b[3]), float(b[4])
            vol = float(b[5]) * 100.0          # 手 → 股
        except (TypeError, ValueError):
            continue
        if not o or not c:
            continue
        rows.append((t[:12] + '00000', o, h, low, c, vol, None))
    rows.sort(key=lambda r: r[0])
    return rows


def fetch_day(symbol: str, trade_date: Optional[str] = None,
              bars: int = _MAX_BARS, retries: int = 2) -> List[tuple]:
    """拉某只票某天的 5 分钟线（腾讯）。返回 (time,o,h,l,c,vol,amount=None) 列表。"""
    key = _prefixed(symbol)
    if not key:
        return []
    d = trade_date or date.today().strftime('%Y-%m-%d')
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            r = requests.get(_URL, params={'param': f'{key},m5,,{int(bars)}'},
                             headers=_UA, timeout=12)
            j = r.json()
            if j.get('code') not in (0, '0', None):
                raise RuntimeError(f"code={j.get('code')} msg={j.get('msg')}")
            node = ((j.get('data') or {}).get(key) or {})
            return _bars_to_rows(node.get('m5') or [], d)
        except Exception as e:
            last_err = e
            if attempt + 1 < retries:
                time.sleep(1 + attempt)
    print(f'[5分钟前向] {symbol} {d} 拉取失败: {type(last_err).__name__}: {last_err}')
    return []


def active_symbols(limit: int = _DEFAULT_LIMIT) -> List[str]:
    """当天"值得采"的票：推荐 ∪ 持仓 ∪ 推荐快照，再用海选池按 r20 补足到 limit。"""
    from services.data import universe
    today = date.today().strftime('%Y-%m-%d')
    out: List[str] = []
    seen = set()

    def _add(seq):
        for s in seq:
            c = _pure(s)
            if c and c not in seen:
                seen.add(c)
                out.append(c)

    conn = sqlite3.connect(SQLITE_DB_PATH, timeout=30)
    try:
        queries = (
            ('SELECT symbol FROM daily_recommendations WHERE rec_date=?', (today,)),
            ('SELECT symbol FROM user_positions', ()),
            ("SELECT symbol FROM strategy_snapshot WHERE snap_date=? AND stage='recommended'",
             (today,)),
        )
        for sql, args in queries:
            try:
                _add([r[0] for r in conn.execute(sql, args).fetchall()])
            except sqlite3.Error as e:
                print(f'[5分钟前向] 活跃票查询跳过（{e}）')
        if len(out) < limit:
            try:
                _add([r[0] for r in conn.execute(
                    'SELECT symbol FROM daily_candidate_pool WHERE pool_date=? '
                    'ORDER BY COALESCE(r20, -999) DESC LIMIT ?', (today, limit)).fetchall()])
            except sqlite3.Error:
                pass
    finally:
        conn.close()
    return [c for c in out if universe.in_universe(c)][:limit]


def collect_active(codes: Optional[List[str]] = None, limit: int = _DEFAULT_LIMIT,
                   trade_date: Optional[str] = None, dry_run: bool = False,
                   pause: float = 0.12) -> Dict:
    """采集并按天落 minute_5m。返回 {date, symbols, rows, empty, failed, dryRun}。"""
    from services.data.minute_history import save_rows
    d = trade_date or date.today().strftime('%Y-%m-%d')
    codes = [_pure(c) for c in (codes or active_symbols(limit))]
    rows_total, empty, failed = 0, 0, 0
    for i, code in enumerate(codes):
        rows = fetch_day(code, d)
        if not rows:
            empty += 1
            continue
        if not dry_run:
            try:
                rows_total += save_rows(code, d, rows)
            except Exception as e:
                failed += 1
                print(f'[5分钟前向] {code} 落库失败: {type(e).__name__}: {e}')
        else:
            rows_total += len(rows)
        if (i + 1) % 20 == 0:
            print(f'[5分钟前向] 进度 {i + 1}/{len(codes)}，已落 {rows_total} 行')
        time.sleep(pause)
    return {'date': d, 'symbols': len(codes), 'rows': rows_total,
            'empty': empty, 'failed': failed, 'dryRun': dry_run}


def stats() -> Dict:
    from services.data.minute_history import stats as _st
    return _st()


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description='腾讯 5 分钟线前向采集（活跃票）')
    ap.add_argument('--codes', default='', help='逗号分隔，指定票（默认推荐+持仓+海选池）')
    ap.add_argument('--date', default=None, help='交易日 YYYY-MM-DD（默认今天）')
    ap.add_argument('--limit', type=int, default=_DEFAULT_LIMIT)
    ap.add_argument('--dry-run', action='store_true', help='只拉不写')
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    codes = [c for c in a.codes.split(',') if c.strip()] or None
    r = collect_active(codes=codes, limit=a.limit, trade_date=a.date, dry_run=a.dry_run,
                       pause=0.05 if a.dry_run else 0.12)
    print(r)
    print(stats())
    return 0 if r['rows'] or r['dryRun'] else 1


if __name__ == '__main__':
    sys.exit(main())
