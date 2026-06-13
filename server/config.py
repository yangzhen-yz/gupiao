import os

# ========== 数据库配置 ==========
MYSQL_CONFIG = {
    'host': os.environ.get('MYSQL_HOST', 'localhost'),
    'port': int(os.environ.get('MYSQL_PORT', 3306)),
    'user': os.environ.get('MYSQL_USER', 'root'),
    'password': os.environ.get('MYSQL_PASSWORD', '123456'),
    'database': os.environ.get('MYSQL_DATABASE', 'gupiao'),
    'charset': 'utf8mb4'
}

# MySQL 连接池配置
MYSQL_POOL_MAX_CONNECTIONS = 10  # 连接池最大连接数
MYSQL_POOL_MIN_CACHED = 2        # 连接池最小缓存连接数

# ========== API 地址配置 ==========
TENCENT_QUOTE_API = 'https://qt.gtimg.cn/q='
TENCENT_KLINE_API = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
EASTMONEY_HOT_LIST_API = 'https://push2.eastmoney.com/api/qt/clist/get'

# ========== AI 诊断配置 ==========
AI_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ai_config.json')

# ========== HTTP 客户端配置 ==========
HTTP_TIMEOUT = 10.0  # HTTP 请求超时时间
HTTP_CONNECT_TIMEOUT = 5.0  # HTTP 连接超时时间
HTTP_MAX_CONNECTIONS = 50  # HTTP 最大连接数
HTTP_KEEPALIVE_CONNECTIONS = 25  # HTTP 保持连接数


# ========== 热搜榜配置 ==========
HOT_SEARCH_TOP_N = 500  # 热搜榜最大显示数量
HOT_SEARCH_SNAPSHOT_CLEANUP_DAYS = 60  # 热搜榜快照清理天数


# ========== 扫描配置 ==========
SCAN_CONCURRENCY = 25  # 扫描并发数
SCAN_KLINE_TIMEOUT = 12.0  # 扫描K线超时时间
SCAN_CACHE_TTL = 300  # 扫描缓存过期时间（秒）默认300秒


# ========== 趋势发现配置 ==========
TREND_MIN_SCORE = 85  # 趋势发现最小分数阈值
TREND_MIN_KLINE_DAYS = 120  # 趋势发现最小K线天数阈值
TREND_LIMIT_UP_THRESHOLD = 9.8  # 趋势发现价格超过120日均线阈值
TREND_LIMIT_UP_120_MIN = 1  # 趋势发现价格超过120日均线最小时间间隔
TREND_LIMIT_UP_250_MIN = 2  # 趋势发现价格超过250日均线最小时间间隔
TREND_MAX_DRAWDOWN = 30  # 趋势发现最大回撤阈值
TREND_CONSECUTIVE_UP_BONUS = 2  # 趋势发现连续上涨奖励分数
TREND_CONSECUTIVE_UP_MAX_BONUS = 10  # 趋势发现连续上涨最大奖励分数阈值
TREND_IS_UP_MIN_SCORE = 60  # 趋势发现是否上涨最小分数阈值

# 趋势判断：是否使用盘中实时价（True=盘中跌破立即排除，可能盘中刺破收盘收复被误移除；False=仅用收盘价判断，更稳健）
USE_INTRADAY_BREAK_CHECK = True

# MA120 斜率向上：判断"近 N 日 MA120 均值 > 前 N 日 MA120 均值"时使用的窗口长度（单位：交易日）
TREND_MA120_SLOPE_WINDOW = 5

# 连续上涨加分：仅统计最近 N 个交易日的连续上涨天数（避免早期数据干扰）
TREND_CONSECUTIVE_UP_MAX_DAYS = 30

TREND_SCORE_WEIGHTS = {
    'priceAboveMa120': 25,  # 价格超过120日均线权重
    'ma120SlopeUp': 25,  # 120日均线斜率权重
    'limitUp120': 20,  # 近120天涨停权重
    'limitUp250': 10,  # 近250天涨停权重
    'drawdown30': 10,  # 30日回撤权重
}


# ========== 策略综合评分配置 ==========
STRATEGY_BULL_THRESHOLD = 60  # 策略评分上涨阈值
STRATEGY_BEAR_THRESHOLD = 40  # 策略评分下跌阈值
STRATEGY_PREDICT_BULL_MIN_SCORE = 65  # 策略预测上涨最小分数阈值
STRATEGY_BACKTEST_PREDICTION_LIMIT = 500  # 策略回测预测限制分数
STRATEGY_BACKTEST_HISTORY_DAYS = 30  # 策略回测历史天数阈值


