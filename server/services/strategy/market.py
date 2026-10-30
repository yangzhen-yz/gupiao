"""market.py - split from main.py"""
import asyncio, re, json, os
from typing import List, Dict, Optional
from datetime import date, datetime
from app.config import (INDEX_SYMBOLS, MARKET_CRASH_THRESHOLD, MARKET_SEVERE_CRASH_THRESHOLD, MARKET_ALERT_RISK_THRESHOLD)
from db.database import (load_hot_search_ranking, load_hot_stock_buttons, load_user_scan_pool, load_stock_basic_info)
from services.data.stock import fetch_with_retry, parse_tencent_batch_data

# ========== 大盘预警持久化 ==========
# 路径要锚在 server/ 根：这个模块 2026-10-16 从 services/ 搬进 services/strategy/ 之后，
# 旧那句 `dirname(__file__) + '/..'` 变成了指向 services/，预警会写到新位置，
# 而仓库里那份 server/market_alert.json 从此不再更新 —— 不报错，只是数据悄悄错位。
_ALERT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'market_alert.json')

def save_market_alert(alert_data: Dict):
    """保存最新大盘预警到文件（供前端轮询）"""
    try:
        alert_data['savedAt'] = datetime.now().isoformat()
        with open(_ALERT_FILE, 'w', encoding='utf-8') as f:
            json.dump(alert_data, f, ensure_ascii=False)
    except Exception as e:
        print(f'[大盘预警] 保存失败: {e}')

