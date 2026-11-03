"""dragon_shadow.py - 影子表与开关矩阵（清单 T2.6 / T4.2，2026-11-01，影子）

这份文件只做两件事，都不改推送与选股：
  1. 建 `dragon_shadow`（T4.2 的 26 项语义 / 27 个语义列 + 4 个主键列 + 7 个辅助列；
     `trigger_type` 既是主键又是语义列，故物理总列数 37 = 4 + 27 + 7 − 1），
     每轮把"这一刻这只票为什么是候选"写一行，按
     `(trade_date, symbol, snapshot_time, trigger_type)` 去重；
  2. 给出七条条件的开关矩阵，每条两维：`on`（算不算）与 `mode`（shadow 只记 / live 才拦）。

七条条件（清单 T2.6 第 3 条）
------------------------------
sector_top3 / vol_ratio / gain_5m / vwap70 / pullback / stop / position

`on=False`      不计算、不拦，影子行里记 `state='off'`；
`mode='shadow'` 计算并写进影子行，但**不改 verdict、不改推送**；
`mode='live'`   才是真正生效的那一条。
默认值必须等于今天的行为（逐票 diff = 0）：
  - 量比 1.5 / 涨幅 2.0%~8.5% / VWAP 0.70 已在 T2.3 结构门里生效 → live；
  - 回踩买是 `ENTRY_LIVE_ARM`，但只出计划不下单 → live（计划层）；
  - 止损只建模（T2.5 `stop_sim`），不发卖单 → shadow；
  - 5% 仓位 `RISK_POSITION_ON=False`（等 P3） → off；
  - 板块前 3 没有 09:45 当日板块资金（T0.2 探针结论） → on 但 `available=False`，只记不拦。

硬边界
------
- 写库失败只打印、返回 0，**不能让主推送失败**；
- `fill_price` / `fill_delay_sec` / `slippage` / `sealed_close` / `exit_reason` /
  `ret_gross` / `ret_net` / `benchmark_ret` / `excess` 九列在写入时一律 NULL，
  由 T3.1 回放真正成交后回填 —— 这里不预填、不猜；
- 旧表（`intraday_dragon_shadow` / `dragon_push_log` / `dragon_features`）一行不动；
- 不自动删数据：`prune_old()` 只手工调用，默认不跑。
"""
import json
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from app.config import (INTRADAY_DRAGON_B_GAIN_HI, INTRADAY_DRAGON_B_GAIN_LO,
                        INTRADAY_DRAGON_B_VOL_RATIO,
                        INTRADAY_DRAGON_COND_GAIN_5M_MODE,
                        INTRADAY_DRAGON_COND_GAIN_5M_ON,
                        INTRADAY_DRAGON_COND_PULLBACK_MODE,
                        INTRADAY_DRAGON_COND_PULLBACK_ON,
                        INTRADAY_DRAGON_COND_SECTOR_MODE,
                        INTRADAY_DRAGON_COND_SECTOR_ON,
                        INTRADAY_DRAGON_COND_VOL_MODE,
                        INTRADAY_DRAGON_COND_VOL_ON,
                        INTRADAY_DRAGON_COND_VWAP70_MODE,
                        INTRADAY_DRAGON_COND_VWAP70_ON,
                        INTRADAY_DRAGON_RISK_POSITION_ON,
                        INTRADAY_DRAGON_RISK_STOP_ON,
                        INTRADAY_DRAGON_SHADOW_KEEP_DAYS,
                        INTRADAY_DRAGON_SHADOW_TABLE_ON,
                        INTRADAY_DRAGON_VWAP_SHARE_MIN)
from db.database import get_db_conn

SHADOW_VERSION = 't2.6-2026-11-01'
TABLE_VERSION = 't4.2-2026-11-01'     # 表结构/索引/对账口径版本，不改写入内容
TABLE = 'dragon_shadow'
MODES = ('shadow', 'live')

# ---------- 26 项语义 / 27 个物理列（清单 T4.2 对账表） ----------

