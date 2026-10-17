"""pool_backtest.py - 盘前海选入口层的历史回测（2026-10-17）

回答一个此前没人验证的问题：**海选这个入口到底有没有超额**，还是只是把"热搜池"换成了
另一份同样没信息量的名单。评分层（13 因子/资金流/概念/公告）没有历史存档，
所以这里只回测**有完整历史支撑的那一层**：选股范围六条 + 海选趋势模板，
再叠"什么时候买"（竞价 / 尾盘两种成本）与形态分档。

时序严格照生产链路来，不留前视：
  信号日 S = 用截至 S 收盘的数据出池（生产是 S+1 早上 9:00 跑）；
  入场日 E = S 之后的**下一个交易日**，竞价成本 = E 开盘价，尾盘成本 = E 收盘价；
  结果 = E 之后第 1/3/5 天收盘，以及"次日开盘即卖"（打板口径）。

近似的三条（都是数据可得性逼出来的，结论里要带着这几条看）
------------------------------------------------------------
  1. **市值/换手用"当前股本 × 历史价"重建**：股本会因送转/定增/回购变动，前复权价里还含
     股息再投的因子，所以 2024 年的"市值"会被低估几个百分点（主板票分红率量级）。
  2. **ST 判定只有当前名称**：一只票后来才被 ST 的话，它在历史里会被当成非 ST 放进来（反之
     亦然）。这是标记偏差，不是幸存者偏差（库里的票退市前后都有K线）。
  3. **原始价用 成交额/成交量 反推**（换手、股价下限、市值都要未复权价，而 OHLC 是前复权）；
     该比值偶有异常（停牌日、零量日），已按 volume>0 过滤。

复现：python scripts/pool_backtest.py [--since 2024-05-01]
"""
import argparse
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kline_forward import add_forward, limit_pct          # noqa: E402
from services.data import universe                         # noqa: E402
import app.config as cfg                                   # noqa: E402

DB = cfg.KLINE_HISTORY_DB_PATH
sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def load_bars(since: str) -> pd.DataFrame:
    conn = sqlite3.connect(DB, timeout=120)
    df = pd.read_sql(
        'SELECT symbol, date, open, high, low, close, volume, turnover FROM stock_daily '
        'WHERE date >= ? ORDER BY symbol, date', conn, params=(since,))
    conn.close()
    for c in ('open', 'high', 'low', 'close', 'volume', 'turnover'):
        df[c] = pd.to_numeric(df[c], errors='coerce')
    return df


def scale_forward_pct(df: pd.DataFrame) -> pd.DataFrame:
    """把 add_forward 的结果列从小数换成百分数。

    ⚠️ `kline_forward.add_forward` 给的 fwd1/gap1/fmin_chg5/hi5/lo5 都是**小数**
    （0.0136 = +1.36%），而本脚本自己算的 chg/r20 是**百分数**；混用会算出
    "尾盘 T+1 均值 0.00%、亏超5% 恰好 0.0%"这种一眼假但很容易被忽略的表
    （2026-10-17 第一版就栽在这）。统一成百分数后再比较阈值。
    """
    # 精确列名，不能用前缀匹配：'hi'/'lo' 会把原始列 high/low 一起乘 100
    # （第一版就是这么把 hi60 算成 440 的，于是"距 60 日高点 8% 以内"一条永远不成立、池子直接空）
    for col in ('fwd1', 'fwd2', 'fwd3', 'fwd5', 'fwd10', 'gap1', 'hi1', 'lo1',
                'hi5', 'lo5', 'hi10', 'lo10', 'fall5', 'fmin_chg5', 'fmin_chg10'):
        if col in df.columns:
            df[col] = df[col] * 100.0
    return df


def attach_shares(df: pd.DataFrame) -> pd.DataFrame:
    """挂上"当前总股本/流通股本"，用于逐日重建市值与换手。"""
    conn = sqlite3.connect(DB, timeout=60)
    rows = conn.execute('SELECT symbol, total_shares, float_cap FROM stock_market_cap').fetchall()
    last = dict(conn.execute(
        'SELECT symbol, close FROM stock_daily WHERE date=(SELECT MAX(date) FROM stock_daily)').fetchall())
    conn.close()
    rec = {}
    for sym, tshares, fcap in rows:
        px = last.get(sym)
        if not px or px <= 0:
            continue
        rec[sym] = (float(tshares or 0), (float(fcap or 0) / px))     # 流通股本 = 流通市值/最新价
    df['total_shares'] = df['symbol'].map(lambda s: rec.get(s, (0, 0))[0])
    df['float_shares'] = df['symbol'].map(lambda s: rec.get(s, (0, 0))[1])
    return df


