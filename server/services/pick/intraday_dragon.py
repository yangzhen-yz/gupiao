"""intraday_dragon.py - 盘中龙头通道（2026-11-01，影子先行）

解决什么
--------
用户原话：主线龙头"开盘后快速拉升 → 短暂停顿换手 → 一般不破分时均线（破了也快速收回）
→ 均线上方震荡 2~5 分钟 → 开盘 10 分钟内拉板"。
现有链路最早 09:45 才出推荐、60 秒轮询一次，龙头那时多半已封板 —— 推出来也买不到。

本模块把形态写成**分钟级状态机 + 连续打分**，输出 0~100 的"龙头模式分"：
  拉升段 R(0~30) + 停顿换手 T(0~15) + 均线纪律 V(0~20) + 震荡收敛 H(0~15) + 突破迫近 B(0~20)
  再扣减：高开过多 / 冲高回落 / 巨量滞涨 / 超时（>10 分钟）
分档：形态成立且距涨停 0.8%~2.0% = 推送；形态成立（或分数 ≥65）= 预警；
      ≥WATCH(52) = 观察。分数不设推送下限（五日回放里高分档反而更差）。

硬约束（与现有日线评分体系解耦）
------------------------------
1. **不读 13 个日线因子、不改任何权重、不改 MIN_RECOMMEND_SCORE / top3 / 主推送**；
2. 默认 `INTRADAY_DRAGON_ENABLED=False`：关掉时调度器连这次扫描都不进，零外部请求；
3. 影子期 `INTRADAY_DRAGON_SHADOW=True`：只写 `intraday_dragon_shadow`，不推企业微信；
4. 数据只用腾讯公开接口（快照 qt + 当日分时 minute/query），zero login。

为什么不是"硬与门"
------------------
"拉升≥3% 且回撤≤1% 且量比≥2"这种写法在回放里会把时间点卡死：同一形态早 30 秒
（回撤还没走完）和晚 30 秒（已经拉走）都判 0 分。连续打分让"差一点"仍然是一个
可比较的数，分档动作再决定要不要出手 —— 也才能拿分数做阈值寻优。

时间口径
--------
bars[0] = 0930（含集合竞价成交），bars[i] 的 price 是第 i 分钟末的价格；
打分只用 0..i，不看未来（回放与实盘同一条代码路径）。
"""
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from app.config import (INTRADAY_DRAGON_B_ARM,
                        INTRADAY_DRAGON_COOLDOWN_SEC, INTRADAY_DRAGON_DAILY_LIMIT,
                        INTRADAY_DRAGON_END, INTRADAY_DRAGON_ENABLED,
                        INTRADAY_DRAGON_FEATURE_LOG,
                        INTRADAY_DRAGON_INTERVAL_SEC, INTRADAY_DRAGON_MAX_PER_STOCK,
                        INTRADAY_DRAGON_MAX_ROOM_PCT, INTRADAY_DRAGON_MIN_OPEN_GAIN,
                        INTRADAY_DRAGON_MIN_ROOM_PCT, INTRADAY_DRAGON_MIN_SCORE,
                        INTRADAY_DRAGON_MIN_VOL_RATIO,
                        INTRADAY_DRAGON_PUSH_ENABLED, INTRADAY_DRAGON_PUSH_MODE,
                        INTRADAY_DRAGON_PUSH_SCORE,
                        INTRADAY_DRAGON_SCAN_MAX, INTRADAY_DRAGON_SCORE_MODE,
                        INTRADAY_DRAGON_SHADOW,
                        INTRADAY_DRAGON_START, INTRADAY_DRAGON_SWEEP_ALL,
                        INTRADAY_DRAGON_SWEEP_INTERVAL_SEC, INTRADAY_DRAGON_WARN_SCORE,
                        INTRADAY_DRAGON_WATCH_SCORE)
from db.database import get_db_conn

# 形态参数（全部具名，回放与实盘共用一份；改阈值只改这里）
DRAGON_PARAMS = {
    # 拉升段
    'raise_gain_lo': 1.0,      # 相对今开涨幅：低于此值拉升分 0
    'raise_gain_hi': 3.5,      # 相对今开涨幅：到此值拉升满分
    'raise_speed_fast': 3,     # 分钟数：3 分钟内到高点 → 速度满分
    'raise_speed_slow': 12,    # 分钟数：12 分钟才到高点 → 速度 0 分
    'raise_vol_lo': 1.2,       # 分钟量能倍数：低于此值量能分 0
    'raise_vol_hi': 3.0,       # 分钟量能倍数：到此值量能满分
    # 停顿换手
    'pause_depth_lo': 0.15,    # 回撤 <0.15% 视为"没停顿"，换手分只给 3
    'pause_depth_hi': 2.0,     # 回撤 0.5~2.0% 是理想换手区
    'pause_depth_bad': 3.0,    # 回撤 >3% 不给换手分（是走弱不是换手）
    'pause_dur_good': 4,       # 停顿 1~4 分钟满分
    'pause_dur_bad': 8,        # 停顿 ≥8 分钟不给分
    'pause_shrink_lo': 0.35,   # 停顿段均量 / 拉升段均量：0.35 左右缩量最健康
    'pause_shrink_hi': 0.95,   # 缩量比接近 1 = 没缩量，不给分
    # 均线纪律
    'vwap_above_frac_lo': 0.60,  # 在分时均线上方的时间占比：60% 起步
    'vwap_above_frac_hi': 0.95,  # 95% 以上满分
    'vwap_breach_hi': 0.80,      # 最大跌破幅度（%）：0.8% 以上不给纪律分
    'vwap_fake_break_sec': 2,    # 跌破后 2 根分钟线内收回 = "假破"，加分
    # 震荡收敛
    'hold_dur_lo': 2,          # 均线上方震荡 ≥2 分钟
    'hold_dur_hi': 6,          # 2~6 分钟最好，>6 分钟开始衰减
    'hold_range_ratio': 0.70,  # 最近 3 分钟振幅 / 拉升段振幅：<0.7 算收敛
    # 突破/迫近涨停
    'break_room_far': 4.0,     # 距涨停 4% 以上，突破分 0
    'break_room_near': 1.0,    # 距涨停 1% 以内，突破分满分
    'break_vol_lo': 1.5,       # 突破那根分钟量 / 前均量：1.5 倍起步
    'break_vol_hi': 2.5,       # 2.5 倍满分
    # 扣分项
    'gap_bad': 5.0,            # 高开 >5% 扣分（溢价已透支）
    'giveback_bad': 2.5,       # 从高点回撤 >2.5% 扣分
    'time_window_min': 10,     # 形态窗口：开盘 10 分钟内
    # 形态完整性（这是"是不是这个形态"的判据，不是分数；见 pattern_complete）
    'pat_gain_lo': 2.0,        # 触发时相对今开涨幅下限
    'pat_gain_hi': 8.5,        # 上限：再高就是追高，不是"起步阶段"
    'pat_room_lo': 0.8,        # 距涨停下限：留出介入空间
    'pat_room_hi': 3.5,        # 上限：太远说明还没进入冲刺段
    'pat_pause_lo': 2,         # 停顿换手 2~5 分钟
    'pat_pause_hi': 5,
    'pat_pullback_lo': 0.3,    # 回撤 0.3~2.0% 是换手；<0.3 没停顿、>2.0 走弱
    'pat_pullback_hi': 2.0,
    'pat_hold_lo': 2,          # 均线上方震荡至少 2 分钟
    'pat_above_frac': 0.70,    # 在均线上方的时间占比
    'pat_shrink_max': 0.80,    # 停顿段缩量比上限
    'pat_gap_max': 5.0,        # 高开超过 5% 不属于"开盘快速拉升"
    'pat_deadline': '0955',    # 形态必须在 09:55 前完成（留出封板时间）
    'pat_cap_score': 60.0,     # 形态不完整时总分封顶（不能进预警/推送档）
}