SEMANTICS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ('score_v2', ('score_v2',)),
    ('score_legacy', ('score_legacy',)),
    ('rank_gain', ('rank_gain',)),
    ('chg_from_open', ('chg_from_open',)),
    ('gap', ('gap',)),
    ('vol_ratio_same_time', ('vol_ratio_same_time',)),
    ('amount_5m', ('amount_5m',)),
    ('spread', ('spread',)),
    ('sector_rank', ('sector_rank',)),
    ('stock_flow', ('stock_flow',)),
    ('ask_bid_ratio', ('ask_bid_ratio',)),
    ('vwap_share', ('vwap_share',)),
    ('vwap_dist', ('vwap_dist',)),
    ('trigger_type', ('trigger_type',)),
    ('entry_range', ('entry_low', 'entry_high')),
    ('stop_price', ('stop_price',)),
    ('stop_mode', ('stop_mode',)),
    ('fill_price', ('fill_price',)),
    ('fill_delay_sec', ('fill_delay_sec',)),
    ('slippage', ('slippage',)),
    ('sealed_close', ('sealed_close',)),
    ('exit_reason', ('exit_reason',)),
    ('ret_gross', ('ret_gross',)),
    ('ret_net', ('ret_net',)),
    ('benchmark_ret', ('benchmark_ret',)),
    ('excess', ('excess',)),
)
SEMANTIC_COLUMNS: Tuple[str, ...] = tuple(c for _, cols in SEMANTICS for c in cols)

# 清单 T4.2 第 3 步：26 项逐项确认"有列、有来源、有单位"。
# `source` 写真实产出点（模块.函数/列），不写"系统计算"这类空话；
# `unit` 是落库数值的物理单位，枚举列写"枚举：..."，标记列写 0/1。
SEMANTIC_META: Dict[str, Dict[str, str]] = {
    'score_v2': {'source': 'dragon_score.score_v2', 'unit': '分'},
    'score_legacy': {'source': 'ev.score_legacy（缺则 point.score）', 'unit': '分'},
    'rank_gain': {'source': 'dragon_entry 涨幅名次（feature rank_gain）', 'unit': '名'},
    'chg_from_open': {'source': 'dragon_features：price/open−1', 'unit': '%'},
    'gap': {'source': 'dragon_features：open/prev_close−1', 'unit': '%'},
    'vol_ratio_same_time': {'source': '腾讯快照同刻量比（dragon_features）', 'unit': '倍'},
    'amount_5m': {'source': 'dragon_features：近 5 分钟成交额', 'unit': '万元'},
    'spread': {'source': 'dragon_features：(ask1−bid1)/mid', 'unit': '%'},
    'sector_rank': {'source': '板块净流入排名（T0.2 不可用，恒 NULL）', 'unit': '名'},
    'stock_flow': {'source': '个股当日净流入（T0.2 不可用，恒 NULL）', 'unit': '万元'},
    'ask_bid_ratio': {'source': 'dragon_features：ask1_vol/bid1_vol', 'unit': '倍'},
    'vwap_share': {'source': 'dragon_features / 结构检查：VWAP 上方分钟占比', 'unit': '比例 0~1'},
    'vwap_dist': {'source': 'dragon_features：price/vwap−1', 'unit': '%'},
    'trigger_type': {'source': 'dragon_push.build_plans 的 entry_arm / legacy 结构 / B 臂入口',
                     'unit': '枚举：structure/gain_top20/pullback/breakout'},
    'entry_range': {'source': 'dragon_push.build_plans（entry_low + entry_high）', 'unit': '元'},
    'stop_price': {'source': 'dragon_push.stop_plan（成交价或区间下限×0.98）', 'unit': '元'},
    'stop_mode': {'source': 'dragon_push.stop_plan 口径', 'unit': '枚举：fixed_2pct/vwap/day_low'},
    'fill_price': {'source': 'dragon_backtest 回放成交价', 'unit': '元'},
    'fill_delay_sec': {'source': 'dragon_backtest：触发到成交秒', 'unit': '秒'},
    'slippage': {'source': 'dragon_backtest：buy_slip_pct', 'unit': '%'},
    'sealed_close': {'source': 'dragon_backtest：收盘是否封住', 'unit': '0/1'},
    'exit_reason': {'source': 'dragon_backtest 出口', 'unit': '枚举：stop/next_open/d_1030…'},
    'ret_gross': {'source': 'dragon_backtest：(exit/fill−1)×100', 'unit': '%'},
    'ret_net': {'source': 'dragon_backtest：扣费+滑点后', 'unit': '%'},
    'benchmark_ret': {'source': 'dragon_backtest：同窗口等权主板', 'unit': '%'},
    'excess': {'source': 'ret_net − benchmark_ret', 'unit': 'pp'},
}
KEY_COLUMNS = ('trade_date', 'symbol', 'snapshot_time', 'trigger_type')
AUX_COLUMNS = ('name', 'verdict', 'gate', 'reasons', 'conds', 'score_version', 'created_at')
ALL_COLUMNS = KEY_COLUMNS + tuple(
    c for c in SEMANTIC_COLUMNS if c not in KEY_COLUMNS) + tuple(
    c for c in AUX_COLUMNS if c not in KEY_COLUMNS and c not in SEMANTIC_COLUMNS)

