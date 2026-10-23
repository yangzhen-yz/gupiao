import json
import os
from datetime import datetime

# ========== 数据库配置 ==========
SQLITE_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'gupiao.db')

# ========== API 地址配置 ==========
TENCENT_QUOTE_API = 'https://qt.gtimg.cn/q='
TENCENT_KLINE_API = 'https://ifzq.gtimg.cn/appstock/app/fqkline/get'
SINA_QUOTE_API = 'https://hq.sinajs.cn/list='
EASTMONEY_HOT_LIST_API = 'https://push2delay.eastmoney.com/api/qt/clist/get'

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


# 扣分项（12 项，分 3 组做互斥/衰减）
TREND_DEDUCT_POSITION_HIGH = 10  # 现价位于250日分位 ≥ 80%（新高突破可豁免）
TREND_DEDUCT_MA60_MA120_BEAR = 8  # MA60 + MA120 同步向下
TREND_DEDUCT_POSITION_MID = 5  # 60% ≤ 分位 < 80% 或 MA60向下/MA120向上
TREND_DEDUCT_MA20_BROKEN = 6  # 近 5 日内跌破 MA20 且 3 日未收回
TREND_DEDUCT_LIMIT_DOWN = 15  # 近 20 日出现单日跌停
TREND_DEDUCT_SHRINK_VOL = 10  # 持续缩量阴跌
TREND_DEDUCT_VOLUME_DIVERGENCE = 6  # 近5日股价小幅抬升但20日均量逐级放大超30%
TREND_DEDUCT_EXTREME_VOLATILITY = 8  # 近20日振幅≥15%且收跌
TREND_DEDUCT_SINGLE_DAY_CRASH = 8  # 单日大跌超-7%且次日未修复
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
RPS_MIN_SAMPLES = 300            # 全市场样本不足时跳过计算（避免误算 RPS）
RPS_TOP_N = 100                  # /api/rps 强势股榜默认返回数量
# 榜单接口性能（2026-10-17）：旧实现为榜上 100 只票逐只现拉腾讯日K/名称，/api/rps 要 38~50 秒，
# 前端 fetch 又没超时，一进去整页就挂住。现在默认全部走本地日K库与本地名录。
RPS_PULLBACK_FROM_LOCAL = True   # False = 退回逐只现拉（老行为，用于对照）
RPS_NAME_LIVE_MAX = 10           # 本地名录补不上时，最多还允许现拉几只的名字（封顶，别再放大）
RPS_LEADERBOARD_TTL = 300        # 榜单结果内存缓存秒数：RPS 本来就是一天算一次，没必要每次重算

# ========== 基本面质量因子（P1a，2026-07-16 新增）==========
# 数据源：东方财富 datacenter RPT_LICO_FN_CPD（核心财务指标，季度披露）
# 取最新一期(ISNEW=1)：WEIGHTAVG_ROE(ROE%)+SJLTZ(归母净利润同比%)，每日18:05盘后更新
FUNDAMENTAL_UPDATE_TIME = '18:05'  # 与股东人数(18:00)错开，避免并发拉取

# ========== 聪明钱因子（P1b，2026-07-16 新增）==========
# 龙虎榜净买入额 + 北向资金净流入，作为"聪明钱"个股/市场级信号
SMART_MONEY_DRAGON_TIGER_DAYS = 5       # 龙虎榜聚合近 N 个交易日
SMART_MONEY_UPDATE_TIME = '17:30'        # 交易日后定时拉取龙虎榜+北向最新数据（盘后约17:30发布）

# ========== 估值分位过滤（P2c，2026-07-16 新增）==========
# 数据源：东方财富 clist（复用 fundflow._clist），字段 f9=PE(TTM)、f23=PB。
# 全市场拉取后本地算 PE/PB 百分位（0~100，分位越高越贵），用于过滤极端高估股。
VALUATION_ENABLED = True
VALUATION_OVERVALUED_PE_PCT = 90   # PE 百分位 ≥ 此值视为偏高
VALUATION_OVERVALUED_PB_PCT = 90   # PB 百分位 ≥ 此值视为偏高
VALUATION_UPDATE_TIME = '18:10'    # 与基本面(18:05)错开，避免并发拉取

# ========== 盘后慢变数据刷新节奏（2026-10-19 降频专项）==========
# 背景：股东人数（月/季披露）和基本面（季度披露）此前每天 18:00/18:05 各拉一次全市场分页
# （约 50 页/次、7 天无休），但接口用 ISNEW=1 取的是"最新一期"，一期数据披露之后内容就不再变
# —— 绝大多数请求只是把同样的行重复拉回来，而东财 datacenter 恰恰是 IP 风控的来源。
# 这里只改"什么时候去拉"，不改任何评分口径、不碰盘中链路（行情/资金流/情绪照旧）。
# 时效代价：非财报季时，新披露最多晚 3 天（股东）/ 6 天（基本面）入库；
# 财报季（周末公告尤其密集）自动恢复每日，最多晚 1 天。
# 口径：weekdays 用 datetime.weekday()（0=周一 … 6=周日）；**置空 = 每个自然日都刷 = 完全等于改造前**。
SLOW_DATA_HOLDER_NUM_WEEKDAYS = [0, 3]        # 股东人数：周一、周四
SLOW_DATA_FUNDAMENTAL_WEEKDAYS = [0]          # 基本面：周一
SLOW_DATA_DAILY_MONTHS = [1, 3, 4, 7, 8, 10]  # 这些月份（年报/一季报/半年报/三季报窗口）恢复每日
SLOW_DATA_VALUATION_TRADING_ONLY = True       # 估值只在交易日跑：周末没有新收盘价，拉回来是同一份

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
# 按**股票名称 substring** 做的行业黑名单（银行/地产/白酒/券商…）。
# 2026-10-16 默认关掉，原因是它 90% 的活已经被流动性三条替代了：实测在选票范围 1094 只里，
# 这张 94 词的表只拦掉 24 只（2.2%），而其中明显误伤的有 平安电工（云母绝缘材料，被"平安"
# 当保险公司拦）、洪都航空（军工整机，被"航空"拦）、时代出版/中国出版/出版传媒（被"出版"拦）、
# 吉林化纤/新乡化纤（被"化纤"拦）—— 用名字当行业判据，连坐是必然的。
# 想恢复硬排除就把这条打开；真要按行业排除，正确做法是用行业字段
# （本地 `stock_basic_info.industry` 目前只覆盖 756 只，撑不起全市场判定）。
# 注意：DELISTING_KEYWORDS（ST/退市风险）不受本开关影响，那是风险不是行业偏好。
SECTOR_NAME_EXCLUDE_ENABLED = False
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
POOL_CONSECUTIVE_DOWN_THRESHOLD = 2  # 连跌N天自动移出股票池（0=不启用）——用户要求：连跌2天及以上当天删掉
SCHEDULE_DAILY_REVIEW_TIME = '15:10'  # 每日审核时间
SCHEDULE_BACKTEST_TIME = '15:20'  # 回测时间
# 智能推荐生成时间：上午两次 + 下午三次（14:45 收盘前最终快照，见 SCHEDULE_PRE_CLOSE_SNAPSHOT_TIME）。
# 2026-10-19 加 10:00 这一枪，两个理由：① 09:45 只有开盘 15 分钟，涨幅段还没成形，
# 涨幅段闸门（弱势区/半山腰死亡区）在这时判出来的东西不太可靠；② 上午只有 09:45 一次机会太脆——
# 10-19 实测 09:45 赶上机器网络故障窗口，那一批静默失败、第一批拖到 10:18 才出，整天就少了上午这一档。
# 注意：同一 rec_date 的 daily_recommendations 是先删后插，10:00 会覆盖 09:45 的名单；
# 盘中各时点推了什么要按 slot 回 strategy_snapshot / recommend_shadow_log 看（那里每批都留档）。
SCHEDULE_RECOMMENDATION_TIMES = ['09:45', '10:00', '13:30', '14:05']  # 智能推荐生成时间
# 14:05 这次专给"尾盘买入"用（2026-10-14）：尾盘票的依据是当日振幅和自高点回落，全是盘中数据，
# 13:30 那批离 14:30 还有整整一小时、形态随时变；而 14:45 的收盘快照离窗口关闭只剩 15 分钟，
# 看到也来不及下单。同一天多次生成是"替换"语义（save_daily_recommendations 先 DELETE 当日）。
RECOMMEND_REVIEW_TIME = '16:10'  # 智能推荐复盘任务时间（本地K线库16:00同步后）
SCHEDULE_PRE_CLOSE_SNAPSHOT_TIME = '14:45'  # 收盘前推荐快照：用最新数据最终生成一次并自动保存（保证"最后一次推荐"落库可复盘）

