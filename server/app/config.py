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


# ========== 热搜榜配置（3类合并）==========
# 数据来源：
#   1. 人气榜：东方财富真实个股人气榜 API（emappdata.eastmoney.com/stockrank/getAllCurrentList）
#      — API 最多返回前 100 名，取全部沪深主板
#   2. 涨幅榜：clist API fid=f3（涨跌幅排序），取前 N 只沪深主板
#   3. 成交额榜：clist API fid=f6（成交额排序），取前 N 只沪深主板
# 合并去重后，人气榜股票排前面（hotRank 从 1 开始），涨幅榜独有的排中间，成交额榜独有的排后面
POPULARITY_RANK_TOP_N = 100       # 人气榜取前 N 只（API 上限 100）
CHANGE_RANK_TOP_N = 100           # 涨幅榜取前 N 只沪深主板
TURNOVER_RANK_TOP_N = 400         # 成交额榜取前 N 只沪深主板
HOT_SEARCH_TOP_N = 600            # 热搜榜最大显示数量（3榜合并后上限）
HOT_SEARCH_SNAPSHOT_CLEANUP_DAYS = 60  # 热搜榜快照清理天数


# ========== 资金流向 Tab 配置（2026-07-16 新增） ==========
FUND_FLOW_STOCK_TOP_N = 50          # 个股主力净流入/净流出排名各取前 N 只
FUND_FLOW_SECTOR_TOP_N = 30         # 板块（行业/概念）净流入/净流出排名各取前 N 个（≥30 以支撑前端每页 10 条翻页）
FUND_FLOW_HIDDEN_CHANGE_THRESHOLD = 3.0   # “非热点但上涨不错”的最低涨幅阈值（%）
FUND_FLOW_HIDDEN_HOT_TOP_N = 30     # 排除热搜榜前 N 名（视为“近期热点”）
FUND_FLOW_HIDDEN_TOP_N = 50         # “非热点上涨股”最多返回 N 只
FUND_FLOW_CACHE_TTL = 20            # 资金流向汇总接口内存缓存有效期（秒）


# ========== 扫描配置 ==========
SCAN_CONCURRENCY = 25  # 扫描并发数
SCAN_KLINE_TIMEOUT = 12.0  # 扫描K线超时时间
SCAN_CACHE_TTL = 300  # 扫描缓存过期时间（秒）默认300秒

# ========== 股票筛选 Tab 过滤配置（2026-07-08）==========
# 第二个 Tab "股票筛选"（/api/hot-stocks-scan）的可选过滤模式
# 用户在前端可切换：all(全部) / bull_only(仅看涨) / bull_and_hold(观望以上) / min_score(自定义分)
# 注意：当前后端默认仍返回全量，前端按此配置做客户端过滤；如需服务端过滤可将 FILTER_MODE 设为 'all' 之外的任意值并改 hot-stocks-scan 路由
# 2026-08-31 选股策略优化：新增 leaders_only（仅优质龙头）过滤并设为默认。
# 背景：扫描宇宙扩大后弱势票（跟风/杂毛）占比高（实测 93%），用户只需高预期优质票。
# leaders_only 规则：去掉 跟风/杂毛 角色，且预期涨幅 ≥ TAB_SCAN_LEADERS_MIN_GAIN。
# 2026-09-10 调整为默认"自定义最低分 60"，让用户一进页面就看到相对高分的股票（观望/看涨），避免弱票刷屏。
TAB_SCAN_FILTER_MODES = ['leaders_only', 'all', 'bull_only', 'bull_and_hold', 'min_score']  # 前端下拉选项
TAB_SCAN_LEADERS_MIN_GAIN = 3.0  # 仅优质龙头模式的最低预期涨幅(%)
TAB_SCAN_DEFAULT_MODE = 'min_score'  # 默认模式：自定义最低分
TAB_SCAN_DEFAULT_MIN_SCORE = 60  # 自定义最低分模式默认值
TAB_SCAN_BULL_MIN_SCORE = 70  # "看涨"区间的最低分（≥ 此值为看涨）
TAB_SCAN_BEAR_MAX_SCORE = 40  # "看跌"区间的最高分（< 此值为看跌）
# 区间划分：score < BEAR_MAX(40) 为看跌，BEAR_MAX(40) ≤ score < BULL_MIN(70) 为观望，score ≥ BULL_MIN(70) 为看涨
# "观望以上" 指 score ≥ BULL_MIN_SCORE，仅看涨也是 score ≥ BULL_MIN_SCORE


# ========== 趋势发现配置（中短线波段评分，满分 100 分） ==========
# 关注周期：未来 ~1 个月
# 评分结构：4 维度基础分（100 分）+ 扣分项 + 加分项(缩放0~5) + 强制排除
TREND_MIN_SCORE = 65  # 趋势发现进入趋势池的最低分
TREND_IS_UP_MIN_SCORE = 35  # 认定为"趋势向上"（isUp=true）的最低分
TREND_MIN_KLINE_DAYS = 60  # 趋势发现最少 K 线天数（覆盖 MA20 + 60日量能均）
TREND_AUTO_ADD_POOL_THRESHOLD = 80  # 趋势评分 ≥ 此值时自动加入用户股票池（0 表示关闭）
TREND_POOL_MAX_SIZE = 50  # 股票池上限：自动入池超过此数时，裁掉最早加入的（防无限膨胀）

# 涨停 / 跌停阈值
TREND_LIMIT_UP_THRESHOLD = 9.8  # 涨停阈值（单日涨幅 % ≥ 此值算涨停）
TREND_LIMIT_DOWN_THRESHOLD = -9.8  # 跌停阈值（单日涨幅 % ≤ 此值算跌停）

# 维度 1：短期均线趋势（满分 30 分；趋势方向 20 + 乖离惩罚 0~-10）
TREND_D1_TREND_FULL = 20  # 近10日全部在MA20上方 + MA20连续抬升 → 趋势满分
TREND_D1_TREND_PARTIAL = 15  # 近7日仅1日短暂跌破MA20（次日收回）+ MA20走平或向上
TREND_D1_TREND_BARE = 8   # 近3日反复穿插MA20但现价站稳
TREND_D1_DIVERGENCE_PENALTY_TIERS = [(15, -10), (10, -6), (5, -3)]  # [(乖离%≥, 扣分)], 当前价偏离MA20越远追高风险越大

# 维度 2：短期资金异动（30 分，多档制；连板高度分层 + 连阳数取高分）
# 2026-06-30 升级：业界龙头战法核心是"龙一溢价 > 龙二 > 龙三"，
# 用连板高度（最大连续涨停数）替代单纯的涨停次数，识别题材内的龙头位置
TREND_D2_LIMIT_UP_TIERS = [(3, 30), (2, 20), (1, 10)]  # [(≥涨停次数, 得分)] — 旧逻辑保留作 fallback
TREND_D2_YANG_LINE_TIERS = [(6, 30), (4, 20), (3, 10)]  # [(≥连阳数, 得分)]
# 连板高度分层（最大连板数 → 得分）：5连板+=市场总龙头30；3-4连板=板块龙头25；2连板=替补龙20；首板=启动12
TREND_D2_CONSECUTIVE_BOARD_TIERS = [(5, 30), (3, 25), (2, 20), (1, 12)]  # [(≥最大连板数, 得分)]

