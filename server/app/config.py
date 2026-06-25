import os

# ========== 数据库配置 ==========
SQLITE_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'gupiao.db')

# ========== API 地址配置 ==========
TENCENT_QUOTE_API = 'https://qt.gtimg.cn/q='
TENCENT_KLINE_API = 'https://ifzq.gtimg.cn/appstock/app/fqkline/get'
EASTMONEY_HOT_LIST_API = 'https://push2delay.eastmoney.com/api/qt/clist/get'

# ========== AI 诊断配置 ==========
AI_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'ai_config.json')

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


# ========== 趋势发现配置（中短线波段评分，满分 100 分） ==========
# 关注周期：未来 ~1 个月
# 评分结构：6 项基础分（100 分）+ 扣分项 + 强制排除
TREND_MIN_SCORE = 60  # 趋势发现进入趋势池的最低分（原85，V3 评分下经 268 样本实测 found=21 合理）
TREND_IS_UP_MIN_SCORE = 35  # 认定为"趋势向上"（isUp=true）的最低分（原60，调低）
TREND_MIN_KLINE_DAYS = 60  # 趋势发现最少 K 线天数（覆盖 MA20 + 60日量能均）
TREND_AUTO_ADD_POOL_THRESHOLD = 80  # 趋势评分 ≥ 此值时自动加入用户股票池（0 表示关闭）

# 涨停 / 跌停阈值
TREND_LIMIT_UP_THRESHOLD = 9.8  # 涨停阈值（单日涨幅 % ≥ 此值算涨停）
TREND_LIMIT_DOWN_THRESHOLD = -9.8  # 跌停阈值（单日涨幅 % ≤ 此值算跌停）

# 维度 1：短期均线趋势（满分 30 分；趋势方向 20 + 乖离惩罚 0~-10）
TREND_D1_TREND_FULL = 20  # 近10日全部在MA20上方 + MA20连续抬升 → 趋势满分
TREND_D1_TREND_PARTIAL = 15  # 近7日仅1日短暂跌破MA20（次日收回）+ MA20走平或向上
TREND_D1_TREND_BARE = 8   # 近3日反复穿插MA20但现价站稳
TREND_D1_DIVERGENCE_PENALTY_TIERS = [(15, -10), (10, -6), (5, -3)]  # [(乖离%≥, 扣分)], 当前价偏离MA20越远追高风险越大

# 维度 2：短期资金异动（30 分，多档制；涨停次数与连阳数分别计算，取高分）
TREND_D2_LIMIT_UP_TIERS = [(3, 30), (2, 20), (1, 10)]  # [(≥涨停次数, 得分)]
TREND_D2_YANG_LINE_TIERS = [(6, 30), (4, 20), (3, 10)]  # [(≥连阳数, 得分)] — 2连板直接30分

# 维度 3：量能配合（满分 20 分 = 量比 10 + 量能趋势 5 + 量能稳定性 5）
TREND_D3_RATIO_TIERS = [(1.5, 10), (1.3, 8), (1.0, 5)]  # [(20日均量/60日均量, 得分)] — 量比子维度
TREND_D3_SLOPE_TIERS = [(0.3, 5), (0.15, 3), (0, 2)]   # [(归一化斜率≥, 得分)] — 量能趋势子维度
TREND_D3_STABILITY_TIERS = [(0.3, 5), (0.5, 3), (0.8, 2)]  # [(CV≤, 得分)] — 量能稳定性子维度（CV越小越健康）

# 维度 4：风险回撤（20 分，5 档）
TREND_D4_DRAWDOWN_TIERS = [(8, 20), (12, 15), (15, 10), (20, 5)]  # [(<回撤%, 得分)]

