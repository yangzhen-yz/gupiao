"""fundflow.py - 主力资金流向 / 板块资金流 / 非热点上涨股 统计

新增 Tab「资金流向」后端支撑服务（2026-07-16）。
数据全部来自东方财富 clist 接口（与 _fetch_hot_sectors / fetch_eastmoney_hot_list 同源），
使用 requests 同步客户端 + asyncio.to_thread 包装，遵循项目 HTTP 硬约束（禁止 httpx）。

三块能力：
  1. 个股主力净流入 / 净流出排名（全市场 沪A+深A，按 f62 排序）
  2. 板块（行业 / 概念）净流入 / 净流出排名（按 f62 排序）
  3. 非近期热点但上涨不错的票：涨幅榜剔除热搜榜前 N + ST/退市，不套用现有选股评分规则
"""
import asyncio
import time
from datetime import datetime
from typing import Dict, List, Optional

import requests
from app.config import (
    EASTMONEY_HEADERS,
    FUND_FLOW_STOCK_TOP_N,
    FUND_FLOW_SECTOR_TOP_N,
    FUND_FLOW_HIDDEN_CHANGE_THRESHOLD,
    FUND_FLOW_HIDDEN_HOT_TOP_N,
    FUND_FLOW_HIDDEN_TOP_N,
    FUND_FLOW_CACHE_TTL,
)
from db.database import load_hot_search_ranking
from services.stock import has_delisting_risk  # 复用 ST/退市 基础过滤

_EASTMONEY_CLIST = 'https://push2delay.eastmoney.com/api/qt/clist/get'

# 沪A + 深A（覆盖主板/创业板/科创板；不含北交所）。
# 注意：push2delay 在该 ut token 下，标准写法 `m:0+t:2,m:1+t:2` 实测仅返回沪A；
# 当前 token 实测可用组合为 `m:1+t:2`（沪A）+ `m:0+t:6`（深A）。
_FS_ALL_A = 'm:1+t:2,m:0+t:6'
# 行业板块 / 概念板块（f:!50 排除 B 股等干扰项，与 _fetch_hot_sectors 一致）
_FS_INDUSTRY = 'm:90+t:2+f:!50'
_FS_CONCEPT = 'm:90+t:3+f:!50'


def _clist(fid: str, po: int, fs: str, fields: str, pz: int, pn: int = 1) -> List[Dict]:
    """东方财富 clist 同步请求，返回 diff 列表（失败抛异常由上层 catch）。

    fltt=2：价格/涨跌幅已为真实数值（f3 直接是百分比，无需 /100）。
    东方财富接口偶发返回空 body / 非 JSON（限流或 gzip 异常），此处重试 2 次提高健壮性。
    """
    params = {
        'pn': pn, 'pz': pz, 'po': po, 'np': 1, 'fltt': 2, 'invt': 2,
        'fid': fid, 'fs': fs, 'fields': fields,
        'ut': 'b2884a393a59ad64002292a3e90d46a5',
    }
    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(_EASTMONEY_CLIST, params=params, headers=EASTMONEY_HEADERS, timeout=10)
            r.raise_for_status()
            text = r.text.strip()
            if not text:
                last_err = ValueError('empty response')
                time.sleep(0.5 * (attempt + 1))
                continue
            data = r.json()
            # 东方财富偶发 rc != 0（限流/验证码），视为失败重试
            if data.get('rc') not in (0, None):
                last_err = ValueError(f"rc={data.get('rc')}")
                time.sleep(0.5 * (attempt + 1))
                continue
            return (data.get('data') or {}).get('diff') or []
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    # 全部重试失败，抛出供上层捕获
    raise last_err if last_err else RuntimeError('clist unknown error')


