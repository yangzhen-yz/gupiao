"""rec_sim.py - 历史推荐的买卖口径模拟器（2026-10-18 从 scripts/rec_exit_sim.py 抽出来）

为什么要抽成模块：用户要把回测曲线放进页面。曲线留在脚本里就只有一种用法——
命令行跑一次、看一眼、下次再跑。而"页面一套实现、脚本另一套实现"是这个仓库
最容易出事的地方：两边口径悄悄分叉之后，页面上那个数和日志里那个数对不上，
谁都不知道该信哪个。所以现在**只有一份实现**在这里，`scripts/rec_exit_sim.py`
退化成命令行壳子，页面调的是同一个函数。

数据源是真实推荐记录（`daily_recommendations`，全部 created_at=14:45 —— 当天收盘前
最后一次快照），所以它回答的是"系统真的推了谁"；当天没被推的票依然看不见，
这是它和 pool_backtest.py（拿 r20 代理海选）最大的区别。

三条最容易骗人的口径，这里都按"现实里做不到就不算"处理
------------------------------------------------------
  1. **尾盘已封死涨停 = 买不到**：默认直接剔除并计数（`entry='tailclose'`）。
     系统偏爱强势票，126 条里有 64 条当天封死——把这些当成"我买到了"，
     三个月能从 -25% 变成 +25%，这种"改进"是假的。
     但完全不处理也不对：卡片对这些票的本意是**次日竞价买**，所以另有
     `entry='auction'` 按推荐顺延一根（并执行卡片写的"高开 5% 以上不追"纪律）。
     `entry='filled'` 是排板 100% 成交的上界，只在比较"错过多少"时用。
  2. **卖不掉就顺延**：次日一字跌停开盘卖不出去 → 顺延到下一根K线。
  3. **前复权 vs 当时实盘价不是一个尺度**：记录价落在当日 [最低, 最高] 之外
     说明期间除过权，这时买入价退回当日复权收盘并计数（比"与收盘差多少"可靠，
     差 1% 以内也可能已经越界）。

分仓与资金：当日有几只推荐就均分**当时可用**现金，100 股一手（高价票因此会自动少买，
这是真实约束不是 bug）；卖出资金当日可用，所以下午能接早盘的回款。
费用：佣金万 2.5（单边最低 5 元）+ 印花税千 1（卖出）+ 过户费万 0.1（双边）。
"""
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

FEE_RATE = 0.00025      # 佣金（单边）
FEE_MIN = 5.0           # 单笔最低佣金
STAMP = 0.001           # 印花税（卖出）
TRANSFER = 0.00001      # 过户费（双边）

ENTRIES = {
    'tailclose': '尾盘买入（封板买不到就剔除）',
    'auction': '按推荐执行（封板票次日竞价买·高开>5%不追）',
    'filled': '排板全成交（上界，现实做不到）',
}
MODES = {'open': '次日开盘卖', 'close': '次日尾盘卖'}
DEFAULT_CURVES = ('auction_open', 'auction_close')


def _fee_buy(amount: float) -> float:
    return max(FEE_MIN, amount * FEE_RATE) + amount * TRANSFER


def _fee_sell(amount: float) -> float:
    return max(FEE_MIN, amount * FEE_RATE) + amount * STAMP + amount * TRANSFER


def _limit_pct(code: str) -> float:
    return 20.0 if code.startswith(('30', '68')) else 10.0


def load(since: str) -> Tuple[List[Dict], Dict[str, List[tuple]]]:
    """读推荐记录 + 这些票的完整日K（前复权）。"""
    import sqlite3
    from app.config import KLINE_HISTORY_DB_PATH, SQLITE_DB_PATH
    gconn = sqlite3.connect(SQLITE_DB_PATH, timeout=30)
    gconn.row_factory = sqlite3.Row
    kconn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=30)
    try:
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
        bars = {}
        for code in sorted({(r['symbol'] or '')[2:] for r in recs}):
            bars[code] = kconn.execute(
                'SELECT date, open, high, low, close FROM stock_daily WHERE symbol=? '
                'ORDER BY date', (code,)).fetchall()
        return recs, bars
    finally:
        gconn.close()
        kconn.close()


