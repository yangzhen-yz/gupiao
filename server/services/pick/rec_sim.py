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
  1. **封死涨停 = 买不到**：默认直接剔除并计数（`entry='tailclose'`）。
     系统偏爱强势票，126 条里有 64 条当天封死——把这些当成"我买到了"，
     三个月能从 -25% 变成 +25%，这种"改进"是假的。
     但完全不处理也不对：卡片对这些票的本意是**次日竞价买**，所以另有
     `entry='auction'` 按推荐顺延一根（并执行卡片写的"高开 5% 以上不追"纪律）。
     `entry='filled'` 是排板 100% 成交的上界，只在比较"错过多少"时用。
     2026-10-20 把"封死"的判据换成**价位**：只要当日最低价低于涨停价，就说明存在过
     可成交的价格（炸过板），不再一律剔除；一字/T 字全天没低下去过才算买不到。
  2. **卖不掉就顺延**：跌停那天如果全天最高价都没高过跌停价（真一字跌停），挂着也出不去，
     顺延到下一根K线。2026-10-20 之前这条**只对开盘卖生效**，尾盘卖（`mode='close'`）连检查都没有,
     等于假设一字跌停也能按收盘价卖掉——回撤因此被系统性低估。现在两种卖法都判。
  3. **前复权 vs 当时实盘价不是一个尺度**：记录价落在当日 [最低, 最高] 之外
     说明期间除过权，这时买入价退回当日复权收盘并计数（比"与收盘差多少"可靠，
     差 1% 以内也可能已经越界）。

