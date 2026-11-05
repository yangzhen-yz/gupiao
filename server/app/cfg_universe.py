import json
import os
from datetime import datetime

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
# 流动性统计（stats_map）的重算节奏（2026-11-04 改）
# 它的输入是本地日K `stock_daily`（实测 346.5 万行，索引只有 (symbol,date)），
# 一次 force 重算单独计时 **14.9 秒**；而此前调度器每 9 分钟的保活循环里都无条件 force 重算，
# 线上日志实测那一行是 **12~170 秒**（长尾=与每 20 秒的推送扫描/日K同步抢资源）。
# 可是日K只在收盘后才变——盘中重算 100% 是白算，代码里 `_STATS_TTL` 的注释自己都写着
# "一天只随行情变一次"。现在保活循环只在"距上次刷新超过兜底上限"时才重算，
# 正常路径改为**日K同步完成后立刻刷一次**（见 core/scheduler.py::job_sync_history）。
# `LIQ_STATS_KEEPWARM_FORCE=True` 逐字节回到"每轮都 force"。
LIQ_STATS_KEEPWARM_MAX_AGE_SEC = 21600   # 保活兜底上限 6 小时：同步钩子失灵时最迟 6 小时补一次
LIQ_STATS_KEEPWARM_FORCE = False
# 启动/保活那步"universe 选票范围缓存完成"线上实测 190.2s（15:11 那次）与 751.1s（15:27 那次），
# 而进程外单独计时同样这几层只有 ~15 秒——差值要么在某一层的冷缓存装载，要么在排队等资源。
# 下面这个阈值只控制"总耗时超过多少秒才打一行分段耗时"，纯观测、不参与任何判定；
# 设成很大（如 99999）就是完全静音。
UNIVERSE_STEP_LOG_SEC = 5.0
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
# 2026-11-03 接线 sentiment_alt（新浪备源）：东财冷却/涨停池失败时，先用备源算
# 涨停/跌停家数与涨跌家数（剔 ST 口径），备源也挂才退盘中快照/收评。
# 备源给不了连板梯队/炸板/昨日溢价，快照会如实标 gap 并 stale（只提示不否决）。
# 关掉即回到"东财挂→直接退快照/收评"的旧行为。
SENTIMENT_ALT_FALLBACK = True
# 快照落库的 stale 判据（2026-11-04 修，用户"大盘这么好为什么没推"顺带查出）：
# `_maybe_save` 原来写的是"stale 一律不落库"。这条禁令的本意是防**沿用旧值**把当天
# live 行覆盖掉（`_latest_live_snapshot` 兜底读的是 stale=0 的行）——但它连"数字是此刻
# 采的、只是某一维取不到"一起拒了：东财涨停池取不到时主路 `stale=not zt_pool` 为真，
# 新浪备源（11-03 接线）返回也固定 `stale=True`。东财连续两天不可用 ⇒
# `market_sentiment_snapshots` 11-03/11-04 **两天 0 行**（最后一条停在 11-02 13:01），
# 盘中情绪档的审计链整段断了，11-03 新加的"情绪减仓影子"读的就是这个档位。
# 现在按来源分面：`SENTIMENT_SNAPSHOT_FRESH_SOURCES` 里的是"当场采集"（intraday 主路、
# alt_source 备源），stale 也落库；不在里面的（snapshot_fallback / snapshot_final /
# review_fallback，都是复用旧值）继续拒绝。开关关掉＝逐字节回到"stale 一律不落库"。
SENTIMENT_SNAPSHOT_SAVE_STALE_FRESH = True
SENTIMENT_SNAPSHOT_FRESH_SOURCES = ('intraday', 'alt_source')
SENTIMENT_ZT_POOL_PAGESIZE = 600      # 涨停池单页拉取上限（全市场涨停极少超过此数）
SENTIMENT_PREMIUM_MAX_CODES = 200     # 昨日涨停溢价最多查多少只（与 review 口径一致）
# 情绪周期 → 精选推荐最低分：改门槛而非改扣分，纪律不该被其他因子平均掉
SENTIMENT_MIN_SCORE = {'冰点': 72, '退潮': 68, '分歧': 65, '回暖': 60, '发酵': 60, '高潮': 64}
# 情绪周期 → 操作纪律。env_shift 是在"大盘涨跌幅档位"上的升降档；
# allow_chase=是否允许追高（涨幅已过 5% 的票）；allow_relay=是否允许接力高位连板。
# 2026-10-28：删除 veto_roles（角色硬否决）—— 角色体系下线，周期只按"追高/接力"两条纪律约束。
# 高潮期 allow_chase=False 是"卖在一致"：越是人人赚钱越不追第一，防接最后一棒。
CYCLE_RULES = {
    '冰点': {'env_shift': -2, 'allow_chase': False, 'allow_relay': False,
             'note': '情绪冰点：严禁接力，只做超跌低吸或空仓等待'},
    '退潮': {'env_shift': -1, 'allow_chase': False, 'allow_relay': False,
             'note': '亏钱效应扩散：规避高位股，有利润即刻走，不补仓不格局'},
    '分歧': {'env_shift': 0, 'allow_chase': False, 'allow_relay': False,
             'note': '多空拉锯：降低操作频率，等方向明确'},
    '回暖': {'env_shift': 0, 'allow_chase': True, 'allow_relay': False,
             'note': '情绪修复：积极低吸主流题材低位股'},
    '发酵': {'env_shift': 1, 'allow_chase': True, 'allow_relay': True,
             'note': '情绪主升：可跟随主线，注意仓位节奏'},
    '高潮': {'env_shift': 0, 'allow_chase': False, 'allow_relay': True,
             'note': '一致看多：不追第一，逐步兑现，谨防高位分歧'},
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


__all__ = ['CANDIDATE_POOL_ENABLED', 'CANDIDATE_POOL_MAX', 'CANDIDATE_POOL_MIN_BARS', 'CANDIDATE_POOL_NEAR_HIGH', 'CANDIDATE_POOL_RPS_TOP', 'CANDIDATE_POOL_TIME', 'CYCLE_RULES', 'HOT_STOCK_POOL', 'INDUSTRY_SYNC_TIME', 'INDUSTRY_SYNC_WEEKDAY', 'KLINE_POOL_MIN_COVERAGE', 'LIQUIDITY_FILTER_ENABLED', 'LIQ_AMOUNT_ENABLED', 'LIQ_MIN_AVG_AMOUNT', 'LIQ_MIN_AVG_TURNOVER_PCT', 'LIQ_MIN_PRICE', 'LIQ_PRICE_ENABLED', 'LIQ_STATS_KEEPWARM_FORCE', 'LIQ_STATS_KEEPWARM_MAX_AGE_SEC', 'LIQ_STATS_WINDOW', 'LIQ_TURNOVER_ENABLED', 'MARKET_CAP_FILTER_ENABLED', 'MARKET_CAP_REFRESH_TIME', 'MARKET_CAP_REFRESH_WEEKDAY', 'MARKET_CAP_REQUEST_DELAY', 'MARKET_CAP_WORKERS', 'MIN_TOTAL_MARKET_CAP', 'PUSH_MUTE_DATES', 'SENTIMENT_ALT_FALLBACK', 'SENTIMENT_ENABLED', 'SENTIMENT_LADDER_COLLAPSE_RATE', 'SENTIMENT_LADDER_MIN_BASE', 'SENTIMENT_LEADER_FAIL_CHANGE', 'SENTIMENT_MIN_SCORE', 'SENTIMENT_PREMIUM_MAX_CODES', 'SENTIMENT_REFRESH_TTL_SEC', 'SENTIMENT_SNAPSHOT_FALLBACK_MAX_AGE_SEC', 'SENTIMENT_SNAPSHOT_FRESH_SOURCES', 'SENTIMENT_SNAPSHOT_SAVE_SEC', 'SENTIMENT_SNAPSHOT_SAVE_STALE_FRESH', 'SENTIMENT_VETO_AFTER_MIN', 'SENTIMENT_VETO_MIN_WEAK_SIGNALS', 'SENTIMENT_ZT_POOL_PAGESIZE', 'SETTINGS_WRITE_USER_IDS', 'UNIVERSE_EXCLUDE_ST', 'UNIVERSE_MAIN_BOARD_ONLY', 'UNIVERSE_STEP_LOG_SEC']
