"""services.stock.py - split from main.py"""
import json, re, asyncio, requests, time
from typing import List, Optional, Dict, Tuple
from datetime import datetime, date, timedelta
from app.config import (TENCENT_QUOTE_API, TENCENT_KLINE_API, SINA_QUOTE_API, EASTMONEY_HOT_LIST_API, EASTMONEY_ANN_API, HTTP_TIMEOUT, HTTP_CONNECT_TIMEOUT, DELISTING_KEYWORDS, FORBIDDEN_KEYWORDS, SECTOR_NAME_EXCLUDE_ENABLED, KLINE_START_DATE, KLINE_DATA_LIMIT, CHANGE_RANK_TOP_N, TURNOVER_RANK_TOP_N, TREND_ANNOUNCEMENT_CACHE_TTL, TREND_ANNOUNCEMENT_LOOKBACK_DAYS)
from services.data import source_health
from db.database import load_stock_basic_info, save_stock_basic_info, load_hot_search_ranking, load_stock_concept_tags

_stock_basic_info_cache = None
_hot_search_ranking_cache = None
_announcement_cache: Dict[str, Tuple[float, Dict]] = {}  # symbol -> (ts, result)
_analysis_cache: Dict[str, Tuple[float, Dict]] = {}  # symbol -> (ts, result), TTL=3s for 2s polling
# 2026-07-08 优化：行情 API 10 秒缓存（symbol -> (ts, raw_data)），避免重复 RTT
_realtime_cache: Dict[str, Tuple[float, str]] = {}
# 2026-10-23：同花顺热榜备源 120 秒缓存（东财人气榜为空时才用）
_ths_hot_cache: Dict[str, Tuple[float, list]] = {}

# 利空公告关键词（出现任一即判定为"重大利空 / 减持 / 立案"）
NEGATIVE_ANNOUNCEMENT_KEYWORDS = [
    '减持', '减持计划', '股份减持', '集中竞价减持', '大宗交易减持',
    '立案', '立案调查', '调查通知书', '调查公告', '被立案',
    '处罚', '行政处罚', '监管措施', '警示函', '监管函', '关注函',
    'ST', '*ST', '退市', '终止上市', '暂停上市', '风险警示',
    '业绩预亏', '首亏', '大幅下修', '商誉减值', '计提减值',
    '重大事项', '停牌', '重大资产重组失败', '终止重组', '解除重组',
    '违规', '被采取监管措施', '冻结', '轮候冻结', '诉讼', '仲裁',
    '债券违约', '债务逾期', '无法偿还', '资金占用', '违规担保',
    '高管变动', '董事长辞职', '总经理辞职', '实际控制人变更',
]

TENCENT_API = TENCENT_QUOTE_API
MAX_RETRIES = 2  # 2026-07-08 优化：3→2，腾讯偶尔超时1次重试足够，避免 3 RTT=1.5s
RETRY_BACKOFF_BASE = 0.1  # 2026-07-08 优化：0.5→0.1，减少无效等待
REALTIME_CACHE_TTL = 5  # 2026-07-08 优化：单只行情 5 秒缓存（腾讯 1-3 秒内数据变化小）

_DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate',
    'Referer': 'https://quote.eastmoney.com/',
}

# 2026-07-08 优化：全局 requests.Session 复用 TCP 连接（keep-alive）
# 避免每次请求都新建 TCP + TLS 握手（~100-200ms 开销）
# 注意：必须在 _DEFAULT_HEADERS 定义之后初始化
_REALTIME_SESSION = requests.Session()
_REALTIME_SESSION.headers.update(_DEFAULT_HEADERS)

# ========== 行情备源：腾讯 → 新浪（2026-10-19）==========
# 腾讯是主源；连续失败 3 次后 60 秒内直接走新浪，避免每次请求都先白等两轮腾讯超时。
# 新浪要求 Referer，否则 403；返回 GBK，字段格式与腾讯不同，需转换成腾讯 50 字段格式，
# 这样所有现有调用方（parse_tencent_batch_data / 前端 parseTencentData）零改动。
_SINA_HEADERS = {
    'User-Agent': _DEFAULT_HEADERS['User-Agent'],
    'Referer': 'https://finance.sina.com.cn/',
    'Accept': '*/*',
}
_SINA_SESSION = requests.Session()
_SINA_SESSION.headers.update(_SINA_HEADERS)

_TENCENT_DOWN_STREAK = 3        # 连续失败 3 次视为腾讯短期不可用
_TENCENT_COOLDOWN_SEC = 60.0    # 冷却期内优先新浪；到期后重试腾讯，避免永久旁路
_tencent_down_until = 0.0
_last_fallback_log = 0.0


# ========== 同步 HTTP 调用辅助（替代原 httpx.AsyncClient）==========
# 遵循 project_memory 硬约束：HTTP 请求到 Eastmoney/腾讯等服务必须用 requests 同步客户端
# 包装 with asyncio.to_thread，避免 httpx "Server disconnected" 错误。
def _sync_get(url: str, headers: Optional[Dict] = None, timeout: Optional[Tuple[float, float]] = None) -> requests.Response:
    """同步 GET。timeout=(connect, read)，默认 (HTTP_CONNECT_TIMEOUT, HTTP_TIMEOUT)。
    headers 会与 _DEFAULT_HEADERS 合并（调用方覆盖默认），避免丢失 User-Agent/Referer。
    """
    merged = {**_DEFAULT_HEADERS, **(headers or {})}
    return requests.get(url, headers=merged,
                        timeout=timeout or (HTTP_CONNECT_TIMEOUT, HTTP_TIMEOUT))

def _sync_post(url: str, json_body: Dict, headers: Optional[Dict] = None,
               timeout: float = 30.0) -> requests.Response:
    """同步 POST JSON（AI/DeepSeek 等接口用）。
    headers 会与 _DEFAULT_HEADERS 合并（调用方覆盖默认）。
    """
    merged = {**_DEFAULT_HEADERS, **(headers or {})}
    return requests.post(url, json=json_body, headers=merged, timeout=timeout)


def get_cached_stock_name(symbol: str) -> Optional[str]:
    global _stock_basic_info_cache, _hot_search_ranking_cache
    s = symbol.lower()
    if _stock_basic_info_cache is None:
        _stock_basic_info_cache = load_stock_basic_info()
    if s in _stock_basic_info_cache:
        return _stock_basic_info_cache[s]
    if _hot_search_ranking_cache is not None and _hot_search_ranking_cache.get('stocks'):
        for st in _hot_search_ranking_cache['stocks']:
            if st['symbol'].lower() == s:
                return st['name']
    return None

def is_forbidden_stock(name: str) -> bool:
    """行业名黑名单（默认已关，见 `SECTOR_NAME_EXCLUDE_ENABLED`）。

    2026-10-16：这条以前是无条件生效的按名字 substring 排除，实测在选票范围里只拦 24 只、
    却把 平安电工/洪都航空/出版传媒 这类无关票一起连坐了；而它真正想筛掉的银行/地产/白酒
    早就被"日均成交额>0.8亿 + 日均换手>1.5%"挤出范围。开关留着，默认关。
    退市/ST 风险是另一件事，走下面的 `has_delisting_risk`，不受影响。
    """
    if not SECTOR_NAME_EXCLUDE_ENABLED:
        return False
    return any(keyword in name for keyword in FORBIDDEN_KEYWORDS)