def _ramp(x: float, lo: float, hi: float) -> float:
    """线性斜坡：lo→0 分、hi→1 分（hi 可以小于 lo，表示越大越差）。"""
    if hi == lo:
        return 0.0 if x < lo else 1.0
    v = (x - lo) / (hi - lo)
    return max(0.0, min(1.0, v))


def pattern_complete(pt: Dict, P: Optional[Dict] = None) -> bool:
    """形态完整性：用户描述的那套动作是否真的走完了。

    为什么要单独一层、而不是全靠分数
    ---------------------------------
    2026-11-01 五日回放（10-26~10-30，316 只、1551 个有效票日）：
      - 只按分数 ≥78 触发，12 次里仅 4 次封板，次日开盘 −1.62%；
      - 加上结构判据后，45 个票日形态成立，其中 **36 个当日任一分钟触及涨停价（80%）**，
        触板组次日开盘 +1.51%（n=25，另 11 个缺次日数据），未触板组 −5.75%（n=9）。
    所以：**分数管强度与排序，结构管"是不是这个形态"**，后者不能靠阈值替代。
    """
    P = P or DRAGON_PARAMS
    return (P['pat_gain_lo'] <= pt['gain_open'] <= P['pat_gain_hi']
            and P['pat_room_lo'] <= pt['room'] <= P['pat_room_hi']
            and P['pat_pause_lo'] <= pt['pause_dur'] <= P['pat_pause_hi']
            and P['pat_pullback_lo'] <= pt['pullback'] <= P['pat_pullback_hi']
            and pt['hold_dur'] >= P['pat_hold_lo']
            and pt['above_vwap'] and pt['above_frac'] >= P['pat_above_frac']
            and pt['shrink'] <= P['pat_shrink_max']
            and pt['gap'] <= P['pat_gap_max']
            and pt['t'] <= P['pat_deadline']
            and not pt['sealed'])


def normalize_symbol(sym: str) -> str:
    """'600000' / 'sh600000' / 'SH600000' → 'sh600000'；无法识别返回原值。"""
    s = (sym or '').strip().lower()
    if s.startswith(('sh', 'sz', 'bj')):
        return s
    if s.startswith(('6', '9')):
        return 'sh' + s
    if s.startswith(('0', '2', '3')):
        return 'sz' + s
    if s.startswith(('4', '8')):
        return 'bj' + s
    return s


def limit_up_price(prec: float, pct: float = 10.0) -> float:
    """涨停价：昨收 ×(1+pct%)，四舍五入到分。主板 10%、ST 5%、创业/科创 20%。"""
    return round(prec * (1 + pct / 100.0) + 1e-9, 2)


# ---------- 解析 ----------

def parse_minute_rows(rows: List[str], prec: float,
                      prev_day_volume_shares: Optional[float] = None) -> List[Dict]:
    """腾讯分时 `"0930 9.98 6676 6662648.00"` → bars。

    每行 = 时间 / 价格 / **累计**成交量(手) / **累计**成交额(元)。
    vwap 用累计额÷累计量（股）算，是当日真实分时均线，不做任何近似。
    """
    bars = []
    prev_cum = 0.0
    for line in rows:
        parts = line.split()
        if len(parts) < 3:
            continue
        t, price, cum_vol = parts[0], float(parts[1]), float(parts[2])
        cum_amt = float(parts[3]) if len(parts) > 3 and parts[3] else 0.0
        vol = max(0.0, cum_vol - prev_cum)
        prev_cum = cum_vol
        vwap = (cum_amt / (cum_vol * 100.0)) if (cum_amt > 0 and cum_vol > 0) else price
        bars.append({'t': t, 'price': price, 'vol': vol, 'cum_vol': cum_vol,
                     'cum_amt': cum_amt, 'vwap': vwap})
    if bars and prev_day_volume_shares:
        avg_min = float(prev_day_volume_shares) / 100.0 / 240.0  # 股→手，再除 240 分钟
        for i, b in enumerate(bars):
            denom = avg_min * max(i, 1)   # 0930 那根含集合竞价，分母按 1 分钟算
            b['vol_ratio_run'] = (b['cum_vol'] / denom) if denom > 0 else 0.0
    return bars


# ---------- 单点打分 ----------