分仓与资金：当日有几只推荐就均分**当时可用**现金，100 股一手（高价票因此会自动少买，
这是真实约束不是 bug）；卖出资金当日可用，所以下午能接早盘的回款。
费用：佣金万 5（单边最低 5 元）+ 印花税千 1（卖出）+ 过户费万 0.1（双边）。
"""
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from app.config import (REC_SIM_BOARD_PRICE_V2, REC_SIM_DEFER_LIMIT_DOWN, REC_SIM_BOARD_EPS,
                        REC_SIM_SIZING_V2, REC_SIM_RISK_PER_TRADE, REC_SIM_STOP_PCT,
                        REC_SIM_MAX_POSITION_PCT, REC_SIM_MAX_EXPOSURE_PCT)

FEE_RATE = 0.0005       # 佣金（单边，万五）
FEE_MIN = 5.0           # 单笔最低佣金
STAMP = 0.001           # 印花税（卖出）
TRANSFER = 0.00001      # 过户费（双边）

ENTRIES = {
    'tailclose': '尾盘买入（封板买不到就剔除）',
    'auction': '按推荐执行（封板票次日竞价买·高开>5%不追）',
    'filled': '排板全成交（上界，现实做不到）',
    # 2026-11-02 加：只由离线对照脚本调用，不进 simulate() 的六口径、页面不下发——
    # 排板挂涨停价，开板那一刻就成交，收盘没封回去的当天就浮亏（旧三条都没算这笔）
    'paiban': '排板真实成交（开板即成交在涨停价·一字排不进）',
}
MODES = {'open': '次日开盘卖', 'close': '次日尾盘卖'}
DEFAULT_CURVES = ('auction_open', 'auction_close')


def _fee_buy(amount: float) -> float:
    return max(FEE_MIN, amount * FEE_RATE) + amount * TRANSFER


def _fee_sell(amount: float) -> float:
    return max(FEE_MIN, amount * FEE_RATE) + amount * STAMP + amount * TRANSFER


def _limit_pct(code: str) -> float:
    return 20.0 if code.startswith(('30', '68')) else 10.0


def _setting(key: str, default):
    """覆盖表里有这个键就现读（复盘子页可热改），读不到退回 config 默认值。"""
    try:
        from services.support import app_settings
        if key in [s['key'] for s in app_settings.SPEC]:
            return app_settings.value(key)
    except Exception:
        pass
    return default


def _limit_prices(prev_close: float, lim_pct: float) -> Tuple[float, float]:
    """按前收盘算出当日涨停价/跌停价（交易所口径：四舍五入到 0.01）。

    不直接用"涨幅≥9.7%"这种百分比近似：4 元以下的票四舍五入后涨停幅度会明显偏离 10%，
    而"封没封死"恰恰要拿**具体价位**比，差一分就判错。
    """
    if not prev_close or prev_close <= 0:
        return 0.0, 0.0
    return (round(prev_close * (1 + lim_pct / 100.0), 2),
            round(prev_close * (1 - lim_pct / 100.0), 2))


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

    封板判定（2026-10-20 改，两个独立开关、都关掉即回到旧行为）：
      · `rec_sim_board_price_v2` 开：封不封死看**当日是否存在突破价成交**——
        买入侧「最低价 < 涨停价」即炸过板、存在过可买价（炸板回封算可买，但按收盘价买
        仍需排队，note 里标出来）；卖出侧「最高价 > 跌停价」即存在过可卖价。
        关：用旧口径（收盘==当日最高价 且 涨幅≥97%限额 视为封死）。
      · `rec_sim_defer_limit_down` 开：真一字跌停卖不掉就顺延到下一根K线，
        **open 和 close 两种卖法都判**（旧口径只判 open）。
    """
    v2 = bool(_setting('rec_sim_board_price_v2', REC_SIM_BOARD_PRICE_V2))
    defer = bool(_setting('rec_sim_defer_limit_down', REC_SIM_DEFER_LIMIT_DOWN))
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
            # 两档判据不能合成一个：
            #   sealed_at_close＝收盘仍钉在涨停价（卡片的本意是"次日竞价关注"，auction 入口该走那条）；
            #   sealed_hard＝全天一分都没低于涨停价（真一字/T字，谁都买不到，只有它才该被剔除）。
            # 旧实现用同一个 sealed 表达这两件事；v2 只放宽"能不能买"，如果继续共用一个变量，
            # 就会把 auction 的分支一起带偏（实测：剔除从 23 掉到 6，那不是更真实，
            # 而是涨停票不再走次日竞价、改成按当天涨停价买——反而更乐观）。
            lu_buy, ld_buy = _limit_prices(prev_c or 0, lim)
            if lu_buy > 0:
                sealed_at_close = (abs(h - c) < 1e-6 and c >= lu_buy - REC_SIM_BOARD_EPS)
            else:                                   # 没有前收（首日/缺数据）退回旧的百分比近似
                sealed_at_close = (abs(h - c) < 1e-6 and chg >= lim * 0.97)
            sealed_hard = ((l >= lu_buy - REC_SIM_BOARD_EPS) if (v2 and lu_buy > 0)
                           else sealed_at_close)
            sealed_note = ''
            if v2 and lu_buy > 0 and sealed_at_close and not sealed_hard:
                sealed_note = '盘中炸过板·收盘回封涨停（按收盘价买仍要排队）'
            sealed = sealed_at_close
            if entry == 'paiban':
                # 排板挂的是**涨停价**，不是收盘价：只有当天真的开过板（价格跌破涨停）才轮得到
                # 你成交，成交那一刻就是涨停价。收盘还封着的票两者数值相同（收盘=涨停价），
                # 但**盘中触板、收盘没封回去**的那部分，你是买在涨停价、当天就浮亏——
                # 现有三条口径都没算这笔：tailclose 按收盘价买，等于假设你能在炸板后的低价买到，
                # 是偏乐观的；filled 干脆假设一字也排得到，是上界。这条是"排队排到"的真实版。
                if lu_buy <= 0:
                    skipped.append((r, '缺前收→算不出涨停价，排板无法判定'))
                    continue
                if sealed_hard:
                    skipped.append((r, '一字涨停（全天无低于涨停价的成交）→排板排不进'))
                    continue
                if h < lu_buy - REC_SIM_BOARD_EPS:
                    skipped.append((r, '全天未触板→没有板可排'))
                    continue
                price = lu_buy
                entry_note = ('排板成交在涨停价（收盘未回封，当天 %+.1f%%）'
                              % ((c / lu_buy - 1) * 100)
                              if c < lu_buy - REC_SIM_BOARD_EPS else '排板成交在涨停价（收盘回封）')
                sealed = False
                sealed_hard = False
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
                # 已经按次日竞价买入了，两档"封死"判定都要清空——
                # 只清 sealed 不清 sealed_hard 的话，下面那道剔除会把这条分支买进来的票再剔一遍
                # （自测就是这么抓出来的：auction 的笔数从 101 掉到 60，跟 tailclose 一模一样）。
                sealed = False
                sealed_hard = False
            if sealed_hard and entry != 'filled':
                skipped.append((r, '一字涨停（全天无低于涨停价的成交）→买不到' if v2
                                else '尾盘已封死涨停，排板未必成交'))
                continue
            if idx + 1 >= len(series):
                skipped.append((r, '次日无K线(未成交/停牌)'))
                continue
            sell_i = idx + 1
            shifted = ''
            while sell_i < len(series):
                so, sh, sl, sc = (series[sell_i][1], series[sell_i][2],
                                  series[sell_i][3], series[sell_i][4])
                spc = series[sell_i - 1][4]
                schg = (sc / spc - 1) * 100 if spc else 0.0
                _, ld = _limit_prices(spc, lim)
                # 卖不卖得掉：v2 看"当天有没有过高于跌停价的成交"（最高价 > 跌停价 就能卖），
                # 最高价一分都没高出去 = 真一字跌停，挂着也出不去 → 顺延到下一根。
                # 旧口径只认"open==low==close 且跌≥97%限额"的那种一字，而且**只对开盘卖判**，
                # 尾盘卖（mode='close'）连这条都没有 —— 于是一字跌停照样按收盘价结算，回撤被低估。
                # defer 开关控制"这条是否也管尾盘卖"；v2+defer 都关掉 = 逐字节回到旧行为。
                apply_stuck = (mode == 'open') or defer
                stuck = False
                if apply_stuck:
                    if v2 and ld > 0:
                        stuck = (sh <= ld + REC_SIM_BOARD_EPS)
                        if stuck:
                            shifted = '一字跌停（全天无高于跌停价的成交）→顺延到下一根'
                    else:
                        stuck = (abs(so - sl) < 1e-6 and abs(so - sc) < 1e-6
                                 and schg <= -lim * 0.97)
                        if stuck:
                            shifted = '次日一字跌停→顺延到下一根'
                if not stuck:
                    break
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
                           # note 只做展示与复盘说明，不参与任何成交/收益计算；
                           # 这里把"越界改价""炸板回封""跌停顺延"三段都留下来（旧实现只留第一段），
                           # 是因为 v2 之后同一笔可能既改了买入价又被顺延，只留一个会误导弹道。
                           'note': '；'.join([x for x in ((entry_note or sealed_note), shifted) if x])})
    return orders, skipped


