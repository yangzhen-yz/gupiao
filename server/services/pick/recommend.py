"""recommend.py - split from main.py"""
import asyncio, json, re, requests, time
from typing import List, Dict, Optional
from datetime import datetime
from app.config import (QUERY_DAILY_RECOMMENDATIONS_LIMIT, STRATEGY_INDUSTRY_NEUTRALIZATION_ENABLED, EASTMONEY_HOT_LIST_API, TREND_LIMIT_UP_THRESHOLD, RECOMMEND_INTRADAY_FILTER_ENABLED, RECOMMEND_LIMIT_UP_PCT, RECOMMEND_BOARD_BREAK_DRAWDOWN, RECOMMEND_MAX_DRAWDOWN_FROM_HIGH, BUY_TIMING_TAIL_AMPLITUDE_MIN, BUY_TIMING_TAIL_DRAWDOWN_MIN, BUY_TIMING_HIGH_CHASE_CHANGE_MIN, RECOMMEND_LOSER_LOOKBACK_DAYS, RECOMMEND_LOSER_LOSS_THRESHOLD, RECOMMEND_EXEC_HINT_ENABLED, RECOMMEND_EXEC_HINT, BUY_SELL_POSITION_LOW_GATE, BUY_SELL_POSITION_MIDLOW_GATE, BUY_SELL_POSITION_HIGH_GATE, BUY_SELL_TARGET_POSITION_LOW_MULT, BUY_SELL_TARGET_POSITION_MIDLOW_MULT, BUY_SELL_TARGET_POSITION_HIGH_MULT, BUY_SELL_TARGET_SCORE_HIGH_MULT, BUY_SELL_TARGET_SCORE_LOW_MULT, BUY_SELL_MAX_TARGET_PCT, RECOMMEND_CYCLE_SCHEDULE_ENABLED, RECOMMEND_MIN_SCORE_BASE, CYCLE_MAX_RECOMMEND, CYCLE_ALLOW_EMPTY, CYCLE_EXTREME_HOT_LIMIT_UPS, CYCLE_EXTREME_HOT_MAINLINE_STALE_SEC, RECOMMEND_MARKET_VETO_ENABLED)
from db.database import (load_daily_recommendations, save_daily_recommendations, load_hot_stock_buttons, load_trend_scan_results, load_stock_concept_tags, save_stock_industry)
from services.data.stock import fetch_stock_data, fetch_with_retry, parse_tencent_batch_data
from services.strategy.strategy import calc_stock_score_v2, apply_industry_neutralization
from services.data.utils import to_float as _to_float
from services.strategy.buy_range import (  # noqa: E402  P0-1 执行诚实化：区间与追高警告的唯一算法
    buy_range, chase_warning, over_pct)
from services.strategy.momentum_zone import (  # noqa: E402  B 涨幅段闸门：分区唯一实现
    cfg as zone_cfg, evaluate as zone_evaluate, gate_stock as zone_gate,
    penalty_stock as zone_penalty)
from services.data.valuation import get_valuation_batch
from services.strategy.risk_control import calc_risk_control
from services.data.placement import fetch_recent_placements


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

    # 2026-10-19：接入共享熔断（scope 'ulist'，东财 IP 级风控根治）——
    # 冷却中返回 {}，推荐链路走本地K线库降级，不再并发批量硬打。
    try:
        from services.data import eastmoney_guard
        if eastmoney_guard.skip('ulist'):
            return {}
    except Exception:
        pass

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
        # 2026-10-24 修：批次异常以前被下面的 `continue` 静默吞掉，结果是
        # "页面显示 0/58、日志里一行错误都没有、熔断器也不记账"——冷却永远不触发，
        # 用户每刷一次页面就重新硬打一次东财，反而给封禁续命（10-24 上午实拍：
        # api_usage 记到 ulist 3 次失败，guard 的 ulist streak 却停在昨天的 5 没动）。
        batch_errs = [r for r in batch_results if isinstance(r, Exception)]
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
        # 失败要留痕并交给熔断器记账：全批失败才算这个接口真的不可用（flaky/blocked 由
        # guard 自己分级），部分失败只打印不动熔断——避免一次网络抖动就把接口关 30 分钟
        if batch_errs:
            print(f'[资金流向] 其中 {len(batch_errs)}/{len(batches)} 批失败: '
                  f'{type(batch_errs[0]).__name__}: {batch_errs[0]}')
            if not found:
                try:
                    from services.data import eastmoney_guard
                    eastmoney_guard.fail('ulist', batch_errs[0])
                except Exception:
                    pass
        # 2026-08-31 性能优化：写入内存缓存（TTL 20s）
        _FUND_FLOW_BATCH_CACHE[_ck] = {'ts': time.time(), 'data': found}
        if found:
            # 2026-10-24：ok 会清掉本 scope 的失败计数与冷却，所以只有**过半成功**才算接口恢复。
            # 原来只要有一批返回就记 ok —— 120 只里只拿到 1 只也会把冷却解除，等于自己把熔断关掉
            if len(found) >= len(codes) * 0.5:
                try:
                    from services.data import eastmoney_guard
                    eastmoney_guard.ok('ulist')
                except Exception:
                    pass
        return found
    except Exception as e:
        print(f'[资金流向] 靶向获取失败: {e}')
        try:
            from services.data import eastmoney_guard
            eastmoney_guard.fail('ulist', e)
        except Exception:
            pass
        return {}


def _position_context(score_result: Optional[Dict],
                      trend_details: Optional[Dict]) -> tuple:
    """位置分位与标签（2026-10-28 起只保留位置，不再做角色分类）。

    位置判定：优先 trend_details.positionPct（年度分位 0-100），
    fallback 用 base_scores['position'] 三档近似（≤5 高位 / ≥15 低位 / 其余中位）。
    原先这里还有 6 级角色分类（总龙头/板块龙头/中军/情绪先锋/补涨龙/跟风/杂毛），
    用户 2026-10-28 要求整体删除：止盈、止损、推送、周期都不再读角色。

    Returns: (position_pct: float, position_label: str)
    """
    base_scores = {}
    if score_result and isinstance(score_result, dict):
        base_scores = score_result.get('base_scores', {}) or {}

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
    return position_pct, position_label


def _build_expectation_note(position_label: str, score: float, sell_pct: float) -> str:
    """构建预期说明文字（2026-10-28 起只剩位置+评分口径，不再带角色文案）"""
    parts = []
    if position_label:
        parts.append(position_label)
    if score >= 80:
        parts.append('高分确认')
    elif score < 55:
        parts.append('低分谨慎')
    type_str = ''.join(parts) or '评分中性'
    note = f'{type_str}，预期涨幅{sell_pct*100:.1f}%'
    if position_label == '高位':
        note += '，注意控制仓位'
    elif position_label in ('低位', '中低位'):
        note += '，位置相对安全'
    return note


