"""rule_filter_check.py - 用系统自己的历史推荐，检验"负面清单 + 两类买点"值多少钱（2026-10-16）

为什么单独做这件事：全市场统计（crash_profile.py / buy_rule_stats.py）能说明形态本身的
好坏，但真正要回答的是 —— **把这套规则加到你自己的推荐流程上，胜率与回撤会变多少**。
所以这里只用 `daily_recommendations` 里真实推过的票，按推荐当天可见的特征切片，
再用本地日K算它们之后的实际走势。

用法：python rule_filter_check.py
"""
import sqlite3
import sys

import pandas as pd

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

MAIN_DB = r'D:\Desktop\guizhou\gupiao\server\gupiao.db'
KLINE_DB = r'D:\Desktop\guizhou\gupiao\server\data\history_kline.db'


def limit_pct(code: str) -> float:
    return 0.20 if code.startswith(('30', '68')) else 0.10


def load():
    m = sqlite3.connect(MAIN_DB)
    rec = pd.read_sql('SELECT rec_date, symbol, name, price, change_percent, score, '
                      'buy_price, buy_timing, sort_order FROM daily_recommendations '
                      'ORDER BY rec_date', m)
    m.close()
    k = sqlite3.connect(KLINE_DB)
    kl = pd.read_sql('SELECT symbol, date, open, high, low, close, volume FROM stock_daily '
                     'WHERE date >= "2024-06-01" ORDER BY symbol, date', k)
    k.close()
    rec['code'] = rec['symbol'].str[-6:]
    kl['code'] = kl['symbol'].str[-6:]
    g = kl.groupby('code', sort=False)
    kl['chg'] = kl['close'] / g['close'].shift(1) - 1
    kl['lim'] = kl['code'].map(limit_pct)
    kl['limit_close'] = kl['chg'] >= kl['lim'] * 0.985
    kl['touched'] = (kl['high'] / g['close'].shift(1) - 1) >= kl['lim'] * 0.985
    kl['zhaban'] = kl['touched'] & ~kl['limit_close']
    kl['vol_ratio'] = kl['volume'] / g['volume'].transform(lambda s: s.rolling(5).mean().shift(1))
    kl['hi60'] = g['high'].transform(lambda s: s.rolling(60).max().shift(1))
    kl['dist_hi60'] = kl['close'] / kl['hi60'] - 1
    kl['r20'] = kl['close'] / g['close'].shift(20) - 1
    # 推荐日之后的实际走势
    kl['fwd1'] = g['close'].shift(-1) / kl['close'] - 1
    kl['fwd3'] = g['close'].shift(-3) / kl['close'] - 1
    kl['fwd5'] = g['close'].shift(-5) / kl['close'] - 1
    kl['open_gap1'] = g['open'].shift(-1) / kl['close'] - 1
    kl['min_chg5'] = g['chg'].transform(lambda s: s.shift(-1).rolling(5, min_periods=1).min())
    kl['worst_fall5'] = (kl['low'] / kl['high'] - 1)
    kl['worst_fall5'] = kl.groupby('code', sort=False)['worst_fall5'].transform(
        lambda s: s.shift(-1).rolling(5, min_periods=1).min())

    cols = ['code', 'date', 'chg', 'limit_close', 'touched', 'zhaban', 'vol_ratio',
            'dist_hi60', 'r20', 'fwd1', 'fwd3', 'fwd5', 'open_gap1', 'min_chg5', 'worst_fall5']
    df = rec.merge(kl[cols], left_on=['code', 'rec_date'], right_on=['code', 'date'], how='inner')
    # 库里 price/buy_price 是文本列，不转数字没法算
    for c in ('price', 'buy_price', 'change_percent', 'score'):
        df[c] = pd.to_numeric(df[c], errors='coerce')
    print(f'可复盘样本 {len(df)} 条（推荐 {len(rec)} 条，其中 {df["rec_date"].nunique()} 个交易日有对应K线）'
          f'，区间 {df["rec_date"].min()} ~ {df["rec_date"].max()}')
    return df


def stat(sub, label, base=None):
    n = len(sub)
    if n == 0:
        return
    f1, f3 = sub['fwd1'].dropna(), sub['fwd3'].dropna()
    t1 = f' T+1均值{f1.mean()*100:>6.2f}%' if len(f1) else ''
    t3 = f'  T+3均值{f3.mean()*100:>6.2f}%' if len(f3) else ''
    win = f'{(f1 > 0).mean()*100:>5.1f}%' if len(f1) else '  n/a'
    line = (f'{label:<30}样本{n:>4}  T+1胜率{win}{t1}{t3}  '
            f'5日内大面{(sub["min_chg5"] <= -0.05).mean()*100:>5.1f}%  '
            f'冲高回落7%+{(sub["worst_fall5"] <= -0.07).mean()*100:>5.1f}%')
    if base is not None and len(base):
        b1 = base['fwd1'].dropna()
        line += f'   （对照全部推荐 T+1 {(b1 > 0).mean()*100:.1f}%/{b1.mean()*100:+.2f}%）'
    print(line)