# 维度 3：量能配合（满分 20 分 = 量比 10 + 量能趋势 5 + 量能稳定性 5）
TREND_D3_RATIO_TIERS = [(1.5, 10), (1.3, 8), (1.0, 5)]  # [(20日均量/60日均量, 得分)] — 量比子维度
TREND_D3_SLOPE_TIERS = [(0.3, 5), (0.15, 3), (0, 2)]   # [(归一化斜率≥, 得分)] — 量能趋势子维度
TREND_D3_STABILITY_TIERS = [(0.3, 5), (0.5, 3), (0.8, 2)]  # [(CV≤, 得分)] — 量能稳定性子维度（CV越小越健康）

# 维度 4：风险回撤（20 分，5 档）
TREND_D4_DRAWDOWN_TIERS = [(8, 20), (12, 15), (15, 10), (20, 5)]  # [(<回撤%, 得分)]

# 扣分项（12 项，分 3 组做互斥/衰减）
TREND_DEDUCT_POSITION_HIGH = 10  # 现价位于250日分位 ≥ 80%（新高突破可豁免）
TREND_DEDUCT_MA60_MA120_BEAR = 8  # MA60 + MA120 同步向下
TREND_DEDUCT_POSITION_MID = 5  # 60% ≤ 分位 < 80% 或 MA60向下/MA120向上
TREND_DEDUCT_MA20_BROKEN = 6  # 近 5 日内跌破 MA20 且 3 日未收回
TREND_DEDUCT_LIMIT_DOWN = 15  # 近 20 日出现单日跌停
TREND_DEDUCT_SHRINK_VOL = 10  # 持续缩量阴跌
TREND_DEDUCT_VOLUME_SPIKE = 10  # 单日成交量创20日新高，股价无力上涨（-1%~1%）→ 放量滞涨
TREND_DEDUCT_VOLUME_DIVERGENCE = 6  # 近5日股价小幅抬升但20日均量逐级放大超30%
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
# 注意：基础分满分100 + 加分项最大 20 = 120，融合时再按比例归一化到 100，
# 避免"基础分顶格+全触发加分"导致 6 只票都拿 100 分无法区分
# 2026-07-02 优化：参考市面"稳步震荡上行 vs 加速拉升"区分
#   - 多头排列+低位 5→6（市面"中长期均线多头"权重 40%，应加强）
#   - 连续阳线 3→4（稳步上行市面更推崇）
#   - 新增稳步上行+3 / 加速拉升+1（市面"稳步上行趋势延续周期更长，加速拉升易深度调整"）
TREND_BONUS_BULL_ALIGNMENT = 6    # MA20>MA60>MA120 标准多头排列 + 现价分位<60%
TREND_BONUS_VOLUME_BREAKOUT = 4   # 近3日出现倍量突破（日成交量≥20日均量×2 + 涨幅≥5%）
TREND_BONUS_CONSECUTIVE_YANG = 4  # 近5日连续阳线 + 每日涨幅≥1%
TREND_BONUS_POCKET_PIVOT = 3      # 口袋支点：倍量突破 + 当日收于当日高点附近 + 收盘价>前日高点
TREND_BONUS_STEADY_UPTREND = 3    # 稳步上行：近20日涨幅5%-15% + 阳线占比≥60% + 最大单日涨幅<5%
TREND_BONUS_ACCELERATING = 1      # 加速拉升：近5日涨幅≥15% + 量比≥2（警示透支风险，低加分）

# 板块动量因子（环境修正：±5% 权重，叠加到总分）
# 2026-06-30 实现：基于个股概念标签(stock_concept_tags)的同题材平均涨幅做环境修正
# 数据源：扫描时统计每个概念标签下所有股票的平均涨幅，个股取其标签的平均涨幅作为 sector_change
TREND_SECTOR_MOMENTUM_WEIGHT = 0.20  # 板块动量占整体评分的权重（保留，用于未来扩展）
TREND_SECTOR_HOT_RANK_MIN = 5       # 板块排名前N视为热门板块
# 板块涨幅 → 修正系数映射（[(涨幅%≥, 修正系数)]，从高到低匹配，命中即停）
# 热门题材 +5%，偏热 +3%，中性 0%，偏冷 -3%，冷门 -5%
TREND_SECTOR_FACTOR_TIERS = [
    (3.0, 0.05),    # 题材平均涨幅 ≥ 3% → 热门 +5%
    (1.0, 0.03),    # ≥ 1% → 偏热 +3%
    (-1.0, 0.0),    # -1%~1% → 中性 0%
    (-3.0, -0.03),  # ≤ -1% → 偏冷 -3%
]
TREND_SECTOR_FACTOR_COLD = -0.05  # 题材平均涨幅 ≤ -3% → 冷门 -5%

# ==========  量价6形态分类（2026-06-30）==========
# 6种量价形态互斥，优先级：突破类 > 滞涨/背离类 > 回调类 > 横盘类
# 作为修正系数叠加到总分（与板块动量同属乘性修正，各自独立计算）
TREND_PATTERN_FACTOR = {
    'volume_breakout':   0.04,   # 放量突破：+4%（最强进攻信号）
    'volume_reversal':   0.03,   # 倍量反包：+3%（止跌反转信号）
    'shrink_pullback':   0.02,   # 缩量回调：+2%（健康回调，洗盘特征）
    'volume_stagnation': -0.04,  # 放量滞涨：-4%（顶部滞涨风险）
    'volume_divergence': -0.03,  # 量价背离：-3%（顶背离风险）
    'shrink_sideways':   -0.01,  # 缩量横盘：-1%（动能不足，但不强烈看空）
    'none':              0.0,    # 无显著形态
}
# 形态识别阈值（与 trend.py 现有 _check_* 函数语义对齐）
TREND_PATTERN_VOLUME_BREAKOUT_RATIO = 2.0    # 倍量阈值：日成交量 ≥ 20日均量 × 2
TREND_PATTERN_VOLUME_BREAKOUT_CHANGE = 5.0   # 放量突破涨幅 ≥ 5%
TREND_PATTERN_REVERSAL_CHANGE = 3.0          # 倍量反包涨幅 ≥ 3%（阳包阴）
TREND_PATTERN_SHRINK_RATIO = 0.5             # 缩量阈值：日成交量 < 60日均量 × 0.5
TREND_PATTERN_SIDEWAYS_AMPLITUDE = 5.0       # 横盘振幅 < 5%
TREND_PATTERN_DIVERGENCE_VOLUME_EXPAND = 1.3 # 量价背离：20日均量放大超 30%