# ========== 智能推荐：日内轨迹风控过滤（2026-10-12，防"涨停炸板/冲高回落"陷阱） ==========
# 背景：2026-10-12 万向德农(sh600371)高开+4.2%→触涨停16.47→炸板崩至-6.81%，
# 而 10:00 的推荐仍以"趋势分97.6+委比88.57%买盘强势"给出71分入选，
# 根因是趋势分取自盘中扫描快照（捕捉到最强瞬间）+ 无日内轨迹过滤。
RECOMMEND_INTRADAY_FILTER_ENABLED = True   # 总开关
RECOMMEND_LIMIT_UP_PCT = 9.8               # 日内高点涨幅≥此值视为"曾触涨停"
RECOMMEND_BOARD_BREAK_DRAWDOWN = 4.0       # 曾触涨停且自日内高点回落≥此值(%)→炸板，不推荐
RECOMMEND_MAX_DRAWDOWN_FROM_HIGH = 8.0     # 自日内高点回落≥此值(%)→冲高回落，不推荐（不限是否涨停）
# 趋势分新鲜度：现价较趋势扫描时价格(checkPrice)下跌超过此比例→趋势分失真，按系数打折
RECOMMEND_TREND_STALE_DRIFT = 0.03         # 3%
RECOMMEND_TREND_STALE_FACTOR = 0.4         # 打折系数（97.6分→39分，无法再靠趋势分越过60分推荐线）

# ========== 死猫反弹趋势门控（2026-10-13，修"下跌趋势短暂反弹股评分高于趋势良好股"） ==========
# 根因：① calc_stock_score_v2 多数调用点(单票评分/持仓/趋势扫描/扫描)未传 trend_result，
#       趋势因子完全缺席，退化为纯日内分；② 日内动量因子只看当日，下跌趋势股单日大涨
#       (死猫反弹)被全部点亮，纯日内分反超温和上行的趋势股约30分。
# 门控：当 trend_result 存在且趋势分处于下行区(<TREND_GATE_LOW_SCORE)且当日涨幅≥
#       TREND_GATE_MIN_CHANGE，判为"下跌中继反弹"，在融合前对日内分打折，抑制虚高。
TREND_GATE_ENABLED = True
TREND_GATE_LOW_SCORE = 45        # 趋势分低于此值视为下行/弱势区（0=被排除/数据不足，走0.7分支不触发门控）
TREND_GATE_MIN_CHANGE = 3.0      # 当日涨幅≥此值(%)才可能是"反弹"；未大涨不触发，避免误伤横盘弱势股
DEAD_CAT_BOUNCE_PENALTY = 0.30   # 日内分打折比例：×0.70（反弹股日内分80→56，再融合低趋势分后总分显著下移）

# ========== 智能推荐：涨幅段闸门（2026-10-18，根因复盘 §3；替换原"半山腰追高惩罚"） ==========
# 复盘数据（218 条，`docs/胜率低根因复盘-2026-10-18.md`）：推荐时当日涨幅
# 0~2%（弱势区）T+1 36.4%/-2.59%、4.5~9.8%（半山腰死亡区）40.0%/-2.27%，
# 两段共 91 条、均值与超额双负；砍掉后剩 52.8%/+0.82%。甜点区 2~4.5%
# 与涨停区 ≥9.8% 历史最强，不拦；负涨幅样本不足（n=2）同样不拦。
# 原 RECOMMEND_CHASE_*（只罚 4.5~9.8% 一段、abs() 误伤下跌股、无开关）已删除，
# 由 services/strategy/momentum_zone.py 统一实现，app_settings 可热改。
RECOMMEND_ZONE_GATE_ENABLED = True       # 总闸：False=完全关（等价 off）
RECOMMEND_ZONE_GATE_MODE = 'drop'        # off=不拦 / demote=罚分 / drop=不进候选
RECOMMEND_ZONE_WEAK_MAX = 2.0            # 弱势区上限（%）：[0, 此值) 算"没涨起来"
RECOMMEND_ZONE_WEAK_PENALTY = 4          # demote 模式弱势区罚分
RECOMMEND_ZONE_CHASE_MIN = 4.5           # 死亡区下限（%）
RECOMMEND_ZONE_CHASE_MAX = 9.8           # 死亡区上限（%），≥此值按涨停区对待不拦
RECOMMEND_ZONE_CHASE_PENALTY = 4         # demote 模式死亡区罚分
# 亏损黑名单：近N天被推荐且T+1亏损超过阈值的股票不再推荐（三祥新材连续两亏26%+案例）
RECOMMEND_LOSER_LOOKBACK_DAYS = 30
RECOMMEND_LOSER_LOSS_THRESHOLD = 3.0  # T+1亏损超过此值(%)进黑名单
# ── 2026-10-17 优化：判据与分级都可调，默认保持 2026-10-12 的老行为 ──
# 老规则有两个毛病（用户确认要改）：① 只看 T+1 一根，噪声大 —— 金安国纪 10-09 推荐
#    T+1 −7.22% 但 T+3 已回到 −2.47%，而利通电子 T+1 只 −3.52%、真正难看的是 T+5 −8.97%；
# ② 一视同仁 —— −3.1% 和 −10.5% 都关 30 天。
RECOMMEND_LOSER_CRITERION = 't1'      # 't1'=现状；'t1_or_t3'=T+1 或 T+3 任一亏超阈值才算
RECOMMEND_LOSER_GRADED = False        # False=统一关 LOOKBACK 天（现状）；True=按下表分级
RECOMMEND_LOSER_HEAVY_LOSS = 5.0      # 重亏线（%）：超过它按 HEAVY_DAYS 关
RECOMMEND_LOSER_HEAVY_DAYS = 30
RECOMMEND_LOSER_LIGHT_DAYS = 10       # 阈值~重亏之间只关这么多天（轻亏早点放回来）
RECOMMEND_LOSER_CACHE_SEC = 600       # 这张表要算全量推荐的 T+1/3/5，盘中扫描 60 秒一次不能每轮重算
# 执行诚实化提示（展示在推荐卡片）。
# 2026-10-18 起这条常量退居"兜底文案"：卡片上的警告改成按票算的（现价比买入上限高多少），
# 胜率数字也换成当天复盘的实测值（`recommend.exec_rates`）。原来那句写着"复盘132次…45%/63%"，
# 样本每天在长、口径刚换成 1094 选票范围，写死的数字迟早变成没人核对的旧结论。
# 同日口径再纠偏（`docs/胜率低根因复盘-2026-10-18.md`）：纸面"低吸参考价 60.4%"里
# 31.7% 是次日没跌到买入价的幻觉胜利（未成交组按构造 100% 胜率），卡片只放
# "低吸单真正成交 42.1%（n=95）+ 成交日追高 25.3% + 现价追入 46.8%（n=218）"。
RECOMMEND_EXEC_HINT_ENABLED = True
RECOMMEND_EXEC_HINT = ('建议挂低吸单或尾盘介入，不追盘中高价：'
                       '按买入参考价成交与按现价追入这两个口径的历史 T+1 胜率差距很大'
                       '（具体数字看推荐复盘页当天统计）')

