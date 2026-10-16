"""market_cap.py - 总市值下限过滤（2026-10-16，防量化操控小微盘）

用户口径：剔除总市值 < 35 亿的票。小盘股流通盘小、易被少量资金拉抬/砸盘做出形态，
把"被做出来"的 K 线当趋势信号很危险。落点覆盖全链路：
选票（候选池 / 智能推荐 / 趋势扫描 / 策略扫描）+ 同步K线 + 分时线。

数据来源的关键约束（2026-10-16 实测，别再去找批量接口了）：
  · 东财 clist 批量（push2/push2delay）、ulist.np 批量都不返回市值 —— f116/f117 恒为 '-'/0；
  · 只有单只 `push2 stock/get` 可靠返回 f116(总市值,元) / f117(流通市值,元) / f84(总股本)。
  所以取数 = 按**可交易范围（主板 + 非ST/退）**的股票清单逐只拉，线程池并发，灌本地表
  `stock_market_cap`（放 history_kline.db，与 stock_basic_full 同库；盘前 9:00 候选池只读本地表、不外联，
  守住候选池"网络脆弱窗口不挂外部接口"的硬约束）。
  本地库全量约 5222（含创业板/科创/北证/ST），按 universe 口径刨完只剩约 3000 ——
  创业板/北证/ST 本就被 universe 排除、永不进选票，拉它们的市值是纯浪费，故只拉主板+非ST。

过滤策略（fail-open，避免刷表前把选票/同步全锁死）：
  · 开关关 或 表为空            → 不过滤（等同改造前行为）；
  · 表里有该票 且 cap<下限      → 剔除；
  · 表里没有该票（新股/漏刷/f116=0）→ 放行。刷新覆盖本地库全清单，实际≈全覆盖。

用法：
  python market_cap.py --refresh     # 按本地库清单灌表/刷新
  python market_cap.py --stats       # 看表里多少只、多少只在下限之下
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from app.config import (KLINE_HISTORY_DB_PATH, MARKET_CAP_FILTER_ENABLED,
                        MARKET_CAP_REQUEST_DELAY, MARKET_CAP_WORKERS,
                        MIN_TOTAL_MARKET_CAP)

_UT = 'b2884a393a59ad64002292a3e90d46a5'
_DDL = '''CREATE TABLE IF NOT EXISTS stock_market_cap (
    symbol       TEXT PRIMARY KEY,
    name         TEXT,
    total_cap    REAL,
    float_cap    REAL,
    total_shares REAL,
    updated_date TEXT
)'''

# 进程内缓存：市值一周才刷一次，没必要每次过滤都查库；refresh 后置 None 失效
_CAP_CACHE: Optional[Dict[str, float]] = None


def _pure(symbol: str) -> str:
    s = (symbol or '').strip().lower()
    return s[2:] if s[:2] in ('sh', 'sz', 'bj') else s


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(KLINE_HISTORY_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=10000')
    conn.execute(_DDL)
    return conn


def _secid(code6: str) -> Optional[str]:
    """裸 6 位码 → 东财 secid。只认沪深（6→沪 / 0,3→深），北交所 4/8 一律不取（universe 本就不要）。"""
    if code6.startswith('6'):
        return '1.' + code6
    if code6.startswith(('0', '3')):
        return '0.' + code6
    return None


def _fetch_one(code6: str, retries: int = 2) -> Optional[tuple]:
    """单只拉市值。返回 (code, name, total_cap, float_cap, total_shares)；拿不到返回 None。"""
    secid = _secid(code6)
    if not secid:
        return None
    time.sleep(MARKET_CAP_REQUEST_DELAY)      # pacing：低并发+小睡，避开东财限流
    url = (f'https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&ut={_UT}'
           f'&fields=f57,f58,f116,f117,f84')
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0', 'Referer': 'https://quote.eastmoney.com/'})
            data = (__import__('json').loads(urllib.request.urlopen(req, timeout=10).read()
                                             .decode('utf-8', 'replace')) or {}).get('data') or {}
            cap = data.get('f116')
            try:
                cap = float(cap)
            except (TypeError, ValueError):
                cap = 0.0
            if cap <= 0:                    # 停牌/指数/异常：f116 为空或 0，视为未知，不落库
                return None
            def _f(k):
                try:
                    return float(data.get(k))
                except (TypeError, ValueError):
                    return None
            return (code6, str(data.get('f58') or ''), cap, _f('f117'), _f('f84'))
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(1.0 + attempt)       # 重试退避
    return None


def _local_symbols() -> List[str]:
    """本地日K库里的全部裸码（≈ 全市场，含创业板/科创/北证/ST）。"""
    conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=30)
    try:
        return [r[0] for r in conn.execute(
            'SELECT DISTINCT symbol FROM stock_daily').fetchall() if r[0]]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _universe_symbols() -> List[str]:
    """只拉"可交易范围"内的市值：主板 + 非ST/退（universe 口径，约 3000 只）。
    创业板/科创/北证/ST 本就被 universe 排除、永不进选票，不拉它们的市值。"""
    from services.data import universe
    return list(universe.filter_symbols(_local_symbols()))


def refresh(symbols: Optional[Iterable[str]] = None,
            workers: int = MARKET_CAP_WORKERS, only_missing: bool = True) -> Dict:
    """按清单逐只拉市值灌表。symbols 为空则用「主板+非ST」清单（universe 口径，约3000只）。

    2026-10-16 实测教训：12 线程猛拉单只 stock/get，约 1100 次后被东财限流，之后 1900 只全失败
    （顺序慢拉却能 12/12 成功 → 是限流不是数据问题）。故：① 默认只补缺失（resumable，重跑接着填）；
    ② 4 线程 + 每请求前小睡；③ 连续失败自适应退避。周刷新即便被限流，下次会继续，不会丢进度。
    """
    global _CAP_CACHE
    codes = [_pure(s) for s in (symbols if symbols is not None else _universe_symbols())]
    codes = [c for c in codes if len(c) == 6 and c.isdigit()]
    if not codes:
        return {'ok': 0, 'fail': 0, 'skip': 0, 'total': 0}
    todo = codes
    if only_missing:
        have = set(_load_cap_map().keys())
        todo = [c for c in codes if c not in have]
    if not todo:
        print('[市值刷新] 目标清单已全部在表，无需刷新')
        _CAP_CACHE = None
        return {'ok': 0, 'fail': 0, 'skip': 0, 'total': len(codes)}
    rows, fail, done, consec_fail = [], 0, 0, 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(_fetch_one, c): c for c in todo}
        for fut in as_completed(futs):
            done += 1
            r = fut.result()
            if r is None:
                fail += 1
                consec_fail += 1
            else:
                rows.append(r)
                consec_fail = 0
            if consec_fail >= 15:            # 连续失败=大概率被限流，退避让 IP 喘口气
                print(f'[市值刷新] 连续失败 {consec_fail} 次，退避 3s（疑似限流）')
                time.sleep(3.0)
                consec_fail = 0
            if done % 300 == 0:
                print(f'[市值刷新] 进度 {done}/{len(todo)}，新入库 {len(rows)}，失败 {fail}')
    today = datetime.now().strftime('%Y-%m-%d')
    conn = _conn()
    try:
        conn.executemany(
            'INSERT OR REPLACE INTO stock_market_cap '
            '(symbol,name,total_cap,float_cap,total_shares,updated_date) VALUES (?,?,?,?,?,?)',
            [(c, n, cap, fcap, sh, today) for (c, n, cap, fcap, sh) in rows])
        conn.commit()
    finally:
        conn.close()
    _CAP_CACHE = None                      # 失效进程内缓存
    print(f'[市值刷新] 完成：新入库 {len(rows)}，未取到(停牌/指数/限流) {fail}，本次清单 {len(todo)}')
    return {'ok': len(rows), 'fail': fail, 'skip': 0, 'total': len(todo)}


def _load_cap_map() -> Dict[str, float]:
    conn = _conn()
    try:
        return {r[0]: r[1] for r in conn.execute(
            'SELECT symbol, total_cap FROM stock_market_cap '
            'WHERE total_cap IS NOT NULL').fetchall()}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def cap_map() -> Dict[str, float]:
    global _CAP_CACHE
    if _CAP_CACHE is None:
        _CAP_CACHE = _load_cap_map()
    return _CAP_CACHE


def passes(symbol: str) -> bool:
    """该票是否过市值下限。未知/未启用一律放行（fail-open）。"""
    if not MARKET_CAP_FILTER_ENABLED:
        return True
    cap = cap_map().get(_pure(symbol))
    if cap is None:
        return True
    return cap >= MIN_TOTAL_MARKET_CAP


def filter_symbols(symbols: Iterable[str]) -> List:
    """批量筛，保留原始写法（带不帶 sh/sz 前缀原样返回）与顺序，去重。"""
    seen, out = set(), []
    for s0 in symbols:
        key = (s0 or '').strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        if passes(key):
            out.append(s0)
    return out


def apply_to_frame(df, symbol_col: str = 'symbol'):
    """按市值下限过滤 DataFrame（symbol 列裸码/带前缀均可）。未启用时原样返回。"""
    if not MARKET_CAP_FILTER_ENABLED or df is None or df.empty:
        return df
    cmap = cap_map()
    if not cmap:
        return df
    caps = df[symbol_col].astype(str).str.lower().map(
        lambda s: cmap.get(_pure(s)))
    keep = caps.isna() | (caps >= MIN_TOTAL_MARKET_CAP)   # 未知放行
    return df[keep]


def count_below() -> int:
    return sum(1 for c in cap_map().values() if c < MIN_TOTAL_MARKET_CAP)


def describe() -> str:
    if not MARKET_CAP_FILTER_ENABLED:
        return '市值下限：关'
    if not cap_map():
        return f'市值下限≥{MIN_TOTAL_MARKET_CAP/1e8:.0f}亿（表空，暂未生效，先跑 python market_cap.py --refresh）'
    return f'总市值≥{MIN_TOTAL_MARKET_CAP/1e8:.0f}亿（已排除{count_below()}只小市值）'


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--refresh', action='store_true')
    ap.add_argument('--stats', action='store_true')
    a = ap.parse_args()
    if a.refresh:
        refresh()
    if a.stats or not a.refresh:
        cm = cap_map()
        print(f'市值表：{len(cm)} 只；下限 {MIN_TOTAL_MARKET_CAP/1e8:.0f} 亿之下 {count_below()} 只')
        print(describe())
