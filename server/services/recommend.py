"""recommend.py - split from main.py"""
import json, asyncio, re, requests
from typing import List, Dict, Optional
from datetime import datetime, date
from app.config import (HOT_STOCK_POOL, STRATEGY_PREDICT_BULL_MIN_SCORE, QUERY_DAILY_RECOMMENDATIONS_LIMIT,
                        STRATEGY_INDUSTRY_NEUTRALIZATION_ENABLED, EASTMONEY_HOT_LIST_API, TREND_LIMIT_UP_THRESHOLD,
                        BUY_SELL_POSITION_LOW_GATE, BUY_SELL_POSITION_MIDLOW_GATE, BUY_SELL_POSITION_HIGH_GATE,
                        BUY_SELL_TARGET_POSITION_LOW_MULT, BUY_SELL_TARGET_POSITION_MIDLOW_MULT, BUY_SELL_TARGET_POSITION_HIGH_MULT,
                        BUY_SELL_TARGET_LEADER_MULT, BUY_SELL_TARGET_FOLLOWER_MULT,
                        BUY_SELL_TARGET_SCORE_HIGH_MULT, BUY_SELL_TARGET_SCORE_LOW_MULT,
                        BUY_SELL_LEADER_CONSECUTIVE_BOARDS, BUY_SELL_LEADER_SCORE_GATE,
                        BUY_SELL_LEADER_FALLBACK_CHANGE_PCT, BUY_SELL_LEADER_FALLBACK_VOL_RATIO,
                        BUY_SELL_MAX_TARGET_PCT)
from db.database import get_db_conn, load_daily_recommendations, save_daily_recommendations, load_user_scan_pool, load_hot_stock_buttons, load_trend_scan_results, load_stock_concept_tags, save_stock_industry
from services.stock import fetch_stock_data, fetch_with_retry, parse_tencent_batch_data
from services.strategy import calc_stock_score_v2, apply_industry_neutralization
from services.utils import to_float as _to_float
from services.valuation import get_valuation_batch
from services.risk_control import calc_risk_control


