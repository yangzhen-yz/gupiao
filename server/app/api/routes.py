"""routes - 所有 API 路由定义"""
from fastapi import HTTPException, APIRouter, Request, Depends
from fastapi.responses import Response
from typing import List, Dict, Optional
import json, asyncio, time, re, inspect, threading
from datetime import date, datetime
from app.config import (STRATEGY_BACKTEST_PREDICTION_LIMIT, STRATEGY_BACKTEST_HISTORY_DAYS, DEFAULT_WEIGHTS, INDEX_SYMBOLS, TENCENT_QUOTE_API, SCAN_CONCURRENCY, HOT_STOCK_POOL, STRATEGY_INDUSTRY_NEUTRALIZATION_ENABLED, EASTMONEY_HEADERS, STRATEGY_SCORE_VALUES, FUND_FLOW_STOCK_TOP_N, FUND_FLOW_SECTOR_TOP_N, FUND_FLOW_HIDDEN_CHANGE_THRESHOLD, FUND_FLOW_HIDDEN_HOT_TOP_N, FUND_FLOW_HIDDEN_TOP_N, RPS_PERIOD_DAYS, SETTINGS_WRITE_USER_IDS, RPS_TOP_N, SMART_MONEY_DRAGON_TIGER_DAYS, PREDICT_SAVE_QUALITY_GATE, PREDICT_SAVE_LIMIT_UP_MIN, PREDICT_SAVE_MIN_SCORE)
from db.database import (get_db_conn, load_daily_recommendations, save_daily_recommendations, delete_daily_recommendations_by_date, load_trend_scan_results, load_stock_basic_info, save_stock_basic_info, load_stock_alias_map, load_hot_search_ranking, save_hot_search_ranking, load_user_scan_pool, save_user_scan_pool, load_hot_stock_buttons, save_hot_stock_buttons, load_stock_concept_tags, save_stock_concept_tags, load_daily_market_reviews, save_daily_market_reviews, load_strategy_factor_weights, save_strategy_factor_weights, add_stock_concept_tags, remove_stock_tag, FACTOR_DESCRIPTIONS, save_stock_industry, load_trend_backtest_reports, load_latest_trend_backtest_report, load_trend_backtest_records)
from services.data.stock import (fetch_stock_data, fetch_eastmoney_hot_list, get_stock_name, get_kline_data, parse_tencent_batch_data, fetch_stock_concepts, is_forbidden_stock, calculate_ma, fetch_with_retry, _sync_get, has_delisting_risk, detect_quote_source, quote_route_status)
from services.strategy.strategy import backtest_yesterday_predictions, save_predictions, calc_stock_score_v2, apply_industry_neutralization
from services.strategy.trend import scan_trend_scan_results, scan_and_remove_below_ma10, calc_pool_stock_indicators
from services.strategy.trend_backtest import backtest_trend_predictions
from services.data.trading_calendar import is_trading_day
# 2026-10-29：推荐模块一律按模块属性取用。10-29 那次 500
# （"_build_expectation_note() takes 3 positional arguments but 4 were given"）的根因是
# app/main.py 的 _reload_config() 曾 importlib.reload(services.pick.recommend)，
# 而本模块启动时 from-import 的旧函数对象不会更新，旧函数体却读到新的全局表。
# 该 reload 已移除；这里仍按属性取用，避免将来有人恢复 reload 时再踩同一个坑。
from services.pick import recommend as _recommend_svc
from services.ops.review import generate_daily_review
from services.strategy.market import predict_market_risk, check_market_crash, load_market_alert
from services.data.fundflow import get_fund_flow_summary
from services.strategy.rps import get_rps_map, get_rps_leaderboard
from services.data.kline_history import normalize_local_symbol  # 将带前缀 symbol 规整为本地库纯代码，匹配 RPS 缓存 key
from services.data.fundamental import get_fundamental_batch, update_fundamental_task
from services.data.smartmoney import get_dragon_tiger, get_northbound_market, get_dragon_tiger_symbol_map
from services.data.valuation import get_valuation_batch, update_valuation_task
from services.strategy.risk_control import calc_risk_control
from services.data.utils import ensure_today_date
from services.support.auth import (current_user, create_user, authenticate, logout as auth_logout,
                           get_user_by_token, list_users, has_any_user, change_password)
from services.ops.action_desk import build_action_desk

router = APIRouter()

TENCENT_API = TENCENT_QUOTE_API
MAX_RETRIES = 2

# ========== 批量扫描结果内存缓存（2026-08-31 性能优化） ==========
_HOT_SCAN_CACHE: Dict[str, Dict] = {}
# 2026-10-24：15s→45s。前端 30s 轮询下单用户 15s TTL 永不命中（每次都过期），
# 每轮触发全量重扫（120 只行情+资金流+评分）；45s > 30s 轮询间隔，交替命中，
# 全量重扫频率减半，缓存数据最旧 45s（30s 一轮的筛选页可接受）。
_HOT_SCAN_CACHE_TTL = 45  # 秒
# 2026-10-30 陈旧兜底：45s TTL 一过，下一个请求就要白付全量重扫（实测冷 2.2s、
# 缓存过期叠并发时 8.7s 甚至 53.9s）。过期但未超 10 分钟的数据先原样返回、由后台
# 线程刷新；筛选页是 30s 轮询的大盘画面，秒级新鲜度足够，页面耗时从此 ≤ 缓存命中。
_HOT_SCAN_STALE_MAX = 600  # 秒
_HOT_SCAN_REFRESHING: Dict[str, float] = {}   # {cache_key: 上次后台刷新时间}，20s 节流
_HOT_SCAN_CTX = threading.local()             # 后台刷新线程内置 compute=True，避免递归再进缓存分支

def _invalidate_hot_scan_cache():
    """清空批量扫描缓存（添加/移除自选股后调用，避免脏缓存）"""
    _HOT_SCAN_CACHE.clear()
    _HOT_SCAN_REFRESHING.clear()


def _spawn_hot_scan_refresh(cache_key: str, codes_raw: str, pool_only: bool):
    """后台刷新一份过期的扫描缓存（2026-10-30 陈旧兜底的另一半）。

    20s 节流防止 30s 轮询叠并发时重复全量扫；子线程自带事件循环
    （asyncio.run）是安全的——扫描的网络调用全部走 requests + to_thread，
    不依赖主循环资源。失败只打日志，旧缓存留给下一个请求继续兜底。
    """
    _now = time.time()
    if _now - _HOT_SCAN_REFRESHING.get(cache_key, 0) < 20:
        return
    _HOT_SCAN_REFRESHING[cache_key] = _now

    def _run():
        _HOT_SCAN_CTX.compute = True
        try:
            asyncio.run(hot_stock_buttons_scan(codes=codes_raw, force=False, pool_only=pool_only))
            print(f'[批量扫描] 后台刷新完成 {cache_key[:40]}…')
        except Exception as e:
            print(f'[批量扫描] 后台刷新失败（旧缓存保留）: {e}')
    threading.Thread(target=_run, daemon=True, name='hot-scan-refresh').start()

# ========== 扫描宇宙扩展（2026-08-31 选股策略优化） ==========
# 背景：原「股票筛选」只扫前端传入的用户自选池（通常 20~30 只），覆盖面窄。
#       而趋势扫描已覆盖 ~477 只，热搜榜有全市场最活跃的 ~600 只。
# 方案：默认合并 用户池 ∪ 趋势池 ∪ 默认热门池 ∪ 热搜前 N，去重后封顶 SCAN_UNIVERSE_CAP，
#       让筛选 Tab 能看到更完整的市场机会；传入 pool_only=1 可退化为仅扫用户池。
SCAN_UNIVERSE_CAP = 120          # 扫描宇宙封顶（兼顾覆盖面与首扫耗时）
SCAN_UNIVERSE_HOT_TOP_N = 60     # 热搜榜补充数量
SCAN_UNIVERSE_MIN_POOL = 15      # 用户池 ≥ 此数量时不再补充热搜榜（尊重用户自选规模）

def _build_scan_universe(codes_list: List[str], pool_only: bool = False) -> List[str]:
    """构建扫描宇宙：用户池 + 趋势池 + 默认热门池 + 热搜前 N（去重、过滤风险股）"""
    universe = [c.lower() for c in codes_list]
    if pool_only:
        return universe[:SCAN_UNIVERSE_CAP]

    # 合并默认热门池（HOT_STOCK_POOL 是经过人工挑选的备选池）
    for c in HOT_STOCK_POOL:
        if c.lower() not in universe:
            universe.append(c.lower())

    # 合并趋势扫描池（已通过趋势评分 ≥ TREND_MIN_SCORE 的强势股，跨系统联动）
    try:
        trend_data = load_trend_scan_results()
        for st in (trend_data.get('stocks') or []):
            sym = (st.get('symbol') or '').lower()
            if re.match(r'^(sh|sz)\d{6}$', sym) and sym not in universe:
                universe.append(sym)
    except Exception as _e:
        print(f'[扫描宇宙] 趋势池合并失败: {_e}')

    # 用户池规模较小时，补充热搜榜前排活跃股（扩大市场覆盖面）
    if len(codes_list) < SCAN_UNIVERSE_MIN_POOL:
        try:
            hot = load_hot_search_ranking()
            for st in (hot.get('stocks') or [])[:SCAN_UNIVERSE_HOT_TOP_N]:
                sym = (st.get('symbol') or '').lower()
                if not re.match(r'^(sh|sz)\d{6}$', sym):
                    continue
                if is_forbidden_stock(st.get('name') or '') or has_delisting_risk(st.get('name') or '', sym):
                    continue
                if sym not in universe:
                    universe.append(sym)
        except Exception as _e:
            print(f'[扫描宇宙] 热搜池合并失败: {_e}')

    return universe[:SCAN_UNIVERSE_CAP]

@router.get("/api/health", tags=["健康检查"])
async def health_check():
    return {"status": "ok", "time": datetime.now().isoformat()}


@router.get("/api/data-sources/health", tags=["健康检查"])
async def data_sources_health():
    """数据源健康台账（2026-10-19，只读，不发网络请求）。

    行情：腾讯主源 / 新浪备源的连续失败计数与当前路由（是否处于腾讯冷却期）。
    东财：clist / ulist / datacenter 三个 scope 的熔断与全域闸门状态。
    页面“数据为什么旧/为什么标了新浪兜底”可以直接在这里找到答案。
    """
    from services.data import source_health, eastmoney_guard
    return {
        "success": True,
        "data": {
            "quotes": {
                "tencent": source_health.status('quote_tencent'),
                "sina": source_health.status('quote_sina'),
                "route": quote_route_status(),
            },
            "eastmoney": {
                # 2026-10-20：加 'ann'——公告接口当天也接入了共享熔断（此前是闸门覆盖缺口）
                scope: eastmoney_guard.status(scope)
                for scope in ('clist', 'ulist', 'datacenter', 'ann')
            },
        },
    }

@router.get("/api/hot_search_ranking", tags=["热搜榜数据"])
async def get_hot_search_ranking_api():
    try:
        data = load_hot_search_ranking()
        return {"success": True, "data": data}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取hot_search_ranking失败: {str(error)}")


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

