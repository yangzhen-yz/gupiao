#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""winrate_rootcause.py - 推荐胜率低根因下钻（一次性分析，2026-10-18）

口径与线上复盘（services/pick/recommend_review.py）逐值对齐：
  r1      = 次日收盘 / 数值型 buy_price - 1        （低吸参考价口径，线上 t1）
  exec_r1 = 次日收盘 / 推荐时现价 - 1              （现价追高口径，线上 exec_t1）
  buy_price 非数值（'竞价关注'等）不参与 r1，与线上一致
额外检验（线上没有的）：
  filled_low  次日最低价 <= buy_price 才算低吸价真正可成交（防"参考价幻觉"）
  market     同日全市场等权 T+1 中位数（市场基线，区分环境 beta 与选股 alpha）

K线源：data/history_kline.db stock_daily（baostock 前复权）
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


# ---------- 1. 加载推荐记录并与K线 join ----------
con = sqlite3.connect(MAIN, uri=True)
rows = con.execute("""
    SELECT rec_date, symbol, name, price, change_percent, score, buy_price, buy_timing,
           COALESCE(mode, '')
    FROM daily_recommendations ORDER BY rec_date, sort_order
""").fetchall()
con.close()

hcon = sqlite3.connect(HKDB, uri=True)
hcur = hcon.cursor()

recs = []
for rec_date, symbol, name, price, chg, score, buy_price, buy_timing, mode in rows:
    code = (symbol or '').lower().replace('sh', '').replace('sz', '').replace('bj', '')
    price = fnum(price) or 0.0
    chg = fnum(chg) or 0.0
    score = fnum(score) or 0.0
    buy = fnum(buy_price)
    if buy is not None and buy <= 0:
        buy = None
    bt_type = ''
    try:
        bt = json.loads(buy_timing) if buy_timing else {}
        bt_type = bt.get('type') or ''
    except Exception:
        pass
    hcur.execute(
        "SELECT date, close, low FROM stock_daily WHERE symbol=? AND date>? ORDER BY date LIMIT 5",
        (code, rec_date))
    bars = hcur.fetchall()
    if not bars:
        continue
    t1_date, t1_close, t1_low = bars[0]
    r = {'date': rec_date, 'symbol': symbol, 'name': name, 'price': price, 'chg': chg,
         'score': score, 'buy': buy, 'bt': bt_type, 'mode': mode or '',
         't1_date': t1_date, 't1_close': t1_close}
    if buy:
        r['r1'] = (t1_close / buy - 1) * 100
        r['filled_low'] = 1 if t1_low <= buy else 0
    if price > 0:
        r['exec_r1'] = (t1_close / price - 1) * 100
    recs.append(r)

# ---------- 2. 市场基线：全市场等权 T+1 中位数（按推荐执行日） ----------
t1_dates = sorted({r['t1_date'] for r in recs})
if t1_dates:
    lo, hi = min(t1_dates), max(t1_dates)
    hcur.execute(
        "SELECT date, symbol, close FROM stock_daily WHERE date BETWEEN ? AND ?",
        (lo, hi))
    by_date = defaultdict(dict)
    for d, sym, c in hcur.fetchall():
        by_date[d][sym] = c
    dates_sorted = sorted(by_date)
    market_ret = {}   # t1_date -> 当日全市场中位数收益%
    for i, d in enumerate(dates_sorted):
        if i == 0:
            continue
        prev = by_date[dates_sorted[i - 1]]
        cur = by_date[d]
        rets = []
        for sym, c in cur.items():
            p = prev.get(sym)
            if p and p > 0 and c:
                rets.append((c / p - 1) * 100)
        if rets:
            market_ret[d] = statistics.median(rets)
    # 市场胜率：当日上涨股票占比
    market_win = {}
    for i, d in enumerate(dates_sorted):
        if i == 0:
            continue
        prev = by_date[dates_sorted[i - 1]]
        cur = by_date[d]
        ups = tot = 0
        for sym, c in cur.items():
            p = prev.get(sym)
            if p and p > 0 and c:
                tot += 1
                if c > p:
                    ups += 1
        if tot:
            market_win[d] = ups / tot * 100
hcon.close()

MKT = market_ret
MKT_WIN = market_win


# ---------- 3. 聚合工具 ----------
def agg(items, key):
    vals = [r[key] for r in items if r.get(key) is not None]
    if not vals:
        return None
    n = len(vals)
    wins = sum(1 for v in vals if v > 0)
    return {'n': n, 'win': round(wins / n * 100, 1), 'avg': round(sum(vals) / n, 2),
            'median': round(statistics.median(vals), 2),
            'min': round(min(vals), 1), 'max': round(max(vals), 1)}