def has_delisting_risk(name: str, symbol: str) -> bool:
    if any(kw in name for kw in DELISTING_KEYWORDS):
        return True
    code = symbol.replace('sh', '').replace('sz', '')
    if code.startswith('4') or code.startswith('8') or code.startswith('9'):
        return True
    return False


def is_main_board(symbol: str) -> bool:
    """
    判断是否为沪深主板股票
    
    主板规则：
    - 沪市主板：sh600xxx, sh601xxx, sh603xxx, sh605xxx
    - 深市主板：sz000xxx, sz001xxx
    """
    symbol = symbol.lower()
    # 沪市主板：sh60开头
    if symbol.startswith('sh60'):
        return True
    # 深市主板：sz000或sz001开头
    if symbol.startswith('sz000') or symbol.startswith('sz001'):
        return True
    # 其他（创业板、科创板、北交所等）→ 不是主板
    return False
def _valid_symbols(symbols: str) -> List[str]:
    """过滤出合法的 sh/sz 六位代码，避免把参数原样拼进备源 URL。"""
    out = []
    for s in (symbols or '').split(','):
        s = s.strip().lower()
        if re.match(r'^(sh|sz)\d{6}$', s):
            out.append(s)
    return out


def _sina_num(fields: List[str], idx: int, default: float = 0.0) -> float:
    try:
        return float(fields[idx])
    except (IndexError, TypeError, ValueError):
        return default


def _sina_volume_lots(symbol: str, shares: float) -> float:
    """新浪成交量换算成腾讯的「手」。

    实测（2026-10-19）：上证指数 sh000* 的成交量已经是手；个股和深证指数 sz399*
    是股，需要 /100。弄反会让成交量放大/缩小 100 倍，均价和量比跟着全错。
    """
    return shares if symbol.startswith('sh000') else shares / 100.0


def _sina_quote_to_tencent(symbol: str, fields: List[str]) -> Optional[str]:
    """把新浪单行行情转换成腾讯 50 字段格式（双方字段位置不同，只填现有解析器用到的）。"""
    if len(fields) < 32:
        return None
    name = (fields[0] or '').strip()
    if not name:
        return None

    price = _sina_num(fields, 3)
    prev_close = _sina_num(fields, 2)
    open_price = _sina_num(fields, 1)
    high = _sina_num(fields, 4)
    low = _sina_num(fields, 5)
    if price <= 0 and prev_close > 0:
        # 停牌票新浪会给 price=0：直接算会变成 -100%，按平盘处理
        price = prev_close
        open_price = prev_close if open_price <= 0 else open_price
        high = prev_close if high <= 0 else high
        low = prev_close if low <= 0 else low
    volume_lots = _sina_volume_lots(symbol, _sina_num(fields, 8))
    amount = _sina_num(fields, 9)
    change = price - prev_close
    change_pct = (change / prev_close * 100.0) if prev_close else 0.0
    amplitude = ((high - low) / prev_close * 100.0) if prev_close else 0.0

    def _lots(idx: int) -> float:
        return _sina_volume_lots(symbol, _sina_num(fields, idx))

    out = [''] * 50
    # 第 0 位：腾讯原始是 "1"，这里写 "2" 作为「新浪兜底」标记，解析器会带出 quoteSource
    out[0] = '2'
    out[1] = name
    out[2] = symbol[2:]
    out[3] = f'{price:.2f}'
    out[4] = f'{prev_close:.2f}'
    out[5] = f'{open_price:.2f}'
    out[6] = f'{volume_lots:.0f}'
    out[7] = '0'          # 外盘：新浪不提供
    out[8] = '0'          # 内盘：新浪不提供
    # 五档买卖（新浪 10~29 = 买一量/价…卖五量/价；腾讯 9~28）
    pairs = [(9, 11, 10), (11, 13, 12), (13, 15, 14), (15, 17, 16), (17, 19, 18),
             (19, 21, 20), (21, 23, 22), (23, 25, 24), (25, 27, 26), (27, 29, 28)]
    for t_price, s_price, t_vol in pairs:
        out[t_price] = fields[s_price] if s_price < len(fields) else ''
        out[t_vol] = f'{_lots(s_price - 1):.0f}'
    out[31] = f'{change:.2f}'
    out[32] = f'{change_pct:.2f}'
    out[33] = f'{high:.2f}'
    out[34] = f'{low:.2f}'
    out[35] = f'{price:.2f}/{volume_lots:.0f}/{amount:.0f}'
    out[36] = f'{volume_lots:.0f}'
    out[37] = f'{amount / 10000.0:.0f}'   # 腾讯成交额单位：万元
    out[38] = '0'          # 换手率：新浪单行行情里没有
    out[39] = '0'          # PE：同上
    out[43] = f'{amplitude:.2f}'
    out[49] = '0'          # 量比：同上
    return f'v_{symbol}="{"~".join(out)}";'


def _sina_raw_to_tencent(raw: str) -> str:
    """新浪批量返回 → 腾讯格式多行字符串；空行/无效代码直接跳过。"""
    lines = []
    for line in (raw or '').splitlines():
        m = re.match(r'\s*var hq_str_([a-z]{2}\d{6})="(.*)";?\s*$', line)
        if not m:
            continue
        symbol, content = m.group(1), m.group(2)
        if not content.strip():
            continue
        converted = _sina_quote_to_tencent(symbol, content.split(','))
        if converted:
            lines.append(converted)
    return '\n'.join(lines)


def _fetch_sina_quotes(symbols: str) -> str:
    """新浪行情备源（同步，调用方用 asyncio.to_thread 包）。"""
    syms = _valid_symbols(symbols)
    if not syms:
        raise ValueError('无有效股票代码')
    out = []
    for i in range(0, len(syms), 120):
        chunk = syms[i:i + 120]
        url = SINA_QUOTE_API + ','.join(chunk)
        resp = _SINA_SESSION.get(url, timeout=(2, 3))
        resp.raise_for_status()
        raw = resp.content.decode('gbk', errors='ignore')
        converted = _sina_raw_to_tencent(raw)
        if converted:
            out.append(converted)
    if not out:
        raise ValueError('新浪行情返回为空/无法解析')
    return '\n'.join(out)


def detect_quote_source(raw: str) -> str:
    """从原始行情串判断来源：'2~' 开头是新浪兜底，其余按腾讯主源。"""
    m = re.search(r'v_[a-z]{2}\d{6}="(\d)~', raw or '')
    return 'sina' if (m and m.group(1) == '2') else 'tencent'


