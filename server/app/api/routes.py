"""routes - 所有 API 路由定义"""
from fastapi import HTTPException, APIRouter, Request
from fastapi.responses import Response
from typing import List, Dict, Optional, Any
import json, asyncio, time, re, inspect
from datetime import date, datetime
from app.config import (STRATEGY_PREDICT_BULL_MIN_SCORE, STRATEGY_BACKTEST_PREDICTION_LIMIT, STRATEGY_BACKTEST_HISTORY_DAYS, DEFAULT_WEIGHTS, HOT_SEARCH_TOP_N, KLINE_DATA_LIMIT, KLINE_DISPLAY_MA_POINTS, KLINE_START_DATE, INDEX_SYMBOLS, REVIEW_TOP_GAINERS_COUNT, REVIEW_TOP_LOSERS_COUNT, REVIEW_TOMORROW_FOCUS_COUNT, TENCENT_QUOTE_API, TENCENT_KLINE_API, TREND_MIN_SCORE, QUERY_DAILY_RECOMMENDATIONS_LIMIT, QUERY_TREND_RESULTS_LIMIT, QUERY_MARKET_REVIEWS_LIMIT, SCAN_CONCURRENCY, HOT_STOCK_POOL, STRATEGY_INDUSTRY_NEUTRALIZATION_ENABLED, EASTMONEY_HEADERS, STRATEGY_SCORE_VALUES)
from db.database import (get_db_conn, load_daily_recommendations, save_daily_recommendations, load_trend_scan_results, save_trend_scan_results, load_stock_basic_info, save_stock_basic_info, load_stock_alias_map, load_hot_search_ranking, save_hot_search_ranking, load_user_scan_pool, save_user_scan_pool, load_hot_stock_buttons, save_hot_stock_buttons, load_stock_concept_tags, save_stock_concept_tags, load_daily_market_reviews, save_daily_market_reviews, load_strategy_factor_weights, save_strategy_factor_weights, add_stock_concept_tags, remove_stock_tag, FACTOR_DESCRIPTIONS, save_stock_industry, load_trend_backtest_reports, load_latest_trend_backtest_report, load_trend_backtest_records)
from services.stock import (get_http_client, fetch_stock_data, fetch_eastmoney_hot_list, get_stock_name, get_kline_data, parse_tencent_batch_data, fetch_stock_concepts, is_forbidden_stock, calculate_ma, fetch_with_retry)
from services.strategy import backtest_yesterday_predictions, save_predictions, calc_stock_score_v2, apply_industry_neutralization
from services.trend import scan_trend_scan_results, scan_and_remove_below_ma10, calc_pool_stock_indicators
from services.trend_backtest import backtest_trend_predictions
from services.ai import _build_diagnosis_prompt, _call_deepseek_api, _fetch_hot_sectors, DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, load_ai_config
from services.recommend import auto_generate_recommendations_task
from services.review import generate_daily_review
from services.market import predict_market_risk, check_market_crash
from services.utils import ensure_today_date

router = APIRouter()

TENCENT_API = TENCENT_QUOTE_API
REQUEST_TIMEOUT = 8.0
MAX_RETRIES = 2

@router.get("/api/health", tags=["健康检查"])
async def health_check():
    return {"status": "ok", "time": datetime.now().isoformat()}

@router.get("/api/hot_search_ranking", tags=["热搜榜数据"])
async def get_hot_search_ranking_api():
    try:
        data = load_hot_search_ranking()
        return {"success": True, "data": data}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取hot_search_ranking失败: {str(error)}")

@router.get("/api/update-hot_search_ranking", tags=["热搜榜数据"])
async def update_hot_search_ranking_api():
    try:
        print('[热搜榜] 手动触发更新...')
        data = await fetch_eastmoney_hot_list()
        save_hot_search_ranking(data)
        
        return {
            "success": True,
            "message": f"成功更新热搜榜数据，共 {data['filteredCount']} 只股票",
            "data": data
        }
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"更新热搜榜失败: {str(error)}")

@router.get("/api/stock/{symbol}", tags=["股票行情"])
async def get_stock(symbol: str):
    if not re.match(r'^(sh|sz)\d{6}$', symbol.lower()):
        raise HTTPException(status_code=400, detail="无效的股票代码格式，示例: sh603985, sz000001")

    try:
        print(f'[单只查询] {symbol}')
        data = await fetch_with_retry(symbol.lower())
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(content=data, media_type="text/plain")
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"获取股票数据失败: {str(error)}")


@router.get("/api/stock/{symbol}/analysis", tags=["股票行情"])
async def get_stock_analysis(symbol: str):
    """单只股票 9 因子评分分析（2026-07-06 修复 TabQuote 评分始终为 0）

    返回 JSON：含 parse 后的行情数据 + calc_stock_score_v2 算出的 score/label/details。
    与 /api/stock/{symbol}（返回腾讯原始字符串）解耦，避免破坏 IndexBar 等现有调用方。
    前端 TabQuote 在 parseTencentData 拿到行情后并行调用本端点补 score。
    """
    if not re.match(r'^(sh|sz)\d{6}$', symbol.lower()):
        raise HTTPException(status_code=400, detail="无效的股票代码格式，示例: sh603985, sz000001")
    sym = symbol.lower()
    try:
        raw = await fetch_with_retry(sym)
        parsed_list = parse_tencent_batch_data(raw)
        if not parsed_list:
            raise HTTPException(status_code=502, detail="行情数据解析失败")
        stock_data = parsed_list[0]
        # 9 因子评分（V2 权威评分，权重由后端统一维护）
        result = calc_stock_score_v2(stock_data)
        base_scores = result.get('base_scores', {})
        # 构造前端期望的 details 格式：{label(中文), score(实际得分), max(该因子满分)}
        # 前端 TabQuote.vue:125-128 渲染 {{ d.label }} {{ d.score }}/{{ d.max }}
        details = []
        for factor_key, score_val in base_scores.items():
            vals = STRATEGY_SCORE_VALUES.get(factor_key, {})
            numeric_vals = [v for v in vals.values() if isinstance(v, (int, float))]
            max_val = max(numeric_vals) if numeric_vals else 0
            details.append({
                "label": FACTOR_DESCRIPTIONS.get(factor_key, factor_key),  # 中文映射，兜底原 key
                "score": int(score_val) if isinstance(score_val, (int, float)) else 0,
                "max": int(max_val),
            })
        return {
            "success": True,
            "data": {
                "symbol": stock_data.get('symbol'),
                "score": result.get('total', 0),
                "label": result.get('label', '无评分'),
                "predictDirection": result.get('predict_direction', 'neutral'),
                "details": details,
            }
        }
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"分析失败: {str(error)}")