def build_signals(df: pd.DataFrame) -> pd.DataFrame:
    """逐票算海选所需的后视指标 + 逐日重建选股范围。"""
    g = df.groupby('symbol', sort=False)
    c, h, l, v, amt = df['close'], df['high'], df['low'], df['volume'], df['turnover']
    df['ma20'] = g['close'].transform(lambda s: s.rolling(20).mean())
    df['ma60'] = g['close'].transform(lambda s: s.rolling(60).mean())
    df['hi60'] = g['high'].transform(lambda s: s.rolling(60).max())
    df['v5'] = g['volume'].transform(lambda s: s.rolling(5).mean())
    df['amt20'] = g['turnover'].transform(lambda s: s.rolling(20).mean())
    df['vol20'] = g['volume'].transform(lambda s: s.rolling(20).mean())
    df['r20'] = g['close'].transform(lambda s: (s / s.shift(20) - 1) * 100)
    df['chg'] = g['close'].transform(lambda s: (s / s.shift(1) - 1) * 100)
    df['bars'] = g['close'].transform('count')
    # 未复权价代理（成交额/成交量）：换手、股价下限、市值都得用真实价，前复权价会系统性偏低
    df['px_raw'] = np.where(v > 0, amt / v.replace(0, np.nan), np.nan)
    df['cap_t'] = df['total_shares'] * df['px_raw']
    df['turn_t'] = np.where(df['float_shares'] > 0, df['vol20'] / df['float_shares'] * 100, np.nan)
    df['intraday'] = np.where(df['open'] > 0, c / df['open'], np.nan)   # 比值（不是百分数）：收盘/开盘
    df['e_chg'] = df['chg']
    return df


def scope_mask(df: pd.DataFrame) -> pd.Series:
    """选股范围六条，逐日重建（与线上 universe+liquidity+market_cap 同一套阈值）。"""
    st = universe.excluded_symbols()
    m = (~df['symbol'].isin(st)) & (df['bars'] >= cfg.CANDIDATE_POOL_MIN_BARS)
    m &= df['px_raw'].notna() & (df['px_raw'] > cfg.LIQ_MIN_PRICE)
    if cfg.MARKET_CAP_FILTER_ENABLED:
        m &= (df['cap_t'].isna()) | (df['cap_t'] >= cfg.MIN_TOTAL_MARKET_CAP)
    if cfg.LIQUIDITY_FILTER_ENABLED:
        if cfg.LIQ_AMOUNT_ENABLED:
            m &= df['amt20'] >= cfg.LIQ_MIN_AVG_AMOUNT
        if cfg.LIQ_TURNOVER_ENABLED:
            m &= df['turn_t'].isna() | (df['turn_t'] >= cfg.LIQ_MIN_AVG_TURNOVER_PCT)
    return m


def pool_mask(df: pd.DataFrame) -> pd.Series:
    """海选趋势模板（与 candidate_pool.build_pool 逐条件对齐）。"""
    p = (df['close'] > df['ma20']) & (df['ma20'] > df['ma60'])
    p &= df['close'] >= cfg.CANDIDATE_POOL_NEAR_HIGH * df['hi60']
    p &= (df['chg'] > 0) & (df['volume'] >= df['v5'])
    p &= df['rank_pct'] >= cfg.CANDIDATE_POOL_RPS_TOP
    return p


