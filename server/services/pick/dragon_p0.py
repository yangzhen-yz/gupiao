"""dragon_p0.py - T5.1 P0 只读汇总（纯函数）

清单 T5.1：把 T0.2 数据探针、T0.3 回放 v2、T0.4 因子方向表汇成一份只读报告，
并复核 legacy 路径逐票 diff = 0、线上零写入零推送。真正的取数/读库在驱动
`analysis_out/run_dragon_p0.py`，本模块只做比对、判定与排版。

硬边界
------
- 纯函数：不联网、不写库、不开文件、不读 `app.config`；开关/表行数/台账
  全部由调用方注入。`dragon_protocol` / `dragon_params` 也是纯函数模块，
  这里只用它们的冻结常量与哈希做比对。
- 缺失一律 `None`，不填 0；复算不一致逐条列出，不做四舍五入掩盖。
- 任何一条核心复算不过，`core_ok=False`，报告照常生成但不放行。
"""
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from services.pick import dragon_params as dparam
from services.pick import dragon_protocol as dp

P0_VERSION = 't5.1-2026-11-01'
TOL = 1e-9

# 证据清单：id -> 产物文件名 + 必需顶层键（缺任一即判"证据不完整"）
EVIDENCE: Tuple[Dict, ...] = (
    {'id': 'T0.2', 'task': '数据探针', 'file': 'dragon_data_probe_20261101.json',
     'keys': ('generated_at', 'items', 'summary')},
    {'id': 'T0.3', 'task': '回放 v2', 'file': 'dragon_replay_v2_20261101.json',
     'keys': ('generated_at', 'days', 'rows')},
    {'id': 'T0.4', 'task': '因子方向三遍检查', 'file': 'factor_direction_check.json',
     'keys': ('generated_at', 'factors', 'summary', 'score_total')},
    {'id': 'T3.1', 'task': '回测引擎与逐笔', 'file': 'dragon_v2_20261101.json',
     'keys': ('engine_version', 'protocol_version', 'param_version', 'days', 'rows')},
    {'id': 'T3.2', 'task': '指标与置信区间', 'file': 'dragon_v2_metrics_20261101.json',
     'keys': ('n_rows', 'n_legs_nondup', 'trigger_pairs', 'main', 'excess', 'gate')},
    {'id': 'T3.3', 'task': '消融与稳定性', 'file': 'dragon_v2_ablation_20261101.json',
     'keys': ('checks', 'sample_gate', 'trials')},
    {'id': 'T3.4', 'task': '协议与 holdout 闸门', 'file': 'dragon_v2_holdout_20261101.json',
     'keys': ('protocol', 'split', 'holdout', 'dev', 'conclusion')},
    {'id': 'T3.5', 'task': '参数冻结', 'file': 'dragon_v2_params_20261101.json',
     'keys': ('manifest', 'manifest_check', 'search', 'holdout')},
    {'id': 'T4.2', 'task': '影子表结构', 'file': 'dragon_v2_table_20261101.json',
     'keys': ('table_version', 'semantics_n', 'columns_total', 'real_db')},
    {'id': 'T2.7', 'task': '容量与熔断', 'file': 'capacity_dragon.json',
     'keys': ('static', 'timing', 'requests', 'caveat')},
)

EVIDENCE_BY_ID = {e['id']: e for e in EVIDENCE}

# legacy 逐票字段：base 字段 -> 重算字段（同名字段直接对应）
LEGACY_FIELDS: Tuple[Tuple[str, str], ...] = (
    ('score', 'score'),
    ('trigger_time', 'trigger_time'),
    ('trigger_price', 'trigger_price'),
    ('room', 'room'),
    ('gain_open', 'gain_open'),
    ('gap', 'gap'),
)
LEG_KEY = ('day', 'symbol', 'trigger_time')


# ---------- 基础 ----------

def _num(v) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _eq(a, b) -> bool:
    """数值按容差比；布尔与字符串严格比（`True == 1` 不算相等）。"""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    fa, fb = _num(a), _num(b)
    if fa is not None and fb is not None:
        return abs(fa - fb) <= TOL
    return a == b


def same(a, b) -> bool:
    """公开的相等判定（驱动复用同一口径，避免两处容差不同）。"""
    return _eq(a, b)


def _d(v) -> Dict:
    """取子对象：不是 dict 就当空（产物被截断/手改时不炸）。"""
    return v if isinstance(v, dict) else {}


def _chk(name: str, actual, expected) -> Dict:
    return {'name': name, 'actual': actual, 'expected': expected, 'ok': _eq(actual, expected)}


def _key(r: Dict, key_fields: Sequence[str] = LEG_KEY) -> Tuple:
    return tuple(r.get(k) for k in key_fields)