# ========== 第三方接口调用用量统计（2026-10-19） ==========
@router.get("/api/api-usage", tags=["运维"])
async def api_usage_api(user: Dict = Depends(current_user)):
    """第三方接口每日调用计数与上限预警（只读）。管理员可改上限。

    用途：改代码前先看"哪个接口快撞上限"，避免把东财/腾讯/新浪等平台的日调用量推过封禁阈值。
    """
    from services.data import api_usage
    from services.support import app_settings as A
    items = api_usage.get_stats()
    history = api_usage.get_history(14)
    return {
        "success": True,
        "data": {
            "items": items,
            "history": history,
            "canWrite": A.can_write(user),
            "writeIds": list(A.write_ids()),
            "alertThreshold": api_usage.ALERT_THRESHOLD * 100,
        },
    }


@router.post("/api/api-usage/limits", tags=["运维"])
async def api_usage_set_limit_api(body: Dict, user: Dict = Depends(current_user)):
    """修改某接口的每日上限（管理员）。key 形如 'eastmoney:clist'。"""
    from services.data import api_usage
    from services.support import app_settings as A
    if not A.can_write(user):
        raise HTTPException(status_code=403, detail='当前账号无权修改接口调用上限（默认仅最早注册的账号可改）')
    key = (body or {}).get('key')
    limit = (body or {}).get('limit')
    if not key or ':' not in key:
        raise HTTPException(status_code=400, detail='key 格式应为 provider:interface，例如 eastmoney:clist')
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail='limit 必须是正整数')
    if limit <= 0:
        raise HTTPException(status_code=400, detail='limit 必须是正整数')
    api_usage.set_limit(key, limit, updated_by=str(user.get('username') or user.get('id')))
    print(f"[接口用量] {user.get('username') or user.get('id')} 改了 {key} 上限 = {limit}")
    return {"success": True, "data": {"key": key, "limit": limit}}


# ========== 因子体检（2026-10-22，P0 评估基座） ==========
# 结果每周六 08:30 由调度任务落 eval_lab_runs；这里只读，不重算。
# 手动重算给管理员一个按钮，但必须防重复点击：一次要读 140 万行日K、约 1 分钟。
_EVAL_LAB_STATE: Dict = {'running': False, 'startedAt': ''}


def _eval_lab_module():
    """轻量读写层（只读 sqlite + 子进程跑体检；不把 numpy/pandas 拉进 web 进程）。"""
    from services.support import eval_lab
    return eval_lab


@router.get("/api/eval-lab/latest", tags=["复盘"])
async def eval_lab_latest_api(user: Dict = Depends(current_user)):
    try:
        mod = _eval_lab_module()
        data = await asyncio.to_thread(mod.latest_run)
        data['running'] = _EVAL_LAB_STATE['running']
        data['startedAt'] = _EVAL_LAB_STATE['startedAt']
        return {"success": True, "data": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'因子体检结果读取失败: {type(e).__name__}: {e}')


@router.post("/api/eval-lab/run", tags=["复盘"])
async def eval_lab_run_api(user: Dict = Depends(current_user)):
    from services.support import app_settings as A
    if not A.can_write(user):
        raise HTTPException(status_code=403, detail='当前账号无权触发全量因子体检')
    if _EVAL_LAB_STATE['running']:
        return {"success": True, "data": {'started': False, 'why': '上一轮还在跑',
                                         'startedAt': _EVAL_LAB_STATE['startedAt']}}
    import threading

    def _work():
        try:
            mod = _eval_lab_module()
            r = mod.run_analysis()
            print(f"[因子体检] 手动触发完成: {r.get('runDate')} / {r.get('interpreter')} / "
                  f"{r.get('universe')} 只 / 5日超额 {((r.get('recommendations') or {}).get('ret5') or {}).get('excessMean')}%")
        except Exception as e:
            print(f'[因子体检] 手动触发失败: {type(e).__name__}: {e}')
        finally:
            _EVAL_LAB_STATE['running'] = False

    _EVAL_LAB_STATE.update(running=True, startedAt=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    threading.Thread(target=_work, daemon=True, name='eval-lab-run').start()
    return {"success": True, "data": {'started': True, 'startedAt': _EVAL_LAB_STATE['startedAt']}}


@router.get("/api/auction/latest", tags=["复盘"])
async def auction_latest_api(date_str: str = ''):
    """09:25 竞价快照（只读）。date_str 为空取今天；用于核对定时任务是否真的采到。"""
    from services.data.auction import load_day
    day = date_str or datetime.now().strftime('%Y-%m-%d')
    try:
        rows = await asyncio.to_thread(load_day, day)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'竞价快照读取失败: {type(e).__name__}: {e}')
    return {"success": True, "data": {'date': day, 'n': len(rows), 'items': rows[:300]}}


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
            raw = await fetch_with_retry(sym)
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
                get_rps_map(),
                asyncio.to_thread(get_holder_num_batch, [sym]),
                asyncio.to_thread(get_fundamental_batch, [sym]),
                asyncio.to_thread(get_dragon_tiger_symbol_map, SMART_MONEY_DRAGON_TIGER_DAYS),
                asyncio.to_thread(get_valuation_batch, [sym]),
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
            tr = await asyncio.to_thread(compute_trend_result, sym, _px)
            result = calc_stock_score_v2(stock_data, holder_data=hd, rps_data={'rps': rps},
                                         fundamental_data=fd, smart_money_data=sm_data,
                                         trend_result=tr)
            # P2c/P2a: 估值分位 + 风控止损
            val = val_map.get(sym)
            _holder_ratio = (hd or {}).get('total_num_ratio')
            risk_control = calc_risk_control(stock_data.get('price', 0), score_result=result,
                                             stock=stock_data, valuation=val,
                                             smart_money=sm_data, holder_ratio=_holder_ratio)
            # 缓存评分结果（含 raw 字符串与衍生字段，缓存命中时无需重新拉取/计算）
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

def attach_rec_count_shadow(records) -> int:
    """给"当日推荐只数 ≠ 满编"的那些天挂一个**影子标注**字段，返回标注条数。

    来历（2026-10-27）：用户提出「回测曲线里所有模式，当推荐数量不等于 3 时说明当天行情不好，
    当天推荐一律不采纳（盘中低吸、次日交易都算），直接空仓」。按这个判据把 2026-06-01 起
    82 个交易日离线复算，**方向与预期相反**：不足 3 只的 17 天（26 条样本）次日开盘买持 3/5 日
    +1.22%/+0.71%、胜率 60%，满 3 只的 65 天反而 1/3/5 日全负（−0.60%/−2.24%/−4.04%）。
    所以这一层**只标注、不拦截**——把现场留在页面上，等样本攒够 ≥60 对再裁决要不要真上线。

    关掉 `rec_below_full_shadow_label` 时一个字段都不下发，前端也就不会多出任何 DOM：
    关掉的代价严格等于改造前。复算脚本 `server/analysis_out/rec_count_vs_winrate.py`
    （该目录被 .gitignore 排除，不在版本控制里）。
    """
    from services.support import app_settings as A
    if not bool(A.value('rec_below_full_shadow_label')):
        return 0
    full = int(A.value('recommend_full_pick_count') or 3)
    hit = 0
    for record in records or []:
        stocks = record.get('daily_recommendations') or record.get('stocks') or []
        n = len(stocks)
        if n == full:
            continue
        record['shadow'] = {
            'count': n, 'full': full,
            'label': f'本日 {n} 只（满编 {full}）· 影子观察',
            'note': (f'你定的规则：只数≠{full} 的当天不采纳、直接空仓。当前只做标注，不拦截任何动作。'
                     '82 个交易日离线复算方向相反：不足 3 只那 17 天次日开盘买持 3/5 日 +1.22%/+0.71%'
                     '（胜率 60%），满 3 只的 65 天 1/3/5 日全负（−0.60%/−2.24%/−4.04%）。'
                     '样本仅 26 条，±5pp 内是噪声，攒够 ≥60 对样本外再裁决。'),
        }
        hit += 1
    return hit


async def _build_daily_recs_payload() -> Dict:
    """实际构建"历史推荐记录 + 实时盈亏"响应体（原 get_daily_recommendations 函数体抽出）

    抽出原因：SWR 模式下首次 miss 要同步算、过期后要在线程里 asyncio.run() 重算，
    两处必须复用同一份构建逻辑，否则必然漂移。
    """
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
                    codes_str = ','.join(syms)
                    url = f"{TENCENT_QUOTE_API}{codes_str}"
                    # -fix: 改用 requests + asyncio.to_thread（去 httpx）
                    resp = await asyncio.to_thread(_sync_get, url)
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

    try:
        attach_rec_count_shadow(daily_recommendations)
    except Exception as _e:
        # 标注这层挂了不能连累推荐数据本身（历史上"影子层拖垮主链路"是踩过的坑）
        print(f'[历史推荐] 只数影子标注没接上（不影响推荐数据）: {_e}')

    return {"success": True, "data": daily_recommendations}


# ========== daily_recommendations 缓存（SWR 模式，2026-10-23） ==========
# 沿革：2026-10-17 加 30s TTL 同步缓存，解决了"每次打开都重拉行情"（0.9~1.3s），
# 但 miss 时用户仍要白等 500ms+（实测 14.6/183.8/518.2ms 三次采样）。
# "历史推荐现在赚多少"是分钟级信息量，没有理由让请求方等一次全量行情重算。
# 现改为 stale-while-revalidate：
#   · fresh 期内（300s）直接返缓存
#   · 过期但有旧档：立即返旧数据（<20ms），同时后台线程重算，下一个请求就是新档
#   · 无旧档（冷启动/被写接口清掉）：同步算，这一次不可避免
# generation：写接口清缓存时 +1，后台刷新完成时若 generation 已变（说明期间有新写入）
# 则丢弃刷新结果，避免把用户刚保存/删除的档又被后台旧数据覆盖回去。
_DAILY_RECS_CACHE: Dict = {'ts': 0.0, 'payload': None, 'generation': 0}
_DAILY_RECS_FRESH_TTL = 300.0          # 新鲜期秒数：历史推荐盈亏是分钟级信息量
_DAILY_RECS_REFRESH_LOCK = threading.Lock()
_DAILY_RECS_REFRESHING = False         # 防并发重复刷新（锁保护）


def _invalidate_daily_recs_cache():
    """写接口调用：清档 + 重置时间 + bump generation（作废进行中的后台刷新）"""
    with _DAILY_RECS_REFRESH_LOCK:
        _DAILY_RECS_CACHE['payload'] = None
        _DAILY_RECS_CACHE['ts'] = 0.0
        _DAILY_RECS_CACHE['generation'] += 1


