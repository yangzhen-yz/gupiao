"""dragon_score.py - 龙头通道评分 v2 + 结构 + 流动性（清单 T2.3，影子）

职责
----
夹在 T2.2 特征层与 T2.4 推送层之间，只回答三个问题，且顺序固定（G3.1）：

    硬排除 → 涨幅/room → 流动性 → 结构 → 评分 → （T2.4 复核）→ （T2.4 额度）

1. `score_v2`：H2.1 公式 `100 × Σ(w_i × pct_i) / Σw_i`；
   缺失因子**不填中档**，直接从分母剔除；覆盖率 <60% 返回 None；封板返回 −100。
2. 结构：VWAP 占比（09:45 起算）、假破 2 分钟内收回、停顿换手 2~5 分钟、room 0.8%~2.0%。
3. 流动性：5 分钟额、价差、卖一/买一量比；阈值优先 20 日影子分位，
   但**只收紧不放松**（不得低于 3000 万、不得高于 0.30%），样本不足标 `fallback_config`。

三条硬约束
----------
- **评分只用于同档排序**：`evaluate()` 先过门，再算分；硬排除/涨幅不符的票
  `scored=False`，一分都不算。分数永远不能把 `watch` 抬成 `candidate`。
- **`MIN_SCORE` 默认 0**：`score >= 70` 只写进 `shadow70` 做对照，不是线上线。
- **默认 legacy**：`INTRADAY_DRAGON_SCORE_MODE='legacy'` 时调用方不进入本模块，
  A 臂逐票 diff=0。`v2_live` 在 20 日影子对照前直接报错，不偷偷上线。

pct 的口径
----------
`pct_i` 是**同档排序用的归一化尺度**（0~100），不是概率、不是阈值、不进推送判据。
当前用固定锚点（`ANCHORS`，见下）逐段线性映射；锚点来自 B1/G2 的形态定义与
T2.1 的四道粗筛，等影子样本够了再换经验分位（`pct_src` 会跟着变）。
它不是"分数越高越该买"的背书 —— B3 已经把这条路否掉了。
"""
import math
import time
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from app.config import (INTRADAY_DRAGON_ASK_BID_MAX, INTRADAY_DRAGON_B_AMOUNT_5M,
                        INTRADAY_DRAGON_B_GAIN_HI, INTRADAY_DRAGON_B_GAIN_LO,
                        INTRADAY_DRAGON_B_SPREAD_MAX, INTRADAY_DRAGON_FEATURE_MIN_COVERAGE,
                        INTRADAY_DRAGON_GAP_MAX, INTRADAY_DRAGON_GAP_PENALTY,
                        INTRADAY_DRAGON_LIQ_AMOUNT_Q, INTRADAY_DRAGON_LIQ_ASK_BID_Q,
                        INTRADAY_DRAGON_LIQ_MIN_SAMPLES,
                        INTRADAY_DRAGON_LIQ_SAMPLE_EVERY_MIN,
                        INTRADAY_DRAGON_LIQ_SHADOW_DAYS, INTRADAY_DRAGON_LIQ_SPREAD_Q,
                        INTRADAY_DRAGON_MAX_ROOM_PCT, INTRADAY_DRAGON_MIN_ROOM_PCT,
                        INTRADAY_DRAGON_MIN_SCORE, INTRADAY_DRAGON_PAUSE_MAX,
                        INTRADAY_DRAGON_PAUSE_MIN, INTRADAY_DRAGON_SCORE_MODE,
                        INTRADAY_DRAGON_VWAP_SHARE_FROM, INTRADAY_DRAGON_VWAP_SHARE_MIN)
from db.database import get_db_conn
from services.pick.dragon_features import REQUIRED_FEATURES

SCORE_VERSION = 't2.3-2026-11-01'
MODES = ('legacy', 'v2_shadow')
SEAL_TOL = 0.01                  # 与 H2.1 第 3 条一致：price ≥ limit_up − 0.01 即视为封板

