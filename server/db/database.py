"""database.py - split from main.py"""
import sqlite3, json, time
from typing import List, Dict, Optional
from datetime import date, timedelta
from app.config import SQLITE_DB_PATH, QUERY_DAILY_RECOMMENDATIONS_LIMIT, QUERY_TREND_RESULTS_LIMIT, QUERY_MARKET_REVIEWS_LIMIT, HOT_SEARCH_TOP_N, HOT_SEARCH_SNAPSHOT_CLEANUP_DAYS, DEFAULT_WEIGHTS

# ========== SQLite 配置 ==========
SQLITE_DB_PATH = SQLITE_DB_PATH

def get_db_conn():
    """获取 SQLite 数据库连接"""
    # isolation_level='IMMEDIATE'：写语句隐式以 BEGIN IMMEDIATE 开启事务，一开始就抢写锁。
    # 避免 DEFERRED 事务「先读后写」升级锁时 SQLite 不重试 busy_timeout 直接返回 SQLITE_BUSY。
    conn = sqlite3.connect(SQLITE_DB_PATH, timeout=30, isolation_level='IMMEDIATE')
    conn.row_factory = sqlite3.Row
    # busy_timeout 必须先于 journal_mode 设置：切换 WAL 本身也需要抢数据库锁。
    # 30s 是上限而非典型等待耗时：定时任务并发时单个写事务可能持续数秒，
    # 原来的 5s 会在这种情况下直接抛 "database is locked"。
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

# ========== 高频读取 TTL 内存缓存（2026-08-31 性能优化） ==========
# 背景：load_stock_basic_info / load_stock_alias_map / load_hot_search_ranking
#       在每次请求中都会被调用，每次都新建 SQLite 连接全表扫描。
#       这些数据更新频率极低（盘后/定时更新），60s 内存缓存可消除绝大部分重复 DB 读。
# 注意：对应的 save_* 函数已做缓存失效，保证写后立即可见。
_DB_TTL_CACHE: Dict[str, Dict] = {}

def _db_cache_get(key: str):
    """命中且在 TTL 内返回缓存数据，否则 None"""
    item = _DB_TTL_CACHE.get(key)
    if item and (time.time() - item['ts']) < item['ttl']:
        return item['data']
    return None

def _db_cache_set(key: str, data, ttl: float = 60.0):
    _DB_TTL_CACHE[key] = {'ts': time.time(), 'ttl': ttl, 'data': data}
    # 惰性清理：超过 200 条时全清（防止内存无限增长）
    if len(_DB_TTL_CACHE) > 200:
        _DB_TTL_CACHE.clear()

def _db_cache_invalidate(key: str):
    _DB_TTL_CACHE.pop(key, None)

# ========== 时间戳 UTC→北京时间 迁移 ==========
# 需要迁移的表及其时间戳列（DEFAULT 曾为 CURRENT_TIMESTAMP=UTC）
_TS_MIGRATION_TABLES = {
    'daily_market_reviews': ['created_at'],
    'trend_scan_results': ['created_at'],
    'hot_stock_buttons': ['created_at', 'updated_at'],
    'stock_alias_map': ['created_at', 'updated_at'],
    'strategy_predict_records': ['created_at'],
    'strategy_factor_weights': ['updated_at'],
    'strategy_backtest_reports': ['created_at'],
    'hot_search_meta': ['updated_at'],
    'daily_recommendations': ['created_at'],
    'stock_basic_info': ['updated_at'],
    'user_scan_pool': ['created_at'],
    'stock_concept_tags': ['updated_at'],
    'trend_backtest_records': ['created_at'],
    'trend_backtest_reports': ['created_at', 'updated_at'],
}

def _prepare_ts_migration(cursor):
    """迁移第 1 步（在 CREATE TABLE 之前调用）：
    检查已有表的 DEFAULT 是否仍为 UTC（CURRENT_TIMESTAMP），
    若是则重命名为 _old_xxx，让后续 CREATE TABLE IF NOT EXISTS 用新 schema 重建。
    仅执行一次，用 _schema_migrations 标记。
    """
    cursor.execute('CREATE TABLE IF NOT EXISTS _schema_migrations (migration_key TEXT PRIMARY KEY, done_at TEXT)')
    cursor.execute('SELECT 1 FROM _schema_migrations WHERE migration_key = ?', ('beijing_timestamps',))
    if cursor.fetchone():
        return  # 已迁移过
    migrated = []
    for table, ts_cols in _TS_MIGRATION_TABLES.items():
        try:
            cursor.execute(f'PRAGMA table_info({table})')
            cols_info = cursor.fetchall()
            if not cols_info:
                continue  # 表不存在（首次启动），无需迁移
            # 检查时间戳列的 DEFAULT 是否仍为 CURRENT_TIMESTAMP
            needs = False
            for ci in cols_info:
                # ci = (cid, name, type, notnull, dflt_value, pk)
                if ci[1] in ts_cols and ci[4] == 'CURRENT_TIMESTAMP':
                    needs = True
                    break
            if not needs:
                continue
            # 重命名旧表（索引会自动跟随，稍后重建）
            cursor.execute(f'ALTER TABLE {table} RENAME TO _old_{table}')
            migrated.append(table)
        except Exception as e:
            print(f'[数据库迁移] {table} 准备阶段跳过: {e}')
    if migrated:
        print(f'[数据库迁移] 以下表将重建为北京时间 DEFAULT: {", ".join(migrated)}')

def _migrate_sim_fk(cursor):
    """确保 sim_* 三表带 user_id 外键级联；缺失则重建（无真实数据时安全）"""
    try:
        fks = cursor.execute("PRAGMA foreign_key_list(sim_accounts)").fetchall()
        if fks:
            return
        print('[数据库] 重建 sim 表以添加外键级联…')
        cursor.execute("DROP TABLE IF EXISTS sim_orders")
        cursor.execute("DROP TABLE IF EXISTS sim_positions")
        cursor.execute("DROP TABLE IF EXISTS sim_accounts")
        cursor.execute('''
            CREATE TABLE sim_accounts (
                user_id INTEGER PRIMARY KEY,
                cash REAL DEFAULT 1000000.0,
                day_start_value REAL DEFAULT 1000000.0,
                day_start_date TEXT DEFAULT '',
                created_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                updated_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)''')
        cursor.execute('''
            CREATE TABLE sim_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT,
                shares INTEGER NOT NULL DEFAULT 0,
                cost_price REAL NOT NULL DEFAULT 0,
                buy_date TEXT DEFAULT '',
                created_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                updated_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                UNIQUE(user_id, symbol),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)''')
        cursor.execute('''
            CREATE TABLE sim_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT,
                side TEXT NOT NULL,
                proposed_price REAL,
                proposed_qty INTEGER,
                filled_price REAL,
                filled_qty INTEGER,
                source TEXT,
                reason TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                reject_reason TEXT,
                created_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                filled_at DATETIME,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)''')
    except Exception as e:
        print(f'[数据库] sim 表外键迁移跳过: {e}')


def _finish_ts_migration(cursor):
    """迁移第 2 步（在 CREATE TABLE / CREATE INDEX 之后调用）：
    将 _old_xxx 表中的数据复制到新建的表（UTC 时间 +8 小时→北京时间），然后删除旧表。
    """
    cursor.execute('SELECT 1 FROM _schema_migrations WHERE migration_key = ?', ('beijing_timestamps',))
    if cursor.fetchone():
        return  # 已迁移过
    for table, ts_cols in _TS_MIGRATION_TABLES.items():
        try:
            # 检查 _old 表是否存在
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (f'_old_{table}',))
            if not cursor.fetchone():
                continue
            # 获取新表的列名
            cursor.execute(f'PRAGMA table_info({table})')
            new_cols = [ci[1] for ci in cursor.fetchall()]
            if not new_cols:
                continue
            # 构建 SELECT 列表（时间戳列做 +8 hours 转换）
            select_cols = []
            for c in new_cols:
                if c in ts_cols:
                    select_cols.append(f"datetime({c}, '+8 hours') AS {c}")
                else:
                    select_cols.append(c)
            select_list = ', '.join(select_cols)
            col_list = ', '.join(new_cols)
            # 复制数据
            cursor.execute(f'INSERT INTO {table} ({col_list}) SELECT {select_list} FROM _old_{table}')
            rowcount = cursor.rowcount
            # 删除旧表
            cursor.execute(f'DROP TABLE _old_{table}')
            print(f'[数据库迁移] {table}: {rowcount} 行数据已转换 UTC→北京时间')
        except Exception as e:
            print(f'[数据库迁移] {table} 数据复制失败: {e}')
    # 标记迁移完成
    cursor.execute(
        "INSERT OR REPLACE INTO _schema_migrations VALUES ('beijing_timestamps', datetime('now', '+8 hours'))"
    )
    print('[数据库迁移] 北京时间统一迁移完成')