def _kick_daily_recs_refresh():
    """后台线程重算 daily_recommendations（节流：已有刷新在跑就直接返回）"""
    global _DAILY_RECS_REFRESHING
    with _DAILY_RECS_REFRESH_LOCK:
        if _DAILY_RECS_REFRESHING:
            return
        _DAILY_RECS_REFRESHING = True
        gen = _DAILY_RECS_CACHE['generation']

    def _run():
        global _DAILY_RECS_REFRESHING
        try:
            payload = asyncio.run(_build_daily_recs_payload())
            with _DAILY_RECS_REFRESH_LOCK:
                if gen == _DAILY_RECS_CACHE['generation']:
                    _DAILY_RECS_CACHE.update(ts=time.time(), payload=payload)
                else:
                    # 刷新期间发生了写入：丢弃本次结果，让下一个请求同步重建
                    pass
        except Exception as _e:
            # 刷新失败不静默：旧档会继续服役（SWR 的容错面），但要有日志可查
            print(f'[历史推荐] 后台刷新失败（旧档继续服役）: {_e}')
        finally:
            with _DAILY_RECS_REFRESH_LOCK:
                _DAILY_RECS_REFRESHING = False

    threading.Thread(target=_run, daemon=True, name='daily-recs-refresh').start()


@router.get("/api/daily_recommendations", tags=["推荐记录"])
async def get_daily_recommendations():
    """历史推荐记录 + 实时盈亏（SWR：过期返旧档+后台刷新，2026-10-23）

    实测背景（本机直连）：30s 同步缓存时三次采样 14.6/183.8/518.2ms——
    命中 15ms，miss 要同步等全量历史票行情重算 500ms+。本接口无轮询
    （TabRecommend 只 onMounted 一次），用户体感 = 每次打开都可能转圈半秒。
    保存/删除推荐后由写接口调 _invalidate_daily_recs_cache 清档。
    """
    now = time.time()
    cached = _DAILY_RECS_CACHE.get('payload')
    if cached is not None:
        age = now - _DAILY_RECS_CACHE['ts']
        if age < _DAILY_RECS_FRESH_TTL:
            return cached                      # 新鲜期：直接返
        _kick_daily_recs_refresh()             # 过期：返旧档 + 后台刷新
        return cached
    # 冷启动或被写接口清档：同步算（唯一不可避免的等待路径）
    try:
        payload = await _build_daily_recs_payload()
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取推荐记录失败: {str(error)}")
    with _DAILY_RECS_REFRESH_LOCK:
        _DAILY_RECS_CACHE.update(ts=time.time(), payload=payload)
    return payload

@router.get("/api/recommend-review", tags=["推荐记录"])
async def get_recommend_review_api(days: int = 30):
    """智能推荐复盘汇总（2026-10-12）：全量+近N天 T+1/T+3/T+5 胜率与盈亏、最好最差案例"""
    try:
        from services.pick.recommend_review import get_review_summary
        return {"success": True, "data": get_review_summary(days)}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取推荐复盘失败: {str(error)}")

_REC_SIM_CACHE: Dict = {}          # {(months, capital, gap, lots, bench): {ts, data}}
_EXCESS_CACHE: Dict = {}           # 可执行超额单独一份：冷算实测 7.4s，别把 0.2 秒的曲线缓存挤掉
_REC_SIM_TTL = 1800                # 秒：底层一天才变一次（14:45 推荐 + 16:10 复盘），算一次 0.2s

@router.get("/api/recommend-sim", tags=["推荐记录"])
async def get_recommend_sim_api(months: int = 3, capital: float = 100000,
                                gap: float = 5.0, lots: bool = True, bench: bool = False):
    """历史推荐的钱曲线（2026-10-18，复盘 → 回测曲线子页用）。

    六种口径（三种入口 × 两种卖法）一次算完，实现只在 `services/pick/rec_sim.py` 一份 ——
    命令行 `scripts/rec_exit_sim.py` 调的是同一个函数，页面与日志不会出现两套数。

    同步的 SQLite 读取全部丢进 `to_thread`：这个接口一次要读 85 只票的完整日K，
    压在事件循环上就是"一进这页全站卡"（rps/趋势扫描那个教训）。

    缓存的只有"六种口径的模拟结果"（0.17s 一次，底层数据一天才变），
    `daily`（每日监控点）**每次都重读**：它是 16:20/16:25 定时任务往里追加的时间序列，
    跟着缓存走就会出"曲线命中缓存 → 监控点停在 30 分钟前"的假象
    （今天就是这样：点已经落库了，页面还是空的）。
    """
    import time as _t
    key = (months, capital, gap, lots, bench)
    from services.pick import rec_sim
    hit = _REC_SIM_CACHE.get(key)
    if hit and _t.time() - hit['ts'] < _REC_SIM_TTL:
        data = dict(hit['data'])          # 浅拷贝：别把 daily 写回缓存里那份
    else:
        try:
            data = await asyncio.to_thread(
                rec_sim.simulate, months=months, capital=capital,
                auction_max_gap=gap, whole_lots=lots, with_benchmark=bench)
            _REC_SIM_CACHE[key] = {'ts': _t.time(), 'data': data}
            if len(_REC_SIM_CACHE) > 12:
                _REC_SIM_CACHE.clear()
            data = dict(data)
        except Exception as error:
            raise HTTPException(status_code=500, detail=f"回测曲线计算失败: {str(error)}")
    try:
        data['daily'] = await asyncio.to_thread(rec_sim.daily_history, 40)
    except Exception as e:
        data['daily'] = []
        data['dailyError'] = str(e)[:120]
    return {'success': True, 'data': data, 'cached': bool(hit)}


@router.get("/api/recommend-batches", tags=["推荐记录"])
async def recommend_batches_api(date: str = ''):
    """当日各批次推荐回看（2026-10-21）：daily_recommendations 先删后插只留末批，
    上午推过什么（如 10-21 的 10:26 上工申贝）只有 strategy_snapshot 记得。
    按批次返回推荐档名单＋批次现场（周期/情绪分），供智能推荐页折叠区展示。"""
    from datetime import datetime as _dt
    d = date or _dt.now().strftime('%Y-%m-%d')
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', d):
        raise HTTPException(status_code=400, detail="日期格式应为 YYYY-MM-DD")

    def _read():
        from db.database import get_db_conn
        conn = get_db_conn()
        rows = conn.execute(
            "SELECT batch, symbol, name, rank, score, timing, mode, price, change_percent "
            "FROM strategy_snapshot WHERE snap_date=? AND stage='recommended' ORDER BY batch, rank",
            (d,)).fetchall()
        meta = conn.execute(
            "SELECT batch, cycle, cycle_score, recommended, market_change "
            "FROM strategy_snapshot_batch WHERE snap_date=? ORDER BY batch", (d,)).fetchall()
        m = {x['batch']: dict(x) for x in meta}
        groups = {}
        for r in rows:
            g = groups.setdefault(r['batch'], [])
            g.append({k: r[k] for k in ('symbol', 'name', 'rank', 'score',
                                        'timing', 'mode', 'price', 'change_percent')})
        return [{'batch': b, 'meta': m.get(b) or {}, 'stocks': groups.get(b, [])}
                for b in sorted(set(groups) | set(m))]
    try:
        batches = await asyncio.to_thread(_read)
        return {'success': True, 'data': {'date': d, 'batches': batches}}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"读取当日批次失败: {str(error)}")


@router.get("/api/recommend-sim-batch", tags=["推荐记录"])
async def get_recommend_sim_batch_api(months: int = 3, capital: float = 100000, gap: float = 5.0):
    """上午档 vs 下午档拆批次回测（2026-10-21）：名单从 strategy_snapshot 还原
    （10-19 起才有），撮合/费用/封板判据与末批曲线同一引擎（rec_sim._curve_stats）。
    只读本地两个 SQLite、一次约 0.1s，丢线程池 + 30 分钟缓存，同 recommend-sim 的规矩。"""
    import time as _t
    key = ('batch', months, capital, gap)
    hit = _REC_SIM_CACHE.get(key)
    if hit and _t.time() - hit['ts'] < _REC_SIM_TTL:
        return {'success': True, 'data': hit['data'], 'cached': True}
    from services.pick import rec_sim_batch
    try:
        data = await asyncio.to_thread(
            rec_sim_batch.simulate_batch, months=months, capital=capital, auction_max_gap=gap)
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"拆档回测计算失败: {str(error)}")
    if len(_REC_SIM_CACHE) > 12:
        _REC_SIM_CACHE.clear()
    _REC_SIM_CACHE[key] = {'ts': _t.time(), 'data': data}
    return {'success': True, 'data': data, 'cached': False}


@router.get("/api/recommend-excess", tags=["推荐记录"])
async def get_recommend_excess_api(months: int = 6):
    """次日开盘买入、持有 1/3/5/10 日的**可执行超额**（2026-10-22，P0 评估基座）。

    为什么不塞进 /api/recommend-sim：主曲线 0.2 秒，这里要多算一遍选股范围等权基准
    （6 个月矩阵法**冷算实测 7.4 秒**，2026-10-27 单进程复测；早期写的 3.8 秒是推荐条数还少
    时量的，已不作数），同一个响应会让曲线白屏等它。页面并行请求，
    曲线先出来、这张表后补；30 分钟缓存，底层一天才变一次。

    口径固定为"推荐日下一交易日开盘买、持 N 个交易日开盘卖"，次日一字涨停买不到就跳过；
    基准是范围内**全部票**同一买入日、同一持有期等权，逐条配对后再平均。
    默认 6 个月而不是 3 个月：3 个月里 10 日样本不足 60 条，月度切片全是噪声。
    """
    import time as _t
    key = (months,)
    hit = _EXCESS_CACHE.get(key)
    if hit and _t.time() - hit['ts'] < _REC_SIM_TTL:
        return {'success': True, 'data': hit['data'], 'cached': True}
    from services.pick import rec_sim
    try:
        data = await asyncio.to_thread(rec_sim.open_hold_excess, months=months)
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"可执行超额计算失败: {str(error)}")
    if len(_EXCESS_CACHE) > 6:
        _EXCESS_CACHE.clear()
    _EXCESS_CACHE[key] = {'ts': _t.time(), 'data': data}
    return {'success': True, 'data': data, 'cached': False}


def prewarm_recommend_excess(months: int = 6) -> float:
    """启动预热用：提前把这份 7.4 秒的冷算结果塞进 `_EXCESS_CACHE`，返回耗时秒数。

    为什么需要它（2026-10-27 实拍）：服务重启会清掉进程内缓存，重启后第一个打开
    「回测曲线」页的人就要现等冷算；前端默认 10s 超时，实测那次直接报
    「超额没算出来：请求超时（10s）」。预热放后台线程跑，失败不致命（首次请求仍会现算）。
    """
    import time as _t
    from services.pick import rec_sim
    t0 = _t.time()
    data = rec_sim.open_hold_excess(months=months)
    _EXCESS_CACHE[(months,)] = {'ts': _t.time(), 'data': data}
    return _t.time() - t0