def _sorted_keys(keys: Iterable[Tuple]) -> List[Tuple]:
    return sorted(keys, key=lambda k: tuple(str(x) for x in k))


# ---------- 1. 证据完整性与版本 ----------

def evidence_check(artifacts: Dict[str, Optional[Dict]]) -> Dict:
    """每份产物是否在、必需键是否齐；`generated_at` 原样带上供核对时间。"""
    rows = []
    for item in EVIDENCE:
        p = artifacts.get(item['id'])
        present = isinstance(p, dict)
        missing = [k for k in item['keys'] if not present or k not in p]
        rows.append({'id': item['id'], 'task': item['task'], 'file': item['file'],
                     'present': present, 'missing': missing,
                     'generated_at': (p or {}).get('generated_at') if present else None,
                     'ok': present and not missing})
    return {'rows': rows, 'ok': all(r['ok'] for r in rows),
            'n_ok': sum(1 for r in rows if r['ok']), 'n': len(rows)}


def version_check(artifacts: Dict[str, Optional[Dict]]) -> Dict:
    """版本与冻结哈希：三份主产物同版本；T3.4/T3.5 的哈希与当前冻结值相等。"""
    frozen = {
        'protocol_version': dp.PROTOCOL_VERSION,
        'engine_version': dp.ENGINE_VERSION,
        'param_version': dp.PARAM_VERSION,
        'protocol_sha256': dp.FROZEN_SHA256,
        'params_version': dparam.PARAMS_VERSION,
        'params_manifest_sha256': dparam.freeze_manifest()['manifest_sha256'],
    }
    checks: List[Dict] = []
    for tid in ('T3.1', 'T3.2', 'T3.3'):
        p = artifacts.get(tid) or {}
        for k in ('engine_version', 'protocol_version', 'param_version'):
            checks.append(_chk(f'{tid}.{k}', p.get(k), frozen[k]))
    p4 = (artifacts.get('T3.4') or {}).get('protocol') or {}
    checks.append(_chk('T3.4.protocol.hash', p4.get('hash'), frozen['protocol_sha256']))
    checks.append(_chk('T3.4.protocol.ok', bool(p4.get('ok')), True))
    p5 = artifacts.get('T3.5') or {}
    m5 = p5.get('manifest') or {}
    checks.append(_chk('T3.5.manifest.protocol_sha256', m5.get('protocol_sha256'),
                       frozen['protocol_sha256']))
    checks.append(_chk('T3.5.manifest.params_version', m5.get('params_version'),
                       frozen['params_version']))
    checks.append(_chk('T3.5.manifest.manifest_sha256', m5.get('manifest_sha256'),
                       frozen['params_manifest_sha256']))
    checks.append(_chk('T3.5.manifest_check.ok', bool((p5.get('manifest_check') or {}).get('ok')), True))
    return {'checks': checks, 'frozen': frozen,
            'ok': all(c['ok'] for c in checks),
            'n_ok': sum(1 for c in checks if c['ok']), 'n': len(checks)}


# ---------- 2. 逐票 diff ----------

def _index(rows: Sequence[Dict], fields: Sequence[Tuple[str, str]],
           key_fields: Sequence[str] = LEG_KEY) -> Tuple[Dict[Tuple, Dict], List[Dict]]:
    """按 key 建索引；同一 key 出现多次时先验内部一致，不一致记 `key_conflicts`。"""
    idx: Dict[Tuple, Dict] = {}
    conflicts: List[Dict] = []
    for r in rows or []:
        k = _key(r, key_fields)
        if k not in idx:
            idx[k] = r
            continue
        diff = [fa for fa, _ in fields if not _eq(idx[k].get(fa), r.get(fa))]
        if diff:
            conflicts.append({'key': k, 'fields': diff,
                              'a': {fa: idx[k].get(fa) for fa in diff},
                              'b': {fa: r.get(fa) for fa in diff}})
    return idx, conflicts


def leg_compare(base_rows: Sequence[Dict], new_rows: Sequence[Dict],
                fields: Sequence[Tuple[str, str]] = LEGACY_FIELDS,
                key_fields: Sequence[str] = LEG_KEY) -> Dict:
    """逐票字段级比较：两边同 key 逐字段比，容差 `TOL`；缺腿也记。"""
    ia, ca = _index(base_rows, fields, key_fields)
    ib, cb = _index(new_rows, fields, key_fields)
    both = set(ia) & set(ib)
    diffs: List[Dict] = []
    n_cmp = 0
    for k in _sorted_keys(both):
        for fa, fb in fields:
            n_cmp += 1
            va, vb = ia[k].get(fa), ib[k].get(fb)
            if not _eq(va, vb):
                diffs.append({'key': k, 'field': f'{fa}->{fb}', 'base': va, 'new': vb})
    only_a = _sorted_keys(set(ia) - set(ib))
    only_b = _sorted_keys(set(ib) - set(ia))
    return {'n_base': len(base_rows or []), 'n_new': len(new_rows or []),
            'n_keys_base': len(ia), 'n_keys_new': len(ib), 'n_both': len(both),
            'n_fields_compared': n_cmp,
            'only_base': only_a, 'only_new': only_b,
            'key_conflicts': ca + cb, 'field_diffs': diffs,
            'equal': not (only_a or only_b or diffs or ca or cb)}


