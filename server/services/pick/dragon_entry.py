"""dragon_entry.py - 龙头通道 B 臂：涨幅前 20 候选入口（清单 T2.1）

解决什么
--------
现行候选池 = 用户池 ∪ 当日推荐 ∪ 海选前 60 ∪ 昨涨停/炸板 ∪ 急拉缓存。
2026-11-01 回放：10:00 前封板的 113 只里只有 33 只（29.2%）落在池内 ——
**第一漏口是入口，不是识别**。B 臂每 5 分钟对主板做一次批量快照，
按"相对今开涨幅"取前 20 交给同一套分钟评分，与 A 臂（结构优先）并行。

硬约束
------
1. 纯候选层：不拉分时、不打分、不推送；调用方（`intraday_dragon.scan_once`）决定怎么用。
2. A 臂逻辑一行不改：B 臂关闭时不导入本模块，零额外请求、零额外行。
3. 只做沪深主板非 ST/退；涨幅 2%~8.5%、量比、5 分钟额、价差四道粗筛；
   "上市 <60 交易日"用本地日K首根近似（库内历史始于 2024-01-02，
   更早上市的一律不会被误判成新股）。
4. 候选每轮落 `dragon_b_candidates`，可按 (日期, 轮次) 完整复现。

与 A 臂的口径差异（必须知道）
----------------------------
- `vol_ratio_same_time`：本机没有前 5 日分时库，粗筛阶段只能用腾讯快照量比
  （其本身就是按同时段折算的），并写 `vol_src='snapshot'`；拿不到才回退，标 `missing`。
- `amount_5m`：优先用"5 分钟前那次快照的累计额"差分（`snapshot_diff`）；
  首轮没有前值就按已过分钟数等比折算（`snapshot_prorata`）。两者都写进行里，
  不做"看起来精确"的伪装。
- `spread`：腾讯字段 9=买一价、19=卖一价（10/20 才是买一/卖一量）。
  盘口缺失时不计入 `spread_high`，单独计 `spread_missing` 并仍参与排序 ——
  T2.3 的最终推送线会再卡一次。
"""
import json
import time
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

from app.config import (INTRADAY_DRAGON_B_AMOUNT_5M, INTRADAY_DRAGON_B_FIRST,
                        INTRADAY_DRAGON_B_GAIN_HI, INTRADAY_DRAGON_B_GAIN_LO,
                        INTRADAY_DRAGON_B_LAST, INTRADAY_DRAGON_B_NEW_DAYS,
                        INTRADAY_DRAGON_B_ROUND_SEC, INTRADAY_DRAGON_B_SPREAD_MAX,
                        INTRADAY_DRAGON_B_TOP_N, INTRADAY_DRAGON_B_VOL_RATIO,
                        KLINE_HISTORY_DB_PATH)
from db.database import get_db_conn

DEFAULT_CFG = {
    'top_n': INTRADAY_DRAGON_B_TOP_N,
    'gain_lo': INTRADAY_DRAGON_B_GAIN_LO,
    'gain_hi': INTRADAY_DRAGON_B_GAIN_HI,
    'vol_ratio_min': INTRADAY_DRAGON_B_VOL_RATIO,
    'amount_5m_min': INTRADAY_DRAGON_B_AMOUNT_5M,
    'spread_max': INTRADAY_DRAGON_B_SPREAD_MAX,
    'first': INTRADAY_DRAGON_B_FIRST,
    'last': INTRADAY_DRAGON_B_LAST,
    'round_sec': INTRADAY_DRAGON_B_ROUND_SEC,
    'new_days': INTRADAY_DRAGON_B_NEW_DAYS,
}

REJECT_KEYS = ('not_main', 'st', 'no_price', 'limit_down', 'sealed',
               'new_listing', 'gain_low', 'gain_high', 'vol_low',
               'amount_low', 'spread_high')


# ---------- 小工具 ----------

def _base(symbol: str) -> str:
    s = str(symbol or '')
    return s[2:] if s.startswith(('sh', 'sz')) else s


def minutes_since_open(hm: Optional[str]) -> int:
    """09:30 → 0；09:35 → 5。盘外/非法返回 0（调用方先判窗口）。"""
    try:
        h, m = str(hm or '').split(':')[:2]
        return max(0, (int(h) * 60 + int(m)) - (9 * 60 + 30))
    except (ValueError, TypeError):
        return 0


def in_round_window(hm: str, cfg: Optional[Dict] = None) -> bool:
    C = cfg or DEFAULT_CFG
    return C['first'] <= hm <= C['last']