# 11 个必需特征默认等权：盘中因子还没有可成交 IC 样本（T0.4 是日线口径），
# 先不臆造权重；等 T3 影子攒够再按 H1.2 的 `sign(ic) × min(|t|,2.5) × coverage` 重估。
FACTORS = tuple(REQUIRED_FEATURES)
DEFAULT_WEIGHTS = {f: 1.0 for f in FACTORS}

# 归一化锚点：(x, y) 逐段线性；x 递增。分高的因子按其经济含义定向，
# 不做"线性越高越好"的默认假设（room 在中段最高、gap/spread 越低越高）。
ANCHORS: Dict[str, Tuple[Tuple[float, float], ...]] = {
    'chg_from_open': ((0.0, 0.0), (2.0, 50.0), (5.0, 100.0)),      # >5 封顶，追高交给 room/gap
    'gap': ((0.0, 100.0), (3.0, 55.0), (7.0, 0.0)),                 # 越低越好
    'room': ((0.0, 50.0), (0.8, 100.0), (2.0, 100.0), (3.5, 0.0)),  # 0.8~2.0 满分，太远无意义
    'vol_ratio_same_time': ((1.0, 0.0), (1.5, 25.0), (3.0, 100.0)),
    'amount_5m': ((300.0, 0.0), (3000.0, 50.0), (15000.0, 100.0)),  # 对数插值
    'spread': ((0.0, 100.0), (0.3, 60.0), (0.6, 0.0)),              # 越低越好
    'vwap_share': ((0.50, 0.0), (0.70, 55.0), (0.90, 100.0)),
    'vwap_dist': ((-1.0, 0.0), (0.0, 40.0), (2.0, 100.0)),
    'fake_break': (),                                                # 特殊：看真假破
    'ask_bid_ratio': ((0.3, 100.0), (1.0, 60.0), (2.0, 0.0)),       # 越低越好（卖压小）
    'pause_dur': ((0.0, 60.0), (2.0, 100.0), (5.0, 100.0), (8.0, 30.0)),
}
LOG_SCALE_FACTORS = ('amount_5m',)


def ensure_mode(mode: Optional[str] = None) -> str:
    """校验开关；v2_live 在影子对照完成前明确拒绝，不静默降级。"""
    m = (mode or INTRADAY_DRAGON_SCORE_MODE or 'legacy').strip()
    if m not in MODES:
        raise ValueError(
            f"INTRADAY_DRAGON_SCORE_MODE={m} 不支持；"
            f"当前只允许 {MODES}（v2_live 需 20 交易日影子对照后再评估）")
    return m


# ---------- 归一化尺度 ----------

def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _piecewise(x: float, pts: Sequence[Tuple[float, float]],
               log_x: bool = False) -> float:
    """逐段线性，超出两端取端点 y。pts 的 x 必须递增。"""
    if not pts:
        return 0.0
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    xx = math.log10(max(x, 1e-9)) if log_x else x
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        a = math.log10(max(x0, 1e-9)) if log_x else x0
        b = math.log10(max(x1, 1e-9)) if log_x else x1
        if a <= xx <= b:
            if b == a:
                return y1
            return y0 + (y1 - y0) * (xx - a) / (b - a)
    return pts[-1][1]


def pct_of(name: str, value, ctx: Optional[Dict] = None) -> Optional[float]:
    """单因子 → 0~100 排序分。缺失一律 None（不填中档）。"""
    if value is None:
        return None
    ctx = ctx or {}
    if name == 'fake_break':
        if bool(value):
            return 100.0
        breach = ctx.get('breach_pct')
        if breach is not None and float(breach) <= 0:
            return 80.0                      # 从未跌破，不算差
        return 0.0                           # 跌破且未在 2 分钟内收回
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    pts = ANCHORS.get(name)
    if pts:
        return round(_clamp(_piecewise(v, pts, log_x=name in LOG_SCALE_FACTORS)), 1)
    # 没有锚点的因子不猜：从分母剔除，由 coverage 反映
    return None


# ---------- score_v2 ----------

def _feature_dict(points: Dict) -> Dict:
    if isinstance(points.get('features'), dict):
        return points['features']
    return points