@router.get("/api/stocks", tags=["股票行情"])
async def get_stocks(symbols: str):
    if not symbols:
        raise HTTPException(status_code=400, detail="请提供symbols参数")
    
    symbol_list = symbols.split(',')
    if len(symbol_list) > SCAN_CONCURRENCY * 2:
        raise HTTPException(status_code=400, detail=f"一次最多查询{SCAN_CONCURRENCY * 2}只股票")
    
    try:
        data = await fetch_with_retry(symbols)
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(content=data, media_type="text/plain")
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"批量获取失败: {str(error)}")

@router.get("/api/daily_recommendations", tags=["推荐记录"])
async def get_daily_recommendations():
    try:
        daily_recommendations = load_daily_recommendations()
        # 实时计算历史推荐盈利
        today_str = date.today().isoformat()

        # 性能优化（2026-07-02）：原实现每条历史记录串行 await client.get（最多29次，4-9s）
        # 改为：先收集所有历史 symbols，一次性批量拉取，再回填
        # Step 1: 标记 isToday + 收集所有历史 symbols
        all_history_symbols = []
        for record in daily_recommendations:
            rec_date = record.get('date', '')
            if rec_date >= today_str:
                record['isToday'] = True
                continue
            record['isToday'] = False
            stocks = record.get('daily_recommendations') or record.get('stocks') or []
            for s in stocks:
                sym = s.get('symbol', '')
                if sym:
                    all_history_symbols.append(sym)

        # Step 2: 一次性批量拉取所有历史 symbols 的实时行情
        price_map = {}
        if all_history_symbols:
            try:
                # 去重
                unique_symbols = list(set(all_history_symbols))
                # 分批拉取（每批最多100只，避免URL过长）
                batch_size = 100
                async def _fetch_quote_batch(syms):
                    try:
                        client = get_http_client()
                        codes_str = ','.join(syms)
                        url = f"{TENCENT_QUOTE_API}{codes_str}"
                        resp = await client.get(url, timeout=5)
                        return resp.text.strip()
                    except Exception as _e:
                        print(f'[历史推荐] 行情批量拉取失败: {_e}')
                        return ''

                batch_tasks = [
                    _fetch_quote_batch(unique_symbols[i:i + batch_size])
                    for i in range(0, len(unique_symbols), batch_size)
                ]
                batch_texts = await asyncio.gather(*batch_tasks, return_exceptions=True)

                for text in batch_texts:
                    if isinstance(text, Exception) or not text:
                        continue
                    for line in text.split(';'):
                        line = line.strip()
                        if not line or '=' not in line:
                            continue
                        parts = line.split('~')
                        if len(parts) > 3:
                            raw_code = parts[2].lower().strip()
                            try:
                                cur_price = float(parts[3])
                            except (ValueError, IndexError):
                                cur_price = 0
                            if cur_price > 0:
                                prefix = 'sz' if raw_code.startswith(('0', '3')) else 'sh'
                                full_sym = prefix + raw_code
                                price_map[full_sym] = cur_price
                                price_map[raw_code] = cur_price
            except Exception as _e:
                print(f'[历史推荐] 行情解析失败: {_e}')

        # Step 3: 回填每条记录的 currentPrice + profitRatio
        for record in daily_recommendations:
            if record.get('isToday'):
                continue
            stocks = record.get('daily_recommendations') or record.get('stocks') or []
            for s in stocks:
                sym = s.get('symbol', '').lower()
                cur = price_map.get(sym, 0)
                if cur > 0:
                    s['currentPrice'] = cur
                    buy_price = 0
                    bsp = s.get('buySellPoints')
                    if bsp and bsp.get('buy'):
                        try:
                            buy_price = float(bsp['buy'])
                        except (ValueError, TypeError):
                            pass
                    if buy_price <= 0:
                        try:
                            buy_price = float(s.get('price', 0))
                        except (ValueError, TypeError):
                            pass
                    if buy_price > 0:
                        s['profitRatio'] = round((cur - buy_price) / buy_price * 100, 2)

        return {"success": True, "data": daily_recommendations}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取推荐记录失败: {str(error)}")

@router.post("/api/daily_recommendations", tags=["推荐记录"])
async def post_daily_recommendations(record: Dict):
    try:
        if not record or 'date' not in record or not isinstance(record.get('daily_recommendations'), list):
            raise HTTPException(status_code=400, detail="无效的推荐记录格式")
        
        record['timestamp'] = int(datetime.now().timestamp() * 1000)
        
        daily_recommendations = load_daily_recommendations()
        
        existing_index = -1
        for i, r in enumerate(daily_recommendations):
            if r.get('date') == record['date']:
                existing_index = i
                break
        
        if existing_index != -1:
            daily_recommendations[existing_index] = record
        else:
            daily_recommendations.insert(0, record)
        
        saved = save_daily_recommendations(daily_recommendations)
        
        if saved:
            return {"success": True, "message": "推荐记录保存成功"}
        else:
            raise HTTPException(status_code=500, detail=f"保存推荐记录失败: {str(error)}")
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"保存推荐记录失败: {str(error)}")

@router.delete("/api/daily_recommendations/{date}", tags=["推荐记录"])
async def delete_recommendation(date: str):
    try:
        daily_recommendations = load_daily_recommendations()
        original_length = len(daily_recommendations)
        daily_recommendations = [r for r in daily_recommendations if r.get('date') != date]
        
        if len(daily_recommendations) == original_length:
            raise HTTPException(status_code=404, detail="未找到该日期的推荐记录")
        
        save_daily_recommendations(daily_recommendations)
        
        return {"success": True, "message": "推荐记录删除成功"}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"删除推荐记录失败: {str(error)}")


