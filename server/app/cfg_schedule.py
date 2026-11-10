import json
import os
from datetime import datetime

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


# ========== P0-2 周期 → 模式调度（2026-10-24 按实施计划 §4 实现，默认关）==========
# 计划原文"11-02 起，前提：影子结论支持"。机制已按计划实现到 recommend 主链路，
# 但截至 2026-10-24 影子仅 7 个交易日、单组事件 6~16（闸门要求 ≥10 日 + 单组 ≥20），
# 故 RECOMMEND_CYCLE_SCHEDULE_ENABLED 默认 False（False 时主链路行为与改造前一致）。
# 影子达标后改 True 即上线；回滚也只需改回 False。
RECOMMEND_CYCLE_SCHEDULE_ENABLED = False
RECOMMEND_MIN_SCORE_BASE = 60           # 原 recommend.py 主链路硬编码 MIN_RECOMMEND_SCORE=60 上移
CYCLE_MAX_RECOMMEND = {'冰点': 1, '退潮': 1, '分歧': 2, '回暖': 3, '发酵': 3, '高潮': 2}
CYCLE_ALLOW_EMPTY = ('退潮', '高潮')     # 仅这两个周期允许 0 只推荐（空仓哨兵）
CYCLE_EXTREME_HOT_LIMIT_UPS = 120       # 涨停 > 120 家 = 极热档：只留打板形态、至多 1 只
# 极热档"仅限主线"的判定来源：读最新主线快照的板块名，与股票概念标签/名称做子串匹配；
# 快照缺失或过期(>MAINLINE_SNAPSHOT_STALE_SEC)视为无主线 → 空仓哨兵（与计划"无主线时空仓哨兵"一致）
CYCLE_EXTREME_HOT_MAINLINE_STALE_SEC = 7200
# 大盘否决位（2026-10-24，策略分析新增，与周期调度同框架）：severe crash 日不出新买入推荐，
# 直接写空仓哨兵。此前 check_market_crash 只是前端展示、对推荐无否决权。
RECOMMEND_MARKET_VETO_ENABLED = False



# ========== 调度链失活告警（2026-11-07，把"观测兜底"那格补上）==========
# 起因：11-06 调度链被冻 03:30~15:52，五张决策表全天 0 行，但**一条通知都没有**——
# 发现方式是用户下午问"大盘这么好为什么没推"，再翻日志再数行数。放大器（逐 job 隔离）已消，
# 按"消除放大器 → 自愈 → 告警"的顺序，这一步只剩告警，而且只留"用户不会主动发现的真损失"。
# 判据两条，设计原因写在 services/ops/sched_liveness.py 文件头：
#   A 心跳陈旧 = 主循环不转了（观察者独立线程，不能用被观察者自己判自己的死）；
#   B 决策链空档 = 循环还在转但决策不产出——11-06 就是这种，心跳完全不陈旧。
# B 的必要条件用"当天情绪快照有行"，是拿历史校出来的：2026-09-21/22/23、10-08 这四天
# 官方交易日也是推荐 0 行，但那是**功能还没上线**（六张表全 0），不是故障；
# 按"推荐 0 行就告警"写会每天发两条假警，第一次响就把人训练成不看告警。
SCHED_LIVENESS_ENABLED = True             # 关掉=不起线程、不查库、不发通知，逐字节等于没这项
SCHED_HEARTBEAT_STALE_MIN = 15            # 距最后一次调度心跳超过这么多分钟 = A 类告警
SCHED_LIVENESS_START_GRACE_MIN = 12       # 刚起来的宽限期，避免每次重启都先报一条假警
SCHED_LIVENESS_COOLDOWN_MIN = 40          # 同一状态多久内只报一次（A 类）
SCHED_LIVENESS_WINDOW = ('09:20', '18:40')  # 只在"本该有产出"的时间窗内判，深夜不报警
SCHED_CHAIN_CHECK_TIMES = ('10:00', '14:10')  # B 类两个时点（09:45 首批之后、午后首批之后）


# ========== 后端 HTTP 自愈探活（2026-11-09）==========
# 起因：11-07 事件循环被卡死 14 分钟、11-09 一次 WinError 64 关掉监听 socket，两次都是
# **进程活着、调度照跑、HTTP 不应答**。看护探的是 TCP 端口，端口在听（11-07）或整个没在听
# （11-09，看护的 port_down 分支自己会拉起）它都可能看不见/用不上，这里补的是"连得上但
# 不应答"那一格：独立线程每 30s 探一次 /api/health，连续 3 次失败写
# server/data/backend_restart_requested 交给 SYSTEM 看护重启。
# 设计边界（refused 不计数、恢复即撤销、盘中推迟）写在 services/ops/http_liveness.py 文件头。
HTTP_LIVENESS_ENABLED = True          # 关掉=不起线程、连探活都不做，逐字节等于没这项
HTTP_LIVENESS_HOST = '127.0.0.1'      # 只探本机，不走隧道/域名（外部链路故障不归它管）
HTTP_LIVENESS_PORT = 3003             # 必须与 serve_3003.py / run.py 一致
HTTP_LIVENESS_PATH = '/api/health'
HTTP_LIVENESS_INTERVAL_SEC = 30       # 探活间隔（改了要重启后端）
HTTP_LIVENESS_TIMEOUT_SEC = 5.0       # 单次请求超时：健康接口不查库，超 5 秒说明循环被占死
HTTP_LIVENESS_FAIL_STREAK = 3         # 连续失败多少次才写标志（30s×3 ≈ 90 秒不应答）
HTTP_LIVENESS_START_GRACE_SEC = 180   # 启动宽限期：刚起来的前 3 分钟不判死
HTTP_LIVENESS_HEAL_COOLDOWN_MIN = 30  # 写完标志后这么多分钟内不重复写（防重启风暴）


__all__ = ['CYCLE_ALLOW_EMPTY', 'CYCLE_EXTREME_HOT_LIMIT_UPS', 'CYCLE_EXTREME_HOT_MAINLINE_STALE_SEC', 'CYCLE_MAX_RECOMMEND', 'HTTP_LIVENESS_ENABLED', 'HTTP_LIVENESS_FAIL_STREAK', 'HTTP_LIVENESS_HEAL_COOLDOWN_MIN', 'HTTP_LIVENESS_HOST', 'HTTP_LIVENESS_INTERVAL_SEC', 'HTTP_LIVENESS_PATH', 'HTTP_LIVENESS_PORT', 'HTTP_LIVENESS_START_GRACE_SEC', 'HTTP_LIVENESS_TIMEOUT_SEC', 'POOL_CONSECUTIVE_DOWN_THRESHOLD', 'RECOMMEND_CYCLE_SCHEDULE_ENABLED', 'RECOMMEND_MARKET_VETO_ENABLED', 'RECOMMEND_MIN_SCORE_BASE', 'RECOMMEND_REVIEW_TIME', 'SCHEDULE_BACKTEST_TIME', 'SCHEDULE_DAILY_REVIEW_TIME', 'SCHEDULE_MA10_CHECK_TIME', 'SCHEDULE_PRE_CLOSE_SNAPSHOT_TIME', 'SCHEDULE_RECOMMENDATION_TIMES', 'SCHEDULE_UPDATE_HOT_SEARCH_TIMES', 'SCHED_CHAIN_CHECK_TIMES', 'SCHED_HEARTBEAT_STALE_MIN', 'SCHED_LIVENESS_COOLDOWN_MIN', 'SCHED_LIVENESS_ENABLED', 'SCHED_LIVENESS_START_GRACE_MIN', 'SCHED_LIVENESS_WINDOW']
