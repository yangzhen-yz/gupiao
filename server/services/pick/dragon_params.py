"""dragon_params.py - 龙头 v2 参数优化与冻结（清单 T3.5）

职责
----
1. **粗网格可复现**：`GRID` 固定"每个旋钮 ±20%"的单因子扫描，外加基线；
   组合轮只对单因子为正且稳健的参数做两两组合。网格是常量，不随行情变。
2. **等效时选保守**：`select()` 先过样本门（≥40 日 / ≥120 触发），然后
   **不追单点最高**——目标差 <0.3pp 视为等效，按"改动少 → 接近现行 →
   扰动稳定 → 区间窄 → id 排序"选那一组；不选单点最高分。
3. **冻参可追**：`freeze_manifest()` 把当前协议里的可调项抄下来，连同
   `PROTOCOL_VERSION` / `PARAM_VERSION` / 协议哈希算一份清单哈希；任何一项
   变了清单哈希就变。
4. **盘中不调参**：`assert_not_intraday()` 在交易日 09:15~15:00 抛错，
   调用方不得捕获后继续。

硬约束
------
- 纯函数：不联网、不写库、不读 `app.config`、不开文件；参数只从已冻结的
  `dragon_protocol.SPEC` 取，不在本模块另存一套默认值。
- **不执行搜索**：本模块只定义"怎么搜、怎么选、怎么冻"；跑网格需要全市场
  分钟与逐轮重跑，由驱动在开发集可评估后调用，缺数据时如实拒绝。
- 不读 holdout：切分与闸门归 `dragon_protocol`，本模块不碰日期样本。
"""
from __future__ import annotations

import hashlib
import json
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from services.pick import dragon_protocol as dp

PARAMS_VERSION = 't3.5-2026-11-01'
EQUIV_TOL_PP = 0.30          # 目标差小于 0.3pp 视为等效，不选单点最高
MIN_SEG_DAYS = dp.SPEC['sample_gate']['min_days']
MIN_TRIGGERS = dp.SPEC['sample_gate']['min_triggers']
MIN_SEGMENTS = dp.SPEC['sample_gate']['stability_min_segments']
INTRADAY_LOCK = ('09:15', '15:00')
OBJECTIVE = '开发集验证窗相对基准的超额改善（pp，可成交净口径，分段同向）'

# 旋钮表：名字 → 在 SPEC 里的路径 + 小数位（±20% 后按这个位数取值）
KNOBS: Dict[str, Dict] = {
    'b_gain_lo': {'path': ('universe', 'b_arm', 'gain_lo'), 'nd': 1},
    'b_gain_hi': {'path': ('universe', 'b_arm', 'gain_hi'), 'nd': 1},
    'b_vol_ratio': {'path': ('universe', 'b_arm', 'vol_ratio_min'), 'nd': 1},
    'b_amount_5m': {'path': ('universe', 'b_arm', 'amount_5m_wan'), 'nd': 0},
    'b_spread_max': {'path': ('universe', 'b_arm', 'spread_max'), 'nd': 2},
    'pat_gain_hi': {'path': ('structure', 'gain_hi'), 'nd': 1},
    'pat_above_frac': {'path': ('structure', 'above_frac'), 'nd': 2},
    'pat_gap_max': {'path': ('structure', 'gap_max'), 'nd': 1},
    'pullback_band': {'path': ('entry', 'pullback_band_pct'), 'nd': 2},
    'stop_pct': {'path': ('stop', 'pct'), 'nd': 1},
    'score_shadow': {'path': ('score', 'shadow_threshold'), 'nd': 0},
}

ROUNDS = (
    {'name': 'R1 基线 + 单因子 ±20%',
     'rule': '基线 1 次 + 每个旋钮 2 个变体（低/高），同一开发窗、同一成本与出口',
     'max_runs': 1 + 2 * len(KNOBS)},
    {'name': 'R2 单因子入选者的两两组合',
     'rule': '只组合 R1 里"方向为正、分段同向、扰动不翻转"的参数；最多 10 组',
     'max_runs': 10},
    {'name': 'R3 入选组 ±10% 邻域复核',
     'rule': '对 R2 选出的那组逐旋钮 ±10%，方向反了就退回 R1/R2 的更保守组',
     'max_runs': 2 * len(KNOBS)},
)


class ParamLockViolation(RuntimeError):
    """盘中改参、样本不足硬选、冻结清单被改却不换版本等。"""


# ---------- 读值 / 网格 ----------

def knob_value(name: str, spec: Optional[Dict] = None):
    """从协议 SPEC 取某个旋钮的当前值；路径不对直接抛错，不填默认值。"""
    node = spec if spec is not None else dp.SPEC
    for key in KNOBS[name]['path']:
        if not isinstance(node, dict) or key not in node:
            raise KeyError(f'旋钮 {name} 在协议里找不到路径 {KNOBS[name]["path"]}')
        node = node[key]
    return node


