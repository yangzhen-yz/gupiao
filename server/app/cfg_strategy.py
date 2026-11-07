import json
import os
from datetime import datetime

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
# 利空"拿不到"的保守处理（2026-11-07）。原行为：`skip('ann')` 或请求失败一律返回空列表，
# 调用方把"没查到"读成"查过、没有利空"——趋势扫描的利空否决在东财停调日静默失效，
# 而且没有任何地方看得出它失效了（原注释就写着"调用方无感"）。现在分三态 ok/cooling/error：
#   · 判定层一字不变（unknown 绝不判成利空，与 momentum_zone 那条"没有数据不做判决"同口径）；
#   · 但状态非 ok 的票**不再被"趋势高分自动进股票池"**（不可逆动作要等有数据支撑）；
#   · 本轮扫描把未知只数如实写进结果 `annUnknownCount`，趋势页与巡检都看得见；
#   · unknown 只缓存这么短的时间就重试（正常值仍用上面 3600 秒），别一次抖动伪装一小时。
# 两个开关关掉都 = 完全回到改造前。
ANN_UNKNOWN_POOL_GUARD = True
ANN_UNKNOWN_RETRY_SEC = 60
# 利空关键词的命中校准（2026-11-07）。起因是修上面的三态时顺带发现 `stock_list` 传错了形状，
# 公告接口"带前缀照样回 200 但恒空"——于是利空否决从上线那天起没生效过；取数修对后第一次
# 真跑就有数据，关键词表的质量问题立刻变成新的风险：实测可交易范围前 300 只、近 20 日
# 1998 条标题，原表命中 33 条/19 只，其中**至少 9 条是误命中**——
#   · 裸 'ST' 命中的是标题开头的**公司名前缀**（"ST绝味:…权益分派实施公告"5 条），
#     第一条恰好是"撤销其他风险警示"＝摘帽，**方向整个反了**；
#   · "重大事项报告制度"/"防范控股股东及关联人资金占用管理办法"是公司制度文件名；
#   · "未减持公司股份并提前终止减持计划"说的是没减持；
#   · "最近五年被证券监管部门…采取监管措施或处罚情况的公告"是再融资例行披露；
#   · "副总经理辞职"命中 '总经理辞职'、"副董事长辞职"命中 '董事长辞职'（子串）。
# 开=按排除短语 + "副"前缀规则 + 去掉裸 ST 类子串；关掉＝逐字节回到原关键词表。
NEG_ANN_CALIBRATED = True

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
# 龙虎榜留档（2026-11-06）：此前它只在内存 _dt_cache 里，进程重启即空、东财停调即整块没有，
# 而它是评分的第 13 因子 smart_money —— "只有东财一条路"的数据里唯一连旧值都留不住的一个。
DRAGON_TIGER_PERSIST = True        # 拉到就落 dragon_tiger_daily（覆盖写、幂等）
DRAGON_TIGER_DB_FALLBACK = True    # 停调/接口失败时改读库内留档，并在返回里标 source='db'
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


# ========== 买卖点位置感知修正（V4.3，2026-08-17；V4.6 2026-10-28 去角色）==========
# 在 V4.1 振幅动态调整基础上，叠加位置高低/评分置信度两维修正。
# 止盈倍率 = 位置倍率 × 评分倍率，封顶 BUY_SELL_MAX_TARGET_PCT。
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


# ===== 角色体系已于 2026-10-28 整体删除 =====
# 用户原话："所有的角色都去掉吧，像什么龙头、跟风、杂毛，这些所有的都去掉吧。"
# 不再按角色分类计算止盈倍率 / 止损倍率 / 推送否决 / 周期否决 / 前端标签；
# 卖出目标与止损只按 **位置分位 + 综合评分** 两维修正（原角色倍率与判定门槛全部移除）。
# 涨停股的止盈基准统一取原七档的中值 4%（原先 2%~7% 按角色分档）。

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


# ========== 修②（2026-11-03，影子）：零样本三因子从归一化分母摘掉 ==========
# rps/fundamental/smart_money 三因子数据源零样本（strategy_factor_weights 里 sample_count=0），
# 每票都吃 no_data 分却占归一化分母 ~29%，系统性压低总分。
# True=主链也摘掉（上线动作）；False=主链现状不变（逐字节等于现状），
# 影子臂 alt_total 恒算（纯算术零 IO）随 stock['score'] 进 recommendation_batches.stocks_json 留痕，
# 20 个交易日后按可成交净期望 + 最深回撤裁决再定是否翻 True。
STRATEGY_ZERO_SAMPLE_EXCLUDE = False


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