def score_detail(points: Dict, factors: Optional[Sequence[str]] = None,
                 weights: Optional[Dict[str, float]] = None,
                 cfg: Optional[Dict] = None) -> Dict:
    """H2.1 公式的明细版。缺失因子从分母剔除；封板 −100；覆盖率不足 None。"""
    C = cfg or {}
    f = _feature_dict(points)
    factors = tuple(factors or FACTORS)
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update({k: float(v) for k, v in weights.items() if k in w})

    price = points.get('price')
    lim = points.get('limit_up')
    if price and lim and float(price) >= float(lim) - SEAL_TOL:
        return {'score': -100.0, 'sealed': True, 'parts': {}, 'weights_used': {},
                'missing': list(FACTORS), 'coverage': 0.0, 'den': 0.0,
                'penalties': {}, 'pct_src': 'anchor', 'score_version': SCORE_VERSION}

    ctx = {'breach_pct': points.get('breach_pct')}
    parts: Dict[str, float] = {}
    parts_weight: Dict[str, float] = {}
    for name in factors:
        p = pct_of(name, f.get(name), ctx)
        if p is None:
            continue
        pw = float(w.get(name, 0.0) or 0.0)
        if pw <= 0:
            continue
        parts[name] = p
        parts_weight[name] = pw

    missing = [k for k in FACTORS if f.get(k) is None]
    cov = points.get('coverage')
    if cov is None:
        cov = (len(FACTORS) - len(missing)) / len(FACTORS)
    cov = float(cov)
    cov_min = float(C.get('coverage_min', INTRADAY_DRAGON_FEATURE_MIN_COVERAGE))

    if cov + 1e-9 < cov_min:
        return {'score': None, 'sealed': False, 'parts': parts,
                'weights_used': parts_weight, 'missing': missing, 'coverage': round(cov, 3),
                'den': 0.0, 'penalties': {}, 'reason': 'coverage_low',
                'pct_src': 'anchor', 'score_version': SCORE_VERSION}

    den = sum(parts_weight.values())
    if den <= 0:
        return {'score': None, 'sealed': False, 'parts': parts,
                'weights_used': parts_weight, 'missing': missing, 'coverage': round(cov, 3),
                'den': 0.0, 'penalties': {}, 'reason': 'no_weighted_factor',
                'pct_src': 'anchor', 'score_version': SCORE_VERSION}

    raw = sum(parts[k] * parts_weight[k] for k in parts) / den
    penalties: Dict[str, float] = {}
    gap = f.get('gap')
    gap_max = float(C.get('gap_max', INTRADAY_DRAGON_GAP_MAX))
    if gap is not None and float(gap) > gap_max:
        penalties['gap_high'] = float(C.get('gap_penalty', INTRADAY_DRAGON_GAP_PENALTY))
    score = raw - sum(penalties.values())
    score = _clamp(score)
    return {'score': round(score, 1), 'sealed': False, 'parts': parts,
            'weights_used': parts_weight, 'missing': missing, 'coverage': round(cov, 3),
            'den': round(den, 4), 'penalties': penalties, 'raw': round(raw, 2),
            'pct_src': 'anchor', 'score_version': SCORE_VERSION}


def _split_score_args(points: Dict, factors) -> Tuple[Dict, Optional[Sequence[str]]]:
    """T4.3 兼容两种第二参数：dict=因子值（并入 `features`）；序列/None=旧的名字过滤。"""
    if isinstance(factors, dict):
        merged = dict(points or {})
        feats = merged.get('features')
        feats = dict(feats) if isinstance(feats, dict) else {}
        feats.update(factors)
        merged['features'] = feats
        return merged, None
    return points, factors