@router.post("/api/generate-recommendations", tags=["推荐记录"])
async def generate_recommendations_api():
    """手动触发生成智能推荐并返回结果"""
    try:
        result = await auto_generate_recommendations_task()
        if result and result.get('recommendations'):
            return {"success": True, "recommendations": result['recommendations']}
        # 如果任务没有返回数据，尝试从数据库读取今天的推荐
        daily = load_daily_recommendations()
        today = date.today().isoformat()
        for r in daily:
            if r.get('date') == today:
                return {"success": True, "recommendations": r.get('daily_recommendations', [])}
        return {"success": False, "error": "生成推荐失败，请稍后重试"}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"生成推荐失败: {str(error)}")


@router.post("/api/scan-ma10", tags=["趋势发现"])
async def scan_ma10_api():
    """手动触发10日线检查（同时检查用户池和趋势池），便于立即清理跌破10日线的股票"""
    try:
        await scan_and_remove_below_ma10()
        return {"success": True, "message": "已触发10日线检查任务，请查看服务器日志"}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"触发失败: {str(error)}")


def _ensure_today_date(data: Dict) -> Dict:
    # 2026-07-06 C-5: 已合并到 services.utils.ensure_today_date，此处保留薄壳透传以最小化改动
    return ensure_today_date(data)

@router.get("/api/trend-stocks", tags=["趋势发现"])
async def get_trend_scan_results():
    try:
        trend_data = load_trend_scan_results()
        return {"success": True, "data": _ensure_today_date(trend_data)}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取趋势股票失败: {str(error)}")


@router.get("/api/scan-trend-stocks", tags=["趋势发现"])
async def scan_trend_stocks(force: bool = False):
    try:
        result = await scan_trend_scan_results(force=force)
        return {"success": True, "data": _ensure_today_date(result.get("data", result))}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"扫描趋势股票失败: {str(error)}")


# ========== P4-1 趋势评分回测验证 API（2026-07-01）==========
@router.post("/api/trend/backtest", tags=["趋势发现"])
async def run_trend_backtest():
    """手动触发趋势回测验证（扫描所有 verified=0 的历史记录）"""
    try:
        result = await backtest_trend_predictions()
        return {"success": True, "data": result}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"趋势回测失败: {str(error)}")


@router.get("/api/trend/backtest-history", tags=["趋势发现"])
async def get_trend_backtest_history(limit: int = 30):
    """获取趋势回测历史报告列表"""
    try:
        reports = load_trend_backtest_reports(limit=limit)
        return {"success": True, "data": reports}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取回测历史失败: {str(error)}")


@router.get("/api/trend/backtest-latest", tags=["趋势发现"])
async def get_trend_backtest_latest():
    """获取最新一条趋势回测报告（前端徽章用）"""
    try:
        report = load_latest_trend_backtest_report()
        return {"success": True, "data": report}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取最新回测报告失败: {str(error)}")


@router.get("/api/trend/backtest-records", tags=["趋势发现"])
async def get_trend_backtest_records(predict_date: str = None, symbol: str = None, limit: int = 100):
    """获取趋势回测明细记录（支持按日期/symbol 过滤）"""
    try:
        records = load_trend_backtest_records(predict_date=predict_date, symbol=symbol, limit=limit)
        return {"success": True, "data": records}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取回测明细失败: {str(error)}")


@router.get("/api/ai-diagnose/{symbol}", tags=["AI诊断"])
async def ai_diagnose(symbol: str):
    load_ai_config()
    if not re.match(r'^(sh|sz)\d{6}$', symbol.lower()):
        raise HTTPException(status_code=400, detail="无效的股票代码格式")

    symbol = symbol.lower()
    client = get_http_client()

    try:
        raw_data = await fetch_with_retry(symbol, client)
        parsed_list = parse_tencent_batch_data(raw_data)
        if not parsed_list or len(parsed_list) == 0:
            raise HTTPException(status_code=404, detail="获取股票数据失败")

        raw_fields = raw_data
        stock_parsed = parsed_list[0]

        content = ""
        lines = raw_fields.split(';')
        for line in lines:
            if line.startswith('v_') and '=' in line:
                content = line[line.index('"') + 1 : line.rfind('"')]
                break
        fields = content.split('~')

        if len(fields) <= 49:
            raise HTTPException(status_code=404, detail="股票数据不完整")

        price = float(fields[3]) if fields[3] else 0
        yesterday_close = float(fields[4]) if fields[4] else 0
        change_val = price - yesterday_close
        change_pct = (change_val / yesterday_close * 100) if yesterday_close > 0 else 0
        volume_raw = float(fields[36]) if fields[36] else 0
        turnover_raw = float(fields[37]) if fields[37] else 0
        bid1_vol = float(fields[12]) if fields[12] else 0
        ask1_vol = float(fields[22]) if fields[22] else 0
        weibi_val = ((bid1_vol - ask1_vol) / (bid1_vol + ask1_vol) * 100) if (bid1_vol + ask1_vol) > 0 else 0
        avg_price_val = (turnover_raw * 10000) / (volume_raw * 100) if volume_raw > 0 else price
        avg_dev = ((price - avg_price_val) / avg_price_val * 100) if avg_price_val > 0 else 0

        stock_info = {
            'name': fields[1],
            'symbol': symbol,
            'price': price,
            'yesterdayClose': yesterday_close,
            'open': float(fields[5]) if fields[5] else 0,
            'high': float(fields[33]) if fields[33] else 0,
            'low': float(fields[34]) if fields[34] else 0,
            'changePercent': round(change_pct, 2),
            'volume': volume_raw,
            'turnover': turnover_raw,
            'turnoverRate': float(fields[38]) if fields[38] else 0,
            'volumeRatio': float(fields[49]) if fields[49] else 0,
            'pe': float(fields[39]) if fields[39] else 0,
            'outerPlate': float(fields[7]) if fields[7] else 0,
            'innerPlate': float(fields[8]) if fields[8] else 0,
            'weibi': round(weibi_val, 2),
            'amplitude': float(fields[43]) if fields[43] else 0,
            'avgPrice': round(avg_price_val, 2),
            'avgPriceDeviation': round(avg_dev, 2),
            'circulateMarketCap': float(fields[44]) if fields[44] else 0,
            'bid1': float(fields[11]) if fields[11] else 0,
            'bid1Vol': bid1_vol,
            'ask1': float(fields[21]) if fields[21] else 0,
            'ask1Vol': ask1_vol,
        }

        kline_data = await asyncio.wait_for(get_kline_data(symbol, client), timeout=12.0)

        market_data = {}
        for key, info in INDEX_SYMBOLS.items():
            try:
                idx_raw = await fetch_with_retry(info['symbol'], client)
                idx_parsed = parse_tencent_batch_data(idx_raw)
                if idx_parsed and len(idx_parsed) > 0:
                    market_data[key] = {
                        'name': info['name'],
                        'price': idx_parsed[0].get('price', 0),
                        'changePercent': idx_parsed[0].get('changePercent', 0)
                    }
            except Exception as _e:
                print(f'[AI诊断] 大盘指数 {key} 获取失败: {_e}')
                market_data[key] = {'name': info['name'], 'price': 0, 'changePercent': 0}

        # 获取个股概念标签
        try:
            all_tags = load_stock_concept_tags()
            stock_concepts = all_tags.get(symbol, [])
        except Exception as _e:
            print(f'[AI诊断] 概念标签加载失败: {_e}')
            stock_concepts = []

        # 获取热点板块和概念数据
        try:
            hot_sectors = await _fetch_hot_sectors()
        except Exception as e:
            print(f'[AI诊断] 热点板块获取失败: {e}')
            hot_sectors = {'industry': [], 'concept': []}

        prompt = _build_diagnosis_prompt(stock_info, kline_data, market_data, stock_concepts, hot_sectors)
        ai_result = await _call_deepseek_api(prompt)

        try:
            diagnosis = json.loads(ai_result)
        except Exception as _e:
            print(f'[AI诊断] AI 返回非 JSON: {_e}')
            diagnosis = {"raw": ai_result}

        return {
            "success": True,
            "data": {
                "stock": stock_info,
                "diagnosis": diagnosis
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f'AI诊断失败: {str(e)}')
        raise HTTPException(status_code=500, detail=f"AI诊断失败: {str(e)}")

@router.get("/api/ai-config", tags=["AI诊断"])
async def get_ai_config():
    load_ai_config()
    return {
        "success": True,
        "data": {
            "configured": bool(DEEPSEEK_API_KEY) and not DEEPSEEK_API_KEY.startswith("在"),
            "baseUrl": DEEPSEEK_BASE_URL,
            "model": DEEPSEEK_MODEL,
            "apiKeyHint": f"****{DEEPSEEK_API_KEY[-4:]}" if len(DEEPSEEK_API_KEY) > 4 else ""
        }
    }

@router.get("/api/stock-map", tags=["股票映射"])
async def get_stock_basic_info_api():
    try:
        stock_basic_info = load_stock_basic_info()
        name_search = load_stock_alias_map()
        return {"success": True, "data": stock_basic_info, "nameSearch": name_search}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取股票映射失败: {str(error)}")

@router.post("/api/update-stock-map", tags=["股票映射"])
async def update_stock_basic_info_api(data: Dict):
    import services.stock
    try:
        symbol = data.get('symbol')
        name = data.get('name')
        if not symbol or not name:
            raise HTTPException(status_code=400, detail="参数缺失")
        
        if is_forbidden_stock(name):
            return {"success": False, "error": "该股票属于禁忌类别，不予添加"}
        
        stock_basic_info = load_stock_basic_info()
        stock_basic_info[symbol.lower()] = name
        save_stock_basic_info(stock_basic_info)
        services.stock._stock_basic_info_cache = stock_basic_info
        
        return {"success": True}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))

