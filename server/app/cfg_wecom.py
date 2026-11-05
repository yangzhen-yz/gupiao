import json
import os
from datetime import datetime

from .cfg_db import *
from .cfg_api import *
from .cfg_hot import *
from .cfg_fundflow import *
from .cfg_scan import *
from .cfg_trend import *
from .cfg_strategy import *
from .cfg_market import *
from .cfg_universe import *
from .cfg_schedule import *
from .cfg_recommend import *
from .cfg_backtest import *
from .cfg_review import *
from .cfg_auth import *

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



def push_muted_today() -> bool:
    """当天是否静音所有企业微信推送（2026-10-15 用户要求：今天别推了）。

    用日期列表而不是布尔总开关：布尔容易忘了关回来，第二天该止损时反而没提醒。
    列里的日期一过，推送自动恢复。
    """
    return datetime.now().strftime('%Y-%m-%d') in PUSH_MUTE_DATES



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
# 2026-10-28：删除 WECOM_PUSH_EXCLUDE_ROLES —— 角色体系下线，推送只按"评分+未涨停+预期涨幅+量比"筛。

# ---- 盘中连续扫描的股票范围（2026-10-16 用户要求："把推荐池也塞进每 60 秒的连续扫描里"）----
# 改造前这条链路只扫用户自选池（8~10 只），于是"9:45 那批推荐"生成后就再没人看了：
# 快照那一刻涨 1.1% 的票，10:05 涨到 7% 也不会再提醒 —— 用户看到的永远是过期的时机判断。
WECOM_PUSH_SCAN_RECOMMEND_POOL = True   # False 就退回"只扫用户池"的老行为
WECOM_PUSH_POOL_TOP_N = 60              # 海选池按 20 日强度取前 N 只并入（控制单轮耗时）
WECOM_PUSH_REC_TOP_N = 5                # 再把当日已生成推荐的前 N 只一起盯（它们可能还在涨）
WECOM_PUSH_DAILY_LIMIT = 6              # 达标推送每天最多几只票（与持仓快反的额度分开算）
WECOM_OPS_NOTIFY_DAILY_LIMIT = 2       # 运维通知类（服务重启/自愈/隧道拉起）每天最多推几条；页面用 ops_notify_daily_limit 可调（live 即时生效）
# 命中这些矩阵档位就不推（2026-10-16 分钟矩阵实测的明确负期望档）；
# "当天不追/不接力"只做标注不删，避免又变成一个没人知道的黑箱否决。
WECOM_PUSH_MATRIX_VETO = ('排除', '减仓日')
WECOM_PUSH_MATRIX_LABEL = True          # 推送文案里带上"这个位置历史什么胜率"
# 盘中 60 秒扫描是否也查亏损黑名单（2026-10-17 补的漏口：以前只有智能推荐查它，
# 被拉黑的票仍会从推送里回来）。关掉=恢复旧行为，仅用于对照。
WECOM_PUSH_APPLY_LOSER_BLACKLIST = True


# ========== 盘中龙头通道（2026-11-01，影子先行，默认关）==========
# 用户描述的形态：开盘快速拉升 → 短暂停顿换手 → 不破分时均线（破了快速收回）→
# 均线上方震荡 2~5 分钟 → 开盘 10 分钟内拉板。痛点：现有链路 09:45 才出推荐，
# 龙头那时已封板买不到。本通道独立于 13 因子日线评分，只做分钟级形态打分，
# 影子期只落库不推送；开关关掉时代码路径完全不进入（一个请求都不多发）。
INTRADAY_DRAGON_ENABLED = True           # 总开关。开=盘中每 15s 额外打腾讯快照+分时（2026-11-02 21:1x 开 P1 影子：窗口 09:31~10:15 之外不产生任何请求，推送仍双关）
INTRADAY_DRAGON_SHADOW = True            # True=只落 intraday_dragon_shadow，不推送
INTRADAY_DRAGON_PUSH_ENABLED = False     # 影子满 20 交易日后才允许打开
INTRADAY_DRAGON_START = '09:31'          # 专用扫描窗口（避开集合竞价）
INTRADAY_DRAGON_END = '10:15'
INTRADAY_DRAGON_INTERVAL_SEC = 15        # 专用轮询间隔（比主推送的 60s 更密）；
                                         # 主循环 10s 一拍，名义 15s 实际落到 20s