def init_db():
    """初始化数据库表"""
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        # 迁移第 1 步：重命名 UTC 时间戳的旧表（仅首次执行）
        _prepare_ts_migration(cursor)
        # 收评表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_market_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                review_date TEXT NOT NULL UNIQUE,
                summary TEXT,
                market_json TEXT,
                trend_json TEXT,
                pool_json TEXT,
                created_at DATETIME DEFAULT (datetime('now', '+8 hours')))''')
        # 趋势股票表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trend_scan_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                score INTEGER DEFAULT 0,
                details_json TEXT,
                latest_price REAL DEFAULT 0,
                ma5_json TEXT,
                ma10_json TEXT,
                ma20_json TEXT,
                recent5_json TEXT,
                total_scanned INTEGER DEFAULT 0,
                source TEXT DEFAULT '',
                scan_time TEXT DEFAULT '',
                created_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                UNIQUE(scan_date, symbol))''')
        # 热门股票表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS hot_stock_buttons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                created_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                updated_at DATETIME DEFAULT (datetime('now', '+8 hours')) )''')
        # 股票名称搜索映射表（名称 -> 代码，含简称，一般不删除）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_alias_map (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                symbol TEXT NOT NULL,
                created_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                updated_at DATETIME DEFAULT (datetime('now', '+8 hours')) )''')
        # 策略预测记录表（每次扫描时记录判定结果）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS strategy_predict_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                predict_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                score INTEGER DEFAULT 0,
                label TEXT DEFAULT '',
                predict_direction TEXT DEFAULT '',
                outer_ratio REAL DEFAULT 0,
                volume_ratio REAL DEFAULT 0,
                turnover_rate REAL DEFAULT 0,
                change_percent REAL DEFAULT 0,
                weibi REAL DEFAULT 0,
                avg_price_deviation REAL DEFAULT 0,
                amplitude REAL DEFAULT 0,
                market_change REAL DEFAULT 0,
                verified INTEGER DEFAULT 0,
                actual_change REAL DEFAULT NULL,
                actual_direction TEXT DEFAULT '',
                is_correct INTEGER DEFAULT NULL,
                created_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                UNIQUE(predict_date, symbol))''')
        # 索引（2026-07-06 A-）：strategy_predict_records 按日期查询 + 未验证筛选是高频路径
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_strategy_predict_date ON strategy_predict_records(predict_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_strategy_predict_verified ON strategy_predict_records(verified)')
        # 复合索引（2026-10-13）：IC权重计算/核销扫描均为 verified+predict_date 组合过滤，
        # 点查/去重为 predict_date+symbol；IF NOT EXISTS 对存量库自动补齐
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_strategy_predict_verified_date ON strategy_predict_records(verified, predict_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_strategy_predict_date_symbol ON strategy_predict_records(predict_date, symbol)')
        # 策略权重表（动态调整的评分权重）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS strategy_factor_weights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                factor_name TEXT NOT NULL UNIQUE,
                weight REAL DEFAULT 1.0,
                accuracy REAL DEFAULT 50.0,
                sample_count INTEGER DEFAULT 0,
                correct_count INTEGER DEFAULT 0,
                updated_at DATETIME DEFAULT (datetime('now', '+8 hours')) )''')
        # 策略回测报告表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS strategy_backtest_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                backtest_date TEXT NOT NULL UNIQUE,
                market_change REAL DEFAULT 0,
                total_predictions INTEGER DEFAULT 0,
                correct_count INTEGER DEFAULT 0,
                accuracy REAL DEFAULT 0,
                bull_accuracy REAL DEFAULT 0,
                bear_accuracy REAL DEFAULT 0,
                bull_misjudge_count INTEGER DEFAULT 0,
                bear_misjudge_count INTEGER DEFAULT 0,
                misjudge_analysis TEXT,
                weight_adjustments TEXT,
                created_at DATETIME DEFAULT (datetime('now', '+8 hours')))''')
        # 热搜榜表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS hot_search_ranking (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                code TEXT DEFAULT '',
                name TEXT NOT NULL,
                market TEXT DEFAULT '',
                price INTEGER DEFAULT 0,
                change_percent INTEGER DEFAULT 0,
                change_val INTEGER DEFAULT 0,
                hot_rank INTEGER DEFAULT 0,
                batch_time TEXT DEFAULT '',
                sort_order INTEGER DEFAULT 0)''')
        # 热搜榜元数据表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS hot_search_meta (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meta_key TEXT NOT NULL UNIQUE,
                meta_value TEXT,
                updated_at DATETIME DEFAULT (datetime('now', '+8 hours')))''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS hot_search_daily_snapshot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT DEFAULT '',
                hot_rank INTEGER DEFAULT 0,
                UNIQUE(snapshot_date, symbol))''')
        # 推荐记录表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rec_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                price TEXT DEFAULT '',
                change_val REAL DEFAULT 0,
                change_percent REAL DEFAULT 0,
                score INTEGER DEFAULT 0,
                buy_price TEXT DEFAULT '',
                current_price TEXT DEFAULT '',
                sell_price TEXT DEFAULT '',
                reason TEXT,
                buy_timing TEXT DEFAULT '',
                sort_order INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                UNIQUE(rec_date, symbol))''')
        # mode 列（2026-10-18，P2-2 按模式复盘）：老库建表时没有这一列，得 ALTER 补。
        # SQLite 的 ADD COLUMN 不幂等，重复执行会报 "duplicate column name"，所以先查 PRAGMA。
        _rec_cols = {r[1] for r in cursor.execute('PRAGMA table_info(daily_recommendations)')}
        if 'mode' not in _rec_cols:
            cursor.execute("ALTER TABLE daily_recommendations ADD COLUMN mode TEXT DEFAULT ''")
            print('[数据库] daily_recommendations 已补 mode 列（低吸/打板/抄底/接力/其他）')

        # 股票映射表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_basic_info (
                symbol TEXT NOT NULL PRIMARY KEY,
                name TEXT NOT NULL,
                updated_at DATETIME DEFAULT (datetime('now', '+8 hours')))''')
        # 自定义扫描池表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_scan_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL UNIQUE,
                created_at DATETIME DEFAULT (datetime('now', '+8 hours')))''')
        # 股票标签表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_concept_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL UNIQUE,
                tags_json TEXT,
                updated_at DATETIME DEFAULT (datetime('now', '+8 hours')) )''')
        # 趋势回测记录表（三周期字段并存于同一行，补验时只 UPDATE 对应周期）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trend_backtest_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                predict_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT DEFAULT '',
                score INTEGER DEFAULT 0,
                predict_direction TEXT DEFAULT 'bull',
                sector_factor REAL DEFAULT 0,
                chip_factor REAL DEFAULT 0,
                pattern TEXT DEFAULT '',
                pattern_factor REAL DEFAULT 0,
                market_regime TEXT DEFAULT 'range',
                total_factor REAL DEFAULT 0,
                verified_1d INTEGER DEFAULT 0,
                actual_change_1d REAL DEFAULT 0,
                market_change_1d REAL DEFAULT 0,
                is_correct_1d INTEGER DEFAULT 0,
                verified_at_1d TEXT DEFAULT '',
                verified_3d INTEGER DEFAULT 0,
                actual_change_3d REAL DEFAULT 0,
                market_change_3d REAL DEFAULT 0,
                is_correct_3d INTEGER DEFAULT 0,
                verified_at_3d TEXT DEFAULT '',
                verified_5d INTEGER DEFAULT 0,
                actual_change_5d REAL DEFAULT 0,
                market_change_5d REAL DEFAULT 0,
                is_correct_5d INTEGER DEFAULT 0,
                verified_at_5d TEXT DEFAULT '',
                created_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                UNIQUE(predict_date, symbol))''')
        # 索引（2026-07-06 A-）：trend_backtest_records 回测验证按 verified_1d/3d/5d 筛选未验证记录是高频路径
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_trend_bt_verified_1d ON trend_backtest_records(verified_1d)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_trend_bt_verified_3d ON trend_backtest_records(verified_3d)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_trend_bt_verified_5d ON trend_backtest_records(verified_5d)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_trend_bt_predict_date ON trend_backtest_records(predict_date)')
        # 趋势回测聚合报告表（按 predict_date 聚合，三周期统计字段并存）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trend_backtest_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                predict_date TEXT NOT NULL,
                total_count INTEGER DEFAULT 0,
                verified_1d_count INTEGER DEFAULT 0,
                win_1d_count INTEGER DEFAULT 0,
                win_rate_1d REAL DEFAULT 0,
                verified_3d_count INTEGER DEFAULT 0,
                win_3d_count INTEGER DEFAULT 0,
                win_rate_3d REAL DEFAULT 0,
                verified_5d_count INTEGER DEFAULT 0,
                win_5d_count INTEGER DEFAULT 0,
                win_rate_5d REAL DEFAULT 0,
                score_60_70_total INTEGER DEFAULT 0,
                score_60_70_win_1d INTEGER DEFAULT 0,
                score_60_70_win_3d INTEGER DEFAULT 0,
                score_60_70_win_5d INTEGER DEFAULT 0,
                score_70_80_total INTEGER DEFAULT 0,
                score_70_80_win_1d INTEGER DEFAULT 0,
                score_70_80_win_3d INTEGER DEFAULT 0,
                score_70_80_win_5d INTEGER DEFAULT 0,
                score_80_plus_total INTEGER DEFAULT 0,
                score_80_plus_win_1d INTEGER DEFAULT 0,
                score_80_plus_win_3d INTEGER DEFAULT 0,
                score_80_plus_win_5d INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                updated_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                UNIQUE(predict_date))''')
        try:
            cursor.execute('ALTER TABLE hot_search_ranking ADD COLUMN hot_rank INTEGER DEFAULT 0')
        except Exception:
            pass
        try:
            cursor.execute('ALTER TABLE trend_scan_results DROP COLUMN hot_rank')
        except Exception:
            pass
        # 行业中性化：stock_basic_info 表增加 industry 列
        try:
            cursor.execute('ALTER TABLE stock_basic_info ADD COLUMN industry TEXT DEFAULT ""')
        except Exception:
            pass  # 列已存在
        # 回测因子补全：strategy_predict_records 表增加 position/main_capital_inflow/holder_structure 列
        # 原表只有 7 个旧因子字段，导致 _adjust_weights 跳过这 3 个因子，sample_count 永远为 0
        for _col, _type in [('position', 'REAL DEFAULT 0'),
                             ('main_capital_inflow', 'REAL DEFAULT 0'),
                             ('holder_structure', 'REAL DEFAULT 0')]:
            try:
                cursor.execute(f'ALTER TABLE strategy_predict_records ADD COLUMN {_col} {_type}')
            except Exception:
                pass  # 列已存在
        # 股东人数筹码面数据表（2026-07-06）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_holder_num (
                symbol TEXT NOT NULL,           -- 小写带前缀 sh600519
                end_date TEXT NOT NULL,         -- 截止日期 YYYY-MM-DD（月末）
                holder_total_num INTEGER,       -- 股东总户数
                num_change INTEGER,             -- 环比增减数
                total_num_ratio REAL,           -- 环比变化率%
                avg_free_shares REAL,           -- 户均流通股
                avg_hold_amt REAL,              -- 户均持股市值
                notice_date TEXT,               -- 公告日期
                updated_at TEXT,                -- 更新时间
                PRIMARY KEY (symbol, end_date))''')
        # 基本面质量数据表（2026-07-16 P1a：ROE + 净利润同比，季度披露）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_fundamental (
                symbol TEXT NOT NULL,            -- 小写带前缀 sh600519
                report_date TEXT,                -- 报告期 YYYY-MM-DD
                roe REAL,                        -- 加权平均净资产收益率(%)
                net_profit_yoy REAL,             -- 归母净利润同比增长率(%)
                eps REAL,                        -- 基本每股收益
                updated_at TEXT,                 -- 更新时间
                PRIMARY KEY (symbol))''')
        # 估值分位数据表（2026-07-16 P2c：PE/PB 百分位，每日盘后更新）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_valuation (
                symbol TEXT NOT NULL,            -- 小写带前缀 sh600519
                pe REAL,                         -- 市盈率(TTM)
                pb REAL,                         -- 市净率
                pe_pct REAL,                     -- PE 全市场百分位(0~100，越高越贵)
                pb_pct REAL,                     -- PB 全市场百分位(0~100，越高越贵)
                updated_at TEXT,                 -- 更新时间
                PRIMARY KEY (symbol))''')
        # K线历史缓存表（2026-07-20 性能优化 A1：日线历史按交易日缓存，当天只拉一次腾讯）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS kline_cache (
                symbol TEXT NOT NULL,            -- 小写带前缀 sh600519
                trade_date TEXT NOT NULL,        -- 缓存对应的交易日 YYYY-MM-DD
                kline_json TEXT NOT NULL,         -- 该交易日收盘后的日线历史 JSON
                updated_at TEXT,                  -- 更新时间
                PRIMARY KEY (symbol, trade_date))''')
        # 资金流向最后成功快照（2026-10-16）：按区块存整包 JSON，服务重启后兜底，
        # 解决「东财 push2 风控拒连 + 重启清空内存兜底 → 收盘后页面全空」
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS fund_flow_snapshot (
                snap_key TEXT PRIMARY KEY,       -- 'stocks:<n>' / 'sectors:<n>' / 'hidden:<key>'
                snap_json TEXT NOT NULL,         -- 该区块最后成功数据的整包 JSON
                updated_at TEXT)''')
        # 双源交叉校验（2026-10-19）：westock（腾讯自选股）每日快照 + 与本地口径比对，
        # 权威源=westock（用户决策"不一致的以 westock 为准"）。
        # 宇宙=当日资金流 top + 热搜 top + 当日推荐（约 150-200 只），非全市场。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS westock_snapshot (
                date TEXT NOT NULL,
                symbol TEXT NOT NULL,            -- 小写带前缀 sh600519
                name TEXT,
                close_price REAL,
                change_pct REAL,
                main_net_inflow REAL,            -- 腾讯口径主力净流入（元，权威值）
                jumbo_net_flow REAL,             -- 超大单净流
                mid_net_flow REAL,
                retail_net_flow REAL,
                main_net_5d REAL,
                main_net_10d REAL,
                main_net_20d REAL,
                industry_rank INTEGER,
                fetched_at TEXT,
                PRIMARY KEY (date, symbol))''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS data_cross_check (
                date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT,
                our_change_pct REAL,              -- 本地口径涨跌幅（%）
                westock_change_pct REAL,          -- westock 涨跌幅（%）
                quote_agreed INTEGER,             -- 行情层是否一致（0/1）
                our_main_inflow REAL,             -- 本地口径主力净流入（元）
                westock_main_inflow REAL,         -- westock 主力净流入（元，权威值）
                inflow_diff REAL,                 -- westock - our（元）
                inflow_diff_pct REAL,             -- 偏差百分比（相对 our）
                direction_conflict INTEGER,       -- 符号相反（0/1）
                authoritative TEXT DEFAULT 'westock',
                checked_at TEXT,
                PRIMARY KEY (date, symbol))''')
        # 接口用量统计（2026-10-19）：第三方 API 每日调用计数与上限预警
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS api_usage_daily (
                date TEXT NOT NULL,
                provider TEXT NOT NULL,          -- tencent / eastmoney / sina / westock / wecom ...
                interface TEXT NOT NULL,          -- quote / kline / clist / ulist / push / chat ...
                count INTEGER DEFAULT 0,          -- 当日累计调用次数
                ok INTEGER DEFAULT 0,             -- 成功（HTTP<400）次数
                fail INTEGER DEFAULT 0,           -- 失败（HTTP>=400 或异常）次数
                PRIMARY KEY (date, provider, interface))''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS api_usage_limits (
                provider TEXT NOT NULL,
                interface TEXT NOT NULL,
                daily_limit INTEGER NOT NULL,     -- 每日上限（防封禁阈值）
                updated_by TEXT DEFAULT '',
                updated_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                PRIMARY KEY (provider, interface))''')
        # 索引（2026-07-20 性能优化 A4）：基本面/估值按 symbol 高频点查
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_fundamental_symbol ON stock_fundamental(symbol)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_valuation_symbol ON stock_valuation(symbol)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_kline_cache_symbol ON kline_cache(symbol)')
        # 迁移第 2 步：将旧表数据（UTC）复制到新表（+8h→北京时间），然后删除旧表
        # V4.4：自动优化引擎表（趋势维度权重 + 角色门槛 + 角色表现 + 优化历史）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trend_dimension_weights (
                dimension TEXT PRIMARY KEY,   -- 'd1', 'd2', 'd3', 'd4'
                weight REAL DEFAULT 1.0,
                accuracy REAL DEFAULT 0,
                discrimination REAL DEFAULT 0,
                sample_count INTEGER DEFAULT 0,
                correct_count INTEGER DEFAULT 0,
                updated_at DATETIME DEFAULT (datetime('now', '+8 hours'))
            )''')
        # 2026-10-28 角色体系整体下线：不再建 role_thresholds / role_performance。
        # 老库里这两张表保留（不 DROP，避免误删历史数据），但已无任何读写方。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS optimization_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                module TEXT NOT NULL,          -- 'trend_dimension_weights'/'grid_search'
                parameter_set TEXT DEFAULT '', -- JSON 参数快照
                metric_name TEXT DEFAULT '',
                metric_value REAL DEFAULT 0,
                old_value REAL DEFAULT NULL,
                new_value REAL DEFAULT NULL,
                details TEXT DEFAULT '',       -- JSON 详情
                created_at DATETIME DEFAULT (datetime('now', '+8 hours'))
            )''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_optimization_module ON optimization_history(module)
        ''')
        # 2026-10-28 角色体系下线：不再补 role 列（老库里已有的同名列保留但无人写读）
        # V4.4：趋势回测记录表添加 s_d1/s_d2/s_d3/s_d4 字段（D1-D4权重优化需要）
        for dim_col in ['s_d1', 's_d2', 's_d3', 's_d4']:
            try:
                cursor.execute(f'ALTER TABLE trend_backtest_records ADD COLUMN {dim_col} REAL DEFAULT NULL')
            except Exception:
                pass  # 列已存在
        # 收评表补充字段（2026-07-30 新增 ai_analysis_json、limit_up_count/limit_down_count；
        # 2026-09-04 新增 sentiment_cycle_json；2026-10-14 新增 focus_meta_json —— 明日关注的
        # 整篮子预期胜率与"被过滤掉几只"，不落库的话前端刷新一次就再也算不出 dropped）
        # 只在初始化时执行一次；此前放在 save_daily_market_reviews 内，每次保存都要跑 12 条 DDL
        for col in ['industry_json', 'concept_json', 'limit_up_json', 'limit_down_json',
                    'top_gainers_json', 'top_losers_json', 'focus_json', 'top_funds_json',
                    'ai_analysis_json', 'sentiment_cycle_json', 'focus_meta_json']:
            try:
                cursor.execute(f'ALTER TABLE daily_market_reviews ADD COLUMN {col} TEXT')
            except Exception:
                pass  # 列已存在
        for col in ['limit_up_count', 'limit_down_count']:
            try:
                cursor.execute(f'ALTER TABLE daily_market_reviews ADD COLUMN {col} INTEGER')
            except Exception:
                pass  # 列已存在
        # 推荐记录表：买入时机（2026-10-12 新增，JSON: {"type","label","note"}）
        try:
            cursor.execute("ALTER TABLE daily_recommendations ADD COLUMN buy_timing TEXT DEFAULT ''")
        except Exception:
            pass  # 列已存在
        # 用户持仓表（2026-10-12 新增，持仓管理+仓位配置功能）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL UNIQUE,
                name TEXT DEFAULT '',
                cost_price REAL DEFAULT 0,
                shares INTEGER DEFAULT 0,
                buy_date TEXT DEFAULT '',
                note TEXT DEFAULT '',
                created_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                updated_at DATETIME DEFAULT (datetime('now', '+8 hours')))''')

        # ===== 多用户账号体系（2026-10-12） =====
        # 持仓是每个用户私有的，行情/推荐/收评是公共的 —— 只隔离私有数据。
        # users：账号 + PBKDF2-SHA256 加盐哈希（标准库实现，无额外依赖）
        # user_sessions：随机 token 会话，可撤销、可过期
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                display_name TEXT DEFAULT '',
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                last_login_at DATETIME DEFAULT '')''')
        # 管理员标记迁移（2026-10-22 用户定：yz 为管理员账号）：
        # 机器人会话列表（discover）会暴露全部会话，只允许管理员刷新。
        _user_cols = [r[1] for r in cursor.execute('PRAGMA table_info(users)')]
        if 'is_admin' not in _user_cols:
            cursor.execute('ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0')
        cursor.execute("UPDATE users SET is_admin = 1 WHERE username = 'yz'")
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                expires_at DATETIME NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE)''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions(user_id)')

        # 每账号的初始本金（2026-10-14）：持仓页要知道"这 10 万是多少钱里的一成"，
        # 否则只持一只票时，仓位只能按持仓市值自己算 → 永远是 100%，超配告警也就成了废话。
        # 单独一张表而不往 users 上加列：users 是认证表，且它的角色列已有历史包袱，不碰为妙。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_capital (
                user_id INTEGER PRIMARY KEY,
                principal REAL NOT NULL DEFAULT 0,
                note TEXT DEFAULT '',
                updated_at DATETIME DEFAULT (datetime('now', '+8 hours')))''')

        # 每账号的累计已实现盈亏（2026-10-23）：减仓/清仓时按 (卖价-成本)×股数 累加。
        # 没有这张表，「当前资金」就只能永远等于初始本金——用户清仓后账面赚的钱
        # 在系统里无处落脚（清仓会整行删除持仓，实现盈亏只留在被删掉的 note 文本里）。
        # 恒等式：可用现金 = 初始本金 + 累计已实现 - 当前持仓占用成本（买入扣现金、
        # 卖出按 成本+已实现 回现金，两边一抵即得），见 get_portfolio_diagnosis。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_realized_pnl (
                user_id INTEGER PRIMARY KEY,
                realized_pnl REAL NOT NULL DEFAULT 0,
                trades INTEGER NOT NULL DEFAULT 0,
                updated_at DATETIME DEFAULT (datetime('now', '+8 hours')))''')

        # user_positions 增加 user_id。
        # 存量表 symbol 是「全局 UNIQUE」—— 多用户持有同一只票会直接冲突，
        # 而 SQLite 不支持 ALTER 唯一约束，故整表重建为 UNIQUE(user_id, symbol)。
        _pos_cols = {r[1] for r in cursor.execute('PRAGMA table_info(user_positions)').fetchall()}
        if 'user_id' not in _pos_cols:
            cursor.execute('''
                CREATE TABLE user_positions_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL DEFAULT 1,
                    symbol TEXT NOT NULL,
                    name TEXT DEFAULT '',
                    cost_price REAL DEFAULT 0,
                    shares INTEGER DEFAULT 0,
                    buy_date TEXT DEFAULT '',
                    note TEXT DEFAULT '',
                    created_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                    updated_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                    UNIQUE(user_id, symbol))''')
            # 存量持仓归 1 号用户（首个注册者自增 id 即为 1，天然认领，无需迁移操作）
            cursor.execute('''
                INSERT INTO user_positions_v2
                    (user_id, symbol, name, cost_price, shares, buy_date, note, created_at, updated_at)
                SELECT 1, symbol, name, cost_price, shares, buy_date, note, created_at, updated_at
                FROM user_positions''')
            cursor.execute('DROP TABLE user_positions')
            cursor.execute('ALTER TABLE user_positions_v2 RENAME TO user_positions')
            print('[数据库] user_positions 已迁移为按用户隔离 UNIQUE(user_id, symbol)')

        # ========== 自选股 / 趋势池按用户隔离（2026-10-13，今日行动台） ==========
        # 这两张表原本是全局 UNIQUE：A 加一只票，B 的列表里也跟着多，互相覆盖。
        # 与 user_positions 的关键区别：持仓有"天然归属"（当初就是 1 号建的），
        # 自选股没有。若照搬"全给 1 号"的做法，2 号打开页面会看到"票不见了"。
        # 因此存量行**克隆给每个已注册用户**：一条自选不占成本，各自删掉自己
        # 不要的那几只即可，任何一人都不会丢数据。
        _hot_cols = {r[1] for r in cursor.execute('PRAGMA table_info(hot_stock_buttons)').fetchall()}
        if _hot_cols and 'user_id' not in _hot_cols:
            cursor.execute('''
                CREATE TABLE hot_stock_buttons_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL DEFAULT 1,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    created_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                    updated_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                    UNIQUE(user_id, code))''')
            cursor.execute('''
                INSERT OR IGNORE INTO hot_stock_buttons_v2
                    (user_id, code, name, created_at, updated_at)
                SELECT u.id, t.code, t.name, t.created_at, t.updated_at
                FROM users u CROSS JOIN hot_stock_buttons t''')
            cursor.execute('DROP TABLE hot_stock_buttons')
            cursor.execute('ALTER TABLE hot_stock_buttons_v2 RENAME TO hot_stock_buttons')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_hot_buttons_user ON hot_stock_buttons(user_id)')
            print('[数据库] hot_stock_buttons 已迁移为按用户隔离（存量克隆给每个用户）')

        _pool_cols = {r[1] for r in cursor.execute('PRAGMA table_info(user_scan_pool)').fetchall()}
        if _pool_cols and 'user_id' not in _pool_cols:
            cursor.execute('''
                CREATE TABLE user_scan_pool_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL DEFAULT 1,
                    symbol TEXT NOT NULL,
                    created_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                    UNIQUE(user_id, symbol))''')
            cursor.execute('''
                INSERT OR IGNORE INTO user_scan_pool_v2 (user_id, symbol, created_at)
                SELECT u.id, t.symbol, t.created_at
                FROM users u CROSS JOIN user_scan_pool t''')
            cursor.execute('DROP TABLE user_scan_pool')
            cursor.execute('ALTER TABLE user_scan_pool_v2 RENAME TO user_scan_pool')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_scan_pool_user ON user_scan_pool(user_id)')
            print('[数据库] user_scan_pool 已迁移为按用户隔离（存量克隆给每个用户）')

        # ========== 模拟交易（2026-10-12，纯模拟盘·不下真单） ==========
        # 每个 app 用户一个独立模拟账户，与真实券商无关，零真金白银风险。
        # user_id 外键级联：删除用户时一并清理其模拟账户/持仓/订单。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sim_accounts (
                user_id INTEGER PRIMARY KEY,
                cash REAL DEFAULT 1000000.0,
                day_start_value REAL DEFAULT 1000000.0,
                day_start_date TEXT DEFAULT '',
                created_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                updated_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sim_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT,
                shares INTEGER NOT NULL DEFAULT 0,
                cost_price REAL NOT NULL DEFAULT 0,
                buy_date TEXT DEFAULT '',
                created_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                updated_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                UNIQUE(user_id, symbol),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sim_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT,
                side TEXT NOT NULL,
                proposed_price REAL,
                proposed_qty INTEGER,
                filled_price REAL,
                filled_qty INTEGER,
                source TEXT,
                reason TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                reject_reason TEXT,
                created_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                filled_at DATETIME,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)''')
        # 迁移：若已存在的 sim 表缺少 FK（早期版本），重建（当前无真实数据，安全）
        _migrate_sim_fk(cursor)

        _migrate_sim_fk(cursor)

        _finish_ts_migration(cursor)
        conn.commit()
        print('[数据库] 表初始化完成')
    except Exception as e:
        print(f'[数据库] 初始化失败: {e}')
    finally:
        conn.close()