# 策略评分阈值配置 - 各指标的档位划分标准
# 用于判断指标处于高/中/低哪个档位，进而计算得分
STRATEGY_SCORE_THRESHOLDS = {
    'outer_ratio': {'high': 55, 'mid': 50},  # 外盘比例：>55%为高，50-55%为中，<50%为低
    'volume_ratio': {'high': 1.5, 'mid': 1.0, 'low': 0.5},  # 量比：>1.5为高，1.0-1.5为中，0.5-1.0为正常，<0.5为低
    'turnover_rate': {'high': [3, 10], 'mid': [1, 3], 'high_penalty': 10},  # 换手率：3-10%为高，1-3%为中，>10%时应用扣分
    'change_percent': {'high': 5, 'mid': 2, 'low': 0, 'negative': -2},  # 涨跌幅：>5%为高，2-5%为中，0-2%为低，<0为负
    'weibi': {'high': 30, 'mid': 0, 'low': -30},  # 委比：>30为高，0-30为中，-30-0为低，<-30为负
    'avg_price_deviation': {'high': 1, 'mid': 0, 'low': -1},  # 均价偏离：>1%为高，-1-1%为中，<-1%为低
    'amplitude': {'high': [2, 5], 'mid': [1, 2]},  # 振幅：2-5%为高，1-2%为中，<1%为低
}


# 策略评分分值配置 - 各指标在不同档位下的具体得分值
# 用于计算综合评分，分值越高表示该指标表现越好
STRATEGY_SCORE_VALUES = {
    'outer_ratio': {'high': 15, 'mid': 10, 'low': 5},  # 外盘比例得分
    'volume_ratio': {'high': 10, 'mid': 7, 'normal': 5, 'low': 2},  # 量比得分
    'turnover_rate': {'high': 10, 'mid': 7, 'normal': 5, 'low': 3},  # 换手率得分
    'change_percent': {'high': 20, 'mid': 15, 'normal': 10, 'low': 5, 'negative': 0},  # 涨跌幅得分
    'weibi': {'high': 10, 'mid': 7, 'low': 4, 'negative': 2},  # 委比得分
    'avg_price_deviation': {'high': 15, 'mid': 10, 'normal': 5, 'low': 2},  # 均价偏离得分
    'amplitude': {'high': 5, 'mid': 3, 'low': 2},  # 振幅得分
    'position': {'high': 10, 'mid': 5, 'low': 0},  # 仓位建议得分
}

# ========== 策略委比验证配置 ==========
# 用于验证委比指标是否真实反映买盘强度，防止虚假信号
STRATEGY_WEBI_VERIFY = {
    'weibi_high': 30,        # 委比高于此值视为高委比
    'outer_ratio_low': 48,   # 外盘比例低于此值视为异常，需结合委比判断
    'volume_ratio_low': 0.8, # 量比低于此值视为成交量不足，委比信号可能失真
}

# ========== 市盈率过滤配置 ==========
# pe_high: 市盈率高于此值视为高估，降低评分
# pe_max: 市盈率超过此值视为异常，直接过滤
STRATEGY_PE_FILTER = {
    'pe_high': 8,
    'pe_max': 700,
}

# 策略标签阈值配置 - 用于根据综合评分判定股票趋势强度
STRATEGY_LABEL_THRESHOLDS = {
    'strong_bull': 80,  # 强势看多：评分≥80，建议积极介入
    'bull': 70,         # 看多：评分≥70，建议关注
    'weak_bull': 60,    # 弱势看多：评分≥60，建议谨慎观察
}

# 默认策略评分权重配置 - 各指标权重相等，可根据策略需求动态调整
DEFAULT_WEIGHTS = {
    'outer_ratio': 1.0,        # 外盘比例权重
    'volume_ratio': 1.0,       # 量比权重
    'turnover_rate': 1.0,      # 换手率权重
    'change_percent': 1.0,     # 涨跌幅权重
    'weibi': 1.0,              # 委比权重
    'avg_price_deviation': 1.0, # 均价偏离权重
    'amplitude': 1.0,          # 振幅权重
    'position': 1.0,           # 仓位建议权重
}

# ========== 大盘预警配置 ==========
MARKET_CRASH_THRESHOLD = -2.0  # 市场预警阈值
MARKET_SEVERE_CRASH_THRESHOLD = -3.0  # 市场严重预警阈值

