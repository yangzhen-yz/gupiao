"""dragon_protocol.py - 龙头 v2 协议冻结与 holdout 闸门（清单 T3.4）

职责
----
1. **协议先于结果存在**：`SPEC` 是本协议唯一事实源，`FROZEN_SHA256` 是它的
   sha256（键排序、紧凑分隔的 UTF-8 JSON）。改动任何一项都会让哈希变，
   必须显式换版本并重新冻结，不能"顺手调一下"。
2. **切分写死在代码里**：dev 2026-05-06~2026-10-30；holdout 从 2026-11-02 起
   的前 20 个交易日（前瞻）。回溯取"最后 20 个交易日"已经包含 T3.1~T3.3
   用过的 10-26~10-30，那五天只算开发用量，**不能当 holdout**。
3. **holdout 只评一次**：`holdout_gate()` 在协议哈希、数据覆盖、使用次数
   任一项不满足时返回"未解锁"，`register_use()` 第二次直接抛错；台账由调用方
   落盘，本模块不写文件。
4. **不够就说不"不能评"**：数据覆盖不够时返回具体原因码，不拿 5 日样本
   冒充 holdout 结论。

硬约束
------
- 纯函数：不联网、不写库、不读 `app.config`、不开文件；只做判定与渲染。
- 不替调用方选样本、不替调用方挑窗口；日期由调用方传入。
- `None` / 缺失一律显式标记，不填 0、不填默认值。
"""
from __future__ import annotations

import hashlib
import json
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

PROTOCOL_VERSION = 'v1.0'
FROZEN_AT = '2026-11-01'
ENGINE_VERSION = 't3.1-2026-11-01'
PARAM_VERSION = '90338bf6d1'
FROZEN_SHA256 = '1bc9b23302be0d2657292aaddb8ab670beeaefbf77046f7905e408f25333c0d3'

# 开发集：清单的 2026-05~10；10-30 是本轮最后一个已有日 K 的交易日。
DEV_START = '2026-05-06'
DEV_END = '2026-10-30'
# 这五天已经有全市场 1 分钟，且 T3.1~T3.3 都在上面出过数：只能算**开发用量**，
# 任何后续报告都不许把它们称作 holdout。
DEVELOPMENT_USED_DAYS = ('2026-10-26', '2026-10-27', '2026-10-28',
                         '2026-10-29', '2026-10-30')
# 前瞻 holdout：冻结之后才开始，冻结前不存在任何一天的数据。
HOLDOUT_START = '2026-11-02'
HOLDOUT_TRADING_DAYS = 20
# 解锁 holdout 的数据要求：全市场 1 分钟覆盖 ≥95%，且每只票 09:30~10:30 至少 60 根。
MIN_COVERAGE = 0.95
MIN_BARS_PER_SYMBOL = 60