def quote_route_status() -> Dict:
    """当前行情路由策略（供诊断接口展示，不做网络请求）。"""
    remain = max(0.0, _tencent_down_until - time.time())
    return {
        'preferred': 'sina' if remain > 0 else 'tencent',
        'tencentDownStreak': source_health.status('quote_tencent')['streak'],
        'tencentCooldownRemainSec': int(remain),
        'cooldownAfterFailures': _TENCENT_DOWN_STREAK,
        'cooldownSec': int(_TENCENT_COOLDOWN_SEC),
    }


async def fetch_with_retry(symbols: str) -> str:
    """腾讯主源 + 新浪备源的行情入口（2026-10-19）。

    主路不变：腾讯 2 次重试（keep-alive + 5 秒缓存）。
    新增降级：腾讯连续失败 3 次后 60 秒内优先走新浪；否则腾讯最终失败时再走新浪。
    新浪返回会转换成腾讯 50 字段格式，所以所有现有解析器/前端都不用改；
    第 0 位写 "2"，解析结果里会带 quoteSource='sina'，方便页面标"新浪兜底"。
    """
    global _tencent_down_until, _last_fallback_log

    symbols_lower = (symbols or '').lower()
    # 2026-07-08 优化：5 秒内同 symbol 直接返回缓存
    now = time.time()
    cached = _realtime_cache.get(symbols_lower)
    if cached and (now - cached[0]) < REALTIME_CACHE_TTL:
        return cached[1]

    last_err: Optional[Exception] = None
    sina_err: Optional[Exception] = None
    syms = _valid_symbols(symbols)
    use_sina_first = bool(syms) and time.time() < _tencent_down_until

    # 腾讯刚被判为不可用：先试新浪，避免每轮都白等两轮腾讯超时
    if use_sina_first:
        t0 = time.time()
        try:
            data = await asyncio.to_thread(_fetch_sina_quotes, symbols)
            source_health.ok('quote_sina', (time.time() - t0) * 1000)
            _realtime_cache[symbols_lower] = (time.time(), data)
            return data
        except Exception as err:
            source_health.fail('quote_sina', err)
            sina_err = err
            # 新浪也不可用 → 继续试腾讯，不直接放弃

    url = TENCENT_API + symbols

    def _sync_fetch() -> str:
        # 2026-07-08 优化：用 _REALTIME_SESSION 复用 keep-alive（无 keep-alive 时自动新建，不影响正确性）
        resp = _REALTIME_SESSION.get(url, timeout=(1.5, 3))  # (connect, read)；2026-11-03 收紧 connect 防挂起拖垮调用方
        resp.raise_for_status()
        raw = resp.content
        if len(raw) < 10:
            raise Exception('返回数据过短')
        try:
            data = raw.decode('gbk')
        except Exception:
            data = raw.decode('utf-8', errors='ignore')
        if '=' not in data or '="\n' in data:
            raise Exception('API返回数据为空')
        return data

    t0 = time.time()
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            data = await asyncio.to_thread(_sync_fetch)
            # 2026-07-08 优化：写入缓存（5 秒 TTL）
            _realtime_cache[symbols_lower] = (time.time(), data)
            source_health.ok('quote_tencent', (time.time() - t0) * 1000)
            _tencent_down_until = 0.0
            return data
        except Exception as err:
            last_err = err
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF_BASE * attempt)

    source_health.fail('quote_tencent', last_err)
    if source_health.status('quote_tencent')['streak'] >= _TENCENT_DOWN_STREAK:
        _tencent_down_until = time.time() + _TENCENT_COOLDOWN_SEC

    # 腾讯最终失败 → 新浪兜底（本轮还没试过新浪才试）
    if syms and not use_sina_first:
        t0 = time.time()
        try:
            data = await asyncio.to_thread(_fetch_sina_quotes, symbols)
            source_health.ok('quote_sina', (time.time() - t0) * 1000)
            _realtime_cache[symbols_lower] = (time.time(), data)
            # 兜底日志限流：最多 60 秒一条，避免 5 秒轮询刷屏
            if time.time() - _last_fallback_log > 60:
                _last_fallback_log = time.time()
                print(f'[行情] 腾讯不可用，已切新浪兜底（{len(syms)} 只，'
                      f'{(time.time() - t0) * 1000:.0f}ms）')
            return data
        except Exception as err:
            source_health.fail('quote_sina', err)
            last_err = Exception(f'腾讯: {last_err}; 新浪: {err}')
    elif sina_err is not None:
        # 冷却期内先试新浪也失败：报错要把两边的错一起带上，别只留腾讯的
        last_err = Exception(f'新浪: {sina_err}; 腾讯: {last_err}')

    # 兜底：str(err) 为空时用 repr，否则 detail 看不到错误
    err_msg = str(last_err) or (repr(last_err) if last_err else '未知错误')
    raise Exception(f'fetch_with_retry 失败（腾讯/新浪均不可用）: {err_msg}')

async def fetch_stock_data(symbol: str) -> Optional[Dict]:
    try:
        raw_data = await fetch_with_retry(symbol)
        parsed_list = parse_tencent_batch_data(raw_data)
        if parsed_list and len(parsed_list) > 0:
            return {'raw': raw_data, 'parsed': parsed_list[0]}
        return None
    except Exception as e:
        print(f'获取股票 {symbol} 数据失败: {str(e)}')
        return None