# ========== 筹码结构修正（代理指标，2026-06-30）==========
# 用 MA60/MA120 成本压力位 + 换手率筹码松动度 + 主力净流入 + 量能稳定性CV 组合
# 作为修正系数叠加到总分（与板块动量同属乘性修正，各自独立计算）
TREND_CHIP_FACTOR_MAX = 0.08   # 筹码修正系数上限 +8%
TREND_CHIP_FACTOR_MIN = -0.08  # 筹码修正系数下限 -8%
# 子项1：成本压力位（现价 vs MA60/MA120）
TREND_CHIP_COST_ABOVE_BOTH = 0.03   # 现价 > MA60 且 > MA120（双线上方，无成本压力）
TREND_CHIP_COST_BELOW_BOTH = -0.05  # 现价 < MA60 且 < MA120（双线下方，双重套牢盘压力）
TREND_CHIP_COST_BETWEEN = 0.0       # 现价在 MA60/MA120 之间
# 子项2：换手率筹码松动度
TREND_CHIP_TURNOVER_LOCKED = 0.02   # 换手率 1-3%（筹码锁定好，主力控盘）
TREND_CHIP_TURNOVER_LOOSE = -0.02   # 换手率 >15%（筹码松动，换手剧烈）
TREND_CHIP_TURNOVER_NORMAL = 0.0    # 其他区间
# 子项3：主力筹码动向（基于 main_capital_inflow 占比%）
TREND_CHIP_MAIN_ACCUMULATE = 0.03   # 主力净流入占比 ≥3%（主力吸筹）
TREND_CHIP_MAIN_DISTRIBUTE = -0.03  # 主力净流入占比 ≤-3%（主力派发）
TREND_CHIP_MAIN_NEUTRAL = 0.0       # -3% ~ 3% 中性
# 子项4：量能稳定性 CV（复用 D3 已计算的 cv）
TREND_CHIP_CV_STABLE = 0.02         # CV ≤ 0.3（量能稳定，筹码沉淀）
TREND_CHIP_CV_VOLATILE = -0.02      # CV > 0.8（量能紊乱，筹码动荡）
TREND_CHIP_CV_NORMAL = 0.0

# ========== 动态阈值（市场状态 + 形态感知，2026-06-30）==========
# 市场状态判定：大盘指数(sh000001) 现价 vs MA20 + 当日涨幅
TREND_DYNAMIC_MARKET_BULL_CHANGE = 0.5    # 大盘 MA20 之上 + 当日涨幅≥0.5% → bull
TREND_DYNAMIC_MARKET_BEAR_CHANGE = -0.5   # 大盘 MA20 之下 + 当日涨幅≤-0.5% → bear
                                          # 其他情况 → range
# 修正系数总和上限（三系数叠加封顶，防止溢出）
TREND_TOTAL_FACTOR_CAP = 0.15
# 动态阈值调整表：[(market_regime, pattern), {阈值覆盖项}]
# 阈值覆盖项为 dict，key 与 TREND_* 常量同名，value 为动态值
TREND_DYNAMIC_THRESHOLDS = {
    # 牛市：放宽乖离惩罚（追高容忍），收紧破位扣分（破位更严重）
    ('bull', 'volume_breakout'): {'D1_DIVERGENCE_PENALTY_TIERS': [(20, -10), (15, -6), (10, -3)]},  # 突破时乖离阈值+5
    ('bull', 'none'):            {'D1_DIVERGENCE_PENALTY_TIERS': [(18, -10), (12, -6), (6, -3)]},   # 牛市普通票乖离阈值+3
    ('bull', 'volume_stagnation'): {'DEDUCT_VOLUME_SPIKE': 12},  # 牛市放量滞涨更危险，扣分加重
    # 熊市：收紧乖离惩罚（追高风险大），加重破位扣分，放宽回撤档位
    ('bear', 'none'):            {'D1_DIVERGENCE_PENALTY_TIERS': [(12, -10), (8, -6), (4, -3)],   # 熊市乖离阈值-3
                                   'DEDUCT_MA20_BROKEN': 8,        # 熊市破位扣分加重 6→8
                                   'D4_DRAWDOWN_TIERS': [(6, 20), (10, 15), (13, 10), (18, 5)]},  # 熊市回撤容忍收紧
    ('bear', 'shrink_pullback'): {'D4_DRAWDOWN_TIERS': [(8, 20), (12, 15), (15, 10), (20, 5)]},  # 熊市缩量回调=趋势恶化，回撤档位收紧
    # 形态特化（不论牛熊）
    ('range', 'volume_divergence'): {'DEDUCT_VOLUME_DIVERGENCE': 10},  # 量价背离扣分加重 6→10
    ('range', 'shrink_sideways'):   {'DEDUCT_AMPLITUDE_FLAT': 12},     # 缩量横盘扣分加重 10→12
}
# 默认阈值（未命中动态表时使用，等同原静态值）
TREND_DYNAMIC_DEFAULTS = {
    'D1_DIVERGENCE_PENALTY_TIERS': [(15, -10), (10, -6), (5, -3)],
    'DEDUCT_VOLUME_SPIKE': 10,
    'DEDUCT_MA20_BROKEN': 6,
    'D4_DRAWDOWN_TIERS': [(8, 20), (12, 15), (15, 10), (20, 5)],
    'DEDUCT_VOLUME_DIVERGENCE': 6,
    'DEDUCT_AMPLITUDE_FLAT': 10,
}

# ========== 行业中性化（2026-06-30）==========
# 对 strategy_score(calc_stock_score_v2 输出) 按行业分位数做后处理调整，
# 避免热门行业股票因整体高 volume_ratio/turnover/change 而集体虚高。
# 作为共享 helper apply_industry_neutralization 在 routes/recommend/trend 三层调用。
STRATEGY_INDUSTRY_NEUTRALIZATION_ENABLED = True
STRATEGY_INDUSTRY_NEUTRAL_MIN_SAMPLE = 5   # 单行业样本 < N 不做中性化（统计无意义）
STRATEGY_INDUSTRY_P80_BONUS = 2            # 行业内总分 ≥ P80 → +2
STRATEGY_INDUSTRY_P20_PENALTY = -2         # 行业内总分 ≤ P20 → -2

# 强制排除阈值
TREND_EXCLUDE_CONSECUTIVE_LIMIT_DOWN = 3  # 近20日出现连续N个跌停 → 直接剔除
TREND_EXCLUDE_MARKET_CAP_MIN = 20  # 总市值 < N亿 直接剔除（可配置）
TREND_RANGE_20D_FLAT_MIN = 5  # 近 20 日振幅 < 5% 视为"完全横盘"（转入扣分项，不再直接排除）