SPEC: Dict = {
    'protocol_version': PROTOCOL_VERSION,
    'frozen_at': FROZEN_AT,
    'engine_version': ENGINE_VERSION,
    'param_version': PARAM_VERSION,
    'universe': {
        'market': '沪深主板（600/601/603/605/000/001/002/003 开头；不含创业板/科创板/北交所）',
        'exclude': ['ST', '*ST', '退'],
        'min_listing_days': 60,
        'a_pool': ['limit_pool_daily.zt', 'limit_pool_daily.zb',
                   'daily_recommendations', 'daily_candidate_pool'],
        'a_pool_rule': '当日涨停/炸板 ∪ 当日推荐 ∪ 当日海选，按天分别取，不做跨日并集',
        'b_arm': {
            'top_n': 20, 'gain_lo': 2.0, 'gain_hi': 8.5, 'vol_ratio_min': 1.5,
            'amount_5m_wan': 3000.0, 'spread_max': 0.3,
            'first': '09:35', 'last': '10:15', 'round_sec': 300,
        },
        'scan_window': ['09:31', '10:15'],
    },
    'structure': {
        'gain_lo': 2.0, 'gain_hi': 8.5,
        'room_lo': 0.8, 'room_hi': 3.5,
        'push_room_lo': 0.8, 'push_room_hi': 2.0,
        'pause_min': 2, 'pause_max': 5,
        'pullback_lo': 0.3, 'pullback_hi': 2.0,
        'hold_min': 2,
        'above_frac': 0.70, 'above_frac_from': '09:45',
        'shrink_max': 0.80,
        'gap_max': 5.0,
        'deadline': '09:55',
        'scan_minutes': 30,
    },
    'factors': {
        'chg_from_open': {'dir': '越高越好至 5% 封顶', 'anchors': [[0.0, 0.0], [2.0, 50.0], [5.0, 100.0]]},
        'gap': {'dir': '越低越好', 'anchors': [[0.0, 100.0], [3.0, 55.0], [7.0, 0.0]]},
        'room': {'dir': '0.8~2.0 满分，太远无意义', 'anchors': [[0.0, 50.0], [0.8, 100.0], [2.0, 100.0], [3.5, 0.0]]},
        'vol_ratio_same_time': {'dir': '越高越好', 'anchors': [[1.0, 0.0], [1.5, 25.0], [3.0, 100.0]]},
        'amount_5m': {'dir': '对数越高越好', 'anchors': [[300.0, 0.0], [3000.0, 50.0], [15000.0, 100.0]]},
        'spread': {'dir': '越低越好', 'anchors': [[0.0, 100.0], [0.3, 60.0], [0.6, 0.0]]},
        'vwap_share': {'dir': '越高越好', 'anchors': [[0.50, 0.0], [0.70, 55.0], [0.90, 100.0]]},
        'vwap_dist': {'dir': '均线上方适度为正', 'anchors': [[-1.0, 0.0], [0.0, 40.0], [2.0, 100.0]]},
        'fake_break': {'dir': '跌破 VWAP 后 2 根内收回算假破加分', 'anchors': []},
        'ask_bid_ratio': {'dir': '越低越好（卖压小）', 'anchors': [[0.3, 100.0], [1.0, 60.0], [2.0, 0.0]]},
        'pause_dur': {'dir': '2~5 分钟满分', 'anchors': [[0.0, 60.0], [2.0, 100.0], [5.0, 100.0], [8.0, 30.0]]},
    },
    'score': {
        'version': 't2.3-2026-11-01',
        'formula': '100 × Σ(w_i × pct_i) / Σw_i，缺失因子从分母剔除',
        'weights': '11 个特征等权 1.0（无可成交 IC 前不臆造权重）',
        'min_coverage': 0.60,
        'min_score': 0.0,
        'shadow_threshold': 70.0,
        'sealed_score': -100.0,
    },
    'entry': {
        'buy_methods': ['immediate', 'pullback'],
        'immediate': '触发分钟之后那一分钟的开盘价',
        'pullback_band_pct': 0.3,
        'pullback_wait_sec': 180,
        'pullback_timeout': '进成交率分母，不进收益分母',
    },
    'stop': {
        'pct': 2.0,
        'trigger': '分钟收盘价 ≤ 成交净价 ×(1−2%)',
        'fill': '下一分钟开盘价；跳空按开盘价，不假设止损价成交',
    },
    'exit': {
        'main': 'd_1030',
        'comparison': 'd_close',
        'sell_hm': '10:30',
        'rule': '10:30 那一刻在涨停 → 持有到次日开盘；否则按 10:30 收盘走；−2% 止损优先',
        'sealed_rule': 'd_1030 只看 10:30 收盘价 ≥ 涨停价 −0.01，不看收盘是否封住（那会前视）',
        'seal_tol': 0.01,
        'sealed_intraday_separate': True,
    },
    'cost': {'fee_pct': 0.15, 'slip_main': 0.2, 'slip_grid': [0.1, 0.2, 0.3]},
    'benchmark': {
        'pool': 'A 臂当日候选池等权',
        'entry': '触发 +1 分钟开盘',
        'cost_exit': '与策略同成本、同出口',
        'excess': '只算策略与基准两边都有值的腿，缺基准记 missing',
    },
    'dedup': {'key': ['day', 'symbol', 'arm'], 'rule': '同票同日只留最早一跳', 'window_days': 30},
    'sample_gate': {
        'min_days': 40, 'min_triggers': 120,
        'stability_min_segments': 3, 'stability_min_n': 10,
        'p2_shadow_days': 20, 'cell_min_triggers': 30,
    },
    'scope': {
        'p0_separate': True,
        'no_cross_quote': '龙头触发口径与 P0 推荐口径 n 不同、买卖规则不同，禁止互相引用收益数字',
        'p0_baseline_source': 'T0.3/清单 §0.2 的 P0 可成交净口径，不在本协议重复',
    },
    'split': {
        'dev_start': DEV_START,
        'dev_end': DEV_END,
        'development_used_days': list(DEVELOPMENT_USED_DAYS),
        'holdout_start': HOLDOUT_START,
        'holdout_trading_days': HOLDOUT_TRADING_DAYS,
        'holdout_kind': 'prospective',
        'retro_holdout': '不设：计划中的回溯 20 日与已用样本重叠，不能当 holdout',
    },
    'evaluation': {
        'holdout_uses': 1,
        'retune_on_holdout': False,
        'new_version_on_change': True,
        'monthly_recheck': True,
        'intraday_param_change': False,
        'failed_keeps_artifact': True,
    },
    'data': {
        'minute': '全市场 1 分钟 OHLC（09:30~10:30 至少 %d 根/票）' % MIN_BARS_PER_SYMBOL,
        'min_coverage': MIN_COVERAGE,
        'daily': 'stock_daily（基准与次日开盘）',
        'a_pool_tables': ['limit_pool_daily', 'daily_recommendations', 'daily_candidate_pool'],
        'note': '本地无全市场 1 分钟库时 holdout 不可评估；不许用 5 分钟或日线代替',
    },
}


