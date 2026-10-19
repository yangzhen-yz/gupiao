"""rps.py - RPS 相对强度因子（欧奈尔体系核心，P0，2026-07-16）

RPS = 个股区间涨幅在全市场的百分位排名（0~100）。
  RPS 90 = 强于 90% 的同期表现，是"强势股/龙头"的量化标志。

数据来源：腾讯前复权日线（K 线）。盘前/手动批量预计算"全市场区间涨幅→百分位"，
结果存入 SQLite rps_cache 表；盘中扫描/分析直接读缓存（秒级），无缓存则降级中档分。

为什么不用东方财富 clist 区间参数：实测 push2delay / push2 的 clist 在带 k1/k2/k3 时
f3 仍返回当日涨跌幅（接口不支持区间统计），故改用 K 线算区间涨幅，最可靠。
"""
import asyncio
import concurrent.futures
import time
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import requests
from app.config import (EASTMONEY_HEADERS, RPS_ENABLED, RPS_PERIOD_DAYS, RPS_MIN_SAMPLES, RPS_TOP_N, KLINE_START_DATE, TENCENT_KLINE_API, KLINE_HISTORY_ENABLED,
                        RPS_PULLBACK_FROM_LOCAL, RPS_NAME_LIVE_MAX, RPS_LEADERBOARD_TTL)
from db.database import get_db_conn
from services.data.stock import _sync_get

_LB_CACHE: Dict = {}    # 榜单结果内存缓存 {(period_days, top_n): (时间戳, 结果)}


