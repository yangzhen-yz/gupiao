"""app - FastAPI 应用实例、中间件、生命周期"""
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import os, sys, time, asyncio, threading
from datetime import date

from . import config as _cfg
import importlib

_cfg_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.py')
_cfg_mtime = os.path.getmtime(_cfg_file)


_CONFIG_ALIAS_MAP = {
    '_MARKET_CRASH_THRESHOLD': 'MARKET_CRASH_THRESHOLD',
}

_CONFIG_SAME_NAMES = [
    'SQLITE_DB_PATH',
    'TENCENT_QUOTE_API', 'TENCENT_KLINE_API', 'EASTMONEY_HOT_LIST_API',
    'AI_CONFIG_FILE',
    'HTTP_TIMEOUT', 'HTTP_CONNECT_TIMEOUT', 'HTTP_MAX_CONNECTIONS', 'HTTP_KEEPALIVE_CONNECTIONS',
    'HOT_SEARCH_TOP_N', 
    'SCAN_CONCURRENCY', 'SCAN_KLINE_TIMEOUT', 'SCAN_CACHE_TTL',
    'TREND_MIN_SCORE', 'TREND_MIN_KLINE_DAYS', 'TREND_LIMIT_UP_THRESHOLD',
    'TREND_LIMIT_UP_120_MIN', 'TREND_LIMIT_UP_250_MIN', 'TREND_MAX_DRAWDOWN',
    'TREND_CONSECUTIVE_UP_BONUS', 'TREND_CONSECUTIVE_UP_MAX_BONUS', 'TREND_IS_UP_MIN_SCORE',
    'TREND_MA120_SLOPE_WINDOW', 'TREND_CONSECUTIVE_UP_MAX_DAYS', 'USE_INTRADAY_BREAK_CHECK',
    'TREND_SCORE_WEIGHTS',
    'STRATEGY_BULL_THRESHOLD', 'STRATEGY_BEAR_THRESHOLD', 'STRATEGY_PREDICT_BULL_MIN_SCORE',
    'STRATEGY_BACKTEST_PREDICTION_LIMIT', 'STRATEGY_BACKTEST_HISTORY_DAYS',
    'STRATEGY_SCORE_THRESHOLDS', 'STRATEGY_SCORE_VALUES', 'STRATEGY_WEBI_VERIFY', 'STRATEGY_PE_FILTER',
    'STRATEGY_LABEL_THRESHOLDS',
    'DEFAULT_WEIGHTS',
    'MARKET_SEVERE_CRASH_THRESHOLD',
    'DELISTING_KEYWORDS', 'FORBIDDEN_KEYWORDS', 'HOT_STOCK_POOL',
    'SCHEDULE_UPDATE_HOT_SEARCH_TIMES', 'SCHEDULE_MA10_CHECK_TIME',
    'SCHEDULE_DAILY_REVIEW_TIME', 'SCHEDULE_BACKTEST_TIME',
    'SCHEDULE_RECOMMENDATION_TIMES',
    'QUERY_DAILY_RECOMMENDATIONS_LIMIT', 'QUERY_TREND_RESULTS_LIMIT',
    'QUERY_MARKET_REVIEWS_LIMIT',
    'REVIEW_TOP_GAINERS_COUNT', 'REVIEW_TOP_LOSERS_COUNT', 'REVIEW_TOP_SECTORS_COUNT',
    'REVIEW_TOMORROW_FOCUS_COUNT',
    'INDEX_SYMBOLS',
    'KLINE_START_DATE', 'KLINE_DATA_LIMIT', 'KLINE_DISPLAY_MA_POINTS',
]




def _reload_config():
    global _cfg_mtime
    try:
        mtime = os.path.getmtime(_cfg_file)
        if mtime <= _cfg_mtime:
            return False
        importlib.reload(_cfg)
        _cfg_mtime = mtime
        g = globals()
        for local_name, cfg_name in _CONFIG_ALIAS_MAP.items():
            val = getattr(_cfg, cfg_name, None)
            if val is not None:
                g[local_name] = val
        for name in _CONFIG_SAME_NAMES:
            val = getattr(_cfg, name, None)
            if val is not None:
                g[name] = val
        g['TENCENT_API'] = g['TENCENT_QUOTE_API']
        # 同步 reload 业务模块，让它们的 from app.config import 常量重新求值
        for mod_name in ('services.stock', 'services.trend', 'services.recommend'):
            mod = sys.modules.get(mod_name)
            if mod is not None:
                try:
                    importlib.reload(mod)
                    print(f'[Config] 已重载 {mod_name}')
                except Exception as e:
                    print(f'[Config] 重载 {mod_name} 失败: {e}')
        print('[Config] 热加载完成 (config.py 已变更)')
        return True
    except Exception as e:
        print(f'[Config] 热加载失败: {e}')
        return False



from db.database import init_db, save_hot_search_ranking, load_trend_scan_results
from services.stock import get_http_client, fetch_eastmoney_hot_list
from services.trend import scan_trend_task
from services.ai import load_ai_config

_last_scan_result = None
_last_scan_date = ''
_last_scan_ts = 0.0

def _lazy_start_scheduler():
    """延迟导入调度器，避免循环引用"""
    from core.scheduler import run_scheduler
    run_scheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _last_scan_result, _last_scan_date, _last_scan_ts
    init_db()
    load_ai_config()
    print('[初始化] 正在获取热搜榜数据...')
    try:
        data = await fetch_eastmoney_hot_list()
        save_hot_search_ranking(data)
        print('[初始化] 热搜榜数据获取完成')
    except Exception as error:
        print(f'[初始化] 获取热搜榜失败: {str(error)}')
    try:
        cached = load_trend_scan_results()
        if cached and cached.get('stocks'):
            _last_scan_result = cached
            _last_scan_date = cached.get('date', '')
            _last_scan_ts = time.time()
            print(f'[初始化] 预加载趋势数据完成: {_last_scan_date}, {len(cached.get("stocks", []))}只股票')
            today = date.today().isoformat()
            if _last_scan_date != today:
                print(f'[初始化] 趋势数据过期({_last_scan_date})，后台开始扫描...')
                asyncio.create_task(scan_trend_task(force=True))
    except Exception as error:
        print(f'[初始化] 预加载趋势数据失败: {str(error)}')
    get_http_client()
    print('[初始化] HTTP连接池已创建')
    scheduler_thread = threading.Thread(target=lambda: _lazy_start_scheduler(), daemon=True)
    scheduler_thread.start()
    yield
    import services.stock as _s
    if _s.http_client and not _s.http_client.is_closed:
        await _s.http_client.aclose()
        print('[关闭] HTTP连接池已关闭')

app = FastAPI(title="A股行情API", description="东方财富热搜榜、股票行情、K线图、趋势发现", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# 注册路由
from app.api.routes import router
app.include_router(router)

@app.get("/eastmoney/{path:path}", tags=["东方财富代理"])
async def eastmoney_proxy(path: str, request: Request):
    """将 /eastmoney/* 请求代理到 https://push2.eastmoney.com/*"""
    from services.stock import get_http_client
    client = get_http_client()
    query_string = str(request.url.query)
    target_url = f"https://push2.eastmoney.com/{path}"
    if query_string:
        target_url += f"?{query_string}"
    try:
        resp = await client.get(target_url, timeout=8.0)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers={"Content-Type": resp.headers.get("Content-Type", "application/json")}
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"代理请求失败: {str(e)}")

@app.middleware("http")
async def _config_reload_middleware(request, call_next):
    _reload_config()
    response = await call_next(request)
    return response
