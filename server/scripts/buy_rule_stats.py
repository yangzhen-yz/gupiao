"""buy_rule_stats.py - 把候选"买法"逐条放到全市场历史里称重量（2026-10-16）

只回答一个问题：**这套条件选出来的票，历史上实际是什么分布**。

口径（全部由 `kline_forward.py` 统一实现，且与朴素循环逐值自检一致）：
  · 决策时点只用当日及以前的信息；标"竞价买"的用次日开盘价 o1 作为成本
  · T+1 = 次日收盘相对买入价；T+2 同理
  · 5 日内大面 = 未来 5 个交易日内出现任一单日收盘跌幅 ≤ -5%
  · 冲高回落 = 未来 5 日内任一交易日"当日最高→最低"回落 ≥ 7%
  · 止盈/破位阈值按该票自己的 5 日平均振幅缩放，否则跨组根本没法比
    （"涨停票 100% 破 -4%"那种数字只是在说它波动大，不是在说它会亏）

用法：python buy_rule_stats.py [--since 2025-01-01]
"""
import argparse
import sys

import numpy as np
import pandas as pd

from kline_forward import load_kline, market_breadth, apply_universe

sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def prep(since: str, only_universe: bool = True) -> pd.DataFrame:
    """日K + 未来窗口 + 当日市场层 + 5 日平均振幅（不含当日）"""
    from kline_forward import _back_window
    df = load_kline('2024-06-01', since)
    # 市场层按全市场算（情绪口径不能跟着股票范围缩小），个股才按范围筛
    df = df.merge(market_breadth(df)[['date', 'adv', 'lim_cnt', 'zb_rate']], on='date', how='left')
    day_range = (df['low'] / df['high'] - 1).abs().to_numpy()
    amp = np.full(len(df), np.nan)
    for _, idx in df.groupby('symbol', sort=False).indices.items():
        idx = np.asarray(idx)
        amp[idx] = _back_window(day_range[idx], 5, 'mean')
    df['amp5'] = amp
    if only_universe:
        df = apply_universe(df)
    return df.dropna(subset=['chg', 'fwd1', 'lo5', 'vol_ratio', 'r20', 'dist_hi60'])


