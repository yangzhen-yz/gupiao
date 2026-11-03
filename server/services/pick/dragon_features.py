"""dragon_features.py - 龙头通道特征层（清单 T2.2）

职责
----
把"这一分钟这一只票"的可观测状态算成 11 个具名特征，并落 `dragon_features`。
它是 T2.1（候选）与 T2.3（score_v2 / 结构 / 流动性）之间的唯一数据层：
候选层只负责"看谁"，本层只负责"看见的是什么"，打分只读这里的输出。

11 个必需特征（公式见 server/analysis_out/docs_backup_20261102/评分与龙头优化-开发任务清单-2026-11-01.md T2.2）
--------------------------------------------------------------
chg_from_open / gap / room / vol_ratio_same_time / amount_5m / spread /
vwap_share / vwap_dist / fake_break / ask_bid_ratio / pause_dur

三个附加特征**不进覆盖率分母**，但同样落库：
- `shrink`      停顿段均量 / 拉升段均量（结构判据用）
- `rank_gain`   B 臂候选的名次（A 臂为 NULL）
- `sector_rank` / `stock_flow` 按 T0.2 探针结论**恒为 NULL**：09:45 拿不到
  当日板块净流入与全市场个股资金，不允许用昨日值兜底（来源写 `unavailable`）。

缺失规则（硬约束）
------------------
1. 缺失**不填中档、不填 0**，一律 NULL，并在 `sources` 里说明为什么缺；
2. `coverage = 非空必需特征数 / 11`；`coverage < 0.60` 时 `score_ready=False`，
   T2.3 不允许给它打分（"只记录不打分"）；
3. `−100` 只留给"封板"，本层不产生 −100，也不用 −100 表示缺失。

无前视
------
所有特征只读 `bars[0..i]`（i = 当前已完成的分钟）与同一时刻的快照字段；
`compute_features(bars[:i+1], ..., i=i)` 与用完整序列算第 i 点结果必须逐字段相等，
自测里会拿"后追加未来 bar"验证这一点。
"""
import json
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from db.database import get_db_conn

FEATURE_VERSION = 't2.2-2026-11-01'

REQUIRED_FEATURES = (
    'chg_from_open', 'gap', 'room', 'vol_ratio_same_time', 'amount_5m',
    'spread', 'vwap_share', 'vwap_dist', 'fake_break', 'ask_bid_ratio',
    'pause_dur',
)
OPTIONAL_FEATURES = ('shrink', 'rank_gain', 'sector_rank', 'stock_flow')

COVERAGE_MIN = 0.60
FAKE_BREAK_RECOVER_BARS = 2          # 跌破 VWAP 后 2 根分钟线内收回 = 假破


def limit_up_price(prec: float, pct: float = 10.0) -> float:
    """主板涨停价：昨收 ×(1+pct%)，四舍五入到分。与 intraday_dragon 同式。"""
    return round(float(prec) * (1 + pct / 100.0) + 1e-9, 2)


def _pct(a: float, b: float) -> Optional[float]:
    if not b:
        return None
    return round((a / b - 1) * 100, 3)


def _r(x: Optional[float], nd: int = 3) -> Optional[float]:
    return None if x is None else round(float(x), nd)


def _hm(t: str) -> str:
    """'0936' / '09:36' -> '09:36'（主键 ts 统一成带冒号，跨轮次才不重行）。"""
    s = str(t or '').strip()
    if len(s) == 4 and s.isdigit():
        return f'{s[:2]}:{s[2:]}'
    return s


# ---------- 分时派生 ----------

def vwap_above_share(bars: Sequence[Dict], i: int) -> Optional[float]:
    """09:30 至第 i 分钟：收盘价 ≥ 分时均线的分钟数 / 总分钟数。

    bars[0] 是集合竞价合并的那根，不参与占比（否则 0 分钟会除零），
    与 `intraday_dragon._comps` 的 `above / i` 完全同口径。
    """
    if i < 1 or len(bars) < i + 1:
        return None
    above = sum(1 for j in range(1, i + 1) if bars[j]['price'] >= bars[j]['vwap'])
    return round(above / i, 3)