def run(orders: List[Dict], capital: float, whole_lots: bool = True,
        sizing: Optional[bool] = None, sizing_opts: Optional[Dict] = None) -> Dict:
    """按交易日推进：先结算当天到期的卖出，再用**当时可用**的现金买当天的单。

    这一步不能省：竞价口径一笔占两天，若把每笔当成"当天买当天回款"来复利，
    资金周转过快、期末会明显偏高。返回里带逐日净值序列（页面画曲线用的就是它）。

    仓位规则（2026-10-20 加，默认关）：`sizing=None` 时读覆盖表/config；关掉就是老规则
    ——`budget = cash / 当天要买的只数`，满仓等分、不留缓冲。打开后每笔仓位取
    「风险预算÷止损幅度」与「单票上限」的较小值，再被「当天剩余总敞口」和现金夹住。
    对照脚本可以直接传 sizing=True/False 与 sizing_opts 覆盖参数，不必改配置。
    """
    opts = dict(sizing_opts or {})
    if sizing is None:
        sizing = bool(_setting('rec_sim_sizing_v2', REC_SIM_SIZING_V2))
    # ⚠️ 单位：覆盖表 `value()` 返回的是**界面单位（百分数）**——SPEC 里 scale=100 会把
    # config 的 0.015 换算成 1.5。所以这里四个值一律按百分数对待：risk÷stop 无量纲，正好就是
    # 仓位比例；cap 与 exposure_cap 要除以 100 才回到比例。`sizing_opts` 传参也用同一单位
    # （risk=1.5、stop=8、cap=25、exposure=60），别混着传 0.015 进去。
    risk = float(opts.get('risk', _setting('rec_sim_risk_per_trade', REC_SIM_RISK_PER_TRADE * 100)))
    stop = float(opts.get('stop', _setting('rec_sim_stop_pct', REC_SIM_STOP_PCT * 100)))
    cap = float(opts.get('cap', _setting('rec_sim_max_position_pct',
                                         REC_SIM_MAX_POSITION_PCT * 100))) / 100.0
    exposure_cap = float(opts.get('exposure', _setting('rec_sim_max_exposure_pct',
                                                        REC_SIM_MAX_EXPOSURE_PCT * 100))) / 100.0
    stats = {'sizing': sizing, 'risk': risk, 'stop': stop, 'cap': cap, 'exposure': exposure_cap,
             'per_trade_pct': round(min(risk / stop, cap) * 100, 2) if stop > 0 else None,
             'cap_hits': 0, 'no_room': 0, 'max_ratio': 0.0, 'one_stock_days': 0}
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
            equity = cash + sum(p['cost'] for p in open_pos)      # 买之前当天的净值（成本法）
            held = equity - cash                                   # 已被持仓占用的敞口
            if len(todays) == 1:
                stats['one_stock_days'] += 1                        # "当天只 1 只→一笔押满"那种日子
            if sizing:
                # 每笔目标仓位 = min(风险预算÷止损幅度, 单票上限)，
                # 再被"当天剩余总敞口"和可用现金夹住；关掉 sizing 就一个字都不受影响
                per = equity * min(risk / stop if stop > 0 else 1.0, cap)
                room = max(0.0, equity * exposure_cap - held)
                budget = min(per, room / len(todays), cash / len(todays))
                if budget <= 0:
                    stats['no_room'] += len(todays)
                    for o in todays:
                        unsold.append((o['r'], '总敞口已满（风险预算口径），今天不加仓'))
                    continue
            else:
                budget = cash / len(todays)
            committed = 0.0
            for o in todays:
                budget_i = budget
                if sizing:
                    # 逐笔扣减：除了单票上限，还要保证"当天累计敞口"不越 exposure_cap 那条线
                    left = max(0.0, equity * exposure_cap - held - committed)
                    budget_i = min(budget, left, cash)
                    if budget_i <= 0:
                        stats['cap_hits'] += 1
                        unsold.append((o['r'], '单票/总敞口上限已满'))
                        continue
                # whole_lots=False 是敏感性检验：100 股一手会让高价票自动少买、低价票多买，
                # 等于给权重掺了取整噪声。要看"等权满仓"的真实结果就得放开整股。
                shares = (int(budget_i / o['entry_price'] / 100) * 100 if whole_lots
                          else budget_i / o['entry_price'])
                if shares <= 0:
                    unsold.append((o['r'], f"一手要 {o['entry_price'] * 100:,.0f} 元，份额不够"))
                    continue
                cost = shares * o['entry_price'] + _fee_buy(shares * o['entry_price'])
                while cost > cash and shares > 0:
                    _prev = shares
                    shares = shares - 100 if whole_lots else shares * 0.98
                    # 两道收手，缺一都会出事（2026-11-07 实拍：有人打
                    # `/api/recommend-sim?capital=1&gap=0.5&lots=false`，这个 while 再也不出来，
                    # 而它跑在事件循环上 ⇒ 从那一刻起整个后端不响应任何请求，只能重启）：
                    # ① 佣金有最低 5 元（`FEE_MIN`），现金连最低佣金都不够时，再怎么缩股都买不进去，
                    #    老写法要一路缩到浮点下溢才可能出来；
                    # ② `whole_lots=False` 是乘法缩股，缩到最小次正规数（5e-324）后 `×0.98` 舍回它自己，
                    #    `shares` 不再单调下降 —— 这才是真正的死循环点。
                    if shares >= _prev or _fee_buy(0.0) > cash:
                        shares = 0.0
                        break
                    cost = (shares * o['entry_price'] + _fee_buy(shares * o['entry_price'])
                            if shares > 0 else 0)
                if shares <= 0:
                    unsold.append((o['r'], '现金不足一手'))
                    continue
                proceeds = shares * o['exit_price'] - _fee_sell(shares * o['exit_price'])
                cash -= cost
                committed += cost
                if equity > 0:
                    # 两种模式都统计：关掉 sizing 时这就是"现状单笔最大占净值比"的实测证据
                    stats['max_ratio'] = max(stats['max_ratio'], cost / equity)
                open_pos.append({**o, 'shares': shares, 'cost': cost, 'proceeds': proceeds,
                                 'ret': (proceeds / cost - 1) * 100})
        invested = sum(p['cost'] for p in open_pos)
        _eq = cash + invested
        nav.append({'date': day, 'nav': round(_eq, 2),
                    'positions': len(open_pos), 'cash': round(cash, 2),
                    'exposurePct': round(invested / _eq * 100, 1) if _eq > 0 else 0.0})
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
            'mdd': round(mdd, 2), 'nav': nav, 'sizing': stats}


