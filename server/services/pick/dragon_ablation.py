"""dragon_ablation.py - T3.3 消融与参数稳定性（纯函数，只读研究）

职责
----
把"七条条件开关"与"参数 ±20%"拆成可单独验证的最小操作，供
`analysis_out/run_dragon_ablation.py` 组合：

1. **入口门**（板块前 3 / 量比 1.5 / 5 分钟涨幅 2%~8.5% / VWAP 70%）：
   只做过滤。`True` 留、`False` 剔、`None` 不拦（缺失 / 不可用 / 09:45 前 pending）。
   三种状态分开计数，不把 None 当 False。
2. **执行门**（回踩买 / −2% 止损 / 单笔 5% 仓位）：不是行级过滤，
   由调用方换 `select()` 的买法、止损、仓位，本模块只保证选择口径一致。
3. **逐票 diff**：全关配置必须与基线逐笔相等（清单验收项）。
4. **参数扰动**：`perturb_values()` 给 ±20% 的两端；**收紧=可测（子集），
   放松=需要重跑触发层**，本模块不假装放松也能从旧样本里算出来。

硬约束
------
- 纯函数：不联网、不写库、不读 `app.config`（阈值由调用方传）。
- 不替调用方挑样本；被剔除的腿原样返回，供报告逐条列出。
- 缺失一律 `None`，不填 0、不填中位。
"""
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from services.pick import dragon_metrics as metrics

ABLATION_VERSION = 't3.3-2026-11-01'
PERTURB_PCT = 0.20

# 七条条件（与 T2.6 开关矩阵同名）
# layer: entry=行级过滤 / exec=换执行规则 / sizing=换仓位（逐笔收益不变）
# arms: live 实际作用的臂；A 臂的入口门不在 live 路径上（结构判定另有等价条件）
GATES: Tuple[Dict, ...] = (
    {'name': 'sector_top3', 'label': '板块前 3', 'layer': 'entry',
     'arms': ('A', 'B'), 'available': False,
     'note': 'sector_rank 恒 NULL（T0.2），只记不拦'},
    {'name': 'vol_ratio', 'label': '量比 1.5', 'layer': 'entry',
     'arms': ('B',), 'available': True,
     'note': 'live 只在 B 臂入口粗筛；A 臂结构判定不含量比门'},
    {'name': 'gain_5m', 'label': '5 分钟涨幅 2%~8.5%', 'layer': 'entry',
     'arms': ('B',), 'available': True,
     'note': 'live 只在 B 臂入口；A 臂由结构 pat_gain 区间等价约束'},
    {'name': 'vwap70', 'label': 'VWAP 70%', 'layer': 'entry',
     'arms': ('A', 'B'), 'available': True,
     'note': 'live 结构检查，09:45 起算；09:45 前为 pending 不推'},
    {'name': 'pullback', 'label': '回踩买', 'layer': 'exec',
     'arms': ('A', 'B'), 'available': True,
     'note': 'VWAP±0.3% 限价，最多等 180 秒；只出计划不下单'},
    {'name': 'stop', 'label': '−2% 止损', 'layer': 'exec',
     'arms': ('A', 'B'), 'available': True,
     'note': '触价后按下一分钟开盘成交，跳空按开盘价'},
    {'name': 'position', 'label': '单笔 5%', 'layer': 'sizing',
     'arms': ('A', 'B'), 'available': True,
     'note': '仓位不改逐笔收益，只改资金占用；组合层未做'},
)
GATE_NAMES = tuple(g['name'] for g in GATES)
ENTRY_GATES = tuple(g['name'] for g in GATES if g['layer'] == 'entry')
EXEC_GATES = tuple(g['name'] for g in GATES if g['layer'] == 'exec')
KEY_FIELDS = ('day', 'symbol', 'arm', 'trigger_time', 'buy_method',
              'stop_pct', 'exit_rule', 'slippage_pct')


def gate_spec(name: str) -> Dict:
    for g in GATES:
        if g['name'] == name:
            return dict(g)
    raise KeyError(f'未知条件: {name!r}，只能是 {GATE_NAMES}')