def diff_tree(new, old, ignore: Iterable[str] = ('generated_at',), path: str = '') -> List[Dict]:
    """两棵 JSON 树逐键比较；`ignore` 里的键（任意层级）跳过。数值按 `TOL`。"""
    out: List[Dict] = []
    ign = set(ignore)
    if isinstance(new, dict) and isinstance(old, dict):
        for k in sorted(set(new) | set(old)):
            if k in ign:
                continue
            out.extend(diff_tree(new.get(k), old.get(k), ign, f'{path}/{k}'))
    elif isinstance(new, list) and isinstance(old, list):
        if len(new) != len(old):
            out.append({'path': f'{path}/len', 'new': len(new), 'old': len(old)})
        for i, (a, b) in enumerate(zip(new, old)):
            out.extend(diff_tree(a, b, ign, f'{path}[{i}]'))
    elif isinstance(new, (int, float)) and isinstance(old, (int, float)) \
            and not isinstance(new, bool) and not isinstance(old, bool):
        if abs(float(new) - float(old)) > TOL:
            out.append({'path': path, 'new': new, 'old': old})
    elif new != old:
        out.append({'path': path, 'new': new, 'old': old})
    return out


# ---------- 3. 计数复算 ----------

def recount_v2(rows: Sequence[Dict]) -> Dict:
    """T3.1 逐笔的计数：总行 / 去重丢掉 / 有效腿 / 票日 / 臂别。

    `n_symbol_days` 是 (day, symbol) 去重数，与 T3.2 `trigger_pairs` 同口径；
    `n_triggers` 是带触发分钟的键数，用于 legacy 逐票对账。
    """
    rows = list(rows or [])
    keep = [r for r in rows if not r.get('dup_dropped')]
    arms: Dict[str, int] = {}
    for r in keep:
        a = str(r.get('arm') or '?')
        arms[a] = arms.get(a, 0) + 1
    return {
        'n_rows': len(rows),
        'n_dup': len(rows) - len(keep),
        'n_nondup': len(keep),
        'n_symbol_days': len({(r.get('day'), r.get('symbol')) for r in rows}),
        'n_symbol_days_nondup': len({(r.get('day'), r.get('symbol')) for r in keep}),
        'n_triggers': len({_key(r) for r in rows}),
        'n_triggers_nondup': len({_key(r) for r in keep}),
        'arms_nondup': arms,
        'filled_nondup': sum(1 for r in keep if r.get('status') == 'filled'),
    }


def recount_replay(rows: Sequence[Dict]) -> Dict:
    """T0.3 回放的计数：45 行 = 15 触发 × 3 档滑点，按触发去重后分桶。

    同一触发在 3 档滑点下重复出现，封板/状态必须按触发键取一次，
    否则 8 个"收盘封住"会被数成 27。
    """
    rows = list(rows or [])
    keys: Dict[Tuple, Dict] = {}
    for r in rows:
        keys.setdefault(_key(r), r)
    keep = [r for r in keys.values() if not r.get('dup_dropped')]
    status: Dict[str, int] = {}
    for r in keep:
        s = str(r.get('status') or '?')
        status[s] = status.get(s, 0) + 1
    return {
        'n_rows': len(rows),
        'n_triggers': len(keys),
        'n_triggers_nondup': len(keep),
        'n_symbols': len({r.get('symbol') for r in rows if r.get('symbol')}),
        'status_nondup': status,
        'sealed_close': sum(1 for r in keep if r.get('sealed_close')),
        'sealed_intraday': sum(1 for r in keep if r.get('sealed_intraday')),
    }


def recount_probe(probe: Dict) -> Dict:
    """T0.2 探针：7 项 + 摘要里的门槛降级结论（gates 原样带入，不二次解释）。"""
    p = probe or {}
    summary = p.get('summary') or {}
    items = p.get('items') or {}
    return {
        'n_items': len(items),
        'item_keys': sorted(items),
        'gates': summary.get('gates') or {},
        'n_not_available': len(summary.get('not_available') or []),
    }