def report(df, mask, name, entry='close'):
    """entry='close' 以 T 日收盘为买入成本；'open' 以次日开盘为成本（竞价买）"""
    sub = df[mask & df['fwd1'].notna() & df['lo5'].notna()]
    n = len(sub)
    if n < 300:
        print(f'{name:<40} 样本{n} 太少，忽略')
        return
    if entry == 'close':
        r1 = sub['fwd1']
        r2 = sub['fwd2']
    else:
        r1 = (1 + sub['fwd1']) / (1 + sub['gap1']) - 1
        r2 = (1 + sub['fwd2']) / (1 + sub['gap1']) - 1
    amp = sub['amp5'].fillna(0.05).clip(lower=0.015, upper=0.20)
    tp = (sub['hi5'] >= np.maximum(0.05, 1.5 * amp))
    sl = (sub['lo5'] <= -np.maximum(0.04, 1.2 * amp))
    print(f'{name:<40}样本{n:>8,}  T+1胜率{(r1 > 0).mean()*100:>5.1f}%  T+1均值{r1.mean()*100:>6.2f}%'
          f'  T+2均值{r2.mean()*100:>6.2f}%  5日大面{(sub["fmin_chg5"] <= -0.05).mean()*100:>5.1f}%'
          f'  冲高回落{(sub["fall5"] <= -0.07).mean()*100:>5.1f}%'
          f'  5日最低中位{sub["lo5"].median()*100:>6.2f}%'
          f'  只达止盈:只破位={(tp & ~sl).sum()}:{(sl & ~tp).sum()}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', default='2025-01-01')
    ap.add_argument('--all-market', action='store_true', help='不筛股票范围（默认只做沪深主板、剔除ST）')
    a = ap.parse_args()
    df = prep(a.since, only_universe=not a.all_market)
    print(f'样本 {df["date"].min()} ~ {df["date"].max()}  共 {len(df):,} 条股票-交易日')
    stat = (df['fwd1'] > 0).mean() * 100
    print(f'基线：T+1 胜率 {stat:.1f}%、均值 {df["fwd1"].mean()*100:.2f}%、'
          f'5 日内大面 {(df["fmin_chg5"] <= -0.05).mean()*100:.1f}%')
    calm = (df['lim_cnt'] <= 120) & (df['zb_rate'] <= 0.50)

    print('\n【最容易踩的几类（你三只票的共同形状）】')
    report(df, df['chg'].between(0.045, 0.098) & ~df['limit_close'], '半山腰 4.5~9.8% 没封板 尾盘买')
    report(df, df['zhaban'], '当天炸板 尾盘买')
    report(df, (df['vol_ratio'] > 3) & (df['chg'] > 0.05), '巨量拉升（量比>3 且涨>5%）')
    report(df, df['r20'] > 0.60, '20 日涨幅>60% 的过热票')
    report(df, df['dist_hi60'].between(0, 0.03), '贴着 60 日高点（0~3%）')

    print('\n【买法A：封涨停当天买（打板/排板），次日怎么卖最实在】')
    lim = df['limit_close'] & df['hi1'].notna()
    base = df['close']
    print('\n  · 卖出方式对比（封板票，成本=T 日收盘）')
    for lab, r in (('次日开盘即卖', df.loc[lim, 'gap1']),
                   ('挂 +2% 限价,不到则收盘卖', None),
                   ('挂 +3% 限价,不到则收盘卖', None),
                   ('挂 +5% 限价,不到则收盘卖', None),
                   ('持有到次日收盘', df.loc[lim, 'fwd1']),
                   ('持有到第 3 日收盘', df.loc[lim, 'fwd2'])):
        s = df.loc[lim]
        if r is None:
            x = float(lab.split('+')[1].split('%')[0]) / 100
            r = pd.Series(np.where(s['hi1'] >= x, x, s['fwd1']), index=s.index)
        print(f'    {lab:<26}均值{r.mean()*100:>6.2f}%  胜率{(r > 0).mean()*100:>5.1f}%  '
              f'中位{r.median()*100:>6.2f}%  亏超5%{(r <= -0.05).mean()*100:>5.1f}%')
    hi3 = (df.loc[lim, 'hi1'] >= 0.03).mean() * 100
    print(f'    次日最高价能给到 +3% 的比例：{hi3:.1f}%（这是"冲高即卖"能不能兑现的关键）')

    print('\n【连板梯队：第几板的接力真有肉、次日晋级率多少】')
    for lab, m in (('首板', lim & (df['board_n'] == 1)), ('二板', lim & (df['board_n'] == 2)),
                   ('三板', lim & (df['board_n'] == 3)), ('四板及以上', lim & (df['board_n'] >= 4))):
        s = df[m & df['gap1'].notna()]
        if len(s) < 300:
            continue
        promote = s['limit_close'].iloc[1:].mean()   # 只作粗略参考，精确晋级率见下
        r1 = s['fwd1']
        print(f'  {lab:<12}样本{len(s):>7,}  T+1胜率{(r1 > 0).mean()*100:>5.1f}%  均值{r1.mean()*100:>6.2f}%  '
              f'开盘溢价中位{s["gap1"].median()*100:>5.2f}%  竞价≤+2%可买到占{(s["gap1"] <= 0.02).mean()*100:>5.1f}%'
              f'  5日大面{(s["fmin_chg5"] <= -0.05).mean()*100:>5.1f}%')
    nxt_lim = df.groupby('symbol', sort=False)['limit_close'].shift(-1)
    up = df[lim].copy()
    up['promote'] = nxt_lim.reindex(up.index).fillna(False)
    for lab, m in (('首板→次日再封', up['board_n'] == 1), ('二板→次日再封', up['board_n'] == 2),
                   ('三板→次日再封', up['board_n'] == 3)):
        s = up[m]
        if len(s) > 200:
            print(f'  {lab:<12}次日继续封板的比例 {s["promote"].mean()*100:.1f}%')

    print('\n【买法B：防守型低吸（横盘缩量）与它的过滤】')
    b = (df['chg'].abs() < 0.02) & df['vol_ratio'].between(0.7, 1.5) & ~df['touched_limit'] & (df['up_days'] <= 2) \
        if 'up_days' in df else (df['chg'].abs() < 0.02) & df['vol_ratio'].between(0.7, 1.5) & ~df['touched_limit']
    report(df, b, 'B 横盘缩量')
    report(df, b & calm, 'B + 情绪闸门')
    report(df, b & df['above_ma20'] & df['r20'].between(0, 0.15), 'B + 站上MA20 + 20日涨0~15%')
    report(df, b & (df['amp5'] > 0.05), 'B 但 5 日振幅>5%（活跃票假装平静）')

    print('\n【买法C：突破 60 日新高当天】')
    c = (df['close'] >= df['hi60_prev']) & df['chg'].between(0.02, 0.06) & df['vol_ratio'].between(1.0, 2.5)
    report(df, c, 'C 突破日尾盘买')
    report(df, c & df['r20'].between(0, 0.25), 'C + 未过热（20日涨<25%）')

    print('\n【最终买法：把条件全叠上，只留次日开盘即卖】')
    # 非一字：开盘价没顶在涨停板上，否则你根本买不到，历史收益是假的
    buyable = df['open'] < df['prev_close'] * (1 + df['limit_pct'] * 0.985)
    cond = (df['limit_close'] & buyable & (df['board_n'] <= 2)
            & (df['r20'] <= 0.40) & (df['vol_ratio'] <= 3) & ~df['zhaban'])
    for yr in ('2025', '2026'):
        s = df[cond & df['date'].str[:4].eq(yr) & df['gap1'].notna()]
        if len(s) < 200:
            continue
        r = s['gap1']
        days = s['date'].nunique()
        print(f'  {yr}  样本{len(s):>6,}（约 {days} 个交易日、平均每天 {len(s)/max(days,1):.0f} 只全市场机会）  '
              f'开盘卖均值{r.mean()*100:>5.2f}%  胜率{(r > 0).mean()*100:>5.1f}%  中位{r.median()*100:>5.2f}%  '
              f'亏超3%{(r <= -0.03).mean()*100:>5.1f}%  亏超5%{(r <= -0.05).mean()*100:>5.1f}%')
    s = df[cond & df['gap1'].notna()]
    r = s['gap1']
    for fee in (0.0, 0.0013, 0.002):
        rr = r - fee
        print(f'  合计（扣单边/双边费用 {fee*100:.2f}%）：均值{rr.mean()*100:>5.2f}%  胜率{(rr > 0).mean()*100:>5.1f}%  '
              f'中位{rr.median()*100:>5.2f}%  最差 5% 分位{rr.quantile(0.05)*100:>6.2f}%')
    print('\n  · 同一批票如果"不等开盘、直接持有到次日收盘"（也就是舍不得卖）')
    rr = s['fwd1']
    print(f'    均值{rr.mean()*100:>5.2f}%  胜率{(rr > 0).mean()*100:>5.1f}%  '
          f'亏超5%{(rr <= -0.05).mean()*100:>5.1f}%  最差 5% 分位{rr.quantile(0.05)*100:>6.2f}%')
    print('\n  · 对照：半山腰 4.5~9.8% 没封板，同样次日开盘卖（你现在的习惯）')
    mid = df[df['chg'].between(0.045, 0.098) & ~df['limit_close'] & df['gap1'].notna()]
    r3 = mid['gap1']
    print(f'    均值{r3.mean()*100:>5.2f}%  胜率{(r3 > 0).mean()*100:>5.1f}%  '
          f'亏超5%{(r3 <= -0.05).mean()*100:>5.1f}%  次日最高给到+3%的{(mid["hi1"] >= 0.03).mean()*100:>5.1f}%')

    print('\n【市场闸门：情绪极热/极冷时，所有买入动作的成色】')
    for lab, m in (('涨停 ≤50 家（冰点/退潮）', df['lim_cnt'] <= 50),
                   ('涨停 50~120 家（正常）', (df['lim_cnt'] > 50) & (df['lim_cnt'] <= 120)),
                   ('涨停 120~250 家（偏热）', (df['lim_cnt'] > 120) & (df['lim_cnt'] <= 250)),
                   ('涨停 >250 家（极热）', df['lim_cnt'] > 250),
                   ('炸板率 ≤30%', df['zb_rate'] <= 0.30),
                   ('炸板率 30~50%', df['zb_rate'].between(0.30, 0.50)),
                   ('炸板率 >50%（承接差）', df['zb_rate'] > 0.50)):
        s = df[m & df['fwd1'].notna()]
        if len(s) < 500:
            continue
        print(f'  {lab:<26}样本{len(s):>9,}  T+1胜率{(s["fwd1"] > 0).mean()*100:>5.1f}%  '
              f'均值{s["fwd1"].mean()*100:>6.2f}%  5日大面{(s["fmin_chg5"] <= -0.05).mean()*100:>5.1f}%')
    print('\n  · 同一动作（封板接力）在不同情绪档的成色')
    for lab, m in (('冰点/退潮（涨停≤50）', df['lim_cnt'] <= 50),
                   ('正常（50~120）', (df['lim_cnt'] > 50) & (df['lim_cnt'] <= 120)),
                   ('偏热/极热（>120）', df['lim_cnt'] > 120)):
        s = df[lim & m & df['gap1'].notna()]
        if len(s) < 300:
            continue
        r = s['gap1']    # 开盘即卖
        print(f'    {lab:<22}样本{len(s):>7,}  开盘即卖均值{r.mean()*100:>6.2f}%  '
              f'胜率{(r > 0).mean()*100:>5.1f}%  亏超5%{(r <= -0.05).mean()*100:>5.1f}%  '
              f'5日大面{(s["fmin_chg5"] <= -0.05).mean()*100:>5.1f}%')


if __name__ == '__main__':
    main()