# ========== 自动优化目标 C（2026-11-01 T1.3）：可回滚 ==========
# v2 = 验证窗 D5−D1 分位差改善 pp + 强制 walk-forward + 连续负向回落；
# v1 = 改造前的 discrimination_avg 口径（只在回滚时用，行为与 2026-11-01 之前一致）。
OPTIMIZER_OBJECTIVE_VERSION = 'v2'
OPTIMIZER_FORCE_WALK_FORWARD = True  # True=没有可用验证窗时不采纳任何权重调整
OPTIMIZER_DECAY_FACTOR = 0.8         # 回落：w ← 1 + (w-1)*0.8（向中性收缩）
OPTIMIZER_DECAY_STREAK = 2           # 连续 N 次验证窗改善为负才触发回落
OPTIMIZER_DECAY_MIN_ABS_PP = 0.0     # 改善小于该值（pp）即记负向；0=只认严格负数
# 2026-11-03 新增「只评估不采纳」闸门：False=优化器照算指标、优化历史照记（供观察期审计），
# 但一律不写权重（D1-D4 与网格搜索两个写入点都拦）。跑 5~10 个交易日后再放开为 True。
OPTIMIZER_APPLY = False


# 双源数据交叉校验（2026-10-19，westock 权威源）：每日收盘后把本地资金流/行情与
# westock（腾讯自选股）比对，权威值=westock（用户决策"不一致的以 westock 为准"）。
# 挂在 16:30：收评(15:10)/rec_sim(16:20)/shadow_eval(16:25) 之后，日K同步(18:15) 之前。
WESTOCK_CROSSCHECK_ENABLED = True
WESTOCK_CROSSCHECK_TIME = '16:30'
WESTOCK_UNIVERSE_CAP = 200           # 宇宙封顶（fund flow 单批 10 只，全市场不现实）



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

# ========== 抗跌因子（2026-11-07，修⑥①，只记录不进门槛）==========
# 选股 alpha≈0 已证（10-23 真推荐开盘口径 1/3/5/10 日超额全负），卖出侧更值得做，
# 第一块就是"大盘跌的时候这票扛不扛"。11-03 把四大指数日线本地化进 `index_daily`
# （实测 702 行×4 指数，到 2026-11-05）但一直没有读取方，这个因子就是那个读取方。
# 口径三条（相对最大回撤 / 下跌日 beta / 抗跌日占比）与"板块那一半为什么先不接"
# 写在 `services/pick/anti_drop_shadow.py` 顶部，数字进策略快照的 antiDrop 字段。
ANTI_DROP_SHADOW_ENABLED = True       # 关掉=一个字段都不写，快照与推荐结果逐字节等于改造前
ANTI_DROP_WINDOW_DAYS = 20            # 窗口（交易日）；与 rev20 同尺度，便于交叉对照
ANTI_DROP_INDEX_SYMBOL = 'sh000001'   # 基准指数。板块基准待 cls_plate_daily 攒够 20 天再接
ANTI_DROP_MIN_LAST_LAG_DAYS = 2       # 本地日K最后一根落后基准日超过这么多天就不算（防"假抗跌"）


