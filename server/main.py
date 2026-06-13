from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from typing import List, Optional, Dict, Any
import httpx
import json
import os
import asyncio
from datetime import datetime, date, timedelta
import uvicorn
import schedule
import threading
import time
import re
import pymysql
from pymysql.cursors import DictCursor
from dbutils.pooled_db import PooledDB
import config as _cfg
import importlib
from config import (
    MYSQL_CONFIG as _MYSQL_CONFIG,
    MYSQL_POOL_MAX_CONNECTIONS, MYSQL_POOL_MIN_CACHED,
    TENCENT_QUOTE_API, TENCENT_KLINE_API, EASTMONEY_HOT_LIST_API,
    AI_CONFIG_FILE,
    HTTP_TIMEOUT, HTTP_CONNECT_TIMEOUT, HTTP_MAX_CONNECTIONS, HTTP_KEEPALIVE_CONNECTIONS,
    HOT_SEARCH_TOP_N, HOT_SEARCH_SNAPSHOT_CLEANUP_DAYS, 
    SCAN_CONCURRENCY, SCAN_KLINE_TIMEOUT, SCAN_CACHE_TTL,
    TREND_MIN_SCORE, TREND_MIN_KLINE_DAYS, TREND_LIMIT_UP_THRESHOLD,
    TREND_LIMIT_UP_120_MIN, TREND_LIMIT_UP_250_MIN, TREND_MAX_DRAWDOWN,
    TREND_CONSECUTIVE_UP_BONUS, TREND_CONSECUTIVE_UP_MAX_BONUS, TREND_IS_UP_MIN_SCORE,
    TREND_MA120_SLOPE_WINDOW, TREND_CONSECUTIVE_UP_MAX_DAYS, USE_INTRADAY_BREAK_CHECK,
    TREND_SCORE_WEIGHTS,
    STRATEGY_BULL_THRESHOLD, STRATEGY_BEAR_THRESHOLD, STRATEGY_PREDICT_BULL_MIN_SCORE,
    STRATEGY_BACKTEST_PREDICTION_LIMIT, STRATEGY_BACKTEST_HISTORY_DAYS,
    STRATEGY_SCORE_THRESHOLDS, STRATEGY_SCORE_VALUES, STRATEGY_WEBI_VERIFY, STRATEGY_PE_FILTER,
    STRATEGY_LABEL_THRESHOLDS,
    DEFAULT_WEIGHTS,
    MARKET_CRASH_THRESHOLD as _MARKET_CRASH_THRESHOLD, MARKET_SEVERE_CRASH_THRESHOLD,
    DELISTING_KEYWORDS, FORBIDDEN_KEYWORDS, HOT_STOCK_POOL,
    SCHEDULE_UPDATE_HOT_SEARCH_TIMES, SCHEDULE_MA10_CHECK_TIME,
    SCHEDULE_DAILY_REVIEW_TIME, SCHEDULE_BACKTEST_TIME,
    SCHEDULE_RECOMMENDATION_TIMES,
    QUERY_DAILY_RECOMMENDATIONS_LIMIT, QUERY_TREND_RESULTS_LIMIT,
    QUERY_MARKET_REVIEWS_LIMIT,
    REVIEW_TOP_GAINERS_COUNT, REVIEW_TOP_LOSERS_COUNT, REVIEW_TOP_SECTORS_COUNT,
    REVIEW_TOMORROW_FOCUS_COUNT,
    INDEX_SYMBOLS,
    KLINE_START_DATE, KLINE_DATA_LIMIT, KLINE_DISPLAY_MA_POINTS,
)

_cfg_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.py')
_cfg_mtime = os.path.getmtime(_cfg_file)

_CONFIG_ALIAS_MAP = {
    '_MYSQL_CONFIG': 'MYSQL_CONFIG',
    '_MARKET_CRASH_THRESHOLD': 'MARKET_CRASH_THRESHOLD',
}

