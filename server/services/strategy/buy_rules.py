"""buy_rules.py - 把 2026-10-16 实测出的"形态 × 时点"矩阵变成可复用的纯函数

来源（全部是本地日K + 5 分钟前复权分钟线跑出来的历史频率，不是观点）：
  · `crash_profile.py`   形态 → 尾部风险（5 日内大面率、冲高回落率）
  · `buy_rule_stats.py`  买法逐条称重（含连板梯队、竞价溢价分档、按年稳健性）
  · `timing_matrix.py`   同一形态按 竞价/尾盘/追最高/挂单低吸 四种成本算收益
  · `intraday_matrix.py` 同一形态按六个真实分钟时点（9:25/9:30~9:35/9:35~10:00/
                          10:00~11:30/13:00~14:00/14:30~15:00）算收益
范围：沪深主板、剔除 ST/*ST（`services/universe.py`），样本 2025-01~2026-10。

现在只用于**影子记录**（`recommend._log_shadow_selection` 里给每只票多打一个
`matrixTiming` 标签），线上 `calc_buy_timing` 的文案暂时不动 —— 两周后拿两套标签
各自对照 T+1 实际结果，再决定换不换。这样即使矩阵判断错了，也不会直接改变推给你的东西。

2026-10-18 校准：选股范围从「主板非ST 3051 只」换成系统实际口径 1094 只
（市值≥35亿 + 流动性三关），样本 458,660 条（2025-01-02~2026-10-15，
`server/analysis_out/*_2462.txt`）。**判定阈值一个没改**，只把文案里的数字
换成新口径实测——旧版有两处结论被数据推翻（接力「六时点全负」不成立、极热
「一刀切禁打板」不成立），详见 `docs/短线买法-2026-10.md`（1094 重跑版）。
"""
from __future__ import annotations

from typing import Dict, Optional

# 阈值都带样本量注释，改的时候必须同时改注释，否则下次没人知道这个数字从哪来
# 2026-10-16 首版；2026-10-18 按 1094 口径重跑校准文案数字（阈值未动）
MATRIX_VERSION = '2026-10-18-1094'

# 1094 口径实测（样本数见 docs/短线买法-2026-10.md 重跑版，458,660 条）
LIMIT_CLOSE_MIN = 0.0985      # 主板收盘封板
HILL_LOW = 0.045              # 半山腰下沿：涨 4.5% 起（样本 22,573，5 日内大面 34.2%）
HILL_HIGH = 0.098             # 半山腰上沿：没封板的 9.8%
OVERHEAT_R20 = 0.60           # 20 日涨幅 >60%：5 日内大面 69.9%、5 日最低中位 -9.66%
BIG_VOL = 3.0                 # 量比 >3 且涨 >5%：追最高胜率 22.6%（样本 3,845）
TIANLIANG = 5.0               # 量比 >5 天量：T+1 胜率 42.8%、次日低开 2%+ 概率 32.1%
FLAT_CHG = 0.02               # 横盘：|涨幅| < 2%
FLAT_VOL = (0.7, 1.5)         # 缩量：量比 0.7~1.5
BIG_DROP = -0.05              # 昨日大面
HOT_MARKET_LIMIT_UPS = 120    # 全市场涨停 >120 家 = 极热：全体买入 T+1 胜率 42.6%、均值 -0.28%


