"""dragon_risk.py - 龙头通道风控层（清单 T2.5，2026-11-01，影子）

职责
----
T2.3 决定"能不能进候选"，T2.4 决定"推什么、推给谁"，本层只做最后一道减法：

1. 追高闸：相对今开 > 8.5% 不推；
2. 高开闸：高开 > 7% 扣 15 分（扣分动作在 T2.3 的 score_v2 里，本层只留痕）；
3. 空间闸：room 不在 0.8%~2.0% 不推；
4. 流动性闸：5 分钟额 / 价差 / 卖买量比任一 fail 不推，unknown 只记录；
5. 仓位闸：单笔 ≤5%、通道 ≤20%（默认关，P3 才接组合层）；
6. 止损建模：−2%、分钟收盘触发、下一分钟成交、计跳空（只给 T3.1/T5 回放用）；
7. 频率状态机：20s 基准 → 失败率 >5% 退 60s → 连续 3 次失败熔断 5 分钟
   （T2.7 已接调度器，见 `due()`；开关 `RATE_GOVERNOR_ON` 默认关，等于改造前 15s）。

硬约束
------
- **每条规则一个独立开关**；关掉只退回 T2.3 的结构门（同一阈值在那里已经挡过一次），
  不假装开关能放松 T2.3。这一点写进文档，不做"关掉就等于放行追高票"的误读。
- **默认零影响**：仓位/频率开关默认关，四道闸默认开且阈值与 T2.3 一致；
  通道总开关 `INTRADAY_DRAGON_ENABLED=False` 时本模块连 import 都不发生。
- **不假设止损价一定成交**：分时只有分钟收盘价，触发用收盘价，成交取
  `min(止损价, 下一分钟收盘)`（更差的一边），没有下一分钟就记 `pending_fill`。
- 不改线上满仓等分规则（现行单笔最大 99.5%）；仓位只在影子里算。
"""
import threading
import time
from collections import deque
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from app.config import (
    INTRADAY_DRAGON_B_GAIN_HI, INTRADAY_DRAGON_GAP_MAX, INTRADAY_DRAGON_GAP_PENALTY,
    INTRADAY_DRAGON_MAX_ROOM_PCT, INTRADAY_DRAGON_MIN_ROOM_PCT,
    INTRADAY_DRAGON_RISK_CHASE_ON, INTRADAY_DRAGON_RISK_GAP_ON,
    INTRADAY_DRAGON_RISK_ROOM_ON, INTRADAY_DRAGON_RISK_LIQ_ON,
    INTRADAY_DRAGON_RISK_POSITION_ON, INTRADAY_DRAGON_RISK_STOP_ON,
    INTRADAY_DRAGON_POSITION_PCT, INTRADAY_DRAGON_CHANNEL_CAP_PCT,
    INTRADAY_DRAGON_STOP_PCT,
    INTRADAY_DRAGON_RATE_GOVERNOR_ON, INTRADAY_DRAGON_INTERVAL_SEC,
    INTRADAY_DRAGON_ENABLED, INTRADAY_DRAGON_START, INTRADAY_DRAGON_END,
    INTRADAY_DRAGON_RATE_BASE_SEC, INTRADAY_DRAGON_RATE_SLOW_SEC,
    INTRADAY_DRAGON_RATE_FAIL_PCT, INTRADAY_DRAGON_RATE_FAIL_STREAK,
    INTRADAY_DRAGON_RATE_BREAK_SEC, INTRADAY_DRAGON_RATE_WINDOW,
)

RISK_VERSION = 't2.5-2026-11-01'
SCHED_VERSION = 't2.7-2026-11-01'    # 调度接线（due/pace）：只改节奏，不改任何阈值

RULES = ('chase', 'gap', 'room', 'liquidity', 'position')

STOP_BASIS = 'minute_close'          # 腾讯分时只有收盘价，没有 high/low
FILL_BASIS = 'next_minute_close_worse_of_stop'