# 全市场 A 股代码列表的 fs 候选（按优先级尝试，合并去重）。
# 注意：push2delay 在该 ut token 下，标准写法 `m:0+t:2,m:1+t:2` 仅返回沪A；
# 实测可用组合为 `m:1+t:2`（沪A）+ `m:0+t:6`（深A）。保留标准写法作为兜底，
# 避免 token 变化后全盘失效。
_FS_CANDIDATES = [
    'm:1+t:2,m:0+t:6',   # 沪A + 深A（当前 token 实测可用）
    'm:0+t:2,m:1+t:2',   # 标准写法（部分 token/环境可用，仅沪A 也无妨，会与前一条合并）
]
_EASTMONEY_CLIST = 'https://push2delay.eastmoney.com/api/qt/clist/get'

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rps_cache (
  period INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  rps REAL,
  interval_change REAL,
  update_time TEXT,
  PRIMARY KEY (period, symbol)
)
"""


def _ensure_schema(conn) -> None:
    conn.execute(_SCHEMA)
    conn.commit()


def _clist_codes() -> List[str]:
    """分页拉全市场 A 股代码列表（接口单页有上限，需翻页合并；多 fs 候选合并去重）。

    2026-10-19：接入共享熔断 eastmoney_guard——冷却中直接返回 []，
    RPS 用本地库降级，不再翻页 ×3 重试硬打（东财 clist IP 级风控根治）。
    """
    try:
        from services.data import eastmoney_guard
        if eastmoney_guard.skip('clist'):
            return []
    except Exception:
        pass
    codes: List[str] = []
    seen = set()
    # 熔断分级要用它：Python 3 在 except 块结束时会把 `e` 删掉，所以自己留一份。
    # 不初始化会在"一次异常都没发生但结果为空"时抛 NameError —— 而那个 except 是
    # `except Exception: pass`，熔断计数就这么被静默吞掉了。
    last_err = None
    pz = 100
    for fs in _FS_CANDIDATES:
        pn = 1
        while True:
            params = {
                'pn': pn, 'pz': pz, 'po': 1, 'np': 1, 'fltt': 2, 'invt': 2,
                'fid': 'f12', 'fs': fs, 'fields': 'f12,f13',
                'ut': 'b2884a393a59ad64002292a3e90d46a5',
            }
            batch = []
            for attempt in range(3):
                try:
                    r = requests.get(_EASTMONEY_CLIST, params=params, headers=EASTMONEY_HEADERS, timeout=10)
                    r.raise_for_status()
                    batch = (r.json().get('data') or {}).get('diff') or []
                    break
                except Exception as e:
                    last_err = e
                    try:
                        from services.data import eastmoney_guard
                        if not eastmoney_guard.should_retry(e):
                            break        # 被封就别再戳，第三次只是续封
                    except Exception:
                        pass
                    if attempt < 2:
                        time.sleep(0.5 * (attempt + 1))
            if not batch:
                break
            for it in batch:
                code = it.get('f12') or ''
                m = 'sh' if it.get('f13') == 1 else 'sz'
                if code:
                    sym = f'{m}{code}'.lower()
                    if sym not in seen:
                        seen.add(sym)
                        codes.append(sym)
            if len(batch) < pz:
                break
            pn += 1
            time.sleep(0.2)
    if codes:
        try:
            from services.data import eastmoney_guard
            eastmoney_guard.ok('clist')
        except Exception:
            pass
    else:
        try:
            from services.data import eastmoney_guard
            eastmoney_guard.fail('clist', last_err)
        except Exception:
            pass
    return codes


def _fetch_kline_for_rps(symbol: str, limit: int) -> List:
    """同步拉前复权日线（独立 limit，避免受全局 KLINE_DATA_LIMIT 截断影响）。"""
    today = date.today().isoformat()
    url = f'{TENCENT_KLINE_API}?param={symbol},day,{KLINE_START_DATE},{today},{limit},qfq'
    last_err = None
    for attempt in range(3):
        try:
            resp = _sync_get(url)
            resp.raise_for_status()
            data = resp.json()
            if data.get('data') and data['data'].get(symbol):
                return data['data'][symbol].get('qfqday', []) or data['data'][symbol].get('day', [])
            return []
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(0.3 * (attempt + 1))
    return []


def _compute_rps(period_days: int, limit_codes: Optional[int] = None) -> Tuple[Dict[str, float], Dict[str, float]]:
    """同步计算全市场 RPS 映射（线程池并发拉 K 线）。

    Returns:
        (rps_map, gain_map)：symbol_lower -> RPS(0~100) / 区间涨跌幅(%)
        样本不足或数据异常返回 ({}, {})
    """
    codes = _clist_codes()
    if limit_codes:
        codes = codes[:limit_codes]
    if len(codes) < RPS_MIN_SAMPLES and not limit_codes:
        return {}, {}

    gains: Dict[str, float] = {}
    def one(sym: str) -> None:
        try:
            kl = _fetch_kline_for_rps(sym, period_days + 15)
            if len(kl) < period_days + 1:
                return
            c0 = float(kl[-(period_days + 1)][2])  # 区间起点收盘价
            c1 = float(kl[-1][2])                   # 区间终点收盘价
            if c0 > 0:
                gains[sym] = (c1 - c0) / c0 * 100
        except Exception:
            pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(one, codes))

    if len(gains) < RPS_MIN_SAMPLES:
        return {}, {}

    # 按区间涨幅升序排名，RPS = 强于多少比例的股票
    items = sorted(gains.items(), key=lambda x: x[1])
    n = len(items)
    rps_map: Dict[str, float] = {}
    for rank, (sym, _g) in enumerate(items):
        rps_map[sym] = round(rank / (n - 1) * 100, 1) if n > 1 else 50.0
    return rps_map, gains


def _compute_rps_vectorized(period_days: int) -> Tuple[Dict[str, float], Dict[str, float]]:
    """向量化计算全市场 RPS（读本地历史库，秒级，无需联网）。

    借鉴 Sequoia-X：从本地前复权日K库一次性取出最近窗口，纯 Python 算区间涨幅 + 百分位排名。
    前复权口径使最新价=实盘价，与实时行情拼接无缝；前/后复权对区间收益率只差常数倍，RPS 百分位排名不受影响。

    Returns:
        (rps_map, gain_map)：symbol_lower -> RPS(0~100) / 区间涨跌幅(%)；
        本地库未就绪/样本不足返回 ({}, {})（调用方回退腾讯逐股法）。
    """
    if not KLINE_HISTORY_ENABLED:
        return {}, {}
    try:
        from services.data.kline_history import get_engine, history_available
    except Exception:
        return {}, {}

    if not history_available():
        return {}, {}

    try:
        eng = get_engine()
        latest, data = eng.load_recent_closes(period_days)
        if not latest or len(data) < RPS_MIN_SAMPLES:
            return {}, {}

        gains: Dict[str, float] = {}
        for sym, series in data.items():
            # 需要 period_days+1 根 K 线才能算起点→终点区间涨幅
            if len(series) < period_days + 1:
                continue
            c0 = series[-(period_days + 1)][1]  # 区间起点收盘价（前复权）
            c1 = series[-1][1]                  # 区间终点收盘价
            if c0 and c0 > 0:
                gains[sym] = (c1 - c0) / c0 * 100

        if len(gains) < RPS_MIN_SAMPLES:
            return {}, {}

        # 与既有腾讯路径一致：按区间涨幅升序排名，RPS = 强于多少比例的股票
        items = sorted(gains.items(), key=lambda x: x[1])
        n = len(items)
        rps_map: Dict[str, float] = {}
        for rank, (sym, _g) in enumerate(items):
            rps_map[sym] = round(rank / (n - 1) * 100, 1) if n > 1 else 50.0
        return rps_map, gains
    except Exception as e:
        print(f"[RPS] 向量化计算异常，回退腾讯法: {e}")
        return {}, {}


def _save_rps(period_days: int, rps_map: Dict[str, float], gains: Dict[str, float]) -> None:
    conn = get_db_conn()
    _ensure_schema(conn)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute('DELETE FROM rps_cache WHERE period=?', (period_days,))
    conn.executemany(
        'INSERT OR REPLACE INTO rps_cache (period, symbol, rps, interval_change, update_time) VALUES (?,?,?,?,?)',
        [(period_days, sym, rps_map[sym], gains.get(sym, 0.0), now) for sym in rps_map],
    )
    conn.commit()
    conn.close()


def _pullback_local(symbols: List[str], bars: int = 22) -> Dict[str, Optional[float]]:
    """近 `bars` 根日K的"最新收盘相对期间最高价的回撤"，**全部从本地日K库算**。

    为什么要换掉原来的写法（2026-10-17 性能实测）：旧实现对榜上 100 只票**逐只**调
    `get_kline_data()` 去拉腾讯日K，`/api/rps` 因此要 38~50 秒 —— 而它真正需要的数据量
    只是 100 行 × 22 根，本地 `stock_daily` 一条 SQL 就够。"页面整体都很慢"的体感，
    很大一部分来自这个接口：它没有超时，前端一进去就把整页的 loading 挂住几十秒。

    口径与旧实现逐值对齐：旧代码是 `kline[-22:]` 里取 `[-1][2]`（最新收盘）与 `max(k[3])`
    （期间最高），本函数用 `ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC)`
    取**该票自己的**最近 22 根，效果一致；不足 22 根返回 None（旧实现是 len<22 直接给 None）。

    唯一的差别要说清楚：旧实现会在盘中把腾讯实时价拼成"今天这根"再算，本函数算到
    **最后一根已收盘的日K**（收盘快照 18:15 后更新）。榜上的 `updateTime` 就是这个数据的日期。
    """
    if not symbols:
        return {}
    import sqlite3
    from app.config import KLINE_HISTORY_DB_PATH
    ph = ','.join('?' * len(symbols))
    sql = f'''WITH ranked AS (
        SELECT symbol, high, close,
               ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) rn
        FROM stock_daily WHERE symbol IN ({ph})
    )
    SELECT symbol,
           MAX(CASE WHEN rn=1 THEN close END),
           MAX(high),
           COUNT(CASE WHEN rn<=22 THEN 1 END)
    FROM ranked WHERE rn<=22 GROUP BY symbol'''
    out: Dict[str, Optional[float]] = {}
    try:
        conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=30)
        try:
            rows = conn.execute(sql, [s[-6:] for s in symbols]).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(f'[RPS] 本地回撤计算失败(本字段留空): {e}')
        return {s: None for s in symbols}
    got = {}
    for sym, close, high, n in rows:
        if close and high and n and n >= bars:
            got[sym] = round((close / high - 1) * 100, 2)
        else:
            got[sym] = None
    for s in symbols:
        out[s] = got.get(s[-6:])
    return out


def _names_local(symbols: List[str]) -> Dict[str, str]:
    """名称也从本地名录取（`stock_basic_full`，8871 条，日K同步时一起刷）。

    旧实现是"名称缺失就逐只现拉行情"，同样是把外部请求塞进榜单接口里。
    """
    if not symbols:
        return {}
    import sqlite3
    from app.config import KLINE_HISTORY_DB_PATH
    ph = ','.join('?' * len(symbols))
    try:
        conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=30)
        try:
            rows = conn.execute(
                f'SELECT symbol, name FROM stock_basic_full WHERE symbol IN ({ph})',
                [s[-6:] for s in symbols]).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(f'[RPS] 本地名录读取失败(名称退回代码): {e}')
        return {}
    return {(s if len(s) == 6 else s)[-6:]: (n or '') for s, n in rows if n}


def _load_rps(period_days: int):
    conn = get_db_conn()
    _ensure_schema(conn)
    rows = conn.execute(
        'SELECT symbol, rps, interval_change, update_time FROM rps_cache WHERE period=?',
        (period_days,),
    ).fetchall()
    conn.close()
    rps_map: Dict[str, float] = {}
    gains: Dict[str, float] = {}
    ts = rows[0]['update_time'] if rows else None
    for r in rows:
        rps_map[r['symbol']] = r['rps']
        gains[r['symbol']] = r['interval_change']
    return rps_map, gains, ts


async def get_rps_map(period_days: int = RPS_PERIOD_DAYS) -> Dict[str, float]:
    """读取 RPS 缓存映射（盘前预计算，秒级）。无缓存返回空 dict（上层降级中档分）。"""
    if not RPS_ENABLED:
        return {}
    rps_map, _gains, _ts = await asyncio.to_thread(_load_rps, period_days)
    return rps_map


async def refresh_rps(period_days: int = RPS_PERIOD_DAYS, limit_codes: Optional[int] = None) -> Dict:
    """预计算全市场 RPS 并写入缓存（盘前/手动触发，后台任务）。

    优先级：本地历史库向量化（秒级、免限流）→ 回退腾讯逐股拉 K 线（旧路径，慢但兼容）。
    limit_codes 仅对回退路径生效（本地库路径一次算全市场）。
    """
    # 首选：本地历史库向量化（快、不依赖东方财富限流）
    rps_map, gains = await asyncio.to_thread(_compute_rps_vectorized, period_days)
    source = 'local_history'
    if not rps_map:
        # 回退：腾讯逐股拉前复权K线（旧实现，首次回填前或本地库异常时使用）
        rps_map, gains = await asyncio.to_thread(_compute_rps, period_days, limit_codes)
        source = 'tencent'
    if rps_map:
        await asyncio.to_thread(_save_rps, period_days, rps_map, gains)
        _LB_CACHE.clear()      # 重算完让下一次请求拿到新榜，而不是继续吃 5 分钟旧缓存
    return {'periodDays': period_days, 'count': len(rps_map), 'source': source}


async def get_rps_leaderboard(period_days: int = RPS_PERIOD_DAYS, top_n: int = RPS_TOP_N) -> Dict:
    """RPS 强势股榜（降序前 top_n），供前端 /api/rps 展示。

    榜单结果按 (周期, 数量) 做 `RPS_LEADERBOARD_TTL` 秒内存缓存：RPS 本身一天只重算一次，
    每次请求都重新组装纯属浪费（`/api/rps/refresh` 会主动清缓存，重算后立刻可见）。
    """
    key = (period_days, top_n)
    hit = _LB_CACHE.get(key)
    if hit and time.time() - hit[0] < RPS_LEADERBOARD_TTL:
        return dict(hit[1], cached=True)
    result = await _build_leaderboard(period_days, top_n)
    if result.get('stocks'):
        _LB_CACHE[key] = (time.time(), result)
    return result


async def _build_leaderboard(period_days: int = RPS_PERIOD_DAYS, top_n: int = RPS_TOP_N) -> Dict:
    """真正组装榜单：读 rps_cache + 本地日K算回撤 + 本地名录补名称（全程不外联）。"""
    rps_map, gains, _ts = await asyncio.to_thread(_load_rps, period_days)

    if not rps_map:
        return {
            'periodDays': period_days,
            'stocks': [],
            'updateTime': '',
            'error': 'RPS 缓存为空，请先触发 /api/rps/refresh 预计算',
        }

    top = sorted(rps_map.items(), key=lambda x: x[1], reverse=True)[:top_n]
    syms = [s for s, _ in top]

    # 名称：本地名录（stock_basic_full）优先，实在补不上的少数才去现拉，并且**封顶**。
    # 旧写法是"缺名字就 100 只一起 gather 行情接口"，那是榜单慢的第二处外部依赖。
    name_map: Dict[str, str] = {}
    try:
        from db.database import load_stock_basic_info
        info = load_stock_basic_info() or {}
        for sym, val in info.items():
            if isinstance(val, str) and val:
                name_map[sym.lower()] = val
    except Exception:
        pass
    for sym, nm in _names_local(syms).items():
        name_map.setdefault(f'sh{sym}' if sym.startswith('6') else f'sz{sym}', nm)
        name_map.setdefault(sym, nm)
    missing = [s for s in syms if not name_map.get(s) or name_map.get(s) == s]
    if missing:
        try:
            from services.data.stock import get_stock_name
            names = await asyncio.gather(*[get_stock_name(s) for s in missing[:RPS_NAME_LIVE_MAX]],
                                         return_exceptions=True)
            for sym, nm in zip(missing[:RPS_NAME_LIVE_MAX], names):
                if isinstance(nm, str) and nm and nm.lower() != sym:
                    name_map[sym] = nm
        except Exception:
            pass

    # 近1月回调：默认走本地日K（一次 SQL），RPS_PULLBACK_FROM_LOCAL=False 才退回逐只现拉
    pullback_map: Dict[str, Optional[float]] = {}
    if RPS_PULLBACK_FROM_LOCAL:
        pullback_map = await asyncio.to_thread(_pullback_local, syms)
    else:
        try:
            from services.data.stock import get_kline_data

            async def _calc_pullback(sym: str) -> Tuple[str, Optional[float]]:
                try:
                    kline = await get_kline_data(sym)
                    if not kline or len(kline) < 22:
                        return sym, None
                    recent = kline[-22:]
                    current = float(recent[-1][2])
                    high = max(float(k[3]) for k in recent)
                    if high > 0:
                        return sym, round((current / high - 1) * 100, 2)
                except Exception:
                    pass
                return sym, None

            pullbacks = await asyncio.gather(*[_calc_pullback(s) for s in syms])
            pullback_map = {sym: pb for sym, pb in pullbacks}
        except Exception:
            pass

    stocks = [{
        'symbol': sym,
        'name': name_map.get(sym, sym),
        'rps': rps,
        'intervalChange': gains.get(sym, 0.0),
        'pullback_1m': pullback_map.get(sym),
    } for sym, rps in top]
    return {
        'periodDays': period_days,
        'stocks': stocks,
        'updateTime': _ts or '',
    }