# ========== 退市风险过滤 ==========
DELISTING_KEYWORDS = ['ST', '*ST', 'SST', 'S*ST', 'NST', 'PT', '退', '退市', '退市整理']

# ========== 禁止股票关键词 ==========
# 退市/ST 等风险词由独立的 DELISTING_KEYWORDS + has_delisting_risk() 处理。
FORBIDDEN_KEYWORDS = [
    # 金融行业
    '银行', '证券', '保险',
    
    # 白酒消费
    '酒',
    
    # 家电行业
    '家电', '美的', '海尔', '格力',
    
    # 房地产行业
    '地产', '房产', '置业', '建设', '房地产', '城投', '万科', '保利', '绿地', '碧桂园', '恒大', '融创',
    
    # 铁路交通
    '铁路', '铁建', '中铁', '高铁',
    
    # 铁路公路
    '公路', '高速', '路桥', '交建',
    
    # 航运港口
    '航运', '港口', '水运', '轮船', '船舶',
    
    # 航空板块
    '航空', '机场', '民航', '国航', '东航', '南航', '海航',

    #物流
    '物流', '运输', '交运', '快递',
    
    # 乳业食品
    '乳业', '牛奶', '伊利', '蒙牛', '光明', '三元', '皇氏', '新乳业', '天润', '燕塘', '李子园',
    
    # 纺织板块
    '纺织', '服装', '服饰', '家纺', '面料', '化纤',
    
    # 农林牧渔
    '农业', '林业', '牧业', '渔业', '农林', '牧渔', '养殖', '饲料', '种业', '化肥',

    #传媒
    '游戏', '影视', '出版'
]

# ========== 备选股票池 ==========
HOT_STOCK_POOL = [
    'sh600900',
    'sz000063', 'sz000725',
    'sz002024', 'sz002460', 'sz002475', 'sz002594',
    'sz300144', 'sz300142', 'sz300750', 'sz300760',
    'sh600487', 'sh603985', 'sz002384', 'sz002281',
    'sh603629', 'sh600584', 'sz002156', 'sh603738', 'sz002428', 'sz002222', 'sh600330', 'sz002484',
    'sh603876', 'sz003031', 'sz001309', 'sz000988', 'sh600105', 'sz002015', 'sz000066',
    'sz000811', 'sh601991', 'sz002709', 'sh603256', 'sz002008', 'sz002636', 'sh601208', 'sz002080'
]

# ========== 定时任务配置 ==========
SCHEDULE_UPDATE_HOT_SEARCH_TIMES = ['10:00', '14:00']  # 更新热门搜索时间
SCHEDULE_MA10_CHECK_TIME = '15:05'  # 检查10日均线时间
SCHEDULE_DAILY_REVIEW_TIME = '15:10'  # 每日审核时间
SCHEDULE_BACKTEST_TIME = '15:20'  # 回测时间
SCHEDULE_RECOMMENDATION_TIMES = ['09:45', '13:30']  # 智能推荐生成时间（上午开盘后、下午开盘后）

# ========== 数据查询限制 ==========
QUERY_DAILY_RECOMMENDATIONS_LIMIT = 30  # 每日推荐股票数量限制
QUERY_TREND_RESULTS_LIMIT = 200  # 趋势结果数量限制
QUERY_MARKET_REVIEWS_LIMIT = 30  # 市场评论数量限制

# ========== 收评配置 ==========
REVIEW_TOP_GAINERS_COUNT = 15  # 顶部涨股票数量限制
REVIEW_TOP_LOSERS_COUNT = 15  # 顶部跌股票数量限制
REVIEW_TOP_SECTORS_COUNT = 5  # 顶部行业数量限制
REVIEW_TOMORROW_FOCUS_COUNT = 10  # 明日关注股票数量限制

# ========== 大盘指数代码 ==========
INDEX_SYMBOLS = {
    'sh': {'symbol': 'sh000001', 'name': '上证指数'},
    'sz': {'symbol': 'sz399001', 'name': '深证成指'},
    'cyb': {'symbol': 'sz399006', 'name': '创业板指'}
}

# ========== K线配置 ==========
KLINE_START_DATE = '2025-01-01'  # K线数据开始日期
KLINE_DATA_LIMIT = 200  # K线数据数量限制
KLINE_DISPLAY_MA_POINTS = 10  # 显示移动平均线点数
