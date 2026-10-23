"""exit_rules.py - 统一卖出规则（2026-10-22，P1）

为什么需要它
------------
系统里同时存在三套止损口径，互相对不上：
  1. `risk_control.calc_risk_control` 按评分/波动给的止损价；
  2. `position.py` 按当日振幅再兜一层，并叠加浮盈后的移动止盈线；
  3. `review` 的"明日计划"又按自己的规则写一遍。
结果是同一只票在三个页面能看到三个止损价，用户不知道该信哪个。

本模块只做一件事：把三条**互相独立**的卖出机制放在同一张计划里，逐条给出是否触发：
  · 固定止损：现价跌破"有效止损线"（风控止损与移动止盈线取高者）；
  · 逻辑止损：现价跌破 MA20（趋势逻辑坏了，不再等固定百分比）；
  · 时间止损：持有满 N 个自然日，浮盈仍不到 X% —— 资金占用也是成本，该换股。

三种模式（默认最保守）
----------------------
  · `exit_rules_shadow_enabled`：只把计划挂到 `exit_plan` 字段给页面看，不改任何动作；
  · `exit_rules_live_enabled`：计划触发时覆盖 `diagnose_position` 的动作与推送；
  · 两个都关 = 本模块完全不参与，持仓页与改造前逐字节一致。

注意：这是**卖出侧**规则，不修改选股/推荐；历史推荐不回溯补算，避免用未来数据。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Dict, Optional


def _set(key: str, cfg_name: str, default):
    try:
        from services.support import app_settings
        v = app_settings.value(key)
        return v if v is not None else default
    except Exception:
        import app.config as _cfg
        return getattr(_cfg, cfg_name, default)


def _enabled() -> bool:
    return bool(_set('exit_rules_shadow_enabled', 'EXIT_RULES_SHADOW_ENABLED', True))


def _live() -> bool:
    return bool(_set('exit_rules_live_enabled', 'EXIT_RULES_LIVE_ENABLED', False))


def _global_on() -> bool:
    """总开关：config 的 EXIT_RULES_ENABLED 且（影子 或 实盘）至少开一个。"""
    try:
        import app.config as _cfg
        master = bool(getattr(_cfg, 'EXIT_RULES_ENABLED', True))
    except Exception:
        master = True
    return master and (_enabled() or _live())


def plan_exit(*, symbol: str, cost: float, price: float, buy_date: str = '',
              stop_price: Optional[float] = None, target_price: Optional[float] = None,
              trailing: Optional[Dict] = None, trend_result: Optional[Dict] = None,
              today: Optional[date] = None) -> Dict:
    """生成统一卖出计划。所有入参都由 `position.py` 已经算好的值传入，本函数不再取数。

    返回：
      {enabled, live, action: 'exit'|'hold', action_label, reason, hit,
       mechanisms: [{id,label,hit,line,detail,available}], stop, ma20, daysHeld}
    调用方只在 `action == 'exit'` 且 `live == True` 时覆盖动作；否则只展示。
    """
    if not _global_on():
        return {'enabled': False}
    if not cost or not price or cost <= 0 or price <= 0:
        return {'enabled': False, 'why': '成本价/现价缺失'}

    days_cfg = int(_set('exit_rules_time_stop_days', 'EXIT_RULES_TIME_STOP_DAYS', 5))
    min_pnl = float(_set('exit_rules_time_stop_min_pnl', 'EXIT_RULES_TIME_STOP_MIN_PNL', 3.0))
    ma_days = int(_set('exit_rules_logic_ma_days', 'EXIT_RULES_LOGIC_MA_DAYS', 20))
    live = _live()
    pnl = (price - cost) / cost * 100.0

    # 固定止损：风控线 vs 移动止盈线，取更高的一条（让盈利单的线只上不下）
    fixed_line = float(stop_price or 0) or None
    trail_line = None
    if isinstance(trailing, dict) and trailing.get('enabled') and trailing.get('line'):
        trail_line = float(trailing['line'])
    effective_stop = max([x for x in (fixed_line, trail_line) if x], default=None)
    fixed_hit = bool(effective_stop and price <= effective_stop)
    fixed_detail = (f'现价{price:.2f} ≤ 有效止损{effective_stop:.2f}'
                    f'（风控{_fmt(fixed_line)} / 移动止盈{_fmt(trail_line)}）'
                    if fixed_hit else
                    f'现价{price:.2f} > 有效止损{_fmt(effective_stop)}，未触发')

    # 逻辑止损：跌破 MA20。trend_result['ma20'] 是最近 10 根 MA20，最后一根是当前值。
    ma20 = None
    ma_series = (trend_result or {}).get('ma20') or []
    if ma_series:
        try:
            ma20 = float(ma_series[-1]) if ma_series[-1] is not None else None
        except (TypeError, ValueError):
            ma20 = None
    logic_hit = bool(ma20 and price < ma20)
    logic_detail = (f'现价{price:.2f} < MA{ma_days} {ma20:.2f}，趋势逻辑已破'
                    if logic_hit else
                    (f'现价{price:.2f} ≥ MA{ma_days} {ma20:.2f}，趋势逻辑未破' if ma20 else
                     f'MA{ma_days} 不可用（本地日K不足），本条不计'))

    # 时间止损：持有满 N 个自然日且浮盈未达及格线
    days_held = None
    if buy_date:
        try:
            bd = datetime.strptime(str(buy_date)[:10], '%Y-%m-%d').date()
            days_held = ((today or date.today()) - bd).days
        except ValueError:
            days_held = None
    time_hit = bool(days_held is not None and days_held >= days_cfg and pnl < min_pnl)
    time_detail = (f'持有{days_held}天（≥{days_cfg}）收益{pnl:+.1f}% < {min_pnl:.0f}%，'
                   f'资金效率不合格' if time_hit else
                   (f'持有{days_held}天，未到时间止损条件' if days_held is not None else
                    '买入日期缺失，时间止损不计'))

    mechs = [
        {'id': 'fixed', 'label': '固定止损', 'hit': fixed_hit,
         'line': _round(effective_stop), 'detail': fixed_detail, 'available': bool(effective_stop)},
        {'id': 'logic', 'label': f'逻辑止损(MA{ma_days})', 'hit': logic_hit,
         'line': _round(ma20), 'detail': logic_detail, 'available': bool(ma20)},
        {'id': 'time', 'label': '时间止损', 'hit': time_hit,
         'line': None, 'detail': time_detail, 'available': days_held is not None},
    ]
    hit = next((m['id'] for m in mechs if m['hit']), '')
    labels = {'fixed': '止损离场', 'logic': f'逻辑离场（跌破MA{ma_days}）',
              'time': '时间止损（持有到期未达标）'}
    out = {
        'enabled': True,
        'live': live,
        'action': 'exit' if hit else 'hold',
        'action_label': labels.get(hit, '继续持有'),
        'hit': hit,
        'reason': next((m['detail'] for m in mechs if m['hit']), '三条机制均未触发'),
        'mechanisms': mechs,
        'stop': _round(effective_stop),
        'ma20': _round(ma20),
        'daysHeld': days_held,
        'pnlPct': round(pnl, 2),
        'asOf': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    if target_price:
        out['target'] = _round(float(target_price))
    return out


def _round(v):
    return round(float(v), 2) if v is not None else None


def _fmt(v) -> str:
    return f'{v:.2f}' if v else '—'
