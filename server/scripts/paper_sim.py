"""paper_sim.py - 组合级模拟：给定本金，按"海选入口 + 时点 + 卖出纪律"逐日滚动（2026-10-17）

为什么要单独写这个：用户问"10 万本金按目前的策略 3 个月后有多少钱"。
逐笔期望（pool_backtest 那张表）回答不了这个问题 —— 它不含仓位、现金约束、
交易成本和 T+1 占用。这里把这些补上，做一个可复现的组合模拟。

⚠️ 这不是"你的系统的回测"，差在哪要写明白：
  · **评分层不可得**：13 因子/资金流/概念标签/利空公告/盘中轨迹都没有历史存档，
    所以池内选哪 3 只能用 `r20` 降序近似（生产是按评分）。这条差异足以让结论不同，
    任何拿这个数当"系统预期收益"的用法都是错的。
  · 选股范围六条是"当前股本 × 历史价"重建的（见 pool_backtest 的三条近似）。
  · 一字板/开盘涨停买不进已处理；但排板能否成交仍然无法建模（乐观偏差）。

成本假设：佣金万 2.5 双边、印花税千 1（卖出）、滑点千 1 双边。
仓位：最多同时 3 只，每只 = 下单时净值 / 3；现金不足则放弃该笔。
卖出（三种纪律 × 两种入场 = 六组）：
  A 次日开盘即卖（打板口径）  B 次日收盘卖（T+1）  C 波动化止盈止损（+1.5×amp5 止盈 /
  −1.2×amp5 止损 / 最长 5 个交易日；同一日双触发按止损算，保守）
"""
import argparse
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app.config as cfg                                  # noqa: E402
from kline_forward import add_forward, limit_pct          # noqa: E402
import pool_backtest as PB                                # noqa: E402

DB = cfg.KLINE_HISTORY_DB_PATH
COMMISSION = 0.00025
STAMP = 0.001
SLIP = 0.001
sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def build_frames(since: str):
    """返回 (全局交易日列表, {symbol: 各列 ndarray})，数组按全局交易日对齐。"""
    conn = sqlite3.connect(DB, timeout=180)
    warm = (pd.Timestamp(since) - pd.Timedelta(days=260)).strftime('%Y-%m-%d')
    df = pd.read_sql('SELECT symbol, date, open, high, low, close, volume, turnover FROM stock_daily '
                     'WHERE date >= ? ORDER BY symbol, date', conn, params=(warm,))
    conn.close()
    for c in ('open', 'high', 'low', 'close', 'volume', 'turnover'):
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df = add_forward(df, win=5)
    df = PB.scale_forward_pct(df)
    df = PB.attach_shares(df)
    df = PB.build_signals(df)
    scope = df[PB.scope_mask(df)].copy()
    scope['rank_pct'] = scope.groupby('date')['r20'].rank(pct=True) * 100
    pool = scope[scope['date'] >= since]
    pool = pool[PB.pool_mask(pool)]
    pool = pool.sort_values(['date', 'r20'], ascending=[True, False])
    pool = pool[pool.groupby('date').cumcount() < cfg.CANDIDATE_POOL_MAX]

    dates = sorted(df['date'].unique())
    idx = {d: i for i, d in enumerate(dates)}
    cols = ['open', 'high', 'low', 'close', 'volume', 'turnover', 'chg', 'r20', 'amp5']
    # amp5：前 5 日平均振幅（不含当日），波动化止盈止损的尺度
    g = df.groupby('symbol', sort=False)
    rng = (df['low'] / df['high'] - 1).abs()
    amp = pd.Series(np.nan, index=df.index)
    tmp = df.assign(_rng=rng)
    amp = tmp.groupby('symbol', sort=False)['_rng'].transform(lambda s: s.shift(1).rolling(5).mean())
    df['amp5'] = amp
    arrays = {}
    for sym, sub in df.groupby('symbol', sort=False):
        pos = sub['date'].map(idx).to_numpy()
        if not len(pos):
            continue
        n = len(dates)
        rec = {c: np.full(n, np.nan) for c in cols}
        for c in cols:
            if c == 'amp5':
                rec[c][pos] = sub['amp5'].to_numpy()
            else:
                rec[c][pos] = sub[c].to_numpy()
        arrays[sym] = rec
    pool_by_day = {}
    for d, sub in pool.groupby('date'):
        pool_by_day[idx[d]] = [(row.symbol, row.r20) for row in sub.itertuples()]
    return dates, idx, arrays, pool_by_day


