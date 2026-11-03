"""情绪备源：不依赖东方财富的涨停/跌停家数与市场宽度（2026-10-28）

为什么要有这个模块：短线情绪温度计的五维里，涨停池/跌停池/涨跌家数三项**只吊在东财上**。
10-27、10-28 两个交易日东财 IP 风控未解（`clist` 0/26、0/11 全失败，`ulist` 0/3），
盘中情绪快循环一次都没出数（`market_sentiment_snapshots` 两天 0 行），全天沿用昨日收评的
"退潮 38 分"，情绪卡显示的涨停 43/跌停 21 其实是昨天的数。
同期腾讯 `quote` 1823/1823、新浪 `quote` 7266/7276 全部成功 —— 换源完全可行，只是没人接。

口径要点（第一版探针实测踩到的）：
  · **不能用"涨跌幅容差"判封板**。用 ≥9.95% 这类容差判，今天跌停会算成 12 只
    （按"现价 == 理论跌停价"判只有 7 只）——因为 −9.98% 这种"砸到跌停附近但没封住"的被算了进来。
    正确做法：拿准昨收（腾讯批量 `fields[4]`，不要用涨跌幅反推），算出理论涨跌停价
    （四舍五入到 0.01 元），再判现价是否等于它。
  · 涨停幅度分档：主板 10%、创业板/科创板 20%、北交所 30%；ST 没有 5% 档（见 `_limit_ratio`）；
    新股（N/C 开头）不参与计数，否则会把上市首日的 +200% 算成涨停。
  · **ST/*ST 一律剔除**（2026-10-28 用户口径，优先级高于"对齐东财"）：涨停、跌停、
    涨跌家数、候选池里的 ST 全部不进分子也不进分母。判定统一走 `is_st()`（底层复用
    `services.data.universe.is_st_name`，与选股范围同一把尺子）。返回体带 `excludeSt: True`
    与 `stExcluded` 计数；`limitUpInclSt`/`limitDownInclSt` 只用于"剔除前/后"对照，不要拿去做情绪值。
  · 涨跌家数只数**沪深京 A 股**：新浪排行里混着 ETF（sh51/56/58，实测 314 行）与 B 股（900/200，97 行），
    它们涨跌不该进"家数"的分母。
  · 涨跌家数要扫全市场（59 页 / 实测 35.4 秒），**不能进 180 秒快循环**，
    所以它单独带 TTL 缓存、默认 5 分钟才刷一次；涨停/跌停只扫两端各几页，每轮都跑得起。

本模块当前**只读、不接线**：先与东财双源并行对照，偏差稳定后再改 `sentiment.py` 的取数顺序。
对照的目标已经不是"和东财数字一致"，而是"口径说清楚、名单能逐票复核"。
"""
from __future__ import annotations

import threading
import time
from typing import Dict, List

from services.data.universe import is_st_name

SOURCE = 'sina_rank'
UP_PAGES = 8          # 涨幅榜前 800 只足以覆盖所有涨停（今天 70 只，含 ST 72 只）
DOWN_PAGES = 6        # 跌幅榜前 600 只足以覆盖所有跌停
BREADTH_PAGES = 60    # 全市场一页 100 只，5900 只够覆盖
BREADTH_TTL = 300.0   # 涨跌家数重扫一次约 35s，5 分钟内复用

_lock = threading.Lock()
_breadth_cache = {'ts': 0.0, 'value': None}

# 沪深京 A 股代码前缀。新浪排行里还混着 ETF（sh51/56/58，今天 314 行）与 B 股（900/200，97 行），
# 它们涨跌不该进"涨跌家数"——不然分母多出 400 多只不是股票的标的。
_A_STOCK_PREFIX = ('sh60', 'sh68', 'sz00', 'sz30', 'bj')


def _is_stock(symbol: str) -> bool:
    """新浪 symbol 是否为沪深京 A 股（ETF/B股/指数一律不算"家数"）。"""
    return str(symbol or '').lower().startswith(_A_STOCK_PREFIX)


