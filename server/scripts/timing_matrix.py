"""timing_matrix.py - 同一只票，你在一天里什么时候买，值多少钱（2026-10-16）

上一轮只给了一种买法（封板 + 次日开盘卖），太窄。这个脚本把**买入时点**单独称重量：
同一批形态的票，分别按四种成本算次日期望 ——

  · 竞价买   成本 = 当日开盘价（9:25 定价，你只能看到昨收和竞价）
  · 尾盘买   成本 = 当日收盘价（14:57 前后，全天信息都在手里）
  · 追高买   成本 = 当日最高价（日内冲到哪儿你追到哪儿，最贵的你）
  · 挂单低吸 成本 = 目标价，**只有当日最低价真的触及才算成交**（这点很关键：
             挂单成交本身就带选择偏差，跌到你价位的票往往是走弱的票）

形态分组只用"当时可见"的信息；日K没有分时，所以"10:00 回踩均价线"这类
盘中口径这里给不出，只能给开盘/收盘/最高/最低四个锚点 —— 这是本脚本的硬边界。

用法：python timing_matrix.py [--since 2025-01-01]
"""
import argparse
import sys

import numpy as np
import pandas as pd

from kline_forward import load_kline, market_breadth, apply_universe

sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def prep(since: str, only_universe: bool = True) -> pd.DataFrame:
    df = load_kline('2024-06-01', since)
    df = df.merge(market_breadth(df)[['date', 'adv', 'lim_cnt', 'zb_rate']], on='date', how='left')
    g = df.groupby('symbol', sort=False)
    df['prev_limit'] = g['limit_close'].shift(1).fillna(False)
    df['prev_chg'] = g['chg'].shift(1)
    df['prev_close2'] = g['close'].shift(2)
    df['half_hill'] = (df['prev_chg'] >= 0.045) & (df['prev_chg'] < 0.098) & ~df['prev_limit']
    df['gap_today'] = df['open'] / df['prev_close'] - 1              # 今日竞价溢价（买前可见）
    # kline_forward 只给收益率口径，这里还原成绝对价，方便和"任意成本"相除
    df['c1'] = df['close'] * (1 + df['fwd1'])
    df['o1'] = df['close'] * (1 + df['gap1'])
    df['c2'] = df['close'] * (1 + df['fwd2'])
    if only_universe:
        df = apply_universe(df)   # 市场层已在全市场上算过，这里只筛个股
    return df.dropna(subset=['chg', 'fwd1', 'gap1', 'open', 'high', 'low', 'prev_chg'])


