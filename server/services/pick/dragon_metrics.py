"""dragon_metrics.py - T3.2 指标与置信区间（纯函数，只读研究）

解决什么
--------
T3.1 回测引擎只会算「n / 胜率 / Wilson / 均值 / t 区间 / 中位 / 盈亏比 / 最差」，
清单 T3.2 要的是一整套**能拿去下结论或否掉结论**的指标：
胜率区间、均值区间（t 与 bootstrap 交叉验证）、中位、盈亏比、最大回撤、吃面率，
成交率/漏单原因、止损率/跳空幅度分开报，超额必须带 n 与区间，
稳定性按日/按情绪/按 gap 分段，并且**每条结论都写清 n 是谁的 n**。

硬约束
------
1. 纯函数：不联网、不写库、不读 `app.config`（阈值由调用方传）。
2. 不替调用方挑样本：传进来什么就算什么；`None` 一律剔除并计入 `n_missing`，
   不填 0、不填中位。
3. n 口径显式：`describe()` 的 n 是**传进来的观测数**（回测里是成交腿数）；
   腿之间可能同日同票重复，所以另有 `daily_cluster()` 把同一天的腿先聚成日均值，
   t 检验要用后者（清单原话：265 是成交腿数，不是独立样本）。
4. 区间一律百分比单位（收益 %、胜率 %），与 T3.1 产物一致，不做 0~1 混用。
"""
import math
import random
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

NOODLE_PCT = -5.0        # 吃面线：与复盘页/`eval_unified` 同一口径（单笔亏 5% 以上）
MIN_SEG_N = 10           # 分段最小样本；不足只记录、不参与"同号"判定
MIN_SEGMENTS = 3         # 清单要求"至少 3 段同号"

_T_TABLE = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
    8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
    15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086, 21: 2.080,
    22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056, 27: 2.052, 28: 2.048,
    29: 2.045, 30: 2.042,
}


def tcrit(df: int) -> float:
    """双侧 95% t 临界值；df>30 用正态 1.96（保守不到 0.2%）。"""
    return _T_TABLE.get(max(1, int(df)), 1.96)


def wilson(k: int, n: int, z: float = 1.96) -> Tuple[Optional[float], Optional[float]]:
    """成功率的 Wilson 区间（百分比）。n=0 返回 (None, None)。"""
    if not n:
        return (None, None)
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, (c - h) * 100), min(100.0, (c + h) * 100))


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def _sd(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((v - m) ** 2 for v in xs) / (len(xs) - 1))


def mean_ci(xs: Sequence[float]) -> Tuple[Optional[float], Optional[float]]:
    """样本均值的 t 区间；n<3 不给区间（标准差没有意义）。"""
    n = len(xs)
    if n < 3:
        return (None, None)
    se = _sd(xs) / math.sqrt(n)
    half = tcrit(n - 1) * se
    m = _mean(xs)
    return (m - half, m + half)


