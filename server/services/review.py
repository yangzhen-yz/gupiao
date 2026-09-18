"""review.py - split from main.py"""
import asyncio, time, requests as _requests
from typing import List, Dict, Optional
from datetime import datetime
from app.config import (STRATEGY_PREDICT_BULL_MIN_SCORE, REVIEW_TOP_GAINERS_COUNT, REVIEW_TOP_LOSERS_COUNT, REVIEW_TOMORROW_FOCUS_COUNT, EASTMONEY_HEADERS)
# 2026-07-02 : _EM_HEADERS 已合并到 config.EASTMONEY_HEADERS
from db.database import (get_db_conn, load_daily_recommendations, load_trend_scan_results)
from services.stock import fetch_stock_data
from services.fundflow import _BROAD_CONCEPT_BLOCKLIST

_EASTMONEY_CLIST_BASE = 'https://push2delay.eastmoney.com/api/qt/clist/get'
_EM_UT = 'b2884a393a59ad64002292a3e90d46a5'
# 东方财富涨停池/跌停池专用API（返回当日真实涨跌停股票，而非按涨幅阈值估算）
_EM_ZT_POOL_URL = 'https://push2ex.eastmoney.com/getTopicZTPool'
_EM_DT_POOL_URL = 'https://push2ex.eastmoney.com/getTopicDTPool'
# 炸板池专用API（曾涨停但盘中开板未回封的股票，炸板率是短线情绪核心指标）
_EM_ZB_POOL_URL = 'https://push2ex.eastmoney.com/getTopicZBPool'
# 批量行情接口（按 secids 查多只股票最新涨跌幅，用于昨日涨停今日溢价）
_EM_ULIST_URL = 'https://push2delay.eastmoney.com/api/qt/ulist.np/get'
_EM_POOL_UT = '7eea3edcaed734bea9cbfc24409ed989'
_EM_POOL_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Referer': 'https://quote.eastmoney.com/ztb/detail',
    'Accept': 'application/json, text/plain, */*',
}