def _comps(bars: List[Dict], i: int, prec: float, lim: float,
           params: Optional[Dict] = None) -> Dict:
    """只看 bars[0..i]，返回该时点的五段分与扣分。"""
    P = params or DRAGON_PARAMS
    b = bars[i]
    p0 = bars[0]['price']
    cur = b['price']
    hi = max(x['price'] for x in bars[:i + 1])
    hi_idx = max(j for j in range(i + 1) if bars[j]['price'] == hi)
    gain_hi = (hi / p0 - 1) * 100 if p0 else 0.0
    gain_now = (cur / p0 - 1) * 100 if p0 else 0.0
    gap = (p0 / prec - 1) * 100 if prec else 0.0
    room = (lim - cur) / cur * 100 if cur else 99.0
    pb = (hi - cur) / hi * 100 if hi else 0.0
    elapsed = i  # 0930 为 0

    # ---- R 拉升（0~30）----
    r_gain = 12 * _ramp(gain_hi, P['raise_gain_lo'], P['raise_gain_hi'])
    r_speed = 10 * _ramp(hi_idx, P['raise_speed_slow'], P['raise_speed_fast'])
    vr_hi = bars[hi_idx].get('vol_ratio_run', 0.0)
    r_vol = 8 * _ramp(vr_hi, P['raise_vol_lo'], P['raise_vol_hi'])
    R = r_gain + r_speed + r_vol

    # ---- T 停顿换手（0~15）----
    T = 0.0
    if hi_idx >= 1 and i > hi_idx:
        if pb < P['pause_depth_lo']:
            t_depth = 3.0                       # 一路直拉、没有停顿 → 换手特征缺失
        elif pb <= P['pause_depth_hi']:
            t_depth = 6.0 * _ramp(pb, P['pause_depth_lo'], 0.5)
        else:
            t_depth = 6.0 * _ramp(P['pause_depth_bad'] - pb, 0, P['pause_depth_bad'] - P['pause_depth_hi'])
        dur = i - hi_idx
        t_dur = 5.0 * _ramp(P['pause_dur_bad'] - dur, 0, P['pause_dur_bad'] - P['pause_dur_good']) \
            if dur <= P['pause_dur_bad'] else 0.0
        pull_vols = [bars[j]['vol'] for j in range(1, hi_idx + 1)]
        pause_vols = [bars[j]['vol'] for j in range(hi_idx + 1, i + 1)]
        if pull_vols and pause_vols and sum(pull_vols) > 0:
            shrink = (sum(pause_vols) / len(pause_vols)) / (sum(pull_vols) / len(pull_vols))
            t_shrink = 4.0 * _ramp(shrink, P['pause_shrink_hi'], P['pause_shrink_lo'])
        else:
            shrink, t_shrink = 1.0, 0.0
        T = t_depth + t_dur + t_shrink
    else:
        shrink = 1.0

    # ---- V 均线纪律（0~20）----
    above = sum(1 for j in range(1, i + 1) if bars[j]['price'] >= bars[j]['vwap'])
    frac = above / max(i, 1)
    v_above = 10 * _ramp(frac, P['vwap_above_frac_lo'], P['vwap_above_frac_hi'])
    breach = max(((bars[j]['vwap'] - bars[j]['price']) / bars[j]['vwap'] * 100)
                 for j in range(1, i + 1)) if i >= 1 else 0.0
    v_breach = 6 * _ramp(breach, P['vwap_breach_hi'], 0.0)
    # 假破：跌破过（幅度在 0~0.6%），且最近一根已收回
    fake = 0.0
    if 0 < breach <= 0.6 and cur >= b['vwap']:
        recovered = False
        for j in range(i, max(0, i - int(P['vwap_fake_break_sec'])), -1):
            if bars[j]['price'] < bars[j]['vwap']:
                break
            recovered = True
        fake = 4.0 if recovered else 0.0
    V = v_above + v_breach + fake

    # ---- H 震荡收敛（0~15）----
    hold_dur = sum(1 for j in range(hi_idx + 1, i + 1) if bars[j]['price'] >= bars[j]['vwap'])
    if hold_dur <= 0:
        h_dur = 0.0
    elif hold_dur < P['hold_dur_lo']:
        h_dur = 8.0 * hold_dur / max(P['hold_dur_lo'], 1)
    elif hold_dur <= P['hold_dur_hi']:
        h_dur = 8.0
    else:
        h_dur = 8.0 * max(0.0, _ramp(12 - hold_dur, 0, 6))
    pull_rng = (max(x['price'] for x in bars[:hi_idx + 1]) -
                min(x['price'] for x in bars[:hi_idx + 1])) if hi_idx >= 1 else 0.0
    last3 = [x['price'] for x in bars[max(0, i - 2):i + 1]]
    rng3 = max(last3) - min(last3)
    h_conv = 5.0 * _ramp(rng3 / pull_rng, P['hold_range_ratio'], 0.30) if pull_rng > 0 else 0.0
    lows_ok = 2.0 if i >= 1 and min((bars[j]['vwap'] - bars[j]['price']) / bars[j]['vwap'] * 100
                                    for j in range(max(1, hi_idx), i + 1)) <= 0.3 else 0.0
    H = h_dur + h_conv + lows_ok

    # ---- B 突破/迫近（0~20）----
    b_room = 12 * _ramp(room, P['break_room_far'], P['break_room_near'])
    b_break = 4.0 if i > hi_idx and cur >= hi * 1.002 else 0.0
    prev_vols = [bars[j]['vol'] for j in range(0, i)] or [1.0]
    vol_mean = sum(prev_vols) / len(prev_vols)
    b_vol = 4.0 * _ramp(b['vol'] / vol_mean, P['break_vol_lo'], P['break_vol_hi']) if vol_mean > 0 else 0.0
    B = b_room + b_break + b_vol

    # ---- 扣分 ----
    pen = 0.0
    if gap > P['gap_bad']:
        pen += 6.0 * _ramp(gap, P['gap_bad'], 8.0)
    if pb > P['giveback_bad']:
        pen += 6.0 * _ramp(pb, P['giveback_bad'], 5.0)
    # 巨量滞涨：最近两根量能 ≥2 倍均量但价格几乎不动
    if i >= 2:
        v2 = max(bars[j]['vol'] for j in (i - 1, i)) / vol_mean if vol_mean > 0 else 0
        move = abs(bars[i]['price'] / bars[i - 1]['price'] - 1) * 100
        if v2 >= 2.0 and move <= 0.2:
            pen += 6.0
    if elapsed > P['time_window_min']:
        pen += min(12.0, 1.2 * (elapsed - P['time_window_min']))

    raw = max(0.0, min(100.0, R + T + V + H + B - pen))
    out = {
        't': b['t'], 'price': cur, 'score': round(raw, 1),
        'R': round(R, 1), 'T': round(T, 1), 'V': round(V, 1),
        'H': round(H, 1), 'B': round(B, 1), 'pen': round(pen, 1),
        'gain_open': round(gain_now, 2), 'gain_hi': round(gain_hi, 2),
        'gap': round(gap, 2), 'room': round(room, 2), 'pullback': round(pb, 2),
        'vwap': round(b['vwap'], 3), 'above_vwap': cur >= b['vwap'],
        'above_frac': round(frac, 2),
        'pause_dur': (i - hi_idx) if i > hi_idx else 0,
        'hold_dur': int(hold_dur),
        'sealed': cur >= lim - 1e-6, 'shrink': round(shrink, 2),
    }
    out['pat'] = pattern_complete(out, P)
    if not out['pat']:
        # 形态没走完：分数封顶，最多到"观察"档 —— 否则 09:33 的急拉会顶到推送线
        out['score'] = round(min(out['score'], float(P['pat_cap_score'])), 1)
    return out