@router.get("/api/shadow-eval", tags=["推荐记录"])
async def get_shadow_eval_api(limit: int = 30):
    """影子对照的每日监控点（2026-10-18，实施计划"工具：影子对照脚本"）。

    这里**只读 16:25 落库的那些点**，不现算：现算一次要读整段日K、还会因为窗口随日期
    漂移而让"今天的数字"和"上周的数字"不可比。计算逻辑只在 `scripts/shadow_eval.py` 一份。
    没跑过（表还没建）时返回 available=False 并说清第一个点什么时候来，不给空数组让人猜。
    """
    def _read():
        from db.database import get_db_conn
        conn = get_db_conn()
        try:
            cur = conn.execute("SELECT eval_date, window_days, real_days, events, "
                               "kept_n, kept_win, kept_avg, excl_n, excl_win, excl_avg, "
                               "gap_avg, gap_win, verdict FROM recommend_shadow_eval "
                               "ORDER BY eval_date DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in cur]
        except Exception:
            return None                       # 表还没建 = 这个监控今天才上线
        finally:
            conn.close()
    rows = await asyncio.to_thread(_read)
    from app.config import SHADOW_EVAL_MIN_DAYS
    if rows is None:
        return {'success': True,
                'data': {'available': False, 'rows': [], 'minDays': SHADOW_EVAL_MIN_DAYS,
                         'note': '影子对照由定时任务在交易日 16:25 写入 recommend_shadow_eval；'
                                 f'判定要攒满 {SHADOW_EVAL_MIN_DAYS} 个有真放行名单的交易日'}}
    return {'success': True,
            'data': {'available': bool(rows), 'rows': rows, 'minDays': SHADOW_EVAL_MIN_DAYS}}

async def _search_stocks_remote(q: str) -> List[Dict]:
    """腾讯 smartbox 远程股票搜索（本地库未命中时的回退，2026-10-12）

    接口：https://smartbox.gtimg.cn/s3/?v=2&q={关键词}&t=all
    返回：v_hint="sz~000949~新乡化纤~xxhx~GP-A;sh~600000~..."（市场~代码~名称~拼音~类型）
    只保留 A 股（GP-A）；中文为 \\uXXXX 转义需解码。
    """
    try:
        from urllib.parse import quote
        import codecs
        # smartbox 不认带交易所前缀的代码（sh600000→无结果），代码查询剥掉前缀再传
        prefix = q.lower()[:2] if q.lower()[:2] in ('sh', 'sz', 'bj') else ''
        remote_q = re.sub(r'^(sh|sz|bj)', '', q.lower()) if re.fullmatch(r'(sh|sz|bj)?\d{1,6}', q.lower()) else q
        url = f'https://smartbox.gtimg.cn/s3/?v=2&q={quote(remote_q)}&t=all'
        resp = await asyncio.to_thread(_sync_get, url, None, (5, 8))
        text = resp.text.strip()
        m = re.search(r'v_hint="([^"]*)"', text)
        if not m or not m.group(1):
            return []
        raw = codecs.decode(m.group(1), 'unicode_escape')
        out = []
        seen = set()
        for item in raw.split(';'):
            parts = item.split('~')
            if len(parts) < 3:
                continue
            mkt, code, name = parts[0].strip().lower(), parts[1].strip(), parts[2].strip()
            typ = parts[4].strip() if len(parts) > 4 else ''
            if not re.fullmatch(r'\d{6}', code) or not name:
                continue
            if typ and typ != 'GP-A':
                continue  # 只要 A 股
            if mkt not in ('sh', 'sz', 'bj'):
                continue
            sym = mkt + code
            if sym in seen:
                continue
            seen.add(sym)
            out.append({'symbol': sym, 'name': name})
        # 查询显式带了交易所前缀时，该市场的结果排前（sz000001 优先平安银行）
        if prefix:
            out.sort(key=lambda x: 0 if x['symbol'].startswith(prefix) else 1)
        return out[:10]
    except Exception as e:
        print(f'[股票搜索] 远程回退失败: {e}')
        return []

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

# ========== 多用户账号认证（2026-10-12） ==========
# 只对私有数据（持仓）强制登录；行情/推荐/收评等公共接口保持免登录，
# 以免影响收评定时任务、推荐快照与截图脚本等既有自动化。
@router.post("/api/auth/register", tags=["用户认证"])
async def api_register(payload: Dict):
    """注册账号（首个注册者 id=1，天然认领迁移过来的存量持仓）"""
    try:
        from app.config import AUTH_ALLOW_REGISTER
        if not AUTH_ALLOW_REGISTER:
            raise HTTPException(status_code=403, detail="系统未开放注册，请联系管理员开号")
        ok, message, user = create_user(
            payload.get('username', ''), payload.get('password', ''),
            payload.get('displayName', '') or payload.get('display_name', ''))
        if not ok:
            raise HTTPException(status_code=400, detail=message)
        # 注册成功直接签发会话，省去再登录一次
        user2, token, err = authenticate(payload.get('username', ''), payload.get('password', ''))
        if err:
            return {"success": True, "data": {"user": user, "token": None},
                    "message": f"{message}，请手动登录"}
        return {"success": True, "data": {"user": user2, "token": token}, "message": message}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"注册失败: {str(error)}")


@router.post("/api/auth/login", tags=["用户认证"])
async def api_login(payload: Dict):
    """账号密码登录，返回 token（默认有效期 30 天）"""
    try:
        user, token, err = authenticate(payload.get('username', ''), payload.get('password', ''))
        if err:
            raise HTTPException(status_code=401, detail=err)
        return {"success": True, "data": {"user": user, "token": token}, "message": "登录成功"}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"登录失败: {str(error)}")


@router.post("/api/auth/logout", tags=["用户认证"])
async def api_logout(request: Request):
    """注销当前会话"""
    try:
        token = request.headers.get('X-Auth-Token', '')
        auth_header = request.headers.get('Authorization', '')
        if not token and auth_header.lower().startswith('bearer '):
            token = auth_header[7:]
        auth_logout(token)
        return {"success": True, "message": "已退出登录"}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"退出失败: {str(error)}")


@router.get("/api/auth/me", tags=["用户认证"])
async def api_me(request: Request):
    """当前登录用户；未登录返回 success=false（前端据此显示登录页，不抛 401）"""
    try:
        token = request.headers.get('X-Auth-Token', '')
        auth_header = request.headers.get('Authorization', '')
        if not token and auth_header.lower().startswith('bearer '):
            token = auth_header[7:]
        user = get_user_by_token(token)
        if not user:
            return {"success": False, "data": None, "message": "未登录"}
        return {"success": True, "data": {"user": user}, "message": "ok"}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"校验失败: {str(error)}")


@router.get("/api/auth/users", tags=["用户认证"])
async def api_users(user: Dict = Depends(current_user)):
    """用户列表（供已登录账号之间切换用）。

    2026-10-13 加鉴权：原来免登录就能拿到全部用户名，等于把账号列表挂在登录页上。
    系统经 cloudflare 隧道短暂对外期间，这一步和 AUTH_ALLOW_REGISTER=False 一起做，
    陌生人打开链接只能看到一个空登录框。前端登录页的快捷切换已改读本机记录。
    """
    try:
        return {"success": True, "data": list_users()}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取用户列表失败: {str(error)}")


@router.get("/api/auth/status", tags=["用户认证"])
async def api_auth_status():
    """认证功能是否开启 / 是否已有账号 / 是否开放注册（登录页初始化用）"""
    from app.config import AUTH_ENABLED, AUTH_ALLOW_REGISTER
    return {"success": True, "data": {
        "enabled": AUTH_ENABLED,
        "hasUser": has_any_user(),
        "allowRegister": AUTH_ALLOW_REGISTER,
    }}


@router.post("/api/auth/change-password", tags=["用户认证"])
async def api_change_password(payload: Dict, user: Dict = Depends(current_user)):
    """修改当前用户密码（原密码校验，成功后吊销全部会话需重新登录）"""
    try:
        ok, message = change_password(user['id'], payload.get('oldPassword', ''),
                                      payload.get('newPassword', ''))
        if not ok:
            raise HTTPException(status_code=400, detail=message)
        return {"success": True, "message": message}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"修改密码失败: {str(error)}")


@router.get("/api/positions", tags=["持仓管理"])
async def get_positions_api(user: Dict = Depends(current_user)):
    """持仓诊断：实时盈亏 + 单票动作（止损/止盈/减仓/持有）+ 组合仓位建议（按登录用户隔离）"""
    try:
        from services.portfolio.position import get_portfolio_diagnosis
        return {"success": True, "data": await get_portfolio_diagnosis(user_id=user['id'])}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取持仓诊断失败: {str(error)}")

@router.post("/api/positions", tags=["持仓管理"])
async def save_position_api(pos: Dict, user: Dict = Depends(current_user)):
    """新增/更新持仓（symbol 唯一，重复提交同一 symbol 为更新）

    2026-10-12 加固：代码格式校验 + 代码/名称互相补全。
    - 接受 sh600000 / sz000001 / 纯6位（自动补 sh/sz 前缀）
    - 代码非法但有名称 → 按名称反查代码（用户记不住编码）
    - 缺名称 → 按代码自动补全名称
    """
    try:
        symbol = (pos.get('symbol') or '').strip().lower()
        name = (pos.get('name') or '').strip()
        cost = float(pos.get('cost_price', 0) or 0)
        shares = int(pos.get('shares', 0) or 0)
        if cost <= 0 or shares <= 0:
            raise HTTPException(status_code=400, detail="成本价与股数必须大于0")

        # 纯6位代码自动补交易所前缀（6→沪，0/3→深，4/8→北）
        if re.fullmatch(r'\d{6}', symbol):
            if symbol[0] == '6':
                symbol = 'sh' + symbol
            elif symbol[0] in '03':
                symbol = 'sz' + symbol
            else:
                symbol = 'bj' + symbol

        if not re.fullmatch(r'(sh|sz|bj)\d{6}', symbol):
            # 代码非法：尝试反查代码（优先用名称字段；没传名称时把输入本身当名称兜底——
            # 覆盖"用户输了名称但没从下拉框选"的场景）
            resolved = ''
            lookup_name = name or symbol
            if lookup_name:
                basic = load_stock_basic_info()
                alias = load_stock_alias_map()
                for sym, nm in basic.items():
                    if nm == lookup_name:
                        resolved = sym
                        break
                if not resolved and lookup_name in alias:
                    resolved = alias[lookup_name].lower()
            if resolved:
                symbol = resolved
            else:
                detail = (f'股票代码"{pos.get("symbol")}"格式不正确（应为 sh600000 / sz000001，或直接输6位代码）'
                          if not lookup_name else
                          f'代码/名称"{lookup_name}"未匹配到股票，请从下拉列表选择或检查输入')
                raise HTTPException(status_code=400, detail=detail)

        # 缺名称时按代码补全；basic_info 有权威名时覆盖传入名（防别名/简称/手输错误名入库）
        authoritative = load_stock_basic_info().get(symbol, '')
        if authoritative:
            name = authoritative
        elif not name:
            try:
                name = await get_stock_name(symbol) or symbol
            except Exception:
                name = symbol

        pos['symbol'] = symbol
        pos['name'] = name
        from db.database import save_user_position
        if not save_user_position(pos, user_id=user['id']):
            raise HTTPException(status_code=500, detail="保存失败")
        return {"success": True, "message": "持仓已保存", "data": {"symbol": symbol, "name": name}}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"保存持仓失败: {str(error)}")