def simulate(dates, idx, arrays, pool_by_day, *, start_capital=100000.0, max_pos=3,
             entry='open', sell='A', first_day=None, last_day=None):
    """逐日滚动一个账户。返回终值与统计。"""
    t0 = idx[first_day] if first_day else 0
    t1 = idx[last_day] if last_day else len(dates) - 1
    cash = start_capital
    holds = {}            # symbol -> dict(shares, entry_px, entry_day, stop, target)
    equity_curve = []
    trades = []
    for t in range(t0, t1 + 1):
        # 1) 先处理已有持仓的卖出（T+1：入场日当天不卖）
        for sym in list(holds):
            h = holds[sym]
            if t <= h['day']:
                continue
            a = arrays[sym]
            o, hi, lo, c = a['open'][t], a['high'][t], a['low'][t], a['close'][t]
            if np.isnan(c):
                continue                       # 停牌：无法卖，留着
            px, why = None, ''
            if sell == 'A':
                px, why = o, '次日开盘'
            elif sell == 'B':
                px, why = c, '次日收盘'
            else:
                if h.get('stop') is not None and lo <= h['stop']:
                    px, why = h['stop'], '止损'
                elif h.get('target') is not None and hi >= h['target']:
                    px, why = h['target'], '止盈'
                elif t - h['day'] >= 5:
                    px, why = c, '到期'
            if px is None:
                continue
            proceeds = h['shares'] * px * (1 - COMMISSION - STAMP - SLIP)
            cost = h['shares'] * h['px'] * (1 + COMMISSION + SLIP)
            cash += proceeds
            trades.append({'sym': sym, 'ret_pct': (proceeds / cost - 1) * 100, 'why': why,
                           'days': t - h['day']})
            del holds[sym]

        # 2) 用"上一个交易日"的海选池决定今天买谁（生产是今早 9:00 出池）
        cand = pool_by_day.get(t - 1) or []
        nav = cash + sum(h['shares'] * (arrays[s][ 'close'][t] if not np.isnan(
            arrays[s]['close'][t]) else h['px']) for s, h in holds.items())
        budget = nav / max_pos
        for sym, _r20 in cand:
            if len(holds) >= max_pos:
                break
            if sym in holds:
                continue
            a = arrays.get(sym)
            if a is None:
                continue
            o, hi, lo, c, pc = a['open'][t], a['high'][t], a['low'][t], a['close'][t], a['close'][t - 1]
            if np.isnan(o) or np.isnan(c) or o <= 0:
                continue                                   # 当天停牌/无数据
            lim = limit_pct(sym) * 100
            px = o if entry == 'open' else c
            if entry == 'open':
                if pc and (o / pc - 1) * 100 >= lim - 0.2:
                    continue                               # 开盘即涨停，竞价买不到
            else:
                if hi == lo and pc and (c / pc - 1) * 100 >= lim - 0.2:
                    continue                               # 一字板，尾盘也买不到
            if cash < budget:
                break
            buy_px = px * (1 + SLIP)
            shares = budget / buy_px
            cash -= budget
            stop = target = None
            amp = a['amp5'][t]
            if sell == 'C' and amp and not np.isnan(amp) and amp > 0:
                stop = buy_px * (1 - 1.2 * amp)
                target = buy_px * (1 + 1.5 * amp)
            holds[sym] = {'shares': shares, 'px': buy_px, 'day': t, 'stop': stop, 'target': target}

        mark = cash + sum(h['shares'] * (arrays[s]['close'][t] if not np.isnan(arrays[s]['close'][t])
                                         else h['px']) for s, h in holds.items())
        equity_curve.append((dates[t], mark))

    eq = np.array([v for _, v in equity_curve])
    peak = np.maximum.accumulate(eq)
    mdd = ((eq / peak) - 1).min() * 100
    wins = [x['ret_pct'] for x in trades]
    return {'final': float(eq[-1]), 'ret_pct': (eq[-1] / start_capital - 1) * 100,
            'max_drawdown_pct': float(mdd), 'trades': len(trades),
            'win_rate': (sum(1 for w in wins if w > 0) / len(wins) * 100) if wins else 0.0,
            'avg_trade_pct': float(np.mean(wins)) if wins else 0.0,
            'open_positions': len(holds), 'curve': equity_curve}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--capital', type=float, default=100000)
    ap.add_argument('--months', type=int, default=3)
    ap.add_argument('--since', default=None, help='信号区间起点（默认按 --months 倒推）')
    ap.add_argument('--max-pos', type=int, default=3)
    a = ap.parse_args()
    since = a.since or (pd.Timestamp.now() - pd.DateOffset(months=a.months)).strftime('%Y-%m-%d')
    print(f'[模拟] 区间 {since} ~ 最新交易日，本金 {a.capital:,.0f} 元，最多同时 {a.max_pos} 只')
    dates, idx, arrays, pool_by_day = build_frames(since)
    print(f'[模拟] 交易日 {len(dates)} 天，池内有票的日子 {len(pool_by_day)} 个\n')
    hdr = (f'{"入场":6}{"卖出纪律":16}{"终值":>12}{"收益率":>9}{"最大回撤":>10}'
           f'{"笔数":>7}{"胜率":>8}{"单笔均值":>10}')
    print(hdr)
    for entry in ('open', 'close'):
        for sell, label in (('A', '次日开盘即卖'), ('B', '次日收盘(T+1)'), ('C', '波动化止盈止损')):
            r = simulate(dates, idx, arrays, pool_by_day, start_capital=a.capital,
                         max_pos=a.max_pos, entry=entry, sell=sell)
            en = '竞价' if entry == 'open' else '尾盘'
            print(f'{en:6}{label:16}{r["final"]:>12,.0f}{r["ret_pct"]:>8.2f}%'
                  f'{r["max_drawdown_pct"]:>9.1f}%{r["trades"]:>7}{r["win_rate"]:>7.1f}%'
                  f'{r["avg_trade_pct"]:>9.2f}%')
    return 0


if __name__ == '__main__':
    sys.exit(main())