# ========== 买入区间宽度：核心（现行）+ 扩展（动态·影子）（2026-10-19 用户要求）==========
# 现状：区间上沿恒定为「低吸参考价 × 1.01」（buy_range 的 branch='ref'），1% 与个股波动无关。
# 于是一只 5 日波动 3% 的票和一只 5 日波动 0.8% 的票，"多付一点能不能买到"的答案完全一样，
# 而复盘实测 fill_rate 只有 68% —— 剩下三成"没跌到买入价"，没法回答"我愿意贵买 1% 是否就成交"。
# 本次把上沿做成随近 5 日已实现波动率变化，但**只作为并列的影子口径**：
#   · 核心区间 buyLow ~ buyLow×1.01（现行）：线上挂单价、主统计、历史对照一律不动；
#   · 扩展区间 buyLow ~ buyHighDyn（动态）：卡片多显示一行、复盘多算一组，不改任何既有判定。
# 扩展上沿照样被「该时机的追高线」和「涨停价」封顶——带宽放开不等于鼓励追高。
# ⚠️ 方向要说在前面：扩展口径的成交率必然更高（区间更宽更易触及），但它的收益是按
#    **更贵的成交价**算的，所以胜率与均值大概率变差而不是变好。这不是策略变强或变弱，
#    而是"真买得到"的代价；先看几天并列数据，再决定要不要把它提为线上口径。
BUY_RANGE_DYNAMIC_ENABLED = True     # 打开=卡片多一行「可达 X」标注；关掉=逐字节等于改造前
                                     # （它只往 buyTiming 里加新键和一行文案，**不改** buy 挂单价、
                                     # 不改 buyHigh 核心上沿、不改成交判定、不改任何统计口径）
BUY_RANGE_BAND_BASE_PCT = 1.0        # 核心带宽（就是现行那个 ×1.01）
BUY_RANGE_BAND_MAX_PCT = 3.0         # 扩展带宽封顶：再波动也不超过低吸价 +3%
BUY_RANGE_VOL_BARS = 5               # 取最近 5 个已收盘交易日的日收益标准差（%）
BUY_RANGE_VOL_K = 1.0                # 扩展带宽 = BASE + K×vol5（K=1、vol5=2% → 带宽 3%）
BUY_RANGE_VOL_CACHE_SEC = 600        # 波动率是日级慢变量，盘中 10 分钟一份就够

# ========== 智能推荐：买入时机判定（2026-10-12，区分早盘/尾盘买入） ==========
# 回答"这只票今天该什么时候买"：涨停→次日竞价；强势情绪股→早盘；
# 高振幅/冲高回落/高位追高→尾盘确认；低位趋势股→回踩低吸；其余→盘中灵活。
BUY_TIMING_STRONG_CHANGE_MIN = 5.0    # 强势角色+涨幅≥此值 → 早盘买入
BUY_TIMING_TAIL_AMPLITUDE_MIN = 5.0   # 振幅≥此值 → 尾盘确认（日内不确定性大）
BUY_TIMING_TAIL_DRAWDOWN_MIN = 2.0    # 自日内高点回落≥此值 → 尾盘确认
BUY_TIMING_HIGH_CHASE_CHANGE_MIN = 3.0  # 高位+涨幅≥此值 → 不追高，尾盘或放弃

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
# 智能机器人（aibot 单聊）总开关。2026-10-22 用户决定翻开：准备给同事接扫码绑定
# （扫机器人码→发一句话→持仓页认领会话→按人推单聊）。注意 2026-10-22 实拍：
# 机器人「消息」使用权限已过期（sessions list 返回 errcode=850003），
# 需机器人创建者点企微返回的授权链接续期后才能真正送达；未续期期间发送链路
# 会走 push.py 的连续失败熔断并回落 webhook，不会卡住提醒。
# 将来确认 `message aibot send` 能送达后，这条才承担多用户单聊。
WECOM_AIBOT_ENABLED = True
WECOM_CLI_CHAT_ID = os.environ.get('WECOM_CLI_CHAT_ID', 'woo0QVPAAAiTuUXPTA8qH_kjcnk1YCSA')   # 授权人 chat_id（内部，优先环境变量 WECOM_CLI_CHAT_ID）
# wecom-cli 不在系统 PATH（它装在企微连接器的 connector_runtime/artifacts 下）。
# 留空则由 services/push.py 自动探测：PATH → %LOCALAPPDATA%/*/User Data/Default/
# connector_runtime/artifacts/wecom-cli.exe。连接器换目录/换宿主应用时，
# 用环境变量 WECOM_CLI_BIN 指定 exe 绝对路径最稳妥。
WECOM_CLI_BIN = os.environ.get('WECOM_CLI_BIN', '')
# ========== 企业微信群机器人 Webhook（2026-10-14 兜底通道） ==========
# 背景：智能机器人(aibot)的「消息」权限会在企微服务端过期（实测 errcode 850003），
# 每次都要人点链接或扫码续期 —— 对"止损必须按时叫醒我"这种用途不可靠。
# 群机器人 webhook 是纯 HTTP POST，不过期、不依赖连接器进程，因此作为兜底：
# aibot 发失败时自动改走 webhook，日志写清走的哪条通道。
# 凭据（URL 里带 key）放本地文件 server/wecom_webhook.json，已加进 .gitignore 不入库。
WECOM_WEBHOOK_ENABLED = True
# 消息类型：**默认 text**。2026-10-14 实测：这个群是「微信↔企业微信」互通群，
# 用户在**个人微信**里看它 —— markdown 消息在微信侧整条渲染成
# 「暂不支持此消息类型，点击前往企业微信查看」，等于推送发出去了但人看不见。
# text 类型在微信/企业微信两端都能正常显示，代价是没有加粗和颜色。
WECOM_WEBHOOK_MSGTYPE = os.environ.get('WECOM_WEBHOOK_MSGTYPE', 'text')
WECOM_WEBHOOK_URL = os.environ.get('WECOM_WEBHOOK_URL', '')           # 优先环境变量
WECOM_WEBHOOK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  '..', 'wecom_webhook.json')          # 其次本地文件
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

