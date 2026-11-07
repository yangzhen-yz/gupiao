# -*- coding: utf-8 -*-
"""candidate_pool_shadow.py - 海选层影子留痕（2026-11-06，`docs/影子实验台账.md` 第 16 条）

一句话：把"被海选挡在门外"的票逐条记下来，攒够 20 个交易日才有资格谈"入口要不要放宽"。

为什么现在要做
--------------
海选的四条门槛（`candidate_pool.py::build_pool`）排在评分之前：收盘 > MA20 > MA60、
收盘距 60 日高点 8% 以内、当日上涨且量能不缩、20 日涨幅排进当天选股范围前 20%。
而 `strategy_snapshot` 只留"进了评分但被挡"的样本（`blocked_by='低于门槛60'` 等），
**被入口挡掉的一条都不留痕**。于是"低位启动票到底有没有 alpha"这个问题，目前只能拿
"已经被闸门筛过的 218 条复盘"去论证闸门该不该放宽——那是循环论证（11-05 那次分析指得对）。
本模块只做一件事：每天把选股范围内**所有**票的四条掩码、是否过筛、次日 OHLC 落一张新表，
不参与任何判定，不改评分、不改门槛、不改推送。

口径（先写在这里，裁决时别改）
------------------------------
  · 记录范围 = `universe.selectable_symbols()`（2026-11-06 实测 1168 只），与海选同一份范围；
  · 掩码一律由 `candidate_pool.condition_masks()` 产出，影子**不复制表达式**，防止两边漂移；
    自测里有一条"影子 passed == 线上 sel"的逐元素断言守着它；
  · `passed=1` = 四条都过；`in_pool=1` = 当天真的写进了 `daily_candidate_pool`
    （还要过 `CANDIDATE_POOL_MAX=250` 的截断，当前实际最多 59 只，两者通常相同）；
  · 次日收益一律 close→close（`t1_close_ret_pct`），另存 open/high/low 的收益，用来判断
    "开盘能不能买到、盘中最高吃到多少"；**次日没有K线（停牌/还没同步）一律留 NULL**，
    用 `t1_filled=0` 标出，绝不填 0 混成"平盘"；
  · 回填历史时，市值与流动性用的是**当前**名录（事后信息），只作方向性对照；
    正式裁决以向前累积的样本为准，`evaluate()` 天数不足直接 `ready=False`。

开关：`CANDIDATE_POOL_SHADOW_ENABLED`（默认 True，只写这张新表）；
配置页 `candidate_pool_shadow_enabled` 可热改。关掉 = 不建表、不写行，逐字节等于改造前。
"""
import argparse
import bisect
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

_FLAGS = ('ma', 'near', 'chg', 'vol', 'rank')
_LABELS = {
    'ma': '均线未多头(close>MA20>MA60)',
    'near': '距60日高点超8%',
    'chg': '当日未涨',
    'vol': '量能未放大',
    'rank': '20日涨幅未进前20%',
}

_DDL = '''CREATE TABLE IF NOT EXISTS candidate_pool_shadow (
    pool_date        TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    close            REAL,
    chg              REAL,
    r20              REAL,
    rank_pct         REAL,
    near_high_pct    REAL,
    n_bars           INTEGER,
    m_ma             INTEGER,
    m_near           INTEGER,
    m_chg            INTEGER,
    m_vol            INTEGER,
    m_rank           INTEGER,
    n_fail           INTEGER,
    rejected_by      TEXT,
    passed           INTEGER,
    in_pool          INTEGER,
    t1_date          TEXT,
    t1_open          REAL,
    t1_high          REAL,
    t1_low           REAL,
    t1_close         REAL,
    t1_open_ret_pct  REAL,
    t1_close_ret_pct REAL,
    t1_high_ret_pct  REAL,
    t1_low_ret_pct   REAL,
    t1_filled        INTEGER DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (pool_date, symbol)
)'''

