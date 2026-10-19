#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sentiment_gate_sim.py - "按市场情绪开关仓"胜率模拟（一次性分析，2026-10-18）

用户问题：根据市场情绪买卖（当天情绪不好就不交易）胜率会不会更高？

做法：把每条推荐的**执行日**（rec_date）配上当日全市场指标（收盘口径），
分档统计真实可成交口径（filled t1）的胜率/均值，再模拟"市场上涨占比低于 X 就
整日不开仓"的整体效果。判据全部来自执行日当天可观测的市场状态，不用未来信息；
唯一保留的后视成分是"收盘口径"（推荐发生在盘中 9:45~15:00，而指标取收盘值）——
方向性结论可信，数值是上界，脚本结论里会显式标注。

与月度复盘（docs/胜率低根因复盘-2026-10-18.md 第二章）的区别：那是按月聚合、
每行 12~66 条；这里按**每个执行日**分组，能看出"同一个好月份里的坏日子"。
温度计（market_sentiment_snapshots）10-14 才上线、只有 4 天数据，
所以本脚本用日K重算市场指标作代理；等影子情绪数据攒够，用真实五维档位重跑即可。
"""
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


def stats(vals):
    if not vals:
        return None
    wins = sum(1 for v in vals if v > 0)
    return {'n': len(vals), 'win': round(wins / len(vals) * 100, 1),
            'avg': round(statistics.fmean(vals), 2)}


# ---------- 1. 加载推荐（与 winrate_rootcause.py 同口径） ----------
con = sqlite3.connect(MAIN, uri=True)
rows = con.execute("""
    SELECT rec_date, symbol, name, price, change_percent, score, buy_price
    FROM daily_recommendations ORDER BY rec_date, sort_order
