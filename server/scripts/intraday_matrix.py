"""intraday_matrix.py - 用 5 分钟真实成交均价，把"早盘买/盘中买/尾盘买"称出来（2026-10-16）

日K只有 开/高/低/收 四个锚点，所以"9:30~10:00 到底买在什么价"一直没有真数 ——
`成交额/成交量` 是未复权均价，与前复权 OHLC 不同尺度（实测仅 71.5% 落在当日高低区间内），
拿来当成本会系统性骗人。本脚本改用 baostock 的 **5 分钟前复权分钟线**（已与本地日K逐价对齐：
末根收盘 == 日K收盘、首根开盘 == 日K开盘），对每种形态的事件，算六个真实时点的
价格加权均价作为买入成本，再看三种卖出的结果。

成本口径（同一事件，六个时点各买一次）：
  9:25 集合竞价   = 当日开盘价
  9:30~9:35      = 头五分钟成交均价（最冲的一批人）
  9:35~10:00     = 早盘其余时段
  10:00~11:30    = 上午后半段
  13:00~14:00    = 午后第一段
  14:30~15:00    = 尾盘（看见全天形态之后才买）

卖出：当日收盘 / 次日开盘 / 次日收盘。

用法：python intraday_matrix.py [--per-form 400] [--since 2025-01-01]
第一次跑会去拉分钟线（约 0.6 秒/事件），之后复用 data/history_minute.db 缓存。
"""
import argparse
import os
import random
import sys
import time
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kline_forward import load_kline, market_breadth, apply_universe  # noqa: E402
from services.data.minute_history import get_day, session_price, stats, logout  # noqa: E402

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# (标签, 段起始分钟标, 段结束分钟标) —— baostock 的 time 标的是"这段结束的时刻"
BUCKETS = (
    ('9:25竞价', None, None),
    ('9:30~9:35', '093500', '093500'),
    ('9:35~10:00', '094000', '100000'),
    ('10:00~11:30', '100500', '113000'),
    ('13:00~14:00', '130500', '140000'),
    ('14:30~15:00', '143500', '150000'),
)


def prep(since: str, only_universe: bool = True) -> pd.DataFrame:
    df = load_kline('2024-06-01', since)
    df = df.merge(market_breadth(df)[['date', 'adv', 'lim_cnt', 'zb_rate']], on='date', how='left')
    g = df.groupby('symbol', sort=False)
    df['prev_chg'] = g['chg'].shift(1)
    df['prev_limit'] = g['limit_close'].shift(1).fillna(False)
    df['prev_vr'] = g['vol_ratio'].shift(1)
    df['half_hill'] = ((df['prev_chg'] >= 0.045) & (df['prev_chg'] < 0.098) & ~df['prev_limit'])
    df['big_vol'] = (df['prev_vr'] > 3) & (df['prev_chg'] > 0.05)
    df['big_drop'] = df['prev_chg'] <= -0.05
    df['quiet_flat'] = ((df['chg'].abs() < 0.02) & df['vol_ratio'].between(0.7, 1.5)
                        & ~df['touched_limit'].fillna(True) & df['above_ma20']
                        & df['r20'].between(-0.05, 0.20))
    df['c1'] = df['close'] * (1 + df['fwd1'])
    df['o1'] = df['close'] * (1 + df['gap1'])
    if only_universe:
        df = apply_universe(df)
    return df.dropna(subset=['chg', 'fwd1', 'open', 'c1', 'o1', 'prev_chg'])


def pick(df: pd.DataFrame, mask, n: int, rng: random.Random) -> pd.DataFrame:
    s = df[mask]
    if len(s) <= n:
        return s
    idx = rng.sample(list(s.index), n)
    return s.loc[sorted(idx)]