def main():
    df = load()
    stat(df, '全部推荐（基线）')
    print('\n── 按推荐当天涨幅（就是推荐卡上那个"涨幅"）')
    for lab, m in (('涨幅 <2%', df['chg'] < 0.02),
                   ('2% ~ 4.5%', df['chg'].between(0.02, 0.045)),
                   ('4.5% ~ 7%（半山腰）', df['chg'].between(0.045, 0.07)),
                   ('7% ~ 9.8%', df['chg'].between(0.07, 0.098)),
                   ('≥9.8%（涨停/准涨停）', df['chg'] >= 0.098)):
        stat(df[m], lab, df)

    print('\n── 按 20 日涨幅（这只票已经涨了多久）')
    for lab, m in (('20日涨幅 <20%', df['r20'] < 0.20),
                   ('20% ~ 40%', df['r20'].between(0.20, 0.40)),
                   ('40% ~ 70%', df['r20'].between(0.40, 0.70)),
                   ('>70%（严重过热）', df['r20'] > 0.70)):
        stat(df[m], lab, df)

    print('\n── 负面清单逐条（你的三只票落在哪几条）')
    neg = ((df['chg'].between(0.045, 0.098) & ~df['limit_close'])   # 半山腰没封板
           | df['zhaban'].fillna(False)                              # 当天炸板
           | (df['r20'] > 0.40)                                      # 20日已涨40%+
           | (df['vol_ratio'] > 3))                                  # 巨量
    stat(df[neg], '命中≥1条负面')
    stat(df[~neg], '负面全清')
    stat(df[df['zhaban'].fillna(False)], '当天炸过板')
    stat(df[df['r20'] > 0.70], '20日涨幅>70%')

    print('\n── 两类可买口径（低吸参考价 vs 现价追入）')
    d = df.dropna(subset=['buy_price', 'price', 'open_gap1', 'fwd1']).copy()
    d['dip_target'] = d['buy_price'] / d['price'] - 1          # 低吸参考价相对推荐价的折价
    d = d[(d['dip_target'] > -0.15) & (d['dip_target'] < 0.10)]  # 剔除异常报价
    stat(d[d['open_gap1'] <= d['dip_target']], '次日竞价能成交低吸单')
    stat(d[d['open_gap1'] > 0.03], '次日高开>3%（追不得）')

    print('\n── 组合买法（负面全清 + 只留两类买点）')
    neg_mask = ((df['chg'].between(0.045, 0.098) & ~df['limit_close'])
                | df['zhaban'].fillna(False) | (df['r20'] > 0.40) | (df['vol_ratio'] > 3))
    clean = ~neg_mask
    a = df[clean & df['limit_close']]                       # A 类：封板/涨停（接力或排板）
    b = df[clean & df['chg'].between(0.005, 0.045) & (df['dist_hi60'] < -0.03)]  # B 类：温和启动且没贴前高
    stat(a, 'A 类 封板接力（干净票里）')
    stat(b, 'B 类 温和启动（涨0.5~4.5%、距前高≥3%）')
    stat(pd.concat([a, b]), 'A+B 合并 = 这套买法实际会留下的票')
    stat(df[neg_mask & (df['r20'] > 0.40)], '同时命中"半山腰/炸板/过热"里两条以上的')

    print('\n── 每类的次日开盘溢价（决定你实际能不能买到、买得贵不贵）')
    for lab, sub in (('全部推荐', df), ('A 类 封板', a), ('B 类 温和启动', b)):
        if len(sub) < 10:
            continue
        gap = sub['open_gap1'].dropna()
        print(f'  {lab:<14}样本{len(sub):>4}  次日开盘溢价中位{gap.median()*100:>6.2f}%  '
              f'开盘就≥+2%（追高成本）的占{(gap >= 0.02).mean()*100:>5.1f}%  '
              f'次日绿盘开盘（可低吸）的占{(gap <= 0).mean()*100:>5.1f}%')

    print('\n── 全市场当天状态（推荐日的涨停家数代理：当日样本里封板占比）')
    hot = df.groupby('rec_date')['limit_close'].transform('mean')
    stat(df[hot <= hot.median()], '推荐日情绪偏弱（封板占比下半区）')
    stat(df[hot > hot.median()], '推荐日情绪偏强（封板占比上半区）')


if __name__ == '__main__':
    main()