def _to_float(v, default: float = 0.0) -> float:
    if v is None or v == '' or v == '-':
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _normalize_stock(item: Dict) -> Dict:
    market = 'sh' if item.get('f13') == 1 else 'sz'
    code = item.get('f12', '') or ''
    return {
        'symbol': f'{market}{code}'.lower(),
        'code': code,
        'name': item.get('f14', '') or '',
        'price': _to_float(item.get('f2'), 0.0),
        'changePercent': _to_float(item.get('f3'), 0.0),   # fltt=2 → 真实百分比
        'mainNetInflow': _to_float(item.get('f62'), 0.0),  # 主力净流入（元）
        'industry': item.get('f100', '') or '',
    }


def _normalize_sector(item: Dict, rank: int) -> Dict:
    return {
        'code': item.get('f12', ''),
        'name': item.get('f14', ''),
        'changePercent': _to_float(item.get('f3'), 0.0),
        'mainNetInflow': _to_float(item.get('f62'), 0.0),
        'riseCount': int(_to_float(item.get('f104'), 0)),
        'fallCount': int(_to_float(item.get('f105'), 0)),
        'rank': rank,
    }


async def get_stock_fund_flow_rankings(top_n: int = FUND_FLOW_STOCK_TOP_N) -> Dict[str, List[Dict]]:
    """个股主力净流入（po=1 降序）/ 净流出（po=0 升序）排名。"""
    def _sync():
        inflow_raw = _clist('f62', 1, _FS_ALL_A, 'f12,f13,f14,f2,f3,f62,f100', top_n)
        time.sleep(0.2)  # 轻量限速，规避东方财富限流
        outflow_raw = _clist('f62', 0, _FS_ALL_A, 'f12,f13,f14,f2,f3,f62,f100', top_n)
        return inflow_raw or [], outflow_raw or []
    inflow_raw, outflow_raw = await asyncio.to_thread(_sync)
    return {
        'inflow': [_normalize_stock(x) for x in inflow_raw],
        'outflow': [_normalize_stock(x) for x in outflow_raw],
    }


async def get_sector_fund_flow_rankings(top_n: int = FUND_FLOW_SECTOR_TOP_N) -> Dict:
    """板块（行业 / 概念）净流入 / 净流出排名。"""
    def _sync():
        ind_in = _clist('f62', 1, _FS_INDUSTRY, 'f12,f14,f3,f62,f104,f105', top_n)
        time.sleep(0.2)
        ind_out = _clist('f62', 0, _FS_INDUSTRY, 'f12,f14,f3,f62,f104,f105', top_n)
        time.sleep(0.2)
        con_in = _clist('f62', 1, _FS_CONCEPT, 'f12,f14,f3,f62,f104,f105', top_n)
        time.sleep(0.2)
        con_out = _clist('f62', 0, _FS_CONCEPT, 'f12,f14,f3,f62,f104,f105', top_n)
        return ind_in or [], ind_out or [], con_in or [], con_out or []
    ind_in, ind_out, con_in, con_out = await asyncio.to_thread(_sync)
    return {
        'industry': {
            'inflow': [_normalize_sector(x, i + 1) for i, x in enumerate(ind_in)],
            'outflow': [_normalize_sector(x, i + 1) for i, x in enumerate(ind_out)],
        },
        'concept': {
            'inflow': [_normalize_sector(x, i + 1) for i, x in enumerate(con_in)],
            'outflow': [_normalize_sector(x, i + 1) for i, x in enumerate(con_out)],
        },
    }