def stage_of(point: Dict, score_push: float = INTRADAY_DRAGON_PUSH_SCORE,
             score_warn: float = INTRADAY_DRAGON_WARN_SCORE,
             score_watch: float = INTRADAY_DRAGON_WATCH_SCORE,
             room_min: float = INTRADAY_DRAGON_MIN_ROOM_PCT,
             room_max: float = INTRADAY_DRAGON_MAX_ROOM_PCT) -> str:
    """分档：sealed 封板 / push 可推 / warn 预警 / watch 观察 / idle。

    2026-11-01 五日回放改了这里的口径：**形态是否走完 + 离涨停还有多少**才是推送判据，
    分数只做排序（PUSH_SCORE 默认 0 = 不设分数下限）。原因是同一批样本里
    分数≥70 的 20 条次日开盘 −1.94%，而"形态成立且距涨停 ≤2%"的 6 条触板 5 只 —
    分数在 60~90 区间与结果并不单调，拿它当硬门槛会先筛掉能封板的票。
    """
    if point['sealed']:
        return 'sealed'
    if (point['pat'] and point['score'] >= score_push
            and room_min <= point['room'] <= room_max):
        return 'push'
    if point['pat'] or point['score'] >= score_warn:
        return 'warn'
    if point['score'] >= score_watch:
        return 'watch'
    return 'idle'


def evaluate_series(bars: List[Dict], prec: float, lim: float,
                    params: Optional[Dict] = None, max_minute: int = 30) -> Dict:
    """逐分钟打分（0..max_minute），返回 {series, pattern_first, trigger, best, seal_time}。

    - `pattern_first`：形态第一次完整成立（拉升→停顿→均线上方震荡→仍距涨停有空间）；
    - `trigger`：形态成立 且 进入 push 档（未封板、距涨停 ≤MAX_ROOM）的那一刻。
    两者分开是为了诚实评估：形态本身有没有料（pattern_first），
    和我们设的推送线有没有把料筛掉（trigger）是两件事。
    """
    P = params or DRAGON_PARAMS
    series = []
    pattern_first = None
    trigger = None
    for i in range(1, min(len(bars), max_minute + 1)):
        pt = _comps(bars, i, prec, lim, P)
        pr = stage_of(pt)
        pt['stage'] = pr
        series.append(pt)
        if pattern_first is None and pt['pat']:
            pattern_first = pt
        if trigger is None and pt['pat'] and pr == 'push':
            trigger = pt
    best = max(series, key=lambda x: x['score']) if series else None
    # 封板时间（第一次 price≥涨停）
    seal_t = None
    for b in bars:
        if b['price'] >= lim - 1e-6:
            seal_t = b['t']
            break
    return {'series': series, 'pattern_first': pattern_first, 'trigger': trigger,
            'best': best, 'seal_time': seal_t}


# ---------- 实时数据 ----------

QT_FIELDS = {'price': 3, 'prec': 4, 'open': 5, 'high': 33, 'low': 34,
             'vol_hand': 36, 'amount_wan': 37, 'limit_up': 47, 'limit_down': 48,
             'vol_ratio': 49, 'turnover': 38, 'bid1_vol': 12, 'ask1_vol': 22,
             # 盘口价格与买一/卖一量（2026-11-01 T2.1 新增）：9=买一价、19=卖一价、
             # 10=买一量、20=卖一量。旧键 bid1_vol/ask1_vol 取的是 12/22（买二/卖二量），
             # 那是既有 weibi 口径，不动；B 臂算价差只用本组新键。
             'bid1_px': 9, 'ask1_px': 19, 'bid1_vol_1': 10, 'ask1_vol_1': 20}


def fetch_snapshot(codes: List[str], batch: int = 60, timeout: float = 8.0,
                   threads: int = 1) -> Dict[str, Dict]:
    """腾讯实时快照（60 只/批，实测 0.36s/批）。返回 {code: {字段}}，失败返回 {}。

    threads>1 时按批并行（全市场 2462 只 = 41 批，6 线程约 2.5s）；默认仍串行。
    """
    out: Dict[str, Dict] = {}
    codes = [c for c in codes if c]
    chunks = [codes[k:k + batch] for k in range(0, len(codes), batch)]

    def _one(part):
        got = {}
        url = 'https://qt.gtimg.cn/q=' + ','.join(part)
        try:
            raw = urllib.request.urlopen(url, timeout=timeout).read().decode('gbk', 'ignore')
        except Exception as e:
            print(f'[龙头通道] 快照失败({len(part)}只): {type(e).__name__}: {e}')
            return got
        for line in raw.split(';'):
            line = line.strip()
            if not line.startswith('v_') or '="' not in line:
                continue
            code = line[2:line.index('=')]
            body = line[line.index('="') + 2:].rstrip('"')
            f = body.split('~')
            if len(f) < 50:
                continue
            try:
                got[code] = {k2: float(f[idx]) for k2, idx in QT_FIELDS.items() if f[idx] not in ('', '-')}
                got[code]['name'] = f[1]
            except (ValueError, IndexError):
                continue
        return got

    if threads <= 1 or len(chunks) <= 1:
        for part in chunks:
            out.update(_one(part))
    else:
        with ThreadPoolExecutor(max_workers=threads) as pool:
            for got in pool.map(_one, chunks):
                out.update(got)
    return out


_UNIVERSE_CACHE: Dict[str, object] = {'ts': 0.0, 'codes': []}