def _enforce(name: str, cfg: Optional[Dict] = None) -> bool:
    """T2.6 开关矩阵：只有 live 才剔除。读失败按 live，保持现行行为。"""
    try:
        from services.pick import dragon_shadow
        return dragon_shadow.enforce(name, cfg)
    except Exception:
        return True


def round_due(hm: str, now_ts: float, last_round_ts: float,
              cfg: Optional[Dict] = None) -> bool:
    """本轮要不要重排候选：窗口内 + 距上次 ≥ round_sec（首轮 last=0 立即通过）。"""
    C = cfg or DEFAULT_CFG
    if not in_round_window(hm, C):
        return False
    return now_ts - float(last_round_ts or 0) >= C['round_sec']


# ---------- 新股清单（近似） ----------

_NEW_CACHE: Dict[str, object] = {'ts': 0.0, 'days': 0, 'codes': set()}


def load_recent_listings(days: Optional[int] = None,
                         cache_sec: float = 21600.0) -> Set[str]:
    """上市不足 days 个交易日的票：用本地日K首根日期近似（6 小时缓存，零外部请求）。

    60 交易日 ≈ 88 自然日；库内历史始于 2024-01-02，所以 2024 年前上市的一律
    不会被当成新股。取不到库时返回空集并打印，宁可漏排除也不要静默改写。
    """
    days = int(days or DEFAULT_CFG['new_days'])
    now = time.time()
    if (_NEW_CACHE['codes'] and _NEW_CACHE['days'] == days
            and now - float(_NEW_CACHE['ts']) < cache_sec):      # type: ignore[arg-type]
        return set(_NEW_CACHE['codes'])                          # type: ignore[arg-type]
    cutoff_days = int(days * 88 / 60) + 2
    cutoff = datetime.fromtimestamp(now - cutoff_days * 86400).strftime('%Y-%m-%d')
    codes: Set[str] = set()
    try:
        import sqlite3
        conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=5)
        try:
            for sym, first in conn.execute(
                    'SELECT symbol, MIN(date) AS f FROM stock_daily GROUP BY symbol'):
                if first and str(first) >= cutoff:
                    b = str(sym).zfill(6)
                    codes.add(('sh' if b.startswith('6') else 'sz') + b)
        finally:
            conn.close()
    except Exception as e:
        print(f'[龙头B臂] 新股清单失败: {type(e).__name__}: {e}')
    _NEW_CACHE.update({'ts': now, 'days': days, 'codes': set(codes)})
    return codes


# ---------- 候选构建（纯函数） ----------

def _snap_items(snap) -> List[Tuple[str, Dict]]:
    """T4.3：快照既接受 `{symbol: quote}`，也接受清单里的 `[{...}]` 列表。

    列表形态必须自带 `symbol`，缺 `symbol` 的行跳过（不猜、不按序号补），
    也不进 reject 分母；`{symbol: quote}` 形态的拒绝守恒断言不受影响。
    """
    if isinstance(snap, dict):
        return list((snap or {}).items())
    out: List[Tuple[str, Dict]] = []
    for q in (snap or []):
        if not isinstance(q, dict):
            continue
        sym = str(q.get('symbol') or '')
        if sym:
            out.append((sym, q))
    return out