__all__ = ['ANN_UNKNOWN_POOL_GUARD', 'ANN_UNKNOWN_RETRY_SEC', 'ANTI_DROP_INDEX_SYMBOL', 'ANTI_DROP_MIN_LAST_LAG_DAYS', 'ANTI_DROP_SHADOW_ENABLED', 'ANTI_DROP_WINDOW_DAYS', 'BUY_SELL_MAX_TARGET_PCT', 'BUY_SELL_POSITION_HIGH_GATE', 'BUY_SELL_POSITION_LOW_GATE', 'BUY_SELL_POSITION_MIDLOW_GATE', 'BUY_SELL_STOP_POSITION_HIGH_MULT', 'BUY_SELL_STOP_POSITION_LOW_MULT', 'BUY_SELL_TARGET_POSITION_HIGH_MULT', 'BUY_SELL_TARGET_POSITION_LOW_MULT', 'BUY_SELL_TARGET_POSITION_MIDLOW_MULT', 'BUY_SELL_TARGET_SCORE_HIGH_MULT', 'BUY_SELL_TARGET_SCORE_LOW_MULT', 'DEFAULT_WEIGHTS', 'DRAGON_TIGER_DB_FALLBACK', 'DRAGON_TIGER_PERSIST', 'EASTMONEY_ANN_API', 'FACTOR_WEIGHT_LEARNING_ENABLED', 'FUNDAMENTAL_UPDATE_TIME', 'NEG_ANN_CALIBRATED', 'OPTIMIZER_APPLY', 'OPTIMIZER_DECAY_FACTOR', 'OPTIMIZER_DECAY_MIN_ABS_PP', 'OPTIMIZER_DECAY_STREAK', 'OPTIMIZER_FORCE_WALK_FORWARD', 'OPTIMIZER_OBJECTIVE_VERSION', 'PREDICT_SAVE_LIMIT_UP_MIN', 'PREDICT_SAVE_MIN_SCORE', 'PREDICT_SAVE_QUALITY_GATE', 'RISK_CONTROL_SCORE_HIGH_RISK', 'RISK_CONTROL_SCORE_MID_RISK', 'RISK_CONTROL_STOP_LOSS_MAX', 'RISK_CONTROL_STOP_LOSS_MIN', 'RISK_CONTROL_STOP_LOSS_VOL_MULT', 'RPS_ENABLED', 'RPS_LEADERBOARD_TTL', 'RPS_MIN_SAMPLES', 'RPS_NAME_LIVE_MAX', 'RPS_PERIOD_DAYS', 'RPS_PULLBACK_FROM_LOCAL', 'RPS_TOP_N', 'SLOW_DATA_DAILY_MONTHS', 'SLOW_DATA_FUNDAMENTAL_WEEKDAYS', 'SLOW_DATA_HOLDER_NUM_WEEKDAYS', 'SLOW_DATA_VALUATION_TRADING_ONLY', 'SMART_MONEY_DRAGON_TIGER_DAYS', 'SMART_MONEY_UPDATE_TIME', 'STRATEGY_BACKTEST_HISTORY_DAYS', 'STRATEGY_BACKTEST_PREDICTION_LIMIT', 'STRATEGY_BEAR_THRESHOLD', 'STRATEGY_BULL_THRESHOLD', 'STRATEGY_LABEL_THRESHOLDS', 'STRATEGY_MAIN_FORCE_CONTROL', 'STRATEGY_MAIN_FORCE_SWEEP', 'STRATEGY_PE_FILTER', 'STRATEGY_PREDICT_BULL_MIN_SCORE', 'STRATEGY_SCORE_THRESHOLDS', 'STRATEGY_SCORE_VALUES', 'STRATEGY_WEBI_VERIFY', 'STRATEGY_ZERO_SAMPLE_EXCLUDE', 'TREND_ANNOUNCEMENT_CACHE_TTL', 'TREND_ANNOUNCEMENT_LOOKBACK_DAYS', 'TREND_CONSECUTIVE_BOARD_EXHAUSTION', 'TREND_CONSECUTIVE_BOARD_EXHAUSTION_DEDUCT', 'TREND_EARLY_SIGNAL_BASE_SCORE_MAX', 'TREND_EARLY_SIGNAL_BASE_SCORE_MIN', 'TREND_EARLY_SIGNAL_CONSOLIDATION_BREAKOUT_VOL_RATIO', 'TREND_EARLY_SIGNAL_ENABLED', 'TREND_EARLY_SIGNAL_FIRST_LIMIT_UP_LOOKBACK', 'TREND_EARLY_SIGNAL_FIRST_LIMIT_UP_POSITION_MAX', 'TREND_EARLY_SIGNAL_MAX_BONUS', 'TREND_EARLY_SIGNAL_NR7_PERIOD', 'TREND_EARLY_SIGNAL_NR7_RANGE_RATIO', 'TREND_EARLY_SIGNAL_PER_HIT_SCORE', 'TREND_EARLY_SIGNAL_POSITION_GATE', 'TREND_EARLY_SIGNAL_RSI_BOTTOM_LOOKBACK', 'TREND_EARLY_SIGNAL_RSI_BOTTOM_PERIOD', 'TREND_EARLY_SIGNAL_VCP_MIN_CONTRACTIONS', 'TREND_EARLY_SIGNAL_VCP_VOL_DECAY', 'TREND_RSI_EXTREME_OVERBOUGHT', 'TREND_RSI_OVERBOUGHT', 'TREND_RSI_OVERBOUGHT_DEDUCT', 'TREND_RSI_PERIOD', 'USE_INTRADAY_BREAK_CHECK', 'VALUATION_ENABLED', 'VALUATION_OVERVALUED_PB_PCT', 'VALUATION_OVERVALUED_PE_PCT', 'VALUATION_UPDATE_TIME', 'WESTOCK_CROSSCHECK_ENABLED', 'WESTOCK_CROSSCHECK_TIME', 'WESTOCK_UNIVERSE_CAP']