async def fetch_eastmoney_hot_list():
    """
    获取东方财富沪深主板热股榜（人气榜 + 涨幅榜 + 成交额榜 3 类合并）。

    数据来源（3 个榜单合并）：
      1. 人气榜：东方财富真实个股人气榜 API（emappdata.eastmoney.com/stockrank/getAllCurrentList）
         — 返回全市场前 100 名（含创业板/科创板），本地筛选沪深主板
         — 需要 httpx 库（requests 会被服务器拒绝连接）
      2. 涨幅榜：clist API fid=f3（涨跌幅排序），取前 CHANGE_RANK_TOP_N 只沪深主板
      3. 成交额榜：clist API fid=f6（成交额排序），取前 TURNOVER_RANK_TOP_N 只沪深主板
    合并去重后，人气榜股票排前面（hotRank 从 1 开始），涨幅榜独有的排中间，成交额榜独有的排后面。
    """
    # 人气榜 API 端点（东方财富股吧真实人气榜）
    _POPULARITY_API = 'https://emappdata.eastmoney.com/stockrank/getAllCurrentList'

    def _fetch_clist(fid: str, fetch_count: int) -> list:
        """从东方财富 clist 接口拉取全 A 股（按 fid 排序），返回原始 diff 列表。

        2026-10-19：接入共享熔断 eastmoney_guard（东财 clist IP 级风控根治）——
        冷却中直接返回 []，热搜榜走降级路径，不再翻页重试硬打。
        """
        try:
            from services.data import eastmoney_guard
            if eastmoney_guard.skip('clist'):
                return []
        except Exception:
            pass
        url = EASTMONEY_HOT_LIST_API
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': '*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Referer': 'https://quote.eastmoney.com/',
            'Origin': 'https://quote.eastmoney.com',
        }
        all_stocks = []
        last_err = None          # 交给熔断器分级：连接被重置=被封，空数据=抖动，两者处置不同
        total_pages = (fetch_count + 99) // 100
        for pn in range(1, total_pages + 1):
            params = {
                'pn': pn, 'pz': 100, 'po': 1, 'np': 1, 'fltt': 1, 'invt': 2, 'fid': fid,
                'fs': 'm:0+t:2,m:1+t:2',
                'fields': 'f2,f3,f4,f6,f12,f13,f14'
            }
            try:
                r = requests.get(url, params=params, headers=headers, timeout=10)
                r.raise_for_status()
                data = r.json()
                if not data or not data.get('data') or not data['data'].get('diff'):
                    break
                page_stocks = data['data']['diff']
                all_stocks.extend(page_stocks)
                if len(page_stocks) < 100:
                    break
            except Exception as e:
                print(f'[热搜榜] clist 第{pn}页失败: {e}')
                last_err = e
                break
        if all_stocks:
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
        return all_stocks

    def _fetch_popularity_rank() -> list:
        """从东方财富真实人气榜 API 获取前 100 名，返回 [(code, market, rank), ...]。
        需要 httpx 库（requests 会被 emappdata 服务器拒绝连接）。

        2026-10-23：接入共享熔断，独立 scope 'appdata'。人气榜的 emappdata 与
        clist/ulist 的 push2delay 不是同一条链路（当天 clist 17/134 成功、
        appdata 16/16 成功），所以三件事要分开：① 只服从自己的 scope 冷却，
        不被 push2 的全域闸门连带停掉（`skip(ignore_domain=True)`），否则封禁期
        热搜榜只能空着；② 成功只复位本 scope，不关全域闸门，避免把 clist 提前
        放出去探测、给封禁续命；③ 失败按 flaky 只冷却本 scope，不打开全域闸门，
        避免人气榜的偶发问题误伤其他东财接口。
        """
        try:
            from services.data import eastmoney_guard
            if eastmoney_guard.skip('appdata', ignore_domain=True):
                return []
        except Exception:
            pass

        def _mark_fail(err=None) -> None:
            try:
                from services.data import eastmoney_guard
                # 固定 flaky：只累计本 scope，不触发全域闸门（见 docstring）
                eastmoney_guard.fail('appdata', err, kind='flaky')
            except Exception:
                pass

        try:
            import httpx
        except ImportError:
            print('[热搜榜] httpx 未安装，跳过人气榜')
            return []

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Content-Type': 'application/json',
            'Accept': 'application/json, text/plain, */*',
            'Origin': 'https://guba.eastmoney.com',
            'Referer': 'https://guba.eastmoney.com/rank/stock',
        }
        body = {'appId': 'appId01', 'pageNo': 1, 'pageSize': 100}
        try:
            with httpx.Client(verify=False, timeout=15, headers=headers) as client:
                r = client.post(_POPULARITY_API, json=body)
                r.raise_for_status()
                data = r.json()
                if not data or data.get('status') != 0 or not data.get('data'):
                    print(f'[热搜榜] 人气榜API返回异常: {data.get("message", "unknown")}')
                    _mark_fail()
                    return []
                result = []
                for item in data['data']:
                    sc = item.get('sc', '')  # e.g. "SH600248"
                    rk = item.get('rk', 0)
                    if len(sc) >= 4:
                        market = sc[:2].lower()  # "sh" or "sz"
                        code = sc[2:]
                        result.append((code, market, rk, ''))
                if not result:
                    _mark_fail()
                    return []
                try:
                    from services.data import eastmoney_guard
                    eastmoney_guard.ok('appdata', clear_domain=False)
                except Exception:
                    pass
                print(f'[热搜榜] 人气榜API返回 {len(result)} 只')
                return result
        except Exception as e:
            print(f'[热搜榜] 人气榜API请求失败: {e}')
            _mark_fail(e)
            return []

    def _fetch_ths_hot_rank() -> list:
        """同花顺热股榜（小时榜），东财人气榜不可用时的**备源**（2026-10-23）。

        为什么加：东财出口 IP 被封时，人气榜（appdata）也会被自己的冷却挡住，
        涨幅/成交额榜（clist）更常空，热搜榜就只剩空壳。同花顺这个榜单免登录、
        返回前 100 只（实测 85 只沪深主板），带名称/涨跌幅/名次，字段稳定。
        口径：与东财人气榜一致返回 [(code, market, rank, name), ...]，
        后续名称/价格仍走本地表 + 腾讯批量行情补齐；本函数只负责"有哪些票、排第几"。
        失败/为空返回 []，由调用方决定是否保留旧缓存；120 秒缓存避免调度重复打。
        """
        now = time.time()
        hit = _ths_hot_cache.get('hour')
        if hit and now - hit[0] < 120:
            return list(hit[1])
        url = 'https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock'
        params = {'stock_type': 'a', 'type': 'hour', 'list_type': 'normal'}
        headers = {
            'User-Agent': _DEFAULT_HEADERS['User-Agent'],
            'Referer': 'https://eq.10jqka.com.cn/',
        }
        try:
            r = requests.get(url, params=params, headers=headers, timeout=8)
            r.raise_for_status()
            payload = r.json()
            items = ((payload.get('data') or {}).get('stock_list') or [])
            out = []
            for item in items:
                code = str(item.get('code') or '')
                if not re.match(r'^\d{6}$', code):
                    continue
                market = 'sh' if code.startswith(('600', '601', '603', '605', '688')) else 'sz'
                try:
                    rank = int(item.get('order') or 0)
                except (TypeError, ValueError):
                    rank = 0
                out.append((code, market, rank, str(item.get('name') or '').strip()))
            if not out:
                print('[热搜榜] 同花顺热榜返回为空')
                return []
            _ths_hot_cache['hour'] = (now, out)
            print(f'[热搜榜] 同花顺热榜返回 {len(out)} 只（备用源）')
            return list(out)
        except Exception as e:
            print(f'[热搜榜] 同花顺热榜请求失败: {e}')
            return []

    def _fetch_batch_quotes(symbols: list) -> dict:
        """用 push2 ulist.np 批量获取股票行情（名称/价格/涨跌幅）。
        symbols: ['sh600248', 'sz000001', ...]
        返回: {'sh600248': {'name': '陕建股份', 'price': 3.01, 'changePercent': 2.38, 'change': 0.07}, ...}

        2026-10-19：接入共享熔断（独立 scope 'ulist'，与 clist 分开计数——
        push2delay 同域，clist 被封时 ulist 大概率同样命运，但分开计数避免误挡）。
        """
        if not symbols:
            return {}
        try:
            from services.data import eastmoney_guard
            if eastmoney_guard.skip('ulist'):
                return {}
        except Exception:
            pass
        # push2 secids 格式: "1.600248,0.000001" (1=沪市, 0=深市)
        secid_map = {'sh': '1', 'sz': '0'}
        secids = ','.join(f"{secid_map.get(s[:2], '1')}.{s[2:]}" for s in symbols)
        url = 'https://push2delay.eastmoney.com/api/qt/ulist.np/get'
        params = {
            'fltt': 2, 'np': 3, 'ut': 'a79f54e3d4c8d44e494efb8f748db291',
            'invt': 2, 'secids': secids,
            'fields': 'f2,f3,f4,f12,f13,f14',
        }
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://quote.eastmoney.com/',
        }
        result = {}
        try:
            r = requests.get(url, params=params, headers=headers, timeout=10)
            r.raise_for_status()
            data = r.json()
            if data and data.get('data') and data['data'].get('diff'):
                for stock in data['data']['diff']:
                    market = 'sh' if stock.get('f13') == 1 else 'sz'
                    code = stock.get('f12', '')
                    sym = market + code
                    def _to_float(v, d=0):
                        if v is None or v == '' or v == '-': return d
                        try: return float(v)
                        except: return d
                    result[sym] = {
                        'name': stock.get('f14', ''),
                        # 2026-10-23 修正：本请求用 fltt=2，f2/f3/f4 已是真实数值
                        # （元 / 百分比 / 元），不能再 /100。此前除错，人气榜独有票
                        # 价格被缩小 100 倍（华瓷股份实际 28.12 → 存成 0.28）。
                        # 封禁期涨幅/成交额榜为空时全部走这条路径，所以整榜都错。
                        'price': round(_to_float(stock.get('f2'), 0), 2),
                        'changePercent': round(_to_float(stock.get('f3'), 0), 2),
                        'change': round(_to_float(stock.get('f4'), 0), 2),
                    }
        except Exception as e:
            print(f'[热搜榜] 批量行情获取失败: {e}')
            try:
                from services.data import eastmoney_guard
                eastmoney_guard.fail('ulist')
            except Exception:
                pass
        if result:
            try:
                from services.data import eastmoney_guard
                eastmoney_guard.ok('ulist')
            except Exception:
                pass
        return result

    def _fetch_tencent_quotes_map(symbols: list) -> dict:
        """腾讯批量行情备源（2026-10-23）。

        背景：人气榜只给代码/名次，名称与价格要靠 push2 ulist 补；东财封禁期
        ulist 常被拒（21:03 实拍 ulist streak=1），热搜榜就会整榜价格 0、名称空，
        ST/退市过滤也会失真。腾讯行情不受东财封禁影响，一次可查几十只，
        所以只在东财行情缺失时补这一刀，正常行情下不触发、不增加请求。
        """
        out = {}
        syms = [s for s in symbols if re.match(r'^(sh|sz)\d{6}$', s or '')]
        for i in range(0, len(syms), 60):
            chunk = syms[i:i + 60]
            try:
                r = requests.get(TENCENT_API + ','.join(chunk), timeout=5,
                                 headers={'User-Agent': _DEFAULT_HEADERS['User-Agent'],
                                          'Referer': 'https://gu.qq.com/'})
                r.raise_for_status()
                r.encoding = 'gbk'
                for line in r.text.split(';'):
                    m = re.match(r'\s*v_(sh|sz)(\d{6})="([^"]*)"', line)
                    if not m:
                        continue
                    fields = m.group(3).split('~')
                    if len(fields) < 33:
                        continue
                    try:
                        price = float(fields[3] or 0)
                        change = float(fields[31] or 0)
                        pct = float(fields[32] or 0)
                    except ValueError:
                        continue
                    out[m.group(1) + m.group(2)] = {
                        'name': fields[1],
                        'price': round(price, 2),
                        'changePercent': round(pct, 2),
                        'change': round(change, 2),
                    }
            except Exception as e:
                print(f'[热搜榜] 腾讯行情补充失败: {e}')
        return out

    def _filter_mainboard(stocks: list, limit: int) -> list:
        """从原始 diff 列表中筛选沪深主板股票，返回前 limit 只。"""
        def _to_float(v, default=0):
            if v is None or v == '' or v == '-':
                return default
            try:
                return float(v)
            except (TypeError, ValueError):
                return default

        result = []
        for stock in stocks:
            market = 'sh' if stock.get('f13') == 1 else 'sz'
            code = stock.get('f12', '')
            name = stock.get('f14', '')

            is_shanghai_main = market == 'sh' and (
                code.startswith('600') or
                code.startswith('601') or
                code.startswith('603') or
                code.startswith('605')
            )
            is_shenzhen_main = market == 'sz' and (
                code.startswith('000') or
                code.startswith('001')
            )

            if is_shanghai_main or is_shenzhen_main:
                if not is_forbidden_stock(name):
                    raw_price = _to_float(stock.get('f2'), 0)
                    raw_change_pct = _to_float(stock.get('f3'), 0)
                    raw_change = _to_float(stock.get('f4'), 0)
                    result.append({
                        'symbol': market + code,
                        'code': code,
                        'name': name,
                        'market': market,
                        'price': round(raw_price / 100, 2),
                        'changePercent': round(raw_change_pct / 100, 2),
                        'change': round(raw_change / 100, 2),
                    })
                    if len(result) >= limit:
                        break
        return result

    # 本轮榜单来源（写入 hot_search_meta，便于页面/排查知道用了哪个源）
    source = {'popularity': ''}

    def _sync_fetch():
        try:
            print('[热搜榜] 正在获取东方财富人气榜 + 涨幅榜 + 成交额榜...')

            # 1. 人气榜（东财真实人气榜，最多 100 只全市场）
            popularity_data = _fetch_popularity_rank()
            source['popularity'] = 'eastmoney' if popularity_data else ''
            if not popularity_data:
                # 2026-10-23：东财被封/人气榜冷却时改用同花顺热榜（免登录、前 100 只）
                popularity_data = _fetch_ths_hot_rank()
                if popularity_data:
                    source['popularity'] = 'ths'
            # 人气榜返回的是全市场前100（含创业板/科创板），需要筛选沪深主板；
            # 用列表保序（rank 来自榜单顺序），集合只用于去重。
            popularity_syms = []
            popularity_codes = set()
            pop_names = {}
            for code, market, rk, pname in popularity_data:
                is_sh_main = market == 'sh' and (code.startswith('600') or code.startswith('601') or code.startswith('603') or code.startswith('605'))
                is_sz_main = market == 'sz' and (code.startswith('000') or code.startswith('001'))
                if is_sh_main or is_sz_main:
                    sym = market + code
                    if sym not in popularity_codes:
                        popularity_codes.add(sym)
                        popularity_syms.append(sym)
                    if pname:
                        pop_names[sym] = pname
            print(f'[热搜榜] 人气榜沪深主板: {len(popularity_syms)} 只'
                  f'（来源 {source["popularity"] or "无"}）')

            # 2. 涨幅榜（clist fid=f3）
            change_fetch = int(CHANGE_RANK_TOP_N * 1.6)
            change_raw = _fetch_clist('f3', change_fetch)
            change_list = _filter_mainboard(change_raw, CHANGE_RANK_TOP_N)
            print(f'[热搜榜] 涨幅榜获取 {len(change_list)} 只')

            # 3. 成交额榜（clist fid=f6）
            time.sleep(0.2)
            turnover_fetch = int(TURNOVER_RANK_TOP_N * 1.6)
            turnover_raw = _fetch_clist('f6', turnover_fetch)
            turnover_list = _filter_mainboard(turnover_raw, TURNOVER_RANK_TOP_N)
            print(f'[热搜榜] 成交额榜获取 {len(turnover_list)} 只')

            # 合并：人气榜在前 → 涨幅榜 → 成交额榜
            seen = set()
            merged = []
            rank = 1

            # 3a. 人气榜股票排最前（需要从涨幅/成交额列表中找到对应的行情数据）
            # 先建立 code → stock 的映射
            all_stocks_map = {}
            for s in change_list + turnover_list:
                all_stocks_map[s['symbol']] = s

            # 3a-1. 找出人气榜中不在涨幅/成交额榜的股票，通过 push2 批量行情API补充名称和行情
            missing_syms = [sym for sym in popularity_codes if sym not in all_stocks_map]
            # 人气榜只给代码/名次；push2 ulist 被熔断时名称为空，ST/退市过滤会失效。
            # 用本地基础信息补名称（零外部请求），补不到才留空。
            local_names = {}
            if any(not (all_stocks_map.get(sym) or {}).get('name') for sym in popularity_codes):
                try:
                    local_names = load_stock_basic_info() or {}
                except Exception as _e:
                    print(f'[热搜榜] 本地名称补充失败(忽略): {_e}')
            if missing_syms:
                print(f'[热搜榜] 人气榜独有 {len(missing_syms)} 只，批量补充行情...')
                # push2 ulist.np 最多支持约 100 只批量查询，分批处理
                for i in range(0, len(missing_syms), 80):
                    batch = missing_syms[i:i+80]
                    quotes = _fetch_batch_quotes(batch)
                    for sym in batch:
                        code = sym[2:]
                        market = sym[:2]
                        q = quotes.get(sym, {})
                        s = {
                            'symbol': sym, 'code': code,
                            # 同花顺备源自带名称；东财 ulist 被跳过/失败时不能把它丢了
                            'name': q.get('name', '') or pop_names.get(sym, ''),
                            'market': market,
                            'price': q.get('price', 0),
                            'changePercent': q.get('changePercent', 0),
                            'change': q.get('change', 0),
                        }
                        all_stocks_map[sym] = s
                    if i + 80 < len(missing_syms):
                        time.sleep(0.1)

            for sym in popularity_syms:
                if sym not in seen:
                    seen.add(sym)
                    if sym in all_stocks_map:
                        s = all_stocks_map[sym].copy()
                    else:
                        # 人气榜有但涨幅/成交额榜没有的股票，用人气榜基本信息构造
                        code = sym[2:]
                        market = sym[:2]
                        s = {'symbol': sym, 'code': code, 'name': pop_names.get(sym, ''), 'market': market, 'price': 0, 'changePercent': 0, 'change': 0}
                    s['hotRank'] = rank
                    merged.append(s)
                    rank += 1

            # 3b. 涨幅榜独有的
            for s in change_list:
                sym = s['symbol']
                if sym not in seen:
                    seen.add(sym)
                    s['hotRank'] = rank
                    merged.append(s)
                    rank += 1

            # 3c. 成交额榜独有的
            for s in turnover_list:
                sym = s['symbol']
                if sym not in seen:
                    seen.add(sym)
                    s['hotRank'] = rank
                    merged.append(s)
                    rank += 1

            # 名称兜底：行情源被熔断时用本地基础信息补，避免空名导致 ST/退市过滤失真
            if local_names:
                for s in merged:
                    if not s.get('name'):
                        s['name'] = local_names.get(s.get('symbol', ''), '')

            # 行情兜底：ulist 被熔断时价格为 0，用腾讯批量行情补（一次几十只，零东财请求）
            need_quote = [s['symbol'] for s in merged if not s.get('price')]
            if need_quote:
                tq = _fetch_tencent_quotes_map(need_quote)
                filled = 0
                for s in merged:
                    q = tq.get(s.get('symbol', ''))
                    if not q:
                        continue
                    if not s.get('name'):
                        s['name'] = q.get('name', '')
                    if not s.get('price'):
                        s['price'] = q.get('price', 0)
                        s['changePercent'] = q.get('changePercent', 0)
                        s['change'] = q.get('change', 0)
                        filled += 1
                print(f'[热搜榜] 腾讯行情补齐 {filled}/{len(need_quote)} 只')

            print(f'[热搜榜] 合并去重后共 {len(merged)} 只沪深主板股票')
            return merged
        except Exception as e:
            print(f'[热搜榜] 同步请求失败: {e}')
            raise

    try:
        result = await asyncio.to_thread(_sync_fetch)

        return {
            'updateTime': datetime.now().isoformat(),
            'total': len(result),
            'filteredCount': len(result),
            'source': source['popularity'],
            'stocks': result
        }

    except Exception as error:
        print(f'[热搜榜] 获取失败: {str(error)}')
        raise error
