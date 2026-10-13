"""recommend.py - split from main.py"""
import asyncio, re, requests, time
from typing import List, Dict, Optional
from datetime import datetime
from app.config import (QUERY_DAILY_RECOMMENDATIONS_LIMIT, STRATEGY_INDUSTRY_NEUTRALIZATION_ENABLED, EASTMONEY_HOT_LIST_API, TREND_LIMIT_UP_THRESHOLD, RECOMMEND_INTRADAY_FILTER_ENABLED, RECOMMEND_LIMIT_UP_PCT, RECOMMEND_BOARD_BREAK_DRAWDOWN, RECOMMEND_MAX_DRAWDOWN_FROM_HIGH, BUY_TIMING_STRONG_CHANGE_MIN, BUY_TIMING_TAIL_AMPLITUDE_MIN, BUY_TIMING_TAIL_DRAWDOWN_MIN, BUY_TIMING_HIGH_CHASE_CHANGE_MIN, RECOMMEND_CHASE_MIN_CHANGE, RECOMMEND_CHASE_MAX_CHANGE, RECOMMEND_CHASE_PENALTY, RECOMMEND_LOSER_LOOKBACK_DAYS, RECOMMEND_LOSER_LOSS_THRESHOLD, RECOMMEND_EXEC_HINT_ENABLED, RECOMMEND_EXEC_HINT, BUY_SELL_POSITION_LOW_GATE, BUY_SELL_POSITION_MIDLOW_GATE, BUY_SELL_POSITION_HIGH_GATE, BUY_SELL_TARGET_POSITION_LOW_MULT, BUY_SELL_TARGET_POSITION_MIDLOW_MULT, BUY_SELL_TARGET_POSITION_HIGH_MULT, BUY_SELL_TARGET_SCORE_HIGH_MULT, BUY_SELL_TARGET_SCORE_LOW_MULT, BUY_SELL_MAX_TARGET_PCT, BUY_SELL_ROLE_TARGET_MULT, BUY_SELL_ROLE_LABELS, BUY_SELL_ROLE_TOTAL_LEADER_BOARDS, BUY_SELL_ROLE_TOTAL_LEADER_LIMITUPS, BUY_SELL_ROLE_SECTOR_LEADER_BOARDS, BUY_SELL_ROLE_SECTOR_LEADER_LIMITUPS, BUY_SELL_ROLE_CORE_TREND_MV, BUY_SELL_ROLE_CORE_TREND_SCORE, BUY_SELL_ROLE_VANGUARD_TURNOVER, BUY_SELL_ROLE_CATCHUP_POSITION_MAX, BUY_SELL_ROLE_FOLLOWER_CHANGE_MIN, BUY_SELL_ROLE_FOLLOWER_VOL_RATIO_MIN, BUY_SELL_ROLE_FOLLOWER_SCORE_MIN, BUY_SELL_LEADER_FALLBACK_CHANGE_PCT, BUY_SELL_LEADER_FALLBACK_VOL_RATIO, BUY_SELL_LEADER_SCORE_GATE)
from db.database import (load_daily_recommendations, save_daily_recommendations, load_hot_stock_buttons, load_trend_scan_results, load_stock_concept_tags, save_stock_industry)
from services.stock import fetch_stock_data, fetch_with_retry, parse_tencent_batch_data
from services.strategy import calc_stock_score_v2, apply_industry_neutralization
from services.utils import to_float as _to_float
from services.valuation import get_valuation_batch
from services.risk_control import calc_risk_control
from services.placement import fetch_recent_placements


