"""rec_exit_sim.py - 历史推荐按"次日开盘卖 / 次日尾盘卖"各跑一遍，10 万还剩多少（2026-10-17）

问题来自用户："统计下历史推荐中按照推荐次日开盘卖和尾盘卖近3个月10万元还剩多少"。

为什么这次的数比之前那版可信：**入口是真实推荐记录**（daily_recommendations，222 行、
全部 created_at=14:45 —— 也就是当天收盘前最后一次快照），不是拿 r20 代理海选重建的近似池。
所以它回答的是"系统真的推了谁"，只是评分链路之外的东西（当天没推的票）依然看不见。

口径（写死在脚本里，别在脑子里换）
--------------------------------
  · 买入价 = 记录里的 price（14:45 的现价 ≈ 尾盘价），按 100 股一手取整；
  · 卖法 A 次日开盘卖 = 次一交易日 open；卖法 B 次日尾盘卖 = 次一交易日 close；
  · **资金复用**：A 在次日开盘就回笼，当天 14:45 可以再接；B 到收盘才回笼，
    与当日 14:45 建仓有几分钟重叠，按"卖出资金当日可用"放行（A股实际如此）；
  · **买不到的不算成交**：记录时点已封死涨停（收盘=全天最高且涨幅≥9.7%/19.7%）→
    尾盘排板未必成交，跳过并计数。这是模拟里最容易被忽略、也最容易把结果算好的一条；
  · **卖不掉就顺延**：次日一字跌停（开=收=最低且跌幅≥9.7%）卖不出去，顺延到下一根K线；
    停牌无K线同样顺延；
  · 费用：佣金万 2.5（单边最低 5 元）+ 印花税千 1（卖出）+ 过户费万 0.1（双边）；
  · 除权核对：日K是前复权、记录价是当时实盘价，若 |复权收盘/记录价 - 1| 超过
    涨停幅度+1%，判为两个尺度对不上，买入价退回当日复权收盘（少算"14:45→收盘"那一段）并计数。

复现：python scripts/rec_exit_sim.py [--since 2026-07-17] [--capital 100000] [--detail]
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfg                       # noqa: E402

FEE_RATE = 0.00025      # 佣金（单边）
FEE_MIN = 5.0           # 单笔最低佣金
STAMP = 0.001           # 印花税（卖出）
TRANSFER = 0.00001      # 过户费（双边）


def _fee_buy(amount):
    return max(FEE_MIN, amount * FEE_RATE) + amount * TRANSFER


def _fee_sell(amount):
    return max(FEE_MIN, amount * FEE_RATE) + amount * STAMP + amount * TRANSFER


def _limit_pct(code):
    return 20.0 if code.startswith(('30', '68')) else 10.0


def load(conn, gconn, since):
    recs = [dict(r) for r in gconn.execute(
        'SELECT rec_date, symbol, name, price, score, change_percent, buy_timing '
        'FROM daily_recommendations WHERE rec_date>=? ORDER BY rec_date, sort_order',
        (since,)).fetchall()]
    # price/score 在表里是 TEXT（'28.14'），不转数字后面每一步都会在字符串上算数
    for r in recs:
        try:
            r['price'] = float(r['price'] or 0)
        except (TypeError, ValueError):
            r['price'] = 0.0
        try:
            r['score'] = float(r['score'] or 0)
        except (TypeError, ValueError):
            r['score'] = None
    out, codes = [], sorted({(r['symbol'] or '')[2:] for r in recs})
    for code in codes:
        rows = conn.execute('SELECT date, open, high, low, close FROM stock_daily '
                            'WHERE symbol=? ORDER BY date', (code,)).fetchall()
        out.append((code, rows))
    return recs, dict(out)


def plan(recs, bars, mode, allow_sealed=False, sealed_auction=False, auction_max_gap=5.0):
    """把每条推荐翻译成"哪天买、哪天卖、什么价"，先不谈钱 —— 资金循环交给 run()。

    mode='open' 次日开盘卖；mode='close' 次日尾盘（收盘）卖。
    allow_sealed=True 把"尾盘已封死涨停"也算成交 —— 排板 100% 成交的**乐观上界**，
    真实世界封死的板经常排不到，所以默认按买不到剔除。
    sealed_auction=True 才是系统对这些票的本意：当天买不到就**次日竞价买**，
    卖出基准随之顺延一根（资金也因此多占一天，run() 按日历结算会如实反映）。
    """
    by_date = defaultdict(list)
    for r in recs:
        by_date[r['rec_date']].append(r)
    orders, skipped = [], []
    for d in sorted(by_date):
        for r in by_date[d]:
            code = (r['symbol'] or '')[2:]
            series = bars.get(code) or []
            idx = next((i for i, b in enumerate(series) if b[0] == d), None)
            if idx is None:
                skipped.append((r, '无当日K线'))
                continue
            buy_date = d
            _day, o, h, l, c = series[idx]
            prev_c = series[idx - 1][4] if idx > 0 else None
            chg = (c / prev_c - 1) * 100 if prev_c else 0.0
            lim = _limit_pct(code)
            # 尺度体检：记录价是当时实盘价，日K是前复权。**落在当日最高最低之外**
            # 就说明两者不同尺度（期间除过权），拿它当买入成本会凭空多算/少算几个点。
            # 比"跟收盘价差多少"可靠 —— 收盘差 1% 以内也可能已经越界。
            rp = r['price'] or 0
            if rp and l * 0.99 <= rp <= h * 1.01:
                entry, entry_note = rp, ''
            else:
                entry = c
                entry_note = ('记录价越出当日高低区间(期间除权)→买入价改用当日复权收盘'
                              if rp else '记录价缺失→买入价用复权收盘')
            sealed = (abs(h - c) < 1e-6 and chg >= lim * 0.97)
            if sealed and sealed_auction and not allow_sealed:
                if idx + 2 >= len(series):
                    skipped.append((r, '次日竞价买入但次次日无K线'))
                    continue
                o1, h1, l1, c1 = series[idx + 1][1], series[idx + 1][2], series[idx + 1][3], series[idx + 1][4]
                if abs(o1 - h1) < 1e-6 and abs(o1 - l1) < 1e-6 and (c1 / c - 1) * 100 >= lim * 0.97:
                    skipped.append((r, '次日一字开，竞价也买不到'))
                    continue
                gap = (o1 / c - 1) * 100
                if gap > auction_max_gap:
                    # 推荐卡片原文就写着"高开 5% 以上不追"，不追才是按推荐执行。
                    # 这条纪律很值钱：共进股份 07-20 竞价 +10.0% 开盘、当天崩到 -10.3%，
                    # 按纪律不追就根本不会变成 -20% 那一笔。
                    skipped.append((r, f'次日竞价高开 {gap:.1f}%＞{auction_max_gap:g}%，按纪律不追'))
                    continue
                entry, entry_note = o1, '封板票按推荐次日竞价买入'
                buy_date = series[idx + 1][0]
                idx += 1                      # 买卖基准整体后移一根
                sealed = False
            if sealed and not allow_sealed:
                skipped.append((r, '尾盘已封死涨停，排板未必成交'))
                continue
            if idx + 1 >= len(series):
                skipped.append((r, '次日无K线(未成交/停牌)'))
                continue
            sell_i = idx + 1
            shifted = ''
            # 次日一字跌停卖不掉就往后顺延（开盘口径尤其容易被这一档吃掉）
            while sell_i < len(series):
                so, sl, sc = series[sell_i][1], series[sell_i][3], series[sell_i][4]
                spc = series[sell_i - 1][4]
                schg = (sc / spc - 1) * 100 if spc else 0.0
                stuck = (mode == 'open' and abs(so - sl) < 1e-6 and abs(so - sc) < 1e-6
                         and schg <= -lim * 0.97)
                if not stuck:
                    break
                shifted = '次日一字跌停→顺延到下一根'
                sell_i += 1
            if sell_i >= len(series):
                skipped.append((r, '顺延到最后一根仍卖不掉'))
                continue
            exit_price = series[sell_i][1] if mode == 'open' else series[sell_i][4]
            if entry <= 0 or exit_price <= 0:
                skipped.append((r, '价格数据为 0（停牌/缺数据）'))
                continue
            orders.append({'rec_date': d, 'buy_date': buy_date, 'sell_date': series[sell_i][0],
                           'entry': entry, 'exit': exit_price, 'r': r,
                           'symbol': r['symbol'], 'name': r['name'], 'score': r['score'],
                           'note': entry_note or shifted})
    return orders, skipped


def run(orders, capital, whole_lots=True):
    """按交易日推进：先结算当天到期的卖出，再用**当时可用**的现金买当天的单。

    这一步不能省：竞价口径一笔占两天，如果把每笔当成"当天买当天回款"复利，
    资金周转过快、期末会明显偏高。A股卖出资金当日可用，所以下午的买入能接上早盘的回款。
    """
    buy_days = defaultdict(list)
    for o in orders:
        buy_days[o['buy_date']].append(o)
    days = sorted({o['buy_date'] for o in orders} | {o['sell_date'] for o in orders})
    cash, open_pos, trades, unsold = capital, [], [], []
    peak, mdd = capital, 0.0
    for day in days:
        due, hold = [], []
        for p in open_pos:
            (due if p['sell_date'] == day else hold).append(p)
        open_pos = hold
        for p in due:
            cash += p['proceeds']
            trades.append(p)
        todays = buy_days.get(day) or []
        if todays and cash > 0:
            budget = cash / len(todays)
            for o in todays:
                # whole_lots=False 是**敏感性检验**：100 股一手会让高价票自动少买、
                # 低价票多买，等于给权重掺了取整噪声。要看"等权满仓"的真实结果就得放开整股。
                shares = (int(budget / o['entry'] / 100) * 100 if whole_lots
                          else budget / o['entry'])
                if shares <= 0:
                    unsold.append((o['r'], f"一手要 {o['entry'] * 100:,.0f} 元，份额不够"))
                    continue
                cost = shares * o['entry'] + _fee_buy(shares * o['entry'])
                while cost > cash and shares > 0:
                    shares = (shares - 100 if whole_lots else shares * 0.98)
                    cost = shares * o['entry'] + _fee_buy(shares * o['entry']) if shares > 0 else 0
                if shares <= 0:
                    unsold.append((o['r'], '现金不足一手'))
                    continue
                proceeds = shares * o['exit'] - _fee_sell(shares * o['exit'])
                cash -= cost
                open_pos.append({**o, 'shares': shares, 'cost': cost, 'proceeds': proceeds,
                                 'ret': (proceeds / cost - 1) * 100})
        nav = cash + sum(p['cost'] for p in open_pos)
        peak = max(peak, nav)
        mdd = max(mdd, (peak - nav) / peak * 100)
    for p in open_pos:                       # 日历尽头还没卖出的（最后一天之后无K线），按口径价收尾
        cash += p['proceeds']
        trades.append(p)
    for p in trades:
        p.pop('r', None)
    return cash, trades, unsold, mdd


def _benchmark(conn, since):
    """同期背景：选票范围（当前口径）等权买入持有。

    注意这是**事后信息**（市值/流动性按今天的表判定），只用来看行情冷热，
    不能当成"策略跑输/跑赢"的严格基准。
    """
    days = [r[0] for r in conn.execute('SELECT DISTINCT date FROM stock_daily WHERE date>=? '
                                       'ORDER BY date', (since,)).fetchall()]
    if len(days) < 2:
        return None, None
    a, b = days[0], days[-1]
    try:
        from services.data import universe
        scope = {s[-6:] for s in universe.eligible_symbols()}
        desc = universe.describe_selectable()
    except Exception:
        scope, desc = None, '（范围读取失败，按全库算）'
    got = []
    for sym, x, y in conn.execute(
            'SELECT symbol, MAX(CASE WHEN date=? THEN close END), '
            'MAX(CASE WHEN date=? THEN close END) FROM stock_daily GROUP BY symbol', (a, b)):
        if x and y and (scope is None or sym in scope):
            got.append((y / x - 1) * 100)
    if not got:
        return None, None
    got.sort()
    return sum(got) / len(got), f'{a}→{b}，{len(got)} 只等权（{desc}）'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', default='2026-07-17')
    ap.add_argument('--capital', type=float, default=100000)
    ap.add_argument('--allow-sealed', action='store_true',
                    help='把"尾盘已封死涨停"的票也算成交（排板 100% 成交的乐观上界）')
    ap.add_argument('--auction-max-gap', type=float, default=5.0,
                    help='竞价买入允许的最大高开幅度%%（推荐原文"高开5%%以上不追"；给 999 就是不限制）')
    ap.add_argument('--sealed-auction', action='store_true',
                    help='封板票按推荐改走"次日竞价买入"，卖出基准顺延一根（最贴近系统本意）')
    ap.add_argument('--no-lots', action='store_true',
                    help='不取整到 100 股（等权满仓的敏感性检验，看整股噪声贡献了多少）')
    ap.add_argument('--detail', action='store_true')
    args = ap.parse_args()

    conn = sqlite3.connect(cfg.KLINE_HISTORY_DB_PATH, timeout=30)
    gconn = sqlite3.connect(cfg.SQLITE_DB_PATH, timeout=30)
    gconn.row_factory = sqlite3.Row
    try:
        recs, bars = load(conn, gconn, args.since)
        days = sorted({r['rec_date'] for r in recs})
        last_bar = max((b[-1][0] for b in bars.values() if b), default='')
        print(f'窗口 {args.since} .. {days[-1] if days else "?"}：{len(days)} 个推荐日、'
              f'{len(recs)} 条推荐（全部来自 14:45 收盘前快照）；日K最新到 {last_bar}')
        print('分仓规则：当日有几只推荐就均分可用现金，100 股一手；费用＝佣金万2.5(最低5元)'
              '+印花税千1(卖)+过户万0.1')
        way = ('按成交算（排板全成交，乐观上界）' if args.allow_sealed
               else ('按推荐改走次日竞价买入（资金多占一天）' if args.sealed_auction
                     else '尾盘买入、买不到就剔除（默认）'))
        print(f'封死涨停的推荐：{way}')
        bench, bdesc = _benchmark(conn, args.since)
        if bench is not None:
            print(f'同期背景（事后信息，只看行情冷热）：{bdesc} 等权 {bench:+.2f}%')
        print()
        rows = []
        tag = ('auction' if args.sealed_auction else
               'sealed_as_filled' if args.allow_sealed else 'tailclose')
        for mode, zh in (('open', '次日开盘卖'), ('close', '次日尾盘(收盘)卖')):
            orders, skipped = plan(recs, bars, mode, allow_sealed=args.allow_sealed,
                                   sealed_auction=args.sealed_auction,
                                   auction_max_gap=args.auction_max_gap)
            cash, trades, unsold, mdd = run(orders, args.capital,
                                             whole_lots=not args.no_lots)
            skipped = skipped + unsold
            rets = [t['ret'] for t in trades]
            win = sum(1 for v in rets if v > 0) / len(rets) * 100 if rets else 0
            wavg = (sum(t['proceeds'] - t['cost'] for t in trades)
                    / sum(t['cost'] for t in trades) * 100) if trades else 0
            if not rets:
                print(f'【{zh}】没有成交')
                continue
            print(f'【{zh}】期末 {cash:,.0f} 元（{cash / args.capital - 1:+.2%}）  '
                  f'成交 {len(trades)} 笔 / 跳过 {len(skipped)} 笔  '
                  f'胜率 {win:.1f}%  单笔均(等权) {sum(rets) / len(rets):+.2f}%  '
                  f'单笔均(按投入资金加权) {wavg:+.2f}%')
            worst = min(trades, key=lambda t: t['ret'])
            best = max(trades, key=lambda t: t['ret'])
            print(f'    最差一笔 {worst["name"]} {worst["rec_date"]} {worst["ret"]:+.2f}%，'
                  f'最好 {best["name"]} {best["rec_date"]} {best["ret"]:+.2f}%')
            print(f'    途中最大回撤 {mdd:.1f}%（按交易日逐日结算，含竞价口径多占的一天）')
            reasons = defaultdict(int)
            for _r, why in skipped:
                reasons[why.split('，')[0][:22]] += 1
            print(f'    跳过构成：{dict(reasons)}')
            note = defaultdict(int)
            for t in trades:
                if t['note']:
                    note[t['note'][:24]] += 1
            print(f'    带口径提示的成交：{dict(note) or "无"}')
            rows.append((zh, cash, trades))
            if args.detail:
                out = os.path.join(os.path.dirname(cfg.SQLITE_DB_PATH), 'analysis_out',
                                  f'rec_exit_{tag}_{mode}.json')
                os.makedirs(os.path.dirname(out), exist_ok=True)
                json.dump(trades, open(out, 'w', encoding='utf-8'),
                          ensure_ascii=False, indent=1)
                print(f'    明细已写 {out}')
        print()
        if len(rows) == 2:
            a, b = rows[0][1], rows[1][1]
            print(f'两种卖法差：{a - b:+,.0f} 元（开盘卖 - 尾盘卖），'
                  f'对应同期收益差 {(a - b) / args.capital * 100:+.2f} 个点')
    finally:
        conn.close()
        gconn.close()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    main()