def classify(stock: Dict, prev: Optional[Dict] = None, market: Optional[Dict] = None) -> Dict:
    """给一只票打上"矩阵怎么看这个买点"的标签。

    Args:
        stock: 当天已知的行情（changePercent / volumeRatio / price 等，推荐链路那套字段）
        prev:  昨日行情（至少要有 changePercent、limit 标记）；没有就把相关规则跳过
        market: 当日市场层（limitUp 家数、cycle），用于极热日闸门
    Returns:
        {'verdict','action','why','risk','sample','version'} —— verdict 是短结论，
        action 是"那到底该怎么做"，why 带历史频率，risk 是尾部风险。
    """
    chg = float(stock.get('changePercent') or stock.get('change_percent') or 0) / 100.0
    vr = float(stock.get('volumeRatio') or stock.get('volume_ratio') or 0)
    r20 = float(stock.get('gain20') or stock.get('r20') or 0)
    if abs(r20) > 1:                     # 有的链路给的是百分数
        r20 = r20 / 100.0
    prev_chg = float((prev or {}).get('changePercent') or 0) / 100.0
    prev_limit = bool((prev or {}).get('limitClose') or (prev or {}).get('limit_close'))
    limit_ups = float((market or {}).get('limitUp') or (market or {}).get('limit_up') or 0)

    out = {'version': MATRIX_VERSION, 'verdict': '中性', 'action': '按原计划',
           'why': '', 'risk': '', 'sample': ''}

    # 1) 一票否决类：位置本身就把期望打负
    if r20 > OVERHEAT_R20 and chg < LIMIT_CLOSE_MIN:
        out.update(verdict='排除', action='这种票今天别买（也不接回调）',
                   why=f'20 日已涨 {r20*100:.0f}%，属严重过热档',
                   risk='5 日内出现 -5% 大面的概率 69.9%，5 日最低中位 -9.66%',
                   sample='1094口径 5,231 条')
        return out
    if vr > TIANLIANG:
        out.update(verdict='排除', action='天量票不接，等它缩量',
                   why=f'量比 {vr:.1f} 已是天量档',
                   risk='T+1 胜率 42.8%、均值 -0.01%，次日低开 2%+ 概率 32.1%',
                   sample='1094口径 1,419 条')
        return out
    if HILL_LOW <= chg < HILL_HIGH and vr > BIG_VOL:
        out.update(verdict='排除', action='巨量拉升当天不追',
                   why=f'涨 {chg*100:.1f}% 且量比 {vr:.1f}',
                   risk='追到当日最高：T+1 胜率 22.6%、均值 -2.93%；尾盘买也只有 45.5%',
                   sample='1094口径 3,845 条')
        return out

    # 2) 半山腰：不是不能碰，是**不能当天追**
    if HILL_LOW <= chg < HILL_HIGH:
        out.update(verdict='当天不追', action='要做只能等明天竞价低开再买',
                   why=f'涨 {chg*100:.1f}% 却没封住板 = 半山腰',
                   risk='当天追高 T+1 胜率 23.9%、均值 -2.80%（亏超5% 28.5%）；'
                       '同一形态改成"次日竞价买"是 +0.48%/胜率 49.8%',
                   sample='1094口径 22,704 条')
        return out

    # 3) 接力票：昨天封板、今天买 —— 开盘卖全负，但收盘卖翻正（10-18 重跑推翻旧结论）
    if prev_limit:
        out.update(verdict='不接力', action='昨天的板不追今天的开盘',
                   why='昨日封涨停',
                   risk='次日开盘卖六个时点全负（-0.20% ~ -0.50%）；持有到次日收盘虽翻正'
                       '（+0.72% ~ +0.85%）但要多押一天、亏超 5% 概率 15~25% —— '
                       '肉在前一天封板那一刻，竞价买亏超 5% 概率 22.5%',
                   sample='1094口径 13,717 条 / 分钟抽样 399 事件')
        return out

    # 4) 今天封板：唯一胜率与期望都站得住的动作，但必须次日开盘走
    if chg >= LIMIT_CLOSE_MIN:
        out.update(verdict='可打板', action='尾盘确认还封死才买，次日 9:25 竞价直接卖',
                   why='当日封住涨停',
                   risk='多留一天就漏钱：次日开盘卖 +2.05%/胜率 64.6%（亏超5% 仅 2.4%），'
                        '持有到次日收盘只 +1.97%/59.3%（亏超5% 涨到 11.4%），'
                        '持有到第 3 日 +2.60%/56.3%（亏超5% 17.7%）；'
                        '一字板买不到，缩量秒板按买不到处理',
                   sample='1094口径封板票 13,612 条（叠条件+费后 +1.62%/62.5%）')
        return out

    # 5) 昨日大面：越早买越赚，但尾部仍大
    if prev_chg <= BIG_DROP:
        out.update(verdict='可竞价低吸', action='要抄就 9:25~9:35 买，别挂单等更低',
                   why=f'昨日 {prev_chg*100:.1f}% 属大面档',
                   risk='竞价买 +0.42%/50.9%（亏超5% 16.9%）→ 尾盘买 +0.10%/51.0%（亏超5% 11.7%）；'
                       '挂 -2% 等它再跌反而 -0.32%（成交率 65%）：能跌到你价位的都在继续跌',
                   sample='1094口径 21,892 条 / 分钟抽样 400 事件')
        return out

    # 6) 横盘缩量：六时点全正、尾部最小，是"输小"的那一类
    if abs(chg) < FLAT_CHG and FLAT_VOL[0] <= vr <= FLAT_VOL[1]:
        out.update(verdict='可低吸', action='挂昨收 -2% 的单等错杀，什么时候买都行',
                   why='全天安静 + 缩量',
                   risk='六个时点全正（当日收盘卖 +0.08% ~ +0.24%），亏超 5% 只有 1.2~2.2%；'
                       '挂 -2% 低吸历史 +1.32%/胜率 66.3%（成交率 22%），'
                       '挂 -4% +3.42%/80.2% 但成交率只有 2%',
                   sample='1094口径 78,074 条 / 分钟抽样 400 事件')
        return out

    # 7) 极热日闸门：形态没问题，但今天整个市场不适合动手
    if limit_ups > HOT_MARKET_LIMIT_UPS:
        out.update(verdict='减仓日', action='今天不开新仓，尤其不做接力',
                   why=f'全市场涨停 {limit_ups:.0f} 家 = 极热档',
                   risk='极热日全体买入 T+1 胜率 42.6%、均值 -0.28%（冰点日反而 52.0%）；'
                       '但封板打板在极热仍 67.1%/+2.06%（957 样本）——禁的是低吸追高，不是打板',
                   sample='1094口径 14,080 条')
        return out

    out.update(why='未命中矩阵里的任何一档', risk='按现有评分与仓位纪律执行',
               action='按原计划')
    return out


