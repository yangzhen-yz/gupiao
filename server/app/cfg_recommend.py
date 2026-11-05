import json
import os
from datetime import datetime

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


# ========== 新③（2026-11-03，影子）：rev20+缩量 候选池影子臂 ==========
# 因子体检（85 截面）里唯一稳健为正的组合：rev20 分位≥0.7 且 vol_shrink 分位≥0.6
# （5 日超额 +0.12pp）。只落 strategy_snapshot 影子列、不改候选/分数/门槛；
# 攒 20 个交易日后按可成交净期望 + 最深回撤与修②同批裁决。
RECOMMEND_REV20_SHADOW_ENABLED = True
# 复盘汇总接口缓存（2026-11-03 第3步）：原来每次现算 0.32~0.81 秒，且同步函数直接调、
# 全程压在事件循环上。开=TTL 600 秒命中返缓存 + 未命中走 to_thread；关=逐字节等于改造前。
RECOMMEND_REVIEW_CACHE_ENABLED = True


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
# 复盘数据（218 条，`server/analysis_out/docs_backup_20261102/胜率低根因复盘-2026-10-18.md`）：推荐时当日涨幅
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
# 同日口径再纠偏（`server/analysis_out/docs_backup_20261102/胜率低根因复盘-2026-10-18.md`）：纸面"低吸参考价 60.4%"里
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


# ========== 智能推荐：买入时机判定（V4.6 2026-10-28 去角色，只剩四条） ==========
# 回答"这只票今天该什么时候买"：涨停封板→次日竞价；
# 高振幅/冲高回落/高位追高→尾盘确认；低位/中低位→回踩低吸；其余→盘中灵活。
# 原「强势角色+涨幅≥5% → 早盘买入」已随角色体系下线删除（且强势股次日追买实测为负），
# 复盘口径 review.py 仍保留自己的本地常量 _SBT_STRONG，与本文件无关。
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
# 2026-11-05 调整：原 10 天门槛导致 13 期全「样本不足」、闸门空转（realKeptDays 仅 4~7）。
# 降到 5 天让闸门能出结论，但同步在 verdict 里附 n 与 95% 置信区间（见 shadow_eval._verdict/summarize），
# 把"低样本"如实标注为「低(参考)」级，不让小样本被当成定论。两组事件数仍各自 ≥ MIN_EVENTS(20)。
SHADOW_EVAL_MIN_DAYS = 5             # 少于 5 个「有真放行名单」的交易日不下结论（10-14 上线，11-05 由 10 降至 5）


# ========== 模拟盘卖出建议修复（2026-11-05，用户"修复一下"）==========
# 症状：卖出建议只有 diagnosis 一个来源，遍历全部未平仓持仓本该每天重新评估，但去重键
# (symbol, side, source) 不带日期、且把 filled 也算进去 → 某只票历史上出过一次同来源卖单
# （哪怕那张单一直挂着、或当时卖掉后来又买回来），以后同一来源对它的卖出建议就永久不再产生。
# 另外 pending 卖单没有跨天作废机制，不执行就一直挂着并继续占键。两条各自独立开关，
# 关掉任意一条 = 那一条逐字节回到改造前。
SIM_SELL_DEDUPE_BY_DAY = True       # True=卖出侧只按「当日」去重（买入侧不动，仍防重复建仓）
SIM_SELL_PENDING_EXPIRE = True      # True=隔夜未执行的 pending 卖单自动置 rejected，让当天重新评估


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


# ========== 情绪减仓影子（2026-11-03，#11）==========
# 情绪进入坏周期（退潮/冰点）时，给每个持仓算一条"该减多少"落 exit_signal_log，
# **不执行、不改推送、不参与任何现有判定**——只攒对照数据，≥20 个交易日后再裁决要不要接进动作链。
# 按本仓惯例，"只记录类"影子默认开（同 RECOMMEND_REV20_SHADOW_ENABLED / 四维影子闸门）；
# 关掉 EXIT_SENTIMENT_SHADOW_ENABLED 时一个信号都不落，逐字节等于改造前。
EXIT_SENTIMENT_SHADOW_ENABLED = True
# 减仓比例是**先验值、未做参数寻优**（用户心法：情绪差先降敞口，但减多少没实测依据）。
# 冰点减一半、退潮减三成；A股卖出按手取整（floor 到 100 股），不足一手记 0 并写明原因。
EXIT_SENTIMENT_REDUCE_RATIO = {'冰点': 0.5, '退潮': 0.3}