def split_batch_raw(raw: str) -> Dict[str, str]:
    """把批量行情原始串按单码拆开：{'sh600000': 'v_sh600000="..."', ...}

    2026-10-24（性能）：趋势扫描已为板块动量把整池 100 只/批拉过一遍行情，
    但结果只用了涨跌幅就丢弃，process_one_stock 又逐只单拉 ~1300 次腾讯请求
    （_realtime_cache 按整串 key 缓存，单码 key 命不中）。拆串后单码原始串
    可直接作为 realtime_raw 传参复用，格式与单码请求完全同构（v_XX="~分隔"），
    parse_realtime_fields / parse_tencent_batch_data 均可直接消费单段。
    """
    out: Dict[str, str] = {}
    if not raw:
        return out
    for piece in re.split(r'[\n;]+', raw.strip()):
        piece = piece.strip()
        if piece.startswith('v_') and '=' in piece:
            eq_idx = piece.index('=')
            out[piece[2:eq_idx].lower()] = piece
    return out

def parse_realtime_fields(data: str) -> Optional[Dict]:
    try:
        lines = data.split(';')
        for line in lines:
            if line.startswith('v_') and '=' in line:
                content = line[line.index('"') + 1 : line.rfind('"')]
                fields = content.split('~')
                if len(fields) > 40:
                    return {
                        'name': fields[1],
                        'price': float(fields[3]) if fields[3] else 0,
                        'yesterdayClose': float(fields[4]) if fields[4] else 0,
                        'open': float(fields[5]) if fields[5] else 0,
                        'high': float(fields[33]) if fields[33] else 0,
                        'low': float(fields[34]) if fields[34] else 0,
                        'volume': float(fields[36]) if fields[36] else 0,
                        'quoteSource': 'sina' if fields[0] == '2' else 'tencent',
                    }
    except Exception as e:
        print(f'[解析实时数据] 失败: {str(e)}')
    return None