# 大盘指数代码
def _row_to_recommendation(row) -> Dict:
    """daily_recommendations 单行 → 前端推荐 dict（load_daily_recommendations 与行动台共用）"""
    # buy_timing 列兼容：旧库尚未执行 ALTER 时 keys 里没有该列，降级为 None
    has_buy_timing_col = 'buy_timing' in row.keys()
    return {
        'name': row['name'],
        'symbol': row['symbol'],
        'price': row['price'],
        'change': float(row['change_val']) if row['change_val'] else 0,
        'changePercent': float(row['change_percent']) if row['change_percent'] else 0,
        'score': row['score'] or 0,
        'buySellPoints': {
            'buy': row['buy_price'] or '',
            'current': row['current_price'] or '',
            'sell': row['sell_price'] or '',
            # 买入时机（2026-10-12）：旧记录无此列/无此值时为 None，前端自动降级隐藏
            'buyTiming': (json.loads(row['buy_timing'])
                          if has_buy_timing_col and row['buy_timing'] else None),
        } if row['buy_price'] else None,
        'reason': row['reason'] or '',
        # 模式标签（低吸/打板/抄底/接力/其他）：老库没这一列时返回空串，前端不显示徽章
        'mode': (row['mode'] or '') if 'mode' in row.keys() else '',
    }