def by_cost(df, mask, name):
    """同一批票，四种成本下的次日结果"""
    sub = df[mask & df['fwd1'].notna()].copy()
    n = len(sub)
    if n < 300:
        print(f'  {name:<34} 样本{n} 太少，忽略')
        return
    print(f'\n  {name}  ——样本 {n:,}')
    print(f'    {"成本口径":<14}{"成本相对昨收":>12}{"次日收盘卖出:均值":>18}{"胜率":>8}{"亏超5%":>9}'
          f'{"次日开盘就卖:均值":>18}{"胜率":>8}')
    rows = []
    # 只有三个可靠成本锚点：开盘价（竞价）、收盘价（尾盘）、最高/最低价（追高与抄底的边界）。
    # 日K给不出"9:30~10:00 的早盘价"——turnover/volume 是未复权均价，与前复权 OHLC 不同尺度
    # （实测仅 71.5% 的行落在当日 [最低,最高] 内），所以早盘/盘中只能用"介于竞价与尾盘之间"
    # 来界定，不假装有数。要做真正的分时口径，得引入分钟K。
    for lab, cost in (('竞价买(开盘)', sub['open']), ('尾盘买(收盘)', sub['close']),
                      ('追最高(上限)', sub['high']), ('买最低(下限,不可能)', sub['low'])):
        ok = cost.notna() & (cost > 0) & sub['c1'].notna()
        c_ = cost[ok]
        r_close = sub.loc[ok, 'c1'] / c_ - 1
        r_open = sub.loc[ok, 'o1'] / c_ - 1
        rows.append((lab, c_ / sub.loc[ok, 'prev_close'] - 1, r_close, r_open))
    # 挂单低吸：目标价=昨收 -2% / -4%，只有当日最低价跌到才成交
    for dip in (0.02, 0.04):
        target = sub['prev_close'] * (1 - dip)
        hit = sub['low'] <= target
        if hit.sum() >= 300:
            cost = target[hit]
            s2 = sub[hit]
            r_close = s2['c1'] / cost - 1
            r_open = s2['o1'] / cost - 1
            rows.append((f'挂-{int(dip*100)}%低吸(成交{hit.mean()*100:.0f}%)',
                         cost / sub['prev_close'].loc[hit] - 1, r_close, r_open))
    for lab, rel, r_close, r_open in rows:
        print(f'    {lab:<14}{rel.mean()*100:>11.2f}%{r_close.mean()*100:>17.2f}%'
              f'{(r_close > 0).mean()*100:>7.1f}%{(r_close <= -0.05).mean()*100:>8.1f}%'
              f'{r_open.mean()*100:>17.2f}%{(r_open > 0).mean()*100:>7.1f}%')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', default='2025-01-01')
    ap.add_argument('--all-market', action='store_true', help='不筛股票范围（默认只做沪深主板、剔除ST）')
    a = ap.parse_args()
    df = prep(a.since, only_universe=not a.all_market)
    print(f'样本 {df["date"].min()} ~ {df["date"].max()}，{len(df):,} 条；'
          f'全体基线：尾盘买次日 {(df["c1"]/df["close"]-1 > 0).mean()*100:.1f}%/'
          f'{(df["c1"]/df["close"]-1).mean()*100:.2f}%，'
          f'竞价买次日 {(df["c1"]/df["open"]-1 > 0).mean()*100:.1f}%/'
          f'{(df["c1"]/df["open"]-1).mean()*100:.2f}%')
    print('（"成本相对昨收"这一列告诉你：这个时点平均比昨收贵多少 / 便宜多少）')

    print('\n═ 形态 1：昨日封涨停（今天要买只能竞价或盘中追）')
    by_cost(df, df['prev_limit'], '昨日封板 → 今天各时点买入')
    print('\n  · 昨日封板，按今天竞价溢价分档（决定你到底该不该按竞价买）')
    lim = df[df['prev_limit'] & df['fwd1'].notna()]
    for lab, m in (('竞价低开 >3%', lim['gap_today'] < -0.03),
                   ('竞价 -3%~0', (lim['gap_today'] >= -0.03) & (lim['gap_today'] < 0)),
                   ('竞价 0~+2%', (lim['gap_today'] >= 0) & (lim['gap_today'] < 0.02)),
                   ('竞价 +2~+5%', (lim['gap_today'] >= 0.02) & (lim['gap_today'] < 0.05)),
                   ('竞价 +5~+8%', (lim['gap_today'] >= 0.05) & (lim['gap_today'] < 0.08)),
                   ('竞价 >+8%', lim['gap_today'] >= 0.08)):
        s = lim[m]
        if len(s) < 200:
            continue
        r = s['c1'] / s['open'] - 1
        ro = s['o1'] / s['open'] - 1
        print(f'    {lab:<16}样本{len(s):>7,}  竞价买持有到次日收盘均值{r.mean()*100:>6.2f}%  '
              f'胜率{(r > 0).mean()*100:>5.1f}%  亏超5%{(r <= -0.05).mean()*100:>5.1f}%  '
              f'再持有到第3日开盘{ro.mean()*100:>6.2f}%')

    print('\n═ 形态 2：昨日半山腰（涨 4.5~9.8% 没封板）——你三只票的共同形状')
    by_cost(df, df['half_hill'], '昨日半山腰 → 今天各时点买入')

    print('\n═ 形态 3：昨日巨量拉升（量比>3 且涨>5%）')
    prev_vr = df.groupby('symbol', sort=False)['vol_ratio'].shift(1)
    big_vol = (prev_vr > 3) & (df['prev_chg'] > 0.05)
    by_cost(df, big_vol.fillna(False), '昨日巨量拉升 → 今天各时点')

    print('\n═ 形态 4：横盘缩量（防守型，今天平静）')
    flat = ((df['chg'].abs() < 0.02) & df['vol_ratio'].between(0.7, 1.5)
            & ~df['touched_limit'].fillna(True) & df['above_ma20']
            & df['r20'].between(-0.05, 0.20))
    by_cost(df, flat, '今日横盘缩量 → 今天各时点')

    print('\n═ 形态 5：昨日跌 -5% 以上（今天要不要接飞刀）')
    by_cost(df, df['prev_chg'] <= -0.05, '昨日大面 → 今天各时点')

    print('\n═ 形态 6：波段口径——同一批票，持有 1/2/3/5/10 个交易日（成本=买入日收盘）')
    swing = ((df['above_ma20']) & df['chg'].between(0.01, 0.045) & df['vol_ratio'].between(1.0, 2.5)
             & df['r20'].between(0.05, 0.35) & (df['dist_hi60'] < -0.02))
    for label, mask in (('趋势温和上行（MA20上、20日涨5~35%、未贴前高）', swing),
                        ('半山腰（昨天涨4.5~9.8%没封板）', df['half_hill']),
                        ('昨日封板', df['prev_limit'])):
        s = df[mask]
        if len(s) < 500:
            continue
        print(f'  {label}  样本{len(s):,}')
        for k, col in ((1, 'fwd1'), (2, 'fwd2'), (3, 'fwd3'), (5, 'fwd5'), (10, 'fwd10')):
            r = s[col].dropna()
            if len(r) < 300:
                continue
            lo = s[f'lo{5 if k <= 5 else 10}'].dropna()
            print(f'    持有 {k:>2} 日：均值{r.mean()*100:>6.2f}%  胜率{(r > 0).mean()*100:>5.1f}%  '
                  f'中位{r.median()*100:>6.2f}%  最差5%分位{r.quantile(0.05)*100:>7.2f}%  '
                  f'期间最大不利中位{lo.median()*100:>7.2f}%')

    print('\n═ 时机本身值多少钱：全市场"开盘买 vs 收盘买"的差')
    d1 = (df['c1'] / df['open'] - 1)
    d2 = (df['c1'] / df['close'] - 1)
    print(f'  竞价买持有到次日收盘：均值{d1.mean()*100:.2f}% 胜率{(d1 > 0).mean()*100:.1f}%')
    print(f'  尾盘买持有到次日收盘：均值{d2.mean()*100:.2f}% 胜率{(d2 > 0).mean()*100:.1f}%')
    drift = df['close'] / df['open'] - 1
    print(f'  当日"开盘→收盘"漂移：均值{drift.mean()*100:.2f}%、中位{drift.median()*100:.2f}%，'
          f'上涨占比{(drift > 0).mean()*100:.1f}% —— 这就是尾盘买比竞价买省下的平均成本')
    up_day = df['prev_chg'] > 0.03
    print(f'  昨日涨>3% 的今天：开盘→收盘漂移均值{drift[up_day].mean()*100:.2f}%、'
          f'上涨占比{(drift[up_day] > 0).mean()*100:.1f}%（冲高票越早买越贵）')
    dn_day = df['prev_chg'] <= -0.05
    print(f'  昨日大面的今天：开盘→收盘漂移均值{drift[dn_day].mean()*100:.2f}%、'
          f'上涨占比{(drift[dn_day] > 0).mean()*100:.1f}%（飞刀票尾盘往往更贵，别以为等等就便宜）')


if __name__ == '__main__':
    main()