@router.get("/api/custom-scan-pool", tags=["自定义扫描池"])
async def get_user_scan_pool_api():
    try:
        symbols = load_user_scan_pool()
        # 获取每只股票的名称 + 实时价 + 连涨连跌天数 + 是否在5日线上方
        stocks_info = []
        if symbols:
            try:
                # 一次性获取所有实时行情
                symbols_str = ','.join(symbols)
                data = await fetch_with_retry(symbols_str)
                parsed = parse_tencent_batch_data(data)
                quote_map = {p['symbol']: p for p in parsed}
                # 获取每只股票的K线 + 计算指标（并发控制）
                sem = asyncio.Semaphore(8)

                async def fetch_one(sym: str) -> Dict:
                    async with sem:
                        info = {'symbol': sym, 'code': sym, 'name': (quote_map.get(sym, {}) or {}).get('name') or get_stock_name(sym)}
                        realtime = 0
                        try:
                            realtime = float(quote_map.get(sym, {}).get('price', 0) or 0)
                        except (TypeError, ValueError):
                            pass
                        info['realtimePrice'] = realtime
                        try:
                            kline = await get_kline_data(sym)
                            indicators = calc_pool_stock_indicators(kline or [], realtime)
                            # 防御：剥离 coroutine（极端情况）
                            clean = {k: v for k, v in indicators.items() if not inspect.iscoroutine(v)}
                            info.update(clean)
                        except Exception as inner_err:
                            print(f'[custom-scan-pool] {sym} 指标计算失败: {inner_err}')
                            info.update({'latestPrice': realtime, 'consecutiveUp': 0, 'consecutiveDown': 0, 'aboveMa5': False, 'ma5': None})
                        return info

                stocks_info = await asyncio.gather(*[fetch_one(s) for s in symbols])
            except Exception as e:
                import traceback
                print(f'[custom-scan-pool] 拉取行情失败: {e}\n{traceback.format_exc()}')
                stocks_info = [{'symbol': s, 'code': s, 'name': get_stock_name(s), 'latestPrice': 0, 'consecutiveUp': 0, 'consecutiveDown': 0, 'aboveMa5': False, 'ma5': None} for s in symbols]
        return {"success": True, "data": {"symbols": symbols, "count": len(symbols), "stocks": stocks_info}}
    except Exception as error:
        import traceback
        print(f'[custom-scan-pool] 顶层异常: {error}\n{traceback.format_exc()}')
        raise HTTPException(status_code=500, detail=f"获取自定义扫描池失败: {error}")

@router.get("/api/custom-scan-pool/symbols", tags=["自定义扫描池"])
async def get_user_scan_pool_symbols_api():
    """轻量端点：仅返回扫描池代码列表，不拉 K 线/行情
    性能优化（2026-07-02）：TabQuote/TabScan 只需 symbols 列表，
    原先调 /api/custom-scan-pool 会触发 N 次 K 线计算（2-4s 纯浪费）。
    """
    try:
        symbols = load_user_scan_pool()
        return {"success": True, "data": {"symbols": symbols, "count": len(symbols)}}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取扫描池代码列表失败: {error}")