def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def gate_value(row: Dict, name: str, cfg: Optional[Dict] = None) -> Optional[bool]:
    """该行在这一条门下的结果：True 命中 / False 不命中 / None 无法判定。

    `None` 的含义按门区分：板块门是数据不可用；VWAP 门是 09:45 前还没有占比
    （live 记 pending，不拦、也不推）；量比/涨幅门是字段缺失。
    执行门与仓位门不是行级过滤，这里一律返回 None，由 `select()` 处理。
    """
    C = cfg or {}
    if name == 'sector_top3':
        return None
    if name == 'vol_ratio':
        v = _f(row.get('vol_ratio'))
        return None if v is None else v >= float(C.get('vol_ratio_min', 1.5))
    if name == 'gain_5m':
        v = _f(row.get('chg_from_open'))
        if v is None:
            return None
        return float(C.get('gain_lo', 2.0)) <= v <= float(C.get('gain_hi', 8.5))
    if name == 'vwap70':
        v = _f(row.get('vwap_share_0945'))
        return None if v is None else v >= float(C.get('vwap_min', 0.70))
    if name in EXEC_GATES or name == 'position':
        return None
    raise KeyError(f'未知条件: {name!r}')


def gate_counts(rows: Sequence[Dict], name: str, cfg: Optional[Dict] = None) -> Dict:
    """逐行计数：命中 / 不命中 / 无法判定；不命中的行原样返回。"""
    hit, miss, unknown = 0, [], 0
    for r in rows:
        v = gate_value(r, name, cfg)
        if v is True:
            hit += 1
        elif v is False:
            miss.append(r)
        else:
            unknown += 1
    return {'name': name, 'n': len(rows), 'hit': hit, 'miss': len(miss),
            'unknown': unknown, 'removed': list(miss)}


def filter_gates(rows: Sequence[Dict], gates: Iterable[str],
                 cfg: Optional[Dict] = None) -> Tuple[List[Dict], Dict[str, List[Dict]]]:
    """依次应用入口门。返回 (保留行, {门名: 被它剔掉的行})。

    门与门之间是"与"：前一条剔掉的行不再进入后一条的计数，
    所以 `removed[name]` 是**增量**剔除，不是单条门的全量命中差。
    顺序固定为 `ENTRY_GATES` 的声明顺序，保证可复现。
    """
    order = [g for g in ENTRY_GATES if g in set(gates)]
    kept = list(rows)
    removed: Dict[str, List[Dict]] = {}
    for name in order:
        kept, rm = _filter_one(kept, name, cfg)
        removed[name] = rm
    return kept, removed


def _filter_one(rows: Sequence[Dict], name: str,
                cfg: Optional[Dict] = None) -> Tuple[List[Dict], List[Dict]]:
    kept, rm = [], []
    for r in rows:
        if gate_value(r, name, cfg) is False:
            rm.append(r)
        else:
            kept.append(r)
    return kept, rm


def select(rows: Sequence[Dict], *, buy: str = 'immediate', stop_pct: float = 2.0,
           exit_rule: str = 'd_1030', slip: float = 0.2, dedup: bool = True,
           gates: Iterable[str] = (), cfg: Optional[Dict] = None) -> List[Dict]:
    """从 T3.1 的全组合逐笔里选出一套配置。

    T3.1 产物是 买法 × 止损 × 出口 × 滑点 的笛卡尔积，所以"换规则"= 换选择条件，
    同一价格路径不重新取数；入口门再在这批触发的行上过滤。
    """
    out = []
    for r in rows:
        if dedup and r.get('dup_dropped'):
            continue
        if r.get('buy_method') != buy:
            continue
        if exit_rule is not None and r.get('exit_rule') != exit_rule:
            continue
        if stop_pct is not None:
            v = _f(r.get('stop_pct'))
            if v is None or abs(v - float(stop_pct)) > 1e-9:
                continue
        if slip is not None:
            v = _f(r.get('slippage_pct'))
            if v is None or abs(v - float(slip)) > 1e-9:
                continue
        out.append(r)
    if gates:
        out, _ = filter_gates(out, gates, cfg)
    return out


def trade_key(row: Dict, ignore: Iterable[str] = ()) -> Tuple:
    """配对键；比较"买法/止损"这类被替换的参数时，把它从键里去掉。"""
    skip = set(ignore)
    return tuple(row.get(k) for k in KEY_FIELDS if k not in skip)


