"""Domain router: scan."""

from fastapi import APIRouter
from ..routes_state import *

router = APIRouter()

@router.get("/api/tab-scan-config", tags=["配置数据"])
async def get_tab_scan_config():
    """股票筛选 Tab 过滤配置（2026-07-08）
    前端下拉框/阈值从后端读取，避免硬编码；与 config.py 单一源保持一致。
    2026-10-24：最低分默认值改由配置页（app_settings）管理，这里每次现读。
    """
    from app.config import (
        TAB_SCAN_FILTER_MODES, TAB_SCAN_DEFAULT_MODE,
        TAB_SCAN_BULL_MIN_SCORE, TAB_SCAN_BEAR_MAX_SCORE,
    )
    from services.support import app_settings as A
    return {
        "success": True,
        "data": {
            "filterModes": TAB_SCAN_FILTER_MODES,
            "defaultMode": TAB_SCAN_DEFAULT_MODE,
            "defaultMinScore": A.value('tab_scan_default_min_score'),
            "bullMinScore": TAB_SCAN_BULL_MIN_SCORE,
            "bearMaxScore": TAB_SCAN_BEAR_MAX_SCORE,
            # 2026-10-28 角色体系下线：不再下发 leadersMinGain / leaders_only（仅优质龙头）模式
            "modeLabels": {
                "all": "全部",
                "bull_only": "仅看涨",
                "bull_and_hold": "观望以上",
                "min_score": "自定义最低分",
            },
        }
    }


@router.get("/api/custom-scan-pool", tags=["自定义扫描池"])
async def get_user_scan_pool_api(user: Dict = Depends(current_user)):
    """扫描池详情（2026-11-04 加 60s SWR）。

    原实现对池里每只票各拉一次日K（并发 8），热态实测 0.73s，是趋势发现页
    首屏最重的一项；而池子详情是分钟级信息，切页重复取没有必要。过期先返旧值、
    后台刷新；写接口走 _invalidate_hot_scan_cache() 一并把这份缓存失效。
    """
    uid = int(user.get('id') or 1)
    data, state = await _swr.async_swr_get(
        _MISC_SWR_CACHE, ('scan-pool', uid), lambda: _load_scan_pool_detail(uid),
        ttl=60, stale_max=600, label='scan-pool', gap=30, max_keys=16)
    if state == 'stale':
        data = {**data, 'stale': True}
    return data


@router.get("/api/custom-scan-pool/symbols", tags=["自定义扫描池"])
async def get_user_scan_pool_symbols_api(user: Dict = Depends(current_user)):
    """轻量端点：仅返回扫描池代码列表，不拉 K 线/行情
    性能优化（2026-07-02）：TabQuote/TabScan 只需 symbols 列表，
    原先调 /api/custom-scan-pool 会触发 N 次 K 线计算（2-4s 纯浪费）。
    """
    try:
        symbols = load_user_scan_pool(user_id=int(user.get('id') or 1))
        return {"success": True, "data": {"symbols": symbols, "count": len(symbols)}}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取扫描池代码列表失败: {error}")


@router.post("/api/custom-scan-pool", tags=["自定义扫描池"])
async def update_user_scan_pool_api(data: Dict, user: Dict = Depends(current_user)):
    try:
        symbols = data.get('symbols', [])
        if not isinstance(symbols, list):
            raise HTTPException(status_code=400, detail="symbols必须是数组")
        
        # 验证股票代码格式
        valid_symbols = []
        for s in symbols:
            if re.match(r'^(sh|sz)\d{6}$', s.lower()):
                valid_symbols.append(s.lower())
        
        save_user_scan_pool(valid_symbols, user_id=int(user.get('id') or 1))
        _invalidate_hot_scan_cache()
        return {"success": True, "data": {"symbols": valid_symbols, "count": len(valid_symbols)}}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@router.put("/api/custom-scan-pool/add", tags=["自定义扫描池"])