async def get_hidden_gems(
    change_threshold: float = FUND_FLOW_HIDDEN_CHANGE_THRESHOLD,
    hot_top_n: int = FUND_FLOW_HIDDEN_HOT_TOP_N,
    top_n: int = FUND_FLOW_HIDDEN_TOP_N,
) -> List[Dict]:
    """非近期热点但上涨不错的票。

    逻辑：取全市场涨幅前 300（f3 降序）→ 过滤 涨幅≥阈值 → 排除 ST/退市 →
    排除热搜榜前 N（视为“近期热点”）→ 返回前 top_n。
    注意：不套用现有 10 因子评分 / 60 分 / FORBIDDEN_KEYWORDS 等选股规则。
    """
    def _sync():
        # 取全市场涨幅前 300，字段含 f62（主力净流入）、f100（行业）
        return _clist('f3', 1, _FS_ALL_A, 'f12,f13,f14,f2,f3,f62,f100', 300) or []
    raw = await asyncio.to_thread(_sync)
    items = [_normalize_stock(x) for x in raw]

    # 加载热搜榜，构建“热点”集合（hotRank ≤ N）
    hot_set = set()
    try:
        hr = load_hot_search_ranking()
        for s in (hr or {}).get('stocks', []):
            if isinstance(s, dict) and (s.get('hotRank', 999) or 999) <= hot_top_n:
                sym = (s.get('symbol') or '').lower()
                if sym:
                    hot_set.add(sym)
    except Exception as e:
        print(f'[资金流向] 热搜榜加载失败，跳过热点过滤: {e}')

    result = []
    for it in items:
        if it['changePercent'] < change_threshold:
            continue
        if has_delisting_risk(it['name'], it['symbol']):
            continue
        if it['symbol'] in hot_set:
            continue
        result.append(it)
        if len(result) >= top_n:
            break
    return result


# ========== 内存缓存（按参数 key，避免轮询重复打 Eastmoney） ==========
_CACHE: Dict[str, Dict] = {}


def _cache_key(stock_top_n, sector_top_n, change_threshold, hot_top_n, hidden_top_n) -> str:
    return f"{stock_top_n}|{sector_top_n}|{change_threshold}|{hot_top_n}|{hidden_top_n}"


async def get_fund_flow_summary(
    stock_top_n: int = FUND_FLOW_STOCK_TOP_N,
    sector_top_n: int = FUND_FLOW_SECTOR_TOP_N,
    change_threshold: float = FUND_FLOW_HIDDEN_CHANGE_THRESHOLD,
    hot_top_n: int = FUND_FLOW_HIDDEN_HOT_TOP_N,
    hidden_top_n: int = FUND_FLOW_HIDDEN_TOP_N,
) -> Dict:
    """汇总三块数据，各部分独立 try 互不拖垮；60s 内存缓存。

    返回结构：
    {
      stocks: {inflow:[...], outflow:[...]},
      sectors: {industry:{inflow,outflow}, concept:{inflow,outflow}},
      hiddenGems: [...],
      updateTime, params
    }
    """
    key = _cache_key(stock_top_n, sector_top_n, change_threshold, hot_top_n, hidden_top_n)
    cached = _CACHE.get(key)
    now = time.time()
    if cached and (now - cached['ts']) < FUND_FLOW_CACHE_TTL:
        return cached['data']

    # 各部分独立 try，互不拖垮；段间轻量休眠规避限流
    stocks: Dict[str, List[Dict]] = {'inflow': [], 'outflow': []}
    sectors: Dict = {}
    hidden: List[Dict] = []
    try:
        stocks = await get_stock_fund_flow_rankings(stock_top_n)
    except Exception as e:
        print(f'[资金流向] 个股排名失败: {e}')
    await asyncio.sleep(0.25)
    try:
        sectors = await get_sector_fund_flow_rankings(sector_top_n)
    except Exception as e:
        print(f'[资金流向] 板块排名失败: {e}')
    await asyncio.sleep(0.25)
    try:
        hidden = await get_hidden_gems(change_threshold, hot_top_n, hidden_top_n)
    except Exception as e:
        print(f'[资金流向] 非热点上涨股失败: {e}')

    data = {
        'stocks': stocks,
        'sectors': sectors,
        'hiddenGems': hidden,
        'updateTime': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'params': {
            'stockTopN': stock_top_n,
            'sectorTopN': sector_top_n,
            'changeThreshold': change_threshold,
            'hotTopN': hot_top_n,
            'hiddenTopN': hidden_top_n,
        },
    }
    _CACHE[key] = {'ts': now, 'data': data}
    return data
