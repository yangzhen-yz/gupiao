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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import (Dict, List, Optional)

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
from db.database import load_hot_search_ranking, save_fund_flow_snapshot, load_fund_flow_snapshot
from services.data.stock import has_delisting_risk  # 复用 ST/退市 基础过滤

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
    # 2026-10-17：5 次重试在东财「整体封锁」时是纯浪费（每次都要跑满 timeout+退避，
    # 三个区块串行下来能把接口拖到 30s+）。东财要么一次就通、要么被封（重试也没用），
    # 且已有熔断 + 新浪兜底接盘，所以降到 2 次：既保留对偶发抖动的重试，又不拖慢响应。
    retries = 2
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
#   - 任一区块本轮拉取失败或返回空时，回退到该区块的 _LAST_GOOD_*（永不过期，内存为空时
#     从 DB fund_flow_snapshot 表恢复）；只有成功且非空的区块才写入兜底（内存 + DB），
#     故历史数据只会被更新的成功数据覆盖，绝不会因一次失败或服务重启被清空。
#   - 收盘后（非交易时段）数据已定型：已有快照的区块直接用快照、完全不请求东财
#     （2026-10-16），避免整晚轮询触发 push2 IP 风控。
_CACHE: Dict[str, Dict] = {}
_LAST_GOOD_STOCK: Dict[str, Dict] = {}
_LAST_GOOD_SECTOR: Dict[str, Dict] = {}
_LAST_GOOD_HIDDEN: Dict[str, List[Dict]] = {}

# ========== clist 熔断（2026-10-17）：东财对 clist 接口偶发 IP 级限流/封禁，
# 表现为 403 / 连接被重置(RemoteDisconnected)。前端 30s 轮询若每次都硬重试，
# 反而会越重试越被封。连续失败达阈值即进入冷却窗口，期间直接走快照兜底、不再打东财，
# 既降低被封概率，也避免无谓重试拖垮响应。解封后首次成功即复位并落盘快照。
# 2026-10-17 起熔断状态收敛到 services.data.eastmoney_guard（情绪/AI诊断等热路径共用），
# 本模块只保留委托，行为与阈值不变。 ==========
_EASTMONEY_GUARD = None


def _guard():
    """共享熔断器（2026-10-17 起收敛到 services.data.eastmoney_guard，情绪/AI诊断等共用）"""
    global _EASTMONEY_GUARD
    if _EASTMONEY_GUARD is None:
        from services.data import eastmoney_guard
        _EASTMONEY_GUARD = eastmoney_guard
    return _EASTMONEY_GUARD


def _clist_circuit_open() -> bool:
    return _guard().skip('clist')


def _clist_on_success() -> None:
    _guard().ok('clist')


def _clist_on_failure() -> None:
    _guard().fail('clist')


# ========== 新浪兜底（2026-10-17）：东财 clist 被封锁期间的替代资金流来源 ==========
# 背景：东财 push2/push2delay 的 API 路径会对高频来源做 IP 级封锁（403 / 连接被重置），
# 一锁往往数小时，资金流向页会长期空白。新浪 MoneyFlow 接口（逐票）实测可用，
#   主力净流入 = 超大单(r0) 净额 + 大单(r1) 净额
# 代价是要逐票拉取，所以一次性算好全市场快照并缓存，排名/非热点都从这份快照派生。
_SINA_MONEY_FLOW_URL = ('https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php'
                        '/MoneyFlow.ssi_ssfx_flzjtj')
_SINA_SNAPSHOT_CACHE: Dict[str, object] = {}   # {'ts': float, 'rows': List[Dict]}
_SINA_SNAPSHOT_TTL = 600                       # 全市场快照缓存 10 分钟（逐票拉取代价高）
_SINA_WAIT_TIMEOUT = 20                        # 首轮最多等 20s，超时让后台继续算
_SINA_WORKERS = 10
_SINA_COMPUTING = False                        # 是否正在计算（防并发重复触发）


