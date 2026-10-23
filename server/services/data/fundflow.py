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

import re as _re     # 行业名去国标码用（见 _clean_industry）

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
            # 连接被重置（RemoteDisconnected）= 这个 IP 正在被东财封，
            # 再睡 0.5s 打第二次只是给它续封；分级交给 eastmoney_guard.classify。
            try:
                from services.data import eastmoney_guard
                if not eastmoney_guard.should_retry(e):
                    break
            except Exception:
                pass
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
    # 重试后仍失败：打印 fid/fs 便于排查，再抛出上层（异常对象带着分级信息一起上去）
    print(f'[资金流向] clist 失败（{retries} 次内）fid={fid!r} fs={fs!r} err={last_err}')
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
    """个股主力净流入 / 净流出排名。

    2026-10-19 请求量治理：原为 inflow/outflow 两次请求，东财 IP 级风控实锤后
    （封禁期间人气榜/涨停池通、clist 全断，10-17/10-19 两次封禁均因请求频率）
    合并为**一次** f62 降序 pz=100：头部即净流入排名、尾部反转即净流出排名，
    展示层按 top_n 截断。配合前端轮询 30s→120s，单页请求量降约 7 倍。
    """
    def _sync():
        # clist 单页硬限 100（东财上限）；top_n 默认 50，前后各取足够
        pz = min(max(100, top_n), 100)
        return _clist('f62', 1, _FS_ALL_A, 'f12,f13,f14,f2,f3,f62,f100', pz) or []
    raw = await asyncio.to_thread(_sync)
    items = [_normalize_stock(x) for x in raw]
    inflow = [x for x in items if x['mainNetInflow'] > 0][:top_n]
    outflow = [x for x in reversed(items) if x['mainNetInflow'] < 0][:top_n]
    return {'inflow': inflow, 'outflow': outflow}