def load_daily_recommendations() -> List[Dict]:
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        # QUERY_DAILY_RECOMMENDATIONS_LIMIT 限制的是「天数」而非「行数」
        # daily_recommendations 表每只股票一行（每天 3 只 = 3 行），
        # 若直接 LIMIT 30 行只能读出 10 天，与 save 的 [:30]（按天数）语义不一致。
        # 改为：先取最新 N 天的日期，再查这些天的所有股票行。
        cursor.execute(
            'SELECT DISTINCT rec_date FROM daily_recommendations ORDER BY rec_date DESC LIMIT ?',
            (QUERY_DAILY_RECOMMENDATIONS_LIMIT,)
        )
        recent_dates = [row['rec_date'] for row in cursor.fetchall()]
        if not recent_dates:
            conn.close()
            return []
        placeholders = ','.join('?' * len(recent_dates))
        cursor.execute(
            f'SELECT * FROM daily_recommendations WHERE rec_date IN ({placeholders}) ORDER BY rec_date DESC, sort_order ASC',
            recent_dates
        )
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
            current_group['daily_recommendations'].append(_row_to_recommendation(row))
        if current_group:
            result.append(current_group)
        return result
    except Exception as error:
        print(f'加载推荐记录失败: {str(error)}')
        return []


def load_recommendation_group(rec_date: str = None) -> Dict:
    """只取某一天的推荐（缺省=库内最新一天），行动台专用。

    为什么不用 load_daily_recommendations：那会读最近 N 天（约 30 天 × 3 行）并逐行
    解析 JSON；行动台每次轮询都要跑，只需要一组。纯 SQL，不触发任何行情重算
    （/api/daily_recommendations 的 GET 会重算历史价格，绝不可用于热路径）。

    Returns:
        {'date': 'YYYY-MM-DD' | '', 'stocks': [...]}
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        if not rec_date:
            cursor.execute('SELECT MAX(rec_date) AS d FROM daily_recommendations')
            row0 = cursor.fetchone()
            rec_date = row0['d'] if row0 and row0['d'] else ''
        if not rec_date:
            conn.close()
            return {'date': '', 'stocks': []}
        cursor.execute(
            'SELECT * FROM daily_recommendations WHERE rec_date = ? ORDER BY sort_order ASC',
            (rec_date,)
        )
        rows = cursor.fetchall()
        conn.close()
        return {'date': rec_date, 'stocks': [_row_to_recommendation(r) for r in rows]}
    except Exception as error:
        print(f'加载单日推荐失败: {str(error)}')
        return {'date': '', 'stocks': []}


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
            cursor.execute('DELETE FROM daily_recommendations WHERE rec_date = ?', (rec_date,))
            for sort_order, rec in enumerate(recs):
                bsp = rec.get('buySellPoints') or {}
                # 买入时机（2026-10-12）：JSON 持久化，保证刷新后徽章不丢
                buy_timing = bsp.get('buyTiming')
                buy_timing_json = json.dumps(buy_timing, ensure_ascii=False) if buy_timing else ''
                cursor.execute('''
                    INSERT INTO daily_recommendations (rec_date, symbol, name, price, change_val, change_percent, score, buy_price, current_price, sell_price, reason, buy_timing, sort_order, mode)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    buy_timing_json,
                    sort_order,
                    # 模式标签（2026-10-18）：由 buy_rules.classify 的 verdict 机械映射而来，
                    # 不人工判断 —— 标签打错，按模式的复盘结论就是歪的。
                    rec.get('mode', '') or ''
                ))
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存推荐记录失败: {str(error)}')
        return False

def delete_daily_recommendations_by_date(rec_date: str) -> bool:
    """按日期物理删除推荐记录（2026-10-12 新增）

    背景：save_daily_recommendations 只"替换"列表内出现的日期（循环内 DELETE+INSERT），
    调用方把某日期从列表中过滤掉后再 save，该日期的旧行永远不会被删——
    DELETE /api/daily_recommendations/{date} 因此返回"删除成功"但记录仍在（既有 bug）。
    删除类操作必须显式 DELETE，不能依赖 save 的替换语义。
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM daily_recommendations WHERE rec_date = ?', (rec_date,))
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        if deleted:
            print(f'删除推荐记录: {rec_date}（{deleted} 行）')
        return True
    except Exception as error:
        print(f'删除推荐记录失败: {str(error)}')
        return False

def load_trend_scan_freshness() -> Dict:
    """趋势扫描的"数据到哪天了"（行动台用，不解析任何 JSON）。

    load_trend_scan_results 会把最近若干行的 ma5/ma10/details JSON 全部解开，
    行动台只需要日期与只数，故单开一条聚合查询。
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT scan_date, MAX(created_at) AS scan_at, "
            "       SUM(CASE WHEN symbol = '__SCAN_EMPTY__' THEN 0 ELSE 1 END) AS found, "
            "       MAX(total_scanned) AS total_scanned "
            "FROM trend_scan_results "
            "WHERE scan_date = (SELECT MAX(scan_date) FROM trend_scan_results)"
        )
        row = cursor.fetchone()
        conn.close()
        if not row or not row['scan_date']:
            return {'date': '', 'scanTime': '', 'found': 0, 'totalScanned': 0}
        return {
            'date': row['scan_date'],
            # 注意：表里的 scan_time 存的是"本次扫描耗时"（如 '446.7s'），
            # 数据时点要用 created_at，别拿 scan_time 当时间戳显示。
            'scanTime': str(row['scan_at'] or ''),
            'found': int(row['found'] or 0),
            'totalScanned': int(row['total_scanned'] or 0),
        }
    except Exception as error:
        print(f'加载趋势扫描新鲜度失败: {str(error)}')
        return {'date': '', 'scanTime': '', 'found': 0, 'totalScanned': 0}


def load_trend_scan_results() -> Dict:
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
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
            # 跳过占位行（0只扫描结果的元数据行）
            if row['symbol'] == '__SCAN_EMPTY__':
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
    conn = None
    try:
        scan_date = data.get('date', date.today().isoformat())
        stocks = data.get('stocks', [])
        total_scanned = data.get('totalScanned', 0)
        source = data.get('source', '')
        scan_time = data.get('scanTime', '')

        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM trend_scan_results WHERE scan_date = ?', (scan_date,))
        for stock in stocks:
            cursor.execute('''
                INSERT INTO trend_scan_results (scan_date, symbol, name, score, details_json, latest_price, ma5_json, ma10_json, ma20_json, recent5_json, total_scanned, source, scan_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        # 当扫描结果为0只时，仍插入一条占位行保留扫描元数据（total_scanned/scan_time等）
        # 占位行 symbol='__SCAN_EMPTY__'，history-counts 查询会排除它不计入符合数
        if not stocks:
            cursor.execute('''
                INSERT INTO trend_scan_results (scan_date, symbol, name, score, details_json, latest_price, ma5_json, ma10_json, ma20_json, recent5_json, total_scanned, source, scan_time)
                VALUES (?, '__SCAN_EMPTY__', '', 0, '{}', 0, '[]', '[]', '[]', '[]', ?, ?, ?)
            ''', (scan_date, total_scanned, source, scan_time))
        conn.commit()
        return True
    except Exception as error:
        print(f'保存趋势股票失败: {str(error)}')
        return False
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


# ========== 趋势回测函数（2026-07-01）==========
def save_trend_backtest_records(predict_date: str, results: List[Dict]) -> bool:
    """保存趋势回测预测记录（每次趋势扫描后调用）

    Args:
        predict_date: 预测日期（即趋势扫描日期）
        results: cleaned_results 列表，每项含 symbol/name/score/details
                 details 子字典内含 sectorFactor/chipFactor/pattern/patternFactor/marketRegime/totalFactor
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        for r in results:
            details = r.get('details', {}) or {}
            cursor.execute('''
                INSERT OR REPLACE INTO trend_backtest_records
                    (predict_date, symbol, name, score, predict_direction,
                     sector_factor, chip_factor, pattern, pattern_factor, market_regime, total_factor,
                     s_d1, s_d2, s_d3, s_d4)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                predict_date,
                r.get('symbol', ''),
                r.get('name', ''),
                int(r.get('score', 0) or 0),
                'bull',
                float(details.get('sectorFactor', 0) or 0),
                float(details.get('chipFactor', 0) or 0),
                str(details.get('pattern', '') or ''),
                float(details.get('patternFactor', 0) or 0),
                str(details.get('marketRegime', 'range') or 'range'),
                float(details.get('totalFactor', 0) or 0),
                details.get('sD1'),
                details.get('sD2'),
                details.get('sD3'),
                details.get('sD4'),
            ))
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'[趋势回测] 保存预测记录失败: {str(error)}')
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def load_trend_backtest_records(predict_date: str = None, symbol: str = None, limit: int = 100) -> List[Dict]:
    """加载趋势回测明细记录（支持按 predict_date / symbol 过滤）"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        sql = 'SELECT * FROM trend_backtest_records'
        conditions = []
        params = []
        if predict_date:
            conditions.append('predict_date = ?')
            params.append(predict_date)
        if symbol:
            conditions.append('symbol = ?')
            params.append(symbol)
        if conditions:
            sql += ' WHERE ' + ' AND '.join(conditions)
        sql += ' ORDER BY predict_date DESC, score DESC LIMIT ?'
        params.append(limit)
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as error:
        print(f'[趋势回测] 加载明细失败: {str(error)}')
        return []


def load_trend_backtest_records_pending_verification() -> List[Dict]:
    """加载所有未完全验证的记录（verified_1d=0 OR verified_3d=0 OR verified_5d=0）

    返回所有待补验的记录，按 predict_date 分组由调用方处理。
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM trend_backtest_records
            WHERE verified_1d = 0 OR verified_3d = 0 OR verified_5d = 0
            ORDER BY predict_date ASC, score DESC
        ''')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as error:
        print(f'[趋势回测] 加载待验证记录失败: {str(error)}')
        return []