_BAND_SQL = ("CASE WHEN chg IS NULL THEN '未知' "
             "WHEN chg >= 9.8 THEN '涨停≥9.8' "
             "WHEN chg >= 4.5 THEN '半山腰4.5~9.8' "
             "WHEN chg >= 2.0 THEN '甜点2~4.5' "
             "WHEN chg > 0 THEN '弱势0~2' ELSE '下跌' END")


# ---------------------------------------------------------------- 开关与配置
def enabled() -> bool:
    """独立开关：先热读覆盖表，读不到退回 config 默认。"""
    try:
        from services.support import app_settings
        v = app_settings.value('candidate_pool_shadow_enabled')
        if v is not None:
            return bool(v)
    except Exception:
        pass
    from app.config import CANDIDATE_POOL_SHADOW_ENABLED
    return bool(CANDIDATE_POOL_SHADOW_ENABLED)


def _cfg_int(key: str, cfg_name: str, default: int) -> int:
    """只有 key 真的进了配置表才走覆盖值；没进表（如 MAX/DAYS 这类内部上限）直接读 config。

    2026-11-06 踩过：`app_settings.value()` 第一行就是 `spec(key)`，没注册的 key 会
    `KeyError` 被这里吞掉，于是覆盖值永远读不到 —— 与其静默退回默认，不如先判清楚。
    """
    try:
        from services.support import app_settings
        if any(i['key'] == key for i in app_settings.SPEC):
            v = app_settings.value(key)
            if v is not None:
                return int(v)
    except Exception:
        pass
    import app.config as _cfg
    return int(getattr(_cfg, cfg_name, default))


def max_rows() -> int:
    return max(1, _cfg_int('candidate_pool_shadow_max', 'CANDIDATE_POOL_SHADOW_MAX', 2000))


def eval_days() -> int:
    return max(1, _cfg_int('candidate_pool_shadow_days', 'CANDIDATE_POOL_SHADOW_DAYS', 20))


# ---------------------------------------------------------------- 建表 / 读写
def _conn():
    from db.database import get_db_conn
    return get_db_conn()


def _ensure_table(conn) -> None:
    conn.execute(_DDL)


