"""review.py - split from main.py"""
import json, httpx
from typing import List, Dict
from datetime import date, datetime, timedelta
from app.config import STRATEGY_PREDICT_BULL_MIN_SCORE, REVIEW_TOP_GAINERS_COUNT, REVIEW_TOP_LOSERS_COUNT, REVIEW_TOP_SECTORS_COUNT, REVIEW_TOMORROW_FOCUS_COUNT, INDEX_SYMBOLS, TENCENT_QUOTE_API, MARKET_CRASH_THRESHOLD
from db.database import get_db_conn, save_daily_market_reviews
from services.stock import get_http_client, fetch_stock_data

_EM_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Referer': 'https://quote.eastmoney.com/',
}


async def generate_daily_review() -> Dict:
    try:
        today_str = datetime.now().strftime('%Y-%m-%d')
        
        # 1. 获取大盘指数数据
        market_data = {}
        indices = [
            {'code': 'sh000001', 'name': '上证指数'},
            {'code': 'sz399001', 'name': '深证成指'},
            {'code': 'sz399006', 'name': '创业板指'}
        ]
        for idx in indices:
            try:
                idx_data = await fetch_stock_data(idx['code'])
                if idx_data:
                    parsed = idx_data.get('parsed', {})
                    market_data[idx['code']] = {
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
        
        # 2. 获取行业板块涨跌排行（东方财富API）
        industry_sectors = []
        concept_sectors = []
        try:
            async with httpx.AsyncClient(timeout=10, headers=_EM_HEADERS) as client:
                # 行业板块
                ind_url = 'https://push2.eastmoney.com/api/qt/clist/get?fid=f3&po=1&pz=50&pn=1&np=1&fltt=2&invt=2&ut=b2884a393a59ad64002292a3e90d46a5&fs=m:90+t:2+f:!50'
                ind_resp = await client.get(ind_url)
                ind_json = ind_resp.json()
                if ind_json.get('data') and ind_json['data'].get('diff'):
                    for item in ind_json['data']['diff']:
                        industry_sectors.append({
                            'name': item.get('f14', ''),
                            'code': item.get('f12', ''),
                            'changePercent': item.get('f3', 0),
                            'mainNetInflow': item.get('f62', 0),
                            'volume': item.get('f5', 0),
                            'amount': item.get('f6', 0),
                            'riseCount': item.get('f104', 0),
                            'fallCount': item.get('f105', 0),
                        })
                
                # 概念板块
                con_url = 'https://push2.eastmoney.com/api/qt/clist/get?fid=f3&po=1&pz=50&pn=1&np=1&fltt=2&invt=2&ut=b2884a393a59ad64002292a3e90d46a5&fs=m:90+t:3+f:!50'
                con_resp = await client.get(con_url)
                con_json = con_resp.json()
                if con_json.get('data') and con_json['data'].get('diff'):
                    for item in con_json['data']['diff']:
                        concept_sectors.append({
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
            print(f'获取板块数据失败: {e}')
        
        # 3. 获取涨跌停个股（东方财富API）
        limit_up_stocks = []
        limit_down_stocks = []
        top_gainers = []
        top_losers = []
        try:
            async with httpx.AsyncClient(timeout=15, headers=_EM_HEADERS) as client:
                # 涨停股：取涨幅前200名，筛选涨幅>=9.8%
                zt_url = 'https://push2.eastmoney.com/api/qt/clist/get?fid=f3&po=1&pz=200&pn=1&np=1&fltt=2&invt=2&ut=b2884a393a59ad64002292a3e90d46a5&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048&fields=f2,f3,f8,f12,f14,f62'
                zt_resp = await client.get(zt_url)
                zt_json = zt_resp.json()
                if zt_json.get('data') and zt_json['data'].get('diff'):
                    for item in zt_json['data']['diff']:
                        pct = item.get('f3', 0)
                        if pct is not None and pct >= 9.8:
                            limit_up_stocks.append({
                                'name': item.get('f14', ''),
                                'code': item.get('f12', ''),
                                'changePercent': pct,
                                'price': item.get('f2', 0),
                                'mainNetInflow': item.get('f62', 0),
                                'turnoverRate': item.get('f8', 0),
                            })
                print(f'[收评] 涨停股获取: {len(limit_up_stocks)} 只')
                
                # 跌停股：取跌幅前200名，筛选涨幅<=-9.8%
                dt_url = 'https://push2.eastmoney.com/api/qt/clist/get?fid=f3&po=0&pz=200&pn=1&np=1&fltt=2&invt=2&ut=b2884a393a59ad64002292a3e90d46a5&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048&fields=f2,f3,f8,f12,f14,f62'
                dt_resp = await client.get(dt_url)
                dt_json = dt_resp.json()
                if dt_json.get('data') and dt_json['data'].get('diff'):
                    for item in dt_json['data']['diff']:
                        pct = item.get('f3', 0)
                        if pct is not None and pct <= -9.8:
                            limit_down_stocks.append({
                                'name': item.get('f14', ''),
                                'code': item.get('f12', ''),
                                'changePercent': pct,
                                'price': item.get('f2', 0),
                                'mainNetInflow': item.get('f62', 0),
                                'turnoverRate': item.get('f8', 0),
                            })
                print(f'[收评] 跌停股获取: {len(limit_down_stocks)} 只')
                
                # 涨幅前20（非涨停）
                gain_url = 'https://push2.eastmoney.com/api/qt/clist/get?fid=f3&po=1&pz=20&pn=1&np=1&fltt=2&invt=2&ut=b2884a393a59ad64002292a3e90d46a5&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048&fields=f2,f3,f4,f5,f6,f7,f8,f12,f14,f15,f16,f17,f62,f184,f66,f69,f72,f75,f78,f81'
                gain_resp = await client.get(gain_url)
                gain_json = gain_resp.json()
                if gain_json.get('data') and gain_json['data'].get('diff'):
                    for item in gain_json['data']['diff']:
                        top_gainers.append({
                            'name': item.get('f14', ''),
                            'code': item.get('f12', ''),
                            'changePercent': item.get('f3', 0),
                            'price': item.get('f2', 0),
                            'volumeRatio': item.get('f7', 0),
                            'turnoverRate': item.get('f8', 0),
                            'mainNetInflow': item.get('f62', 0),
                            'amplitude': item.get('f7', 0),
                        })
                
                # 跌幅前20
                lose_url = 'https://push2.eastmoney.com/api/qt/clist/get?fid=f3&po=0&pz=20&pn=1&np=1&fltt=2&invt=2&ut=b2884a393a59ad64002292a3e90d46a5&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048&fields=f2,f3,f4,f5,f6,f7,f8,f12,f14,f15,f16,f17,f62,f184,f66,f69,f72,f75,f78,f81'
                lose_resp = await client.get(lose_url)
                lose_json = lose_resp.json()
                if lose_json.get('data') and lose_json['data'].get('diff'):
                    for item in lose_json['data']['diff']:
                        top_losers.append({
                            'name': item.get('f14', ''),
                            'code': item.get('f12', ''),
                            'changePercent': item.get('f3', 0),
                            'price': item.get('f2', 0),
                            'volumeRatio': item.get('f7', 0),
                            'turnoverRate': item.get('f8', 0),
                            'mainNetInflow': item.get('f62', 0),
                            'amplitude': item.get('f7', 0),
                        })
        except Exception as e:
            print(f'获取涨跌停数据失败: {e}')
        
        # 4. 明日关注个股（结合策略预测 + 今日强势股）
        tomorrow_focus = []
        try:
            # 从今日预测记录中选取高分看涨的票
            conn = get_db_conn()
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT * FROM strategy_predict_records WHERE predict_date = ? AND predict_direction = 'bull' AND score >= {STRATEGY_PREDICT_BULL_MIN_SCORE} ORDER BY score DESC LIMIT {REVIEW_TOMORROW_FOCUS_COUNT}",
                (today_str,)
            )
            predictions = cursor.fetchall()
            conn.close()
            for p in predictions:
                tomorrow_focus.append({
                    'name': p['name'],
                    'code': p['symbol'],
                    'score': p['score'],
                    'reason': f"评分{p['score']}分，外盘占比{float(p['outer_ratio']):.1f}%，量比{float(p['volume_ratio']):.2f}，委比{float(p['weibi']):.1f}%",
                    'predictDirection': 'bull',
                })
        except Exception as e:
            print(f'获取明日关注失败: {e}')
        
        # 如果策略预测不够，从涨幅前列补充
        if len(tomorrow_focus) < 5:
            for g in top_gainers[:REVIEW_TOP_SECTORS_COUNT]:
                if not any(f['code'] == g['code'] for f in tomorrow_focus):
                    tomorrow_focus.append({
                        'name': g['name'],
                        'code': g['code'],
                        'score': 0,
                        'reason': f"今日涨幅{g['changePercent']:.2f}%，资金净流入{g.get('mainNetInflow', 0):.0f}万",
                        'predictDirection': 'bull',
                    })
        
        # 5. 生成收评
        review = {
            'date': today_str,
            'timestamp': datetime.now().isoformat(),
            'market': market_data,
            'industrySectors': industry_sectors[:20],
            'conceptSectors': concept_sectors[:20],
            'limitUpStocks': limit_up_stocks[:20],
            'limitDownStocks': limit_down_stocks[:20],
            'topGainers': top_gainers[:REVIEW_TOP_GAINERS_COUNT],
            'topLosers': top_losers[:REVIEW_TOP_LOSERS_COUNT],
            'tomorrowFocus': tomorrow_focus[:10],
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
                       limit_up, limit_down, top_gainers, top_losers, tomorrow_focus):
    """规则引擎：根据市场/板块/涨跌停/涨跌幅生成情绪评分 + 智能分析结论"""
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

    # ---- 4) 风险提示 ----
    risk_level = 'low'
    risk_title = '暂无明显风险'
    risk_detail = ''

    if top_gainers and top_gainers[0].get('changePercent', 0) > 19:
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

    # ---- 5) 亮点摘要 ----
    highlights = []
    if factors:
        highlights.append('情绪驱动: ' + '、'.join(factors[:3]))
    if industry_sectors:
        top = industry_sectors[0]
        highlights.append(f'领涨板块: {top["name"]} +{top["changePercent"]:.2f}%')
    if limit_up and len(limit_up) > 0:
        # 涨停股按 changePercent 排序找连板高度
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