def fake_break(bars: Sequence[Dict], i: int,
               recover_bars: int = FAKE_BREAK_RECOVER_BARS) -> Optional[bool]:
    """跌破 VWAP 后 `recover_bars` 根内收盘重新站上 → True。

    - 从未跌破：False（有明确观测，不是缺失）；
    - 跌破后未收回：False；
    - 当前仍在水下：False（正在破位，不算假破）。
    """
    if i < 2 or len(bars) < i + 1:
        return None
    cur = bars[i]['price']
    if cur < bars[i]['vwap']:
        return False
    for j in range(1, i + 1):
        if bars[j]['price'] < bars[j]['vwap']:
            for k in range(j + 1, min(j + int(recover_bars), i) + 1):
                if bars[k]['price'] >= bars[k]['vwap']:
                    return True
    return False


def vwap_breach_pct(bars: Sequence[Dict], i: int) -> float:
    """最大跌破幅度（%），没有跌破为 0。只读 1..i。"""
    if i < 1:
        return 0.0
    return max(((bars[j]['vwap'] - bars[j]['price']) / bars[j]['vwap'] * 100)
               for j in range(1, i + 1) if bars[j]['vwap'])


def pause_state(bars: Sequence[Dict], i: int):
    """高点之后的停顿：(pause_dur, shrink, hi_idx)。

    pause_dur = i − 高点所在分钟；高点取**最后一次**出现的最高价（与 `_comps` 同）；
    shrink = 高点之后均量 / 拉升段（1..hi_idx）均量；没有停顿段时为 None。
    """
    if i < 1:
        return None, None, None
    hi = max(x['price'] for x in bars[:i + 1])
    hi_idx = max(j for j in range(i + 1) if bars[j]['price'] == hi)
    pause_dur = i - hi_idx
    shrink = None
    if hi_idx >= 1 and i > hi_idx:
        pull = [bars[j]['vol'] for j in range(1, hi_idx + 1)]
        pause = [bars[j]['vol'] for j in range(hi_idx + 1, i + 1)]
        if pull and pause and sum(pull) > 0:
            shrink = round((sum(pause) / len(pause)) / (sum(pull) / len(pull)), 3)
    return pause_dur, shrink, hi_idx


def amount_5m_from_bars(bars: Sequence[Dict], i: int):
    """最近 5 分钟成交额（万元）。用累计成交额差分，返回 (值, 来源)。

    分钟线本身就是累计额（cum_amt，单位元），所以末 5 根差分就是真实的
    5 分钟成交额；不足 5 根时按实际根数算，来源标 `minute_bars_partial`。
    """
    if i < 1 or len(bars) < i + 1:
        return None, 'missing'
    end = bars[i].get('cum_amt') or 0.0
    j0 = max(0, i - 4)                       # 含当前分钟共 5 根
    start = (bars[j0 - 1].get('cum_amt') or 0.0) if j0 > 0 else 0.0
    if end <= 0 or end < start:
        return None, 'missing'
    n = i - j0 + 1
    return round((end - start) / 1e4, 1), ('minute_bars' if n >= 5 else 'minute_bars_partial')


# ---------- 主函数 ----------