def is_st(name: str) -> bool:
    """名称是否属于"统计里一律剔除"的那一类：ST / *ST / S*ST / SST / NST …及退市整理（带"退"）。

    2026-10-28 用户口径原话："以后统计涨停、跌停、以及其它任何类型的股票数量时，
    一律剔除 ST 和 *ST 股票（名称含 ST/*ST 的全部排除，包括 S*ST、SST 之类的变体），
    不要把它们算进分子或分母，也不要在候选池、涨跌家数、情绪指标里出现。"

    实现在 `services.data.universe.is_st_name`（按"名称含 ST"判，天然覆盖各种前缀变体），
    这里只做转发而不是各写一套：选股范围剔 ST、统计剔 ST 必须是同一把尺子，
    否则会出现"统计里 73 家、选票里只有 71 只"这种谁也说不清的口径差。
    """
    return is_st_name(name)


def _limit_ratio(symbol: str, name: str) -> float:
    """该票的涨跌停幅度；返回 0 表示不参与统计（新股/次新无限制）。

    ST 没有单独的 5% 档 —— 这是 2026-10-28 用数据推翻的旧假设（第一版按教科书写的 5%）：
      · 今日 *ST和科(002816) 收 17.94、昨收 16.31，正好是 ×1.10 并封在买一；
      · 主板 259 只 ST 里，近 3 个月有 137 只出现过单日 |涨跌| > 5%（最高 10.71%），
        若是 5% 板这些记录根本不可能存在。
    所以 ST 与主板同档 10%。反正 ST 整体不进家数（`is_st()` 已拦），这里只影响"剔除前"的对照：
    按 5% 判会把今天 *ST和科 这只真涨停漏掉，对照数就从 72 变成 71。
    """
    nm = (name or '').upper()
    if nm.startswith(('N', 'C')) or '新股' in nm:
        return 0.0
    code = symbol[2:] if symbol[:2].isalpha() else symbol
    if symbol.startswith('bj'):
        return 0.30
    if code.startswith(('30', '68')):
        return 0.20
    return 0.10


def _sweep(sort: str, asc: int, pages: int) -> List[Dict]:
    """新浪排行分页（每页 100 只，按 sort 字段、asc 方向）。"""
    from services.data.fundflow import _sina_rank_page
    rows: List[Dict] = []
    for pg in range(1, pages + 1):
        page = _sina_rank_page(sort, asc, pg)
        if not page:
            break
        rows.extend(page)
        if len(page) < 100:
            break
    return rows


async def _sweep_async(sort: str, asc: int, pages: int) -> List[Dict]:
    """排行分页是同步 requests，放线程里跑，别冻住事件循环（情绪快循环在 asyncio 里）。"""
    import asyncio
    return await asyncio.to_thread(_sweep, sort, asc, pages)


def _plausible(row: Dict, up: bool) -> bool:
    """涨幅是否"接近该票的涨跌停线"，值得再去取一次昨收确认。

    这里故意放宽（lim − 1.2 个百分点）：低价股从"分"上四舍五入后，真实封板的涨跌幅
    可能是 −9.91%（昨收 5.55 → 跌停 5.00），只看 ≥9.95% 会把这种漏掉。
    """
    try:
        ratio = float(row.get('changeratio') or 0)
    except (TypeError, ValueError):
        return False
    lim = _limit_ratio(row.get('symbol') or '', row.get('name') or '')
    if not lim or ratio == 0:
        return False
    return ratio >= lim - 0.012 if up else ratio <= -(lim - 0.012)


async def _prev_close_map(symbols: List[str]) -> Dict[str, tuple]:
    """批量取 (现价, 昨收)。昨收必须拿准：用涨跌幅反推会在低价股上误判。"""
    from services.data.stock import fetch_with_retry, parse_realtime_fields
    out: Dict[str, tuple] = {}
    for i in range(0, len(symbols), 60):
        chunk = symbols[i:i + 60]
        try:
            raw = await fetch_with_retry(','.join(chunk))
        except Exception as e:
            print(f'[情绪备源] 昨收批量拉取失败（{len(chunk)} 只）: {e}')
            continue
        for line in str(raw or '').split(';'):
            t = line.strip()
            if not t.startswith('v_') or '=' not in t:
                continue
            sym = t[2:t.index('=')].lower()
            one = parse_realtime_fields(f'v_{sym}={t[t.index("=") + 1:]}')
            if one and one.get('yesterdayClose'):
                out[sym] = (one['price'], one['yesterdayClose'])
    return out