# 2026-08-31 性能优化：资金流批量拉取结果 20s 内存缓存。
# 背景：/api/hot-stocks-scan 与智能推荐每次都调本函数打东方财富 ulist 接口，
#       盘中资金流向变化不频繁，20s 内复用可显著降低外部请求频率与尾延迟。
_FUND_FLOW_BATCH_CACHE: Dict[str, Dict] = {}
_FUND_FLOW_BATCH_TTL = 20  # 秒


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

    # 2026-08-31 性能优化：按排序后的代码集合做 20s 内存缓存
    _ck = ','.join(sorted(set(c.lower() for c in codes)))
    _now = time.time()
    _hit = _FUND_FLOW_BATCH_CACHE.get(_ck)
    if _hit and (_now - _hit['ts']) < _FUND_FLOW_BATCH_TTL:
        return _hit['data']

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
        # 2026-08-31 性能优化：写入内存缓存（TTL 20s）
        _FUND_FLOW_BATCH_CACHE[_ck] = {'ts': time.time(), 'data': found}
        return found
    except Exception as e:
        print(f'[资金流向] 靶向获取失败: {e}')
        return {}


def _classify_stock_role(stock: Dict, score_result: Optional[Dict],
                         trend_details: Optional[Dict]) -> tuple:
    """6级角色分类（V4.3，替换原龙头/跟风二分法）

    按优先级从高到低匹配，命中即停：
    1. total_leader       总龙头：连板≥5 + 涨停次数≥4（全市场最高标）
    2. sector_leader      板块龙头：连板≥3 + 涨停次数≥2（板块内连板最高之一）
    3. core_trend         中军：大市值(≥300亿) + 连板≤1 + 评分≥65（趋势型权重股）
    4. sentiment_vanguard 情绪先锋：今日涨停 + 连板=1 + 高换手(≥5%)（率先试盘的弹性小票）
    5. catch_up           补涨龙：低位(分位<50) + 近期涨停(1-2板)（龙头高位后的低位新领涨）
    6. follower           跟风：涨幅≥3% + 量比≥1 + 评分≥50（被动跟随）
    7. marginal           杂毛：其余（辨识度最低、逻辑最弱）

    Returns: (position_pct: Optional[float], role: str, position_label: str)
    """
    base_scores = {}
    factor_values = {}
    stock_score = 0.0

    if score_result and isinstance(score_result, dict):
        base_scores = score_result.get('base_scores', {}) or {}
        factor_values = score_result.get('factor_values', {}) or {}
        stock_score = float(score_result.get('total', 0) or 0)

    # 兼容 stock 自带 score dict（hot-stocks-scan 路径 stock['score'] 是 dict）
    if not stock_score and isinstance(stock.get('score'), dict):
        stock_score = float(stock['score'].get('total', 0) or 0)
        base_scores = stock['score'].get('base_scores', {}) or {}
        factor_values = stock['score'].get('factor_values', {}) or {}

    # ---- 位置分位 ----
    position_pct = None
    if trend_details and isinstance(trend_details, dict):
        position_pct = trend_details.get('positionPct')

    if position_pct is None:
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

    # ---- 提取分类所需数据 ----
    # 连板数：优先 maxConsecutiveBoards（精确最大连板），fallback consecutiveUpDays（连涨天数）
    max_boards = 0
    limit_up_count = 0
    if trend_details and isinstance(trend_details, dict):
        max_boards = int(trend_details.get('maxConsecutiveBoards', 0) or
                         trend_details.get('consecutiveUpDays', 0) or 0)
        limit_up_count = int(trend_details.get('limitUpCount20d', 0) or 0)

    # 当日涨幅、量比、换手率、流通市值
    change_pct = abs(float(factor_values.get('change_percent', 0) or stock.get('changePercent', 0) or 0))
    vol_ratio = float(factor_values.get('volume_ratio', 0) or stock.get('volumeRatio', 0) or 0)
    turnover_rate = float(stock.get('turnoverRate', 0) or 0)
    circ_mv = float(stock.get('circ_mv', 0) or stock.get('flowMarketCap', 0) or 0)

    is_limit_up_today = change_pct >= TREND_LIMIT_UP_THRESHOLD

    # ---- 缓存角色门槛（V4.4：自动优化引擎）----
    _ROLE_THRESHOLDS = None
    try:
        from db.database import load_role_thresholds
        _ROLE_THRESHOLDS = load_role_thresholds()
    except Exception:
        _ROLE_THRESHOLDS = {}

    def _get_threshold(role: str, key: str, default_val):
        return _ROLE_THRESHOLDS.get(role, {}).get(key, default_val)

    # ---- 6级分类（按优先级从高到低） ----

    # 1. 总龙头
    tl_boards = _get_threshold('total_leader', 'min_boards', BUY_SELL_ROLE_TOTAL_LEADER_BOARDS)
    tl_limitups = _get_threshold('total_leader', 'min_limitups', BUY_SELL_ROLE_TOTAL_LEADER_LIMITUPS)
    if max_boards >= tl_boards and limit_up_count >= tl_limitups:
        return position_pct, 'total_leader', position_label

    # 2. 板块龙头
    sl_boards = _get_threshold('sector_leader', 'min_boards', BUY_SELL_ROLE_SECTOR_LEADER_BOARDS)
    sl_limitups = _get_threshold('sector_leader', 'min_limitups', BUY_SELL_ROLE_SECTOR_LEADER_LIMITUPS)
    if max_boards >= sl_boards and limit_up_count >= sl_limitups:
        return position_pct, 'sector_leader', position_label

    # 3. 中军
    ct_mv = _get_threshold('core_trend', 'min_mv', BUY_SELL_ROLE_CORE_TREND_MV)
    ct_score = _get_threshold('core_trend', 'min_score', BUY_SELL_ROLE_CORE_TREND_SCORE)
    if circ_mv >= ct_mv and max_boards <= 1 and stock_score >= ct_score:
        return position_pct, 'core_trend', position_label

    # 4. 情绪先锋
    vg_turnover = _get_threshold('sentiment_vanguard', 'min_turnover', BUY_SELL_ROLE_VANGUARD_TURNOVER)
    if is_limit_up_today and max_boards == 1 and turnover_rate >= vg_turnover:
        return position_pct, 'sentiment_vanguard', position_label

    # 5. 补涨龙
    cp_max_pos = _get_threshold('catch_up', 'max_position', BUY_SELL_ROLE_CATCHUP_POSITION_MAX)
    if position_pct < cp_max_pos and 1 <= max_boards <= 2:
        return position_pct, 'catch_up', position_label

    # 6. 跟风
    fl_change = _get_threshold('follower', 'min_change', BUY_SELL_ROLE_FOLLOWER_CHANGE_MIN)
    fl_vol = _get_threshold('follower', 'min_vol_ratio', BUY_SELL_ROLE_FOLLOWER_VOL_RATIO_MIN)
    fl_score = _get_threshold('follower', 'min_score', BUY_SELL_ROLE_FOLLOWER_SCORE_MIN)
    if (change_pct >= fl_change and vol_ratio >= fl_vol and stock_score >= fl_score):
        return position_pct, 'follower', position_label

    # ---- fallback：无 trend_details 时用涨停/涨幅+量比/高分判定 ----
    if max_boards == 0 and limit_up_count == 0:
        # 涨停 + 高换手 → 情绪先锋
        if is_limit_up_today and turnover_rate >= BUY_SELL_ROLE_VANGUARD_TURNOVER:
            return position_pct, 'sentiment_vanguard', position_label
        # 涨停 → 板块龙头（单日涨停至少是板块级别领涨）
        if is_limit_up_today:
            return position_pct, 'sector_leader', position_label
        # 涨幅≥5% + 量比≥2 → 板块龙头
        if change_pct >= BUY_SELL_LEADER_FALLBACK_CHANGE_PCT and vol_ratio >= BUY_SELL_LEADER_FALLBACK_VOL_RATIO:
            return position_pct, 'sector_leader', position_label
        # 高分 + 非高位 → 中军（趋势型强势股）
        if stock_score >= BUY_SELL_LEADER_SCORE_GATE and position_pct < BUY_SELL_POSITION_HIGH_GATE:
            return position_pct, 'core_trend', position_label

    # 7. 杂毛：其余
    return position_pct, 'marginal', position_label