def build_candidates(snap: dict | List[dict],
                     cfg: Optional[Dict] = None,
                     *,
                     new_listing: Optional[Set[str]] = None,
                     prev_amount: Optional[Dict[str, float]] = None,
                     now_hm: str = '') -> Tuple[List[Dict], Dict[str, int]]:
    """快照 → 粗筛后的候选行（未截断）。返回 `(rows, rejects)`，纯计算、不写库。

    清单 T4.3 冻结的位置参数只有前两个（`snap`, `cfg`）；`new_listing` /
    `prev_amount` / `now_hm` 收成仅关键字，便于单测注入。
    `rejects` 每个 key 是"第一道没过的原因"，同一只票只计一次，
    各 key 相加 = 输入数量（自测里会断言这个守恒）。
    """
    C = cfg or DEFAULT_CFG
    new_listing = new_listing or set()
    prev_amount = prev_amount or {}
    passed_min = minutes_since_open(now_hm) or 5      # 09:35 首轮按 5 分钟算
    rows: List[Dict] = []
    rej = {k: 0 for k in REJECT_KEYS}

    for symbol, q in _snap_items(snap):
        base = _base(symbol)
        name = str(q.get('name') or '')
        price = float(q.get('price') or 0)
        op = float(q.get('open') or 0)
        prec = float(q.get('prec') or 0)
        vol_hand = float(q.get('vol_hand') or 0)
        lim = float(q.get('limit_up') or 0) or (round(prec * 1.1 + 1e-9, 2) if prec else 0)
        dn = float(q.get('limit_down') or 0) or (round(prec * 0.9 + 1e-9, 2) if prec else 0)

        if not base.startswith(('60', '00')):
            rej['not_main'] += 1
            continue
        if 'ST' in name or '退' in name:
            rej['st'] += 1
            continue
        if price <= 0 or op <= 0 or prec <= 0 or vol_hand <= 0:
            rej['no_price'] += 1                       # 停牌 / 无成交 / 数据不全
            continue
        if dn and price <= dn + 1e-6:
            rej['limit_down'] += 1
            continue
        if lim and price >= lim - 0.01:
            rej['sealed'] += 1                         # 开盘即封或已封：买不到
            continue
        if symbol in new_listing or base in new_listing:
            rej['new_listing'] += 1
            continue

        gain_open = (price / op - 1) * 100
        conds: Dict[str, bool] = {}
        if gain_open < C['gain_lo']:
            if _enforce('gain_5m', C):
                rej['gain_low'] += 1
                continue
            conds['gain_5m'] = False
        if gain_open > C['gain_hi']:
            if _enforce('gain_5m', C):
                rej['gain_high'] += 1
                continue
            conds['gain_5m'] = False

        vr = float(q.get('vol_ratio') or 0)
        if vr < C['vol_ratio_min']:
            if _enforce('vol_ratio', C):
                rej['vol_low'] += 1
                continue
            conds['vol_ratio'] = False

        # 5 分钟额：优先上一次快照差分，其次按已过分钟等比折算
        amt_cum = float(q.get('amount_wan') or 0)
        if symbol in prev_amount and amt_cum >= float(prev_amount[symbol]):
            amt5 = amt_cum - float(prev_amount[symbol])
            amt_src = 'snapshot_diff'
        else:
            amt5 = amt_cum * min(5.0, passed_min) / max(passed_min, 1)
            amt_src = 'snapshot_prorata'
        if amt5 < C['amount_5m_min']:
            rej['amount_low'] += 1
            continue

        bid1 = float(q.get('bid1_px') or 0)
        ask1 = float(q.get('ask1_px') or 0)
        spread: Optional[float] = None
        if bid1 > 0 and ask1 >= bid1:
            mid = (bid1 + ask1) / 2.0
            spread = (ask1 - bid1) / mid * 100 if mid > 0 else None
        if spread is not None and spread > C['spread_max']:
            rej['spread_high'] += 1
            continue

        rows.append({
            'symbol': symbol, 'name': name, 'price': price, 'open': op,
            'prec': prec, 'limit_up': lim,
            'gain_open': round(gain_open, 3),
            'gap': round((op / prec - 1) * 100, 3),
            'room': round((lim - price) / price * 100, 3) if lim else None,
            'vol_ratio': round(vr, 3),
            'vol_src': 'snapshot' if vr > 0 else 'missing',
            'amount_5m': round(amt5, 1), 'amount_src': amt_src,
            'spread': (round(spread, 3) if spread is not None else None),
            'spread_src': ('quote' if spread is not None else 'missing'),
            'bid1_vol': float(q.get('bid1_vol_1') or 0),
            'ask1_vol': float(q.get('ask1_vol_1') or 0),
            # 以下三项只在内存里传给特征层（T2.2），不进 dragon_b_candidates 的 19 列
            'bid1_px': bid1, 'ask1_px': ask1, 'amount_wan': amt_cum,
            'bid1_vol_1': float(q.get('bid1_vol_1') or 0),
            'ask1_vol_1': float(q.get('ask1_vol_1') or 0),
            # T2.6：shadow/off 的条件命中，只在内存与 dragon_shadow 行里留痕
            'conds': conds,
        })
    return rows, rej


def _gain_of(r: Dict) -> Optional[float]:
    """排序用的相对今开涨幅：优先 `chg_from_open`（清单口径），退回 `gain_open`。"""
    v = r.get('chg_from_open')
    return v if v is not None else r.get('gain_open')


def select_top_gain(rows: List[dict], n: int | None = None, *,
                    cfg: Optional[Dict] = None) -> List[dict]:
    """按相对今开涨幅降序取前 n；同值按代码升序（稳定、可复现），纯函数。

    Args:
        rows: 候选行；涨幅字段优先 `chg_from_open`，缺则用 `gain_open`。
        n: 取前几只；None 时用 `cfg['top_n']`，再回退 `DEFAULT_CFG['top_n']`。
        cfg: 仅关键字；只在 n 为 None 时提供默认值。
    """
    C = cfg or DEFAULT_CFG
    n = int(n if n is not None else C.get('top_n', DEFAULT_CFG['top_n']))
    return sorted(rows, key=lambda r: (-float(_gain_of(r) or 0),
                                       r.get('symbol') or ''))[:n]