def universe_codes() -> List[str]:
    """主板非 ST/退 的代码表（本地 stock_basic，6 小时缓存，零外部请求）。"""
    if _UNIVERSE_CACHE['codes'] and time.time() - float(_UNIVERSE_CACHE['ts']) < 21600:
        return list(_UNIVERSE_CACHE['codes'])          # type: ignore[arg-type]
    codes: List[str] = []
    try:
        import sqlite3
        from app.config import KLINE_HISTORY_DB_PATH
        conn = sqlite3.connect(KLINE_HISTORY_DB_PATH)
        try:
            for sym, name in conn.execute('SELECT symbol, name FROM stock_basic'):
                s = str(sym).zfill(6)
                if not s.startswith(('60', '00')):
                    continue
                if 'ST' in (name or '') or '退' in (name or ''):
                    continue
                codes.append(normalize_symbol(s))
        finally:
            conn.close()
    except Exception as e:
        print(f'[龙头通道] 读股票范围失败: {type(e).__name__}: {e}')
    _UNIVERSE_CACHE.update({'ts': time.time(), 'codes': codes})
    return list(codes)


_SWEEP: Dict[str, object] = {'ts': 0.0, 'hits': {}}     # 全市场预筛结果（code -> 快照字段）
_SURGE: Dict[str, float] = {}                            # 本轮之前扫到的急拉票（保留 30 分钟）
_AMT_PREV: Dict[str, Tuple[str, float]] = {}             # 特征层回退用：code -> (HH:MM, 累计额万元)


def fetch_minute_today(code: str, prec: float = 0.0,
                       prev_day_volume_shares: Optional[float] = None,
                       timeout: float = 8.0) -> Tuple[str, List[Dict]]:
    """当日分时（单只约 0.5s）。返回 (日期 YYYYMMDD, bars)；失败返回 ('', [])。"""
    url = f'https://ifzq.gtimg.cn/appstock/app/minute/query?code={code}'
    try:
        d = json.loads(urllib.request.urlopen(url, timeout=timeout).read().decode('utf-8'))
        k = d['data'][code]
        rows = k['data']['data']
        date = str(k['data'].get('date') or '')
        return date, parse_minute_rows(rows, prec, prev_day_volume_shares)
    except Exception as e:
        print(f'[龙头通道] 分时失败 {code}: {type(e).__name__}: {e}')
        return '', []


_AVG_VOL_CACHE: Dict[str, Tuple[float, Optional[float]]] = {}


def avg_volume_5d(symbol: str) -> Optional[float]:
    """本地日K取最近 5 个交易日均量（股）。零外部请求；缓存 1 小时。"""
    hit = _AVG_VOL_CACHE.get(symbol)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    code = normalize_symbol(symbol)[2:]
    try:
        import sqlite3
        from app.config import KLINE_HISTORY_DB_PATH
        conn = sqlite3.connect(KLINE_HISTORY_DB_PATH)
        try:
            rows = conn.execute(
                'SELECT volume FROM stock_daily WHERE symbol=? ORDER BY date DESC LIMIT 5',
                (code,)).fetchall()
        finally:
            conn.close()
        val = (sum(r[0] for r in rows) / len(rows)) if rows and all(r[0] for r in rows) else None
    except Exception:
        val = None
    _AVG_VOL_CACHE[symbol] = (time.time(), val)
    return val


# ---------- 影子落库 ----------

_DDL = '''CREATE TABLE IF NOT EXISTS intraday_dragon_shadow (
    trade_date TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    name       TEXT,
    stage      TEXT NOT NULL,
    first_seen TEXT,
    last_seen  TEXT,
    best_score REAL,
    price      REAL,
    gain_open  REAL,
    room_pct   REAL,
    comps      TEXT,
    updated_at TEXT,
    PRIMARY KEY (trade_date, symbol, stage)
)'''

_ALERT_DDL = '''CREATE TABLE IF NOT EXISTS dragon_alert_log (
    trade_date TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    pushed_at  TEXT NOT NULL,
    score      REAL,
    stage      TEXT,
    PRIMARY KEY (trade_date, symbol, pushed_at)
)'''


def ensure_tables():
    conn = get_db_conn()
    try:
        conn.execute(_DDL)
        conn.execute(_ALERT_DDL)
        conn.commit()
    finally:
        conn.close()


def _save_stage(trade_date: str, symbol: str, name: str, pt: Dict):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    comps = json.dumps({k: pt[k] for k in ('R', 'T', 'V', 'H', 'B', 'pen', 'pullback', 'shrink')},
                       ensure_ascii=False)
    conn = get_db_conn()
    try:
        conn.execute(
            'INSERT INTO intraday_dragon_shadow '
            '(trade_date,symbol,name,stage,first_seen,last_seen,best_score,price,gain_open,room_pct,comps,updated_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(trade_date,symbol,stage) DO UPDATE SET '
            'last_seen=excluded.last_seen, '
            'best_score=MAX(COALESCE(best_score,0),COALESCE(excluded.best_score,0)), '
            'price=excluded.price, gain_open=excluded.gain_open, room_pct=excluded.room_pct, '
            'comps=excluded.comps, updated_at=excluded.updated_at',
            (trade_date, symbol, name, pt['stage'], now, now, pt['score'], pt['price'],
             pt['gain_open'], pt['room'], comps, now))
        conn.commit()
    except Exception as e:
        print(f'[龙头通道] 影子落库失败 {symbol}: {type(e).__name__}: {e}')
    finally:
        conn.close()


# ---------- 一轮扫描 ----------