def load_market_alert() -> Optional[Dict]:
    """读取最新大盘预警"""
    try:
        if not os.path.exists(_ALERT_FILE):
            return None
        with open(_ALERT_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None

async def get_market_index(index_symbol: str) -> Optional[Dict]:
    """获取单个大盘指数数据"""
    try:
        data = await fetch_with_retry(index_symbol)
        match = re.search(r'v_\w+="([^"]+)"', data)
        if match:
            fields = match.group(1).split('~')
            if len(fields) > 40:
                return {
                    'symbol': index_symbol,
                    'name': fields[1],
                    'price': float(fields[3]) if fields[3] else 0,
                    'yesterdayClose': float(fields[4]) if fields[4] else 0,
                    'open': float(fields[5]) if fields[5] else 0,
                    'high': float(fields[33]) if fields[33] else 0,
                    'low': float(fields[34]) if fields[34] else 0,
                    'change': float(fields[31]) if fields[31] else 0,
                    'changePercent': float(fields[32]) if fields[32] else 0,
                    'volume': float(fields[36]) if fields[36] else 0
                }
    except Exception as e:
        print(f'[大盘指数] 获取 {index_symbol} 失败: {str(e)}')
    return None

async def check_market_crash() -> Dict:
    """检查大盘是否大跌

    2026-10-17 提速：3 个指数从「3 次并行 RTT」改为**一次批量请求**——腾讯行情接口
    支持逗号分隔多 symbol，且 `fetch_with_retry` 的 10 秒实时缓存按整串 key 生效，
    IndexBar 5 秒轮询大部分直接命中缓存（实测单次 0.7~1.0s → 命中 <5ms）。
    输出结构与逐只拉取时保持一致（字段名不变，消费方无感）。
    """
    from services.data.stock import fetch_with_retry, parse_tencent_batch_data
    symbols = ','.join(info['symbol'] for info in INDEX_SYMBOLS.values())
    indexes = []
    try:
        data = await fetch_with_retry(symbols)
        by_sym = {p['symbol'].lower(): p for p in parse_tencent_batch_data(data)}
        for info in INDEX_SYMBOLS.values():
            p = by_sym.get(info['symbol'].lower())
            if not p:
                continue
            indexes.append({
                'symbol': info['symbol'],
                'name': p.get('name') or info['name'],
                'price': float(p.get('priceRaw') or 0),
                'yesterdayClose': float(p.get('prevCloseRaw') or 0),
                'open': float(p.get('open') or 0),
                'high': float(p.get('highRaw') or 0),
                'low': float(p.get('lowRaw') or 0),
                'change': float(p.get('change') or 0),
                'changePercent': float(p.get('changePercent') or 0),
                'volume': float(p.get('volumeRaw') or 0),
            })
    except Exception as e:
        print(f'[大盘指数] 批量获取失败: {e}')

    any_crash = False
    any_severe_crash = False
    for r in indexes:
        if r['changePercent'] <= MARKET_CRASH_THRESHOLD:
            any_crash = True
        if r['changePercent'] <= MARKET_SEVERE_CRASH_THRESHOLD:
            any_severe_crash = True

    # 判断整体市场状态
    market_status = 'normal'
    crash_level = 'none'
    
    if any_severe_crash:
        market_status = 'severe_crash'
        crash_level = 'severe'
    elif any_crash:
        market_status = 'crash'
        crash_level = 'moderate'
    
    # 生成建议
    suggestion = get_crash_suggestion(market_status, indexes)
    
    return {
        'updateTime': datetime.now().isoformat(),
        'marketStatus': market_status,
        'crashLevel': crash_level,
        'indexes': indexes,
        'suggestion': suggestion,
        'thresholds': {
            'crash': MARKET_CRASH_THRESHOLD,
            'severeCrash': MARKET_SEVERE_CRASH_THRESHOLD
        }
    }

def get_crash_suggestion(market_status: str, indexes: List[Dict]) -> Dict:
    """根据市场状态生成建议"""
    if market_status == 'normal':
        return {
            'level': 'info',
            'title': '市场正常',
            'message': '当前市场运行平稳，可以正常操作。',
            'action': '保持观望或按计划操作',
            'shouldSell': False,
            'urgency': 0
        }
    elif market_status == 'crash':
        return {
            'level': 'warning',
            'title': '⚠️ 市场大幅下跌',
            'message': '大盘出现明显下跌，建议控制仓位，注意风险。',
            'action': '考虑减仓或观望',
            'shouldSell': True,
            'urgency': 1
        }
    elif market_status == 'severe_crash':
        return {
            'level': 'danger',
            'title': '🚨 紧急警报：市场暴跌！',
            'message': '大盘出现严重下跌，建议立即清仓避险！',
            'action': '紧急清仓！',
            'shouldSell': True,
            'urgency': 2
        }
    else:
        return {
            'level': 'info',
            'title': '市场状态未知',
            'message': '无法获取市场状态。',
            'action': '保持观望',
            'shouldSell': False,
            'urgency': 0
        }

# ========== 大盘预测系统（主力资金流向预测） ==========
# 性能优化（2026-07-02）：60 秒内存缓存，避免 TabQuote/TabStrategy 重复触发 500 只股票扫描
# 2026-10-30 陈旧兜底：60s TTL 一过，下一个请求要白付 ~500 只行情重扫（实测 5.38s）。
# 过期但未超 10 分钟 → 先返回旧值、后台线程刷新；行情页轮询场景秒级新鲜度足够。
import threading as _threading
_market_predict_cache: Dict[str, tuple] = {}  # {date_str: (timestamp, result)}
_MARKET_PREDICT_CACHE_TTL = 60  # 秒
_MARKET_PREDICT_STALE_MAX = 600  # 秒
_MARKET_PREDICT_REFRESH_TS = {'t': 0.0}          # 后台刷新 30s 节流
_MARKET_PREDICT_CTX = _threading.local()         # 后台刷新线程内置 compute=True

def _spawn_market_predict_refresh():
    _now = datetime.now().timestamp()
    if _now - _MARKET_PREDICT_REFRESH_TS['t'] < 30:
        return
    _MARKET_PREDICT_REFRESH_TS['t'] = _now

    def _run():
        _MARKET_PREDICT_CTX.compute = True
        try:
            asyncio.run(predict_market_risk())
            print(f'[大盘预测] 后台刷新完成')
        except Exception as e:
            print(f'[大盘预测] 后台刷新失败（旧缓存保留）: {e}')
    _threading.Thread(target=_run, daemon=True, name='market-predict-refresh').start()

async def predict_market_risk() -> Dict:
    """基于全市场股票数据预测大盘风险（主力资金流向预测）"""
    # 缓存检查：60 秒内同日命中直接返回
    today_str = date.today().isoformat()
    cached = _market_predict_cache.get(today_str)
    now_ts = datetime.now().timestamp()
    if cached and (now_ts - cached[0]) < _MARKET_PREDICT_CACHE_TTL:
        return cached[1]
    # 2026-10-30 陈旧兜底：过期但未超 10 分钟 → 立即返回旧值并后台刷新
    if cached and (now_ts - cached[0]) < _MARKET_PREDICT_STALE_MAX \
            and not getattr(_MARKET_PREDICT_CTX, 'compute', False):
        _spawn_market_predict_refresh()
        return cached[1]

    # 收集所有可用的股票池数据
    hot_search_ranking_data = load_hot_search_ranking()
    hot_stock_buttons = load_hot_stock_buttons()
    user_scan_pool = load_user_scan_pool()
    stock_basic_info = load_stock_basic_info()
    
    # 合并所有数据源，去重
    all_stocks = {}
    
    # 从hot_search_ranking添加
    for s in hot_search_ranking_data.get('stocks', []):
        all_stocks[s['symbol'].lower()] = s['symbol']
    
    # 从hot_stock_buttons添加
    for s in hot_stock_buttons:
        symbol = s.get('code', '').lower()
        if symbol and re.match(r'^(sh|sz)\d{6}$', symbol):
            all_stocks[symbol] = symbol
    
    # 从user_scan_pool添加
    for symbol in user_scan_pool:
        if re.match(r'^(sh|sz)\d{6}$', symbol):
            all_stocks[symbol.lower()] = symbol.lower()
    
    # 从stock_basic_info添加
    for symbol in stock_basic_info.keys():
        if re.match(r'^(sh|sz)\d{6}$', symbol):
            all_stocks[symbol.lower()] = symbol.lower()
    
    # 转换为列表
    stocks = list(all_stocks.values())
    
    if not stocks:
        # 如果没有数据，回到hot_search_ranking
        hot_search_ranking_stocks = hot_search_ranking_data.get('stocks', [])
        if not hot_search_ranking_stocks:
            return {
                'updateTime': datetime.now().isoformat(),
                'predictStatus': 'unknown',
                'riskScore': 0,
                'prediction': {
                    'level': 'info',
                    'title': '数据不足',
                    'message': '无法获取足够的股票数据进行预测',
                    'action': '稍后再试',
                    'shouldSell': False,
                    'urgency': 0
                },
                'signals': {},
                'details': {}
            }
        stocks = [s['symbol'] for s in hot_search_ranking_stocks]
    
    # 分批获取实时数据（最多取500只代表性股票）
    # 性能优化（2026-07-02）：batch 20→100 + asyncio.gather 并发（每批 100 只）
    # 旧实现：500只/20=25 批串行 await + 新建 httpx.AsyncClient，5-12s
    # 新实现：500只/100=5 批 gather 并发 + requests 同步，1-2s
    all_symbols = stocks[:500]
    batch_size = 100
    all_data = []

    async def _fetch_one_batch(batch):
        try:
            symbols_str = ','.join(batch)
            raw_data = await fetch_with_retry(symbols_str)
            return parse_tencent_batch_data(raw_data)
        except Exception as e:
            print(f'[预测扫描] 批次失败: {e}')
            return []

    batch_tasks = [
        _fetch_one_batch(all_symbols[i:i + batch_size])
        for i in range(0, len(all_symbols), batch_size)
    ]
    batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)
    for r in batch_results:
        if isinstance(r, Exception):
            continue
        all_data.extend(r)
    
    if not all_data:
        return {
            'updateTime': datetime.now().isoformat(),
            'predictStatus': 'unknown',
            'riskScore': 0,
            'prediction': {
                'level': 'info',
                'title': '数据不足',
                'message': '无法获取实时数据进行预测',
                'action': '稍后再试',
                'shouldSell': False,
                'urgency': 0
            },
            'signals': {},
            'details': {}
        }
    
    total = len(all_data)
    
    # === 信号1: 内外盘比（主力资金流向）===
    outer_lt_inner_count = 0
    outer_sum = 0
    inner_sum = 0
    outer_ratio_total = 0
    for s in all_data:
        outer = s.get('outerPlateRaw', 0)
        inner = s.get('innerPlateRaw', 0)
        outer_sum += outer
        inner_sum += inner
        if outer + inner > 0:
            ratio = (outer / (outer + inner)) * 100
            outer_ratio_total += ratio
            if ratio < 45:
                outer_lt_inner_count += 1
    
    avg_outer_ratio = outer_ratio_total / total if total > 0 else 50
    outer_weak_ratio = (outer_lt_inner_count / total * 100) if total > 0 else 0
    total_outer_inner_ratio = (outer_sum / (outer_sum + inner_sum) * 100) if (outer_sum + inner_sum) > 0 else 50
    
    # === 信号2: 涨跌分布（市场情绪）===
    up_count = 0
    down_count = 0
    heavy_down_count = 0
    mild_down_count = 0
    
    for s in all_data:
        cp = s.get('changePercent', 0)
        if cp > 0:
            up_count += 1
        elif cp < 0:
            down_count += 1
            if cp <= -3:
                heavy_down_count += 1
            elif cp <= -1:
                mild_down_count += 1
    
    up_ratio = (up_count / total * 100) if total > 0 else 50
    down_ratio = (down_count / total * 100) if total > 0 else 50
    heavy_down_ratio = (heavy_down_count / total * 100) if total > 0 else 0
    mild_down_ratio = (mild_down_count / total * 100) if total > 0 else 0
    
    # === 信号3: 放量下跌（主力出货信号）===
    volume_dump_count = 0
    for s in all_data:
        cp = s.get('changePercent', 0)
        vr = s.get('volumeRatio', 0)
        if cp < -1 and vr > 1.5:
            volume_dump_count += 1
        elif cp < -2 and vr > 1.0:
            volume_dump_count += 1
    
    volume_dump_ratio = (volume_dump_count / total * 100) if total > 0 else 0
    
    # === 信号4: 委比分布（卖压分析）===
    weibi_neg_count = 0
    weibi_heavy_neg_count = 0
    weibi_total = 0
    
    for s in all_data:
        wb = s.get('weibi', 0)
        weibi_total += wb
        if wb < -20:
            weibi_neg_count += 1
            if wb < -40:
                weibi_heavy_neg_count += 1
    
    avg_weibi = weibi_total / total if total > 0 else 0
    weibi_neg_ratio = (weibi_neg_count / total * 100) if total > 0 else 0
    weibi_heavy_neg_ratio = (weibi_heavy_neg_count / total * 100) if total > 0 else 0
    
    # === 综合风险评分 ===
    # 先判断是否为交易时间
    def is_trading_time():
        now = datetime.now()
        hour = now.hour
        minute = now.minute
        day = now.weekday()
        
        # 周末休市
        if day == 5 or day == 6:
            return False
        
        # 交易时间: 9:30-11:30, 13:00-15:00
        if (hour == 9 and minute >= 30) or (hour == 10) or (hour == 11 and minute < 30):
            return True
        if hour == 13 or hour == 14 or (hour == 15 and minute == 0):
            return True
        
        return False
    
    # 非交易时间降低风险评分
    if not is_trading_time():
        return {
            'updateTime': datetime.now().isoformat(),
            'predictStatus': 'safe',
            'riskScore': 0,
            'prediction': {
                'level': 'info',
                'title': '✅ 当前非交易时间',
                'message': '市场休市中，主力资金流向分析暂停。交易时间将自动恢复监控。',
                'action': '正常操作',
                'shouldSell': False,
                'urgency': 0
            },
            'signals': {},
            'details': {
                'totalStocks': total,
                'upCount': up_count,
                'downCount': down_count,
                'heavyDownCount': heavy_down_count,
                'mildDownCount': mild_down_count,
                'volumeDumpCount': volume_dump_count,
                'outerWeakCount': outer_lt_inner_count,
                'avgOuterRatio': round(avg_outer_ratio, 1),
                'totalOuterInnerRatio': round(total_outer_inner_ratio, 1),
                'avgWeibi': round(avg_weibi, 1),
                'weibiNegCount': weibi_neg_count,
                'weibiHeavyNegCount': weibi_heavy_neg_count
            }
        }
    
    # 信号1: 内外盘比 (权重35) - 最核心的主力资金指标
    score1 = 0
    if total_outer_inner_ratio < 40:
        score1 = 35
    elif total_outer_inner_ratio < 43:
        score1 = 28
    elif total_outer_inner_ratio < 45:
        score1 = 20
    elif total_outer_inner_ratio < 47:
        score1 = 12
    elif total_outer_inner_ratio < 49:
        score1 = 5
    
    # 信号2: 涨跌分布 (权重25) - 市场整体情绪
    score2 = 0
    if heavy_down_ratio > 12:
        score2 = 25
    elif heavy_down_ratio > 8:
        score2 = 20
    elif heavy_down_ratio > 5:
        score2 = 15
    elif down_ratio > 60:
        score2 = 18
    elif down_ratio > 50:
        score2 = 10
    elif down_ratio > 40:
        score2 = 5
    elif down_ratio == 0 and up_ratio == 100:
        # 非交易时间所有股票都显示上涨，不加分
        score2 = 0
    
    # 信号3: 放量下跌 (权重25) - 主力出货最直接信号
    score3 = 0
    if volume_dump_ratio > 10:
        score3 = 25
    elif volume_dump_ratio > 6:
        score3 = 20
    elif volume_dump_ratio > 4:
        score3 = 15
    elif volume_dump_ratio > 2:
        score3 = 10
    elif volume_dump_ratio > 1:
        score3 = 5
    elif volume_dump_ratio == 0 and down_ratio == 0:
        # 非交易时间没有成交量，不加分
        score3 = 0
    
    # 信号4: 委比分布 (权重15) - 卖压提前信号
    score4 = 0
    if weibi_heavy_neg_ratio > 15:
        score4 = 15
    elif weibi_heavy_neg_ratio > 8:
        score4 = 12
    elif weibi_neg_ratio > 35:
        score4 = 10
    elif weibi_neg_ratio > 25:
        score4 = 5
    elif weibi_neg_ratio > 15:
        score4 = 3
    elif weibi_neg_ratio == 0 and avg_weibi > 90:
        # 非交易时间委比异常高，不加分
        score4 = 0
    
    risk_score = score1 + score2 + score3 + score4
    
    # 判断预测状态（更灵敏的阈值）
    if risk_score >= 60:
        predict_status = 'severe_danger'
        prediction = {
            'level': 'danger',
            'title': '🚨 紧急警报：主力资金正在疯狂出逃！',
            'message': f'检测到强烈的主力出货信号：放量下跌股票占比{volume_dump_ratio:.1f}%，外盘/内盘比仅{total_outer_inner_ratio:.1f}%，卖压严重。预计大盘即将大跌，建议立即清仓避险！',
            'action': '立即清仓！',
            'shouldSell': True,
            'urgency': 2
        }
    elif risk_score >= 40:
        predict_status = 'danger'
        prediction = {
            'level': 'danger',
            'title': '⚠️ 预警：主力资金持续流出，风险加剧',
            'message': f'主力资金净流出明显：{volume_dump_ratio:.1f}%的股票出现放量下跌，外盘/内盘比{total_outer_inner_ratio:.1f}%，{weibi_neg_ratio:.1f}%的股票卖盘占优。建议大幅减仓控制风险。',
            'action': '大幅减仓',
            'shouldSell': True,
            'urgency': 1
        }
    elif risk_score >= 20:
        predict_status = 'warning'
        prediction = {
            'level': 'warning',
            'title': '👀 关注：主力资金有流出迹象',
            'message': f'市场出现初步风险信号：{down_ratio:.1f}%的股票下跌，{volume_dump_ratio:.1f}%的股票放量下跌。建议减仓观望，不要追高。',
            'action': '减仓观望',
            'shouldSell': True,
            'urgency': 0
        }
    else:
        predict_status = 'safe'
        prediction = {
            'level': 'info',
            'title': '✅ 市场风险较低',
            'message': f'当前主力资金流向正常，{up_ratio:.1f}%的股票上涨，市场运行平稳。',
            'action': '正常操作',
            'shouldSell': False,
            'urgency': 0
        }

    result = {
        'updateTime': datetime.now().isoformat(),
        'predictStatus': predict_status,
        'riskScore': risk_score,
        'prediction': prediction,
        'signals': {
            'capitalFlow': {
                'name': '主力资金流向',
                'value': f'{total_outer_inner_ratio:.1f}%',
                'detail': f'外盘/内盘比 {total_outer_inner_ratio:.1f}%（<45%为出货信号）',
                'score': score1,
                'maxScore': 30,
                'status': 'danger' if score1 >= 20 else 'warning' if score1 >= 10 else 'safe'
            },
            'upDownRatio': {
                'name': '涨跌分布',
                'value': f'{up_ratio:.1f}% / {down_ratio:.1f}%',
                'detail': f'上涨 {up_count} 只 / 下跌 {down_count} 只（大跌>3%: {heavy_down_count} 只）',
                'score': score2,
                'maxScore': 30,
                'status': 'danger' if score2 >= 20 else 'warning' if score2 >= 10 else 'safe'
            },
            'volumeDump': {
                'name': '放量下跌（主力出货）',
                'value': f'{volume_dump_ratio:.1f}%',
                'detail': f'{volume_dump_count} 只股票放量下跌（量比>1.5且跌幅>1%）',
                'score': score3,
                'maxScore': 25,
                'status': 'danger' if score3 >= 15 else 'warning' if score3 >= 10 else 'safe'
            },
            'weibi': {
                'name': '委比分布（卖压）',
                'value': f'{weibi_neg_ratio:.1f}%',
                'detail': f'{weibi_neg_count} 只股票委比小于-20，平均委比 {avg_weibi:.1f}',
                'score': score4,
                'maxScore': 15,
                'status': 'danger' if score4 >= 10 else 'warning' if score4 >= 5 else 'safe'
            }
        },
        'details': {
            'totalStocks': total,
            'upCount': up_count,
            'downCount': down_count,
            'heavyDownCount': heavy_down_count,
            'mildDownCount': mild_down_count,
            'volumeDumpCount': volume_dump_count,
            'outerWeakCount': outer_lt_inner_count,
            'avgOuterRatio': round(avg_outer_ratio, 1),
            'totalOuterInnerRatio': round(total_outer_inner_ratio, 1),
            'avgWeibi': round(avg_weibi, 1),
            'weibiNegCount': weibi_neg_count,
            'weibiHeavyNegCount': weibi_heavy_neg_count
        }
    }
    # 写入缓存
    _market_predict_cache[today_str] = (now_ts, result)
    return result


async def run_scheduled_market_alert():
    """定时任务：自动扫描大盘风险并保存预警（供前端轮询推送）"""
    try:
        print('[大盘预警] 定时扫描开始...')
        result = await predict_market_risk()
        risk_score = result.get('riskScore', 0)
        predict_status = result.get('predictStatus', 'safe')

        # 风险评分 >= 阈值时保存预警
        if risk_score >= MARKET_ALERT_RISK_THRESHOLD and predict_status != 'safe':
            save_market_alert({
                'date': date.today().isoformat(),
                'riskScore': risk_score,
                'predictStatus': predict_status,
                'prediction': result.get('prediction', {}),
                'signals': result.get('signals', {}),
                'details': result.get('details', {}),
            })
            print(f'[大盘预警] 风险评分 {risk_score} >= {MARKET_ALERT_RISK_THRESHOLD}，已保存预警')
        else:
            print(f'[大盘预警] 风险评分 {risk_score} < {MARKET_ALERT_RISK_THRESHOLD}，暂无预警')
    except Exception as e:
        print(f'[大盘预警] 定时扫描失败: {e}')