@router.post("/api/custom-scan-pool", tags=["自定义扫描池"])
async def update_user_scan_pool_api(data: Dict):
    try:
        symbols = data.get('symbols', [])
        if not isinstance(symbols, list):
            raise HTTPException(status_code=400, detail="symbols必须是数组")
        
        # 验证股票代码格式
        valid_symbols = []
        for s in symbols:
            if re.match(r'^(sh|sz)\d{6}$', s.lower()):
                valid_symbols.append(s.lower())
        
        save_user_scan_pool(valid_symbols)
        return {"success": True, "data": {"symbols": valid_symbols, "count": len(valid_symbols)}}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))

@router.put("/api/custom-scan-pool/add", tags=["自定义扫描池"])
async def add_to_user_scan_pool_api(data: Dict):
    try:
        # 同时支持 'symbol' 和 'symbols' 参数
        if 'symbol' in data:
            symbols_to_add = [data['symbol']]
        else:
            symbols_to_add = data.get('symbols', [])
            if not isinstance(symbols_to_add, list):
                symbols_to_add = [str(symbols_to_add)]
        
        current_pool = load_user_scan_pool()
        
        for s in symbols_to_add:
            s_lower = s.lower()
            if re.match(r'^(sh|sz)\d{6}$', s_lower) and s_lower not in current_pool:
                current_pool.append(s_lower)
        
        save_user_scan_pool(current_pool)
        return {"success": True, "data": {"symbols": current_pool, "count": len(current_pool)}}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))

@router.put("/api/custom-scan-pool/remove", tags=["自定义扫描池"])
async def remove_from_user_scan_pool_api(data: Dict):
    try:
        # 同时支持 'symbol' 和 'symbols' 参数
        if 'symbol' in data:
            symbols_to_remove = [data['symbol']]
        else:
            symbols_to_remove = data.get('symbols', [])
            if not isinstance(symbols_to_remove, list):
                symbols_to_remove = [str(symbols_to_remove)]
        
        symbols_to_remove = [s.lower() for s in symbols_to_remove]
        current_pool = load_user_scan_pool()
        current_pool = [s for s in current_pool if s not in symbols_to_remove]
        
        save_user_scan_pool(current_pool)
        return {"success": True, "data": {"symbols": current_pool, "count": len(current_pool)}}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))

@router.delete("/api/custom-scan-pool", tags=["自定义扫描池"])
async def clear_user_scan_pool_api():
    try:
        save_user_scan_pool([])
        return {"success": True, "message": "自定义扫描池已清空"}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"清空自定义扫描池失败: {str(error)}")

@router.get("/api/hot-stocks-scan", tags=["股票扫描"])
async def hot_stock_buttons_scan(codes: str = ''):
    t0 = time.time()
    try:
        if not codes:
            return {"error": "请提供codes参数（逗号分隔的股票代码）"}
        
        codes_list = [c for c in codes.split(',') if re.match(r'^(sh|sz)\d{6}$', c.lower())]
        if not codes_list:
            return {"error": "无有效股票代码"}
        
        if len(codes_list) > 100:
            codes_list = codes_list[:100]
        
        print(f'[批量扫描] 开始扫描 {len(codes_list)} 只股票...')

        # 性能优化（2026-07-02）：腾讯行情接口支持批量，单次拉取 100 只以减少 RTT
        # 原 batch_size=20 会产生 5 次请求，改为 100 只 1 次请求
        batch_size = 100
        all_results = []

        async def _fetch_one_batch(batch):
            symbols = ','.join(batch)
            try:
                data = await fetch_with_retry(symbols)
                return parse_tencent_batch_data(data)
            except Exception as e:
                print(f'  [批次] 失败: {e}')
                return []

        batch_tasks = [
            _fetch_one_batch(codes_list[i:i + batch_size])
            for i in range(0, len(codes_list), batch_size)
        ]
        batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)
        for idx, r in enumerate(batch_results, 1):
            if isinstance(r, Exception):
                print(f'  [批次 {idx}] 异常: {r}')
                continue
            all_results.extend(r)

        # 性能优化（2026-07-02）：大盘涨跌幅 + 主力资金流向 并发
        async def _fetch_market_change() -> float:
            try:
                # 优先从缓存获取（如果需要进一步优化可以加缓存逻辑）
                idx_data = await fetch_stock_data('sh000001')
                if idx_data:
                    return idx_data.get('parsed', {}).get('changePercent', 0)
            except Exception as _e:
                print(f'[批量扫描] 大盘涨跌幅获取失败: {_e}')
            return 0.0

        async def _fetch_fund_flow() -> Dict:
            try:
                from services.recommend import fetch_batch_fund_flow
                return await fetch_batch_fund_flow([s.get('symbol', '') for s in all_results])
            except Exception as e:
                print(f'[批量扫描] 资金流向获取失败: {e}')
                return {}

        market_change, fund_flow_map = await asyncio.gather(
            _fetch_market_change(),
            _fetch_fund_flow()
        )

        # 评分（传入 market_change + fund_flow_data，与 recommend 路径对齐）
        for stock in all_results:
            sym = stock.get('symbol', '').lower()
            ff = fund_flow_map.get(sym)
            stock['score'] = calc_stock_score_v2(stock, market_change=market_change, fund_flow_data=ff)

        # P3-2 行业中性化：按行业 P20/P80 分位数调整 strategy_score.total
        # industry_map 来自 fetch_batch_fund_flow 的 f100 字段；样本<5/行业跳过
        if STRATEGY_INDUSTRY_NEUTRALIZATION_ENABLED:
            industry_map = {sym: (ff or {}).get('industry', '') for sym, ff in fund_flow_map.items()}
            apply_industry_neutralization(all_results, industry_map)
            # 持久化行业映射（增量更新，失败不阻塞）
            try:
                save_stock_industry(industry_map)
            except Exception as e:
                print(f'[批量扫描] 行业映射持久化失败（不影响评分）: {e}')

        all_results.sort(key=lambda x: x['score']['total'], reverse=True)

        # 过滤掉60分以下的数据
        all_results = [s for s in all_results if s['score']['total'] >= 60]
        
        # 保存预测记录 - 只记录看涨的
        today_str = date.today().isoformat()
        predictions = []
        for s in all_results:
            score_data = s['score']
            if score_data['predict_direction'] != 'bull':
                continue
            predictions.append({
                'predict_date': today_str,
                'symbol': s['symbol'],
                'name': s['name'],
                'score': score_data['total'],
                'label': score_data['label'],
                'predict_direction': score_data['predict_direction'],
                'outer_ratio': s.get('outerRatio', 0),
                'volume_ratio': s.get('volumeRatio', 0),
                'turnover_rate': s.get('turnoverRate', 0),
                'change_percent': s.get('changePercent', 0),
                'weibi': s.get('weibi', 0),
                'avg_price_deviation': s.get('avgPriceDeviation', 0),
                'amplitude': s.get('amplitude', 0),
                'market_change': market_change,
            })
        if predictions:
            save_predictions(predictions)
        
        response = [
            {
                'name': s['name'],
                'symbol': s['symbol'],
                'code': s['code'],
                'price': s['price'],
                'change': s['change'],
                'changePercent': s['changePercent'],
                'isUp': s['isUp'],
                'outerPlateRaw': s['outerPlateRaw'],
                'innerPlateRaw': s['innerPlateRaw'],
                'volumeRaw': s['volumeRaw'],
                'turnoverRate': s['turnoverRate'],
                'volumeRatio': s['volumeRatio'],
                'amplitude': s['amplitude'],
                'pe': s['pe'],
                'weibi': s['weibi'],
                'outerRatio': s['outerRatio'],
                # P1-1: 附加主力净流入占比(%)，供前端 score.js calculateScore 使用
                'mainInflowRatio': (fund_flow_map.get(s['symbol'].lower(), {}) or {}).get('main_inflow_ratio'),
                # P3-2: 行业中性化调整值（±2/0），供前端展示
                'industryAdjust': s['score'].get('industryAdjust', 0),
                'score': s['score']['total'],
                'scoreLabel': s['score']['label'],
                'predictDirection': s['score']['predict_direction'],
            }
            for s in all_results
        ]
        
        print(f'[批量扫描] 完成: {len(response)} 只, 耗时 {(time.time() - t0):.1f}s')
        return response
    except Exception as error:
        print(f'批量扫描失败: {str(error)}')
        raise HTTPException(status_code=502, detail=f"扫描失败: {str(error)}")