def update_trend_backtest_verification(predict_date: str, symbol: str, period: str,
                                       actual_change: float, market_change: float,
                                       is_correct: bool) -> bool:
    """补验单条记录的指定周期

    Args:
        period: '1d' / '3d' / '5d'
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        from datetime import datetime
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        sql = f'''UPDATE trend_backtest_records
                  SET verified_{period} = 1,
                      actual_change_{period} = ?,
                      market_change_{period} = ?,
                      is_correct_{period} = ?,
                      verified_at_{period} = ?
                  WHERE predict_date = ? AND symbol = ?'''
        cursor.execute(sql, (
            float(actual_change), float(market_change),
            1 if is_correct else 0, now,
            predict_date, symbol
        ))
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'[趋势回测] 更新验证记录失败: {str(error)}')
        return False


def upsert_trend_backtest_report(predict_date: str, total_count: int,
                                 period_stats: Dict, score_segment_stats: Dict) -> bool:
    """UPSERT 趋势回测聚合报告

    Args:
        predict_date: 预测日期
        total_count: 该日入选趋势股总数
        period_stats: {'1d': {'verified': n, 'win': n, 'win_rate': r}, '3d': {...}, '5d': {...}}
        score_segment_stats: {'60_70': {'total': n, 'win_1d': n, 'win_3d': n, 'win_5d': n},
                              '70_80': {...}, '80_plus': {...}}
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        from datetime import datetime
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        p1 = period_stats.get('1d', {})
        p3 = period_stats.get('3d', {})
        p5 = period_stats.get('5d', {})
        s60 = score_segment_stats.get('60_70', {})
        s70 = score_segment_stats.get('70_80', {})
        s80 = score_segment_stats.get('80_plus', {})
        cursor.execute('''
            INSERT INTO trend_backtest_reports (
                predict_date, total_count,
                verified_1d_count, win_1d_count, win_rate_1d,
                verified_3d_count, win_3d_count, win_rate_3d,
                verified_5d_count, win_5d_count, win_rate_5d,
                score_60_70_total, score_60_70_win_1d, score_60_70_win_3d, score_60_70_win_5d,
                score_70_80_total, score_70_80_win_1d, score_70_80_win_3d, score_70_80_win_5d,
                score_80_plus_total, score_80_plus_win_1d, score_80_plus_win_3d, score_80_plus_win_5d,
                updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(predict_date) DO UPDATE SET
                total_count = excluded.total_count,
                verified_1d_count = excluded.verified_1d_count, win_1d_count = excluded.win_1d_count, win_rate_1d = excluded.win_rate_1d,
                verified_3d_count = excluded.verified_3d_count, win_3d_count = excluded.win_3d_count, win_rate_3d = excluded.win_rate_3d,
                verified_5d_count = excluded.verified_5d_count, win_5d_count = excluded.win_5d_count, win_rate_5d = excluded.win_rate_5d,
                score_60_70_total = excluded.score_60_70_total, score_60_70_win_1d = excluded.score_60_70_win_1d, score_60_70_win_3d = excluded.score_60_70_win_3d, score_60_70_win_5d = excluded.score_60_70_win_5d,
                score_70_80_total = excluded.score_70_80_total, score_70_80_win_1d = excluded.score_70_80_win_1d, score_70_80_win_3d = excluded.score_70_80_win_3d, score_70_80_win_5d = excluded.score_70_80_win_5d,
                score_80_plus_total = excluded.score_80_plus_total, score_80_plus_win_1d = excluded.score_80_plus_win_1d, score_80_plus_win_3d = excluded.score_80_plus_win_3d, score_80_plus_win_5d = excluded.score_80_plus_win_5d,
                updated_at = excluded.updated_at
        ''', (
            predict_date, total_count,
            p1.get('verified', 0), p1.get('win', 0), p1.get('win_rate', 0),
            p3.get('verified', 0), p3.get('win', 0), p3.get('win_rate', 0),
            p5.get('verified', 0), p5.get('win', 0), p5.get('win_rate', 0),
            s60.get('total', 0), s60.get('win_1d', 0), s60.get('win_3d', 0), s60.get('win_5d', 0),
            s70.get('total', 0), s70.get('win_1d', 0), s70.get('win_3d', 0), s70.get('win_5d', 0),
            s80.get('total', 0), s80.get('win_1d', 0), s80.get('win_3d', 0), s80.get('win_5d', 0),
            now,
        ))
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'[趋势回测] 保存报告失败: {str(error)}')
        return False


def load_trend_backtest_reports(limit: int = 30) -> List[Dict]:
    """加载历史回测报告列表（按 predict_date DESC）"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM trend_backtest_reports ORDER BY predict_date DESC LIMIT ?',
            (limit,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as error:
        print(f'[趋势回测] 加载报告列表失败: {str(error)}')
        return []


def load_latest_trend_backtest_report() -> Dict:
    """加载最新一条回测报告（前端徽章用）"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM trend_backtest_reports ORDER BY predict_date DESC LIMIT 1')
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else {}
    except Exception as error:
        print(f'[趋势回测] 加载最新报告失败: {str(error)}')
        return {}


def load_stock_basic_info() -> Dict:
    _ck = 'stock_basic_info'
    _hit = _db_cache_get(_ck)
    if _hit is not None:
        return _hit
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT symbol, name FROM stock_basic_info')
        rows = cursor.fetchall()
        conn.close()
        _result = {row['symbol']: row['name'] for row in rows}
        _db_cache_set(_ck, _result, 60.0)
        return _result
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
                'INSERT INTO stock_basic_info (symbol, name) VALUES (?, ?)',
                (symbol, name)
            )
        conn.commit()
        conn.close()
        _db_cache_invalidate('stock_basic_info')
        return True
    except Exception as error:
        print(f'保存股票映射失败: {str(error)}')
        return False

def save_stock_industry(industry_map: Dict[str, str]) -> bool:
    """批量更新 symbol → industry（增量更新，不删除已有名称）（2026-06-30）"""
    if not industry_map:
        return False
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        for symbol, industry in industry_map.items():
            if not industry:
                continue
            cursor.execute(
                "UPDATE stock_basic_info SET industry = ?, updated_at = datetime('now', '+8 hours') WHERE symbol = ?",
                (industry, symbol.lower())
            )
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存股票行业映射失败: {str(error)}')
        return False
def add_stock_basic_info(symbol: str, name: str) -> bool:
    """增量写入单只股票基础信息（远程搜索回写用，2026-10-12）

    与 save_stock_basic_info（全表替换）区分：避免回写时重写整表。
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('''INSERT INTO stock_basic_info (symbol, name) VALUES (?, ?)
            ON CONFLICT(symbol) DO UPDATE SET name=excluded.name''', (symbol.lower(), name))
        conn.commit()
        conn.close()
        _db_cache_invalidate('stock_basic_info')
        return True
    except Exception as error:
        print(f'增量写入股票信息失败: {str(error)}')
        return False

def load_stock_alias_map() -> Dict:
    """加载名称搜索映射（名称 -> 代码）"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
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
    'main_capital_inflow': '主力净流入',  # （2026-06-30）：第 9 因子，配套展示中文
    'holder_structure': '筹码集中度',  # （2026-07-06）：第 10 因子，股东人数筹码面
    'rps': 'RPS相对强度',  # （2026-07-16）：第 11 因子，欧奈尔相对强度
    'fundamental': '基本面质量',  # （2026-07-16 P1a）：第 12 因子，ROE+净利润增速
    'smart_money': '聪明钱',  # （2026-07-16 P1b）：第 13 因子，龙虎榜净买+北向
}

def save_holder_num_batch(records: List[Dict]) -> bool:
    """批量保存股东人数数据（UPSERT 语义，2026-07-06）

    Args:
        records: [{'symbol', 'end_date', 'holder_total_num', 'num_change',
                   'total_num_ratio', 'avg_free_shares', 'avg_hold_amt',
                   'notice_date', 'updated_at'}]
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        for r in records:
            cursor.execute('''INSERT OR REPLACE INTO stock_holder_num
                (symbol, end_date, holder_total_num, num_change, total_num_ratio,
                 avg_free_shares, avg_hold_amt, notice_date, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (r.get('symbol', ''), r.get('end_date', ''),
                 r.get('holder_total_num'), r.get('num_change'),
                 r.get('total_num_ratio'), r.get('avg_free_shares'),
                 r.get('avg_hold_amt'), r.get('notice_date'),
                 r.get('updated_at', '')))
        conn.commit()
        conn.close()
        return True
    except Exception as _e:
        print(f'[数据库] 保存股东人数数据失败: {_e}')
        return False


def load_holder_num_batch(symbols: List[str]) -> Dict[str, Dict]:
    """批量查询每只股票最新一期股东人数（2026-07-06）

    Args:
        symbols: 股票代码列表（小写带前缀 sh600519）
    Returns:
        {symbol: {holder_total_num, num_change, total_num_ratio,
                  avg_free_shares, avg_hold_amt, end_date}}
    """
    if not symbols:
        return {}
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        placeholders = ','.join('?' * len(symbols))
        # 子查询取每只股票最新一期 end_date
        cursor.execute(f'''SELECT symbol, end_date, holder_total_num, num_change,
            total_num_ratio, avg_free_shares, avg_hold_amt
            FROM stock_holder_num
            WHERE symbol IN ({placeholders})
            AND end_date = (
                SELECT MAX(end_date) FROM stock_holder_num h2
                WHERE h2.symbol = stock_holder_num.symbol)''', symbols)
        rows = cursor.fetchall()
        conn.close()
        result = {}
        for row in rows:
            result[row['symbol']] = {
                'holder_total_num': row['holder_total_num'],
                'num_change': row['num_change'],
                'total_num_ratio': row['total_num_ratio'],
                'avg_free_shares': row['avg_free_shares'],
                'avg_hold_amt': row['avg_hold_amt'],
                'end_date': row['end_date'],
            }
        return result
    except Exception as _e:
        print(f'[数据库] 查询股东人数数据失败: {_e}')
        return {}


def save_fundamental_batch(records: List[Dict]) -> bool:
    """批量保存基本面质量数据（UPSERT 语义，2026-07-16 P1a）

    Args:
        records: [{'symbol', 'report_date', 'roe', 'net_profit_yoy', 'eps', 'updated_at'}]
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        for r in records:
            cursor.execute('''INSERT OR REPLACE INTO stock_fundamental
                (symbol, report_date, roe, net_profit_yoy, eps, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)''',
                (r.get('symbol', ''), r.get('report_date', ''),
                 r.get('roe'), r.get('net_profit_yoy'), r.get('eps'),
                 r.get('updated_at', '')))
        conn.commit()
        conn.close()
        return True
    except Exception as _e:
        print(f'[数据库] 保存基本面数据失败: {_e}')
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def load_fundamental_batch(symbols: List[str]) -> Dict[str, Dict]:
    """批量查询每只股票最新基本面质量数据（2026-07-16 P1a）

    Args:
        symbols: 股票代码列表（小写带前缀 sh600519）
    Returns:
        {symbol: {report_date, roe, net_profit_yoy, eps}}
    """
    if not symbols:
        return {}
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        placeholders = ','.join('?' * len(symbols))
        cursor.execute(f'''SELECT symbol, report_date, roe, net_profit_yoy, eps
            FROM stock_fundamental WHERE symbol IN ({placeholders})''', symbols)
        rows = cursor.fetchall()
        conn.close()
        result = {}
        for row in rows:
            result[row['symbol']] = {
                'report_date': row['report_date'],
                'roe': row['roe'],
                'net_profit_yoy': row['net_profit_yoy'],
                'eps': row['eps'],
            }
        return result
    except Exception as _e:
        print(f'[数据库] 查询基本面数据失败: {_e}')
        return {}