# ---- 盘中连续扫描的股票范围（2026-10-16 用户要求："把推荐池也塞进每 60 秒的连续扫描里"）----
# 改造前这条链路只扫用户自选池（8~10 只），于是"9:45 那批推荐"生成后就再没人看了：
# 快照那一刻涨 1.1% 的票，10:05 涨到 7% 也不会再提醒 —— 用户看到的永远是过期的时机判断。
WECOM_PUSH_SCAN_RECOMMEND_POOL = True   # False 就退回"只扫用户池"的老行为
WECOM_PUSH_POOL_TOP_N = 60              # 海选池按 20 日强度取前 N 只并入（控制单轮耗时）
WECOM_PUSH_REC_TOP_N = 5                # 再把当日已生成推荐的前 N 只一起盯（它们可能还在涨）
WECOM_PUSH_DAILY_LIMIT = 6              # 达标推送每天最多几只票（与持仓快反的额度分开算）
# 命中这些矩阵档位就不推（2026-10-16 分钟矩阵实测的明确负期望档）；
# "当天不追/不接力"只做标注不删，避免又变成一个没人知道的黑箱否决。
WECOM_PUSH_MATRIX_VETO = ('排除', '减仓日')
WECOM_PUSH_MATRIX_LABEL = True          # 推送文案里带上"这个位置历史什么胜率"
# 盘中 60 秒扫描是否也查亏损黑名单（2026-10-17 补的漏口：以前只有智能推荐查它，
# 被拉黑的票仍会从推送里回来）。关掉=恢复旧行为，仅用于对照。
WECOM_PUSH_APPLY_LOSER_BLACKLIST = True
TREND_BACKTEST_KLINE_CONCURRENCY = 5                # 批量拉 K 线并发度
TREND_BACKTEST_MARKET_SYMBOL = 'sh000001'           # 大盘基准（上证指数）
TREND_BACKTEST_SCORE_SEGMENTS = [(60, 70), (70, 80), (80, 101)]  # 分数段（左闭右开，80+ 用 101 兜顶）

# ========== 策略决策快照（2026-10-17）==========
# 每批次把"扫了谁、每只评多少分、走到哪一步、为什么被挡、当天什么情绪档"落库，
# 目的只有一个：以后能用**真实决策**回测，而不是拿 r20 代理口径重建。
# 为什么不能回填历史：13 因子吃的是一盘中的实时字段（委比/外盘/量比/现价 vs 扫描价），
# 日K库里根本没有这些列，历史每一天只能近似、不能还原。所以从今天开始攒。
STRATEGY_SNAPSHOT_ENABLED = True        # 关掉=停写，推荐链路一字不动
STRATEGY_SNAPSHOT_MAX_ROWS = 900        # 每批次最多写多少只（候选与精选必留，长尾按分数截）
STRATEGY_SNAPSHOT_RETENTION_DAYS = 730  # 保留天数，0=永久（实测 900 行/批 × 4 批 × 250 交易日 ≈ 25 万行/年）
# 每日可成交性监控（2026-10-18，实施计划 P0-0）：16:10 推荐复盘之后跑一次 rec_sim 六种口径，
# 落 rec_sim_daily。存"每天一个点"而不是每次现算，是因为窗口会随日期漂移，
# 现算的两天的数字不可比 —— "放宽动作后 auction 口径有没有恶化"这条判据会当场失效。
REC_SIM_DAILY_ENABLED = True
REC_SIM_DAILY_TIME = '16:20'
REC_SIM_DAILY_MONTHS = 3            # 每天固定回看 3 个月（与用户最初那个问题同口径）

# ========== 回测封板判定（2026-10-20：改判"有没有过突破价成交"）==========
# 旧口径有两处不真实：
#   买入侧把「收盘价==当日最高价 且 涨幅≥97%限额」当成封死直接剔除——可盘中炸过板的票
#   （最低价明显低于涨停价）其实存在过可成交的机会，被一起剔掉了；
#   卖出侧只在「开盘卖 + 次日 open==low==close 且跌幅≥97%限额」（真·一字跌停）才顺延，
#   **尾盘卖（mode='close'）根本没有这道检查** —— 一字跌停的票在 close 口径里照样按收盘价
#   卖掉，回撤因此被系统性低估（而这正是回撤曲线最需要诚实的地方）。
# 新口径按"当日是否存在可成交的突破价"判断：
#   买入：涨停价 = 前收×(1+板幅)；当日最低价 < 涨停价 → 存在过低于涨停的成交 → 可买；
#         最低价 ≥ 涨停价（全天一分都没低于涨停）才是一字/T字封死、买不到。
#   卖出：跌停价 = 前收×(1-板幅)；当日最高价 > 跌停价 → 存在过高于跌停的成交 → 能卖；
#         最高价 ≤ 跌停价 → 卖不掉，顺延到下一根K线，open/close 两种卖法都受这条约束。
# 两个开关各自独立，**都关掉就逐字节回到旧判定**——改造前的曲线必须还能复现，否则对照断掉。
REC_SIM_BOARD_PRICE_V2 = True       # 封死与否改看"有无突破价成交"（炸板回封算可买）
REC_SIM_DEFER_LIMIT_DOWN = True     # 一字跌停卖不掉就顺延（open 与 close 都判）
REC_SIM_BOARD_EPS = 0.005           # 涨跌停价比较容差（半分；价格四舍五入到分后仍需留 eps）

# ========== 回测仓位规则（2026-10-20：风险预算 + 单票上限 + 总敞口，三个独立开关）==========
# 现状（也是关掉时的行为）：`budget = cash / 当天要买的只数` —— 满仓轮动、不留缓冲。
# 实测后果：单笔占净值中位 32%，而**当天只推 1 只时就是一笔押满 99.5%**（7-18~10-19 窗口里
# 有 2 天如此，竞价口径 12 天、尾盘口径 16 天）。逐笔凯利也只有 2.0%（连续口径 μ/σ²），
# 满仓比它高一个数量级，12%~44% 的回撤就是这么来的。
# 这里把仓位规则做成可切换参数，**只为先对照**：默认全关，曲线与改造前逐字节一致；
# 三条各自独立，可以只开单票上限看效果，不必一次全开。
REC_SIM_SIZING_V2 = False           # 总开关：关掉=沿用「现金按当天只数等分」的老规则
REC_SIM_RISK_PER_TRADE = 0.015      # 每笔只让账户承担 1.5% 风险（仓位＝该值 ÷ 止损幅度）
REC_SIM_STOP_PCT = 0.08             # 折算用的止损幅度（注意：线上三套止损还没统一，见任务 #11）
REC_SIM_MAX_POSITION_PCT = 0.25     # 单票占净值上限（治"当天只 1 只就满仓押一只"）
REC_SIM_MAX_EXPOSURE_PCT = 0.60     # 同日总敞口上限（持仓＋当天新买单，留 40% 现金缓冲）
# 影子 vs 线上对照（实施计划"工具：影子对照脚本"）：第1批 P0-2（周期门槛线上化）的上线判据。
# 为什么要有 MIN_DAYS 这道闸门：影子每天只对分数前 15 只候选复算，样本攒得慢，
# 拿三五天的差集去改线上门槛，等于用噪声做决策 —— 不足天数只报数、不给结论。
SHADOW_EVAL_ENABLED = True
SHADOW_EVAL_TIME = '16:25'
SHADOW_EVAL_MIN_DAYS = 10            # 少于 10 个交易日不下结论（10-14 上线，10-30 窗口闭合）

# ========== 评估基座：因子体检（2026-10-22）==========
# 每周六盘前跑一次本地日K体检，落 eval_lab_runs。只读 history_kline.db/gupiao.db，
# 不联网、不改推荐；回答"哪些因子在我们的 2462 只范围内还有横截面预测力"。
EVAL_LAB_ENABLED = True
EVAL_LAB_TIME = '08:30'              # 周六 08:30（非交易日，不抢盘中任务）
EVAL_LAB_WEEKDAY = 5                 # 0=周一 … 5=周六

# ========== 数据采集：09:25 集合竞价快照（2026-10-22）==========
# 用腾讯批量行情，在 9:25 后取竞价匹配价/量。只采选票范围（2462 只），分批请求，
# 失败静默降级（不阻塞 09:45 推荐）。这是以后做"弱转强"的最小数据前提。
AUCTION_SNAPSHOT_ENABLED = True
AUCTION_SNAPSHOT_TIME = '09:25'
AUCTION_SNAPSHOT_BATCH = 60          # 每批代码数；腾讯 URL 不宜过长