@router.post("/api/positions/capital", tags=["持仓管理"])
async def save_capital_api(body: Dict, user: Dict = Depends(current_user)):
    """设置本账号初始本金（仓位口径的分母）

    没有本金，"仓位"就只能拿持仓市值自己除自己 —— 只持一只票就永远是 100%，
    超配告警和建议总仓位也全都失去意义。填一次本金后，单票仓位与总仓位才按真实资金占比算。
    principal 传 0 表示清除设置，回到旧的"按总成本"口径。
    """
    try:
        raw = body.get('principal', None)
        if raw is None or str(raw).strip() == '':
            raise HTTPException(status_code=400, detail="请提供本金金额 principal")
        try:
            principal = round(float(raw), 2)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="本金必须是数字")
        if principal < 0:
            raise HTTPException(status_code=400, detail="本金不能为负")
        if principal > 1e12:
            raise HTTPException(status_code=400, detail="本金数额过大，请确认单位是否为元")

        from db.database import save_user_capital
        if not save_user_capital(principal, user_id=user['id']):
            raise HTTPException(status_code=500, detail="保存失败")
        return {"success": True,
                "message": ("已清除本金设置" if principal <= 0 else f"初始本金已设为 {principal:,.2f} 元"),
                "data": {"principal": principal}}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"保存本金失败: {str(error)}")


# ========== 企业微信推送通道（按账号自助绑定，2026-10-14） ==========
# 别人注册后要接推送，只需要在自己手机上建个群、加个自定义机器人、把地址贴进来点绑定；
# 不需要改代码、不需要重启服务。绑定动作会真的发一条确认消息，发不出去就不算绑定成功。

@router.get("/api/push-channels", tags=["推送设置"])
async def list_push_channels_api(user: Dict = Depends(current_user)):
    from services.support.push_channel import list_channels, sent_today
    from app.config import PUSH_DAILY_LIMIT_PER_USER
    import services.support.push_channel as pc
    return {"success": True, "data": {
        "channels": list_channels(user['id']),
        "sent_today": sent_today(user['id']),
        "daily_limit": PUSH_DAILY_LIMIT_PER_USER,
        # 一条都没绑时消息会落到运维者的全局群；把这件事摆在明面上，不藏着
        "using_global_fallback": bool(pc._global_fallback_url()) and not list_channels(user['id']),
    }}


@router.post("/api/push-channels", tags=["推送设置"])
async def bind_push_channel_api(body: Dict, user: Dict = Depends(current_user)):
    from services.support.push_channel import bind_webhook
    url = str(body.get('url') or '').strip()
    label = str(body.get('label') or '').strip()
    if not url:
        raise HTTPException(status_code=400, detail="请粘贴群机器人的 Webhook 地址")
    ok, msg, cid = bind_webhook(user['id'], url, label)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "message": msg, "data": {"id": cid}}


@router.get("/api/push-channels/discover", tags=["推送设置"])
async def discover_push_sessions_api(user: Dict = Depends(current_user)):
    """列出机器人最近的会话：给机器人发一句话，再刷新这里，认领自己那条即可绑定单聊。

    不需要公网域名、不需要管理员建应用 —— 智能机器人的授权走腾讯托管页面，
    本地 CLI 轮询取结果，所以回调和域名都没关系。
    2026-10-22：列表含全部会话（姓名+chat_id），只有管理员（yz）可刷新，
    普通账号认领走"手填 chat_id"或让管理员代认领。
    """
    if not user.get('isAdmin'):
        raise HTTPException(status_code=403, detail='机器人会话列表仅管理员可查看/刷新')
    from services.support.push_channel import discover_sessions
    ok, err, items = discover_sessions()
    return {"success": ok, "data": {"sessions": items},
            **({} if ok else {"error": err, "detail": err})}


@router.post("/api/push-channels/search-user", tags=["推送设置"])
async def search_push_user_api(body: Dict, user: Dict = Depends(current_user)):
    """按姓名查企微成员 userid（需机器人「通讯录」权限；没授权时返回带授权链接的错误）
    与 discover 同理：查的是企业通讯录，仅管理员可用。"""
    if not user.get('isAdmin'):
        raise HTTPException(status_code=403, detail='通讯录查询仅管理员可用')
    from services.support.push_channel import search_users
    ok, err, items = search_users(str(body.get('keyword') or ''))
    return {"success": ok, "data": {"users": items},
            **({} if ok else {"error": err, "detail": err})}


@router.post("/api/push-channels/aibot", tags=["推送设置"])
async def bind_push_aibot_api(body: Dict, user: Dict = Depends(current_user)):
    """绑定智能机器人单聊（chat_id 来自 discover 列表或手填），实发确认消息成功才落库"""
    from services.support.push_channel import bind_aibot
    chat_id = str(body.get('chat_id') or '').strip()
    if not chat_id:
        raise HTTPException(status_code=400, detail="请先给机器人发一句话，再在列表里认领会话")
    ok, msg, cid = bind_aibot(user['id'], chat_id, str(body.get('label') or '').strip())
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "message": msg, "data": {"id": cid}}


@router.get("/api/push-channels/agent/status", tags=["推送设置"])
async def push_agent_status_api(user: Dict = Depends(current_user)):
    """自建应用（单聊）通道的就绪自检：缺哪个参数、token 取不取得到、还差管理员哪一步"""
    from services.support.push_channel import agent_status
    return {"success": True, "data": agent_status()}


@router.post("/api/push-channels/agent", tags=["推送设置"])
async def bind_push_agent_api(body: Dict, user: Dict = Depends(current_user)):
    """绑定企业微信「自建应用」单聊：填自己的企微账号ID，服务端实发一条确认消息才算成功"""
    from services.support.push_channel import bind_userid
    userid = str(body.get('userid') or '').strip()
    if not userid:
        raise HTTPException(status_code=400, detail="请填写企业微信通讯录里的「账号」(userid)")
    ok, msg, cid = bind_userid(user['id'], userid, str(body.get('label') or '').strip())
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "message": msg, "data": {"id": cid}}


@router.post("/api/push-channels/{ch_id}/test", tags=["推送设置"])
async def test_push_channel_api(ch_id: int, user: Dict = Depends(current_user)):
    from services.support.push_channel import test_channel
    ok, msg = test_channel(user['id'], ch_id)
    if not ok:
        raise HTTPException(status_code=400, detail=msg or '发送失败')
    return {"success": True, "message": "测试消息已发出，请到企业微信查看"}


@router.delete("/api/push-channels/{ch_id}", tags=["推送设置"])
async def unbind_push_channel_api(ch_id: int, user: Dict = Depends(current_user)):
    from services.support.push_channel import unbind
    if not unbind(user['id'], ch_id):
        raise HTTPException(status_code=404, detail="通道不存在或不属于本账号")
    return {"success": True, "message": "已解绑"}


@router.post("/api/positions/adjust", tags=["持仓管理"])
async def adjust_position_api(body: Dict, user: Dict = Depends(current_user)):
    """加仓/减仓：加权成本由服务端算（口径见 services/position_adjust）"""
    from datetime import datetime
    from db.database import load_user_positions, save_user_position, delete_user_position
    from services.portfolio.position_adjust import apply_adjust, describe

    symbol = (body.get('symbol') or '').strip().lower()
    if not re.fullmatch(r'(sh|sz|bj)\d{6}', symbol):
        raise HTTPException(status_code=400, detail="股票代码格式应为 sh600000 / sz000001")
    try:
        delta = int(float(body.get('shares', 0) or 0))
        price = round(float(body.get('price', 0) or 0), 3)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="股数与成交价必须是数字")

    cur = next((p for p in load_user_positions(user_id=user['id'])
                if (p.get('symbol') or '').lower() == symbol), None)
    if not cur:
        raise HTTPException(status_code=404, detail="没有找到这条持仓，请先添加")

    try:
        res = apply_adjust(cur, (body.get('mode') or '').strip(), delta, price,
                           datetime.now().strftime('%Y-%m-%d'))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if res['cleared']:
        if not delete_user_position(symbol, user_id=user['id']):
            raise HTTPException(status_code=500, detail="清仓失败")
    else:
        pos = {'symbol': symbol, 'name': cur.get('name') or symbol,
               'cost_price': res['cost_price'], 'shares': res['shares'],
               'buy_date': cur.get('buy_date') or '', 'note': res['note']}
        if not save_user_position(pos, user_id=user['id']):
            raise HTTPException(status_code=500, detail="保存失败")

    # 已实现盈亏落账（2026-10-23）：减仓/清仓赚的 money 不能只活在 note 文本里——
    # 清仓会整行删除持仓，不落数的话「当前资金/总盈亏」永远停在初始本金。
    realized_total = None
    if res['realized_pnl'] is not None:
        from db.database import add_user_realized_pnl
        realized_total = add_user_realized_pnl(res['realized_pnl'], user_id=user['id'])

    msg = describe(res, cur.get('name') or symbol)
    return {"success": True, "message": msg,
            "data": {"symbol": symbol, 'cleared': res['cleared'],
                     'cost_price': res['cost_price'], 'shares': res['shares'],
                     'realized_pnl': res['realized_pnl'],
                     'realized_total': realized_total}}


@router.delete("/api/positions/{symbol}", tags=["持仓管理"])
async def delete_position_api(symbol: str, user: Dict = Depends(current_user)):
    """删除持仓（只能删自己的）"""
    try:
        from db.database import delete_user_position
        if not delete_user_position(symbol, user_id=user['id']):
            raise HTTPException(status_code=404, detail="未找到该持仓")
        return {"success": True, "message": "持仓已删除"}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"删除持仓失败: {str(error)}")

# ========== 模拟交易（纯模拟盘·不下真单） ==========
@router.get("/api/sim/state", tags=["模拟交易"])
async def sim_state_api(user: Dict = Depends(current_user)):
    """获取模拟账户 + 持仓 + 待执行建议 + 审计日志"""
    try:
        from services.portfolio.trade_sim import get_sim_state
        return await get_sim_state(user['id'])
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取模拟账户失败: {str(error)}")

@router.post("/api/sim/generate", tags=["模拟交易"])
async def sim_generate_api(payload: Dict, user: Dict = Depends(current_user)):
    """从信号源生成建议订单。body: {sources:[...], symbols:[...]}"""
    try:
        from services.portfolio.trade_sim import generate_proposals
        sources = payload.get('sources') or ['tomorrow_plan', 'diagnosis', 'screener']
        symbols = payload.get('symbols') or None
        return await generate_proposals(user['id'], sources, symbols)
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"生成建议失败: {str(error)}")

@router.post("/api/sim/execute", tags=["模拟交易"])
async def sim_execute_api(user: Dict = Depends(current_user)):
    """执行模拟成交（按实时价撮合 pending 建议，仅在模拟账户记账）"""
    try:
        from services.portfolio.trade_sim import execute_sim
        return await execute_sim(user['id'])
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"模拟成交失败: {str(error)}")