INTRADAY_DRAGON_PUSH_SCORE = 0            # 推送的分数下限。0=不设：2026-11-01 五日回放里
                                          # 分数≥70 的子集反而更差（B −1.94%），分数只用于排序与观察档
INTRADAY_DRAGON_WARN_SCORE = 65           # ≥此值预警（形态未成立但强度高）
INTRADAY_DRAGON_WATCH_SCORE = 52          # ≥此值观察
INTRADAY_DRAGON_MIN_ROOM_PCT = 0.8        # 距涨停价至少留 0.8% 才推（封板前必须还有空间）
INTRADAY_DRAGON_MAX_ROOM_PCT = 2.0        # 距涨停价超过 2% 不推：五日回放里 ≤2% 的 6 只触板 5 只
INTRADAY_DRAGON_COOLDOWN_SEC = 600        # 同一只票两次推送的最小间隔（10 分钟）
INTRADAY_DRAGON_MAX_PER_STOCK = 2        # 同一只票一天最多推几次（含分数升级）
INTRADAY_DRAGON_DAILY_LIMIT = 4          # 龙头通道一天最多推几只（与主推送额度分账）
INTRADAY_DRAGON_SCAN_MAX = 30            # 单轮最多对多少只票做分钟级评分（控请求量）。P1 首日降量：30 只≈3,990 次分时/44 分钟（≈91 次/分），120 只≈15,960 次；腾讯单 IP 频次未实测，先小口径，失败率 0 且单轮 <5s 才升 60/120
INTRADAY_DRAGON_MIN_VOL_RATIO = 1.2      # 快照预筛：量比下限
# 全市场预筛（默认关）：打开后 09:31~09:50 每 SWEEP_INTERVAL 秒对主板 3052 只做一次批量快照
# （41 批 qt，6 线程实测 3.1s），把"昨日没涨停、今天平开急拉"的票补进候选 ——
# 五日回放里早封龙只有 29.2% 落在盘前池内，不补这一层，通道再准也只能看见三成。
# 命中约 186 只（相对今开 2%~8.5% 且量比≥1.2），单轮分钟抓取会因此涨到 ~10s；
# 腾讯批量频次未验证，所以默认关，等用户确认请求预算后再开。
INTRADAY_DRAGON_SWEEP_ALL = False
INTRADAY_DRAGON_SWEEP_INTERVAL_SEC = 60
INTRADAY_DRAGON_MIN_OPEN_GAIN = 1.2      # 快照预筛：相对今开涨幅下限（%）

# B 臂：涨幅前 20 入口（清单 T2.1，2026-11-01，影子 / 默认关）
# 解决"候选池只有昨涨停/海选/推荐，早封龙头只有 29.2% 在池内"。
# 每 ROUND_SEC 秒对主板做一次批量快照，按相对今开涨幅取前 TOP_N，与 A 臂（结构优先）
# 并行交给同一套分钟评分；只多出候选行，不改 A 臂任何判定。
# 关掉时 scan_once 连这个模块都不导入 —— 零额外请求、零额外行。
INTRADAY_DRAGON_B_ARM = False            # B 臂总开关
INTRADAY_DRAGON_B_TOP_N = 20             # 涨幅榜取前 N
INTRADAY_DRAGON_B_ROUND_SEC = 300        # 候选重排间隔（5 分钟）；20s 轮询只做触发检查
INTRADAY_DRAGON_B_FIRST = '09:35'        # 第一轮粗筛（开盘 5 分钟走完）
INTRADAY_DRAGON_B_LAST = '10:15'         # 最后一轮
INTRADAY_DRAGON_B_GAIN_LO = 2.0          # 相对今开涨幅下限（%）
INTRADAY_DRAGON_B_GAIN_HI = 8.5          # 上限：再高是追高，不是"起步阶段"
INTRADAY_DRAGON_B_VOL_RATIO = 1.5        # 同刻量比下限（快照量比为准，缺则标 vol_src）
INTRADAY_DRAGON_B_AMOUNT_5M = 3000.0     # 最近 5 分钟成交额下限（万元）
INTRADAY_DRAGON_B_SPREAD_MAX = 0.3       # 买卖价差上限（%）
INTRADAY_DRAGON_B_NEW_DAYS = 60          # 上市不足 N 个交易日剔除（用本地日K首根近似）