def save_valuation_batch(records: List[Dict]) -> int:
    """批量保存估值分位数据（UPSERT 语义，2026-07-16 P2c）

    Args:
        records: [{'symbol', 'pe', 'pb', 'pe_pct', 'pb_pct', 'updated_at'}]
    Returns:
        成功保存的条数
    """
    if not records:
        return 0
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        for r in records:
            cursor.execute('''INSERT OR REPLACE INTO stock_valuation
                (symbol, pe, pb, pe_pct, pb_pct, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)''',
                (r.get('symbol', ''), r.get('pe'), r.get('pb'),
                 r.get('pe_pct'), r.get('pb_pct'), r.get('updated_at', '')))
        conn.commit()
        n = len(records)
        conn.close()
        return n
    except Exception as _e:
        print(f'[数据库] 保存估值数据失败: {_e}')
        return 0


def load_valuation_batch(symbols: List[str]) -> List[Dict]:
    """批量查询每只股票估值分位（2026-07-16 P2c）

    Args:
        symbols: 股票代码列表（小写带前缀 sh600519）
    Returns:
        [{'symbol', 'pe', 'pb', 'pe_pct', 'pb_pct'}]
    """
    if not symbols:
        return []
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        placeholders = ','.join('?' * len(symbols))
        cursor.execute(f'''SELECT symbol, pe, pb, pe_pct, pb_pct
            FROM stock_valuation WHERE symbol IN ({placeholders})''', symbols)
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as _e:
        print(f'[数据库] 查询估值数据失败: {_e}')
        return []


def load_strategy_factor_weights() -> Dict:
    """加载动态权重，如果数据库无数据则返回默认权重"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT factor_name, weight, accuracy, sample_count, correct_count FROM strategy_factor_weights')
        rows = cursor.fetchall()
        conn.close()
        if not rows:
            return {k: {'weight': v, 'accuracy': 50.0, 'sample_count': 0, 'correct_count': 0}
                    for k, v in DEFAULT_WEIGHTS.items()}
        result = {}
        for row in rows:
            factor_name = row['factor_name']
            # 过滤掉数据库中的孤儿因子（不再使用的旧 key），避免前端显示英文 key
            if factor_name not in DEFAULT_WEIGHTS:
                continue
            result[factor_name] = {
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
            # 跳过非法因子 key（防止前端传入未识别的英文 key 写入数据库）
            if factor_name not in DEFAULT_WEIGHTS:
                continue
            weight = data.get('weight', 1.0)
            accuracy = data.get('accuracy', 50.0)
            sample_count = data.get('sample_count', 0)
            correct_count = data.get('correct_count', 0)
            cursor.execute(
                '''INSERT OR REPLACE INTO strategy_factor_weights (factor_name, weight, accuracy, sample_count, correct_count)
                   VALUES (?, ?, ?, ?, ?)''',
                (factor_name, weight, accuracy, sample_count, correct_count)
            )
        # 清理数据库中已不存在的孤儿因子（防止历史脏数据残留）
        valid_keys = tuple(DEFAULT_WEIGHTS.keys())
        if valid_keys:
            placeholders = ','.join(['?'] * len(valid_keys))
            cursor.execute(
                f'DELETE FROM strategy_factor_weights WHERE factor_name NOT IN ({placeholders})',
                valid_keys
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
                '''INSERT OR REPLACE INTO strategy_predict_records
                   (predict_date, symbol, name, score, label, predict_direction,
                    outer_ratio, volume_ratio, turnover_rate, change_percent,
                    weibi, avg_price_deviation, amplitude, market_change,
                    position, main_capital_inflow, holder_structure)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (p['predict_date'], p['symbol'], p['name'], p['score'], p['label'],
                 p['predict_direction'], p.get('outer_ratio', 0), p.get('volume_ratio', 0),
                 p.get('turnover_rate', 0), p.get('change_percent', 0),
                 p.get('weibi', 0), p.get('avg_price_deviation', 0),
                 p.get('amplitude', 0), p.get('market_change', 0),
                 p.get('position', 0), p.get('main_capital_inflow', 0),
                 p.get('holder_structure', 0))
            )
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存预测记录失败: {str(error)}')
        return False
def list_user_ids() -> List[int]:
    """全部注册用户 id（调度器按用户循环写各自的池/自选时用）"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM users ORDER BY id')
        rows = cursor.fetchall()
        conn.close()
        return [r['id'] for r in rows]
    except Exception as error:
        print(f'读取用户列表失败: {str(error)}')
        return []


