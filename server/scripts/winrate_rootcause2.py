#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""winrate_rootcause2.py - 补充检验（2026-10-18）"""
import sqlite3
import json
import statistics
from collections import defaultdict

BASE = 'D:/Desktop/guizhou/gupiao/server'
MAIN = f'file:{BASE}/gupiao.db?mode=ro'
HKDB = f'file:{BASE}/data/history_kline.db?mode=ro'


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


con = sqlite3.connect(MAIN, uri=True)
rows = con.execute("""
    SELECT rec_date, symbol, name, price, change_percent, score, buy_price, buy_timing,
           COALESCE(mode, '')
    FROM daily_recommendations ORDER BY rec_date, sort_order
""").fetchall()
con.close()

hconn = sqlite3.connect(HKDB, uri=True)
hcur = hconn.cursor()

recs = []
for rec_date, symbol, name, price, chg, score, buy_price, buy_timing, mode in rows:
    code = (symbol or '').lower().replace('sh', '').replace('sz', '').replace('bj', '')
    price = fnum(price) or 0.0
    chg = fnum(chg) or 0.0
    score = fnum(score) or 0.0
    buy = fnum(buy_price)
    if buy is not None and buy <= 0:
        buy = None
    hcur.execute(
        "SELECT date, close, low FROM stock_daily WHERE symbol=? AND date>? ORDER BY date LIMIT 1",
        (code, rec_date))
    bar = hcur.fetchone()
    if not bar:
        continue
    t1_date, t1_close, t1_low = bar
    r = {'date': rec_date, 'symbol': symbol, 'name': name, 'price': price, 'chg': chg,
         'score': score, 'buy': buy, 't1_date': t1_date}
    if buy:
        r['r1'] = (t1_close / buy - 1) * 100
        r['filled_low'] = 1 if t1_low <= buy else 0
    if price > 0:
        r['exec_r1'] = (t1_close / price - 1) * 100
    recs.append(r)

# 市场基线
t1_dates = sorted({r['t1_date'] for r in recs})
lo, hi = min(t1_dates), max(t1_dates)
hcur.execute("SELECT date, symbol, close FROM stock_daily WHERE date BETWEEN ? AND ?", (lo, hi))
by_date = defaultdict(dict)
for d, sym, c in hcur.fetchall():
    by_date[d][sym] = c
dates_sorted = sorted(by_date)
market_ret, market_win = {}, {}
for i, d in enumerate(dates_sorted):
    if i == 0:
        continue
    prev, cur = by_date[dates_sorted[i - 1]], by_date[d]
    rets, ups, tot = [], 0, 0
    for sym, c in cur.items():
        p = prev.get(sym)
        if p and p > 0 and c:
            rets.append((c / p - 1) * 100)
            tot += 1
            ups += 1 if c > p else 0
    if rets:
        market_ret[d] = statistics.median(rets)
        market_win[d] = ups / tot * 100
hconn.close()

print('=' * 72)
print('补充检验')
print('=' * 72)

# 1. 139 子集（线上同口径）exec_t1 核对
sub = [r for r in recs if r.get('r1') is not None]
e = [r['exec_r1'] for r in sub if r.get('exec_r1') is not None]
print(f"\n[1] 139数值买入价子集 exec_t1: n={len(e)} 胜率 "
      f"{sum(1 for v in e if v > 0) / len(e) * 100:.1f}% 均值 {sum(e) / len(e):.2f}%"
      f"（线上存档 43.2% / -1.39%，应对齐）")

# 2. 剔除最大5条吃面后的均值（损失集中度）
ev = sorted((r['exec_r1'] for r in recs if r.get('exec_r1') is not None))
trimmed = ev[5:]
print(f"[2] 现价口径分布：n={len(ev)} 均值 {sum(ev) / len(ev):.2f}% 中位数 "
      f"{statistics.median(ev):.2f}% 最小 {ev[0]:.1f}% 最大 {ev[-1]:.1f}%")
print(f"    剔除最差5条后：均值 {sum(trimmed) / len(trimmed):+.2f}% "
      f"（最差5条贡献 {(ev[0] + ev[1] + ev[2] + ev[3] + ev[4]) / len(ev):+.2f}pct）")
rv = sorted((r['r1'] for r in recs if r.get('r1') is not None))
rt = rv[5:]
print(f"    低吸口径剔除最差5条后：均值 {sum(rt) / len(rt):+.2f}%（原 {sum(rv) / len(rv):+.2f}%）")

