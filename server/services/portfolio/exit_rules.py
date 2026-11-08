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
              today: Optional[date] = None,
              yest_limit_up: bool = False, vol_today: Optional[float] = None,
              vol_yest: Optional[float] = None, day_gain_pct: Optional[float] = None,
              now_hm: Optional[str] = None, late_spike: bool = False,
              limit_down: Optional[bool] = None, market_down: bool = False,
              sector_down: bool = False) -> Dict:
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

    # ---------- 用户 4 条卖点（2026-11-08，影子先行）----------
    # 每条独立、缺数据则 available=False（绝不误触发），与既有三机制并列；
    # 只算计划不动作，仅 exit_rules_live_enabled=True 才接管（见 position.py 映射）。
    user4_on = bool(_set('exit_rules_user4_enabled', 'EXIT_RULES_USER4_ENABLED', True))
    _now = (now_hm or datetime.now().strftime('%H:%M'))
    _market_down = bool(market_down)
    _day_gain = day_gain_pct  # 今日涨跌幅%；缺失则相关规则不可用，严禁回落到持仓盈亏

    # 规则1：昨涨停 + 今量≥2倍昨 + 涨幅<5% → 放量滞涨/出货
    u1_avail, u1_hit, u1_detail = False, False, '数据缺失（需昨涨停标记+今/昨量能）'
    if user4_on and yest_limit_up and vol_today and vol_yest and vol_yest > 0 and _day_gain is not None:
        u1_avail = True
        u1_ratio = vol_today / vol_yest
        u1_hit = (u1_ratio >= 2.0) and (_day_gain < 5.0)
        u1_detail = (f'昨涨停+今量{u1_ratio:.1f}×昨(≥2)但涨幅{_day_gain:+.1f}%<5%，放量滞涨疑似出货'
                     if u1_hit else
                     f'昨涨停+今量{u1_ratio:.1f}×昨、涨幅{_day_gain:+.1f}%，未触发滞涨')

    # 规则2：14:00 后突然拉升 → 尾盘偷袭（需调用方给出 late_spike 检测）
    u2_avail, u2_hit, u2_detail = False, False, '数据缺失（需尾盘拉升检测 late_spike）'
    if user4_on and late_spike:
        u2_avail = True
        u2_hit = _now >= '14:00'
        u2_detail = (f'{_now} 尾盘拉升→减仓信号' if u2_hit else
                     f'{_now} 早于14:00，尾盘偷袭不触发')

    # 规则3：持仓（非当日买入）跌停 → 能走一定要走（T+1 纪律）
    u3_avail, u3_hit, u3_detail = False, False, '数据缺失（需跌停标记）'
    if user4_on and limit_down is not None:
        u3_avail = True
        if limit_down:
            if days_held is not None and days_held >= 1:
                u3_hit = True
                u3_detail = '持仓跌停且为前日买入，T+1 可卖，立即离场'
            else:
                u3_detail = '当日买入跌停，T+1 无法卖出，次日再处理'
        else:
            u3_detail = (f'现价未跌停（{_day_gain:+.1f}%），不触发'
                         if _day_gain is not None else '现价未跌停，不触发')

    # 规则4：大盘下跌 + 个股红盘但涨幅<5% → 逆势抗跌，防次日补跌
    u4_avail, u4_hit, u4_detail = False, False, '大盘方向缺失'
    if user4_on and _market_down and _day_gain is not None:
        u4_avail = True
        # 用户规则4 = 大盘+板块“都下跌”才走。板块方向已知时严格按双跌判定；
        # 未知时按大盘弱近似（保守不误杀：只有板块明确未跌才放行为不触发）。
        if sector_down is True:
            _sector_note = '板块同步下跌，'
            u4_hit = (0.0 < _day_gain < 5.0)
        elif sector_down is False:
            _sector_note = '但板块未下跌（与规则不符），'
            u4_hit = False
        else:
            _sector_note = '板块方向未接入（暂按大盘弱近似），'
            u4_hit = (0.0 < _day_gain < 5.0)
        u4_detail = (f'大盘下跌+个股红盘{_day_gain:+.1f}%<5%：{_sector_note}次日补跌概率大'
                     if u4_hit else
                     f'大盘下跌但个股{_day_gain:+.1f}%（非“红盘小涨”区间，或板块未同跌），不触发')

    mechs = [
        {'id': 'fixed', 'label': '固定止损', 'hit': fixed_hit,
         'line': _round(effective_stop), 'detail': fixed_detail, 'available': bool(effective_stop)},
        {'id': 'logic', 'label': f'逻辑止损(MA{ma_days})', 'hit': logic_hit,
         'line': _round(ma20), 'detail': logic_detail, 'available': bool(ma20)},
        {'id': 'time', 'label': '时间止损', 'hit': time_hit,
         'line': None, 'detail': time_detail, 'available': days_held is not None},
        {'id': 'u1', 'label': '放量滞涨', 'hit': u1_hit,
         'line': None, 'detail': u1_detail, 'available': u1_avail},
        {'id': 'u2', 'label': '尾盘偷袭', 'hit': u2_hit,
         'line': None, 'detail': u2_detail, 'available': u2_avail},
        {'id': 'u3', 'label': '持仓跌停', 'hit': u3_hit,
         'line': None, 'detail': u3_detail, 'available': u3_avail},
        {'id': 'u4', 'label': '逆势抗跌', 'hit': u4_hit,
         'line': None, 'detail': u4_detail, 'available': u4_avail},
    ]
    hit = next((m['id'] for m in mechs if m['hit']), '')
    labels = {'fixed': '止损离场', 'logic': f'逻辑离场（跌破MA{ma_days}）',
              'time': '时间止损（持有到期未达标）',
              'u1': '出货离场（放量滞涨）', 'u2': '尾盘减仓（偷袭）',
              'u3': '跌停离场', 'u4': '逆势减仓（防补跌）'}
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


