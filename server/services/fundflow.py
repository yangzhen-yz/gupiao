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
from typing import (Dict, List)

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

# 概念板块黑名单：东方财富 m:90+t:3 返回的概念板块中混入了大量"风格类 / 通道类 / 筛选类"
# 宽泛概念（如融资融券、深股通、昨日高振幅等），这些概念因涵盖个股多、主力净流入天然偏大，
# 会把真正的题材概念（如商业航天、机器人、MLCC）挤出排名。以下集合用于过滤这些宽泛概念。
_BROAD_CONCEPT_BLOCKLIST = frozenset({
    # 通道/机制类
    '融资融券', '沪股通', '深股通', '沪港通', '深港通', '转融券', '标普道琼斯A股', 'MSCI中国',
    '富时罗素', '标准普尔', 'GDR', '科创板做市商',
    # 风格/规模类
    '大盘股', '中盘股', '小盘股', '大盘价值', '大盘成长', '中盘价值', '中盘成长', '小盘价值', '小盘成长',
    '价值股', '成长股', '蓝筹股', '红利股', '低价股', '高价股', '中价股', '破净股', '高股息股',
    '科技风格', '消费风格', '金融地产风格', '趋势股',
    # 持仓/筹码统计类
    '基金重仓', '机构重仓', 'QFII重仓', '券商金股', '股权分散', '东方财富热股',
    # 筛选/统计类
    '昨日涨停', '昨日连板', '昨日高振幅', '昨日强势', '昨日弱势', '昨日大幅回落',
    '今日涨停', '今日连板', '今日强势', '今日弱势',
    '连续涨停', '连续跌停', '涨停板', '跌停板',
    '强势股', '弱势股', '活跃股', '换手率', '量比', '委比',
    '高换手', '低换手', '放量大涨', '缩量下跌',
    # 价格/估值筛选类
    '百元股', '千元股', '低市净率', '高市净率', '低市盈率', '高市盈率',
    '近期新高', '百日新高', '历史新高', '近期新低', '破增发价股', '破发股',
    '长期破净', '红利破净股',
    # 指数成分类
    '上证50', '沪深300', '中证500', '中证1000', '中证2000', '创业板50', '科创50', '北证50',
    '上证180', '上证380', '深证100', '深证300', '深成500', 'HS300_', '深证100R',
    '创业板综', '创业成份', '茅指数', '宁组合',
    # 业绩/财务类
    '预盈预增', '预亏预减', '高送转', '高派息', '高分红', '业绩预升', '业绩预降',
    '2026中报预增', '2026一季报预增', '2025年报预增', '中报预增', '年报预增', '一季报预增',
    '摘帽', 'ST板块', '退市',
    # 其他宽泛
    '全部A股', '沪市A股', '深市A股', '科创板', '创业板', '主板', '北交所',
    '注册制', '核准制', 'IPO',
})