def calc_buy_timing(stock: Dict, change_pct: float, amplitude: float,
                    position_label: str,
                    score: float = 0.0, market_change: float = 0.0) -> Dict:
    """买入时机判定（2026-10-12）：回答"这只票今天该什么时候买"

    2026-10-28 起不再按角色分档（原"总龙头/板块龙头/情绪先锋 → 早盘买入"整条分支删除），
    只按 涨停封板 / 振幅与回落 / 位置 / 大盘 四条判定：

    1. 已涨停封板    → 次日竞价：当日场内基本买不到，次日 9:15-9:25 竞价参与
    2. 高不确定性    → 尾盘买入：振幅≥5% / 自高点回落≥2% / 高位追高，14:30 后确认全天形态再买
    3. 低位趋势股    → 回踩低吸：低位/中低位，不追高，等回踩至买入参考价
    4. 其余          → 盘中灵活：日内均价线附近介入，不追尾盘急拉

    Args:
        stock: 行情 dict（用 highRaw/priceRaw 算回落幅度）
        change_pct: 当日涨幅%
        amplitude: 当日振幅%
        position_label: 位置标签（低位/中低位/中位/高位）
        score: 综合评分
        market_change: 大盘涨跌幅%（弱市下强势股也降级为谨慎）

    Returns:
        {'type': 'auction'|'tail'|'pullback'|'intraday',
         'label': '次日竞价'|'尾盘买入'|'回踩低吸'|'盘中灵活',
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

    # 2. 高不确定性 → 尾盘确认
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

    # 3. 低位趋势股 → 回踩低吸
    if position_label in ('低位', '中低位'):
        return {
            'type': 'pullback', 'label': '回踩低吸',
            'note': '不追高；挂低单等回踩至买入参考价附近，盘中跌破均价线或涨幅转负放弃'
        }

    # 4. 其余 → 盘中灵活
    note = '日内均价线附近介入为宜，避免尾盘急拉时追高'
    if market_change <= -0.5:
        note += '；大盘弱，轻仓参与'
    return {'type': 'intraday', 'label': '盘中灵活', 'note': note}


_RATES_CACHE: Dict = {'ts': 0.0, 'rates': {}}


def exec_rates(max_age: float = 600.0) -> Dict:
    """复盘实测的胜率（低吸单真实成交口径 / 现价追入口径），带 10 分钟缓存。

    为什么要现取：`RECOMMEND_EXEC_HINT` 那句常量写的是"复盘132次：45%/63%"，
    而复盘样本每天在长（现在 218 条）、口径也在改（1094 选票范围）。
    警告文案里挂一个会过期的数字，等于让用户信一个没人再核对的结论。
    单股分析、行动台、命令行都不传 rates，靠这里拿到同一份数。

    2026-10-18 口径纠偏（根因下钻 `server/analysis_out/docs_backup_20261102/胜率低根因复盘-2026-10-18.md`）：
    纸面"低吸参考价 60.4%"里 44/139 条次日没跌到买入价（挂单不会成交，
    未成交组按构造 100% 胜率）——那个数字不能往卡片上放。现在给三组：
      filledWin/filledAvg/filledN   低吸单真正成交部分的 T+1（42.1%/-1.06%，n=95）
      filledChaseWin                同样这些成交日如果按现价追高的 T+1（25.3%）
                                    —— 两个数的差就是"挂低吸"躲掉的诱多；
      fillRate/dixiEv               低吸价可成交比例与纪律期望（未成交按 0，不幻想）
      chaseWin/chaseAvg/chaseN      现价追入口径（覆盖全部推荐，不只数值买入价那些）
    """
    import time as _t
    now = _t.time()
    if now - _RATES_CACHE['ts'] < max_age and _RATES_CACHE['rates']:
        return _RATES_CACHE['rates']
    r = {}
    try:
        from services.pick.recommend_review import get_review_summary
        s = get_review_summary(120) or {}
        tf = s.get('t1_filled') or {}
        ef = s.get('exec_t1_filled') or {}
        ex = s.get('exec_t1') or {}
        r = {'dixiWin': tf.get('win_rate'),
             'dixiAvg': tf.get('avg'),
             'dixiN': tf.get('n'),
             'filledChaseWin': ef.get('win_rate'),
             'fillRate': s.get('fill_rate'),
             'dixiEv': s.get('dixi_ev'),
             'chaseWin': ex.get('win_rate'),
             'chaseAvg': ex.get('avg'),
             'chaseN': ex.get('n')}
    except Exception as e:
        print(f'[执行诚实化] 复盘实测数没拿到（文案退回文档口径）: {type(e).__name__}: {e}')
    _RATES_CACHE.update({'ts': now, 'rates': r})
    return r


def _vol5_for_stock(stock: Dict) -> Optional[float]:
    """单票近 5 日已实现波动率（%），供 buy_range 算动态带宽。

    三条纪律：
      · 开关没开就直接返回 None，连本地库都不查——关掉的代价必须等于改造前，一行多余查询也不该有；
      · 数据只来自本地日K库那份带 TTL 的全市场缓存，**零外部请求**（10-19 刚做完降频专项，
        不能为了算波动率再开一条每轮打接口的路）；
      · 取不到（库未回填、新上市/停牌样本不足）返回 None，buy_range 会退回核心带宽。
        这里故意不打日志：它在批量链路里逐票调用，一次盘后同步晚到就能刷几百行。
    """
    try:
        from app.config import BUY_RANGE_DYNAMIC_ENABLED
        from services.support import app_settings
        enabled = app_settings.value('buy_band_dynamic_enabled') \
            if 'buy_band_dynamic_enabled' in [s['key'] for s in app_settings.SPEC] \
            else BUY_RANGE_DYNAMIC_ENABLED
        if not enabled:
            return None
        from services.data.kline_history import get_vol_closes_for
        from services.strategy.buy_range import recent_volatility
        return recent_volatility(get_vol_closes_for(stock.get('symbol') or ''))
    except Exception:
        return None


def _apply_exec_honesty(stock: Dict, bsp: Dict, rates: Optional[Dict] = None,
                        vol5: Optional[float] = None) -> Dict:
    """把"低吸区间 / 追价上限 / 现价超了多少"按票算出来，挂进 buyTiming（2026-10-18，P0-1）。

    以前这里是一句全局常量 `RECOMMEND_EXEC_HINT`："建议低吸或尾盘介入，不追盘中高价"。
    问题是它对所有票都说同一句话，而"现在这只票的现价到底比买入上限高了多少"没人算 ——
    实测现价追入口径 T+1 胜率 46.8%，低吸单真正成交的口径 42.1%（纸面 60.4% 里
    三成是未成交的幻觉胜利，见 `server/analysis_out/docs_backup_20261102/胜率低根因复盘-2026-10-18.md`），
    光靠一句劝不住。现在按票给出具体数字，超价 ≥3% 直接写"不建议买入"。

    算不出来（行情缺字段、价格非法）就退回全局常量文案，推荐链路绝不因为文案崩掉。
    """
    try:
        timing = bsp.get('buyTiming')
        rates = rates or exec_rates()           # 单股分析/命令行调用不传 rates，这里自取一份
        price = _to_float(stock.get('priceRaw') or stock.get('price'))
        if not timing or not price or price <= 0:
            return bsp
        ref_buy = _to_float(bsp.get('buy'))            # '竞价关注' 这类非数值 → 0
        lim_up = _to_float(stock.get('changePercent')) >= TREND_LIMIT_UP_THRESHOLD
        # 近 5 日波动率：批量调用方（智能推荐/盘中推送）会传进来，单股分析与命令行调用
        # 没传时自取一次——取的是本地日K库那份带 TTL 的全市场缓存，不产生任何外部请求。
        if vol5 is None:
            vol5 = _vol5_for_stock(stock)
        rng = buy_range(price, _to_float(stock.get('amplitude')),
                        ref_buy=ref_buy or None, timing_type=timing.get('type') or 'intraday',
                        limit_up=lim_up, code=stock.get('symbol') or '', vol5=vol5)
        timing.update({'buyLow': rng['buyLow'], 'buyHigh': rng['buyHigh'],
                       'limitPrice': rng['limitPrice'], 'chasePct': rng['chasePct'],
                       'buyNote': rng['buyNote'],
                       'overPct': over_pct(price, rng['buyHighRaw'])})
        # 动态带宽是**并列的影子口径**：核心那套键一个字不改，扩展信息只进新键。
        # dynEnabled=False（开关关着或波动率取不到）时这些键等于核心值，行为与改造前一致。
        timing.update({'buyHighDyn': rng['buyHighDyn'], 'bandPct': rng['bandPct'],
                       'vol5': rng['vol5'], 'overPctDyn': over_pct(price, rng['buyHighDynRaw']),
                       'dynEnabled': rng['dynEnabled'], 'dynNote': rng['dynNote']})
        if RECOMMEND_EXEC_HINT_ENABLED:
            warn = chase_warning(price, rng['buyHighRaw'], rates)
            if warn:
                timing['execHint'] = warn
            elif rates.get('dixiWin') is not None and rates.get('chaseWin') is not None:
                # 没超价也要说清"为什么默认挂低吸"：用当天复盘的实测数，不抄旧常量。
                # 2026-10-18 口径纠偏：纸面"低吸 60.4%"里三成是未成交的幻觉胜利，
                # 卡片只放真正成交口径，并列"成交日追高只有 filledChaseWin%"——
                # 挂低吸的意义是躲诱多，不是保证赚（`server/analysis_out/docs_backup_20261102/胜率低根因复盘-2026-10-18.md`）。
                # 两个附属数字可能缺（复盘表没算出来），缺就不写，不许出现"None%"。
                _fill = rates.get('fillRate')
                _fill_txt = f'，成交率 {_fill}%' if _fill is not None else ''
                _fcw = rates.get('filledChaseWin')
                _fcw_txt = (f'；同样这些日子按现价追高只有 {_fcw}%——挂低吸是躲诱多，'
                            f'不是保证赚' if _fcw is not None else '')
                timing['execHint'] = (
                    f'现价还在买入区间内（超价 {timing.get("overPct")}%），可以按纪律买。'
                    f'复盘实测：低吸单真正成交 T+1 胜率 {rates["dixiWin"]}%'
                    f'/均值 {rates.get("dixiAvg")}%（{rates.get("dixiN")}条成交{_fill_txt}）'
                    f'{_fcw_txt}。现价追入全样本 {rates["chaseWin"]}%'
                    f'/均值 {rates.get("chaseAvg")}%（{rates.get("chaseN")}条），'
                    f'盘中冲高时别改主意')
            else:
                timing['execHint'] = RECOMMEND_EXEC_HINT
        # 打板票的默认出口径（写进卡片，不留给临场决定）：`docs/短线买法-2026-10.md` §2
        # 实测过"封板票怎么卖"，次日开盘即走是均值最高、尾部最薄的卖法。
        # 2026-11-05 改文案（阈值与动作都没动）：那 +2.0% 条件在"到收盘还封着"上，
        # 摸板就买的整体期望是 −0.28%（`analysis_out/sealed_board_selection_check.py` L5），
        # 所以卡片先讲前提再讲数字——旧文案写 +2.05%/64.6% 是 10-18 那版的 1094 口径数。
        if timing.get('type') == 'auction':
            timing['exitNote'] = (
                '默认出口径：次日 9:15-9:25 竞价挂卖、开盘即走。'
                '+2.0% 只在"到收盘还封着"时才拿得到（封板票次日开盘卖 +2.01%/胜率 64.3%）；'
                '摸板就买整体 −0.28%（32.1% 当天炸板、那批 −5.15%）——确认封死才买。'
                '持有到次日收盘只 +1.97%/59.3%，亏超 5% 涨到 11.4%。'
                '竞价高开超过 5% 不追（高开 +5~8% 追板持有到次日收盘 -0.11%/46.5%、亏超 5% 30.7%）')
    except Exception as e:
        print(f'[执行诚实化] 按票计算失败（回落全局文案）: {type(e).__name__}: {e}')
    return bsp


def calc_buy_sell_points(stock: Dict, score_result: Optional[Dict] = None,
                         trend_details: Optional[Dict] = None,
                         market_change: float = 0.0) -> Dict:
    """计算买入点、当前价、卖出点（V4.6，2026-10-28 删除角色体系）

    V4.1: 基于振幅动态调整买卖比例
    V4.2: 叠加位置高低修正 + 评分置信度修正
    V4.3: 曾改为 6 级角色分类 × 角色倍率（2026-10-28 整体删除，见下）
    V4.5 (2026-10-12): 新增 buyTiming 买入时机判定（次日竞价/尾盘/回踩低吸/盘中灵活）
    V4.6 (2026-10-28): 角色体系下线，止盈倍率 = 位置倍率 × 评分倍率（不再乘角色倍率）；
          涨停股基准统一 4%（原七档 2%~7% 的中值），**这个数是占位不是验证结果**：
          真正的封板强度（封单额/流通市值、首次涨停时间、开板次数）目前没有数据源，
          待补齐后再做分档，届时用影子对照决定是否替换。
          位置判定：优先 trend_details.positionPct，fallback base_scores['position']

    Args:
        stock: 行情数据 dict
        score_result: calc_stock_score_v2 返回值（含 total/base_scores/factor_values）
        trend_details: 趋势扫描 details dict（含 positionPct，可选）
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

    # ===== 提取位置/评分上下文 =====
    position_pct, position_label = _position_context(score_result, trend_details)

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

    # ===== 评分置信度止盈倍率 =====
    if stock_score >= 80:
        score_mult = BUY_SELL_TARGET_SCORE_HIGH_MULT
    elif stock_score < 55:
        score_mult = BUY_SELL_TARGET_SCORE_LOW_MULT
    else:
        score_mult = 1.0

    # ===== 涨停板特殊处理 =====
    if change_pct >= TREND_LIMIT_UP_THRESHOLD:
        # 4% 是原七档角色基准的中值占位；封板质量数据（封单比/涨停时间/开板次数）
        # 补齐前不做分档，避免再拿一个没验证的映射去乘价格。
        base_sell_pct = 0.04
        sell_pct = min(base_sell_pct * pos_mult * score_mult, BUY_SELL_MAX_TARGET_PCT)

        return _apply_exec_honesty(stock, {
            'buy': '竞价关注',
            'current': f'{price:.2f}',
            'sell': f'{price * (1 + sell_pct):.2f}',
            'sellPct': round(sell_pct * 100, 1),
            'expectationNote': _build_expectation_note(position_label, stock_score, sell_pct),
            'buyTiming': calc_buy_timing(stock, change_pct, amplitude,
                                         position_label, stock_score, market_change),
        })

    # ===== 非涨停板：振幅基础 + 两维修正 =====
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

    # 止盈倍率 = 位置 × 评分
    total_mult = pos_mult * score_mult
    sell_pct = min(base_sell_pct * total_mult, BUY_SELL_MAX_TARGET_PCT)

    # 买入回撤受位置影响：低位可以多等（可能还有低点），高位少等（机会稍纵即逝）
    if position_pct <= BUY_SELL_POSITION_LOW_GATE:
        buy_pct = base_buy_pct * 1.3
    elif position_pct >= BUY_SELL_POSITION_HIGH_GATE:
        buy_pct = base_buy_pct * 0.7
    else:
        buy_pct = base_buy_pct

    return _apply_exec_honesty(stock, {
        'buy': f'{price * (1 - buy_pct):.2f}',
        'current': f'{price:.2f}',
        'sell': f'{price * (1 + sell_pct):.2f}',
        'sellPct': round(sell_pct * 100, 1),
        'buyPct': round(buy_pct * 100, 1),
        'expectationNote': _build_expectation_note(position_label, stock_score, sell_pct),
        'buyTiming': calc_buy_timing(stock, change_pct, amplitude,
                                     position_label, stock_score, market_change),
    })


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
                # 2026-11-10：原文案「已涨停，次日竞价关注」与 push.py:472「已涨停，不推」冲突，
                # 且 11-04 实测当天摸板按涨停价买整体 −0.28%（扣费 −0.41%），属负期望动作。
                # 只改展示文案，不动评分、选票与推送判据。
                reasons.append('已封板，按推送规则不追（仅观察）')
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