# ========== 情绪收盘定版（2026-11-03）：#11 减仓回测的前置 ==========
# 现状：盘中快照每 300 秒落一行 market_sentiment_snapshots，但**当天最终档位从来没有落过库**——
# `final` 只是 payload 里的一个字段、盘中恒 False，收盘定版靠 _fallback_from_review(final=True)
# 在**读时**兜底。结果是 #11 想回测"情绪转弱那天该减多少"时，找不到一行可信的当日定版。
# 为什么单独一张表而不往 snapshots 加 final 列：那张表主键是 (sent_date, ts)，且
# _latest_live_snapshot 按 ts 取最新——塞定版行进去会被当成"最新盘中快照"读出来
# （'FINAL' 这类字符串按 ASCII 排序比时间戳大），等于直接改变现有页面行为。
# 独立表 = 盘中序列一行不动，本步只新增留痕、不改任何读取路径。
SENTIMENT_FINAL_SNAPSHOT_ENABLED = True
SENTIMENT_FINAL_SNAPSHOT_TIME = '15:40'   # 首版排 15:30，撞上已有的"15:30 趋势评分回测"，改到 15:40
                                          # （15:05/15:10 收评/15:12/15:20 回测/15:30/15:35/15:45/15:47
                                          # 都有任务，15:40 是唯一空档；重试仍留 16:05）



# ========== 持仓交易流水（2026-11-03）：卖出侧三条主线的回测地基 ==========
# 只追加、不参与任何决策读取，写入失败只打一行日志。关掉后一行都不写，逐字节等于改造前。
# 为什么现在必须有：#11 该减多少 / #12 抗跌阈值 / #13 结构确认，三条的判据全靠"卖出之后的结果"，
# 而 position_adjust.py 当初刻意不建流水表（note 是自由文本、还截断 200 字符），回放不出
# "哪天买、成本多少、持有多少天"。表设计与 source 五种取值的口径见 db/database.py。
POSITION_TRADE_LOG_ENABLED = True



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


# ========== 出口分叉影子（2026-11-05，影子台账第 15 条 exit_seal_split）==========
# 每天 20:35 把当天推荐的三条出口结果各算一遍落 `exit_seal_shadow`：①拿到次日开盘（现状默认）
# ②当日收盘就走 ③分叉"封住就拿隔夜、没封住当天收盘走"。样本内实测（201 条/91 交易日，成本=推荐当时的价，
# 双边 0.13%）净口径 ①+0.23% t=1.22 / ②−0.26% / ③+0.68% t=3.17，且③把"亏超 5%"从 9.0% 压到 4.5%。
# **不执行、不改出口文案、不改推送**——只攒前向对照，≥20 个交易日后按 `evaluate()` 的判据裁决。
# 关掉 EXIT_SEAL_SHADOW_ENABLED 时一个字段都不写，逐字节等于改造前。
EXIT_SEAL_SHADOW_ENABLED = True


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