# ========== 风险硬过滤（2026-08-03 新增，参考 CANSLIM/SEPA 风控）==========
# RSI 超买过滤：RSI≥95 极度超买（强制排除），RSI≥88 严重超买（扣分）
TREND_RSI_PERIOD = 14                 # RSI 计算周期（标准 14 日）
TREND_RSI_EXTREME_OVERBOUGHT = 95     # RSI ≥ 此值 → 极度超买，强制排除
TREND_RSI_OVERBOUGHT = 88             # RSI ≥ 此值 → 严重超买，扣分
TREND_RSI_OVERBOUGHT_DEDUCT = 8       # 严重超买扣分值
# 连板透支过滤：5+ 连板后追高风险极高（参考龙头战法"5板以上不追"纪律）
TREND_CONSECUTIVE_BOARD_EXHAUSTION = 5  # 连板数 ≥ 此值 → 透支风险扣分
TREND_CONSECUTIVE_BOARD_EXHAUSTION_DEDUCT = 10  # 连板透支扣分值

# ========== 早期信号层（第5维度，2026-08-17 新增）==========
# 在 D1-D4 基础评分上叠加"早期信号加成层"，识别趋势形成前的转折信号，降低均线系统滞后性。
# 仅对边界股（base_score ∈ [40, 92]）运行，RSI≥88 跳过，命中数×3 封顶 +10。
TREND_EARLY_SIGNAL_ENABLED = True
TREND_EARLY_SIGNAL_PER_HIT_SCORE = 3
TREND_EARLY_SIGNAL_MAX_BONUS = 10
TREND_EARLY_SIGNAL_POSITION_GATE = 70       # 分位 > 此值时大部分信号失效（高位无早期信号价值）
TREND_EARLY_SIGNAL_BASE_SCORE_MIN = 40      # base_score 下界（低于此分说明趋势太弱，早期信号无意义）
TREND_EARLY_SIGNAL_BASE_SCORE_MAX = 92      # base_score 上界（高于此分已确认强势，无需早期信号加成）

# 检测器1: NR7 / Inside Bar 缩量变盘
TREND_EARLY_SIGNAL_NR7_PERIOD = 7           # NR7 比较周期（当前 N 日区间 vs 近 30 日最小）
TREND_EARLY_SIGNAL_NR7_RANGE_RATIO = 1.0    # 当前区间 ≤ 历史最小 × 此比例 → 命中 NR7

# 检测器2: VCP 波动率收缩（Minervini SEPA 体系）
TREND_EARLY_SIGNAL_VCP_MIN_CONTRACTIONS = 2  # 最少收缩次数（振幅严格递减）
TREND_EARLY_SIGNAL_VCP_VOL_DECAY = 0.3       # 量能衰减容忍度（最后一次 ≤ 第一次 × (1+此值)）

# 检测器3: RSI 底背离
TREND_EARLY_SIGNAL_RSI_BOTTOM_LOOKBACK = 60  # 回看周期（找两个价格低点）
TREND_EARLY_SIGNAL_RSI_BOTTOM_PERIOD = 14    # RSI 计算周期

# 检测器4: 底部首板
TREND_EARLY_SIGNAL_FIRST_LIMIT_UP_POSITION_MAX = 50  # 分位 ≤ 此值才算"底部"
TREND_EARLY_SIGNAL_FIRST_LIMIT_UP_LOOKBACK = 60      # 回看周期（此期间无其他涨停才算"首板"）

# 检测器5: 缩量整理后首放
TREND_EARLY_SIGNAL_CONSOLIDATION_BREAKOUT_VOL_RATIO = 1.8  # 当日量 ≥ 20日均量 × 此比例 → 倍量确认

# 公告/利空数据源（东方财富公告接口）
EASTMONEY_ANN_API = 'https://np-anotice-stock.eastmoney.com/api/security/ann'
TREND_ANNOUNCEMENT_LOOKBACK_DAYS = 20  # 公告回看天数
TREND_ANNOUNCEMENT_CACHE_TTL = 3600  # 公告缓存秒数（1小时）

# 趋势判断：是否使用盘中实时价（True=盘中跌破立即排除，可能盘中刺破收盘收复被误移除；False=仅用收盘价判断，更稳健）
USE_INTRADAY_BREAK_CHECK = True


# ========== RPS 相对强度因子（P0，2026-07-16 新增）==========
# 欧奈尔体系核心：RPS = 个股区间涨幅在全市场的百分位排名（0~100）。
# RPS 90 表示强于 90% 的股票的同期表现，是"强势股/龙头"的量化标志。
# 数据来源：东方财富 clist + k1/k2/k3=1（区间涨跌幅），一次请求拿全市场，本地算百分位。
RPS_ENABLED = True
RPS_PERIOD_DAYS = 120            # 主评分周期（欧奈尔常用 120 日），calc_stock_score_v2 默认用此周期
RPS_PERIODS = [50, 120, 250]     # 多周期（前端展示/未来组合用），分别对应月/季/年相对强度
RPS_CACHE_TTL = 3600             # RPS 全市场映射缓存（秒），盘前算一次即可，盘中复用
RPS_MIN_SAMPLES = 300            # 全市场样本不足时跳过计算（避免误算 RPS）
RPS_TOP_N = 100                  # /api/rps 强势股榜默认返回数量

# ========== 基本面质量因子（P1a，2026-07-16 新增）==========
# 数据源：东方财富 datacenter RPT_LICO_FN_CPD（核心财务指标，季度披露）
# 取最新一期(ISNEW=1)：WEIGHTAVG_ROE(ROE%)+SJLTZ(归母净利润同比%)，每日18:05盘后更新
FUNDAMENTAL_ENABLED = True
FUNDAMENTAL_UPDATE_TIME = '18:05'  # 与股东人数(18:00)错开，避免并发拉取

# ========== 聪明钱因子（P1b，2026-07-16 新增）==========
# 龙虎榜净买入额 + 北向资金净流入，作为"聪明钱"个股/市场级信号
SMART_MONEY_ENABLED = True
SMART_MONEY_DRAGON_TIGER_DAYS = 5       # 龙虎榜聚合近 N 个交易日
SMART_MONEY_NET_AMT_HIGH = 500_000_000   # 龙虎榜净买入 ≥5亿 → high（机构/游资重仓介入）
SMART_MONEY_NET_AMT_MID = 100_000_000    # 龙虎榜净买入 ≥1亿 → mid（游资介入）
SMART_MONEY_UPDATE_TIME = '17:30'        # 交易日后定时拉取龙虎榜+北向最新数据（盘后约17:30发布）

# ========== 估值分位过滤（P2c，2026-07-16 新增）==========
# 数据源：东方财富 clist（复用 fundflow._clist），字段 f9=PE(TTM)、f23=PB。
# 全市场拉取后本地算 PE/PB 百分位（0~100，分位越高越贵），用于过滤极端高估股。
VALUATION_ENABLED = True
VALUATION_OVERVALUED_PE_PCT = 90   # PE 百分位 ≥ 此值视为偏高
VALUATION_OVERVALUED_PB_PCT = 90   # PB 百分位 ≥ 此值视为偏高
VALUATION_UPDATE_TIME = '18:10'    # 与基本面(18:05)错开，避免并发拉取