def load_user_scan_pool(user_id: Optional[int] = None) -> List[str]:
    """自定义扫描池。

    user_id=None → 所有用户的并集（调度器语义：扫描宇宙要覆盖每个人关注的票，
                   与"谁在查看"无关；漏传也只是退化成改造前的全局行为，不会丢数据）
    user_id=具体 → 该用户的池（前端展示与写入）
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        if user_id is None:
            cursor.execute('SELECT symbol FROM user_scan_pool GROUP BY symbol ORDER BY MIN(id)')
        else:
            cursor.execute('SELECT symbol FROM user_scan_pool WHERE user_id = ? ORDER BY id',
                           (int(user_id),))
        rows = cursor.fetchall()
        conn.close()
        return [row['symbol'] for row in rows]
    except Exception as error:
        print(f'加载自定义扫描池失败: {str(error)}')
        return []

def save_user_scan_pool(symbols: List[str], user_id: int = 1) -> bool:
    """整池覆盖写入（只动该用户的行，不再清空全表）"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM user_scan_pool WHERE user_id = ?', (int(user_id),))
        for symbol in symbols:
            cursor.execute(
                'INSERT OR IGNORE INTO user_scan_pool (user_id, symbol) VALUES (?, ?)',
                (int(user_id), symbol)
            )
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存自定义扫描池失败: {str(error)}')
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass
# ========== 用户持仓（2026-10-12，持仓管理+仓位配置；2026-10-12 起按用户隔离） ==========
def load_user_positions(user_id: int = 1) -> List[Dict]:
    """加载指定用户的全部持仓记录（user_id 缺省为 1，兼容旧调用）"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM user_positions WHERE user_id = ? ORDER BY id',
                       (int(user_id),))
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as error:
        print(f'加载持仓失败: {str(error)}')
        return []

def save_user_position(pos: Dict, user_id: int = 1) -> bool:
    """新增/更新一条持仓（按 (user_id, symbol) 去重，不同用户互不覆盖）"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('''INSERT INTO user_positions (user_id, symbol, name, cost_price, shares, buy_date, note, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now', '+8 hours'))
            ON CONFLICT(user_id, symbol) DO UPDATE SET
                name=excluded.name, cost_price=excluded.cost_price, shares=excluded.shares,
                buy_date=excluded.buy_date, note=excluded.note,
                updated_at=datetime('now', '+8 hours')''',
            (int(user_id), pos.get('symbol', '').lower(), pos.get('name', ''),
             float(pos.get('cost_price', 0) or 0), int(pos.get('shares', 0) or 0),
             pos.get('buy_date', ''), pos.get('note', '')))
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存持仓失败: {str(error)}')
        return False

def delete_user_position(symbol: str, user_id: int = 1) -> bool:
    """按 (user_id, symbol) 删除持仓 —— 只能删自己的"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM user_positions WHERE user_id = ? AND symbol = ?',
                       (int(user_id), symbol.lower()))
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        return deleted > 0
    except Exception as error:
        print(f'删除持仓失败: {str(error)}')
        return False


def get_user_capital(user_id: int = 1) -> float:
    """读账号初始本金；未设置返回 0（调用方据此回退到旧的"市值/市值"口径）"""
    try:
        conn = get_db_conn()
        row = conn.execute('SELECT principal FROM user_capital WHERE user_id = ?',
                           (int(user_id),)).fetchone()
        conn.close()
        return float(row['principal'] or 0) if row else 0.0
    except Exception as error:
        print(f'读取本金失败: {str(error)}')
        return 0.0

def save_user_capital(principal: float, user_id: int = 1, note: str = '') -> bool:
    """写入初始本金（每个账号一行，重复保存为更新）"""
    try:
        conn = get_db_conn()
        conn.execute('''INSERT INTO user_capital (user_id, principal, note, updated_at)
            VALUES (?, ?, ?, datetime('now', '+8 hours'))
            ON CONFLICT(user_id) DO UPDATE SET
                principal=excluded.principal, note=excluded.note,
                updated_at=datetime('now', '+8 hours')''',
            (int(user_id), float(principal), note))
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存本金失败: {str(error)}')
        return False


def get_user_realized_pnl(user_id: int = 1) -> float:
    """读账号累计已实现盈亏；无记录返回 0（从未减仓/清仓过）"""
    try:
        conn = get_db_conn()
        row = conn.execute('SELECT realized_pnl FROM user_realized_pnl WHERE user_id = ?',
                           (int(user_id),)).fetchone()
        conn.close()
        return round(float(row['realized_pnl'] or 0), 2) if row else 0.0
    except Exception as error:
        print(f'读取已实现盈亏失败: {str(error)}')
        return 0.0


def add_user_realized_pnl(delta: float, user_id: int = 1) -> float:
    """累计一笔已实现盈亏（减仓/清仓时调用），返回累计后的总值；失败返回 None"""
    if delta is None:
        return None
    try:
        conn = get_db_conn()
        conn.execute('''INSERT INTO user_realized_pnl (user_id, realized_pnl, trades, updated_at)
            VALUES (?, ?, 1, datetime('now', '+8 hours'))
            ON CONFLICT(user_id) DO UPDATE SET
                realized_pnl=ROUND(realized_pnl + excluded.realized_pnl, 2),
                trades=trades + 1,
                updated_at=datetime('now', '+8 hours')''',
            (int(user_id), round(float(delta), 2)))
        row = conn.execute('SELECT realized_pnl FROM user_realized_pnl WHERE user_id = ?',
                           (int(user_id),)).fetchone()
        conn.commit()
        conn.close()
        return round(float(row['realized_pnl'] or 0), 2) if row else None
    except Exception as error:
        print(f'累计已实现盈亏失败: {str(error)}')
        return None


_DEFAULT_HOT_STOCKS = [
    {'name': '深科技', 'code': 'sz000021'},
    {'name': '亨通光电', 'code': 'sh600487'},
    {'name': '中天科技', 'code': 'sh600522'},
    {'name': '东山精密', 'code': 'sz002384'},
    {'name': '光迅科技', 'code': 'sz002281'},
    {'name': '利通电子', 'code': 'sh603629'},
]


def load_hot_stock_buttons(user_id: Optional[int] = None) -> List[Dict]:
    """快捷查询自选（前端 chips）。

    user_id=None → 所有用户的并集（调度器语义：扫描宇宙覆盖每个人关注的票）
    user_id=具体 → 该用户的 chips
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        if user_id is None:
            cursor.execute('SELECT code, MAX(name) AS name FROM hot_stock_buttons '
                           'GROUP BY code ORDER BY MIN(id)')
        else:
            cursor.execute('SELECT code, name FROM hot_stock_buttons WHERE user_id = ? ORDER BY id',
                           (int(user_id),))
        rows = cursor.fetchall()
        conn.close()
        if rows:
            return [{'code': row['code'], 'name': row['name']} for row in rows]
        # 仅在全库为空（全新安装、第一次有人打开）时播下默认自选。
        # 用户自己清空过就不再自动加回——否则"删干净的列表"会被系统偷偷填满。
        if user_id is not None:
            conn = get_db_conn()
            total = conn.execute('SELECT COUNT(*) AS n FROM hot_stock_buttons').fetchone()['n']
            conn.close()
            if total == 0:
                save_hot_stock_buttons(_DEFAULT_HOT_STOCKS, user_id=user_id)
                return list(_DEFAULT_HOT_STOCKS)
        return []
    except Exception as error:
        print(f'加载热门股票失败: {str(error)}')
        return []

def save_hot_stock_buttons(stocks: List[Dict], user_id: int = 1) -> bool:
    """整列表覆盖写入（只动该用户的行）"""
    conn = None
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM hot_stock_buttons WHERE user_id = ?', (int(user_id),))
        for stock in stocks:
            if isinstance(stock, dict) and stock.get('code') and stock.get('name'):
                cursor.execute(
                    'INSERT OR IGNORE INTO hot_stock_buttons (user_id, code, name) VALUES (?, ?, ?)',
                    (int(user_id), stock['code'].lower(), stock['name'])
                )
        conn.commit()
        return True
    except Exception as error:
        print(f'保存热门股票失败: {str(error)}')
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass

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
        cursor = conn.cursor()
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
                'INSERT INTO stock_concept_tags (symbol, tags_json) VALUES (?, ?)',
                (symbol, json.dumps(tag_list, ensure_ascii=False))
            )
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存股票标签失败: {str(error)}')
        return False
def load_latest_focus_plan() -> Dict:
    """最新一条收评的「明日重点关注」（行动台盘后/休市段的主数据源）。

    只取一行 + 只解析 focus_json，避免 load_daily_market_reviews 那样把最近 N 天
    收评的全部 JSON（板块/涨停/资金榜…）都解一遍。

    Returns:
        {'date': 'YYYY-MM-DD' | '', 'items': [...]}  # items 内含 buyTiming / tomorrowPlan
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT review_date, focus_json FROM daily_market_reviews '
                       'ORDER BY review_date DESC LIMIT 1')
        row = cursor.fetchone()
        conn.close()
        if not row:
            return {'date': '', 'items': []}
        items = []
        if 'focus_json' in row.keys() and row['focus_json']:
            try:
                items = json.loads(row['focus_json']) or []
            except Exception:
                items = []
        return {'date': row['review_date'] or '', 'items': items}
    except Exception as error:
        print(f'加载明日关注计划失败: {str(error)}')
        return {'date': '', 'items': []}


def load_daily_market_reviews() -> List[Dict]:
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute(f'SELECT * FROM daily_market_reviews ORDER BY review_date DESC LIMIT {QUERY_MARKET_REVIEWS_LIMIT}')
        rows = cursor.fetchall()
        conn.close()

        reviews = []
        for row in rows:
            review = {
                'date': row['review_date'],
                'timestamp': row['created_at'].isoformat() if row['created_at'] and hasattr(row['created_at'], 'isoformat') else (str(row['created_at']) if row['created_at'] else ''),
                'summary': row['summary'] or '',
                'market': json.loads(row['market_json']) if row['market_json'] else {},
            }
            # 兼容新旧格式
            if 'industry_json' in row.keys() and row['industry_json']:
                review['industrySectors'] = json.loads(row['industry_json'])
            else:
                review['industrySectors'] = []
            if 'concept_json' in row.keys() and row['concept_json']:
                review['conceptSectors'] = json.loads(row['concept_json'])
            else:
                review['conceptSectors'] = []
            if 'limit_up_json' in row.keys() and row['limit_up_json']:
                review['limitUpStocks'] = json.loads(row['limit_up_json'])
            else:
                review['limitUpStocks'] = []
            if 'limit_down_json' in row.keys() and row['limit_down_json']:
                review['limitDownStocks'] = json.loads(row['limit_down_json'])
            else:
                review['limitDownStocks'] = []
            if 'top_gainers_json' in row.keys() and row['top_gainers_json']:
                review['topGainers'] = json.loads(row['top_gainers_json'])
            else:
                review['topGainers'] = []
            if 'top_losers_json' in row.keys() and row['top_losers_json']:
                review['topLosers'] = json.loads(row['top_losers_json'])
            else:
                review['topLosers'] = []
            if 'focus_json' in row.keys() and row['focus_json']:
                review['tomorrowFocus'] = json.loads(row['focus_json'])
            else:
                review['tomorrowFocus'] = []
            # 明日关注的整篮子预期（2026-10-14）：旧数据没这一列，前端按 v-if 自动降级
            if 'focus_meta_json' in row.keys() and row['focus_meta_json']:
                try:
                    review['focusExpect'] = json.loads(row['focus_meta_json'])
                except Exception:
                    review['focusExpect'] = None
            if 'top_funds_json' in row.keys() and row['top_funds_json']:
                review['topFunds'] = json.loads(row['top_funds_json'])
            else:
                review['topFunds'] = []
            if 'ai_analysis_json' in row.keys() and row['ai_analysis_json']:
                try:
                    review['aiAnalysis'] = json.loads(row['ai_analysis_json'])
                except Exception:
                    review['aiAnalysis'] = None
            else:
                review['aiAnalysis'] = None
            # 短线情绪周期（2026-09-04 新增；历史数据无此字段时前端不渲染该区块）
            if 'sentiment_cycle_json' in row.keys() and row['sentiment_cycle_json']:
                try:
                    review['sentimentCycle'] = json.loads(row['sentiment_cycle_json'])
                except Exception:
                    review['sentimentCycle'] = None
            # 涨停/跌停真实总数（2026-07-30 补齐）；若缺失则降级回数组长度（历史数据）
            if 'limit_up_count' in row.keys() and row['limit_up_count'] is not None:
                review['limitUpCount'] = int(row['limit_up_count'])
            else:
                review['limitUpCount'] = len(review.get('limitUpStocks', []))
            if 'limit_down_count' in row.keys() and row['limit_down_count'] is not None:
                review['limitDownCount'] = int(row['limit_down_count'])
            else:
                review['limitDownCount'] = len(review.get('limitDownStocks', []))
            # 旧格式兼容
            if 'trend_json' in row.keys() and row['trend_json']:
                review['trendStocks'] = json.loads(row['trend_json'])
            if 'pool_json' in row.keys() and row['pool_json']:
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
            # 整篮子预期（胜率/平均收益/被过滤掉几只）。不落库的话，前端刷新一次
            # 就再也算不出 dropped —— 候选总数在选完之后已经不在这份名单里了。
            _focus_expect = review.get('focusExpect')
            focus_meta_json = json.dumps(_focus_expect, ensure_ascii=False) if _focus_expect else None
            top_funds_json = json.dumps(review.get('topFunds', []), ensure_ascii=False)
            trend_json = json.dumps(review.get('trendStocks', {}), ensure_ascii=False)
            pool_json = json.dumps(review.get('stockPool', {}), ensure_ascii=False)
            ai_analysis_json = json.dumps(review.get('aiAnalysis'), ensure_ascii=False) if review.get('aiAnalysis') else None
            sentiment_cycle_json = json.dumps(review.get('sentimentCycle'), ensure_ascii=False) if review.get('sentimentCycle') else None
            # 真实涨停/跌停总数：优先读 count 字段，否则退化为数组长度（兼容旧数据）
            limit_up_count = review.get('limitUpCount')
            if limit_up_count is None:
                limit_up_count = len(review.get('limitUpStocks', []))
            limit_down_count = review.get('limitDownCount')
            if limit_down_count is None:
                limit_down_count = len(review.get('limitDownStocks', []))
            cursor.execute('''
                INSERT OR REPLACE INTO daily_market_reviews (review_date, summary, market_json, trend_json, pool_json, industry_json, concept_json, limit_up_json, limit_down_json, top_gainers_json, top_losers_json, focus_json, top_funds_json, ai_analysis_json, sentiment_cycle_json, focus_meta_json, limit_up_count, limit_down_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (review_date, summary, market_json, trend_json, pool_json, industry_json, concept_json, limit_up_json, limit_down_json, top_gainers_json, top_losers_json, focus_json, top_funds_json, ai_analysis_json, sentiment_cycle_json, focus_meta_json, limit_up_count, limit_down_count))
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存收评失败: {str(error)}')
        import traceback
        print(f'[收评保存] 错误堆栈: {traceback.format_exc()}')
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass
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
# ========== K线历史缓存（2026-07-20 性能优化 A1）==========
def load_kline_cache(symbol: str, trade_date: str) -> List:
    """加载某股票指定交易日的日线历史缓存

    Args:
        symbol: 小写带前缀 sh600519
        trade_date: 交易日 YYYY-MM-DD
    Returns:
        成功返回日线列表（同 get_kline_data 的 qfqday 结构），未命中返回 None
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT kline_json FROM kline_cache WHERE symbol = ? AND trade_date = ?',
            (symbol, trade_date))
        row = cursor.fetchone()
        conn.close()
        if row and row['kline_json']:
            return json.loads(row['kline_json'])
        return None
    except Exception as _e:
        print(f'[数据库] 加载K线缓存失败: {_e}')
        return None


def save_kline_cache(symbol: str, trade_date: str, kline: List, updated_at: str = '') -> bool:
    """保存某股票指定交易日的日线历史缓存（UPSERT 语义）

    Args:
        symbol: 小写带前缀 sh600519
        trade_date: 交易日 YYYY-MM-DD
        kline: 日线列表
    """
    if not kline:
        return False
    try:
        from datetime import datetime
        updated_at = updated_at or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('''INSERT OR REPLACE INTO kline_cache
            (symbol, trade_date, kline_json, updated_at)
            VALUES (?, ?, ?, ?)''',
            (symbol, trade_date, json.dumps(kline, ensure_ascii=False), updated_at))
        conn.commit()
        conn.close()
        return True
    except Exception as _e:
        print(f'[数据库] 保存K线缓存失败: {_e}')
        return False


def save_fund_flow_snapshot(snap_key: str, data) -> bool:
    """保存资金流向某区块（个股/板块/非热点上涨股）最后成功快照（UPSERT）。

    用途：服务重启会清空内存兜底 _LAST_GOOD_*，持久化后重启仍可回退到最后成功数据，
    覆盖「收盘后东财 push2 风控拒连 → 页面全空」的场景（收盘后数据本应保持不变）。
    """
    if not data:
        return False
    conn = None
    try:
        from datetime import datetime
        conn = get_db_conn()
        conn.execute('''INSERT INTO fund_flow_snapshot (snap_key, snap_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(snap_key) DO UPDATE SET
                snap_json=excluded.snap_json, updated_at=excluded.updated_at''',
            (snap_key, json.dumps(data, ensure_ascii=False),
             datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        return True
    except Exception as _e:
        print(f'[数据库] 保存资金流向快照失败({snap_key}): {_e}')
        return False
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def load_fund_flow_snapshot(snap_key: str):
    """读取资金流向某区块的最后成功快照，无记录/解析失败返回 None。"""
    conn = None
    try:
        conn = get_db_conn()
        row = conn.execute(
            'SELECT snap_json FROM fund_flow_snapshot WHERE snap_key = ?', (snap_key,)).fetchone()
        return json.loads(row['snap_json']) if row else None
    except Exception as _e:
        print(f'[数据库] 读取资金流向快照失败({snap_key}): {_e}')
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def save_westock_snapshot(date_key: str, rows: List[Dict]) -> int:
    """保存某交易日 westock 快照（逐只 UPSERT，按 date+symbol 幂等）。

    rows: [{'symbol','name','close_price','change_pct','main_net_inflow',...}]
    返回写入条数。
    """
    from datetime import datetime
    if not rows:
        return 0
    conn = None
    try:
        conn = get_db_conn()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        n = 0
        for r in rows:
            sym = (r.get('symbol') or '').lower()
            if not sym:
                continue
            conn.execute('''
                INSERT INTO westock_snapshot
                    (date, symbol, name, close_price, change_pct, main_net_inflow,
                     jumbo_net_flow, mid_net_flow, retail_net_flow,
                     main_net_5d, main_net_10d, main_net_20d, industry_rank, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date, symbol) DO UPDATE SET
                    name=excluded.name, close_price=excluded.close_price,
                    change_pct=excluded.change_pct, main_net_inflow=excluded.main_net_inflow,
                    jumbo_net_flow=excluded.jumbo_net_flow, mid_net_flow=excluded.mid_net_flow,
                    retail_net_flow=excluded.retail_net_flow,
                    main_net_5d=excluded.main_net_5d, main_net_10d=excluded.main_net_10d,
                    main_net_20d=excluded.main_net_20d, industry_rank=excluded.industry_rank,
                    fetched_at=excluded.fetched_at''',
                         (date_key, sym, r.get('name'), r.get('close_price'),
                          r.get('change_pct'), r.get('main_net_inflow'),
                          r.get('jumbo_net_flow'), r.get('mid_net_flow'),
                          r.get('retail_net_flow'), r.get('main_net_5d'),
                          r.get('main_net_10d'), r.get('main_net_20d'),
                          r.get('industry_rank'), now))
            n += 1
        conn.commit()
        return n
    except Exception as e:
        print(f'[数据库] 保存 westock 快照失败: {e}')
        return 0
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def save_cross_check(date_key: str, rows: List[Dict]) -> int:
    """保存某交易日双源比对结果（UPSERT，authoritative=westock）。"""
    from datetime import datetime
    if not rows:
        return 0
    conn = None
    try:
        conn = get_db_conn()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        n = 0
        for r in rows:
            sym = (r.get('symbol') or '').lower()
            if not sym:
                continue
            # 三态：True→1 / False→0 / None（未比对）→ NULL（不与"不一致"混淆）
            qg = r.get('quote_agreed')
            qg_val = None if qg is None else (1 if qg else 0)
            dc = r.get('direction_conflict')
            dc_val = None if dc is None else (1 if dc else 0)
            conn.execute('''
                INSERT INTO data_cross_check
                    (date, symbol, name, our_change_pct, westock_change_pct, quote_agreed,
                     our_main_inflow, westock_main_inflow, inflow_diff, inflow_diff_pct,
                     direction_conflict, authoritative, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'westock', ?)
                ON CONFLICT(date, symbol) DO UPDATE SET
                    name=excluded.name, our_change_pct=excluded.our_change_pct,
                    westock_change_pct=excluded.westock_change_pct, quote_agreed=excluded.quote_agreed,
                    our_main_inflow=excluded.our_main_inflow,
                    westock_main_inflow=excluded.westock_main_inflow,
                    inflow_diff=excluded.inflow_diff, inflow_diff_pct=excluded.inflow_diff_pct,
                    direction_conflict=excluded.direction_conflict,
                    authoritative='westock', checked_at=excluded.checked_at''',
                         (date_key, sym, r.get('name'), r.get('our_change_pct'),
                          r.get('westock_change_pct'), qg_val,
                          r.get('our_main_inflow'), r.get('westock_main_inflow'),
                          r.get('inflow_diff'), r.get('inflow_diff_pct'),
                          dc_val, now))
            n += 1
        conn.commit()
        return n
    except Exception as e:
        print(f'[数据库] 保存双源比对失败: {e}')
        return 0
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def load_westock_snapshot(date_key: str) -> List[Dict]:
    """读取某交易日 westock 快照（无记录返回 []）。"""
    conn = None
    try:
        conn = get_db_conn()
        rows = conn.execute(
            'SELECT * FROM westock_snapshot WHERE date = ?', (date_key,)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f'[数据库] 读取 westock 快照失败: {e}')
        return []
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def load_cross_check(date_key: str) -> List[Dict]:
    """读取某交易日双源比对结果（无记录返回 []）。"""
    conn = None
    try:
        conn = get_db_conn()
        rows = conn.execute(
            'SELECT * FROM data_cross_check WHERE date = ?', (date_key,)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f'[数据库] 读取双源比对失败: {e}')
        return []
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def load_hot_search_ranking() -> Dict:
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
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
                'hotRank': row['hot_rank'] if 'hot_rank' in row.keys() else 0
            })

        return {
            'updateTime': meta.get('updateTime', ''),
            'total': int(meta.get('total', 0)),
            'filteredCount': int(meta.get('filteredCount', 0)),
            'source': meta.get('source', ''),
            'stocks': stocks
        }
    except Exception as error:
        print(f'加载hot_search_ranking数据失败: {str(error)}')
        return {'updateTime': '', 'total': 0, 'filteredCount': 0, 'stocks': []}

def save_hot_search_ranking(data: Dict) -> bool:
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        # 2026-10-23：熔断/降级时抓不到任何榜单，不能拿空结果覆盖上一份可用缓存。
        # 否则封禁期一次服务重启（启动时会拉热搜）就会把当日榜单清成 0 只，
        # 连当日快照也一起删掉——数据看起来"更新成功"，实际是把可用数据擦掉。
        if not (data.get('stocks') or []):
            existing = cursor.execute('SELECT COUNT(*) FROM hot_search_ranking').fetchone()[0]
            if existing:
                conn.close()
                print(f'[热搜榜] 本轮结果为空（熔断/降级），保留已有 {existing} 只缓存，'
                      f'不覆盖也不删当日快照')
                return False
        cursor.execute('DELETE FROM hot_search_ranking')
        cursor.execute('DELETE FROM hot_search_meta')
        for key, value in [('updateTime', data.get('updateTime', '')),
                           ('total', str(data.get('total', 0))),
                           ('filteredCount', str(data.get('filteredCount', 0))),
                           ('source', str(data.get('source', '') or ''))]:
            cursor.execute(
                'INSERT INTO hot_search_meta (meta_key, meta_value) VALUES (?, ?)',
                (key, value)
            )
        for idx, stock in enumerate(data.get('stocks', [])):
            cursor.execute('''
                INSERT INTO hot_search_ranking (symbol, code, name, market, price, change_percent, change_val, hot_rank, sort_order)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            cursor.execute('DELETE FROM hot_search_daily_snapshot WHERE snapshot_date = ?', (today_str,))
            for stock in data.get('stocks', []):
                if stock.get('hotRank', 0) <= HOT_SEARCH_TOP_N:
                    cursor.execute('''
                        INSERT OR REPLACE INTO hot_search_daily_snapshot (snapshot_date, symbol, name, hot_rank)
                        VALUES (?, ?, ?, ?)
                    ''', (today_str, stock.get('symbol', ''), stock.get('name', ''), stock.get('hotRank', 0)))
        except Exception as snap_err:
            print(f'保存热搜快照失败(非致命): {snap_err}')
        cutoff = (date.today() - timedelta(days=HOT_SEARCH_SNAPSHOT_CLEANUP_DAYS)).isoformat()
        try:
            cursor.execute('DELETE FROM hot_search_daily_snapshot WHERE snapshot_date < ?', (cutoff,))
        except Exception:
            pass
        conn.commit()
        conn.close()
        _db_cache_invalidate('hot_search_ranking')
        return True
    except Exception as error:
        print(f'保存hot_search_ranking数据失败: {str(error)}')
        return False


# ========== 自动优化引擎 CRUD（V4.4）==========

def load_trend_dimension_weights() -> Dict:
    """加载 D1-D4 趋势维度权重（无数据时返回 1.0 默认值）"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM trend_dimension_weights')
        rows = cursor.fetchall()
        conn.close()
        result = {
            'd1': {'weight': 1.0, 'accuracy': 50.0, 'discrimination': 0, 'sample_count': 0, 'correct_count': 0},
            'd2': {'weight': 1.0, 'accuracy': 50.0, 'discrimination': 0, 'sample_count': 0, 'correct_count': 0},
            'd3': {'weight': 1.0, 'accuracy': 50.0, 'discrimination': 0, 'sample_count': 0, 'correct_count': 0},
            'd4': {'weight': 1.0, 'accuracy': 50.0, 'discrimination': 0, 'sample_count': 0, 'correct_count': 0},
        }
        for row in rows:
            d = row['dimension'].lower()
            if d in result:
                result[d] = {
                    'weight': float(row['weight']),
                    'accuracy': float(row['accuracy']),
                    'discrimination': float(dict(row).get('discrimination') or 0),
                    'sample_count': int(row['sample_count']),
                    'correct_count': int(row['correct_count']),
                }
        return result
    except Exception as error:
        print(f'加载趋势维度权重失败: {str(error)}')
        return {'d1': {'weight': 1.0}, 'd2': {'weight': 1.0}, 'd3': {'weight': 1.0}, 'd4': {'weight': 1.0}}