async def get_sector_fund_flow_rankings(top_n: int = FUND_FLOW_SECTOR_TOP_N) -> Dict:
    """板块（行业 / 概念）净流入 / 净流出排名。

    2026-10-19 请求量治理：原为 行业in/out + 概念in/out 四次请求，合并为
    行业一次 + 概念一次（各 pz=100 f62 降序），展示层按 f62 正负拆分截断。
    概念板块会过滤掉风格类/通道类宽泛概念（黑名单见 _BROAD_CONCEPT_BLOCKLIST），
    只保留商业航天、机器人、MLCC 等真正的题材概念。
    """
    def _sync():
        ind = _clist('f62', 1, _FS_INDUSTRY, 'f12,f14,f3,f62,f104,f105', 100)
        time.sleep(0.2)
        # 概念多拉（过滤宽泛概念后会变少），pz=100 覆盖默认 20
        con = _clist('f62', 1, _FS_CONCEPT, 'f12,f14,f3,f62,f104,f105', 100)
        return ind or [], con or []
    ind_raw, con_raw = await asyncio.to_thread(_sync)

    def _split(raw: List[Dict], is_concept: bool = False):
        rows = raw
        if is_concept:
            rows = [x for x in raw if (x.get('f14') or '') not in _BROAD_CONCEPT_BLOCKLIST]
        inflow = [_normalize_sector(x, i + 1) for i, x in enumerate(rows)
                  if _to_float(x.get('f62')) > 0][:top_n]
        outflow = [_normalize_sector(x, i + 1) for i, x in enumerate(reversed(rows))
                   if _to_float(x.get('f62')) < 0][:top_n]
        return inflow, outflow

    ind_in, ind_out = _split(ind_raw)
    con_in, con_out = _split(con_raw, is_concept=True)
    return {
        'industry': {'inflow': ind_in, 'outflow': ind_out},
        'concept': {'inflow': con_in, 'outflow': con_out},
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
        # 取全市场涨幅前 100（f3 降序），字段含 f62（主力净流入）、f100（行业）。
        # 2026-10-19：300→100（clist 单页硬限附近、hidden_top_n 默认 20 足够筛），降负载。
        return _clist('f3', 1, _FS_ALL_A, 'f12,f13,f14,f2,f3,f62,f100', 100) or []
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
#   2) _LAST_GOOD      各区块「最后一次成功数据」，永不过期、只被成功刷新覆盖 —— 核心兜底
# 设计要点：
#   - **2026-10-19 快照 key 去参数化**（stocks/sectors/hidden，不再带 top_n）：
#     修复「实时数据落在 stocks:25、前端默认参数读 stocks:50 旧快照」的割裂——
#     东财封禁日测试参数刷新成功、默认参数刷新失败，页面永远展示旧快照。
#     现在快照恒存 top 100 原始行，展示层按 top_n 截断，任何参数刷新都惠及所有展示。
#   - 任一区块本轮拉取失败或返回空时，回退到该区块的 _LAST_GOOD（永不过期，内存为空时
#     从 DB fund_flow_snapshot 表恢复）；只有成功且非空的区块才写入兜底（内存 + DB），
#     故历史数据只会被更新的成功数据覆盖，绝不会因一次失败或服务重启被清空。
#   - 收盘后（非交易时段）数据已定型：已有快照的区块直接用快照、完全不请求东财
#     （2026-10-16），避免整晚轮询触发 push2 IP 风控。
#   - **2026-10-19 龄期检查**：closed 分支用快照前检查年龄，超过 _STALE_AFTER_SEC
#     （2 小时）标记 stale——封禁日「今天从未成功刷新」时静默返回上周五快照，
#     用户根本看不出数据是旧的（updateTime 还显示本次调用时间，双重误导）。
_CACHE: Dict[str, Dict] = {}
_LAST_GOOD: Dict[str, Dict] = {}   # 'stocks'/'sectors'/'hidden' -> {'data':..., 'ts': epoch}
_STALE_AFTER_SEC = 2 * 3600        # 快照超过 2 小时标记 stale（盘中正常刷新间隔分钟级）

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


def _clist_on_failure(err=None) -> None:
    # 把异常对象交给熔断器分级：blocked（连不上）会连带打开全域闸门，
    # 因为封的是 IP —— 资金流向被封时，涨停池/龙虎榜/公告也一定在吃闭门羹。
    _guard().fail('clist', err)


# ========== 新浪兜底（2026-10-17，2026-10-19 改成排行分页）==========
# 背景：东财 push2/push2delay 的 clist 会对来源做 IP 级封锁（403 / 连上就重置），一锁数小时，
# 资金流向页会长期停在旧快照上。新浪 MoneyFlow 实测可用，问题在**怎么用它**：
#
#   旧写法（逐票）：`MoneyFlow.ssi_ssfx_flzjtj?daima=xxx` 一次一只，全市场 1094 只
#     → 一轮 1094 个请求、约 2 分钟算完；这期间 `_SINA_COMPUTING` 挡着，页面只能继续显示
#     旧快照（今天 08:51 之后一直是这样）。更要命的是：这个量本身就是在给新浪攒封禁，
#     备源先被关掉，等于把"东财被封"变成"两源都没数据"。
#   新写法（排行分页）：`MoneyFlow.ssl_bkzj_ssggzj?page&num&sort&asc` 一次 100 只、
#     服务端排好序 → 个股净流入/净流出排名只要 **4 个请求**（降序 2 页 + 升序 2 页）；
#     板块聚合与非热点要全市场，才翻到底（约 70 页、30 分钟缓存一次）。
#     请求量从 1094/10分钟 降到 4/5分钟 + 70/30分钟，约 **1/16**，且首屏不再等 2 分钟。
#
# 口径差（必须说清，不能假装两源一样）：排行接口只给到 r0（超大单）净额，
# 没有 r1（大单），而东财 f62 = 超大单 + 大单。所以备源的"主力净流入"偏小一点、
# 排名也可能有出入 —— 行里带 `flowCaliber`，前端标注出来，不悄悄替换。
_SINA_RANK_URL = ('https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php'
                  '/MoneyFlow.ssl_bkzj_ssggzj')
_SINA_MONEY_FLOW_URL = ('https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php'
                        '/MoneyFlow.ssi_ssfx_flzjtj')      # 单票口径（r0+r1），只给少数票补口径用
_SINA_RANK_PAGE_SIZE = 100         # 新浪单页上限实测 100
_SINA_RANK_MAX_PAGES = 80          # 全市场（含基金/债券ETF）约 70 页到底，留余量
_SINA_RANK_WORKERS = 4             # 并发 4：70 页约 12~18 秒，且不至于像 10 并发那样刺眼
_SINA_RANK_TOP_PAGES = 2           # 个股排名每侧取 2 页（200 只，过滤掉基金后够截 top_n）
_SINA_SNAPSHOT_CACHE: Dict[str, object] = {}   # 全市场快照 {'ts': float, 'rows': List[Dict]}
_SINA_SNAPSHOT_TTL = 1800                      # 全市场快照 30 分钟（板块聚合用它，30 分钟够新）
_SINA_RANK_CACHE: Dict[str, Dict] = {}         # 排名结果按 side 缓存
_SINA_IND_CACHE: Dict[str, object] = {'ts': 0.0, 'map': {}}   # 本地行业分类，1 小时
_SINA_RANK_TTL = 300                           # 个股排名 5 分钟新鲜度（只要 4 个请求，可以勤一点）
_SINA_WAIT_TIMEOUT = 6                         # 首轮最多等 6s：全市场扫描要 ~18s，
#     堵在请求里就是 6 秒白屏；超时先返回空、后台算完入缓存，下一轮轮询自然拿到
_SINA_COMPUTING = False                        # 是否正在计算（防并发重复触发）
_NO_PROXY = {'http': None, 'https': None}      # 数据源都在国内，直连即可（走代理反而偶发断连）
_SINA_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                               '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                 'Referer': 'https://vip.stock.finance.sina.com.cn/',
                 'Accept': '*/*'}
# 只保留沪深 A 股：沪 60/68，深 00/30。新浪排行里混着场内基金（sh51x/sz15x）、
# 国债 ETF（sh511x）与北交所（bjxxxx），这些不是"个股主力净流入"页的口径，
# 而且它们动辄几十亿的净流进/流出会把真正的个股挤到看不见。
_A_SHARE_PREFIX = ('sh60', 'sh68', 'sz00', 'sz30')