# ========== 风控止损（P2a，2026-07-16 新增）==========
# 止损幅度基于当日振幅(波动率代理)动态缩放，clamp 在 [STOP_LOSS_MIN, STOP_LOSS_MAX] 之间。
RISK_CONTROL_STOP_LOSS_MIN = 0.04   # 最小止损幅度 4%（低波动股不会过于贴近）
RISK_CONTROL_STOP_LOSS_MAX = 0.12   # 最大止损幅度 12%（高波动股防止过早被洗）
RISK_CONTROL_STOP_LOSS_VOL_MULT = 1.5  # 止损幅度 = 当日振幅 × 此系数（波动率缩放）
# 风险等级阈值（基于综合评分 + 估值/聪明钱/筹码信号综合判定）
RISK_CONTROL_SCORE_HIGH_RISK = 55   # 评分 < 此值 → 高风险
RISK_CONTROL_SCORE_MID_RISK = 65    # 评分 < 此值 → 中风险，否则低风险

# ========== 买卖点位置+角色感知修正（V4.3，2026-08-17）==========
# 在 V4.1 振幅动态调整基础上，叠加位置高低/角色分类/评分置信度三维修正。
# 止盈倍率 = 位置倍率 × 角色倍率 × 评分倍率，封顶 BUY_SELL_MAX_TARGET_PCT。
# 位置信息优先取 trend_details.positionPct（精确年度分位），fallback 用 base_scores['position']（5/10/15 粗估）。

# 位置分位门限（年度分位 0-100）
BUY_SELL_POSITION_LOW_GATE = 30         # ≤ 此值 = 低位（预期空间大）
BUY_SELL_POSITION_MIDLOW_GATE = 50      # ≤ 此值 = 中低位
BUY_SELL_POSITION_HIGH_GATE = 70        # ≥ 此值 = 高位（预期空间收窄）

# 位置止盈倍率
BUY_SELL_TARGET_POSITION_LOW_MULT = 1.4     # 低位：预期空间放大 40%
BUY_SELL_TARGET_POSITION_MIDLOW_MULT = 1.2  # 中低位：放大 20%
BUY_SELL_TARGET_POSITION_HIGH_MULT = 0.7    # 高位：收缩 30%

# 评分置信度止盈倍率
BUY_SELL_TARGET_SCORE_HIGH_MULT = 1.1       # 评分≥80：放大 10%
BUY_SELL_TARGET_SCORE_LOW_MULT = 0.8        # 评分<55：收缩 20%

# ===== 6级角色分类（V4.3，替换原龙头/跟风二分法）=====
# 角色判定条件（按优先级从高到低匹配，命中即停）
# 1. total_leader    总龙头：连板≥5 + 涨停次数≥4（全市场最高标）
# 2. sector_leader   板块龙头：连板≥3 + 涨停次数≥2（板块内连板最高之一）
# 3. core_trend      中军：大市值(≥300亿) + 连板≤1 + 评分≥65（趋势型权重股）
# 4. sentiment_vanguard 情绪先锋：今日涨停 + 连板=1 + 高换手(≥5%)（率先试盘的弹性小票）
# 5. catch_up        补涨龙：低位(分位<50) + 近期涨停(1-2板)（龙头高位后的低位新领涨）
# 6. follower        跟风：涨幅≥3% + 量比≥1 + 评分≥50（被动跟随）
# 7. marginal        杂毛：其余（辨识度最低、逻辑最弱）

# 角色止盈倍率（叠在位置倍率之上）
BUY_SELL_ROLE_TARGET_MULT = {
    'total_leader':       1.4,   # 总龙头：预期上限放大 40%（容错率最高，穿越分歧）
    'sector_leader':      1.25,  # 板块龙头：放大 25%
    'core_trend':         1.15,  # 中军：放大 15%（稳趋势，不靠连板）
    'sentiment_vanguard': 1.05,  # 情绪先锋：放大 5%（波动大，快进快出）
    'catch_up':           0.95,  # 补涨龙：收缩 5%（高度通常低于初代龙头）
    'follower':           0.8,   # 跟风：收缩 20%（无独立行情）
    'marginal':           0.6,   # 杂毛：收缩 40%（盈亏比极差）
}

# 角色止损倍率（叠在位置倍率之上）
BUY_SELL_ROLE_STOP_MULT = {
    'total_leader':       1.2,   # 总龙头：止损放宽 20%（回撤后恢复概率最高）
    'sector_leader':      1.1,   # 板块龙头：放宽 10%
    'core_trend':         1.0,   # 中军：标准（大市值波动小）
    'sentiment_vanguard': 0.8,   # 情绪先锋：收紧 20%（波动极大，转弱最先被杀）
    'catch_up':           0.9,   # 补涨龙：收紧 10%（龙头崩盘易跟随崩）
    'follower':           0.85,  # 跟风：收紧 15%（破位快）
    'marginal':           0.8,   # 杂毛：收紧 20%（无主力运作，下跌最快）
}

# 角色中文标签（前端展示）
BUY_SELL_ROLE_LABELS = {
    'total_leader':       '总龙头',
    'sector_leader':      '板块龙头',
    'core_trend':         '中军',
    'sentiment_vanguard': '情绪先锋',
    'catch_up':           '补涨龙',
    'follower':           '跟风',
    'marginal':           '杂毛',
}

# 角色判定条件参数
BUY_SELL_ROLE_TOTAL_LEADER_BOARDS = 5       # 总龙头最低连板数
BUY_SELL_ROLE_TOTAL_LEADER_LIMITUPS = 4     # 总龙头最低涨停次数
BUY_SELL_ROLE_SECTOR_LEADER_BOARDS = 3      # 板块龙头最低连板数
BUY_SELL_ROLE_SECTOR_LEADER_LIMITUPS = 2    # 板块龙头最低涨停次数
BUY_SELL_ROLE_CORE_TREND_MV = 300e8         # 中军最低流通市值（300亿，单位元）
BUY_SELL_ROLE_CORE_TREND_SCORE = 65         # 中军最低评分
BUY_SELL_ROLE_VANGUARD_TURNOVER = 5.0       # 情绪先锋最低换手率(%)
BUY_SELL_ROLE_CATCHUP_POSITION_MAX = 50     # 补涨龙最高位置分位
BUY_SELL_ROLE_FOLLOWER_CHANGE_MIN = 3.0     # 跟风最低涨幅(%)
BUY_SELL_ROLE_FOLLOWER_VOL_RATIO_MIN = 1.0  # 跟风最低量比
BUY_SELL_ROLE_FOLLOWER_SCORE_MIN = 50       # 跟风最低评分

# 无 trend_details 时的 fallback 龙头判定（保持向后兼容）
BUY_SELL_LEADER_FALLBACK_CHANGE_PCT = 5.0   # 涨幅≥此值 + 量比≥2 = 龙头
BUY_SELL_LEADER_FALLBACK_VOL_RATIO = 2.0    # 量比阈值
BUY_SELL_LEADER_SCORE_GATE = 80             # 评分≥此值且非高位 = 龙头