def _build_stock_type(role: str, position_label: str) -> str:
    """构建股票类型标签，如 '低位总龙头' / '高位跟风' / '杂毛'"""
    role_label = BUY_SELL_ROLE_LABELS.get(role, '杂毛')
    return f'{position_label}{role_label}' if position_label else role_label


def _build_expectation_note(role: str, position_label: str, score: float, sell_pct: float) -> str:
    """构建预期说明文字（V4.3 6级角色版）"""
    role_label = BUY_SELL_ROLE_LABELS.get(role, '杂毛')
    parts = []
    if position_label:
        parts.append(position_label)
    parts.append(role_label)
    if score >= 80:
        parts.append('高分确认')
    elif score < 55:
        parts.append('低分谨慎')
    type_str = ''.join(parts)
    note = f'{type_str}，预期涨幅{sell_pct*100:.1f}%'

    # 角色专属建议
    if role in ('total_leader', 'sector_leader') and position_label in ('低位', '中低位'):
        note += '，上行空间较大'
    elif role == 'sentiment_vanguard':
        note += '，快进快出，波动极大'
    elif role == 'catch_up':
        note += '，注意龙头崩盘风险'
    elif role == 'core_trend' and position_label in ('低位', '中低位'):
        note += '，趋势稳健，适合格局'
    elif role == 'follower' and position_label == '高位':
        note += '，快进快出'
    elif role == 'marginal':
        note += '，盈亏比极差，建议回避'
    elif position_label == '高位':
        note += '，注意控制仓位'
    return note