def record_day(pool_date: str, day, masks: Dict[str, object],
               in_pool: Optional[Set[str]] = None) -> int:
    """把某交易日的全范围掩码落库（幂等：同键只更新掩码与价格，**不覆盖已回填的 T+1**）。

    day   : 含 symbol/close/chg/r20/rank_pct/hi60/n_bars 的 DataFrame（build_pool 的当天切片）
    masks : candidate_pool.condition_masks(day) 的结果（与线上筛选同一份表达式）
    in_pool: 当天真的进了 daily_candidate_pool 的前缀代码集合
    """
    if not enabled() or day is None or len(day) == 0:
        return 0

    keep = day.copy()
    for f in _FLAGS:
        m = masks[f]
        keep['m_' + f] = m.reindex(keep.index).fillna(False).astype(bool)
    n_pass = keep[['m_' + f for f in _FLAGS]].astype(int).sum(axis=1)
    keep['n_fail'] = (len(_FLAGS) - n_pass).astype(int)
    keep['passed'] = (n_pass == len(_FLAGS)).astype(int)
    keep['rejected_by'] = keep.apply(
        lambda r: ','.join(f for f in _FLAGS if not bool(r['m_' + f])), axis=1)
    keep = keep.sort_values('r20', ascending=False).head(max_rows())

    pool_set = in_pool or set()
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    rows = []
    for r in keep.itertuples():
        code = str(getattr(r, 'symbol', '') or '')
        sym = _prefixed(code) or code
        rows.append((
            pool_date, sym,
            _f(getattr(r, 'close', None)), _f(getattr(r, 'chg', None)),
            _f(getattr(r, 'r20', None)), _f(getattr(r, 'rank_pct', None)),
            _f(getattr(r, 'near_high_pct', None)), int(getattr(r, 'n_bars', 0) or 0),
            int(bool(r.m_ma)), int(bool(r.m_near)), int(bool(r.m_chg)),
            int(bool(r.m_vol)), int(bool(r.m_rank)),
            int(r.n_fail), r.rejected_by, int(r.passed),
            1 if sym in pool_set else 0, ts, ts,
        ))
    if not rows:
        return 0

    conn = _conn()
    try:
        _ensure_table(conn)
        conn.executemany(
            'INSERT INTO candidate_pool_shadow (pool_date,symbol,close,chg,r20,rank_pct,'
            'near_high_pct,n_bars,m_ma,m_near,m_chg,m_vol,m_rank,n_fail,rejected_by,passed,'
            'in_pool,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(pool_date,symbol) DO UPDATE SET '
            'close=excluded.close, chg=excluded.chg, r20=excluded.r20, '
            'rank_pct=excluded.rank_pct, near_high_pct=excluded.near_high_pct, '
            'n_bars=excluded.n_bars, m_ma=excluded.m_ma, m_near=excluded.m_near, '
            'm_chg=excluded.m_chg, m_vol=excluded.m_vol, m_rank=excluded.m_rank, '
            'n_fail=excluded.n_fail, rejected_by=excluded.rejected_by, '
            'passed=excluded.passed, in_pool=excluded.in_pool, updated_at=excluded.updated_at',
            rows)
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def fill_outcomes(kline_path: Optional[str] = None) -> Dict:
    """把还没回填的 T+1 补齐。**先读日K并关连接，再开写连接即写即提交**（11-03 锁库教训）。"""
    if not enabled():
        return {'enabled': False, 'filled': 0}
    # 只补最近 45 个自然日内的待回填行：停牌/退市的行会永远没有"下一根"，若把它们算进
    # min(pool_date)，每次都要从很早的那天开始扫全库 —— 读取窗口会随天数无界增长。
    # 超过窗口的行保持 NULL（evaluate 只统计 t1_filled=1，不会被混成平盘）。
    cutoff = (datetime.now() - timedelta(days=45)).strftime('%Y-%m-%d')
    conn = _conn()
    try:
        _ensure_table(conn)
        # 按列位取，不吃 row_factory —— 真连接是 sqlite3.Row，自测里的桩可能是 tuple
        pending = [{'pool_date': r[0], 'symbol': r[1], 'close': r[2]}
                   for r in conn.execute(
                       'SELECT pool_date, symbol, close FROM candidate_pool_shadow '
                       'WHERE t1_filled=0 AND pool_date >= ?', (cutoff,)).fetchall()]
    finally:
        conn.close()
    if not pending:
        return {'enabled': True, 'filled': 0, 'pending': 0}

    min_d = min(p['pool_date'] for p in pending)
    bars = _read_bars(min_d, kline_path)
    by_code: Dict[str, List[tuple]] = {}
    for sym, dte, o, h, l, c in bars:
        by_code.setdefault(str(sym), []).append((dte, o, h, l, c))
    for v in by_code.values():
        v.sort(key=lambda x: x[0])
    dates_by_code = {k: [x[0] for x in v] for k, v in by_code.items()}

    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    upd = []
    for p in pending:
        code = str(p['symbol'])[-6:]
        lst = by_code.get(code)
        if not lst:
            continue
        i = bisect.bisect_right(dates_by_code[code], p['pool_date'])
        if i >= len(lst):
            continue                      # 次日那根还没来：留 NULL，等下一轮
        dte, o, h, l, c = lst[i]
        base = _f(p['close'])
        if not base:
            continue
        upd.append((
            dte, o, h, l, c,
            _ret(o, base), _ret(c, base), _ret(h, base), _ret(l, base),
            p['pool_date'], p['symbol'], ts,
        ))
    if not upd:
        return {'enabled': True, 'filled': 0, 'pending': len(pending)}

    conn = _conn()
    try:
        conn.executemany(
            'UPDATE candidate_pool_shadow SET t1_date=?, t1_open=?, t1_high=?, t1_low=?, '
            't1_close=?, t1_open_ret_pct=?, t1_close_ret_pct=?, t1_high_ret_pct=?, '
            't1_low_ret_pct=?, t1_filled=1, updated_at=? '
            'WHERE pool_date=? AND symbol=?',
            [(u[0], u[1], u[2], u[3], u[4], u[5], u[6], u[7], u[8],
              datetime.now().strftime('%Y-%m-%d %H:%M:%S'), u[9], u[10]) for u in upd])
        conn.commit()
        return {'enabled': True, 'filled': len(upd), 'pending': len(pending)}
    finally:
        conn.close()