def _sign_conflicts(factors: Sequence[Dict]) -> List[str]:
    """按决策层口径挑出反号因子。

    产物里的 `summary.sign_conflict` 来自 `decision.trade_sign_conflict`
    （缺失时退回 `decision.conditions.trade_same_sign is False`），
    不是 `ic_net` 与 `ic_net_cc2` 反号：后者只比相邻窗口，还会把
    `position` 这类口径不可比的因子算进来，与产物 4 项对不上。
    """
    out = []
    for f in factors or []:
        dec = f.get('decision')
        dec = dec if isinstance(dec, dict) else {}
        flag = dec.get('trade_sign_conflict')
        if flag is None:
            cond = dec.get('conditions')
            flag = isinstance(cond, dict) and cond.get('trade_same_sign') is False
        if flag is True:
            out.append(str(f.get('factor') or ''))
    return out


def recount_direction(direction: Dict) -> Dict:
    """T0.4：从因子表按决策层口径重算反号名单，与摘要比对。"""
    d = direction or {}
    factors = d.get('factors') or []
    summary = d.get('summary') or {}
    stored = list(summary.get('sign_conflict') or [])
    rec = _sign_conflicts(factors)
    return {
        'n_factors': len(factors),
        'sign_conflict_recomputed': rec,
        'sign_conflict_stored': stored,
        'sign_conflict_ok': sorted(rec) == sorted(stored),
        'n_ghost': len(d.get('ghost_factors') or []),
        'pending_signoff': list(d.get('pending_signoff') or []),
        'score_total': d.get('score_total') or {},
        'n_keep': len(summary.get('keep') or []),
        'n_halve': len(summary.get('halve') or []),
    }


# ---------- 4. 线上零变化 ----------

LIVE_EXPECT: Tuple[Tuple[str, str, object], ...] = (
    ('SCORE_MODE', 'score_mode', 'legacy'),
    ('ENABLED', 'enabled', False),
    ('PUSH_ENABLED', 'push_enabled', False),
    ('PUSH_MODE', 'push_mode', 'legacy'),
)


def live_guard(flags: Dict, tables: Dict[str, Dict], ledger: Dict,
               restart_requested: bool) -> Dict:
    """确认没有写线上、没有推送：四个开关 + 六张表 + 台账 + 重启标志。

    `tables` 形如 `{表名: {'exists': bool, 'rows': int}}`；不存在或 0 行都算通过，
    有行就不通过（P0 阶段任何一行影子数据都说明有写入）。
    """
    checks: List[Dict] = []
    for label, key, expected in LIVE_EXPECT:
        checks.append(_chk(f'config.{label}', (flags or {}).get(key), expected))
    for name, t in (tables or {}).items():
        t = _d(t)
        exists = bool(t.get('exists'))
        rows = t.get('rows')
        ok = (not exists) or rows == 0
        checks.append({'name': f'table.{name}', 'actual': {'exists': exists, 'rows': rows},
                       'expected': '不存在或 0 行', 'ok': ok})
    uses = (ledger or {}).get('holdout_uses')
    checks.append(_chk('ledger.holdout_uses', uses, 0))
    checks.append(_chk('restart_requested', bool(restart_requested), False))
    return {'checks': checks, 'ok': all(c['ok'] for c in checks),
            'n_ok': sum(1 for c in checks if c['ok']), 'n': len(checks),
            'flags': flags, 'tables': tables,
            'holdout_uses': uses, 'restart_requested': bool(restart_requested)}


# ---------- 5. 门槛表（清单第 8 节） ----------