# 特征层（清单 T2.2，2026-11-01）：每轮把候选的 11 个特征落 dragon_features。
# 零外部请求（复用已抓的分时与同一轮快照），只多本地写入；关掉只停写，不打分、不影响 A 臂。
# 缺失一律 NULL，不填中档；coverage<0.60 时 score_ready=0（T2.3 不给它打分）。
INTRADAY_DRAGON_FEATURE_LOG = True
INTRADAY_DRAGON_FEATURE_MIN_COVERAGE = 0.60

# 评分 v2 / 结构 / 流动性（清单 T2.3，2026-11-01，影子）
# 判定顺序固定 G3.1：硬排除 → 涨幅/room → 流动性 → 结构 → 评分 → 推送前复核 → 额度。
# 默认 legacy：一行都不算，A 臂逐票 diff=0；v2_shadow 只算分、只记录，不改变谁被推。
# v2_live 要等 20 交易日影子对照（≥70 与 ≥0 两套触发）后再谈，现在打开会直接报错。
INTRADAY_DRAGON_SCORE_MODE = 'v2_shadow' # legacy / v2_shadow（P1 影子：落影子表与 v2 分，不进推送判据）
INTRADAY_DRAGON_MIN_SCORE = 0            # v2 分数下限；0=不设。70 只做影子对照，不当线上线
INTRADAY_DRAGON_VWAP_SHARE_MIN = 0.70    # 结构：VWAP 上方分钟占比下限（开盘至今）
INTRADAY_DRAGON_VWAP_SHARE_FROM = '09:45'  # 结构：占比起点。09:45 前只记 pending，不推
INTRADAY_DRAGON_PAUSE_MIN = 2            # 结构：停顿换手分钟数下限（含）
INTRADAY_DRAGON_PAUSE_MAX = 5            # 结构：停顿换手分钟数上限（含）
INTRADAY_DRAGON_GAP_MAX = 7.0            # 高开上限（%）：>7 不硬拒，只扣 15 分并记录
INTRADAY_DRAGON_GAP_PENALTY = 15.0       # 同上扣分（清单 T2.5 第 1 条）
INTRADAY_DRAGON_LIQ_SHADOW_DAYS = 20     # 流动性影子分位数的窗口（交易日）
INTRADAY_DRAGON_LIQ_SAMPLE_EVERY_MIN = 5  # 取样本的分钟步长（5 分钟一次，避免全量几百万行）
INTRADAY_DRAGON_LIQ_MIN_SAMPLES = 200    # 影子样本少于此数，回退配置阈值并标 fallback
INTRADAY_DRAGON_LIQ_SPREAD_Q = 0.80      # 价差影子分位：只收紧不放松（min(0.30, q80)）
INTRADAY_DRAGON_LIQ_ASK_BID_Q = 0.80     # 卖一量/买一量影子分位上限
INTRADAY_DRAGON_LIQ_AMOUNT_Q = 0.20      # 5 分钟额影子分位：只收紧不放松（max(3000, q20)）
INTRADAY_DRAGON_ASK_BID_MAX = 2.0        # 影子样本不足时的回退上限（来源标 fallback_config）

