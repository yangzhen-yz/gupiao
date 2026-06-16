"""review.py - split from main.py"""
import json, httpx
from typing import List, Dict
from datetime import date, datetime, timedelta
from app.config import STRATEGY_PREDICT_BULL_MIN_SCORE, REVIEW_TOP_GAINERS_COUNT, REVIEW_TOP_LOSERS_COUNT, REVIEW_TOP_SECTORS_COUNT, REVIEW_TOMORROW_FOCUS_COUNT, INDEX_SYMBOLS, TENCENT_QUOTE_API, MARKET_CRASH_THRESHOLD
from db.database import get_db_conn, save_daily_market_reviews
from services.stock import get_http_client, fetch_stock_data

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
            async with httpx.AsyncClient(timeout=10) as client:
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
            async with httpx.AsyncClient(timeout=15) as client:
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
        
        return review
    except Exception as error:
        print(f'生成收评失败: {str(error)}')
        import traceback
        print(f'[收评] 错误堆栈: {traceback.format_exc()}')
        raise