def _candidate_pool() -> List[str]:
    """本轮看的票：用户池 ∪ 当日推荐 ∪ 海选池前 N ∪ 昨日涨停池（龙头多半从这来）∪ 全市场预筛命中。

    全部走本地表/缓存，不额外打接口；截断发生在快照预筛之后（scan_max）。
    """
    out, seen = [], set()

    def _add(codes):
        for c in codes or []:
            s = normalize_symbol(c)
            if s and s not in seen:
                seen.add(s)
                out.append(s)

    try:
        from services.support.push import load_user_scan_pool
        _add(load_user_scan_pool())
    except Exception:
        pass
    conn = get_db_conn()
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        _add([r[0] for r in conn.execute(
            'SELECT symbol FROM daily_recommendations WHERE rec_date=?', (today,))])
        # 昨日涨停池 + 炸板池：龙头候选的天然来源（本地表，零请求）
        _add([r[0] for r in conn.execute(
            'SELECT symbol FROM limit_pool_daily WHERE trade_date=(SELECT MAX(trade_date) FROM limit_pool_daily) '
            "AND pool IN ('zt','zb')")])
    except Exception as e:
        print(f'[龙头通道] 候选池读取失败: {e}')
    finally:
        conn.close()
    try:
        from services.pick.candidate_pool import load_pool
        _add([p.get('symbol') for p in load_pool(limit=60)])
    except Exception:
        pass
    # 全市场预筛扫到的急拉票（保留 30 分钟）：它们可能昨天没涨停、今天才启动
    now = time.time()
    for s, ts in list(_SURGE.items()):
        if now - ts > 1800:
            _SURGE.pop(s, None)
        else:
            _add([s])
    return out


def _sweep_due(hm: str) -> Dict[str, Dict]:
    """全市场快照预筛（默认关）。只在 09:31~09:50、距上次 ≥ SWEEP_INTERVAL 时真扫。

    五日回放：10:00 前封板的 113 只里，只有 33 只（29.2%）落在盘前候选池 ——
    不补这一层，龙头通道的召回上限就是三成。代价是 41 批 qt 请求/次。
    """
    if not INTRADAY_DRAGON_SWEEP_ALL or not ('09:31' <= hm <= '09:50'):
        return {}
    last = float(_SWEEP['ts'])                          # type: ignore[arg-type]
    if time.time() - last < INTRADAY_DRAGON_SWEEP_INTERVAL_SEC:
        return dict(_SWEEP['hits'])                     # type: ignore[arg-type]
    codes = universe_codes()
    if not codes:
        return {}
    t0 = time.time()
    snap = fetch_snapshot(codes, threads=6)
    hits: Dict[str, Dict] = {}
    for code, q in snap.items():
        price, op, prec = q.get('price', 0), q.get('open', 0), q.get('prec', 0)
        if not (price > 0 and op > 0 and prec > 0):
            continue
        nm = q.get('name', '')
        if 'ST' in nm or '退' in nm:
            continue
        lim = q.get('limit_up') or limit_up_price(prec)
        if price >= lim - 1e-6:
            continue
        # 全市场预筛要比池内更严：只留"形态目标区"（相对今开 2%~8.5%）且量比过线，
        # 否则 1091 只急拉票会把单轮分钟抓取时间撑爆（实测 OR 条件命中 1091、AND 后 186）。
        gain = (price / op - 1) * 100
        if (DRAGON_PARAMS['pat_gain_lo'] <= gain <= DRAGON_PARAMS['pat_gain_hi']
                and q.get('vol_ratio', 0) >= INTRADAY_DRAGON_MIN_VOL_RATIO):
            hits[code] = q
    _SWEEP.update({'ts': time.time(), 'hits': hits})
    for c in hits:
        _SURGE[c] = time.time()
    print(f'[龙头通道] 全市场预筛 {len(codes)} 只 → 急拉 {len(hits)} 只，'
          f'{time.time() - t0:.1f}s')
    return hits