# 扣分项（12 项，分 3 组做互斥/衰减）
TREND_DEDUCT_POSITION_HIGH = 10  # 现价位于250日分位 ≥ 80%（新高突破可豁免）
TREND_DEDUCT_MA60_MA120_BEAR = 8  # MA60 + MA120 同步向下（原15，减半）
TREND_DEDUCT_POSITION_MID = 5  # 60% ≤ 分位 < 80% 或 MA60向下/MA120向上（原10，减半）
TREND_DEDUCT_MA20_BROKEN = 6  # 近 5 日内跌破 MA20 且 3 日未收回（原12，减半）
TREND_DEDUCT_LIMIT_DOWN = 15  # 近 20 日出现单日跌停
TREND_DEDUCT_SHRINK_VOL = 10  # 持续缩量阴跌
TREND_DEDUCT_VOLUME_SPIKE = 10  # 单日成交量创20日新高，股价无力上涨（-1%~1%）→ 放量滞涨
TREND_DEDUCT_VOLUME_DIVERGENCE = 6  # 近5日股价小幅抬升但20日均量逐级放大超30%（原12，减半）
TREND_DEDUCT_EXTREME_VOLATILITY = 8  # 近20日振幅≥15%且收跌
TREND_DEDUCT_SINGLE_DAY_CRASH = 8  # 单日大跌超-7%且次日未修复
TREND_DEDUCT_AMPLITUDE_FLAT = 10  # 近20日振幅<5%（完全横盘，高位扣满/低位豁免）
TREND_DEDUCT_NON_MAIN_BOARD = 5  # 非主板（创业板/科创板）
# 扣分项分组（同组取最大值，防止同类风险重复惩罚）
TREND_DEDUCT_GROUPS = {
    '均线趋势': ['MA20_BROKEN', 'MA60_MA120_BEAR', 'POSITION_MID'],
    '量价异常': ['VOLUME_SPIKE', 'VOLUME_DIVERGENCE', 'SHRINK_VOL'],
    '极端事件': ['LIMIT_DOWN', 'EXTREME_VOLATILITY', 'SINGLE_DAY_CRASH'],
}

# 分位豁免：近20日涨幅超过此阈值视为"新高突破"，高位扣分豁免
TREND_POSITION_BREAKTHROUGH_GAIN = 20  #

# 横盘位置判断
TREND_SIDEWAYS_LOW_PCT = 30   # 分位 < 此值视为低位横盘（蓄力，不扣）
TREND_SIDEWAYS_MID_PCT = 60   # 分位 < 此值视为中位横盘（减半扣）
TREND_SIDEWAYS_BONUS = 3      # 低位横盘蓄力加分

# 加分项（额外奖励，用于拉开分差 + 平衡低位分位）
TREND_BONUS_BULL_ALIGNMENT = 12    # MA20>MA60>MA120 标准多头排列 + 现价分位<60%（原5，提升）
TREND_BONUS_VOLUME_BREAKOUT = 10   # 近3日出现倍量突破（日成交量≥20日均量×2 + 涨幅≥5%）（原5，提升）
TREND_BONUS_CONSECUTIVE_YANG = 5   # 近5日连续阳线 + 每日涨幅≥1%（原3，提升）
TREND_BONUS_POCKET_PIVOT = 8       # 口袋支点：倍量突破 + 当日收于当日高点附近 + 收盘价>前日高点

# 板块动量因子（环境修正：±20% 权重，叠加到总分）
TREND_SECTOR_MOMENTUM_WEIGHT = 0.20  # 板块动量占整体评分的权重
TREND_SECTOR_HOT_RANK_MIN = 5       # 板块排名前N视为热门板块

# 强制排除阈值
TREND_EXCLUDE_CONSECUTIVE_LIMIT_DOWN = 3  # 近20日出现连续N个跌停 → 直接剔除
TREND_EXCLUDE_MARKET_CAP_MIN = 20  # 总市值 < N亿 直接剔除（可配置）
TREND_RANGE_20D_FLAT_MIN = 5  # 近 20 日振幅 < 5% 视为"完全横盘"（转入扣分项，不再直接排除）

# 公告/利空数据源（东方财富公告接口）
EASTMONEY_ANN_API = 'https://np-anotice-stock.eastmoney.com/api/security/ann'
TREND_ANNOUNCEMENT_LOOKBACK_DAYS = 20  # 公告回看天数
TREND_ANNOUNCEMENT_CACHE_TTL = 3600  # 公告缓存秒数（1小时）

# 趋势判断：是否使用盘中实时价（True=盘中跌破立即排除，可能盘中刺破收盘收复被误移除；False=仅用收盘价判断，更稳健）
USE_INTRADAY_BREAK_CHECK = True


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
    '银行', '证券', '保险', '信托', '金控',
    # 保险公司（按名称直接列，避免中国人寿/平安/太保等被漏过）
    '中国人寿', '平安', '太保', '太平洋', '新华保险', '人保', '财险', '寿险',
    '中国人保', '中国平安', '中国太保', '中国太平', '中国人寿', '国寿',
    '众安', '华泰', '天安', '前海', '华夏人寿', '泰康', '太平', '民生人寿',
    
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