def score_v2(points: Dict,
             factors: dict | Sequence[str] | None = None,
             *,
             weights: Dict[str, float] | None = None,
             cfg: Dict | None = None) -> Tuple[float | None, Dict]:
    """清单 T4.3 的冻结签名：纯计算，不写库、不推送。

    Args:
        points: 行情上下文（`price` / `limit_up` / `coverage` / `breach_pct`），
            可带 `features`。
        factors: 因子值 dict（清单签名，并入 `points['features']`）；
            兼容旧调用：因子名字序列或 None 时按名字过滤。
        weights: 仅关键字；覆盖 `DEFAULT_WEIGHTS` 里的部分因子权重。
        cfg: 仅关键字；阈值覆盖（`coverage_min` / `gap_max` / `gap_penalty`）。

    Returns:
        `(score, detail)`：封板 `(-100.0, {...})`；覆盖率 <60% `(None, {...})`，
        只记录不打分；缺失因子不填中档，直接从分母剔除。
    """
    pts, names = _split_score_args(points, factors)
    detail = score_detail(pts, names, weights, cfg)
    return detail.get('score'), detail


# ---------- 结构 ----------

def _hm(t) -> str:
    s = str(t or '').strip()
    if len(s) == 4 and s.isdigit():
        return f'{s[:2]}:{s[2:]}'
    return s


def _cond(name: str, cfg: Optional[Dict] = None) -> str:
    """T2.6 开关矩阵：live / shadow / off / unavailable。读失败按 live，保持现行行为。"""
    try:
        from services.pick import dragon_shadow
        return dragon_shadow.cond_state(name, cfg)
    except Exception:
        return 'live'


def vwap_share_since(bars: Sequence[Dict], i: int, start_hm: str) -> Optional[float]:
    """从 `start_hm` 那一分钟起，收盘价 ≥ VWAP 的分钟数 / 总分钟数。

    bars[0] 是集合竞价合并的那根，不参与；`i` 小于起点时返回 None（还没到起算时刻）。
    """
    if i < 1 or len(bars) < i + 1:
        return None
    start = None
    for j in range(1, i + 1):
        if _hm(bars[j].get('t')) >= start_hm:
            start = j
            break
    if start is None or i < start:
        return None
    n = i - start + 1
    above = sum(1 for j in range(start, i + 1) if bars[j]['price'] >= bars[j]['vwap'])
    return round(above / n, 3)


def structure_check(feat: Dict, bars: Optional[Sequence[Dict]] = None,
                    cfg: Optional[Dict] = None) -> Dict:
    """四条件：VWAP 占比、假破、停顿换手、room。任一 pending → pending，任一 fail → fail。

    没有 bars 时退化为特征层存的"开盘至今"占比（`share_from='09:30'`），
    仍要求 ≥0.70；口径更严，但不是 09:45 起算，结果里标 `degraded=True`。
    """
    C = cfg or {}
    f = feat.get('features') or {}
    share_min = float(C.get('vwap_share_min', INTRADAY_DRAGON_VWAP_SHARE_MIN))
    start_hm = str(C.get('vwap_share_from', INTRADAY_DRAGON_VWAP_SHARE_FROM))
    pause_lo = int(C.get('pause_min', INTRADAY_DRAGON_PAUSE_MIN))
    pause_hi = int(C.get('pause_max', INTRADAY_DRAGON_PAUSE_MAX))
    room_lo = float(C.get('room_min', INTRADAY_DRAGON_MIN_ROOM_PCT))
    room_hi = float(C.get('room_max', INTRADAY_DRAGON_MAX_ROOM_PCT))

    degraded = False
    if bars:
        share = vwap_share_since(bars, int(feat.get('bar_count', len(bars))) - 1, start_hm)
        share_from = start_hm
    else:
        share = f.get('vwap_share')
        share_from = '09:30'
        degraded = True

    checks: Dict[str, Dict] = {}

    # T2.6：VWAP 70% 是七条条件之一。off 不检查；shadow 只记录不拦；live 照旧拦。
    vwap_state = _cond('vwap70', C)
    if share is None:
        checks['vwap_share'] = {'ok': None, 'value': None, 'reason': 'before_start'}
    elif float(share) >= share_min:
        checks['vwap_share'] = {'ok': True, 'value': share, 'reason': ''}
    elif vwap_state == 'live':
        checks['vwap_share'] = {'ok': False, 'value': share, 'reason': 'share_low'}
    elif vwap_state == 'shadow':
        checks['vwap_share'] = {'ok': True, 'value': share,
                                'reason': 'shadow_share_low'}
    else:
        checks['vwap_share'] = {'ok': True, 'value': share, 'reason': 'switch_off'}

    breach = feat.get('breach_pct')
    fake = f.get('fake_break')
    if breach is None:
        checks['fake_break'] = {'ok': None, 'value': None, 'reason': 'no_breach_data'}
    elif float(breach) <= 1e-9:
        checks['fake_break'] = {'ok': True, 'value': False, 'reason': 'never_breached'}
    elif fake is None:
        checks['fake_break'] = {'ok': None, 'value': None, 'reason': 'no_breach_data'}
    else:
        checks['fake_break'] = {'ok': bool(fake), 'value': bool(fake),
                                'reason': '' if fake else 'breach_not_recovered'}

    pause = f.get('pause_dur')
    if pause is None:
        checks['pause'] = {'ok': None, 'value': None, 'reason': 'no_pause_data'}
    else:
        ok = pause_lo <= int(pause) <= pause_hi
        checks['pause'] = {'ok': ok, 'value': int(pause),
                           'reason': '' if ok else 'pause_out_of_range'}

    room = f.get('room')
    if room is None:
        checks['room'] = {'ok': None, 'value': None, 'reason': 'no_room'}
    else:
        ok = room_lo <= float(room) <= room_hi
        checks['room'] = {'ok': ok, 'value': float(room),
                          'reason': '' if ok else 'room_out_of_range'}

    pending = [k for k, v in checks.items() if v['ok'] is None]
    failed = [k for k, v in checks.items() if v['ok'] is False]
    if failed:
        status = 'fail'
    elif pending:
        status = 'pending'
    else:
        status = 'pass'
    return {'status': status, 'checks': checks, 'pending': pending, 'failed': failed,
            'share_from': share_from, 'degraded': degraded}