""").fetchall()
con.close()

hcon = sqlite3.connect(HKDB, uri=True)
hcur = hcon.cursor()

recs = []
for rec_date, symbol, name, price, chg, score, buy_price in rows:
    code = (symbol or '').lower().replace('sh', '').replace('sz', '').replace('bj', '')
    price = fnum(price) or 0.0
    buy = fnum(buy_price)
    if buy is not None and buy <= 0:
        buy = None
    hcur.execute(
        "SELECT date, close, low FROM stock_daily WHERE symbol=? AND date>? ORDER BY date LIMIT 5",
        (code, rec_date))
    bars = hcur.fetchall()
    if not bars:
        continue
    t1_date, t1_close, t1_low = bars[0]
    r = {'date': rec_date, 'symbol': symbol, 'price': price, 'buy': buy,
         't1_date': t1_date}
    if buy:
        r['r1'] = (t1_close / buy - 1) * 100
        r['filled'] = 1 if t1_low <= buy else 0
    if price > 0:
        r['exec_r1'] = (t1_close / price - 1) * 100
    recs.append(r)

print(f'推荐 {len(rows)} 条 → join 到K线 {len(recs)} 条')

# ---------- 2. 每个执行日的全市场指标（收盘口径） ----------
rec_dates = sorted({r['date'] for r in recs})
lo, hi = min(rec_dates), max(rec_dates)
hcur.execute("SELECT date, symbol, close FROM stock_daily WHERE date BETWEEN ? AND ?",
             (lo, hi))
by_date = defaultdict(dict)
for d, sym, c in hcur.fetchall():
    if c:
        by_date[d][sym] = c
dates_sorted = sorted(by_date)

day_state = {}     # rec_date -> {'up_ratio': 涨家数占比%, 'median': 中位收益%, 'limitup': 涨停代理家数}
for i, d in enumerate(dates_sorted):
    if i == 0:
        continue
    prev = by_date[dates_sorted[i - 1]]
    cur = by_date[d]
    rets, up, limitup = [], 0, 0
    for sym, c in cur.items():
        p = prev.get(sym)
        if not p or p <= 0:
            continue
        r = (c / p - 1) * 100
        rets.append(r)
        if r > 0:
            up += 1
        if r >= 9.5:      # 涨停代理：主板 10%（1094 口径全是主板，创业板混入会低估，方向不变）
            limitup += 1
    if len(rets) < 500:
        continue
    day_state[d] = {'up_ratio': round(up / len(rets) * 100, 1),
                    'median': round(statistics.median(rets), 2),
                    'limitup': limitup, 'n_stock': len(rets)}

print(f'执行日有市场指标的：{len(day_state)} / {len(rec_dates)} 天')

# ---------- 3. 按当日市场状态分档统计（filled 真实成交口径优先） ----------
recs2 = [r for r in recs if r['date'] in day_state]
print(f'可参与分档的推荐：{len(recs2)} 条\n')

BANDS = [('差（上涨占比<30%）', lambda s: s['up_ratio'] < 30),
         ('中（30~45%）', lambda s: 30 <= s['up_ratio'] < 45),
         ('好（≥45%）', lambda s: s['up_ratio'] >= 45)]

print('=' * 78)
print('一、当日市场状态分档 × 推荐结果（filled=低吸单真正成交口径；exec=现价追高口径）')
print('=' * 78)
for label, test in BANDS:
    grp = [r for r in recs2 if test(day_state[r['date']])]
    if not grp:
        print(f'{label}: 无样本')
        continue
    days = sorted({r['date'] for r in grp})
    filled = [r['r1'] for r in grp if r.get('filled') == 1 and r.get('r1') is not None]
    unfilled = [r['r1'] for r in grp if r.get('filled') == 0 and r.get('r1') is not None]
    ex = [r['exec_r1'] for r in grp if r.get('exec_r1') is not None]
    nb = sum(1 for r in grp if r.get('buy'))
    sf, sx = stats(filled), stats(ex)
    mkt_med = statistics.fmean([day_state[d]['median'] for d in days])
    print(f'\n{label}：{len(grp)} 条 / {len(days)} 天（当日市场中位收益均值 {mkt_med:+.2f}%）')
    print(f'  低吸成交 {len(filled)}/{nb}（{len(filled)/nb*100:.0f}%）'
          f' → 成交组胜率 {sf["win"]}% / 均值 {sf["avg"]}%' if sf else '  无成交样本')
    if unfilled:
        su = stats(unfilled)
        print(f'  未成交 {len(unfilled)} 条（按构造是幻觉胜利）：胜率 {su["win"]}% / 均值 {su["avg"]}%')
    print(f'  现价追高口径：胜率 {sx["win"]}% / 均值 {sx["avg"]}%（n={sx["n"]}）')

# ---------- 4. 模拟"市场上涨占比低于 X 就整日不开仓" ----------
print('\n' + '=' * 78)
print('二、开关仓模拟：当日上涨占比 < X% → 当日推荐全部不执行（其余日子照常）')
print('=' * 78)
base_filled = [r['r1'] for r in recs2 if r.get('filled') == 1 and r.get('r1') is not None]
base = stats(base_filled)
print(f'基准（全部执行）：filled 胜率 {base["win"]}% / 均值 {base["avg"]}%（n={base["n"]}）')
print(f'覆盖 {len(rec_dates)} 个推荐日\n')
print(f'{"阈值X":>6} | {"空仓天数":>8} | {"过滤条数":>8} | {"执行后胜率":>10} | {"执行后均值":>10} | {"躲掉组胜率":>10} | {"躲掉组均值":>10}')
print('-' * 78)
for x in (20, 25, 30, 35, 40, 45, 50):
    skip_days = {d for d, s in day_state.items() if s['up_ratio'] < x}
    if not skip_days:
        continue
    kept = [r for r in recs2 if r['date'] not in skip_days]
    skipped = [r for r in recs2 if r['date'] in skip_days]
    kf = [r['r1'] for r in kept if r.get('filled') == 1 and r.get('r1') is not None]
    sf_ = [r['r1'] for r in skipped if r.get('filled') == 1 and r.get('r1') is not None]
    sk, ss = stats(kf), stats(sf_)
    days_kept = len({r['date'] for r in kept})
    print(f'{x:>5}% | {len(skip_days):>4}/{len(day_state)}天 | {len(skipped):>6}条 | '
          f'{sk["win"]:>8}% | {sk["avg"]:>8}% | '
          + (f'{ss["win"]:>8}% | {ss["avg"]:>8}%' if ss else f'{"-":>8} | {"-":>8}'))
print('\n注：躲掉组=被闸门拦下不做的交易；其胜率显著低于执行后整体，才证明闸门有效。')

# ---------- 5. 交互检验：退潮日低吸单更容易成交吗？成交后呢？ ----------
print('\n' + '=' * 78)
print('三、交互检验：市场差的日子，低吸单是"更容易成交"还是"成交了也更亏"？')
print('=' * 78)
bad_days = {d for d, s in day_state.items() if s['up_ratio'] < 30}
good_days = {d for d, s in day_state.items() if s['up_ratio'] >= 45}
for label, dset in (('差日子(<30%)', bad_days), ('好日子(≥45%)', good_days)):
    grp = [r for r in recs2 if r['date'] in dset and r.get('buy')]
    if not grp:
        continue
    nf = sum(1 for r in grp if r['filled'] == 1)
    fv = [r['r1'] for r in grp if r['filled'] == 1]
    print(f'{label}: n={len(grp)}，成交率 {nf/len(grp)*100:.0f}%，'
          f'成交组 {stats(fv)["win"]}%/{stats(fv)["avg"]}%' if fv else f'{label}: 无成交')

hcon.close()