def current_params(spec: Optional[Dict] = None) -> Dict:
    """当前冻结参数（11 个旋钮）——唯一来源是协议 SPEC。"""
    return {name: knob_value(name, spec) for name in KNOBS}


def _round(x: float, nd: int) -> float:
    """按小数位取整，**保持 float**：格点与基线同类型，canonical JSON 不会
    因为 `2400` / `2400.0` 的写法差异产生假 diff。"""
    return round(float(x), int(nd))


def sweep_values(name: str, pct: float = 0.20, spec: Optional[Dict] = None) -> List:
    """某旋钮的 ±pct 两档（按 KNOBS 的小数位四舍五入）。"""
    cur = knob_value(name, spec)
    nd = KNOBS[name]['nd']
    lo, hi = _round(cur * (1 - pct), nd), _round(cur * (1 + pct), nd)
    return [lo, hi]


def grid(spec: Optional[Dict] = None) -> Dict:
    """完整搜索计划：基线、单因子变体、轮次与上限。不含任何结果。"""
    base = current_params(spec)
    variants = []
    for name in KNOBS:
        lo, hi = sweep_values(name, 0.20, spec)
        variants.append({'id': f'{name}-lo', 'knob': name, 'value': lo,
                         'params': {**base, name: lo}})
        variants.append({'id': f'{name}-hi', 'knob': name, 'value': hi,
                         'params': {**base, name: hi}})
    return {'params_version': PARAMS_VERSION, 'objective': OBJECTIVE,
            'equiv_tol_pp': EQUIV_TOL_PP,
            'min_days': MIN_SEG_DAYS, 'min_triggers': MIN_TRIGGERS,
            'min_segments': MIN_SEGMENTS,
            'baseline': {'id': 'baseline', 'params': dict(base)},
            'variants': variants, 'rounds': [dict(r) for r in ROUNDS],
            'total_runs_max': sum(int(r['max_runs']) for r in ROUNDS)}


# ---------- 选择 ----------

def _relative_distance(params: Dict, base: Dict) -> float:
    """相对现值的平均偏离（对 0 以外的旋钮取 |Δ|/现值）。"""
    ds = []
    for name, cur in base.items():
        if name not in params:
            continue
        try:
            cur_f, new_f = float(cur), float(params[name])
        except (TypeError, ValueError):
            continue
        if cur_f == 0:
            ds.append(0.0 if new_f == 0 else 1.0)
        else:
            ds.append(abs(new_f - cur_f) / abs(cur_f))
    return round(sum(ds) / len(ds), 6) if ds else 0.0


def _ci_width(c) -> float:
    try:
        lo, hi = float(c[0]), float(c[1])
        return hi - lo
    except (TypeError, ValueError, IndexError):
        return float('inf')


def eligible(c: Dict) -> Tuple[bool, str]:
    """样本门：天数/触发数不够不参与选择；没有目标值也不参与。"""
    if c.get('objective_pp') is None:
        return False, 'objective_missing'
    if int(c.get('n_days') or 0) < MIN_SEG_DAYS:
        return False, f"days_short({int(c.get('n_days') or 0)}/{MIN_SEG_DAYS})"
    if int(c.get('n_triggers') or 0) < MIN_TRIGGERS:
        return False, f"triggers_short({int(c.get('n_triggers') or 0)}/{MIN_TRIGGERS})"
    if int(c.get('n_segments') or 0) < MIN_SEGMENTS:
        return False, f"segments_short({int(c.get('n_segments') or 0)}/{MIN_SEGMENTS})"
    if c.get('sign_flip'):
        return False, 'segment_sign_flip'
    return True, ''


def select(candidates: Sequence[Dict], base: Optional[Dict] = None) -> Dict:
    """等效时选保守：改动少 → 接近现行 → 扰动稳定 → 区间窄 → id。

    返回 `{'chosen': id|None, 'equivalent': [...], 'rejected': [...], 'reason': str}`；
    没有候选过样本门时返回 `chosen=None`，不硬选。
    """
    cur = dict(base or current_params())
    ok, rejected = [], []
    for c in candidates or []:
        good, why = eligible(c)
        (ok if good else rejected).append(
            c if good else {'id': c.get('id'), 'reason': why})
    if not ok:
        return {'chosen': None, 'equivalent': [], 'rejected': rejected,
                'best_objective': None,
                'reason': '没有候选通过样本门（%d 个被拒）' % len(rejected)}

    best = max(float(c['objective_pp']) for c in ok)
    equiv = [c for c in ok if float(c['objective_pp']) >= best - EQUIV_TOL_PP]

    def key(c: Dict):
        params = c.get('params') or {}
        changed = c.get('changed')
        if changed is None:
            changed = [k for k, v in params.items() if cur.get(k) != v]
        return (len(changed), _relative_distance(params, cur),
                bool(c.get('perturb_flip')), _ci_width(c.get('ci')),
                str(c.get('id')))

    equiv_sorted = sorted(equiv, key=key)
    win = equiv_sorted[0]
    tie = [c['id'] for c in equiv_sorted[1:]]
    reason = (f"最优 {best:.2f}pp；{len(equiv)} 组落在 ±{EQUIV_TOL_PP}pp 内，"
              f"按改动数/偏离/扰动/区间选 {win['id']}"
              + (f"（等效未选：{', '.join(map(str, tie))}）" if tie else '（无等效组）'))
    return {'chosen': win['id'], 'chosen_objective': float(win['objective_pp']),
            'equivalent': [c['id'] for c in equiv_sorted],
            'rejected': rejected, 'best_objective': best, 'reason': reason}