SHADOW_EVAL_POOL = 15  # 影子精选只对分数靠前这么多只做位置/买卖点复算，避免整天任务变重


def _ensure_shadow_table(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS recommend_shadow_log (
        rec_date  TEXT NOT NULL,
        slot      TEXT NOT NULL,
        cycle     TEXT,
        min_score INTEGER,
        payload   TEXT,
        PRIMARY KEY (rec_date, slot)
    )''')


def _shadow_item(s, bsp):
    return {'symbol': (s.get('symbol') or '').lower(), 'name': s.get('name'),
            'score': (s.get('score') or {}).get('total'),
            'changePercent': s.get('changePercent'),
            'volumeRatio': s.get('volumeRatio'),
            'timing': ((bsp or {}).get('buyTiming') or {}).get('label') or '',
            'tag': s.get('primary_tag') or '其他'}


def _prev_bars(symbols):
    """从本地日K取每只票**上一交易日**的涨幅与是否封板（今天这根还没同步，所以最新那根=昨天）。

    矩阵里有几条规则是"看昨天"的（昨日封板不接力、昨日大面可竞价低吸），
    而推荐链路手里的行情只有今天 —— 不补这一步，那些规则永远命中不了。
    """
    out = {}
    try:
        import sqlite3
        from app.config import KLINE_HISTORY_DB_PATH
        codes = {(x or '')[-6:] for x in symbols if x}
        if not codes:
            return out
        conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=30)
        try:
            for code in codes:
                rows = conn.execute('SELECT date, close FROM stock_daily WHERE symbol=? '
                                    'ORDER BY date DESC LIMIT 3', (code,)).fetchall()
                if len(rows) < 2:
                    continue
                prev_close, last_close = rows[1][1], rows[0][1]
                if not prev_close:
                    continue
                chg = (last_close / prev_close - 1) * 100
                lim = 0.20 if code.startswith(('30', '68')) else 0.10
                out[code] = {'changePercent': round(chg, 2),
                             'limitClose': chg >= lim * 98.5,
                             'barDate': rows[0][0]}
        finally:
            conn.close()
    except Exception as e:
        print(f'[影子矩阵] 读上一交易日K线失败（矩阵会少两条规则）: {e}')
    return out


def _attach_matrix(items, sent):
    """给影子清单每只票加一个 matrix 标签（只落库，不改线上时机文案）。

    两周后拿 timing 与 matrix 各自对 T+1 实际结果，看哪套更少让人追高，再决定换不换。
    """
    from services.strategy.buy_rules import classify
    prev = _prev_bars([i.get('symbol') for i in items])
    market = {'limitUp': (sent or {}).get('limitUp')}
    for it in items:
        code = (it.get('symbol') or '')[-6:]
        try:
            it['matrix'] = classify({'changePercent': it.get('changePercent'),
                                     'volumeRatio': it.get('volumeRatio')},
                                    prev.get(code), market)
        except Exception as e:
            it['matrix'] = {'verdict': '未判定', 'why': str(e)[:80]}
    return items


def _cycle_info(gate, cycle_name, cycle_max, cash):
    """P0-2（2026-10-24）：给推荐记录附"今日模式"信息，前端常驻横幅的数据源。
    gate=None（调度关或情绪获取失败）时返回体仍完整，只是规则字段为空。"""
    try:
        from app.config import CYCLE_RULES as _cr
        _note = (_cr.get(cycle_name) or {}).get('note', '')
    except Exception:
        _note = ''
    _g = gate or {}
    return {
        'cycle': cycle_name,
        'note': _note,
        'maxRecommend': int(cycle_max),
        'vetoEnabled': bool(_g.get('vetoEnabled')),
        'minScore': _g.get('minScore') if _g.get('vetoEnabled') else None,
        'allowChase': bool(_g.get('allowChase', True)),
        'cash': bool(cash),
    }


async def _log_shadow_selection(now, date_key, candidates, live_top3, trend_map,
                               market_change, live_min_score, zone_dropped=None):
    """影子精选：同一份候选池，按"情绪周期门槛 + 不追高"再选一遍，只落库不替换线上。

    2026-10-28 起删掉"角色否决"这一条（角色体系整体下线），影子与线上都只剩
    min_score 与不追高两条市场级规则；两天结果的差集仍可直接回答：
      · 被挡掉的票 T+1 是不是真的更差（证明门槛有效）；
      · 影子多推的票 T+1 是不是真的更好（证明门槛有效）。

    zone_dropped：涨幅段闸门（drop 模式）从候选里整只拦掉的票。并进 excluded 组，
    让 shadow_eval 给它们算 T+1 —— 被闸的票不是消失，是换个组别继续接受检验。
    """
    from db.database import get_db_conn
    from services.strategy.sentiment import get_sentiment, cycle_gate
    from services.strategy.buy_rules import MATRIX_VERSION   # payload 里要写版本号，先导好

    sent = await get_sentiment()
    gate = cycle_gate(sent)
    # 影子用 advisory 口径（不受"过 10:00 才拦"的限制）：影子只是记录，不是执行
    min_score = gate.get('advisoryMinScore') or 60
    allow_chase = gate.get('advisoryChase', True)
    cycle = sent.get('cycle')

    pool = candidates[:SHADOW_EVAL_POOL]
    kept, excluded = [], []
    bsp_by_sym = {}      # 线上 Top3 的时机标签要从这里取：candidates 本身不带 buySellPoints
    for s in pool:
        sym = (s.get('symbol') or '').lower()
        _t = trend_map.get(sym) or {}
        try:
            bsp = calc_buy_sell_points(s, score_result=s.get('score'),
                                       trend_details=(_t or {}).get('details'),
                                       market_change=market_change)
        except Exception:
            bsp = {}
        total = (s.get('score') or {}).get('total') or 0
        chg = float(s.get('changePercent') or 0)
        reasons = []
        if total < min_score:
            reasons.append(f'低于{cycle}门槛{min_score}（现{total:.0f}分）')
        if not allow_chase and chg >= 5:
            reasons.append(f'{cycle}·涨幅{chg:.1f}%不追高')
        bsp_by_sym[(s.get('symbol') or '').lower()] = bsp
        item = _shadow_item(s, bsp)
        if reasons:
            item['reasons'] = reasons
            excluded.append(item)
        else:
            kept.append(item)

    # 与线上同一套"概念分散取前 3"，否则两边差异会混进选法差异而不是门槛差异
    shadow_top, seen_tags = [], set()
    for s in kept:
        if s['tag'] not in seen_tags:
            shadow_top.append(s)
            seen_tags.add(s['tag'])
        if len(shadow_top) >= 3:
            break
    if len(shadow_top) < 3:
        shadow_top.extend([s for s in kept if s not in shadow_top][:3 - len(shadow_top)])

    # 涨幅段闸门挡掉的票并入 excluded：reasons 写清落在哪一段、当日涨幅多少，
    # 之后 shadow_eval 对这组算 T+1，就是"闸门该不该留"的反事实证据。
    for s in (zone_dropped or []):
        _z = s.get('zone') or {}
        try:
            _bsp = calc_buy_sell_points(s, score_result=s.get('score'),
                                        trend_details=((trend_map.get((s.get('symbol') or '').lower())) or {}).get('details'),
                                        market_change=market_change)
        except Exception:
            _bsp = {}
        _item = _shadow_item(s, _bsp)
        _item['reasons'] = [f'涨幅段闸门·{_z.get("label") or "死亡区"}'
                            f'（当日涨幅 {float(s.get("changePercent") or 0):+.1f}%）']
        excluded.append(_item)
    if zone_dropped:
        print(f'[影子精选] 涨幅段闸门挡掉 {len(zone_dropped)} 只并入 excluded，'
              f'shadow_eval 将自动跟踪其 T+1 表现')

    payload = {
        'generatedAt': now.isoformat(timespec='seconds'),
        'matrixVersion': MATRIX_VERSION,
        'liveMinScore': live_min_score,
        'shadowMinScore': min_score,
        'cycle': cycle,
        'cycleScore': sent.get('cycleScore'),
        'sentimentDetail': sent.get('detail'),
        'stale': bool(sent.get('stale')),
        'live': [_shadow_item(s, bsp_by_sym.get((s.get('symbol') or '').lower()))
                 for s in live_top3],
        'shadow': shadow_top,
        'kept': kept,          # 影子会放行名单（快照要区分"放行"与"没评估"）
        'excluded': excluded,
    }
    # 矩阵标签只挂到影子里（线上 timing 一字未改）：先并行两周攒对照。
    # 必须排在 INSERT 之前 —— 挂晚了 payload 落库时 matrix 字段是空的，对照数据就白攒了。
    try:
        _attach_matrix(payload['live'], sent)
        _attach_matrix(payload['shadow'], sent)
        _attach_matrix(payload['excluded'], sent)
        _gap = [f"{x['name']}：线上「{x['timing'] or '无'}」vs 矩阵「{x['matrix']['verdict']}」"
                f"（{x['matrix']['why']}）" for x in payload['live'] if x.get('matrix')]
        if _gap:
            print('[影子矩阵] ' + '；'.join(_gap))
    except Exception as e:
        print(f'[影子矩阵] 标签附加失败（不影响本次推荐）: {type(e).__name__}: {e}')

    conn = get_db_conn()
    try:
        _ensure_shadow_table(conn)
        conn.execute('INSERT OR REPLACE INTO recommend_shadow_log '
                     '(rec_date, slot, cycle, min_score, payload) VALUES (?,?,?,?,?)',
                     (date_key, now.strftime('%H:%M'), cycle, int(min_score),
                      json.dumps(payload, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()
    diff = '、'.join(x['name'] for x in payload['excluded']) or '无'
    print(f'[智能推荐] 影子精选（{cycle}·门槛{min_score}）：'
          f'线上{len(live_top3)}只 / 影子{len(shadow_top)}只，被挡 {diff}')
    # 返回 payload 给决策快照用：timing/matrix 标签在这里才算得出来，
    # 快照再自己跑一遍 buy_sell_points 既费时间又可能和影子口径对不上。
    return payload


# ===== 影子闸门四维调整（2026-10-31，只记录、不生效）=====
# 在现有闸门链之外另算一遍"市场情绪 + 个股历史 + 行业特征 + 主线题材"的加减分，
# 把 base→shadow 与 keep/drop 落库，攒够样本后拿 T+1 实测裁决方向对不对。
# 硬约束：不改 MIN_RECOMMEND_SCORE、candidates、top3、推送、快照的任何字段；
#        本模块内任何异常只打日志，绝不冒泡影响推荐。
#
# 幅度是**先验设定，未做参数寻优**，不是实测最优值。影子期的任务就是检验方向：
# 若"影子留"的 T+1 显著好于"影子挡"，再谈给每维分配正式权重。
SHADOW_GATE_LOG_ENABLED = True
SHADOW_GATE_MAX_DELTA = 15.0   # 四维合计上限，防止影子分把线上分改得面目全非
_SG_SENT_DELTA = {'冰点': -6.0, '退潮': -5.0, '分歧': -2.0,
                  '回暖': 0.0, '发酵': 3.0, '高潮': 2.0}
_SG_DIM_LABEL = {'sentiment': '情绪', 'history': '历史',
                 'industry': '行业', 'mainline': '主线'}


def _sg_industry_stats(results, industry_map):
    """行业当日平均涨幅（只用本批扫描结果，n 一并记下当样本量）。

    口径限制：这是"推荐批次扫描到的票"的行业均值，不是全市场行业指数涨幅；
    n<3 的行业在下游按样本不足处理，调整量记 0。
    """
    agg = {}
    for s in results or []:
        ind = (industry_map or {}).get((s.get('symbol') or '').lower()) or ''
        if not ind:
            continue
        try:
            chg = float(s.get('changePercent') or 0)
        except (TypeError, ValueError):
            continue
        b = agg.setdefault(ind, {'n': 0, 'sum': 0.0})
        b['n'] += 1
        b['sum'] += chg
    return {k: {'n': v['n'], 'avg': round(v['sum'] / v['n'], 2)} for k, v in agg.items()}


def _sg_name_hit(ticker_names, sector_names):
    """题材/行业名与主线板块名的粗匹配：互相包含或共享 ≥2 个汉字。

    行业分类（申万/国标）与东财板块名不是同一套口径，这里只能粗匹配；
    命中与否都要把板块名写进 why —— 匹配失败不等于"不在主线"，
    这个误判率本身也是影子期要观察的指标。
    """
    for a in ticker_names:
        a = (a or '').strip()
        if len(a) < 2:
            continue
        for b in sector_names:
            b = (b or '').strip()
            if len(b) < 2:
                continue
            if a in b or b in a:
                return b
            if len(set(a) & set(b)) >= 2:
                return b
    return ''


def shadow_gate_adjustment(stock: Dict, ctx: Dict) -> Dict:
    """四维影子闸门调整（纯函数，不改动传入的任何对象）。

    参数 ctx：
      sentiment       get_sentiment() 返回体（只用 cycle / cycleScore / stale）
      mainline        get_mainline() 或 peek_mainline_cached() 返回体（只用 main 名单）
      industry_map    {symbol(小写): 行业名}
      industry_stats  _sg_industry_stats() 的结果
      loser_map       recommend_review.get_loser_map() 的结果
      hist_count      {symbol: 近 30 天被推荐次数}
      min_score       本批线上门槛（只用于和影子分对比，不参与调整）

    返回：base / delta / shadow / dims / verdict，verdict 仅供对照，不驱动任何下单。
    """
    sym = (stock.get('symbol') or '').lower()
    base = float((stock.get('score') or {}).get('total') or 0)
    try:
        chg = float(stock.get('changePercent') or 0)
    except (TypeError, ValueError):
        chg = 0.0
    dims = {}

    # 1) 市场情绪：周期档直接给方向；盘中 stale / 未知档不调（宁可不错，不可乱调）。
    #    收盘定版（final=True）的 stale 只是"不做盘中否决"的纪律标记，不是数据坏了，
    #    与 mainline._sync_score 的 senti_stale 同口径 —— 否则周末/盘后样本永远记 0。
    sent = ctx.get('sentiment') or {}
    cyc = sent.get('cycle') or '未知'
    if (sent.get('stale') and not sent.get('final')) or cyc not in _SG_SENT_DELTA:
        dims['sentiment'] = {'delta': 0.0,
                             'why': f'情绪不可用或未知档({cyc}'
                                    f'{"/stale" if sent.get("stale") else ""})'}
    else:
        dims['sentiment'] = {'delta': _SG_SENT_DELTA[cyc],
                             'why': f'{cyc}·{sent.get("cycleScore")}'
                                    f'{"·定版" if sent.get("final") else ""}'}

    # 2) 个股历史：禁推期内最重；近 30 天重复推荐次之（重复推是设计，但连推 3 次要扣）
    lm = (ctx.get('loser_map') or {}).get(sym)
    hist = int((ctx.get('hist_count') or {}).get(sym) or 0)
    if lm:
        d = -8.0
        why = (f'近30天被推荐后{lm.get("basis")}{lm.get("worst")}%，'
               f'禁推至{lm.get("until")}')
    elif hist >= 3:
        d, why = -3.0, f'近30天已被推荐{hist}次'
    elif hist == 2:
        d, why = -1.5, '近30天已被推荐2次'
    elif hist == 0:
        d, why = 0.0, '近30天未被推荐'
    else:
        d, why = 0.0, '近30天已被推荐1次'
    dims['history'] = {'delta': d, 'why': why}

    # 3) 行业特征：先看行业当日均值强弱，再看个股相对行业的超额（同涨但自身弱=跟风）
    ind = (ctx.get('industry_map') or {}).get(sym) or ''
    ist = (ctx.get('industry_stats') or {}).get(ind)
    if not ind:
        dims['industry'] = {'delta': 0.0, 'why': '行业未知'}
    elif not ist or ist.get('n', 0) < 3:
        dims['industry'] = {'delta': 0.0,
                            'why': f'{(ist or {}).get("n", 0)}只样本不足'}
    else:
        avg = float(ist.get('avg') or 0)
        if avg >= 2:
            d = 3.0
        elif avg >= 0.5:
            d = 1.5
        elif avg >= -0.5:
            d = 0.0
        elif avg >= -2:
            d = -2.0
        else:
            d = -3.5
        ex = chg - avg
        if ex >= 2:
            d += 1.0
        elif ex <= -2:
            d -= 1.5
        dims['industry'] = {'delta': round(d, 1),
                            'why': f'均值{avg:+.2f}%(n={ist["n"]})，本票超额{ex:+.2f}%'}

    # 4) 主线题材：只认 main 名单（sub/候选不算主线）；今日无主线时本维记 0
    ml = ctx.get('mainline') or {}
    mains = [m.get('sector') for m in (ml.get('main') or []) if m.get('sector')]
    if ml.get('disabled') or not mains:
        dims['mainline'] = {'delta': 0.0, 'why': '今日无主线（空档）'}
    else:
        hit = _sg_name_hit([ind, stock.get('primary_tag') or ''], mains)
        if hit:
            dims['mainline'] = {'delta': 5.0, 'why': f'属主线「{hit}」'}
        else:
            dims['mainline'] = {'delta': -2.0, 'why': f'不在主线（{"、".join(mains[:2])}）'}

    delta = round(sum(v['delta'] for v in dims.values()), 1)
    delta = max(-SHADOW_GATE_MAX_DELTA, min(SHADOW_GATE_MAX_DELTA, delta))
    shadow = round(base + delta, 1)
    min_score = float(ctx.get('min_score') or 60)
    return {
        'symbol': sym,
        'name': stock.get('name'),
        'base': round(base, 1),
        'delta': delta,
        'shadow': shadow,
        'dims': dims,
        'baseKeep': base >= min_score,
        'shadowKeep': shadow >= min_score,
        'verdict': 'keep' if shadow >= min_score else 'drop',
    }


def _ensure_shadow_gate_table(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS shadow_gate_adjust_log (
        rec_date     TEXT NOT NULL,
        slot         TEXT NOT NULL,
        symbol       TEXT NOT NULL,
        name         TEXT,
        base_score   REAL,
        delta        REAL,
        shadow_score REAL,
        online_kept  INTEGER,
        online_top3  INTEGER,
        verdict      TEXT,
        dims         TEXT,
        PRIMARY KEY (rec_date, slot, symbol)
    )''')