@router.post("/api/sim/sell", tags=["模拟交易"])
async def sim_sell_api(payload: Dict, user: Dict = Depends(current_user)):
    """模拟盘手动卖出（按实时价立即成交）。body: {symbol, qty?}；不传 qty = 全部"""
    try:
        from services.portfolio.trade_sim import manual_sell
        r = await manual_sell(user['id'], payload.get('symbol') or '', payload.get('qty'))
        if not r.get('success'):
            raise HTTPException(status_code=400, detail=r.get('message') or '卖出失败')
        return r
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"卖出失败: {str(error)}")

@router.post("/api/sim/reset", tags=["模拟交易"])
async def sim_reset_api(user: Dict = Depends(current_user)):
    """重置模拟账户（清空持仓/订单/现金回到初始）"""
    try:
        from services.portfolio.trade_sim import reset_sim
        return reset_sim(user['id'])
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"重置失败: {str(error)}")

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
            _invalidate_daily_recs_cache()      # 写后清档，避免旧档多显示一轮
            return {"success": True, "message": "推荐记录保存成功"}
        else:
            raise HTTPException(status_code=500, detail="保存推荐记录失败: save_daily_recommendations 返回 False（详见服务端日志）")
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
        
        # 2026-10-12 修复：save_daily_recommendations 只替换列表内日期的行，
        # 被过滤掉的日期必须显式 DELETE，否则旧行残留（端点曾返回"删除成功"但记录仍在）。
        save_daily_recommendations(daily_recommendations)
        delete_daily_recommendations_by_date(date)
        _invalidate_daily_recs_cache()          # 写后清档
        
        return {"success": True, "message": "推荐记录删除成功"}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"删除推荐记录失败: {str(error)}")


@router.post("/api/generate-recommendations", tags=["推荐记录"])
async def generate_recommendations_api():
    """手动触发生成智能推荐并返回结果"""
    try:
        result = await _recommend_svc.auto_generate_recommendations_task()
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
    # 2026-07-06 : 已合并到 services.utils.ensure_today_date，此处保留薄壳透传以最小化改动
    return ensure_today_date(data)

@router.get("/api/trend-stocks", tags=["趋势发现"])
async def get_trend_scan_results():
    try:
        trend_data = load_trend_scan_results()
        trend_data = _ensure_today_date(trend_data)
        # V4.6: 为每只趋势股票注入买卖点（位置感知，trend_details 精确度最高）
        stocks = trend_data.get('stocks', [])
        for s in stocks:
            # 涨跌幅（2026-10-23）：新快照写在 details.changePercent；
            # 旧快照没有该字段时用 recent5Days[-2]（昨收）与 latestPrice 回退，
            # 二者同属一次扫描快照，保证价格与涨跌幅口径一致。
            change_pct = (s.get('details') or {}).get('changePercent')
            if change_pct is None:
                recent = s.get('recent5Days') or []
                price = s.get('latestPrice')
                try:
                    prev_close = float(recent[-2]) if len(recent) >= 2 else 0
                    if prev_close > 0 and price:
                        change_pct = (float(price) - prev_close) / prev_close * 100
                except (TypeError, ValueError):
                    change_pct = None
            try:
                s['changePercent'] = round(float(change_pct), 2) if change_pct is not None else None
            except (TypeError, ValueError):
                s['changePercent'] = None
            try:
                details = s.get('details', {}) or {}
                score_result = {'total': s.get('score', 0), 'base_scores': {}, 'factor_values': {}}
                s['buySellPoints'] = _recommend_svc.calc_buy_sell_points(s, score_result=score_result, trend_details=details)
            except Exception as _e:
                print(f'[趋势] 买卖点计算失败 {s.get("symbol")}: {_e}')
        return {"success": True, "data": trend_data}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取趋势股票失败: {str(error)}")


@router.get("/api/trend/history-counts", tags=["趋势发现"])
async def get_trend_history_counts(days: int = 30):
    """趋势扫描历史数量统计（市场温度计：每日扫描数/符合数，辅助判断减仓加仓时机）"""
    try:
        conn = get_db_conn()
        c = conn.cursor()
        c.execute('''
            SELECT
                scan_date,
                SUM(CASE WHEN symbol != '__SCAN_EMPTY__' THEN 1 ELSE 0 END) as result_count,
                MIN(total_scanned) as scanned_count,
                ROUND(AVG(CASE WHEN symbol != '__SCAN_EMPTY__' THEN score ELSE NULL END), 1) as avg_score,
                MAX(CASE WHEN symbol != '__SCAN_EMPTY__' THEN score ELSE 0 END) as max_score,
                SUM(CASE WHEN symbol != '__SCAN_EMPTY__' AND score >= 80 THEN 1 ELSE 0 END) as count_80plus,
                SUM(CASE WHEN symbol != '__SCAN_EMPTY__' AND score >= 55 THEN 1 ELSE 0 END) as count_55plus
            FROM trend_scan_results
            WHERE scan_date IS NOT NULL AND scan_date != ''
            GROUP BY scan_date
            ORDER BY scan_date DESC
            LIMIT ?
        ''', (days * 3,))
        rows = c.fetchall()
        conn.close()
        data = [{
            'date': r['scan_date'],
            'scanned': r['scanned_count'] or 0,
            'result_count': r['result_count'],
            'avg_score': r['avg_score'] or 0,
            'max_score': r['max_score'] or 0,
            'count_80plus': r['count_80plus'] or 0,
            'count_55plus': r['count_55plus'] or 0,
        } for r in rows]
        # 仅保留交易日（排除周末与法定节假日），再截取所需条数
        data = [d for d in data if is_trading_day(d['date'])][:days]
        return {"success": True, "data": data}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取趋势历史统计失败: {str(error)}")


@router.get("/api/scan-trend-stocks", tags=["趋势发现"])
async def scan_trend_stocks(force: bool = False):
    try:
        result = await scan_trend_scan_results(force=force)
        return {"success": True, "data": _ensure_today_date(result.get("data", result))}
    except Exception as error:
        # 2026-07-14 修复：500 时打印完整 traceback，否则只看到 "扫描趋势股票失败: xxx" 无法定位
        import traceback as _tb
        print(f'[API] /api/scan-trend-stocks 500: {type(error).__name__}: {error}')
        print(f'[API] /api/scan-trend-stocks traceback:\n{_tb.format_exc()}')
        raise HTTPException(status_code=500, detail=f"扫描趋势股票失败: {str(error)}")


# ========== 趋势评分回测验证 API（2026-07-01）==========
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


@router.get("/api/stock-diagnose/{symbol}", tags=["五层诊断"])
async def stock_diagnose_api(symbol: str):
    """五层本地诊断书（2026-10-20，替代已删除的 AI 诊断）：位置/资金/环境/系统历史/纪律价，
    纯本地库聚合＋规则判据，不接任何大模型；DB 密集读取放线程池，不压事件循环。"""
    if not re.match(r'^(sh|sz)?\d{6}$', symbol.lower()):
        raise HTTPException(status_code=400, detail="无效的股票代码格式")
    from services.support.diagnose import diagnose_stock
    try:
        return await asyncio.to_thread(diagnose_stock, symbol)
    except Exception as error:
        print(f'[五层诊断] 失败 {symbol}: {error}')
        raise HTTPException(status_code=500, detail=f"诊断失败: {str(error)}")

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
    # 2026-10-17 修复：services/stock.py 已搬到 services/data/stock.py，
    # 旧写法 `import services.data.stock` + `services.stock.x` 会抛
    # AttributeError: module 'services' has no attribute 'stock'。
    from services.data import stock as _stock_data
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
        _stock_data._stock_basic_info_cache = stock_basic_info
        
        return {"success": True}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))

@router.get("/api/custom-scan-pool", tags=["自定义扫描池"])
async def get_user_scan_pool_api(user: Dict = Depends(current_user)):
    try:
        symbols = load_user_scan_pool(user_id=int(user.get('id') or 1))
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
                        # -fix: get_stock_name 是 async def，必须 await，否则 info['name'] 会是 coroutine 对象
                        # FastAPI jsonable_encoder 序列化时炸 TypeError: 'coroutine' object is not iterable
                        name_from_quote = (quote_map.get(sym, {}) or {}).get('name')
                        if name_from_quote and not inspect.iscoroutine(name_from_quote):
                            info = {'symbol': sym, 'code': sym, 'name': name_from_quote}
                        else:
                            info = {'symbol': sym, 'code': sym, 'name': await get_stock_name(sym)}
                        realtime = 0
                        try:
                            price_val = quote_map.get(sym, {}).get('price', 0) or 0
                            # 防御：若 price 是 coroutine 同样丢弃
                            if not inspect.iscoroutine(price_val):
                                realtime = float(price_val)
                        except (TypeError, ValueError):
                            pass
                        info['realtimePrice'] = realtime
                        try:
                            kline = await get_kline_data(sym)
                            indicators = calc_pool_stock_indicators(kline or [], realtime)
                            # 防御：剥离 coroutine（极端情况），递归处理嵌套 dict/list
                            def _strip_coro(obj):
                                if inspect.iscoroutine(obj):
                                    return None
                                if isinstance(obj, dict):
                                    return {k: _strip_coro(v) for k, v in obj.items()}
                                if isinstance(obj, (list, tuple)):
                                    return [_strip_coro(x) for x in obj]
                                return obj
                            info.update(_strip_coro(indicators))
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
        cache_key = ','.join(sorted(set(codes_list)))
        _now_ts = time.time()
        _cached = _HOT_SCAN_CACHE.get(cache_key)
        _compute_now = force or getattr(_HOT_SCAN_CTX, 'compute', False)
        if not _compute_now and _cached:
            _age = _now_ts - _cached['ts']
            if _age < _HOT_SCAN_CACHE_TTL:
                return _cached['data']
            if _age < _HOT_SCAN_STALE_MAX:
                _spawn_hot_scan_refresh(cache_key, codes, pool_only)
                return _cached['data']
        
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
            stock['score'] = calc_stock_score_v2(stock, market_change=market_change,
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

        # 2026-08-31 性能优化：写入内存缓存（TTL 15s），下次轮询直接命中
        _HOT_SCAN_CACHE[cache_key] = {'ts': time.time(), 'data': response}
        return response
    except Exception as error:
        print(f'批量扫描失败: {str(error)}')
        raise HTTPException(status_code=502, detail=f"扫描失败: {str(error)}")

@router.get("/api/hot-stocks", tags=["热门股票"])
async def get_hot_stock_buttons_api(user: Dict = Depends(current_user)):
    try:
        stocks = load_hot_stock_buttons(user_id=int(user.get('id') or 1))
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
async def update_hot_stock_buttons_api(data: Dict, user: Dict = Depends(current_user)):
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
        
        save_hot_stock_buttons(valid_stocks, user_id=int(user.get('id') or 1))
        return {"success": True, "data": {"stocks": valid_stocks, "count": len(valid_stocks)}}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))