# 止盈/止损封顶
BUY_SELL_MAX_TARGET_PCT = 0.15              # 止盈上限封顶 15%

# 位置止损倍率（叠加在振幅止损基础上）
BUY_SELL_STOP_POSITION_LOW_MULT = 1.2       # 低位：止损放宽 20%（波动空间大，给更多恢复余地）
BUY_SELL_STOP_POSITION_HIGH_MULT = 0.8      # 高位：止损收紧 20%（下跌风险大，破位快跑）

# ========== 策略综合评分配置 ==========
STRATEGY_BULL_THRESHOLD = 60  # 策略评分上涨阈值
STRATEGY_BEAR_THRESHOLD = 40  # 策略评分下跌阈值
STRATEGY_PREDICT_BULL_MIN_SCORE = 65  # 策略预测上涨最小分数阈值
STRATEGY_BACKTEST_PREDICTION_LIMIT = 500  # 策略回测预测限制分数
STRATEGY_BACKTEST_HISTORY_DAYS = 30  # 策略回测历史天数阈值

# 2026-09-01 预测保存质量门槛 v2（数据驱动，全周期 05-27~08-31 共 1973 条已验证回测）：
#   涨停(当日涨幅>=9.8%) 是最稳健的核心信号：全周期胜率 56.6%、次日均涨 +1.82%，
#     前后半段均稳定为正（59.7% / 51.8%），64/65 个交易日有信号。
#   非涨停整体负期望（47.2% / -0.08%），仅"综合评分>=80"的高分票为正期望（+1.5%+均涨）。
#   上一版"量比>=0.9 且(非高位或涨停)"门槛经全周期复核反而跑输基准（45.8% vs 49.5%）：
#     错杀"涨停+缩量<1"最优子集（60.7%胜率），且 position 字段 74% 缺失不可靠。
# 新门槛（v2）：
#   当日涨停 或 综合评分 >= PREDICT_SAVE_MIN_SCORE（默认80）。
#   全周期胜率 57.3%、次日均涨 +1.80%，每日选出中位 6 只。
PREDICT_SAVE_QUALITY_GATE = True
PREDICT_SAVE_LIMIT_UP_MIN = 9.8    # 涨停判定涨幅(%)
PREDICT_SAVE_MIN_SCORE = 80        # 非涨停票保留所需最低综合评分


# 策略评分阈值配置 - 各指标的档位划分标准
# 用于判断指标处于高/中/低哪个档位，进而计算得分
STRATEGY_SCORE_THRESHOLDS = {
    'outer_ratio': {'high': 55, 'mid': 50},  # 外盘比例：>55%为高，50-55%为中，<50%为低
    'volume_ratio': {'high': 1.5, 'mid': 1.0, 'low': 0.5},  # 量比：>1.5为高，1.0-1.5为中，0.5-1.0为正常，<0.5为低
    'turnover_rate': {'high': [3, 15], 'mid': [1, 3], 'high_penalty': 20},  # 换手率：3-15%为高，1-3%为中，>20%才扣分（原10%太严，活跃股普遍>10%）
    'change_percent': {'high': 5, 'mid': 2, 'low': 0, 'negative': -2},  # 涨跌幅：>5%为高，2-5%为中，0-2%为低，<0为负
    'weibi': {'high': 30, 'mid': 0, 'low': -30},  # 委比：>30为高，0-30为中，-30-0为低，<-30为负
    'avg_price_deviation': {'high': 1, 'mid': 0, 'low': -1},  # 均价偏离：>1%为高，-1-1%为中，<-1%为低
    'amplitude': {'high': [2, 8], 'mid': [1, 2]},  # 振幅：2-8%为高（原5%太严，涨停/急拉股普遍>5%），1-2%为中，<1%为低
    # 2026-06-30 新增：主力净流入占比（主力净流入/流通市值 ×100%）
    # ≥3%为高（强力吸筹），1-3%为中，0-1%为低，<0为负（主力出逃）
    'main_capital_inflow': {'high': 3.0, 'mid': 1.0, 'low': 0.0, 'negative': -1.0},
    # 2026-07-06 新增：筹码集中度（股东人数户数 + 环比变化率%）
    # tiny_hold/small_hold/large_hold 为户数绝对值门槛；concentrate/dispersion 为环比%阈值
    'holder_structure': {
        'tiny_hold': 30000,          # 小盘筹码门槛（<3万户为小盘，易拉升）
        'small_hold': 80000,         # 中小盘门槛
        'large_hold': 200000,        # 大盘抛压门槛（>20万户抛压重）
        'concentrate_ratio': -10.0,  # 筹码集中阈值（环比减少≥10%为主力吸筹）
        'dispersion_ratio': 10.0,    # 散户化阈值（环比增加≥10%为散户涌入）
        'stable_range': 5.0,         # 稳定区间（环比±5%内为筹码稳定）
    },
    # 2026-07-16 新增：RPS 相对强度（欧奈尔核心因子）
    # RPS 百分位：≥90（强于90%个股，龙头候选）→ high；80-90 → mid；70-80 → low；50-70 → weak；<50 → 弱
    'rps': {'high': 90, 'mid': 80, 'low': 70, 'weak': 50},
    # 2026-07-16 新增：基本面质量（P1a）
    # ROE%(加权净资产收益率) + 归母净利润同比%(SJLTZ)：双高=高质量，双低/亏损=低质量
    'fundamental': {'roe_high': 15, 'roe_mid': 10, 'yoy_high': 30, 'yoy_mid': 15},
    # 2026-07-16 新增：聪明钱（P1b，纯加分信号，无数据不给分）
    # 龙虎榜净买入额(元)：≥5000万=high；≥1000万=mid；>0=low；≤0/无=none(0)
    'smart_money': {'net_amt_high': 50_000_000, 'net_amt_mid': 10_000_000},
}