@router.get("/api/hot-stocks", tags=["热门股票"])
async def get_hot_stock_buttons_api():
    try:
        stocks = load_hot_stock_buttons()
        # 获取实时价格用于排序
        if stocks:
            try:
                symbols = ','.join([s['code'] for s in stocks])
                data = await fetch_with_retry(symbols)
                parsed = parse_tencent_batch_data(data)
                price_map = {p['symbol']: p.get('price', 0) for p in parsed}
                for s in stocks:
                    try:
                        s['price'] = float(price_map.get(s['code'], 0) or 0)
                    except (TypeError, ValueError):
                        s['price'] = 0.0
                # 按股价降序排序
                stocks.sort(key=lambda x: x.get('price', 0), reverse=True)
            except Exception as _e:
                print(f'[热门股票] 实时行情批量获取失败，price 置 0: {_e}')
                for s in stocks:
                    s['price'] = 0.0
        return {"success": True, "data": {"stocks": stocks, "count": len(stocks)}}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取热门股票失败: {str(error)}")

@router.post("/api/hot-stocks", tags=["热门股票"])
async def update_hot_stock_buttons_api(data: Dict):
    try:
        stocks = data.get('stocks', [])
        if not isinstance(stocks, list):
            raise HTTPException(status_code=400, detail="stocks必须是数组")
        
        # 验证股票格式
        valid_stocks = []
        for s in stocks:
            if isinstance(s, dict) and s.get('code') and s.get('name'):
                if re.match(r'^(sh|sz)\d{6}$', s['code'].lower()):
                    valid_stocks.append({
                        'code': s['code'].lower(),
                        'name': s['name']
                    })
        
        save_hot_stock_buttons(valid_stocks)
        return {"success": True, "data": {"stocks": valid_stocks, "count": len(valid_stocks)}}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))

@router.put("/api/hot-stocks/add", tags=["热门股票"])
async def add_to_hot_stock_buttons_api(data: Dict):
    try:
        code = data.get('code')
        name = data.get('name')
        
        if not code or not name:
            raise HTTPException(status_code=400, detail="参数缺失：需要code和name")
        
        if not re.match(r'^(sh|sz)\d{6}$', code.lower()):
            raise HTTPException(status_code=400, detail="股票代码格式无效")
        
        current_stocks = load_hot_stock_buttons()
        
        # 检查是否已存在
        exists = any(s['code'].lower() == code.lower() for s in current_stocks)
        if exists:
            return {"success": False, "error": "该股票已在热门股票中"}
        
        current_stocks.append({
            'code': code.lower(),
            'name': name
        })
        
        save_hot_stock_buttons(current_stocks)
        return {"success": True, "data": {"stocks": current_stocks, "count": len(current_stocks)}}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))

@router.put("/api/hot-stocks/remove", tags=["热门股票"])
async def remove_from_hot_stock_buttons_api(data: Dict):
    try:
        code = data.get('code')
        if not code:
            raise HTTPException(status_code=400, detail="参数缺失：需要code")
        
        code_lower = code.lower()
        current_stocks = load_hot_stock_buttons()
        current_stocks = [s for s in current_stocks if s['code'].lower() != code_lower]
        
        save_hot_stock_buttons(current_stocks)
        return {"success": True, "data": {"stocks": current_stocks, "count": len(current_stocks)}}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))

# ========== 股票标签API ==========
@router.get("/api/stock-tags", tags=["股票标签"])
async def get_stock_concept_tags_api():
    try:
        tags = load_stock_concept_tags()
        return {"success": True, "data": {"tags": tags, "count": len(tags)}}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取股票标签失败: {str(error)}")

@router.get("/api/stock-tags/{symbol}", tags=["股票标签"])
async def get_stock_concept_tags_by_symbol_api(symbol: str):
    try:
        tags = load_stock_concept_tags()
        return {"success": True, "data": {"symbol": symbol, "tags": tags.get(symbol.lower(), [])}}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取股票标签失败: {str(error)}")