def excess(items, key='exec_r1'):
    """相对市场基线的超额：每条 rec 的收益 - 同日市场中位数"""
    vals = []
    for r in items:
        v = r.get(key)
        m = MKT.get(r['t1_date'])
        if v is not None and m is not None:
            vals.append(v - m)
    if not vals:
        return None
    n = len(vals)
    wins = sum(1 for v in vals if v > 0)
    return {'n': n, 'win': round(wins / n * 100, 1), 'avg': round(sum(vals) / n, 2)}


def sec(title):
    print('\n' + '=' * 72)
    print(title)
    print('=' * 72)


# ---------- 4. 总量与月度趋势 ----------
sec('一、总量核对（与线上 recommendation_review 对齐）')
n_buy = sum(1 for r in recs if r.get('r1') is not None)
n_exec = sum(1 for r in recs if r.get('exec_r1') is not None)
print(f'推荐总行数 {len(rows)}，有K线可算 {len(recs)}，数值型买入价 {n_buy}，可算现价口径 {n_exec}')
t1 = agg(recs, 'r1')
ex = agg(recs, 'exec_r1')
print(f"低吸口径 t1    : n={t1['n']} 胜率 {t1['win']}% 均值 {t1['avg']}% 中位数 {t1['median']}% "
      f"[{t1['min']}%, {t1['max']}%]")
print(f"现价口径 exec_t1: n={ex['n']} 胜率 {ex['win']}% 均值 {ex['avg']}% 中位数 {ex['median']}% "
      f"[{ex['min']}%, {ex['max']}%]")
print('（线上存档：t1 60.4%/+1.24%，exec_t1 43.2%/-1.39%）')

# 低吸价可成交性
fl = [r for r in recs if r.get('filled_low') is not None]
filled = sum(r['filled_low'] for r in fl)
print(f"\n低吸参考价可成交性：{filled}/{len(fl)} = {filled / len(fl) * 100:.1f}% "
      f'（次日最低价确实触及买入价；其余为“参考价幻觉”）')
r1_f = agg([r for r in fl if r['filled_low'] == 1], 'r1')
r1_u = agg([r for r in fl if r['filled_low'] == 0], 'r1')
if r1_f:
    print(f"  可成交部分 t1: 胜率 {r1_f['win']}% 均值 {r1_f['avg']}% (n={r1_f['n']})")
if r1_u:
    print(f"  未成交部分 t1: 胜率 {r1_u['win']}% 均值 {r1_u['avg']}% (n={r1_u['n']})")

sec('二、月度趋势：环境 vs 选股')
print(f"{'月份':<9}{'n':>4}{'现价胜率':>9}{'现价均值':>9}{'低吸胜率':>9}{'低吸均值':>9}"
      f"{'市场中位':>9}{'市场胜率':>9}{'超额胜率':>9}{'超额均值':>9}")
by_month = defaultdict(list)
for r in recs:
    by_month[r['date'][:7]].append(r)
for m in sorted(by_month):
    items = by_month[m]
    e = agg(items, 'exec_r1')
    t = agg(items, 'r1')
    # 该月推荐执行日的市场汇总
    mk = [MKT[r['t1_date']] for r in items if r['t1_date'] in MKT]
    mw = [MKT_WIN[r['t1_date']] for r in items if r['t1_date'] in MKT_WIN]
    ex_ = excess(items)
    print(f"{m:<9}{e['n']:>4}{e['win']:>8.1f}%{e['avg']:>8.2f}%"
          f"{(t['win'] if t else 0):>8.1f}%{(t['avg'] if t else 0):>8.2f}%"
          f"{(statistics.median(mk) if mk else 0):>8.2f}%{(statistics.median(mw) if mw else 0):>8.1f}%"
          f"{(ex_['win'] if ex_ else 0):>8.1f}%{(ex_['avg'] if ex_ else 0):>8.2f}%")

sec('三、按推荐时涨幅分段（“半山腰死亡区”检验，现价口径）')
bands = [('≤2%', -100, 2), ('2~4.5%', 2, 4.5), ('4.5~9.8%', 4.5, 9.8), ('≥9.8%', 9.8, 1000)]
print(f"{'涨幅段':<10}{'n':>4}{'现价胜率':>9}{'现价均值':>9}{'低吸胜率':>9}{'低吸均值':>9}{'超额均值':>9}")
for label, lo_, hi_ in bands:
    items = [r for r in recs if lo_ <= r['chg'] < hi_]
    if not items:
        continue
    e = agg(items, 'exec_r1')
    t = agg(items, 'r1')
    ex_ = excess(items)
    print(f"{label:<10}{e['n']:>4}{e['win']:>8.1f}%{e['avg']:>8.2f}%"
          f"{(t['win'] if t else 0):>8.1f}%{(t['avg'] if t else 0):>8.2f}%"
          f"{(ex_['avg'] if ex_ else 0):>8.2f}%")