# 策略评分分值配置 - 各指标在不同档位下的具体得分值
# 用于计算综合评分，分值越高表示该指标表现越好
STRATEGY_SCORE_VALUES = {
    'outer_ratio': {'high': 15, 'mid': 10, 'low': 5},  # 外盘比例得分
    'volume_ratio': {'high': 10, 'mid': 7, 'normal': 5, 'low': 2},  # 量比得分
    'turnover_rate': {'high': 10, 'mid': 7, 'normal': 5, 'low': 3},  # 换手率得分
    'change_percent': {'high': 28, 'mid': 22, 'normal': 16, 'low': 8, 'negative': 0},  # 涨跌幅得分（2026-07-08 强化封板分：high=28/优质封板）
    'weibi': {'high': 10, 'mid': 7, 'low': 4, 'negative': 2},  # 委比得分
    'avg_price_deviation': {'high': 15, 'mid': 10, 'normal': 5, 'low': 2},  # 均价偏离得分
    'amplitude': {'high': 5, 'mid': 3, 'low': 2},  # 振幅得分
    'position': {'high': 10, 'mid': 5, 'low': 0},  # 仓位建议得分
    # 2026-06-30 新增：主力净流入因子（满分15，无数据时给中档7分避免拖累总分）
    'main_capital_inflow': {'high': 15, 'mid': 10, 'low': 7, 'negative': 2, 'no_data': 7},
    # 2026-07-06 新增：筹码集中度因子（满分15，无数据时给中档8分避免拖累总分）
    'holder_structure': {'high': 15, 'mid': 12, 'normal': 8, 'low': 5, 'negative': 2, 'no_data': 8},
    # 2026-07-16 新增：RPS 相对强度因子（满分15，无数据时给中档7分避免拖累总分）
    'rps': {'high': 15, 'mid': 10, 'low': 7, 'weak': 3, 'no_data': 7},
    # 2026-07-16 新增：基本面质量因子（满分15，无数据时给中档8分避免拖累总分）
    'fundamental': {'high': 15, 'mid': 11, 'low': 8, 'weak': 4, 'negative': 3, 'no_data': 8},
    # 2026-07-16 新增：聪明钱因子（满分10，纯加分，无数据=0分不拖累也不虚高）
    'smart_money': {'high': 10, 'mid': 6, 'low': 4, 'none': 0},
}

# ========== 策略委比验证配置 ==========
# 用于验证委比指标是否真实反映买盘强度，防止虚假信号
STRATEGY_WEBI_VERIFY = {
    'weibi_high': 30,        # 委比高于此值视为高委比
    'outer_ratio_low': 48,   # 外盘比例低于此值视为异常，需结合委比判断
    'volume_ratio_low': 0.8, # 量比低于此值视为成交量不足，委比信号可能失真
}

# ========== 主力扫货识别配置（P1-4，2026-07-07）==========
# 核心场景：急拉 + 负委比（卖盘堆积被吃光）+ 高外盘占比 = 主力扫货
# 时间无关：上午/午后/尾盘急拉扫货均可命中，关键是组合特征而非发生时刻
# 例：002384 东山精密 急拉6.79% / 委比-26.67% / 外盘55.7% / 量比1.17
# 原逻辑：负委比给 low(4) 分，低量比给 normal(5) 分 → 总分被低估
# 新逻辑：识别"急拉+负委比+高外盘"组合 → 委比给予 high(10) 分奖励
STRATEGY_MAIN_FORCE_SWEEP = {
    'change_percent_min': 5.0,    # 急拉阈值：涨幅≥5%
    'weibi_negative_max': -10.0,  # 负委比阈值：委比≤-10%（卖盘显著堆积）
    'outer_ratio_min': 52.0,      # 外盘占比阈值：≥52%（买盘主动吃单）
    'volume_ratio_max': 1.5,      # 量比上限：≤1.5（剔除天量对倒，专注"温和量能扫货"）
    'turnover_rate_min': 1.5,     # 换手率下限：≥1.5%（确保有真实成交，排除停牌/无量）
    'weibi_bonus_score': 10,      # 命中时委比得分（=high）
    'volume_ratio_bonus_score': 8, # 命中时量比得分（接近 high）
}

# ========== 主力控盘识别配置（P1-4，2026-07-07）==========
# 核心场景：低量比 + 急拉 + 高换手 = 主力控盘锁仓拉升
# 原逻辑：低量比给 normal(5) 分 → 控盘锁仓特征被低估
# 新逻辑：识别"低量比+急拉+高换手"组合 → 量比给予 high(10) 分奖励
STRATEGY_MAIN_FORCE_CONTROL = {
    'change_percent_min': 4.0,    # 急拉阈值：涨幅≥4%
    'volume_ratio_max': 1.0,      # 量比上限：≤1.0（量能温和）
    'turnover_rate_min': 2.0,     # 换手率下限：≥2%（活跃但有控盘痕迹）
    'outer_ratio_min': 55.0,      # 外盘占比阈值：≥55%（买盘主导）
    'volume_ratio_bonus_score': 9, # 命中时量比得分（接近 high）
}

# ========== 市盈率过滤配置 ==========
# pe_high: 市盈率高于此值视为高估，降低评分
# pe_max: 市盈率超过此值视为异常，直接过滤
STRATEGY_PE_FILTER = {
    'pe_high': 10.5,  # 2026-07-08：从8提高到10.5，避免涨停股(9.8-10%)被仓位因子惩罚
    'pe_max': 700,
}

# 策略标签阈值配置 - 用于根据综合评分判定股票趋势强度
STRATEGY_LABEL_THRESHOLDS = {
    'strong_bull': 80,  # 强势看多：评分≥80，建议积极介入
    'bull': 70,         # 看多：评分≥70，建议关注
    'weak_bull': 60,    # 弱势看多：评分≥60，建议谨慎观察
}

# 默认策略评分权重配置（2026-07-02  优化：参考市面"中长期动量复合打分模型"调整）
# 市面经验：资金面(35%)+均线(40%)+斜率(25%)，对应本系统：
#   资金面 = 主力净流入 + 外盘占比 + 委比
#   龙头溢价 = 涨幅及龙头
#   均线/形态 = 量比 + 换手 + 均价偏离 + 振幅 + 位置
DEFAULT_WEIGHTS = {
    'outer_ratio': 1.1,          # 外盘占比：主动买入意愿，市面"资金面确认"核心，1.0→1.1
    'volume_ratio': 1.0,         # 量比：流动性已通过换手率体现，保持
    'turnover_rate': 1.0,        # 换手率：市面"日均成交额≥5亿"对应，已合理，保持
    'change_percent': 1.2,       # 涨幅及龙头：龙头战法是市面核心，1.0→1.2
    'weibi': 0.9,                # 委比：易被操纵（已检测虚假信号），1.0→0.9
    'avg_price_deviation': 1.0,  # 均价偏离：中性指标，保持
    'amplitude': 0.9,            # 振幅：高振幅既有机会也有风险，1.0→0.9
    'position': 1.0,             # 仓位建议：已含 PE+涨幅组合，保持
    'main_capital_inflow': 1.3,  # 主力净流入：市面强调"连续3日净流入+占流通市值≥0.5%"，1.0→1.3
    'holder_structure': 1.2,     # 筹码集中度：股东人数筹码面，月度数据有滞后，1.0→1.2
    'rps': 1.1,                  # RPS 相对强度：欧奈尔核心因子，强于90%个股给高分，1.0→1.1
    'fundamental': 1.0,          # 基本面质量：ROE+净利润增速，季度数据，质量防护维度
    'smart_money': 0.6,          # 聪明钱：龙虎榜净买+北向，纯加分信号（权重略低避免单一事件主导）
}

# ========== 大盘预警配置 ==========
MARKET_CRASH_THRESHOLD = -2.0  # 市场预警阈值
MARKET_SEVERE_CRASH_THRESHOLD = -3.0  # 市场严重预警阈值

