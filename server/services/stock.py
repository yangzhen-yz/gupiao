"""services.stock.py - split from main.py"""
import httpx, json, re, asyncio, requests, time
from typing import List, Optional, Dict, Tuple
from datetime import datetime, date, timedelta
from app.config import TENCENT_QUOTE_API, TENCENT_KLINE_API, EASTMONEY_HOT_LIST_API, EASTMONEY_ANN_API, HTTP_TIMEOUT, HTTP_CONNECT_TIMEOUT, HTTP_MAX_CONNECTIONS, HTTP_KEEPALIVE_CONNECTIONS, DELISTING_KEYWORDS, FORBIDDEN_KEYWORDS, KLINE_START_DATE, KLINE_DATA_LIMIT, HOT_SEARCH_TOP_N, INDEX_SYMBOLS, TREND_ANNOUNCEMENT_CACHE_TTL, TREND_ANNOUNCEMENT_LOOKBACK_DAYS
from db.database import load_stock_basic_info, save_stock_basic_info, load_hot_search_ranking, load_stock_concept_tags

_stock_basic_info_cache = None
_hot_search_ranking_cache = None
_announcement_cache: Dict[str, Tuple[float, Dict]] = {}  # symbol -> (ts, result)
http_client = None

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
MAX_RETRIES = 3

_DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate',
    'Referer': 'https://quote.eastmoney.com/',
}


def get_http_client() -> httpx.AsyncClient:
    global http_client
    if http_client is None or http_client.is_closed:
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(HTTP_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT),
            limits=httpx.Limits(max_connections=HTTP_MAX_CONNECTIONS, max_keepalive_connections=HTTP_KEEPALIVE_CONNECTIONS),
            http2=False,
            follow_redirects=True,
            headers=_DEFAULT_HEADERS,
        )
    return http_client

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
async def fetch_with_retry(symbols: str, client: Optional[httpx.AsyncClient] = None) -> str:
    url = TENCENT_API + symbols
    last_err = None
    cl = client or get_http_client()
    
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = await cl.get(url, headers={'Accept-Encoding': 'gzip'})
            response.raise_for_status()
            
            raw = response.content
            if len(raw) < 10:
                raise Exception('返回数据过短')
            
            try:
                data = raw.decode('gbk')
            except:
                data = raw.decode('utf-8', errors='ignore')
            
            if '=' not in data or '="\n' in data:
                raise Exception('API返回数据为空')
            
            return data
        except Exception as err:
            last_err = err
            if attempt < MAX_RETRIES:
                await asyncio.sleep(0.5 * attempt)
    
    raise last_err

async def fetch_stock_data(symbol: str, client: Optional[httpx.AsyncClient] = None) -> Optional[Dict]:
    try:
        raw_data = await fetch_with_retry(symbol, client)
        parsed_list = parse_tencent_batch_data(raw_data)
        if parsed_list and len(parsed_list) > 0:
            return {'raw': raw_data, 'parsed': parsed_list[0]}
        return None
    except Exception as e:
        print(f'获取股票 {symbol} 数据失败: {str(e)}')
        return None

async def fetch_eastmoney_hot_list():
    """
    获取东方财富沪深主板热股榜。
    改用 requests 同步请求：服务器环境下 httpx 访问 push2delay.eastmoney.com
    会触发 RemoteProtocolError (Server disconnected without sending a response)，
    而 requests 库能正常返回。
    """
    def _sync_fetch():
        try:
            print('[热搜榜] 正在获取东方财富热搜榜数据...')
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
            # 一次最多 100，分多页取（按涨幅倒序）
            for pn in (1, 2, 3):
                params = {
                    'pn': pn, 'pz': 100, 'po': 1, 'np': 1, 'fltt': 1, 'invt': 2, 'fid': 'f3',
                    'fs': 'm:0+t:2,m:1+t:2',
                    'fields': 'f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,f20,f21,f23,f24,f25,f26,f22,f33,f11,f62,f128,f136,f115,f152'
                }
                r = requests.get(url, params=params, headers=headers, timeout=10)
                r.raise_for_status()
                data = r.json()
                if not data or not data.get('data') or not data['data'].get('diff'):
                    break
                page_stocks = data['data']['diff']
                all_stocks.extend(page_stocks)
                if len(page_stocks) < 100:
                    break
            return all_stocks
        except Exception as e:
            print(f'[热搜榜] 同步请求失败: {e}')
            raise

    try:
        stocks = await asyncio.to_thread(_sync_fetch)
        result = []
        
        for idx, stock in enumerate(stocks):
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
                    # 东方财富 f2/f3/f4 字段都是"放大 100 倍"的值（f2 单位是"分"，f3/f4 单位是"%"*100）
                    raw_price = stock.get('f2', 0) or 0
                    raw_change_pct = stock.get('f3', 0) or 0
                    raw_change = stock.get('f4', 0) or 0
                    result.append({
                        'symbol': market + code,
                        'code': code,
                        'name': name,
                        'market': market,
                        'price': round(float(raw_price) / 100, 2) if raw_price else 0,
                        'changePercent': round(float(raw_change_pct) / 100, 2) if raw_change_pct else 0,
                        'change': round(float(raw_change) / 100, 2) if raw_change else 0,
                        'hotRank': idx + 1
                    })
        
        print(f'[热搜榜] 成功获取 {len(result)} 只沪深主板股票')
        
        return {
            'updateTime': datetime.now().isoformat(),
            'total': len(stocks),
            'filteredCount': len(result),
            'stocks': result
        }
            
    except Exception as error:
        print(f'[热搜榜] 获取失败: {str(error)}')
        raise error
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
                        'volume': float(fields[36]) if fields[36] else 0
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

async def get_kline_data(symbol: str, client: Optional[httpx.AsyncClient] = None) -> Optional[List[List]]:
    today = date.today().isoformat()
    start_date = KLINE_START_DATE
    url = f'{TENCENT_KLINE_API}?param={symbol},day,{start_date},{today},{KLINE_DATA_LIMIT},qfq'
    cl = client or get_http_client()
    
    try:
        kline = []
        
        try:
            response = await cl.get(url)
            response.raise_for_status()
            data = response.json()
            if data.get('data') and data['data'].get(symbol):
                kline = data['data'][symbol].get('qfqday', []) or data['data'][symbol].get('day', [])
        except Exception as e:
            print(f'[K线API请求] {str(e)}')
        
        if not kline or len(kline) < 30:
            return None
        
        try:
            realtime_data = await fetch_with_retry(symbol, cl)
            if realtime_data:
                fields = parse_realtime_fields(realtime_data)
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
        
        return kline
    except Exception as e:
        print(f'[K线获取失败] {symbol}: {str(e)}')
        return None
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
        change_val = float(fields[31]) if fields[31] else 0
        change_pct = float(fields[32]) if fields[32] else 0
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
            'price': fields[3],
            'priceRaw': price,
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