async def fetch_batch_fund_flow(codes: List[str]) -> Dict[str, Dict]:
    """批量获取主力资金流向数据（P4-3 靶向优化，2026-07-02）

    改用东方财富 ulist.get 接口，通过 secids 直接获取指定股票的资金流向。
    相比 P4-2 的全市场分页扫描（~6s），靶向请求耗时降至 ~200ms。

    Args:
        codes: 股票代码列表（小写，如 ['sh600519', 'sz000858']）

    Returns:
        {symbol: {'main_net_inflow': float(元), 'main_inflow_ratio': float(%),
                  'circ_mv': float(元), 'industry': str}}
    """
    if not codes:
        return {}

    # 构建 secids: 1.600519 (沪), 0.000858 (深)
    secids = []
    for c in codes:
        code = c[2:]
        market = '1' if c.startswith('sh') else '0'
        secids.append(f"{market}.{code}")

    _HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': '*/*',
        'Referer': 'https://quote.eastmoney.com/',
    }

    # 2026-07-08 修复：分批避免 414，100/批安全（<2KB URL），并发执行
    _BATCH_SIZE = 100

    def _fetch_batch(batch_secids: List[str]) -> List[Dict]:
        # ulist.get 支持批量获取指定 secids
        params = {
            'pi': 0, 'pz': len(batch_secids), 'po': 1, 'np': 1, 'fltt': 2, 'invt': 2,
            'fields': 'f12,f13,f62,f21,f100',
            'secids': ','.join(batch_secids)
        }
        # 使用 EASTMONEY_HOT_LIST_API 的基础域名，但接口路径不同
        # 实际地址示例: https://push2.eastmoney.com/api/qt/ulist/get
        url = EASTMONEY_HOT_LIST_API.replace('/clist/get', '/ulist/get')
        r = requests.get(url, params=params, headers=_HEADERS, timeout=5)
        r.raise_for_status()
        data = r.json()
        return data.get('data', {}).get('diff', []) or []

    # 2026-07-06 : _to_float 已合并到 services.utils.to_float（顶部 import 别名 _to_float）
    # 此处原局部函数定义已删除

    try:
        # 分批（100/批），每批并行执行
        batches = [secids[i:i + _BATCH_SIZE] for i in range(0, len(secids), _BATCH_SIZE)]
        batch_results = await asyncio.gather(
            *(asyncio.to_thread(_fetch_batch, b) for b in batches),
            return_exceptions=True
        )
        # 合并所有批次的 diff 列表
        items: List[Dict] = []
        for r in batch_results:
            if isinstance(r, Exception) or not r:
                continue
            items.extend(r)

        found: Dict[str, Dict] = {}
        for item in items:
            code = item.get('f12', '')
            if not code: continue
            market = item.get('f13', 0)
            symbol = f'{"sh" if market == 1 else "sz"}{code}'.lower()
            main_inflow = _to_float(item.get('f62'))
            circ_mv = _to_float(item.get('f21'))
            main_ratio = (main_inflow / circ_mv * 100) if circ_mv > 0 else 0.0
            industry = item.get('f100', '') or ''
            found[symbol] = {
                'main_net_inflow': main_inflow,
                'main_inflow_ratio': main_ratio,
                'circ_mv': circ_mv,
                'industry': industry,
            }
        print(f'[资金流向] 靶向获取 {len(found)}/{len(codes)} 只股票数据 (分 {len(batches)} 批)')
        return found
    except Exception as e:
        print(f'[资金流向] 靶向获取失败: {e}')
        return {}


def _extract_position_and_leader(stock: Dict, score_result: Optional[Dict],
                                  trend_details: Optional[Dict]) -> tuple:
    """从 score_result / trend_details 提取位置分位和龙头身份。

    位置优先级：trend_details.positionPct（精确年度分位）> base_scores['position']（5/10/15 粗估）> None
    龙头优先级：trend_details.consecutiveUpDays（精确连板）> 涨幅+量比 fallback > 评分+位置 fallback

    Returns: (position_pct: Optional[float], is_leader: bool, position_label: str)
    """
    details = {}
    base_scores = {}
    factor_values = {}
    stock_score = 0.0

    if score_result and isinstance(score_result, dict):
        base_scores = score_result.get('base_scores', {}) or {}
        factor_values = score_result.get('factor_values', {}) or {}
        stock_score = float(score_result.get('total', 0) or 0)

    # 也兼容 stock 自带 score dict（hot-stocks-scan 路径 stock['score'] 是 dict）
    if not stock_score and isinstance(stock.get('score'), dict):
        stock_score = float(stock['score'].get('total', 0) or 0)
        base_scores = stock['score'].get('base_scores', {}) or {}
        factor_values = stock['score'].get('factor_values', {}) or {}

    # ---- 位置分位 ----
    position_pct = None
    if trend_details and isinstance(trend_details, dict):
        position_pct = trend_details.get('positionPct')

    if position_pct is None:
        # fallback: base_scores['position'] 5=高位 / 10=中位 / 15=低位 → 映射到近似分位
        pos_score = base_scores.get('position', 10)
        if pos_score <= 5:
            position_pct = 80   # 高位
        elif pos_score >= 15:
            position_pct = 20   # 低位
        else:
            position_pct = 50   # 中位

    # ---- 位置标签 ----
    if position_pct <= BUY_SELL_POSITION_LOW_GATE:
        position_label = '低位'
    elif position_pct <= BUY_SELL_POSITION_MIDLOW_GATE:
        position_label = '中低位'
    elif position_pct >= BUY_SELL_POSITION_HIGH_GATE:
        position_label = '高位'
    else:
        position_label = '中位'

    # ---- 龙头判定 ----
    is_leader = False
    if trend_details and isinstance(trend_details, dict):
        consecutive = trend_details.get('consecutiveUpDays', 0) or 0
        limit_up_count = trend_details.get('limitUpCount20d', 0) or 0
        is_leader = (consecutive >= BUY_SELL_LEADER_CONSECUTIVE_BOARDS or
                     limit_up_count >= 3 or
                     (stock_score >= BUY_SELL_LEADER_SCORE_GATE and position_pct < BUY_SELL_POSITION_HIGH_GATE))

    if not is_leader:
        # fallback: 涨停 = 龙头；或 涨幅≥5% + 量比≥2 = 龙头
        change_pct = abs(float(factor_values.get('change_percent', 0) or stock.get('changePercent', 0) or 0))
        vol_ratio = float(factor_values.get('volume_ratio', 0) or stock.get('volumeRatio', 0) or 0)
        if change_pct >= TREND_LIMIT_UP_THRESHOLD:
            is_leader = True
        elif change_pct >= BUY_SELL_LEADER_FALLBACK_CHANGE_PCT and vol_ratio >= BUY_SELL_LEADER_FALLBACK_VOL_RATIO:
            is_leader = True
        elif stock_score >= BUY_SELL_LEADER_SCORE_GATE and position_pct < BUY_SELL_POSITION_HIGH_GATE:
            is_leader = True

    return position_pct, is_leader, position_label


def _build_stock_type(is_leader: bool, position_label: str) -> str:
    """构建股票类型标签，如 '低位龙头' / '高位跟风'"""
    leader_str = '龙头' if is_leader else '跟风'
    return f'{position_label}{leader_str}' if position_label else leader_str


def _build_expectation_note(is_leader: bool, position_label: str, score: float, sell_pct: float) -> str:
    """构建预期说明文字"""
    parts = []
    if position_label:
        parts.append(position_label)
    parts.append('龙头' if is_leader else '跟风')
    if score >= 80:
        parts.append('高分确认')
    elif score < 55:
        parts.append('低分谨慎')
    type_str = ''.join(parts)
    note = f'{type_str}，预期涨幅{sell_pct*100:.1f}%'
    if is_leader and position_label in ('低位', '中低位'):
        note += '，上行空间较大'
    elif not is_leader and position_label == '高位':
        note += '，快进快出'
    elif position_label == '高位':
        note += '，注意控制仓位'
    return note


def calc_buy_sell_points(stock: Dict, score_result: Optional[Dict] = None,
                         trend_details: Optional[Dict] = None) -> Dict:
    """计算买入点、当前价、卖出点（V4.2 位置+龙头感知版）

    V4.1: 基于振幅动态调整买卖比例
    V4.2: 叠加位置高低修正 + 龙头/跟风修正 + 评分置信度修正
          止盈倍率 = 位置倍率 × 龙头倍率 × 评分倍率，封顶 15%
          龙头判定：连板≥2 / 涨停 / (涨幅≥5%+量比≥2) / (高分+非高位)
          位置判定：优先 trend_details.positionPct，fallback base_scores['position']

    Args:
        stock: 行情数据 dict
        score_result: calc_stock_score_v2 返回值（含 total/base_scores/factor_values）
        trend_details: 趋势扫描 details dict（含 positionPct/consecutiveUpDays，可选）
    """
    try:
        price = float(stock.get('priceRaw') or stock.get('price') or 0)
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        return {'buy': '', 'current': '', 'sell': ''}

    change_pct = float(stock.get('changePercent', 0) or 0)
    amplitude = float(stock.get('amplitude', 0) or 0)

    # ===== 提取位置/龙头/评分上下文 =====
    position_pct, is_leader, position_label = _extract_position_and_leader(
        stock, score_result, trend_details)

    stock_score = 0.0
    if score_result and isinstance(score_result, dict):
        stock_score = float(score_result.get('total', 0) or 0)
    elif isinstance(stock.get('score'), dict):
        stock_score = float(stock['score'].get('total', 0) or 0)
    elif stock.get('score'):
        stock_score = float(stock.get('score', 0) or 0)

    # ===== 位置止盈倍率 =====
    if position_pct <= BUY_SELL_POSITION_LOW_GATE:
        pos_mult = BUY_SELL_TARGET_POSITION_LOW_MULT
    elif position_pct <= BUY_SELL_POSITION_MIDLOW_GATE:
        pos_mult = BUY_SELL_TARGET_POSITION_MIDLOW_MULT
    elif position_pct >= BUY_SELL_POSITION_HIGH_GATE:
        pos_mult = BUY_SELL_TARGET_POSITION_HIGH_MULT
    else:
        pos_mult = 1.0

    # ===== 龙头止盈倍率 =====
    leader_mult = BUY_SELL_TARGET_LEADER_MULT if is_leader else BUY_SELL_TARGET_FOLLOWER_MULT

    # ===== 评分置信度止盈倍率 =====
    if stock_score >= 80:
        score_mult = BUY_SELL_TARGET_SCORE_HIGH_MULT
    elif stock_score < 55:
        score_mult = BUY_SELL_TARGET_SCORE_LOW_MULT
    else:
        score_mult = 1.0

    # ===== 涨停板特殊处理 =====
    if change_pct >= TREND_LIMIT_UP_THRESHOLD:
        weibi = float(stock.get('weibi', 0) or 0)
        turnover = float(stock.get('turnoverRate', 0) or 0)
        # 龙头涨停给更高预期
        if is_leader and weibi > 80 and 2 <= turnover <= 15:
            base_sell_pct = 0.05   # 龙头封单坚决
        elif is_leader:
            base_sell_pct = 0.07   # 龙头普通封板
        else:
            base_sell_pct = 0.04   # 跟风封板

        # 位置修正（龙头倍率已在 base_sell_pct 中体现，此处只叠位置+评分）
        sell_pct = base_sell_pct * pos_mult * score_mult
        sell_pct = min(sell_pct, BUY_SELL_MAX_TARGET_PCT)

        stock_type = _build_stock_type(is_leader, position_label)
        return {
            'buy': '竞价关注',
            'current': f'{price:.2f}',
            'sell': f'{price * (1 + sell_pct):.2f}',
            'sellPct': round(sell_pct * 100, 1),
            'stockType': stock_type,
            'expectationNote': _build_expectation_note(is_leader, position_label, stock_score, sell_pct),
        }

    # ===== 非涨停板：振幅基础 + 三维修正 =====
    if amplitude >= 5:
        base_buy_pct = 0.03
        base_sell_pct = 0.06
    elif amplitude >= 3:
        base_buy_pct = 0.025
        base_sell_pct = 0.05
    elif amplitude >= 2:
        base_buy_pct = 0.02
        base_sell_pct = 0.04
    else:
        base_buy_pct = 0.015
        base_sell_pct = 0.03

    # 止盈倍率 = 位置 × 龙头 × 评分
    total_mult = pos_mult * leader_mult * score_mult
    sell_pct = min(base_sell_pct * total_mult, BUY_SELL_MAX_TARGET_PCT)

    # 买入回撤受位置影响：低位可以多等（可能还有低点），高位少等（机会稍纵即逝）
    if position_pct <= BUY_SELL_POSITION_LOW_GATE:
        buy_pct = base_buy_pct * 1.3
    elif position_pct >= BUY_SELL_POSITION_HIGH_GATE:
        buy_pct = base_buy_pct * 0.7
    else:
        buy_pct = base_buy_pct

    stock_type = _build_stock_type(is_leader, position_label)
    return {
        'buy': f'{price * (1 - buy_pct):.2f}',
        'current': f'{price:.2f}',
        'sell': f'{price * (1 + sell_pct):.2f}',
        'sellPct': round(sell_pct * 100, 1),
        'buyPct': round(buy_pct * 100, 1),
        'stockType': stock_type,
        'expectationNote': _build_expectation_note(is_leader, position_label, stock_score, sell_pct),
    }


def gen_recommend_reason_server(stock: Dict, score_result: Dict) -> str:
    """生成推荐理由（V4.1 增强版，含趋势分和风险提示）"""
    reasons = []
    try:
        volume_ratio = float(stock.get('volumeRatio', 0) or 0)
        turnover_rate = float(stock.get('turnoverRate', 0) or 0)
        weibi = float(stock.get('weibi', 0) or 0)
        avg_dev = float(stock.get('avgPriceDeviation', 0) or 0)
        outer_ratio = stock.get('outerRatio', None)
        if outer_ratio is None:
            outer = stock.get('outerPlateRaw', 0) or 0
            inner = stock.get('innerPlateRaw', 0) or 0
            total_plate = outer + inner
            outer_ratio = (outer / total_plate * 100) if total_plate > 0 else 50

        # 趋势分（如果有）
        trend_score = score_result.get('trend_score')
        intraday_score = score_result.get('intraday_score')
        if trend_score is not None:
            reasons.append(f'趋势分{trend_score} + 日内分{intraday_score} → 综合{score_result.get("total", 0)}分')

        if volume_ratio > 1.2:
            reasons.append(f'量比放大({volume_ratio:.2f})，资金关注')
        if 2 < turnover_rate < 8:
            reasons.append(f'换手率适中({turnover_rate:.2f}%)，交投活跃')
        if weibi > 0:
            reasons.append(f'委比为正({weibi:.2f}%)，买盘强势')
        if 0 < avg_dev < 2:
            reasons.append('价格位于均价附近，走势稳健')
        if outer_ratio > 55:
            reasons.append(f'外盘占比({outer_ratio:.1f}%)高于内盘，主动买入较多')

        # 风险提示
        change_pct = float(stock.get('changePercent', 0) or 0)
        if change_pct >= TREND_LIMIT_UP_THRESHOLD:
            if weibi < 50 or turnover_rate > 15:
                reasons.append('⚠ 涨停但封单偏弱/换手偏高，追板有风险')
            else:
                reasons.append('已涨停，次日竞价关注')
    except Exception:
        pass

    if not reasons:
        reasons.append(f'综合评分{score_result.get("total", 0)}分，符合{score_result.get("label", "")}特征')

    return '📊 ' + '；'.join(reasons)
def _load_trend_score_map() -> Dict[str, Dict]:
    """从 trend_scan_results 加载今日所有 isUp 股票的符号→趋势分映射"""
    try:
        trend = load_trend_scan_results()
        stocks = trend.get('stocks', [])
        return {s['symbol'].lower(): {'score': s['score'], 'details': s.get('details', {})} for s in stocks}
    except Exception:
        return {}


async def auto_generate_recommendations_task():
    """定时任务：自动生成智能推荐股票并保存到数据库（V4.1）
    
    优化：
    - P0: 50%日内因子 + 50%趋势因子混合评分
    - P1: 推荐池 = hot_stock_buttons ∪ trend_scan_results(isUp=true)
    - P2: 行业分散（Top3不同概念）+ 智能买卖点
    """
    try:
        now = datetime.now()
        # 周末不生成推荐
        if now.weekday() >= 5:
            print(f'[智能推荐] 周末不生成推荐（{now.strftime("%Y-%m-%d %H:%M")}）')
            return

        print(f'[智能推荐] 开始自动生成推荐股票（{now.strftime("%Y-%m-%d %H:%M")}）...')

        # ===== P1: 推荐池 = hot_stock_buttons ∪ trend_scan_results =====
        pool = load_hot_stock_buttons()
        codes_list = [s['code'].lower() for s in pool if s.get('code')]
        codes_list = [c for c in codes_list if re.match(r'^(sh|sz)\d{6}$', c)]

        # 并入趋势扫描结果
        trend_map = _load_trend_score_map()
        before_merge = len(codes_list)
        for sym in trend_map:
            if sym not in codes_list:
                codes_list.append(sym)
        if trend_map:
            added = len(codes_list) - before_merge
            print(f'[智能推荐] 合并趋势池 {len(trend_map)} 只（新增 {added} 只），股票池共 {len(codes_list)} 只')

        if not codes_list:
            print('[智能推荐] 股票池为空，跳过本次推荐')
            return

        # ===== 2. 获取大盘涨跌幅 =====
        # 性能优化（2026-07-02）：大盘拉取与股票池批量行情并发
        async def _fetch_market_change() -> float:
            try:
                idx_data = await fetch_stock_data('sh000001')
                if idx_data:
                    return idx_data.get('parsed', {}).get('changePercent', 0)
            except Exception:
                pass
            return 0.0

        # ===== 3. 批量扫描行情 =====
        # 性能优化（2026-07-02）：batch 20→100 + asyncio.gather 并发（大盘一并并发），参考 /api/hot-stocks-scan 模式
        batch_size = 100

        async def _fetch_one_batch(batch):
            try:
                data = await fetch_with_retry(','.join(batch))
                return parse_tencent_batch_data(data)
            except Exception as e:
                print(f'  [智能推荐] 批次失败: {e}')
                return []

        batch_tasks = [
            _fetch_one_batch(codes_list[i:i + batch_size])
            for i in range(0, len(codes_list), batch_size)
        ]
        # 大盘 + 所有批次 gather 一起
        gather_results = await asyncio.gather(
            _fetch_market_change(),
            *[t for t in batch_tasks],
            return_exceptions=True
        )
        market_change = gather_results[0] if not isinstance(gather_results[0], Exception) else 0.0
        all_results = []
        for r in gather_results[1:]:
            if isinstance(r, Exception):
                continue
            all_results.extend(r)

        if not all_results:
            print('[智能推荐] 未获取到任何股票行情数据')
            return

        # ===== 4. 混合评分（P0: 50%日内 + 50%趋势 + 主力资金因子） =====
        concept_tags_raw = load_stock_concept_tags()
        concept_tags = {k.lower(): v for k, v in concept_tags_raw.items()}  # 统一小写
        # : 批量预取主力资金流向，注入第9因子
        fund_flow_map = {}
        try:
            fund_flow_map = await fetch_batch_fund_flow([s.get('symbol', '') for s in all_results])
        except Exception as e:
            print(f'[智能推荐] 资金流向预取失败（降级为无数据）: {e}')
        # : 批量预取股东人数（从 DB 缓存，月度数据），注入第10因子
        holder_map = {}
        try:
            from services.shareholder import get_holder_num_batch
            holder_map = await asyncio.to_thread(get_holder_num_batch,
                                                  [s.get('symbol', '') for s in all_results])
        except Exception as e:
            print(f'[智能推荐] 股东人数预取失败（降级为无数据）: {e}')
        # : P1a/P1b 批量预取基本面质量 + 聪明钱(龙虎榜)，注入第12/13因子
        fund_map = {}
        sm_map = {}
        try:
            from services.fundamental import get_fundamental_batch
            fund_map = await asyncio.to_thread(get_fundamental_batch,
                                               [s.get('symbol', '') for s in all_results])
        except Exception as e:
            print(f'[智能推荐] 基本面预取失败（降级为无数据）: {e}')
        try:
            from services.smartmoney import get_dragon_tiger_symbol_map
            sm_map = await asyncio.to_thread(get_dragon_tiger_symbol_map)
        except Exception as e:
            print(f'[智能推荐] 聪明钱预取失败（降级为无数据）: {e}')
        # : P2c 批量预取估值分位（PE/PB 全市场百分位），注入风控过滤
        val_map = {}
        try:
            val_map = await asyncio.to_thread(get_valuation_batch,
                                              [s.get('symbol', '') for s in all_results])
        except Exception as e:
            print(f'[智能推荐] 估值分位预取失败（降级为无数据）: {e}')
        for stock in all_results:
            sym = stock.get('symbol', '').lower()
            trend = trend_map.get(sym)  # 趋势扫描已有的结果
            ff = fund_flow_map.get(sym)
            hd = holder_map.get(sym)
            fd = fund_map.get(sym)
            sm = sm_map.get(sym)
            val = val_map.get(sym)
            sm_data = {'on_board': True, 'net_amt': (sm or {}).get('net_amt', 0),
                       'appearances': (sm or {}).get('appearances', 0)} if sm else None
            stock['score'] = calc_stock_score_v2(stock, market_change=market_change,
                                                 trend_result=trend, fund_flow_data=ff,
                                                 holder_data=hd,
                                                 fundamental_data=fd, smart_money_data=sm_data)
            # P2a/P2c: 风控止损 + 估值分位（纯函数，无额外 IO）
            _holder_ratio = (hd or {}).get('total_num_ratio')
            stock['valuation'] = val
            stock['riskControl'] = calc_risk_control(
                stock.get('price', 0), score_result=stock['score'],
                stock=stock, valuation=val, smart_money=sm_data, holder_ratio=_holder_ratio)
            # 预取概念标签用于行业分散
            tags = concept_tags.get(sym)
            stock['primary_tag'] = tags[0] if tags else '其他'

        # 行业中性化：按行业 P20/P80 分位数调整 strategy_score.total（与 routes 路径对齐）
        if STRATEGY_INDUSTRY_NEUTRALIZATION_ENABLED:
            industry_map = {sym: (ff or {}).get('industry', '') for sym, ff in fund_flow_map.items()}
            apply_industry_neutralization(all_results, industry_map)
            try:
                save_stock_industry(industry_map)
            except Exception as e:
                print(f'[智能推荐] 行业映射持久化失败（不影响评分）: {e}')

        all_results.sort(key=lambda x: x['score']['total'], reverse=True)

        # ===== 5. 行业分散 + 过滤取前3（P2） =====
        MIN_RECOMMEND_SCORE = 60
        above_threshold = [s for s in all_results if s['score']['total'] >= MIN_RECOMMEND_SCORE]

        # P2c: 估值分位过滤 —— 极端高估(PE&PB 百分位双高)且非强多(>=80)的票不参与精选推荐
        candidates = []
        skipped_overvalued = 0
        for s in above_threshold:
            val = s.get('valuation')
            if val and val.get('overvalued') and s['score']['total'] < 80:
                skipped_overvalued += 1
                continue
            candidates.append(s)
        if skipped_overvalued:
            print(f'[智能推荐] 估值分位过滤：跳过 {skipped_overvalued} 只极端高估股')

        # 行业分散：每种概念取最高分的一只，最多3只
        top3 = []
        seen_tags = set()
        for s in candidates:
            tag = s.get('primary_tag', '其他')
            if tag not in seen_tags:
                top3.append(s)
                seen_tags.add(tag)
            if len(top3) >= 3:
                break

        # 如果行业分散后不足3只，从剩余中按分数补足（允许同概念）
        if len(top3) < 3:
            remaining = [s for s in candidates if s not in top3]
            top3.extend(remaining[:3 - len(top3)])

        if not top3:
            print(f'[智能推荐] 无评分>={MIN_RECOMMEND_SCORE}的股票，跳过')
            return

        # ===== 6. 计算买卖点 + 推荐理由 =====
        rec_items = []
        for r in top3:
            sym = r.get('symbol', '').lower()
            _trend = trend_map.get(sym)
            bsp = calc_buy_sell_points(r, score_result=r.get('score'),
                                       trend_details=(_trend or {}).get('details'))
            reason = gen_recommend_reason_server(r, {
                'total': r['score']['total'],
                'label': r['score']['label'],
                'intraday_score': r['score'].get('intraday_score'),
                'trend_score': r['score'].get('trend_score'),
            })
            rec_items.append({
                'name': r['name'],
                'symbol': r['symbol'],
                'price': r['price'],
                'change': r.get('change', 0),
                'changePercent': r.get('changePercent', 0),
                'score': r['score']['total'],
                'buySellPoints': bsp,
                'valuation': r.get('valuation'),
                'riskControl': r.get('riskControl'),
                'reason': reason,
            })

        # ===== 7. 保存到数据库 =====
        date_key = now.date().isoformat()
        record = {
            'date': date_key,
            'daily_recommendations': rec_items,
        }
        existing = load_daily_recommendations()
        existing = [r for r in existing if r.get('date') != date_key]
        existing.insert(0, record)
        if len(existing) > QUERY_DAILY_RECOMMENDATIONS_LIMIT:
            existing = existing[:QUERY_DAILY_RECOMMENDATIONS_LIMIT]
        saved = save_daily_recommendations(existing)

        if saved:
            symbols_str = '、'.join([f'{r["name"]}({r["symbol"]})' for r in rec_items])
            scores_str = '、'.join([f'{r["name"]}:{r["score"]}分' for r in rec_items])
            print(f'[智能推荐] 生成完成: {symbols_str}，评分: {scores_str}')
        else:
            print('[智能推荐] 保存失败')
        return {"recommendations": rec_items}
    except Exception as error:
        print(f'[智能推荐] 自动生成失败: {str(error)}')
        return None