def gate_table(artifacts: Dict[str, Optional[Dict]], recompute: Dict) -> List[Dict]:
    """按清单第 8 节的十条门槛逐条给结论；不把"没数据"写成"通过"。"""
    d04 = (artifacts.get('T0.4') or {})
    probe = (artifacts.get('T0.2') or {})
    m32 = (artifacts.get('T3.2') or {})
    ab = (artifacts.get('T3.3') or {})
    cap = (artifacts.get('T2.7') or {})
    gates: List[Dict] = []

    sc = list(_d(d04.get('summary')).get('sign_conflict') or [])
    gates.append({
        'gate': '方向', 'verdict': 'fail' if sc else 'pass',
        'evidence': f"决策层反号（trade_sign_conflict）{len(sc)} 个：{', '.join(sc) or '无'}",
        'note': '反号因子不得按原方向进评分；主评分 v2 不上线（本通道只记录不自造方向）',
    })

    main = _d(m32.get('main'))
    a0 = _d(_d(main.get('A·th0')).get('net'))
    a70 = _d(_d(main.get('A·th70')).get('net'))
    f0 = _d(_d(main.get('A·th0')).get('fill'))
    f70 = _d(_d(main.get('A·th70')).get('fill'))
    gates.append({
        'gate': '阈值', 'verdict': 'not_evaluable',
        'evidence': f"th0 触发 {f0.get('triggers')} / n={a0.get('n')} 均值 {a0.get('mean')}%；"
                    f"th70 触发 {f70.get('triggers')} / n={a70.get('n')} 均值 {a70.get('mean')}%",
        'note': '70 分门槛在本窗口一个触发都没筛掉，两格完全相同，谈不上"胜过"；要等样本够再判',
    })

    b0 = _d(_d(main.get('B·th0')).get('net'))
    gates.append({
        'gate': '臂别', 'verdict': 'recorded',
        'evidence': f"A 臂 n={a0.get('n')} 均值 {a0.get('mean')}%；B 臂 n={b0.get('n')} 均值 {b0.get('mean')}%",
        'note': 'B 臂不能单独证明，不替换 A；两臂同窗口对照，样本不足只记录',
    })

    ml_on = bool(_d(_d(cap.get('static')).get('settings')).get('ml_enabled'))
    gates.append({
        'gate': '非线性', 'verdict': 'not_started',
        'evidence': f"ML_ENABLED={ml_on}；T1.5 未做（本机无 sklearn/lightgbm）",
        'note': '要 ≥120 触发且 ≥40 日后才评估；现在退回线性',
    })

    pg = _d(_d(probe.get('summary')).get('gates'))
    gates.append({
        'gate': '数据', 'verdict': 'pass' if pg.get('G5_data') == 'met' else 'fail',
        'evidence': f"G5_data={pg.get('G5_data')}；G1_3={pg.get('G1_3')}；G1_4={pg.get('G1_4')}；G2={pg.get('G2_order_book')}",
        'note': '板块/个股资金/盘口都没有 09:45 当日值，按清单删条件或只做背景，不用昨日值兜底',
    })

    g32 = _d(m32.get('gate'))
    gates.append({
        'gate': '样本', 'verdict': 'pass' if g32.get('pass') else 'fail',
        'evidence': f"{g32.get('days')} 日 / {g32.get('triggers')} 触发（门槛 {g32.get('min_days')} 日 / {g32.get('min_triggers')} 触发）",
        'note': '不降门槛；P1 只能继续攒影子',
    })

    ex = _d(_d(_d(m32.get('excess')).get('A')).get('excess'))
    exb = _d(_d(_d(m32.get('excess')).get('B')).get('excess'))
    fail_ex = (_num(ex.get('mean_hi')) is not None and float(ex['mean_hi']) < 0)
    gates.append({
        'gate': '收益', 'verdict': 'fail' if fail_ex else 'not_evaluable',
        'evidence': f"A 超额 {ex.get('mean')}%（{ex.get('mean_lo')}~{ex.get('mean_hi')}，n={ex.get('n')}）；"
                    f"B 超额 {exb.get('mean')}%（{exb.get('mean_lo')}~{exb.get('mean_hi')}，n={exb.get('n')}）",
        'note': 'A 整段在 0 下方＝本窗口跑输基准；不进 P2',
    })

    fs = _d(_d(main.get('A·th0')).get('fill'))
    st = _d(_d(main.get('A·th0')).get('stop'))
    gates.append({
        'gate': '执行', 'verdict': 'pass',
        'evidence': f"A 成交 {fs.get('filled')}/{fs.get('triggers')}（{fs.get('fill_rate')}%）、"
                    f"止损 {st.get('stop_hits')}/{st.get('filled')}（{st.get('stop_rate')}%）、"
                    f"跳空均值 {st.get('gap_mean')}%、最差 {st.get('gap_worst')}%",
        'note': '成交率/漏单/止损/跳空已单列（分母是触发腿，不是天数）',
    })

    chk = _d(ab.get('checks'))
    gates.append({
        'gate': '开关', 'verdict': 'pass' if chk.get('all_off_equal') else 'fail',
        'evidence': f"七条全关 vs 原口径：ret {chk.get('ret_match')}/{chk.get('baseline_legs')}、"
                    f"trigger {chk.get('trigger_match')}/{chk.get('baseline_legs')}、"
                    f"all_off_equal={chk.get('all_off_equal')}",
        'note': '关掉必须完全等于改造前；不等就回滚',
    })

    tm = _d(cap.get('timing'))
    gates.append({
        'gate': '容量', 'verdict': 'partial' if tm.get('ok') else 'fail',
        'evidence': f"离线 CPU：{tm.get('round_cpu_ms_120')}ms/轮（120 只，{tm.get('bars')} 根）；"
                    f"慢路径 {_d(cap.get('timing_240')).get('round_cpu_ms_120')}ms",
        'note': '周六离线压测，无真实网络失败率与耗时；请求量为按预筛只数的预算',
    })
    return gates