# 3. 低吸价可成交性 按月
print('\n[3] 低吸参考价可成交性 按月（未成交=幻觉胜利，虚增 t1）：')
print(f"{'月份':<9}{'n':>4}{'可成交':>7}{'可成交占比':>11}{'可成交t1胜率':>13}{'未成交t1胜率':>13}")
by_month = defaultdict(list)
for r in recs:
    if r.get('filled_low') is not None:
        by_month[r['date'][:7]].append(r)
for m in sorted(by_month):
    items = by_month[m]
    f = [r for r in items if r['filled_low'] == 1]
    u = [r for r in items if r['filled_low'] == 0]
    fw = sum(1 for r in f if r['r1'] > 0) / len(f) * 100 if f else 0
    uw = sum(1 for r in u if r['r1'] > 0) / len(u) * 100 if u else 0
    print(f"{m:<9}{len(items):>4}{len(f):>7}{len(f) / len(items) * 100:>10.0f}%{fw:>12.1f}%{uw:>12.1f}%")

# 4. 全周期：推荐日 vs 市场
mk = [market_win[r['t1_date']] for r in recs if r['t1_date'] in market_win]
ew = sum(1 for r in recs if (r.get('exec_r1') or -1) > 0) / n_exec if (n_exec := sum(1 for r in recs if r.get('exec_r1') is not None)) else 0
print(f"\n[4] 全周期对比：推荐现价口径胜率 {ew * 100:.1f}% vs 同期全市场上涨股占比中位数 "
      f"{statistics.median(mk):.1f}%（市场每日胜率中位数）")
beat = sum(1 for r in recs if r.get('exec_r1') is not None and r['t1_date'] in market_ret
           and r['exec_r1'] > market_ret[r['t1_date']])
tot_b = sum(1 for r in recs if r.get('exec_r1') is not None and r['t1_date'] in market_ret)
print(f"    跑赢当日市场中位数的比例：{beat}/{tot_b} = {beat / tot_b * 100:.1f}%")

# 5. 若只推 2~4.5% 与 ≥9.8% 两个优势段（模拟"砍掉死亡区"）
keep = [r for r in recs if 2 <= r['chg'] < 4.5 or r['chg'] >= 9.8]
ke = [r['exec_r1'] for r in keep if r.get('exec_r1') is not None]
print(f"\n[5] 模拟“砍掉 ≤2% 与 4.5~9.8% 两个劣势段”后：n={len(ke)} 胜率 "
      f"{sum(1 for v in ke if v > 0) / len(ke) * 100:.1f}% 均值 {sum(ke) / len(ke):+.2f}%"
      f"（原 n=218 胜率 46.8% 均值 -0.55%）")

# 6. 高频重复推荐票的整体表现 vs 只推一次的票
rep = defaultdict(list)
for r in recs:
    rep[r['symbol']].append(r)
once = [r for s, v in rep.items() if len(v) == 1 for r in v]
multi = [r for s, v in rep.items() if len(v) > 1 for r in v]
oe = [r['exec_r1'] for r in once if r.get('exec_r1') is not None]
me = [r['exec_r1'] for r in multi if r.get('exec_r1') is not None]
print(f"\n[6] 只推一次的票：n={len(oe)} 胜率 {sum(1 for v in oe if v > 0) / len(oe) * 100:.1f}% "
      f"均值 {sum(oe) / len(oe):+.2f}%")
print(f"    重复推荐的票：n={len(me)} 胜率 {sum(1 for v in me if v > 0) / len(me) * 100:.1f}% "
      f"均值 {sum(me) / len(me):+.2f}%（重复推荐是否更差）")

# 7. 损失尾部：exec_r1 <= -8% 的命中场景特征
tail_losses = [r for r in recs if (r.get('exec_r1') or 0) <= -8]
print(f"\n[7] 单笔亏超8%的 {len(tail_losses)} 条特征：")
chg_ge98 = sum(1 for r in tail_losses if r['chg'] >= 9.8)
print(f"    推荐时已涨停(≥9.8%)的占 {chg_ge98}/{len(tail_losses)}；"
      f"平均评分 {sum(r['score'] for r in tail_losses) / len(tail_losses):.0f}")
print('    ' + '；'.join(f"{r['name']}({r['date'][5:]},涨{r['chg']:.0f}%,{r['exec_r1']:.1f}%)"
                         for r in tail_losses[:12]))