def _prefixed(code: str) -> str:
    """纯代码 → 带市场前缀（新浪 daima 与腾讯批量行情都要带前缀的代码）。"""
    c = str(code)
    if c.startswith(('sh', 'sz')):
        return c
    return ('sh' + c) if c.startswith('6') else ('sz' + c)


def _fetch_sina_money_flow(symbol: str) -> Optional[float]:
    """单只主力净流入（元）= 超大单(r0)+大单(r1) 的净额；取不到返回 None。"""
    try:
        r = requests.get(f'{_SINA_MONEY_FLOW_URL}?daima={symbol}',
                         headers={'User-Agent': 'Mozilla/5.0',
                                  'Referer': 'https://finance.sina.com.cn'},
                         timeout=8, proxies={'http': None, 'https': None})
        r.raise_for_status()
        d = r.json() or {}

        def _f(k):
            try:
                return float(d.get(k) or 0)
            except (TypeError, ValueError):
                return 0.0
        return (_f('r0_in') - _f('r0_out')) + (_f('r1_in') - _f('r1_out'))
    except Exception:
        return None


async def _sina_compute_snapshot() -> Optional[List[Dict]]:
    """真正干活：逐票拉新浪资金流 + 批量补腾讯行情，结果写入缓存。"""
    global _SINA_COMPUTING
    try:
        try:
            from services.data.universe import selectable_symbols
            codes = [c for c in selectable_symbols() if len(str(c)) == 6 and str(c).isdigit()]
        except Exception as e:
            print(f'[资金流向] 新浪兜底：取可选股清单失败 {e}')
            return None
        if not codes:
            return None
        symbols = [_prefixed(c) for c in codes]

        def _sync_flows():
            out = {}
            with ThreadPoolExecutor(max_workers=_SINA_WORKERS) as ex:
                futs = {ex.submit(_fetch_sina_money_flow, s): s for s in symbols}
                for fut in as_completed(futs):
                    s = futs[fut]
                    try:
                        v = fut.result()
                    except Exception:
                        v = None
                    if v is not None:
                        out[s] = v
            return out

        flows = await asyncio.to_thread(_sync_flows)
        if not flows:
            print('[资金流向] 新浪兜底：资金流一只都没取到，本轮放弃')
            return None

        # 批量补行情（名称/价格/涨跌幅）—— 腾讯行情源正常，与东财互不影响
        try:
            from services.data.stock import fetch_with_retry, parse_tencent_batch_data
            quotes: Dict[str, Dict] = {}
            for i in range(0, len(symbols), 60):
                try:
                    data = await fetch_with_retry(','.join(symbols[i:i + 60]))
                    for row in parse_tencent_batch_data(data):
                        quotes[row.get('symbol')] = row
                except Exception:
                    pass
                await asyncio.sleep(0.05)
        except Exception as e:
            print(f'[资金流向] 新浪兜底：补行情失败(仍返回资金流) {e}')
            quotes = {}

        rows = []
        for sym, net in flows.items():
            q = quotes.get(sym) or {}
            rows.append({
                'symbol': sym,
                'code': q.get('code') or sym[2:],
                'name': q.get('name') or '',
                'price': _to_float(q.get('priceRaw') or q.get('price')),
                'changePercent': _to_float(q.get('changePercent')),
                'mainNetInflow': net,
                'industry': '',
            })
        print(f'[资金流向] 新浪兜底快照完成：{len(rows)} 只')
        _SINA_SNAPSHOT_CACHE['ts'] = time.time()
        _SINA_SNAPSHOT_CACHE['rows'] = rows
        return rows
    finally:
        _SINA_COMPUTING = False