# ========== 退市风险过滤 ==========
DELISTING_KEYWORDS = ['ST', '*ST', 'SST', 'S*ST', 'NST', 'PT', '退', '退市', '退市整理']

# ========== 禁止股票关键词 ==========
# 退市/ST 等风险词由独立的 DELISTING_KEYWORDS + has_delisting_risk() 处理。
# 2026-07-08 精简：移除单字/泛义词（'酒'、'太平洋'、'光明'、'太平'、'华泰'、'前海'、'天安'），
# 这些词会误伤大量无关股票。用更精确的关键词替代。
FORBIDDEN_KEYWORDS = [
    # 金融行业
    '银行', '证券', '保险', '信托', '金控',
    # 保险公司（按名称直接列，避免中国人寿/平安/太保等被漏过）
    '中国人寿', '平安', '太保', '新华保险', '人保', '财险', '寿险',
    '中国人保', '中国平安', '中国太保', '中国太平', '国寿',
    '众安', '华夏人寿', '泰康', '民生人寿',

    # 白酒/酒类消费（原'酒'单字会误伤"酒店"等无关股票，改为精确匹配）
    '白酒', '啤酒', '红酒', '黄酒', '酒业', '酒股份', '茅台', '五粮液', '泸州', '汾酒', '洋河',

    # 家电行业
    '家电', '美的', '海尔', '格力',

    # 房地产行业
    '地产', '房产', '置业', '房地产', '城投', '万科', '保利', '绿地', '碧桂园', '恒大', '融创',

    # 铁路交通
    '铁路', '铁建', '中铁', '高铁',

    # 铁路公路
    '公路', '高速', '路桥', '交建',

    # 航运港口
    '航运', '港口', '水运', '轮船', '船舶',

    # 航空板块
    '航空', '机场', '民航', '国航', '东航', '南航', '海航',

    # 物流
    '物流', '交运', '快递',

    # 乳业食品（移除'光明'、'三元'等泛义词，保留明确乳业词）
    '乳业', '牛奶', '伊利', '蒙牛', '新乳业',

    # 纺织板块
    '纺织', '服装', '服饰', '家纺', '面料', '化纤',

    # 农林牧渔
    '农业', '林业', '牧业', '渔业', '农林', '牧渔', '养殖', '饲料', '种业', '化肥',

    # 传媒
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
POOL_CONSECUTIVE_DOWN_THRESHOLD = 3  # 连跌N天自动移出股票池（0=不启用）
SCHEDULE_DAILY_REVIEW_TIME = '15:10'  # 每日审核时间
SCHEDULE_BACKTEST_TIME = '15:20'  # 回测时间
SCHEDULE_RECOMMENDATION_TIMES = ['09:45', '13:30']  # 智能推荐生成时间（上午开盘后、下午开盘后）

# 趋势发现自动扫描：交易日 9:00 ~ 15:00 每 30 分钟一次（覆盖盘前/盘中/收盘后）
# 包含 9:00（盘前预热）、9:30/10:00/10:30/11:00/11:30（上午盘中）、
# 12:00/12:30（午休过渡）、13:00/13:30/14:00/14:30/15:00（下午盘中+收盘）
SCHEDULE_TREND_SCAN_INTERVAL_MIN = 30  # 扫描间隔（分钟）
SCHEDULE_TREND_SCAN_START = '09:00'   # 扫描起始时间
SCHEDULE_TREND_SCAN_END = '15:00'     # 扫描结束时间

# 趋势评分回测验证闭环（2026-07-01）
SCHEDULE_TREND_BACKTEST_TIME = '15:30'  # 趋势回测定时任务时间（收盘后触发）

# 股东人数更新时间（每日收盘后拉取最新一期，避开交易高峰）
SHAREHOLDER_UPDATE_TIME = '18:00'
TREND_BACKTEST_PERIODS = [1, 3, 5]                  # 回测周期（天）

# 大盘风险预警自动扫描（盘中多时段，覆盖开盘/上午/下午）
# 9:35 = 开盘5分钟早期预警；10:00/10:30 = 上午检查；13:05 = 下午开盘；14:00 = 尾盘
SCHEDULE_MARKET_ALERT_TIMES = ['09:35', '10:00', '10:30', '13:05', '14:00']
MARKET_ALERT_RISK_THRESHOLD = 40  # 风险评分 >= 此值时触发预警推送

# ========== 企业微信选股推送（2026-09-01） ==========
# 盘中准实时推送：满足条件的候选股实时推送到企业微信（wecom-cli message aibot send）。
# chat_id 为授权真人用户的内部 ID（机器人"杨珍的机器人"的授权人），仅内部调用，不对外展示。
WECOM_PUSH_ENABLED = True
WECOM_CLI_CHAT_ID = os.environ.get('WECOM_CLI_CHAT_ID', 'woo0QVPAAAiTuUXPTA8qH_kjcnk1YCSA')   # 授权人 chat_id（内部，优先环境变量 WECOM_CLI_CHAT_ID）
WECOM_PUSH_INTERVAL_SEC = 60        # 盘中轮询间隔（秒），1 分钟级准实时
WECOM_PUSH_START = '09:31'          # 盘中推送开始时间（连续竞价后）
WECOM_PUSH_END = '14:57'            # 盘中推送结束时间（收盘前）
WECOM_PUSH_PREVIEW_TIME = '09:25'   # 开盘前推送"今日关注池"预览时间
# 推送过滤条件（与用户确认的方案，2026-09-01）
WECOM_PUSH_MIN_SCORE = 70           # 综合评分 >= 此值
WECOM_PUSH_MIN_GAIN = 3.0           # 预期涨幅 >= 此值（%）
WECOM_PUSH_MIN_VOL_RATIO = 1.0      # 量比 >= 此值
WECOM_PUSH_MAX_CHANGE = 9.8         # 涨幅 < 此值（未涨停，保留买入空间）
WECOM_PUSH_EXCLUDE_ROLES = ('跟风', '杂毛')  # 排除的角色关键词（stockType 中命中即排除）
TREND_BACKTEST_KLINE_CONCURRENCY = 5                # 批量拉 K 线并发度
TREND_BACKTEST_MARKET_SYMBOL = 'sh000001'           # 大盘基准（上证指数）
TREND_BACKTEST_SCORE_SEGMENTS = [(60, 70), (70, 80), (80, 101)]  # 分数段（左闭右开，80+ 用 101 兜顶）

# ========== 数据查询限制 ==========
QUERY_DAILY_RECOMMENDATIONS_LIMIT = 300  # 历史推荐记录保留天数（load 取最新 N 天 / save 截断到 N 天，均按天数语义）
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

# ========== 东方财富请求头（统一常量） ==========
# 2026-07-02 优化：从 review.py/ai.py/routes.py 三处分散定义统一到此处
EASTMONEY_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Referer': 'https://quote.eastmoney.com/',
    'Origin': 'https://quote.eastmoney.com',
}
