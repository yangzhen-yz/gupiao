"""Domain router: stock."""

from fastapi import APIRouter
from ..routes_state import *

router = APIRouter()

@router.get("/api/stock/{symbol}", tags=["股票行情"])
async def get_stock(symbol: str):
    if not re.match(r'^(sh|sz)\d{6}$', symbol.lower()):
        raise HTTPException(status_code=400, detail="无效的股票代码格式，示例: sh603985, sz000001")

    try:
        print(f'[单只查询] {symbol}')
        data = await fetch_with_retry(symbol.lower())
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(content=data, media_type="text/plain",
                                 headers={'X-Quote-Source': detect_quote_source(data)})
    except Exception as error:
        err_msg = str(error) or repr(error) or '未知错误'
        raise HTTPException(status_code=502, detail=f"获取股票数据失败: {err_msg}")


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
        # 10 因子评分（V2 权威评分，权重由后端统一维护）
        # : 从 DB 缓存查股东人数（月度数据，无需实时拉取）
        from services.data.shareholder import get_holder_num_batch
        holder_map = get_holder_num_batch([sym])
        hd = holder_map.get(sym)
        rps_map = await get_rps_map()
        rps = rps_map.get(normalize_local_symbol(sym))
        # P1a/P1b: 基本面质量 + 聪明钱(龙虎榜)注入
        fd = get_fundamental_batch([sym]).get(sym)
        dt_map = await asyncio.to_thread(get_dragon_tiger_symbol_map, SMART_MONEY_DRAGON_TIGER_DAYS)
        sm = dt_map.get(sym)
        sm_data = {'on_board': True, 'net_amt': (sm or {}).get('net_amt', 0),
                   'appearances': (sm or {}).get('appearances', 0)} if sm else None
        from services.strategy.trend import compute_trend_result
        _px = float(stock_data.get('priceRaw') or stock_data.get('price') or 0)
        tr = await asyncio.to_thread(compute_trend_result, sym, _px)
        result = calc_stock_score_v2(stock_data, holder_data=hd, rps_data={'rps': rps},
                                     fundamental_data=fd, smart_money_data=sm_data,
                                     trend_result=tr)
        # P2c/P2a: 估值分位 + 风控止损
        val = get_valuation_batch([sym]).get(sym)
        _holder_ratio = (hd or {}).get('total_num_ratio')
        risk_control = calc_risk_control(stock_data.get('price', 0), score_result=result,
                                         stock=stock_data, valuation=val,
                                         smart_money=sm_data, holder_ratio=_holder_ratio)
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
        # 买卖点（预期涨幅，V4.6 位置感知版）
        buy_sell_points = _recommend_svc.calc_buy_sell_points(stock_data, score_result=result)
        return {
            "success": True,
            "data": {
                "symbol": stock_data.get('symbol'),
                "quoteSource": stock_data.get('quoteSource', 'tencent'),
                "score": result.get('total', 0),
                "label": result.get('label', '无评分'),
                "predictDirection": result.get('predict_direction', 'neutral'),
                "rps": rps,
                "fundamental": fd,
                "smartMoney": sm_data,
                "valuation": val,
                "riskControl": risk_control,
                "buySellPoints": buy_sell_points,
                "details": details,
            }
        }
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"分析失败: {str(error)}")