def scan_once(scan_max: Optional[int] = None) -> List[Dict]:
    """一轮：快照预筛 → 拉分时 → 打分 → 影子落库（推送另开关）。返回命中列表。"""
    scan_max = scan_max or INTRADAY_DRAGON_SCAN_MAX
    codes = _candidate_pool()
    if not codes:
        return []
    snap = fetch_snapshot(codes, threads=4)
    sweep = _sweep_due(datetime.now().strftime('%H:%M'))
    if sweep:
        snap.update(sweep)
    if not snap:
        return []
    pre = []
    for code, q in snap.items():
        price, op, prec = q.get('price', 0), q.get('open', 0), q.get('prec', 0)
        if not (price > 0 and op > 0 and prec > 0):
            continue
        # 只做主板非 ST/退：涨停价按 10% 算（limit_up_price 的默认），
        # 创业板/科创板是 20%、北交所 30%，混进来会算错"距涨停"。
        base = code[2:] if code.startswith(('sh', 'sz')) else code
        nm = q.get('name', '')
        if not base.startswith(('60', '00')) or 'ST' in nm or '退' in nm:
            continue
        lim = q.get('limit_up') or limit_up_price(prec)
        if price >= lim - 1e-6:
            continue                                    # 已封板：买不到，不必再算
        gain_open = (price / op - 1) * 100
        vr = q.get('vol_ratio', 0)
        if gain_open < INTRADAY_DRAGON_MIN_OPEN_GAIN and vr < INTRADAY_DRAGON_MIN_VOL_RATIO:
            continue
        pre.append((code, q))
    if not pre and not INTRADAY_DRAGON_B_ARM:
        return []
    # 优先"形态目标区"（涨幅 2%~8.5%）+ 量比高的，再按涨幅；否则截断会把真正的龙头切掉
    def _rank(item):
        _, q = item
        gain = (q['price'] / q['open'] - 1) * 100 if q.get('open') else 0
        band = 1.0 if DRAGON_PARAMS['pat_gain_lo'] <= gain <= DRAGON_PARAMS['pat_gain_hi'] else 0.0
        return (band, min(q.get('vol_ratio', 0), 5.0), gain)
    pre.sort(key=_rank, reverse=True)
    pre = pre[:scan_max]
    # B 臂（涨幅前 20，清单 T2.1）：只补候选，不改 A 臂的排序、截断与判定。
    # 开关关时连模块都不导入 —— 零额外请求、零额外行；开关开时每 ROUND_SEC 重排一次，
    # 两次重排之间的 20s 轮次继续检查上一批候选（清单：触发仍按 20s 检查）。
    if INTRADAY_DRAGON_B_ARM:
        try:
            from services.pick import dragon_entry
            if dragon_entry.round_due(datetime.now().strftime('%H:%M'), time.time(),
                                      float(dragon_entry._STATE['last_round_ts'])):
                dragon_entry.run_round()
            have = {c for c, _ in pre}
            for r in dragon_entry.last_round():
                if r['symbol'] in have:
                    continue
                pre.append((r['symbol'], {
                    'price': r['price'], 'open': r['open'], 'prec': r['prec'],
                    'limit_up': r['limit_up'], 'name': r['name'],
                    'vol_ratio': r['vol_ratio'],
                    # 特征层（T2.2）要用的盘口与累计额；B 臂候选在 last_round 里带着
                    'bid1_px': r.get('bid1_px'), 'ask1_px': r.get('ask1_px'),
                    'bid1_vol_1': r.get('bid1_vol_1'), 'ask1_vol_1': r.get('ask1_vol_1'),
                    'amount_wan': r.get('amount_wan'),
                }))
                have.add(r['symbol'])
        except Exception as b_err:
            print(f'[龙头通道] B 臂异常: {type(b_err).__name__}: {b_err}')
    if not pre:
        return []
    today = datetime.now().strftime('%Y-%m-%d')
    now_hm = datetime.now().strftime('%H:%M')
    # 评分 v2（清单 T2.3，影子）：只算分、只记录，不改变谁被推。
    # legacy 时连模块都不导入；流动性阈值每轮读一次缓存（20 日影子分位 → 只收紧不放松）。
    score_mode = INTRADAY_DRAGON_SCORE_MODE
    liq_th = None
    if score_mode != 'legacy' and not INTRADAY_DRAGON_FEATURE_LOG:
        print('[龙头评分v2] 特征层开关关闭，无法评估，本轮退回 legacy')
        score_mode = 'legacy'
    if score_mode != 'legacy':
        try:
            from services.pick import dragon_score
            score_mode = dragon_score.ensure_mode(score_mode)
            liq_th = dragon_score.load_liquidity_thresholds(today=today)
        except Exception as s_err:
            print(f'[龙头评分v2] 初始化失败，本轮退回 legacy: {type(s_err).__name__}: {s_err}')
            score_mode = 'legacy'
            liq_th = None
    # 特征层回退用：上一轮快照的累计额（只在分时拿不到 cum_amt 时才会用到）
    prev_amt = {s: v[1] for s, v in _AMT_PREV.items()}
    prev_hm = {s: v[0] for s, v in _AMT_PREV.items()}
    rank_map: Dict[str, int] = {}
    if INTRADAY_DRAGON_B_ARM:
        try:
            from services.pick import dragon_entry
            rank_map = {r['symbol']: i for i, r in enumerate(dragon_entry.last_round(), 1)}
        except Exception:
            rank_map = {}

    def _one(item):
        code, q = item
        prev_vol = avg_volume_5d(code)
        _, bars = fetch_minute_today(code, q['prec'], prev_vol)
        if len(bars) < 3:
            return None
        lim = q.get('limit_up') or limit_up_price(q['prec'])
        # 特征层（清单 T2.2）：只读 bars[0..i] 与同一轮快照，缺失留 NULL
        feat = None
        if INTRADAY_DRAGON_FEATURE_LOG:
            try:
                from services.pick import dragon_features
                feat = dragon_features.compute_features(
                    bars, q, i=len(bars) - 1, now_hm=now_hm,
                    rank_gain=rank_map.get(code),
                    prev_amount=prev_amt.get(code), prev_hm=prev_hm.get(code, ''))
            except Exception as f_err:
                print(f'[龙头特征] 计算失败 {code}: {type(f_err).__name__}: {f_err}')
        res = evaluate_series(bars, q['prec'], lim, max_minute=len(bars) - 1)
        if not res['best']:
            return None
        # 取"此刻"的时点（最后一根已完成的分钟）与当日最佳
        last = res['series'][-1]
        # 评分 v2 判定（G3.1）：结构用 bars 从 09:45 起算；分数只做同档排序。
        ev = None
        if score_mode != 'legacy' and feat:
            try:
                from services.pick import dragon_score
                ev = dragon_score.evaluate(
                    {'symbol': code, 'name': q.get('name', ''), 'price': last['price'],
                     'limit_up': lim},
                    feat, bars=bars, legacy_score=last['score'], liq=liq_th, now_hm=now_hm)
            except Exception as s_err:
                print(f'[龙头评分v2] {code} 判定失败: {type(s_err).__name__}: {s_err}')
        return {'symbol': code, 'name': q.get('name', ''), 'point': last,
                'best': res['best'], 'trigger': res['trigger'],
                'pattern_first': res['pattern_first'], 'q': q, 'feat': feat, 'ev': ev,
                'bars': bars}

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = [r for r in pool.map(_one, pre) if r]

    if INTRADAY_DRAGON_FEATURE_LOG and results:
        try:
            from services.pick import dragon_features
            n = dragon_features.save_features(today, [
                {'symbol': r['symbol'], 'name': r['name'], 'feat': r['feat']}
                for r in results if r.get('feat')])
            if n:
                print(f'[龙头特征] 本轮落库 {n} 行')
        except Exception as f_err:
            print(f'[龙头特征] 落库失败: {type(f_err).__name__}: {f_err}')
        _AMT_PREV.clear()
        for s, q in snap.items():
            amt = float(q.get('amount_wan') or 0)
            if amt > 0:
                _AMT_PREV[s] = (now_hm, amt)

    if score_mode != 'legacy':
        evs = [r['ev'] for r in results if r.get('ev')]
        n_cand = sum(1 for e in evs if e['verdict'] == 'candidate')
        n_watch = sum(1 for e in evs if e['verdict'] == 'watch')
        n_rej = sum(1 for e in evs if e['verdict'] == 'reject')
        n70 = sum(1 for e in evs if e.get('shadow70'))
        print(f'[龙头评分v2 {now_hm}] 评估 {len(evs)} 只 → 候选 {n_cand} / watch {n_watch} / '
              f'reject {n_rej}；影子≥70 {n70}（MIN_SCORE={INTRADAY_DRAGON_MIN_SCORE}）')
        for e in evs:
            if e['verdict'] == 'candidate':
                print(f"  {e['name']}({e['symbol']}) score_v2={e['score_v2']} "
                      f"旧分={e['score_legacy']} 结构={e['structure']['status']} "
                      f"流动性={e['liquidity']['status']}")

    # T2.6：影子表（每轮候选/触发各一行，按秒去重）。写库失败只打印，不影响推送。
    try:
        from services.pick import dragon_shadow
        dragon_shadow.observe(results, now=datetime.now())
    except Exception as s_err:
        print(f'[龙头影子] 本轮写入异常（不影响主链路）: {type(s_err).__name__}: {s_err}')

    # T2.7：持久化候选票当日完整分钟 bar，供 dragon_backtest.simulate() 复盘真实收益。
    # 落库失败只打印，绝不打断扫描主链路；UPSERT 保证每只票留当日最完整一根。
    try:
        from services.pick import dragon_minute_store as _dms
        _saved = 0
        for r in results:
            b = r.get('bars')
            if b:
                _dms.save_minute(today, r['symbol'], b)
                _saved += 1
        if _saved:
            print(f'[龙头分钟bar] 本轮落库 {_saved} 只')
    except Exception as m_err:
        print(f'[龙头分钟bar] 本轮落库异常（不影响主链路）: {type(m_err).__name__}: {m_err}')

    hits = []
    for r in results:
        pt = r['best'] if r['best']['score'] >= r['point']['score'] else r['point']
        if r['pattern_first'] and pt['score'] < INTRADAY_DRAGON_WATCH_SCORE:
            pt = dict(pt)
            pt['stage'] = 'pattern'      # 形态成立但强度不够：只记，不推
        # 只落 watch 及以上（含形态成立），避免每 15 秒把 idle 状态写成几万行
        if pt['score'] >= INTRADAY_DRAGON_WATCH_SCORE or pt['sealed'] or r['pattern_first']:
            _save_stage(today, r['symbol'], r['name'], pt)
        if r['trigger']:
            hits.append(r)
    # 评分 v2 的候选也进返回列表（带 ev，供 T2.4 dispatch 用）；legacy 下这一支不执行
    if score_mode != 'legacy':
        seen = {id(h) for h in hits}
        for r in results:
            if (r.get('ev') or {}).get('verdict') == 'candidate' and id(r) not in seen:
                hits.append(r)
    if hits:
        print(f'[龙头通道] 本轮推送候选 {len(hits)} 只')
    pf = [r for r in results if r['pattern_first']]
    if pf:
        print(f'[龙头通道] 本轮形态成立 {len(pf)} 只: '
              + ', '.join(f"{r['name']}({r['symbol']}) {r['pattern_first']['t']}" for r in pf[:6]))
    return hits