def _em_zt_pool_get(date_str: str, pool_type: str = 'zt', pagesize: int = 500, retries: int = 3, timeout: float = 12.0) -> List[Dict]:
    """东方财富涨停池/跌停池专用API。
    Args:
        date_str: 日期 'YYYYMMDD' 格式
        pool_type: 'zt'=涨停池, 'dt'=跌停池
    Returns:
        涨停/跌停股票列表，每项含 c(代码)/n(名称)/zdp(涨跌幅)/p(价格)/fund(封板资金)/hs(换手率)/lbc(连板数) 等字段。
        失败返回 []。
    """
    url = _EM_ZT_POOL_URL if pool_type == 'zt' else _EM_DT_POOL_URL
    sort_type = 'fbt:asc' if pool_type == 'zt' else 'fund:asc'
    params = {
        'ut': _EM_POOL_UT,
        'dpt': 'wz.ztzt',
        'Pageindex': 0,
        'pagesize': pagesize,
        'sort': sort_type,
        'date': date_str,
    }
    last_err = None
    for attempt in range(retries):
        try:
            r = _requests.get(url, params=params, headers=_EM_POOL_HEADERS, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            pool = (data.get('data') or {}).get('pool') or []
            return pool
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
    print(f'[收评] {pool_type}pool 全部{retries}次重试失败 date={date_str} err={last_err}')
    return []


def _get_limit_threshold_by_code(code: str) -> float:
    """根据股票代码前缀返回涨停涨幅阈值（降级方案用）。
    主板(60/00): 10% → 9.8%
    科创板(688)/创业板(300/301): 20% → 19.8%
    北交所(8/4): 30% → 29.8%
    """
    if not code:
        return 9.8
    if code.startswith('688') or code.startswith('300') or code.startswith('301'):
        return 19.8
    elif code.startswith('8') or code.startswith('4'):
        return 29.8
    else:
        return 9.8


def _em_clist_get(fid: str, fs: str, fields: str, pz: int, po: int = 1, pn: int = 1, retries: int = 3, timeout: float = 12.0) -> List[Dict]:
    """统一东方财富 clist 请求封装（与 fundflow._clist 一致）。
    - 3 次重试 + 指数退避；
    - raise_for_status（503/429 走重试）；
    - 空 body / rc!=0 / JSONDecodeError 统一走重试；
    - 所有重试失败：返回 []（绝不抛异常，上层业务 100% 降级）。
    """
    params = {
        'pn': pn, 'pz': pz, 'po': po, 'np': 1, 'fltt': 2, 'invt': 2,
        'fid': fid, 'fs': fs, 'fields': fields, 'ut': _EM_UT,
    }
    last_err = None
    for attempt in range(retries):
        try:
            r = _requests.get(_EASTMONEY_CLIST_BASE, params=params, headers=EASTMONEY_HEADERS, timeout=timeout)
            r.raise_for_status()
            text = r.text.strip() if r.text else ''
            if not text:
                last_err = ValueError('empty response')
                time.sleep(0.5 * (attempt + 1))
                continue
            data = r.json()
            if data.get('rc') not in (0, None):
                last_err = ValueError(f"rc={data.get('rc')}")
                time.sleep(0.5 * (attempt + 1))
                continue
            return (data.get('data') or {}).get('diff') or []
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
    print(f'[收评] clist 全部{retries}次重试失败 fid={fid!r} fs={fs!r} err={last_err}')
    return []


def _em_zb_pool_get(date_str: str, pagesize: int = 500, retries: int = 3, timeout: float = 12.0) -> List[Dict]:
    """东方财富炸板池专用API（曾涨停但开板未回封）。
    失败返回 []。
    """
    params = {
        'ut': _EM_POOL_UT,
        'dpt': 'wz.ztzt',
        'Pageindex': 0,
        'pagesize': pagesize,
        'sort': 'fbt:asc',
        'date': date_str,
    }
    last_err = None
    for attempt in range(retries):
        try:
            r = _requests.get(_EM_ZB_POOL_URL, params=params, headers=_EM_POOL_HEADERS, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            return (data.get('data') or {}).get('pool') or []
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
    print(f'[收评] 炸板池全部{retries}次重试失败 date={date_str} err={last_err}')
    return []


def _em_batch_change_percent(codes: List[str], batch_size: int = 40,
                             retries: int = 2, timeout: float = 10.0) -> Dict[str, float]:
    """按代码批量查询最新涨跌幅（东财 ulist 接口）。返回 {code: changePercent}。"""
    result: Dict[str, float] = {}

    def _to_secid(code: str) -> str:
        # 东财 secid 前缀：沪市 1.，深市/北交所 0.
        return ('1.' if code.startswith('6') else '0.') + code

    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        secids = ','.join(_to_secid(c) for c in batch if c)
        if not secids:
            continue
        for attempt in range(retries):
            try:
                r = _requests.get(_EM_ULIST_URL, params={
                    'ut': _EM_UT, 'fltt': 2, 'invt': 2, 'fields': 'f12,f3',
                    'secids': secids,
                }, headers=EASTMONEY_HEADERS, timeout=timeout)
                r.raise_for_status()
                data = r.json()
                diff = (data.get('data') or {}).get('diff') or []
                for item in diff:
                    code = str(item.get('f12', ''))
                    pct = item.get('f3')
                    if code and pct is not None:
                        result[code] = float(pct)
                break
            except Exception as e:
                if attempt == retries - 1:
                    print(f'[收评] 批量行情查询失败({len(batch)}只): {e}')
                else:
                    time.sleep(0.4)
    return result


def _fetch_market_breadth() -> Dict:
    """市场宽度：汇总全部行业板块的上涨/下跌家数（东财行业板块覆盖全市场个股）"""
    breadth = {'riseCount': None, 'fallCount': None, 'riseFallRatio': None}
    try:
        diff = _em_clist_get(fid='f3', po=1, pz=100, fs='m:90+t:2+f:!50',
                             fields='f12,f104,f105')
        if diff:
            rise = sum(int(item.get('f104') or 0) for item in diff)
            fall = sum(int(item.get('f105') or 0) for item in diff)
            if rise + fall > 0:
                breadth['riseCount'] = rise
                breadth['fallCount'] = fall
                breadth['riseFallRatio'] = round(rise / fall, 2) if fall > 0 else 99.0
    except Exception as e:
        print(f'[收评] 市场宽度统计失败: {e}')
    return breadth


def _calc_lianban_ladder(zt_pool_raw: List[Dict]) -> Dict:
    """连板梯队：从涨停池 lbc（连板数）统计高度分布"""
    ladder = {'maxLianban': None, 'lianbanGe3': None, 'lianbanEq2': None, 'firstBoard': None}
    lbcs = [int(item.get('lbc') or 0) for item in (zt_pool_raw or []) if item.get('lbc')]
    if not lbcs:
        return ladder
    ladder['maxLianban'] = max(lbcs)
    ladder['lianbanGe3'] = sum(1 for v in lbcs if v >= 3)
    ladder['lianbanEq2'] = sum(1 for v in lbcs if v == 2)
    ladder['firstBoard'] = sum(1 for v in lbcs if v == 1)
    return ladder


def _calc_yesterday_limitup_premium(zt_limit: int = 200) -> Optional[float]:
    """昨日涨停股今日平均表现（打板资金赚钱效应，短线情绪最直接指标）

    实现回溯最近4个自然日找到有涨停池数据的交易日，拉其涨停池，
    再批量查这些股票今日涨跌幅取均值。
    """
    from datetime import timedelta
    today = datetime.now().date()
    for back in range(1, 5):
        d = (today - timedelta(days=back)).strftime('%Y%m%d')
        try:
            pool = _em_zt_pool_get(d, pagesize=zt_limit, retries=1)
        except Exception:
            pool = []
        if not pool:
            continue
        codes = [str(item.get('c', '')) for item in pool if item.get('c')]
        if not codes:
            return None
        quotes = _em_batch_change_percent(codes)
        if not quotes:
            return None
        pcts = [quotes[c] for c in codes if c in quotes]
        if not pcts:
            return None
        return round(sum(pcts) / len(pcts), 2)
    return None


def _calc_sentiment_cycle(limit_up_count: int, limit_down_count: int,
                          breadth: Dict, ladder: Dict,
                          zhaban_count: Optional[int],
                          yesterday_premium: Optional[float]) -> Dict:
    """情绪周期定位（对标短线情绪周期框架：冰点→回暖→发酵→高潮→分歧→退潮→冰点）

    量化打分（0-100，基准50）：
      - 涨停家数、炸板率、连板高度、昨日涨停溢价、涨跌家数比五维合成
      - 跌停潮/昨日涨停大幅亏钱 → 强制降档
    阶段映射：≥75高潮 / 65-75发酵 / 55-65回暖 / 45-55分歧 / 35-45退潮 / <35冰点
    """
    score = 50
    parts = []

    # 1) 涨停家数
    if limit_up_count >= 80:
        score += 12
    elif limit_up_count >= 50:
        score += 8
    elif limit_up_count >= 30:
        score += 4
    elif limit_up_count <= 20:
        score -= 10
        parts.append(f'涨停仅{limit_up_count}只')

    # 2) 炸板率（炸板/(涨停+炸板)）
    zhaban_rate = None
    if zhaban_count is not None and (limit_up_count + zhaban_count) > 0:
        zhaban_rate = round(zhaban_count / (limit_up_count + zhaban_count) * 100, 1)
        if zhaban_rate < 20:
            score += 8
        elif zhaban_rate > 50:
            score -= 12
            parts.append(f'炸板率{zhaban_rate}%过高')
        elif zhaban_rate > 35:
            score -= 8
            parts.append(f'炸板率{zhaban_rate}%偏高')

    # 3) 连板高度
    if ladder.get('maxLianban'):
        ml = ladder['maxLianban']
        if ml >= 6:
            score += 8
        elif ml >= 4:
            score += 4
        elif ml >= 3:
            score += 2
        elif ml <= 2:
            score -= 4
            parts.append(f'最高仅{ml}连板')

    # 4) 昨日涨停今日溢价（打板赚钱效应）
    if yesterday_premium is not None:
        if yesterday_premium >= 2:
            score += 10
        elif yesterday_premium >= 0.5:
            score += 5
        elif yesterday_premium < -2:
            score -= 12
            parts.append(f'昨日涨停今日平均{yesterday_premium}%亏钱')
        elif yesterday_premium < 0:
            score -= 6
            parts.append(f'昨日涨停今日平均{yesterday_premium}%')

    # 5) 涨跌家数比
    rf = breadth.get('riseFallRatio')
    if rf is not None:
        if rf >= 2:
            score += 6
        elif rf >= 1.2:
            score += 3
        elif rf < 0.6:
            score -= 6
            parts.append(f'涨跌比{rf}失衡')

    # 6) 跌停潮强制降档
    if limit_down_count >= 20:
        score = min(score, 38)
        parts.append(f'跌停{limit_down_count}只')
    elif limit_down_count >= 10:
        score -= 5
        parts.append(f'跌停{limit_down_count}只')

    score = max(0, min(100, score))

    if score >= 75:
        cycle, advice = '高潮', '情绪亢奋，注意高位股分歧风险，不宜追高'
    elif score >= 65:
        cycle, advice = '发酵', '情绪主升，可跟随主线龙头，注意仓位节奏'
    elif score >= 55:
        cycle, advice = '回暖', '情绪修复，轻仓试错主流题材低位股'
    elif score >= 45:
        cycle, advice = '分歧', '多空拉锯，等待方向，降低操作频率'
    elif score >= 35:
        cycle, advice = '退潮', '亏钱效应扩散，防守为主，规避高位股'
    else:
        cycle, advice = '冰点', '情绪冰点，耐心等待企稳信号，严禁接力'

    detail = (f'涨停{limit_up_count}/跌停{limit_down_count}'
              + (f'，炸板率{zhaban_rate}%' if zhaban_rate is not None else '')
              + (f'，最高{ladder["maxLianban"]}连板' if ladder.get('maxLianban') else '')
              + (f'，昨日涨停今日平均{yesterday_premium:+.2f}%' if yesterday_premium is not None else '')
              + (f'，涨跌家数{breadth.get("riseCount")}:{breadth.get("fallCount")}'
                 if breadth.get('riseCount') is not None else ''))

    return {
        'cycle': cycle,
        'cycleScore': score,
        'advice': advice,
        'detail': detail,
        'weakSignals': parts,
        'indicators': {
            'zhabanRate': zhaban_rate,
            'maxLianban': ladder.get('maxLianban'),
            'lianbanGe3': ladder.get('lianbanGe3'),
            'lianbanEq2': ladder.get('lianbanEq2'),
            'firstBoard': ladder.get('firstBoard'),
            'riseCount': breadth.get('riseCount'),
            'fallCount': breadth.get('fallCount'),
            'yesterdayPremium': yesterday_premium,
        },
    }

async def generate_daily_review() -> Dict:
    try:
        today_str = datetime.now().strftime('%Y-%m-%d')
        
        # 1. 获取大盘指数数据（2026-07-06 A-P3-3：3 指数串行 → asyncio.gather 并发）
        market_data = {}
        indices = [
            {'code': 'sh000001', 'name': '上证指数'},
            {'code': 'sz399001', 'name': '深证成指'},
            {'code': 'sz399006', 'name': '创业板指'}
        ]

        async def _fetch_index(idx):
            try:
                idx_data = await fetch_stock_data(idx['code'])
                if idx_data:
                    parsed = idx_data.get('parsed', {})
                    return idx['code'], {
                        'name': idx['name'],
                        'price': parsed.get('price'),
                        'change': parsed.get('change'),
                        'changePercent': parsed.get('changePercent'),
                        'high': parsed.get('high'),
                        'low': parsed.get('low'),
                        'open': parsed.get('open'),
                        'volume': parsed.get('volume'),
                        'amount': parsed.get('amount'),
                    }
            except Exception as e:
                print(f'获取指数 {idx["name"]} 数据失败: {e}')
            return idx['code'], None

        _idx_results = await asyncio.gather(*[_fetch_index(idx) for idx in indices])
        for code, data in _idx_results:
            if data is not None:
                market_data[code] = data
        
        # 2. 获取行业板块涨跌排行（东方财富API）— 改用 requests 同步，避免 httpx 被封
        industry_sectors = []
        concept_sectors = []
        def _sync_fetch_sectors():
            ind_list, con_list = [], []
            try:
                # 行业板块 fid=f3（涨跌幅降序）；f:!50 排除 B 股干扰
                diff = _em_clist_get(fid='f3', po=1, pz=50, fs='m:90+t:2+f:!50',
                                     fields='f12,f14,f3,f6,f5,f62,f104,f105')
                for item in diff:
                    ind_list.append({
                        'name': item.get('f14', ''),
                        'code': item.get('f12', ''),
                        'changePercent': item.get('f3', 0),
                        'mainNetInflow': item.get('f62', 0),
                        'volume': item.get('f5', 0),
                        'amount': item.get('f6', 0),
                        'riseCount': item.get('f104', 0),
                        'fallCount': item.get('f105', 0),
                    })
            except Exception as e:
                print(f'[收评] 行业板块请求失败: {e}')
            try:
                # 概念板块 fid=f3（涨跌幅降序）；过滤宽泛概念黑名单
                diff = _em_clist_get(fid='f3', po=1, pz=50, fs='m:90+t:3+f:!50',
                                     fields='f12,f14,f3,f6,f5,f62,f104,f105')
                _blocklist = _BROAD_CONCEPT_BLOCKLIST
                for item in diff:
                    _name = item.get('f14', '')
                    if _name in _blocklist:
                        continue
                    con_list.append({
                        'name': _name,
                        'code': item.get('f12', ''),
                        'changePercent': item.get('f3', 0),
                        'mainNetInflow': item.get('f62', 0),
                        'volume': item.get('f5', 0),
                        'amount': item.get('f6', 0),
                        'riseCount': item.get('f104', 0),
                        'fallCount': item.get('f105', 0),
                    })
            except Exception as e:
                print(f'[收评] 概念板块请求失败: {e}')
            return ind_list, con_list
        try:
            industry_sectors, concept_sectors = await asyncio.to_thread(_sync_fetch_sectors)
            print(f'[收评] 行业板块:{len(industry_sectors)} 概念板块:{len(concept_sectors)}')
        except Exception as e:
            print(f'获取板块数据失败: {e}')
        
        # 3. 获取涨跌停个股 — 优先使用东方财富涨停池/跌停池专用API（准确），降级用clist+分板阈值
        limit_up_stocks = []
        limit_down_stocks = []
        top_gainers = []
        top_losers = []
        top_funds = []  # 全市场主力净流入前20
        def _sync_fetch_stocks():
            result = {'limit_up': [], 'limit_down': [], 'gainers': [], 'losers': [], 'top_funds': [],
                      'zt_pool_raw': [], 'zhaban_count': None}
            _fs_all_a = 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048'
            _today_yyyymmdd = datetime.now().strftime('%Y%m%d')

            # ---- 涨停股：优先用涨停池专用API ----
            _zt_pool = _em_zt_pool_get(_today_yyyymmdd, pool_type='zt')
            # 保留原始池数据（含 lbc 连板数）供情绪周期梯队计算
            result['zt_pool_raw'] = _zt_pool
            # 炸板数：仅当涨停池API正常返回时才查询；空结果视为未知（None），避免误判为0炸板
            if _zt_pool:
                _zb_pool = _em_zb_pool_get(_today_yyyymmdd)
                result['zhaban_count'] = len(_zb_pool) if _zb_pool else None
            if _zt_pool:
                for item in _zt_pool:
                    result['limit_up'].append({
                        'name': item.get('n', ''), 'code': item.get('c', ''),
                        'changePercent': item.get('zdp', 0),
                        'price': round(item.get('p', 0) / 1000, 2),  # API返回毫元，转为元
                        'mainNetInflow': item.get('fund', 0), 'turnoverRate': item.get('hs', 0),
                    })
                print(f'[收评] 涨停池API成功: {len(_zt_pool)}只涨停')
            else:
                # 降级：clist 涨幅排序 + 分板阈值
                print(f'[收评] 涨停池API无数据，降级为clist+分板阈值')
                try:
                    diff = _em_clist_get(fid='f3', po=1, pz=500, fs=_fs_all_a,
                                         fields='f2,f3,f8,f12,f14,f62')
                    for item in diff:
                        pct = item.get('f3', 0)
                        code = item.get('f12', '')
                        if pct is not None and pct >= _get_limit_threshold_by_code(code):
                            result['limit_up'].append({
                                'name': item.get('f14', ''), 'code': code,
                                'changePercent': pct, 'price': item.get('f2', 0),
                                'mainNetInflow': item.get('f62', 0), 'turnoverRate': item.get('f8', 0),
                            })
                except Exception as e:
                    print(f'[收评] 涨停股降级请求失败: {e}')

            # ---- 跌停股：优先用跌停池专用API ----
            _dt_pool = _em_zt_pool_get(_today_yyyymmdd, pool_type='dt')
            if _dt_pool:
                for item in _dt_pool:
                    result['limit_down'].append({
                        'name': item.get('n', ''), 'code': item.get('c', ''),
                        'changePercent': item.get('zdp', 0),
                        'price': round(item.get('p', 0) / 1000, 2),  # API返回毫元，转为元
                        'mainNetInflow': item.get('fund', 0), 'turnoverRate': item.get('hs', 0),
                    })
                print(f'[收评] 跌停池API成功: {len(_dt_pool)}只跌停')
            else:
                # 降级：clist 跌幅排序 + 分板阈值
                print(f'[收评] 跌停池API无数据，降级为clist+分板阈值')
                try:
                    diff = _em_clist_get(fid='f3', po=0, pz=500, fs=_fs_all_a,
                                         fields='f2,f3,f8,f12,f14,f62')
                    for item in diff:
                        pct = item.get('f3', 0)
                        code = item.get('f12', '')
                        if pct is not None and pct <= -_get_limit_threshold_by_code(code):
                            result['limit_down'].append({
                                'name': item.get('f14', ''), 'code': code,
                                'changePercent': pct, 'price': item.get('f2', 0),
                                'mainNetInflow': item.get('f62', 0), 'turnoverRate': item.get('f8', 0),
                            })
                except Exception as e:
                    print(f'[收评] 跌停股降级请求失败: {e}')

            # 涨幅前20
            try:
                diff = _em_clist_get(fid='f3', po=1, pz=20, fs=_fs_all_a,
                                     fields='f2,f3,f7,f8,f12,f14,f62')
                for item in diff:
                    result['gainers'].append({
                        'name': item.get('f14', ''), 'code': item.get('f12', ''),
                        'changePercent': item.get('f3', 0), 'price': item.get('f2', 0),
                        'turnoverRate': item.get('f8', 0), 'mainNetInflow': item.get('f62', 0),
                    })
            except Exception as e:
                print(f'[收评] 涨幅榜请求失败: {e}')
            # 跌幅前20
            try:
                diff = _em_clist_get(fid='f3', po=0, pz=20, fs=_fs_all_a,
                                     fields='f2,f3,f7,f8,f12,f14,f62')
                for item in diff:
                    result['losers'].append({
                        'name': item.get('f14', ''), 'code': item.get('f12', ''),
                        'changePercent': item.get('f3', 0), 'price': item.get('f2', 0),
                        'turnoverRate': item.get('f8', 0), 'mainNetInflow': item.get('f62', 0),
                    })
            except Exception as e:
                print(f'[收评] 跌幅榜请求失败: {e}')
            # 主力净流入全市场前20（按f62排序，与涨幅榜完全独立）
            try:
                diff = _em_clist_get(fid='f62', po=1, pz=20, fs=_fs_all_a,
                                     fields='f2,f3,f7,f8,f12,f14,f62')
                for item in diff:
                    result['top_funds'].append({
                        'name': item.get('f14', ''), 'code': item.get('f12', ''),
                        'changePercent': item.get('f3', 0), 'price': item.get('f2', 0),
                        'turnoverRate': item.get('f8', 0), 'mainNetInflow': item.get('f62', 0),
                    })
            except Exception as e:
                print(f'[收评] 主力资金榜请求失败: {e}')
            return result
        try:
            _stock_data = await asyncio.to_thread(_sync_fetch_stocks)
            limit_up_stocks = _stock_data['limit_up']
            limit_down_stocks = _stock_data['limit_down']
            top_gainers = _stock_data['gainers']
            top_losers = _stock_data['losers']
            top_funds = _stock_data['top_funds']
            print(f'[收评] 涨停:{len(limit_up_stocks)} 跌停:{len(limit_down_stocks)} 涨幅榜:{len(top_gainers)} 跌幅榜:{len(top_losers)} 主力TOP20:{len(top_funds)}')
        except Exception as e:
            print(f'获取涨跌停数据失败: {e}')
            _stock_data = {}

        # 4.5 短线情绪周期（涨停家数/炸板率/连板高度/昨日涨停溢价/涨跌家数比 五维合成）
        sentiment_cycle = None
        try:
            def _sync_fetch_sentiment():
                breadth = _fetch_market_breadth()
                ladder = _calc_lianban_ladder(_stock_data.get('zt_pool_raw') or [])
                yesterday_premium = _calc_yesterday_limitup_premium()
                return _calc_sentiment_cycle(
                    limit_up_count=len(limit_up_stocks),
                    limit_down_count=len(limit_down_stocks),
                    breadth=breadth, ladder=ladder,
                    zhaban_count=_stock_data.get('zhaban_count'),
                    yesterday_premium=yesterday_premium)
            sentiment_cycle = await asyncio.to_thread(_sync_fetch_sentiment)
            if sentiment_cycle:
                print(f'[收评] 情绪周期: {sentiment_cycle["cycle"]}({sentiment_cycle["cycleScore"]}分) {sentiment_cycle["detail"]}')
        except Exception as e:
            print(f'[收评] 情绪周期计算失败: {e}')
        
        # 4. 明日关注个股（独立三层来源，不再退化到涨幅榜）
        # 第一层：智能推荐（50%日内+50%趋势融合，最权威）
        # 第二层：策略预测（今日 scan 时记录的高分看涨票）
        # 第三层：趋势扫描（趋势分≥60 的票）
        # 三层去重合并；与"今日涨幅榜"完全独立
        tomorrow_focus = []
        seen_codes = set()
        source_tags = {}

        # 第一层：智能推荐
        try:
            recs = load_daily_recommendations() or []
            # 取最新一天
            today_recs = []
            if recs and isinstance(recs[0], dict) and 'daily_recommendations' in recs[0]:
                today_recs = recs[0].get('daily_recommendations', []) or []
            elif recs and isinstance(recs[0], dict):
                today_recs = recs
            for r in today_recs:
                code = r.get('symbol') or r.get('code')
                if not code or code in seen_codes:
                    continue
                seen_codes.add(code)
                source_tags[code] = '智能推荐'
                score = r.get('score', 0) or 0
                tomorrow_focus.append({
                    'name': r.get('name', ''),
                    'code': code,
                    'score': score,
                    'reason': f"智能推荐融合分{score}分，{r.get('reason', '')}" if r.get('reason') else f"智能推荐融合分{score}分",
                    'predictDirection': 'bull',
                })
        except Exception as e:
            print(f'[收评] 智能推荐加载失败: {e}')

        # 第二层：策略预测（今日高分看涨票）
        try:
            conn = get_db_conn()
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT * FROM strategy_predict_records WHERE predict_date = ? AND predict_direction = 'bull' AND score >= {STRATEGY_PREDICT_BULL_MIN_SCORE} ORDER BY score DESC LIMIT {REVIEW_TOMORROW_FOCUS_COUNT}",
                (today_str,)
            )
            predictions = cursor.fetchall()
            conn.close()
            for p in predictions:
                code = p['symbol']
                if code in seen_codes:
                    continue
                seen_codes.add(code)
                source_tags[code] = '策略预测'
                tomorrow_focus.append({
                    'name': p['name'],
                    'code': code,
                    'score': p['score'],
                    'reason': f"策略评分{p['score']}分，外盘占比{float(p['outer_ratio']):.1f}%，量比{float(p['volume_ratio']):.2f}，委比{float(p['weibi']):.1f}%",
                    'predictDirection': 'bull',
                })
        except Exception as e:
            print(f'[收评] 策略预测加载失败: {e}')

        # 第三层：趋势扫描高分票
        try:
            trend_data = load_trend_scan_results() or {}
            for t in (trend_data.get('stocks') or [])[:REVIEW_TOMORROW_FOCUS_COUNT]:
                code = t.get('symbol', '')
                if not code or code in seen_codes:
                    continue
                seen_codes.add(code)
                source_tags[code] = '趋势扫描'
                trend_score = t.get('score', 0) or 0
                tomorrow_focus.append({
                    'name': t.get('name', ''),
                    'code': code,
                    'score': trend_score,
                    'reason': f"趋势扫描分{trend_score}分，{t.get('details', {}).get('shortNote', '趋势向上')}（中线关注）",
                    'predictDirection': 'bull',
                })
        except Exception as e:
            print(f'[收评] 趋势扫描加载失败: {e}')

        # 截取前 N 只
        tomorrow_focus = tomorrow_focus[:REVIEW_TOMORROW_FOCUS_COUNT]

        # 标注来源到 reason 中便于前端展示
        for f in tomorrow_focus:
            tag = source_tags.get(f['code'], '')
            if tag and tag not in f['reason']:
                f['reason'] = f"[{tag}] " + f['reason']
        
        # 5. 生成收评
        review = {
            'date': today_str,
            'timestamp': datetime.now().isoformat(),
            'market': market_data,
            'industrySectors': industry_sectors[:20],
            'conceptSectors': concept_sectors[:20],
            # 真实总数：涨停/跌停家数（前端 KPI 卡直接展示，不再依赖截取后的数组长度
            'limitUpCount': len(limit_up_stocks),
            'limitDownCount': len(limit_down_stocks),
            # 数组只截前 20 只用于前端详情列表（避免 DB 存太多）
            'limitUpStocks': limit_up_stocks[:20],
            'limitDownStocks': limit_down_stocks[:20],
            'topGainers': top_gainers[:REVIEW_TOP_GAINERS_COUNT],
            'topLosers': top_losers[:REVIEW_TOP_LOSERS_COUNT],
            'topFunds': top_funds[:20],
            'tomorrowFocus': tomorrow_focus[:10],
            'sentimentCycle': sentiment_cycle,
            'summary': ''
        }
        
        # 生成摘要
        summary_lines = [f'【{today_str}收评】']
        if market_data.get('sh000001'):
            sz = market_data['sh000001']
            change_str = '上涨' if sz['changePercent'] > 0 else '下跌' if sz['changePercent'] < 0 else '平盘'
            summary_lines.append(f'大盘今日{change_str}，上证指数收报{sz["price"]}，涨跌幅{sz["changePercent"]}%。')
        
        if industry_sectors:
            top_ind = industry_sectors[0] if industry_sectors else None
            if top_ind:
                summary_lines.append(f'行业板块方面，{top_ind["name"]}领涨（+{top_ind["changePercent"]}%），', )
            if len(industry_sectors) > 1:
                worst_ind = industry_sectors[-1]
                summary_lines.append(f'{worst_ind["name"]}领跌（{worst_ind["changePercent"]}%）。')
        
        if concept_sectors:
            top_con = concept_sectors[0]
            summary_lines.append(f'概念板块中，{top_con["name"]}表现最强（+{top_con["changePercent"]}%）。')
        
        summary_lines.append(f'涨停{len(limit_up_stocks)}只，跌停{len(limit_down_stocks)}只。')

        if sentiment_cycle:
            summary_lines.append(f'短线情绪：{sentiment_cycle["cycle"]}期（{sentiment_cycle["cycleScore"]}分）。{sentiment_cycle["advice"]}')

        if tomorrow_focus:
            focus_names = '、'.join([f['name'] for f in tomorrow_focus[:3]])
            summary_lines.append(f'明日重点关注：{focus_names}等。')

        review['summary'] = '\n'.join(summary_lines)

        # ---- 情绪评分 & AI 智能分析结论 ----
        try:
            review['aiAnalysis'] = _build_ai_analysis(
                market=market_data,
                industry_sectors=industry_sectors,
                concept_sectors=concept_sectors,
                limit_up=limit_up_stocks,
                limit_down=limit_down_stocks,
                top_gainers=top_gainers,
                top_losers=top_losers,
                tomorrow_focus=tomorrow_focus,
                sentiment_cycle=sentiment_cycle,
            )
        except Exception as ai_err:
            print(f'[收评] AI 分析生成失败: {ai_err}')
            review['aiAnalysis'] = _empty_ai_analysis()

        return review
    except Exception as error:
        print(f'生成收评失败: {str(error)}')
        import traceback
        print(f'[收评] 错误堆栈: {traceback.format_exc()}')
        raise


def _empty_ai_analysis():
    return {
        'sentimentScore': 50,
        'sentimentLabel': '情绪平稳',
        'operationAdvice': {'level': 'hold', 'title': '震荡观望', 'detail': '数据不足，建议控制仓位。'},
        'tomorrowOutlook': {'title': '等待方向', 'detail': '明日关注大盘量能变化及板块轮动持续性。'},
        'riskWarning': {'level': 'low', 'title': '暂无明显风险', 'detail': '市场波动有限，按既定策略执行。'},
        'highlights': [],
    }


def _build_ai_analysis(market, industry_sectors, concept_sectors,
                       limit_up, limit_down, top_gainers, top_losers, tomorrow_focus,
                       sentiment_cycle=None):
    """规则引擎：根据市场/板块/涨跌停/涨跌幅 + 短线情绪周期生成情绪评分 + 智能分析结论"""
    # ---- 1) 情绪评分 (0-100) ----
    score = 50
    factors = []

    # 涨跌停差
    lu_cnt, ld_cnt = len(limit_up), len(limit_down)
    limit_diff = lu_cnt - ld_cnt
    if limit_diff > 30:
        score += 20; factors.append(f'涨停{lu_cnt}远多于跌停{ld_cnt}')
    elif limit_diff > 10:
        score += 12; factors.append(f'涨停{lu_cnt}明显多于跌停{ld_cnt}')
    elif limit_diff > 0:
        score += 5; factors.append(f'涨停{lu_cnt}略多于跌停{ld_cnt}')
    elif limit_diff < -10:
        score -= 15; factors.append(f'跌停{ld_cnt}明显多于涨停{lu_cnt}')
    elif limit_diff < 0:
        score -= 8; factors.append(f'跌停{ld_cnt}略多于涨停{lu_cnt}')

    # 大盘指数
    sh = market.get('sh000001', {})
    sz = market.get('sz399001', {})
    cy = market.get('sz399006', {})
    avg_change = sum([sh.get('changePercent', 0), sz.get('changePercent', 0), cy.get('changePercent', 0)]) / 3
    if avg_change > 1.5:
        score += 10; factors.append(f'三大指数平均涨{avg_change:.2f}%')
    elif avg_change > 0.3:
        score += 5
    elif avg_change < -1.5:
        score -= 10; factors.append(f'三大指数平均跌{avg_change:.2f}%')
    elif avg_change < -0.3:
        score -= 5

    # 板块轮动
    if industry_sectors:
        top = industry_sectors[0].get('changePercent', 0)
        lag = industry_sectors[-1].get('changePercent', 0)
        spread = top - lag
        if spread > 8:
            score += 5; factors.append(f'板块分化大({spread:.1f}%)')
        elif spread < 2:
            score -= 3; factors.append('板块联动弱')

    # 涨停/跌停比
    ratio = (lu_cnt / ld_cnt) if ld_cnt > 0 else (lu_cnt if lu_cnt > 0 else 1)
    if ratio >= 5:
        score += 5; factors.append('赚钱效应强')
    elif ratio < 0.5 and ld_cnt > 0:
        score -= 5; factors.append('亏钱效应扩散')

    # 短线情绪周期分（涨停家数/炸板率/连板高度/昨日涨停溢价/涨跌家数比五维合成）
    # 与上面的市场情绪分各占50%：前者偏短线打板生态，后者偏大盘整体环境
    cycle = sentiment_cycle or {}
    cycle_score = cycle.get('cycleScore')
    if cycle_score is not None:
        score = round(score * 0.5 + cycle_score * 0.5)
        factors.append(f'短线情绪周期：{cycle.get("cycle", "未知")}期({cycle_score}分)')

    score = max(0, min(100, score))

    if score >= 75: label = '情绪高涨'
    elif score >= 60: label = '情绪偏暖'
    elif score >= 45: label = '情绪平稳'
    elif score >= 30: label = '情绪偏冷'
    else: label = '情绪低迷'

    # ---- 2) 操作建议 ----
    if score >= 70:
        op_level, op_title = 'bull', '偏多：可适度加仓'
        op_detail = f'市场情绪{label}，涨停{lu_cnt}只显示资金活跃度高。'
        op_detail += '可关注领涨板块中位股补涨机会，仓位建议 6-8 成。'
    elif score >= 55:
        op_level, op_title = 'hold', '中性偏多：精选个股'
        op_detail = f'市场情绪{label}，结构机会存在。'
        op_detail += '建议聚焦主线板块龙头股，仓位 4-6 成，避免追高。'
    elif score >= 40:
        op_level, op_title = 'hold', '震荡观望：控制仓位'
        op_detail = f'市场情绪{label}，多空均衡。'
        op_detail += '建议降低仓位至 3-5 成，等待方向明朗。'
    elif score >= 25:
        op_level, op_title = 'bear', '偏空：谨慎为主'
        op_detail = f'市场情绪{label}，亏钱效应明显。'
        op_detail += '建议仓位降至 2-3 成，规避高位股，关注防御性板块。'
    else:
        op_level, op_title = 'bear', '空仓观望'
        op_detail = f'市场情绪{label}，系统性风险显现。'
        op_detail += '建议空仓或极低仓位（≤2成），等待市场企稳。'

    # ---- 3) 明日展望 ----
    if industry_sectors and concept_sectors:
        top_ind = industry_sectors[0]['name']
        top_con = concept_sectors[0]['name']
        if score >= 60:
            outlook_title = '主线有望延续'
            outlook_detail = f'关注"{top_ind}"行业及"{top_con}"概念能否持续走强。'
            outlook_detail += '若龙头股继续封板，可挖掘板块内低吸机会。'
        elif score >= 40:
            outlook_title = '结构性轮动'
            outlook_detail = f'关注"{top_ind}"是否切换，或新主线出现。'
            outlook_detail += '建议跟踪明日早盘量能及开盘 30 分钟板块表现。'
        else:
            outlook_title = '防御为主'
            outlook_detail = '情绪走弱时建议关注消费、医药等防御性板块，'
            outlook_detail += '以及前期超跌反弹机会。'
    else:
        outlook_title = '数据待完善'
        outlook_detail = '板块数据缺失，建议结合大盘走势及消息面综合判断。'

    # 明日关注个股（智能推荐/策略预测/趋势扫描三层来源）
    if tomorrow_focus:
        focus_parts = []
        for f in tomorrow_focus[:3]:
            tag = next((t for t in ('智能推荐', '策略预测', '趋势扫描') if f'[{t}]' in f.get('reason', '')), '')
            focus_parts.append(f'{f["name"]}({tag})' if tag else f['name'])
        outlook_detail += f'明日关注：{"、".join(focus_parts)}。'
        if cycle.get('cycle') in ('高潮', '发酵') and tomorrow_focus[0].get('score', 0) >= 70:
            outlook_detail += '情绪主升期，优先关注其中与当前主线共振的标的。'

    # ---- 4) 风险提示 ----
    risk_level = 'low'
    risk_title = '暂无明显风险'
    risk_detail = ''

    if cycle.get('cycle') == '冰点':
        risk_level = 'high'
        risk_title = '情绪冰点风险'
        risk_detail = (cycle.get('advice', '短线情绪冰点。') + ' '
                       + (cycle.get('detail') or ''))
    elif cycle.get('cycle') == '退潮':
        risk_level = 'mid'
        risk_title = '情绪退潮风险'
        risk_detail = (cycle.get('advice', '短线情绪退潮。') + ' '
                       + (cycle.get('detail') or ''))
    elif top_gainers and top_gainers[0].get('changePercent', 0) > 19:
        risk_level = 'mid'
        risk_title = '高位股追涨风险'
        risk_detail = f'今日涨幅第一{top_gainers[0]["name"]}达{top_gainers[0]["changePercent"]:.1f}%，'
        risk_detail += '高位股分歧加大，避免追涨。'
    elif lu_cnt >= 80 and score >= 75:
        risk_level = 'mid'
        risk_title = '情绪过热风险'
        risk_detail = f'涨停{lu_cnt}只，情绪进入高潮区，谨防次日分化。'
    elif ld_cnt >= 30 and score < 40:
        risk_level = 'high'
        risk_title = '系统性风险'
        risk_detail = f'跌停{ld_cnt}只，跌停潮可能扩散，建议减仓避险。'
    elif industry_sectors and industry_sectors[0].get('changePercent', 0) > 8:
        risk_level = 'mid'
        risk_title = '板块过热风险'
        risk_detail = f'{industry_sectors[0]["name"]}涨幅{industry_sectors[0]["changePercent"]:.1f}%，注意回调。'
    else:
        risk_detail = '市场波动正常范围内，按策略执行。'

    # 情绪周期弱势信号补充（不升级风险等级，仅提示）
    weak = cycle.get('weakSignals') or []
    if weak and cycle.get('cycle') not in ('退潮', '冰点'):
        risk_detail += f' 注意：{"、".join(weak[:2])}。'

    # ---- 5) 亮点摘要 ----
    highlights = []
    if factors:
        highlights.append('情绪驱动: ' + '、'.join(factors[:3]))
    if cycle.get('cycle'):
        highlights.append(f'情绪周期: {cycle["cycle"]}期({cycle.get("cycleScore")}分)')
    if industry_sectors:
        top = industry_sectors[0]
        highlights.append(f'领涨板块: {top["name"]} +{top["changePercent"]:.2f}%')
    if cycle.get('indicators', {}).get('maxLianban'):
        ind = cycle['indicators']
        highlights.append(f'连板高度: 最高{ind["maxLianban"]}连板（3板以上{ind.get("lianbanGe3") or 0}只）')
    elif limit_up and len(limit_up) > 0:
        # 无梯队数据时降级：按涨幅找最强涨停股
        high = max(limit_up, key=lambda s: s.get('changePercent', 0))
        highlights.append(f'涨停高度: {high.get("name")} +{high.get("changePercent", 0):.2f}%')
    if lu_cnt > 0 or ld_cnt > 0:
        highlights.append(f'涨跌停比: {lu_cnt}:{ld_cnt}')

    return {
        'sentimentScore': score,
        'sentimentLabel': label,
        'operationAdvice': {'level': op_level, 'title': op_title, 'detail': op_detail},
        'tomorrowOutlook': {'title': outlook_title, 'detail': outlook_detail},
        'riskWarning': {'level': risk_level, 'title': risk_title, 'detail': risk_detail},
        'highlights': highlights,
    }