# ---------- 冻结 ----------

def canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def freeze_manifest(spec: Optional[Dict] = None) -> Dict:
    """当前参数的冻结清单：11 个旋钮 + 协议/引擎版本 + 清单哈希。"""
    params = current_params(spec)
    core = {'params_version': PARAMS_VERSION,
            'protocol_version': dp.PROTOCOL_VERSION,
            'protocol_sha256': dp.FROZEN_SHA256,
            'engine_version': dp.ENGINE_VERSION,
            'param_version': dp.PARAM_VERSION,
            'params': params}
    h = hashlib.sha256(canonical(core).encode('utf-8')).hexdigest()
    return {**core, 'manifest_sha256': h,
            'frozen_at': dp.FROZEN_AT,
            'monthly_recheck': bool(dp.SPEC['evaluation']['monthly_recheck']),
            'intraday_change': bool(dp.SPEC['evaluation']['intraday_param_change'])}


def _knob_diff(a: Dict, b: Dict) -> List[str]:
    out = []
    for name in KNOBS:
        if (a or {}).get(name) != (b or {}).get(name):
            out.append(f"{name}: {(a or {}).get(name)!r} vs {(b or {}).get(name)!r}")
    return out


def verify_manifest(m: Dict) -> Dict:
    """核对清单：哈希一致 + 旋钮与当前协议相等。"""
    core = {k: m.get(k) for k in ('params_version', 'protocol_version',
                                  'protocol_sha256', 'engine_version',
                                  'param_version', 'params')}
    h = hashlib.sha256(canonical(core).encode('utf-8')).hexdigest()
    diffs = _knob_diff(current_params(), m.get('params') or {})
    ok = (h == m.get('manifest_sha256')) and not diffs \
        and m.get('protocol_sha256') == dp.FROZEN_SHA256
    return {'ok': ok, 'hash': h, 'frozen': m.get('manifest_sha256'),
            'diffs': diffs,
            'verdict': '冻结清单一致' if ok else '清单与协议不一致，必须换 params_version 重新冻结'}


def change_diff(old: Dict, new: Dict) -> List[Dict]:
    """变更前后逐项 diff（可查用）。只比旋钮，值不同才列出。"""
    out = []
    for name in KNOBS:
        a, b = (old or {}).get(name), (new or {}).get(name)
        if a != b:
            out.append({'knob': name, 'old': a, 'new': b})
    return out


# ---------- 盘中锁 ----------

def _hm_min(hm: str) -> int:
    s = str(hm or '').strip().replace(':', '')
    if len(s) != 4 or not s.isdigit():
        raise ValueError(f'非法时刻: {hm!r}')
    return int(s[:2]) * 60 + int(s[2:])


def assert_not_intraday(now_hm: str, trading_day: bool = True) -> None:
    """交易日 09:15~15:00 禁止改参；非交易日不拦。"""
    if not trading_day:
        return
    t = _hm_min(now_hm)
    lo, hi = _hm_min(INTRADAY_LOCK[0]), _hm_min(INTRADAY_LOCK[1])
    if lo <= t <= hi:
        raise ParamLockViolation(
            f'盘中({now_hm})禁止改参：交易日 {INTRADAY_LOCK[0]}~{INTRADAY_LOCK[1]} 只读')


def plan_status(dev_evaluable: bool, dev_days: int = 0,
                minute_ok_days: int = 0) -> Dict:
    """能不能开搜：只用 T3.4 审计过的开发集状态，不自己猜。"""
    if dev_evaluable:
        return {'status': 'ready_to_search', 'searched': False,
                'verdict': '开发集可评估，可按 grid() 跑 R1~R3；执行器尚未接线'}
    return {'status': 'blocked_no_dev_sample', 'searched': False,
            'dev_days': int(dev_days), 'minute_ok_days': int(minute_ok_days),
            'need_days': MIN_SEG_DAYS,
            'verdict': (f'开发集不可评（{minute_ok_days}/{dev_days} 天有全市场 1 分钟，'
                        f'门槛 {MIN_SEG_DAYS} 天）：本轮不搜参，只冻结现行参数')}