def calc_buy_timing(stock: Dict, change_pct: float, amplitude: float,
                    role: str, position_label: str,
                    score: float = 0.0, market_change: float = 0.0) -> Dict:
    """买入时机判定（2026-10-12）：回答"这只票今天该什么时候买"

    A股实战规则（按优先级）：
    1. 已涨停封板    → 次日竞价：当日场内基本买不到，次日 9:15-9:25 竞价参与
    2. 强势情绪股    → 早盘买入：总龙头/板块龙头/情绪先锋 + 涨幅≥5%，情绪溢价最高在早盘
    3. 高不确定性    → 尾盘买入：振幅≥5% / 自高点回落≥2% / 高位追高，14:30 后确认全天形态再买
    4. 低位趋势股    → 回踩低吸：中军/补涨龙低位，不追高，等回踩至买入参考价
    5. 其余          → 盘中灵活：日内均价线附近介入，不追尾盘急拉

    Args:
        stock: 行情 dict（用 highRaw/priceRaw 算回落幅度）
        change_pct: 当日涨幅%
        amplitude: 当日振幅%
        role: _classify_stock_role 的角色
        position_label: 位置标签（低位/中低位/中位/高位）
        score: 综合评分
        market_change: 大盘涨跌幅%（弱市下强势股也降级为谨慎）

    Returns:
        {'type': 'auction'|'early'|'tail'|'pullback'|'intraday',
         'label': '次日竞价'|'早盘买入'|'尾盘买入'|'回踩低吸'|'盘中灵活',
         'note': '操作说明'}
    """
    # 自日内高点回落幅度（缺数据时为 0）
    drawdown = 0.0
    try:
        price = _to_float(stock.get('priceRaw') or stock.get('price'))
        high = _to_float(stock.get('highRaw') or stock.get('high'))
        if price > 0 and high > 0:
            drawdown = (high - price) / high * 100
    except Exception:
        pass

    # 1. 已涨停封板 → 次日竞价
    if change_pct >= TREND_LIMIT_UP_THRESHOLD:
        return {
            'type': 'auction', 'label': '次日竞价',
            'note': '今日已封板场内难买入，排队性价比低；次日 9:15-9:25 竞价参与，高开 5% 以上不追，防一字/T字诱多'
        }

    # 2. 强势情绪股 → 早盘
    if role in ('total_leader', 'sector_leader', 'sentiment_vanguard') \
            and change_pct >= BUY_TIMING_STRONG_CHANGE_MIN:
        if market_change <= -0.5:
            return {
                'type': 'early', 'label': '早盘买入',
                'note': f'大盘弱({market_change:+.1f}%)，早盘冲高易回落；轻仓试错或改尾盘确认，严格止损'
            }
        return {
            'type': 'early', 'label': '早盘买入',
            'note': '情绪溢价最高在早盘，尽量 10:00 前决策；只低吸强势回踩，不追直线拉升'
        }

    # 3. 高不确定性 → 尾盘确认
    high_chase = position_label == '高位' and change_pct >= BUY_TIMING_HIGH_CHASE_CHANGE_MIN
    if amplitude >= BUY_TIMING_TAIL_AMPLITUDE_MIN or drawdown >= BUY_TIMING_TAIL_DRAWDOWN_MIN or high_chase:
        if high_chase:
            note = '高位票不追高，谨防接力资金断档；14:30 后确认再介入，弱势直接放弃'
        elif drawdown >= BUY_TIMING_TAIL_DRAWDOWN_MIN:
            note = f'股价已自日内高点回落{drawdown:.1f}%，方向未定；14:30 后确认全天量价形态再介入'
        else:
            note = f'日内振幅{amplitude:.1f}%波动剧烈；14:30 后确认再介入，避开盘中诱多'
        if market_change <= -0.5:
            note += '；大盘弱，宁可错过不可做错'
        return {'type': 'tail', 'label': '尾盘买入', 'note': note}

    # 4. 低位趋势股 → 回踩低吸
    if position_label in ('低位', '中低位') and role in ('core_trend', 'catch_up'):
        return {
            'type': 'pullback', 'label': '回踩低吸',
            'note': '不追高；挂低单等回踩至买入参考价附近，盘中跌破均价线或涨幅转负放弃'
        }

    # 5. 其余 → 盘中灵活
    note = '日内均价线附近介入为宜，避免尾盘急拉时追高'
    if market_change <= -0.5:
        note += '；大盘弱，轻仓参与'
    return {'type': 'intraday', 'label': '盘中灵活', 'note': note}