# ---------- 流动性 ----------

def _quantile(sorted_vals: Sequence[float], q: float) -> Optional[float]:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    q = min(1.0, max(0.0, q))
    pos = (len(sorted_vals) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(sorted_vals[lo])
    return float(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo))


_LIQ_CACHE: Dict[str, object] = {'key': None, 'ts': 0.0, 'val': None}


def load_liquidity_thresholds(days: Optional[int] = None, today: Optional[str] = None,
                              force: bool = False, cache_sec: float = 1800.0) -> Dict:
    """20 日影子分位 → 流动性阈值。样本不足回退配置，来源如实标注。

    只收紧不放松：
      amount_min = max(3000 万, 影子 q20)
      spread_max = min(0.30%, 影子 q80)
      ask_bid_max = 影子 q80（无配置上限，样本不足才用 ASK_BID_MAX=2.0）
    """
    days = int(days or INTRADAY_DRAGON_LIQ_SHADOW_DAYS)
    today = today or datetime.now().strftime('%Y-%m-%d')
    key = f'{days}|{today}|{INTRADAY_DRAGON_LIQ_SAMPLE_EVERY_MIN}'
    now = time.time()
    if (not force and _LIQ_CACHE['key'] == key and _LIQ_CACHE['val']
            and now - float(_LIQ_CACHE['ts'] or 0.0) < cache_sec):
        return dict(_LIQ_CACHE['val'])                       # type: ignore[arg-type]

    fallback = {
        'amount_min': float(INTRADAY_DRAGON_B_AMOUNT_5M),
        'spread_max': float(INTRADAY_DRAGON_B_SPREAD_MAX),
        'ask_bid_max': float(INTRADAY_DRAGON_ASK_BID_MAX),
        'source': 'fallback_config', 'n': 0, 'window': (None, today),
        'quantiles': {}, 'notes': ['影子样本不足，回退 T2.1/配置阈值'],
    }
    try:
        conn = get_db_conn()
        try:
            dates = [r[0] for r in conn.execute(
                'SELECT DISTINCT trade_date FROM dragon_features '
                'WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT ?',
                (today, days)).fetchall()]
            if not dates:
                _LIQ_CACHE.update({'key': key, 'ts': now, 'val': dict(fallback)})
                return dict(fallback)
            start = min(dates)
            step = max(1, int(INTRADAY_DRAGON_LIQ_SAMPLE_EVERY_MIN))
            rows = conn.execute(
                'SELECT amount_5m, spread, ask_bid_ratio FROM dragon_features '
                'WHERE trade_date >= ? AND trade_date <= ? AND score_ready = 1 '
                'AND CAST(substr(ts, 4, 2) AS INTEGER) % ? = 0 LIMIT 50000',
                (start, today, step)).fetchall()
        finally:
            conn.close()
    except Exception as e:
        fallback['notes'] = [f'读影子分位失败 {type(e).__name__}: {e}']
        return fallback

    amt = sorted(float(r[0]) for r in rows if r[0] is not None)
    spr = sorted(float(r[1]) for r in rows if r[1] is not None)
    ab = sorted(float(r[2]) for r in rows if r[2] is not None)
    n = len(rows)
    min_n = int(INTRADAY_DRAGON_LIQ_MIN_SAMPLES)
    if n < min_n:
        fallback.update({'n': n, 'window': (start, today),
                         'quantiles': {'amount_5m_q20': _quantile(amt, INTRADAY_DRAGON_LIQ_AMOUNT_Q),
                                       'spread_q80': _quantile(spr, INTRADAY_DRAGON_LIQ_SPREAD_Q),
                                       'ask_bid_q80': _quantile(ab, INTRADAY_DRAGON_LIQ_ASK_BID_Q)},
                         'notes': [f'影子样本 {n} < {min_n}，回退配置阈值']})
        _LIQ_CACHE.update({'key': key, 'ts': now, 'val': dict(fallback)})
        return dict(fallback)

    q_amt = _quantile(amt, INTRADAY_DRAGON_LIQ_AMOUNT_Q)
    q_spr = _quantile(spr, INTRADAY_DRAGON_LIQ_SPREAD_Q)
    q_ab = _quantile(ab, INTRADAY_DRAGON_LIQ_ASK_BID_Q)
    val = {
        'amount_min': max(float(INTRADAY_DRAGON_B_AMOUNT_5M), float(q_amt or 0.0)),
        'spread_max': min(float(INTRADAY_DRAGON_B_SPREAD_MAX), float(q_spr or INTRADAY_DRAGON_B_SPREAD_MAX)),
        'ask_bid_max': float(q_ab if q_ab is not None else INTRADAY_DRAGON_ASK_BID_MAX),
        'source': 'shadow_quantile', 'n': n, 'window': (start, today),
        'quantiles': {'amount_5m_q20': q_amt, 'spread_q80': q_spr, 'ask_bid_q80': q_ab},
        'notes': [f'{len(dates)} 个交易日 / {n} 个抽样点；只收紧不放松'],
    }
    _LIQ_CACHE.update({'key': key, 'ts': now, 'val': dict(val)})
    return dict(val)