@router.post("/api/stock-tags/{symbol}", tags=["股票标签"])
async def add_stock_concept_tags_api(symbol: str, data: Dict):
    try:
        tags_to_add = data.get('tags', [])
        if not isinstance(tags_to_add, list):
            tags_to_add = [str(tags_to_add)]
        
        updated_tags = add_stock_concept_tags(symbol, tags_to_add)
        return {"success": True, "data": {"symbol": symbol, "tags": updated_tags}}
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))

@router.delete("/api/stock-tags/{symbol}", tags=["股票标签"])
async def remove_stock_tag_api(symbol: str, data: Dict = {}):
    try:
        tag_to_remove = data.get('tag') if data else None
        if not tag_to_remove:
            # 删除该股票的所有标签
            all_tags = load_stock_concept_tags()
            if symbol.lower() in all_tags:
                del all_tags[symbol.lower()]
                save_stock_concept_tags(all_tags)
            return {"success": True, "data": {"symbol": symbol, "tags": []}}
        else:
            # 删除单个标签
            updated_tags = remove_stock_tag(symbol, tag_to_remove)
            return {"success": True, "data": {"symbol": symbol, "tags": updated_tags}}
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))

@router.get("/api/stock-concepts/{symbol}", tags=["股票标签"])
async def fetch_stock_concepts_api(symbol: str, name: str = None):
    try:
        if not name:
            # 尝试从股票映射表获取名称
            stock_basic_info = load_stock_basic_info()
            name = stock_basic_info.get(symbol.lower(), symbol)
        
        result = await fetch_stock_concepts(symbol, name)
        return {"success": result.get('success', True), "data": result}
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@router.get("/api/market-predict", tags=["大盘预测"])
async def get_market_predict_api():
    """获取大盘预测数据（基于主力资金流向的预测）"""
    try:
        result = await predict_market_risk()
        return {"success": True, "data": result}
    except Exception as error:
        print(f'[大盘预测API] 错误: {str(error)}')
        raise HTTPException(status_code=500, detail=f"获取大盘预测失败: {str(error)}")

@router.get("/api/market-index", tags=["大盘指数"])
async def get_market_index_api():
    """获取大盘指数数据"""
    try:
        result = await check_market_crash()
        return {"success": True, "data": result}
    except Exception as error:
        print(f'[大盘指数API] 错误: {str(error)}')
        raise HTTPException(status_code=500, detail=f"获取大盘指数失败: {str(error)}")

@router.get("/api/kline/{symbol}", tags=["K线数据"])
async def get_kline(symbol: str):
    if not re.match(r'^(sh|sz)\d{6}$', symbol.lower()):
        raise HTTPException(status_code=400, detail="无效的股票代码格式")
    
    try:
        kline_data = await get_kline_data(symbol.lower())
        if not kline_data:
            return {"success": False, "error": "获取K线数据失败或数据不足"}
        
        dates = [k[0] for k in kline_data]
        values = [[float(k[1]), float(k[2]), float(k[4]), float(k[3])] for k in kline_data]
        vols = [float(k[5]) if len(k) > 5 and k[5] else 0 for k in kline_data]
        ma5 = calculate_ma(kline_data, 5)
        ma10 = calculate_ma(kline_data, 10)
        ma20 = calculate_ma(kline_data, 20)
        
        name = await get_stock_name(symbol) or symbol
        
        return {
            "success": True,
            "data": {
                "symbol": symbol,
                "name": name,
                "dates": dates,
                "values": values,
                "vols": vols,
                "ma5": ma5,
                "ma10": ma10,
                "ma20": ma20
            }
        }
    except Exception as error:
        print(f'[K线API] 错误: {str(error)}')
        raise HTTPException(status_code=500, detail=f"获取K线数据失败: {str(error)}")

@router.get("/api/kline/{symbol}/minute", tags=["K线数据"])
async def get_minute_kline(symbol: str):
    if not re.match(r'^(sh|sz)\d{6}$', symbol.lower()):
        raise HTTPException(status_code=400, detail="无效的股票代码格式")
    
    try:
        symbol_lower = symbol.lower()
        url = f'https://ifzq.gtimg.cn/appstock/app/minute/query?_var=min_data&code={symbol_lower}'
        
        response = await get_http_client().get(url)
        response.raise_for_status()
        text = response.text
        
        if not text.startswith('min_data='):
            return {"success": False, "error": "分时数据格式异常"}
        
        json_str = text[len('min_data='):]
        data = json.loads(json_str)
        
        if data.get('code') != 0:
            return {"success": False, "error": f"获取分时数据失败: code={data.get('code')}"}
        
        stock_data = data.get('data', {}).get(symbol_lower, {})
        minute_data = stock_data.get('data', {}).get('data', [])
        qt_data = stock_data.get('qt', {}).get(symbol_lower, [])
        
        if not minute_data:
            return {"success": False, "error": "暂无分时数据"}
        
        times = []
        prices = []
        vols = []
        amounts = []
        
        prev_close = 0
        if len(qt_data) > 4:
            prev_close = float(qt_data[4]) if qt_data[4] else 0
        
        for item in minute_data:
            parts = item.split(' ')
            if len(parts) >= 3:
                time_str = parts[0]
                formatted_time = f'{time_str[:2]}:{time_str[2:4]}'
                price = float(parts[1])
                vol = int(parts[2])
                amt = float(parts[3]) if len(parts) > 3 else 0
                times.append(formatted_time)
                prices.append(price)
                vols.append(vol)
                amounts.append(amt)
        
        name = await get_stock_name(symbol) or symbol
        
        return {
            "success": True,
            "data": {
                "symbol": symbol,
                "name": name,
                "times": times,
                "prices": prices,
                "vols": vols,
                "prevClose": prev_close
            }
        }
    except Exception as error:
        print(f'[分时K线API] 错误: {str(error)}')
        raise HTTPException(status_code=500, detail=f"获取分时数据失败: {str(error)}")


@router.post("/api/daily-review/generate", tags=["收评"])
async def api_generate_daily_review():
    try:
        print('[收评API] 开始生成今日收评...')
        review = await generate_daily_review()
        print('[收评API] 收评生成成功，正在保存...')
        
        save_daily_market_reviews([review])
        print('[收评API] 收评保存完成！')
        
        return {"success": True, "data": review}
    except Exception as error:
        print(f'[收评API] 生成收评失败: {str(error)}')
        import traceback
        print(f'[收评API] 错误堆栈: {traceback.format_exc()}')
        return {"success": False, "error": str(error)}

