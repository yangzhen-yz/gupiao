# -*- coding: utf-8 -*-
"""
历史推荐复盘脚本（Task #63）
对 daily_recommendations 全部历史记录按 buy_price 模拟成交：
- 限定价：次日 open < buy_price → 开盘价成交；否则 low <= buy_price → buy_price 成交；否则不成交
- '竞价关注'：次日开盘价成交，高开 > 5% 不追（跳过）
卖出：T+1 / T+3 收盘价（买入日为 T，T+1 = 买入日之后第 1 个交易日收盘）
统计：整体胜率/盈亏比/平均收益 + 按评分段/买入时机/竞价vs限价/月份分组 + 买入后最大回撤
"""
import sqlite3
import sys
import os
import json
import re
from datetime import date, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_DB = os.path.join(BASE_DIR, 'gupiao.db')
HIST_DB = os.path.join(BASE_DIR, 'data', 'history_kline.db')

HIGH_OPEN_SKIP_PCT = 5.0  # 竞价高开超过该幅度不追


def normalize_symbol(sym: str) -> str:
    s = (sym or '').strip().lower()
    for p in ('sh.', 'sz.', 'bj.', 'sh', 'sz', 'bj'):
        if s.startswith(p):
            return s[len(p):]
    return s


def load_calendar():
    """交易日历：取成交股票数 > 4000 的日期"""
    conn = sqlite3.connect(HIST_DB, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    cur = conn.cursor()
    cur.execute("SELECT date, COUNT(*) c FROM stock_daily GROUP BY date HAVING c > 4000 ORDER BY date")
    dates = [r[0] for r in cur.fetchall()]
    conn.close()
    return dates


def load_klines(symbols):
    """symbol -> {date: (open, high, low, close)} 前复权日线"""
    conn = sqlite3.connect(HIST_DB, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    cur = conn.cursor()
    result = {}
    for sym in symbols:
        cur.execute(
            "SELECT date, open, high, low, close FROM stock_daily WHERE symbol=? ORDER BY date",
            (sym,))
        rows = cur.fetchall()
        if rows:
            result[sym] = {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}
    conn.close()
    return result


def next_trading_day(calendar, d):
    """d 之后的第一个交易日（推荐盘后发出，次日执行；d 本身是交易日也返回其后一日）"""
    for x in calendar:
        if x > d:
            return x
    return None


def trading_offset(calendar, d, n):
    """d 之后第 n 个交易日（d 是交易日时，n=1 即下一个交易日）"""
    idx = None
    for i, x in enumerate(calendar):
        if x == d:
            idx = i
            break
    if idx is None:
        # d 非交易日，找其后第一个交易日的索引，offset 从该日之后算起
        for i, x in enumerate(calendar):
            if x > d:
                idx = i - 1
                break
    if idx is None:
        return None
    j = idx + n
    return calendar[j] if j < len(calendar) else None


def simulate(rec, kline, calendar):
    """返回成交信息或 None（未成交）"""
    sym = normalize_symbol(rec['symbol'])
    k = kline.get(sym)
    if not k:
        return {'status': 'no_kline'}
    rec_date = rec['rec_date']
    buy_raw = (rec['buy_price'] or '').strip()
    is_auction = not buy_raw.replace('.', '', 1).isdigit()
    try:
        buy_price = float(buy_raw)
    except ValueError:
        buy_price = None

    # 买入日 = rec_date 之后第一个交易日（推荐多为盘后/尾盘发出，次日执行）
    buy_day = next_trading_day(calendar, rec_date)
    if not buy_day or buy_day not in k:
        return {'status': 'no_buy_day'}
    bar = k[buy_day]
    o, h, l, c = bar
    # rec_date 前一交易日收盘价（算竞价高开幅度）
    prev_day = None
    for i, x in enumerate(calendar):
        if x == buy_day:
            if i > 0:
                prev_day = calendar[i - 1]
            break
    prev = k[prev_day][3] if prev_day and prev_day in k else None

    if is_auction:
        if o <= 0:
            return {'status': 'bad_data'}
        if prev and prev > 0 and (o / prev - 1) * 100 > HIGH_OPEN_SKIP_PCT:
            return {'status': 'skip_high_open', 'buy_day': buy_day}
        fill_price = o
    else:
        if buy_price is None or buy_price <= 0:
            return {'status': 'bad_price'}
        if o > 0 and o < buy_price:
            fill_price = o  # 低开直接吃到更优价
        elif l <= buy_price:
            fill_price = buy_price
        else:
            return {'status': 'no_touch', 'buy_day': buy_day}

    # 卖出：T+1 / T+3（相对买入日）
    t1_day = trading_offset(calendar, buy_day, 1)
    t3_day = trading_offset(calendar, buy_day, 3)
    if not t1_day or not t3_day or t1_day not in k or t3_day not in k:
        return {'status': 'no_sell_day', 'buy_day': buy_day, 'fill_price': fill_price}

    sell_t1 = k[t1_day][3]
    sell_t3 = k[t3_day][3]

    # 买入后最大回撤 / 最大浮盈（从买入日到 T+3）
    max_dd = 0.0
    max_up = 0.0
    idx_buy = calendar.index(buy_day)
    idx_t3 = calendar.index(t3_day)
    for d in calendar[idx_buy:idx_t3 + 1]:
        if d in k:
            low_d = k[d][2]
            high_d = k[d][1]
            if low_d > 0:
                max_dd = min(max_dd, (low_d / fill_price - 1) * 100)
            if high_d > 0:
                max_up = max(max_up, (high_d / fill_price - 1) * 100)

    return {
        'status': 'filled',
        'buy_day': buy_day,
        'fill_price': fill_price,
        'sell_t1': sell_t1,
        'sell_t3': sell_t3,
        'ret_t1': (sell_t1 / fill_price - 1) * 100,
        'ret_t3': (sell_t3 / fill_price - 1) * 100,
        'max_dd': max_dd,
        'max_up': max_up,
        'is_auction': is_auction,
    }


def main():
    conn = sqlite3.connect(MAIN_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM daily_recommendations ORDER BY rec_date, sort_order")
    recs = [dict(r) for r in cur.fetchall()]
    conn.close()

    calendar = load_calendar()
    print(f"交易日历: {calendar[0]} ~ {calendar[-1]} 共 {len(calendar)} 天")
    symbols = list({normalize_symbol(r['symbol']) for r in recs})
    kline = load_klines(symbols)
    missing = [s for s in symbols if s not in kline]
    print(f"推荐记录 {len(recs)} 条，涉及 {len(symbols)} 只股票，本地无K线 {len(missing)} 只: {missing[:10]}")

    # 大盘环境代理：全市场每日涨跌幅中位数（本地库 5000+ 只，仅算推荐日区间）
    mconn = sqlite3.connect(HIST_DB, timeout=30)
    mconn.execute("PRAGMA busy_timeout=30000")
    mcur = mconn.cursor()
    need_dates = sorted({r['rec_date'] for r in recs})
    market_proxy = {}
    for d in need_dates:
        # 当日与前一交易日的收盘价对比
        mcur.execute("SELECT MAX(date) FROM stock_daily WHERE date<?", (d,))
        row = mcur.fetchone()
        prev = row[0] if row else None
        if not prev or d not in calendar:
            continue
        mcur.execute("SELECT symbol, close FROM stock_daily WHERE date=? AND close>0", (d,))
        today_map = dict(mcur.fetchall())
        mcur.execute("SELECT symbol, close FROM stock_daily WHERE date=? AND close>0", (prev,))
        prev_map = dict(mcur.fetchall())
        chgs = [(t / p - 1) * 100 for s, t in today_map.items()
                if (p := prev_map.get(s)) and p > 0]
        if chgs:
            chgs.sort()
            market_proxy[d] = chgs[len(chgs) // 2]
    mconn.close()

    # 执行模拟
    trades = []
    for rec in recs:
        sim = simulate(rec, kline, calendar)
        if sim['status'] != 'filled':
            continue
        rec_date = rec['rec_date']
        mkt = market_proxy.get(rec['rec_date'], market_proxy.get(sim['buy_day'], 0.0))
        buy_timing = rec.get('buy_timing') or ''
        try:
            bt = json.loads(buy_timing) if buy_timing else {}
        except Exception:
            bt = {}
        # 从理由文本提取趋势分/日内分
        trend_score = None
        m = re.search(r'趋势分([\d.]+)', rec.get('reason') or '')
        if m:
            trend_score = float(m.group(1))
        trades.append({
            'rec_date': rec_date,
            'month': rec_date[:7],
            'symbol': rec['symbol'],
            'name': rec['name'],
            'score': rec['score'] or 0,
            'change_percent': rec['change_percent'] or 0,
            'trend_score': trend_score,
            'is_auction': sim['is_auction'],
            'timing_type': bt.get('type', 'legacy'),
            'fill_price': sim['fill_price'],
            'ret_t1': sim['ret_t1'],
            'ret_t3': sim['ret_t3'],
            'max_dd': sim['max_dd'],
            'max_up': sim['max_up'],
            'market': mkt,
            'buy_day': sim['buy_day'],
        })

    n_total = len(recs)
    n_filled = len(trades)
    print(f"\n=== 成交概况 ===")
    print(f"总推荐 {n_total}，模拟成交 {n_filled}（成交率 {n_filled/n_total*100:.1f}%）")
    if not trades:
        print("无成交样本，退出")
        return

    def stats(subset, label):
        if not subset:
            print(f"{label}: 无样本")
            return
        t1 = [t['ret_t1'] for t in subset]
        t3 = [t['ret_t3'] for t in subset]
        wins1 = sum(1 for x in t1 if x > 0)
        wins3 = sum(1 for x in t3 if x > 0)
        avg1 = sum(t1) / len(t1)
        avg3 = sum(t3) / len(t3)
        win_avg1 = sum(x for x in t1 if x > 0) / wins1 if wins1 else 0.0
        loss_avg1 = sum(x for x in t1 if x <= 0) / (len(t1) - wins1) if len(t1) - wins1 else 0.0
        win_avg3 = sum(x for x in t3 if x > 0) / wins3 if wins3 else 0.0
        loss_avg3 = sum(x for x in t3 if x <= 0) / (len(t3) - wins3) if len(t3) - wins3 else 0.0
        pl1 = abs(win_avg1 / loss_avg1) if loss_avg1 else float('inf')
        pl3 = abs(win_avg3 / loss_avg3) if loss_avg3 else float('inf')
        dd = sum(t['max_dd'] for t in subset) / len(subset)
        print(f"{label}: n={len(subset)} | T+1胜率 {wins1/len(subset)*100:.1f}% 均收益 {avg1:+.2f}% 盈亏比 {pl1:.2f} | "
              f"T+3胜率 {wins3/len(subset)*100:.1f}% 均收益 {avg3:+.2f}% 盈亏比 {pl3:.2f} | 平均最大回撤 {dd:.2f}%")

    print(f"\n=== 整体表现 ===")
    stats(trades, "全部")

    print(f"\n=== 按评分段 ===")
    stats([t for t in trades if t['score'] < 70], "60-70 分")
    stats([t for t in trades if 70 <= t['score'] < 80], "70-80 分")
    stats([t for t in trades if t['score'] >= 80], "80+ 分")

    print(f"\n=== 按买入方式 ===")
    stats([t for t in trades if t['is_auction']], "竞价关注")
    stats([t for t in trades if not t['is_auction']], "限价买入")

    print(f"\n=== 按买入时机类型 ===")
    for tp in ['auction', 'early', 'tail', 'pullback', 'intraday', 'legacy']:
        subset = [t for t in trades if t['timing_type'] == tp]
        if subset:
            stats(subset, f"时机={tp}")

    print(f"\n=== 按大盘环境（推荐日全市场中位数涨跌幅） ===")
    stats([t for t in trades if t['market'] < -0.5], "大盘跌(<-0.5%)")
    stats([t for t in trades if -0.5 <= t['market'] < 0.5], "大盘平")
    stats([t for t in trades if t['market'] >= 0.5], "大盘涨(>=0.5%)")

    print(f"\n=== 按推荐日涨幅（追高程度） ===")
    stats([t for t in trades if t['change_percent'] < 0], "推荐日收跌")
    stats([t for t in trades if 0 <= t['change_percent'] < 3], "推荐日涨 0~3%")
    stats([t for t in trades if 3 <= t['change_percent'] < 6], "推荐日涨 3~6%")
    stats([t for t in trades if t['change_percent'] >= 6], "推荐日涨 6%+")

    print(f"\n=== 按趋势分（扫描时趋势质量） ===")
    ts_known = [t for t in trades if t['trend_score'] is not None]
    stats([t for t in ts_known if t['trend_score'] < 60], "趋势分 <60")
    stats([t for t in ts_known if 60 <= t['trend_score'] < 75], "趋势分 60~75")
    stats([t for t in ts_known if t['trend_score'] >= 75], "趋势分 75+")

    print(f"\n=== 按月份 ===")
    months = sorted({t['month'] for t in trades})
    for m in months:
        stats([t for t in trades if t['month'] == m], m)

    # 亏损 pattern 明细：T+1 亏损 > 5% 的
    print(f"\n=== 重灾样本（T+1 亏损 > 5%） ===")
    bad = sorted([t for t in trades if t['ret_t1'] < -5], key=lambda t: t['ret_t1'])
    for t in bad[:20]:
        print(f"  {t['rec_date']} {t['name']}({t['symbol']}) 评分{t['score']} 推荐日{t['change_percent']:+.2f}% "
              f"大盘{t['market']:+.2f}% {'竞价' if t['is_auction'] else '限价'} T+1 {t['ret_t1']:+.2f}% T+3 {t['ret_t3']:+.2f}% 最大回撤 {t['max_dd']:.2f}%")
    print(f"  共 {len(bad)} 笔")

    # 保存明细供后续分析
    out = os.path.join(BASE_DIR, 'backtest_recommend_result.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(trades, f, ensure_ascii=False, indent=1)
    print(f"\n明细已保存: {out}")


if __name__ == '__main__':
    main()