@router.get("/api/stock/{symbol}/full", tags=["股票行情"])
async def get_stock_full(symbol: str):
    """单只股票行情+评分合并接口（2026-07-08 性能优化）

    替代原先 2 个并行请求（/api/stock/{symbol} + /api/stock/{symbol}/analysis），
    一次返回行情原始字符串 + 解析数据 + 10因子评分，减少 50% HTTP 请求数。
    评分结果带 3 秒内存缓存，适配 2 秒轮询。
    """
    if not re.match(r'^(sh|sz)\d{6}$', symbol.lower()):
        raise HTTPException(status_code=400, detail="无效的股票代码格式，示例: sh603985, sz000001")
    sym = symbol.lower()
    try:
        # 评分缓存（3 秒 TTL，适配 2 秒轮询，偶数次命中缓存）
        import time as _time; from services.data.stock import _analysis_cache
        now = _time.time()
        cached = _analysis_cache.get(sym)
        if cached and (now - cached[0]) < 3.0:
            raw, result, rps, fd, sm_data, val, risk_control = cached[1:8]
        else:
            # 2026-11-04：低频增强数据统一加超时闸门（stale-while-revalidate）。
            # 冷路径分项实测：本地K线引擎首次初始化 6.7~8.0s、history_available() 全表统计
            # 1.3~8s、龙虎榜东财 15s、腾讯行情重试最多 ~7s；旧写法把这些全串在单票请求
            # 关键路径上，线上切票实测 16.7s / 23.9s。wait_for 只取消 Future，to_thread 的
            # 线程会继续跑完并写各自缓存，下一次 2s 轮询自动补齐 → 首屏快、最终数据完整。
            _ENRICH_TIMEOUT = 1.2
            _timed_out: list = []

            async def _guarded(label, coro, default):
                try:
                    return await asyncio.wait_for(coro, timeout=_ENRICH_TIMEOUT)
                except asyncio.TimeoutError:
                    _timed_out.append(label)
                    return default
                except Exception as e:
                    print(f'[单票全量] 增强数据降级 {label}: {type(e).__name__}: {e}')
                    return default

            try:
                raw = await asyncio.wait_for(fetch_with_retry(sym), timeout=3.0)
            except asyncio.TimeoutError:
                # 腾讯/新浪都卡住时，宁可回一份 10 分钟内的旧快照，也不让用户卡 spinner
                from services.data.stock import _realtime_cache as _qcache
                stale = _qcache.get(sym)
                if stale and stale[1] and (time.time() - stale[0]) < 600:
                    raw = stale[1]
                else:
                    raise HTTPException(status_code=504, detail='行情源响应超时，请稍后重试')
            parsed_list = parse_tencent_batch_data(raw)
            if not parsed_list:
                raise HTTPException(status_code=502, detail="行情数据解析失败")
            stock_data = parsed_list[0]
            from services.data.shareholder import get_holder_num_batch
            # 2026-07-20 A2：低频/重 IO 并行拉取，缩短尾延迟
            # - get_rps_map 为 async（腾讯多股批量）
            # - get_holder_num_batch / get_fundamental_batch / get_dragon_tiger_symbol_map /
            #   get_valuation_batch 均为同步 IO 或本地 DB 查询，用 to_thread 并发执行
            rps_map, holder_map, fd_map, dt_map, val_map = await asyncio.gather(
                _guarded('rps', get_rps_map(), {}),
                _guarded('holder', asyncio.to_thread(get_holder_num_batch, [sym]), {}),
                _guarded('fundamental', asyncio.to_thread(get_fundamental_batch, [sym]), {}),
                _guarded('dragon', asyncio.to_thread(get_dragon_tiger_symbol_map, SMART_MONEY_DRAGON_TIGER_DAYS), {}),
                _guarded('valuation', asyncio.to_thread(get_valuation_batch, [sym]), {}),
            )
            hd = holder_map.get(sym)
            rps = rps_map.get(normalize_local_symbol(sym))
            # P1a/P1b: 基本面质量 + 聪明钱(龙虎榜)注入
            fd = fd_map.get(sym)
            sm = dt_map.get(sym)
            sm_data = {'on_board': True, 'net_amt': (sm or {}).get('net_amt', 0),
                       'appearances': (sm or {}).get('appearances', 0)} if sm else None
            from services.strategy.trend import compute_trend_result
            _px = float(stock_data.get('priceRaw') or stock_data.get('price') or 0)
            tr = await _guarded('trend', asyncio.to_thread(compute_trend_result, sym, _px), None)
            result = calc_stock_score_v2(stock_data, holder_data=hd, rps_data={'rps': rps},
                                         fundamental_data=fd, smart_money_data=sm_data,
                                         trend_result=tr)
            # P2c/P2a: 估值分位 + 风控止损
            val = val_map.get(sym)
            _holder_ratio = (hd or {}).get('total_num_ratio')
            risk_control = calc_risk_control(stock_data.get('price', 0), score_result=result,
                                             stock=stock_data, valuation=val,
                                             smart_money=sm_data, holder_ratio=_holder_ratio)
            # 只在完整路径写缓存：降级结果不占 3s 缓存，下一次轮询立即重算补齐
            if not _timed_out:
                _analysis_cache[sym] = (now, raw, result, rps, fd, sm_data, val, risk_control)
                # 清理过期缓存（懒惰清理，超过 100 条时）
                if len(_analysis_cache) > 100:
                    _analysis_cache.clear()

        base_scores = result.get('base_scores', {})
        details = []
        for factor_key, score_val in base_scores.items():
            vals = STRATEGY_SCORE_VALUES.get(factor_key, {})
            numeric_vals = [v for v in vals.values() if isinstance(v, (int, float))]
            max_val = max(numeric_vals) if numeric_vals else 0
            details.append({
                "label": FACTOR_DESCRIPTIONS.get(factor_key, factor_key),
                "score": int(score_val) if isinstance(score_val, (int, float)) else 0,
                "max": int(max_val),
            })
        # 买卖点（V4.6 位置感知版）：raw 始终可用（缓存命中或新拉取），解析后纯函数计算
        _parsed_full = parse_tencent_batch_data(raw) if raw else []
        buy_sell_points = _recommend_svc.calc_buy_sell_points(_parsed_full[0], score_result=result) if _parsed_full else {}
        return {
            "success": True,
            "data": {
                "symbol": sym,
                "raw": raw,  # 腾讯原始行情字符串，前端 parseTencentData 解析
                "quoteSource": detect_quote_source(raw),  # 腾讯主源 / 新浪兜底（缓存命中也能判）
                "score": result.get('total', 0),
                "label": result.get('label', '无评分'),
                "predictDirection": result.get('predict_direction', 'neutral'),
                "rps": rps,
                "fundamental": fd,
                "smartMoney": sm_data,
                "valuation": val,
                "riskControl": risk_control,
                "buySellPoints": buy_sell_points,
                "details": details,
            }
        }
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取全量行情失败: {str(error)}")


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
        return PlainTextResponse(content=data, media_type="text/plain",
                                 headers={'X-Quote-Source': detect_quote_source(data)})
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"批量获取失败: {str(error)}")