def run(events: pd.DataFrame) -> pd.DataFrame:
    """对每个事件取当天分钟线，算六个时点的真实成交均价"""
    t0 = time.time()
    recs = []
    miss = 0
    for i, (sym, date, o, c, c1, o1) in enumerate(
            events[['symbol', 'date', 'open', 'close', 'c1', 'o1']].itertuples(index=False, name=None)):
        rows = get_day(sym, date)
        if len(rows) < 20:
            miss += 1
            continue
        price_map = {'9:25竞价': o}
        for lab, a, b in BUCKETS[1:]:
            p = session_price(rows, a, b)
            if p:
                price_map[lab] = p
        recs.append({'symbol': sym, 'date': date, 'open': o, 'close': c,
                     'c1': c1, 'o1': o1, **price_map})
        if (i + 1) % 100 == 0:
            print(f'  …已取 {i + 1} 个事件的分钟线，耗时 {time.time() - t0:.0f}s，'
                  f'缺数据 {miss}')
    print(f'分钟线取样完成：{len(recs)} 个事件（缺 {miss}），耗时 {time.time() - t0:.0f}s')
    return pd.DataFrame(recs)


def report(ev: pd.DataFrame, label: str):
    if ev.empty or len(ev) < 30:
        print(f'\n【{label}】样本不足（{len(ev)}），跳过')
        return
    print(f'\n【{label}】事件数 {len(ev):,}')
    print(f'  {"买入时点":<12}{"均价/开盘":>10}{"成本比开盘":>11}'
          f'{"当日收盘卖 均值":>16}{"胜率":>7}{"次日开盘卖 均值":>16}{"胜率":>7}'
          f'{"次日收盘卖 均值":>16}{"胜率":>7}{"次日收盘亏超5%":>14}')
    for lab, _, _ in BUCKETS:
        if lab not in ev.columns:
            continue
        s = ev.dropna(subset=[lab])
        cost = s[lab]
        if len(s) < 10:
            continue
        r_day = s['close'] / cost - 1
        r_no = s['o1'] / cost - 1
        r_nc = s['c1'] / cost - 1
        print(f'  {lab:<12}{cost.median():>10.2f}{(cost / s["open"] - 1).mean()*100:>10.2f}%'
              f'{r_day.mean()*100:>15.2f}%{(r_day > 0).mean()*100:>6.1f}%'
              f'{r_no.mean()*100:>15.2f}%{(r_no > 0).mean()*100:>6.1f}%'
              f'{r_nc.mean()*100:>15.2f}%{(r_nc > 0).mean()*100:>6.1f}%'
              f'{(r_nc <= -0.05).mean()*100:>13.1f}%')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--per-form', type=int, default=400)
    ap.add_argument('--since', default='2025-01-01')
    ap.add_argument('--seed', type=int, default=20261016)
    ap.add_argument('--all-market', action='store_true', help='不筛股票范围（默认只做沪深主板、剔除ST）')
    a = ap.parse_args()
    rng = random.Random(a.seed)
    df = prep(a.since, only_universe=not a.all_market)
    print(f'日线样本 {df["date"].min()} ~ {df["date"].max()}，{len(df):,} 条')

    forms = (
        ('形态1 昨日封板（今天要接力）', df['prev_limit']),
        ('形态2 昨日半山腰 4.5~9.8% 未封板', df['half_hill']),
        ('形态3 昨日巨量拉升（量比>3 且涨>5%）', df['big_vol']),
        ('形态4 昨日大面 -5% 以上（今天想抄）', df['big_drop']),
        ('形态5 当日横盘缩量（防守型低吸）', df['quiet_flat']),
    )
    for label, mask in forms:
        sel = pick(df, mask, a.per_form, rng)
        if len(sel) < 30:
            print(f'\n【{label}】候选太少，跳过')
            continue
        print(f'\n── {label}：从 {int(mask.sum()):,} 个事件里抽 {len(sel)} 个取分钟线')
        ev = run(sel)
        report(ev, label)
    print('\n分钟库现状：', stats())
    logout()


if __name__ == '__main__':
    main()
