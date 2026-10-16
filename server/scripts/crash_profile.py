"""crash_profile.py - "为什么我买的老是大跳水"的数据回答（2026-10-16）

用法：python crash_profile.py [--since 2025-01-01]

只回答一件事：**在买入那一刻能看到的东西**，哪些真的预示接下来会跳水。
特征全部来自当日及以前（收盘、量比、距 60 日高、20 日涨幅、连涨天数、是否封板/炸板），
结果全部来自未来（次日、5 日内），口径由 `kline_forward.py` 统一实现并自检。

三个"坏事"的定义：
  · 大面     ：未来 5 日内任一交易日单日收盘跌幅 ≤ -5%
  · 冲高回落 ：未来 5 日内任一交易日"当日最高→最低"回落 ≥ -7%
  · 次日低开 ：次日开盘价相对买入价 ≤ -2%

所有百分比都是历史频率，不是承诺。样本 500 多万条"股票-交易日"，分组都打印样本数。
"""
import argparse
import sys

import pandas as pd

from kline_forward import load_kline, market_breadth, apply_universe

sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def prep(since: str, only_universe: bool = True) -> pd.DataFrame:
    df = load_kline('2024-06-01', since)
    # 市场层（涨停家数/炸板率/上涨占比）永远按**全市场**算：情绪温度计看的就是整个市场，
    # 股票范围只约束"能在哪些票上动手"，不能把情绪口径也缩小。
    b = market_breadth(df)
    df = df.merge(b[['date', 'adv', 'lim_cnt', 'zb_rate']], on='date', how='left')
    if only_universe:
        df = apply_universe(df)
    return df.dropna(subset=['chg', 'fwd1', 'lo5', 'vol_ratio', 'r20', 'dist_hi60'])


def grp_table(df, col, bins, labels, title):
    d = df.copy()
    d['_g'] = pd.cut(d[col], bins=bins, labels=labels)
    print(f'\n── 按{title}')
    print(f'{"区间":<18}{"样本":>10}{"T+1胜率":>9}{"T+1均值":>9}{"5日大面":>9}{"冲高回落":>9}{"次日低开2%":>11}{"5日最低中位":>12}')
    for lab, s in d.groupby('_g', observed=True):
        n = len(s)
        if n < 300:
            continue
        print(f'{str(lab):<18}{n:>10,}{(s["fwd1"] > 0).mean()*100:>8.1f}%{s["fwd1"].mean()*100:>8.2f}%'
              f'{(s["fmin_chg5"] <= -0.05).mean()*100:>8.1f}%{(s["fall5"] <= -0.07).mean()*100:>8.1f}%'
              f'{(s["gap1"] <= -0.02).mean()*100:>10.1f}%{s["lo5"].median()*100:>11.2f}%')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', default='2025-01-01')
    ap.add_argument('--all-market', action='store_true', help='不筛股票范围（默认只做沪深主板、剔除ST）')
    a = ap.parse_args()
    df = prep(a.since, only_universe=not a.all_market)
    print(f'样本 {df["date"].min()} ~ {df["date"].max()}  共 {len(df):,} 条股票-交易日，'
          f'{df["symbol"].nunique()} 只')
    print(f'基线：T+1 胜率 {(df["fwd1"] > 0).mean()*100:.1f}%、均值 {df["fwd1"].mean()*100:.2f}%；'
          f'5 日内吃大面 {(df["fmin_chg5"] <= -0.05).mean()*100:.1f}%；'
          f'次日低开 2%+ {(df["gap1"] <= -0.02).mean()*100:.1f}%；'
          f'未来 5 日最低中位 {df["lo5"].median()*100:.2f}%')

    grp_table(df, 'chg', [-1, -0.02, 0, 0.02, 0.045, 0.07, 0.098, 0.19, 0.205, 1],
              ['<-2%', '-2~0', '0~2%', '2~4.5%', '4.5~7%', '7~9.8%', '准涨停9.8~19%', '19~20.5%', '>20.5%'],
              '当日涨幅（半山腰就是这里）')
    grp_table(df, 'dist_hi60', [-1, -0.25, -0.15, -0.07, -0.02, 0.001, 0.03, 1],
              ['<-25%', '-25~-15%', '-15~-7%', '-7~-2%', '-2~0%', '贴前高0~3%', '新高>3%'],
              '距 60 日高点')
    grp_table(df, 'r20', [-1, -0.1, 0, 0.1, 0.2, 0.35, 0.6, 10],
              ['<-10%', '-10~0', '0~10%', '10~20%', '20~35%', '35~60%', '>60%过热'],
              '20 日涨幅（已经涨了多久）')
    grp_table(df, 'vol_ratio', [0, 0.7, 1.2, 2, 3, 5, 1000],
              ['<0.7缩量', '0.7~1.2', '1.2~2', '2~3', '3~5巨量', '>5天量'],
              '量比')
    grp_table(df, 'board_n', [-0.5, 0.5, 1.5, 2.5, 3.5, 100],
              ['0未封板', '1首板', '2二板', '3三板', '4板+'], '连板层级')

    print('\n── 按当日封板形态')
    for name, mask in (('收盘封住涨停', df['limit_close']),
                       ('炸板（触板没封住）', df['zhaban']),
                       ('涨 4.5~9.8% 且没封板（半山腰）', (df['chg'] >= 0.045) & (df['chg'] < 0.098) & ~df['limit_close']),
                       ('冲高回落≥5%但收盘仍红', (df['low'] / df['high'] - 1 <= -0.05) & (df['chg'] > 0)),
                       ('|涨幅|<1%（平盘）', df['chg'].abs() < 0.01)):
        s = df[mask]
        if len(s) < 300:
            continue
        print(f'  {name:<26}样本{len(s):>9,}  T+1胜率{(s["fwd1"] > 0).mean()*100:>5.1f}%  '
              f'均值{s["fwd1"].mean()*100:>6.2f}%  5日大面{(s["fmin_chg5"] <= -0.05).mean()*100:>5.1f}%  '
              f'冲高回落{(s["fall5"] <= -0.07).mean()*100:>5.1f}%  5日最低中位{s["lo5"].median()*100:>6.2f}%')

    print('\n── 按当天市场层（先情绪后个股）')
    for title, col, bins, labels in (
            ('涨停家数', 'lim_cnt', [0, 20, 50, 90, 120, 250, 1e9], ['≤20', '20~50', '50~90', '90~120', '120~250', '>250']),
            ('炸板率', 'zb_rate', [0, .25, .4, .55, .7, 1], ['≤25%', '25~40%', '40~55%', '55~70%', '>70%']),
            ('上涨家数占比', 'adv', [0, .35, .45, .55, .65, 1], ['≤35%', '35~45%', '45~55%', '55~65%', '>65%'])):
        print(f'  · 按{title}')
        d = df.copy()
        d['_g'] = pd.cut(d[col], bins=bins, labels=labels)
        for lab, s in d.groupby('_g', observed=True):
            if len(s) < 500:
                continue
            print(f'    {str(lab):<10}样本{len(s):>9,}  T+1胜率{(s["fwd1"] > 0).mean()*100:>5.1f}%  '
                  f'均值{s["fwd1"].mean()*100:>6.2f}%  5日大面{(s["fmin_chg5"] <= -0.05).mean()*100:>5.1f}%')


if __name__ == '__main__':
    main()