# 推送机制（清单 T2.4，2026-11-01，默认 legacy / 零消息）
# 三条独立约束：窗口 / 复核 / 额度。逐条可关；legacy 时整个模块不导入、不落一行。
# v2_dry：算完整推送字段并落 dragon_push_log，但不发消息（测试群验证靠 DRY_SEND）；
# v2_live：真正发企业微信，并同时写 dragon_alert_log（与 legacy 同一张表，5 列）。
INTRADAY_DRAGON_PUSH_MODE = 'legacy'      # legacy / v2_dry / v2_live
INTRADAY_DRAGON_PUSH_WINDOW_START = '09:31'
INTRADAY_DRAGON_PUSH_WINDOW_END = '10:15'
INTRADAY_DRAGON_PUSH_QUOTE_MAX_AGE = 60   # 秒：报价时间超过它 → 改观察，不推
INTRADAY_DRAGON_PUSH_DEVIATION_MAX = 1.0  # %：现价偏离触发价超过它 → 改观察，不推
INTRADAY_DRAGON_PUSH_DRY_SEND = False     # dry 也真发（只用于测试群演练）
INTRADAY_DRAGON_ENTRY_ARMS = 'both'       # both / pullback / breakout：影子两臂对照，不混算
INTRADAY_DRAGON_ENTRY_LIVE_ARM = 'pullback'  # live 发哪一臂；两臂未对照完，默认回踩
INTRADAY_DRAGON_ENTRY_BAND_PCT = 0.3      # 回踩臂：VWAP ± 0.3%
INTRADAY_DRAGON_BREAKOUT_ADD_PCT = 0.2    # 突破臂：触发分钟价 ~ +0.2%
INTRADAY_DRAGON_STOP_MODE = 'fixed_2pct'  # fixed_2pct / vwap / day_low
INTRADAY_DRAGON_STOP_PCT = 2.0            # 止损：成交价 ×(1−2%)；无成交价用区间下限估计
INTRADAY_DRAGON_STOP_DAY_LOW_BUF = 0.2    # day_low 模式：当日分钟最低价再留 0.2%
INTRADAY_DRAGON_SELL_TIME = '10:30'       # 未封板的卖出时点
INTRADAY_DRAGON_POSITION_PCT = 5.0        # 提示用仓位上限（组合层另算；不改线上满仓等分）
INTRADAY_DRAGON_CAP_DAILY_ON = True       # 额度三条各自独立开关
INTRADAY_DRAGON_CAP_PER_STOCK_ON = True
INTRADAY_DRAGON_CAP_COOLDOWN_ON = True

# 风控与止损/仓位（清单 T2.5，2026-11-01，影子）
# 四条闸各自独立：关掉只退回 T2.3 结构门（同一阈值已经挡过一次），不是放行追高票。
# 仓位与频率默认关：前者等 P3 接组合层，后者等 T2.7 接调度器；现行满仓等分与 15s 轮询都不动。
INTRADAY_DRAGON_RISK_CHASE_ON = True      # 相对今开 > 8.5% 不推
INTRADAY_DRAGON_RISK_GAP_ON = True        # 高开 > 7% 扣 15 分（分数在 T2.3 里扣，这里只管开关与留痕）
INTRADAY_DRAGON_RISK_ROOM_ON = True       # room <0.8% 或 >2.0% 不推
INTRADAY_DRAGON_RISK_LIQ_ON = True        # 流动性 fail 不推；unknown 不挡但记录
INTRADAY_DRAGON_RISK_POSITION_ON = False  # 单笔 ≤5%、通道 ≤20%；P3 才接
INTRADAY_DRAGON_CHANNEL_CAP_PCT = 20.0    # 龙头通道占总资金上限（仅 position_on 时生效）
INTRADAY_DRAGON_RISK_STOP_ON = True       # −2% 止损只在回放/影子里建模，不假设止损价一定成交
INTRADAY_DRAGON_RATE_GOVERNOR_ON = True   # 频率状态机：T2.7 已接调度（dragon_risk.due）；P1 首日打开——失败率>5% 退 60s、连败 3 次熔断 300s，只会变疏不会变密
                                          # 默认关＝保持现行节奏，打开后 20s 基准（实际不变密）
INTRADAY_DRAGON_RATE_BASE_SEC = 20        # 基准轮询（10s 主循环上 15s 与 20s 都落到 20s，节奏不变密）
INTRADAY_DRAGON_RATE_SLOW_SEC = 60        # 失败率 >5% 退到 60s
INTRADAY_DRAGON_RATE_FAIL_PCT = 5.0       # 失败率阈值（%）
INTRADAY_DRAGON_RATE_FAIL_STREAK = 3      # 连续失败次数 → 熔断
INTRADAY_DRAGON_RATE_BREAK_SEC = 300      # 熔断时长（5 分钟）
INTRADAY_DRAGON_RATE_WINDOW = 20          # 算失败率的滑动窗口（轮）