async def _sina_market_snapshot() -> Optional[List[Dict]]:
    """取全市场「行情 + 主力净流入」快照（东财不可用时的兜底数据源）。

    一次计算约千级请求、实测约 2 分钟，绝不能跟着 30s 轮询重复跑，所以：
      · 有新鲜缓存 → 直接返回；
      · 正在计算中 → 本轮不等、直接返回 None（下轮来取）；
      · 否则后台起算，本轮最多等 _SINA_WAIT_TIMEOUT 秒；超时先返回 None，
        后台继续算完入缓存，下一轮轮询即可拿到数据（避免接口干等两分钟）。
    """
    now = time.time()
    cached = _SINA_SNAPSHOT_CACHE.get('rows')
    if cached and (now - float(_SINA_SNAPSHOT_CACHE.get('ts') or 0)) < _SINA_SNAPSHOT_TTL:
        return cached
    global _SINA_COMPUTING
    if _SINA_COMPUTING:
        return None
    _SINA_COMPUTING = True
    task = asyncio.create_task(_sina_compute_snapshot())
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=_SINA_WAIT_TIMEOUT)
    except Exception:
        return None


async def _sina_stock_rankings(top_n: int) -> Optional[Dict[str, List[Dict]]]:
    """从新浪快照派生个股主力净流入 / 净流出排名。"""
    rows = await _sina_market_snapshot()
    if not rows:
        return None
    ranked = sorted(rows, key=lambda r: r['mainNetInflow'], reverse=True)
    return {
        'inflow': [r for r in ranked[:top_n] if r['mainNetInflow'] > 0],
        'outflow': [r for r in ranked[-top_n:][::-1] if r['mainNetInflow'] < 0],
    }


async def _sina_hidden_gems(change_threshold: float, hot_top_n: int,
                            top_n: int) -> List[Dict]:
    """从新浪快照派生「非热点上涨股」：涨幅达标且不在热搜榜前 N。"""
    rows = await _sina_market_snapshot()
    if not rows:
        return []
    try:
        hot = load_hot_search_ranking() or {}
        hot_syms = {_prefixed(s.get('symbol')) for s in (hot.get('stocks') or [])[:hot_top_n]
                    if s.get('symbol')}
    except Exception:
        hot_syms = set()
    gems = [r for r in rows
            if r['changePercent'] >= change_threshold and r['symbol'] not in hot_syms]
    gems.sort(key=lambda r: r['changePercent'], reverse=True)
    return gems[:top_n]