def save_trend_dimension_weights(weights_data: Dict) -> bool:
    """保存 D1-D4 趋势维度权重"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        for dim_key, data in weights_data.items():
            weight = data.get('weight', 1.0)
            accuracy = data.get('accuracy', 0)
            discrimination = data.get('discrimination', 0)
            sample_count = data.get('sample_count', 0)
            correct_count = data.get('correct_count', 0)
            cursor.execute('''
                INSERT OR REPLACE INTO trend_dimension_weights
                    (dimension, weight, accuracy, discrimination, sample_count, correct_count, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now', '+8 hours'))
            ''', (dim_key, weight, accuracy, discrimination, sample_count, correct_count))
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存趋势维度权重失败: {str(error)}')
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ===== 角色体系已于 2026-10-28 整体删除 =====
# load_role_thresholds / save_role_thresholds / load_role_performance / save_role_performance
# 四个函数随之移除；老库里的 role_thresholds / role_performance 两张表保留但不再读写。


def save_optimization_history(module: str, parameter_set: str = '',
                              metric_name: str = '', metric_value: float = 0,
                              old_value: float = None, new_value: float = None,
                              details: str = '') -> bool:
    """保存优化历史"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO optimization_history (module, parameter_set, metric_name, metric_value,
                                              old_value, new_value, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (module, parameter_set, metric_name, metric_value,
              old_value, new_value, details))
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存优化历史失败: {str(error)}')
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def load_optimization_history(module: str = None, limit: int = 50) -> List[Dict]:
    """加载优化历史"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        if module:
            cursor.execute(
                'SELECT * FROM optimization_history WHERE module = ? ORDER BY created_at DESC LIMIT ?',
                (module, limit)
            )
        else:
            cursor.execute(
                'SELECT * FROM optimization_history ORDER BY created_at DESC LIMIT ?',
                (limit,)
            )
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as error:
        print(f'加载优化历史失败: {str(error)}')
        return []


# ========== 接口用量统计（2026-10-19） ==========
def save_api_usage_daily(rows) -> int:
    """批量 upsert 当日接口调用计数。rows = [(date, provider, interface, count, ok, fail), ...]"""
    if not rows:
        return 0
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.executemany(
            '''INSERT INTO api_usage_daily(date, provider, interface, count, ok, fail)
               VALUES(?, ?, ?, ?, ?, ?)
               ON CONFLICT(date, provider, interface) DO UPDATE SET
                 count=excluded.count, ok=excluded.ok, fail=excluded.fail''',
            rows,
        )
        conn.commit()
        conn.close()
        return len(rows)
    except Exception as error:
        print(f'保存接口用量失败: {str(error)}')
        return 0


def load_api_usage_today(date_str: str) -> List[Dict]:
    """读取某一天的接口用量计数（install 时回填内存，避免重启丢当天累计）。"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        rows = cursor.execute(
            'SELECT * FROM api_usage_daily WHERE date = ?', (date_str,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as error:
        print(f'读取当日接口用量失败: {str(error)}')
        return []


def load_api_usage_history(days: int = 14) -> List[Dict]:
    """读取近 N 天接口用量（按 date 升序），供趋势图。"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        rows = cursor.execute(
            '''SELECT * FROM api_usage_daily
               WHERE date >= date('now', ?)
               ORDER BY date ASC''',
            (f'-{days - 1} days',),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as error:
        print(f'读取接口用量历史失败: {str(error)}')
        return []


def save_api_usage_limit(provider: str, interface: str, limit: int, updated_by: str = '') -> bool:
    """保存（upsert）某接口的每日上限。"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO api_usage_limits(provider, interface, daily_limit, updated_by, updated_at)
               VALUES(?, ?, ?, ?, datetime('now', '+8 hours'))
               ON CONFLICT(provider, interface) DO UPDATE SET
                 daily_limit=excluded.daily_limit, updated_by=excluded.updated_by,
                 updated_at=datetime('now', '+8 hours')''',
            (provider, interface, int(limit), updated_by),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存接口上限失败: {str(error)}')
        return False


def load_api_usage_limits() -> List[Dict]:
    """读取所有已持久化的接口上限。"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        rows = cursor.execute('SELECT * FROM api_usage_limits').fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as error:
        print(f'读取接口上限失败: {str(error)}')
        return []