# ---------------------------------------------------------------- 评估
def evaluate(min_days: Optional[int] = None) -> Dict:
    """只读汇总：按涨幅段 × 是否进池、以及"被哪条单独挡掉"给均值/胜率。
    天数不足直接 `ready=False`——影子没到期就写"未裁决"。"""
    min_days = min_days or eval_days()
    conn = _conn()
    try:
        _ensure_table(conn)
        days = conn.execute(
            'SELECT COUNT(DISTINCT pool_date) FROM candidate_pool_shadow WHERE t1_filled=1'
        ).fetchone()[0]
        groups: Dict[str, List[float]] = {}
        highs: Dict[str, List[float]] = {}
        for r in conn.execute(
                f'SELECT {_BAND_SQL} b, passed, in_pool, n_fail, rejected_by, '
                't1_close_ret_pct, t1_high_ret_pct FROM candidate_pool_shadow '
                'WHERE t1_filled=1').fetchall():
            band, passed, in_pool, n_fail, rej, ret, hi = r
            tag = ('进池' if in_pool else ('过筛未进池' if passed else '被挡'))
            key = f'{band} | {tag}'
            groups.setdefault(key, []).append(float(ret))
            if hi is not None:
                highs.setdefault(key, []).append(float(hi))
            if not in_pool and n_fail == 1:
                k2 = f'{band} | 只被「{_LABELS.get(rej, rej)}」挡'
                groups.setdefault(k2, []).append(float(ret))
        out = {'ready': days >= min_days, 'days': days, 'min_days': min_days,
               'groups': {}}
        for k, v in sorted(groups.items()):
            n = len(v)
            if n < 10:
                continue
            out['groups'][k] = {
                'n': n, 'mean': round(sum(v) / n, 2),
                'win': round(sum(1 for x in v if x > 0) / n * 100, 1),
                'median': round(sorted(v)[n // 2], 2),
                'mean_high': (round(sum(highs[k]) / len(highs[k]), 2)
                              if highs.get(k) else None),
            }
        return out
    finally:
        conn.close()


def print_eval(res: Optional[Dict] = None) -> None:
    res = res or evaluate()
    print(f"[海选影子] 已回填 {res['days']} 个交易日（判据 ≥{res['min_days']}）"
          f" → {'可裁决' if res['ready'] else '样本不足，未裁决'}")
    for k, s in res['groups'].items():
        print(f"  {k:<44s} n={s['n']:<5d} 次日收盘均值 {s['mean']:>6.2f}% "
              f"胜率 {s['win']:>5.1f}% 中位 {s['median']:>6.2f}%"
              + (f" 最高均值 {s['mean_high']:.2f}%" if s['mean_high'] is not None else ''))


# ---------------------------------------------------------------- 历史回填（事后信息，只作方向性对照）
def backfill(days: int = 20, kline_path: Optional[str] = None) -> Dict:
    if not enabled():
        return {'enabled': False, 'days': 0}
    import pandas as pd
    from services.pick import candidate_pool as cp

    path = kline_path or cp.KLINE_HISTORY_DB_PATH
    since = (datetime.now() - timedelta(days=420)).strftime('%Y-%m-%d')
    kconn = sqlite3.connect(path, timeout=60)
    try:
        df = pd.read_sql('SELECT symbol, date, high, close, volume FROM stock_daily '
                         'WHERE date >= ?', kconn, params=(since,))
    finally:
        kconn.close()
    if df.empty:
        return {'enabled': True, 'days': 0}

    from services.data import universe
    scope = set(universe.selectable_symbols())
    df['symbol'] = df['symbol'].astype(str).str.lower()
    df = df[df['symbol'].isin(scope)].sort_values(['symbol', 'date'])
    g = df.groupby('symbol', group_keys=False)
    df['ma20'] = g['close'].transform(lambda s: s.rolling(20).mean())
    df['ma60'] = g['close'].transform(lambda s: s.rolling(60).mean())
    df['hi60'] = g['high'].transform(lambda s: s.rolling(60).max())
    df['v5'] = g['volume'].transform(lambda s: s.rolling(5).mean())
    df['r20'] = g['close'].transform(lambda s: (s / s.shift(20) - 1) * 100)
    df['chg'] = g['close'].transform(lambda s: (s / s.shift(1) - 1) * 100)
    df['n_bars'] = g['close'].transform('count')
    full = df.dropna(subset=['ma60', 'hi60', 'v5', 'r20', 'chg']).copy()
    # rank_pct 必须与 build_pool 同口径：**按天**在选股范围内排百分位
    full['rank_pct'] = full.groupby('date')['r20'].rank(pct=True) * 100
    full['near_high_pct'] = full['close'] / full['hi60'] * 100

    dates = sorted(full['date'].unique())[-days:]
    written = 0
    conn = _conn()
    try:
        for d in dates:
            pool = {r[0] for r in conn.execute(
                'SELECT symbol FROM daily_candidate_pool WHERE pool_date=?', (d,)).fetchall()}
            day = full[full['date'] == d]
            masks = cp.condition_masks(day)
            written += record_day(str(d), day, masks, in_pool=pool)
    finally:
        conn.close()
    filled = fill_outcomes(kline_path=path)
    return {'enabled': True, 'days': len(dates), 'written': written, **filled}


# ---------------------------------------------------------------- 内部工具
def _prefixed(code: str) -> str:
    if code.startswith('6'):
        return 'sh' + code
    if code.startswith(('0', '3')):
        return 'sz' + code
    return ''


def _f(v) -> Optional[float]:
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


def _ret(v, base) -> Optional[float]:
    try:
        return round((float(v) / float(base) - 1) * 100, 3)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _read_bars(since_date: str, kline_path: Optional[str] = None) -> List[tuple]:
    from app.config import KLINE_HISTORY_DB_PATH
    path = kline_path or KLINE_HISTORY_DB_PATH
    conn = sqlite3.connect(path, timeout=60)
    try:
        return conn.execute(
            'SELECT symbol, date, open, high, low, close FROM stock_daily WHERE date >= ?',
            (since_date,)).fetchall()
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description='海选层影子：记录 / 回填 / 评估')
    ap.add_argument('--backfill', type=int, default=0, help='回填最近 N 个交易日（含 T+1）')
    ap.add_argument('--fill', action='store_true', help='只补 T+1')
    ap.add_argument('--eval', action='store_true', help='打印汇总')
    ap.add_argument('--stats', action='store_true', help='表内天数/行数/抓取范围')
    a = ap.parse_args()
    if not enabled():
        print('开关 candidate_pool_shadow_enabled / CANDIDATE_POOL_SHADOW_ENABLED 已关，空跑')
        return
    if a.backfill:
        print(backfill(a.backfill))
    if a.fill:
        print(fill_outcomes())
    if a.stats:
        conn = _conn()
        try:
            _ensure_table(conn)
            for r in conn.execute('SELECT COUNT(DISTINCT pool_date), COUNT(*), '
                                  'SUM(t1_filled), MIN(pool_date), MAX(pool_date) '
                                  'FROM candidate_pool_shadow').fetchall():
                print(f'交易日 {r[0]}、行 {r[1]}、已回填 {r[2]}、区间 {r[3]}~{r[4]}')
        finally:
            conn.close()
    if a.eval or not (a.backfill or a.fill or a.stats):
        print_eval()


if __name__ == '__main__':
    main()