sec('四、按评分分段（分数是否预测收益？现价口径）')
sbands = [('<60', 0, 60), ('60~69', 60, 70), ('70~79', 70, 80), ('≥80', 80, 1000)]
print(f"{'评分段':<10}{'n':>4}{'现价胜率':>9}{'现价均值':>9}{'低吸胜率':>9}{'低吸均值':>9}{'超额均值':>9}")
for label, lo_, hi_ in sbands:
    items = [r for r in recs if lo_ <= r['score'] < hi_]
    if not items:
        continue
    e = agg(items, 'exec_r1')
    t = agg(items, 'r1')
    ex_ = excess(items)
    print(f"{label:<10}{e['n']:>4}{e['win']:>8.1f}%{e['avg']:>8.2f}%"
          f"{(t['win'] if t else 0):>8.1f}%{(t['avg'] if t else 0):>8.2f}%"
          f"{(ex_['avg'] if ex_ else 0):>8.2f}%")

sec('五、按 buy_timing 类型（现价口径）')
print(f"{'时机':<12}{'n':>4}{'现价胜率':>9}{'现价均值':>9}{'低吸胜率':>9}{'低吸均值':>9}{'超额均值':>9}")
by_bt = defaultdict(list)
for r in recs:
    by_bt[r['bt'] or '(无)'].append(r)
for k in sorted(by_bt, key=lambda k: -len(by_bt[k])):
    items = by_bt[k]
    e = agg(items, 'exec_r1')
    t = agg(items, 'r1')
    ex_ = excess(items)
    print(f"{k:<12}{e['n']:>4}{e['win']:>8.1f}%{e['avg']:>8.2f}%"
          f"{(t['win'] if t else 0):>8.1f}%{(t['avg'] if t else 0):>8.2f}%"
          f"{(ex_['avg'] if ex_ else 0):>8.2f}%")

sec('六、损失集中度：谁在亏？（现价口径 r1 < -5% 为吃面）')
noodles = sorted([r for r in recs if (r.get('exec_r1') or 0) < -5],
                 key=lambda r: r['exec_r1'])
print(f"吃面条数：{len(noodles)}/{n_exec} = {len(noodles) / n_exec * 100:.1f}%"
      f"（线上 noodle_count 判据是 r1<-5%，此处用 exec_r1）")
print(f"{'日期':<12}{'代码':<10}{'名称':<10}{'推荐时涨幅':>9}{'评分':>5}{'现价T+1':>9}{'低吸T+1':>9}")
for r in noodles[:20]:
    print(f"{r['date']:<12}{r['symbol']:<10}{r['name']:<10}{r['chg']:>8.1f}%"
          f"{r['score']:>5.0f}{r['exec_r1']:>8.1f}%{(r.get('r1') if r.get('r1') is not None else 0):>8.1f}%")
# 重复推荐同一只票
rep = defaultdict(list)
for r in recs:
    rep[r['symbol']].append(r)
multi = {s: v for s, v in rep.items() if len(v) > 1}
print(f"\n重复推荐个股：{len(multi)} 只（占 {len(rep)} 只去重个股的 "
      f"{len(multi) / len(rep) * 100:.0f}%）")
print(f"{'代码':<10}{'名称':<10}{'次数':>4}{'现价均值':>9}{'明细'}")
for s, v in sorted(multi.items(), key=lambda kv: -len(kv[1]))[:10]:
    e = agg(v, 'exec_r1')
    detail = ' '.join(f"{r['date'][5:]}:{r['exec_r1']:+.1f}%" for r in v if r.get('exec_r1') is not None)
    print(f"{s:<10}{v[0]['name']:<10}{len(v):>4}{e['avg']:>8.1f}%  {detail}")

sec('七、近30天（用户体感区间）明细')
recent = [r for r in recs if r['date'] >= '2026-09-18']
e = agg(recent, 'exec_r1')
t = agg(recent, 'r1')
print(f"近30天 n={len(recent)}：现价口径 胜率 {e['win']}% 均值 {e['avg']}% | "
      f"低吸口径 胜率 {t['win'] if t else '-'}% 均值 {t['avg'] if t else '-'}%")
ex_ = excess(recent)
if ex_:
    print(f"  相对市场基线：超额胜率 {ex_['win']}% 超额均值 {ex_['avg']:+.2f}%")

print('\n完成。')