class ProtocolViolation(RuntimeError):
    """协议被破坏时抛错：改参数没换版本、拿 holdout 调参、重复评估等。"""


# ---------- 哈希与校验 ----------

def canonical(obj) -> str:
    """键排序、紧凑分隔的 UTF-8 JSON；本协议的哈希只认这一种写法。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def spec_hash(spec: Optional[Dict] = None) -> str:
    return hashlib.sha256(canonical(spec if spec is not None else SPEC).encode('utf-8')).hexdigest()


def _diff(a, b, path: str = '') -> List[str]:
    out: List[str] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            p = f'{path}.{k}' if path else str(k)
            if k not in a:
                out.append(f'{p}: 协议缺失，文件多出 {b[k]!r}')
            elif k not in b:
                out.append(f'{p}: 文件缺失，协议是 {a[k]!r}')
            else:
                out.extend(_diff(a[k], b[k], p))
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f'{path}: 长度 {len(a)} vs {len(b)}')
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                out.extend(_diff(x, y, f'{path}[{i}]'))
    elif a != b:
        out.append(f'{path}: {a!r} vs {b!r}')
    return out


def verify_spec(spec: Optional[Dict] = None) -> Dict:
    """核对传入规格与冻结版本：逐键 diff + 哈希比对。"""
    S = spec if spec is not None else SPEC
    diffs = _diff(SPEC, S)
    h = spec_hash(S)
    ok = (not diffs) and h == FROZEN_SHA256
    return {'ok': ok, 'hash': h, 'frozen': FROZEN_SHA256,
            'version': PROTOCOL_VERSION, 'frozen_at': FROZEN_AT,
            'diffs': diffs,
            'verdict': '协议一致' if ok else '协议已偏离冻结版本，必须换版本重新冻结'}


def parse_markdown_block(text: str) -> Optional[Dict]:
    """取文档里第一段 ```json 代码块；解析失败返回 None。"""
    lines = (text or '').splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.strip().lower() == '```json':
            start = i + 1
            break
    if start is None:
        return None
    buf: List[str] = []
    for ln in lines[start:]:
        if ln.strip() == '```':
            try:
                return json.loads('\n'.join(buf))
            except ValueError:
                return None
        buf.append(ln)
    return None


def verify_markdown(text: str) -> Dict:
    """文档校验：json 块必须与 SPEC 逐键相等，哈希必须等于冻结值。"""
    block = parse_markdown_block(text)
    if block is None:
        return {'ok': False, 'hash': None, 'frozen': FROZEN_SHA256,
                'diffs': ['未找到可解析的 ```json 规格块'], 'verdict': '文档不可解析'}
    v = verify_spec(block)
    v['has_block'] = True
    return v