def liquidity_check(feat: Dict, thresholds: Optional[Dict] = None,
                    cfg: Optional[Dict] = None) -> Dict:
    """三档：pass / unknown（数据缺，只记）/ fail。缺 spread 不冒充通过。"""
    f = feat.get('features') or {}
    th = thresholds or load_liquidity_thresholds()
    amount_min = float((cfg or {}).get('amount_min', th['amount_min']))
    spread_max = float((cfg or {}).get('spread_max', th['spread_max']))
    ab_max = float((cfg or {}).get('ask_bid_max', th['ask_bid_max']))

    checks: Dict[str, Dict] = {}
    amt = f.get('amount_5m')
    if amt is None:
        checks['amount_5m'] = {'ok': None, 'value': None, 'threshold': amount_min,
                               'reason': 'amount_missing'}
    else:
        checks['amount_5m'] = {'ok': float(amt) >= amount_min, 'value': float(amt),
                               'threshold': amount_min,
                               'reason': '' if float(amt) >= amount_min else 'amount_low'}

    spr = f.get('spread')
    if spr is None:
        checks['spread'] = {'ok': None, 'value': None, 'threshold': spread_max,
                            'reason': 'spread_missing'}
    else:
        checks['spread'] = {'ok': float(spr) <= spread_max, 'value': float(spr),
                            'threshold': spread_max,
                            'reason': '' if float(spr) <= spread_max else 'spread_high'}

    ab = f.get('ask_bid_ratio')
    if ab is None:
        checks['ask_bid_ratio'] = {'ok': None, 'value': None, 'threshold': ab_max,
                                   'reason': 'ask_bid_missing'}
    else:
        checks['ask_bid_ratio'] = {'ok': float(ab) <= ab_max, 'value': float(ab),
                                   'threshold': ab_max,
                                   'reason': '' if float(ab) <= ab_max else 'ask_bid_high'}

    if any(v['ok'] is False for v in checks.values()):
        status = 'fail'
    elif any(v['ok'] is None for v in checks.values()):
        status = 'unknown'
    else:
        status = 'pass'
    return {'status': status, 'checks': checks,
            'thresholds': {'amount_min': amount_min, 'spread_max': spread_max,
                           'ask_bid_max': ab_max},
            'threshold_source': th.get('source'), 'n': th.get('n')}


