"""recommend.py - split from main.py"""
import json, asyncio, re, requests
from typing import List, Dict, Optional
from datetime import datetime, date
from app.config import HOT_STOCK_POOL, STRATEGY_PREDICT_BULL_MIN_SCORE, QUERY_DAILY_RECOMMENDATIONS_LIMIT, STRATEGY_INDUSTRY_NEUTRALIZATION_ENABLED, EASTMONEY_HOT_LIST_API, TREND_LIMIT_UP_THRESHOLD
from db.database import get_db_conn, load_daily_recommendations, save_daily_recommendations, load_user_scan_pool, load_hot_stock_buttons, load_trend_scan_results, load_stock_concept_tags, save_stock_industry
from services.stock import fetch_stock_data, fetch_with_retry, parse_tencent_batch_data
from services.strategy import calc_stock_score_v2, apply_industry_neutralization
from services.utils import to_float as _to_float


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

    def _fetch_targeted():
        # ulist.get 支持批量获取指定 secids
        params = {
            'pi': 0, 'pz': len(secids), 'po': 1, 'np': 1, 'fltt': 2, 'invt': 2,
            'fields': 'f12,f13,f62,f21,f100',
            'secids': ','.join(secids)
        }
        # 使用 EASTMONEY_HOT_LIST_API 的基础域名，但接口路径不同
        # 实际地址示例: https://push2.eastmoney.com/api/qt/ulist/get
        url = EASTMONEY_HOT_LIST_API.replace('/clist/get', '/ulist/get')
        r = requests.get(url, params=params, headers=_HEADERS, timeout=5)
        r.raise_for_status()
        data = r.json()
        return data.get('data', {}).get('diff', []) or []

    # 2026-07-06 C-5: _to_float 已合并到 services.utils.to_float（顶部 import 别名 _to_float）
    # 此处原局部函数定义已删除

    try:
        items = await asyncio.to_thread(_fetch_targeted)
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
        print(f'[资金流向] 靶向获取 {len(found)}/{len(codes)} 只股票数据')
        return found
    except Exception as e:
        print(f'[资金流向] 靶向获取失败: {e}')
        return {}


def calc_buy_sell_points(stock: Dict) -> Dict:
    """计算买入点、当前价、卖出点（V4.1 智能版）
    - 涨停板 (涨跌幅 >= 9.8%)：买入提示「竞价关注」，卖出动态调整
    - 非涨停板：结合近20日波动率动态调整买卖比例
    """
    try:
        price = float(stock.get('priceRaw') or stock.get('price') or 0)
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        return {'buy': '', 'current': '', 'sell': ''}

    change_pct = float(stock.get('changePercent', 0) or 0)
    amplitude = float(stock.get('amplitude', 0) or 0)

    # 涨停板：不设固定买点，提示竞价关注
    if change_pct >= TREND_LIMIT_UP_THRESHOLD:
        # 卖出价：封单坚决 +2%，普通封板 +4%
        weibi = float(stock.get('weibi', 0) or 0)
        turnover = float(stock.get('turnoverRate', 0) or 0)
        if weibi > 80 and 2 <= turnover <= 15:
            sell_pct = 0.03  # 龙头封单坚决，次日高溢价
        else:
            sell_pct = 0.05
        return {
            'buy': '竞价关注',
            'current': f'{price:.2f}',
            'sell': f'{price * (1 + sell_pct):.2f}',
        }

    # 非涨停板：根据振幅动态调整买卖比例
    # 振幅越大 → 波动越剧烈 → 买点回撤更多、卖点空间更大
    if amplitude >= 5:
        buy_pct = 0.03   # 高波动，等更大回撤
        sell_pct = 0.06
    elif amplitude >= 3:
        buy_pct = 0.025
        sell_pct = 0.05
    elif amplitude >= 2:
        buy_pct = 0.02
        sell_pct = 0.04
    else:
        buy_pct = 0.015  # 低波动，小幅回撤即可
        sell_pct = 0.03

    return {
        'buy': f'{price * (1 - buy_pct):.2f}',
        'current': f'{price:.2f}',
        'sell': f'{price * (1 + sell_pct):.2f}',
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

        # ===== 4. 混合评分（P0: 50%日内 + 50%趋势 + P1-1 主力资金因子） =====
        concept_tags_raw = load_stock_concept_tags()
        concept_tags = {k.lower(): v for k, v in concept_tags_raw.items()}  # 统一小写
        # P1-1: 批量预取主力资金流向，注入第9因子
        fund_flow_map = {}
        try:
            fund_flow_map = await fetch_batch_fund_flow([s.get('symbol', '') for s in all_results])
        except Exception as e:
            print(f'[智能推荐] 资金流向预取失败（降级为无数据）: {e}')
        for stock in all_results:
            sym = stock.get('symbol', '').lower()
            trend = trend_map.get(sym)  # 趋势扫描已有的结果
            ff = fund_flow_map.get(sym)
            stock['score'] = calc_stock_score_v2(stock, market_change=market_change,
                                                 trend_result=trend, fund_flow_data=ff)
            # 预取概念标签用于行业分散
            tags = concept_tags.get(sym)
            stock['primary_tag'] = tags[0] if tags else '其他'

        # P3-2 行业中性化：按行业 P20/P80 分位数调整 strategy_score.total（与 routes 路径对齐）
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

        # 行业分散：每种概念取最高分的一只，最多3只
        top3 = []
        seen_tags = set()
        for s in above_threshold:
            tag = s.get('primary_tag', '其他')
            if tag not in seen_tags:
                top3.append(s)
                seen_tags.add(tag)
            if len(top3) >= 3:
                break

        # 如果行业分散后不足3只，从剩余中按分数补足（允许同概念）
        if len(top3) < 3:
            remaining = [s for s in above_threshold if s not in top3]
            top3.extend(remaining[:3 - len(top3)])

        if not top3:
            print(f'[智能推荐] 无评分>={MIN_RECOMMEND_SCORE}的股票，跳过')
            return

        # ===== 6. 计算买卖点 + 推荐理由 =====
        rec_items = []
        for r in top3:
            bsp = calc_buy_sell_points(r)
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
