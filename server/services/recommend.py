"""recommend.py - split from main.py"""
import json, asyncio, re
from typing import List, Dict
from datetime import datetime, date
from app.config import HOT_STOCK_POOL, STRATEGY_PREDICT_BULL_MIN_SCORE, QUERY_DAILY_RECOMMENDATIONS_LIMIT
from db.database import get_db_conn, load_daily_recommendations, save_daily_recommendations, load_user_scan_pool, load_hot_stock_buttons
from services.stock import get_http_client, fetch_stock_data, parse_realtime_fields, get_stock_name, fetch_with_retry, parse_tencent_batch_data
from services.trend import scan_trend_scan_results
from services.strategy import calc_stock_score_v2

def calc_buy_sell_points(stock: Dict) -> Dict:
    """计算买入点、当前价、卖出点（与前端 calculateBuySellPoints 逻辑一致）"""
    try:
        price = float(stock.get('priceRaw') or stock.get('price') or 0)
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        return {'buy': '', 'current': '', 'sell': ''}
    return {
        'buy': f'{price * (1 - 0.02):.2f}',
        'current': f'{price:.2f}',
        'sell': f'{price * (1 + 0.04):.2f}',
    }


def gen_recommend_reason_server(stock: Dict, score_result: Dict) -> str:
    """生成推荐理由（与前端 generateRecommendReason 逻辑一致）"""
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
    except Exception:
        pass

    if not reasons:
        reasons.append(f'综合评分{score_result.get("total", 0)}分，符合{score_result.get("label", "")}特征')

    return '📊 ' + '；'.join(reasons)
async def auto_generate_recommendations_task():
    """定时任务：自动生成智能推荐股票并保存到数据库"""
    try:
        now = datetime.now()
        # 周末不生成推荐
        if now.weekday() >= 5:
            print(f'[智能推荐] 周末不生成推荐（{now.strftime("%Y-%m-%d %H:%M")}）')
            return

        print(f'[智能推荐] 开始自动生成推荐股票（{now.strftime("%Y-%m-%d %H:%M")}）...')

        # 1. 获取股票池
        pool = load_hot_stock_buttons()
        if not pool:
            print('[智能推荐] 股票池为空，跳过本次推荐')
            return

        codes_list = [s['code'].lower() for s in pool if s.get('code')]
        codes_list = [c for c in codes_list if re.match(r'^(sh|sz)\d{6}$', c)]
        if not codes_list:
            print('[智能推荐] 股票池无有效股票代码')
            return

        # 2. 获取当前大盘涨跌幅，用于根据市场环境动态调整策略阈值
        market_change = 0.0
        try:
            idx_data = await fetch_stock_data('sh000001')
            if idx_data:
                market_change = idx_data.get('parsed', {}).get('changePercent', 0)
        except Exception as e:
            print(f'[智能推荐] 获取大盘数据失败: {str(e)}')

        # 3. 批量扫描行情
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

        # 4. 评分
        for stock in all_results:
            stock['score'] = calc_stock_score_v2(stock, market_change=market_change)
        all_results.sort(key=lambda x: x['score']['total'], reverse=True)

        # 5. 过滤并取前3
        MIN_RECOMMEND_SCORE = 60
        filtered = [s for s in all_results if s['score']['total'] >= MIN_RECOMMEND_SCORE]
        top3 = filtered[:3]
        if not top3:
            print(f'[智能推荐] 无评分>={MIN_RECOMMEND_SCORE}的股票，跳过')
            return

        # 5. 计算买卖点 + 推荐理由
        rec_items = []
        for r in top3:
            bsp = calc_buy_sell_points(r)
            reason = gen_recommend_reason_server(r, {'total': r['score']['total'], 'label': r['score']['label']})
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

        # 6. 保存到数据库（同一日期：09:45 生成后，13:30 会覆盖更新）
        date_key = now.date().isoformat()
        record = {
            'date': date_key,
            'daily_recommendations': rec_items,
        }
        # 加载现有记录，替换同日期的
        existing = load_daily_recommendations()
        existing = [r for r in existing if r.get('date') != date_key]
        existing.insert(0, record)
        # 控制总数
        if len(existing) > QUERY_DAILY_RECOMMENDATIONS_LIMIT:
            existing = existing[:QUERY_DAILY_RECOMMENDATIONS_LIMIT]
        saved = save_daily_recommendations(existing)

        if saved:
            symbols_str = '、'.join([f'{r["name"]}({r["symbol"]})' for r in rec_items])
            print(f'[智能推荐] 生成完成: {symbols_str}，共 {len(rec_items)} 只股票')
        else:
            print('[智能推荐] 保存失败')
        return {"recommendations": rec_items}
    except Exception as error:
        print(f'[智能推荐] 自动生成失败: {str(error)}')
        return None