@router.get("/api/stock-search", tags=["股票行情"])
async def search_stocks_api(q: str = ''):
    """股票模糊搜索（2026-10-12）：输入代码或名称（支持部分匹配），返回候选列表

    解决"记不住编码、要手查名称"的痛点：输入"美诺"即可匹配到"美诺华"。
    数据源：stock_basic_info（742只）+ stock_alias_map + 自选热股按钮 + 扫描池；
    代码支持纯6位（603538→sh603538）；结果按 精确代码>名称精确>名称前缀>名称包含 排序，
    批量附带实时价（选中即见现价），最多返回10条。
    """
    q = (q or '').strip()
    if not q:
        return {"success": True, "data": []}
    try:
        basic = load_stock_basic_info()        # {symbol: name}
        alias = load_stock_alias_map()         # {name: symbol}
        extra = {}                             # symbol -> name（热股/扫描池补充）
        for b in load_hot_stock_buttons():
            if b.get('code'):
                extra[b['code'].lower()] = b.get('name') or ''
        for s in load_user_scan_pool():
            extra.setdefault(s.lower(), basic.get(s.lower(), ''))
        for name, sym in alias.items():
            extra.setdefault(sym.lower(), name)

        q_lower = q.lower()
        is_code_query = bool(re.fullmatch(r'(sh|sz|bj)?\d{1,6}', q_lower))
        code_q = re.sub(r'^(sh|sz|bj)', '', q_lower) if is_code_query else ''

        hits = {}  # symbol -> {'symbol','name','industry','source','rank'}
        def _add(sym, name, rank):
            sym = sym.lower()
            # 显示名统一用权威名（basic_info），别名只参与匹配不参与显示
            # （2026-10-12 修复：alias_map 里手工别名"生益"精确匹配盖过权威名"生益科技"）
            display = basic.get(sym) or name
            if sym not in hits or hits[sym]['rank'] > rank:
                hits[sym] = {'symbol': sym, 'name': display, 'rank': rank}
        if is_code_query:
            # 代码查询：精确 > 前缀
            for sym in basic:
                pure = re.sub(r'^(sh|sz|bj)', '', sym)
                if pure == code_q:
                    _add(sym, basic[sym], 0)
                elif pure.startswith(code_q):
                    _add(sym, basic[sym], 3)
            for sym in extra:
                pure = re.sub(r'^(sh|sz|bj)', '', sym)
                if pure == code_q:
                    _add(sym, extra[sym], 1)
                elif pure.startswith(code_q):
                    _add(sym, extra[sym], 4)
        else:
            # 名称查询：精确 > 前缀 > 包含
            for sym, name in basic.items():
                if not name:
                    continue
                if name == q:
                    _add(sym, name, 0)
                elif name.startswith(q):
                    _add(sym, name, 1)
                elif q in name:
                    _add(sym, name, 2)
            for name, sym in alias.items():
                if name == q:
                    _add(sym, name, 0)
                elif q in name:
                    _add(sym, name, 2)

        results = sorted(hits.values(), key=lambda x: (x['rank'], x['symbol']))[:10]
        # 查询显式带交易所前缀时，该市场排前（本地路径同样适用：sz000001 优先平安银行）
        _q_prefix = q.lower()[:2] if q.lower()[:2] in ('sh', 'sz', 'bj') else ''
        if _q_prefix:
            results.sort(key=lambda x: 0 if x['symbol'].startswith(_q_prefix) else 1)

        # 本地库覆盖不到时（stock_basic_info 仅742只历史积累），回退腾讯 smartbox 远程搜索，
        # 并把新股票增量回写本地库（越用越全）——解决"新乡化纤/000949 本地查不到"（2026-10-12 用户反馈）
        if not results:
            results = await _search_stocks_remote(q)
            if results:
                try:
                    from db.database import add_stock_basic_info
                    for r in results:
                        add_stock_basic_info(r['symbol'], r['name'])
                except Exception:
                    pass
        if not results:
            return {"success": True, "data": []}

        # 批量附带实时价 + 权威名称（行情接口的名字最权威，覆盖别名/简称）
        price_map = {}
        name_map = {}
        try:
            raw = await fetch_with_retry(','.join(r['symbol'] for r in results))
            for p in parse_tencent_batch_data(raw):
                price_map[p['symbol'].lower()] = p.get('priceRaw') or p.get('price')
                if p.get('name'):
                    name_map[p['symbol'].lower()] = p['name']
        except Exception:
            pass
        for r in results:
            r['price'] = price_map.get(r['symbol'])
            # 行情名优先（修复"生益"简称显示问题）；远程回写本地库也借此纠正旧名
            auth_name = name_map.get(r['symbol'])
            if auth_name and auth_name != r['name']:
                try:
                    from db.database import add_stock_basic_info
                    add_stock_basic_info(r['symbol'], auth_name)
                except Exception:
                    pass
                r['name'] = auth_name
            r.pop('rank', None)
        return {"success": True, "data": results}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"股票搜索失败: {str(error)}")

