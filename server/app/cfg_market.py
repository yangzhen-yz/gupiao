import json
import os
from datetime import datetime

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


# ========== 大盘指数日线本地化（2026-11-03，#12 抗跌因子的前置）==========
# 本地原本一个指数都没有：history_kline.db.stock_daily 的 symbol 全是 6 位个股纯代码，
# 上证指数只能靠主库 kline_cache 里 200 根、每次现拉外部源的 JSON。没有本地指数日线，
# "相对大盘回撤"就只能每次请求走网络——和资金流那个 9 秒是同一类毛病。
# 源：腾讯 web.ifzq.gtimg.cn/appstock/app/fqkline/get（2026-11-03 实测可用，一次可取 700 根，
# 所以首日就能回填全历史，不必像板块表那样干等攒数）。
INDEX_DAILY_ENABLED = True
INDEX_DAILY_BARS = 700              # 每次取多少根（700 ≈ 本地个股日K的 685 个交易日，对齐便于联算）
INDEX_DAILY_SYNC_TIME = '15:15'     # 收盘后同步（收评 15:10 之后、回测 15:20 之前，仅交易日）



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

# 除权重算队列的消费时刻（2026-11-01）：`kline_restate_queue` 原来只有写入方（收盘快照
# 检测到除权就写一行），没有任何消费方 —— baostock 被封之后那 126 只票就一直挂在队里。
# 排在 21:30：晚于 18:15~20:15 的五轮日K同步，且夜间腾讯 fqkline 限流压力最小。
# 每晚只处理这么多只，剩下的留给下一晚（单线程节流，不追一次性清空）。
KLINE_RESTATE_TIME = '21:30'
KLINE_RESTATE_LIMIT = 60

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


__all__ = ['BAOSTOCK_BAN_COOLDOWN_MIN', 'DELISTING_KEYWORDS', 'DESK_SECTOR_CHIPS_IN_SENTIMENT', 'FORBIDDEN_KEYWORDS', 'INDEX_DAILY_BARS', 'INDEX_DAILY_ENABLED', 'INDEX_DAILY_SYNC_TIME', 'INDEX_SYMBOLS', 'KLINE_DATA_LIMIT', 'KLINE_DISPLAY_MA_POINTS', 'KLINE_HISTORY_DB_PATH', 'KLINE_HISTORY_ENABLED', 'KLINE_HISTORY_START_DATE', 'KLINE_HISTORY_SYNC_LAST', 'KLINE_HISTORY_SYNC_TIME', 'KLINE_HISTORY_SYNC_TIMES', 'KLINE_MAX_CONSECUTIVE_FAILS', 'KLINE_RECONNECT_EVERY', 'KLINE_RESTATE_LIMIT', 'KLINE_RESTATE_TIME', 'KLINE_START_DATE', 'KLINE_SYNC_UNIVERSE_ONLY', 'MAINLINE_ENABLED', 'MAINLINE_FLOW_STALE_SEC', 'MAINLINE_GAP', 'MAINLINE_JUDGE_AFTER_MIN', 'MAINLINE_MIN_SCORE', 'MAINLINE_REFRESH_TTL_SEC', 'MAINLINE_SNAPSHOT_SAVE_SEC', 'MAINLINE_SUB_MIN_SCORE', 'MAINLINE_WEIGHTS', 'MARKET_CRASH_THRESHOLD', 'MARKET_SEVERE_CRASH_THRESHOLD', 'MATRIX_RERUN_ENABLED', 'MATRIX_RERUN_TIME', 'SECTOR_MIN_MEMBERS', 'SECTOR_NAME_EXCLUDE_ENABLED', 'TENCENT_SNAPSHOT_BATCH_SIZE', 'TENCENT_SNAPSHOT_ENABLED', 'TENCENT_SNAPSHOT_MAX_DIFF']