def plan(recs: List[Dict], bars: Dict[str, List[tuple]], mode: str, entry: str = 'tailclose',
         auction_max_gap: float = 5.0) -> Tuple[List[Dict], List[Tuple[Dict, str]]]:
    """把每条推荐翻译成"哪天买、哪天卖、什么价"，先不谈钱 —— 资金循环交给 run()。

    mode: 'open' 次日开盘卖 / 'close' 次日尾盘（收盘）卖
    entry: 见 ENTRIES；auction_max_gap 是"高开多少就不追"的纪律阈值（999=不限制）
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
            rp = r['price'] or 0
            if rp and l * 0.99 <= rp <= h * 1.01:
                price, entry_note = rp, ''
            else:
                price = c
                entry_note = ('记录价越出当日高低区间(期间除权)→买入价改用当日复权收盘'
                              if rp else '记录价缺失→买入价用复权收盘')
            sealed = (abs(h - c) < 1e-6 and chg >= lim * 0.97)
            if sealed and entry == 'auction':
                if idx + 2 >= len(series):
                    skipped.append((r, '次日竞价买入但次次日无K线'))
                    continue
                o1, h1, l1, c1 = series[idx + 1][1], series[idx + 1][2], series[idx + 1][3], series[idx + 1][4]
                if abs(o1 - h1) < 1e-6 and abs(o1 - l1) < 1e-6 and (c1 / c - 1) * 100 >= lim * 0.97:
                    skipped.append((r, '次日一字开，竞价也买不到'))
                    continue
                gap = (o1 / c - 1) * 100
                if gap > auction_max_gap:
                    # 卡片原文就写着"高开 5% 以上不追"。这条纪律很值钱：共进股份 07-20
                    # 竞价 +10.0% 开盘、当天崩到 -10.3%，按纪律不追就不会有那笔 -20%。
                    skipped.append((r, f'次日竞价高开 {gap:.1f}%＞{auction_max_gap:g}%，按纪律不追'))
                    continue
                price, entry_note = o1, '封板票按推荐次日竞价买入'
                buy_date = series[idx + 1][0]
                idx += 1                      # 买卖基准整体后移一根
                sealed = False
            if sealed and entry != 'filled':
                skipped.append((r, '尾盘已封死涨停，排板未必成交'))
                continue
            if idx + 1 >= len(series):
                skipped.append((r, '次日无K线(未成交/停牌)'))
                continue
            sell_i = idx + 1
            shifted = ''
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
            if price <= 0 or exit_price <= 0:
                skipped.append((r, '价格数据为 0（停牌/缺数据）'))
                continue
            orders.append({'rec_date': d, 'buy_date': buy_date, 'sell_date': series[sell_i][0],
                           'entry_price': price, 'exit_price': exit_price, 'r': r,
                           'symbol': r['symbol'], 'name': r['name'], 'score': r['score'],
                           'note': entry_note or shifted})
    return orders, skipped


def run(orders: List[Dict], capital: float, whole_lots: bool = True) -> Dict:
    """按交易日推进：先结算当天到期的卖出，再用**当时可用**的现金买当天的单。

    这一步不能省：竞价口径一笔占两天，若把每笔当成"当天买当天回款"来复利，
    资金周转过快、期末会明显偏高。返回里带逐日净值序列（页面画曲线用的就是它）。
    """
    buy_days = defaultdict(list)
    for o in orders:
        buy_days[o['buy_date']].append(o)
    days = sorted({o['buy_date'] for o in orders} | {o['sell_date'] for o in orders})
    cash, open_pos, trades, unsold = capital, [], [], []
    peak, mdd = capital, 0.0
    nav: List[Dict] = []
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
                # whole_lots=False 是敏感性检验：100 股一手会让高价票自动少买、低价票多买，
                # 等于给权重掺了取整噪声。要看"等权满仓"的真实结果就得放开整股。
                shares = (int(budget / o['entry_price'] / 100) * 100 if whole_lots
                          else budget / o['entry_price'])
                if shares <= 0:
                    unsold.append((o['r'], f"一手要 {o['entry_price'] * 100:,.0f} 元，份额不够"))
                    continue
                cost = shares * o['entry_price'] + _fee_buy(shares * o['entry_price'])
                while cost > cash and shares > 0:
                    shares = shares - 100 if whole_lots else shares * 0.98
                    cost = (shares * o['entry_price'] + _fee_buy(shares * o['entry_price'])
                            if shares > 0 else 0)
                if shares <= 0:
                    unsold.append((o['r'], '现金不足一手'))
                    continue
                proceeds = shares * o['exit_price'] - _fee_sell(shares * o['exit_price'])
                cash -= cost
                open_pos.append({**o, 'shares': shares, 'cost': cost, 'proceeds': proceeds,
                                 'ret': (proceeds / cost - 1) * 100})
        invested = sum(p['cost'] for p in open_pos)
        nav.append({'date': day, 'nav': round(cash + invested, 2),
                    'positions': len(open_pos), 'cash': round(cash, 2)})
        peak = max(peak, cash + invested)
        mdd = max(mdd, (peak - (cash + invested)) / peak * 100)
    for p in open_pos:                       # 日历尽头还没卖出的（最后一天之后无K线），按口径价收尾
        cash += p['proceeds']
        trades.append(p)
        nav.append({'date': p['sell_date'], 'nav': round(cash, 2), 'positions': 0,
                    'cash': round(cash, 2)})
    for p in trades:
        p.pop('r', None)
    return {'end': round(cash, 2), 'trades': trades, 'unsold': unsold,
            'mdd': round(mdd, 2), 'nav': nav}


def _curve_stats(entry: str, mode: str, recs, bars, capital: float,
                 auction_max_gap: float, whole_lots: bool) -> Dict:
    orders, skipped = plan(recs, bars, mode, entry=entry, auction_max_gap=auction_max_gap)
    res = run(orders, capital, whole_lots=whole_lots)
    trades, unsold = res['trades'], skipped + [(o, w) for o, w in res['unsold']]
    rets = [t['ret'] for t in trades]
    cost_sum = sum(t['cost'] for t in trades)
    reasons = defaultdict(int)
    for _r, why in unsold:
        reasons[why.split('，')[0].split('(')[0][:24]] += 1
    worst = min(trades, key=lambda t: t['ret']) if trades else None
    best = max(trades, key=lambda t: t['ret']) if trades else None
    return {
        'key': f'{entry}_{mode}',
        'entry': entry, 'mode': mode,
        'entryLabel': ENTRIES[entry], 'modeLabel': MODES[mode],
        'label': f'{ENTRIES[entry]} · {MODES[mode]}',
        'default': (entry, mode) in [(e, m) for e, m in
                                     [tuple(k.rsplit('_', 1)) for k in DEFAULT_CURVES]],
        'nav': res['nav'],
        'end': res['end'],
        'retPct': round((res['end'] / capital - 1) * 100, 2),
        'mdd': res['mdd'],
        'trades': len(trades),
        'skipped': len(unsold),
        'winRate': round(sum(1 for v in rets if v > 0) / len(rets) * 100, 1) if rets else None,
        'avgEqual': round(sum(rets) / len(rets), 2) if rets else None,
        'avgWeighted': round((sum(t['proceeds'] - t['cost'] for t in trades) / cost_sum * 100), 2)
        if cost_sum else None,
        'best': {k: best[k] for k in ('name', 'rec_date', 'ret')} if best else None,
        'worst': {k: worst[k] for k in ('name', 'rec_date', 'ret')} if worst else None,
        'skipReasons': dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        'notes': {t['name']: t['note'] for t in trades if t['note']},
    }


def simulate(months: int = 3, capital: float = 100000.0, since: Optional[str] = None,
             auction_max_gap: float = 5.0, whole_lots: bool = True,
             with_benchmark: bool = False) -> Dict:
    """一次算完六种口径（三种入口 × 两种卖法），给页面用。

    默认窗口=近 3 个月（与用户提的问题一致）；`since` 可以指定起始日。
    全程只读两个 SQLite 库，耗时约 0.2s（基准背景另加 ~2.5s，默认不算）。
    """
    if not since:
        since = (datetime.now() - timedelta(days=months * 31)).strftime('%Y-%m-%d')
    recs, bars = load(since)
    days = sorted({r['rec_date'] for r in recs})
    last_bar = max((b[-1][0] for b in bars.values() if b), default='')
    out = {
        'window': {
            'since': since, 'until': days[-1] if days else '',
            'recDays': len(days), 'recCount': len(recs),
            'lastKlineDate': last_bar, 'months': months,
        },
        'capital': capital,
        'auctionMaxGap': auction_max_gap,
        'wholeLots': whole_lots,
        'curves': [],
        'generatedAt': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    for entry in ('tailclose', 'auction', 'filled'):
        for mode in ('open', 'close'):
            out['curves'].append(_curve_stats(entry, mode, recs, bars, capital,
                                              auction_max_gap, whole_lots))
    if with_benchmark:
        out['benchmark'] = benchmark(since)
    return out


def benchmark(since: str) -> Dict:
    """同期背景：数据范围内等权买入持有。

    注意这是**事后信息**（范围按今天的市值/流动性表判定），只用来看行情冷热，
    不能当成"策略跑赢/跑输"的严格基准 —— 所以页面默认不请求它（要多花 2.5 秒）。
    """
    import sqlite3
    from app.config import KLINE_HISTORY_DB_PATH
    conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=30)
    try:
        kdays = [r[0] for r in conn.execute(
            'SELECT DISTINCT date FROM stock_daily WHERE date>=? ORDER BY date', (since,))]
        if len(kdays) < 2:
            return {'available': False}
        a, b = kdays[0], kdays[-1]
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
            return {'available': False}
        return {'available': True, 'from': a, 'to': b, 'count': len(got),
                'avgPct': round(sum(got) / len(got), 2), 'scopeText': desc}
    finally:
        conn.close()


# ---------- 日常监控：把三入口曲线变成每天一个点 ----------
#
# 为什么要有这张表（2026-10-18，实施计划 P0-0）：路线图里所有"放宽/多推/改模式"的提案，
# 判据都写成"rec_sim auction 口径不恶化"。但 simulate() 是一次性现算的，
# 没有历史点就没法回答"上周那次改动之后有没有恶化"——只能重新跑一遍，
# 而重新跑用的是**今天为止的全部推荐**，窗口一直在动，前后两天的数字根本不可比。
# 所以每天固定时刻算一次、把窗口和数字一起存下来，之后的比较才有同一个基准。

_DDL_DAILY = '''CREATE TABLE IF NOT EXISTS rec_sim_daily (
    run_date   TEXT NOT NULL,
    key        TEXT NOT NULL,
    entry      TEXT NOT NULL,
    mode       TEXT NOT NULL,
    end        REAL,
    ret_pct    REAL,
    mdd        REAL,
    trades     INTEGER,
    skipped    INTEGER,
    win_rate   REAL,
    avg_equal  REAL,
    avg_weighted REAL,
    window_since TEXT,
    window_until TEXT,
    capital    REAL,
    created_at TEXT,
    PRIMARY KEY (run_date, key)
)'''


def record_daily(capital: float = 100000.0, months: int = 3) -> Dict:
    """每日跑一次六种口径并落库（同一 run_date 重跑覆盖自己）。返回摘要。"""
    from db.database import get_db_conn
    now = datetime.now()
    run_date = now.strftime('%Y-%m-%d')
    res = simulate(months=months, capital=capital)
    rows = [(run_date, c['key'], c['entry'], c['mode'], c['end'], c['retPct'], c['mdd'],
             c['trades'], c['skipped'], c['winRate'], c['avgEqual'], c['avgWeighted'],
             res['window']['since'], res['window']['until'], capital,
             now.strftime('%Y-%m-%d %H:%M:%S')) for c in res['curves']]
    conn = get_db_conn()
    try:
        conn.execute(_DDL_DAILY)
        conn.execute('DELETE FROM rec_sim_daily WHERE run_date=?', (run_date,))
        conn.executemany('INSERT INTO rec_sim_daily (run_date,key,entry,mode,end,ret_pct,mdd,'
                         'trades,skipped,win_rate,avg_equal,avg_weighted,window_since,'
                         'window_until,capital,created_at) VALUES (' +
                         ','.join('?' * 16) + ')', rows)
        conn.commit()
    finally:
        conn.close()
    head = {c['key']: c for c in res['curves']}
    a, b = head.get('auction_open'), head.get('tailclose_open')
    sealed = sum(v for k, v in ((b.get('skipReasons') or {}).items()) if '封死' in k) if b else 0
    summary = (f"auction开盘卖 {a['retPct']:+.2f}%（{a['trades']}笔）vs "
               f"尾盘硬吃 {b['retPct']:+.2f}%（{b['trades']}笔，其中 {sealed} 条推荐时已封死=买不到）"
               if a and b else '无数据')
    print(f'[可成交性监控] {run_date} 窗口 {res["window"]["since"]}~{res["window"]["until"]}：{summary}')
    return {'runDate': run_date, 'summary': summary, 'curves': len(rows),
            'window': res['window']}


def daily_history(days: int = 40) -> List[Dict]:
    """最近的每日监控点（前端画"入口口径差"随时间的走势用）。"""
    from db.database import get_db_conn
    conn = get_db_conn()
    try:
        conn.execute(_DDL_DAILY)
        latest = [r[0] for r in conn.execute(
            'SELECT DISTINCT run_date FROM rec_sim_daily ORDER BY run_date DESC LIMIT ?',
            (days,)).fetchall()]
        if not latest:
            return []
        ph = ','.join('?' * len(latest))
        return [dict(r) for r in conn.execute(
            f'SELECT * FROM rec_sim_daily WHERE run_date IN ({ph}) '
            'ORDER BY run_date, key', sorted(latest)).fetchall()]
    finally:
        conn.close()