def calculate_ma(data: List[List], period: int) -> List[Optional[float]]:
    result = []
    for i in range(len(data)):
        if i < period - 1:
            result.append(None)
            continue
        total = 0.0
        for j in range(period):
            total += float(data[i - j][2])
        result.append(round(total / period, 3))
    return result
def get_full_name_from_hot_search_ranking(symbol: str) -> Optional[str]:
    try:
        hot_search_ranking = load_hot_search_ranking()
        if hot_search_ranking.get('stocks'):
            for s in hot_search_ranking['stocks']:
                if s['symbol'].lower() == symbol.lower():
                    return s['name']
    except:
        pass
    return None

async def get_stock_name(symbol: str) -> Optional[str]:
    global _stock_basic_info_cache
    s = symbol.lower()
    
    cached = get_cached_stock_name(s)
    if cached:
        return cached
    
    try:
        data = await fetch_with_retry(s)
        match = re.search(r'v_\w+="([^"]+)"', data)
        if match:
            name = match.group(1).split('~')[1] if len(match.group(1).split('~')) > 1 else s
            
            hot_search_ranking_name = get_full_name_from_hot_search_ranking(s)
            if hot_search_ranking_name and len(hot_search_ranking_name) > len(name):
                name = hot_search_ranking_name
            
            if is_forbidden_stock(name):
                return None
            
            if name != s:
                if _stock_basic_info_cache is None:
                    _stock_basic_info_cache = load_stock_basic_info()
                _stock_basic_info_cache[s] = name
                save_stock_basic_info(_stock_basic_info_cache)
            
            return name
    except:
        pass
    return symbol