# ---------- 切分 ----------

def classify(day: str) -> str:
    """按日期切分：dev / holdout / outside。

    holdout 只按起点判定；"前 N 个交易日"由调用方用交易日列表截断（`holdout_window`），
    这样节假日变动不会把某一天悄悄算进来或漏掉。
    """
    d = str(day)
    if DEV_START <= d <= DEV_END:
        return 'dev'
    if d >= HOLDOUT_START:
        return 'holdout'
    return 'outside'


def dev_window(trading_days: Sequence[str]) -> List[str]:
    return sorted(d for d in trading_days if classify(d) == 'dev')


def holdout_window(trading_days: Sequence[str],
                   n: int = HOLDOUT_TRADING_DAYS) -> List[str]:
    """起点之后的前 n 个交易日；不足 n 个就原样返回（由闸门判"未就绪"）。"""
    days = sorted(d for d in trading_days if d >= HOLDOUT_START)
    return days[:int(n)]


def assert_no_holdout(days: Iterable[str]) -> None:
    """dev 侧代码在取数前调用：碰到 holdout / 未来日期直接抛错。"""
    bad = [d for d in days if classify(d) != 'dev']
    if bad:
        raise ProtocolViolation(
            f'禁止在开发流程里读取 holdout/未来样本: {", ".join(sorted(bad)[:5])}'
            f'{"" if len(bad) <= 5 else f" 等 {len(bad)} 天"}')


# ---------- holdout 闸门 ----------

def _coverage_ok(item: Dict) -> Tuple[bool, Optional[str]]:
    d = str(item.get('day'))
    ratio = item.get('ratio')
    bars = item.get('min_bars')
    if ratio is None or float(ratio) < MIN_COVERAGE:
        return False, f'coverage_low:{d}({ratio})'
    if bars is None or int(bars) < MIN_BARS_PER_SYMBOL:
        return False, f'bars_incomplete:{d}({bars})'
    return True, None


def holdout_gate(*, hash_ok: bool, days: Sequence[str],
                 coverage: Sequence[Dict], used: int,
                 n: int = HOLDOUT_TRADING_DAYS) -> Dict:
    """holdout 能不能评：哈希一致 + 天数齐 + 覆盖够 + 从没用过。

    返回 `{'unlocked': bool, 'reasons': [...], 'checks': {...}}`；
    任何一项不满足都只给原因码，不返回"部分评估"。
    """
    ds = list(days or [])
    reasons: List[str] = []
    if not hash_ok:
        reasons.append('protocol_hash_mismatch')
    if int(used or 0) >= 1:
        reasons.append('holdout_already_used')
    if len(ds) < int(n):
        reasons.append(f'holdout_days_incomplete({len(ds)}/{int(n)})')
    outside = [d for d in ds if classify(d) != 'holdout']
    if outside:
        reasons.append('day_outside_holdout:' + ','.join(sorted(outside)[:3]))
    cov = {str(c.get('day')): c for c in (coverage or [])}
    for d in ds:
        item = cov.get(d)
        if item is None:
            reasons.append(f'coverage_missing:{d}')
            continue
        ok, why = _coverage_ok(item)
        if not ok:
            reasons.append(str(why))
    checks = {'hash_ok': bool(hash_ok), 'used': int(used or 0),
              'days': len(ds), 'need_days': int(n),
              'coverage_days': len(cov), 'min_coverage': MIN_COVERAGE,
              'min_bars': MIN_BARS_PER_SYMBOL}
    return {'unlocked': not reasons, 'reasons': reasons, 'checks': checks,
            'verdict': '可一次性评估' if not reasons else '未解锁（%s）' % '；'.join(reasons[:3])}


def new_ledger() -> Dict:
    return {'protocol_version': PROTOCOL_VERSION,
            'protocol_sha256': FROZEN_SHA256,
            'holdout_uses': 0, 'history': []}