_CONFIG_SAME_NAMES = [
    'MYSQL_POOL_MAX_CONNECTIONS', 'MYSQL_POOL_MIN_CACHED',
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
        g['MYSQL_CONFIG'] = g['_MYSQL_CONFIG']
        g['TENCENT_API'] = g['TENCENT_QUOTE_API']
        print('[Config] 热加载完成 (config.py 已变更)')
        return True
    except Exception as e:
        print(f'[Config] 热加载失败: {e}')
        return False

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动事件
    init_db()
    load_ai_config()
    
    print('[初始化] 正在获取热搜榜数据...')
    try:
        data = await fetch_eastmoney_hot_list()
        save_hot_search_ranking(data)
        print('[初始化] 热搜榜数据获取完成')
    except Exception as error:
        print(f'[初始化] 获取热搜榜失败: {str(error)}')
    
    global _last_scan_result, _last_scan_date, _last_scan_ts
    try:
        cached = load_trend_scan_results()
        if cached and cached.get('stocks'):
            _last_scan_result = cached
            _last_scan_date = cached.get('date', '')
            _last_scan_ts = time.time()
            print(f'[初始化] 预加载趋势数据完成: {_last_scan_date}, {len(cached.get("stocks", []))}只股票')
            
            # 检查是否是今天的数据，不是的话后台扫描
            today = date.today().isoformat()
            if _last_scan_date != today:
                print(f'[初始化] 趋势数据过期（{_last_scan_date}），后台开始扫描...')
                asyncio.create_task(scan_trend_task(force=True))
    except Exception as error:
        print(f'[初始化] 预加载趋势数据失败: {str(error)}')
    
    get_http_client()
    print('[初始化] HTTP连接池已创建')
    
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    
    yield
    
    # 关闭事件
    global http_client
    if http_client and not http_client.is_closed:
        await http_client.aclose()
        print('[关闭] HTTP连接池已关闭')

app = FastAPI(title="A股行情API", description="东方财富热搜榜、股票行情、K线图、趋势发现", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def _config_reload_middleware(request, call_next):
    _reload_config()
    response = await call_next(request)
    return response

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.join(BASE_DIR, '..')
CLIENT_DIR = os.path.join(ROOT_DIR, 'client')

TENCENT_API = TENCENT_QUOTE_API
REQUEST_TIMEOUT = 8.0
MAX_RETRIES = 2

# ========== DeepSeek AI 配置 ==========
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DEEPSEEK_BASE_URL = os.environ.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com')
DEEPSEEK_MODEL = os.environ.get('DEEPSEEK_MODEL', 'DeepSeek-V4-Flash')
AI_CONFIG_MTIME = 0.0

def load_ai_config():
    global DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, AI_CONFIG_MTIME
    try:
        if not os.path.exists(AI_CONFIG_FILE):
            return
        mtime = os.path.getmtime(AI_CONFIG_FILE)
        if mtime == AI_CONFIG_MTIME:
            return
        AI_CONFIG_MTIME = mtime
        with open(AI_CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
            key = (config.get('apiKey') or '').strip()
            url = (config.get('baseUrl') or '').strip()
            model = (config.get('model') or '').strip()
            if key and not key.startswith('在'):
                DEEPSEEK_API_KEY = key
            if url:
                DEEPSEEK_BASE_URL = url
            if model:
                DEEPSEEK_MODEL = model
        if DEEPSEEK_API_KEY:
            print(f'[AI配置] 已加载，模型: {DEEPSEEK_MODEL}')
        else:
            print(f'[AI配置] 未配置API Key，请编辑 server/ai_config.json')
    except Exception as e:
        print(f'[AI配置] 加载失败: {str(e)}')

# ========== MySQL 配置 ==========
MYSQL_CONFIG = _MYSQL_CONFIG

_mysql_pool: Optional[PooledDB] = None

def get_mysql_pool() -> PooledDB:
    global _mysql_pool
    if _mysql_pool is None:
        _mysql_pool = PooledDB(
            creator=pymysql,
            maxconnections=MYSQL_POOL_MAX_CONNECTIONS,
            mincached=MYSQL_POOL_MIN_CACHED,
            **MYSQL_CONFIG
        )
    return _mysql_pool

def get_db_conn():
    return get_mysql_pool().connection()

def init_db():
    """初始化数据库表"""
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        # 收评表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_market_reviews (
                id INT AUTO_INCREMENT PRIMARY KEY,
                review_date VARCHAR(10) NOT NULL UNIQUE,
                summary TEXT,
                market_json JSON,
                trend_json JSON,
                pool_json JSON,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_date (review_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        ''')
        # 趋势股票表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trend_scan_results (
                id INT AUTO_INCREMENT PRIMARY KEY,
                scan_date VARCHAR(10) NOT NULL,
                symbol VARCHAR(20) NOT NULL,
                name VARCHAR(50) NOT NULL,
                score INT DEFAULT 0,
                details_json JSON,
                latest_price DECIMAL(12,2) DEFAULT 0,
                ma5_json JSON,
                ma10_json JSON,
                ma20_json JSON,
                recent5_json JSON,
                total_scanned INT DEFAULT 0,
                source VARCHAR(50) DEFAULT '',
                scan_time VARCHAR(20) DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_date (scan_date),
                INDEX idx_symbol (symbol),
                UNIQUE KEY uk_date_symbol (scan_date, symbol)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        ''')
        # 热门股票表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS hot_stock_buttons (
                id INT AUTO_INCREMENT PRIMARY KEY,
                code VARCHAR(20) NOT NULL UNIQUE,
                name VARCHAR(50) NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_code (code)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        ''')
        # 股票名称搜索映射表（名称 -> 代码，含简称，一般不删除）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_alias_map (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(50) NOT NULL UNIQUE,
                symbol VARCHAR(20) NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_name (name),
                INDEX idx_symbol (symbol)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        ''')
        # 策略预测记录表（每次扫描时记录判定结果）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS strategy_predict_records (
                id INT AUTO_INCREMENT PRIMARY KEY,
                predict_date VARCHAR(10) NOT NULL,
                symbol VARCHAR(20) NOT NULL,
                name VARCHAR(50) NOT NULL,
                score INT DEFAULT 0,
                label VARCHAR(20) DEFAULT '',
                predict_direction VARCHAR(10) DEFAULT '',
                outer_ratio DECIMAL(5,2) DEFAULT 0,
                volume_ratio DECIMAL(8,2) DEFAULT 0,
                turnover_rate DECIMAL(8,2) DEFAULT 0,
                change_percent DECIMAL(8,2) DEFAULT 0,
                weibi DECIMAL(8,2) DEFAULT 0,
                avg_price_deviation DECIMAL(8,2) DEFAULT 0,
                amplitude DECIMAL(8,2) DEFAULT 0,
                market_change DECIMAL(8,2) DEFAULT 0,
                verified TINYINT DEFAULT 0,
                actual_change DECIMAL(8,2) DEFAULT NULL,
                actual_direction VARCHAR(10) DEFAULT '',
                is_correct TINYINT DEFAULT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_date (predict_date),
                INDEX idx_symbol (symbol),
                UNIQUE KEY uk_date_symbol (predict_date, symbol)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        ''')
        # 策略权重表（动态调整的评分权重）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS strategy_factor_weights (
                id INT AUTO_INCREMENT PRIMARY KEY,
                factor_name VARCHAR(30) NOT NULL UNIQUE,
                weight DECIMAL(8,4) DEFAULT 1.0,
                accuracy DECIMAL(5,2) DEFAULT 50.0,
                sample_count INT DEFAULT 0,
                correct_count INT DEFAULT 0,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_factor (factor_name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        ''')
        # 策略回测报告表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS strategy_backtest_reports (
                id INT AUTO_INCREMENT PRIMARY KEY,
                backtest_date VARCHAR(10) NOT NULL UNIQUE,
                market_change DECIMAL(8,2) DEFAULT 0,
                total_predictions INT DEFAULT 0,
                correct_count INT DEFAULT 0,
                accuracy DECIMAL(5,2) DEFAULT 0,
                bull_accuracy DECIMAL(5,2) DEFAULT 0,
                bear_accuracy DECIMAL(5,2) DEFAULT 0,
                bull_misjudge_count INT DEFAULT 0,
                bear_misjudge_count INT DEFAULT 0,
                misjudge_analysis TEXT,
                weight_adjustments TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        ''')
        # 热搜榜表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS hot_search_ranking (
                id INT AUTO_INCREMENT PRIMARY KEY,
                symbol VARCHAR(20) NOT NULL,
                code VARCHAR(10) DEFAULT '',
                name VARCHAR(50) NOT NULL,
                market VARCHAR(10) DEFAULT '',
                price INT DEFAULT 0,
                change_percent INT DEFAULT 0,
                change_val INT DEFAULT 0,
                hot_rank INT DEFAULT 0,
                batch_time VARCHAR(30) DEFAULT '',
                sort_order INT DEFAULT 0,
                INDEX idx_symbol (symbol)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        ''')
        # 热搜榜元数据表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS hot_search_meta (
                id INT AUTO_INCREMENT PRIMARY KEY,
                meta_key VARCHAR(50) NOT NULL UNIQUE,
                meta_value TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS hot_search_daily_snapshot (
                id INT AUTO_INCREMENT PRIMARY KEY,
                snapshot_date VARCHAR(10) NOT NULL,
                symbol VARCHAR(20) NOT NULL,
                name VARCHAR(50) DEFAULT '',
                hot_rank INT DEFAULT 0,
                UNIQUE KEY uk_date_symbol (snapshot_date, symbol),
                INDEX idx_date (snapshot_date),
                INDEX idx_symbol (symbol)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        ''')
        # 推荐记录表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_recommendations (
                id INT AUTO_INCREMENT PRIMARY KEY,
                rec_date VARCHAR(10) NOT NULL,
                symbol VARCHAR(20) NOT NULL,
                name VARCHAR(50) NOT NULL,
                price VARCHAR(20) DEFAULT '',
                change_val DECIMAL(10,2) DEFAULT 0,
                change_percent DECIMAL(10,2) DEFAULT 0,
                score INT DEFAULT 0,
                buy_price VARCHAR(20) DEFAULT '',
                current_price VARCHAR(20) DEFAULT '',
                sell_price VARCHAR(20) DEFAULT '',
                reason TEXT,
                sort_order INT DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_date (rec_date),
                UNIQUE KEY uk_date_symbol (rec_date, symbol)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        ''')
        # 股票映射表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_basic_info (
                symbol VARCHAR(20) NOT NULL PRIMARY KEY,
                name VARCHAR(50) NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        ''')
        # 自定义扫描池表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_scan_pool (
                id INT AUTO_INCREMENT PRIMARY KEY,
                symbol VARCHAR(20) NOT NULL UNIQUE,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        ''')
        # 股票标签表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_concept_tags (
                id INT AUTO_INCREMENT PRIMARY KEY,
                symbol VARCHAR(20) NOT NULL UNIQUE,
                tags_json JSON,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_symbol (symbol)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        ''')
        try:
            cursor.execute('ALTER TABLE hot_search_ranking ADD COLUMN hot_rank INT DEFAULT 0')
        except Exception:
            pass
        try:
            cursor.execute('ALTER TABLE trend_scan_results DROP COLUMN hot_rank')
        except Exception:
            pass
        conn.commit()
        print('[数据库] 表初始化完成')
    except Exception as e:
        print(f'[数据库] 初始化失败: {e}')
    finally:
        conn.close()

# 大盘指数代码
INDEX_SYMBOLS = INDEX_SYMBOLS

# 大盘大跌阈值（跌幅超过此值触发提醒）
MARKET_CRASH_THRESHOLD = _MARKET_CRASH_THRESHOLD
MARKET_SEVERE_CRASH_THRESHOLD = MARKET_SEVERE_CRASH_THRESHOLD

http_client: Optional[httpx.AsyncClient] = None
SCAN_SEMAPHORE = asyncio.Semaphore(SCAN_CONCURRENCY)

_stock_basic_info_cache: Optional[Dict] = None
_hot_search_ranking_cache: Optional[Dict] = None

def get_http_client() -> httpx.AsyncClient:
    global http_client
    if http_client is None or http_client.is_closed:
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(HTTP_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT),
            limits=httpx.Limits(max_connections=HTTP_MAX_CONNECTIONS, max_keepalive_connections=HTTP_KEEPALIVE_CONNECTIONS),
            http2=False,
            follow_redirects=True
        )
    return http_client

def get_cached_stock_name(symbol: str) -> Optional[str]:
    global _stock_basic_info_cache, _hot_search_ranking_cache
    s = symbol.lower()
    if _stock_basic_info_cache is None:
        _stock_basic_info_cache = load_stock_basic_info()
    if s in _stock_basic_info_cache:
        return _stock_basic_info_cache[s]
    if _hot_search_ranking_cache is not None and _hot_search_ranking_cache.get('stocks'):
        for st in _hot_search_ranking_cache['stocks']:
            if st['symbol'].lower() == s:
                return st['name']
    return None

def is_forbidden_stock(name: str) -> bool:
    return any(keyword in name for keyword in FORBIDDEN_KEYWORDS)

def has_delisting_risk(name: str, symbol: str) -> bool:
    if any(kw in name for kw in DELISTING_KEYWORDS):
        return True
    code = symbol.replace('sh', '').replace('sz', '')
    if code.startswith('4') or code.startswith('8') or code.startswith('9'):
        return True
    return False


def is_main_board(symbol: str) -> bool:
    """
    判断是否为沪深主板股票
    
    主板规则：
    - 沪市主板：sh600xxx, sh601xxx, sh603xxx, sh605xxx
    - 深市主板：sz000xxx, sz001xxx
    """
    symbol = symbol.lower()
    # 沪市主板：sh60开头
    if symbol.startswith('sh60'):
        return True
    # 深市主板：sz000或sz001开头
    if symbol.startswith('sz000') or symbol.startswith('sz001'):
        return True
    # 其他（创业板、科创板、北交所等）→ 不是主板
    return False

def load_daily_recommendations() -> List[Dict]:
    try:
        conn = get_db_conn()
        cursor = conn.cursor(DictCursor)
        cursor.execute(f'SELECT * FROM daily_recommendations ORDER BY rec_date DESC, sort_order ASC LIMIT {QUERY_DAILY_RECOMMENDATIONS_LIMIT}')
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            return []

        # 按日期分组
        result = []
        current_date = None
        current_group = None
        for row in rows:
            if row['rec_date'] != current_date:
                if current_group:
                    result.append(current_group)
                current_date = row['rec_date']
                current_group = {
                    'date': current_date,
                    'daily_recommendations': []
                }
            current_group['daily_recommendations'].append({
                'name': row['name'],
                'symbol': row['symbol'],
                'price': row['price'],
                'change': float(row['change_val']) if row['change_val'] else 0,
                'changePercent': float(row['change_percent']) if row['change_percent'] else 0,
                'score': row['score'] or 0,
                'buySellPoints': {
                    'buy': row['buy_price'] or '',
                    'current': row['current_price'] or '',
                    'sell': row['sell_price'] or ''
                } if row['buy_price'] else None,
                'reason': row['reason'] or ''
            })
        if current_group:
            result.append(current_group)
        return result
    except Exception as error:
        print(f'加载推荐记录失败: {str(error)}')
        return []

def save_daily_recommendations(daily_recommendations: List[Dict]) -> bool:
    try:
        if len(daily_recommendations) > QUERY_DAILY_RECOMMENDATIONS_LIMIT:
            daily_recommendations = daily_recommendations[:QUERY_DAILY_RECOMMENDATIONS_LIMIT]
        conn = get_db_conn()
        cursor = conn.cursor()
        for idx, rec_group in enumerate(daily_recommendations):
            rec_date = rec_group.get('date', '')
            recs = rec_group.get('daily_recommendations', [])
            # 删除该日期旧数据
            cursor.execute('DELETE FROM daily_recommendations WHERE rec_date = %s', (rec_date,))
            for sort_order, rec in enumerate(recs):
                bsp = rec.get('buySellPoints') or {}
                cursor.execute('''
                    INSERT INTO daily_recommendations (rec_date, symbol, name, price, change_val, change_percent, score, buy_price, current_price, sell_price, reason, sort_order)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ''', (
                    rec_date,
                    rec.get('symbol', ''),
                    rec.get('name', ''),
                    str(rec.get('price', '')),
                    rec.get('change', 0),
                    rec.get('changePercent', 0),
                    rec.get('score', 0),
                    bsp.get('buy', ''),
                    bsp.get('current', ''),
                    bsp.get('sell', ''),
                    rec.get('reason', ''),
                    sort_order
                ))
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存推荐记录失败: {str(error)}')
        return False

def load_trend_scan_results() -> Dict:
    try:
        conn = get_db_conn()
        cursor = conn.cursor(DictCursor)
        cursor.execute(f'SELECT * FROM trend_scan_results ORDER BY scan_date DESC, score DESC LIMIT {QUERY_TREND_RESULTS_LIMIT}')
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            return {'date': '', 'stocks': []}

        first_row = rows[0]
        stocks = []
        for row in rows:
            if row['scan_date'] != first_row['scan_date']:
                continue
            stocks.append({
                'symbol': row['symbol'],
                'name': row['name'],
                'score': row['score'],
                'details': json.loads(row['details_json']) if row['details_json'] else {},
                'latestPrice': float(row['latest_price']) if row['latest_price'] else 0,
                'ma5': json.loads(row['ma5_json']) if row['ma5_json'] else [],
                'ma10': json.loads(row['ma10_json']) if row['ma10_json'] else [],
                'ma20': json.loads(row['ma20_json']) if row['ma20_json'] else [],
                'recent5Days': json.loads(row['recent5_json']) if row['recent5_json'] else [],
            })

        return {
            'date': first_row['scan_date'],
            'totalScanned': first_row['total_scanned'] or 0,
            'found': len(stocks),
            'stocks': stocks,
            'scanTime': first_row['scan_time'] or '',
            'source': first_row['source'] or ''
        }
    except Exception as error:
        print(f'加载趋势股票失败: {str(error)}')
        return {'date': '', 'stocks': []}

def save_trend_scan_results(data: Dict) -> bool:
    try:
        scan_date = data.get('date', date.today().isoformat())
        stocks = data.get('stocks', [])
        total_scanned = data.get('totalScanned', 0)
        source = data.get('source', '')
        scan_time = data.get('scanTime', '')

        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM trend_scan_results WHERE scan_date = %s', (scan_date,))
        for stock in stocks:
            cursor.execute('''
                INSERT INTO trend_scan_results (scan_date, symbol, name, score, details_json, latest_price, ma5_json, ma10_json, ma20_json, recent5_json, total_scanned, source, scan_time)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ''', (
                scan_date,
                stock.get('symbol', ''),
                stock.get('name', ''),
                stock.get('score', 0),
                json.dumps(stock.get('details', {}), ensure_ascii=False),
                stock.get('latestPrice', 0),
                json.dumps(stock.get('ma5', []), ensure_ascii=False),
                json.dumps(stock.get('ma10', []), ensure_ascii=False),
                json.dumps(stock.get('ma20', []), ensure_ascii=False),
                json.dumps(stock.get('recent5Days', []), ensure_ascii=False),
                total_scanned,
                source,
                scan_time
            ))
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存趋势股票失败: {str(error)}')
        return False

def load_stock_basic_info() -> Dict:
    try:
        conn = get_db_conn()
        cursor = conn.cursor(DictCursor)
        cursor.execute('SELECT symbol, name FROM stock_basic_info')
        rows = cursor.fetchall()
        conn.close()
        return {row['symbol']: row['name'] for row in rows}
    except Exception as error:
        print(f'加载股票映射失败: {str(error)}')
        return {}

def save_stock_basic_info(stock_basic_info: Dict) -> bool:
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM stock_basic_info')
        for symbol, name in stock_basic_info.items():
            cursor.execute(
                'INSERT INTO stock_basic_info (symbol, name) VALUES (%s, %s)',
                (symbol, name)
            )
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存股票映射失败: {str(error)}')
        return False

def load_stock_alias_map() -> Dict:
    """加载名称搜索映射（名称 -> 代码）"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor(DictCursor)
        cursor.execute('SELECT name, symbol FROM stock_alias_map')
        rows = cursor.fetchall()
        conn.close()
        return {row['name']: row['symbol'] for row in rows}
    except Exception as error:
        print(f'加载名称搜索映射失败: {str(error)}')
        return {}

# ========== 自适应选股策略系统 ==========

# 因子说明
FACTOR_DESCRIPTIONS = {
    'outer_ratio': '外盘占比',
    'volume_ratio': '量比',
    'turnover_rate': '换手率',
    'change_percent': '涨跌幅',
    'weibi': '委比',
    'avg_price_deviation': '均价偏离',
    'amplitude': '振幅',
    'position': '位置评估',
}

def load_strategy_factor_weights() -> Dict:
    """加载动态权重，如果数据库无数据则返回默认权重"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor(DictCursor)
        cursor.execute('SELECT factor_name, weight, accuracy, sample_count, correct_count FROM strategy_factor_weights')
        rows = cursor.fetchall()
        conn.close()
        if not rows:
            return {k: {'weight': v, 'accuracy': 50.0, 'sample_count': 0, 'correct_count': 0}
                    for k, v in DEFAULT_WEIGHTS.items()}
        result = {}
        for row in rows:
            result[row['factor_name']] = {
                'weight': float(row['weight']),
                'accuracy': float(row['accuracy']),
                'sample_count': int(row['sample_count']),
                'correct_count': int(row['correct_count']),
            }
        # 补充数据库中可能缺少的因子
        for k, v in DEFAULT_WEIGHTS.items():
            if k not in result:
                result[k] = {'weight': v, 'accuracy': 50.0, 'sample_count': 0, 'correct_count': 0}
        return result
    except Exception as error:
        print(f'加载策略权重失败: {str(error)}')
        return {k: {'weight': v, 'accuracy': 50.0, 'sample_count': 0, 'correct_count': 0}
                for k, v in DEFAULT_WEIGHTS.items()}

def save_strategy_factor_weights(weights_data: Dict) -> bool:
    """保存动态权重"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        for factor_name, data in weights_data.items():
            weight = data.get('weight', 1.0)
            accuracy = data.get('accuracy', 50.0)
            sample_count = data.get('sample_count', 0)
            correct_count = data.get('correct_count', 0)
            cursor.execute(
                '''INSERT INTO strategy_factor_weights (factor_name, weight, accuracy, sample_count, correct_count)
                   VALUES (%s, %s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE weight=%s, accuracy=%s, sample_count=%s, correct_count=%s''',
                (factor_name, weight, accuracy, sample_count, correct_count,
                 weight, accuracy, sample_count, correct_count)
            )
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存策略权重失败: {str(error)}')
        return False

def save_predictions(predictions: List[Dict]) -> bool:
    """保存扫描预测记录"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        for p in predictions:
            cursor.execute(
                '''INSERT INTO strategy_predict_records
                   (predict_date, symbol, name, score, label, predict_direction,
                    outer_ratio, volume_ratio, turnover_rate, change_percent,
                    weibi, avg_price_deviation, amplitude, market_change)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE
                   score=VALUES(score), label=VALUES(label), predict_direction=VALUES(predict_direction),
                   outer_ratio=VALUES(outer_ratio), volume_ratio=VALUES(volume_ratio),
                   turnover_rate=VALUES(turnover_rate), change_percent=VALUES(change_percent),
                   weibi=VALUES(weibi), avg_price_deviation=VALUES(avg_price_deviation),
                   amplitude=VALUES(amplitude), market_change=VALUES(market_change)''',
                (p['predict_date'], p['symbol'], p['name'], p['score'], p['label'],
                 p['predict_direction'], p.get('outer_ratio', 0), p.get('volume_ratio', 0),
                 p.get('turnover_rate', 0), p.get('change_percent', 0),
                 p.get('weibi', 0), p.get('avg_price_deviation', 0),
                 p.get('amplitude', 0), p.get('market_change', 0))
            )
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存预测记录失败: {str(error)}')
        return False

def calc_stock_score_v2(data: Dict, weights: Dict = None, market_change: float = 0.0) -> Dict:
    """使用动态权重的评分函数 V2
    
    Args:
        data: 股票特征数据
        weights: 策略因子权重
        market_change: 大盘涨跌幅，用于根据市场环境动态调整评分阈值
    """
    if weights is None:
        weights = load_strategy_factor_weights()

    outer_ratio = data.get('outerRatio', 50)
    volume_ratio = data.get('volumeRatio', 0)
    turnover_rate = data.get('turnoverRate', 0)
    change_percent = data.get('changePercent', 0)
    weibi = data.get('weibi', 0)
    avg_price_deviation = data.get('avgPriceDeviation', 0)
    amplitude = data.get('amplitude', 0)
    pe = data.get('pe', 0)

    # 基础得分（与原逻辑一致）
    base_scores = {
        'outer_ratio': STRATEGY_SCORE_VALUES['outer_ratio']['high'] if outer_ratio >= STRATEGY_SCORE_THRESHOLDS['outer_ratio']['high'] else STRATEGY_SCORE_VALUES['outer_ratio']['mid'] if outer_ratio >= STRATEGY_SCORE_THRESHOLDS['outer_ratio']['mid'] else STRATEGY_SCORE_VALUES['outer_ratio']['low'],
        'volume_ratio': STRATEGY_SCORE_VALUES['volume_ratio']['high'] if volume_ratio >= STRATEGY_SCORE_THRESHOLDS['volume_ratio']['high'] else STRATEGY_SCORE_VALUES['volume_ratio']['mid'] if volume_ratio >= STRATEGY_SCORE_THRESHOLDS['volume_ratio']['mid'] else STRATEGY_SCORE_VALUES['volume_ratio']['normal'] if volume_ratio >= STRATEGY_SCORE_THRESHOLDS['volume_ratio']['low'] else STRATEGY_SCORE_VALUES['volume_ratio']['low'],
        'turnover_rate': STRATEGY_SCORE_VALUES['turnover_rate']['high'] if (STRATEGY_SCORE_THRESHOLDS['turnover_rate']['high'][0] <= turnover_rate <= STRATEGY_SCORE_THRESHOLDS['turnover_rate']['high'][1]) else STRATEGY_SCORE_VALUES['turnover_rate']['mid'] if (STRATEGY_SCORE_THRESHOLDS['turnover_rate']['mid'][0] <= turnover_rate < STRATEGY_SCORE_THRESHOLDS['turnover_rate']['mid'][1]) else STRATEGY_SCORE_VALUES['turnover_rate']['normal'] if turnover_rate > STRATEGY_SCORE_THRESHOLDS['turnover_rate']['high_penalty'] else STRATEGY_SCORE_VALUES['turnover_rate']['low'],
        'change_percent': STRATEGY_SCORE_VALUES['change_percent']['high'] if change_percent > STRATEGY_SCORE_THRESHOLDS['change_percent']['high'] else STRATEGY_SCORE_VALUES['change_percent']['mid'] if change_percent > STRATEGY_SCORE_THRESHOLDS['change_percent']['mid'] else STRATEGY_SCORE_VALUES['change_percent']['normal'] if change_percent > STRATEGY_SCORE_THRESHOLDS['change_percent']['low'] else STRATEGY_SCORE_VALUES['change_percent']['negative'] if change_percent > STRATEGY_SCORE_THRESHOLDS['change_percent']['negative'] else 0,
        'weibi': STRATEGY_SCORE_VALUES['weibi']['high'] if weibi > STRATEGY_SCORE_THRESHOLDS['weibi']['high'] else STRATEGY_SCORE_VALUES['weibi']['mid'] if weibi > STRATEGY_SCORE_THRESHOLDS['weibi']['mid'] else STRATEGY_SCORE_VALUES['weibi']['low'] if weibi > STRATEGY_SCORE_THRESHOLDS['weibi']['low'] else STRATEGY_SCORE_VALUES['weibi']['negative'],
        'avg_price_deviation': STRATEGY_SCORE_VALUES['avg_price_deviation']['high'] if avg_price_deviation > STRATEGY_SCORE_THRESHOLDS['avg_price_deviation']['high'] else STRATEGY_SCORE_VALUES['avg_price_deviation']['mid'] if avg_price_deviation > STRATEGY_SCORE_THRESHOLDS['avg_price_deviation']['mid'] else STRATEGY_SCORE_VALUES['avg_price_deviation']['normal'] if avg_price_deviation > STRATEGY_SCORE_THRESHOLDS['avg_price_deviation']['low'] else STRATEGY_SCORE_VALUES['avg_price_deviation']['low'],
        'amplitude': STRATEGY_SCORE_VALUES['amplitude']['high'] if (STRATEGY_SCORE_THRESHOLDS['amplitude']['high'][0] < amplitude <= STRATEGY_SCORE_THRESHOLDS['amplitude']['high'][1]) else STRATEGY_SCORE_VALUES['amplitude']['mid'] if (STRATEGY_SCORE_THRESHOLDS['amplitude']['mid'][0] < amplitude <= STRATEGY_SCORE_THRESHOLDS['amplitude']['mid'][1]) else STRATEGY_SCORE_VALUES['amplitude']['low'],
    }
    if weibi > STRATEGY_WEBI_VERIFY['weibi_high'] and change_percent < 0:
        base_scores['weibi'] = 3
    elif weibi > STRATEGY_WEBI_VERIFY['weibi_high'] and change_percent < STRATEGY_SCORE_THRESHOLDS['change_percent']['negative']:
        base_scores['weibi'] = 2
    elif weibi > STRATEGY_WEBI_VERIFY['weibi_high'] and outer_ratio < STRATEGY_WEBI_VERIFY['outer_ratio_low']:
        base_scores['weibi'] = 4
    elif weibi > STRATEGY_WEBI_VERIFY['weibi_high'] and volume_ratio < STRATEGY_WEBI_VERIFY['volume_ratio_low']:
        base_scores['weibi'] = 4
        
    # AI回测优化建议规则1: 结合分时图量价关系，当外盘占比低于50%且量比>3时，降低看涨权重 (惩罚分数)
    if outer_ratio < 50 and volume_ratio > 3:
        # 如果是这种典型的诱多出货形态，直接扣除部分外盘和量比的分数
        base_scores['outer_ratio'] = max(0, base_scores['outer_ratio'] - 5)
        base_scores['volume_ratio'] = max(0, base_scores['volume_ratio'] - 5)

    # 2026-06-11 策略优化建议：增加放量出货检测（骗线过滤）
    # 当量比显著（>1.5）且外盘占比并未显著超过内盘（<55%）时，可能存在主力利用对倒放量吸引关注后悄悄出货。
    if volume_ratio > 1.5 and outer_ratio < 55:
        base_scores['volume_ratio'] = max(0, base_scores['volume_ratio'] - 5)
        base_scores['outer_ratio'] = max(0, base_scores['outer_ratio'] - 5)
        
    # 2026-06-11 策略优化建议：不再盲目惩罚涨幅，而是区分“强势启动”与“力竭赶顶”
    # 2026-06-11 策略优化建议：拥抱强势龙头股。涨停不代表风险，往往代表次日的高溢价。
    if change_percent > 9.5:
        # 涨停股评分逻辑：如果委比极高且换手率适中，说明封单坚决，给予高分
        if weibi > 80 and 2 <= turnover_rate <= 15:
            base_scores['change_percent'] = 25 # 龙头股奖励分，鼓励次日竞价关注
        else:
            base_scores['change_percent'] = 15 # 普通封板
    elif change_percent > 7.0:
        # 高位强势，如果不放量滞涨，给予高分奖励
        if volume_ratio < 2.5:
            base_scores['change_percent'] = 20
        else:
            base_scores['change_percent'] = 12
    elif change_percent > 3.0:
        # 强势区间：量价齐升则给满分
        if 1.0 <= volume_ratio <= 2.2 and outer_ratio > 56:
            base_scores['change_percent'] = 20
        else:
            base_scores['change_percent'] = 15
    elif change_percent > 0:
        # 稳健启动区
        base_scores['change_percent'] = 18
    else:
        # 下跌股保持原逻辑
        pass

    # 2026-06-11 策略优化建议：大盘上涨时的优选策略
    # 当大盘上涨（market_change > 0）时，优先选择“外盘占比 > 60% 且 量比 < 1.5”的股票（缩量稳步上涨，非对倒放量）。
    if market_change > 0 and outer_ratio > 60 and volume_ratio < 1.5:
        base_scores['outer_ratio'] += 5
        base_scores['volume_ratio'] += 5

    # 2026-06-11 策略优化建议：虚假封单过滤 (委比 100% 陷阱)
    # 误判样本如旭光电子、昊华科技在委比 100% 时实际下跌，说明封单可能是诱多虚假单。
    if weibi > 99:
        # 如果委比接近 100% 但外盘占比并未同步处于高位（>60%），则该委比可信度极低。
        if outer_ratio < 60:
            base_scores['weibi'] = 0
            base_scores['outer_ratio'] = max(0, base_scores['outer_ratio'] - 5)

    cp = abs(change_percent)
    if cp > STRATEGY_PE_FILTER['pe_high'] or (pe > 0 and pe > STRATEGY_PE_FILTER['pe_max']):
        base_scores['position'] = 5
    elif cp < 1 and pe > 0 and pe < 50:
        base_scores['position'] = 15
    else:
        base_scores['position'] = 10

    # 应用动态权重
    total = 0
    for factor, base_score in base_scores.items():
        w = weights.get(factor, {}).get('weight', 1.0)
        total += base_score * w

    # 归一化到100分（8个因子满分总和为95，乘以平均权重后需要归一化）
    max_possible = sum([
        15, 10, 10, 20, 10, 15, 5, 15  # 各因子满分
    ]) * sum(weights.get(f, {}).get('weight', 1.0) for f in base_scores) / len(base_scores)
    if max_possible > 0:
        total = round(total / max_possible * 100)
    total = min(100, max(0, total))

    label = ''
    
    # AI回测优化建议规则2: 考虑大盘环境因素，当大盘下跌时降低策略的看涨阈值或增加惩罚
    bull_threshold = STRATEGY_BULL_THRESHOLD
    strong_bull_threshold = STRATEGY_LABEL_THRESHOLDS['strong_bull']
    label_bull_threshold = STRATEGY_LABEL_THRESHOLDS['bull']
    
    if market_change < 0:
        # 大盘下跌时，提高看涨门槛（更难被判定为看涨）
        penalty = min(10, abs(int(market_change * 5))) # 跌幅越大，门槛提高越多，最多提高10分
        bull_threshold += penalty
        strong_bull_threshold += penalty
        label_bull_threshold += penalty

    if total >= strong_bull_threshold:
        label = '强烈看涨'
    elif total >= label_bull_threshold:
        label = '偏多看涨'
    elif total >= 45:
        label = '震荡观望'
    elif total >= 30:
        label = '偏空看跌'
    else:
        label = '强烈看跌'

    predict_direction = 'bull' if total >= bull_threshold else 'bear' if total < STRATEGY_BEAR_THRESHOLD else 'neutral'

    return {
        'total': total,
        'label': label,
        'predict_direction': predict_direction,
        'base_scores': base_scores,
        'factor_values': {
            'outer_ratio': outer_ratio,
            'volume_ratio': volume_ratio,
            'turnover_rate': turnover_rate,
            'change_percent': change_percent,
            'weibi': weibi,
            'avg_price_deviation': avg_price_deviation,
            'amplitude': amplitude,
        }
    }

async def backtest_yesterday_predictions() -> Dict:
    """回溯验证昨日预测，计算准确率，调整权重"""
    try:
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        # 如果今天是周一，则回测周五
        if date.today().weekday() == 0:  # Monday
            yesterday = (date.today() - timedelta(days=3)).isoformat()

        conn = get_db_conn()
        cursor = conn.cursor(DictCursor)

        # 获取昨日未验证的预测
        cursor.execute(
            'SELECT * FROM strategy_predict_records WHERE predict_date = %s AND verified = 0',
            (yesterday,)
        )
        predictions = cursor.fetchall()

        if not predictions:
            # 没有未验证的预测，检查是否已有回测报告
            cursor.execute(
                'SELECT id FROM strategy_backtest_reports WHERE backtest_date = %s',
                (yesterday,)
            )
            existing = cursor.fetchone()
            if existing:
                conn.close()
                return {'date': yesterday, 'message': '回测报告已存在'}
            
            # 没有回测报告但预测已验证，从已验证预测生成报告
            cursor.execute(
                'SELECT * FROM strategy_predict_records WHERE predict_date = %s AND verified = 1',
                (yesterday,)
            )
            predictions = cursor.fetchall()
            if not predictions:
                conn.close()
                return {'date': yesterday, 'message': '无待验证的预测记录'}

        # 获取昨日大盘涨跌
        market_change = 0.0
        try:
            idx_data = await fetch_stock_data('sh000001')
            if idx_data:
                market_change = idx_data.get('parsed', {}).get('changePercent', 0)
        except Exception:
            pass

        # 逐个验证
        correct_count = 0
        total_count = len(predictions)
        bull_correct = 0
        bull_total = 0
        bull_misjudge = []

        for pred in predictions:
            symbol = pred['symbol']
            predict_dir = pred['predict_direction']
            
            if pred.get('verified') and pred.get('actual_change') is not None:
                actual_change = float(pred['actual_change'])
                actual_dir = pred.get('actual_direction', ('bull' if actual_change > 0 else 'bear' if actual_change < 0 else 'neutral'))
                is_correct = bool(pred.get('is_correct'))
            else:
                try:
                    stock_data = await fetch_stock_data(symbol)
                    if not stock_data:
                        continue
                    actual_change = stock_data.get('parsed', {}).get('changePercent', 0)
                except Exception:
                    continue

                actual_dir = 'bull' if actual_change > 0 else 'bear' if actual_change < 0 else 'neutral'
                is_correct = predict_dir == actual_dir

                cursor.execute(
                    '''UPDATE strategy_predict_records
                       SET verified=1, actual_change=%s, actual_direction=%s, is_correct=%s
                       WHERE predict_date=%s AND symbol=%s''',
                    (actual_change, actual_dir, 1 if is_correct else 0, yesterday, symbol)
                )

            if is_correct:
                correct_count += 1

            bull_total += 1
            if is_correct:
                bull_correct += 1
            else:
                bull_misjudge.append({
                    'symbol': symbol, 'name': pred['name'],
                    'score': pred['score'], 'predict_change': float(pred['change_percent']),
                    'actual_change': actual_change,
                    'outer_ratio': float(pred['outer_ratio']),
                    'volume_ratio': float(pred['volume_ratio']),
                    'weibi': float(pred['weibi']),
                })

        conn.commit()

        # 计算各因子准确率并调整权重
        weights = load_strategy_factor_weights()
        weight_adjustments = _adjust_weights(predictions, weights, cursor)

        # 保存更新后的权重
        save_strategy_factor_weights(weights)

        # 生成误判分析
        misjudge_analysis = await _analyze_misjudgments(bull_misjudge, [], market_change, weights)

        # 计算准确率
        accuracy = round(correct_count / total_count * 100, 2) if total_count > 0 else 0
        bull_accuracy = round(bull_correct / bull_total * 100, 2) if bull_total > 0 else 0

        # 保存回测报告
        import json
        cursor.execute(
            '''INSERT INTO strategy_backtest_reports
               (backtest_date, market_change, total_predictions, correct_count, accuracy,
                bull_accuracy, bear_accuracy, bull_misjudge_count, bear_misjudge_count,
                misjudge_analysis, weight_adjustments)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON DUPLICATE KEY UPDATE
               market_change=VALUES(market_change), total_predictions=VALUES(total_predictions),
               correct_count=VALUES(correct_count), accuracy=VALUES(accuracy),
               bull_accuracy=VALUES(bull_accuracy), bear_accuracy=VALUES(bear_accuracy),
               bull_misjudge_count=VALUES(bull_misjudge_count),
               bear_misjudge_count=VALUES(bear_misjudge_count),
               misjudge_analysis=VALUES(misjudge_analysis),
               weight_adjustments=VALUES(weight_adjustments)''',
            (yesterday, market_change, total_count, correct_count, accuracy,
             bull_accuracy, 0, len(bull_misjudge), 0,
             json.dumps(misjudge_analysis, ensure_ascii=False),
             json.dumps(weight_adjustments, ensure_ascii=False))
        )
        conn.commit()
        conn.close()

        return {
            'date': yesterday,
            'market_change': market_change,
            'total': total_count,
            'correct': correct_count,
            'accuracy': accuracy,
            'bull_accuracy': bull_accuracy,
            'bull_misjudge': bull_misjudge,
            'misjudge_analysis': misjudge_analysis,
            'weight_adjustments': weight_adjustments,
        }
    except Exception as error:
        print(f'回测验证失败: {str(error)}')
        import traceback
        traceback.print_exc()
        return {'error': str(error)}

def _adjust_weights(predictions: List[Dict], weights: Dict, cursor) -> List[Dict]:
    """根据预测准确性调整各因子权重"""
    adjustments = []
    
    db_columns = {'outer_ratio', 'volume_ratio', 'turnover_rate', 'change_percent',
                  'weibi', 'avg_price_deviation', 'amplitude'}

    for factor_name in DEFAULT_WEIGHTS.keys():
        if factor_name not in db_columns:
            adjustments.append({'factor': factor_name, 'action': 'keep', 'reason': '非数据库因子'})
            continue
        # 统计该因子高分时预测的准确率
        cursor.execute(
            f'''SELECT predict_direction, actual_change, is_correct, %s as factor_val
               FROM strategy_predict_records
               WHERE verified = 1 AND predict_date >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
               ORDER BY predict_date DESC LIMIT {STRATEGY_BACKTEST_PREDICTION_LIMIT}''' % factor_name
        )
        recent = cursor.fetchall()

        if len(recent) < 10:
            adjustments.append({'factor': factor_name, 'action': 'keep', 'reason': '样本不足'})
            continue

        # 计算该因子在预测正确和错误样本中的分布差异
        correct_samples = [r for r in recent if r['is_correct'] == 1]
        wrong_samples = [r for r in recent if r['is_correct'] == 0]

        if not correct_samples or not wrong_samples:
            adjustments.append({'factor': factor_name, 'action': 'keep', 'reason': '无错误样本'})
            continue

        # 因子准确率
        factor_accuracy = len(correct_samples) / len(recent) * 100
        current_weight = weights[factor_name]['weight']
        sample_count = len(recent)
        correct_count = len(correct_samples)

        # 更新权重数据
        weights[factor_name]['accuracy'] = round(factor_accuracy, 2)
        weights[factor_name]['sample_count'] = sample_count
        weights[factor_name]['correct_count'] = correct_count

        # 权重调整逻辑：
        # 准确率 > 60%: 增强权重（最高2.0）
        # 准确率 < 40%: 减弱权重（最低0.3）
        # 40%-60%: 保持不变
        old_weight = current_weight
        if factor_accuracy > 60:
            new_weight = min(2.0, current_weight * 1.05)
            action = 'increase'
            reason = f'准确率{factor_accuracy:.1f}%>60%，权重从{old_weight:.3f}提升到{new_weight:.3f}'
        elif factor_accuracy < 40:
            new_weight = max(0.3, current_weight * 0.95)
            action = 'decrease'
            reason = f'准确率{factor_accuracy:.1f}%<40%，权重从{old_weight:.3f}降低到{new_weight:.3f}'
        else:
            new_weight = current_weight
            action = 'keep'
            reason = f'准确率{factor_accuracy:.1f}%在40%-60%之间，权重保持{old_weight:.3f}'

        weights[factor_name]['weight'] = round(new_weight, 4)
        adjustments.append({'factor': factor_name, 'action': action, 'old_weight': round(old_weight, 4), 'new_weight': round(new_weight, 4), 'reason': reason})

    return adjustments

async def _analyze_misjudgments(bull_misjudge: List[Dict], bear_misjudge: List[Dict],
                                market_change: float, weights: Dict) -> Dict:
    """分析误判原因"""
    analysis = {
        'market_context': '',
        'bull_misjudge_analysis': '',
        'bear_misjudge_analysis': '',
        'suggestions': [],
    }

    # 大盘环境分析
    if market_change > 0:
        analysis['market_context'] = f'大盘上涨{market_change:.2f}%'
    elif market_change < 0:
        analysis['market_context'] = f'大盘下跌{market_change:.2f}%'
    else:
        analysis['market_context'] = '大盘平盘'

    # 看涨误判基础数据统计
    if bull_misjudge:
        avg_predict_change = sum(s['predict_change'] for s in bull_misjudge) / len(bull_misjudge)
        avg_actual_change = sum(s['actual_change'] for s in bull_misjudge) / len(bull_misjudge)
        avg_outer = sum(s.get('outer_ratio', 50) for s in bull_misjudge) / len(bull_misjudge)
        avg_weibi = sum(s.get('weibi', 0) for s in bull_misjudge) / len(bull_misjudge)
        avg_vol = sum(s.get('volume_ratio', 1) for s in bull_misjudge) / len(bull_misjudge)
        
        # 尝试使用AI进行深度诊断
        try:
            load_ai_config()
            if DEEPSEEK_API_KEY:
                import json
                prompt = f"""
作为专业的A股量化分析师，请对昨日选股策略的"看涨误判"进行深度分析。
【大盘环境】
{analysis['market_context']}

【误判数据统计】
- 误判数量: {len(bull_misjudge)}只 (策略预测看涨，但实际下跌)
- 预测时平均涨幅: {avg_predict_change:.2f}%
- 实际平均跌幅: {avg_actual_change:.2f}%
- 误判样本平均特征:
  * 外盘占比: {avg_outer:.1f}%
  * 委比: {avg_weibi:.1f}%
  * 量比: {avg_vol:.2f}

【部分误判样本详情】
{json.dumps(bull_misjudge[:5], ensure_ascii=False)}

请分析误判的核心原因，并给出下一步优化选股策略的建议。
返回格式必须是JSON:
{{
  "bull_misjudge_analysis": "总结误判的核心原因，例如指出哪些技术指标存在失效或被主力骗线的可能（约100字）",
  "suggestions": ["优化建议1", "优化建议2"]
}}
"""
                ai_response = await _call_deepseek_api(prompt)
                ai_result = json.loads(ai_response)
                
                analysis['bull_misjudge_analysis'] = f"共{len(bull_misjudge)}只看涨误判，预测时平均涨幅{avg_predict_change:.2f}%，实际平均跌幅{avg_actual_change:.2f}%。" + ai_result.get('bull_misjudge_analysis', '')
                analysis['suggestions'] = ai_result.get('suggestions', [])
                
        except Exception as e:
            print(f"AI误判分析失败，回退到规则分析: {str(e)}")
            # 回退到基于规则的分析
            patterns = []
            if market_change > 0 and bull_misjudge:
                patterns.append('大盘上涨但个股下跌，可能是个股基本面问题或行业轮动')
            if avg_outer > STRATEGY_SCORE_THRESHOLDS['outer_ratio']['high']:
                patterns.append('外盘占比偏高但下跌，可能存在主力对倒出货')
            if avg_weibi > 20:
                patterns.append('委比偏高但下跌，可能是虚假委托（挂大买单不成交）')

            analysis['bull_misjudge_analysis'] = (
                f'共{len(bull_misjudge)}只看涨误判，'
                f'预测时平均涨幅{avg_predict_change:.2f}%，实际平均跌幅{avg_actual_change:.2f}%。'
                + '；'.join(patterns) if patterns else ''
            )

            if avg_outer > STRATEGY_SCORE_THRESHOLDS['outer_ratio']['high']:
                analysis['suggestions'].append('外盘占比指标的可靠性下降，建议降低该因子权重')
            if avg_weibi > 20:
                analysis['suggestions'].append(f'委比指标可能存在虚假信号，已增加交叉验证：委比>{STRATEGY_WEBI_VERIFY["weibi_high"]}但股价下跌时降分，委比高但外盘占比<{STRATEGY_WEBI_VERIFY["outer_ratio_low"]}%或量比<{STRATEGY_WEBI_VERIFY["volume_ratio_low"]}时降分')

    # 看跌误判分析（预测跌但实际涨）
    if bear_misjudge:
        avg_actual_change = sum(s['actual_change'] for s in bear_misjudge) / len(bear_misjudge)
        analysis['bear_misjudge_analysis'] = (
            f'共{len(bear_misjudge)}只看跌误判，实际平均涨幅{avg_actual_change:.2f}%。'
            f'{"大盘上涨带动反弹，属于系统性机会" if market_change > 1 else "可能低估了超跌反弹动能"}'
        )

    if not analysis['suggestions']:
        if bull_misjudge or bear_misjudge:
            analysis['suggestions'].append('建议持续观察，积累更多样本后优化权重')
        else:
            analysis['suggestions'].append('当前策略表现良好，维持现有权重')

    return analysis

def load_user_scan_pool() -> List[str]:
    try:
        conn = get_db_conn()
        cursor = conn.cursor(DictCursor)
        cursor.execute('SELECT symbol FROM user_scan_pool ORDER BY id')
        rows = cursor.fetchall()
        conn.close()
        return [row['symbol'] for row in rows]
    except Exception as error:
        print(f'加载自定义扫描池失败: {str(error)}')
        return []

def save_user_scan_pool(symbols: List[str]) -> bool:
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM user_scan_pool')
        for symbol in symbols:
            cursor.execute(
                'INSERT INTO user_scan_pool (symbol) VALUES (%s)',
                (symbol,)
            )
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存自定义扫描池失败: {str(error)}')
        return False

def load_hot_stock_buttons() -> List[Dict]:
    try:
        conn = get_db_conn()
        cursor = conn.cursor(DictCursor)
        cursor.execute('SELECT code, name FROM hot_stock_buttons ORDER BY id')
        rows = cursor.fetchall()
        conn.close()
        if rows:
            return [{'code': row['code'], 'name': row['name']} for row in rows]
        # 默认热门股票
        default_stocks = [
            {'name': '深科技', 'code': 'sz000021'},
            {'name': '亨通光电', 'code': 'sh600487'},
            {'name': '中天科技', 'code': 'sh600522'},
            {'name': '东山精密', 'code': 'sz002384'},
            {'name': '光迅科技', 'code': 'sz002281'},
            {'name': '利通电子', 'code': 'sh603629'}
        ]
        save_hot_stock_buttons(default_stocks)
        return default_stocks
    except Exception as error:
        print(f'加载热门股票失败: {str(error)}')
        return []

def save_hot_stock_buttons(stocks: List[Dict]) -> bool:
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM hot_stock_buttons')
        for stock in stocks:
            if isinstance(stock, dict) and stock.get('code') and stock.get('name'):
                cursor.execute(
                    'INSERT INTO hot_stock_buttons (code, name) VALUES (%s, %s)',
                    (stock['code'].lower(), stock['name'])
                )
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存热门股票失败: {str(error)}')
        return False

DEFAULT_STOCK_TAGS = {
    'sz000021': ['存储芯片', '半导体', '华为概念', '人工智能'],
    'sh600487': ['光通信', '海洋科技', '特高压'],
    'sh603618': ['光通信', '特高压', '智能电网'],
    'sz002281': ['光模块', '光纤通信', '华为概念'],
    'sh603629': ['消费电子', '新能源', '智能控制'],
    'sh600186': ['农业', '食品加工', '乡村振兴'],
    'sh603131': ['机器人', '工业4.0', '智能装备'],
    'sz002851': ['新能源', '工业自动化', '智能制造'],
    'sh603985': ['风电', '碳中和', '新能源'],
    'sh603738': ['消费电子', '芯片', '华为概念'],
    'sz003031': ['半导体', '芯片', '汽车电子'],
    'sh600884': ['锂电池', '新能源', '储能'],
    'sz002709': ['锂电池', '新能源', '储能'],
    'sh603256': ['PCB', '电子制造', '5G'],
    'sz002008': ['激光设备', '高端制造', '工业4.0'],
    'sz002636': ['PCB', '5G', '消费电子'],
    'sh601208': ['新材料', '化工', '半导体材料'],
    'sz002080': ['新材料', '玻纤', '风电'],
    'sh600584': ['芯片封测', '半导体', '华为概念'],
    'sz002156': ['芯片封测', '半导体', '算力'],
    'sz002185': ['芯片封测', '半导体', '5G'],
    'sz300014': ['锂电池', '储能', '新能源'],
    'sz002484': ['电容', '新能源', '光伏'],
    'sh600522': ['光通信', '海洋科技', '特高压'],
    'sh600118': ['航天军工', '北斗导航', '卫星通信'],
    'sz002384': ['消费电子', '精密制造', '汽车电子'],
    'sh603876': ['锂电池', '新能源', '储能'],
    'sz001309': ['存储芯片', '半导体', '消费电子'],
    'sz000063': ['5G', '通信设备', '数字经济'],
    'sz002241': ['VR/AR', '消费电子', '元宇宙'],
    'sz002475': ['消费电子', '苹果概念', '精密制造'],
    'sz300124': ['工业自动化', '机器人', '智能制造'],
    'sz002230': ['人工智能', 'AI', 'ChatGPT'],
    'sh688981': ['芯片', '半导体', '国产替代'],
    'sh601991': ['电力', '新能源发电', '绿色能源'],
    'sz002428': ['半导体材料', '锗', '稀缺资源'],
    'sz002222': ['激光', '光学', '半导体'],
    'sh600330': ['磁性材料', '稀土', '新能源'],
    'sz000988': ['激光', '5G', '光通信', '华为'],
    'sh600105': ['光通信', '光纤', '特高压'],
    'sz002015': ['新能源', '储能', '换电'],
    'sz000066': ['信创', '国产替代', '信息安全'],
    'sh605196': ['电线电缆', '新能源', '电网'],
    'sz000811': ['制冷设备', '碳中和', '氢能源', '冷链物流'],
    'sz002463': ['PCB', '汽车电子', '5G', '半导体'],
    'sz002916': ['PCB', '汽车电子', '服务器', '算力'],
    'sz002938': ['PCB', '消费电子', '苹果概念', '精密制造'],
    'sz300476': ['PCB', '新能源', '汽车电子', '5G'],
    'sz300657': ['PCB', '消费电子', 'MiniLED', 'VR/AR'],
    'sz002579': ['PCB', '消费电子', '汽车电子', '柔性电子'],
    'sh603920': ['PCB', '消费电子', '苹果概念', '5G'],
    'sz002913': ['PCB', '消费电子', '汽车电子', '高端制造'],
    'sz002815': ['PCB', '5G', '服务器', '汽车电子'],
    'sh603228': ['PCB', '消费电子', '汽车电子', '5G'],
    'sh600183': ['PCB', '5G', '服务器', '半导体材料']
}

def load_stock_concept_tags() -> Dict[str, List[str]]:
    try:
        conn = get_db_conn()
        cursor = conn.cursor(DictCursor)
        cursor.execute('SELECT symbol, tags_json FROM stock_concept_tags')
        rows = cursor.fetchall()
        conn.close()
        if rows:
            return {row['symbol']: json.loads(row['tags_json']) for row in rows if row['tags_json']}
        # 首次加载，初始化默认标签到数据库
        save_stock_concept_tags(DEFAULT_STOCK_TAGS)
        return DEFAULT_STOCK_TAGS.copy()
    except Exception as error:
        print(f'加载股票标签失败: {str(error)}')
        return DEFAULT_STOCK_TAGS.copy()

def save_stock_concept_tags(tags: Dict[str, List[str]]) -> bool:
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM stock_concept_tags')
        for symbol, tag_list in tags.items():
            cursor.execute(
                'INSERT INTO stock_concept_tags (symbol, tags_json) VALUES (%s, %s)',
                (symbol, json.dumps(tag_list, ensure_ascii=False))
            )
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存股票标签失败: {str(error)}')
        return False

def load_daily_market_reviews() -> List[Dict]:
    try:
        conn = get_db_conn()
        cursor = conn.cursor(DictCursor)
        cursor.execute(f'SELECT * FROM daily_market_reviews ORDER BY review_date DESC LIMIT {QUERY_MARKET_REVIEWS_LIMIT}')
        rows = cursor.fetchall()
        conn.close()

        reviews = []
        for row in rows:
            review = {
                'date': row['review_date'],
                'timestamp': row['created_at'].isoformat() if row['created_at'] else '',
                'summary': row['summary'] or '',
                'market': json.loads(row['market_json']) if row['market_json'] else {},
            }
            # 兼容新旧格式
            if row.get('industry_json'):
                review['industrySectors'] = json.loads(row['industry_json'])
            else:
                review['industrySectors'] = []
            if row.get('concept_json'):
                review['conceptSectors'] = json.loads(row['concept_json'])
            else:
                review['conceptSectors'] = []
            if row.get('limit_up_json'):
                review['limitUpStocks'] = json.loads(row['limit_up_json'])
            else:
                review['limitUpStocks'] = []
            if row.get('limit_down_json'):
                review['limitDownStocks'] = json.loads(row['limit_down_json'])
            else:
                review['limitDownStocks'] = []
            if row.get('top_gainers_json'):
                review['topGainers'] = json.loads(row['top_gainers_json'])
            else:
                review['topGainers'] = []
            if row.get('top_losers_json'):
                review['topLosers'] = json.loads(row['top_losers_json'])
            else:
                review['topLosers'] = []
            if row.get('focus_json'):
                review['tomorrowFocus'] = json.loads(row['focus_json'])
            else:
                review['tomorrowFocus'] = []
            # 旧格式兼容
            if row.get('trend_json'):
                review['trendStocks'] = json.loads(row['trend_json'])
            if row.get('pool_json'):
                review['stockPool'] = json.loads(row['pool_json'])
            reviews.append(review)
        return reviews
    except Exception as error:
        print(f'加载收评失败: {str(error)}')
        return []

def save_daily_market_reviews(reviews: List[Dict]) -> bool:
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        # 确保新字段存在
        new_cols = ['industry_json', 'concept_json', 'limit_up_json', 'limit_down_json', 'top_gainers_json', 'top_losers_json', 'focus_json']
        for col in new_cols:
            try:
                cursor.execute(f'ALTER TABLE daily_market_reviews ADD COLUMN {col} LONGTEXT')
            except Exception:
                pass  # 列已存在
        conn.commit()
        
        for review in reviews:
            review_date = review.get('date', '')
            summary = review.get('summary', '')
            market_json = json.dumps(review.get('market', {}), ensure_ascii=False)
            industry_json = json.dumps(review.get('industrySectors', []), ensure_ascii=False)
            concept_json = json.dumps(review.get('conceptSectors', []), ensure_ascii=False)
            limit_up_json = json.dumps(review.get('limitUpStocks', []), ensure_ascii=False)
            limit_down_json = json.dumps(review.get('limitDownStocks', []), ensure_ascii=False)
            top_gainers_json = json.dumps(review.get('topGainers', []), ensure_ascii=False)
            top_losers_json = json.dumps(review.get('topLosers', []), ensure_ascii=False)
            focus_json = json.dumps(review.get('tomorrowFocus', []), ensure_ascii=False)
            trend_json = json.dumps(review.get('trendStocks', {}), ensure_ascii=False)
            pool_json = json.dumps(review.get('stockPool', {}), ensure_ascii=False)
            cursor.execute('''
                INSERT INTO daily_market_reviews (review_date, summary, market_json, trend_json, pool_json, industry_json, concept_json, limit_up_json, limit_down_json, top_gainers_json, top_losers_json, focus_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE summary=%s, market_json=%s, trend_json=%s, pool_json=%s, industry_json=%s, concept_json=%s, limit_up_json=%s, limit_down_json=%s, top_gainers_json=%s, top_losers_json=%s, focus_json=%s
            ''', (review_date, summary, market_json, trend_json, pool_json, industry_json, concept_json, limit_up_json, limit_down_json, top_gainers_json, top_losers_json, focus_json,
                  summary, market_json, trend_json, pool_json, industry_json, concept_json, limit_up_json, limit_down_json, top_gainers_json, top_losers_json, focus_json))
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存收评失败: {str(error)}')
        import traceback
        print(f'[收评保存] 错误堆栈: {traceback.format_exc()}')
        return False

def add_stock_concept_tags(symbol: str, new_tags: List[str]) -> List[str]:
    """添加标签到指定股票，返回更新后的标签列表"""
    tags = load_stock_concept_tags()
    symbol_lower = symbol.lower()
    
    if symbol_lower not in tags:
        tags[symbol_lower] = []
    
    for tag in new_tags:
        tag_clean = tag.strip()
        if tag_clean and tag_clean not in tags[symbol_lower]:
            tags[symbol_lower].append(tag_clean)
    
    save_stock_concept_tags(tags)
    return tags[symbol_lower]

def remove_stock_tag(symbol: str, tag: str) -> List[str]:
    """从指定股票移除标签，返回更新后的标签列表"""
    tags = load_stock_concept_tags()
    symbol_lower = symbol.lower()
    
    if symbol_lower in tags:
        tag_clean = tag.strip()
        if tag_clean in tags[symbol_lower]:
            tags[symbol_lower].remove(tag_clean)
        if len(tags[symbol_lower]) == 0:
            del tags[symbol_lower]
    
    save_stock_concept_tags(tags)
    return tags.get(symbol_lower, [])

async def fetch_stock_concepts(symbol: str, name: str) -> Dict[str, any]:
    """尝试从网络获取股票相关概念标签（模拟实现）"""
    # 这里可以对接真实的财经数据API，比如东方财富、同花顺等
    # 现在实现一个基于常见概念的推理逻辑
    
    try:
        # 首先检查是否已有标签
        existing_tags = load_stock_concept_tags().get(symbol.lower(), [])
        
        # 基于股票名称和常见概念的推理
        concepts = []
        name_lower = name.lower() if name else ''
        
        # 常见概念关键词
        keyword_map = {
            '科技': ['科技', '电子', '通信', '信息'],
            '新能源': ['新能', '光伏', '风电', '锂电', '储能', '氢能', '绿色'],
            '半导体': ['芯片', '半导体', '集成电路', 'IC', '封测'],
            '人工智能': ['AI', '人工', '智能', '机器人', '机器视觉'],
            '华为概念': ['华为', '鸿蒙', '鲲鹏'],
            '汽车': ['汽车', '车', '特斯拉', '比亚迪'],
            '消费电子': ['消费', '电子', '智能穿戴'],
            '5G': ['5G', '通信', '光模块', '光纤'],
            '军工': ['军工', '航天', '航空', '武器'],
            '医疗': ['医疗', '医药', '生物', '健康'],
            '农业': ['农业', '乡村', '种植'],
            '金融': ['银行', '证券', '保险', '金融'],
            '房地产': ['地产', '房产', '保利', '万科']
        }
        
        # 检查股票名称中的关键词
        for concept, keywords in keyword_map.items():
            if any(keyword in name_lower for keyword in keywords):
                concepts.append(concept)
        
        # 如果没有找到，添加一些通用标签
        if not concepts:
            concepts.extend(['待分析', '关注'])
        
        return {
            'success': True,
            'symbol': symbol,
            'name': name,
            'concepts': concepts,
            'existing_tags': existing_tags,
            'suggestions': concepts  # 建议的标签
        }
    except Exception as e:
        print(f'获取股票概念失败: {str(e)}')
        return {
            'success': False,
            'symbol': symbol,
            'name': name,
            'concepts': [],
            'error': str(e)
        }

def load_hot_search_ranking() -> Dict:
    try:
        conn = get_db_conn()
        cursor = conn.cursor(DictCursor)
        # 读取元数据
        cursor.execute('SELECT meta_key, meta_value FROM hot_search_meta')
        meta_rows = cursor.fetchall()
        meta = {row['meta_key']: row['meta_value'] for row in meta_rows}
        # 读取股票数据
        cursor.execute('SELECT * FROM hot_search_ranking ORDER BY sort_order ASC')
        stock_rows = cursor.fetchall()
        conn.close()

        stocks = []
        for row in stock_rows:
            stocks.append({
                'symbol': row['symbol'],
                'code': row['code'],
                'name': row['name'],
                'market': row['market'],
                'price': row['price'],
                'changePercent': row['change_percent'],
                'change': row['change_val'],
                'hotRank': row.get('hot_rank', 0)
            })

        return {
            'updateTime': meta.get('updateTime', ''),
            'total': int(meta.get('total', 0)),
            'filteredCount': int(meta.get('filteredCount', 0)),
            'stocks': stocks
        }
    except Exception as error:
        print(f'加载hot_search_ranking数据失败: {str(error)}')
        return {'updateTime': '', 'total': 0, 'filteredCount': 0, 'stocks': []}

def save_hot_search_ranking(data: Dict) -> bool:
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM hot_search_ranking')
        cursor.execute('DELETE FROM hot_search_meta')
        for key, value in [('updateTime', data.get('updateTime', '')),
                           ('total', str(data.get('total', 0))),
                           ('filteredCount', str(data.get('filteredCount', 0)))]:
            cursor.execute(
                'INSERT INTO hot_search_meta (meta_key, meta_value) VALUES (%s, %s)',
                (key, value)
            )
        for idx, stock in enumerate(data.get('stocks', [])):
            cursor.execute('''
                INSERT INTO hot_search_ranking (symbol, code, name, market, price, change_percent, change_val, hot_rank, sort_order)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ''', (
                stock.get('symbol', ''),
                stock.get('code', ''),
                stock.get('name', ''),
                stock.get('market', ''),
                stock.get('price', 0),
                stock.get('changePercent', 0),
                stock.get('change', 0),
                stock.get('hotRank', 0),
                idx
            ))
        today_str = date.today().isoformat()
        try:
            cursor.execute('DELETE FROM hot_search_daily_snapshot WHERE snapshot_date = %s', (today_str,))
            for stock in data.get('stocks', []):
                if stock.get('hotRank', 0) <= HOT_SEARCH_TOP_N:
                    cursor.execute('''
                        INSERT INTO hot_search_daily_snapshot (snapshot_date, symbol, name, hot_rank)
                        VALUES (%s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE hot_rank = VALUES(hot_rank), name = VALUES(name)
                    ''', (today_str, stock.get('symbol', ''), stock.get('name', ''), stock.get('hotRank', 0)))
        except Exception as snap_err:
            print(f'保存热搜快照失败(非致命): {snap_err}')
        cutoff = (date.today() - timedelta(days=HOT_SEARCH_SNAPSHOT_CLEANUP_DAYS)).isoformat()
        try:
            cursor.execute('DELETE FROM hot_search_daily_snapshot WHERE snapshot_date < %s', (cutoff,))
        except Exception:
            pass
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存hot_search_ranking数据失败: {str(error)}')
        return False

async def fetch_with_retry(symbols: str, client: Optional[httpx.AsyncClient] = None) -> str:
    url = TENCENT_API + symbols
    last_err = None
    cl = client or get_http_client()
    
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = await cl.get(url, headers={'Accept-Encoding': 'gzip'})
            response.raise_for_status()
            
            raw = response.content
            if len(raw) < 10:
                raise Exception('返回数据过短')
            
            try:
                data = raw.decode('gbk')
            except:
                data = raw.decode('utf-8', errors='ignore')
            
            if '=' not in data or '="\n' in data:
                raise Exception('API返回数据为空')
            
            return data
        except Exception as err:
            last_err = err
            if attempt < MAX_RETRIES:
                await asyncio.sleep(0.5 * attempt)
    
    raise last_err

async def fetch_stock_data(symbol: str, client: Optional[httpx.AsyncClient] = None) -> Optional[Dict]:
    try:
        raw_data = await fetch_with_retry(symbol, client)
        parsed_list = parse_tencent_batch_data(raw_data)
        if parsed_list and len(parsed_list) > 0:
            return {'raw': raw_data, 'parsed': parsed_list[0]}
        return None
    except Exception as e:
        print(f'获取股票 {symbol} 数据失败: {str(e)}')
        return None

async def fetch_eastmoney_hot_list():
    try:
        print('[热搜榜] 正在获取东方财富热搜榜数据...')
        
        url = EASTMONEY_HOT_LIST_API
        
        params = {
            'pn': 1,
            'pz': HOT_SEARCH_TOP_N,
            'po': 1,
            'np': 1,
            'fltt': 1,
            'invt': 2,
            'fid': 'f3',
            'fs': 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23',
            'fields': 'f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,f20,f21,f23,f24,f25,f26,f22,f33,f11,f62,f128,f136,f115,f152'
        }
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        
        client = get_http_client()
        response = await client.get(url, params=params, headers=headers)
        response.raise_for_status()
        
        data = response.json()
        
        if not data or not data.get('data') or not data['data'].get('diff'):
            raise Exception('API返回数据格式错误')
        
        stocks = data['data']['diff']
        result = []
        
        for idx, stock in enumerate(stocks):
            market = 'sh' if stock.get('f13') == 1 else 'sz'
            code = stock.get('f12', '')
            name = stock.get('f14', '')
            
            is_shanghai_main = market == 'sh' and (
                code.startswith('600') or 
                code.startswith('601') or 
                code.startswith('603') or 
                code.startswith('605')
            )
            is_shenzhen_main = market == 'sz' and (
                code.startswith('000') or 
                code.startswith('001')
            )
            
            if is_shanghai_main or is_shenzhen_main:
                if not is_forbidden_stock(name):
                    result.append({
                        'symbol': market + code,
                        'code': code,
                        'name': name,
                        'market': market,
                        'price': stock.get('f2', 0) or 0,
                        'changePercent': stock.get('f3', 0) or 0,
                        'change': stock.get('f4', 0) or 0,
                        'hotRank': idx + 1
                    })
        
        print(f'[热搜榜] 成功获取 {len(result)} 只沪深主板股票')
        
        return {
            'updateTime': datetime.now().isoformat(),
            'total': len(stocks),
            'filteredCount': len(result),
            'stocks': result
        }
            
    except Exception as error:
        print(f'[热搜榜] 获取失败: {str(error)}')
        raise error

def parse_realtime_fields(data: str) -> Optional[Dict]:
    try:
        lines = data.split(';')
        for line in lines:
            if line.startswith('v_') and '=' in line:
                content = line[line.index('"') + 1 : line.rfind('"')]
                fields = content.split('~')
                if len(fields) > 40:
                    return {
                        'name': fields[1],
                        'price': float(fields[3]) if fields[3] else 0,
                        'yesterdayClose': float(fields[4]) if fields[4] else 0,
                        'open': float(fields[5]) if fields[5] else 0,
                        'high': float(fields[33]) if fields[33] else 0,
                        'low': float(fields[34]) if fields[34] else 0,
                        'volume': float(fields[36]) if fields[36] else 0
                    }
    except Exception as e:
        print(f'[解析实时数据] 失败: {str(e)}')
    return None

def calculate_ma(data: List[List], period: int) -> List[Optional[float]]:
    result = []
    for i in range(len(data)):
        if i < period - 1:
            result.append(None)
            continue
        total = 0.0
        for j in range(period):
            total += float(data[i - j][2])
        result.append(round(total / period, 3))
    return result

def is_up_trend(kline_data: List[List], realtime_price: Optional[float] = None) -> Dict:
    """
    判断是否处于上升趋势（统一的趋势判断入口）

    整合了：
    1. K线数据计算5/10/20/60/120日均线
    2. 5日线/10日线硬过滤（合并在一处）
    3. 5项趋势评分条件（精简后，去除冗余项）
    4. 实时价校准（解决盘中跌破无法及时发现的问题）

    关键计算口径（与 README §2.2/2.3 严格一致）：
    - "价格站上 120 日线"：最新收盘价（盘中时为实时价）> MA120
    - "MA120 斜率向上"：近 N 日 MA120 均值 > 前 N 日 MA120 均值（N = TREND_MA120_SLOPE_WINDOW）
    - "近 120 天涨停"：近 120 个交易日内涨幅 ≥ 9.8% 的次数 ≥ 1（前复权）
    - "近 250 天涨停"：近 250 个交易日内涨幅 ≥ 9.8% 的次数 ≥ 2（前复权）
    - "回撤控制"：近 120 天最大回撤 = (峰值 - 谷值) / 峰值 × 100%，< 30%
    - "连续上涨"：每个交易日收盘价（前复权）较前一日涨幅 > 0%，从最新交易日往前往后连续计数
      （仅统计最近 TREND_CONSECUTIVE_UP_MAX_DAYS 个交易日内的连续上涨天数）
    - 均线空头排列：MA5 同时 < MA10 且 < MA20（即 MA20 > MA10 > MA5）

    设计权衡（盘中实时价）：
    - USE_INTRADAY_BREAK_CHECK = True（默认）：盘中跌破立即排除，及时识别转弱信号
      副作用：股票盘中短暂刺破均线但收盘收复，会被临时移出趋势池，需等下次全量扫描
    - USE_INTRADAY_BREAK_CHECK = False：仅用收盘价判断，更稳健，但盘中跌破无法实时发现

    :param kline_data: K线数据
    :param realtime_price: 实时价格（用于盘中跌破检查，可选）
    :return: 趋势判断结果
    """
    if not kline_data or len(kline_data) < TREND_MIN_KLINE_DAYS:
        return {'isUp': False, 'reason': f'数据不足，需要至少{TREND_MIN_KLINE_DAYS}日K线', 'score': 0, 'details': {}}

    closes = [float(k[2]) for k in kline_data]
    ma5 = calculate_ma(kline_data, 5)
    ma10 = calculate_ma(kline_data, 10)
    ma20 = calculate_ma(kline_data, 20)
    ma60 = calculate_ma(kline_data, 60)
    ma120 = calculate_ma(kline_data, 120)

    latest_idx = len(kline_data) - 1
    latest_close = closes[latest_idx]

    # ========== 硬过滤：5日线/10日线检查（合并在一处） ==========
    # 优先使用实时价进行盘中跌破检查（如果提供），解决数据滞后问题
    # 可通过 USE_INTRADAY_BREAK_CHECK 关闭盘中实时价校准，改为仅用收盘价判断
    if USE_INTRADAY_BREAK_CHECK and realtime_price and realtime_price > 0:
        check_price = realtime_price
        is_realtime = True
    else:
        check_price = latest_close
        is_realtime = False

    # 5日线检查：必须在MA5之上
    if ma5[latest_idx] and check_price < ma5[latest_idx]:
        price_type = '实时价' if is_realtime else '收盘价'
        return {
            'isUp': False,
            'reason': f'{price_type}{check_price:.2f}跌破5日线{ma5[latest_idx]:.2f}',
            'score': 0,
            'details': {
                'ma5': round(ma5[latest_idx], 2) if ma5[latest_idx] else None,
                'ma10': round(ma10[latest_idx], 2) if ma10[latest_idx] else None,
                'latestClose': latest_close,
                'checkPrice': check_price,
                'isRealtime': is_realtime
            }
        }

    # 10日线检查：必须在MA10之上
    if ma10[latest_idx] and check_price < ma10[latest_idx]:
        price_type = '实时价' if is_realtime else '收盘价'
        return {
            'isUp': False,
            'reason': f'{price_type}{check_price:.2f}跌破10日线{ma10[latest_idx]:.2f}',
            'score': 0,
            'details': {
                'ma5': round(ma5[latest_idx], 2) if ma5[latest_idx] else None,
                'ma10': round(ma10[latest_idx], 2) if ma10[latest_idx] else None,
                'latestClose': latest_close,
                'checkPrice': check_price,
                'isRealtime': is_realtime
            }
        }

    # ========== 硬过滤：均线排列检查 ==========
    # 禁止空头排列：MA5 同时低于 MA10 和 MA20（即 MA20 > MA10 > MA5）
    # 允许多头排列：MA5 > MA10 > MA20
    # 允许 MA5 处于 MA10 和 MA20 中间：MA20 < MA5 < MA10 或 MA10 < MA5 < MA20
    if ma5[latest_idx] and ma10[latest_idx] and ma20[latest_idx]:
        if ma5[latest_idx] < min(ma10[latest_idx], ma20[latest_idx]):
            return {
                'isUp': False,
                'reason': f'均线空头排列(MA5{ma5[latest_idx]:.2f} < MA10{ma10[latest_idx]:.2f} 且 < MA20{ma20[latest_idx]:.2f})',
                'score': 0,
                'details': {
                    'ma5': round(ma5[latest_idx], 2),
                    'ma10': round(ma10[latest_idx], 2),
                    'ma20': round(ma20[latest_idx], 2),
                    'latestClose': latest_close,
                    'checkPrice': check_price,
                    'isRealtime': is_realtime
                }
            }

    # ========== 评分计算 ==========
    # 连续上涨天数：从最新交易日往前往后连续计数，每日收盘价（前复权）较前一日 > 0% 算上涨
    # 仅在最近 TREND_CONSECUTIVE_UP_MAX_DAYS 个交易日内统计，避免早期数据干扰
    consecutive_up_days = 0
    scan_end = max(0, latest_idx - TREND_CONSECUTIVE_UP_MAX_DAYS)
    for i in range(latest_idx, scan_end, -1):
        if closes[i] > closes[i - 1]:
            consecutive_up_days += 1
        else:
            break

    # 近120天涨停次数（前复权：基于 K 线收盘价较前一日涨幅 ≥ 9.8%）
    limit_up_count_120 = 0
    for i in range(max(0, len(kline_data) - 120), len(kline_data)):
        prev_close = closes[i - 1] if i > 0 else float(kline_data[i][1])
        change_pct = (closes[i] - prev_close) / prev_close * 100 if prev_close > 0 else 0
        if change_pct >= TREND_LIMIT_UP_THRESHOLD:
            limit_up_count_120 += 1

    # 近250天涨停次数（前复权：基于 K 线收盘价较前一日涨幅 ≥ 9.8%）
    limit_up_count_250 = 0
    for i in range(max(0, len(kline_data) - 250), len(kline_data)):
        prev_close = closes[i - 1] if i > 0 else float(kline_data[i][1])
        change_pct = (closes[i] - prev_close) / prev_close * 100 if prev_close > 0 else 0
        if change_pct >= TREND_LIMIT_UP_THRESHOLD:
            limit_up_count_250 += 1

    # 近120天最大回撤（基于前复权收盘价：(峰值 - 谷值) / 峰值 × 100%）
    max_drawdown_120 = 0.0
    peak = closes[max(0, len(kline_data) - 120)]
    for i in range(max(0, len(kline_data) - 120), len(kline_data)):
        peak = max(peak, closes[i])
        drawdown = (peak - closes[i]) / peak * 100 if peak > 0 else 0
        max_drawdown_120 = max(max_drawdown_120, drawdown)

    # ========== 5项趋势条件评分（精简后，去除冗余） ==========
    # 优化说明：原6项中"MA60>MA120"与"MA120站上+MA120斜率向上+收盘价>MA120"高度重复
    # MA60>MA120 已被其他三项隐含，去除该项避免冗余评分
    # MA120斜率向上：近 N 日 MA120 均值 > 前 N 日 MA120 均值（N = TREND_MA120_SLOPE_WINDOW）
    slope_window = TREND_MA120_SLOPE_WINDOW
    if (latest_idx >= 2 * slope_window and ma120[latest_idx]
            and all(ma120[latest_idx - k] is not None for k in range(2 * slope_window + 1))):
        recent_avg = sum(ma120[latest_idx - k] for k in range(slope_window)) / slope_window
        prev_avg = sum(ma120[latest_idx - slope_window - k] for k in range(slope_window)) / slope_window
        ma120_slope_up = recent_avg > prev_avg
    else:
        ma120_slope_up = False

    conditions = {
        'priceAboveMa120': latest_close > ma120[latest_idx] if ma120[latest_idx] else False,  # 价格站上120日线
        'ma120SlopeUp': ma120_slope_up,  # MA120斜率向上（近N日均值 > 前N日均值）
        'limitUp120': limit_up_count_120 >= TREND_LIMIT_UP_120_MIN,  # 近120天涨停
        'limitUp250': limit_up_count_250 >= TREND_LIMIT_UP_250_MIN,  # 近250天涨停
        'drawdown30': max_drawdown_120 < TREND_MAX_DRAWDOWN  # 回撤控制
    }
    
    details = {
        **conditions,
        'consecutiveUpDays': consecutive_up_days,
        'limitUpCount120': limit_up_count_120,
        'limitUpCount250': limit_up_count_250,
        'maxDrawdown120': round(max_drawdown_120, 1),
        'ma5': round(ma5[latest_idx], 2) if ma5[latest_idx] else None,
        'ma10': round(ma10[latest_idx], 2) if ma10[latest_idx] else None,
        'ma20': round(ma20[latest_idx], 2) if ma20[latest_idx] else None,
        'ma60': round(ma60[latest_idx], 2) if ma60[latest_idx] else None,
        'ma120': round(ma120[latest_idx], 2) if ma120[latest_idx] else None,
        'latestClose': latest_close,
        'checkPrice': check_price,
        'isRealtime': is_realtime
    }
    
    # 计算总分
    total_score = 0
    if conditions['priceAboveMa120']: total_score += TREND_SCORE_WEIGHTS['priceAboveMa120']
    if conditions['ma120SlopeUp']: total_score += TREND_SCORE_WEIGHTS['ma120SlopeUp']
    if conditions['limitUp120']: total_score += TREND_SCORE_WEIGHTS['limitUp120']
    if conditions['limitUp250']: total_score += TREND_SCORE_WEIGHTS['limitUp250']
    if conditions['drawdown30']: total_score += TREND_SCORE_WEIGHTS['drawdown30']
    # 连续上涨加分（每连涨1天+2分，最高+10分）
    total_score += min(consecutive_up_days * TREND_CONSECUTIVE_UP_BONUS, TREND_CONSECUTIVE_UP_MAX_BONUS)
    
    is_trend_up = total_score >= TREND_IS_UP_MIN_SCORE
    
    return {
        'isUp': is_trend_up,
        'score': total_score,
        'details': details,
        'ma5': ma5[-10:] if len(ma5) >= 10 else ma5,
        'ma10': ma10[-10:] if len(ma10) >= 10 else ma10,
        'ma20': ma20[-10:] if len(ma20) >= 10 else ma20,
        'latestPrice': latest_close,
        'checkPrice': check_price,
        'isRealtime': is_realtime,
        'recent5Days': closes[-5:] if len(closes) >= 5 else closes
    }

def get_full_name_from_hot_search_ranking(symbol: str) -> Optional[str]:
    try:
        hot_search_ranking = load_hot_search_ranking()
        if hot_search_ranking.get('stocks'):
            for s in hot_search_ranking['stocks']:
                if s['symbol'].lower() == symbol.lower():
                    return s['name']
    except:
        pass
    return None

async def get_stock_name(symbol: str) -> Optional[str]:
    global _stock_basic_info_cache
    s = symbol.lower()
    
    cached = get_cached_stock_name(s)
    if cached:
        return cached
    
    try:
        data = await fetch_with_retry(s)
        match = re.search(r'v_\w+="([^"]+)"', data)
        if match:
            name = match.group(1).split('~')[1] if len(match.group(1).split('~')) > 1 else s
            
            hot_search_ranking_name = get_full_name_from_hot_search_ranking(s)
            if hot_search_ranking_name and len(hot_search_ranking_name) > len(name):
                name = hot_search_ranking_name
            
            if is_forbidden_stock(name):
                return None
            
            if name != s:
                if _stock_basic_info_cache is None:
                    _stock_basic_info_cache = load_stock_basic_info()
                _stock_basic_info_cache[s] = name
                save_stock_basic_info(_stock_basic_info_cache)
            
            return name
    except:
        pass
    return symbol

async def get_kline_data(symbol: str, client: Optional[httpx.AsyncClient] = None) -> Optional[List[List]]:
    today = date.today().isoformat()
    start_date = KLINE_START_DATE
    url = f'{TENCENT_KLINE_API}?param={symbol},day,{start_date},{today},{KLINE_DATA_LIMIT},qfq'
    cl = client or get_http_client()
    
    try:
        kline = []
        
        try:
            response = await cl.get(url)
            response.raise_for_status()
            data = response.json()
            if data.get('data') and data['data'].get(symbol):
                kline = data['data'][symbol].get('qfqday', []) or data['data'][symbol].get('day', [])
        except Exception as e:
            print(f'[K线API请求] {str(e)}')
        
        if not kline or len(kline) < 30:
            return None
        
        try:
            realtime_data = await fetch_with_retry(symbol, cl)
            if realtime_data:
                fields = parse_realtime_fields(realtime_data)
                if fields and fields['price']:
                    latest_kline = kline[-1] if kline else None
                    latest_date = latest_kline[0] if latest_kline else ''
                    
                    open_val = fields['open'] or fields['price']
                    price_val = fields['price']
                    high_val = fields['high'] or fields['price']
                    low_val = fields['low'] or fields['price']
                    vol_val = fields['volume'] or 0
                    
                    if latest_date != today:
                        kline.append([today, open_val, price_val, high_val, low_val, vol_val])
                    else:
                        latest_kline[2] = price_val
                        latest_kline[3] = max(float(latest_kline[3]), float(high_val))
                        latest_kline[4] = min(float(latest_kline[4]), float(low_val))
        except Exception as e:
            pass
        
        return kline
    except Exception as e:
        print(f'[K线获取失败] {symbol}: {str(e)}')
        return None

def parse_tencent_batch_data(data: str) -> List[Dict]:
    results = []
    lines = re.split(r'[\n;]+', data.strip())
    
    for line in lines:
        trimmed = line.strip()
        if not trimmed.startswith('v_') or '=' not in trimmed:
            continue
        
        eq_idx = trimmed.index('=')
        symbol = trimmed[2:eq_idx]
        content = trimmed[eq_idx + 1:].strip('"')
        fields = content.split('~')
        
        if len(fields) <= 49:
            continue
        
        price = float(fields[3]) if fields[3] else 0
        change_val = float(fields[31]) if fields[31] else 0
        change_pct = float(fields[32]) if fields[32] else 0
        outer_plate = int(fields[7]) if fields[7] else 0
        inner_plate = int(fields[8]) if fields[8] else 0
        volume_raw = int(fields[36]) if fields[36] else 0
        turnover = float(fields[37]) if fields[37] else 0
        turnover_rate = float(fields[38]) if fields[38] else 0
        volume_ratio = float(fields[49]) if fields[49] else 0
        amplitude = float(fields[43]) if fields[43] else 0
        pe = float(fields[39]) if fields[39] else 0
        bid1_vol = int(fields[12]) if fields[12] else 0
        ask1_vol = int(fields[22]) if fields[22] else 0
        
        weibi = (((bid1_vol - ask1_vol) / (bid1_vol + ask1_vol)) * 100) if (bid1_vol + ask1_vol) > 0 else 0
        weibi = round(weibi, 2)
        
        avg_price = ((turnover * 10000) / (volume_raw * 100)) if volume_raw > 0 else price
        avg_price = round(avg_price, 2)
        
        avg_price_deviation = (((price - avg_price) / avg_price) * 100) if avg_price > 0 else 0
        avg_price_deviation = round(avg_price_deviation, 2)
        
        total_plate = outer_plate + inner_plate
        outer_ratio = ((outer_plate / total_plate) * 100) if total_plate > 0 else 50
        outer_ratio = round(outer_ratio, 1)
        
        results.append({
            'name': fields[1],
            'symbol': symbol,
            'code': fields[2],
            'price': fields[3],
            'priceRaw': price,
            'change': change_val,
            'changePercent': change_pct,
            'isUp': change_val > 0,
            'open': fields[5],
            'outerPlateRaw': outer_plate,
            'innerPlateRaw': inner_plate,
            'volumeRaw': volume_raw,
            'turnover': turnover,
            'turnoverRate': turnover_rate,
            'volumeRatio': volume_ratio,
            'amplitude': amplitude,
            'pe': pe,
            'weibi': weibi,
            'avgPrice': avg_price,
            'avgPriceDeviation': avg_price_deviation,
            'outerRatio': outer_ratio
        })
    
    return results

def check_below_ma10(kline_data: List[List]) -> Dict:
    """
    检查股票是否跌破10日线且当天没有收回

    注意：本函数由每天 15:05 的定时任务调用，作用对象是**用户股票池**（非趋势池）。
    趋势池通过 `scan_trend_scan_results` 全量替换机制自行维护跌破移除逻辑；
    本函数是用户自选股的兜底检查，避免用户股票池中长期保留已破位股票。

    判定标准：
    - 当天最低价 < MA10 → 跌破
    - 且当天收盘价 < MA10 → 未收回，应该删除（shouldRemove=True）
    - 若最低价 < MA10 但收盘价 >= MA10 → 视为盘中刺破已收复，不删除

    返回: {'shouldRemove': True/False, 'reason': '原因', 'latestClose': 收盘价, 'ma10': 10日线}
    """
    if not kline_data or len(kline_data) < 10:
        return {
            'shouldRemove': False,
            'reason': 'K线数据不足，需要至少10日数据',
            'latestClose': None,
            'ma10': None
        }
    
    # 计算MA10
    ma10_list = calculate_ma(kline_data, 10)
    latest_idx = len(kline_data) - 1
    latest_kline = kline_data[latest_idx]
    latest_close = float(latest_kline[2])
    latest_low = float(latest_kline[4])
    ma10 = ma10_list[latest_idx]
    
    if ma10 is None:
        return {
            'shouldRemove': False,
            'reason': 'MA10数据不足',
            'latestClose': latest_close,
            'ma10': None
        }
    
    # 检查是否跌破10日线（最低价 < MA10）
    if latest_low < ma10:
        # 检查当天是否收回（收盘价 >= MA10）
        if latest_close < ma10:
            # 跌破且没有收回，应该删除
            return {
                'shouldRemove': True,
                'reason': f'跌破10日线且未收回（收盘价{latest_close:.2f} < MA10{ma10:.2f}）',
                'latestClose': latest_close,
                'ma10': ma10
            }
        else:
            # 跌破但收回了，不需要删除
            return {
                'shouldRemove': False,
                'reason': f'曾跌破10日线但已收回（收盘价{latest_close:.2f} >= MA10{ma10:.2f}）',
                'latestClose': latest_close,
                'ma10': ma10
            }
    else:
        # 没有跌破，不需要删除
        return {
            'shouldRemove': False,
            'reason': f'未跌破10日线（收盘价{latest_close:.2f}，MA10{ma10:.2f}）',
            'latestClose': latest_close,
            'ma10': ma10
        }

async def scan_and_remove_below_ma10():
    """
    扫描股票池，删除跌破10日线且未收回的股票

    同时检查两个池：
    1. user_scan_pool（用户股票池）
    2. trend_scan_results（趋势股票池，当日扫描结果）
    """
    try:
        print('[10日线检查] 开始扫描股票池...')
        
        # 加载用户股票池
        symbols = load_user_scan_pool()
        if not symbols:
            print('[10日线检查] 用户股票池为空')
        else:
            print(f'[10日线检查] 用户股票池共{len(symbols)}只股票需要检查')
        
        # 加载今日趋势股票池（用于在用户股票池之外，额外检查趋势列表）
        trend_symbols = []
        try:
            trend_data = load_trend_scan_results()
            today = date.today().isoformat()
            # 只检查当天的趋势扫描结果
            if trend_data and trend_data.get('date') == today and trend_data.get('stocks'):
                trend_symbols = [s['symbol'] for s in trend_data['stocks'] if s.get('symbol')]
                print(f'[10日线检查] 当日趋势股票池共{len(trend_symbols)}只股票需要检查')
        except Exception as e:
            print(f'[10日线检查] 加载趋势股票池失败: {str(e)}')
        
        # 合并两个池（去重）
        all_symbols = list(dict.fromkeys((symbols or []) + trend_symbols))
        if not all_symbols:
            print('[10日线检查] 股票池为空，无需检查')
            return
        
        # 区分用户池 vs 趋势池中的股票（用于决定从哪个池删除）
        user_set = set(symbols or [])
        trend_set = set(trend_symbols)
        
        removed_user_symbols = []
        kept_user_symbols = []
        removed_trend_symbols = []
        kept_trend_symbols = []
        
        # 逐个检查股票
        for symbol in all_symbols:
            try:
                # 获取K线数据
                kline_data = await get_kline_data(symbol.lower())
                if not kline_data:
                    print(f'[10日线检查] {symbol} 获取K线数据失败，跳过')
                    if symbol in user_set:
                        kept_user_symbols.append(symbol)
                    if symbol in trend_set:
                        kept_trend_symbols.append(symbol)
                    continue
                
                # 检查是否跌破10日线
                result = check_below_ma10(kline_data)
                stock_name = get_cached_stock_name(symbol) or symbol
                in_user = symbol in user_set
                in_trend = symbol in trend_set
                
                if result['shouldRemove']:
                    pool_tags = []
                    if in_user:
                        pool_tags.append('用户池')
                    if in_trend:
                        pool_tags.append('趋势池')
                    tag_str = '/'.join(pool_tags) if pool_tags else '股票池'
                    print(f'[10日线检查] {stock_name}({symbol}): {result["reason"]} - 从{tag_str}删除')
                    if in_user:
                        removed_user_symbols.append(symbol)
                    else:
                        kept_user_symbols.append(symbol)
                    if in_trend:
                        removed_trend_symbols.append(symbol)
                    else:
                        kept_trend_symbols.append(symbol)
                else:
                    print(f'[10日线检查] {stock_name}({symbol}): {result["reason"]}')
                    if in_user:
                        kept_user_symbols.append(symbol)
                    if in_trend:
                        kept_trend_symbols.append(symbol)
                
            except Exception as e:
                print(f'[10日线检查] {symbol} 检查失败: {str(e)}')
                if symbol in user_set:
                    kept_user_symbols.append(symbol)
                if symbol in trend_set:
                    kept_trend_symbols.append(symbol)
                continue
        
        # 1. 保存更新后的用户股票池
        if symbols:  # 用户池有数据时（即使没有删除也要保持原状）
            if removed_user_symbols:
                save_user_scan_pool(kept_user_symbols)
                print(f'[10日线检查] 用户池扫描完成！共删除{len(removed_user_symbols)}只，保留{len(kept_user_symbols)}只')
                print(f'[10日线检查] 用户池已删除: {removed_user_symbols}')
            else:
                print(f'[10日线检查] 用户池扫描完成！没有需要删除的股票')
        
        # 2. 处理趋势股票池 - 从 trend_scan_results 中删除跌破 MA10 的股票
        if removed_trend_symbols:
            # 从趋势扫描结果中删除跌破 MA10 的股票
            current_trend = load_trend_scan_results()
            current_stocks = current_trend.get('stocks', [])
            original_count = len(current_stocks)
            kept_stocks = [s for s in current_stocks if s.get('symbol') not in set(removed_trend_symbols)]
            new_count = len(kept_stocks)
            
            if new_count < original_count:
                # 重新保存趋势扫描结果
                updated_trend = dict(current_trend)
                updated_trend['stocks'] = kept_stocks
                updated_trend['found'] = new_count
                save_trend_scan_results(updated_trend)
                # 同步更新内存缓存
                global _last_scan_result
                if _last_scan_result and _last_scan_result.get('date') == updated_trend.get('date'):
                    _last_scan_result = updated_trend
                print(f'[10日线检查] 趋势池扫描完成！共删除{original_count - new_count}只，保留{new_count}只')
                print(f'[10日线检查] 趋势池已删除: {[s for s in removed_trend_symbols]}')
            else:
                print(f'[10日线检查] 趋势池扫描完成！没有需要删除的股票')
        else:
            print(f'[10日线检查] 趋势池扫描完成！没有需要删除的股票')
            
    except Exception as error:
        print(f'[10日线检查] 扫描失败: {str(error)}')

async def update_hot_search_ranking_task():
    try:
        print('[定时任务] 正在更新热搜榜数据...')
        data = await fetch_eastmoney_hot_list()
        save_hot_search_ranking(data)
        print('[定时任务] 热搜榜数据更新完成')
    except Exception as error:
        print(f'[定时任务] 更新热搜榜失败: {str(error)}')

async def generate_review_task():
    try:
        print('[定时任务] 正在生成今日收评...')
        review = await generate_daily_review()
        
        save_daily_market_reviews([review])
        
        print('[定时任务] 今日收评生成完成！')
    except Exception as error:
        print(f'[定时任务] 生成收评失败: {str(error)}')

async def backtest_task():
    try:
        print('[定时任务] 正在回测验证昨日预测...')
        result = await backtest_yesterday_predictions()
        if 'error' in result:
            print(f'[定时任务] 回测失败: {result["error"]}')
        elif 'message' in result:
            print(f'[定时任务] {result["message"]}')
        else:
            print(f'[定时任务] 回测完成: 准确率{result["accuracy"]}%, 看涨准确率{result["bull_accuracy"]}%, 看跌准确率{result["bear_accuracy"]}%')
            if result.get('weight_adjustments'):
                for adj in result['weight_adjustments']:
                    if adj['action'] != 'keep':
                        print(f'  权重调整: {adj["factor"]} {adj["action"]} ({adj["reason"]})')
    except Exception as error:
        print(f'[定时任务] 回测验证失败: {str(error)}')

async def scan_trend_task(force: bool = False):
    try:
        print('[趋势扫描] 定时任务开始...')
        await scan_trend_scan_results(force=force)
    except Exception as e:
        print(f'[趋势扫描] 定时任务失败: {e}')


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
    except Exception as error:
        print(f'[智能推荐] 自动生成失败: {str(error)}')


def run_scheduler():
    def job_update_hot_search_ranking():
        asyncio.run(update_hot_search_ranking_task())
    
    def job_scan_ma10():
        asyncio.run(scan_and_remove_below_ma10())
    
    def job_generate_review():
        asyncio.run(generate_review_task())
    
    def job_backtest():
        asyncio.run(backtest_task())
    
    def job_scan_trend():
        asyncio.run(scan_trend_task())
    
    def job_generate_recommendations():
        asyncio.run(auto_generate_recommendations_task())
    
    # 原有的定时任务
    schedule.every().day.at(SCHEDULE_UPDATE_HOT_SEARCH_TIMES[0]).do(job_update_hot_search_ranking)
    schedule.every().day.at(SCHEDULE_UPDATE_HOT_SEARCH_TIMES[1]).do(job_update_hot_search_ranking)
    
    schedule.every().day.at(SCHEDULE_MA10_CHECK_TIME).do(job_scan_ma10)
    
    schedule.every().day.at(SCHEDULE_DAILY_REVIEW_TIME).do(job_generate_review)
    
    schedule.every().day.at(SCHEDULE_BACKTEST_TIME).do(job_backtest)
    
    # 新增：每天自动扫描趋势股
    schedule.every().day.at('09:30').do(job_scan_trend)
    schedule.every().day.at('11:30').do(job_scan_trend)
    schedule.every().day.at('13:00').do(job_scan_trend)
    schedule.every().day.at('15:00').do(job_scan_trend)
    
    # 新增：每天自动生成智能推荐股票
    for t in SCHEDULE_RECOMMENDATION_TIMES:
        schedule.every().day.at(t).do(job_generate_recommendations)
    
    print('[定时任务] 已设置定时任务：')
    print(f'[定时任务]   - {SCHEDULE_UPDATE_HOT_SEARCH_TIMES[0]}/{SCHEDULE_UPDATE_HOT_SEARCH_TIMES[1]}: 更新热搜榜')
    print(f'[定时任务]   - 09:30/11:30/13:00/15:00: 自动扫描趋势股')
    print(f'[定时任务]   - {SCHEDULE_MA10_CHECK_TIME}: 检查10日线并删除跌破股票')
    print(f'[定时任务]   - {SCHEDULE_DAILY_REVIEW_TIME}: 自动生成每日收评')
    print(f'[定时任务]   - {SCHEDULE_BACKTEST_TIME}: 回测验证昨日预测并调整策略权重')
    print(f'[定时任务]   - {"/".join(SCHEDULE_RECOMMENDATION_TIMES)}: 自动生成智能推荐股票')
    
    while True:
        schedule.run_pending()
        time.sleep(60)



@app.get("/api/health", tags=["健康检查"])
async def health_check():
    return {"status": "ok", "time": datetime.now().isoformat()}

@app.get("/api/hot_search_ranking", tags=["热搜榜数据"])
async def get_hot_search_ranking_api():
    try:
        data = load_hot_search_ranking()
        return {"success": True, "data": data}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取hot_search_ranking失败: {str(error)}")

@app.get("/api/update-hot_search_ranking", tags=["热搜榜数据"])
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

@app.get("/api/stock/{symbol}", tags=["股票行情"])
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

@app.get("/api/stocks", tags=["股票行情"])
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

@app.get("/api/daily_recommendations", tags=["推荐记录"])
async def get_daily_recommendations():
    try:
        daily_recommendations = load_daily_recommendations()
        return {"success": True, "data": daily_recommendations}
    except Exception as error:
        raise HTTPException(status_code=500, detail="获取推荐记录失败")

@app.post("/api/daily_recommendations", tags=["推荐记录"])
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
            raise HTTPException(status_code=500, detail="保存推荐记录失败")
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail="保存推荐记录失败")

@app.delete("/api/daily_recommendations/{date}", tags=["推荐记录"])
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
        raise HTTPException(status_code=500, detail="删除推荐记录失败")


@app.post("/api/auto-generate-recommendations", tags=["推荐记录"])
async def auto_generate_recommendations_api():
    """手动触发自动生成智能推荐（用于调试和测试）"""
    try:
        await auto_generate_recommendations_task()
        return {"success": True, "message": "已触发自动生成推荐任务，请查看服务器日志"}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"触发失败: {str(error)}")


@app.post("/api/scan-ma10", tags=["趋势发现"])
async def scan_ma10_api():
    """手动触发10日线检查（同时检查用户池和趋势池），便于立即清理跌破10日线的股票"""
    try:
        await scan_and_remove_below_ma10()
        return {"success": True, "message": "已触发10日线检查任务，请查看服务器日志"}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"触发失败: {str(error)}")

def _ensure_today_date(data: Dict) -> Dict:
    if data:
        data['scanDate'] = data.get('date', '')
        data['date'] = date.today().isoformat()
    return data

@app.get("/api/trend-stocks", tags=["趋势发现"])
async def get_trend_scan_results():
    try:
        trend_data = load_trend_scan_results()
        return {"success": True, "data": _ensure_today_date(trend_data)}
    except Exception as error:
        raise HTTPException(status_code=500, detail="获取趋势股票失败")

_scan_trend_lock = asyncio.Lock()
_last_scan_result: Optional[Dict] = None
_last_scan_date: Optional[str] = None
_last_scan_ts: float = 0
SCAN_CACHE_TTL = SCAN_CACHE_TTL

@app.get("/api/scan-trend-stocks", tags=["趋势发现"])
async def scan_trend_scan_results(force: bool = False):
    global _last_scan_result, _last_scan_date, _last_scan_ts
    
    today = date.today().isoformat()
    now_ts = time.time()
    
    if not force and _last_scan_result and _last_scan_date == today and (now_ts - _last_scan_ts) < SCAN_CACHE_TTL:
        print(f'[趋势扫描] 使用内存缓存（{_last_scan_date}，{now_ts - _last_scan_ts:.0f}s前扫描）')
        return {"success": True, "data": _ensure_today_date(_last_scan_result.copy())}
    
    if _scan_trend_lock.locked():
        if _last_scan_result and _last_scan_date == today:
            return {"success": True, "data": _ensure_today_date(_last_scan_result.copy())}
        return {"success": True, "data": _ensure_today_date(load_trend_scan_results())}
    
    async with _scan_trend_lock:
        if not force and _last_scan_result and _last_scan_date == today and (time.time() - _last_scan_ts) < SCAN_CACHE_TTL:
            return {"success": True, "data": _ensure_today_date(_last_scan_result.copy())}
        
        t0 = time.time()
        global _stock_basic_info_cache, _hot_search_ranking_cache
        
        hot_search_ranking_data = load_hot_search_ranking()
        _hot_search_ranking_cache = hot_search_ranking_data
        _stock_basic_info_cache = None
        
        scan_pool = []
        source = ''
        
        if hot_search_ranking_data.get('stocks') and len(hot_search_ranking_data['stocks']) > 0:
            scan_pool = [s['symbol'] for s in hot_search_ranking_data['stocks']]
            source = 'eastmoney-hot'
            print(f'[趋势扫描] 使用热搜榜数据，共 {len(scan_pool)} 只股票 (更新时间: {hot_search_ranking_data.get("updateTime")})')
        else:
            local_map = load_stock_basic_info()
            map_symbols = list(local_map.keys())
            scan_pool = list(set(HOT_STOCK_POOL + map_symbols))
            source = 'fallback'
            print(f'[趋势扫描] 热搜榜数据为空，使用备选池，共 {len(scan_pool)} 只股票')
        
        # 把"上一次趋势列表"也并入扫描池，确保昨天在趋势列表里、今天从热搜榜掉出的股票也会被重新评估
        try:
            prev_trend = load_trend_scan_results()
            if prev_trend and prev_trend.get('stocks'):
                prev_symbols = [s['symbol'] for s in prev_trend['stocks'] if s.get('symbol')]
                if prev_symbols:
                    before_count = len(scan_pool)
                    scan_pool = list(dict.fromkeys(scan_pool + prev_symbols))
                    added = len(scan_pool) - before_count
                    if added > 0:
                        print(f'[趋势扫描] 已合并上一轮趋势列表 {len(prev_symbols)} 只（新增 {added} 只不在热搜榜中的），共 {len(scan_pool)} 只')
        except Exception as e:
            print(f'[趋势扫描] 合并上轮趋势列表失败: {str(e)}')
        
        client = get_http_client()
        weights = load_strategy_factor_weights()
        
        # 提前获取大盘涨跌幅，传递给评分函数
        market_change = 0.0
        try:
            idx_data = await fetch_stock_data('sh000001')
            if idx_data:
                market_change = idx_data.get('parsed', {}).get('changePercent', 0)
        except Exception:
            pass
        
        async def process_one_stock(symbol: str) -> Optional[Dict]:
            async with SCAN_SEMAPHORE:
                try:
                    kline = await asyncio.wait_for(get_kline_data(symbol, client), timeout=SCAN_KLINE_TIMEOUT)
                    if not kline:
                        return None
                    
                    # 获取实时数据（同时用于趋势判断的盘中跌破校准 + 策略评分）
                    realtime_data = None
                    try:
                        realtime_raw = await fetch_with_retry(symbol, client)
                        if realtime_raw:
                            parsed_list = parse_tencent_batch_data(realtime_raw)
                            if parsed_list:
                                realtime_data = parsed_list[0]
                    except Exception:
                        pass
                    
                    # 优先使用实时价进行趋势判断（解决盘中跌破数据滞后问题）
                    realtime_price = None
                    if realtime_data and realtime_data.get('priceRaw'):
                        realtime_price = realtime_data['priceRaw']
                    
                    # 趋势判断（传入实时价进行盘中跌破校准）
                    trend_result = is_up_trend(kline, realtime_price=realtime_price)
                    if not trend_result['isUp']:
                        return None
                    
                    name = get_cached_stock_name(symbol)
                    if not name:
                        name = await get_stock_name(symbol)
                    if not name or name == symbol:
                        return None
                    
                    if has_delisting_risk(name, symbol):
                        return None
                    
                    if not is_main_board(symbol):
                        return None
                    
                    strategy_score = None
                    if realtime_data:
                        # 传入 market_change 以便应用 AI 优化规则2
                        strategy_score = calc_stock_score_v2(realtime_data, weights, market_change=market_change)
                    
                    return {
                        'symbol': symbol,
                        'name': name,
                        'score': trend_result['score'],
                        'details': trend_result['details'],
                        'latestPrice': trend_result['latestPrice'],
                        'ma5': trend_result['ma5'],
                        'ma10': trend_result['ma10'],
                        'ma20': trend_result['ma20'],
                        'recent5Days': trend_result['recent5Days'],
                        'strategyScore': strategy_score,
                        'realtimeData': realtime_data
                    }
                except Exception as e:
                    return None
        
        try:
            tasks = [process_one_stock(s) for s in scan_pool]
            results = await asyncio.gather(*tasks)
            
            trend_scan_results = [r for r in results if r is not None and r['score'] >= TREND_MIN_SCORE]
            
            trend_scan_results.sort(key=lambda x: x['score'], reverse=True)
            
            # 保存策略预测记录 (复用上面已获取的 market_change)
            predictions = []
            cleaned_results = []
            for r in trend_scan_results:
                if r.get('strategyScore') and r.get('realtimeData'):
                    score_data = r['strategyScore']
                    realtime_data = r['realtimeData']
                    if score_data['predict_direction'] == 'bull':
                        predictions.append({
                            'predict_date': today,
                            'symbol': r['symbol'],
                            'name': r['name'],
                            'score': score_data['total'],
                            'label': score_data['label'],
                            'predict_direction': score_data['predict_direction'],
                            'outer_ratio': realtime_data.get('outerRatio', 0),
                            'volume_ratio': realtime_data.get('volumeRatio', 0),
                            'turnover_rate': realtime_data.get('turnoverRate', 0),
                            'change_percent': realtime_data.get('changePercent', 0),
                            'weibi': realtime_data.get('weibi', 0),
                            'avg_price_deviation': realtime_data.get('avgPriceDeviation', 0),
                            'amplitude': realtime_data.get('amplitude', 0),
                            'market_change': market_change,
                        })
                # 清理不需要返回给前端的字段
                cleaned_r = {
                    'symbol': r['symbol'],
                    'name': r['name'],
                    'score': r['score'],
                    'details': r['details'],
                    'latestPrice': r['latestPrice'],
                    'ma5': r['ma5'],
                    'ma10': r['ma10'],
                    'ma20': r['ma20'],
                    'recent5Days': r['recent5Days']
                }
                cleaned_results.append(cleaned_r)
            
            if predictions:
                save_predictions(predictions)
                print(f'[趋势扫描] 保存了 {len(predictions)} 条策略预测记录')
            
            result = {
                'date': today,
                'totalScanned': len(scan_pool),
                'found': len(trend_scan_results),
                'stocks': cleaned_results,
                'scanTime': f'{(time.time() - t0):.1f}s',
                'source': source
            }
            
            save_trend_scan_results(result)
            _last_scan_result = result
            _last_scan_date = today
            _last_scan_ts = time.time()
            print(f'[趋势扫描] 完成，发现 {len(trend_scan_results)} 只趋势股，耗时 {result["scanTime"]}')
            
            return {"success": True, "data": _ensure_today_date(result)}
        except Exception as error:
            print(f'趋势扫描失败: {str(error)}')
            raise HTTPException(status_code=500, detail="趋势扫描失败")

# ========== AI 诊断功能 ==========

def _calc_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def _calc_kdj(kline_data: List[List], n: int = 9) -> Dict:
    if len(kline_data) < n:
        return {'K': None, 'D': None, 'J': None}
    closes = [float(k[2]) for k in kline_data]
    highs = [float(k[3]) for k in kline_data]
    lows = [float(k[4]) for k in kline_data]
    recent_closes = closes[-n:]
    recent_highs = highs[-n:]
    recent_lows = lows[-n:]
    hn = max(recent_highs)
    ln = min(recent_lows)
    rsv = ((closes[-1] - ln) / (hn - ln) * 100) if (hn - ln) > 0 else 50
    k = 2 / 3 * 50 + 1 / 3 * rsv
    d = 2 / 3 * 50 + 1 / 3 * k
    j = 3 * k - 2 * d
    return {'K': round(k, 2), 'D': round(d, 2), 'J': round(j, 2)}

def _calc_macd(closes: List[float], short: int = 12, long: int = 26, signal: int = 9) -> Dict:
    if len(closes) < long + signal:
        return {'dif': None, 'dea': None, 'macd': None}
    ema_short = closes[0]
    ema_long = closes[0]
    dif_list = []
    for c in closes[1:]:
        ema_short = (2 / (short + 1)) * c + (short - 1) / (short + 1) * ema_short
        ema_long = (2 / (long + 1)) * c + (long - 1) / (long + 1) * ema_long
        dif_list.append(ema_short - ema_long)
    dea = dif_list[0]
    for d in dif_list[1:]:
        dea = (2 / (signal + 1)) * d + (signal - 1) / (signal + 1) * dea
    macd_val = 2 * (dif_list[-1] - dea)
    return {
        'dif': round(dif_list[-1], 3),
        'dea': round(dea, 3),
        'macd': round(macd_val, 3)
    }

def _detect_kline_pattern(kline_data: List[List]) -> List[str]:
    patterns = []
    if len(kline_data) < 3:
        return patterns
    recent = kline_data[-3:]
    c0 = [float(recent[0][2]), float(recent[0][3]), float(recent[0][4]), float(recent[0][1])]
    c1 = [float(recent[1][2]), float(recent[1][3]), float(recent[1][4]), float(recent[1][1])]
    c2 = [float(recent[2][2]), float(recent[2][3]), float(recent[2][4]), float(recent[2][1])]
    body2 = abs(c2[0] - c2[3])
    upper2 = c2[1] - max(c2[0], c2[3])
    lower2 = min(c2[0], c2[3]) - c2[2]
    total2 = c2[1] - c2[2] if c2[1] != c2[2] else 0.01
    if body2 / total2 < 0.15 and upper2 > body2 * 2 and lower2 > body2 * 2:
        patterns.append('十字星(变盘信号)')
    if c2[0] > c2[3] and c1[0] > c1[3] and c0[0] < c0[3]:
        if c0[3] > c1[0]:
            patterns.append('看跌吞没(看空)')
    if c2[0] < c2[3] and c1[0] < c1[3] and c0[0] > c0[3]:
        if c0[3] < c1[0]:
            patterns.append('看涨吞没(看多)')
    if c2[0] > c2[3] and c1[0] < c1[3] and c0[0] < c0[3]:
        if c1[3] < c2[3] and c0[3] < c2[3]:
            patterns.append('黄昏之星(看空)')
    if c2[0] < c2[3] and c1[0] > c1[3] and c0[0] > c0[3]:
        if c1[3] > c2[3] and c0[3] > c2[3]:
            patterns.append('晨星(看多)')
    if c2[0] > c2[3] and body2 / total2 > 0.6 and lower2 < body2 * 0.1:
        patterns.append('光头大阳线(强势)')
    if c2[0] < c2[3] and body2 / total2 > 0.6 and upper2 < body2 * 0.1:
        patterns.append('光头大阴线(弱势)')
    return patterns

def _calc_support_resistance(kline_data: List[List]) -> Dict:
    if len(kline_data) < 10:
        return {'support': [], 'resistance': []}
    closes = [float(k[2]) for k in kline_data]
    highs = [float(k[3]) for k in kline_data]
    lows = [float(k[4]) for k in kline_data]
    recent = kline_data[-30:] if len(kline_data) >= 30 else kline_data
    current_price = closes[-1]
    supports = []
    resistances = []
    for i in range(1, len(recent) - 1):
        lo = float(recent[i][4])
        hi = float(recent[i][3])
        prev_lo = float(recent[i - 1][4])
        next_lo = float(recent[i + 1][4]) if i + 1 < len(recent) else lo
        prev_hi = float(recent[i - 1][3])
        next_hi = float(recent[i + 1][3]) if i + 1 < len(recent) else hi
        if lo < prev_lo and lo < next_lo and lo < current_price:
            supports.append(round(lo, 2))
        if hi > prev_hi and hi > next_hi and hi > current_price:
            resistances.append(round(hi, 2))
    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else None
    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
    if ma5 and ma5 < current_price:
        supports.append(round(ma5, 2))
    elif ma5 and ma5 > current_price:
        resistances.append(round(ma5, 2))
    if ma10:
        if ma10 < current_price:
            supports.append(round(ma10, 2))
        else:
            resistances.append(round(ma10, 2))
    if ma20:
        if ma20 < current_price:
            supports.append(round(ma20, 2))
        else:
            resistances.append(round(ma20, 2))
    supports = sorted(set(supports), reverse=True)[:3]
    resistances = sorted(set(resistances))[:3]
    return {'support': supports, 'resistance': resistances}

def _build_diagnosis_prompt(stock_info: Dict, kline_data: List, market_data: Dict) -> str:
    name = stock_info.get('name', '')
    symbol = stock_info.get('symbol', '')
    price = stock_info.get('price', 0)
    change_pct = stock_info.get('changePercent', 0)
    open_price = stock_info.get('open', 0)
    high = stock_info.get('high', 0)
    low = stock_info.get('low', 0)
    volume = stock_info.get('volume', 0)
    turnover = stock_info.get('turnover', 0)
    turnover_rate = stock_info.get('turnoverRate', 0)
    volume_ratio = stock_info.get('volumeRatio', 0)
    pe = stock_info.get('pe', 0)
    outer_plate = stock_info.get('outerPlate', 0)
    inner_plate = stock_info.get('innerPlate', 0)
    weibi = stock_info.get('weibi', 0)
    amplitude = stock_info.get('amplitude', 0)
    avg_price = stock_info.get('avgPrice', 0)
    avg_price_deviation = stock_info.get('avgPriceDeviation', 0)
    circulate_market_cap = stock_info.get('circulateMarketCap', 0)
    bid1 = stock_info.get('bid1', 0)
    bid1_vol = stock_info.get('bid1Vol', 0)
    ask1 = stock_info.get('ask1', 0)
    ask1_vol = stock_info.get('ask1Vol', 0)
    yesterday_close = stock_info.get('yesterdayClose', 0)

    outer_ratio = (outer_plate / (outer_plate + inner_plate) * 100) if (outer_plate + inner_plate) > 0 else 50
    plate_diff = outer_plate - inner_plate

    tech_indicators = ""
    sr_levels = {'support': [], 'resistance': []}
    kline_patterns = []
    if kline_data and len(kline_data) > 0:
        closes = [float(k[2]) for k in kline_data]
        recent = kline_data[-20:] if len(kline_data) >= 20 else kline_data
        kline_summary = "\n".join([
            f"  {k[0]}: 开{k[1]} 收{k[2]} 高{k[3]} 低{k[4]} 量{k[5] if len(k) > 5 else '-'}"
            for k in recent
        ])
        ma5_vals = calculate_ma(kline_data, 5)
        ma10_vals = calculate_ma(kline_data, 10)
        ma20_vals = calculate_ma(kline_data, 20)
        ma60_vals = calculate_ma(kline_data, 60)
        latest_idx = len(kline_data) - 1
        kline_summary += f"\n  MA5={ma5_vals[latest_idx]} MA10={ma10_vals[latest_idx]} MA20={ma20_vals[latest_idx]} MA60={ma60_vals[latest_idx] if ma60_vals[latest_idx] else 'N/A'}"

        consecutive_up = 0
        for i in range(latest_idx, 0, -1):
            if closes[i] > closes[i - 1]:
                consecutive_up += 1
            else:
                break
        consecutive_down = 0
        for i in range(latest_idx, 0, -1):
            if closes[i] < closes[i - 1]:
                consecutive_down += 1
            else:
                break
        kline_summary += f"\n  连涨天数={consecutive_up} 连跌天数={consecutive_down}"

        rsi = _calc_rsi(closes)
        kdj = _calc_kdj(kline_data)
        macd = _calc_macd(closes)
        sr_levels = _calc_support_resistance(kline_data)
        kline_patterns = _detect_kline_pattern(kline_data)

        tech_indicators = f"""
## 技术指标
- RSI(14): {rsi if rsi is not None else 'N/A'}
- KDJ: K={kdj['K']} D={kdj['D']} J={kdj['J']}
- MACD: DIF={macd['dif']} DEA={macd['dea']} MACD柱={macd['macd']}
- K线形态: {', '.join(kline_patterns) if kline_patterns else '无明显形态'}
- 支撑位: {', '.join(map(str, sr_levels['support'])) if sr_levels['support'] else '暂无'}
- 压力位: {', '.join(map(str, sr_levels['resistance'])) if sr_levels['resistance'] else '暂无'}
"""
    else:
        kline_summary = "  无K线数据"

    market_summary = ""
    market_trend = ""
    for key, info in market_data.items():
        m_name = info.get('name', key)
        m_price = info.get('price', 0)
        m_change = info.get('changePercent', 0)
        market_summary += f"\n  {m_name}: {m_price} ({m_change:+.2f}%)"
    market_changes = [info.get('changePercent', 0) for info in market_data.values()]
    if market_changes:
        avg_market = sum(market_changes) / len(market_changes)
        if avg_market > 0.5:
            market_trend = "大盘强势上涨，市场情绪偏多"
        elif avg_market > 0:
            market_trend = "大盘小幅上涨，市场情绪中性偏多"
        elif avg_market > -0.5:
            market_trend = "大盘小幅下跌，市场情绪中性偏空"
        else:
            market_trend = "大盘明显下跌，市场情绪偏空"

    prompt = f"""你是一位资深的A股量化分析师，擅长从多维度综合分析个股走势，给出有实战价值的操作建议。

## 股票基本信息
- 名称: {name} ({symbol})
- 当前价: {price} | 昨收: {yesterday_close}
- 涨跌幅: {change_pct:+.2f}% | 振幅: {amplitude:.2f}%
- 今开: {open_price} | 最高: {high} | 最低: {low}

## 成交数据
- 成交量: {volume}手 | 成交额: {turnover/10000:.2f}万
- 换手率: {turnover_rate:.2f}% | 量比: {volume_ratio:.2f}
- 流通市值: {circulate_market_cap/10000:.2f}亿

## 主力资金数据
- 外盘(主动买): {outer_plate} | 内盘(主动卖): {inner_plate}
- 外盘占比: {outer_ratio:.1f}% | 内外盘差: {plate_diff}
- 委比: {weibi:+.2f}%（注意：委比可能存在虚假挂单，需结合涨跌和量比验证）
- 买一: {bid1}({bid1_vol}手) | 卖一: {ask1}({ask1_vol}手)

## 均价分析
- 均价: {avg_price} | 偏离均价: {avg_price_deviation:+.2f}%

## 近20日K线数据
{kline_summary}
{tech_indicators}
## 大盘环境
{market_summary}
市场整体判断: {market_trend}

## 基本面
- 市盈率: {pe if pe > 0 else '亏损'}

---

## 分析要求

请严格按照以下JSON格式输出分析结果（不要输出其他内容）：

{{
  "direction": "方向，必须是以下之一：强烈买入/买入/轻仓关注/观望/减仓/卖出",
  "confidence": 75,
  "summary": "一句话总结核心观点，要具体有数据支撑",
  "scores": {{
    "volume": 0,
    "capital": 0,
    "technique": 0,
    "market": 0,
    "fundamental": 0
  }},
  "analysis": {{
    "volume": "成交量分析：结合量比、换手率、成交额变化，判断资金参与度",
    "capital": "主力资金分析：结合外盘占比、委比（注意虚假挂单）、内外盘差，判断主力意图",
    "technique": "技术面分析：结合均线排列、MACD/RSI/KDJ指标、K线形态、支撑压力位，判断趋势方向",
    "market": "大盘环境分析：大盘走势对个股的影响",
    "fundamental": "基本面分析：市盈率、流通市值的合理性"
  }},
  "keySignals": {{
    "bullish": ["看多信号1", "看多信号2"],
    "bearish": ["看空信号1", "看空信号2"]
  }},
  "triggerCondition": "转为买入/加仓的具体条件（如：放量突破XX元、RSI回落至30以下等）",
  "risk": "主要风险提示",
  "suggestion": "具体操作建议（含仓位比例和关键价位）"
}}

## 重要规则：
1. direction不要默认给"观望"，必须根据数据给出有倾向性的判断
2. 如果多数指标偏多但有个别风险，应给"买入"或"轻仓关注"而非"观望"
3. 如果连跌多日出现缩量企稳+RSI超卖，应给"轻仓关注"而非"卖出"
4. 如果放量上涨+外盘占优+均线多头，应给"买入"或"强烈买入"
5. scores中每项0-100分，50为中性，>60偏多，<40偏空
6. keySignals必须列出至少1个看多和1个看空信号
7. triggerCondition必须给出具体的价位或指标条件"""

    return prompt

async def _call_deepseek_api(prompt: str) -> str:
    if not DEEPSEEK_API_KEY:
        raise HTTPException(status_code=400, detail="未配置DeepSeek API Key，请设置环境变量 DEEPSEEK_API_KEY")

    client = get_http_client()
    url = f"{DEEPSEEK_BASE_URL}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": "你是专业的A股量化分析师，擅长根据成交量、主力资金、技术面、基本面和大盘环境给出精准的买卖建议。请始终以JSON格式回复。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3,
        "max_tokens": 1500,
        "response_format": {"type": "json_object"}
    }

    try:
        response = await client.post(url, headers=headers, json=payload, timeout=30.0)
        response.raise_for_status()
        result = response.json()
        content = result['choices'][0]['message']['content']
        return content
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="AI诊断超时，请稍后重试")
    except httpx.HTTPStatusError as e:
        detail = f"DeepSeek API错误: {e.response.status_code}"
        try:
            err_body = e.response.json()
            detail = f"DeepSeek API错误: {err_body.get('error', {}).get('message', str(e.response.status_code))}"
        except:
            pass
        raise HTTPException(status_code=502, detail=detail)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI诊断失败: {str(e)}")

@app.get("/api/ai-diagnose/{symbol}", tags=["AI诊断"])
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
            except:
                market_data[key] = {'name': info['name'], 'price': 0, 'changePercent': 0}

        prompt = _build_diagnosis_prompt(stock_info, kline_data, market_data)
        ai_result = await _call_deepseek_api(prompt)

        try:
            diagnosis = json.loads(ai_result)
        except:
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

@app.get("/api/ai-config", tags=["AI诊断"])
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

@app.get("/api/stock-map", tags=["股票映射"])
async def get_stock_basic_info_api():
    try:
        stock_basic_info = load_stock_basic_info()
        name_search = load_stock_alias_map()
        return {"success": True, "data": stock_basic_info, "nameSearch": name_search}
    except Exception as error:
        raise HTTPException(status_code=500, detail="获取股票映射失败")

@app.post("/api/update-stock-map", tags=["股票映射"])
async def update_stock_basic_info_api(data: Dict):
    global _stock_basic_info_cache
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
        _stock_basic_info_cache = stock_basic_info
        
        return {"success": True}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))

@app.get("/api/custom-scan-pool", tags=["自定义扫描池"])
async def get_user_scan_pool_api():
    try:
        symbols = load_user_scan_pool()
        return {"success": True, "data": {"symbols": symbols, "count": len(symbols)}}
    except Exception as error:
        raise HTTPException(status_code=500, detail="获取自定义扫描池失败")

@app.post("/api/custom-scan-pool", tags=["自定义扫描池"])
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

@app.put("/api/custom-scan-pool/add", tags=["自定义扫描池"])
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

@app.put("/api/custom-scan-pool/remove", tags=["自定义扫描池"])
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

@app.post("/api/custom-scan-pool/check-ma10", tags=["自定义扫描池"])
async def check_ma10_api():
    """手动触发10日线检查并删除跌破股票"""
    try:
        print('[手动触发] 开始执行10日线检查...')
        
        # 加载股票池
        symbols = load_user_scan_pool()
        if not symbols:
            return {
                "success": True, 
                "data": {
                    "message": "股票池为空，无需检查",
                    "removed": [],
                    "kept": [],
                    "details": []
                }
            }
        
        removed_symbols = []
        kept_symbols = []
        details = []
        
        # 逐个检查股票
        for symbol in symbols:
            try:
                # 获取K线数据
                kline_data = await get_kline_data(symbol.lower())
                if not kline_data:
                    stock_name = get_cached_stock_name(symbol) or symbol
                    detail = {
                        "symbol": symbol,
                        "name": stock_name,
                        "shouldRemove": False,
                        "reason": "获取K线数据失败，跳过"
                    }
                    details.append(detail)
                    kept_symbols.append(symbol)
                    continue
                
                # 检查是否跌破10日线
                result = check_below_ma10(kline_data)
                stock_name = get_cached_stock_name(symbol) or symbol
                
                detail = {
                    "symbol": symbol,
                    "name": stock_name,
                    "shouldRemove": result['shouldRemove'],
                    "reason": result['reason'],
                    "latestClose": result['latestClose'],
                    "ma10": result['ma10']
                }
                details.append(detail)
                
                if result['shouldRemove']:
                    removed_symbols.append(symbol)
                else:
                    kept_symbols.append(symbol)
                
            except Exception as e:
                stock_name = get_cached_stock_name(symbol) or symbol
                detail = {
                    "symbol": symbol,
                    "name": stock_name,
                    "shouldRemove": False,
                    "reason": f"检查失败: {str(e)}"
                }
                details.append(detail)
                kept_symbols.append(symbol)
                continue
        
        # 保存更新后的股票池
        if removed_symbols:
            save_user_scan_pool(kept_symbols)
            message = f"扫描完成！共删除{len(removed_symbols)}只股票，保留{len(kept_symbols)}只股票"
        else:
            message = "扫描完成！没有需要删除的股票"
        
        return {
            "success": True,
            "data": {
                "message": message,
                "removed": removed_symbols,
                "kept": kept_symbols,
                "details": details
            }
        }
        
    except Exception as error:
        print(f'[手动触发] 10日线检查失败: {str(error)}')
        raise HTTPException(status_code=500, detail=str(error))

@app.delete("/api/custom-scan-pool", tags=["自定义扫描池"])
async def clear_user_scan_pool_api():
    try:
        save_user_scan_pool([])
        return {"success": True, "message": "自定义扫描池已清空"}
    except Exception as error:
        raise HTTPException(status_code=500, detail="清空自定义扫描池失败")

@app.get("/api/hot-stocks-scan", tags=["股票扫描"])
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
        
        batch_size = 20
        all_results = []
        
        for i in range(0, len(codes_list), batch_size):
            batch = codes_list[i:i + batch_size]
            symbols = ','.join(batch)
            
            try:
                data = await fetch_with_retry(symbols)
                parsed = parse_tencent_batch_data(data)
                all_results.extend(parsed)
                print(f'  [批次 {i // batch_size + 1}] {len(parsed)} 只成功')
            except Exception as e:
                print(f'  [批次 {i // batch_size + 1}] 失败: {str(e)}')
        
        for stock in all_results:
            stock['score'] = calc_stock_score_v2(stock)
        
        all_results.sort(key=lambda x: x['score']['total'], reverse=True)
        
        # 过滤掉60分以下的数据
        all_results = [s for s in all_results if s['score']['total'] >= 60]
        
        # 获取大盘涨跌用于预测记录
        market_change = 0.0
        try:
            idx_data = await fetch_stock_data('sh000001')
            if idx_data:
                market_change = idx_data.get('parsed', {}).get('changePercent', 0)
        except Exception:
            pass
        
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

@app.get("/api/hot-stocks", tags=["热门股票"])
async def get_hot_stock_buttons_api():
    try:
        stocks = load_hot_stock_buttons()
        return {"success": True, "data": {"stocks": stocks, "count": len(stocks)}}
    except Exception as error:
        raise HTTPException(status_code=500, detail="获取热门股票失败")

@app.post("/api/hot-stocks", tags=["热门股票"])
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

@app.put("/api/hot-stocks/add", tags=["热门股票"])
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

@app.put("/api/hot-stocks/remove", tags=["热门股票"])
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
@app.get("/api/stock-tags", tags=["股票标签"])
async def get_stock_concept_tags_api():
    try:
        tags = load_stock_concept_tags()
        return {"success": True, "data": {"tags": tags, "count": len(tags)}}
    except Exception as error:
        raise HTTPException(status_code=500, detail="获取股票标签失败")

@app.get("/api/stock-tags/{symbol}", tags=["股票标签"])
async def get_stock_concept_tags_by_symbol_api(symbol: str):
    try:
        tags = load_stock_concept_tags()
        return {"success": True, "data": {"symbol": symbol, "tags": tags.get(symbol.lower(), [])}}
    except Exception as error:
        raise HTTPException(status_code=500, detail="获取股票标签失败")

@app.post("/api/stock-tags/{symbol}", tags=["股票标签"])
async def add_stock_concept_tags_api(symbol: str, data: Dict):
    try:
        tags_to_add = data.get('tags', [])
        if not isinstance(tags_to_add, list):
            tags_to_add = [str(tags_to_add)]
        
        updated_tags = add_stock_concept_tags(symbol, tags_to_add)
        return {"success": True, "data": {"symbol": symbol, "tags": updated_tags}}
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))

@app.delete("/api/stock-tags/{symbol}", tags=["股票标签"])
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

@app.get("/api/stock-concepts/{symbol}", tags=["股票标签"])
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

async def get_market_index(index_symbol: str) -> Optional[Dict]:
    """获取单个大盘指数数据"""
    try:
        data = await fetch_with_retry(index_symbol)
        match = re.search(r'v_\w+="([^"]+)"', data)
        if match:
            fields = match.group(1).split('~')
            if len(fields) > 40:
                return {
                    'symbol': index_symbol,
                    'name': fields[1],
                    'price': float(fields[3]) if fields[3] else 0,
                    'yesterdayClose': float(fields[4]) if fields[4] else 0,
                    'open': float(fields[5]) if fields[5] else 0,
                    'high': float(fields[33]) if fields[33] else 0,
                    'low': float(fields[34]) if fields[34] else 0,
                    'change': float(fields[31]) if fields[31] else 0,
                    'changePercent': float(fields[32]) if fields[32] else 0,
                    'volume': float(fields[36]) if fields[36] else 0
                }
    except Exception as e:
        print(f'[大盘指数] 获取 {index_symbol} 失败: {str(e)}')
    return None

async def check_market_crash() -> Dict:
    """检查大盘是否大跌"""
    indexes = []
    any_crash = False
    any_severe_crash = False
    
    for key, info in INDEX_SYMBOLS.items():
        index_data = await get_market_index(info['symbol'])
        if index_data:
            indexes.append(index_data)
            # 检查跌幅
            if index_data['changePercent'] <= MARKET_CRASH_THRESHOLD:
                any_crash = True
            if index_data['changePercent'] <= MARKET_SEVERE_CRASH_THRESHOLD:
                any_severe_crash = True
    
    # 判断整体市场状态
    market_status = 'normal'
    crash_level = 'none'
    
    if any_severe_crash:
        market_status = 'severe_crash'
        crash_level = 'severe'
    elif any_crash:
        market_status = 'crash'
        crash_level = 'moderate'
    
    # 生成建议
    suggestion = get_crash_suggestion(market_status, indexes)
    
    return {
        'updateTime': datetime.now().isoformat(),
        'marketStatus': market_status,
        'crashLevel': crash_level,
        'indexes': indexes,
        'suggestion': suggestion,
        'thresholds': {
            'crash': MARKET_CRASH_THRESHOLD,
            'severeCrash': MARKET_SEVERE_CRASH_THRESHOLD
        }
    }

def get_crash_suggestion(market_status: str, indexes: List[Dict]) -> Dict:
    """根据市场状态生成建议"""
    if market_status == 'normal':
        return {
            'level': 'info',
            'title': '市场正常',
            'message': '当前市场运行平稳，可以正常操作。',
            'action': '保持观望或按计划操作',
            'shouldSell': False,
            'urgency': 0
        }
    elif market_status == 'crash':
        return {
            'level': 'warning',
            'title': '⚠️ 市场大幅下跌',
            'message': '大盘出现明显下跌，建议控制仓位，注意风险。',
            'action': '考虑减仓或观望',
            'shouldSell': True,
            'urgency': 1
        }
    elif market_status == 'severe_crash':
        return {
            'level': 'danger',
            'title': '🚨 紧急警报：市场暴跌！',
            'message': '大盘出现严重下跌，建议立即清仓避险！',
            'action': '紧急清仓！',
            'shouldSell': True,
            'urgency': 2
        }
    else:
        return {
            'level': 'info',
            'title': '市场状态未知',
            'message': '无法获取市场状态。',
            'action': '保持观望',
            'shouldSell': False,
            'urgency': 0
        }

# ========== 大盘预测系统（主力资金流向预测） ==========

PREDICT_SEMAPHORE = asyncio.Semaphore(5)

async def predict_market_risk() -> Dict:
    """基于全市场股票数据预测大盘风险（主力资金流向预测）"""
    # 收集所有可用的股票池数据
    hot_search_ranking_data = load_hot_search_ranking()
    hot_stock_buttons = load_hot_stock_buttons()
    user_scan_pool = load_user_scan_pool()
    stock_basic_info = load_stock_basic_info()
    
    # 合并所有数据源，去重
    all_stocks = {}
    
    # 从hot_search_ranking添加
    for s in hot_search_ranking_data.get('stocks', []):
        all_stocks[s['symbol'].lower()] = s['symbol']
    
    # 从hot_stock_buttons添加
    for s in hot_stock_buttons:
        symbol = s.get('code', '').lower()
        if symbol and re.match(r'^(sh|sz)\d{6}$', symbol):
            all_stocks[symbol] = symbol
    
    # 从user_scan_pool添加
    for symbol in user_scan_pool:
        if re.match(r'^(sh|sz)\d{6}$', symbol):
            all_stocks[symbol.lower()] = symbol.lower()
    
    # 从stock_basic_info添加
    for symbol in stock_basic_info.keys():
        if re.match(r'^(sh|sz)\d{6}$', symbol):
            all_stocks[symbol.lower()] = symbol.lower()
    
    # 转换为列表
    stocks = list(all_stocks.values())
    
    if not stocks:
        # 如果没有数据，回到hot_search_ranking
        hot_search_ranking_stocks = hot_search_ranking_data.get('stocks', [])
        if not hot_search_ranking_stocks:
            return {
                'updateTime': datetime.now().isoformat(),
                'predictStatus': 'unknown',
                'riskScore': 0,
                'prediction': {
                    'level': 'info',
                    'title': '数据不足',
                    'message': '无法获取足够的股票数据进行预测',
                    'action': '稍后再试',
                    'shouldSell': False,
                    'urgency': 0
                },
                'signals': {},
                'details': {}
            }
        stocks = [s['symbol'] for s in hot_search_ranking_stocks]
    
    # 分批获取实时数据（最多取500只代表性股票）
    all_symbols = stocks[:500]
    batch_size = 20
    all_data = []
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        for i in range(0, len(all_symbols), batch_size):
            batch = all_symbols[i:i + batch_size]
            try:
                symbols_str = ','.join(batch)
                raw_data = await fetch_with_retry(symbols_str, client)
                parsed = parse_tencent_batch_data(raw_data)
                all_data.extend(parsed)
            except Exception as e:
                print(f'[预测扫描] 批次 {i // batch_size + 1} 失败: {str(e)}')
                continue
    
    if not all_data:
        return {
            'updateTime': datetime.now().isoformat(),
            'predictStatus': 'unknown',
            'riskScore': 0,
            'prediction': {
                'level': 'info',
                'title': '数据不足',
                'message': '无法获取实时数据进行预测',
                'action': '稍后再试',
                'shouldSell': False,
                'urgency': 0
            },
            'signals': {},
            'details': {}
        }
    
    total = len(all_data)
    
    # === 信号1: 内外盘比（主力资金流向）===
    outer_lt_inner_count = 0
    outer_sum = 0
    inner_sum = 0
    outer_ratio_total = 0
    for s in all_data:
        outer = s.get('outerPlateRaw', 0)
        inner = s.get('innerPlateRaw', 0)
        outer_sum += outer
        inner_sum += inner
        if outer + inner > 0:
            ratio = (outer / (outer + inner)) * 100
            outer_ratio_total += ratio
            if ratio < 45:
                outer_lt_inner_count += 1
    
    avg_outer_ratio = outer_ratio_total / total if total > 0 else 50
    outer_weak_ratio = (outer_lt_inner_count / total * 100) if total > 0 else 0
    total_outer_inner_ratio = (outer_sum / (outer_sum + inner_sum) * 100) if (outer_sum + inner_sum) > 0 else 50
    
    # === 信号2: 涨跌分布（市场情绪）===
    up_count = 0
    down_count = 0
    heavy_down_count = 0
    mild_down_count = 0
    
    for s in all_data:
        cp = s.get('changePercent', 0)
        if cp > 0:
            up_count += 1
        elif cp < 0:
            down_count += 1
            if cp <= -3:
                heavy_down_count += 1
            elif cp <= -1:
                mild_down_count += 1
    
    up_ratio = (up_count / total * 100) if total > 0 else 50
    down_ratio = (down_count / total * 100) if total > 0 else 50
    heavy_down_ratio = (heavy_down_count / total * 100) if total > 0 else 0
    mild_down_ratio = (mild_down_count / total * 100) if total > 0 else 0
    
    # === 信号3: 放量下跌（主力出货信号）===
    volume_dump_count = 0
    for s in all_data:
        cp = s.get('changePercent', 0)
        vr = s.get('volumeRatio', 0)
        if cp < -1 and vr > 1.5:
            volume_dump_count += 1
        elif cp < -2 and vr > 1.0:
            volume_dump_count += 1
    
    volume_dump_ratio = (volume_dump_count / total * 100) if total > 0 else 0
    
    # === 信号4: 委比分布（卖压分析）===
    weibi_neg_count = 0
    weibi_heavy_neg_count = 0
    weibi_total = 0
    
    for s in all_data:
        wb = s.get('weibi', 0)
        weibi_total += wb
        if wb < -20:
            weibi_neg_count += 1
            if wb < -40:
                weibi_heavy_neg_count += 1
    
    avg_weibi = weibi_total / total if total > 0 else 0
    weibi_neg_ratio = (weibi_neg_count / total * 100) if total > 0 else 0
    weibi_heavy_neg_ratio = (weibi_heavy_neg_count / total * 100) if total > 0 else 0
    
    # === 综合风险评分 ===
    # 先判断是否为交易时间
    def is_trading_time():
        now = datetime.now()
        hour = now.hour
        minute = now.minute
        day = now.weekday()
        
        # 周末休市
        if day == 5 or day == 6:
            return False
        
        # 交易时间: 9:30-11:30, 13:00-15:00
        if (hour == 9 and minute >= 30) or (hour == 10) or (hour == 11 and minute < 30):
            return True
        if hour == 13 or hour == 14 or (hour == 15 and minute == 0):
            return True
        
        return False
    
    # 非交易时间降低风险评分
    if not is_trading_time():
        return {
            'updateTime': datetime.now().isoformat(),
            'predictStatus': 'safe',
            'riskScore': 0,
            'prediction': {
                'level': 'info',
                'title': '✅ 当前非交易时间',
                'message': '市场休市中，主力资金流向分析暂停。交易时间将自动恢复监控。',
                'action': '正常操作',
                'shouldSell': False,
                'urgency': 0
            },
            'signals': {},
            'details': {
                'totalStocks': total,
                'upCount': up_count,
                'downCount': down_count,
                'heavyDownCount': heavy_down_count,
                'mildDownCount': mild_down_count,
                'volumeDumpCount': volume_dump_count,
                'outerWeakCount': outer_lt_inner_count,
                'avgOuterRatio': round(avg_outer_ratio, 1),
                'totalOuterInnerRatio': round(total_outer_inner_ratio, 1),
                'avgWeibi': round(avg_weibi, 1),
                'weibiNegCount': weibi_neg_count,
                'weibiHeavyNegCount': weibi_heavy_neg_count
            }
        }
    
    # 信号1: 内外盘比 (权重35) - 最核心的主力资金指标
    score1 = 0
    if total_outer_inner_ratio < 40:
        score1 = 35
    elif total_outer_inner_ratio < 43:
        score1 = 28
    elif total_outer_inner_ratio < 45:
        score1 = 20
    elif total_outer_inner_ratio < 47:
        score1 = 12
    elif total_outer_inner_ratio < 49:
        score1 = 5
    
    # 信号2: 涨跌分布 (权重25) - 市场整体情绪
    score2 = 0
    if heavy_down_ratio > 12:
        score2 = 25
    elif heavy_down_ratio > 8:
        score2 = 20
    elif heavy_down_ratio > 5:
        score2 = 15
    elif down_ratio > 60:
        score2 = 18
    elif down_ratio > 50:
        score2 = 10
    elif down_ratio > 40:
        score2 = 5
    elif down_ratio == 0 and up_ratio == 100:
        # 非交易时间所有股票都显示上涨，不加分
        score2 = 0
    
    # 信号3: 放量下跌 (权重25) - 主力出货最直接信号
    score3 = 0
    if volume_dump_ratio > 10:
        score3 = 25
    elif volume_dump_ratio > 6:
        score3 = 20
    elif volume_dump_ratio > 4:
        score3 = 15
    elif volume_dump_ratio > 2:
        score3 = 10
    elif volume_dump_ratio > 1:
        score3 = 5
    elif volume_dump_ratio == 0 and down_ratio == 0:
        # 非交易时间没有成交量，不加分
        score3 = 0
    
    # 信号4: 委比分布 (权重15) - 卖压提前信号
    score4 = 0
    if weibi_heavy_neg_ratio > 15:
        score4 = 15
    elif weibi_heavy_neg_ratio > 8:
        score4 = 12
    elif weibi_neg_ratio > 35:
        score4 = 10
    elif weibi_neg_ratio > 25:
        score4 = 5
    elif weibi_neg_ratio > 15:
        score4 = 3
    elif weibi_neg_ratio == 0 and avg_weibi > 90:
        # 非交易时间委比异常高，不加分
        score4 = 0
    
    risk_score = score1 + score2 + score3 + score4
    
    # 判断预测状态（更灵敏的阈值）
    if risk_score >= 60:
        predict_status = 'severe_danger'
        prediction = {
            'level': 'danger',
            'title': '🚨 紧急警报：主力资金正在疯狂出逃！',
            'message': f'检测到强烈的主力出货信号：放量下跌股票占比{volume_dump_ratio:.1f}%，外盘/内盘比仅{total_outer_inner_ratio:.1f}%，卖压严重。预计大盘即将大跌，建议立即清仓避险！',
            'action': '立即清仓！',
            'shouldSell': True,
            'urgency': 2
        }
    elif risk_score >= 40:
        predict_status = 'danger'
        prediction = {
            'level': 'danger',
            'title': '⚠️ 预警：主力资金持续流出，风险加剧',
            'message': f'主力资金净流出明显：{volume_dump_ratio:.1f}%的股票出现放量下跌，外盘/内盘比{total_outer_inner_ratio:.1f}%，{weibi_neg_ratio:.1f}%的股票卖盘占优。建议大幅减仓控制风险。',
            'action': '大幅减仓',
            'shouldSell': True,
            'urgency': 1
        }
    elif risk_score >= 20:
        predict_status = 'warning'
        prediction = {
            'level': 'warning',
            'title': '👀 关注：主力资金有流出迹象',
            'message': f'市场出现初步风险信号：{down_ratio:.1f}%的股票下跌，{volume_dump_ratio:.1f}%的股票放量下跌。建议减仓观望，不要追高。',
            'action': '减仓观望',
            'shouldSell': True,
            'urgency': 0
        }
    else:
        predict_status = 'safe'
        prediction = {
            'level': 'info',
            'title': '✅ 市场风险较低',
            'message': f'当前主力资金流向正常，{up_ratio:.1f}%的股票上涨，市场运行平稳。',
            'action': '正常操作',
            'shouldSell': False,
            'urgency': 0
        }
    
    return {
        'updateTime': datetime.now().isoformat(),
        'predictStatus': predict_status,
        'riskScore': risk_score,
        'prediction': prediction,
        'signals': {
            'capitalFlow': {
                'name': '主力资金流向',
                'value': f'{total_outer_inner_ratio:.1f}%',
                'detail': f'外盘/内盘比 {total_outer_inner_ratio:.1f}%（<45%为出货信号）',
                'score': score1,
                'maxScore': 30,
                'status': 'danger' if score1 >= 20 else 'warning' if score1 >= 10 else 'safe'
            },
            'upDownRatio': {
                'name': '涨跌分布',
                'value': f'{up_ratio:.1f}% / {down_ratio:.1f}%',
                'detail': f'上涨 {up_count} 只 / 下跌 {down_count} 只（大跌>3%: {heavy_down_count} 只）',
                'score': score2,
                'maxScore': 30,
                'status': 'danger' if score2 >= 20 else 'warning' if score2 >= 10 else 'safe'
            },
            'volumeDump': {
                'name': '放量下跌（主力出货）',
                'value': f'{volume_dump_ratio:.1f}%',
                'detail': f'{volume_dump_count} 只股票放量下跌（量比>1.5且跌幅>1%）',
                'score': score3,
                'maxScore': 25,
                'status': 'danger' if score3 >= 15 else 'warning' if score3 >= 10 else 'safe'
            },
            'weibi': {
                'name': '委比分布（卖压）',
                'value': f'{weibi_neg_ratio:.1f}%',
                'detail': f'{weibi_neg_count} 只股票委比小于-20，平均委比 {avg_weibi:.1f}',
                'score': score4,
                'maxScore': 15,
                'status': 'danger' if score4 >= 10 else 'warning' if score4 >= 5 else 'safe'
            }
        },
        'details': {
            'totalStocks': total,
            'upCount': up_count,
            'downCount': down_count,
            'heavyDownCount': heavy_down_count,
            'mildDownCount': mild_down_count,
            'volumeDumpCount': volume_dump_count,
            'outerWeakCount': outer_lt_inner_count,
            'avgOuterRatio': round(avg_outer_ratio, 1),
            'totalOuterInnerRatio': round(total_outer_inner_ratio, 1),
            'avgWeibi': round(avg_weibi, 1),
            'weibiNegCount': weibi_neg_count,
            'weibiHeavyNegCount': weibi_heavy_neg_count
        }
    }

@app.get("/api/market-predict", tags=["大盘预测"])
async def get_market_predict_api():
    """获取大盘预测数据（基于主力资金流向的预测）"""
    try:
        result = await predict_market_risk()
        return {"success": True, "data": result}
    except Exception as error:
        print(f'[大盘预测API] 错误: {str(error)}')
        raise HTTPException(status_code=500, detail="获取大盘预测失败")

@app.get("/api/market-index", tags=["大盘指数"])
async def get_market_index_api():
    """获取大盘指数数据"""
    try:
        result = await check_market_crash()
        return {"success": True, "data": result}
    except Exception as error:
        print(f'[大盘指数API] 错误: {str(error)}')
        raise HTTPException(status_code=500, detail="获取大盘指数失败")

@app.get("/api/kline/{symbol}", tags=["K线数据"])
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
        raise HTTPException(status_code=500, detail="获取K线数据失败")

@app.get("/api/kline/{symbol}/minute", tags=["K线数据"])
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
        raise HTTPException(status_code=500, detail="获取分时数据失败")

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
            async with httpx.AsyncClient(timeout=10) as client:
                # 涨停股
                zt_url = 'https://push2.eastmoney.com/api/qt/clist/get?fid=f3&po=1&pz=30&pn=1&np=1&fltt=2&invt=2&ut=b2884a393a59ad64002292a3e90d46a5&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048&fields=f2,f3,f4,f12,f14,f15,f16,f17,f62,f184,f66,f69,f72,f75,f78,f81,f84,f105,f1'
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
                
                # 跌停股
                dt_url = 'https://push2.eastmoney.com/api/qt/clist/get?fid=f3&po=0&pz=30&pn=1&np=1&fltt=2&invt=2&ut=b2884a393a59ad64002292a3e90d46a5&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048&fields=f2,f3,f4,f12,f14,f15,f16,f17,f62,f184,f66,f69,f72,f75,f78,f81,f84,f105,f1'
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
            cursor = conn.cursor(DictCursor)
            cursor.execute(
                f"SELECT * FROM strategy_predict_records WHERE predict_date = %s AND predict_direction = 'bull' AND score >= {STRATEGY_PREDICT_BULL_MIN_SCORE} ORDER BY score DESC LIMIT {REVIEW_TOMORROW_FOCUS_COUNT}",
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

@app.post("/api/daily-review/generate", tags=["收评"])
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

@app.get("/api/daily-reviews", tags=["收评"])
async def get_daily_market_reviews():
    try:
        reviews = load_daily_market_reviews()
        return {"success": True, "data": reviews}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取收评失败: {str(error)}")

# ========== 策略管理 API ==========

@app.get("/api/strategy/weights", tags=["策略管理"])
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

@app.post("/api/strategy/weights/reset", tags=["策略管理"])
async def reset_strategy_factor_weights():
    """重置策略权重为默认值"""
    try:
        weights = {k: {'weight': v, 'accuracy': 50.0, 'sample_count': 0, 'correct_count': 0}
                   for k, v in DEFAULT_WEIGHTS.items()}
        save_strategy_factor_weights(weights)
        return {"success": True, "message": "策略权重已重置为默认值"}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"重置策略权重失败: {str(error)}")

@app.post("/api/strategy/backtest", tags=["策略管理"])
async def run_backtest():
    """手动触发回测验证"""
    try:
        result = await backtest_yesterday_predictions()
        return {"success": True, "data": result}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"回测失败: {str(error)}")

@app.get("/api/strategy/backtest-history", tags=["策略管理"])
async def get_backtest_history(days: int = STRATEGY_BACKTEST_HISTORY_DAYS):
    """获取回测历史报告"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor(DictCursor)
        cursor.execute(
            'SELECT * FROM strategy_backtest_reports ORDER BY backtest_date DESC LIMIT %s',
            (days,)
        )
        rows = cursor.fetchall()
        conn.close()
        # 解析 JSON 字段
        import json
        for row in rows:
            if row.get('misjudge_analysis'):
                try:
                    parsed = json.loads(row['misjudge_analysis'])
                    if isinstance(parsed, dict) and 'suggestions' in parsed:
                        parsed['suggestions'] = [
                            s.replace('建议增加委比验证条件', '已增加委比交叉验证：委比>30但股价下跌时降分，委比高但外盘占比<48%或量比<0.8时降分')
                            for s in parsed['suggestions']
                        ]
                    row['misjudge_analysis'] = parsed
                except Exception:
                    pass
            if row.get('weight_adjustments'):
                try:
                    row['weight_adjustments'] = json.loads(row['weight_adjustments'])
                except Exception:
                    pass
        return {"success": True, "data": rows}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取回测历史失败: {str(error)}")

@app.get("/api/strategy/predictions", tags=["策略管理"])
async def get_predictions(date_str: str = None, verified: int = None, include_today_and_pending: bool = False):
    """获取预测记录"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor(DictCursor)
        sql = 'SELECT * FROM strategy_predict_records WHERE 1=1'
        params = []
        
        if include_today_and_pending:
            import datetime
            today = datetime.datetime.now().strftime('%Y-%m-%d')
            sql += ' AND (predict_date = %s OR verified = 0)'
            params.append(today)
        else:
            if date_str:
                sql += ' AND predict_date = %s'
                params.append(date_str)
            if verified is not None:
                sql += ' AND verified = %s'
                params.append(verified)
                
        sql += f' ORDER BY predict_date DESC, score DESC LIMIT {STRATEGY_BACKTEST_PREDICTION_LIMIT}'
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        return {"success": True, "data": rows}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取预测记录失败: {str(error)}")

# 最后挂载静态文件，这样API路由会先被匹配到
if os.path.exists(CLIENT_DIR):
    app.mount("/", StaticFiles(directory=CLIENT_DIR, html=True), name="client")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=3003, reload=True)