async def get_kline_data(symbol: str, return_realtime: bool = False):
    """获取股票K线数据（-fix: 改用 requests + asyncio.to_thread，去 httpx）

    性能优化（2026-07-20 A1）：日线历史按交易日缓存到 SQLite（kline_cache），
    当天只拉一次腾讯；实时行情复用 _realtime_cache（5 秒 TTL），避免每次重拉全量历史。

    Args:
        return_realtime: 若为 True，返回 (kline, realtime_raw) 元组，
                         realtime_raw 为内部已拉的实时行情原始字符串，供调用方复用避免重复请求
    """
    today = date.today().isoformat()
    start_date = KLINE_START_DATE
    url = f'{TENCENT_KLINE_API}?param={symbol},day,{start_date},{today},{KLINE_DATA_LIMIT},qfq'

    # 2026-07-20 A1：优先读 SQLite 缓存（当天已拉过则直接命中，省一次腾讯请求）
    from db.database import load_kline_cache, save_kline_cache
    cached_kline = load_kline_cache(symbol, today)
    kline = cached_kline if cached_kline else []

    try:
        if not kline:
            try:
                def _fetch_kline():
                    resp = _sync_get(url)
                    resp.raise_for_status()
                    return resp.json()
                data = await asyncio.to_thread(_fetch_kline)
                if data.get('data') and data['data'].get(symbol):
                    kline = data['data'][symbol].get('qfqday', []) or data['data'][symbol].get('day', [])
                    # 成功拉到足量数据才写缓存，避免脏数据污染
                    if kline and len(kline) >= 30:
                        save_kline_cache(symbol, today, kline)
            except Exception as e:
                print(f'[K线API请求] {str(e)}')

        if not kline or len(kline) < 30:
            return (None, None) if return_realtime else None

        realtime_raw = None
        try:
            # 2026-07-20 A1：复用实时行情缓存（_realtime_cache 5秒 TTL），避免重复 RTT
            realtime_raw = await fetch_with_retry(symbol)
            if realtime_raw:
                fields = parse_realtime_fields(realtime_raw)
                if fields and fields['price']:
                    latest_kline = kline[-1] if kline else None
                    latest_date = latest_kline[0] if latest_kline else ''

                    open_val = fields['open'] or fields['price']
                    price_val = fields['price']
                    high_val = fields['high'] or fields['price']
                    low_val = fields['low'] or fields['price']
                    vol_val = fields['volume'] or 0

                    if latest_date != today:
                        kline.append([today, open_val, price_val, high_val, low_val, vol_val])
                    else:
                        latest_kline[2] = price_val
                        latest_kline[3] = max(float(latest_kline[3]), float(high_val))
                        latest_kline[4] = min(float(latest_kline[4]), float(low_val))
        except Exception as e:
            pass

        if return_realtime:
            return (kline, realtime_raw)
        return kline
    except Exception as e:
        print(f'[K线获取失败] {symbol}: {str(e)}')
        return (None, None) if return_realtime else None
def parse_tencent_batch_data(data: str) -> List[Dict]:
    results = []
    lines = re.split(r'[\n;]+', data.strip())
    
    for line in lines:
        trimmed = line.strip()
        if not trimmed.startswith('v_') or '=' not in trimmed:
            continue
        
        eq_idx = trimmed.index('=')
        symbol = trimmed[2:eq_idx]
        content = trimmed[eq_idx + 1:].strip('"')
        fields = content.split('~')
        
        if len(fields) <= 49:
            continue
        
        price = float(fields[3]) if fields[3] else 0
        prev_close = float(fields[4]) if fields[4] else 0
        change_val = float(fields[31]) if fields[31] else 0
        change_pct = float(fields[32]) if fields[32] else 0
        day_high = float(fields[33]) if fields[33] else 0
        day_low = float(fields[34]) if fields[34] else 0
        outer_plate = int(fields[7]) if fields[7] else 0
        inner_plate = int(fields[8]) if fields[8] else 0
        volume_raw = int(fields[36]) if fields[36] else 0
        turnover = float(fields[37]) if fields[37] else 0
        turnover_rate = float(fields[38]) if fields[38] else 0
        volume_ratio = float(fields[49]) if fields[49] else 0
        amplitude = float(fields[43]) if fields[43] else 0
        pe = float(fields[39]) if fields[39] else 0
        bid1_vol = int(fields[12]) if fields[12] else 0
        ask1_vol = int(fields[22]) if fields[22] else 0
        
        weibi = (((bid1_vol - ask1_vol) / (bid1_vol + ask1_vol)) * 100) if (bid1_vol + ask1_vol) > 0 else 0
        weibi = round(weibi, 2)
        
        avg_price = ((turnover * 10000) / (volume_raw * 100)) if volume_raw > 0 else price
        avg_price = round(avg_price, 2)
        
        avg_price_deviation = (((price - avg_price) / avg_price) * 100) if avg_price > 0 else 0
        avg_price_deviation = round(avg_price_deviation, 2)
        
        total_plate = outer_plate + inner_plate
        outer_ratio = ((outer_plate / total_plate) * 100) if total_plate > 0 else 50
        outer_ratio = round(outer_ratio, 1)
        
        results.append({
            'name': fields[1],
            'symbol': symbol,
            'code': fields[2],
            'quoteSource': 'sina' if fields[0] == '2' else 'tencent',
            'price': fields[3],
            'priceRaw': price,
            'prevClose': fields[4],
            'prevCloseRaw': prev_close,
            'high': fields[33],
            'highRaw': day_high,
            'low': fields[34],
            'lowRaw': day_low,
            'change': change_val,
            'changePercent': change_pct,
            'isUp': change_val > 0,
            'open': fields[5],
            'outerPlateRaw': outer_plate,
            'innerPlateRaw': inner_plate,
            'volumeRaw': volume_raw,
            'turnover': turnover,
            'turnoverRate': turnover_rate,
            'volumeRatio': volume_ratio,
            'amplitude': amplitude,
            'pe': pe,
            'weibi': weibi,
            'avgPrice': avg_price,
            'avgPriceDeviation': avg_price_deviation,
            'outerRatio': outer_ratio
        })
    
    return results