# T3.1 回放才写的九列：写入时保持 NULL，不许预填
PENDING_COLUMNS = ('fill_price', 'fill_delay_sec', 'slippage', 'sealed_close',
                   'exit_reason', 'ret_gross', 'ret_net', 'benchmark_ret', 'excess')

_COL_TYPES = {c: 'REAL' for c in SEMANTIC_COLUMNS}
_COL_TYPES.update({
    # ALTER 加列不写 NOT NULL：SQLite 要求非空列必须带默认值，幂等补列时不能失败
    'trade_date': 'TEXT', 'symbol': 'TEXT',
    'snapshot_time': 'TEXT', 'trigger_type': 'TEXT',
    'name': 'TEXT', 'verdict': 'TEXT', 'gate': 'TEXT', 'reasons': 'TEXT',
    'conds': 'TEXT', 'score_version': 'TEXT', 'created_at': 'TEXT',
    'rank_gain': 'INTEGER', 'fill_delay_sec': 'REAL',
    'sealed_close': 'INTEGER', 'exit_reason': 'TEXT',
})

# 清单 T4.2 第 2 步：按现有日期列建三个索引（列名全部沿用现有，不新造）。
_INDEXES = (
    ('ix_dragon_shadow_date_symbol', ('trade_date', 'symbol')),
    ('ix_dragon_shadow_date_score', ('trade_date', 'score_v2')),
    ('ix_dragon_shadow_date_trigger', ('trade_date', 'trigger_type')),
)

_DDL = '''CREATE TABLE IF NOT EXISTS dragon_shadow (
    trade_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    snapshot_time TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    name TEXT,
    verdict TEXT,
    gate TEXT,
    reasons TEXT,
    conds TEXT,
    score_version TEXT,
    created_at TEXT,
    score_v2 REAL,
    score_legacy REAL,
    rank_gain INTEGER,
    chg_from_open REAL,
    gap REAL,
    vol_ratio_same_time REAL,
    amount_5m REAL,
    spread REAL,
    sector_rank REAL,
    stock_flow REAL,
    ask_bid_ratio REAL,
    vwap_share REAL,
    vwap_dist REAL,
    entry_low REAL,
    entry_high REAL,
    stop_price REAL,
    stop_mode TEXT,
    fill_price REAL,
    fill_delay_sec REAL,
    slippage REAL,
    sealed_close INTEGER,
    exit_reason TEXT,
    ret_gross REAL,
    ret_net REAL,
    benchmark_ret REAL,
    excess REAL,
    PRIMARY KEY (trade_date, symbol, snapshot_time, trigger_type)
)'''


# ---------- 开关矩阵 ----------