def _prefixed(code: str) -> str:
    """纯代码 → 带市场前缀（新浪 daima 与腾讯批量行情都要带前缀的代码）。"""
    c = str(code)
    if c.startswith(('sh', 'sz')):
        return c
    return ('sh' + c) if c.startswith('6') else ('sz' + c)


def _is_a_share(symbol: str) -> bool:
    """新浪 symbol（sh600487/sz000001/bj920196/sh510500）→ 是否沪深 A 股。"""
    return str(symbol or '').lower().startswith(_A_SHARE_PREFIX)


def _clean_industry(name) -> str:
    """baostock 的行业名带国标分类码（'J66货币金融服务'），页面上只留中文那截。

    注意这跟东财 f100 的板块名（'银行'、'半导体'）**不是同一套分类**，
    去掉码只是让两源在观感上接近，不假装它们是同一个口径 ——
    数据源标注（dataSource/flowCaliber）里会写清这一列现在来自哪。
    """
    return _re.sub(r'^[A-Z]\d{2}', '', str(name or '')).strip()


def _sina_industry_map() -> Dict[str, str]:
    """本地行业分类（symbol -> 行业名），1 小时缓存。

    行业原来只有东财 clist 的 f100 一个来源，于是被封时整列变成"—"（2026-10-19 截图）。
    行业分类是几个月都不怎么变的东西，不该跟着行情接口一起失效：
    `scripts/sync_stock_industry.py` 用 baostock 一次登录把全市场分类落成 `stock_industry_map`
    （周更），这里优先读它，读不到再退回老的 `stock_basic_info.industry`（历史遗留，多数为空）。
    """
    global _SINA_IND_CACHE
    now = time.time()
    if _SINA_IND_CACHE['map'] and now - _SINA_IND_CACHE['ts'] < 3600:
        return _SINA_IND_CACHE['map']
    m: Dict[str, str] = {}
    try:
        from db.database import get_db_conn
        conn = get_db_conn()
        try:
            try:      # 首选：baostock 全量行业表（5200+ 只有行业名）
                for r in conn.execute(
                        "SELECT symbol, industry FROM stock_industry_map "
                        "WHERE industry IS NOT NULL AND industry != ''"):
                    m[str(r['symbol']).lower()] = _clean_industry(r['industry'])
            except Exception:
                pass      # 表还没建（没跑过同步）不算错，继续走下面的老表
            if not m:
                for r in conn.execute(
                        "SELECT symbol, industry FROM stock_basic_info "
                        "WHERE industry IS NOT NULL AND industry != ''"):
                    m[_prefixed(r['symbol'])] = _clean_industry(r['industry'])
        finally:
            conn.close()
    except Exception as e:
        print(f'[资金流向] 本地行业分类读取失败（行业列留空）: {e}')
        return _SINA_IND_CACHE['map']
    if m:
        _SINA_IND_CACHE = {'ts': now, 'map': m}
    return m


def _sina_rank_page(sort: str, asc: int, page: int) -> List[Dict]:
    """新浪资金流排行单页（100 只，服务端已按 sort 排好）。失败返回 []。"""
    try:
        r = requests.get(_SINA_RANK_URL,
                         params={'page': page, 'num': _SINA_RANK_PAGE_SIZE,
                                 'sort': sort, 'asc': asc},
                         headers=_SINA_HEADERS, timeout=10, proxies=_NO_PROXY)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        if page == 1:
            print(f'[资金流向] 新浪排行第{page}页失败: {type(e).__name__}: {str(e)[:80]}')
        return []


def _sina_map_rows(raw_pages: List[List[Dict]], keep: set) -> List[Dict]:
    """把若干页原始排行映射成页面要的行结构（并过滤掉基金/ETF/北交所）。

    行结构与旧版逐票快照一致（symbol/code/name/price/changePercent/mainNetInflow/industry），
    额外带 totalNetInflow / amount / turnover / flowCaliber；
    这样板块聚合、非热点、个股排名三条路都不用改调用方。
    `keep` 用于跨页去重（调用方传同一个 set 进来）。
    """
    ind = _sina_industry_map()
    out: List[Dict] = []
    for rows in raw_pages:
        for x in rows or []:
            sym = str(x.get('symbol') or '').lower()
            if not _is_a_share(sym) or sym in keep:
                continue
            keep.add(sym)

            def _f(k):
                try:
                    return float(x.get(k) or 0)
                except (TypeError, ValueError):
                    return 0.0
            out.append({
                'symbol': sym,
                'code': sym[2:],
                'name': x.get('name') or '',
                'price': _f('trade'),
                'changePercent': round(_f('changeratio') * 100, 2),
                # 排行接口只到 r0（超大单）；东财 f62 是超大单+大单，口径差写进 flowCaliber
                'mainNetInflow': _f('r0_net'),
                'totalNetInflow': _f('netamount'),
                'amount': _f('amount'),
                'turnover': _f('turnover'),
                'industry': ind.get(sym, ''),
                'flowCaliber': 'sina_r0',
            })
    return out


def _sina_rank_pages(sort: str, asc: int, pages: List[int]) -> List[List[Dict]]:
    """并发取指定页（4 并发，按传入顺序返回，保持服务端排序）。"""
    with ThreadPoolExecutor(max_workers=_SINA_RANK_WORKERS) as ex:
        return list(ex.map(lambda pg: _sina_rank_page(sort, asc, pg), pages))