def register_use(ledger: Dict, *, at: str, days: Sequence[str],
                 hash_: str = '') -> Dict:
    """登记一次 holdout 评估；第二次调用直接抛错（返回新台账，不改入参）。"""
    led = dict(ledger or {})
    hist = list(led.get('history') or [])
    if int(led.get('holdout_uses') or 0) >= 1 or hist:
        raise ProtocolViolation('holdout 已评估过，禁止重复使用')
    if hash_ and hash_ != led.get('protocol_sha256'):
        raise ProtocolViolation('登记使用的协议哈希与台账不一致')
    led['holdout_uses'] = int(led.get('holdout_uses') or 0) + 1
    hist.append({'at': str(at), 'protocol_sha256': hash_ or led.get('protocol_sha256'),
                 'days': list(days)})
    led['history'] = hist
    return led


# ---------- 渲染 ----------

def _fmt(v) -> str:
    if isinstance(v, bool):
        return '开' if v else '关'
    if v is None:
        return '—'
    if isinstance(v, (list, tuple)):
        return '、'.join(str(x) for x in v) if v else '—'
    return str(v)


def protocol_markdown() -> str:
    """把 SPEC 渲染成 `analysis_out/dragon_v2_protocol.md`（唯一事实源的投影）。"""
    S = SPEC
    L: List[str] = []
    L.append(f'# 龙头 v2 回测协议（{PROTOCOL_VERSION}，冻结于 {FROZEN_AT}）')
    L.append('')
    L.append(f'- 协议哈希 `sha256={FROZEN_SHA256}`（键排序、紧凑分隔的 UTF-8 JSON；'
             '下方 `json` 块就是被哈希的原文）')
    L.append(f'- 引擎 `{ENGINE_VERSION}`；冻结时的参数指纹 `{PARAM_VERSION}`')
    L.append('- 本文件**先于任何 holdout 结果存在**：冻结后只允许按新版本换哈希，'
             '不允许在同一版本上改阈值。')
    L.append('- 唯一事实源是 `server/services/pick/dragon_protocol.py` 的 `SPEC`；'
             '本文件是它的投影，第 11 节的 `json` 块逐字来自 `SPEC`，可被 `verify_markdown` 核对。')
    L.append('')
    L.append('## 0. 口径分家与封板判据')
    L.append('')
    sc0, ex0 = S['scope'], S['exit']
    L.append(f"- P0 推荐口径与龙头触发口径{'分开计算' if sc0['p0_separate'] else '合并'}：{sc0['no_cross_quote']}。")
    L.append(f"- P0 基准引用：{sc0['p0_baseline_source']}。")
    L.append(f"- 封板判据（`{ex0['main']}`）：{ex0['sealed_rule']}；容差 {ex0['seal_tol']} 元。")
    L.append('- 盘中触板与 10:30 仍在涨停是**两个标记**，分开记录、不合并：'
             '盘中触板只用于样本描述，是否顺延到次日开盘只由 10:30 那一刻决定。')
    L.append('')
    L.append('## 1. 宇宙与样本')
    L.append('')
    u = S['universe']
    L.append(f"- 市场：{u['market']}；剔除 {'、'.join(u['exclude'])}；上市不足 {u['min_listing_days']} 个交易日剔除。")
    L.append(f"- A 臂候选池：{u['a_pool_rule']}；来源 {'、'.join(u['a_pool'])}。")
    b = u['b_arm']
    L.append(f"- B 臂入口：每 {b['round_sec']} 秒重排一次，{b['first']}~{b['last']}，取涨幅前 {b['top_n']}；"
             f"涨幅 {b['gain_lo']}%~{b['gain_hi']}%、量比 ≥{b['vol_ratio_min']}、"
             f"5 分钟额 ≥{b['amount_5m_wan']} 万、价差 ≤{b['spread_max']}%。")
    L.append(f"- 扫描窗口：{u['scan_window'][0]}~{u['scan_window'][1]}；形态必须在 09:55 前成立。")
    L.append('')
    L.append('## 2. 结构判据（形态完整性，不是分数）')
    L.append('')
    st = S['structure']
    L.append('| 项 | 门槛 |')
    L.append('|---|---|')
    L.append(f"| 相对今开涨幅 | {st['gain_lo']}%~{st['gain_hi']}% |")
    L.append(f"| 距涨停（形态） | {st['room_lo']}%~{st['room_hi']}% |")
    L.append(f"| 距涨停（推送档） | {st['push_room_lo']}%~{st['push_room_hi']}% |")
    L.append(f"| 停顿换手 | {st['pause_min']}~{st['pause_max']} 分钟，回撤 {st['pullback_lo']}%~{st['pullback_hi']}% |")
    L.append(f"| 均线上方震荡 | ≥{st['hold_min']} 分钟 |")
    L.append(f"| VWAP 上方时间占比 | ≥{st['above_frac']}（{st['above_frac_from']} 起算，之前记 pending） |")
    L.append(f"| 停顿段缩量比 | ≤{st['shrink_max']} |")
    L.append(f"| 高开上限（形态） | ≤{st['gap_max']}% |")
    L.append(f"| 完成时限 | {st['deadline']}；扫描 {st['scan_minutes']} 根分钟 |")
    L.append('')
    L.append('## 3. 因子与方向（评分 v2）')
    L.append('')
    L.append('| 因子 | 方向 | 锚点（原始值 → 分） |')
    L.append('|---|---|---|')
    for name, f in S['factors'].items():
        anc = '、'.join(f'{a[0]}→{a[1]}' for a in f.get('anchors') or []) or '特殊规则'
        L.append(f"| `{name}` | {f['dir']} | {anc} |")
    L.append('')
    sc = S['score']
    L.append(f"- 公式：`{sc['formula']}`；权重：{sc['weights']}。")
    L.append(f"- 覆盖 <{sc['min_coverage']} 不打分；封板计 {sc['sealed_score']}；"
             f"分数下限 {sc['min_score']}，{sc['shadow_threshold']} 只做影子对照。")
    L.append('')
    L.append('## 4. 执行、成本与出口')
    L.append('')
    en, so, ex, co = S['entry'], S['stop'], S['exit'], S['cost']
    L.append(f"- 买法：立即买（{en['immediate']}）/ 回踩买（VWAP±{en['pullback_band_pct']}%，"
             f"最多等 {en['pullback_wait_sec']} 秒；未成交{en['pullback_timeout']}）。")
    L.append(f"- 止损：−{so['pct']}%，{so['trigger']}，{so['fill']}。")
    L.append(f"- 出口主口径 `{ex['main']}`：{ex['rule']}；对照 `{ex['comparison']}`。")
    L.append(f"- 成本：往返费 {co['fee_pct']}%；滑点主口径 {co['slip_main']}%，"
             f"另跑 {'/'.join(str(x) for x in co['slip_grid'])}% 三档。")
    L.append('')
    L.append('## 5. 基准、去重与门槛')
    L.append('')
    bm, dd, sg = S['benchmark'], S['dedup'], S['sample_gate']
    L.append(f"- 基准：{bm['pool']}，{bm['entry']}，{bm['cost_exit']}；{bm['excess']}。")
    L.append(f"- 去重：按 {'+'.join(dd['key'])}，{dd['rule']}。")
    L.append(f"- 样本门槛：≥{sg['min_days']} 个交易日且 ≥{sg['min_triggers']} 个触发；"
             f"稳定性 ≥{sg['stability_min_segments']} 段且每段 n ≥{sg['stability_min_n']}，"
             '不满足一律"无法判定"。')
    L.append(f"- 上线前 P2 影子至少 {sg['p2_shadow_days']} 个交易日；"
             f"分格统计每格 ≥{sg['cell_min_triggers']} 个触发才报比率，不足只报 n。")
    L.append('')
    L.append('## 6. dev / holdout 切分')
    L.append('')
    sp = S['split']
    L.append(f"- 开发集：{sp['dev_start']} ~ {sp['dev_end']}（清单的 2026-05~10）。")
    L.append(f"- 开发用量（已有全市场 1 分钟、T3.1~T3.3 已在其上出数）："
             f"{'、'.join(sp['development_used_days'])}——**不是 holdout**。")
    L.append(f"- holdout：{sp['holdout_kind']}，{sp['holdout_start']} 起的前 "
             f"{sp['holdout_trading_days']} 个交易日；冻结前不存在任何一天数据。")
    L.append(f"- 回溯 holdout：{sp['retro_holdout']}。")
    L.append('')
    L.append('## 7. 评估纪律')
    L.append('')
    ev = S['evaluation']
    L.append(f"- holdout 只评 {ev['holdout_uses']} 次；{'允许' if ev['retune_on_holdout'] else '禁止'}拿 holdout 回调参；"
             f"{'要' if ev['new_version_on_change'] else '不要'}换版本才改参数；"
             f"上线后{'每月复核' if ev['monthly_recheck'] else '不复核'}；"
             f"{'禁止' if not ev['intraday_param_change'] else '允许'}盘中调参；"
             f"失败{'也保留' if ev['failed_keeps_artifact'] else '不保留'}产物。")
    L.append('- 数据不够时写"无法评估/未解锁"，不拿旧样本、不换窗口重试、不写"通过"。')
    L.append('')
    L.append('## 8. 数据要求（holdout 解锁条件）')
    L.append('')
    de = S['data']
    L.append(f"- 分时：{de['minute']}；全市场覆盖率 ≥{de['min_coverage']}。")
    L.append(f"- 日线：{de['daily']}；A 池表：{'、'.join(de['a_pool_tables'])}。")
    L.append(f"- {de['note']}。")
    L.append('')
    L.append('## 9. 冻结时的数据现实（2026-11-01 只读实测）')
    L.append('')
    L.append('- 全市场 1 分钟只有 **2026-10-26~2026-10-30** 五天（腾讯回放缓存 `dragon_replay_cache`，'
             '3053 个 JSON / 约 134MB / 每日每票 09:30–15:30 共 267 根）；这五天已在第 6 节列为开发用量。')
    L.append('- `history_minute.db` 的 `minute_5m` 共 429 个交易日、每天只有 2~55 只（热点链），'
             '且止于 2026-10-15：覆盖与粒度都不够，不能代替全市场 1 分钟。')
    L.append('- `stock_daily` 有 683 个交易日（2024-01-02~2026-10-30），2026-10-30 覆盖 2458 只；'
             '`limit_pool_daily`、`auction_snapshot` 各只有 2026-10-23~2026-10-30 六天。')
    L.append('- 开发集 2026-05-06~2026-10-30 共 121 个交易日（10-23 及以前 116 天），'
             '其中只有 5 天同时具备日 K、A 池与全市场分钟 → **开发集目前不可完整评估**，T3.5 不许据此搜参。')
    L.append(f'- holdout {HOLDOUT_START} 起 {HOLDOUT_TRADING_DAYS} 个交易日（至 2026-11-27）'
             '在冻结当日数据为 0 天 → 闸门未解锁，本文件不含任何 holdout 收益数字。')
    L.append('')
    L.append('## 10. 变更记录')
    L.append('')
    L.append('- **2026-11-01 T0.1 草案 → v1.0 冻结（本文件）**：')
    L.append('  - 草案只写了宇宙、出口与 D 规则；缺因子集/方向/权重、成本、止损、样本门槛与切分，本次按 '
             'T3.1~T3.3 已实现口径补齐。')
    L.append('  - 草案出口写「收盘封住 → 次日开盘」，并规定「D 规则只用 sealed_close」——用收盘价决定 10:30 的卖法，'
             '属前视。冻结版改为 `d_1030`：**只看 10:30 收盘价是否已达涨停价（容差 0.01），不看当日是否封住**；'
             '`d_close` 降为对照出口。这是口径更正，不是调参。')
    L.append('  - 草案 §7 的「WP2/WP3 未开始」已过期：T3.1/T3.2/T3.3 已完成，T3.4 即本次冻结，T3.5 未开始。')
    L.append('  - `PROTOCOL_VERSION` 维持 v1.0、`param_version` 维持 `90338bf6d1`：冻结只写死参数，不改引擎与默认阈值。')
    L.append('')
    L.append('## 11. 冻结规格（被哈希的原文）')
    L.append('')
    L.append('```json')
    L.append(json.dumps(SPEC, ensure_ascii=False, sort_keys=True, indent=2))
    L.append('```')
    L.append('')
    L.append(f'`sha256(SPEC) = {FROZEN_SHA256}`')
    L.append('')
    L.append('---')
    L.append('')
    L.append('跑法（server 目录下）：`python analysis_out/run_dragon_holdout.py`；'
             '自测 `python analysis_out/selftest_dragon_protocol.py`。')
    return '\n'.join(L)