def _in_trading_window() -> bool:
    """粗判盘中（周一~周五 9:15-15:05）。收盘后已有快照的区块不再请求东财。"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= hm <= (15 * 60 + 5)


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
    closed = not _in_trading_window()
    cached = _CACHE.get(key)
    now = time.time()
    # 盘中：短 TTL 缓存命中直接返回；收盘后：数据已定型，有缓存即返回
    if cached and (not closed or (now - cached['ts']) < FUND_FLOW_CACHE_TTL):
        return cached['data']

    stock_k = str(stock_top_n)
    sector_k = str(sector_top_n)
    hidden_k = _hidden_key(change_threshold, hot_top_n, hidden_top_n)

    # 兜底先就位（内存 → DB 快照恢复）；收盘后已有快照的区块不再请求东财
    lg_stocks = _LAST_GOOD_STOCK.get(stock_k)
    if not lg_stocks:
        lg_stocks = load_fund_flow_snapshot(f'stocks:{stock_k}')
        if lg_stocks:
            _LAST_GOOD_STOCK[stock_k] = lg_stocks
    lg_sectors = _LAST_GOOD_SECTOR.get(sector_k)
    if not lg_sectors:
        lg_sectors = load_fund_flow_snapshot(f'sectors:{sector_k}')
        if lg_sectors:
            _LAST_GOOD_SECTOR[sector_k] = lg_sectors
    lg_hidden = _LAST_GOOD_HIDDEN.get(hidden_k)
    if not lg_hidden:
        lg_hidden = load_fund_flow_snapshot(f'hidden:{hidden_k}')
        if lg_hidden:
            _LAST_GOOD_HIDDEN[hidden_k] = lg_hidden

    # 各部分独立拉取；失败或返回空则回退历史成功数据（内存/DB 快照）
    stocks: Dict[str, List[Dict]] = {'inflow': [], 'outflow': []}
    sectors: Dict = {}
    hidden: List[Dict] = []
    cooling = _clist_circuit_open()

    if cooling:
        print('[资金流向] clist 冷却中，本轮跳过东财、改走新浪兜底/快照')
    # 个股排名三级回退：东财（最优）→ 新浪兜底（新鲜）→ 历史快照（陈旧但不空）
    src_stocks = 'none'
    if closed and lg_stocks:
        stocks = lg_stocks
        src_stocks = 'snapshot'
    else:
        if not cooling:
            try:
                _r = await get_stock_fund_flow_rankings(stock_top_n)
                if _r.get('inflow') or _r.get('outflow'):
                    stocks, src_stocks = _r, 'eastmoney'
                    _clist_on_success()
                    _LAST_GOOD_STOCK[stock_k] = stocks
                    save_fund_flow_snapshot(f'stocks:{stock_k}', stocks)
            except Exception as e:
                print(f'[资金流向] 个股排名拉取失败: {e}')
                _clist_on_failure()
        if src_stocks == 'none':
            # 东财不可用（异常 / 空 / 冷却）→ 新浪逐票资金流兜底
            _fb = await _sina_stock_rankings(stock_top_n)
            if _fb and (_fb.get('inflow') or _fb.get('outflow')):
                stocks, src_stocks = _fb, 'sina'
                print('[资金流向] 个股排名使用新浪兜底（东财不可用）')
                _LAST_GOOD_STOCK[stock_k] = stocks
                save_fund_flow_snapshot(f'stocks:{stock_k}', stocks)
            elif lg_stocks:
                stocks, src_stocks = lg_stocks, 'snapshot'
                print('[资金流向] 个股排名使用最后成功快照兜底')
    await asyncio.sleep(0.25)
    if closed and lg_sectors:
        sectors = lg_sectors
    elif cooling:
        sectors = lg_sectors or {}
    else:
        try:
            sectors = await get_sector_fund_flow_rankings(sector_top_n)
            if sectors.get('industry') or sectors.get('concept'):
                _clist_on_success()
                _LAST_GOOD_SECTOR[sector_k] = sectors
                save_fund_flow_snapshot(f'sectors:{sector_k}', sectors)
            else:
                if lg_sectors:
                    sectors = lg_sectors
                    print('[资金流向] 板块排名使用最后成功快照兜底')
        except Exception as e:
            print(f'[资金流向] 板块排名拉取失败: {e}')
            _clist_on_failure()
            if lg_sectors:
                sectors = lg_sectors
                print('[资金流向] 板块排名使用最后成功快照兜底')
    await asyncio.sleep(0.25)
    # 非热点上涨股三级回退：东财 → 新浪兜底 → 历史快照
    src_hidden = 'none'
    if closed and lg_hidden:
        hidden = lg_hidden
        src_hidden = 'snapshot'
    else:
        if not cooling:
            try:
                _h = await get_hidden_gems(change_threshold, hot_top_n, hidden_top_n)
                if _h:
                    hidden, src_hidden = _h, 'eastmoney'
                    _clist_on_success()
                    _LAST_GOOD_HIDDEN[hidden_k] = hidden
                    save_fund_flow_snapshot(f'hidden:{hidden_k}', hidden)
            except Exception as e:
                print(f'[资金流向] 非热点上涨股拉取失败: {e}')
                _clist_on_failure()
        if src_hidden == 'none':
            _hg = await _sina_hidden_gems(change_threshold, hot_top_n, hidden_top_n)
            if _hg:
                hidden, src_hidden = _hg, 'sina'
                print('[资金流向] 非热点上涨股使用新浪兜底（东财不可用）')
                _LAST_GOOD_HIDDEN[hidden_k] = hidden
                save_fund_flow_snapshot(f'hidden:{hidden_k}', hidden)
            elif lg_hidden:
                hidden, src_hidden = lg_hidden, 'snapshot'
                print('[资金流向] 非热点上涨股使用最后成功快照兜底')

    data = {
        'stocks': stocks,
        'sectors': sectors,
        'hiddenGems': hidden,
        'cooling': cooling,
        'dataSource': {'stocks': src_stocks, 'hidden': src_hidden},
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