def attach_forward_entry(df: pd.DataFrame) -> pd.DataFrame:
    """把"入场日 E = 下一根K线"的结果列挪到信号行上。

    ⚠️ 必须在**完整密集日K**上调用，不能在筛过的池子上调用：池子是稀疏的（一只票隔两周
    才再次入选），在池子上 `shift(-1)` 拿到的"下一根"其实是"下一次入选"，日期差几十天，
    于是所有收益列都被算成接近 0、而"竞价成本"变成了两周后那天的日内涨幅 —— 第一版就是这么
    跑出一张"海选池竞价 T+1 胜率 90%"的假表的。
    """
    g = df.groupby('symbol', sort=False)
    for col, src in (('e_open', 'open'), ('e_close', 'close'), ('e_high', 'high'), ('e_low', 'low'),
                     ('e_chg', 'chg'), ('fwd1', 'fwd1'), ('fwd3', 'fwd3'), ('fwd5', 'fwd5'),
                     ('gap1', 'gap1'), ('fmin5', 'fmin_chg5'), ('intraday', 'intraday')):
        df[col] = g[src].shift(-1)
    return df


def ret_from_entry(row, k: str) -> float:
    """竞价买（成本=E开盘）持有到 E+k 收盘的收益%。"""
    f = row[k]
    if pd.isna(f) or pd.isna(row['intraday']) or row['intraday'] <= 0:
        return np.nan
    return ((1 + f / 100.0) * row['intraday'] - 1) * 100.0