@router.get("/api/daily-reviews", tags=["收评"])
async def get_daily_market_reviews():
    try:
        reviews = load_daily_market_reviews()
        return {"success": True, "data": reviews}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取收评失败: {str(error)}")

# ========== 策略管理 API ==========

@router.get("/api/strategy/weights", tags=["策略管理"])
async def get_strategy_factor_weights():
    """获取当前策略权重和各因子准确率"""
    try:
        weights = load_strategy_factor_weights()
        result = {}
        for factor, data in weights.items():
            result[factor] = {
                'weight': data['weight'],
                'accuracy': data['accuracy'],
                'sample_count': data['sample_count'],
                'correct_count': data['correct_count'],
                'description': FACTOR_DESCRIPTIONS.get(factor, factor),
            }
        return {"success": True, "data": result}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取策略权重失败: {str(error)}")

@router.post("/api/strategy/weights/reset", tags=["策略管理"])
async def reset_strategy_factor_weights():
    """重置策略权重为默认值"""
    try:
        weights = {k: {'weight': v, 'accuracy': 50.0, 'sample_count': 0, 'correct_count': 0}
                   for k, v in DEFAULT_WEIGHTS.items()}
        save_strategy_factor_weights(weights)
        return {"success": True, "message": "策略权重已重置为默认值"}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"重置策略权重失败: {str(error)}")

@router.post("/api/strategy/backtest", tags=["策略管理"])
async def run_backtest():
    """手动触发回测验证"""
    try:
        result = await backtest_yesterday_predictions()
        return {"success": True, "data": result}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"回测失败: {str(error)}")

@router.get("/api/strategy/backtest-history", tags=["策略管理"])
async def get_backtest_history(days: int = STRATEGY_BACKTEST_HISTORY_DAYS):
    """获取回测历史报告"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM strategy_backtest_reports ORDER BY backtest_date DESC LIMIT ?',
            (days,)
        )
        rows = cursor.fetchall()
        conn.close()
        # 解析 TEXT 字段
        import json
        result_rows = []
        for row in rows:
            d = dict(row)
            if d.get('misjudge_analysis'):
                try:
                    parsed = json.loads(d['misjudge_analysis'])
                    if isinstance(parsed, dict) and 'suggestions' in parsed:
                        parsed['suggestions'] = [
                            s.replace('建议增加委比验证条件', '已增加委比交叉验证：委比>30但股价下跌时降分，委比高但外盘占比<48%或量比<0.8时降分')
                            for s in parsed['suggestions']
                        ]
                    d['misjudge_analysis'] = parsed
                except Exception as _e:
                    print(f'[回测历史] misjudge_analysis JSON 解析失败(id={d.get("id")}): {_e}')
            if d.get('weight_adjustments'):
                try:
                    d['weight_adjustments'] = json.loads(d['weight_adjustments'])
                except Exception as _e:
                    print(f'[回测历史] weight_adjustments JSON 解析失败(id={d.get("id")}): {_e}')
            result_rows.append(d)
        return {"success": True, "data": result_rows}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取回测历史失败: {str(error)}")

@router.get("/api/strategy/predictions", tags=["策略管理"])
async def get_predictions(date_str: str = None, verified: int = None, include_today_and_pending: bool = False):
    """获取预测记录"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        sql = 'SELECT * FROM strategy_predict_records WHERE 1=1'
        params = []
        
        if include_today_and_pending:
            import datetime
            today = datetime.datetime.now().strftime('%Y-%m-%d')
            sql += ' AND (predict_date = ? OR verified = 0)'
            params.append(today)
        else:
            if date_str:
                sql += ' AND predict_date = ?'
                params.append(date_str)
            if verified is not None:
                sql += ' AND verified = ?'
                params.append(verified)
                
        sql += f' ORDER BY predict_date DESC, score DESC LIMIT {STRATEGY_BACKTEST_PREDICTION_LIMIT}'
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        return {"success": True, "data": rows}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取预测记录失败: {str(error)}")

# ========== 东方财富 API 代理（兼容生产模式） ==========
# 前端 fetchEnrichData() 需要通过 /eastmoney/* 调用东方财富接口
# Vite 开发模式下由 vite.config.js 代理；生产模式下由此路由代理
# 2026-07-02 C-5: 请求头统一使用 config.EASTMONEY_HEADERS
# 2026-07-06 A-P3-2: 模块级 requests.Session 复用连接池，避免每次请求新建 TCP 连接
import requests as _req
_eastmoney_session: Optional[_req.Session] = None

def _get_eastmoney_session() -> _req.Session:
    """获取模块级 requests.Session（懒初始化，复用连接池）"""
    global _eastmoney_session
    if _eastmoney_session is None:
        _eastmoney_session = _req.Session()
        _eastmoney_session.headers.update(EASTMONEY_HEADERS)
        # 配置 HTTPAdapter 复用连接池（pool_connections=连接数, pool_maxsize=最大并发）
        adapter = _req.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=0)
        _eastmoney_session.mount('https://', adapter)
        _eastmoney_session.mount('http://', adapter)
    return _eastmoney_session

@router.api_route("/eastmoney/{path:path}", methods=["GET", "POST"])
async def proxy_eastmoney(request: Request, path: str):
    """将 /eastmoney/* 请求转发到东方财富 API（requests Session + 重试，解决 RemoteDisconnected）"""
    import asyncio, time as _time
    target_url = f"https://push2delay.eastmoney.com/{path}"
    query_params = dict(request.query_params)
    session = _get_eastmoney_session()

    def _do_request():
        last_err = None
        for attempt in range(3):
            try:
                if request.method == "GET":
                    return session.get(target_url, params=query_params, timeout=15)
                else:
                    body_bytes = request._body
                    return session.post(target_url, params=query_params, data=body_bytes, timeout=15)
            except Exception as e:
                last_err = e
                if attempt < 2:
                    _time.sleep(0.5 * (attempt + 1))  # 0.5s, 1.0s 退避
        raise last_err

    try:
        resp = await asyncio.to_thread(_do_request)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers={"Content-Type": resp.headers.get("content-type", "application/json")},
        )
    except Exception as e:
        print(f'[东方财富代理] 请求失败: {e}')
        return Response(content=b"{}", status_code=502, media_type="application/json")

# 最后挂载静态文件，这样API路由会先被匹配到