# 5 分钟线前向采集（2026-10-22）：只采"今天被推荐/持仓/候选"的活跃票，不全市场。
# baostock 已多次判黑，优先走腾讯 5 分钟接口；默认关，先验证字段与落库口径。
MINUTE5_FORWARD_ENABLED = False
MINUTE5_FORWARD_TIME = '15:05'

# ========== 卖出规则统一（2026-10-22）：影子先跑，实盘开关默认关 ==========
# 三套止损（risk_control / position 分档 / review 明日计划）口径不一致是已知短板。
# exit_rules 生成"固定/逻辑/时间"三机制的统一建议，先作为 exit_plan 影子字段展示；
# 只有 LIVE 打开才覆盖页面动作与推送。关掉 = 逐字节回到改造前。
EXIT_RULES_ENABLED = True
EXIT_RULES_SHADOW_ENABLED = True
EXIT_RULES_LIVE_ENABLED = False
EXIT_RULES_TIME_STOP_DAYS = 5        # 持有满 N 个自然日仍没有 +3% 浮盈 → 时间止损提示
EXIT_RULES_TIME_STOP_MIN_PNL = 3.0   # 时间止损的"及格线"（%）
EXIT_RULES_LOGIC_MA_DAYS = 20        # 逻辑止损参考均线（收盘跌破 MA20）

# 双源数据交叉校验（2026-10-19，westock 权威源）：每日收盘后把本地资金流/行情与
# westock（腾讯自选股）比对，权威值=westock（用户决策"不一致的以 westock 为准"）。
# 挂在 16:30：收评(15:10)/rec_sim(16:20)/shadow_eval(16:25) 之后，日K同步(18:15) 之前。
WESTOCK_CROSSCHECK_ENABLED = True
WESTOCK_CROSSCHECK_TIME = '16:30'
WESTOCK_UNIVERSE_CAP = 200           # 宇宙封顶（fund flow 单批 10 只，全市场不现实）


# ========== 企业微信「自建应用」单聊通道（2026-10-14，待凭据启用） ==========
# 群机器人 webhook 能用，但它是"群"；要给每个注册用户推**单聊**、并且让他点一下链接
# 就完成绑定，需要企业微信自建应用的三个参数（由企微管理员在「管理后台 → 应用管理 →
# 自建应用」里创建后提供）：
#   WECOM_AGENT_CORP_ID   企业 ID（详情页顶部）
#   WECOM_AGENT_ID        应用 AgentId
#   WECOM_AGENT_SECRET    应用 Secret（点「查看」发到管理员企微）
# 另外两件事必须由管理员做，代码替代不了：
#   1) 「企业可信 IP」加上本机出口 IP（错误提示里会直接写出当前 IP）；
#   2) 「网页授权及 JS-SDK 可信域名」——只有配了可信域名，才能走 OAuth 点链接自助绑定；
#      没有公网域名时退一步：用户手填自己的企微 账号ID(userid)，一样能推单聊。
# 三个参数任一为空 → 本通道整体视为"未启用"，绑定接口会明确报缺哪个，
# 绝不静默降级成"看起来绑上了其实发不出"。
_WECOM_AGENT_KEYS = {
    'WECOM_AGENT_CORP_ID': 'corp_id',
    'WECOM_AGENT_ID': 'agent_id',
    'WECOM_AGENT_SECRET': 'secret',
    'WECOM_AGENT_REDIRECT_BASE': 'redirect_base',
}


def _wecom_agent_cred(name: str, default: str = '') -> str:
    """自建应用凭据取值顺序：环境变量 → 本地文件 server/wecom_agent.json → 默认值。

    为什么不要求管理员把三个值配成 Windows 环境变量：这台机器上服务是用
    `python run.py` 直接起的，环境变量得改系统设置并重启终端才生效，容易配错也难核对；
    本地 json 与 webhook 凭据同等待遇 —— 已加进 .gitignore，不入库。
    """
    v = (os.environ.get(name) or '').strip()
    if v:
        return v
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..',
                               'wecom_agent.json'), encoding='utf-8') as f:
            data = json.load(f)
        return str(data.get(_WECOM_AGENT_KEYS.get(name, name.lower())) or '').strip()
    except FileNotFoundError:
        return default
    except Exception as e:
        print(f'[Config] 读取 wecom_agent.json 失败（忽略）: {e}')
        return default


WECOM_AGENT_CORP_ID = _wecom_agent_cred('WECOM_AGENT_CORP_ID')
WECOM_AGENT_ID = _wecom_agent_cred('WECOM_AGENT_ID')
WECOM_AGENT_SECRET = _wecom_agent_cred('WECOM_AGENT_SECRET')
# OAuth 回调用的可信域名（如 https://your-domain.com）；留空 = 不启用扫码/链接绑定，只支持手填 userid
WECOM_AGENT_REDIRECT_BASE = _wecom_agent_cred('WECOM_AGENT_REDIRECT_BASE')


def wecom_agent_missing():
    """返回自建应用还缺哪些参数（全齐则空列表）"""
    miss = []
    if not (WECOM_AGENT_CORP_ID or '').strip():
        miss.append('WECOM_AGENT_CORP_ID（企业 ID）')
    if not (WECOM_AGENT_ID or '').strip():
        miss.append('WECOM_AGENT_ID（应用 AgentId）')
    if not (WECOM_AGENT_SECRET or '').strip():
        miss.append('WECOM_AGENT_SECRET（应用 Secret）')
    return miss


def wecom_agent_ready() -> bool:
    return not wecom_agent_missing()


# ========== 股票范围（universe）——用户口径 2026-10-16 ==========
# 用户原话："目前我暂时只针对沪深主板的股票，而且要排除掉 ST 和 *ST"。
# 这不只是偏好问题：主板涨跌停 10%、创业板/科创板 20%、北交所 30%、ST 5%，
# 而海选/复盘里"封板、炸板、准涨停"全按百分比阈值判定 —— 混板等于混口径。
UNIVERSE_MAIN_BOARD_ONLY = True   # 只做 600/601/603/605 + 000/001/002/003
UNIVERSE_EXCLUDE_ST = True        # 剔除 ST/*ST/退市整理（名称含 ST 或"退"）
# 名称与状态表：需要先在办公室电脑执行 `python scripts/sync_stock_basic.py`
# （baostock 全量名录 → history_kline.db 的 stock_basic_full；那张表之前是空的，ST 根本判不了）

# ========== 总市值下限（2026-10-16，防量化操控小微盘） ==========
# 用户口径：剔除总市值 < 35 亿的票。小盘股流通盘小、易被少量资金拉抬/砸盘做出形态，
# 评分与推荐里混进来等于把"被做出来"的信号当趋势。落点覆盖全链路：
# 选票（候选池/智能推荐/趋势扫描/策略扫描）+ 同步K线 + 分时线。
# 取数约束（2026-10-16 实测）：东财 clist 批量、ulist 批量都不返回市值（f116='-'/0），
# 只有单只 push2 stock/get 可靠返回 f116(总市值,元)/f117(流通市值,元)/f84(总股本)，
# 故后台按本地日K库清单逐只拉取灌本地表 stock_market_cap，盘前只读本地表不外联。
# 数据层与过滤逻辑见 services/market_cap.py。
MARKET_CAP_FILTER_ENABLED = True
MIN_TOTAL_MARKET_CAP = 35e8        # 总市值下限（元），35 亿