async def add_to_user_scan_pool_api(data: Dict, user: Dict = Depends(current_user)):
    try:
        # 同时支持 'symbol' 和 'symbols' 参数
        if 'symbol' in data:
            symbols_to_add = [data['symbol']]
        else:
            symbols_to_add = data.get('symbols', [])
            if not isinstance(symbols_to_add, list):
                symbols_to_add = [str(symbols_to_add)]
        
        current_pool = load_user_scan_pool(user_id=int(user.get('id') or 1))
        
        for s in symbols_to_add:
            s_lower = s.lower()
            if re.match(r'^(sh|sz)\d{6}$', s_lower) and s_lower not in current_pool:
                current_pool.append(s_lower)
        
        save_user_scan_pool(current_pool, user_id=int(user.get('id') or 1))
        _invalidate_hot_scan_cache()
        return {"success": True, "data": {"symbols": current_pool, "count": len(current_pool)}}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@router.put("/api/custom-scan-pool/remove", tags=["自定义扫描池"])
async def remove_from_user_scan_pool_api(data: Dict, user: Dict = Depends(current_user)):
    try:
        # 同时支持 'symbol' 和 'symbols' 参数
        if 'symbol' in data:
            symbols_to_remove = [data['symbol']]
        else:
            symbols_to_remove = data.get('symbols', [])
            if not isinstance(symbols_to_remove, list):
                symbols_to_remove = [str(symbols_to_remove)]
        
        symbols_to_remove = [s.lower() for s in symbols_to_remove]
        current_pool = load_user_scan_pool(user_id=int(user.get('id') or 1))
        current_pool = [s for s in current_pool if s not in symbols_to_remove]
        
        save_user_scan_pool(current_pool, user_id=int(user.get('id') or 1))
        # 2026-11-04：原先漏了失效（add 有、remove 没有），移除后扫描结果会沿用旧池；
        # 顺带也让新增的扫描池详情 SWR 缓存立即失效。
        _invalidate_hot_scan_cache()
        return {"success": True, "data": {"symbols": current_pool, "count": len(current_pool)}}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@router.delete("/api/custom-scan-pool", tags=["自定义扫描池"])
async def clear_user_scan_pool_api(user: Dict = Depends(current_user)):
    try:
        save_user_scan_pool([], user_id=int(user.get('id') or 1))
        _invalidate_hot_scan_cache()
        return {"success": True, "message": "自定义扫描池已清空"}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"清空自定义扫描池失败: {str(error)}")