def bootstrap_mean_ci(xs: Sequence[float], iters: int = 2000,
                      seed: int = 7) -> Tuple[Optional[float], Optional[float]]:
    """均值区间的 bootstrap 交叉验证（固定种子，可复现；n<3 不给）。"""
    n = len(xs)
    if n < 3:
        return (None, None)
    rng = random.Random(seed)
    means = []
    for _ in range(int(iters)):
        s = 0.0
        for _ in range(n):
            s += xs[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    lo = means[max(0, int(round(0.025 * (iters - 1))))]
    hi = means[min(iters - 1, int(round(0.975 * (iters - 1))))]
    return (lo, hi)


def _r(v, nd: int = 3):
    return None if v is None else round(float(v), nd)


def describe(vals: Iterable[Optional[float]], label: str = '') -> Dict:
    """一组收益的基础统计。空样本返回 `{'n': 0, 'label': ...}`，不编数字。"""
    raw = list(vals)
    xs = [float(v) for v in raw if v is not None]
    n = len(xs)
    out = {'label': label, 'n': n, 'n_missing': len(raw) - n}
    if not n:
        return out
    win = sum(1 for v in xs if v > 0)
    mean = _mean(xs)
    sd = _sd(xs)
    lo, hi = wilson(win, n)
    mlo, mhi = mean_ci(xs)
    blo, bhi = bootstrap_mean_ci(xs)
    gains = [v for v in xs if v > 0]
    losses = [v for v in xs if v < 0]
    pf = (mean_of(gains) / -mean_of(losses)) if gains and losses else None
    srt = sorted(xs)
    out.update({
        'win': win, 'win_rate': _r(win / n * 100, 1),
        'wilson_lo': _r(lo, 1), 'wilson_hi': _r(hi, 1),
        'mean': _r(mean, 3), 'sd': _r(sd, 3),
        'mean_lo': _r(mlo, 3), 'mean_hi': _r(mhi, 3),
        'boot_lo': _r(blo, 3), 'boot_hi': _r(bhi, 3),
        'median': _r(srt[n // 2], 3), 'pf': (_r(pf, 2) if pf is not None else None),
        'noodle_rate': _r(sum(1 for v in xs if v <= NOODLE_PCT) / n * 100, 1),
        'worst': _r(min(xs), 3), 'best': _r(max(xs), 3),
    })
    return out


def mean_of(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def max_drawdown(returns: Iterable[Optional[float]]) -> Dict:
    """按成交顺序累加净收益（%）的最大回撤（pp）。

    这是**逐笔累加**，不是组合净值：同一天多笔同时计入、没有仓位与并发约束，
    只能用来比较同一批样本下不同规则的相对回撤，不能写成"账户回撤"。
    """
    xs = [float(v) for v in returns if v is not None]
    if not xs:
        return {'n': 0, 'equity_end': None, 'mdd': None}
    eq = 0.0
    peak = 0.0
    mdd = 0.0
    at = 0
    for i, v in enumerate(xs, 1):
        eq += v
        peak = max(peak, eq)
        if peak - eq > mdd:
            mdd = peak - eq
            at = i
    return {'n': len(xs), 'equity_end': _r(eq, 2), 'mdd': _r(mdd, 2), 'mdd_at': at}


def fill_stats(rows: Sequence[Dict]) -> Dict:
    """成交率与漏单原因（分母 = 该配置下的全部触发，包括没成交的）。"""
    n = len(rows)
    reasons: Dict[str, int] = {}
    filled = 0
    filled_no_exit = 0
    for r in rows:
        if r.get('status') == 'filled':
            filled += 1
            if r.get('ret_net') is None:
                filled_no_exit += 1
        else:
            k = r.get('unfilled_reason') or r.get('status') or 'unknown'
            reasons[k] = reasons.get(k, 0) + 1
    return {
        'triggers': n, 'filled': filled, 'fill_rate': _r(filled / n * 100, 1) if n else None,
        'filled_no_exit': filled_no_exit,
        'returnable': filled - filled_no_exit,
        'unfilled_by_reason': dict(sorted(reasons.items())),
    }


def stop_stats(rows: Sequence[Dict]) -> Dict:
    """止损率只在**成交腿**里算；跳空幅度只统计真正止损的腿。

    `gap_slip` 是相对止损价的偏离比例（负数=跳空更差），这里换算成 %。
    """
    filled = [r for r in rows if r.get('status') == 'filled']
    hits = [r for r in filled if r.get('stop_hit')]
    gaps = [float(r['gap_slip']) * 100 for r in hits if r.get('gap_slip') is not None]
    out = {
        'filled': len(filled), 'stop_hits': len(hits),
        'stop_rate': _r(len(hits) / len(filled) * 100, 1) if filled else None,
        'gap_n': len(gaps),
    }
    if gaps:
        srt = sorted(gaps)
        out.update({
            'gap_mean': _r(_mean(gaps), 3),
            'gap_median': _r(srt[len(srt) // 2], 3),
            'gap_p90': _r(srt[min(len(srt) - 1, int(0.9 * (len(srt) - 1)))], 3),
            'gap_worst': _r(min(gaps), 3),
        })
    return out


def gap_bucket(gap) -> str:
    """高开幅度分档（与清单 T3.2 步骤 4 的"分 gap"一致）。"""
    try:
        g = float(gap)
    except (TypeError, ValueError):
        return '未知'
    if g < 0:
        return '低开(<0%)'
    if g < 2:
        return '0~2%'
    if g < 5:
        return '2~5%'
    return '≥5%'


def segment(rows: Sequence[Dict], keyfn: Callable[[Dict], str],
            value_key: str = 'ret_net') -> List[Dict]:
    """按 keyfn 分组，每组跑一次 describe；组按 key 排序，稳定可复现。"""
    buckets: Dict[str, List[Optional[float]]] = {}
    for r in rows:
        b = buckets.setdefault(str(keyfn(r)), [])
        b.append(r.get(value_key))
    return [describe(v, label=k) for k, v in sorted(buckets.items())]


def stability(segs: Sequence[Dict], min_n: int = MIN_SEG_N) -> Dict:
    """至少 3 段、每段 n≥min_n 且均值同号，才算"稳定"。

    不足 3 段或只有 0 段达标时，结论必须是"无法判定"而不是"稳定"。
    """
    usable = [s for s in segs if s.get('n', 0) >= min_n and s.get('mean') is not None]
    pos = [s['label'] for s in usable if s['mean'] > 0]
    neg = [s['label'] for s in usable if s['mean'] < 0]
    same = None
    if len(usable) >= MIN_SEGMENTS:
        same = (len(pos) == len(usable)) or (len(neg) == len(usable))
    if same is True:
        verdict = '同号（可作分段证据，样本仍受总 n 约束）'
    elif same is False:
        verdict = '不同号（分段不稳，视为过拟合风险）'
    else:
        verdict = f'无法判定（达标段数 {len(usable)} < {MIN_SEGMENTS}，或每段 n < {min_n}）'
    return {'min_n': min_n, 'usable': len(usable), 'positive': len(pos),
            'negative': len(neg), 'same_sign': same, 'verdict': verdict,
            'segments': list(segs)}


def paired_diff(a: Sequence[Optional[float]], b: Sequence[Optional[float]]) -> Dict:
    """配对差 a−b 的均值与 t 区间（只保留两边都有的样本）。"""
    diffs = [float(x) - float(y) for x, y in zip(a, b)
             if x is not None and y is not None]
    n = len(diffs)
    if n < 3:
        return {'n': n, 'mean': (_r(_mean(diffs), 3) if diffs else None),
                'mean_lo': None, 'mean_hi': None, 't': None, 'significant': None}
    m = _mean(diffs)
    se = _sd(diffs) / math.sqrt(n)
    tc = tcrit(n - 1)
    t = (m / se) if se > 0 else None
    return {'n': n, 'mean': _r(m, 3),
            'mean_lo': _r(m - tc * se, 3), 'mean_hi': _r(m + tc * se, 3),
            't': (_r(t, 2) if t is not None else None),
            'significant': (abs(t) >= tc) if t is not None else None}


def excess_stats(rows: Sequence[Dict], value_key: str = 'ret_net',
                 bench_key: str = 'benchmark_ret') -> Dict:
    """超额 = 策略净收益 − 同笔基准净收益。缺基准的腿计入 missing，不当 0。

    `strategy_mean` / `bench_mean` 只用**两边都有值**的腿，保证与超额同分母；
    全样本的策略均值请另用 `describe([r[value_key] ...])`，别混。
    """
    diffs = []
    strat = []
    bench = []
    missing = 0
    for r in rows:
        s, b = r.get(value_key), r.get(bench_key)
        if s is None or b is None:
            if s is not None and b is None:
                missing += 1
            continue
        diffs.append(float(s) - float(b))
        strat.append(float(s))
        bench.append(float(b))
    out = describe(diffs, label='excess')
    out['missing_benchmark'] = missing
    out['strategy_mean'] = _r(_mean(strat), 3) if strat else None
    out['bench_mean'] = _r(_mean(bench), 3) if bench else None
    return out


def daily_cluster(rows: Sequence[Dict], value_key: str = 'ret_net',
                  day_key: str = 'day') -> Dict:
    """先按交易日取均值，再对"每日均值"做统计——这才是 t 检验的独立单位。

    返回 `days` 个日均值；`n` 是**天数**，不是成交腿数。
    """
    by_day: Dict[str, List[float]] = {}
    for r in rows:
        v = r.get(value_key)
        if v is None:
            continue
        by_day.setdefault(str(r.get(day_key)), []).append(float(v))
    means = [(_mean(v), k, len(v)) for k, v in sorted(by_day.items())]
    out = describe([m[0] for m in means], label='daily_mean')
    out['n_legs'] = sum(m[2] for m in means)
    out['per_day'] = [{'day': m[1], 'mean': _r(m[0], 3), 'legs': m[2]} for m in means]
    return out


def verdict_gate(days: int, triggers: int, filled: int,
                 min_days: int = 40, min_triggers: int = 120) -> Dict:
    """清单的样本门槛：P1 正式验收要 ≥40 个交易日且 ≥120 个触发。"""
    ok = days >= min_days and triggers >= min_triggers
    return {
        'days': days, 'triggers': triggers, 'filled': filled,
        'min_days': min_days, 'min_triggers': min_triggers,
        'pass': ok,
        'verdict': ('达到样本门槛，可做正式判定' if ok else
                    f'样本不足（{days} 日 / {triggers} 触发，门槛 {min_days} 日 / '
                    f'{min_triggers} 触发），只能写可行性'),
    }