def stat(sub: pd.DataFrame, label: str) -> list:
    out = []
    n = len(sub)
    if n < 50:
        return [(label, n, None, None, None, None, None)]
    for hold, col in (('T+1', 'fwd1'), ('T+3', 'fwd3'), ('T+5', 'fwd5')):
        for mode in ('竞价', '尾盘'):
            r = sub[f'{mode}_{col}'].dropna() if mode == '竞价' else sub[col].dropna()
            if len(r) < 50:
                continue
            out.append((f'{label}｜{hold}｜{mode}买', len(r), round(r.mean(), 2),
                        round((r > 0).mean() * 100, 1), round((r <= -5).mean() * 100, 1),
                        round((sub['fmin5'].dropna() <= -5).mean() * 100, 1), None))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', default='2024-05-01', help='信号日起点（前面留 80 根预热）')
    ap.add_argument('--sample', type=int, default=0, help='只算前 N 只（调试用）')
    a = ap.parse_args()

    print(f'[回测] 读日K（{a.since} 起为信号区间，前面另留预热）…')
    conn = sqlite3.connect(DB, timeout=120)
    warm = (pd.Timestamp(a.since) - pd.Timedelta(days=200)).strftime('%Y-%m-%d')
    df = pd.read_sql('SELECT symbol, date, open, high, low, close, volume, turnover FROM stock_daily '
                     'WHERE date >= ? ORDER BY symbol, date', conn, params=(warm,))
    conn.close()
    for c in ('open', 'high', 'low', 'close', 'volume', 'turnover'):
        df[c] = pd.to_numeric(df[c], errors='coerce')
    if a.sample:
        keep = sorted(df['symbol'].unique())[:a.sample]
        df = df[df['symbol'].isin(keep)]
    print(f'[回测] {df["symbol"].nunique()} 只 / {len(df)} 行；加未来窗口（已自检的向量化实现）…')
    df = add_forward(df, win=5)
    df = scale_forward_pct(df)
    df = attach_shares(df)
    df = build_signals(df)
    df = attach_forward_entry(df)        # 在密集帧上先挪好 E 列，再筛池子（见函数注释）

    # 市场层（涨停家数）按**全市场**算 —— 情绪口径不跟着选股范围缩小
    df['lim_up'] = (df['chg'] >= (df['symbol'].map(limit_pct) * 100 - 0.3))
    breadth = df.groupby('date')['lim_up'].sum()

    scope = df[scope_mask(df)].copy()
    scope['rank_pct'] = scope.groupby('date')['r20'].rank(pct=True) * 100
    sig = scope[scope['date'] >= a.since].copy()
    pool = sig[pool_mask(sig)].copy()

    # 每天最多 CANDIDATE_POOL_MAX 只（与线上一致），按 r20 降序取
    pool = pool.sort_values(['date', 'r20'], ascending=[True, False])
    pool = pool[pool.groupby('date').cumcount() < cfg.CANDIDATE_POOL_MAX]

    base = sig[sig['date'] >= a.since].copy()
    for frame in (pool, base):
        frame['竞价_fwd1'] = frame.apply(lambda r: ret_from_entry(r, 'fwd1'), axis=1)
        frame['竞价_fwd3'] = frame.apply(lambda r: ret_from_entry(r, 'fwd3'), axis=1)
        frame['竞价_fwd5'] = frame.apply(lambda r: ret_from_entry(r, 'fwd5'), axis=1)
        frame['yizi'] = (frame['e_high'] == frame['e_low']) & (frame['e_chg'] >= 9.5)

    days = pool['date'].nunique()
    per_day = pool.groupby('date').size()
    print(f'\n=== 池子规模：{days} 个信号日，每天中位 {per_day.median():.0f} 只 '
          f'(min {per_day.min()} / p90 {per_day.quantile(0.9):.0f} / max {per_day.max()}) ===')

    print('\n=== 主表：海选池 vs 基线（同期选股范围内所有票）===')
    hdr = f'{"口径":42}{"样本":>8}{"均值":>8}{"胜率":>8}{"亏超5%":>9}{"5日内大面":>11}'
    print(hdr)
    rows = []
    rows += stat(pool[~pool['yizi']], '海选池(剔一字板)')
    rows += stat(base[~base['yizi']], '基线(范围内全部)')
    for r in rows:
        if r[2] is None:
            print(f'{r[0]:42}{r[1]:>8}   样本不足')
            continue
        print(f'{r[0]:42}{r[1]:>8}{r[2]:>7.2f}%{r[3]:>7.1f}%{r[4]:>8.1f}%{r[5]:>10.1f}%')

    print('\n=== 分年稳健性（海选池，T+1）===')
    pool['yr'] = pool['date'].str[:4]
    for yr, sub in pool.groupby('yr'):
        s = sub[~sub['yizi']]
        for col, mode in (('fwd1', '尾盘'), ('竞价_fwd1', '竞价')):
            r = s[col].dropna()
            if len(r) >= 50:
                print(f'  {yr} {mode}买 T+1: 样本{len(r):>6}  均值{r.mean():+.2f}%  '
                      f'胜率{(r>0).mean()*100:.1f}%  亏超5% {(r<=-5).mean()*100:.1f}%')

    print('\n=== 按情绪档（信号日全市场涨停家数）===')
    pool['lim_cnt'] = pool['date'].map(breadth)
    bins = [(0, 50), (50, 90), (90, 120), (120, 999)]
    for lo, hi in bins:
        s = pool[(pool['lim_cnt'] >= lo) & (pool['lim_cnt'] < hi) & (~pool['yizi'])]
        r = s['fwd1'].dropna(); rb = s['竞价_fwd1'].dropna()
        if len(r) >= 50:
            print(f'  涨停 {lo}~{hi} 家: 样本{len(s):>6}  尾盘 T+1 {r.mean():+.2f}%/胜率{(r>0).mean()*100:.1f}%'
                  f'   竞价 T+1 {rb.mean():+.2f}%/胜率{(rb>0).mean()*100:.1f}%')

    print('\n=== 按形态分档（信号日可见的特征，对应时机矩阵的桶）===')
    def bucket(r):
        if r['chg'] >= (limit_pct(r['symbol']) * 100 - 0.3):
            return 'A 当日封板'
        if r['chg'] >= 4.5:
            return 'B 半山腰4.5~9.8'
        if r['r20'] > 60:
            return 'E 20日>60%过热'
        if r['volume'] / max(r['v5'], 1e-9) > 3:
            return 'D 巨量拉升'
        return 'C 其余（横盘/温和上行）'
    pool['bucket'] = pool.apply(bucket, axis=1)
    for b, sub in pool.groupby('bucket'):
        s = sub[~sub['yizi']]
        r = s['fwd1'].dropna(); rb = s['竞价_fwd1'].dropna(); r3 = s['fwd3'].dropna()
        if len(r) >= 40:
            print(f'  {b:22} 样本{len(s):>6}  尾盘T+1 {r.mean():+.2f}%/{(r>0).mean()*100:.1f}%  '
                  f'竞价T+1 {(rb.mean() if len(rb) else float("nan")):+.2f}%/'
                  f'{(rb>0).mean()*100 if len(rb) else float("nan"):.1f}%  尾盘T+3 {r3.mean():+.2f}%  '
                  f'亏超5% {(r<=-5).mean()*100:.1f}%')
    return 0


if __name__ == '__main__':
    sys.exit(main())