def _curve_stats(entry: str, mode: str, recs, bars, capital: float,
                 auction_max_gap: float, whole_lots: bool,
                 sizing: Optional[bool] = None, sizing_opts: Optional[Dict] = None) -> Dict:
    orders, skipped = plan(recs, bars, mode, entry=entry, auction_max_gap=auction_max_gap)
    res = run(orders, capital, whole_lots=whole_lots, sizing=sizing, sizing_opts=sizing_opts)
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
        # 仓位规则的实际参数与触发次数（关掉 sizing 时也要回传，页面才能显示"现在是老规则"）
        'sizing': res.get('sizing'),
    }


def simulate(months: int = 3, capital: float = 100000.0, since: Optional[str] = None,
             auction_max_gap: float = 5.0, whole_lots: bool = True,
             with_benchmark: bool = False, sizing: Optional[bool] = None,
             sizing_opts: Optional[Dict] = None) -> Dict:
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
                                              auction_max_gap, whole_lots,
                                              sizing=sizing, sizing_opts=sizing_opts))
    if with_benchmark:
        out['benchmark'] = benchmark(since)
    return out


def open_hold_excess(months: int = 6, since: Optional[str] = None,
                     hold_days: Tuple[int, ...] = (1, 3, 5, 10),
                     slippage_bps: int = 10) -> Dict:
    """次日开盘买入、持有 N 个交易日卖出的**可执行超额**（2026-10-22，P0 评估基座）。

    为什么单独算这一条：现有六条曲线是"次日就卖"的短线口径，回答不了
    "这些票拿 5 天到底行不行"。而 `daily_recommendations.price` 是推荐时点的参考价，
    用它当买入价会混入"当天已经涨了多少"的日内收益；这里统一改用**次日开盘价**，
    与实盘最容易执行的动作一致，再和同期范围内的等权基准（同样次日开盘买、持 N 日）
    逐日对齐，超额才不是 beta 冒充 alpha。

    口径：
      · 买入：推荐日下一交易日开盘；次日一字涨停买不到 → 跳过；
      · 卖出：买入后第 N 个交易日开盘；
      · 费用：买卖双边按 10 万名义本金计，和 rec_sim 主引擎同一套费率；
      · 滑点：买卖各按 slippage_bps 基点恶化成交价；默认 10bp/边是情景假设，不是实测；
      · 基准：选股范围内所有票，同一买入日开盘买、同一持有期开盘卖，等权平均；
      · 超额：推荐和基准都扣相同费用/滑点，只在相同买入日有基准时配对。

    此处历史股票范围按当前 selectable universe 回看，仍有幸存者偏差；前向口径另由
    candidate_pool_shadow 只使用当日留存的股票范围快照。
    """
    if type(slippage_bps) is not int or not 0 <= slippage_bps <= 50:
        raise ValueError('slippage_bps must be an integer from 0 to 50')
    if not since:
        since = (datetime.now() - timedelta(days=months * 31)).strftime('%Y-%m-%d')
    import sqlite3
    from app.config import KLINE_HISTORY_DB_PATH

    recs, bars = load(since)
    days = sorted({r['rec_date'] for r in recs})
    target_dates = set()
    per_day: Dict[int, List[Dict]] = {n: [] for n in hold_days}
    skipped: Dict[str, int] = {}

    def _skip(reason: str):
        skipped[reason] = skipped.get(reason, 0) + 1

    for r in recs:
        code = (r['symbol'] or '')[2:]
        series = bars.get(code) or []
        idx = next((i for i, b in enumerate(series) if b[0] == r['rec_date']), None)
        if idx is None:
            _skip('推荐日无K线')
            continue
        j = idx + 1
        if j >= len(series):
            _skip('次日无K线')
            continue
        b0, b1 = series[idx], series[j]
        o1, h1, l1, c1 = b1[1], b1[2], b1[3], b1[4]
        lim = _limit_pct(code)
        _, lu = _limit_prices(b0[4], lim)
        if (lu > 0 and abs(o1 - h1) < 1e-6 and abs(o1 - l1) < 1e-6
                and c1 >= lu - REC_SIM_BOARD_EPS):
            _skip('次日一字涨停买不到')
            continue
        if not o1 or o1 <= 0:
            _skip('次日开盘价无效')
            continue
        target_dates.add(b1[0])
        for n in hold_days:
            k = j + n
            if k >= len(series):
                _skip(f'持有{n}日K线不足')
                continue
            exit_px = series[k][1]
            if not exit_px or exit_px <= 0:
                _skip(f'持有{n}日开盘价无效')
                continue
            ret = _net_return_with_slippage(o1, exit_px, slippage_bps)
            per_day[n].append({'date': b1[0], 'ret': ret, 'month': b1[0][:7],
                               'code': code, 'entry': o1, 'exit': exit_px})

    # 基准：仍用选股范围内全部票、同一买入日、同一持有期，但改成"票 × 交易日"矩阵一次算完。
    # 旧版逐票两两配对 + 逐票算费，6 个月 30 万行来回搬实测 8 秒；矩阵法只搬一次数据、
    # 每只票顺着时间轴累加，实测约 2.5 秒。不引 numpy：venv 里没有，核心服务不为一个
    # 基准函数新增第三方依赖（2026-10-23 踩过：系统 Python 有 numpy、服务跑在 venv 里就 500）。
    bench: Dict[int, Dict[str, float]] = {n: {} for n in hold_days}
    scope_desc = ''
    if target_dates:
        conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=60)
        try:
            try:
                from services.data import universe
                scope = {s[-6:] for s in universe.eligible_symbols()}
                scope_desc = universe.describe_selectable()
            except Exception:
                scope, scope_desc = None, '（范围读取失败，按全库算）'
            kdays = [r[0] for r in conn.execute(
                'SELECT DISTINCT date FROM stock_daily WHERE date>=? ORDER BY date',
                (since,)).fetchall()]
            if scope:
                # 2462 只票塞不进 IN (...) 的参数上限，用临时表让 SQLite 自己 join。
                conn.execute('CREATE TEMP TABLE sc(symbol TEXT PRIMARY KEY)')
                conn.executemany('INSERT INTO sc VALUES (?)', [(s,) for s in scope])
                rows = conn.execute(
                    'SELECT d.symbol,d.date,d.open FROM stock_daily d '
                    'JOIN sc ON sc.symbol=d.symbol WHERE d.date>=? AND d.open>0',
                    (since,)).fetchall()
            else:
                rows = conn.execute(
                    'SELECT symbol,date,open FROM stock_daily '
                    'WHERE date>=? AND open>0', (since,)).fetchall()
        finally:
            conn.close()
        didx = {d: i for i, d in enumerate(kdays)}
        code_at: Dict[str, int] = {}
        mat: List[List[Optional[float]]] = []
        for sym, d, o in rows:
            j = didx.get(d)
            if j is None:
                continue
            ci = code_at.get(sym)
            if ci is None:
                ci = code_at[sym] = len(mat)
                mat.append([None] * len(kdays))
            mat[ci][j] = o
        sums = {n: [0.0] * len(kdays) for n in hold_days}
        counts = {n: [0] * len(kdays) for n in hold_days}
        for row in mat:
            for n in hold_days:
                if n >= len(kdays):
                    continue
                s, c = sums[n], counts[n]
                for i in range(len(kdays) - n):
                    a, b = row[i], row[i + n]
                    if a and b:
                        s[i] += _net_return_with_slippage(a, b, slippage_bps)
                        c[i] += 1
        for n in hold_days:
            if n >= len(kdays):
                continue
            for i in range(len(kdays) - n):
                if not counts[n][i]:
                    continue
                bench[n][kdays[i]] = sums[n][i] / counts[n][i]

    by_day = {}
    by_month: Dict[str, Dict[str, Dict]] = {}
    for n in hold_days:
        rows = per_day[n]
        paired = [x for x in rows if x['date'] in bench[n]]
        ex_list = [x['ret'] - bench[n][x['date']] for x in paired]
        bench_vals = [bench[n][x['date']] for x in paired]
        by_day[str(n)] = {
            'n': len(paired), 'totalN': len(rows), 'pairedN': len(paired),
            'winRate': round(sum(1 for x in paired if x['ret'] > 0) / len(paired) * 100, 1)
            if paired else None,
            'avg': round(sum(x['ret'] for x in paired) / len(paired), 2) if paired else None,
            'benchAvg': round(sum(bench_vals) / len(bench_vals), 2) if bench_vals else None,
            'excess': round(sum(ex_list) / len(ex_list), 2) if ex_list else None,
        }
        for x in paired:
            m = by_month.setdefault(x['month'], {}).setdefault(
                str(n), {'n': 0, 'sum': 0.0, 'win': 0, 'exc': [], 'bench': []})
            m['n'] += 1
            m['sum'] += x['ret']
            m['win'] += 1 if x['ret'] > 0 else 0
            if x['date'] in bench[n]:
                m['exc'].append(x['ret'] - bench[n][x['date']])
                m['bench'].append(bench[n][x['date']])
    for mon, d in by_month.items():
        for n, m in d.items():
            m['avg'] = round(m['sum'] / m['n'], 2) if m['n'] else None
            m['winRate'] = round(m['win'] / m['n'] * 100, 1) if m['n'] else None
            m['excess'] = round(sum(m['exc']) / len(m['exc']), 2) if m['exc'] else None
            del m['sum'], m['win'], m['exc'], m['bench']
    return {
        'available': bool(recs), 'since': since,
        'until': days[-1] if days else '', 'recCount': len(recs), 'days': list(hold_days),
        'byDay': by_day, 'byMonth': by_month, 'skipped': skipped,
        'scopeText': scope_desc,
        'slippageBpsPerSide': slippage_bps,
        'feeModel': '佣金单边万五（最低5元）+卖出印花税千一+双边过户费万0.1',
        'commissionRatePerSide': FEE_RATE,
        'commissionMinimum': FEE_MIN,
        'costModel': '费用按feeModel；双边滑点按指定基点恶化成交价。历史基准使用当前股票范围，存在幸存者偏差。',
        'generatedAt': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


def _rt_cost_pct(entry: float, exit_px: float) -> float:
    """双边费用占名义本金的比例（%）。按 10 万本金算，避免最低 5 元佣金扭曲小单。"""
    amt = 100000.0
    qty = amt / entry
    return (_fee_buy(amt) + _fee_sell(qty * exit_px)) / amt * 100.0


def _net_return_with_slippage(entry: float, exit_px: float, slippage_bps: int) -> float:
    """净收益率：买入价上浮、卖出价下调同一单边滑点，再扣双边费用。"""
    slip = slippage_bps / 10000.0
    entry_fill = entry * (1.0 + slip)
    exit_fill = exit_px * (1.0 - slip)
    gross = (exit_fill / entry_fill - 1.0) * 100.0
    return gross - _rt_cost_pct(entry_fill, exit_fill)


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
    conn = get_db_conn(wait_for_lock=True)
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