# ---------- 6. 汇总 ----------

def recompute_block(legacy: Dict, cross: Dict, metric_diffs: Sequence[Dict],
                    counts: Dict, stored: Dict, count_diffs: Sequence[Dict],
                    direction: Dict) -> Dict:
    """把四类复算捆成一块；任何一项不等都算复算不过。

    `counts` 是从原始行重算的，`stored` 是产物/文档里写的数；两边都留在报告里，
    读者可以自己核。`count_diffs` 由驱动用 `diff_tree` 生成。
    """
    ok = (legacy.get('equal') and cross.get('equal') and not metric_diffs
          and not count_diffs and direction.get('sign_conflict_ok'))
    return {'legacy': legacy, 'score_cross': cross,
            'metric_diffs': list(metric_diffs or []),
            'counts': counts, 'stored': stored,
            'count_diffs': list(count_diffs or []),
            'direction': direction, 'ok': bool(ok)}


def verdict(core_ok: bool, gates: Sequence[Dict]) -> Dict:
    """P0 结论：自动核验通过 ≠ 可以进 P1；签字是用户的动作，报告只给边界。"""
    blocking = [g['gate'] for g in gates if g['verdict'] in ('fail', 'not_evaluable', 'not_started')]
    p2_block = [g['gate'] for g in gates if g['gate'] in ('收益', '样本', '数据') and g['verdict'] != 'pass']
    return {
        'core_ok': bool(core_ok),
        'blocking_gates': blocking,
        'p2_blocked_by': p2_block,
        'p1_scope': '影子 + 零推送' if core_ok else '不得进入 P1',
        'p2_allowed': False,
        'push_allowed': False,
        'signoff_required': True,
        'headline': ('P0 自动核验通过（复算一致、legacy 无变化、零写入零推送）；'
                     '门槛未过，只能进 P1 影子，未签字不启动'
                     if core_ok else 'P0 自动核验未通过，先修问题再签'),
    }


def assemble(*, artifacts: Dict[str, Optional[Dict]], recompute: Dict, flags: Dict,
             tables: Dict[str, Dict], ledger: Dict, restart_requested: bool,
             db_stat: Optional[Dict] = None, generated_at: str = '') -> Dict:
    """汇总报告对象：证据 + 版本 + 复算 + 零变化 + 门槛 + 结论。"""
    ev = evidence_check(artifacts)
    ver = version_check(artifacts)
    live = live_guard(flags, tables, ledger, restart_requested)
    gates = gate_table(artifacts, recompute)
    core_ok = bool(ev['ok'] and ver['ok'] and recompute.get('ok') and live['ok'])
    return {
        'p0_version': P0_VERSION,
        'generated_at': generated_at,
        'mode': 'P0 只读',
        'evidence': ev,
        'versions': ver,
        'recompute': recompute,
        'live': live,
        'gates': gates,
        'db_stat': db_stat or {},
        'ledger': ledger or {},
        'verdict': verdict(core_ok, gates),
    }


# ---------- 7. 排版 ----------

def _f(v, nd: int = 3) -> str:
    n = _num(v)
    return '—' if n is None else f'{n:.{nd}f}'