_SPECS = (
    # (name, 中文, on 配置, mode 配置, 阈值来源, 说明)
    ('sector_top3', '板块前 3', INTRADAY_DRAGON_COND_SECTOR_ON,
     INTRADAY_DRAGON_COND_SECTOR_MODE, 'sector_rank', 'sector_rank 恒 NULL（T0.2），暂时只记不拦'),
    ('vol_ratio', '量比 1.5', INTRADAY_DRAGON_COND_VOL_ON,
     INTRADAY_DRAGON_COND_VOL_MODE, f'≥{INTRADAY_DRAGON_B_VOL_RATIO}', 'B 臂入口粗筛'),
    ('gain_5m', '5 分钟涨幅 2%', INTRADAY_DRAGON_COND_GAIN_5M_ON,
     INTRADAY_DRAGON_COND_GAIN_5M_MODE,
     f'{INTRADAY_DRAGON_B_GAIN_LO}%~{INTRADAY_DRAGON_B_GAIN_HI}%', '相对今开，结构下界'),
    ('vwap70', 'VWAP 70%', INTRADAY_DRAGON_COND_VWAP70_ON,
     INTRADAY_DRAGON_COND_VWAP70_MODE, f'≥{INTRADAY_DRAGON_VWAP_SHARE_MIN}', '09:45 起算'),
    ('pullback', '回踩买', INTRADAY_DRAGON_COND_PULLBACK_ON,
     INTRADAY_DRAGON_COND_PULLBACK_MODE, 'VWAP±0.3%', '只出计划，不下单'),
    ('stop', '−2% 止损', INTRADAY_DRAGON_RISK_STOP_ON,
     'shadow', '−2%', '只建模，不假设止损价成交'),
    ('position', '单笔 5%', INTRADAY_DRAGON_RISK_POSITION_ON,
     'shadow', '5% / 通道 20%', 'RISK_POSITION_ON=False，等 P3'),
)
_NAMES = tuple(s[0] for s in _SPECS)


def _pick(cfg: Optional[Dict], key: str, default):
    if cfg is not None and key in cfg:
        return cfg[key]
    return default


def matrix(cfg: Optional[Dict] = None) -> List[Dict]:
    """七条条件的开关矩阵。`effective = on and mode=='live' and available`。"""
    C = cfg or {}
    out: List[Dict] = []
    for name, label, on_def, mode_def, threshold, note in _SPECS:
        on = bool(_pick(C, f'cond_{name}_on', on_def))
        mode = str(_pick(C, f'cond_{name}_mode', mode_def)).strip().lower()
        if mode not in MODES:
            raise ValueError(f'条件 {name} 的 mode 只能是 {MODES}，收到 {mode!r}')
        # 止损/仓位只建模：即使 on 也永远不是 live（没有下单链路）
        if name in ('stop', 'position'):
            mode = 'shadow'
        available = name != 'sector_top3'
        out.append({
            'name': name, 'label': label, 'on': on, 'mode': mode,
            'available': available, 'effective': bool(on and mode == 'live' and available),
            'threshold': threshold, 'note': note,
        })
    return out


def matrix_map(cfg: Optional[Dict] = None) -> Dict[str, Dict]:
    return {m['name']: m for m in matrix(cfg)}


def cond_state(name: str, cfg: Optional[Dict] = None) -> str:
    """返回 off / unavailable / shadow / live，供判定层决定拦不拦。"""
    m = matrix_map(cfg).get(name)
    if not m:
        raise KeyError(f'未知条件: {name!r}，只能是 {_NAMES}')
    if not m['on']:
        return 'off'
    if not m['available']:
        return 'unavailable'
    return m['mode']


def enforce(name: str, cfg: Optional[Dict] = None) -> bool:
    """只有 live 才拦。开关读失败时**按 live 处理**，保持现行行为，不悄悄放松。"""
    try:
        return cond_state(name, cfg) == 'live'
    except Exception:
        return True


# ---------- 建表 / 对账 ----------

