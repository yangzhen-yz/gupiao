"""dragon_backtest.py - 龙头通道回测引擎（清单 T3.1，只读研究）

职责
----
把 T0.3 的回放口径固化成**纯函数**，再补上清单 T3.1 要的四组对照：

1. 臂别：A 臂（结构优先，现行候选池） vs B 臂（全市场涨幅前 20 + 同一套结构判定）；
2. 阈值：`score_v2 >= 0` vs `>= 70`，**同一批触发上比较，不换样本**；
3. 止损：`D_no_stop` vs `D_stop`（−2%）；
4. 买法：`immediate`（触发后下一分钟开盘） vs `pullback`（VWAP±0.3% 限价，最多等 3 分钟）。

四组两两分开报，不混算；同一笔触发的不同组合共用一次价格路径，只改成交/出口规则。

与 T0.3 的三处口径差异（都为了"无前视"）
----------------------------------------
1. **出口判定**：T0.3 用 `sealed_close`（收盘封住）决定是否持有到次日 —— 这要等收盘
   才知道，属于前视。本引擎主口径改成 `sealed_1030`（10:30 那一刻是否在涨停价），
   这是盘中真能执行的判据；`sealed_close` 只保留为 `exit_rule='d_close'` 的对照列，
   报告里标明它是前视口径、不进验收。
2. **回踩买不叠买入滑点**：回踩是限价单，成交价不可能高于限价；再加 0.2% 等于
   假设你付得比自己的限价还高。卖出侧滑点照常。
3. **B 臂只用当刻可见数据**：每轮候选由 `bars[0..i]` 生成，`i` 是轮次时刻；
   自测用"追加未来 bar 后重算"证明逐字段相等。

硬边界
------
- 纯函数：不联网、不写库、不读 app_settings；唯一的外部依赖是 `dragon_entry` /
  `dragon_features` / `dragon_score` 三个纯计算模块和 `app.config` 的默认值。
- **不被线上链路导入**：`intraday_dragon` / `dragon_push` 都不引用本模块。
- 九列回填（`fill_price` … `excess`）只在 `apply_backfill()` 里写，且只改
  `fill_price IS NULL` 的行；默认 dry-run，不悄悄改影子表。
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from app.config import (INTRADAY_DRAGON_B_AMOUNT_5M, INTRADAY_DRAGON_B_FIRST,
                        INTRADAY_DRAGON_B_GAIN_HI, INTRADAY_DRAGON_B_GAIN_LO,
                        INTRADAY_DRAGON_B_LAST, INTRADAY_DRAGON_B_NEW_DAYS,
                        INTRADAY_DRAGON_B_ROUND_SEC, INTRADAY_DRAGON_B_SPREAD_MAX,
                        INTRADAY_DRAGON_B_TOP_N, INTRADAY_DRAGON_B_VOL_RATIO,
                        INTRADAY_DRAGON_ENTRY_BAND_PCT, INTRADAY_DRAGON_MIN_SCORE,
                        INTRADAY_DRAGON_SELL_TIME, INTRADAY_DRAGON_STOP_PCT,
                        INTRADAY_DRAGON_VWAP_SHARE_FROM, INTRADAY_DRAGON_VWAP_SHARE_MIN)
from services.pick import dragon_entry
from services.pick import dragon_features
from services.pick import dragon_score
from services.pick.dragon_shadow import PENDING_COLUMNS

ENGINE_VERSION = 't3.1-2026-11-01'
PROTOCOL_VERSION = 'v1.0'          # analysis_out/dragon_v2_protocol.md（2026-11-01 冻结）
FEE_PCT = 0.15                      # 往返费用（佣金+印花+过户），与 rec_sim 同口径
SLIPS = (0.1, 0.2, 0.3)             # 单边滑点三档（%）
SLIP_MAIN = 0.2
STOP_PCT = float(INTRADAY_DRAGON_STOP_PCT or 2.0)
SELL_HM = str(INTRADAY_DRAGON_SELL_TIME or '10:30')
BAND_PCT = float(INTRADAY_DRAGON_ENTRY_BAND_PCT or 0.3)
PULLBACK_WAIT_SEC = 180             # 回踩最多等 3 分钟（清单 T4.1 第 10 项）
WINDOW_MIN = 30                     # 形态扫描窗口（与 T0.3 一致）
DEDUP_DAYS = 30                     # 同票只留最早一跳
SCORE_THRESHOLDS = (float(INTRADAY_DRAGON_MIN_SCORE or 0), 70.0)

BUY_METHODS = ('immediate', 'pullback')
EXIT_RULES = ('d_1030', 'd_close')  # 主口径 / 前视对照
ARMS = ('A', 'B')


# ---------- 参数快照 ----------

def cfg_snapshot(overrides: Optional[Dict] = None) -> Dict:
    """本轮回测用到的**全部**阈值。任何一项变了 `param_version` 就会变。"""
    cfg = {
        'engine_version': ENGINE_VERSION,
        'protocol_version': PROTOCOL_VERSION,
        'top_n': int(INTRADAY_DRAGON_B_TOP_N),
        'gain_lo': float(INTRADAY_DRAGON_B_GAIN_LO),
        'gain_hi': float(INTRADAY_DRAGON_B_GAIN_HI),
        'vol_ratio_min': float(INTRADAY_DRAGON_B_VOL_RATIO),
        'amount_5m_min': float(INTRADAY_DRAGON_B_AMOUNT_5M),
        'spread_max': float(INTRADAY_DRAGON_B_SPREAD_MAX),
        'first': str(INTRADAY_DRAGON_B_FIRST),
        'last': str(INTRADAY_DRAGON_B_LAST),
        'round_sec': int(INTRADAY_DRAGON_B_ROUND_SEC),
        'new_days': int(INTRADAY_DRAGON_B_NEW_DAYS),
        'score_thresholds': list(SCORE_THRESHOLDS),
        'stop_pct': STOP_PCT,
        'slips': list(SLIPS),
        'slip_main': SLIP_MAIN,
        'fee_pct': FEE_PCT,
        'sell_hm': SELL_HM,
        'band_pct': BAND_PCT,
        'pullback_wait_sec': PULLBACK_WAIT_SEC,
        'window_min': WINDOW_MIN,
        'dedup_days': DEDUP_DAYS,
        'vwap_share_min': float(INTRADAY_DRAGON_VWAP_SHARE_MIN),
        'vwap_share_from': str(INTRADAY_DRAGON_VWAP_SHARE_FROM),
        # 七条条件在回测里一律按"改造前"固定：量比/涨幅/VWAP/回踩 live，
        # 止损/仓位 shadow，板块 unavailable。写死是为了同输入同输出。
        'cond_gain_5m_on': True, 'cond_gain_5m_mode': 'live',
        'cond_vol_on': True, 'cond_vol_mode': 'live',
        'cond_vwap70_on': True, 'cond_vwap70_mode': 'live',
        'cond_pullback_on': True, 'cond_pullback_mode': 'live',
        'cond_sector_on': True, 'cond_sector_mode': 'shadow',
        'cond_stop_on': True, 'cond_position_on': False,
    }
    if overrides:
        cfg.update(overrides)
    return cfg


def param_version(cfg: Optional[Dict] = None) -> str:
    """参数指纹（sha1 前 10 位）。同输入同参数 → 同指纹，可直接核对两次产物。"""
    blob = json.dumps(cfg or cfg_snapshot(), ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode('utf-8')).hexdigest()[:10]


# ---------- 时间 ----------

def hm_min(hm: str) -> int:
    """'09:35' / '0935' → 575。非法值抛 ValueError（回测里静默容错会出假样本）。"""
    s = str(hm or '').strip().replace(':', '')
    if len(s) != 4 or not s.isdigit():
        raise ValueError(f'非法时刻: {hm!r}')
    return int(s[:2]) * 60 + int(s[2:])


def min_hm(m: int) -> str:
    return f'{m // 60:02d}:{m % 60:02d}'


def rounds_of(cfg: Optional[Dict] = None) -> List[str]:
    """B 臂重排轮次：09:35 起每 5 分钟到 10:15（含两端）。"""
    C = cfg or cfg_snapshot()
    a, b, step = hm_min(C['first']), hm_min(C['last']), int(C['round_sec'] // 60 or 5)
    return [min_hm(m) for m in range(a, b + 1, max(step, 1))]


def bar_index(bars: Sequence[Dict], hm: str) -> Optional[int]:
    """时刻 ≤ hm 的最后一根分钟线下标；没有返回 None。"""
    target = hm_min(hm)
    hit = None
    for i, b in enumerate(bars):
        if hm_min(b['t']) <= target:
            hit = i
        else:
            break
    return hit


def bar_at(bars: Sequence[Dict], t: str) -> Optional[Dict]:
    for b in bars:
        if b['t'] == t:
            return b
    return None


def bar_at_or_before(bars: Sequence[Dict], t: str) -> Optional[Dict]:
    target = hm_min(t)
    hit = None
    for b in bars:
        if hm_min(b['t']) <= target:
            hit = b
        else:
            break
    return hit


# ---------- 快照与 B 臂 ----------

def minute_quote(bars: Sequence[Dict], i: int, prec: float, name: str = '',
                 limit_up: Optional[float] = None) -> Dict:
    """把 bars[0..i] 折成 T2.1 需要的快照字段。**只读前缀**。

    盘口（买一/卖一价与量）分时里没有 → 不填、不猜；`build_candidates`
    对 spread=None 的处理是"不剔除、也不算 spread_high"，与 T2.1 一致。
    """
    if i < 0 or i >= len(bars):
        raise ValueError(f'分钟下标越界: i={i} len={len(bars)}')
    b = bars[i]
    lim = limit_up or dragon_features.limit_up_price(prec)
    return {
        'name': name,
        'price': float(b['price']),
        'open': float(bars[0]['price']),
        'prec': float(prec),
        'limit_up': float(lim),
        'limit_down': round(float(prec) * 0.9 + 1e-9, 2),
        'vol_hand': float(b.get('cum_vol') or 0.0),
        'amount_wan': float(b.get('cum_amt') or 0.0) / 1e4,
        'vol_ratio': float(b.get('vol_ratio_run') or 0.0),
    }


def amount_5m_ago(bars: Sequence[Dict], i: int) -> Optional[float]:
    """5 分钟前的累计成交额（万元），给 `build_candidates` 做真差分。"""
    if i < 5:
        return None
    v = float(bars[i - 5].get('cum_amt') or 0.0)
    return v / 1e4 if v > 0 else None


def build_round(snap: Dict[str, Dict], cfg: Optional[Dict] = None,
                new_listing: Optional[set] = None,
                prev_amount: Optional[Dict[str, float]] = None,
                now_hm: str = '') -> Dict:
    """一轮 B 臂：粗筛 → 按相对今开涨幅取前 N。返回 {rows, rejects, top}。"""
    C = cfg or cfg_snapshot()
    rows, rej = dragon_entry.build_candidates(
        snap, cfg=C, new_listing=new_listing or set(),
        prev_amount=prev_amount or {}, now_hm=now_hm)
    top = dragon_entry.select_top_gain(rows, C['top_n'])
    return {'rows': rows, 'rejects': rej, 'top': top}


# ---------- 特征与分数 ----------

def features_at(bars: Sequence[Dict], i: int, prec: float,
                limit_up: Optional[float] = None,
                rank_gain: Optional[int] = None,
                now_hm: str = '') -> Dict:
    """第 i 分钟的特征（只读 bars[0..i]）。盘口两项缺失，覆盖率 9/11。"""
    q = {
        'open': float(bars[0]['price']),
        'prec': float(prec),
        'limit_up': float(limit_up or dragon_features.limit_up_price(prec)),
        'vol_ratio': float(bars[i].get('vol_ratio_run') or 0.0),
        'amount_wan': float(bars[i].get('cum_amt') or 0.0) / 1e4,
    }
    return dragon_features.compute_features(
        bars, q, i=i, now_hm=now_hm or bars[i]['t'], rank_gain=rank_gain)


def score_at(feat: Dict) -> Optional[float]:
    """score_v2 的分数（缺失因子从分母剔除；覆盖率 <60% 返回 None）。"""
    if not feat:
        return None
    return dragon_score.score_v2(feat)[0]


# ---------- 成交 / 止损 / 出口 ----------

def _blank(buy: str, slip: float) -> Dict:
    return {
        'buy_method': buy, 'status': 'unfilled', 'unfilled_reason': None,
        'fill_time': None, 'fill_price': None, 'fill_delay_sec': None,
        'slippage_pct': slip, 'buy_slip_pct': None,
        'stop_price': None, 'stop_hit': False, 'stop_time': None, 'gap_slip': None,
        'exit_reason': None, 'exit_time': None, 'exit_price': None,
        'ret_gross': None, 'ret_net': None,
    }


def simulate(bars: Sequence[Dict], trigger: Dict, lim: float,
             sealed_close: bool, next_open: Optional[float],
             slip_pct: float = SLIP_MAIN, stop_pct: float = STOP_PCT,
             buy: str = 'immediate', vwap: Optional[float] = None,
             band_pct: float = BAND_PCT, wait_sec: int = PULLBACK_WAIT_SEC,
             exit_rule: str = 'd_1030', sell_hm: str = SELL_HM) -> Dict:
    """单笔：买入 → 止损 → 出口。价格全部在原始未复权空间。

    - `buy='immediate'`：触发后下一分钟**开盘**成交，买入侧加滑点；
    - `buy='pullback'`：触发时 VWAP×(1+band%) 挂限价，最多等 `wait_sec` 秒；
      开盘已低于限价按开盘成交，盘中最低触及按限价成交；**买入侧不加滑点**；
    - 止损：分钟最低价触及 → 下一分钟开盘成交，跳空按开盘价（不假设止损价一定成交）；
    - 出口：`d_1030` 看 10:30 那一刻是否在涨停（无前视）；`d_close` 看收盘（T0.3 口径，
      前视对照，不进验收）。
    """
    if buy not in BUY_METHODS:
        raise ValueError(f'未知买法: {buy!r}')
    if exit_rule not in EXIT_RULES:
        raise ValueError(f'未知出口口径: {exit_rule!r}')
    out = _blank(buy, slip_pct)
    slip = slip_pct / 100.0
    t0 = trigger['t']
    i0 = next((i for i, b in enumerate(bars) if b['t'] == t0), None)
    if i0 is None:
        out['unfilled_reason'] = 'trigger_bar_missing'
        return out

    j = None
    if buy == 'immediate':
        j = i0 + 1
        if j >= len(bars):
            out['unfilled_reason'] = 'next_bar_missing'
            return out
        fb = bars[j]
        if fb['open'] <= 0:
            out['unfilled_reason'] = 'bad_open'
            return out
        if fb['open'] >= lim - 0.01:
            out['unfilled_reason'] = 'next_open_at_limit'
            return out
        fill_ref = float(fb['open'])
        buy_net = min(fill_ref * (1 + slip), lim - 0.01)
        out['buy_slip_pct'] = slip_pct
    else:
        if not vwap:
            out['unfilled_reason'] = 'vwap_missing'
            return out
        limit_px = min(round(float(vwap) * (1 + band_pct / 100.0) + 1e-9, 2), lim - 0.01)
        out['pullback_limit'] = limit_px
        wait_bars = max(1, int(math.ceil(wait_sec / 60.0)))
        for k in range(i0 + 1, min(i0 + 1 + wait_bars, len(bars))):
            b = bars[k]
            if b['open'] > 0 and b['open'] <= limit_px:
                j, fill_ref = k, float(b['open'])
                break
            if b['low'] <= limit_px + 1e-9:
                j, fill_ref = k, limit_px
                break
        if j is None:
            out['unfilled_reason'] = 'pullback_timeout'
            return out
        buy_net = min(fill_ref, lim - 0.01)
        out['buy_slip_pct'] = 0.0

    out['fill_time'] = bars[j]['t']
    out['fill_delay_sec'] = (hm_min(bars[j]['t']) - hm_min(t0)) * 60
    out['fill_price'] = round(fill_ref, 3)
    out['status'] = 'filled'

    stop_price = (round(buy_net * (1 - stop_pct / 100.0), 2) if stop_pct > 0 else None)
    out['stop_price'] = stop_price
    if stop_pct > 0:
        for k in range(j, len(bars)):
            b = bars[k]
            if hm_min(b['t']) > hm_min(sell_hm):
                break
            if b['low'] <= stop_price + 1e-9:
                out['stop_hit'] = True
                out['stop_time'] = b['t']
                if k + 1 < len(bars) and hm_min(bars[k + 1]['t']) <= hm_min(sell_hm):
                    exit_ref, exit_t = float(bars[k + 1]['open']), bars[k + 1]['t']
                else:
                    exit_ref, exit_t = float(b['close']), b['t']
                out['gap_slip'] = exit_ref / stop_price - 1 if stop_price else None
                out['exit_reason'] = 'stop'
                out['exit_time'] = exit_t
                out['exit_price'] = round(exit_ref, 3)
                sell_net = exit_ref * (1 - slip)
                out['ret_gross'] = (exit_ref / fill_ref - 1) * 100
                out['ret_net'] = (sell_net / buy_net - 1) * 100 - FEE_PCT
                return out

    b_sell = bar_at_or_before(bars, sell_hm)
    if not b_sell:
        out['status'] = 'exit_unavailable'
        out['unfilled_reason'] = 'sell_bar_missing'
        return out
    sealed_1030 = b_sell['close'] >= lim - 0.01
    out['sealed_1030'] = bool(sealed_1030)
    hold = sealed_1030 if exit_rule == 'd_1030' else bool(sealed_close)
    if hold:
        if next_open is None:
            out['status'] = 'exit_unavailable'
            out['unfilled_reason'] = 'next_open_missing'
            return out
        exit_ref, exit_t = float(next_open), 'next_open'
        out['exit_reason'] = 'next_open_sealed'
    else:
        exit_ref, exit_t = float(b_sell['close']), b_sell['t']
        # 分时时刻有 '1030' 与 '10:30' 两种写法，比较前统一成分钟数
        out['exit_reason'] = ('time_1030' if hm_min(b_sell['t']) == hm_min(sell_hm)
                              else 'time_1030_last')
    out['exit_time'] = exit_t
    out['exit_price'] = round(exit_ref, 3)
    sell_net = exit_ref * (1 - slip)
    out['ret_gross'] = (exit_ref / fill_ref - 1) * 100
    out['ret_net'] = (sell_net / buy_net - 1) * 100 - FEE_PCT
    return out


# ---------- 统计 ----------

def wilson(k: int, n: int, z: float = 1.96) -> Tuple[Optional[float], Optional[float]]:
    if not n:
        return (None, None)
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, (c - h) * 100), min(100.0, (c + h) * 100))


def _tcrit(n: int) -> float:
    table = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
             8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160,
             14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093,
             20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
             26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042}
    return table.get(max(1, n - 1), 1.96)


def stat(vals: Iterable[Optional[float]]) -> Dict:
    """n / 胜率 / Wilson / 均值 / t 区间 / 中位 / 盈亏比 / 最差。空样本返回 {'n': 0}。"""
    xs = [float(v) for v in vals if v is not None]
    n = len(xs)
    if not n:
        return {'n': 0}
    win = sum(1 for v in xs if v > 0)
    mean = sum(xs) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in xs) / max(1, n - 1))
    se = sd / math.sqrt(n)
    tc = _tcrit(n)
    lo, hi = wilson(win, n)
    gains = [v for v in xs if v > 0]
    losses = [v for v in xs if v < 0]
    pf = ((sum(gains) / len(gains)) / (-sum(losses) / len(losses))
          if gains and losses else None)
    return {
        'n': n, 'win': win, 'win_rate': round(win / n * 100, 1),
        'wilson_lo': round(lo, 1), 'wilson_hi': round(hi, 1),
        'mean': round(mean, 3), 'sd': round(sd, 3),
        'mean_lo': round(mean - tc * se, 3), 'mean_hi': round(mean + tc * se, 3),
        'median': round(sorted(xs)[n // 2], 3),
        'pf': (round(pf, 2) if pf else None),
        'worst': round(min(xs), 3),
    }


# ---------- 影子表九列回填 ----------

def backfill_values(row: Dict) -> Dict:
    """把一笔回测结果映射成 `dragon_shadow` 的九列（只取该表有的字段）。"""
    pick = {
        'fill_price': row.get('fill_price'),
        'fill_delay_sec': row.get('fill_delay_sec'),
        'slippage': row.get('buy_slip_pct'),
        'sealed_close': (None if row.get('sealed_close') is None
                         else int(bool(row['sealed_close']))),
        'exit_reason': row.get('exit_reason'),
        'ret_gross': row.get('ret_gross'),
        'ret_net': row.get('ret_net'),
        'benchmark_ret': row.get('benchmark_ret'),
        'excess': row.get('excess'),
    }
    return {k: pick[k] for k in PENDING_COLUMNS}


def apply_backfill(conn, rows: Sequence[Dict], dry_run: bool = True) -> Dict:
    """把回测结果回填到 `dragon_shadow` 的九列（T5.3 才真跑）。

    匹配规则（按现场数据，不按设计猜测）：
    - `trigger_type` 用的是影子表自己的取值：有买法计划时是 `entry_arm`
      （`pullback` / `breakout`），没有计划时是 `structure` / `gain_top20`；
    - 时点取**触发时刻及之前最近的一条**快照行（影子每轮写一行，不能拿当日第一条
      冒充这笔触发的成交）；
    - 只改 `fill_price IS NULL` 的行，已填过的不覆盖；
    - `dry_run=True` 只统计将改行数，不提交；
    - 影子表还没建（T5 之前线上没有这张表）时返回 `table_missing=True`，不抛错。
    """
    stats = {'planned': 0, 'updated': 0, 'skipped_filled': 0, 'missing': 0,
             'dry_run': bool(dry_run), 'table_missing': False, 'rows': []}
    try:
        has = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dragon_shadow'"
        ).fetchone()
    except Exception:
        has = None
    if has is None:
        stats['table_missing'] = True
        stats['missing'] = len(rows)
        return stats
    for r in rows:
        vals = backfill_values(r)
        if vals.get('fill_price') is None:
            stats['missing'] += 1
            continue
        day, sym = r['day'], r['symbol']
        tt = r.get('trigger_type') or 'structure'
        trig_hm = str(r.get('trigger_time') or '').replace(':', '')[:4]
        if trig_hm:
            cur = conn.execute(
                "SELECT snapshot_time FROM dragon_shadow WHERE trade_date=? AND symbol=? "
                "AND trigger_type=? AND substr(replace(snapshot_time,':',''),1,4)<=? "
                'ORDER BY snapshot_time DESC LIMIT 1',
                (day, sym, tt, trig_hm)).fetchone()
        else:
            cur = conn.execute(
                'SELECT snapshot_time FROM dragon_shadow WHERE trade_date=? AND symbol=? '
                'AND trigger_type=? ORDER BY snapshot_time LIMIT 1',
                (day, sym, tt)).fetchone()
        if cur is None:
            stats['missing'] += 1
            continue
        snap_t = cur[0]
        filled = conn.execute(
            'SELECT fill_price FROM dragon_shadow WHERE trade_date=? AND symbol=? '
            'AND trigger_type=? AND snapshot_time=?', (day, sym, tt, snap_t)).fetchone()
        if filled is not None and filled[0] is not None:
            stats['skipped_filled'] += 1
            continue
        stats['planned'] += 1
        stats['rows'].append({'key': (day, sym, tt, snap_t), **vals})
        if not dry_run:
            cols = list(PENDING_COLUMNS)
            conn.execute(
                'UPDATE dragon_shadow SET ' + ', '.join(f'{c}=?' for c in cols) +
                ' WHERE trade_date=? AND symbol=? AND trigger_type=? AND snapshot_time=?',
                [vals[c] for c in cols] + [day, sym, tt, snap_t])
            stats['updated'] += 1
    if not dry_run:
        conn.commit()
    return stats


# ---------- 自检用：输入指纹 ----------

def input_fingerprint(days: Sequence[str], trigger_keys: Sequence[Tuple],
                      cfg: Optional[Dict] = None) -> str:
    blob = json.dumps({'days': list(days),
                       'triggers': sorted(list(t) for t in trigger_keys),
                       'param_version': param_version(cfg)},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode('utf-8')).hexdigest()[:12]