def run_scan() -> List[Dict]:
    """调度器入口：影子期只落库；推送需同时打开 SHADOW=False 与 PUSH_ENABLED=True。"""
    if not INTRADAY_DRAGON_ENABLED:
        return []
    t0 = time.time()
    try:
        hits = scan_once()
    except Exception:
        # T2.5：失败也要记一笔（开关关时 note_scan 是空操作）
        try:
            from services.pick import dragon_risk
            dragon_risk.note_scan(False)
        except Exception:
            pass
        raise
    try:
        from services.pick import dragon_risk
        dragon_risk.note_scan(True)
    except Exception:
        pass
    cost = round(time.time() - t0, 2)
    if hits:
        legacy_hits = [h for h in hits if h.get('trigger')]
        if legacy_hits:
            print(f'[龙头通道 {datetime.now():%H:%M:%S}] 触发 {len(legacy_hits)} 只（{cost}s）: '
                  + ', '.join(f"{h['name']}({h['symbol']}) {h['trigger']['score']}分"
                              for h in legacy_hits))
        push_mode = INTRADAY_DRAGON_PUSH_MODE
        if push_mode != 'legacy':
            # T2.4：候选 → 两臂计划 → 复核/额度 → dry 落库或实发；异常不影响主链路
            try:
                from services.pick import dragon_push
                st = dragon_push.dispatch(hits)
                print(f"[龙头推送 {datetime.now():%H:%M:%S}] {st['mode']} 候选{st['rows']} "
                      f"计划{st['plans']} dry{st['dry']} 发{st['sent']} "
                      f"观察{st['observe']} 额度挡{st['blocked']} 风控挡{st['risk_blocked']}"
                      + (f" 跳过:{st['skipped']}" if st.get('skipped') else ''))
            except Exception as p_err:
                print(f'[龙头推送] 异常（不影响主链路）: {type(p_err).__name__}: {p_err}')
        elif INTRADAY_DRAGON_PUSH_ENABLED and not INTRADAY_DRAGON_SHADOW:
            _push_hits(hits)
    return hits


def _push_hits(hits: List[Dict]):
    """未封板前推送（影子期不执行）。去重与冷却都落 dragon_alert_log。"""
    from services.support.push import push_wecom_webhook
    hits = [h for h in hits if h.get('trigger')]      # v2 候选没有 legacy 触发点，不进这条老路
    if not hits:
        return
    today = datetime.now().strftime('%Y-%m-%d')
    conn = get_db_conn()
    try:
        sent = conn.execute('SELECT COUNT(*) FROM dragon_alert_log WHERE trade_date=?',
                            (today,)).fetchone()[0]
        if sent >= INTRADAY_DRAGON_DAILY_LIMIT:
            return
        lines = [f'【龙头通道】{datetime.now():%H:%M:%S}']
        for h in hits:
            sym = h['symbol']
            row = conn.execute(
                'SELECT COUNT(*), MAX(pushed_at) FROM dragon_alert_log WHERE trade_date=? AND symbol=?',
                (today, sym)).fetchone()
            if row[0] >= INTRADAY_DRAGON_MAX_PER_STOCK:
                continue
            if row[1]:
                last = datetime.strptime(row[1], '%Y-%m-%d %H:%M:%S')
                if (datetime.now() - last).total_seconds() < INTRADAY_DRAGON_COOLDOWN_SEC:
                    continue
            pt = h['trigger']
            lines.append(f"{h['name']}({sym}) {pt['score']}分 现价{pt['price']} "
                         f"涨{pt['gain_open']:+.1f}% 距涨停{pt['room']:.1f}% — {pt['t']}")
            conn.execute('INSERT OR IGNORE INTO dragon_alert_log VALUES (?,?,?,?,?)',
                         (today, sym, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                          pt['score'], pt['stage']))
        conn.commit()
        if len(lines) > 1:
            push_wecom_webhook('\n'.join(lines))
    finally:
        conn.close()


def in_window(hm: str) -> bool:
    return INTRADAY_DRAGON_START <= hm <= INTRADAY_DRAGON_END


def interval_sec() -> int:
    """调度器该等多久。T2.5 的频率状态机开关关时原样返回现行 15s。"""
    try:
        from services.pick import dragon_risk
        return int(dragon_risk.current_interval_sec())
    except Exception:
        return int(INTRADAY_DRAGON_INTERVAL_SEC)