def gather_sell_context(symbol: str, quote: Optional[dict] = None) -> Dict:
    """为 u1/u4 准备真实数据（best-effort，缺数据即返回空，绝不误触发）。

    返回 {yest_limit_up, vol_today, vol_yest, sector_down}：
      · yest_limit_up：昨日收盘≈涨停价（主板 10% 近似；ST/科创/创业板的 5%/20% 暂未细分）；
      · vol_today/vol_yest：今量/昨量（手），u1 量比用；
      · sector_down：板块当日方向（best-effort；本地暂缺实时板块涨跌缓存 → 恒为 None，
        由 u4 逻辑按“未知则按大盘弱近似”处理，不会因未知而误触发）。
    仅在持仓诊断（position.py）调用，单票少、本地只读 kline_cache，开销可忽略。
    """
    out = {'yest_limit_up': False, 'vol_today': None, 'vol_yest': None, 'sector_down': None}
    if not symbol:
        return out
    # 今日成交量（实时 quote）
    if isinstance(quote, dict):
        for k in ('volume', 'vol', 'volume_hand'):
            v = quote.get(k)
            if v is not None:
                try:
                    out['vol_today'] = float(v)
                except (TypeError, ValueError):
                    out['vol_today'] = None
                break
    # 昨日 K 线（本地 kline_cache，日级，盘后更新）
    try:
        import json as _json
        from datetime import date as _date
        from db.dal_state import get_db_conn
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT kline_json FROM kline_cache WHERE symbol=?", (symbol,))
        row = cur.fetchone()
        if row and row[0]:
            arr = _json.loads(row[0])
            if len(arr) >= 1:
                today = _date.today().strftime('%Y-%m-%d')
                # 取严格早于今日的最近一根 = 昨日（若缓存已含今日则用前一根，避免用错日）
                yest = None
                for r in reversed(arr):
                    if r[0] < today:
                        yest = r
                        break
                if yest is None:
                    yest = arr[-1]
                prec = float(yest[2])            # 昨收
                vol_yest = float(yest[5])        # 昨量(手)
                lim = round(prec * 1.1 + 1e-9, 2)
                out['yest_limit_up'] = abs(prec - lim) < 0.011
                out['vol_yest'] = vol_yest
    except Exception:
        # 任何取数异常都不应让卖出判定误触发：保持默认空值
        pass
    return out


def _round(v):
    return round(float(v), 2) if v is not None else None


def _fmt(v) -> str:
    return f'{v:.2f}' if v else '—'