def compute_features(bars: Sequence[Dict], q: Optional[Dict] = None, i: Optional[int] = None,
                     now_hm: str = '', rank_gain: Optional[int] = None,
                     prev_amount: Optional[float] = None,
                     prev_hm: str = '') -> Dict:
    """只读 bars[0..i] 与同一时刻快照，返回特征字典（缺失均为 None）。

    `prev_amount` / `prev_hm` 是上一轮快照的累计成交额（万元）与时刻，
    仅在分时拿不到累计额时用于 `snapshot_diff` / `snapshot_prorata` 回退。
    """
    q = q or {}
    if i is None:
        i = len(bars) - 1
    i = int(i)
    if i < 0 or i >= len(bars):
        raise ValueError(f'分钟下标越界: i={i} len={len(bars)}')

    b = bars[i]
    price = float(b['price'])
    opening = float(q.get('open') or bars[0]['price'])
    prec = float(q.get('prec') or 0)
    lim = float(q.get('limit_up') or 0) or (limit_up_price(prec) if prec else 0.0)

    f: Dict[str, object] = {}
    src: Dict[str, str] = {}

    # 价格类
    f['chg_from_open'] = _pct(price, opening)
    src['chg_from_open'] = 'minute_bars' if q.get('open') else 'minute_bars_open_fallback'
    f['gap'] = _pct(opening, prec) if prec else None
    src['gap'] = 'snapshot' if prec else 'missing'
    f['room'] = _pct(lim, price) if lim and price else None
    src['room'] = 'snapshot' if lim else 'missing'

    # 量能 / 盘口（只能来自快照）
    vr = float(q.get('vol_ratio') or 0)
    f['vol_ratio_same_time'] = round(vr, 3) if vr > 0 else None
    src['vol_ratio_same_time'] = 'snapshot' if vr > 0 else 'missing'

    amt, amt_src = amount_5m_from_bars(bars, i)
    if amt is None:
        cum = float(q.get('amount_wan') or 0)
        if cum > 0:
            amt, amt_src = _fallback_amount_5m(cum, float(prev_amount or 0), now_hm, prev_hm)
    f['amount_5m'] = amt
    src['amount_5m'] = amt_src

    bid1_px = float(q.get('bid1_px') or 0)
    ask1_px = float(q.get('ask1_px') or 0)
    if bid1_px > 0 and ask1_px >= bid1_px:
        mid = (bid1_px + ask1_px) / 2.0
        f['spread'] = round((ask1_px - bid1_px) / mid * 100, 3) if mid > 0 else None
        src['spread'] = 'quote' if f['spread'] is not None else 'missing'
    else:
        f['spread'] = None
        src['spread'] = 'missing'

    bid1_vol = float(q.get('bid1_vol_1') or 0)
    ask1_vol = float(q.get('ask1_vol_1') or 0)
    f['ask_bid_ratio'] = round(ask1_vol / bid1_vol, 3) if bid1_vol > 0 else None
    src['ask_bid_ratio'] = 'quote' if bid1_vol > 0 else 'missing'

    # 分时派生
    f['vwap_share'] = vwap_above_share(bars, i)
    src['vwap_share'] = 'minute_bars' if f['vwap_share'] is not None else 'missing'
    vwap = float(b.get('vwap') or 0)
    f['vwap_dist'] = _pct(price, vwap) if vwap > 0 else None
    src['vwap_dist'] = 'minute_bars' if f['vwap_dist'] is not None else 'missing'
    f['fake_break'] = fake_break(bars, i)
    src['fake_break'] = 'minute_bars' if f['fake_break'] is not None else 'missing'
    pause_dur, shrink, hi_idx = pause_state(bars, i)
    f['pause_dur'] = pause_dur
    src['pause_dur'] = 'minute_bars' if pause_dur is not None else 'missing'

    # 附加特征（不进覆盖率分母）
    f['shrink'] = shrink
    src['shrink'] = 'minute_bars' if shrink is not None else 'no_pause'
    f['rank_gain'] = rank_gain
    src['rank_gain'] = 'b_arm' if rank_gain is not None else 'not_b_arm'
    f['sector_rank'] = None
    src['sector_rank'] = 'unavailable'          # T0.2：09:45 无当日板块净流入
    f['stock_flow'] = None
    src['stock_flow'] = 'unavailable'           # T0.2：无全市场当日个股资金

    missing = [k for k in REQUIRED_FEATURES if f.get(k) is None]
    coverage = round((len(REQUIRED_FEATURES) - len(missing)) / len(REQUIRED_FEATURES), 3)

    return {
        't': (b.get('t') or now_hm or ''),
        'ts': _hm(now_hm or b.get('t') or ''),
        'price': price,
        'open': opening,
        'prec': prec,
        'limit_up': lim,
        'vwap': round(vwap, 3) if vwap else None,
        'above_vwap': (price >= vwap) if vwap else None,
        'breach_pct': round(vwap_breach_pct(bars, i), 3),
        'hi_idx': hi_idx,
        'bar_count': i + 1,
        'features': f,
        'sources': src,
        'missing': missing,
        'coverage': coverage,
        'score_ready': coverage >= COVERAGE_MIN,
        'feature_version': FEATURE_VERSION,
    }


def _fallback_amount_5m(cum_wan: float, prev_wan: float, now_hm: str, prev_hm: str):
    """分时拿不到累计额时才走的回退：优先真差分，否则按已过分钟等比折算。"""
    minutes = _minutes_since_open(now_hm) or 5
    prev_minutes = _minutes_since_open(prev_hm)
    if prev_wan > 0 and cum_wan >= prev_wan and prev_minutes > 0:
        span = max(1, minutes - prev_minutes)
        diff = cum_wan - prev_wan
        if span >= 4:                      # 间隔接近 5 分钟，直接当 5 分钟额
            return round(diff, 1), 'snapshot_diff'
        return round(diff * 5.0 / span, 1), 'snapshot_diff_scaled'
    return round(cum_wan * min(5.0, minutes) / max(minutes, 1), 1), 'snapshot_prorata'