# 影子表与开关矩阵（清单 T2.6 / T4.2，2026-11-01，影子）
# 七个条件各自独立：on=是否计算，mode=shadow 只记录 / live 才据它改判定。
# 默认必须等于今天的行为：量比/涨幅/VWAP 已在 T2.3 里生效（live），
# 回踩买/止损只建模（shadow），5% 仓位与板块前 3 未接（off / unavailable）。
INTRADAY_DRAGON_SHADOW_TABLE_ON = True    # 写 dragon_shadow；关掉=零写入（不影响主链路）
INTRADAY_DRAGON_SHADOW_KEEP_DAYS = 40     # 至少保留 40 个交易日；手工 prune 时才删更早的
INTRADAY_DRAGON_COND_SECTOR_ON = True     # 板块前 3：sector_rank 恒 NULL，暂时不可用
INTRADAY_DRAGON_COND_SECTOR_MODE = 'shadow'
INTRADAY_DRAGON_COND_VOL_ON = True        # 同刻量比 ≥ 1.5（B 臂入口；live）
INTRADAY_DRAGON_COND_VOL_MODE = 'live'
INTRADAY_DRAGON_COND_GAIN_5M_ON = True    # 相对今开 2.0%~8.5%（结构下界；live）
INTRADAY_DRAGON_COND_GAIN_5M_MODE = 'live'
INTRADAY_DRAGON_COND_VWAP70_ON = True     # VWAP 上方占比 ≥ 0.70（结构；live）
INTRADAY_DRAGON_COND_VWAP70_MODE = 'live'
INTRADAY_DRAGON_COND_PULLBACK_ON = True   # 回踩 VWAP 买（两臂之一；live_arm=pullback）
INTRADAY_DRAGON_COND_PULLBACK_MODE = 'live'

# WP4 的 12 个配置项（清单 T4.1，2026-11-01）：默认值全部等于改造前行为。
# 本次只新增 3 项（ML_ENABLED / PULLBACK_WAIT_SEC / PUSH_MIN_INTERVAL_SEC），
# 其余 9 项在 T2.x 落地时已经存在；清单名与代码名不同的以本文件为准：
#   TOP_GAIN_N        -> INTRADAY_DRAGON_B_TOP_N
#   MAX_OPEN_GAIN     -> INTRADAY_DRAGON_B_GAIN_HI
#   MIN_AMOUNT_5M     -> INTRADAY_DRAGON_B_AMOUNT_5M
#   MAX_SPREAD        -> INTRADAY_DRAGON_B_SPREAD_MAX
#   MAX_PUSH_PER_DAY  -> INTRADAY_DRAGON_DAILY_LIMIT
# 热读边界：这 12 项在导入时绑定进各模块命名空间，改这里**必须重启**才生效；
# 没有一项走 app_settings 实时覆盖（对照表见 services/pick/dragon_settings.py）。
INTRADAY_DRAGON_ML_ENABLED = False        # 无 sklearn/lightgbm、T1.5 未做；改为 True 会被校验拒绝
INTRADAY_DRAGON_PULLBACK_WAIT_SEC = 180   # 回踩限价最多等 3 分钟；与 dragon_backtest.PULLBACK_WAIT_SEC 同值
INTRADAY_DRAGON_PUSH_MIN_INTERVAL_SEC = 20  # 两条推送之间的最小间隔（秒）；P2 纸面时才接，当前未被读取

TREND_BACKTEST_KLINE_CONCURRENCY = 5                # 批量拉 K 线并发度
TREND_BACKTEST_MARKET_SYMBOL = 'sh000001'           # 大盘基准（上证指数）
TREND_BACKTEST_SCORE_SEGMENTS = [(60, 70), (70, 80), (80, 101)]  # 分数段（左闭右开，80+ 用 101 兜顶）


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


WECOM_AGENT_CORP_ID = _wecom_agent_cred('WECOM_AGENT_CORP_ID')
WECOM_AGENT_ID = _wecom_agent_cred('WECOM_AGENT_ID')
WECOM_AGENT_SECRET = _wecom_agent_cred('WECOM_AGENT_SECRET')
# OAuth 回调用的可信域名（如 https://your-domain.com）；留空 = 不启用扫码/链接绑定，只支持手填 userid
WECOM_AGENT_REDIRECT_BASE = _wecom_agent_cred('WECOM_AGENT_REDIRECT_BASE')