# ---------- 模式标签（2026-10-18，实施计划 P2-2）----------
# 为什么放在这里而不是 recommend.py：verdict 的语义归本模块管，
# 映射表写在旁边才不会哪天改了 verdict 名字、标签却还在按旧字符串匹配（静默打错标签）。
# 「按模式复盘」的全部前提就是这个标签，标签错 → 榜就歪，所以只允许机械映射、不给人工判断。
MODE_BY_VERDICT = {
    '可低吸': 'dixi',            # 横盘缩量挂昨收 -2%（实测胜率最高但成交率仅 22%）
    '可打板': 'daban',            # 当日封板、次日开盘即卖
    '可竞价低吸': 'chaodi',       # 昨日大面 ≥5%，今天竞价抄回调
    '不接力': 'relay',            # 昨日封板今天买 —— 正常不该出现在推荐里，出现即告警
    '当天不追': 'chase',          # 半山腰（4.5~9.8% 没封板）当天还涨着
    '排除': 'ban',                # 过热/天量/巨量拉升
    '减仓日': 'reduce',           # 涨停 >120 家的极热日
    '冻结买入': 'reduce',         # 同上（极热日的另一种表述）
}
MODE_LABELS = {
    'dixi': '低吸', 'daban': '打板', 'chaodi': '抄底', 'relay': '接力',
    'chase': '半山腰', 'ban': '排除形态', 'reduce': '减仓日', 'other': '其他',
}


def pick_mode(verdict: str, prev: dict = None) -> str:
    """verdict → 模式代码。没命中任何档但"昨天封过板"的，按接力记账：
    接力是我们明确不做的动作，它出现在推荐里就是漏口，得被榜照出来。"""
    code = MODE_BY_VERDICT.get(verdict or '')
    if code is None:
        code = 'relay' if (prev and prev.get('limitClose')) else 'other'
    return code


def mode_label(code: str) -> str:
    return MODE_LABELS.get(code, code or '其他')