# ========== 流动性/价位下限（2026-10-16 用户追加，"减少无效的筛选"） ==========
# 用户原话："近20个交易日日均成交额大于8000万，近20个交易日平均换手率大于1.5%，股价大于3元"。
# 这三条都在筛"今天能不能买得进去、卖得出来"：成交额与换手不够的票，形态再好也只是账面收益，
# 实际挂单会滑；3 元以下票集中在被边缘化的老股/庄股，跳空与单日异动的概率明显更高。
# 实测三条同时通过的只数：沪深主板非ST 且 市值≥35亿（2462 只）里 **1094 只**通过，
# 其中收口最紧的是换手率（只有 1234 只 >1.5%），成交额 1882 只、价格 2340 只。
LIQUIDITY_FILTER_ENABLED = True    # 总开关（关掉=三条一起不生效）
# 三条各自独立开关：想单独试某一条的作用，只关它这一行即可。
# 三条全关时 `liquidity.passes()` 一律放行（连"窗口内没K线就剔除"也不生效），
# 这样"关开关"是真的回到改造前，而不是留下一个隐蔽的剔除规则。
LIQ_AMOUNT_ENABLED = True          # 日均成交额
LIQ_TURNOVER_ENABLED = True        # 日均换手率
LIQ_PRICE_ENABLED = True           # 最低股价
LIQ_STATS_WINDOW = 20              # 统计窗口：近 20 个交易日（与用户口径一致）
LIQ_MIN_AVG_AMOUNT = 8e7           # 日均成交额下限（元）= 8000 万
LIQ_MIN_AVG_TURNOVER_PCT = 1.5     # 日均换手率下限（%）
LIQ_MIN_PRICE = 3.0                # 最新收盘价下限（元）；前复权最后一根 = 实盘价
# 换手率分母用**流通股本**（= 流通市值 / 最新价），不用流通市值本身：
# 窗口内涨过一波的票，用当前市值当分母会把过去的换手算轻（实测 603200 市值法 7.55% vs 股本法 8.84%）。
# 复盘子页"口径与开关"的写权限：这些开关一改就影响**所有账号**的选票范围，
# 而多账号是自注册体系、users 表没有角色列 —— 所以读=登录即可，写=只允许白名单账号。
# 留空 () = 自动取**最早注册的账号**（id 最小那个）当管理员。
# ⚠️ 别写死 id：2026-10-17 第一版填的是 (1,)，而本机唯一账号其实是 id=2 的 yz
#    （AUTH 关闭时"视为 1 号用户"那句注释不等于真实 id），结果谁登录都只能看不能改。
SETTINGS_WRITE_USER_IDS = ()
MARKET_CAP_WORKERS = 4             # 刷新线程数：实测 12 线程猛拉约 1100 次后被东财限流，降到 4 线程才稳
MARKET_CAP_REQUEST_DELAY = 0.5     # 每个请求前小睡（秒），配合低并发避开限流；周刷新慢几分钟无所谓
# 行业分类周更（2026-10-19）：`stock_industry_map` 由 baostock 一次登录全量刷新。
# 为什么单独一张表、还周更：资金流向页的「行业」列原来只有东财 clist f100 一个来源，
# 10-19 clist 被封一整天 → 整列变"—"、板块聚合也没了分母。行业几个月才动一次，
# 不该跟着行情接口一起失效。放在周二凌晨：离 18:15~20:15 日K同步和 09:00 海选都远，
# 且只在 baostock 冷却期外才登录（脚本里读同一份冷却文件）。
INDUSTRY_SYNC_WEEKDAY = 'tuesday'
INDUSTRY_SYNC_TIME = '03:30'

MARKET_CAP_REFRESH_WEEKDAY = 'sunday'  # 每周刷新星期（schedule 库：monday..sunday）
MARKET_CAP_REFRESH_TIME = '18:30'  # 刷新时刻（盘后，错开 16:00 K线同步 / 18:05 基本面）

# ========== 盘前全市场海选候选池（2026-10-15） ==========
# 替代"只看东财人气榜 444 只"的入口：用本地日K库向量化海选，把没上热搜但已走主升的票捞进来。
# 回放验证：九阳股份 09-11 命中（其后 +81.6%）、澳弘电子 09-14 命中（+74.9%），
# 而旧入口对九阳从头到尾无感。
CANDIDATE_POOL_ENABLED = True
CANDIDATE_POOL_MAX = 250          # 每天最多喂给评分链路多少只（控制 9:45 那批的耗时）
CANDIDATE_POOL_MIN_BARS = 80      # 上市不足 80 根日K的不参与（MA60/60日高点要算得出来）
CANDIDATE_POOL_RPS_TOP = 80       # 20 日涨幅需排进当天全市场前 20%（≥80 百分位）
CANDIDATE_POOL_NEAR_HIGH = 0.92   # 收盘需在 60 日最高点的 8% 以内（强势未破位）
CANDIDATE_POOL_TIME = '09:00'     # 盘前海选时刻（本地日K由 18:15 同步，9:00 已含昨收）
# 出池前要求的本地日K覆盖度：当天有K线的股票数 / 全库股票数。
# 只看 MAX(date) 会被半截的一天骗过去（盘后同步是逐只写入的），
# 覆盖度不足就当数据不新 —— 宁可用老池子并告警，也不要拿"只有一刻钟的市场"去海选。
KLINE_POOL_MIN_COVERAGE = 0.90

# 手动静音的日期列表（当天所有企业微信推送都不发，含 T+1 预案/情绪提醒/买入候选）。
PUSH_MUTE_DATES = ('2026-10-15',)


def push_muted_today() -> bool:
    """当天是否静音所有企业微信推送（2026-10-15 用户要求：今天别推了）。

    用日期列表而不是布尔总开关：布尔容易忘了关回来，第二天该止损时反而没提醒。
    列里的日期一过，推送自动恢复。
    """
    return datetime.now().strftime('%Y-%m-%d') in PUSH_MUTE_DATES