async def _log_shadow_gate_adjustment(now, date_key, pool, zone_dropped, top3,
                                      min_score, results, industry_map):
    """取上下文 → 逐只算四维调整 → 落 shadow_gate_adjust_log + 打日志。

    评估对象 = 过基础门槛的票 ∪ 被涨幅段闸门整只拦掉的票：
    前者能回答"该不该留"，后者能回答"闸门拦得对不对"，两组都要有 T+1 才能对比。
    """
    if not SHADOW_GATE_LOG_ENABLED:
        return
    try:
        from datetime import timedelta
        from db.database import get_db_conn
        from services.strategy.sentiment import get_sentiment
        from services.strategy.mainline import peek_mainline_cached, get_mainline
        from services.pick.recommend_review import get_loser_map

        sent = await get_sentiment()
        ml = peek_mainline_cached(max_age_sec=7200)
        if ml is None:
            try:
                ml = await get_mainline()
            except Exception as e:
                ml = {}
                print(f'[影子闸门] 主线读取失败（本维记 0）: {type(e).__name__}: {e}')
        try:
            loser_map = get_loser_map()
        except Exception as e:
            loser_map = {}
            print(f'[影子闸门] 亏损黑名单读取失败（本维按 0 次）: {type(e).__name__}: {e}')

        # 近 30 天推荐次数：用与亏损黑名单同一份推荐记录，口径好对账
        hist = {}
        try:
            cutoff = (now - timedelta(days=30)).date().isoformat()
            for rec in (load_daily_recommendations() or []):
                if str(rec.get('date') or '') < cutoff:
                    continue
                for it in (rec.get('daily_recommendations') or []):
                    _s = (it.get('symbol') or '').lower()
                    if _s:
                        hist[_s] = hist.get(_s, 0) + 1
        except Exception as e:
            print(f'[影子闸门] 历史推荐统计失败（本维按 0 次）: {type(e).__name__}: {e}')

        ctx = {
            'sentiment': sent,
            'mainline': ml or {},
            'industry_map': industry_map or {},
            'industry_stats': _sg_industry_stats(results, industry_map),
            'loser_map': loser_map,
            'hist_count': hist,
            'min_score': min_score,
        }

        seen, targets = set(), []
        for s in list(pool or []) + list(zone_dropped or []):
            _sym = (s.get('symbol') or '').lower()
            if not _sym or _sym in seen:
                continue
            seen.add(_sym)
            targets.append(s)
        if not targets:
            print('[影子闸门] 本批无可评估标的（候选为空）')
            return

        kept_syms = {(s.get('symbol') or '').lower() for s in (pool or [])}
        top3_syms = {(s.get('symbol') or '').lower() for s in (top3 or [])}
        rows = []
        for s in targets:
            r = shadow_gate_adjustment(s, ctx)
            r['onlineKept'] = r['symbol'] in kept_syms
            r['onlineTop3'] = r['symbol'] in top3_syms
            rows.append(r)

        conn = get_db_conn(wait_for_lock=True)
        try:
            _ensure_shadow_gate_table(conn)
            for r in rows:
                conn.execute(
                    'INSERT OR REPLACE INTO shadow_gate_adjust_log '
                    '(rec_date, slot, symbol, name, base_score, delta, shadow_score, '
                    ' online_kept, online_top3, verdict, dims) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (date_key, now.strftime('%H:%M'), r['symbol'], r['name'],
                     r['base'], r['delta'], r['shadow'],
                     1 if r['onlineKept'] else 0, 1 if r['onlineTop3'] else 0,
                     r['verdict'], json.dumps(r['dims'], ensure_ascii=False)))
            conn.commit()
        finally:
            conn.close()

        flip = [r for r in rows if r['shadowKeep'] != r['onlineKept']]
        for r in flip:
            print(f'[影子闸门] {r["name"]}({r["symbol"]}) {r["base"]:.0f}→{r["shadow"]:.0f}'
                  f'({r["delta"]:+.1f}) 线上{"留" if r["onlineKept"] else "挡"}'
                  f' / 影子{"留" if r["shadowKeep"] else "挡"} | '
                  + '；'.join(f'{_SG_DIM_LABEL.get(k, k)}{v["delta"]:+.1f}'
                             for k, v in r['dims'].items()))
        print(f'[影子闸门] {now.strftime("%H:%M")} {sent.get("cycle")}·门槛{min_score:.0f}：'
              f'评估{len(rows)}只，线上候选留{sum(1 for r in rows if r["onlineKept"])}只，'
              f'影子留{sum(1 for r in rows if r["shadowKeep"])}只，判定翻转{len(flip)}只 '
              f'（已落 shadow_gate_adjust_log）')
    except Exception as e:
        print(f'[影子闸门] 计算/落库失败（不影响线上推荐）: {type(e).__name__}: {e}')