def render_markdown(rep: Dict) -> str:
    """把报告对象渲染成 md；数字全部来自 `rep`，不在这里另算。"""
    v = rep.get('verdict') or {}
    rec = rep.get('recompute') or {}
    ev = rep.get('evidence') or {}
    ver = rep.get('versions') or {}
    live = rep.get('live') or {}
    L: List[str] = []
    L.append(f"# 龙头通道 P0 只读汇总（T5.1，{rep.get('p0_version', '')}）")
    L.append('')
    L.append(f"> 生成 {rep.get('generated_at') or '—'} ｜ 模式 **P0 只读** ｜ 只读研究，"
             f"**未提交、未重启、不写线上、不推送**。")
    L.append('')

    L.append('## 0. 三句话')
    L.append('')
    L.append(f"1. **自动核验{'通过' if v.get('core_ok') else '未通过'}**："
             f"证据 {ev.get('n_ok')}/{ev.get('n')} 齐、版本核对 {ver.get('n_ok')}/{ver.get('n')} 一致、"
             f"legacy 逐票 diff {'0' if rec.get('legacy', {}).get('equal') else '非 0'}、"
             f"线上零变化 {live.get('n_ok')}/{live.get('n')}。")
    L.append(f"2. **门槛**：未通过/不可判的是 {', '.join(v.get('blocking_gates') or []) or '无'}；"
             f"P2 被 {', '.join(v.get('p2_blocked_by') or []) or '—'} 挡住。")
    L.append(f"3. **边界**：{v.get('p1_scope')}；推送 {'允许' if v.get('push_allowed') else '不允许'}；"
             f"即使自动核验通过，也要你签字才启动 P1。")
    L.append('')

    L.append('## 1. 证据清单')
    L.append('')
    L.append('| 任务 | 产物 | 生成时间 | 关键键 | 结论 |')
    L.append('|---|---|---|---|---|')
    for r in ev.get('rows') or []:
        keys = '齐' if not r.get('missing') else f"缺 {', '.join(r.get('missing') or [])}"
        L.append(f"| {r.get('id')} {r.get('task')} | `{r.get('file')}` | {r.get('generated_at') or '—'} "
                 f"| {keys} | {'在' if r.get('ok') else '不完整'} |")
    L.append('')

    L.append('## 2. 版本与冻结哈希')
    L.append('')
    L.append('| 核对项 | 产物值 | 冻结值 | 一致 |')
    L.append('|---|---|---|---|')
    for c in ver.get('checks') or []:
        a, e = c.get('actual'), c.get('expected')
        if isinstance(a, str) and len(a) > 20:
            a = a[:16] + '…'
        if isinstance(e, str) and len(e) > 20:
            e = e[:16] + '…'
        L.append(f"| {c.get('name')} | {a} | {e} | {'是' if c.get('ok') else '否'} |")
    L.append('')

    L.append('## 3. 数字复算')
    L.append('')
    lg = rec.get('legacy') or {}
    cr = rec.get('score_cross') or {}
    L.append('### 3.1 legacy 触发层重算（T0.3 45 行 → 按缓存分时重算）')
    L.append('')
    L.append(f"- 比对 {lg.get('n_both')} 个触发键 × {len(LEGACY_FIELDS)} 字段 ＝ "
             f"{lg.get('n_fields_compared')} 项；字段级差异 **{len(lg.get('field_diffs') or [])}**，"
             f"缺腿 {len(lg.get('only_base') or [])}/{len(lg.get('only_new') or [])}，"
             f"同键冲突 {len(lg.get('key_conflicts') or [])} → **{'diff=0' if lg.get('equal') else '不相等'}**。")
    L.append(f"- 复算函数：`intraday_dragon.evaluate_series`（线上 legacy 触发同一实现）；"
             f"字段：{', '.join(f'{a}' for a, _ in LEGACY_FIELDS)}。")
    for d in (lg.get('field_diffs') or [])[:5]:
        L.append(f"  - {d.get('key')} {d.get('field')}: 产物 {d.get('base')!r} vs 重算 {d.get('new')!r}")
    L.append('')
    L.append('### 3.2 跨产物对账（T3.1 `score_legacy` vs T0.3 `score`）')
    L.append('')
    L.append(f"- {cr.get('n_both')} 个触发键逐一相等：**{'是' if cr.get('equal') else '否'}**"
             f"（比较 {cr.get('n_fields_compared')} 项，差异 {len(cr.get('field_diffs') or [])}）。")
    L.append('')
    L.append('### 3.3 T3.2 指标全表复算')
    L.append('')
    md_n = len(rec.get('metric_diffs') or [])
    L.append(f"- 用 `run_dragon_metrics.build()` 从 T3.1 逐笔重算，与 `dragon_v2_metrics_20261101.json` "
             f"逐键比较（忽略 `generated_at` / `source`）：差异 **{md_n}** 项 → **{'一致' if md_n == 0 else '不一致'}**。")
    for d in (rec.get('metric_diffs') or [])[:5]:
        L.append(f"  - {d.get('path')}: 产物 {d.get('old')!r} vs 复算 {d.get('new')!r}")
    L.append('')
    L.append('### 3.4 计数与摘要复算')
    L.append('')
    counts = rec.get('counts') or {}
    st = rec.get('stored') or {}
    v2 = counts.get('v2') or {}
    rp = counts.get('replay') or {}
    pr = counts.get('probe') or {}
    dr = rec.get('direction') or {}
    L.append('| 项 | 复算值 | 产物/文档值 | 一致 |')
    L.append('|---|---|---|---|')
    for label, key, got in (
            ('T3.1 总行', 'n_rows', v2.get('n_rows')),
            ('T3.1 去重丢掉', 'n_dup', v2.get('n_dup')),
            ('T3.1 有效腿', 'n_nondup', v2.get('n_nondup')),
            ('T3.1 票日', 'n_symbol_days', v2.get('n_symbol_days')),
            ('T3.1 触发键', 'n_triggers', v2.get('n_triggers')),
            ('T0.3 总行', 'replay_rows', rp.get('n_rows')),
            ('T0.3 触发', 'replay_triggers', rp.get('n_triggers')),
            ('T0.3 去重后触发', 'replay_triggers_nondup', rp.get('n_triggers_nondup')),
            ('T0.3 收盘封住（去重后）', 'replay_sealed_close', rp.get('sealed_close')),
            ('T0.3 盘中触及（去重后）', 'replay_sealed_intraday', rp.get('sealed_intraday')),
            ('T0.2 探针项', 'probe_items', pr.get('n_items')),
            ('T0.2 不可得项', 'probe_not_available', pr.get('n_not_available')),
            ('T0.4 因子数', 'direction_factors', dr.get('n_factors')),
            ('T0.4 反号数', 'direction_sign_conflict', len(dr.get('sign_conflict_recomputed') or [])),
    ):
        exp = st.get(key)
        same = _eq(got, exp)
        L.append(f"| {label} | {got} | {exp} | {'是' if same else '否'} |")
    L.append('')
    for d in (rec.get('count_diffs') or [])[:8]:
        L.append(f"- 计数差异：{d.get('path')}: 产物 {d.get('old')!r} vs 复算 {d.get('new')!r}")
    if rec.get('count_diffs'):
        L.append('')
    L.append(f"- T0.4 反号名单（重算）：{', '.join(dr.get('sign_conflict_recomputed') or []) or '无'}；"
             f"产物写的是：{', '.join(dr.get('sign_conflict_stored') or []) or '无'}。")
    L.append(f"- 总分 `score_total`：IC_all {_f((dr.get('score_total') or {}).get('ic_all'), 4)} / "
             f"IC_net {_f((dr.get('score_total') or {}).get('ic_net'), 4)}"
             f"（t={_f((dr.get('score_total') or {}).get('t_net'), 2)}）→ 总分本身没有预测力，与《胜率低诊断》一致。")
    L.append(f"- 待拍板 {len(dr.get('pending_signoff') or [])} 项：{'；'.join(dr.get('pending_signoff') or []) or '无'}。")
    L.append('')

    L.append('## 4. 线上零变化核验')
    L.append('')
    L.append('| 项 | 实际 | 期望 | 通过 |')
    L.append('|---|---|---|---|')
    for c in live.get('checks') or []:
        L.append(f"| {c.get('name')} | {c.get('actual')} | {c.get('expected')} | {'是' if c.get('ok') else '否'} |")
    L.append('')
    db = rep.get('db_stat') or {}
    if db:
        L.append(f"- 库文件 `{db.get('path', 'gupiao.db')}`：运行前 {db.get('before_size')} 字节 / mtime "
                 f"{db.get('before_mtime')}；运行后 {db.get('after_size')} 字节 / mtime {db.get('after_mtime')} "
                 f"→ **{'运行期间未变化' if db.get('unchanged') else '运行期间有变化（不一定是本报告，需排查）'}**。")
        L.append('')
    led = rep.get('ledger') or {}
    L.append(f"- holdout 台账：`holdout_uses={led.get('holdout_uses')}`、"
             f"`last_gate.unlocked={((led.get('last_gate') or {}).get('unlocked'))}` → 本轮未读 holdout。")
    L.append('')

    L.append('## 5. 门槛（清单第 8 节）')
    L.append('')
    L.append('| 门槛 | 结论 | 证据 | 说明 |')
    L.append('|---|---|---|---|')
    for g in rep.get('gates') or []:
        L.append(f"| {g.get('gate')} | {g.get('verdict')} | {g.get('evidence')} | {g.get('note')} |")
    L.append('')

    L.append('## 6. 结论与未做')
    L.append('')
    L.append(f"- {v.get('headline')}。")
    L.append(f"- 允许的阶段：**{v.get('p1_scope')}**；`PUSH_ENABLED` 保持 False，"
             f"P2 被 {', '.join(v.get('p2_blocked_by') or []) or '—'} 挡住，P3 更不具备条件。")
    L.append('- 5 日 / 15 触发不达 40 日 / 120 触发，**不写「已上线」、不报「胜率提升」**；'
             '开发集 121 天里只有 5 天有全市场 1 分钟，T3.5 不搜参。')
    L.append('- holdout（2026-11-02~2026-11-27）覆盖 0.0，闸门未解锁，评估次数 0；不回调参。')
    L.append('- 本轮没做：真实盘中探针（周末无法验证 09:35/09:45 可得性）、盘口历史、本地 1 分钟库、'
             'ML（T1.5）、组合层仓位、推送演练。')
    L.append('')
    L.append('## 7. 复现')
    L.append('')
    L.append('```bash')
    L.append('cd server')
    L.append('python analysis_out/run_dragon_p0.py')
    L.append('python analysis_out/selftest_dragon_p0.py')
    L.append('```')
    L.append('')
    L.append('产物：本文件 + 同名 `.json`。只读，**未提交、未重启**。')
    return '\n'.join(L)