def _sealed_with_prev(price: float, prev: float, lim: float, up: bool) -> bool:
    """现价是否等于交易所的涨/跌停价。

    涨停价必须按**四舍五入到分**算，而 Python 内置 `round` 是"四舍六入五成双"
    （round(6.175, 2) = 6.17，交易场所是 6.18），低价股上正好差一分就会漏判——
    实测少算的 3 只涨停就是这么来的，所以这里改用 Decimal ROUND_HALF_UP。
    """
    from decimal import Decimal, ROUND_HALF_UP
    if price <= 0 or prev <= 0:
        return False
    raw = Decimal(str(prev)) * (Decimal(1) + Decimal(str(lim)) if up
                                else Decimal(1) - Decimal(str(lim)))
    target = float(raw.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
    return abs(price - target) <= 0.005


def _classify(cand: List[Dict], quotes: Dict[str, tuple]) -> tuple:
    """候选 → (涨停, 跌停, ST涨停, ST跌停) 四份名单。

    ST 只进后两个"对照"桶：`ztList`/`dtList` 永远不含 ST/*ST（2026-10-28 用户口径）。
    同一次扫描里把两套数一起算出来，是因为"剔除前/后"的对照不该再跑一遍网络。
    """
    zt: List[Dict] = []
    dt: List[Dict] = []
    st_zt: List[Dict] = []
    st_dt: List[Dict] = []
    for r in cand:
        sym = r['symbol']
        name = r.get('name') or ''
        lim = _limit_ratio(sym, name)
        got = quotes.get(sym)
        if not lim or not got:
            continue
        price, prev = got
        hit_up = _sealed_with_prev(price, prev, lim, True)
        hit_down = False if hit_up else _sealed_with_prev(price, prev, lim, False)
        if not (hit_up or hit_down):
            continue
        item = {'c': sym[2:], 'n': name, 'p': price * 1000,
                'zdf': round(float(r.get('changeratio') or 0) * 100, 2)}
        if is_st(name):
            (st_zt if hit_up else st_dt).append(item)
        else:
            (zt if hit_up else dt).append(item)
    return zt, dt, st_zt, st_dt


def _payload(zt: List[Dict], dt: List[Dict], st_zt: List[Dict],
             st_dt: List[Dict], scanned: Dict) -> Dict:
    """统一返回体：`limitUp`/`limitDown` 是剔除 ST 后的正式口径。

    `limitUpInclSt`/`limitDownInclSt` 与 `stExcluded` 只为"口径变更前后"的对照与复核而留，
    接线上情绪值时**只许读** limitUp/limitDown，别拿含 ST 的数去算分数。
    """
    return {
        'source': SOURCE,
        'excludeSt': True,
        'limitUp': len(zt),
        'limitDown': len(dt),
        'ztList': zt,
        'dtList': dt,
        'limitUpInclSt': len(zt) + len(st_zt),
        'limitDownInclSt': len(dt) + len(st_dt),
        'stExcluded': {'zt': len(st_zt), 'dt': len(st_dt),
                       'names': [x['n'] for x in st_zt + st_dt]},
        'scanned': scanned,
    }


def limit_counts() -> Dict:
    """涨停/跌停家数与名单（东财之外的口径，已剔除 ST/*ST）。

    两步：新浪排行取两端候选（14 次请求）→ 腾讯批量补昨收做精确封板判定（候选通常 <200 只）。
    ST 候选仍会被扫到，但只用来算"剔除前"的对照数，不会出现在 ztList/dtList 里。
    """
    import asyncio
    up = _sweep('changeratio', 0, UP_PAGES)
    down = _sweep('changeratio', 1, DOWN_PAGES)
    cand = [r for r in up if _is_stock(r.get('symbol')) and _plausible(r, True)] \
        + [r for r in down if _is_stock(r.get('symbol')) and _plausible(r, False)]
    syms = [r['symbol'] for r in cand]
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            raise RuntimeError('请在异步上下文里调用 limit_counts_async()')
        quotes = loop.run_until_complete(_prev_close_map(syms))
    except RuntimeError as e:
        if '异步上下文' in str(e):
            raise
        quotes = asyncio.run(_prev_close_map(syms))
    zt, dt, st_zt, st_dt = _classify(cand, quotes)
    return _payload(zt, dt, st_zt, st_dt, {
        'up': len(up), 'down': len(down), 'cand': len(cand),
        'stCand': sum(1 for r in cand if is_st(r.get('name') or '')),
        'prevCloseGot': len(quotes),
    })


async def limit_counts_async() -> Dict:
    """异步版（给情绪快循环用）：候选昨收走 `fetch_with_retry`；口径同上，已剔除 ST。"""
    up = await _sweep_async('changeratio', 0, UP_PAGES)
    down = await _sweep_async('changeratio', 1, DOWN_PAGES)
    cand = [r for r in up if _is_stock(r.get('symbol')) and _plausible(r, True)] \
        + [r for r in down if _is_stock(r.get('symbol')) and _plausible(r, False)]
    quotes = await _prev_close_map([r['symbol'] for r in cand])
    zt, dt, st_zt, st_dt = _classify(cand, quotes)
    return _payload(zt, dt, st_zt, st_dt, {
        'up': len(up), 'down': len(down), 'cand': len(cand),
        'stCand': sum(1 for r in cand if is_st(r.get('name') or '')),
        'prevCloseGot': len(quotes),
    })


def market_breadth(force: bool = False) -> Dict:
    """涨跌家数（市场宽度），只数沪深京 A 股、已剔除 ST/*ST。5 分钟 TTL（全市场扫一次约 35 秒）。

    两道过滤都在计数之前完成：
      · 非股票标的（ETF 314 行 / B 股 97 行）不算家数 —— 分母只留 A 股；
      · ST/*ST 与退市整理剔除，`total`/`riseCount`/`fallCount`/`flatCount` 都不含它们。
    对照字段：`totalInclSt` 是含 ST 的 A 股总数，`stExcluded.breadth` / `droppedNonStock` 是剔掉的量。
    """
    now = time.time()
    with _lock:
        if not force and _breadth_cache['value'] and now - _breadth_cache['ts'] < BREADTH_TTL:
            return dict(_breadth_cache['value'], cached=True)
    rows_all = _sweep('changeratio', 0, BREADTH_PAGES)
    stocks = [r for r in rows_all if _is_stock(r.get('symbol'))]
    rows = [r for r in stocks if not is_st(r.get('name') or '')]
    rise = sum(1 for r in rows if float(r.get('changeratio') or 0) > 0)
    fall = sum(1 for r in rows if float(r.get('changeratio') or 0) < 0)
    out = {'source': SOURCE, 'excludeSt': True,
           'riseCount': rise, 'fallCount': fall,
           'flatCount': len(rows) - rise - fall, 'total': len(rows),
           'totalInclSt': len(stocks),
           'stExcluded': {'breadth': len(stocks) - len(rows)},
           'droppedNonStock': len(rows_all) - len(stocks),
           'cached': False}
    with _lock:
        _breadth_cache.update(ts=time.time(), value=out)
    return out


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    t0 = time.time()
    lc = limit_counts()
    print(f"涨停 {lc['limitUp']} / 跌停 {lc['limitDown']}（已剔除 ST）"
          f"｜剔除前 涨停 {lc['limitUpInclSt']} / 跌停 {lc['limitDownInclSt']}"
          f"（ST 占 {lc['stExcluded']['zt']} / {lc['stExcluded']['dt']}）"
          f"｜扫 {lc['scanned']}，耗时 {time.time() - t0:.1f}s")
    if lc['stExcluded']['names']:
        print(f"剔除的 ST 名单: {lc['stExcluded']['names']}")
    t1 = time.time()
    bd = market_breadth()
    print(f"A股 涨 {bd['riseCount']} / 跌 {bd['fallCount']} / 平 {bd['flatCount']}"
          f"（{bd['total']} 只，已剔除 ST {bd['stExcluded']['breadth']} 只；"
          f"含 ST 口径 {bd['totalInclSt']} 只，另剔除 ETF/B股 {bd['droppedNonStock']} 行，"
          f"耗时 {time.time() - t1:.1f}s）")
