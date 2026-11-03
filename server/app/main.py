"""app - FastAPI 应用实例、中间件、生命周期"""

# ── 运行期日志编码兜底（2026-11-02）────────────────────────────────────────
# 本机看护把进程 stdout 重定向进 server/server_supervised.log，而那条管道是按 **GBK**
# 建的（日志里 GBK 与 UTF-8 混排就是证据）。后果不是"难看"而是"任务被日志打死"：
# 全仓 32 处运行期 print 里有 ⚠️/⚠ 这类 GBK 编不出的字符，实锤一例——`[推荐模式]`
# 的接力票告警自 10-26 起抛 UnicodeEncodeError 16 次（'gbk' can't encode '\u26a0'），
# 连带把后面的标签汇总日志一起吞了。盘后/盘中任务最怕失败得看不见，所以在应用入口
# 一次性把 stdout/stderr 收到 UTF-8 + errors='replace'：**任何一条日志都不该再把它的
# 调用方打死**。只在探测到 GBK 家族编码时动手，别人显式配了 UTF-8 就不越权。
# 回滚：删掉本块即可（各 print 恢复原行为）。
try:
    import sys as _sys

    def _make_log_utf8_safe() -> None:
        for _name in ('stdout', 'stderr'):
            _s = getattr(_sys, _name, None)
            if _s is None or not hasattr(_s, 'reconfigure'):
                continue
            _enc = (getattr(_s, 'encoding', '') or '').lower().replace('-', '').replace('_', '')
            if _enc in ('gbk', 'cp936', 'gb2312', 'gb18030'):
                _s.reconfigure(encoding='utf-8', errors='replace')

    _make_log_utf8_safe()
except Exception as _e:      # 兜底本身绝不影响启动
    try:
        print(f'[启动] 日志编码兜底未生效（不影响启动）: {type(_e).__name__}: {_e}')
    except Exception:
        pass

# ── Windows proactor 事件循环的"客户端硬断开"噪声过滤（2026-11-02）────────
# `server/server_supervised.log` 里 `Exception in callback` 一共 160 条，**100% 是同一个签名**：
# `_ProactorBasePipeTransport._call_connection_lost` → `OSError: [WinError 10022] 提供了一个无效
# 的参数`。这是 Windows asyncio proactor 循环在隧道/浏览器直接断开连接时的固有行为（socket 已
# 被对端关掉，再 shutdown 就报 10022），**不是本项目的故障**，但它把真正的错误埋了——排查时
# grep 'Traceback' 命中 160 次全是它。所以只掐这一个组合（两个标记都在才丢），其余 asyncio
# 记录一律放行。回滚：删掉本块。
PROACTOR_NOISE_MARKERS = ('_call_connection_lost', 'WinError 10022')


class _ProactorNoiseFilter:
    """logging.Filter：命中 proactor "连接已断"固定噪声才丢弃（两个标记必须同时出现）。"""

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:          # 取不到文本就放行，绝不因为过滤器本身出错而丢日志
            return True
        return not all(m in msg for m in PROACTOR_NOISE_MARKERS)

    # 兼容 addFilter(callable) 与 addFilter(Filter) 两种用法
    __call__ = filter


try:
    import logging as _logging

    _asyncio_logger = _logging.getLogger('asyncio')
    if not any(getattr(f, 'drops_proactor_noise', False) for f in _asyncio_logger.filters):
        _pf = _ProactorNoiseFilter()
        _pf.drops_proactor_noise = True
        _asyncio_logger.addFilter(_pf)
except Exception as _e:
    try:
        print(f'[启动] proactor 噪声过滤未生效（不影响启动）: {type(_e).__name__}: {_e}')
    except Exception:
        pass

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from contextlib import asynccontextmanager
import os, time, asyncio, threading
from datetime import date, datetime, timedelta

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
    'HOT_SEARCH_TOP_N', 'POPULARITY_RANK_TOP_N', 'CHANGE_RANK_TOP_N', 'TURNOVER_RANK_TOP_N',
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
    'SCHEDULE_TREND_SCAN_INTERVAL_MIN', 'SCHEDULE_TREND_SCAN_START', 'SCHEDULE_TREND_SCAN_END',
    'SCHEDULE_TREND_BACKTEST_TIME',
    'TREND_BACKTEST_PERIODS', 'TREND_BACKTEST_KLINE_CONCURRENCY',
    'TREND_BACKTEST_MARKET_SYMBOL', 'TREND_BACKTEST_SCORE_SEGMENTS',
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
        # 2026-10-29：不再 importlib.reload 业务模块。
        # reload 只替换模块字典，不会替换别处 already from-import 的旧函数对象：
        # 旧函数体 + 新全局表混用，就是 10-29 那次
        # "_build_expectation_note() takes 3 positional arguments but 4 were given"
        # 的根因（全仓有 33 处顶层 from-import 指向这三个模块）。
        # 常改的开关请走 app_settings 覆盖表（每次取值现读，不受此影响）；
        # 直接改 config.py 的常量需要重启后端才生效（写 server/data/backend_restart_requested）。
        print('[Config] config.py 已变更：app.config 已重载；业务模块常量需重启后端生效')
        return True
    except Exception as e:
        print(f'[Config] 热加载失败: {e}')
        return False