# ---------- 落库 ----------

_DDL = '''CREATE TABLE IF NOT EXISTS dragon_b_candidates (
    trade_date TEXT NOT NULL,
    round_ts   TEXT NOT NULL,
    rank       INTEGER,
    symbol     TEXT NOT NULL,
    name       TEXT,
    price      REAL,
    open       REAL,
    prec       REAL,
    gain_open  REAL,
    gap        REAL,
    room       REAL,
    vol_ratio  REAL,
    vol_src    TEXT,
    amount_5m  REAL,
    amount_src TEXT,
    spread     REAL,
    spread_src TEXT,
    reject_stats TEXT,
    created_at TEXT,
    PRIMARY KEY (trade_date, round_ts, symbol)
)'''


def ensure_tables():
    conn = get_db_conn()
    try:
        conn.execute(_DDL)
        conn.commit()
    finally:
        conn.close()


def _save_round(trade_date: str, round_ts: str, rows: List[Dict],
                rej: Dict[str, int]):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    stats = json.dumps(rej, ensure_ascii=False)
    conn = get_db_conn()
    try:
        conn.execute(_DDL)          # 幂等：首轮前可能没走过 ensure_tables
        for i, r in enumerate(rows, 1):
            conn.execute(
                'INSERT OR REPLACE INTO dragon_b_candidates '
                '(trade_date,round_ts,rank,symbol,name,price,open,prec,gain_open,gap,room,'
                'vol_ratio,vol_src,amount_5m,amount_src,spread,spread_src,reject_stats,created_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (trade_date, round_ts, i, r['symbol'], r['name'], r['price'], r['open'],
                 r['prec'], r['gain_open'], r['gap'], r['room'], r['vol_ratio'],
                 r['vol_src'], r['amount_5m'], r['amount_src'], r['spread'],
                 r['spread_src'], stats, now))
        conn.commit()
    except Exception as e:
        print(f'[龙头B臂] 候选落库失败: {type(e).__name__}: {e}')
    finally:
        conn.close()


# ---------- 一轮 ----------

_STATE: Dict[str, object] = {'last_round_ts': 0.0, 'prev_amount': {}, 'last': []}


def run_round(snap: Optional[Dict[str, Dict]] = None,
              now: Optional[datetime] = None,
              cfg: Optional[Dict] = None) -> Dict:
    """一轮 B 臂：全市场快照 → 粗筛 → 前 20 → 落库。返回 {rows, rejects, meta}。

    调用方负责判窗口与间隔（`round_due`）；本函数不自我限流，方便回放。
    """
    C = cfg or DEFAULT_CFG
    now = now or datetime.now()
    hm = now.strftime('%H:%M')
    if snap is None:
        from services.pick.intraday_dragon import fetch_snapshot, universe_codes
        t0 = time.time()
        snap = fetch_snapshot(universe_codes(), threads=6)
        cost = round(time.time() - t0, 2)
    else:
        cost = 0.0
    rows, rej = build_candidates(
        snap, C,                                   # type: ignore[arg-type]
        new_listing=load_recent_listings(C['new_days']),
        prev_amount=dict(_STATE['prev_amount']), now_hm=hm)
    top = select_top_gain(rows, C['top_n'])
    trade_date = now.strftime('%Y-%m-%d')
    round_ts = now.strftime('%Y-%m-%d %H:%M:%S')
    _save_round(trade_date, round_ts, top, rej)
    _STATE['last_round_ts'] = time.time()
    _STATE['prev_amount'] = {s: float(q.get('amount_wan') or 0)
                             for s, q in (snap or {}).items()}
    _STATE['last'] = top
    print(f'[龙头B臂 {now:%H:%M:%S}] 快照 {len(snap or {})} 只 {cost}s → '
          f'候选 {len(rows)} / 前 {len(top)}；剔除 {rej}')
    return {'rows': top, 'rejects': rej,
            'meta': {'snap_n': len(snap or {}), 'rows_n': len(rows),
                     'top_n': len(top), 'cost_s': cost, 'round_ts': round_ts}}


def last_round() -> List[Dict]:
    return list(_STATE['last'])                               # type: ignore[arg-type]


def reset_state():
    _STATE.update({'last_round_ts': 0.0, 'prev_amount': {}, 'last': []})