# ========== 盘中短线情绪温度计（2026-10-14 新增） ==========
# 背景：五维情绪周期（涨停家数/炸板率/连板高度/昨日涨停溢价/涨跌家数）原来只在 15:10
# 收评算一次，选股链路（recommend/strategy）从头到尾只看得到上证指数涨跌幅。
# 结果是冰点日和高潮日，同一只票同一套阈值算出同一个分、同样进 Top3。
# 本模块把同一套口径搬进盘中，并作为"门槛/否决"参与决策——不改 13 因子权重，
# 避免污染 15:35 auto_optimizer 学到的 strategy_factor_weights。
SENTIMENT_ENABLED = True
SENTIMENT_REFRESH_TTL_SEC = 180       # 盘中情绪刷新间隔（秒）：3~5 分钟，压住东财限流风险
SENTIMENT_SNAPSHOT_SAVE_SEC = 300     # 情绪快照落库间隔（秒），供影子回测/复盘对照
# 实时涨停池不可用时，允许沿用多少秒内的当日盘中快照。超过才退收评。
# 2026-10-23 晚修正：原 90 分钟会在下午长冷却时退回昨日收评，用户明确要求
# “盘中不要显示昨天”。改成 6 小时覆盖整个交易时段（09:15→15:05）；宁可显示
# 今早的快照并标注 HH:MM，也不把昨天的连板高度当成今天的。查询仍限当日。
SENTIMENT_SNAPSHOT_FALLBACK_MAX_AGE_SEC = 21600
SENTIMENT_ZT_POOL_PAGESIZE = 600      # 涨停池单页拉取上限（全市场涨停极少超过此数）
SENTIMENT_PREMIUM_MAX_CODES = 200     # 昨日涨停溢价最多查多少只（与 review 口径一致）
# 情绪周期 → 精选推荐最低分：改门槛而非改扣分，纪律不该被其他因子平均掉
SENTIMENT_MIN_SCORE = {'冰点': 72, '退潮': 68, '分歧': 65, '回暖': 60, '发酵': 60, '高潮': 64}
# 情绪周期 → 操作纪律。env_shift 是在"大盘涨跌幅档位"上的升降档；
# allow_chase=是否允许追高（涨幅已过 5% 的票）；allow_relay=是否允许接力高位连板；
# veto_roles=硬否决角色（命中即不进"此时机可介入"，移入"稍后时机"并写原因）。
# 高潮期 allow_chase=False 是"卖在一致"：越是人人赚钱越不追第一，防接最后一棒。
CYCLE_RULES = {
    '冰点': {'env_shift': -2, 'allow_chase': False, 'allow_relay': False,
             'veto_roles': ('跟风', '杂毛', '补涨龙'), 'note': '情绪冰点：严禁接力，只做超跌低吸或空仓等待'},
    '退潮': {'env_shift': -1, 'allow_chase': False, 'allow_relay': False,
             'veto_roles': ('跟风', '杂毛'), 'note': '亏钱效应扩散：规避高位股，有利润即刻走，不补仓不格局'},
    '分歧': {'env_shift': 0, 'allow_chase': False, 'allow_relay': False,
             'veto_roles': ('杂毛',), 'note': '多空拉锯：降低操作频率，等方向明确'},
    '回暖': {'env_shift': 0, 'allow_chase': True, 'allow_relay': False,
             'veto_roles': ('杂毛',), 'note': '情绪修复：积极低吸主流题材低位股'},
    '发酵': {'env_shift': 1, 'allow_chase': True, 'allow_relay': True,
             'veto_roles': ('杂毛',), 'note': '情绪主升：可跟随主线龙头，注意仓位节奏'},
    '高潮': {'env_shift': 0, 'allow_chase': False, 'allow_relay': True,
             'veto_roles': ('跟风', '杂毛'), 'note': '一致看多：不追第一，逐步兑现，谨防高位分歧'},
}
# 硬否决的确认条件：单指标假信号多（炸板率高但涨停数也高 = 分歧而非退潮），
# 要求至少 2 个弱信号同向，且过 10:00 才启用（9:35 涨停数天然少，会把回暖日误判成冰点）
SENTIMENT_VETO_MIN_WEAK_SIGNALS = 2
SENTIMENT_VETO_AFTER_MIN = 10 * 60    # 当日分钟数，10:00
# 梯队崩塌 / 龙头失势：昨日 3 板以上个股今日晋级率 ≤ 此值（且昨日基数≥3只）→ 梯队崩塌；
# 昨日高位股今日跌幅 ≤ 此值 → 龙头失势（养家式无条件离场信号）
SENTIMENT_LADDER_COLLAPSE_RATE = 0.5
SENTIMENT_LEADER_FAIL_CHANGE = -5.0
SENTIMENT_LADDER_MIN_BASE = 3

# ========== 市场主线/支线板块（2026-10-22，计划 crisp-summit-newt） ==========
# 五维评分：涨停梯队 0.30 + 主力资金 0.30 + 涨幅持续性 0.20 + 量能 0.10，
# 情绪周期做市场级门槛不做板块分。各维先在数据源内做分位归一——东财 f62 /
# 新浪 r0 / 旧快照三套口径的绝对额不可跨源比较，分位单调性才可比。
# 排除维度（政策催化/产业链/业绩支撑）：本仓库无结构化数据源，进分=把主观包装成分数。
MAINLINE_ENABLED = False              # 总开关：关＝行动台无 sectors 卡、零执行、返回体等于现状
MAINLINE_REFRESH_TTL_SEC = 300        # 判定缓存（用户要求 5 分钟级）
MAINLINE_SNAPSHOT_SAVE_SEC = 300      # 判定落库间隔（market_mainline_snapshots，攒影子数据）
MAINLINE_WEIGHTS = {'limit': 0.30, 'fund': 0.30, 'persist': 0.20, 'volume': 0.10}
MAINLINE_MIN_SCORE = 62               # 主线门槛（冰点/退潮档 +8）
MAINLINE_SUB_MIN_SCORE = 42           # 支线地板；最高分低于它 → "无主线"（空仓哨兵）
MAINLINE_GAP = 10                     # 第2主线与第1分差≥此值 → 断层，只留1个
MAINLINE_JUDGE_AFTER_MIN = 10 * 60    # 10:00 前不判 main（与情绪否决同一分钟数）
MAINLINE_FLOW_STALE_SEC = 7200        # 资金流快照超 2h → 不判 main（旧资金定不了今日主线）
SECTOR_MIN_MEMBERS = 8                # 板块持续性聚合的最少成员（交集口径），不足 → None
DESK_SECTOR_CHIPS_IN_SENTIMENT = False  # 情绪条上的 leadSectors chip（名单归一到新卡后默认撤）

# ========== 因子权重学习开关（2026-10-14） ==========
# 背景：strategy._adjust_weights 自 2026-09-04 起每次抛 AttributeError
# （sqlite3.Row 没有 .get()，IC 引擎第一行就崩），崩在外层 try 里 → 回测报告不落库、
# 权重不落库，**strategy_factor_weights 冻结在 2026-09-04 15:20:12 至今 40 天没动过**。
# 已实测近 30 天核销样本仅 98 行，凑不出"≥10 样本 × ≥5 截面日"的 IC，会回退区分度兜底；
# 近 90 天才有稳定 IC：change_percent IC_IR +0.34（方向一致 69%，该增权）、
# amplitude IC_IR -0.40（方向一致 38%，该降权）、main_capital_inflow +0.30（该增权）。
# 结论：修 bug 恢复的是"策略能自我纠错 + 回测可观测"，不等于胜率立刻提升——
# 所以先影子运行（调整明细照样写进回测报告的 weight_adjustments，但不落权重），
# 攒几天对照后把本开关改 True 才真正让线上评分吃到权重学习。
FACTOR_WEIGHT_LEARNING_ENABLED = False

# ========== 持仓快反推送（2026-10-14 新增） ==========
# 原状态：push.py 只推"买入候选"，持仓跌破止损/到止盈全靠前端轮询，
# 最快 60s 轮询 + 8s 行动台缓存 + 5s 行情缓存 ≈ 73 秒才可能被看见，
# 用户不在电脑前（办公室窄屏/手机）时等于看不见。
POSITION_WATCH_ENABLED = True
POSITION_WATCH_INTERVAL_SEC = 20      # 盘中扫描间隔（秒）
POSITION_WATCH_START = '09:30'
POSITION_WATCH_END = '15:00'
POSITION_WATCH_REPUSH_DROP_PCT = 2.0  # 已推过止损后，价格再跌此幅度允许重推一次
# 每人每天最多发多少条持仓类提醒（防轰炸：异常行情下一轮扫出多只票会连着响）。
# 超出后信号仍然照常记入行动台页面，只是不再发消息。0 或留空 = 不限。
PUSH_DAILY_LIMIT_PER_USER = 10
# 监控哪些账号的持仓。**留空 = 全部账号**（2026-10-14 改）。
# 原来硬编码 (1,)，而 id=1 是调试账号 sim_dbg、真实用户 yz 是 id=2 ——
# 结果是"扫了没人看的仓，真正在跑的仓一条都不推"。写死 id 迟早再踩一次，默认全扫。
POSITION_WATCH_USERS = ()
# 次日竞价推送"昨日冻结·今日可卖"预案的时刻。A股 T+1：当天买的当天卖不掉，
# 所以那天买的票触发的信号，正确的提醒时点是第二天 9:15 集合竞价（能挂单了）。
PUSH_T1_PLAN_TIME = '09:15'