from db.database import init_db, save_hot_search_ranking, load_trend_scan_results, load_daily_market_reviews
from services.data.stock import fetch_eastmoney_hot_list
from services.strategy.trend import scan_trend_task
from services.data.trading_calendar import is_trading_day

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
    # 2026-10-24（性能）：扩容 asyncio.to_thread 默认线程池。
    # 默认池 min(32, CPU+4)（本机 12 核 = 16 线程）会被趋势扫描打满
    # （并发 25 × 每票 ≥2 次 to_thread），扫描期间全站 to_thread 请求排队，
    # 实测连鉴权 401 拒绝都要 20s+。扫描是 IO 密集，放大线程数不挤占 CPU；
    # 上限走 config.TO_THREAD_POOL_MAX_WORKERS 可调。
    try:
        from concurrent.futures import ThreadPoolExecutor
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=_cfg.TO_THREAD_POOL_MAX_WORKERS,
                               thread_name_prefix='to_thread'))
        print(f'[初始化] to_thread 线程池已扩容: max_workers={_cfg.TO_THREAD_POOL_MAX_WORKERS}')
    except Exception as e:
        print(f'[初始化] to_thread 线程池扩容失败（沿用默认池）: {e}')
    init_db()
    # 接口用量统计：在 scheduler 启动前接管所有出站 HTTP（requests/httpx/urllib），
    # 这样首屏热搜榜拉取、盘后同步等全部被计入，避免遗漏。失败不影响主流程。
    try:
        from services.data import api_usage
        api_usage.install()
    except Exception as _e:
        print(f'[初始化] 接口用量统计安装失败（不影响其他功能）: {_e}')
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
    # 启动时收评补生成：交易日 15:10 后若今日收评缺失，后台自动补生成
    try:
        _now = datetime.now()
        _today_str = _now.strftime('%Y-%m-%d')
        if is_trading_day(_today_str) and _now.strftime('%H:%M') >= '15:10':
            _existing_reviews = load_daily_market_reviews()
            _has_today_review = any(r.get('date') == _today_str for r in (_existing_reviews or [])[:3])
            if not _has_today_review:
                print(f'[初始化] 今日({_today_str})收评缺失，后台补生成中...')
                from services.ops.review import generate_daily_review
                from db.database import save_daily_market_reviews
                async def _startup_review():
                    try:
                        _review = await generate_daily_review()
                        save_daily_market_reviews([_review])
                        print(f'[初始化] 收评补生成完成，情绪分={_review.get("aiAnalysis",{}).get("sentimentScore","N/A")}')
                    except Exception as _e:
                        print(f'[初始化] 收评补生成失败: {_e}')
                asyncio.create_task(_startup_review())
    except Exception as error:
        print(f'[初始化] 收评补生成检查失败: {str(error)}')
    # 启动时回测补执行：交易日 15:20 后若最近交易日回测报告缺失，后台自动补跑
    try:
        _now = datetime.now()
        if is_trading_day(_now.date()) and _now.strftime('%H:%M') >= _cfg.SCHEDULE_BACKTEST_TIME:
            # 计算最近一个交易日（与 backtest_yesterday_predictions 的逻辑一致）
            _today = _now.date()
            if _today.weekday() == 0:      # 周一 → 上周五
                _latest_td = _today - timedelta(days=3)
            elif _today.weekday() == 6:    # 周日 → 上周五
                _latest_td = _today - timedelta(days=2)
            elif _today.weekday() == 5:    # 周六 → 上周五
                _latest_td = _today - timedelta(days=1)
            else:
                _latest_td = _today - timedelta(days=1)
            _latest_str = _latest_td.isoformat()
            from db.database import get_db_conn
            _conn = get_db_conn()
            _row = _conn.execute(
                'SELECT id FROM strategy_backtest_reports WHERE backtest_date = ?', (_latest_str,)
            ).fetchone()
            _conn.close()
            if not _row:
                print(f'[初始化] 最近交易日({_latest_str})回测报告缺失，后台补跑中...')
                from services.strategy.strategy import backtest_yesterday_predictions
                async def _startup_backtest():
                    try:
                        _r = await backtest_yesterday_predictions()
                        if 'error' in _r:
                            print(f'[初始化] 回测补跑失败: {_r["error"]}')
                        else:
                            print(f'[初始化] 回测补跑完成: 准确率{_r.get("accuracy", 0)}%')
                    except Exception as _e:
                        print(f'[初始化] 回测补跑失败: {_e}')
                asyncio.create_task(_startup_backtest())
    except Exception as error:
        print(f'[初始化] 回测补跑检查失败: {str(error)}')
    # -fix: 全局改用 requests + asyncio.to_thread，不再需要 httpx 连接池
    scheduler_thread = threading.Thread(target=lambda: _lazy_start_scheduler(), daemon=True)
    scheduler_thread.start()
    yield