def _sina_rank_rows(sort: str, asc: int, pages: int) -> List[Dict]:
    """取前 `pages` 页 → 排名行（快路径：个股净流入/净流出各只要 2 页）。"""
    return _sina_map_rows(_sina_rank_pages(sort, asc, list(range(1, pages + 1))), set())


def _sina_sweep(sort: str = 'r0_net', max_pages: int = _SINA_RANK_MAX_PAGES) -> List[Dict]:
    """把一个排序**翻到底** = 全市场快照（板块聚合、非热点要用）。

    降序一路翻过去就是全市场（从最大净流入到最大净流出），不需要正反各翻一遍。
    整窗都空 = 到底了，立刻收手 —— 别按 max_pages 硬翻页。
    """
    out: List[Dict] = []
    keep: set = set()
    pg = 1
    while pg <= max_pages:
        window = list(range(pg, min(pg + _SINA_RANK_WORKERS, max_pages + 1)))
        got = _sina_rank_pages(sort, 0, window)
        out.extend(_sina_map_rows(got, keep))
        if not any(got):
            break
        pg += len(window)
    return out


def _sina_rank_side_cached(asc: int) -> List[Dict]:
    """某一侧（asc=0 净流入降序 / asc=1 净流出升序）的排名，缓存 _SINA_RANK_TTL 秒。"""
    now = time.time()
    hit = _SINA_RANK_CACHE.get(asc)
    if hit and now - hit['ts'] < _SINA_RANK_TTL:
        return hit['rows']
    rows = _sina_rank_rows('r0_net', asc, _SINA_RANK_TOP_PAGES)
    if rows:                      # 空结果不进缓存：下一轮继续重试，别把一次失败冻 5 分钟
        _SINA_RANK_CACHE[asc] = {'ts': now, 'rows': rows}
    return rows


def _fetch_sina_money_flow(symbol: str) -> Optional[float]:
    """单只主力净流入（元）= 超大单(r0)+大单(r1) 的净额；取不到返回 None。

    2026-10-19 起**不再用于全市场**：那是要 1094 个请求的老写法（详见本段文件头）。
    保留它是因为排行接口拿不到 r1（大单），个别需要与东财 f62 对齐的场景可以按需补几只。
    """
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
    """全市场快照：把新浪排行接口翻到底（约 70 页 / 4 并发 / 十几秒）。

    行情、名称、涨跌幅、行业都在排行里带齐了，不用再逐票拉、也不用再补腾讯行情
    （老写法要 1094 次资金流 + 19 次批量行情，约 2 分钟；现在 70 次、十几秒）。
    这份全量只服务"板块聚合"和"非热点上涨股"；个股净流入/净流出排名走
    `_sina_stock_rankings` 的 4 请求快路径，不用等它。
    """
    global _SINA_COMPUTING
    try:
        rows = await asyncio.to_thread(_sina_sweep)
        if not rows:
            print('[资金流向] 新浪兜底：排行接口一页都没拿到，本轮放弃')
            return None
        print(f'[资金流向] 新浪兜底快照完成：{len(rows)} 只（排行分页，非逐票）')
        _SINA_SNAPSHOT_CACHE['ts'] = time.time()
        _SINA_SNAPSHOT_CACHE['rows'] = rows
        return rows
    finally:
        _SINA_COMPUTING = False