@router.get("/api/hot-stocks-scan", tags=["股票扫描"])
async def hot_stock_buttons_scan(codes: str = '', force: bool = False, pool_only: bool = False):
    t0 = time.time()
    try:
        if not codes:
            return {"error": "请提供codes参数（逗号分隔的股票代码）"}
        
        codes_list = [c for c in codes.split(',') if re.match(r'^(sh|sz)\d{6}$', c.lower())]
        if not codes_list:
            return {"error": "无有效股票代码"}

        # 2026-08-31 选股策略优化：扩展扫描宇宙。
        # 默认合并 用户池 ∪ 趋势池 ∪ 默认热门池 ∪ 热搜前N（去重、过滤风险股，封顶 120）。
        # 传 pool_only=1 仅扫用户池。需在缓存检查前构建，保证缓存覆盖完整宇宙。
        codes_list = _build_scan_universe(codes_list, pool_only)

        # 选股范围（主板 + 非ST/退 + 总市值≥35亿），放在缓存 key 之前，缓存即过滤后范围。
        # 2026-10-16：以前这里只过市值，创业板/科创/ST 仍能进批量扫描结果 —— 与海选口径不一致。
        try:
            from services.data import universe
            _cb = len(codes_list)
            codes_list = universe.selectable_prefixed(codes_list)
            if _cb != len(codes_list):
                print(f'[批量扫描] 股票范围 {universe.describe_selectable()}：{_cb} → {len(codes_list)} 只')
        except Exception as e:
            print(f'[批量扫描] 股票范围过滤失败(不过滤): {e}')

        # 2026-10-30：阶段耗时（用户要求页面 2s 内，先把 8.7s 到底花在哪一段量出来）。
        # 只 print、不改任何返回结构；冷路径才有意义，热缓存会直接命中返回、看不到这几行。
        print(f'[批量扫描] 阶段 候选+范围 {time.time() - t0:.2f}s（{len(codes_list)} 只）')

        # 2026-08-31 性能优化：内存缓存覆盖前端 30s 轮询的重复请求（2026-10-24 TTL 15s→45s，
        # 15s 时单用户每轮必过期）。命中缓存直接返回，跳过网络批量拉取 + 资金流 + RPS + 评分等重计算。
        # 2026-10-30 加陈旧兜底：45s 过期但未超 10 分钟 → 立即返回旧数据并后台刷新，
        # 用户请求从此最多等"缓存命中"级别的耗时；只有完全无缓存（重启后首次）或 force 才同步算。
        # 2026-11-07（修④）：键换成"内容 sha1 + 只数"的稳定短键，不再直接拼整串代码。
        # 原来键长随池子大小线性增长，而这份缓存挂的是全站最大的单值（120 只全字段），
        # 又同时缺上限、缺淘汰、过期不回收 ⇒ 任何带 codes 参数的公开请求都能永久占一份内存。
        cache_key = hot_scan_key(codes_list)
        _now_ts = time.time()
        _cached = _HOT_SCAN_CACHE.get(cache_key)
        _compute_now = force or getattr(_HOT_SCAN_CTX, 'compute', False)
        if not _compute_now and _cached:
            _age = _now_ts - _cached['ts']
            _touch_hot_scan(cache_key)      # 命中即记"这份刚被读过"，淘汰时按最久未用扔
            if _age < _HOT_SCAN_CACHE_TTL:
                return _scan_as_of_json(_cached['data'], _cached['ts'])
            if _age < _HOT_SCAN_STALE_MAX:
                _spawn_hot_scan_refresh(cache_key, codes, pool_only)
                return _scan_as_of_json(_cached['data'], _cached['ts'])
        
        # 2026-07-08 修复：取消 100 只截断，支持全量扫描
        # 原 codes_list[:100] 导致 80% 股票未被扫描
        # 批量拉取逻辑（batch_size=100 分批并发）已支持大规模扫描

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
        _t_quote = time.time()
        print(f'[批量扫描] 阶段 行情批量 {_t_quote - t0:.2f}s')
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
                from services.pick.recommend import fetch_batch_fund_flow
                return await fetch_batch_fund_flow([s.get('symbol', '') for s in all_results])
            except Exception as e:
                print(f'[批量扫描] 资金流向获取失败: {e}')
                return {}

        # : 从 DB 批量查股东人数（月度数据，同步 DB 查询用 to_thread 包装避免阻塞事件循环）
        async def _fetch_holder_num() -> Dict:
            try:
                from services.data.shareholder import get_holder_num_batch
                symbols = [s.get('symbol', '') for s in all_results]
                return await asyncio.to_thread(get_holder_num_batch, symbols)
            except Exception as e:
                print(f'[批量扫描] 股东人数获取失败: {e}')
                return {}

        async def _fetch_rps() -> Dict:
            try:
                return await get_rps_map()
            except Exception as e:
                print(f'[批量扫描] RPS 获取失败: {e}')
                return {}

        # : 从 DB 批量查基本面质量（季度数据，同步 DB 查询用 to_thread 包装避免阻塞）
        async def _fetch_fundamental() -> Dict:
            try:
                from services.data.fundamental import get_fundamental_batch
                symbols = [s.get('symbol', '') for s in all_results]
                return await asyncio.to_thread(get_fundamental_batch, symbols)
            except Exception as e:
                print(f'[批量扫描] 基本面获取失败: {e}')
                return {}

        # : 龙虎榜聪明钱信号（同步聚合+缓存，用 to_thread 包装避免阻塞事件循环）
        async def _fetch_smart_money() -> Dict:
            try:
                return await asyncio.to_thread(get_dragon_tiger_symbol_map, SMART_MONEY_DRAGON_TIGER_DAYS)
            except Exception as e:
                print(f'[批量扫描] 聪明钱获取失败: {e}')
                return {}

        # : 估值分位（PE/PB 全市场百分位，DB 缓存，同步查询用 to_thread 包装）
        async def _fetch_valuation() -> Dict:
            try:
                symbols = [s.get('symbol', '') for s in all_results]
                return await asyncio.to_thread(get_valuation_batch, symbols)
            except Exception as e:
                print(f'[批量扫描] 估值分位获取失败: {e}')
                return {}

        _acc_t: Dict[str, float] = {}

        async def _timed(name: str, coro):
            _s = time.time()
            try:
                return await coro
            finally:
                _acc_t[name] = round(time.time() - _s, 2)

        market_change, fund_flow_map, holder_map, rps_map, fund_map, sm_map, val_map = await asyncio.gather(
            _timed('market', _fetch_market_change()),
            _timed('fund_flow', _fetch_fund_flow()),
            _timed('holder', _fetch_holder_num()),
            _timed('rps', _fetch_rps()),
            _timed('fundamental', _fetch_fundamental()),
            _timed('smart_money', _fetch_smart_money()),
            _timed('valuation', _fetch_valuation()),
        )
        print(f'[批量扫描] 阶段 附加数据 {time.time() - _t_quote:.2f}s 明细 {_acc_t}')

        # 评分（传入 market_change + fund_flow_data + holder_data + rps + 基本面 + 聪明钱）
        # 趋势分复用今日趋势扫描缓存（isUp 票）；全市场逐票现算 is_up_trend 成本过高，
        # 反弹股抑制已在单票评分/持仓/推荐/趋势扫描路径覆盖，此处作为明确边界。
        _t_score = time.time()
        # 2026-11-04 代码层审查 P1：原来是循环内每票调 calc_stock_score_v2 且不传
        # weights → 函数内部 load_strategy_factor_weights() 每票查一次 DB（进程内实测
        # 14.31ms/次 × 59 只 ≈ 844ms，就是日志里「评分+趋势缓存 0.78s」的大头）。
        # 典型 N+1：一次扫描内权重表不变，循环外查一次传进去，评分结果完全等价；
        # 比「按单票缓存评分」干净得多——不引入配置过期、脏分数、深拷贝等新风险。
        try:
            scan_weights = await asyncio.to_thread(load_strategy_factor_weights)
        except Exception as _we:
            print(f'[批量扫描] 权重表预取失败（退回逐票读取）: {_we}')
            scan_weights = None
        try:
            from services.strategy.trend import load_trend_scan_results
            _scan_trend = load_trend_scan_results().get('stocks', [])
            scan_trend_map = {s['symbol'].lower(): {'score': s.get('score'), 'details': s.get('details', {})}
                              for s in _scan_trend if s.get('symbol') is not None}
        except Exception as _e:
            print(f'[批量扫描] 趋势分缓存加载失败(按无趋势评分): {_e}')
            scan_trend_map = {}
        for stock in all_results:
            sym = stock.get('symbol', '').lower()
            ff = fund_flow_map.get(sym)
            hd = holder_map.get(sym)
            rps = rps_map.get(normalize_local_symbol(sym))
            fd = fund_map.get(sym)
            sm = sm_map.get(sym)
            val = val_map.get(sym)
            sm_data = {'on_board': True, 'net_amt': (sm or {}).get('net_amt', 0),
                       'appearances': (sm or {}).get('appearances', 0)} if sm else None
            stock['score'] = calc_stock_score_v2(stock, weights=scan_weights,
                                                  market_change=market_change,
                                                  fund_flow_data=ff, holder_data=hd,
                                                  rps_data={'rps': rps},
                                                  fundamental_data=fd, smart_money_data=sm_data,
                                                  trend_result=scan_trend_map.get(sym))
            # P2a/P2c: 风控止损 + 估值分位（纯函数，无额外 IO）
            _holder_ratio = (hd or {}).get('total_num_ratio')
            stock['valuation'] = val
            stock['riskControl'] = calc_risk_control(
                stock.get('price', 0), score_result=stock['score'],
                stock=stock, valuation=val, smart_money=sm_data, holder_ratio=_holder_ratio)

        # 行业中性化：按行业 P20/P80 分位数调整 strategy_score.total
        # industry_map 来自 fetch_batch_fund_flow 的 f100 字段；样本<5/行业跳过
        if STRATEGY_INDUSTRY_NEUTRALIZATION_ENABLED:
            industry_map = {sym: (ff or {}).get('industry', '') for sym, ff in fund_flow_map.items()}
            apply_industry_neutralization(all_results, industry_map)
            # 持久化行业映射（增量更新，失败不阻塞）
            # 2026-10-24：SQLite 写包 to_thread，避免写锁等待冻住事件循环（该接口 30s 轮询）
            try:
                await asyncio.to_thread(save_stock_industry, industry_map)
            except Exception as e:
                print(f'[批量扫描] 行业映射持久化失败（不影响评分）: {e}')

        all_results.sort(key=lambda x: x['score']['total'], reverse=True)
        print(f'[批量扫描] 阶段 评分+趋势缓存 {time.time() - _t_score:.2f}s')
        _t_db = time.time()

        # 2026-07-08 修复：移除 60 分硬性过滤，返回全部结果由前端排序展示
        # 原逻辑直接丢弃低于 60 分的股票，导致用户无法看到完整市场全貌
        # 前端 TabScan 已有按分数排序 + 分页，用户可自行筛选
        
        # 保存预测记录 - 只记录看涨的（2026-09-01 增加质量门槛）
        today_str = date.today().isoformat()
        predictions = []
        skipped = 0
        for s in all_results:
            score_data = s['score']
            if score_data['predict_direction'] != 'bull':
                continue
            # 质量门槛 v2（2 个月全周期回测，1973 条已验证样本）：
            # 涨停是最稳健核心信号（56.6% 胜率 / +1.82% 次日）；非涨停仅保留综合评分>=80 的高分票。
            # 上一版"量比>=0.9 且(非高位或涨停)"经复核全周期反而跑输基准，已废弃。
            if PREDICT_SAVE_QUALITY_GATE:
                _chg = float(s.get('changePercent', 0) or 0)
                _score = float(score_data.get('total', 0) or 0)
                _is_limit_up = _chg >= PREDICT_SAVE_LIMIT_UP_MIN
                if not _is_limit_up and _score < PREDICT_SAVE_MIN_SCORE:
                    skipped += 1
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
                # 回测因子补全：从 score_data 注入 position/main_capital_inflow/holder_structure
                'position': score_data.get('base_scores', {}).get('position', 0),
                'main_capital_inflow': score_data.get('factor_values', {}).get('main_capital_inflow', 0) or 0,
                'holder_structure': score_data.get('base_scores', {}).get('holder_structure', 0),
            })
        if predictions:
            # 2026-10-24：SQLite 写包 to_thread，避免写锁等待冻住事件循环
            await asyncio.to_thread(save_predictions, predictions)
        if PREDICT_SAVE_QUALITY_GATE and skipped:
            print(f'[扫描] 质量门槛v2过滤 {skipped} 只看涨信号（非涨停且评分<{PREDICT_SAVE_MIN_SCORE}），保留 {len(predictions)} 条')
        print(f'[批量扫描] 阶段 落库+行业 {time.time() - _t_db:.2f}s')
        
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
                # : 附加主力净流入占比(%)，供前端 score.js calculateScore 使用
                'mainInflowRatio': (fund_flow_map.get(s['symbol'].lower(), {}) or {}).get('main_inflow_ratio'),
                # : 行业中性化调整值（±2/0），供前端展示
                'industryAdjust': s['score'].get('industryAdjust', 0),
                'score': s['score']['total'],
                'scoreLabel': s['score']['label'],
                'predictDirection': s['score']['predict_direction'],
                'rps': rps_map.get(normalize_local_symbol(s['symbol'])),
                'fundamental': fund_map.get(s['symbol'].lower()),
                'smartMoney': ({'on_board': True,
                                'net_amt': (sm or {}).get('net_amt', 0),
                                'appearances': (sm or {}).get('appearances', 0)}
                               if (sm := sm_map.get(s['symbol'].lower())) else None),
                'valuation': s.get('valuation'),
                'riskControl': s.get('riskControl'),
                'buySellPoints': _recommend_svc.calc_buy_sell_points(s, score_result=s.get('score')),
            }
            for s in all_results
        ]
        
        print(f'[批量扫描] 完成: {len(response)} 只, 耗时 {(time.time() - t0):.1f}s')

        # 2026-08-31 性能优化：写入内存缓存（TTL 45s），下次轮询直接命中
        # 2026-11-07（修④）：改成走 `_remember_hot_scan`——先回收过服役上限的，仍超过
        # `_HOT_SCAN_MAX_KEYS=8` 就按最久没被读的淘汰；原来是裸赋值，只堆不扔。
        _ts_now = time.time()
        _remember_hot_scan(cache_key, response, _ts_now)
        return _scan_as_of_json(response, _ts_now)
    except Exception as error:
        print(f'批量扫描失败: {str(error)}')
        raise HTTPException(status_code=502, detail=f"扫描失败: {str(error)}")