app = FastAPI(title="A股行情API", description="东方财富热搜榜、股票行情、K线图、趋势发现", lifespan=lifespan)
# GZip 压缩（2026-10-23）：实测服务端此前零压缩——带 Accept-Encoding: gzip 请求
# index js 仍原样返回 102KB、content-encoding=None。本项目最大响应体
# /api/daily_recommendations 107.8KB、/api/recommend-sim 68KB、静态产物 1.2MB，
# JSON/JS 压缩比通常 5~10×。minimum_size=500：健康检查(51B)、tab-scan-config(0.3KB)
# 这类小响应不压，省 CPU。注意：手机走 cloudflared 隧道时 CF 边缘也会压一遍，
# 本中间件主要收益在本地/局域网直连与回源段，不是重复劳动。
app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"], expose_headers=["X-Quote-Source", "X-Process-Time"])

# 注册路由
from app.api.routes import router
app.include_router(router)

@app.get("/eastmoney/{path:path}", tags=["东方财富代理"])
async def eastmoney_proxy(path: str, request: Request):
    """将 /eastmoney/* 请求代理到 https://push2delay.eastmoney.com/*（使用 requests 同步库，避免 httpx 断连）

    2026-10-19：接入共享熔断 eastmoney_guard——这是前端唯一能任意路径直连东财的通道
    （复盘页 4 路并发 clist 等），冷却中直接 503，不再代理 3 次重试硬打
    （东财 clist IP 级风控实锤：10-17/10-19 两次封禁均封数小时，频率是主因）。
    前端对非 2xx 均为 catch(()=>null) 静默降级，行为兼容。
    """
    import requests as _req
    import asyncio, time as _time
    try:
        from services.data import eastmoney_guard
        if eastmoney_guard.skip('clist'):
            raise HTTPException(status_code=503, detail='eastmoney clist cooling')
    except HTTPException:
        raise
    except Exception:
        pass
    query_string = str(request.url.query)
    target_url = f"https://push2delay.eastmoney.com/{path}"
    if query_string:
        target_url += f"?{query_string}"
    _EM_PROXY_HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': '*/*',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'Referer': 'https://quote.eastmoney.com/',
        'Origin': 'https://quote.eastmoney.com',
    }

    def _do_req():
        last_err = None
        for attempt in range(3):
            try:
                return _req.get(target_url, headers=_EM_PROXY_HEADERS, timeout=10)
            except Exception as e:
                last_err = e
                if attempt < 2:
                    _time.sleep(0.5 * (attempt + 1))
        raise last_err

    try:
        resp = await asyncio.to_thread(_do_req)
        try:
            from services.data import eastmoney_guard
            eastmoney_guard.ok('clist')
        except Exception:
            pass
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers={"Content-Type": resp.headers.get("Content-Type", "application/json")}
        )
    except Exception as e:
        try:
            from services.data import eastmoney_guard
            eastmoney_guard.fail('clist')
        except Exception:
            pass
        raise HTTPException(status_code=502, detail=f"代理请求失败: {str(e)}")


@app.middleware("http")
async def _config_reload_middleware(request, call_next):
    _reload_config()
    # 2026-10-30：把服务端处理耗时写进响应头，前端「本次加载」条用它把
    # "接口本身慢" 与 "传输/隧道排队慢" 分开。只加头，不改任何响应体。
    _t0 = time.perf_counter()
    response = await call_next(request)
    response.headers['X-Process-Time'] = f'{(time.perf_counter() - _t0) * 1000:.0f}'
    return response
