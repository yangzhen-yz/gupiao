"""DAL domain: schema / migrations (DB bootstrap)."""

from ..dal_state import *

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
    conn = get_db_conn(wait_for_lock=True)
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
        # 批次留痕（2026-11-01）：daily_recommendations 是"当日最后一次推荐"的快照，
        # 每批保存都 DELETE+INSERT 覆盖当天——09:45/10:00/13:30/14:05 的四批内容与
        # 14:45 收盘前那一批全被抹掉（实测 10-31 的 241 行 created_at 全是 14:45:10）。
        # 于是"上午推的是A、下午换成B"这件事事后无从考证，复盘只能拿最后一批判"系统
        # 选得准不准"，把"盘中换股换对了还是换错了"整个维度丢了。
        # 现在每批另存一行到 recommendation_batches（只追加不覆盖），行里带 batch_ref。
        if 'batch_ref' not in _rec_cols:
            cursor.execute("ALTER TABLE daily_recommendations ADD COLUMN batch_ref INTEGER DEFAULT NULL")
            print('[数据库] daily_recommendations 已补 batch_ref 列（指向 recommendation_batches）')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS recommendation_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rec_date TEXT NOT NULL,
                batch_time TEXT NOT NULL,
                source TEXT DEFAULT '',
                stock_count INTEGER DEFAULT 0,
                payload_hash TEXT DEFAULT '',
                stocks_json TEXT DEFAULT '',
                created_at DATETIME DEFAULT (datetime('now', '+8 hours')))''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_rec_batches_date '
                       'ON recommendation_batches(rec_date, id)')

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
        # 主题篮子成分日线（2026-10-31）：人形机器人链 39 只等权指数用。
        # 本地 stock_daily 只同步主板选股范围，创业板/科创板 22 只停更；
        # 这张表由盘后 15:05/18:30 用腾讯批量行情（qt.gtimg.cn，含昨收/最高/最低/成交额）
        # 追加当日 OHLC，不依赖已被 WAF 拦掉的 ifzq 日K接口。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS theme_member_daily (
                theme TEXT NOT NULL,             -- robot / ...
                symbol TEXT NOT NULL,            -- 小写带前缀 sz300580
                date TEXT NOT NULL,              -- YYYY-MM-DD
                name TEXT,
                open REAL, high REAL, low REAL, close REAL,
                prev_close REAL,                 -- 交易所口径昨收（除权日自动调整）
                amount REAL,                     -- 成交额（元）
                volume REAL,                     -- 成交量（股）
                source TEXT,                     -- tencent_quote / backfill
                created_at TEXT,
                PRIMARY KEY (theme, symbol, date))''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_theme_member_daily
            ON theme_member_daily (theme, date)''')
        # 财联社四池逐票日表（2026-10-31，影子）：涨停/连板/炸板/跌停各存一份。
        # 用途是"题材梯队"落档与后续一周对照；财联社网页接口不支持历史日期，
        # 所以只能从上线当天起逐日留档，不做回填。pool 值沿用接口名：
        # up_pool / continuous_up_pool / up_open_pool / down_pool。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cls_pool_daily (
                trade_date TEXT NOT NULL,
                pool       TEXT NOT NULL,        -- up / continuous / up_open / down
                symbol     TEXT NOT NULL,        -- 六位代码（与本地库一致）
                code       TEXT,                 -- 财联社原始代码 sh605338
                name       TEXT,
                change_pct REAL,
                last_px    REAL,
                seal_time  TEXT,                 -- 首封/最后触及时间，原始 YYYY-MM-DD HH:MM:SS
                days       INTEGER,              -- 连板数（跌停池为空）
                is_st      INTEGER,
                reason     TEXT,                 -- up_reason 原文
                themes     TEXT,                 -- JSON 数组（up_reason 前缀切分）
                plates     TEXT,                 -- JSON 数组（财联社板块标签）
                num_stocks INTEGER,              -- 该池当日总只数
                source     TEXT DEFAULT 'cls',
                created_at TEXT,
                PRIMARY KEY (trade_date, pool, symbol))''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_cls_pool_daily
            ON cls_pool_daily (trade_date, pool)''')
        # 财联社板块榜日快照（2026-10-31，展示兜底）：行业 54 / 概念 398。
        # 用途是资金流页概念板块与收评板块轮动的兜底，以及逐日留档；
        # 财联社网页接口不支持历史日期（date 参数无效），所以只能从上线起逐日写。
        # 口径与东财/申万不同，不参与选股与门槛。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cls_plate_daily (
                trade_date    TEXT NOT NULL,
                plate_type    TEXT NOT NULL,     -- industry / concept
                code          TEXT NOT NULL,     -- cls80014
                name          TEXT,
                change_pct    REAL,
                main_fund_diff REAL,             -- 元
                up_count      INTEGER,
                down_count    INTEGER,
                limit_up_num  INTEGER,
                limit_down_num INTEGER,
                first_stock   TEXT,              -- JSON {name, code, changePct}
                source        TEXT DEFAULT 'cls',
                created_at    TEXT,
                PRIMARY KEY (trade_date, plate_type, code))''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_cls_plate_daily
            ON cls_plate_daily (trade_date, plate_type)''')
        # 龙虎榜逐票落库（2026-11-06，用户要求"停调不能导致后续功能受影响"里的一条）。
        # 此前龙虎榜**只在内存 `_dt_cache` 里**，进程重启即空、东财停调即整块没有：
        # 它是评分的第 13 因子 smart_money（recommend/trend/scan 三处读），也是龙虎榜页
        # 的全部内容 —— 是所有"只有东财一条路"的数据里唯一连旧值都留不住的。
        # 粒度取接口原始粒度 (交易日, 股票, 上榜原因)：一只票一天可能因多个席位口径上榜，
        # 压成一行会丢 reasons，聚合时又要把 reasons 复原，不如按原样存。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS dragon_tiger_daily (
                trade_date   TEXT NOT NULL,      -- YYYY-MM-DD（接口 TRADE_DATE）
                symbol       TEXT NOT NULL,      -- 带市场前缀 sh600000（与本地库一致）
                name         TEXT,
                reason       TEXT NOT NULL,      -- EXPLANATION 原文，空原因记 ''
                net_amt      REAL,               -- 元（BILLBOARD_NET_AMT）
                buy_amt      REAL,
                sell_amt     REAL,
                change_rate  REAL,
                close_price  REAL,
                float_mcap   REAL,               -- 元（FREE_MARKET_CAP）
                source       TEXT DEFAULT 'eastmoney',
                created_at   TEXT,
                PRIMARY KEY (trade_date, symbol, reason))''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_dragon_tiger_daily
            ON dragon_tiger_daily (trade_date)''')
        # 动态热门板块链（2026-10-31，行动台主题卡）：当天从财联社概念榜选出的前 3 个板块
        # 及核心成分。只做展示，不进选股；成分明细同时写 theme_member_daily（chain_id 作 theme）。
        # 落库目的有二：重启后先显示最近一次的链（不必等接口），以及回溯"当天为什么是它"。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS theme_chain_daily (
                trade_date      TEXT NOT NULL,
                chain_id        TEXT NOT NULL,     -- cls80014
                rank            INTEGER,
                name            TEXT,              -- 白酒链
                plate_code      TEXT,              -- cls80014
                plate_name      TEXT,
                change_pct      REAL,
                limit_up_num    INTEGER,
                breadth         INTEGER,           -- 上涨+下跌家数
                member_count    INTEGER,
                members_json    TEXT,              -- [[symbol, name], ...]
                note            TEXT,
                source          TEXT DEFAULT 'cls',
                created_at      TEXT,
                PRIMARY KEY (trade_date, chain_id))''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_theme_chain_daily
            ON theme_chain_daily (trade_date)''')
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

        # 持仓交易流水（2026-11-04 建，卖出侧三条主线的地基）。
        # position_adjust.py 当初**刻意不建**这张表，理由是"多一张表就多一处要和
        # user_positions 对齐的状态"——这个顾虑站得住，所以做法是改掉它的前提，不是无视：
        #   · 本表是**只追加的旁路留痕**，没有任何决策读它（持仓仍只由 user_positions 决定），
        #     所以某行没写进去也不会算错持仓或买卖，代价上限是"少一条审计"；
        #   · 写入一律包在 try 里、失败只打日志，绝不向上抛（见 routes 的三个接入点）；
        #   · 独立开关 POSITION_TRADE_LOG_ENABLED，关掉后一行都不写，逐字节等于改造前。
        # 为什么现在必须有：#11 该减多少、#12 抗跌阈值、#13 结构确认的判据全靠"卖出之后的结果"，
        # 而 note 是自由文本且截断 200 字符，回放不出"哪天买、成本多少、持有多少天"。
        # source 必须分开记（见 save_position_trade_log）：回测只用 adjust_*，edit/delete 只作审计——
        # 因为「编辑持仓」与「改历史」在代码层面区分不了，混进回测会造出根本不存在的买卖。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS position_trade_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                ts DATETIME DEFAULT (datetime('now', '+8 hours')),
                user_id INTEGER NOT NULL DEFAULT 1,
                symbol TEXT NOT NULL,
                name TEXT DEFAULT '',
                source TEXT NOT NULL,
                shares_before INTEGER,
                price REAL,
                qty INTEGER,
                shares_after INTEGER,
                cost_price_after REAL,
                realized_pnl REAL,
                note TEXT DEFAULT '')''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_ptl_user_symbol_date ON position_trade_log(user_id, symbol, trade_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_ptl_date ON position_trade_log(trade_date)')

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


def daily_recs_write_version() -> int:
    """当前推荐表写版本号（进程内单调；缓存校验用，不落库）。"""
    return _DAILY_RECS_WRITE_VERSION


def save_api_usage_daily(rows) -> int:
    """批量 upsert 当日接口调用计数。rows = [(date, provider, interface, count, ok, fail), ...]"""
    if not rows:
        return 0
    try:
        # 2026-11-03 从 fast_fail 摘回 30 秒（昨天分类分错了）：这不是"可重拉的缓存"，
        # 而是接口调用量的**审计留痕**——它是"东财 fail 比率为什么暴涨""哪个调用方没接闸门"
        # 这些判断的唯一依据，丢一天就永久补不回来（计数是累加值，没有源头可重算）。
        conn = get_db_conn(wait_for_lock=True)
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
        conn = get_db_conn(wait_for_lock=True)
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


__all__ = ['_prepare_ts_migration', '_migrate_sim_fk', '_finish_ts_migration', 'init_db']