def _f(v) -> Optional[float]:
    try:
        return None if v is None or v == '' else float(v)
    except (TypeError, ValueError):
        return None


def rule_switches(cfg: Optional[Dict] = None) -> Dict:
    """五条规则各自的开关与阈值。返回值就是"这一轮到底开了什么"的现场。"""
    C = cfg or {}
    return {
        'chase': {
            'on': bool(C.get('chase_on', INTRADAY_DRAGON_RISK_CHASE_ON)),
            'max_gain': float(C.get('chase_max', INTRADAY_DRAGON_B_GAIN_HI)),
        },
        'gap': {
            'on': bool(C.get('gap_on', INTRADAY_DRAGON_RISK_GAP_ON)),
            'gap_max': float(C.get('gap_max', INTRADAY_DRAGON_GAP_MAX)),
            'penalty': float(C.get('gap_penalty', INTRADAY_DRAGON_GAP_PENALTY)),
        },
        'room': {
            'on': bool(C.get('room_on', INTRADAY_DRAGON_RISK_ROOM_ON)),
            'min': float(C.get('room_min', INTRADAY_DRAGON_MIN_ROOM_PCT)),
            'max': float(C.get('room_max', INTRADAY_DRAGON_MAX_ROOM_PCT)),
        },
        'liquidity': {
            'on': bool(C.get('liq_on', INTRADAY_DRAGON_RISK_LIQ_ON)),
        },
        'position': {
            'on': bool(C.get('position_on', INTRADAY_DRAGON_RISK_POSITION_ON)),
            'per_trade_pct': float(C.get('position_pct', INTRADAY_DRAGON_POSITION_PCT)),
            'channel_cap_pct': float(C.get('channel_cap_pct', INTRADAY_DRAGON_CHANNEL_CAP_PCT)),
        },
    }


# ---------- 单票风控闸 ----------

def chase_check(feat: Dict, sw: Dict) -> Dict:
    """追高：相对今开涨幅超过上限。缺失不挡（T2.3 已把缺失判 watch）。"""
    out = {'on': sw['on'], 'value': None, 'limit': sw['max_gain'],
           'hit': False, 'state': 'off' if not sw['on'] else 'pass'}
    if not sw['on']:
        return out
    v = _f((feat or {}).get('chg_from_open'))
    out['value'] = v
    if v is None:
        out['state'] = 'unknown'
        return out
    if v > sw['max_gain']:
        out.update({'hit': True, 'state': 'hit', 'reason': 'risk_chase'})
    return out


def gap_check(feat: Dict, ev: Dict, sw: Dict) -> Dict:
    """高开：只扣分不拦。扣分动作在 score_v2；这里把"是否触发、扣了多少"摊开。"""
    out = {'on': sw['on'], 'value': None, 'limit': sw['gap_max'],
           'hit': False, 'penalty': 0.0, 'state': 'off' if not sw['on'] else 'pass'}
    if not sw['on']:
        return out
    v = _f((feat or {}).get('gap'))
    out['value'] = v
    applied = ((ev or {}).get('score_detail') or {}).get('penalties') or {}
    if applied.get('gap_high'):
        out.update({'hit': True, 'penalty': float(sw['penalty']), 'state': 'hit',
                    'note': 'gap_penalty_applied_in_score'})
    elif v is not None and v > sw['gap_max']:
        # 分数没扣说明当前是 legacy / 分数没算；如实标"命中但未扣"
        out.update({'hit': True, 'penalty': 0.0, 'state': 'hit',
                    'note': 'gap_penalty_not_applied'})
    return out


def room_check(feat: Dict, sw: Dict) -> Dict:
    """空间：距涨停必须在区间内。缺失不挡（T2.3 已判 watch）。"""
    out = {'on': sw['on'], 'value': None, 'min': sw['min'], 'max': sw['max'],
           'hit': False, 'state': 'off' if not sw['on'] else 'pass'}
    if not sw['on']:
        return out
    v = _f((feat or {}).get('room'))
    out['value'] = v
    if v is None:
        out['state'] = 'unknown'
        return out
    if v < sw['min'] or v > sw['max']:
        out.update({'hit': True, 'state': 'hit', 'reason': 'risk_room'})
    return out


