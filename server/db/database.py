"""database.py - split from main.py"""
import sqlite3, json
from typing import List, Dict
from datetime import date, timedelta
from app.config import SQLITE_DB_PATH, QUERY_DAILY_RECOMMENDATIONS_LIMIT, QUERY_TREND_RESULTS_LIMIT, QUERY_MARKET_REVIEWS_LIMIT, HOT_SEARCH_TOP_N, HOT_SEARCH_SNAPSHOT_CLEANUP_DAYS, DEFAULT_WEIGHTS

# ========== SQLite 配置 ==========
SQLITE_DB_PATH = SQLITE_DB_PATH

def get_db_conn():
    """获取 SQLite 数据库连接"""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    """初始化数据库表"""
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        # 收评表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_market_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                review_date TEXT NOT NULL UNIQUE,
                summary TEXT,
                market_json TEXT,
                trend_json TEXT,
                pool_json TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
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
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(scan_date, symbol))''')
        # 热门股票表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS hot_stock_buttons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP )''')
        # 股票名称搜索映射表（名称 -> 代码，含简称，一般不删除）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_alias_map (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                symbol TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP )''')
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
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(predict_date, symbol))''')
        # 策略权重表（动态调整的评分权重）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS strategy_factor_weights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                factor_name TEXT NOT NULL UNIQUE,
                weight REAL DEFAULT 1.0,
                accuracy REAL DEFAULT 50.0,
                sample_count INTEGER DEFAULT 0,
                correct_count INTEGER DEFAULT 0,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP )''')
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
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
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
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
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
                sort_order INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(rec_date, symbol))''')
        # 股票映射表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_basic_info (
                symbol TEXT NOT NULL PRIMARY KEY,
                name TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        # 自定义扫描池表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_scan_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL UNIQUE,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        # 股票标签表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_concept_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL UNIQUE,
                tags_json TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP )''')
        try:
            cursor.execute('ALTER TABLE hot_search_ranking ADD COLUMN hot_rank INTEGER DEFAULT 0')
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
def load_daily_recommendations() -> List[Dict]:
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
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
            cursor.execute('DELETE FROM daily_recommendations WHERE rec_date = ?', (rec_date,))
            for sort_order, rec in enumerate(recs):
                bsp = rec.get('buySellPoints') or {}
                cursor.execute('''
                    INSERT INTO daily_recommendations (rec_date, symbol, name, price, change_val, change_percent, score, buy_price, current_price, sell_price, reason, sort_order)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存趋势股票失败: {str(error)}')
        return False
def load_stock_basic_info() -> Dict:
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
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
                'INSERT INTO stock_basic_info (symbol, name) VALUES (?, ?)',
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
                    weibi, avg_price_deviation, amplitude, market_change)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
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
def load_user_scan_pool() -> List[str]:
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
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
                'INSERT INTO user_scan_pool (symbol) VALUES (?)',
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
        cursor = conn.cursor()
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
                    'INSERT INTO hot_stock_buttons (code, name) VALUES (?, ?)',
                    (stock['code'].lower(), stock['name'])
                )
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存热门股票失败: {str(error)}')
        return False
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
            if 'top_funds_json' in row.keys() and row['top_funds_json']:
                review['topFunds'] = json.loads(row['top_funds_json'])
            else:
                review['topFunds'] = []
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
        # 确保新字段存在
        new_cols = ['industry_json', 'concept_json', 'limit_up_json', 'limit_down_json', 'top_gainers_json', 'top_losers_json', 'focus_json', 'top_funds_json']
        for col in new_cols:
            try:
                cursor.execute(f'ALTER TABLE daily_market_reviews ADD COLUMN {col} TEXT')
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
            top_funds_json = json.dumps(review.get('topFunds', []), ensure_ascii=False)
            trend_json = json.dumps(review.get('trendStocks', {}), ensure_ascii=False)
            pool_json = json.dumps(review.get('stockPool', {}), ensure_ascii=False)
            cursor.execute('''
                INSERT OR REPLACE INTO daily_market_reviews (review_date, summary, market_json, trend_json, pool_json, industry_json, concept_json, limit_up_json, limit_down_json, top_gainers_json, top_losers_json, focus_json, top_funds_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (review_date, summary, market_json, trend_json, pool_json, industry_json, concept_json, limit_up_json, limit_down_json, top_gainers_json, top_losers_json, focus_json, top_funds_json))
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
        return True
    except Exception as error:
        print(f'保存hot_search_ranking数据失败: {str(error)}')
        return False