def diff_rows(a: Sequence[Dict], b: Sequence[Dict]) -> Dict:
    """逐票 diff（按 `KEY_FIELDS` 对齐）：全关配置必须与基线 `equal=True`。"""
    ka = {trade_key(r): r for r in a}
    kb = {trade_key(r): r for r in b}
    only_a = sorted(set(ka) - set(kb), key=str)
    only_b = sorted(set(kb) - set(ka), key=str)
    diffs = []
    for k in sorted(set(ka) & set(kb), key=str):
        ra, rb = ka[k], kb[k]
        for f in ('status', 'ret_net', 'ret_gross', 'fill_price', 'exit_price'):
            va, vb = ra.get(f), rb.get(f)
            if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
                if abs(float(va) - float(vb)) > 1e-9:
                    diffs.append({'key': k, 'field': f, 'a': va, 'b': vb})
            elif va != vb:
                diffs.append({'key': k, 'field': f, 'a': va, 'b': vb})
    return {'n_a': len(a), 'n_b': len(b), 'only_a': only_a, 'only_b': only_b,
            'field_diffs': diffs, 'equal': not (only_a or only_b or diffs)}


def summarize(rows: Sequence[Dict]) -> Dict:
    """一套配置的完整画像：成交/漏单、止损/跳空、净收益区间、超额、日均、逐笔回撤。"""
    filled = [r for r in rows if r.get('ret_net') is not None]
    ordered = sorted(filled, key=lambda r: (r.get('day'), r.get('trigger_time'),
                                            r.get('symbol'), r.get('arm')))
    return {
        'rows': len(rows), 'filled': len(filled),
        'fill': metrics.fill_stats(rows),
        'stop': metrics.stop_stats(rows),
        'net': metrics.describe([r.get('ret_net') for r in ordered], label='ret_net'),
        'gross': metrics.describe([r.get('ret_gross') for r in ordered], label='ret_gross'),
        'excess': metrics.excess_stats(filled),
        'daily': metrics.daily_cluster(filled),
        'mdd': metrics.max_drawdown([r.get('ret_net') for r in ordered]),
    }


def perturb_values(base: float, pct: float = PERTURB_PCT) -> Tuple[float, float]:
    """±pct 两端（保留 4 位，避免 0.3*1.2=0.36 变成 0.35999999）。"""
    b = float(base)
    return (round(b * (1 - float(pct)), 4), round(b * (1 + float(pct)), 4))


def split_by(rows: Sequence[Dict],
             pred) -> Tuple[List[Dict], List[Dict]]:
    """按谓词二分：返回 (命中, 未命中)；谓词返回 None 的行按"未命中"处理，
    调用方要区分缺失时请自己先看 `gate_counts()`。"""
    kept, dropped = [], []
    for r in rows:
        (kept if pred(r) is True else dropped).append(r)
    return kept, dropped


def paired_legs(a: Sequence[Dict], b: Sequence[Dict], value: str = 'ret_net',
                ignore: Iterable[str] = ()) -> Dict:
    """按（可忽略若干参数的）配对键比较同一字段，只保留两边都有值。"""
    va = {trade_key(r, ignore): r.get(value) for r in a}
    vb = {trade_key(r, ignore): r.get(value) for r in b}
    keys = [k for k in va if k in vb]
    return metrics.paired_diff([va[k] for k in keys], [vb[k] for k in keys])


def verdict(lo: Optional[float], hi: Optional[float], n: int,
            eps: float = 1e-9) -> str:
    """按区间下判定；区间跨 0 或 n<3 一律"无结论"，不拿点估计当结论。"""
    if n < 3 or lo is None or hi is None:
        return '无结论（n<3 或无法计算区间）'
    if lo > eps:
        return '显著为正'
    if hi < -eps:
        return '显著为负'
    return '跨 0（无结论）'


def structure_variants(base_params: Dict, names: Sequence[str],
                       pct: float = PERTURB_PCT) -> List[Dict]:
    """结构参数的 ±pct 档位（不读配置，调用方传基线字典）。"""
    out = []
    for name in names:
        base = float(base_params[name])
        lo, hi = perturb_values(base, pct)
        out.append({'name': name, 'base': base, 'lo': lo, 'hi': hi})
    return out