# ========== 数据查询限制 ==========
QUERY_DAILY_RECOMMENDATIONS_LIMIT = 300  # 历史推荐记录保留天数（load 取最新 N 天 / save 截断到 N 天，均按天数语义）
QUERY_TREND_RESULTS_LIMIT = 200  # 趋势结果数量限制
QUERY_MARKET_REVIEWS_LIMIT = 30  # 市场评论数量限制

# ========== 收评配置 ==========
REVIEW_TOP_GAINERS_COUNT = 15  # 顶部涨股票数量限制
REVIEW_TOP_LOSERS_COUNT = 15  # 顶部跌股票数量限制
REVIEW_TOP_SECTORS_COUNT = 5  # 顶部行业数量限制
REVIEW_TOMORROW_FOCUS_COUNT = 6  # 明日关注最多几只（2026-10-14：从 10 收到 6，按历史胜率选而非按来源顺序凑）
# 入选门槛：该来源该分数段的历史 T+1 胜率 ≥ 50% 且平均收益 > 0。
# 50% 就是抛硬币线 —— 低于它的票进名单只会把整篮子的胜率拉下去（用户明确要"提示整体胜率"）。
# 实测：趋势扫描各档平均收益全为负（整体 42.1% / -0.40%，n=1335），因此整层被挡在门外。
REVIEW_TOMORROW_FOCUS_MIN_WIN_RATE = 50.0
# 弱市里可能一只都过不了门槛，留个下限保证名单不空白（按胜率从高到低补）
REVIEW_TOMORROW_FOCUS_MIN_KEEP = 3


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

# ========== 本地历史日K线库（A 级优化，2026-09-25 新增） ==========
# 借鉴 Sequoia-X：用 baostock（免费/免注册/无限流）把全市场前复权日K落到本地 SQLite，
# RPS/趋势/评分的区间涨幅从本地库向量化计算，彻底绕开东方财富限流与逐股现拉导致的 167s 扫描。
# 选前复权：最新价=实盘价，与实时行情拼接无缝；前/后复权对收益率只差常数倍，RPS 排名不受影响。
KLINE_HISTORY_ENABLED = True
KLINE_HISTORY_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'history_kline.db')
KLINE_HISTORY_START_DATE = '2024-01-01'  # 回填起点（约 2.5 年，覆盖 250 日 RPS 所需窗口）
KLINE_HISTORY_SYNC_TIMES = ['18:15', '18:45', '19:15', '19:45', '20:15']
# ↑ 为什么是一串时刻而不是一个：2026-10-16 实测 16:00 那次同步**写入 0 条**、
#   跑完后 `stock_daily` 里当天(10-16)仍是 0 行 —— baostock 的当日 EOD 没这么早出。
#   原来 16:00 一个点跑完就算今天完了：数据没到 = 当天白跑，还连 RPS 一起变成旧账。
#   现在从 18:15 起每 30 分钟重试，谁先等到数据谁就把今天的账做完，后面的时刻自动跳过；
#   最后一个时刻(20:15)仍拿不到数据，才由盘后自检真的告警（避免 16:05 就叫假警报）。
KLINE_HISTORY_SYNC_TIME = KLINE_HISTORY_SYNC_TIMES[0]   # 兼容旧引用
KLINE_HISTORY_SYNC_LAST = KLINE_HISTORY_SYNC_TIMES[-1]  # 过了这个点还没数据，才算真失败

# 日K同步的范围是否只跟"选股范围"（主板 + 非ST/退 + 总市值≥下限）。
# 2026-10-16 用户原话："目前我不是增加了限制默认所有的选股条件：沪深主板、非ST和*ST、市值大于35亿"。
# 实测这条把同步清单从 5222（整库）压到 2462 —— 原来每晚有 ~2160 次 baostock 查询花在
# 创业板/科创板/北交所/ST 上，那些票选票环节 100% 会被剔除，等于纯浪费配额；
# 更糟的是多出来的那部分登录次数正是 19:40 被 baostock 判定"黑名单用户"的诱因之一。
# 关掉它 = 恢复整库同步（历史存量本来就在库里，不受影响）。
KLINE_SYNC_UNIVERSE_ONLY = True
# 连续失败多少只就判定"被限流/拉黑"并终止本次（不再逐只 login/logout 加深封禁）。
KLINE_MAX_CONSECUTIVE_FAILS = 20
KLINE_RECONNECT_EVERY = 800      # 主动重连间隔：长连接确实会僵，但每 200 只重登一次太激进

# ========== 收盘日K主路：腾讯批量快照（2026-10-16，被 baostock 拉黑后改架构） ==========
# 每天补"今天这一根K线"，本来不需要登录任何服务：腾讯 qt.gtimg.cn 一条请求带 ~60 只的
# 收盘快照（今开/最高/最低/收盘/成交量/成交额），2462 只 ≈ 41 次请求、几秒钟，且没有会话概念。
# baostock 从此退为**备路**：只在主路失败、补历史某天、或重算除权票前复权序列时才登录。
TENCENT_SNAPSHOT_ENABLED = True
TENCENT_SNAPSHOT_BATCH_SIZE = 60     # 单次请求带多少只（腾讯这条接口实测 60~80 稳定）
TENCENT_SNAPSHOT_MAX_DIFF = 0.02     # 与 baostock 同日数据比对的容差；超过就拒绝写库（防字段改版）
# 一旦确认拉黑（10001011），本机所有进程都停止敲门的冷却时长（分钟）
BAOSTOCK_BAN_COOLDOWN_MIN = 360

# 买法矩阵按新分母重跑（一次性任务）：02:10 拉起脱离进程，脚本自己等 baostock 冷却解除。
# 为什么放在凌晨：前三张只吃本地日K（几分钟），第四张要补 5 分钟线 = 依赖 baostock，
# 而它今晚刚把我们的 IP 判成黑名单；凌晨那次登录竞争最少，也不会挤占 09:00 海选的窗口。
MATRIX_RERUN_ENABLED = True
MATRIX_RERUN_TIME = '02:10'

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

# ========== 多用户账号与认证（2026-10-12） ==========
# 背景：持仓是每个用户私有的，行情/推荐/收评是公共的 —— 因此只对持仓等私有资源做认证，
# 公共接口保持免登录，收评定时任务、截图脚本等既有自动化不受影响。
AUTH_ENABLED = True             # 总开关（置 False 可整体回退到免登录模式，持仓归 user_id=1）
# 2026-10-13：cloudflare 隧道暴露公网期间临时置 False，隧道关闭后恢复 True。
# 下次若要再次对外分享链接，记得先把它置 False（自助注册 = 任何人都能进来读全量公共数据）。
AUTH_ALLOW_REGISTER = False      # 2026-10-14 隧道暴露公网期间临时关闭自助注册，隧道关闭后恢复 True
AUTH_SESSION_DAYS = 30          # 会话有效期（天）
AUTH_MIN_PASSWORD_LEN = 6       # 最短密码长度
AUTH_PBKDF2_ROUNDS = 120000     # PBKDF2-SHA256 迭代轮数（标准库实现，无额外依赖）
AUTH_MAX_LOGIN_FAILS = 5        # 同账号连续登录失败上限，超过后临时锁定
AUTH_LOCK_MINUTES = 5           # 登录失败锁定时长（分钟）