async def fetch_stock_concepts(symbol: str, name: str) -> Dict[str, any]:
    """尝试从网络获取股票相关概念标签（模拟实现）"""
    # 这里可以对接真实的财经数据API，比如东方财富、同花顺等
    # 现在实现一个基于常见概念的推理逻辑
    
    try:
        # 首先检查是否已有标签
        existing_tags = load_stock_concept_tags().get(symbol.lower(), [])
        
        # 基于股票名称和常见概念的推理
        concepts = []
        name_lower = name.lower() if name else ''
        
        # 常见概念关键词
        keyword_map = {
            '科技': ['科技', '电子', '通信', '信息'],
            '新能源': ['新能', '光伏', '风电', '锂电', '储能', '氢能', '绿色'],
            '半导体': ['芯片', '半导体', '集成电路', 'IC', '封测'],
            '人工智能': ['AI', '人工', '智能', '机器人', '机器视觉'],
            '华为概念': ['华为', '鸿蒙', '鲲鹏'],
            '汽车': ['汽车', '车', '特斯拉', '比亚迪'],
            '消费电子': ['消费', '电子', '智能穿戴'],
            '5G': ['5G', '通信', '光模块', '光纤'],
            '军工': ['军工', '航天', '航空', '武器'],
            '医疗': ['医疗', '医药', '生物', '健康'],
            '农业': ['农业', '乡村', '种植'],
            '金融': ['银行', '证券', '保险', '金融'],
            '房地产': ['地产', '房产', '保利', '万科']
        }
        
        # 检查股票名称中的关键词
        for concept, keywords in keyword_map.items():
            if any(keyword in name_lower for keyword in keywords):
                concepts.append(concept)
        
        # 如果没有找到，添加一些通用标签
        if not concepts:
            concepts.extend(['待分析', '关注'])
        
        return {
            'success': True,
            'symbol': symbol,
            'name': name,
            'concepts': concepts,
            'existing_tags': existing_tags,
            'suggestions': concepts  # 建议的标签
        }
    except Exception as e:
        print(f'获取股票概念失败: {str(e)}')
        return {
            'success': False,
            'symbol': symbol,
            'name': name,
            'concepts': [],
            'error': str(e)
        }


# ========== 公告/利空检测（东方财富公告接口） ==========

def _strip_eastmoney_jsonp(raw: str) -> str:
    """东方财富接口多数走 JSONP，去掉包裹函数（也可能直接返回 JSON）"""
    if not raw:
        return raw
    raw = raw.strip()
    m = re.match(r'^\s*\w+\((.*)\)\s*;?\s*$', raw, re.DOTALL)
    if m:
        return m.group(1)
    return raw


async def fetch_stock_announcements(symbol: str, lookback_days: int = None) -> List[Dict]:
    """
    拉取个股近 N 天公告。返回 list，每项 { title, art_code, notice_date, columns_name }。
    含内存缓存（TREND_ANNOUNCEMENT_CACHE_TTL 秒）。
    接口限流友好：失败时返回空列表，不抛异常（避免趋势扫描中断）。
    """
    if lookback_days is None:
        lookback_days = TREND_ANNOUNCEMENT_LOOKBACK_DAYS
    sym = symbol.lower()

    # 缓存
    cached = _announcement_cache.get(sym)
    if cached and (time.time() - cached[0]) < TREND_ANNOUNCEMENT_CACHE_TTL:
        return cached[1].get('announcements', [])

    def _sync_fetch() -> List[Dict]:
        # 2026-10-20：接入共享熔断 eastmoney_guard（scope 'ann'）。趋势扫描逐票调这里，
        # 东财被封当天曾白打几百次（今日 ann 378 次/115 失败的来源之一）。冷却期内
        # 静默返回空列表——本函数契约本来就是"失败返回空、不抛异常"，调用方无感。
        from services.data import eastmoney_guard
        if eastmoney_guard.skip('ann'):
            return []
        try:
            cutoff = date.today() - timedelta(days=lookback_days)
            # 东方财富公告 API：ann_type=A 全部公告，page_size=50
            url = EASTMONEY_ANN_API
            params = {
                'cb': '',
                'page_size': 50,
                'page_index': 1,
                'ann_type': 'A',
                'client_source': 'web',
                'stock_list': sym,
                'f_node': 0,
                's_node': 0,
            }
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Referer': 'https://data.eastmoney.com/',
                'Accept': '*/*',
            }
            r = requests.get(url, params=params, headers=headers, timeout=8)
            r.raise_for_status()
            # 解析 JSON
            try:
                data = r.json()
            except Exception:
                data = json.loads(_strip_eastmoney_jsonp(r.text))
            # 接口返回结构：data.list = [{ art_code, title, notice_date, columns_name, ... }]
            eastmoney_guard.ok('ann')
            items = []
            for grp in (data.get('data') or {}).get('list', []) or []:
                # list 可能是嵌套的二级 list
                if isinstance(grp, list):
                    for it in grp:
                        items.append(it)
                else:
                    items.append(grp)
            out = []
            for it in items:
                title = (it.get('title') or '').replace('<em>', '').replace('</em>', '')
                notice_date = (it.get('notice_date') or '')[:10]
                if not title or not notice_date:
                    continue
                try:
                    if date.fromisoformat(notice_date) < cutoff:
                        continue
                except Exception:
                    continue
                out.append({
                    'title': title,
                    'art_code': it.get('art_code') or '',
                    'notice_date': notice_date,
                    'columns_name': it.get('columns_name') or '',
                })
            return out
        except Exception as e:
            eastmoney_guard.fail('ann', e)
            print(f'[公告] 拉取 {symbol} 公告失败: {e}')
            return []

    try:
        anns = await asyncio.to_thread(_sync_fetch)
    except Exception as e:
        print(f'[公告] 异步拉取 {symbol} 公告失败: {e}')
        anns = []

    _announcement_cache[sym] = (time.time(), {'announcements': anns})
    return anns


def check_negative_announcement(announcements: List[Dict]) -> Dict:
    """
    检查公告列表中是否含利空关键词。
    返回: { hasNegative: bool, hits: [str] }
    """
    if not announcements:
        return {'hasNegative': False, 'hits': []}
    hits = []
    for ann in announcements:
        title = ann.get('title', '') or ''
        # 标题命中关键词 → 记录
        for kw in NEGATIVE_ANNOUNCEMENT_KEYWORDS:
            if kw in title:
                hits.append(f'[{ann.get("notice_date", "")}] {title}（命中:{kw}）')
                break  # 一条公告只记一次
    return {'hasNegative': bool(hits), 'hits': hits}


async def has_negative_announcement(symbol: str) -> Dict:
    """
    综合：拉公告 + 关键词检测。
    """
    anns = await fetch_stock_announcements(symbol)
    return check_negative_announcement(anns)