__all__ = ['INTRADAY_DRAGON_ASK_BID_MAX', 'INTRADAY_DRAGON_BREAKOUT_ADD_PCT', 'INTRADAY_DRAGON_B_AMOUNT_5M', 'INTRADAY_DRAGON_B_ARM', 'INTRADAY_DRAGON_B_FIRST', 'INTRADAY_DRAGON_B_GAIN_HI', 'INTRADAY_DRAGON_B_GAIN_LO', 'INTRADAY_DRAGON_B_LAST', 'INTRADAY_DRAGON_B_NEW_DAYS', 'INTRADAY_DRAGON_B_ROUND_SEC', 'INTRADAY_DRAGON_B_SPREAD_MAX', 'INTRADAY_DRAGON_B_TOP_N', 'INTRADAY_DRAGON_B_VOL_RATIO', 'INTRADAY_DRAGON_CAP_COOLDOWN_ON', 'INTRADAY_DRAGON_CAP_DAILY_ON', 'INTRADAY_DRAGON_CAP_PER_STOCK_ON', 'INTRADAY_DRAGON_CHANNEL_CAP_PCT', 'INTRADAY_DRAGON_COND_GAIN_5M_MODE', 'INTRADAY_DRAGON_COND_GAIN_5M_ON', 'INTRADAY_DRAGON_COND_PULLBACK_MODE', 'INTRADAY_DRAGON_COND_PULLBACK_ON', 'INTRADAY_DRAGON_COND_SECTOR_MODE', 'INTRADAY_DRAGON_COND_SECTOR_ON', 'INTRADAY_DRAGON_COND_VOL_MODE', 'INTRADAY_DRAGON_COND_VOL_ON', 'INTRADAY_DRAGON_COND_VWAP70_MODE', 'INTRADAY_DRAGON_COND_VWAP70_ON', 'INTRADAY_DRAGON_COOLDOWN_SEC', 'INTRADAY_DRAGON_DAILY_LIMIT', 'INTRADAY_DRAGON_ENABLED', 'INTRADAY_DRAGON_END', 'INTRADAY_DRAGON_ENTRY_ARMS', 'INTRADAY_DRAGON_ENTRY_BAND_PCT', 'INTRADAY_DRAGON_ENTRY_LIVE_ARM', 'INTRADAY_DRAGON_FEATURE_LOG', 'INTRADAY_DRAGON_FEATURE_MIN_COVERAGE', 'INTRADAY_DRAGON_GAP_MAX', 'INTRADAY_DRAGON_GAP_PENALTY', 'INTRADAY_DRAGON_INTERVAL_SEC', 'INTRADAY_DRAGON_LIQ_AMOUNT_Q', 'INTRADAY_DRAGON_LIQ_ASK_BID_Q', 'INTRADAY_DRAGON_LIQ_MIN_SAMPLES', 'INTRADAY_DRAGON_LIQ_SAMPLE_EVERY_MIN', 'INTRADAY_DRAGON_LIQ_SHADOW_DAYS', 'INTRADAY_DRAGON_LIQ_SPREAD_Q', 'INTRADAY_DRAGON_MAX_PER_STOCK', 'INTRADAY_DRAGON_MAX_ROOM_PCT', 'INTRADAY_DRAGON_MIN_OPEN_GAIN', 'INTRADAY_DRAGON_MIN_ROOM_PCT', 'INTRADAY_DRAGON_MIN_SCORE', 'INTRADAY_DRAGON_MIN_VOL_RATIO', 'INTRADAY_DRAGON_ML_ENABLED', 'INTRADAY_DRAGON_PAUSE_MAX', 'INTRADAY_DRAGON_PAUSE_MIN', 'INTRADAY_DRAGON_POSITION_PCT', 'INTRADAY_DRAGON_PULLBACK_WAIT_SEC', 'INTRADAY_DRAGON_PUSH_DEVIATION_MAX', 'INTRADAY_DRAGON_PUSH_DRY_SEND', 'INTRADAY_DRAGON_PUSH_ENABLED', 'INTRADAY_DRAGON_PUSH_MIN_INTERVAL_SEC', 'INTRADAY_DRAGON_PUSH_MODE', 'INTRADAY_DRAGON_PUSH_QUOTE_MAX_AGE', 'INTRADAY_DRAGON_PUSH_SCORE', 'INTRADAY_DRAGON_PUSH_WINDOW_END', 'INTRADAY_DRAGON_PUSH_WINDOW_START', 'INTRADAY_DRAGON_RATE_BASE_SEC', 'INTRADAY_DRAGON_RATE_BREAK_SEC', 'INTRADAY_DRAGON_RATE_FAIL_PCT', 'INTRADAY_DRAGON_RATE_FAIL_STREAK', 'INTRADAY_DRAGON_RATE_GOVERNOR_ON', 'INTRADAY_DRAGON_RATE_SLOW_SEC', 'INTRADAY_DRAGON_RATE_WINDOW', 'INTRADAY_DRAGON_RISK_CHASE_ON', 'INTRADAY_DRAGON_RISK_GAP_ON', 'INTRADAY_DRAGON_RISK_LIQ_ON', 'INTRADAY_DRAGON_RISK_POSITION_ON', 'INTRADAY_DRAGON_RISK_ROOM_ON', 'INTRADAY_DRAGON_RISK_STOP_ON', 'INTRADAY_DRAGON_SCAN_MAX', 'INTRADAY_DRAGON_SCORE_MODE', 'INTRADAY_DRAGON_SELL_TIME', 'INTRADAY_DRAGON_SHADOW', 'INTRADAY_DRAGON_SHADOW_KEEP_DAYS', 'INTRADAY_DRAGON_SHADOW_TABLE_ON', 'INTRADAY_DRAGON_START', 'INTRADAY_DRAGON_STOP_DAY_LOW_BUF', 'INTRADAY_DRAGON_STOP_MODE', 'INTRADAY_DRAGON_STOP_PCT', 'INTRADAY_DRAGON_SWEEP_ALL', 'INTRADAY_DRAGON_SWEEP_INTERVAL_SEC', 'INTRADAY_DRAGON_VWAP_SHARE_FROM', 'INTRADAY_DRAGON_VWAP_SHARE_MIN', 'INTRADAY_DRAGON_WARN_SCORE', 'INTRADAY_DRAGON_WATCH_SCORE', 'TREND_BACKTEST_KLINE_CONCURRENCY', 'TREND_BACKTEST_MARKET_SYMBOL', 'TREND_BACKTEST_SCORE_SEGMENTS', 'WECOM_AGENT_CORP_ID', 'WECOM_AGENT_ID', 'WECOM_AGENT_REDIRECT_BASE', 'WECOM_AGENT_SECRET', 'WECOM_AIBOT_ENABLED', 'WECOM_CLI_BIN', 'WECOM_CLI_CHAT_ID', 'WECOM_OPS_NOTIFY_DAILY_LIMIT', 'WECOM_PUSH_APPLY_LOSER_BLACKLIST', 'WECOM_PUSH_DAILY_LIMIT', 'WECOM_PUSH_ENABLED', 'WECOM_PUSH_END', 'WECOM_PUSH_INTERVAL_SEC', 'WECOM_PUSH_MATRIX_LABEL', 'WECOM_PUSH_MATRIX_VETO', 'WECOM_PUSH_MAX_CHANGE', 'WECOM_PUSH_MIN_GAIN', 'WECOM_PUSH_MIN_SCORE', 'WECOM_PUSH_MIN_VOL_RATIO', 'WECOM_PUSH_POOL_TOP_N', 'WECOM_PUSH_PREVIEW_TIME', 'WECOM_PUSH_REC_TOP_N', 'WECOM_PUSH_SCAN_RECOMMEND_POOL', 'WECOM_PUSH_START', 'WECOM_WEBHOOK_ENABLED', 'WECOM_WEBHOOK_FILE', 'WECOM_WEBHOOK_MSGTYPE', 'WECOM_WEBHOOK_URL', '_WECOM_AGENT_KEYS', '_wecom_agent_cred', 'push_muted_today', 'wecom_agent_missing', 'wecom_agent_ready']
