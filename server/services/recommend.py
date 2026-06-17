"""recommend.py - split from main.py"""
import json, asyncio, re
from typing import List, Dict
from datetime import datetime, date
from app.config import HOT_STOCK_POOL, STRATEGY_PREDICT_BULL_MIN_SCORE, QUERY_DAILY_RECOMMENDATIONS_LIMIT
from db.database import get_db_conn, load_daily_recommendations, save_daily_recommendations, load_user_scan_pool, load_hot_stock_buttons, load_trend_scan_results, load_stock_concept_tags
from services.stock import get_http_client, fetch_stock_data, parse_realtime_fields, get_stock_name, fetch_with_retry, parse_tencent_batch_data
from services.trend import scan_trend_scan_results
from services.strategy import calc_stock_score_v2

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
    if change_pct >= 9.8:
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
        if change_pct >= 9.8:
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
        market_change = 0.0
        try:
            idx_data = await fetch_stock_data('sh000001')
            if idx_data:
                market_change = idx_data.get('parsed', {}).get('changePercent', 0)
        except Exception as e:
            print(f'[智能推荐] 获取大盘数据失败: {str(e)}')

        # ===== 3. 批量扫描行情 =====
        batch_size = 20
        all_results = []
        for i in range(0, len(codes_list), batch_size):
            batch = codes_list[i:i + batch_size]
            symbols = ','.join(batch)
            try:
                data = await fetch_with_retry(symbols)
                parsed = parse_tencent_batch_data(data)
                all_results.extend(parsed)
            except Exception as e:
                print(f'  [智能推荐] 批次 {i // batch_size + 1} 失败: {str(e)}')

        if not all_results:
            print('[智能推荐] 未获取到任何股票行情数据')
            return

        # ===== 4. 混合评分（P0: 50%日内 + 50%趋势） =====
        concept_tags_raw = load_stock_concept_tags()
        concept_tags = {k.lower(): v for k, v in concept_tags_raw.items()}  # 统一小写
        for stock in all_results:
            sym = stock.get('symbol', '').lower()
            trend = trend_map.get(sym)  # 趋势扫描已有的结果
            stock['score'] = calc_stock_score_v2(stock, market_change=market_change, trend_result=trend)
            # 预取概念标签用于行业分散
            tags = concept_tags.get(sym)
            stock['primary_tag'] = tags[0] if tags else '其他'

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
