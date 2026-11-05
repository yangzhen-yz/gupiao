import json
import os
from datetime import datetime

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


__all__ = ['STRATEGY_INDUSTRY_NEUTRALIZATION_ENABLED', 'STRATEGY_INDUSTRY_NEUTRAL_MIN_SAMPLE', 'STRATEGY_INDUSTRY_P20_PENALTY', 'STRATEGY_INDUSTRY_P80_BONUS', 'TREND_AUTO_ADD_POOL_THRESHOLD', 'TREND_BONUS_ACCELERATING', 'TREND_BONUS_BULL_ALIGNMENT', 'TREND_BONUS_CONSECUTIVE_YANG', 'TREND_BONUS_POCKET_PIVOT', 'TREND_BONUS_STEADY_UPTREND', 'TREND_BONUS_VOLUME_BREAKOUT', 'TREND_CHIP_COST_ABOVE_BOTH', 'TREND_CHIP_COST_BELOW_BOTH', 'TREND_CHIP_COST_BETWEEN', 'TREND_CHIP_CV_NORMAL', 'TREND_CHIP_CV_STABLE', 'TREND_CHIP_CV_VOLATILE', 'TREND_CHIP_FACTOR_MAX', 'TREND_CHIP_FACTOR_MIN', 'TREND_CHIP_MAIN_ACCUMULATE', 'TREND_CHIP_MAIN_DISTRIBUTE', 'TREND_CHIP_MAIN_NEUTRAL', 'TREND_CHIP_TURNOVER_LOCKED', 'TREND_CHIP_TURNOVER_LOOSE', 'TREND_CHIP_TURNOVER_NORMAL', 'TREND_D1_DIVERGENCE_PENALTY_TIERS', 'TREND_D1_TREND_BARE', 'TREND_D1_TREND_FULL', 'TREND_D1_TREND_PARTIAL', 'TREND_D2_CONSECUTIVE_BOARD_TIERS', 'TREND_D2_LIMIT_UP_TIERS', 'TREND_D2_YANG_LINE_TIERS', 'TREND_D3_RATIO_TIERS', 'TREND_D3_SLOPE_TIERS', 'TREND_D3_STABILITY_TIERS', 'TREND_DEDUCT_EXTREME_VOLATILITY', 'TREND_DEDUCT_GROUPS', 'TREND_DEDUCT_LIMIT_DOWN', 'TREND_DEDUCT_MA20_BROKEN', 'TREND_DEDUCT_MA60_MA120_BEAR', 'TREND_DEDUCT_NON_MAIN_BOARD', 'TREND_DEDUCT_POSITION_HIGH', 'TREND_DEDUCT_POSITION_MID', 'TREND_DEDUCT_SHRINK_VOL', 'TREND_DEDUCT_SINGLE_DAY_CRASH', 'TREND_DEDUCT_VOLUME_DIVERGENCE', 'TREND_DYNAMIC_DEFAULTS', 'TREND_DYNAMIC_MARKET_BEAR_CHANGE', 'TREND_DYNAMIC_MARKET_BULL_CHANGE', 'TREND_DYNAMIC_THRESHOLDS', 'TREND_EXCLUDE_MARKET_CAP_MIN', 'TREND_IS_UP_MIN_SCORE', 'TREND_LIMIT_DOWN_THRESHOLD', 'TREND_LIMIT_UP_THRESHOLD', 'TREND_MIN_KLINE_DAYS', 'TREND_MIN_SCORE', 'TREND_PATTERN_DIVERGENCE_VOLUME_EXPAND', 'TREND_PATTERN_FACTOR', 'TREND_PATTERN_REVERSAL_CHANGE', 'TREND_PATTERN_SHRINK_RATIO', 'TREND_PATTERN_SIDEWAYS_AMPLITUDE', 'TREND_PATTERN_VOLUME_BREAKOUT_CHANGE', 'TREND_PATTERN_VOLUME_BREAKOUT_RATIO', 'TREND_POOL_MAX_SIZE', 'TREND_POSITION_BREAKTHROUGH_GAIN', 'TREND_RANGE_20D_FLAT_MIN', 'TREND_SECTOR_FACTOR_COLD', 'TREND_SECTOR_FACTOR_TIERS', 'TREND_SIDEWAYS_BONUS', 'TREND_SIDEWAYS_LOW_PCT', 'TREND_SIDEWAYS_MID_PCT', 'TREND_TOTAL_FACTOR_CAP']