@router.put("/api/hot-stocks/add", tags=["热门股票"])
async def add_to_hot_stock_buttons_api(data: Dict, user: Dict = Depends(current_user)):
    try:
        code = data.get('code')
        name = data.get('name')
        
        if not code or not name:
            raise HTTPException(status_code=400, detail="参数缺失：需要code和name")
        
        if not re.match(r'^(sh|sz)\d{6}$', code.lower()):
            raise HTTPException(status_code=400, detail="股票代码格式无效")
        
        current_stocks = load_hot_stock_buttons(user_id=int(user.get('id') or 1))
        
        # 检查是否已存在
        exists = any(s['code'].lower() == code.lower() for s in current_stocks)
        if exists:
            return {"success": False, "error": "该股票已在热门股票中"}
        
        current_stocks.append({
            'code': code.lower(),
            'name': name
        })
        
        save_hot_stock_buttons(current_stocks, user_id=int(user.get('id') or 1))
        return {"success": True, "data": {"stocks": current_stocks, "count": len(current_stocks)}}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))

@router.put("/api/hot-stocks/remove", tags=["热门股票"])
async def remove_from_hot_stock_buttons_api(data: Dict, user: Dict = Depends(current_user)):
    try:
        code = data.get('code')
        if not code:
            raise HTTPException(status_code=400, detail="参数缺失：需要code")
        
        code_lower = code.lower()
        current_stocks = load_hot_stock_buttons(user_id=int(user.get('id') or 1))
        current_stocks = [s for s in current_stocks if s['code'].lower() != code_lower]
        
        save_hot_stock_buttons(current_stocks, user_id=int(user.get('id') or 1))
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

@router.get("/api/market-alert", tags=["大盘预警"])
async def get_market_alert_api():
    """获取最新大盘风险预警（前端轮询用，轻量级）"""
    try:
        alert = load_market_alert()
        return {"success": True, "data": alert}
    except Exception as error:
        return {"success": True, "data": None}

@router.get("/api/market-index", tags=["大盘指数"])
async def get_market_index_api():
    """获取大盘指数数据"""
    try:
        result = await check_market_crash()
        return {"success": True, "data": result}
    except Exception as error:
        print(f'[大盘指数API] 错误: {str(error)}')
        raise HTTPException(status_code=500, detail=f"获取大盘指数失败: {str(error)}")

@router.get("/api/action-desk", tags=["今日行动台"])
async def get_action_desk_api(phase: str = '', user: Dict = Depends(current_user)):
    """今日行动台：按当前交易时段聚合"现在该做什么"（前端首页唯一数据源）。

    只读聚合，全部走 DB / 文件 / 5 秒行情缓存，不触发任何全市场扫描。
    phase 参数仅供联调与截图脚本强制时段（如 ?phase=tail），线上不传即可。
    """
    try:
        data = await build_action_desk(user_id=int(user.get('id') or 1),
                                       phase_override=(phase or '').strip())
        return {"success": True, "data": data}
    except Exception as error:
        print(f'[行动台API] 错误: {str(error)}')
        raise HTTPException(status_code=500, detail=f"生成行动台数据失败: {str(error)}")

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

        # -fix: 改用 requests + asyncio.to_thread（去 httpx）
        response = await asyncio.to_thread(_sync_get, url)
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

@router.get("/api/fund-flow", tags=["资金流向"])
async def get_fund_flow_api(
    stock_top_n: int = FUND_FLOW_STOCK_TOP_N,
    sector_top_n: int = FUND_FLOW_SECTOR_TOP_N,
    change_threshold: float = FUND_FLOW_HIDDEN_CHANGE_THRESHOLD,
    hot_top_n: int = FUND_FLOW_HIDDEN_HOT_TOP_N,
    hidden_top_n: int = FUND_FLOW_HIDDEN_TOP_N,
):
    """资金流向汇总：个股主力净流入/净流出排名 + 板块净流入/净流出排名 + 非热点上涨股。

    三部分数据各自独立获取、独立容错；结果带 60s 内存缓存，适配前端 60s 轮询。
    查询参数可覆盖默认阈值（前端“非热点上涨股”区块支持自定义涨幅阈值 / 热点排除排名）。
    """
    try:
        data = await get_fund_flow_summary(
            stock_top_n=stock_top_n,
            sector_top_n=sector_top_n,
            change_threshold=change_threshold,
            hot_top_n=hot_top_n,
            hidden_top_n=hidden_top_n,
        )
        return {"success": True, "data": data}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取资金流向失败: {str(error)}")


@router.get("/api/rps", tags=["RPS相对强度"])
async def get_rps_api(period_days: int = RPS_PERIOD_DAYS, top_n: int = RPS_TOP_N):
    """RPS 相对强度强势股榜（欧奈尔核心因子，P0，2026-07-16）

    返回全市场区间涨幅百分位排名前 top_n 的强势股（RPS + 区间涨跌幅）。
    数据来自东方财富 clist 区间涨幅，带 1 小时内存缓存，适配前端定时刷新。
    """
    try:
        data = await get_rps_leaderboard(period_days, top_n)
        return {"success": True, "data": data}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取RPS强势股失败: {str(error)}")


@router.post("/api/rps/refresh", tags=["RPS相对强度"])
async def refresh_rps_api(period_days: int = RPS_PERIOD_DAYS, limit_codes: int = None):
    """后台触发全市场 RPS 预计算（盘前/手动）。

    耗时较长（拉全市场 K 线），立即返回 accepted，结果写入 rps_cache 表；
    盘中扫描/分析读取缓存。可选 limit_codes 用于小范围验证。
    """
    from services.strategy.rps import refresh_rps
    async def _bg():
        try:
            res = await refresh_rps(period_days, limit_codes)
            print(f'[RPS] 预计算完成: {res}')
        except Exception as e:
            print(f'[RPS] 预计算失败: {e}')
    asyncio.create_task(_bg())
    return {"success": True, "message": "RPS 预计算已在后台启动", "periodDays": period_days}


@router.get("/api/kline-history/stats", tags=["本地历史K线库"])
async def kline_history_stats_api():
    """查看本地历史日K线库统计（股票数/K线行数/数据区间）。

    A 级优化：RPS/趋势/评分的区间涨幅从此本地库向量化计算，绕开东方财富限流。
    若 symbolCount=0 表示尚未回填，需先执行 `python scripts/backfill_kline.py --mode backfill`。
    """
    try:
        from services.data.kline_history import get_engine, history_available
        eng = get_engine()
        stats = eng.get_db_stats(with_scope=True)
        stats["available"] = history_available()
        return {"success": True, "data": stats}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取历史库统计失败: {str(error)}")


# ========== 运行时配置覆盖（复盘子页"口径与开关"，2026-10-17） ==========
# 为什么读和写的权限不一样：这些开关一拨就改变**所有账号**的选票范围与推送量，
# 而 users 表没有角色列（自注册体系）—— 所以读=登录即可，写=只允许
# `SETTINGS_WRITE_USER_IDS` 里的账号 id，改动人会记进 app_settings.updated_by 便于回溯。
@router.get("/api/settings", tags=["配置"])
async def get_settings_api(user: Dict = Depends(current_user)):
    """列出可改项（当前值/默认值/是否被覆盖/生效方式）+ 口径摘要与改动记录。

    2026-10-17 提速：口径摘要四项（范围/选票/预览一致性/陈旧数）改为 to_thread 并行，
    热路径全命中时 <50ms。
    2026-10-30 再提速：摘要整份缓存化（settings_preview.compute_summary），调度器
    启动预热 + 每 9 分钟保活刷新，改「选股范围」配置后由 app_settings 触发后台重算。
    请求只读快照，实测冷路径 27.8s / 过期 5.3s 从此不再出现在请求路径上；
    只有快照缺失（重启后预热未完成的窗口）才由本请求现算兜底。
    """
    from services.support import app_settings as A, settings_preview as P
    items = A.list_items()

    _snap = P.summary_snapshot()
    if _snap is None:
        summary = await asyncio.to_thread(P.compute_summary)
    else:
        summary = dict(_snap)
        if P.summary_age() > P.SUMMARY_REFRESH_SEC:
            P.refresh_summary_async()
    return {"success": True, "data": {'items': items, 'summary': summary,
                                      'history': A.history(15),
                                      'canWrite': A.can_write(user),
                                      'writeIds': list(A.write_ids())}}


@router.get("/api/settings/preview", tags=["配置"])
async def preview_setting_api(key: str, value: str, user: Dict = Depends(current_user)):
    """"改成这个值会怎样" —— 界面切换/改数时按需调一次（约 0.3~0.8 秒，不做全量预取）。"""
    from services.support import app_settings as A, settings_preview as P
    if key not in [s['key'] for s in A.SPEC]:
        raise HTTPException(status_code=404, detail=f'不允许修改的配置项: {key}')
    incoming = (value.lower() in ('1', 'true', 'on', 'yes', '开')) if A.spec(key)['kind'] == 'bool' else value
    ok, val = A.coerce(key, incoming)
    if not ok:
        raise HTTPException(status_code=400, detail=val)
    return {"success": True, "data": P.preview_for(key, val)}


@router.post("/api/settings/{key}", tags=["配置"])
async def set_setting_api(key: str, body: Dict, user: Dict = Depends(current_user)):
    from services.support import app_settings as A
    if not A.can_write(user):
        raise HTTPException(status_code=403, detail=(
            f'当前账号无权修改口径（可改账号 id={A.write_ids()}，默认是最早注册的账号）；'
            '这些开关一改就影响所有账号的选票范围'))
    res = A.set_value(key, body.get('value'), user=str(user.get('username') or user.get('id')))
    if not res.get('ok'):
        raise HTTPException(status_code=400, detail=res.get('error') or '写入失败')
    print(f"[配置] {user.get('username') or user.get('id')} 改了 {key} = {res['value']}"
          f"（{'即时生效' if res['live'] else '需重启才生效'}）")
    return {"success": True, "data": res}


@router.post("/api/settings/{key}/reset", tags=["配置"])
async def reset_setting_api(key: str, user: Dict = Depends(current_user)):
    from services.support import app_settings as A
    if not A.can_write(user):
        raise HTTPException(status_code=403, detail='当前账号无权修改口径（默认只有最早注册的账号能改）')
    res = A.reset_value(key, user=str(user.get('username') or user.get('id')))
    if not res.get('ok'):
        raise HTTPException(status_code=400, detail=res.get('error') or '清除失败')
    return {"success": True, "data": res}


@router.post("/api/kline-history/sync", tags=["本地历史K线库"])
async def kline_history_sync_api():
    """后台触发本地历史日K线库每日增量同步（baostock 前复权），完成后向量化刷新 RPS。

    轻量（1~2 分钟），对已有数据幂等；立即返回 accepted。
    """
    async def _bg():
        try:
            from services.data.kline_history import get_engine, history_available
            eng = get_engine()
            eng.sync_today_bulk()
            if history_available():
                from services.strategy.rps import refresh_rps
                await refresh_rps(RPS_PERIOD_DAYS)
            print("[历史K线] 增量同步 + RPS 刷新完成")
        except Exception as e:
            print(f"[历史K线] 增量同步失败: {e}")
    asyncio.create_task(_bg())
    return {"success": True, "message": "本地历史库增量同步已在后台启动"}