def liquidity_check(ev: Dict, sw: Dict) -> Dict:
    """流动性：fail 挡，unknown 只记录。判定本身复用 T2.3 的 liquidity_check。"""
    out = {'on': sw['on'], 'status': None, 'hit': False,
           'state': 'off' if not sw['on'] else 'pass'}
    if not sw['on']:
        return out
    liq = (ev or {}).get('liquidity') or {}
    status = liq.get('status')
    out['status'] = status
    if status == 'fail':
        out.update({'hit': True, 'state': 'hit', 'reason': 'risk_liquidity',
                    'detail': [v.get('reason') for v in (liq.get('checks') or {}).values()
                               if v.get('ok') is False]})
    elif status in (None, 'unknown'):
        out['state'] = 'unknown'
    return out


def position_plan(price: float, cfg: Optional[Dict] = None) -> Dict:
    """仓位建议：单笔 ≤5%、龙头通道 ≤20%；与主通道/存量持仓合并算总敞口（只报告不设限）。

    默认关。开关打开但缺 `capital` 时按"算不清就不推"处理，不拿默认资金瞎编。
    `shares` 按 A 股一手 100 股向下取整；价格无效时返回 0 股并标原因。
    """
    C = cfg or {}
    sw = rule_switches(C)['position']
    if not sw['on']:
        return {'on': False, 'ok': True, 'state': 'off', 'pct': None,
                'amount': None, 'shares': None, 'reason': 'position_off'}
    capital = _f(C.get('capital'))
    if not capital or capital <= 0:
        return {'on': True, 'ok': False, 'state': 'unknown', 'pct': None,
                'amount': None, 'shares': None, 'reason': 'capital_missing'}
    px = _f(price)
    if not px or px <= 0:
        return {'on': True, 'ok': False, 'state': 'unknown', 'pct': None,
                'amount': None, 'shares': None, 'reason': 'price_missing'}

    used_channel = _f(C.get('channel_used') or 0.0) or 0.0     # 本通道已用金额
    other = _f(C.get('other_exposure') or 0.0) or 0.0          # 主通道 + 存量持仓金额
    channel_left_pct = sw['channel_cap_pct'] - used_channel / capital * 100.0
    pct = min(sw['per_trade_pct'], max(0.0, channel_left_pct))
    capped_by = 'per_trade' if sw['per_trade_pct'] <= channel_left_pct else 'channel'
    amount = round(capital * pct / 100.0, 2)
    shares = int(amount / px // 100 * 100)
    out = {
        'on': True, 'ok': shares > 0, 'state': 'pass' if shares > 0 else 'full',
        'pct': round(pct, 4), 'amount': amount, 'shares': shares,
        'capped_by': capped_by,
        'channel_used_pct': round(used_channel / capital * 100.0, 3),
        'channel_cap_pct': sw['channel_cap_pct'],
        'merged_exposure_pct': round((used_channel + other) / capital * 100.0, 3),
        'reason': '' if shares > 0 else 'channel_full',
    }
    return out


def gate(row: Dict, cfg: Optional[Dict] = None) -> Dict:
    """推送前最后一道减法。返回 `ok` + 命中的原因 + 每条规则的现场。

    `row` 是 `scan_once()` 的一行（含 `feat` / `ev`）。只收紧、不放松：
    关掉的规则记 `off`，其余仍由 T2.3 的结构门兜着。
    """
    C = cfg or {}
    sw = rule_switches(C)
    feat = ((row or {}).get('feat') or {}).get('features') or {}
    ev = (row or {}).get('ev') or {}
    checks = {
        'chase': chase_check(feat, sw['chase']),
        'gap': gap_check(feat, ev, sw['gap']),
        'room': room_check(feat, sw['room']),
        'liquidity': liquidity_check(ev, sw['liquidity']),
    }
    price = _f((row or {}).get('point', {}).get('price')
               if isinstance((row or {}).get('point'), dict) else None)
    if price is None:
        price = _f((row or {}).get('ev', {}).get('price'))
    checks['position'] = position_plan(price or 0.0, C)

    reasons: List[str] = []
    for k in ('chase', 'room', 'liquidity'):
        if checks[k].get('hit'):
            reasons.append(checks[k]['reason'])
    if checks['position'].get('on') and not checks['position'].get('ok'):
        reasons.append(checks['position'].get('reason') or 'position_blocked')

    notes: List[str] = []
    if checks['gap'].get('hit'):
        notes.append(f"gap_penalty:{checks['gap'].get('penalty', 0):g}")
    if checks['liquidity'].get('state') == 'unknown':
        notes.append('liquidity_unknown')
    if checks['position'].get('on'):
        notes.append(f"position:{checks['position'].get('pct')}%")

    return {
        'version': RISK_VERSION,
        'ok': not reasons,
        'reasons': list(dict.fromkeys(reasons)),
        'notes': notes,
        'checks': checks,
        'switches': {k: v['on'] for k, v in sw.items()},
    }


# ---------- 止损建模（只给回放/跟踪用） ----------

def stop_sim(fill_price: float, stop_price: float, bars: Sequence[Dict],
             from_index: int = 0, cfg: Optional[Dict] = None) -> Dict:
    """−2% 止损建模：分钟收盘触发、下一分钟成交、计跳空、不假设按止损价成交。

    - 触发：`bars[j]['price'] <= stop_price`（收盘价即触发价，分时无 high/low）；
    - 成交：`min(stop_price, bars[j+1]['price'])` —— 取更差的一边；
    - 没有下一分钟：`pending_fill=True`，不产生成交价，也不假装按止损价成交；
    - `gap_slip` = 止损价 − 成交价（正数 = 滑点，跳空低开时更大）。
    """
    C = cfg or {}
    on = bool(C.get('stop_on', INTRADAY_DRAGON_RISK_STOP_ON))
    out = {
        'enabled': on, 'stop_hit': None, 'state': 'off' if not on else 'not_hit',
        'stop_price': round(float(stop_price), 3) if stop_price else None,
        'fill_price': None, 'fill_basis': FILL_BASIS, 'basis': STOP_BASIS,
        'trigger_ts': None, 'fill_ts': None, 'gap_slip': None, 'gap_slip_pct': None,
        'loss_pct': None, 'bars_held': None, 'pending_fill': False,
        'reason': 'stop_2pct', 'stop_pct': float(C.get('stop_pct', INTRADAY_DRAGON_STOP_PCT)),
    }
    if not on:
        return out
    sp = _f(stop_price)
    fp = _f(fill_price)
    if not sp or not fp or sp <= 0 or fp <= 0:
        out.update({'state': 'unknown', 'reason': 'invalid_input'})
        return out
    out['stop_hit'] = False
    seq = list(bars or [])
    start = max(0, int(from_index or 0))
    hit = None
    for j in range(start, len(seq)):
        px = _f((seq[j] or {}).get('price'))
        if px is not None and px <= sp:
            hit = j
            break
    if hit is None:
        return out
    out.update({'stop_hit': True, 'state': 'hit', 'trigger_ts': (seq[hit] or {}).get('t'),
                'bars_held': hit - start})
    if hit + 1 >= len(seq):
        out.update({'pending_fill': True, 'state': 'pending_fill'})
        return out
    nxt = _f((seq[hit + 1] or {}).get('price'))
    if nxt is None:
        out.update({'pending_fill': True, 'state': 'pending_fill'})
        return out
    fill = min(sp, nxt)
    out.update({
        'fill_price': round(fill, 3),
        'fill_ts': (seq[hit + 1] or {}).get('t'),
        'gap_slip': round(sp - fill, 3),
        'gap_slip_pct': round((sp - fill) / sp * 100.0, 3),
        'loss_pct': round((fill / fp - 1) * 100.0, 3),
    })
    return out


# ---------- 频率状态机 ----------

class RateGovernor:
    """20s 基准 / 失败率 >5% → 60s / 连续 3 次失败 → 熔断 5 分钟 / 恢复回 20s。

    只维护状态与"下一轮该等多久"；调度器经 `due()` / `current_interval_sec()` 读取（T2.7）。
    """

    def __init__(self, cfg: Optional[Dict] = None):
        C = cfg or {}
        self.base = int(C.get('rate_base_sec', INTRADAY_DRAGON_RATE_BASE_SEC))
        self.slow = int(C.get('rate_slow_sec', INTRADAY_DRAGON_RATE_SLOW_SEC))
        self.fail_pct = float(C.get('rate_fail_pct', INTRADAY_DRAGON_RATE_FAIL_PCT))
        self.streak_max = int(C.get('rate_fail_streak', INTRADAY_DRAGON_RATE_FAIL_STREAK))
        self.break_sec = int(C.get('rate_break_sec', INTRADAY_DRAGON_RATE_BREAK_SEC))
        self.window = max(1, int(C.get('rate_window', INTRADAY_DRAGON_RATE_WINDOW)))
        self._win = deque(maxlen=self.window)
        self._streak = 0
        self._break_until = 0.0
        self._last_mode = 'base'
        self._lock = threading.Lock()

    def reset(self):
        with self._lock:
            self._win.clear()
            self._streak = 0
            self._break_until = 0.0
            self._last_mode = 'base'

    def record(self, ok: bool, now: Optional[float] = None) -> Dict:
        ts = time.time() if now is None else float(now)
        with self._lock:
            self._win.append(bool(ok))
            self._streak = 0 if ok else self._streak + 1
            action = 'base'
            if (not ok and self._streak >= self.streak_max
                    and ts >= self._break_until):
                # 已在熔断窗口内不重复续期；窗口过了还失败才再熔 5 分钟
                self._break_until = ts + self.break_sec
                action = 'break'
            elif ts < self._break_until:
                action = 'break'
            elif self._fail_rate() * 100.0 > self.fail_pct:
                action = 'slow'
            st = self._state(ts, action)
            st['changed'] = st['mode'] != self._last_mode
            st['prev_mode'] = self._last_mode
            self._last_mode = st['mode']
            return st

    def _fail_rate(self) -> float:
        if not self._win:
            return 0.0
        return sum(1 for x in self._win if not x) / len(self._win)

    def _state(self, ts: Optional[float] = None, action: Optional[str] = None) -> Dict:
        now = time.time() if ts is None else float(ts)
        in_break = now < self._break_until
        rate = self._fail_rate()
        if in_break:
            mode, interval, skip = 'break', self.break_sec, True
        elif rate * 100.0 > self.fail_pct:
            mode, interval, skip = 'slow', self.slow, False
        else:
            mode, interval, skip = 'base', self.base, False
        return {
            'mode': mode,
            'action': action or mode,
            'interval_sec': interval,
            'skip': skip,
            'fail_rate': round(rate * 100.0, 2),
            'streak': self._streak,
            'break_left_sec': max(0, int(self._break_until - now)) if in_break else 0,
            'samples': len(self._win),
        }

    def interval(self, now: Optional[float] = None) -> Dict:
        with self._lock:
            return self._state(now)


_GOV = RateGovernor()


def governor_state(now: Optional[float] = None) -> Dict:
    return _GOV.interval(now)


def note_scan(ok: bool, now: Optional[float] = None,
              cfg: Optional[Dict] = None) -> Optional[Dict]:
    """每一轮扫完记一次成败。开关关时直接返回 None，不留任何副作用。"""
    C = cfg or {}
    if not bool(C.get('rate_governor_on', INTRADAY_DRAGON_RATE_GOVERNOR_ON)):
        return None
    st = _GOV.record(ok, now)
    if st.get('changed') and st.get('mode') in ('slow', 'break'):
        print(f"[龙头风控] 频率{'熔断' if st['mode'] == 'break' else '降速'} "
              f"失败率 {st['fail_rate']}% 连续失败 {st['streak']} 次 "
              f"间隔 {st['interval_sec']}s")
    elif st.get('changed') and st.get('prev_mode') in ('slow', 'break'):
        print(f"[龙头风控] 频率恢复：回到 {st['mode']} {st['interval_sec']}s")
    return st


def current_interval_sec(now: Optional[float] = None,
                         cfg: Optional[Dict] = None) -> int:
    """调度器该等多久。开关关时原样返回现行 15s，不做任何改变。"""
    return int(pace(now, cfg).get('interval_sec') or INTRADAY_DRAGON_INTERVAL_SEC)


def pace(now: Optional[float] = None, cfg: Optional[Dict] = None) -> Dict:
    """调度器一拍的节奏（T2.7）。开关关时与改造前逐字节等价：15s、不跳轮。

    开关开时返回状态机：base 20s / slow 60s / break 300s（skip=True）。
    """
    C = cfg or {}
    if not bool(C.get('rate_governor_on', INTRADAY_DRAGON_RATE_GOVERNOR_ON)):
        return {'mode': 'off', 'interval_sec': int(INTRADAY_DRAGON_INTERVAL_SEC),
                'skip': False, 'fail_rate': 0.0, 'streak': 0,
                'break_left_sec': 0, 'samples': 0}
    return dict(_GOV.interval(now))


def due(window_hm: str, trading: bool, last_ts: float, now_ts: float,
        enabled: Optional[bool] = None, start: Optional[str] = None,
        end: Optional[str] = None, cfg: Optional[Dict] = None) -> Dict:
    """主循环每一拍问一次：这一轮跑不跑。

    返回 {'due', 'interval_sec', 'skip', 'mode', 'reason'}。四条不进：
    总开关关 / 不在 09:31~10:15 / 非交易日 / 熔断中（skip=True）。
    不进的时候调用方**不要**刷新 last_ts，熔断结束才能立刻恢复。
    """
    on = INTRADAY_DRAGON_ENABLED if enabled is None else bool(enabled)
    s = INTRADAY_DRAGON_START if start is None else start
    e = INTRADAY_DRAGON_END if end is None else end
    base = {'due': False, 'interval_sec': int(INTRADAY_DRAGON_INTERVAL_SEC),
            'skip': False, 'mode': 'off'}
    if not on:
        return dict(base, reason='总开关关')
    if not (s <= window_hm <= e):
        return dict(base, reason='不在窗口')
    if not trading:
        return dict(base, reason='非交易日')
    st = pace(now_ts, cfg)
    iv = int(st.get('interval_sec') or INTRADAY_DRAGON_INTERVAL_SEC)
    if st.get('skip'):
        return {'due': False, 'interval_sec': iv, 'skip': True,
                'mode': st.get('mode', 'break'), 'reason': '熔断中'}
    return {'due': (now_ts - last_ts) >= iv, 'interval_sec': iv, 'skip': False,
            'mode': st.get('mode', 'base'), 'reason': ''}


def reset_governor():
    """自测用：把状态清干净。"""
    _GOV.reset()


def describe(now: Optional[float] = None) -> Dict:
    """一眼看全：版本、开关、频率状态。给自测与日志用。"""
    st = _GOV.interval(now)
    return {'version': RISK_VERSION, 'sched_version': SCHED_VERSION,
            'switches': {k: v['on'] for k, v in rule_switches().items()},
            'rate': st, 'pace': pace(now),
            'ts': datetime.now().strftime('%H:%M:%S')}