async def _sina_market_snapshot() -> Optional[List[Dict]]:
    """取全市场「行情 + 主力净流入」快照（东财不可用时的兜底数据源）。

    一次计算约 70 个请求、十几秒，但仍不能跟着轮询重复跑（TTL 30 分钟），所以：
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
    """个股主力净流入 / 净流出排名：服务端排序 + 每侧 2 页，共 4 个请求。

    旧写法是"等全市场快照算完再本地排序"，于是封禁日首屏要空等约 2 分钟；
    排行接口本来就按净流入排好，头部即净流入、升序头部即净流出，取两页足够
    过滤掉基金后凑满 top_n。全量快照留给板块聚合用，两条路互不阻塞。
    """
    desc = await asyncio.to_thread(_sina_rank_side_cached, 0)
    asc = await asyncio.to_thread(_sina_rank_side_cached, 1)
    if not desc and not asc:
        return None
    inflow = [r for r in sorted(desc, key=lambda x: x['mainNetInflow'], reverse=True)
              if r['mainNetInflow'] > 0][:top_n]
    outflow = [r for r in sorted(asc, key=lambda x: x['mainNetInflow'])
               if r['mainNetInflow'] < 0][:top_n]
    return {'inflow': inflow, 'outflow': outflow}


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


async def _sina_sector_rankings(top_n: int) -> Optional[Dict]:
    """板块排名兜底（2026-10-19）：东财板块 clist 被风控时的替代来源。

    新浪无板块资金流接口（industry_bill 等路径实测 Service not found），
    改用**本地行业分类聚合**：stock_basic_info.industry × 新浪全市场个股资金流快照。
    只覆盖行业板块；概念板块无本地分类，返回空列表由前端诚实降级提示。
    """
    rows = await _sina_market_snapshot()
    if not rows:
        return None
    # 行业分类：行里已经带了（`_sina_map_rows` 按本地表填过），没有再回查一次本地表。
    # 这里原来直接读 `stock_basic_info.industry` —— 那张表只有 759 行且行业列全空，
    # 于是聚合永远 return None，板块那一栏在东财被封期间一直是空的（10-19 实测）。
    ind_map: Dict[str, str] = {r['symbol']: r.get('industry') or ''
                               for r in rows if r.get('industry')}
    if not ind_map:
        ind_map = _sina_industry_map()
    if not ind_map:
        print('[资金流向] 板块兜底：本地行业分类为空，无法聚合（先跑 scripts/sync_stock_industry.py）')
        return None
    # 聚合：行业 -> 净流入合计 / 上涨家数 / 下跌家数 / 成员股涨幅累计
    # （chgSum/chgN 用于等权平均涨跌幅——旧实现把板块 changePercent 写死 0.0，
    #   2026-10-21 用户手机实拍"行业板块涨跌幅全 +0.00%"；成员股涨幅现成就有，没理由不算）
    agg: Dict[str, Dict] = {}
    for r in rows:
        ind = ind_map.get(r['symbol'])
        if not ind:
            continue
        a = agg.setdefault(ind, {'net': 0.0, 'rise': 0, 'fall': 0, 'chgSum': 0.0, 'chgN': 0})
        a['net'] += r['mainNetInflow']
        a['chgSum'] += r['changePercent']
        a['chgN'] += 1
        if r['changePercent'] > 0:
            a['rise'] += 1
        elif r['changePercent'] < 0:
            a['fall'] += 1
    if not agg:
        return None
    ordered = sorted(agg.items(), key=lambda kv: kv[1]['net'], reverse=True)

    def _avg_chg(a):
        return round(a['chgSum'] / a['chgN'], 2) if a['chgN'] else 0.0

    inflow = [{'name': name, 'changePercent': _avg_chg(a), 'mainNetInflow': round(a['net'], 2),
               'riseCount': a['rise'], 'fallCount': a['fall'], 'rank': i + 1,
               'chgCaliber': '成员股等权平均'}
              for i, (name, a) in enumerate(ordered) if a['net'] > 0][:top_n]
    outflow = [{'name': name, 'changePercent': _avg_chg(a), 'mainNetInflow': round(a['net'], 2),
                'riseCount': a['rise'], 'fallCount': a['fall'], 'rank': i + 1,
                'chgCaliber': '成员股等权平均'}
               for i, (name, a) in enumerate(reversed(ordered)) if a['net'] < 0][:top_n]
    return {'industry': {'inflow': inflow, 'outflow': outflow}, 'concept': {'inflow': [], 'outflow': []}}


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


def peek_sector_flow() -> Dict:
    """只读板块资金流（2026-10-22，供主线判定）：**零请求、绝不触发新浪全市场扫描**。

    mainline 在行动台热路径上跑，若走 get_fund_flow_summary 可能连带 12~18s 的
    sweep（9.2s 事故同类）。本函数只按 内存缓存 → _LAST_GOOD → DB 快照 的顺序读，
    读到什么算什么，返回 {rows, src, ts}；src='none' 时由调用方降级。
    口径判别：行里带 chgCaliber 的是新浪×本地行业聚合（国标行业名），否则是东财 f62。
    """
    for entry in _CACHE.values():
        sec = (entry.get('data') or {}).get('sectors') or {}
        if (sec.get('industry') or {}).get('inflow'):
            return {'rows': sec, 'src': 'cache', 'ts': float(entry.get('ts') or 0)}
    lg = _LAST_GOOD.get('sectors')
    if not lg:
        snap = load_fund_flow_snapshot('sectors')
        if snap and isinstance(snap, dict) and 'data' in snap:
            lg = snap
            _LAST_GOOD['sectors'] = lg
    if lg and lg.get('data'):
        return {'rows': lg['data'], 'src': 'lastgood', 'ts': float(lg.get('ts') or 0)}
    return {'rows': {}, 'src': 'none', 'ts': 0.0}


async def soft_refresh_sector_flow(top_n: int = FUND_FLOW_SECTOR_TOP_N) -> bool:
    """服务端板块保活（2026-10-22，为主线判定供数）：没人开资金流页时板块榜不会自己刷新，
    主线会一直"资金流缺失/超2小时"。由调度器在主线预热前触发，30 分钟节流：
    · clist 未熔断 → 2 次 clist（≈4 次/小时，远低于 10-19 那次 840 次/小时的封禁阈值）；
    · clist 熔断/失败 → 新浪×本地行业聚合（与资金流页 cooling 分支同一兜底；
      概念无本地分类仍空，行带 chgCaliber、mainline 按源内分位处理口径差异）。
    失败静默：mainline 会按 peek 的降级矩阵如实处理，绝不拿旧资金装新判定。"""
    pf = peek_sector_flow()
    if pf['src'] != 'none' and time.time() - pf['ts'] < 1800:
        return False
    s = None
    if not _clist_circuit_open():
        try:
            s = await get_sector_fund_flow_rankings(top_n)
        except Exception:
            s = None
    if not s or not (s.get('industry') or {}).get('inflow'):
        # sina 兜底。注意：不能用 _sina_sector_rankings 的默认路径——它内部是
        # "后台起算、本轮最多等 6s 返回 None"的设计（页面轮询场景下不卡响应），
        # 而调度器是 asyncio.run 的一次性循环，后台任务会随循环退出被杀 → 死循环空转。
        # 所以这里直接 await 扫描算完（~18s，调度器是独立线程，代价是本轮 tick 晚 18s，
        # 30 分钟一次可接受）；正有人算时让路，下个 tick 再来。
        if _SINA_COMPUTING:
            return False
        try:
            await _sina_compute_snapshot()
            s = await _sina_sector_rankings(top_n)
        except Exception:
            s = None
    if s and ((s.get('industry') or {}).get('inflow') or (s.get('concept') or {}).get('inflow')):
        now = time.time()
        _LAST_GOOD['sectors'] = {'data': s, 'ts': now}
        save_fund_flow_snapshot('sectors', {'data': s, 'ts': now})
        return True
    return False


async def get_fund_flow_summary(
    stock_top_n: int = FUND_FLOW_STOCK_TOP_N,
    sector_top_n: int = FUND_FLOW_SECTOR_TOP_N,
    change_threshold: float = FUND_FLOW_HIDDEN_CHANGE_THRESHOLD,
    hot_top_n: int = FUND_FLOW_HIDDEN_HOT_TOP_N,
    hidden_top_n: int = FUND_FLOW_HIDDEN_TOP_N,
) -> Dict:
    """汇总三块数据，各部分独立拉取互不拖垮；带内存缓存与「历史成功兜底」。

    2026-10-19 重构要点（东财 clist IP 级风控根治）：
      1. 请求量治理：个股 2→1 次、板块 4→2 次、hidden 300→100 条，配合前端轮询
         30s→120s，单页东财请求量降约 7 倍（风控主因是频率不是单次）。
      2. 快照 key 去参数化（stocks/sectors/hidden 恒存 top 100 原始行，展示截断），
         修复「测试参数刷新成功、默认参数读旧快照」的割裂。
      3. 板块排名新增兜底：新浪个股资金流 × 本地 stock_basic_info.industry 聚合
         （概念板块无本地分类，诚实返回空由前端提示）。
      4. 龄期诚实化：closed 分支用快照前查年龄，>2h 标 stale；updateTime 改为
         **数据真实时间**，新增 fetchTime（本次调用时间）——杜绝"旧数据装新"。

    返回结构：
    {
      stocks: {inflow:[...], outflow:[...]},
      sectors: {industry:{inflow,outflow}, concept:{inflow,outflow}},
      hiddenGems: [...],
      cooling, stale, dataSource, dataTs, updateTime, fetchTime, params
    }
    """
    key = _cache_key(stock_top_n, sector_top_n, change_threshold, hot_top_n, hidden_top_n)
    closed = not _in_trading_window()
    cached = _CACHE.get(key)
    now = time.time()
    # 盘中：短 TTL 缓存命中直接返回；收盘后：数据已定型，有缓存即返回
    if cached and (not closed or (now - cached['ts']) < FUND_FLOW_CACHE_TTL):
        return cached['data']

    # 兜底先就位（内存 → DB 快照恢复）；key 去参数化后只此三个 key
    def _load_lg(name: str) -> Optional[Dict]:
        lg = _LAST_GOOD.get(name)
        if not lg:
            snap = load_fund_flow_snapshot(name)
            if snap and isinstance(snap, dict) and 'data' in snap:
                lg = snap
                _LAST_GOOD[name] = lg
        return lg

    lg_stocks = _load_lg('stocks')
    lg_sectors = _load_lg('sectors')
    lg_hidden = _load_lg('hidden')

    def _lg_data(lg: Optional[Dict], default):
        if not lg:
            return default, 0.0
        return lg.get('data') or default, float(lg.get('ts') or 0)

    stocks: Dict[str, List[Dict]] = {'inflow': [], 'outflow': []}
    sectors: Dict = {}
    hidden: List[Dict] = []
    cooling = _clist_circuit_open()
    data_ts = 0.0          # 最旧那一块的数据真实时间（0=无数据）
    block_ts: Dict[str, float] = {}    # 区块 -> 所用数据的真实时间

    if cooling:
        print('[资金流向] clist 冷却中，本轮跳过东财、改走兜底/快照')

    def _fill_industry(rows, limit=None):
        """给缺行业的行按本地分类表补上（快照行、备源行都可能空）。

        行业列原来只有东财 f100 一个来源，被封时整列变"—"；本地 `stock_industry_map`
        （baostock 周更）补一遍，两源都不通也还有行业。
        """
        try:
            mp = _sina_industry_map()
            if not mp:
                return rows
            for r in rows or []:
                if isinstance(r, dict) and not r.get('industry'):
                    ind = mp.get(str(r.get('symbol') or '').lower())
                    if ind:
                        r['industry'] = ind
        except Exception:
            pass
        return rows

    def _mark(block: str, src: str, ts: float):
        """记下这一块用的数据到底是什么时间。

        sina / eastmoney 都是本轮现取 → 时间就是 now；只有 snapshot 可能是旧的，
        而且**旧多少要按块算**：10-19 实测出现过"个股已经是当天的、非热点那一块还是
        两天前的"，这时整页 stale 取最旧值，就会把一句"今日未刷新"扣在其实已经更新的
        主口径上 —— 用户只看到一个警示，分不清哪一块旧。所以 `stale` 只跟个股那块走，
        其余旧块用 `staleBlocks` 点名。
        """
        block_ts[block] = ts if (src == 'snapshot' and ts) else now

    # ---- 个股排名三级回退：东财（1 次）→ 新浪兜底 → 历史快照 ----
    src_stocks = 'none'
    if closed:
        d, ts = _lg_data(lg_stocks, {'inflow': [], 'outflow': []})
        has_snap = bool(d.get('inflow') or d.get('outflow'))
        # "收盘后就用快照"的前提是**这份快照是今天的**。10-19 东财 clist 被封一整天，
        # 页面从 08:51 起就一直停在旧数据上（用户截图看到的正是这个），而新浪排行
        # 只要 4 个请求就能拿到今天已经定型的数 —— 收盘后数据不再变，这次补算成本几乎为零。
        fresh_snap = has_snap and (now - ts) <= _STALE_AFTER_SEC
        # 注意这里**不看 cooling**：熔断关的是东财那条路，新浪排行不是东财。
        # 10-19 实测：加了 `and not cooling` 之后，"东财被封"反而变成"页面彻底不动"——
        # 冷却期越长（现在起步 30 分钟、指数退避到 4 小时）页面就越旧，正好把兜底废掉。
        if not fresh_snap:
            _fb = await _sina_stock_rankings(stock_top_n)
            if _fb and (_fb.get('inflow') or _fb.get('outflow')):
                stocks, src_stocks = _fb, 'sina'
                _LAST_GOOD['stocks'] = {'data': _fb, 'ts': now}
                save_fund_flow_snapshot('stocks', {'data': _fb, 'ts': now})
                print('[资金流向] 收盘后快照缺失/过期 → 用新浪排行补算当日排名（当日数据不再变）')
        if src_stocks == 'none' and has_snap:
            # 补算也没成（冷却中/新浪也不通）时，过期的快照仍然给 ——
            # 但 `_mark` 会把它的真实时间带进 dataTs，`stale` 随之亮起。
            # 返回空白页比返回旧页更糟：用户分不清"没数据"和"数据旧"。
            stocks, src_stocks = d, 'snapshot'
            _mark('stocks', src_stocks, ts)
    else:
        if not cooling:
            try:
                _r = await get_stock_fund_flow_rankings(stock_top_n)
                if _r.get('inflow') or _r.get('outflow'):
                    stocks, src_stocks = _r, 'eastmoney'
                    _clist_on_success()
                    _LAST_GOOD['stocks'] = {'data': _r, 'ts': now}
                    save_fund_flow_snapshot('stocks', {'data': _r, 'ts': now})
            except Exception as e:
                print(f'[资金流向] 个股排名拉取失败: {e}')
                _clist_on_failure(e)
        if src_stocks == 'none':
            _fb = await _sina_stock_rankings(stock_top_n)
            if _fb and (_fb.get('inflow') or _fb.get('outflow')):
                stocks, src_stocks = _fb, 'sina'
                print('[资金流向] 个股排名使用新浪兜底（东财不可用）')
                _LAST_GOOD['stocks'] = {'data': _fb, 'ts': now}
                save_fund_flow_snapshot('stocks', {'data': _fb, 'ts': now})
            else:
                d, ts = _lg_data(lg_stocks, {'inflow': [], 'outflow': []})
                if d.get('inflow') or d.get('outflow'):
                    stocks, src_stocks = d, 'snapshot'
                    print('[资金流向] 个股排名使用最后成功快照兜底')
                    _mark('stocks', src_stocks, ts)
    await asyncio.sleep(0.25)

    # ---- 板块排名三级回退：东财（2 次）→ 新浪+本地行业聚合 → 历史快照 ----
    src_sectors = 'none'
    if closed:
        d, ts = _lg_data(lg_sectors, {})
        has_snap = bool(d and ((d.get('industry') or {}).get('inflow')
                               or (d.get('concept') or {}).get('inflow')))
        ok_snap = has_snap and (now - ts) <= _STALE_AFTER_SEC
        if not ok_snap:
            _sb = await _sina_sector_rankings(sector_top_n)
            if _sb and (_sb.get('industry') or {}).get('inflow'):
                sectors, src_sectors = _sb, 'sina'
                _LAST_GOOD['sectors'] = {'data': _sb, 'ts': now}
                save_fund_flow_snapshot('sectors', {'data': _sb, 'ts': now})
                print('[资金流向] 收盘后板块快照缺失/过期 → 新浪×本地行业聚合补算'
                      '（概念板块无本地分类，仍缺）')
        if src_sectors == 'none' and has_snap:
            sectors, src_sectors = d, 'snapshot'
            _mark('sectors', src_sectors, ts)
    else:
        if not cooling:
            try:
                _s = await get_sector_fund_flow_rankings(sector_top_n)
                if _s.get('industry') or _s.get('concept'):
                    sectors, src_sectors = _s, 'eastmoney'
                    _clist_on_success()
                    _LAST_GOOD['sectors'] = {'data': _s, 'ts': now}
                    save_fund_flow_snapshot('sectors', {'data': _s, 'ts': now})
            except Exception as e:
                print(f'[资金流向] 板块排名拉取失败: {e}')
                _clist_on_failure(e)
        if src_sectors == 'none':
            _sb = await _sina_sector_rankings(sector_top_n)
            if _sb and (_sb.get('industry') or {}).get('inflow'):
                sectors, src_sectors = _sb, 'sina'
                print('[资金流向] 板块排名使用新浪+本地行业聚合兜底（东财不可用，概念板块暂缺）')
                _LAST_GOOD['sectors'] = {'data': _sb, 'ts': now}
                save_fund_flow_snapshot('sectors', {'data': _sb, 'ts': now})
            else:
                d, ts = _lg_data(lg_sectors, {})
                if d:
                    sectors, src_sectors = d, 'snapshot'
                    print('[资金流向] 板块排名使用最后成功快照兜底')
                    _mark('sectors', src_sectors, ts)
    await asyncio.sleep(0.25)

    # ---- 非热点上涨股三级回退：东财（1 次）→ 新浪快照派生 → 历史快照 ----
    src_hidden = 'none'
    if closed:
        d, ts = _lg_data(lg_hidden, [])
        fresh = bool(d) and (now - ts) <= _STALE_AFTER_SEC
        if not fresh:
            _hg = await _sina_hidden_gems(change_threshold, hot_top_n, hidden_top_n)
            if _hg:
                hidden, src_hidden = _hg, 'sina'
                _LAST_GOOD['hidden'] = {'data': _hg, 'ts': now}
                save_fund_flow_snapshot('hidden', {'data': _hg, 'ts': now})
                print('[资金流向] 收盘后非热点快照缺失/过期 → 新浪排行补算')
        if src_hidden == 'none' and d:
            hidden, src_hidden = d, 'snapshot'
            _mark('hidden', src_hidden, ts)
    else:
        if not cooling:
            try:
                _h = await get_hidden_gems(change_threshold, hot_top_n, hidden_top_n)
                if _h:
                    hidden, src_hidden = _h, 'eastmoney'
                    _clist_on_success()
                    _LAST_GOOD['hidden'] = {'data': _h, 'ts': now}
                    save_fund_flow_snapshot('hidden', {'data': _h, 'ts': now})
            except Exception as e:
                print(f'[资金流向] 非热点上涨股拉取失败: {e}')
                _clist_on_failure(e)
        if src_hidden == 'none':
            _hg = await _sina_hidden_gems(change_threshold, hot_top_n, hidden_top_n)
            if _hg:
                hidden, src_hidden = _hg, 'sina'
                print('[资金流向] 非热点上涨股使用新浪兜底（东财不可用）')
                _LAST_GOOD['hidden'] = {'data': _hg, 'ts': now}
                save_fund_flow_snapshot('hidden', {'data': _hg, 'ts': now})
            else:
                d, ts = _lg_data(lg_hidden, [])
                if d:
                    hidden, src_hidden = d, 'snapshot'
                    print('[资金流向] 非热点上涨股使用最后成功快照兜底')
                    _mark('hidden', src_hidden, ts)

    # 龄期判定：本轮现取的（sina/eastmoney）时间就是 now；只有快照可能是旧的。
    for _b, _src in (('stocks', src_stocks), ('sectors', src_sectors), ('hidden', src_hidden)):
        if _src in ('sina', 'eastmoney'):
            block_ts.setdefault(_b, now)
    stale_blocks = [b for b, t in block_ts.items() if t and (now - t) > _STALE_AFTER_SEC]
    # `stale` 只跟着**个股那一块**走 —— 它是这页的主口径；其余旧块由 staleBlocks 点名。
    # dataTs 取最旧那一块（"这页里最旧的东西是什么时候的"）。
    if block_ts:
        data_ts = min(block_ts.values())
    stale = (('stocks' in stale_blocks)
             if (stocks.get('inflow') or stocks.get('outflow')) else True)

    _fill_industry(stocks.get('inflow'))
    _fill_industry(stocks.get('outflow'))
    _fill_industry(hidden)

    data = {
        'stocks': stocks,
        'sectors': sectors,
        'hiddenGems': hidden,
        'cooling': cooling,
        # 只说"冷却中"没用，得说还剩多久 —— 否则用户只能反复点刷新碰运气
        'coolingRemainSec': int(_guard().status('clist').get('cooldownRemainSec') or 0)
                            if cooling else 0,
        'stale': stale,
        'staleBlocks': stale_blocks,        # 哪几块还是旧数据（前端按块说明，不一刀切）
        'blockAges': {b: int(now - t) for b, t in block_ts.items()},
        'dataSource': {'stocks': src_stocks, 'sectors': src_sectors, 'hidden': src_hidden},
        'dataTs': data_ts,
        'updateTime': datetime.fromtimestamp(data_ts).strftime('%Y-%m-%d %H:%M:%S') if data_ts else '--',
        'fetchTime': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
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