def _clist(fid: str, po: int, fs: str, fields: str, pz: int, pn: int = 1) -> List[Dict]:
    """东方财富 clist 同步请求，返回 diff 列表（失败抛异常由上层 catch）。

    fltt=2：价格/涨跌幅已为真实数值（f3 直接是百分比，无需 /100）。
    东方财富接口偶发返回空 body / 非 JSON（限流或 gzip 异常），此处重试 5 次提高健壮性；
    最后一次失败会在日志里打印 fid/fs 便于定位。
    """
    params = {
        'pn': pn, 'pz': pz, 'po': po, 'np': 1, 'fltt': 2, 'invt': 2,
        'fid': fid, 'fs': fs, 'fields': fields,
        'ut': 'b2884a393a59ad64002292a3e90d46a5',
    }
    last_err = None
    retries = 5
    # 强制直连东方财富，忽略环境代理（沙箱透明代理 127.0.0.1 偶发断连会导致 ProxyError；
    # A股数据源国内直连即可，无需代理）。
    _NO_PROXY = {'http': None, 'https': None}
    for attempt in range(retries):
        try:
            r = requests.get(_EASTMONEY_CLIST, params=params, headers=EASTMONEY_HEADERS,
                             timeout=12, proxies=_NO_PROXY)
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
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
    # 全部重试失败：打印 fid/fs 便于排查，再抛出上层
    print(f'[资金流向] clist 全部{retries}次重试失败 fid={fid!r} fs={fs!r} err={last_err}')
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
    """板块（行业 / 概念）净流入 / 净流出排名。

    概念板块会过滤掉风格类/通道类宽泛概念（黑名单见 _BROAD_CONCEPT_BLOCKLIST），
    只保留商业航天、机器人、MLCC 等真正的题材概念。
    """
    _concept_fetch = top_n * 2  # 多拉取，过滤后截断到 top_n

    def _sync():
        ind_in = _clist('f62', 1, _FS_INDUSTRY, 'f12,f14,f3,f62,f104,f105', top_n)
        time.sleep(0.2)
        ind_out = _clist('f62', 0, _FS_INDUSTRY, 'f12,f14,f3,f62,f104,f105', top_n)
        time.sleep(0.2)
        con_in = _clist('f62', 1, _FS_CONCEPT, 'f12,f14,f3,f62,f104,f105', _concept_fetch)
        time.sleep(0.2)
        con_out = _clist('f62', 0, _FS_CONCEPT, 'f12,f14,f3,f62,f104,f105', _concept_fetch)
        return ind_in or [], ind_out or [], con_in or [], con_out or []
    ind_in, ind_out, con_in, con_out = await asyncio.to_thread(_sync)

    # 过滤宽泛概念，只保留题材概念
    con_in_f = [x for x in con_in if (x.get('f14') or '') not in _BROAD_CONCEPT_BLOCKLIST]
    con_out_f = [x for x in con_out if (x.get('f14') or '') not in _BROAD_CONCEPT_BLOCKLIST]

    return {
        'industry': {
            'inflow': [_normalize_sector(x, i + 1) for i, x in enumerate(ind_in)],
            'outflow': [_normalize_sector(x, i + 1) for i, x in enumerate(ind_out)],
        },
        'concept': {
            'inflow': [_normalize_sector(x, i + 1) for i, x in enumerate(con_in_f[:top_n])],
            'outflow': [_normalize_sector(x, i + 1) for i, x in enumerate(con_out_f[:top_n])],
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


# ========== 内存缓存 ==========
# 三层缓存，共同解决「东财偶发限流/空响应 → 前端 30s 轮询把『暂无数据』闪到页面上」
# （表现为『切 Tab 后数据没了，需手动刷新』）：
#   1) _CACHE          组合缓存（按完整参数 key，TTL 命中直接返回，省去每轮重复打 Eastmoney）
#   2) _LAST_GOOD_*    各区块「最后一次成功数据」，永不过期、只被成功刷新覆盖 —— 核心兜底
# 设计要点：
#   - 个股 / 板块区块不依赖 hiddenGems 的参数(change_threshold/hot_top_n)，用独立子 key，
#     因此切换「非热点上涨股」筛选条件不会令个股 / 板块缓存失效；
#   - 任一区块本轮拉取失败时，回退到该区块的 _LAST_GOOD_*（永不过期）；只有成功且非空的区块
#     才写入 _LAST_GOOD_*，故历史数据只会被更新的成功数据覆盖，绝不会因一次失败被清空。
_CACHE: Dict[str, Dict] = {}
_LAST_GOOD_STOCK: Dict[str, Dict] = {}
_LAST_GOOD_SECTOR: Dict[str, Dict] = {}
_LAST_GOOD_HIDDEN: Dict[str, List[Dict]] = {}


def _cache_key(stock_top_n, sector_top_n, change_threshold, hot_top_n, hidden_top_n) -> str:
    return f"{stock_top_n}|{sector_top_n}|{change_threshold}|{hot_top_n}|{hidden_top_n}"


def _hidden_key(change_threshold, hot_top_n, hidden_top_n) -> str:
    return f"{change_threshold}|{hot_top_n}|{hidden_top_n}"


async def get_fund_flow_summary(
    stock_top_n: int = FUND_FLOW_STOCK_TOP_N,
    sector_top_n: int = FUND_FLOW_SECTOR_TOP_N,
    change_threshold: float = FUND_FLOW_HIDDEN_CHANGE_THRESHOLD,
    hot_top_n: int = FUND_FLOW_HIDDEN_HOT_TOP_N,
    hidden_top_n: int = FUND_FLOW_HIDDEN_TOP_N,
) -> Dict:
    """汇总三块数据，各部分独立拉取互不拖垮；带内存缓存与「历史成功兜底」。

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
    # 组合缓存命中（未过期）：直接返回，省去三块重拉
    if cached and (now - cached['ts']) < FUND_FLOW_CACHE_TTL:
        return cached['data']

    stock_k = str(stock_top_n)
    sector_k = str(sector_top_n)
    hidden_k = _hidden_key(change_threshold, hot_top_n, hidden_top_n)

    # 历史成功兜底（永不过期，仅被成功刷新覆盖）
    lg_stocks = _LAST_GOOD_STOCK.get(stock_k)
    lg_sectors = _LAST_GOOD_SECTOR.get(sector_k)
    lg_hidden = _LAST_GOOD_HIDDEN.get(hidden_k)

    # 各部分独立拉取；失败则回退历史成功数据（若从未成功过则留空，由前端 loading 兜底）
    stocks: Dict[str, List[Dict]] = {'inflow': [], 'outflow': []}
    sectors: Dict = {}
    hidden: List[Dict] = []
    try:
        stocks = await get_stock_fund_flow_rankings(stock_top_n)
    except Exception as e:
        print(f'[资金流向] 个股排名失败，回退历史数据: {e}')
        if lg_stocks:
            stocks = lg_stocks
    await asyncio.sleep(0.25)
    try:
        sectors = await get_sector_fund_flow_rankings(sector_top_n)
    except Exception as e:
        print(f'[资金流向] 板块排名失败，回退历史数据: {e}')
        if lg_sectors:
            sectors = lg_sectors
    await asyncio.sleep(0.25)
    try:
        hidden = await get_hidden_gems(change_threshold, hot_top_n, hidden_top_n)
    except Exception as e:
        print(f'[资金流向] 非热点上涨股失败，回退历史数据: {e}')
        if lg_hidden:
            hidden = lg_hidden

    # 仅把「成功且非空」的区块写入历史成功缓存（核心：失败绝不清空历史）
    if stocks.get('inflow') or stocks.get('outflow'):
        _LAST_GOOD_STOCK[stock_k] = stocks
    if sectors.get('industry') or sectors.get('concept'):
        _LAST_GOOD_SECTOR[sector_k] = sectors
    if hidden:
        _LAST_GOOD_HIDDEN[hidden_k] = hidden

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
    # 组合缓存：记录本次（已兜底）结果，供 TTL 内快速返回
    _CACHE[key] = {'ts': now, 'data': data}
    return data