# ---------- G3.1 判定 ----------

def _main_board(symbol: str) -> bool:
    b = str(symbol or '')
    if b.startswith(('sh', 'sz')):
        b = b[2:]
    return b.startswith(('60', '00'))


def evaluate(row: Dict, feat: Optional[Dict], bars: Optional[Sequence[Dict]] = None,
             legacy_score: Optional[float] = None, liq: Optional[Dict] = None,
             cfg: Optional[Dict] = None, now_hm: str = '') -> Dict:
    """按 G3.1 顺序判定一只票：硬排除 → 涨幅/room → 流动性 → 结构 → 评分。

    返回 `verdict`（reject/watch/candidate）与 `gate`（第一道挡住的关口）。
    `candidate` 仍只是"可进 T2.4 推送前复核"，不等于已推送。
    硬排除与涨幅不符的票 `scored=False`，不进入评分。
    """
    C = cfg or {}
    row = row or {}
    f = (feat or {}).get('features') or {}
    sym = str(row.get('symbol') or '')
    name = str(row.get('name') or '')
    price = float(row.get('price') or (feat or {}).get('price') or 0)
    lim = float(row.get('limit_up') or (feat or {}).get('limit_up') or 0)
    gain_lo = float(C.get('gain_lo', INTRADAY_DRAGON_B_GAIN_LO))
    gain_hi = float(C.get('gain_hi', INTRADAY_DRAGON_B_GAIN_HI))
    min_score = float(C.get('min_score', INTRADAY_DRAGON_MIN_SCORE))

    out: Dict = {
        'symbol': sym, 'name': name, 'ts': (feat or {}).get('ts') or now_hm,
        'price': price, 'gate': 'pass', 'verdict': 'candidate', 'reasons': [],
        'shadow_notes': [],
        'scored': False, 'score_v2': None, 'score_legacy': legacy_score,
        'score_detail': None, 'shadow70': False, 'min_score': min_score,
        'structure': None, 'liquidity': None, 'score_version': SCORE_VERSION,
        'order': ['hard', 'gain_room', 'liquidity', 'structure', 'score'],
    }

    # 1) 硬排除：不进入任何统计、不算分
    hard = []
    if not _main_board(sym):
        hard.append('not_main')
    if 'ST' in name or '退' in name:
        hard.append('st')
    if price <= 0:
        hard.append('no_price')
    if row.get('new_listing'):
        hard.append('new_listing')
    if lim and price >= lim - SEAL_TOL:
        hard.append('sealed')
    if row.get('limit_down'):
        hard.append('limit_down')
    if hard:
        out.update({'gate': 'hard', 'verdict': 'reject', 'reasons': hard,
                    'scored': False, 'score_v2': None})
        return out

    # 2) 涨幅与空间（涨幅不符直接丢；room 不符只记录）
    gain = f.get('chg_from_open')
    if gain is None:
        out.update({'gate': 'gain_room', 'verdict': 'watch',
                    'reasons': ['chg_from_open_missing']})
        return out
    if float(gain) < gain_lo or float(gain) > gain_hi:
        # T2.6：shadow 只记一笔，off 连记录都不写（on=False = 不算），默认 live 与今天一致
        gain_state = _cond('gain_5m', C)
        if gain_state == 'live':
            out.update({'gate': 'gain_room', 'verdict': 'reject',
                        'reasons': ['gain_out_of_range'], 'scored': False})
            return out
        if gain_state == 'shadow':
            out['shadow_notes'].append('gain_out_of_range')

    watch_reasons: List[str] = []
    room = f.get('room')
    room_lo = float(C.get('room_min', INTRADAY_DRAGON_MIN_ROOM_PCT))
    room_hi = float(C.get('room_max', INTRADAY_DRAGON_MAX_ROOM_PCT))
    if room is None:
        watch_reasons.append('room_missing')
    elif not (room_lo <= float(room) <= room_hi):
        watch_reasons.append('room_out_of_range')

    # 3) 流动性
    lq = liquidity_check(feat or {}, liq, C)
    out['liquidity'] = lq
    if lq['status'] == 'fail':
        watch_reasons.extend(v['reason'] for v in lq['checks'].values() if v['ok'] is False)
    elif lq['status'] == 'unknown':
        watch_reasons.extend(v['reason'] for v in lq['checks'].values() if v['ok'] is None)

    # 4) 结构
    st = structure_check(feat or {}, bars, C)
    out['structure'] = st
    if st['status'] != 'pass':
        watch_reasons.extend(v['reason'] for k, v in st['checks'].items()
                             if v['ok'] is not True and v['reason'])

    # 5) 评分：只排序，不绕门
    detail = score_detail({
        'features': f, 'price': price, 'limit_up': lim,
        'breach_pct': (feat or {}).get('breach_pct'),
        'coverage': (feat or {}).get('coverage'),
    }, cfg=C)
    out['score_detail'] = detail
    out['scored'] = True
    out['score_v2'] = detail['score']
    if detail.get('sealed'):
        # score_v2 的封板语义；evaluate 正常走不到这里（硬排除已短路）
        out.update({'gate': 'hard', 'verdict': 'reject', 'reasons': ['sealed']})
        return out
    if detail['score'] is None:
        watch_reasons.append(detail.get('reason') or 'no_score')
    elif float(detail['score']) < min_score:
        watch_reasons.append('score_low')
    out['shadow70'] = bool(detail['score'] is not None and float(detail['score']) >= 70.0)

    if watch_reasons:
        out['gate'] = ('gain_room' if any(r.startswith('room') or r == 'room_missing'
                                          for r in watch_reasons)
                       else 'liquidity' if lq['status'] != 'pass'
                       else 'structure' if st['status'] != 'pass'
                       else 'score')
        out['verdict'] = 'watch'
        out['reasons'] = list(dict.fromkeys(watch_reasons))   # 同一原因只报一次
    return out


def sort_candidates(rows: List[Dict]) -> List[Dict]:
    """同档排序：score_v2 降序 → 相对今开涨幅降序 → 代码。None 排最后。"""
    def key(r: Dict):
        score = r.get('score_v2')
        s = float(score) if isinstance(score, (int, float)) else -1e9
        f = (r.get('feat') or {}).get('features') or {}
        gain = f.get('chg_from_open')
        return (-s, -(float(gain) if gain is not None else -1e9), str(r.get('symbol') or ''))
    return sorted(rows, key=key)


def trigger_flags(ev: Dict, cfg: Optional[Dict] = None) -> Dict:
    """两套影子触发：MIN_SCORE（默认 0）与 70。两家都只在 candidate 上比较。"""
    C = cfg or {}
    min_score = float(C.get('min_score', INTRADAY_DRAGON_MIN_SCORE))
    score = ev.get('score_v2')
    cand = ev.get('verdict') == 'candidate'
    ok = isinstance(score, (int, float))
    return {
        'candidate': cand,
        'min_score_ok': bool(cand and ok and float(score) >= min_score),
        'shadow70_ok': bool(cand and ok and float(score) >= 70.0),
        'min_score': min_score,
    }