def table_columns(conn) -> List[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info({TABLE})').fetchall()]


def ensure_tables(conn=None) -> None:
    """幂等：先 CREATE IF NOT EXISTS，再把缺的列 ALTER 进来（旧表加列不丢数据）。"""
    own = conn is None
    c = conn or get_db_conn()
    try:
        c.execute(_DDL)
        have = set(table_columns(c))
        for col in ALL_COLUMNS:
            if col not in have:
                c.execute(f'ALTER TABLE {TABLE} ADD COLUMN {col} {_COL_TYPES[col]}')
        for name, cols in _INDEXES:
            c.execute(f'CREATE INDEX IF NOT EXISTS {name} '
                      f'ON {TABLE} ({", ".join(cols)})')
        c.commit()
    finally:
        if own:
            c.close()


def reconcile(conn=None) -> Dict:
    """26 项语义 / 27 个物理列逐项对账（有列、有来源、有单位）+ 三个索引。"""
    own = conn is None
    c = conn or get_db_conn()
    try:
        ensure_tables(c)
        have = set(table_columns(c))
        idx = {name: False for name, _ in _INDEXES}
        for r in c.execute(f'PRAGMA index_list({TABLE})').fetchall():
            if r[1] in idx:
                idx[r[1]] = True
    finally:
        if own:
            c.close()
    missing = [col for col in ALL_COLUMNS if col not in have]
    sem = {}
    bad = []
    bad_meta = []
    for name, cols in SEMANTICS:
        meta = SEMANTIC_META.get(name) or {}
        ok = all(col in have for col in cols)
        meta_ok = bool(str(meta.get('source') or '').strip()) \
            and bool(str(meta.get('unit') or '').strip())
        sem[name] = {'columns': list(cols), 'ok': ok,
                     'source': meta.get('source'), 'unit': meta.get('unit'),
                     'meta_ok': meta_ok}
        if not ok:
            bad.append(name)
        if not meta_ok:
            bad_meta.append(name)
    return {
        'ok': not missing and not bad and not bad_meta and all(idx.values()),
        'table': TABLE,
        'table_version': TABLE_VERSION,
        'semantics_n': len(SEMANTICS),
        'semantic_columns_n': len(SEMANTIC_COLUMNS),
        'columns_total': len(have),
        'missing_columns': missing,
        'bad_semantics': bad,
        'meta_n': len(SEMANTIC_META),
        'bad_meta': bad_meta,
        'indexes': idx,
        'semantics': sem,
        'keys': list(KEY_COLUMNS),
        'aux': [c for c in AUX_COLUMNS if c not in KEY_COLUMNS],
    }


# ---------- 一行影子记录 ----------

def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _cond_hits(r: Dict, plan: Optional[Dict], cfg: Optional[Dict]) -> Dict[str, Dict]:
    """七条条件在这一行的观测值：只记 hit，不改判定。"""
    f = ((r.get('feat') or {}).get('features')) or {}
    ev = r.get('ev') or {}
    gain = _f(f.get('chg_from_open'))
    vr = _f(f.get('vol_ratio_same_time'))
    share = f.get('vwap_share')
    st = (ev.get('structure') or {}).get('checks') or {}
    if st.get('vwap_share', {}).get('value') is not None:
        share = st['vwap_share']['value']
    hits = {
        'sector_top3': None,                       # 数据不可用，不猜
        'vol_ratio': (vr >= float(INTRADAY_DRAGON_B_VOL_RATIO)) if vr is not None else None,
        'gain_5m': ((float(INTRADAY_DRAGON_B_GAIN_LO) <= gain
                     <= float(INTRADAY_DRAGON_B_GAIN_HI)) if gain is not None else None),
        'vwap70': ((float(share) >= float(INTRADAY_DRAGON_VWAP_SHARE_MIN))
                   if share is not None else None),
        'pullback': bool(plan and plan.get('entry_arm') == 'pullback') if plan else None,
        'stop': bool(plan and _f(plan.get('stop_price')) is not None) if plan else None,
        'position': bool(plan and _f(plan.get('entry_low')) is not None) if plan else None,
    }
    out = {}
    maps = {m['name']: m for m in matrix(cfg)}       # 一次算完，7 条各取一次
    for name, m in maps.items():
        if not m['on']:
            state = 'off'
        elif not m['available']:
            state = 'unavailable'
        else:
            state = m['mode']
        out[name] = {'state': state, 'hit': hits.get(name),
                     'effective': m['effective']}
    return out


def build_row(r: Dict, plan: Optional[Dict] = None,
              now: Optional[datetime] = None, cfg: Optional[Dict] = None) -> Dict:
    """一只候选的一行。`plan` 为 T2.4 的买法计划（没有就写 NULL 区间与止损）。"""
    now = now or datetime.now()
    ev = r.get('ev') or {}
    feat = r.get('feat') or {}
    f = feat.get('features') or {}
    pt = r.get('point') or {}
    plan = plan or {}
    trig = plan.get('entry_arm')
    if not trig:
        if r.get('trigger'):
            trig = 'structure'                    # A 臂 legacy 触发
        elif f.get('rank_gain') is not None:
            trig = 'gain_top20'                   # B 臂入口但没出买法计划
        else:
            trig = 'unknown'
    share = f.get('vwap_share')
    st = (ev.get('structure') or {}).get('checks') or {}
    if st.get('vwap_share', {}).get('value') is not None:
        share = st['vwap_share']['value']
    score_legacy = ev.get('score_legacy')
    if score_legacy is None:
        score_legacy = pt.get('score')
    row = {
        'trade_date': now.strftime('%Y-%m-%d'),
        'symbol': str(r.get('symbol') or ''),
        'snapshot_time': now.strftime('%H:%M:%S'),
        'trigger_type': trig,
        'name': r.get('name'),
        'verdict': ev.get('verdict'),
        'gate': ev.get('gate'),
        'reasons': ev.get('reasons') or [],
        'conds': _cond_hits(r, plan or None, cfg),
        'score_version': ev.get('score_version') or (feat.get('feature_version') if feat else None),
        'score_v2': ev.get('score_v2'),
        'score_legacy': score_legacy,
        'rank_gain': f.get('rank_gain'),
        'chg_from_open': f.get('chg_from_open'),
        'gap': f.get('gap'),
        'vol_ratio_same_time': f.get('vol_ratio_same_time'),
        'amount_5m': f.get('amount_5m'),
        'spread': f.get('spread'),
        'sector_rank': f.get('sector_rank'),
        'stock_flow': f.get('stock_flow'),
        'ask_bid_ratio': f.get('ask_bid_ratio'),
        'vwap_share': share,
        'vwap_dist': f.get('vwap_dist'),
        'entry_low': plan.get('entry_low'),
        'entry_high': plan.get('entry_high'),
        'stop_price': plan.get('stop_price'),
        'stop_mode': plan.get('stop_mode'),
    }
    for col in PENDING_COLUMNS:
        row[col] = None                            # T3.1 回填，不在写入时猜
    row['created_at'] = now.strftime('%Y-%m-%d %H:%M:%S')
    return row


def save_rows(trade_date: str, rows: Sequence[Dict], conn=None) -> int:
    """按 (日期, 代码, 快照时刻, 触发类型) 去重写入。失败只打印，返回 0。"""
    if not rows:
        return 0
    own = conn is None
    c = None
    try:
        if c is None:
            c = conn or get_db_conn()       # 连库失败也必须走 except，不许打断扫描
        ensure_tables(c)
        cols = [c_ for c_ in ALL_COLUMNS]
        sql = (f'INSERT OR REPLACE INTO {TABLE} ({",".join(cols)}) '
               f'VALUES ({",".join("?" for _ in cols)})')
        n = 0
        for r in rows:
            vals = []
            for col in cols:
                v = r.get(col)
                if col in ('reasons', 'conds') and v is not None and not isinstance(v, str):
                    v = json.dumps(v, ensure_ascii=False)
                vals.append(v)
            c.execute(sql, tuple(vals))
            n += 1
        c.commit()
        return n
    except Exception as e:
        print(f'[龙头影子] 落库失败（不影响主链路）: {type(e).__name__}: {e}')
        return 0
    finally:
        if own and c is not None:
            c.close()


# ---------- 入口（scan_once 每轮调一次） ----------

def observe(results: Sequence[Dict], now: Optional[datetime] = None,
            cfg: Optional[Dict] = None) -> int:
    """把本轮候选与 legacy 触发写成影子行；任何异常都不抛出。

    v2 候选与 legacy 触发各写各的 `trigger_type`；两臂计划各一行（不混算）。
    非候选、非触发（watch/reject）不写 —— 每轮写几万行没有意义，
    被挡原因已在 `dragon_b_candidates.reject_stats` 与内存统计里。
    """
    C = cfg or {}
    if not bool(C.get('shadow_table_on', INTRADAY_DRAGON_SHADOW_TABLE_ON)):
        return 0
    now = now or datetime.now()
    rows: List[Dict] = []
    try:
        from services.pick import dragon_push
    except Exception:
        dragon_push = None
    for r in results or []:
        ev = r.get('ev') or {}
        is_cand = ev.get('verdict') == 'candidate'
        is_trig = bool(r.get('trigger'))
        if not (is_cand or is_trig):
            continue
        plans: List[Dict] = []
        # 只有 v2 候选才出两臂计划；legacy 触发没有买法计划，写一行 structure，
        # 免得把 A 臂触发伪装成回踩/突破（清单 T2.4 “两臂不混算”）。
        if is_cand and dragon_push is not None:
            try:
                plans = dragon_push.build_plans(r, None, now.strftime('%H:%M'), C)
            except Exception as p_err:
                print(f"[龙头影子] {r.get('symbol')} 计划生成失败: "
                      f'{type(p_err).__name__}: {p_err}')
        if plans:
            for p in plans:
                rows.append(build_row(r, p, now, C))
        else:
            rows.append(build_row(r, None, now, C))
    if not rows:
        return 0
    n = save_rows(now.strftime('%Y-%m-%d'), rows)
    if n:
        print(f'[龙头影子 {now:%H:%M:%S}] 写入 {n} 行')
    return n


# ---------- 保留策略（手工，默认不跑） ----------

def prune_old(keep_days: Optional[int] = None, conn=None) -> int:
    """只保留最近 N 个交易日的影子行；N 天以内一行不删。返回删除行数。

    默认不自动调用：删数据不可逆，等 T5 定完回放窗口再决定。
    """
    keep = int(keep_days or INTRADAY_DRAGON_SHADOW_KEEP_DAYS)
    own = conn is None
    c = conn or get_db_conn()
    try:
        ensure_tables(c)
        dates = [r[0] for r in c.execute(
            f'SELECT DISTINCT trade_date FROM {TABLE} ORDER BY trade_date DESC LIMIT ?',
            (keep,)).fetchall()]
        if len(dates) < keep:
            return 0
        cutoff = min(dates)
        cur = c.execute(f'DELETE FROM {TABLE} WHERE trade_date < ?', (cutoff,))
        c.commit()
        return int(cur.rowcount or 0)
    except Exception as e:
        print(f'[龙头影子] 清理失败: {type(e).__name__}: {e}')
        return 0
    finally:
        if own:
            c.close()


def describe() -> Dict:
    """只读自检：表列 + 矩阵 + 写入开关。"""
    rec = reconcile()
    return {
        'shadow_version': SHADOW_VERSION,
        'table_version': TABLE_VERSION,
        'table': TABLE,
        'reconcile_ok': rec['ok'],
        'semantics_n': rec['semantics_n'],
        'semantic_columns_n': rec['semantic_columns_n'],
        'columns_total': rec['columns_total'],
        'indexes': rec['indexes'],
        'matrix': matrix(),
        'table_on': bool(INTRADAY_DRAGON_SHADOW_TABLE_ON),
        'keep_days': int(INTRADAY_DRAGON_SHADOW_KEEP_DAYS),
    }