async def auto_generate_recommendations_task(source: str = 'manual'):
    """定时任务：自动生成智能推荐股票并保存到数据库（V4.1）

    `source`（2026-11-01）：只用于批次留痕 —— 'schedule'（09:45/10:00/13:30/14:05）、
    'pre_close'（14:45 收盘前最终版）、'manual'（接口手动触发）。以前这几批全被当天
    最后一批覆盖，事后只能看到"最终推荐"，看不到盘中换过谁。
    
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

        # ===== P1: 推荐池 = hot_stock_buttons ∪ trend_scan_results ∪ 盘前海选池 =====
        pool = load_hot_stock_buttons()
        codes_list = [s['code'].lower() for s in pool if s.get('code')]
        codes_list = [c for c in codes_list if re.match(r'^(sh|sz)\d{6}$', c)]
        # 快照用：每只票是从哪个入口进来的（可叠加）。复盘时"热搜入口"和"海选入口"
        # 的胜率本来就不该混在一个数里看 —— 这是 2026-10 那轮回测最想说清的一件事。
        src_map = {c: '热搜' for c in codes_list}
        pool_info: Dict[str, Dict] = {}

        # 盘前海选（2026-10-15）：热搜榜是"散户在搜什么"的滞后指标，会系统性漏掉
        # 没进热榜的主升票（九阳股份 20 日 +103% 从未被扫到）。这里并入本地日K海选池，
        # 只扩入口、不动评分权重；海选失败或数据不新时它返回空，行为退回改造前。
        try:
            from services.pick.candidate_pool import load_pool
            _cp = load_pool()
            _cp_new = 0
            for p in _cp:
                sym = (p.get('symbol') or '').lower()
                if not re.match(r'^(sh|sz)\d{6}$', sym):
                    continue
                pool_info[sym] = p
                if sym not in codes_list:
                    codes_list.append(sym)
                    _cp_new += 1
                src_map[sym] = f"{src_map.get(sym, '')}+海选".lstrip('+')
            print(f'[智能推荐] 盘前海选并入 {len(_cp)} 只（新增 {_cp_new} 只），'
                  f'扫描宇宙 {len(codes_list)} 只')
        except Exception as e:
            print(f'[智能推荐] 盘前海选池读取失败（不阻塞，退回热搜池）: {e}')

        # 并入趋势扫描结果
        trend_map = _load_trend_score_map()
        before_merge = len(codes_list)
        for sym in trend_map:
            if sym not in codes_list:
                codes_list.append(sym)
            src_map[sym] = f"{src_map.get(sym, '')}+趋势".lstrip('+')
        if trend_map:
            added = len(codes_list) - before_merge
            print(f'[智能推荐] 合并趋势池 {len(trend_map)} 只（新增 {added} 只），股票池共 {len(codes_list)} 只')

        # 股票范围（2026-10-16 用户口径："所有的选股条件默认：沪深主板、非ST和*ST、市值大于35亿"）
        # 三个入口（热搜按钮、盘前海选、趋势池）都在这一处收口 —— 分散在各处过滤，
        # 迟早会出现"某一入口漏了 300/688"这种口径不一致。filter_prefixed 现在三条件一次过。
        _scope_desc, _scope_n = '', None
        try:
            from services.data import universe
            _before = len(codes_list)
            codes_list = universe.selectable_prefixed(codes_list)
            _scope_desc = universe.describe_selectable()
            _scope_n = universe.selectable_count()
            print(f'[智能推荐] 股票范围 {_scope_desc}：{_before} → {len(codes_list)} 只')
        except Exception as e:
            print(f'[智能推荐] 股票范围过滤失败（不阻塞，按原池继续）: {type(e).__name__}: {e}')

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

        # 名称层 ST 复核（2026-11-01）：范围过滤读的是本地名录，名录落后一天就会放过新戴帽的票
        # （10-15 的 *ST威领 就是这么进来的：名录 fetched_at 还停在 10-16）。行情里的名称是
        # **当下**的，用它再挡一次，成本只是一次字符串匹配。
        try:
            from services.data.universe import is_st_name as _is_st
            _n_before = len(all_results)
            all_results = [s for s in all_results if not _is_st(s.get('name'))]
            if len(all_results) != _n_before:
                print(f'[智能推荐] 名称层 ST 复核剔除 {_n_before - len(all_results)} 只')
        except Exception as e:
            print(f'[智能推荐] ST 名称复核跳过: {type(e).__name__}: {e}')

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
            from services.data.shareholder import get_holder_num_batch
            holder_map = await asyncio.to_thread(get_holder_num_batch,
                                                  [s.get('symbol', '') for s in all_results])
        except Exception as e:
            print(f'[智能推荐] 股东人数预取失败（降级为无数据）: {e}')
        # : P1a/P1b 批量预取基本面质量 + 聪明钱(龙虎榜)，注入第12/13因子
        fund_map = {}
        sm_map = {}
        try:
            from services.data.fundamental import get_fundamental_batch
            fund_map = await asyncio.to_thread(get_fundamental_batch,
                                               [s.get('symbol', '') for s in all_results])
        except Exception as e:
            print(f'[智能推荐] 基本面预取失败（降级为无数据）: {e}')
        try:
            from services.data.smartmoney import get_dragon_tiger_symbol_map
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
        from services.strategy.trend import compute_trend_result
        # 2026-11-04 代码层审查 P1：与 hot-stocks-scan 同一个 N+1 —— 循环内每票
        # calc_stock_score_v2 不传 weights，函数内部就每票查一次权重表（14.31ms/次）。
        # 一次推荐扫描内权重不变，循环外查一次传入，评分结果完全等价。
        try:
            from db.database import load_strategy_factor_weights
            rec_weights = await asyncio.to_thread(load_strategy_factor_weights)
        except Exception as _we:
            print(f'[智能推荐] 权重表预取失败（退回逐票读取）: {_we}')
            rec_weights = None
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
            stock['score'] = calc_stock_score_v2(stock, weights=rec_weights,
                                                 market_change=market_change,
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

        # ===== 3.5 涨幅段闸门·罚分与分区标签（2026-10-18，根因复盘 §3；原"半山腰追高惩罚"重写） =====
        # 老规则三个毛病：只罚 4.5~9.8% 一段（漏了更差的 0~2% 弱势区）、
        # abs() 把下跌股算进死亡区、没有开关想关只能改代码。
        # 复盘补全后：弱势区 0~2% 36.4%/-2.59% 与死亡区 4.5~9.8% 40.0%/-2.27%
        # 都是负期望（砍掉后剩 52.8%/+0.82%），负涨幅样本不足不闸。
        # 分区实现与三处调用点（主链/影子/推送）统一收在 momentum_zone。
        _zone_c = zone_cfg()
        _zone_drop = _zone_c['enabled'] and _zone_c['mode'] == 'drop'
        for s in all_results:
            # 每只票都挂分区标签：快照复盘时能按段统计，不依赖事后重算
            _z = zone_evaluate(s.get('changePercent'), _zone_c)
            s['zone'] = {'key': _z['key'], 'label': _z['label'], 'chg': _z['chg']}
        if _zone_c['enabled'] and _zone_c['mode'] == 'demote':
            zone_penalized = 0
            for s in all_results:
                _z = zone_penalty(s, _zone_c)
                if _z:
                    s['score']['total'] = max(0, s['score']['total'] - _z['penalty'])
                    s['score']['zone_penalty'] = _z['penalty']
                    zone_penalized += 1
            if zone_penalized:
                print(f'[智能推荐] 涨幅段闸门·罚分：{zone_penalized} 只落在负期望涨幅段'
                      f'（弱势区 0~{_zone_c["weak_max"]:g}% / 死亡区 '
                      f'{_zone_c["chase_min"]:g}~{_zone_c["chase_max"]:g}%）各罚 '
                      f'{_zone_c["weak_penalty"]}~{_zone_c["chase_penalty"]}分')
        # drop 模式不在这里罚分 —— 票在 4.8 整只拦掉，罚分没有意义。

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
        # P0-2（2026-10-24，默认关）：门槛上移 config；周期调度启用时按 gate 抬门槛。
        MIN_RECOMMEND_SCORE = RECOMMEND_MIN_SCORE_BASE
        # 周期调度（实施计划 §4）：gate/minScore/allowChase 用 live 口径
        # （vetoEnabled 才生效，过 10:00 + ≥2 弱信号，见 sentiment.cycle_gate 四前提）；
        # 2026-10-28 起角色体系下线，周期只剩"门槛 + 不追高 + 不接力"三条约束。
        # 关闭时 gate=None、行为与改造前逐字节一致。
        gate = None
        cycle_name = None
        cycle_max = 3
        if RECOMMEND_CYCLE_SCHEDULE_ENABLED:
            try:
                from services.strategy.sentiment import get_sentiment, cycle_gate as _cg
                _sent_live = await get_sentiment()
                gate = _cg(_sent_live)
                cycle_name = _sent_live.get('cycle')
                cycle_max = int(CYCLE_MAX_RECOMMEND.get(cycle_name, 3))
                if gate.get('vetoEnabled') and gate.get('minScore'):
                    MIN_RECOMMEND_SCORE = max(RECOMMEND_MIN_SCORE_BASE, int(gate['minScore']))
                print(f'[周期调度] 启用：{cycle_name}·veto={gate.get("vetoEnabled")} '
                      f'门槛={MIN_RECOMMEND_SCORE} 推荐上限={cycle_max} '
                      f'allowChase={gate.get("allowChase", True)}')
            except Exception as e:
                print(f'[周期调度] 情绪/闸门获取失败（按基础门槛与上限 3 走）: {e}')
                gate = None
                cycle_max = 3
        # 快照用：每只票"没往下走的那一步"是被哪条规则挡的。
        # 门槛之下的（score<60）不进这里 —— 快照读的时候按 min_score 一筛就有，
        # 给几百只都写一句"低于门槛"只会把真正有意思的拦截原因淹掉。
        blocked: Dict[str, List[str]] = {}
        # symbol → 模式代码（P2-2）。声明必须放在这里、早于 _capture_snapshot 与 `if not top3`
        # 那条提前 return：闭包是在调用时才读这个名字，晚声明会让"一只都不推"的批次
        # 抛 NameError 被快照自己的 try 吞掉 —— 那时最需要的空仓批次反而没快照。
        modes: Dict[str, str] = {}

        def _blk(sym, why):
            if sym:
                lst = blocked.setdefault(sym, [])
                if why not in lst:
                    lst.append(why)

        above_threshold = [s for s in all_results if s['score']['total'] >= MIN_RECOMMEND_SCORE]

        # P2c: 估值分位过滤 —— 极端高估(PE&PB 百分位双高)且非强多(>=80)的票不参与精选推荐
        candidates = []
        skipped_overvalued = 0
        for s in above_threshold:
            val = s.get('valuation')
            if val and val.get('overvalued') and s['score']['total'] < 80:
                skipped_overvalued += 1
                _blk((s.get('symbol') or '').lower(), '估值极端高估')
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
                    _blk((s.get('symbol') or '').lower(), '、'.join(tags))
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
            from services.pick.recommend_review import get_recent_loser_symbols
            # 不传参：判据/阈值/分级都由覆盖表(复盘子页)现读，config 只是默认值
            loser_symbols = get_recent_loser_symbols()
        except Exception as e:
            print(f'[智能推荐] 亏损黑名单加载失败（不阻塞推荐）: {e}')
            loser_symbols = set()
        if loser_symbols:
            before = len(candidates)
            _safe2 = []
            for s in candidates:
                _sym = (s.get('symbol') or '').lower()
                if _sym in loser_symbols:
                    _blk(_sym, f'亏损黑名单(近{RECOMMEND_LOSER_LOOKBACK_DAYS}天)')
                else:
                    _safe2.append(s)
            candidates = _safe2
            skipped = before - len(candidates)
            if skipped:
                print(f'[智能推荐] 亏损黑名单过滤：跳过 {skipped} 只近{RECOMMEND_LOSER_LOOKBACK_DAYS}天'
                      f'推荐亏损>{RECOMMEND_LOSER_LOSS_THRESHOLD}%的股票')

        # ===== 4.8 涨幅段闸门·过滤模式（2026-10-18，B） =====
        # drop 模式：弱势区 0~2% 与死亡区 4.5~9.8% 的票不进候选。
        # 它们不是被静默吞掉 —— 全部进影子 excluded（见 7.5 传参），
        # shadow_eval 照样给这组算 T+1：闸门拦得对不对，两周后拿实测说，
        # 不信这个结论就直接把模式改回 demote/off，代码不用动。
        zone_dropped = []
        if _zone_drop:
            _safe3 = []
            for s in candidates:
                _z = zone_gate(s, _zone_c)
                if _z:
                    _sym = (s.get('symbol') or '').lower()
                    _blk(_sym, f'涨幅段闸门·{_z["label"]}')
                    zone_dropped.append(s)
                    print(f'[智能推荐] 涨幅段闸门: {s["name"]}({s["symbol"]}) '
                          f'当日涨幅{_z["chg"]:+.1f}% 落在{_z["label"]}，'
                          f'评分{s["score"]["total"]}跳过')
                else:
                    _safe3.append(s)
            candidates = _safe3
            if zone_dropped:
                print(f'[智能推荐] 涨幅段闸门：跳过 {len(zone_dropped)} 只负期望涨幅段股票'
                      f'（弱势区 0~{_zone_c["weak_max"]:g}%、死亡区 '
                      f'{_zone_c["chase_min"]:g}~{_zone_c["chase_max"]:g}%）')

        # ===== 4.9 周期调度·不追高 / 极热模式禁令 / 大盘否决（P0-2，2026-10-24，默认关） =====
        # 实施计划 §4：RECOMMEND_CYCLE_SCHEDULE_ENABLED=False 时本节整体跳过，
        # 主链路行为与改造前一致；每条挡掉的票都写 blocked 快照，事后可回放。
        # 口径与影子 _log_shadow_selection 一致：不追高 = not allowChase 且当日涨幅 ≥5%
        # （影子里用的是 advisory 口径，这里用 live 口径 = gate.vetoEnabled 才生效，
        # 见 sentiment.cycle_gate 的四前提）。2026-10-28 角色体系下线，角色否决整块移除。
        cash_reason: Optional[str] = None   # 非空 → 本批写空仓哨兵、不出买入推荐
        if RECOMMEND_CYCLE_SCHEDULE_ENABLED:
            # --- 大盘否决位：severe crash 日不出新买入推荐 ---
            if RECOMMEND_MARKET_VETO_ENABLED:
                try:
                    from services.strategy.market import check_market_crash
                    _mc = await check_market_crash()
                    if _mc.get('crashLevel') == 'severe':
                        cash_reason = '大盘预警·severe（指数跌超 -3%，当日不出新买入推荐）'
                        print(f'[周期调度] {cash_reason}，本批只写空仓哨兵')
                except Exception as e:
                    print(f'[周期调度] 大盘预警检查失败（不否决，按周期规则走）: {e}')

            if cash_reason is None and gate is not None:
                # --- 不追高（与影子同口径） ---
                _no_chase = not gate.get('allowChase', True)
                _chase_n = 0
                if _no_chase:
                    _safe4 = []
                    for s in candidates:
                        _sym = (s.get('symbol') or '').lower()
                        _chg = float(s.get('changePercent') or 0)
                        if _chg >= 5:
                            _blk(_sym, f'{cycle_name}·涨幅{_chg:.1f}%不追高')
                            _chase_n += 1
                            continue
                        _safe4.append(s)
                    candidates = _safe4
                    if _chase_n:
                        print(f'[周期调度] {cycle_name}：不追高挡掉 {_chase_n} 只')

                # --- 极热模式级规则（非一刀切）：涨停 >120 且非 stale → 禁低吸/追高形态，
                #     打板保留（1094 口径：极热档全体买入 42.6%/-0.28% 最差，封板接力仍
                #     67.1%/+2.06%，957 样本）；打板仅限主线内且当日最多 1 只，无主线 → 哨兵 ---
                _limit_ups = None
                try:
                    from services.strategy.sentiment import get_sentiment as _gs
                    _s2 = await _gs()
                    if not _s2.get('stale'):
                        _limit_ups = (_s2.get('indicators') or {}).get('limitUpCount')
                except Exception:
                    _limit_ups = None
                if isinstance(_limit_ups, (int, float)) and _limit_ups > CYCLE_EXTREME_HOT_LIMIT_UPS:
                    print(f'[周期调度] 极热档（涨停{_limit_ups:.0f}家>{CYCLE_EXTREME_HOT_LIMIT_UPS}）：'
                          f'禁低吸/追高形态，只留主线内打板、至多 1 只')
                    # 主线名集合：最新主线快照（缺失/过期 → 视为无主线 → 空仓哨兵）
                    _ml_names = []
                    try:
                        import sqlite3 as _sq
                        from app.config import SQLITE_DB_PATH as _dbp
                        _c = _sq.connect(_dbp, timeout=10)
                        _r = _c.execute("SELECT payload, ts FROM market_mainline_snapshots "
                                        "ORDER BY main_date DESC, ts DESC LIMIT 1").fetchone()
                        _c.close()
                        if _r and _r[0]:
                            _ts = str(_r[1] or '')
                            import datetime as _dtmod
                            _fresh = False
                            try:
                                _fresh = (datetime.now() - _dtmod.datetime.fromisoformat(_ts)
                                          ).total_seconds() <= CYCLE_EXTREME_HOT_MAINLINE_STALE_SEC
                            except Exception:
                                _fresh = False
                            if _fresh:
                                _pl = json.loads(_r[0])
                                _ml_names = [m.get('name', '') for m in (_pl.get('main') or [])]
                    except Exception as e:
                        print(f'[周期调度] 主线快照读取失败（视为无主线）: {e}')
                        _ml_names = []
                    if not _ml_names:
                        cash_reason = (f'极热档·涨停{_limit_ups:.0f}家且无可用主线'
                                       f'（打板仅限主线内，无主线 → 空仓）')
                        print(f'[周期调度] {cash_reason}')
                    else:
                        from services.strategy.buy_rules import classify as _cls2
                        _tags_raw = load_stock_concept_tags() or {}
                        _tags = {k.lower(): v for k, v in _tags_raw.items()}
                        _pb = _prev_bars([s.get('symbol') for s in candidates])
                        _keep, _daban = [], []
                        for s in candidates:
                            _sym = (s.get('symbol') or '').lower()
                            _chg = float(s.get('changePercent') or 0)
                            try:
                                _v = (_cls2({'changePercent': s.get('changePercent'),
                                             'volumeRatio': s.get('volumeRatio')},
                                            _pb.get(_sym[-6:]),
                                            {'limitUp': _limit_ups}) or {}).get('verdict')
                            except Exception:
                                _v = None
                            # 主线内：概念标签或名称与主线板块名子串互含
                            _name = s.get('name') or ''
                            _stock_tags = _tags.get(_sym) or []
                            _in_ml = any((n and (n in _name or any(n in t for t in _stock_tags if t)))
                                         for n in _ml_names)
                            if _v == '可打板' and _in_ml:
                                _daban.append(s)
                            else:
                                _why = '极热·非主线' if _v == '可打板' else f'极热·禁{_v or "未判定"}形态'
                                _blk(_sym, _why)
                        if _daban:
                            _daban.sort(key=lambda x: (x.get('score') or {}).get('total', 0), reverse=True)
                            for s in _daban[1:]:
                                _blk((s.get('symbol') or '').lower(), '极热·打板当日限 1 只')
                            candidates = _daban[:1]
                            print(f'[周期调度] 极热档保留主线内打板 1 只：'
                                  f'{candidates[0].get("name")}（主线：{"、".join(_ml_names[:3])}）')
                        else:
                            cash_reason = f'极热档·无主线内可打板形态（涨停{_limit_ups:.0f}家）'
                            print(f'[周期调度] {cash_reason} → 空仓哨兵')
                            candidates = []

        # 概念分散：每种「有标签的」概念取最高分的一只，最多 3 只。
        # 2026-10-15 修正：'其他' 不再参与去重。stock_concept_tags 只覆盖 55 只票，
        # 其余全落到 '其他' —— 旧写法等于"第一只无标签票入选后，所有无标签票一起出局"。
        # 实测受害者：澳弘电子 10-09 预测分 72（当日第 3）、10-13 76（当日第 4），
        # 两次都被这条规则挤在 Top3 之外，而入选的是分数更低的同'其他'票。
        top3 = []
        seen_tags = set()
        for s in candidates:
            tag = s.get('primary_tag', '其他')
            if tag != '其他' and tag in seen_tags:
                continue
            top3.append(s)
            if tag != '其他':
                seen_tags.add(tag)
            # P0-2：推荐数上限随周期（默认关时 cycle_max=3，行为不变）
            if len(top3) >= cycle_max:
                break

        # 如果行业分散后不足3只，从剩余中按分数补足（允许同概念）
        # P0-2：周期调度启用时**不补足**——退潮日 1 只就是 1 只，空缺即纪律（计划 §4）
        if len(top3) < 3 and not RECOMMEND_CYCLE_SCHEDULE_ENABLED:
            remaining = [s for s in candidates if s not in top3]
            top3.extend(remaining[:3 - len(top3)])

        # 快照用：过了全部门槛却没进精选的，原因就是"概念分散/名额只有 3 个"
        _top3_syms = {(s.get('symbol') or '').lower() for s in top3}
        for s in candidates:
            _sym = (s.get('symbol') or '').lower()
            if _sym and _sym not in _top3_syms:
                _blk(_sym, '未进精选(概念分散/名额3只)')

        async def _capture_snapshot(shadow_payload: Optional[Dict] = None):
            """把本批次的决策现场落库。任何失败只打日志，绝不冒泡影响推荐。

            放在这里而不是 section 7 之后：`if not top3` 那条"今天一只都不推"的分支
            也是决策，缺了它，以后按快照算的胜率会因为少掉空仓日而系统性偏高。
            """
            try:
                from services.pick.strategy_snapshot import write_snapshot
                from services.strategy.sentiment import get_sentiment, cycle_gate
                _sent = await get_sentiment()
                _gate = cycle_gate(_sent)
                _labs: Dict[str, Dict] = {}
                # 模式标签合并进来：快照每只票一行都要带上它，
                # 否则"按模式复盘"只能看到最终那 3 只，看不到同一天被挡掉的同模式票。
                for _sym, _m in (modes or {}).items():
                    if _sym:
                        _labs.setdefault(_sym, {})['mode'] = _m
                # RPS 影子标签（2026-10-22）：只落快照、**不参与评分**。
                # 背景：rps.py 早就算出了全市场 RPS，但 recommend 从未把 rps_data 传进
                # calc_stock_score_v2，第 13 因子一直是常数。本轮先把真实值与三种待验口径
                # （单因子强 / 强+回踩MA20 / 强+缩量）落下来，等快照攒够两周再决定是否进评分。
                # 因子体检显示 60 日动量类 IC 为负，所以绝不能在没对照前直接加正向权重。
                try:
                    from services.strategy.rps import get_rps_map
                    _rps_map = await get_rps_map()
                except Exception as _e:
                    _rps_map = {}
                    print(f'[策略快照] RPS 读取失败（rps 列留空）: {type(_e).__name__}: {_e}')
                for _s in all_results:
                    _sym = (_s.get('symbol') or '').lower()
                    if not _sym:
                        continue
                    _v = _rps_map.get(_sym)
                    if _v is None:
                        _v = _rps_map.get(_sym[-6:])       # 带 sh/sz 前缀时退回裸码
                    if _v is None:
                        continue
                    _tr = trend_map.get(_sym) or {}
                    _ma = None
                    try:
                        _mas = _tr.get('ma20') or []
                        if _mas and _mas[-1] is not None:
                            _ma = float(_mas[-1])
                    except (TypeError, ValueError):
                        _ma = None
                    try:
                        _px = float(_s.get('priceRaw') or _s.get('price') or 0)
                    except (TypeError, ValueError):
                        _px = 0.0
                    try:
                        _vr = float(_s.get('volumeRatio') or 0)
                    except (TypeError, ValueError):
                        _vr = 0.0
                    _pull = round((_px / _ma - 1) * 100, 2) if (_ma and _px) else None
                    _tag = {'rps': round(float(_v), 1)}
                    _strong = float(_v) >= 80
                    _pull_ok = _pull is not None and 0 <= _pull <= 5
                    _shrink = _vr > 0 and _vr < 1
                    if _strong:
                        _tag['strong'] = True
                    if _pull is not None:
                        _tag['pullbackPct'] = _pull
                        _tag['pullback'] = _pull_ok
                    if _vr:
                        _tag['volRatio'] = _vr
                        _tag['shrink'] = _shrink
                    _tag['verdict'] = ([k for k, ok in (
                        ('strong', _strong), ('strong+pullback', _strong and _pull_ok),
                        ('strong+shrink', _strong and _shrink)) if ok])
                    _labs.setdefault(_sym, {})['rps'] = _tag['rps']
                    _labs[_sym]['rps_shadow'] = _tag
                # 新③（2026-11-03，影子）：rev20+缩量 候选池影子臂——只落快照、不改候选。
                # 依据：因子体检 85 个截面里唯一稳健为正的组合（rev20 分位≥0.7 且
                # vol_shrink 分位≥0.6，5 日超额 +0.12pp）。分位分母=当日打分集
                # （体检是全市场截面，口径差在裁决时注明）；日K取本地库最近 21 根
                # 已收盘 bar（盘中即昨收基准）。
                try:
                    from services.pick.rev20_shadow import rev20_shrink_labs
                    for _sym, _t in (await rev20_shrink_labs(all_results)).items():
                        _labs.setdefault(_sym, {}).update(_t)
                except Exception as _e:
                    print(f'[策略快照] rev20 影子失败（列留空）: {type(_e).__name__}: {_e}')
                # 修⑥①（2026-11-07，影子）：抗跌因子——大盘跌的时候这票扛不扛。
                # 基准=sh000001 近 20 个已收盘日，三条口径（相对最大回撤 pp / 下跌日 beta /
                # 抗跌日占比）与"板块腿为什么先不接"见 services/pick/anti_drop_shadow.py 顶部；
                # 本地日K落后的票一律不算（11-06 炸板影子那次就是栽在拿旧序列算）。
                # **只填快照 antiDrop 字段，不改候选、不改分数、不改动作。**
                try:
                    from services.pick.anti_drop_shadow import anti_drop_labs
                    _ad_labs, _ad_meta = await anti_drop_labs(all_results)
                    for _sym, _t in _ad_labs.items():
                        _labs.setdefault(_sym, {}).update(_t)
                    if _ad_meta.get('asked'):
                        print(f"[快照] 抗跌影子：算 {_ad_meta.get('computed')}/{_ad_meta.get('asked')} 只"
                              f"（日K落后 {_ad_meta.get('stale')}、不足 {_ad_meta.get('notEnough')}），"
                              f"基准 {_ad_meta.get('bench')} {str(_ad_meta.get('indexDays'))} 天")
                except Exception as _e:
                    print(f'[策略快照] 抗跌影子失败（字段留空）: {type(_e).__name__}: {_e}')
                if shadow_payload:
                    def _lab(_sym):
                        return _labs.setdefault(_sym, {}) if _sym else {}

                    for it in (shadow_payload.get('kept') or []):
                        _lab((it.get('symbol') or '').lower())['shadow'] = '影子放行'
                    for it in (shadow_payload.get('excluded') or []):
                        _lab((it.get('symbol') or '').lower())['shadow'] = \
                            '影子挡:' + '；'.join(it.get('reasons') or [])
                    for _key in ('live', 'shadow'):
                        for it in (shadow_payload.get(_key) or []):
                            _cur = _lab((it.get('symbol') or '').lower())
                            for _k in ('timing',):
                                if it.get(_k):
                                    _cur[_k] = it[_k]
                            _mx = it.get('matrix') or {}
                            if _mx:
                                _cur['matrix'] = _mx.get('verdict')
                                _cur['matrix_why'] = _mx.get('why')
                _n = write_snapshot({
                    'date_key': now.date().isoformat(),
                    'batch': now.strftime('%H:%M'),
                    'generated_at': now.isoformat(timespec='seconds'),
                    'scored': all_results,
                    'candidates': candidates,
                    'top3': top3,
                    'blocked': blocked,
                    'sources': src_map,
                    'pool_info': pool_info,
                    'labels': _labs,
                    'modes': modes,
                    'market_change': market_change,
                    'sent': _sent,
                    'gate': _gate,
                    'live_min_score': MIN_RECOMMEND_SCORE,
                    'scope_desc': _scope_desc,
                    'scope_size': _scope_n,
                    'matrix_version': shadow_payload.get('matrixVersion') if shadow_payload else None,
                })
                _st = {'recommended': len(_top3_syms), 'candidate': len(candidates),
                       'scored': len(all_results)}
                print(f'[策略快照] {now:%H:%M} 落库 {_n} 行：'
                      f'扫描{_st["scored"]}只 / 过门槛{_st["candidate"]}只 / 精选{_st["recommended"]}只，'
                      f'情绪{_sent.get("cycle")}·涨停{_sent.get("indicators", {}).get("limitUpCount")}')
            except Exception as e:
                print(f'[策略快照] 落库失败（不影响本次推荐）: {type(e).__name__}: {e}')

        if not top3:
            # P0-2 空仓哨兵（默认关时维持原"跳过"行为）：退潮/高潮允许 0 只推荐；
            # 极热无主线（cash_reason）与大盘 severe 否决同样直接哨兵。
            if RECOMMEND_CYCLE_SCHEDULE_ENABLED and (cash_reason or cycle_name in CYCLE_ALLOW_EMPTY):
                try:
                    from app.config import CYCLE_RULES as _cr
                    _note = (_cr.get(cycle_name) or {}).get('note', '')
                except Exception:
                    _note = ''
                _reason = cash_reason or f'{cycle_name}·{_note}'
                _sentinel = {
                    'name': '空仓观望', 'symbol': 'CASH',
                    'price': '', 'change': 0, 'changePercent': 0, 'score': 0,
                    # buy 留空：复盘按非数值买入价自动跳过，不会给哨兵算假收益
                    'buySellPoints': {'buy': '', 'current': '', 'sell': ''},
                    'reason': _reason,
                    'isCash': True,
                }
                _date_key = now.date().isoformat()
                _existing = load_daily_recommendations()
                _existing = [r for r in _existing if r.get('date') != _date_key]
                _existing.insert(0, {'date': _date_key,
                                     'daily_recommendations': [_sentinel],
                                     'cycleInfo': _cycle_info(gate, cycle_name, cycle_max, True)})
                if len(_existing) > QUERY_DAILY_RECOMMENDATIONS_LIMIT:
                    _existing = _existing[:QUERY_DAILY_RECOMMENDATIONS_LIMIT]
                save_daily_recommendations(_existing, batch_time=now.strftime('%H:%M:%S'),
                                           source=source or 'manual')
                print(f'[周期调度] 空仓哨兵已写入：{_reason}')
            else:
                print(f'[智能推荐] 无评分>={MIN_RECOMMEND_SCORE}的股票，跳过')
            # 空仓批次也要影子评估：'该不该空仓'同样需要 T+1 对照，不能只在有票的日子记样本
            try:
                _ind_map_sg = {sym: (ff or {}).get('industry', '')
                               for sym, ff in (fund_flow_map or {}).items()}
                await _log_shadow_gate_adjustment(
                    now, now.date().isoformat(), above_threshold, zone_dropped, [],
                    MIN_RECOMMEND_SCORE, all_results, _ind_map_sg)
            except Exception as e:
                print(f'[影子闸门] 入口异常（不影响线上推荐）: {type(e).__name__}: {e}')
            await _capture_snapshot()
            return

        # ===== 6. 计算买卖点 + 推荐理由 =====
        rec_items = []
        # 追高警告里的胜率用**当天复盘的实测值**（10 分钟缓存），不写死：
        # 低吸单真正成交 42.1%（n=95）/ 现价追入 46.8%（n=218）是历史推荐算出来的，
        # 样本每天在长、口径刚换成 1094，写死就等于三个月后还在引用没人核对的数字。
        _rates = exec_rates()
        for r in top3:
            sym = r.get('symbol', '').lower()
            _trend = trend_map.get(sym)
            bsp = calc_buy_sell_points(r, score_result=r.get('score'),
                                       trend_details=(_trend or {}).get('details'),
                                       market_change=market_change)
            # 执行诚实化（2026-10-18，P0-1）：买卖点里已经挂过区间，这里再带**当天实测胜率**
            # 覆写一次文案 —— 同一个函数、同一套数字，只是把默认值换成实测量。
            _apply_exec_honesty(r, bsp, _rates)
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

        # 模式标签（2026-10-18，P2-2）：把买法矩阵的 verdict 机械映射成 dixi/daban/chaodi/…，
        # 复盘第一次有"按模式"这个维度（以前只有"按日期"，回答不了"哪种模式值得加码"）。
        # 注意这是**线上主链路第一次调用 classify**（此前只有影子与推送算它）：
        # 每天 ≤3 只、每只一条本地日K查询，不碰网络；失败只少一个标签，不影响推荐。
        try:
            from services.strategy.buy_rules import classify as _cls, pick_mode as _pick, mode_label as _ml
            _pm = _prev_bars([r.get('symbol') for r in top3])
            for r, item in zip(top3, rec_items):
                _sym = (r.get('symbol') or '').lower()
                _prev = _pm.get(_sym[-6:])
                _v = (_cls({'changePercent': r.get('changePercent'),
                            'volumeRatio': r.get('volumeRatio'),
                            'gain20': r.get('gain20')}, _prev, None) or {}).get('verdict')
                item['mode'] = _pick(_v, _prev)
                modes[(r.get('symbol') or '').lower()] = item['mode']
            _relay = [i['name'] for i in rec_items if i.get('mode') == 'relay']
            if _relay:
                print(f'[推荐模式] [警告] 接力票出现在推荐里（矩阵判"不接力"、实测次日开盘卖六格全负）：'
                      f'{"、".join(_relay)} —— 这条本该被 4.6/4.7 或门槛挡住，出现即说明有漏口')
            print('[推荐模式] ' + '、'.join(
                f"{i['name']}={_ml(i.get('mode', ''))}" for i in rec_items))
        except Exception as e:
            print(f'[推荐模式] 标签计算失败（不影响推荐，仅少一个复盘维度）: {type(e).__name__}: {e}')

        # ===== 7. 保存到数据库 =====
        date_key = now.date().isoformat()
        record = {
            'date': date_key,
            'daily_recommendations': rec_items,
        }
        # P0-2：周期调度启用时给记录带"今日模式"信息，前端常驻横幅（默认关时无此键）
        if RECOMMEND_CYCLE_SCHEDULE_ENABLED:
            record['cycleInfo'] = _cycle_info(gate, cycle_name, cycle_max, False)
        existing = load_daily_recommendations()
        existing = [r for r in existing if r.get('date') != date_key]
        existing.insert(0, record)
        if len(existing) > QUERY_DAILY_RECOMMENDATIONS_LIMIT:
            existing = existing[:QUERY_DAILY_RECOMMENDATIONS_LIMIT]
        saved = save_daily_recommendations(existing, batch_time=now.strftime('%H:%M:%S'),
                                           source=source or 'manual')

        # ===== 7.5 影子精选（2026-10-14，只记录不替换）=====
        # 为什么必须影子而不是直接换：15:20 的回测与 15:35 的自动优化都在学现有口径，
        # 直接把情绪门槛塞进线上，之后胜率涨跌分不清是"策略变好"还是"行情变好"。
        # 影子每天多存一份"如果按周期门槛会推谁/会挡掉谁"，攒够 ≥10 个交易日后
        # 用 recommend_review 的 T+1 胜率与 backtest_recommend 的分组口径对比再决定切换。
        try:
            _shadow_payload = await _log_shadow_selection(now, date_key, candidates, top3,
                                                          trend_map, market_change,
                                                          MIN_RECOMMEND_SCORE, zone_dropped)
        except Exception as e:
            print(f'[智能推荐] 影子精选落库失败（不影响线上推荐）: {e}')
            _shadow_payload = None

        # ===== 7.55 影子闸门四维调整（2026-10-31，只记录不生效）=====
        # 与 7.5 影子精选的区别：那边调的是"门槛 / 不追高"两条市场级规则，
        # 这里调的是个股层面的四维加减分；落独立表，两边互不影响。
        try:
            _ind_map_sg = {sym: (ff or {}).get('industry', '')
                           for sym, ff in (fund_flow_map or {}).items()}
            await _log_shadow_gate_adjustment(now, date_key, above_threshold, zone_dropped,
                                              top3, MIN_RECOMMEND_SCORE, all_results,
                                              _ind_map_sg)
        except Exception as e:
            print(f'[影子闸门] 入口异常（不影响线上推荐）: {type(e).__name__}: {e}')

        # ===== 7.6 策略决策快照（2026-10-17）=====
        # 影子给的是"某天选了谁"，快照给的是"那天每一只为什么被选/被挡"。
        # 有了后者，以后的回测才不用拿 r20 近似口径重建海选。
        await _capture_snapshot(_shadow_payload)

        if saved:
            symbols_str = '、'.join([f'{r["name"]}({r["symbol"]})' for r in rec_items])
            scores_str = '、'.join([f'{r["name"]}:{r["score"]}分' for r in rec_items])
            print(f'[智能推荐] 生成完成: {symbols_str}，评分: {scores_str}')
        else:
            print('[智能推荐] 保存失败')
        # P0-2：带 cycleInfo 让前端横幅即时更新（调度关时无此键，前端不渲染横幅）
        if RECOMMEND_CYCLE_SCHEDULE_ENABLED:
            return {"recommendations": rec_items,
                    "cycleInfo": _cycle_info(gate, cycle_name, cycle_max, False)}
        return {"recommendations": rec_items}
    except Exception as error:
        print(f'[智能推荐] 自动生成失败: {str(error)}')
        return None