def _minutes_since_open(hm: str) -> int:
    try:
        h, m = str(hm or '').split(':')[:2]
        return max(0, (int(h) * 60 + int(m)) - (9 * 60 + 30))
    except (ValueError, TypeError):
        return 0


# ---------- 落库 ----------

_DDL = '''CREATE TABLE IF NOT EXISTS dragon_features (
    trade_date  TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    ts          TEXT NOT NULL,
    name        TEXT,
    price       REAL,
    open        REAL,
    prec        REAL,
    limit_up    REAL,
    vwap        REAL,
    chg_from_open REAL,
    gap         REAL,
    room        REAL,
    vol_ratio_same_time REAL,
    vol_src     TEXT,
    amount_5m   REAL,
    amount_src  TEXT,
    spread      REAL,
    spread_src  TEXT,
    vwap_share  REAL,
    vwap_dist   REAL,
    fake_break  INTEGER,
    ask_bid_ratio REAL,
    pause_dur   INTEGER,
    shrink      REAL,
    sector_rank REAL,
    stock_flow  REAL,
    rank_gain   INTEGER,
    coverage    REAL,
    score_ready INTEGER,
    missing     TEXT,
    sources     TEXT,
    feature_version TEXT,
    updated_at  TEXT,
    PRIMARY KEY (trade_date, symbol, ts)
)'''

_ROW_KEYS = ('chg_from_open', 'gap', 'room', 'vol_ratio_same_time', 'amount_5m',
             'spread', 'vwap_share', 'vwap_dist', 'fake_break', 'ask_bid_ratio',
             'pause_dur', 'shrink', 'sector_rank', 'stock_flow', 'rank_gain')


def ensure_tables():
    conn = get_db_conn()
    try:
        conn.execute(_DDL)
        conn.commit()
    finally:
        conn.close()


def save_features(trade_date: str, rows: List[Dict]) -> int:
    """批量落库；同一 (日期, 代码, 分钟) 覆盖更新。返回写入行数。

    rows 每项：{symbol, name, feat}（feat 为 compute_features 的返回）。
    """
    if not rows:
        return 0
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db_conn()
    try:
        conn.execute(_DDL)
        for r in rows:
            feat = r.get('feat') or {}
            f = feat.get('features') or {}
            src = feat.get('sources') or {}
            conn.execute(
                'INSERT OR REPLACE INTO dragon_features '
                '(trade_date,symbol,ts,name,price,open,prec,limit_up,vwap,chg_from_open,gap,room,'
                'vol_ratio_same_time,vol_src,amount_5m,amount_src,spread,spread_src,vwap_share,'
                'vwap_dist,fake_break,ask_bid_ratio,pause_dur,shrink,sector_rank,stock_flow,'
                'rank_gain,coverage,score_ready,missing,sources,feature_version,updated_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (trade_date, r['symbol'], feat.get('ts') or feat.get('t') or '', r.get('name'),
                 feat.get('price'), feat.get('open'), feat.get('prec'), feat.get('limit_up'),
                 feat.get('vwap'), f.get('chg_from_open'), f.get('gap'), f.get('room'),
                 f.get('vol_ratio_same_time'), src.get('vol_ratio_same_time'),
                 f.get('amount_5m'), src.get('amount_5m'), f.get('spread'), src.get('spread'),
                 f.get('vwap_share'), f.get('vwap_dist'),
                 (None if f.get('fake_break') is None else int(bool(f['fake_break']))),
                 f.get('ask_bid_ratio'), f.get('pause_dur'), f.get('shrink'),
                 f.get('sector_rank'), f.get('stock_flow'), f.get('rank_gain'),
                 feat.get('coverage'), int(bool(feat.get('score_ready'))),
                 json.dumps(feat.get('missing') or [], ensure_ascii=False),
                 json.dumps(src, ensure_ascii=False), feat.get('feature_version') or FEATURE_VERSION,
                 now))
        conn.commit()
        return len(rows)
    except Exception as e:
        print(f'[龙头特征] 落库失败: {type(e).__name__}: {e}')
        return 0
    finally:
        conn.close()