def calc_buy_sell_points(stock: Dict, score_result: Optional[Dict] = None,
                         trend_details: Optional[Dict] = None,
                         market_change: float = 0.0) -> Dict:
    """计算买入点、当前价、卖出点（V4.3 6级角色分类版）

    V4.1: 基于振幅动态调整买卖比例
    V4.2: 叠加位置高低修正 + 龙头/跟风修正 + 评分置信度修正
    V4.3: 替换二分法为6级角色分类（总龙头/板块龙头/中军/情绪先锋/补涨龙/跟风/杂毛）
          止盈倍率 = 位置倍率 × 角色倍率 × 评分倍率，封顶 15%
          角色判定：按优先级匹配连板高度+涨停次数+市值+换手+位置+涨幅+量比
          位置判定：优先 trend_details.positionPct，fallback base_scores['position']
    V4.5 (2026-10-12): 新增 buyTiming 买入时机判定（次日竞价/早盘/尾盘/回踩低吸/盘中灵活）

    Args:
        stock: 行情数据 dict
        score_result: calc_stock_score_v2 返回值（含 total/base_scores/factor_values）
        trend_details: 趋势扫描 details dict（含 positionPct/maxConsecutiveBoards，可选）
        market_change: 大盘涨跌幅%，用于买入时机的大盘环境修正（可选）
    """
    try:
        price = float(stock.get('priceRaw') or stock.get('price') or 0)
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        return {'buy': '', 'current': '', 'sell': ''}

    change_pct = float(stock.get('changePercent', 0) or 0)
    amplitude = float(stock.get('amplitude', 0) or 0)

    # ===== 提取位置/角色/评分上下文 =====
    position_pct, role, position_label = _classify_stock_role(
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

    # ===== 角色止盈倍率 =====
    role_mult = BUY_SELL_ROLE_TARGET_MULT.get(role, 1.0)

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
        # 角色分级涨停预期：龙头最高，杂毛最低
        if role in ('total_leader', 'sector_leader'):
            if weibi > 80 and 2 <= turnover <= 15:
                base_sell_pct = 0.06   # 龙头封单坚决，预期稳定
            else:
                base_sell_pct = 0.07   # 龙头普通封板
        elif role == 'core_trend':
            base_sell_pct = 0.05       # 中军趋势封板
        elif role == 'sentiment_vanguard':
            base_sell_pct = 0.05       # 情绪先锋封板（波动大，不贪）
        elif role == 'catch_up':
            base_sell_pct = 0.04       # 补涨龙封板
        elif role == 'follower':
            base_sell_pct = 0.03       # 跟风封板
        else:
            base_sell_pct = 0.02       # 杂毛封板（烂板概率高）

        # 位置修正（角色倍率已在 base_sell_pct 中体现，此处只叠位置+评分）
        sell_pct = base_sell_pct * pos_mult * score_mult
        sell_pct = min(sell_pct, BUY_SELL_MAX_TARGET_PCT)

        stock_type = _build_stock_type(role, position_label)
        return {
            'buy': '竞价关注',
            'current': f'{price:.2f}',
            'sell': f'{price * (1 + sell_pct):.2f}',
            'sellPct': round(sell_pct * 100, 1),
            'stockType': stock_type,
            'expectationNote': _build_expectation_note(role, position_label, stock_score, sell_pct),
            'buyTiming': calc_buy_timing(stock, change_pct, amplitude, role,
                                         position_label, stock_score, market_change),
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

    # 止盈倍率 = 位置 × 角色 × 评分
    total_mult = pos_mult * role_mult * score_mult
    sell_pct = min(base_sell_pct * total_mult, BUY_SELL_MAX_TARGET_PCT)

    # 买入回撤受位置影响：低位可以多等（可能还有低点），高位少等（机会稍纵即逝）
    if position_pct <= BUY_SELL_POSITION_LOW_GATE:
        buy_pct = base_buy_pct * 1.3
    elif position_pct >= BUY_SELL_POSITION_HIGH_GATE:
        buy_pct = base_buy_pct * 0.7
    else:
        buy_pct = base_buy_pct

    stock_type = _build_stock_type(role, position_label)
    return {
        'buy': f'{price * (1 - buy_pct):.2f}',
        'current': f'{price:.2f}',
        'sell': f'{price * (1 + sell_pct):.2f}',
        'sellPct': round(sell_pct * 100, 1),
        'buyPct': round(buy_pct * 100, 1),
        'stockType': stock_type,
        'expectationNote': _build_expectation_note(role, position_label, stock_score, sell_pct),
        'buyTiming': calc_buy_timing(stock, change_pct, amplitude, role,
                                     position_label, stock_score, market_change),
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

        # 日内高点回落幅度（2026-10-12）：用于识别急跌途中的误导性信号
        drawdown = 0.0
        try:
            _price = _to_float(stock.get('priceRaw') or stock.get('price'))
            _high = _to_float(stock.get('highRaw') or stock.get('high'))
            if _price > 0 and _high > 0:
                drawdown = (_high - _price) / _high * 100
        except Exception:
            pass

        if volume_ratio > 1.2:
            if drawdown >= RECOMMEND_BOARD_BREAK_DRAWDOWN:
                reasons.append(f'⚠ 放量下跌(量比{volume_ratio:.2f})，自日内高点回落{drawdown:.1f}%，疑似出货')
            else:
                reasons.append(f'量比放大({volume_ratio:.2f})，资金关注')
        if 2 < turnover_rate < 8:
            reasons.append(f'换手率适中({turnover_rate:.2f}%)，交投活跃')
        # 2026-10-12 修复：急跌途中委比高多为抄底挂单堆积造成的假象（万向德农案例：
        # 10:00 炸板下跌中委比仍高达 88.57%，被误读为"买盘强势"），
        # 自高点回落≥3% 时不再输出正面解读，改为回落警示。
        if weibi > 0 and drawdown < 3:
            reasons.append(f'委比为正({weibi:.2f}%)，买盘强势')
        elif weibi > 0:
            reasons.append(f'⚠ 委比{weibi:.2f}%但股价自日内高点回落{drawdown:.1f}%，谨防诱多')
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


def calc_intraday_trajectory(stock: Dict) -> Dict:
    """计算股票当日日内轨迹指标（2026-10-12 新增，防"涨停炸板/冲高回落"陷阱）

    依赖 parse_tencent_batch_data 解析出的 high/low/prevClose/price 字段。

    Returns:
        {
          'high_pct': 日内高点相对昨收涨幅(%),
          'drawdown': 现价相对日内高点回落幅度(%)，负数表示现价创新高,
          'board_break': 曾触涨停且自高点回落≥阈值（炸板）,
          'big_drawdown': 自日内高点回落≥阈值（冲高回落，不限是否涨停）,
          'valid': 数据是否可用（缺 high/prevClose 时不过滤）,
        }
    """
    result = {'high_pct': 0.0, 'drawdown': 0.0, 'board_break': False,
              'big_drawdown': False, 'valid': False}
    try:
        price = _to_float(stock.get('priceRaw') or stock.get('price'))
        high = _to_float(stock.get('highRaw') or stock.get('high'))
        prev_close = _to_float(stock.get('prevCloseRaw') or stock.get('prevClose'))
        if price <= 0 or high <= 0 or prev_close <= 0:
            return result
        result['valid'] = True
        result['high_pct'] = (high - prev_close) / prev_close * 100
        result['drawdown'] = (high - price) / high * 100
        touched_limit_up = result['high_pct'] >= RECOMMEND_LIMIT_UP_PCT
        result['board_break'] = bool(touched_limit_up
                                     and result['drawdown'] >= RECOMMEND_BOARD_BREAK_DRAWDOWN)
        result['big_drawdown'] = result['drawdown'] >= RECOMMEND_MAX_DRAWDOWN_FROM_HIGH
    except Exception:
        pass
    return result


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
        # : 批量预取近期增发公告（30天窗口），打"近期增发"标签
        placement_map = {}
        try:
            placement_map = await asyncio.to_thread(fetch_recent_placements,
                                                    [s.get('symbol', '') for s in all_results], 30)
        except Exception as e:
            print(f'[智能推荐] 增发公告预取失败（降级为无数据）: {e}')
        from services.trend import compute_trend_result
        for stock in all_results:
            sym = stock.get('symbol', '').lower()
            # 近期增发标签：placement_map 的 key 为 6 位纯代码
            stock['placement_info'] = placement_map.get(sym[-6:])
            trend = trend_map.get(sym)  # 趋势扫描已有的结果
            if trend is None:
                # 未进趋势扫描isUp缓存的票（含下跌趋势中的反弹股）现算补齐，
                # 使其参与 calc_stock_score_v2 的死猫反弹门控，避免纯日内分虚高入选。
                trend = await asyncio.to_thread(
                    compute_trend_result, sym,
                    float(stock.get('priceRaw') or stock.get('price') or 0))
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

        # ===== 3.5 半山腰追高惩罚（2026-10-12，基于132条历史推荐复盘） =====
        # 复盘：推荐时涨幅 4.5%~9.8%（未涨停）T+1胜率仅44%、均值-2.31%（死亡区），
        # 而 2~4.5% 为 72%/+3.80%、涨停为 73%/+3.81%。
        # 半山腰=已涨一段但未涨停=接力资金最犹豫的位置，罚分压低其入选概率。
        chase_penalized = 0
        for s in all_results:
            chg = abs(float(s.get('changePercent', 0) or 0))
            if RECOMMEND_CHASE_MIN_CHANGE <= chg < RECOMMEND_CHASE_MAX_CHANGE:
                s['score']['total'] = max(0, s['score']['total'] - RECOMMEND_CHASE_PENALTY)
                s['score']['chase_penalty'] = RECOMMEND_CHASE_PENALTY
                chase_penalized += 1
        if chase_penalized:
            print(f'[智能推荐] 半山腰追高惩罚：{chase_penalized} 只涨幅'
                  f'{RECOMMEND_CHASE_MIN_CHANGE}~{RECOMMEND_CHASE_MAX_CHANGE}%未涨停股票 -{RECOMMEND_CHASE_PENALTY}分')

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

        # ===== 4.6 日内轨迹风控过滤（2026-10-12，防"涨停炸板/冲高回落"陷阱） =====
        # 教训案例：2026-10-12 万向德农(sh600371)高开+4.2%→9:35触涨停16.47→炸板，
        # 10:00推荐时趋势分仍沿用9:30扫描快照的97.6（捕捉到最强瞬间），
        # 综合分71入选；随后股价崩至-6.81%，较买入参考15.09跌13.6%。
        # 趋势分是"扫描时刻"的快照，若候选股当日已从高点大幅回落，说明形态已坏，
        # 无论趋势分/委比多高都不再参与精选推荐（不打炸板股是纪律）。
        filtered_intraday = 0
        if RECOMMEND_INTRADAY_FILTER_ENABLED:
            safe = []
            for s in candidates:
                traj = calc_intraday_trajectory(s)
                if not traj['valid']:
                    safe.append(s)  # 缺 high/prevClose 数据时不误伤
                    continue
                if traj['board_break'] or traj['big_drawdown']:
                    filtered_intraday += 1
                    tags = []
                    if traj['board_break']:
                        tags.append('炸板')
                    if traj['big_drawdown']:
                        tags.append('冲高回落')
                    print(f'[智能推荐] 日内轨迹过滤: {s["name"]}({s["symbol"]}) '
                          f'日内高点{traj["high_pct"]:+.1f}%、现价回落{traj["drawdown"]:.1f}%'
                          f'({"、".join(tags)})，评分{s["score"]["total"]}跳过')
                    continue
                safe.append(s)
            candidates = safe
            if filtered_intraday:
                print(f'[智能推荐] 日内轨迹过滤：跳过 {filtered_intraday} 只炸板/冲高回落股')

        # ===== 4.7 亏损黑名单过滤（2026-10-12） =====
        # 复盘：三祥新材 06-10/06-29 连续两次被推荐、各亏 26%+——同一只票短期内
        # 推荐失败后不应再次出现。近30天推荐且T+1亏损>3% → 不再推荐。
        try:
            from services.recommend_review import get_recent_loser_symbols
            loser_symbols = get_recent_loser_symbols(RECOMMEND_LOSER_LOOKBACK_DAYS,
                                                     RECOMMEND_LOSER_LOSS_THRESHOLD)
        except Exception as e:
            print(f'[智能推荐] 亏损黑名单加载失败（不阻塞推荐）: {e}')
            loser_symbols = set()
        if loser_symbols:
            before = len(candidates)
            candidates = [s for s in candidates if s.get('symbol', '').lower() not in loser_symbols]
            skipped = before - len(candidates)
            if skipped:
                print(f'[智能推荐] 亏损黑名单过滤：跳过 {skipped} 只近{RECOMMEND_LOSER_LOOKBACK_DAYS}天'
                      f'推荐亏损>{RECOMMEND_LOSER_LOSS_THRESHOLD}%的股票')

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
                                       trend_details=(_trend or {}).get('details'),
                                       market_change=market_change)
            # 执行诚实化提示（2026-10-12）：复盘证明系统的edge在低吸价口径，
            # 现价追入会被次日低开吃掉——把真相写在卡片上，管住用户的手。
            # 放进 buyTiming（已随 buy_timing 列持久化，刷新不丢）。
            if RECOMMEND_EXEC_HINT_ENABLED and bsp.get('buyTiming'):
                bsp['buyTiming']['execHint'] = RECOMMEND_EXEC_HINT
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
                'placement_info': r.get('placement_info'),
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
