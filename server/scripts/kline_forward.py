"""kline_forward.py - 本地日K + 未来窗口统计的**唯一可信实现**（2026-10-16）

为什么单独有这个模块：我第一版复盘脚本用
    df.groupby('symbol')['low'].transform(lambda s: s.shift(-1).rolling(5).min())
算"未来 5 日最低价"，看着天经地义，实测**与朴素逐行循环有 90% 的行不一致**：
封板票"5 日内跌破买价 -4%"的比例，错误实现给出 100.0%（中位 -15.7%），
朴素实现给出 65.0%（中位 -6.4%）。这种偏差会一路传染到所有结论上。

原因是 pandas 在 transform 里对带 shift 的窗口做索引对齐时，会把结果按组内位置
回填到原索引，而 shift(-1) 已经把语义挪了一格 —— 于是"未来 5 日"变成了"含今天往后再
错位若干格"的某个窗口。凡是"用未来数据算结果"的口径，都必须显式按股票分组、
用整数组切片，不能靠 transform 的 lambda。

这里用 numpy 的 sliding_window_view 显式切片，并对 200 只随机样本与朴素循环逐值比对
（`python kline_forward.py --selftest`）。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from typing import Optional

import numpy as np
import pandas as pd

# 脚本 2026-10-16 被搬进 server/scripts/ 之后，`import services.xxx` 需要 server 根目录
# 在 sys.path 上。这一步漏掉的后果是**静默出错数**：范围过滤 import 失败就跳过，
# 产物打印的是全市场 2,187,193 条（基线 48.5%），看着像结果、其实根本不是选股范围。
_SERVER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_ROOT not in sys.path:
    sys.path.insert(0, _SERVER_ROOT)

KLINE_DB = r'D:\Desktop\guizhou\gupiao\server\data\history_kline.db'

FUTURE_WIN = 5      # 看未来 5 个交易日（≈ 一周）


def limit_pct(code: str) -> float:
    """涨停幅度：创业/科创 20%，主板 10%（ST 的 5% 与北交所 30% 这里不单独处理）。"""
    c = code[-6:]
    if c.startswith(('30', '68')):
        return 0.20
    if c.startswith(('43', '83', '87', '92')):
        return 0.30
    return 0.10


def _future_extreme(arr: np.ndarray, n: int, win: int, how: str) -> np.ndarray:
    """未来 win 根（不含当日）的最大/最小值，按整数组切片显式算，绝不靠 transform。

    结果长度 n；末尾不足 win 根的位置给 nan（这些行后续会被 dropna 掉，
    因为它们还没有"未来"，留着就会把最后一周变成空洞样本）。
    """
    out = np.full(n, np.nan)
    if n <= 1:
        return out
    shifted = np.concatenate([arr[1:], [np.nan]])            # shifted[i] = arr[i+1]
    tail = np.full(win - 1, np.nan)
    padded = np.concatenate([shifted, tail])                 # 保证窗口能滑到末尾
    w = np.lib.stride_tricks.sliding_window_view(padded, win)[:n]
    with np.errstate(invalid='ignore'):
        # 末尾未来不足 win 根的窗口整片是 nan，nanmax 会叫唤一声 —— 结果本来就该是 nan
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', category=RuntimeWarning)
            res = np.nanmax(w, axis=1) if how == 'max' else np.nanmin(w, axis=1)
    # 窗口里全是 nan（未来不足 win 根）时 nanmax 会给 nan，正好；但若只有一根有效也照算
    out[:] = res
    valid = np.arange(n) + win < n                           # 只有未来满 win 根的才算完整
    out[~valid] = np.nan
    return out


def _future_shift(arr: np.ndarray, k: int) -> np.ndarray:
    out = np.full(len(arr), np.nan)
    if len(arr) > k:
        out[:-k] = arr[k:]
    return out


def _back_window(arr: np.ndarray, win: int, how: str) -> np.ndarray:
    """过去 win 根（不含当日）的最大/最小/均值 —— 决策时点真正能看到的东西。"""
    padded = np.concatenate([np.full(win, np.nan), arr])
    w = np.lib.stride_tricks.sliding_window_view(padded, win)[:len(arr)]
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', category=RuntimeWarning)
        if how == 'max':
            return np.nanmax(w, axis=1)
        if how == 'min':
            return np.nanmin(w, axis=1)
        return np.nanmean(w, axis=1)


def _lag(arr: np.ndarray, k: int) -> np.ndarray:
    """取 k 根之前的值（前面补 nan）。"""
    return np.concatenate([np.full(k, np.nan), arr])[:len(arr)]


def add_forward(df: pd.DataFrame, win: int = FUTURE_WIN) -> pd.DataFrame:
    """给按 (symbol, date) 升序排好的日K加"未来窗口"结果列。

    产出列（都以当日 close 为买入成本）：
      fwd1/fwd2/fwd3/fwd5   未来第 1/2/3/5 天收盘相对买入价
      gap1                  次日开盘溢价（竞价能不能买到、买得贵不贵）
      hi1                   次日最高相对买入价（冲高卖出的可兑现度）
      fmin_chg{win}         未来 win 天内最差的单日涨跌幅（"吃大面"口径）
      fall{win}             未来 win 天内最坏的"当日最高→最低"回落
      hi{win}/lo{win}       未来 win 天内的最高/最低相对买入价
    """
    need = ['symbol', 'date', 'open', 'high', 'low', 'close', 'volume']
    df = df.copy()
    for c in need[2:]:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    SWING_WIN = 10      # 波段口径再看远一周
    out = {k: np.full(len(df), np.nan) for k in
           ('fwd1', 'fwd2', 'fwd3', 'fwd5', 'fwd10', 'gap1', 'hi1', f'fmin_chg{win}', f'fall{win}',
            f'hi{win}', f'lo{win}', f'fmin_chg{SWING_WIN}', f'hi{SWING_WIN}', f'lo{SWING_WIN}')}
    prev_close = np.full(len(df), np.nan)
    chg = np.full(len(df), np.nan)
    vol_ma5_prev = np.full(len(df), np.nan)
    hi60_prev = np.full(len(df), np.nan)
    r20 = np.full(len(df), np.nan)
    ma20 = np.full(len(df), np.nan)
    board_n = np.zeros(len(df))
    lim_pct_arr = df['symbol'].map(limit_pct).to_numpy()
    rows = df.reset_index(drop=True)
    for _, idx in rows.groupby('symbol', sort=False).indices.items():
        idx = np.asarray(idx)
        o = rows['open'].to_numpy()[idx]
        h = rows['high'].to_numpy()[idx]
        l = rows['low'].to_numpy()[idx]
        c = rows['close'].to_numpy()[idx]
        v = rows['volume'].to_numpy()[idx]
        n = len(idx)
        pc = np.concatenate([[np.nan], c[:-1]])          # 前一日收盘
        chg[idx] = c / pc - 1
        prev_close[idx] = pc
        base = c
        out['fwd1'][idx] = _future_shift(c, 1) / base - 1
        out['fwd2'][idx] = _future_shift(c, 2) / base - 1
        out['fwd3'][idx] = _future_shift(c, 3) / base - 1
        out['fwd5'][idx] = _future_shift(c, 5) / base - 1
        out['gap1'][idx] = _future_shift(o, 1) / base - 1
        out['hi1'][idx] = _future_shift(h, 1) / base - 1
        hi = _future_extreme(h, n, win, 'max')
        lo = _future_extreme(l, n, win, 'min')
        out[f'hi{win}'][idx] = hi / base - 1
        out[f'lo{win}'][idx] = lo / base - 1
        hi10 = _future_extreme(h, n, SWING_WIN, 'max')
        lo10 = _future_extreme(l, n, SWING_WIN, 'min')
        out[f'hi{SWING_WIN}'][idx] = hi10 / base - 1
        out[f'lo{SWING_WIN}'][idx] = lo10 / base - 1
        out[f'fwd{SWING_WIN}'][idx] = _future_shift(c, SWING_WIN) / base - 1
        day_chg = chg[idx]
        out[f'fmin_chg{win}'][idx] = _future_extreme(day_chg, n, win, 'min')
        out[f'fmin_chg{SWING_WIN}'][idx] = _future_extreme(day_chg, n, SWING_WIN, 'min')
        fall = l / h - 1
        out[f'fall{win}'][idx] = _future_extreme(fall, n, win, 'min')
        # 只用当日及以前信息的特征（决策时点可见），全部按组向量化
        vol_ma5_prev[idx] = _back_window(v, 5, 'mean')
        ma20[idx] = _back_window(c, 20, 'mean')
        hi60_prev[idx] = _back_window(h, 60, 'max')
        with np.errstate(invalid='ignore', divide='ignore'):
            r20[idx] = c / _lag(c, 20) - 1
        # 连板层级：连续封板天数（1=首板）。这个必须顺序算，但只是一次线性扫描
        run = 0
        lims = lim_pct_arr[idx]
        is_lim = np.where(np.isfinite(day_chg), day_chg >= lims * 0.985, False)
        for k in range(n):
            run = run + 1 if is_lim[k] else 0
            board_n[idx[k]] = run
    for k, vals in out.items():
        df[k] = vals
    df['prev_close'] = prev_close
    df['chg'] = chg
    df['vol_ma5_prev'] = vol_ma5_prev
    df['vol_ratio'] = df['volume'] / vol_ma5_prev
    df['hi60_prev'] = hi60_prev
    df['dist_hi60'] = df['close'] / hi60_prev - 1
    df['r20'] = r20
    df['limit_pct'] = df['symbol'].map(limit_pct)
    df['limit_close'] = df['chg'] >= df['limit_pct'] * 0.985
    df['touched_limit'] = (df['high'] / df['prev_close'] - 1) >= df['limit_pct'] * 0.985
    df['zhaban'] = df['touched_limit'].fillna(False) & ~df['limit_close'].fillna(False)
    df['ma20'] = ma20
    df['above_ma20'] = df['close'] > df['ma20']
    df['board_n'] = board_n
    return df


def apply_universe(df: pd.DataFrame) -> pd.DataFrame:
    """按当前股票范围过滤（默认按用户口径：仅沪深主板、剔除 ST/*ST）。

    为什么复盘也要跟着范围走：不同板的涨跌停幅度不一样（主板 10%、创业/科创 20%、
    北交所 30%、ST 5%），"封板/炸板/准涨停"这些判定的阈值本身就是板块相关的。
    拿全市场的平均去指导只做主板的操作，等于用他买不到的票给他算胜率。
    名录缺失时**直接报错**，不返回"未过滤的 df"。2026-10-16 实测教训：旧版这里 print 一行就
    原样返回，脚本搬进 `server/scripts/` 之后 `import services` 因 sys.path 失效而失败，
    于是三份产物全都静默变成了全市场 218 万条的数（基线 48.5%），而日志只在第一行提了一句 ——
    "错数"比"没数"危险得多，宁可让脚本当场停下。
    """
    try:
        from services.data import universe
    except Exception as e:
        raise RuntimeError(f'[股票范围] 载入 universe 失败，拒绝按"不过滤"继续出数'
                           f'（那等于悄悄换成全市场口径）: {e}') from e
    before = df['symbol'].nunique()
    out = universe.apply_to_frame(df)
    # 市值下限 + 流动性三条也一起上（2026-10-16 用户口径："所有的选股条件默认：主板、非ST、
    # 市值>35亿"，同日追加"近20日日均成交额>8000万、日均换手>1.5%、价>3元"）。
    # 注意这两层用的都是**今天**的表（市值快照 / 近20日成交统计），用在历史回测里属于事后信息；
    # 这里要的是"研究口径 = 下单口径"，不是预测力，所以打上标签，
    # 别让读表的人以为当年就知道这些票。
    scope = universe.describe()
    try:
        from services.data import market_cap
        out = market_cap.apply_to_frame(out, 'symbol')
        scope += '、总市值≥35亿'
    except Exception as e:
        print(f'[股票范围] 市值这层没接上(按不含市值处理): {e}')
    try:
        from services.data import universe as _u
        liq = set(_u.selectable_symbols())
        if liq:
            out = out[out['symbol'].astype(str).str.lower().isin(liq)]
            scope += '、近20日日均成交额>0.8亿/换手>1.5%/价>3元'
    except Exception as e:
        print(f'[股票范围] 流动性这层没接上(按不含流动性处理): {e}')
    scope += '（以上两层按当前数据表，历史回放里属事后信息）'
    after = out['symbol'].nunique()
    print(f'[股票范围] {scope}：{before} → {after} 只')
    if after == 0:
        # 绝不"退回不过滤"：2026-10-16 就是这个退回把三份产物静默变成全市场口径的错数。
        raise RuntimeError('[股票范围] 过滤后一只不剩（名录/市值表没同步？）—— '
                           '先跑 python scripts/sync_stock_basic.py 与 python -m services.data.market_cap --refresh')
    return out


def load_kline(since: str = '2024-06-01', forward_since: Optional[str] = None,
               db_path: str = KLINE_DB, universe: bool = False) -> pd.DataFrame:
    """读本地日K → 加未来窗口统计 → （可选）截断到分析区间 → （可选）按股票范围过滤。"""
    conn = sqlite3.connect(db_path)
    df = pd.read_sql('SELECT symbol, date, open, high, low, close, volume, turnover '
                     'FROM stock_daily WHERE date >= ? ORDER BY symbol, date',
                     conn, params=(since,))
    conn.close()
    df['symbol'] = df['symbol'].astype(str).str.lower()
    # 注意：turnover/volume 算出来的是**未复权**均价，而 open/high/low/close 全是前复权价。
    # 实测只有 71.5% 的行落在当日 [最低,最高] 区间内（分红除权后前复权价被缩小，未复权均价
    # 就会顶到区间外，比如 000028 均价 27.85 对最高 25.05）。所以这一列只能当"未复权参考"，
    # 绝不能拿来与前复权 OHLC 一起算收益 —— 想近似"盘中买入成本"，请用 open 与 close 两个锚点框住。
    with np.errstate(invalid='ignore', divide='ignore'):
        df['vwap_unadj'] = np.where(df['volume'].to_numpy() > 0,
                                    df['turnover'].to_numpy() / df['volume'].to_numpy(), np.nan)
    df = add_forward(df)
    if forward_since:
        df = df[df['date'] >= forward_since]
    if universe:
        df = apply_universe(df)
    return df


def market_breadth(df: pd.DataFrame) -> pd.DataFrame:
    """按日聚合出当天市场层（涨停家数 / 上涨占比 / 炸板率），merge 回个股表用。"""
    b = df.groupby('date').agg(
        adv=('chg', lambda s: (s > 0.002).mean()),
        lim_cnt=('limit_close', 'sum'),
        touch=('touched_limit', 'sum'),
        zb_cnt=('zhaban', 'sum')).reset_index()
    b['zb_rate'] = b['zb_cnt'] / b['touch'].clip(lower=1)
    return b


def _selftest(n_symbols: int = 200) -> int:
    """把向量化实现和"最笨的下标循环"逐值对一遍，防止再犯 transform 的错。

    覆盖：未来窗口（gap1/hi1/fwd1/fwd5/lo5/hi5/fall5）、量比、20 日涨幅、MA20、
    距 60 日高、连板层级。任何一项对不上就返回非 0 —— 这套数字是要直接拿去
    定买法的，错了比不算更糟。
    """
    import random
    import sys
    import warnings
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    warnings.filterwarnings('ignore')
    rng = random.Random(20261016)
    df = load_kline('2025-06-01')
    syms = rng.sample(sorted(df['symbol'].unique()), n_symbols)
    checked = 0
    bad = {}
    for sym in syms:
        s = df[df['symbol'] == sym].reset_index(drop=True)
        o, h, l, c, v = (s[k].to_numpy() for k in ('open', 'high', 'low', 'close', 'volume'))
        lim = limit_pct(sym)
        pc = np.concatenate([[np.nan], c[:-1]])
        chg = c / pc - 1
        run = 0
        runs = []
        for i in range(len(c)):
            run = run + 1 if (np.isfinite(chg[i]) and chg[i] >= lim * 0.985) else 0
            runs.append(run)
        for i in range(60, len(c) - FUTURE_WIN):
            naive = {
                'gap1': o[i + 1] / c[i] - 1,
                'hi1': h[i + 1] / c[i] - 1,
                'fwd1': c[i + 1] / c[i] - 1,
                'fwd5': c[i + 5] / c[i] - 1,
                f'hi{FUTURE_WIN}': h[i + 1:i + FUTURE_WIN + 1].max() / c[i] - 1,
                f'lo{FUTURE_WIN}': l[i + 1:i + FUTURE_WIN + 1].min() / c[i] - 1,
                f'fall{FUTURE_WIN}': min((l[j] / h[j] - 1) for j in range(i + 1, i + FUTURE_WIN + 1)),
                f'fmin_chg{FUTURE_WIN}': min(chg[i + 1:i + FUTURE_WIN + 1]),
                'vol_ratio': v[i] / v[i - 5:i].mean(),
                'r20': c[i] / c[i - 20] - 1,
                'ma20': c[i - 20:i].mean(),
                'dist_hi60': c[i] / h[i - 60:i].max() - 1,
                'board_n': float(runs[i]),
            }
            for k, want in naive.items():
                got = s[k].iloc[i]
                if not (np.isfinite(want) and np.isfinite(got)):
                    continue
                if abs(float(want) - float(got)) > 1e-9:
                    bad[k] = bad.get(k, 0) + 1
            checked += 1
    print(f'自检：随机 {n_symbols} 只、比对 {checked:,} 行 × 13 个口径')
    if bad:
        print('  ✗ 与朴素算法不一致：', bad)
        return 1
    print('  ✓ 全部一致（gap1/hi1/fwd1/fwd5/hi5/lo5/fall5/fmin_chg5/vol_ratio/r20/ma20/dist_hi60/board_n）')
    return 0


if __name__ == '__main__':
    import argparse
    import sys
    ap = argparse.ArgumentParser()
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if a.selftest:
        sys.exit(_selftest())
    print('用法：作为模块被复盘脚本 import；或跑 python kline_forward.py --selftest 验证口径')