__all__ = ['AUCTION_SNAPSHOT_BATCH', 'AUCTION_SNAPSHOT_ENABLED', 'AUCTION_SNAPSHOT_TIME', 'BUY_RANGE_BAND_BASE_PCT', 'BUY_RANGE_BAND_MAX_PCT', 'BUY_RANGE_DYNAMIC_ENABLED', 'BUY_RANGE_VOL_BARS', 'BUY_RANGE_VOL_CACHE_SEC', 'BUY_RANGE_VOL_K', 'BUY_TIMING_HIGH_CHASE_CHANGE_MIN', 'BUY_TIMING_TAIL_AMPLITUDE_MIN', 'BUY_TIMING_TAIL_DRAWDOWN_MIN', 'DEAD_CAT_BOUNCE_PENALTY', 'EVAL_LAB_ENABLED', 'EVAL_LAB_TIME', 'EVAL_LAB_WEEKDAY', 'EXIT_RULES_ENABLED', 'EXIT_RULES_LIVE_ENABLED', 'EXIT_RULES_LOGIC_MA_DAYS', 'EXIT_RULES_SHADOW_ENABLED', 'EXIT_RULES_TIME_STOP_DAYS', 'EXIT_RULES_TIME_STOP_MIN_PNL', 'EXIT_SEAL_SHADOW_ENABLED', 'EXIT_SENTIMENT_REDUCE_RATIO', 'EXIT_SENTIMENT_SHADOW_ENABLED', 'MARKET_ALERT_RISK_THRESHOLD', 'MINUTE5_FORWARD_ENABLED', 'MINUTE5_FORWARD_TIME', 'POSITION_TRADE_LOG_ENABLED', 'POSITION_WATCH_ENABLED', 'POSITION_WATCH_END', 'POSITION_WATCH_INTERVAL_SEC', 'POSITION_WATCH_REPUSH_DROP_PCT', 'POSITION_WATCH_START', 'POSITION_WATCH_USERS', 'PUSH_DAILY_LIMIT_PER_USER', 'PUSH_T1_PLAN_TIME', 'RECOMMEND_BOARD_BREAK_DRAWDOWN', 'RECOMMEND_EXEC_HINT', 'RECOMMEND_EXEC_HINT_ENABLED', 'RECOMMEND_INTRADAY_FILTER_ENABLED', 'RECOMMEND_LIMIT_UP_PCT', 'RECOMMEND_LOSER_CACHE_SEC', 'RECOMMEND_LOSER_CRITERION', 'RECOMMEND_LOSER_GRADED', 'RECOMMEND_LOSER_HEAVY_DAYS', 'RECOMMEND_LOSER_HEAVY_LOSS', 'RECOMMEND_LOSER_LIGHT_DAYS', 'RECOMMEND_LOSER_LOOKBACK_DAYS', 'RECOMMEND_LOSER_LOSS_THRESHOLD', 'RECOMMEND_MAX_DRAWDOWN_FROM_HIGH', 'RECOMMEND_REV20_SHADOW_ENABLED', 'RECOMMEND_REVIEW_CACHE_ENABLED', 'RECOMMEND_TREND_STALE_DRIFT', 'RECOMMEND_TREND_STALE_FACTOR', 'RECOMMEND_ZONE_CHASE_MAX', 'RECOMMEND_ZONE_CHASE_MIN', 'RECOMMEND_ZONE_CHASE_PENALTY', 'RECOMMEND_ZONE_GATE_ENABLED', 'RECOMMEND_ZONE_GATE_MODE', 'RECOMMEND_ZONE_WEAK_MAX', 'RECOMMEND_ZONE_WEAK_PENALTY', 'REC_SIM_BOARD_EPS', 'REC_SIM_BOARD_PRICE_V2', 'REC_SIM_DEFER_LIMIT_DOWN', 'REC_SIM_MAX_EXPOSURE_PCT', 'REC_SIM_MAX_POSITION_PCT', 'REC_SIM_RISK_PER_TRADE', 'REC_SIM_SIZING_V2', 'REC_SIM_STOP_PCT', 'SCHEDULE_MARKET_ALERT_TIMES', 'SCHEDULE_TREND_BACKTEST_TIME', 'SCHEDULE_TREND_SCAN_END', 'SCHEDULE_TREND_SCAN_INTERVAL_MIN', 'SCHEDULE_TREND_SCAN_START', 'SENTIMENT_FINAL_SNAPSHOT_ENABLED', 'SENTIMENT_FINAL_SNAPSHOT_TIME', 'SHADOW_EVAL_ENABLED', 'SHADOW_EVAL_MIN_DAYS', 'SHADOW_EVAL_TIME', 'SHAREHOLDER_UPDATE_TIME', 'SIM_SELL_DEDUPE_BY_DAY', 'SIM_SELL_PENDING_EXPIRE', 'TREND_BACKTEST_PERIODS', 'TREND_GATE_ENABLED', 'TREND_GATE_LOW_SCORE', 'TREND_GATE_MIN_CHANGE']
