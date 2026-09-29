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
from app.config import (EASTMONEY_HEADERS, RPS_ENABLED, RPS_PERIOD_DAYS, RPS_MIN_SAMPLES, RPS_TOP_N, KLINE_START_DATE, TENCENT_KLINE_API, KLINE_HISTORY_ENABLED)
from db.database import get_db_conn
from services.stock import _sync_get

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
    """分页拉全市场 A 股代码列表（接口单页有上限，需翻页合并；多 fs 候选合并去重）。"""
    codes: List[str] = []
    seen = set()
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
        from services.kline_history import get_engine, history_available
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
    return {'periodDays': period_days, 'count': len(rps_map), 'source': source}


async def get_rps_leaderboard(period_days: int = RPS_PERIOD_DAYS, top_n: int = RPS_TOP_N) -> Dict:
    """RPS 强势股榜（降序前 top_n），供前端 /api/rps 展示。"""
    rps_map, gains, _ts = await asyncio.to_thread(_load_rps, period_days)

    if not rps_map:
        return {
            'periodDays': period_days,
            'stocks': [],
            'updateTime': '',
            'error': 'RPS 缓存为空，请先触发 /api/rps/refresh 预计算',
        }

    name_map: Dict[str, str] = {}
    try:
        from db.database import load_stock_basic_info
        info = load_stock_basic_info() or {}
        for sym, val in info.items():
            if isinstance(val, str) and val:
                name_map[sym.lower()] = val
    except Exception:
        pass

    # 缺失/无效名称兜底：实时从行情接口补名称（次新股、未扫描股常见）
    top = sorted(rps_map.items(), key=lambda x: x[1], reverse=True)[:top_n]
    missing = [sym for sym, _ in top if not name_map.get(sym) or name_map.get(sym) == sym]
    if missing:
        try:
            from services.stock import get_stock_name
            names = await asyncio.gather(*[get_stock_name(s) for s in missing], return_exceptions=True)
            for sym, nm in zip(missing, names):
                if isinstance(nm, str) and nm and nm.lower() != sym:
                    name_map[sym] = nm
        except Exception:
            pass

    # 近1月回调（约22个交易日）：当前收盘价相对近1月最高价的回撤幅度
    pullback_map: Dict[str, Optional[float]] = {}
    try:
        from services.stock import get_kline_data

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

        pullbacks = await asyncio.gather(*[_calc_pullback(sym) for sym, _ in top])
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