@router.post("/api/kline-history/backfill", tags=["本地历史K线库"])
async def kline_history_backfill_api(start_date: str = None, limit: int = 0):
    """后台触发全市场历史日K线回填（baostock 前复权，单线程断点续传，约 12 分钟）。

    仅首次部署需要；日常用 /sync 即可。立即返回 accepted。
    """
    async def _bg():
        try:
            from services.data.kline_history import get_engine
            eng = get_engine()
            from app.config import KLINE_HISTORY_START_DATE
            symbols = eng.get_all_symbols()
            if not symbols:
                print("[历史K线] 无法获取股票列表（baostock 登录失败？），回填终止")
                return
            if limit and limit > 0:
                symbols = symbols[:limit]
            eng.backfill(symbols=symbols, start_date=start_date or KLINE_HISTORY_START_DATE)
            print("[历史K线] 全量回填完成")
        except Exception as e:
            print(f"[历史K线] 全量回填失败: {e}")
    asyncio.create_task(_bg())
    return {"success": True, "message": "全市场历史K线回填已在后台启动（约12分钟）"}


@router.post("/api/fundamental/refresh", tags=["基本面质量"])
async def refresh_fundamental_api():
    """后台触发全市场基本面质量预计算（盘后/手动，P1a）。

    拉取最新一期 ROE/净利润同比，写入 stock_fundamental 表；立即返回 accepted。
    """
    async def _bg():
        try:
            await update_fundamental_task()
            print('[基本面] 预计算完成')
        except Exception as e:
            print(f'[基本面] 预计算失败: {e}')
    asyncio.create_task(_bg())
    return {"success": True, "message": "基本面质量预计算已在后台启动"}


@router.get("/api/dragon-tiger", tags=["聪明钱"])
async def get_dragon_tiger_api(days: int = SMART_MONEY_DRAGON_TIGER_DAYS, top_n: int = 30, force: bool = False):
    """龙虎榜聪明钱聚合榜（P1b）。返回近 days 日上榜个股净买入排名。force=true 时忽略缓存。"""
    try:
        data = get_dragon_tiger(days, top_n, force=force)
        return {"success": True, "data": data}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取龙虎榜失败: {str(error)}")


@router.get("/api/northbound", tags=["聪明钱"])
async def get_northbound_api(force: bool = False):
    """北向资金市场级净买额（P1b）。返回沪深股通当日/月度净买及情绪判断。force=true 时忽略缓存。"""
    try:
        data = get_northbound_market(force=force)
        return {"success": True, "data": data}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取北向资金失败: {str(error)}")


@router.post("/api/valuation/refresh", tags=["估值分位"])
async def refresh_valuation_api():
    """后台触发全市场估值分位预计算（盘后/手动，P2c）。

    拉取全市场 PE/PB 算百分位，写入 stock_valuation 表；立即返回 accepted。
    """
    async def _bg():
        try:
            res = update_valuation_task()
            print(f'[估值] 预计算完成: {res}')
        except Exception as e:
            print(f'[估值] 预计算失败: {e}')
    asyncio.create_task(_bg())
    return {"success": True, "message": "估值分位预计算已在后台启动"}


@router.get("/api/valuation", tags=["估值分位"])
async def get_valuation_api(symbols: str = ''):
    """批量查询估值分位（P2c）。symbols 逗号分隔，留空返回空。"""
    try:
        syms = [s.lower() for s in symbols.split(',') if re.match(r'^(sh|sz)\d{6}$', s.lower())]
        data = get_valuation_batch(syms)
        return {"success": True, "data": data}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取估值分位失败: {str(error)}")


@router.get("/api/trend/backtest-summary", tags=["趋势发现"])
async def get_trend_backtest_summary(limit: int = 30):
    """趋势回测策略自检验汇总（P2b 增强）。

    聚合所有回测报告：总样本数、1/3/5 日加权胜率、平均超额收益
    （个股 N 日涨跌 − 大盘 N 日涨跌，来自 trend_backtest_records）、近期报告列表。
    让"回头看自己"的闭环对前端可见、可量化。
    """
    try:
        reports = load_trend_backtest_reports(limit=limit)
        if not reports:
            return {"success": True, "data": {"hasData": False}}
        # 跨报告加权胜率（按各周期验证样本数加权）
        agg = {'verified_1d': 0, 'win_1d': 0, 'verified_3d': 0, 'win_3d': 0,
               'verified_5d': 0, 'win_5d': 0, 'total_count': 0, 'reports': 0}
        for r in reports:
            agg['reports'] += 1
            agg['total_count'] += r.get('total_count', 0) or 0
            for p in ('1d', '3d', '5d'):
                agg[f'verified_{p}'] += r.get(f'verified_{p}_count', 0) or 0
                agg[f'win_{p}'] += r.get(f'win_{p}_count', 0) or 0

        def _wr(w, v):
            return round(w / v * 100, 1) if v > 0 else 0.0

        win_rates = {'1d': _wr(agg['win_1d'], agg['verified_1d']),
                     '3d': _wr(agg['win_3d'], agg['verified_3d']),
                     '5d': _wr(agg['win_5d'], agg['verified_5d'])}

        # 平均超额收益（个股 N 日涨跌 − 大盘 N 日涨跌），来自明细记录
        conn = get_db_conn()
        c = conn.cursor()
        excess = {}
        for p in ('1d', '3d', '5d'):
            c.execute(
                f'''SELECT AVG(actual_change_{p} - market_change_{p})
                    FROM trend_backtest_records WHERE verified_{p} = 1
                    AND actual_change_{p} IS NOT NULL AND market_change_{p} IS NOT NULL''')
            row = c.fetchone()
            val = row[0] if row and row[0] is not None else 0.0
            excess[p] = round(float(val), 2)
        conn.close()

        return {
            "success": True,
            "data": {
                "hasData": True,
                "reports": agg['reports'],
                "totalSamples": agg['total_count'],
                "verified": {'1d': agg['verified_1d'], '3d': agg['verified_3d'], '5d': agg['verified_5d']},
                "winRates": win_rates,
                "avgExcessReturn": excess,
                "recent": [
                    {
                        'predict_date': r.get('predict_date'),
                        'total_count': r.get('total_count', 0),
                        'win_rate_1d': round(r.get('win_rate_1d', 0) * 100, 1),
                        'win_rate_3d': round(r.get('win_rate_3d', 0) * 100, 1),
                        'win_rate_5d': round(r.get('win_rate_5d', 0) * 100, 1),
                    } for r in reports[:10]
                ],
            }
        }
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取回测汇总失败: {str(error)}")


# ========== 东方财富 API 代理（兼容生产模式） ==========
# 前端 fetchEnrichData() 需要通过 /eastmoney/* 调用东方财富接口
# Vite 开发模式下由 vite.config.js 代理；生产模式下由此路由代理
# 2026-07-02 : 请求头统一使用 config.EASTMONEY_HEADERS
# 2026-07-06 A-: 模块级 requests.Session 复用连接池，避免每次请求新建 TCP 连接
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
    """将 /eastmoney/* 请求转发到东方财富 API（requests Session + 重试，解决 RemoteDisconnected）

    2026-10-19：接入共享熔断 eastmoney_guard——前端唯一能任意路径直连东财的通道，
    冷却中直接 503，不再 3 次重试硬打（东财 clist IP 级风控实锤，频率是主因）。
    前端对非 2xx 均 catch(()=>null) 静默降级，行为兼容。
    """
    import asyncio, time as _time
    try:
        from services.data import eastmoney_guard
        if eastmoney_guard.skip('clist'):
            return Response(content=b'{"error":"eastmoney clist cooling"}',
                            status_code=503, media_type="application/json")
    except Exception:
        pass
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
        try:
            from services.data import eastmoney_guard
            eastmoney_guard.ok('clist')
        except Exception:
            pass
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers={"Content-Type": resp.headers.get("content-type", "application/json")},
        )
    except Exception as e:
        print(f'[东方财富代理] 请求失败: {e}')
        try:
            from services.data import eastmoney_guard
            eastmoney_guard.fail('clist')
        except Exception:
            pass
        return Response(content=b"{}", status_code=502, media_type="application/json")


# ========== 自动优化引擎 API（V4.4）==========

@router.get("/api/optimization/history", tags=["自动优化"])
async def get_optimization_history(module: str = '', limit: int = 50):
    """查询自动优化历史"""
    from db.database import load_optimization_history
    module_param = module if module else None
    data = load_optimization_history(module=module_param, limit=limit)
    return {"success": True, "data": data}


@router.get("/api/optimization/status", tags=["自动优化"])
async def get_optimization_status():
    """查询当前优化状态（D1-D4权重）
    2026-10-28 角色体系下线：不再返回 roleThresholds / rolePerformance。"""
    from db.database import load_trend_dimension_weights
    dim_weights = load_trend_dimension_weights()
    return {
        "success": True,
        "data": {
            "dimensionWeights": dim_weights,
        }
    }


@router.post("/api/optimization/trigger", tags=["自动优化"])
async def trigger_optimization(run_grid: bool = False):
    """手动触发自动优化"""
    from services.ops.auto_optimizer import run_auto_optimization
    result = await run_auto_optimization(run_grid=run_grid)
    return {"success": True, "data": result}


@router.get("/api/optimization/trend", tags=["自动优化"])
async def get_optimization_trend():
    """优化趋势聚合数据（前端面板展示用）"""
    from db.database import get_db_conn
    conn = get_db_conn()
    cursor = conn.cursor()

    # 1. 权重变化趋势（按天聚合 optimization_history 中 trend_dimension_weights 模块）
    cursor.execute('''
        SELECT created_at, parameter_set, metric_value, details
        FROM optimization_history
        WHERE module = 'trend_dimension_weights'
        ORDER BY created_at DESC LIMIT 60
    ''')
    weight_history = [dict(r) for r in cursor.fetchall()]

    # 2. 网格搜索结果历史（2026-10-28 删掉"角色近期表现"：角色体系已下线）
    cursor.execute('''
        SELECT created_at, metric_value as best_winrate,
               old_value as current_winrate,
               new_value - old_value as improvement,
               parameter_set
        FROM optimization_history
        WHERE module = 'grid_search'
        ORDER BY created_at DESC LIMIT 10
    ''')
    grid_history = [dict(r) for r in cursor.fetchall()]

    # 3. 全量优化统计
    cursor.execute('SELECT count(*) as cnt FROM optimization_history')
    total_opt = cursor.fetchone()['cnt']

    cursor.execute('''
        SELECT count(*) as cnt FROM optimization_history
        WHERE created_at >= date('now', '-7 days')
    ''')
    week_opt = cursor.fetchone()['cnt']

    conn.close()

    return {
        "success": True,
        "data": {
            "weightHistory": weight_history,
            "gridHistory": grid_history,
            "stats": {
                "totalOptimizations": total_opt,
                "weeklyOptimizations": week_opt,
            }
        }
    }


# 最后挂载静态文件，这样API路由会先被匹配到

