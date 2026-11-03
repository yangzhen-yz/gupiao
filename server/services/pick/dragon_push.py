"""dragon_push.py - 龙头通道推送（清单 T2.4，2026-11-01，默认 legacy）

职责边界
--------
T2.3 只回答"这只票现在能不能看"（`verdict='candidate'`）。本模块回答三件事：
  1. 以什么价买：回踩臂 `VWAP ± 0.3%`、突破臂"触发分钟价 ~ +0.2%"，两臂影子对照、不混算；
  2. 错了在哪走：默认成交价 ×0.98，同时记 `stop_mode`（fixed_2pct / vwap / day_low）；
  3. 对了在哪卖：封板→次日开盘；未封→10:30；−2% 止损先触发先走；三个出口都记 `exit_reason`。

推送前三道闸：
  - 重新取现价：报价超 60s 或偏离触发价超 1% → 改"观察"不推；
  - 时间窗：09:31~10:15 之外不推（09:30~09:31 也不推）；
  - 额度：当日 ≤4 只、同票 ≤2 次、冷却 600s，三条各自独立开关。

硬约束
------
- `INTRADAY_DRAGON_PUSH_MODE='legacy'`：不导入、不落库、不发消息，逐票 diff = 0；
- `v2_dry`：只落 `dragon_push_log`（`push_mode='v2_dry'`），零消息；
- 额度按 `push_mode` 分账：dry 的记录不占 live 的额度；
- 任何写库异常只记日志，不让推送主链路失败；
- 缺失字段写 None / "—"，不填中档、不编造：`sector_rank` 恒 NULL 时模板写"未接入"；
- 金额单位沿用 T2.2：`amount_5m` 是万元。
"""
import json
import urllib.request
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from app.config import (INTRADAY_DRAGON_BREAKOUT_ADD_PCT,
                        INTRADAY_DRAGON_CAP_COOLDOWN_ON,
                        INTRADAY_DRAGON_CAP_DAILY_ON,
                        INTRADAY_DRAGON_CAP_PER_STOCK_ON,
                        INTRADAY_DRAGON_COOLDOWN_SEC,
                        INTRADAY_DRAGON_DAILY_LIMIT,
                        INTRADAY_DRAGON_ENTRY_ARMS,
                        INTRADAY_DRAGON_ENTRY_BAND_PCT,
                        INTRADAY_DRAGON_ENTRY_LIVE_ARM,
                        INTRADAY_DRAGON_MAX_PER_STOCK,
                        INTRADAY_DRAGON_POSITION_PCT, INTRADAY_DRAGON_PUSH_DEVIATION_MAX,
                        INTRADAY_DRAGON_PUSH_DRY_SEND, INTRADAY_DRAGON_PUSH_ENABLED,
                        INTRADAY_DRAGON_PUSH_MODE, INTRADAY_DRAGON_PUSH_QUOTE_MAX_AGE,
                        INTRADAY_DRAGON_PUSH_WINDOW_END,
                        INTRADAY_DRAGON_PUSH_WINDOW_START,
                        INTRADAY_DRAGON_SELL_TIME, INTRADAY_DRAGON_STOP_DAY_LOW_BUF,
                        INTRADAY_DRAGON_STOP_MODE, INTRADAY_DRAGON_STOP_PCT)
from db.database import get_db_conn

PUSH_VERSION = 't2.4-2026-11-01'

MODES = ('legacy', 'v2_dry', 'v2_live')
ARMS = ('pullback', 'breakout')
STOP_MODES = ('fixed_2pct', 'vwap', 'day_low')

EXIT_STOP = 'stop_2pct'
EXIT_SEALED = 'sealed_next_open'
EXIT_TIME = 'time_1030'          # 实际文案里的时点由 SELL_TIME 生成，key 保持稳定


# ---------- 小工具 ----------

def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _px(v) -> str:
    x = _f(v)
    return f'{x:.2f}' if x is not None else '—'


def _hm(t: str) -> str:
    s = str(t or '').strip()
    if len(s) == 4 and s.isdigit():
        return f'{s[:2]}:{s[2:]}'
    return s


def _amount(v) -> str:
    x = _f(v)
    if x is None:
        return '—'
    return f'{x / 1e4:.2f}亿' if x >= 1e4 else f'{x:.0f}万'


def ensure_mode(mode: str) -> str:
    m = str(mode or '').strip()
    if m not in MODES:
        raise ValueError(f'推送模式只能是 {MODES}，收到 {mode!r}；'
                         'v2_live 要等 T2.4 影子对照与用户拍板后再开')
    return m


def in_window(hm: str, cfg: Optional[Dict] = None) -> bool:
    """09:31~10:15（含两端）。09:30~09:31 与 10:15 之后都不推。"""
    C = cfg or {}
    lo = C.get('window_start', INTRADAY_DRAGON_PUSH_WINDOW_START)
    hi = C.get('window_end', INTRADAY_DRAGON_PUSH_WINDOW_END)
    t = _hm(hm)
    return bool(t) and lo <= t <= hi


# ---------- 买入区间 / 止损 / 卖出 ----------

def entry_range(arm: str, vwap: Optional[float], trigger_price: Optional[float],
                cfg: Optional[Dict] = None) -> Optional[Dict]:
    """两臂买入区间。缺参照价返回 None（不猜一个数）。

    - 回踩臂：VWAP ± 0.3%
    - 突破臂：触发分钟价 ~ +0.2%（分钟线只有收盘价，这里是收盘价近似；
      真正的"分钟高点"要等 1 分钟 high 字段，记为数据债）
    """
    C = cfg or {}
    band = float(C.get('entry_band_pct', INTRADAY_DRAGON_ENTRY_BAND_PCT))
    add = float(C.get('breakout_add_pct', INTRADAY_DRAGON_BREAKOUT_ADD_PCT))
    v = _f(vwap)
    t = _f(trigger_price)
    if arm == 'pullback':
        if not v or v <= 0:
            return None
        return {'arm': 'pullback', 'low': round(v * (1 - band / 100.0), 2),
                'high': round(v * (1 + band / 100.0), 2), 'basis': 'vwap_band',
                'ref': round(v, 3)}
    if arm == 'breakout':
        if not t or t <= 0:
            return None
        return {'arm': 'breakout', 'low': round(t, 2),
                'high': round(t * (1 + add / 100.0), 2), 'basis': 'trigger_minute_close',
                'ref': round(t, 3)}
    raise ValueError(f'未知买入臂: {arm!r}，只能是 {ARMS}')


def _stop_by_mode(mode: str, entry_low: Optional[float], trigger: Optional[float],
                  vwap: Optional[float], bars: Optional[List[Dict]],
                  cfg: Optional[Dict]) -> Tuple[Optional[float], str]:
    C = cfg or {}
    pct = float(C.get('stop_pct', INTRADAY_DRAGON_STOP_PCT))
    if mode == 'vwap':
        v = _f(vwap)
        return (round(v * (1 - pct / 100.0), 2) if v else None), 'vwap'
    if mode == 'day_low':
        lows = [float(b['price']) for b in (bars or []) if _f(b.get('price'))]
        if not lows:
            return None, 'minute_close_min'
        buf = float(C.get('stop_day_low_buf', INTRADAY_DRAGON_STOP_DAY_LOW_BUF))
        return round(min(lows) * (1 - buf / 100.0), 2), 'minute_close_min'
    # fixed_2pct：有区间下限就用下限（最不利成交），否则用触发价
    ref = _f(entry_low) or _f(trigger)
    if not ref:
        return None, 'missing'
    basis = 'entry_low_est' if _f(entry_low) else 'trigger_est'
    return round(ref * (1 - pct / 100.0), 2), basis


def stop_plan(entry_low: Optional[float], trigger: Optional[float],
              vwap: Optional[float], bars: Optional[List[Dict]],
              cfg: Optional[Dict] = None) -> Dict:
    """选定止损 + 另两种口径的对照价。止损价不低于区间下限时给警告。"""
    C = cfg or {}
    mode = str(C.get('stop_mode', INTRADAY_DRAGON_STOP_MODE))
    if mode not in STOP_MODES:
        raise ValueError(f'止损模式只能是 {STOP_MODES}，收到 {mode!r}')
    price, basis = _stop_by_mode(mode, entry_low, trigger, vwap, bars, C)
    alt = {}
    for m in STOP_MODES:
        if m == mode:
            continue
        alt[m] = _stop_by_mode(m, entry_low, trigger, vwap, bars, C)[0]
    warn = []
    if price is None:
        warn.append('stop_missing')
    elif _f(entry_low) is not None and price >= float(entry_low):
        warn.append('stop_not_below_entry')
    return {'mode': mode, 'price': price, 'basis': basis, 'alt': alt, 'warn': warn}


def sell_plan(cfg: Optional[Dict] = None) -> Dict:
    """三个出口：封板→次日开盘；未封→SELL_TIME；−2% 止损优先。"""
    C = cfg or {}
    t = str(C.get('sell_time', INTRADAY_DRAGON_SELL_TIME))
    return {
        'sealed': EXIT_SEALED,
        'unsealed': EXIT_TIME,
        'stop': EXIT_STOP,
        'sell_time': t,
        'priority': [EXIT_STOP, EXIT_SEALED, EXIT_TIME],
        'text': f'封板→次日开盘；未封→{t}；−2% 止损先触发先走',
    }


# ---------- 组装一条推送 ----------

def build_plans(row: Dict, quote: Optional[Dict], now_hm: str,
                cfg: Optional[Dict] = None) -> List[Dict]:
    """一只候选 → 每臂一条计划（cfg.entry_arms 决定 1 或 2 条）。

    row 是 `intraday_dragon.scan_once()` 的结果，需要 `ev` / `feat` / `point`。
    `quote` 是推送前重取的现价：{'price': .., 'ts': 'YYYYMMDDHHMMSS'}。
    """
    C = cfg or {}
    ev = row.get('ev') or {}
    feat = row.get('feat') or {}
    f = feat.get('features') or {}
    pt = row.get('point') or row.get('trigger') or {}
    sym = str(row.get('symbol') or ev.get('symbol') or '')
    name = str(row.get('name') or ev.get('name') or '')
    trigger = _f(pt.get('price')) or _f(ev.get('price'))
    vwap = _f(feat.get('vwap'))
    lim = _f(feat.get('limit_up')) or _f(ev.get('limit_up'))
    sealed = bool(pt.get('sealed')) or bool(lim and trigger and trigger >= lim - 0.01)
    quote = quote or {}
    qp = _f(quote.get('price'))
    qts = str(quote.get('ts') or '')
    dev = None
    if qp is not None and trigger:
        dev = round((qp / trigger - 1) * 100, 3)

    arms_cfg = str(C.get('entry_arms', INTRADAY_DRAGON_ENTRY_ARMS)).strip().lower()
    arms = list(ARMS if arms_cfg == 'both' else (arms_cfg,))
    # T2.6 开关矩阵：off 才不产回踩臂；shadow 仍两臂都算（只记录，live 不选它）
    try:
        from services.pick import dragon_shadow
        if dragon_shadow.cond_state('pullback', C) == 'off':
            arms = [a for a in arms if a != 'pullback']
    except Exception:
        pass
    out: List[Dict] = []
    for arm in arms:
        er = entry_range(arm, vwap, trigger, C)
        entry_low = er['low'] if er else None
        stop = stop_plan(entry_low, trigger, vwap, row.get('bars'), C)
        out.append({
            'symbol': sym, 'name': name, 'hm': _hm(pt.get('t') or now_hm),
            'score_v2': ev.get('score_v2'), 'score_legacy': ev.get('score_legacy'),
            'price': trigger, 'vwap': vwap, 'limit_up': lim, 'sealed': sealed,
            'chg_from_open': f.get('chg_from_open'), 'gap': f.get('gap'),
            'room': f.get('room'), 'vol_ratio': f.get('vol_ratio_same_time'),
            'amount_5m': f.get('amount_5m'), 'sector_rank': f.get('sector_rank'),
            'trigger_price': trigger,
            'entry_arm': arm if er else None,
            'entry_low': entry_low,
            'entry_high': er['high'] if er else None,
            'entry_basis': er['basis'] if er else None,
            'stop_price': stop['price'], 'stop_mode': stop['mode'],
            'stop_basis': stop['basis'], 'stop_alt': stop['alt'],
            'stop_warn': stop['warn'],
            'stop_pct': float(C.get('stop_pct', INTRADAY_DRAGON_STOP_PCT)),
            'sell': sell_plan(C),
            'position_pct': float(C.get('position_pct', INTRADAY_DRAGON_POSITION_PCT)),
            'quote_price': qp, 'quote_ts': qts, 'deviation': dev,
            'coverage': feat.get('coverage'),
            'score_version': ev.get('score_version'),
            'reasons': [],
        })
    return out


def _score_txt(v) -> str:
    x = _f(v)
    return f'{x:.0f}' if x is not None else '—'


def _pct_txt(v, signed: bool = False) -> str:
    x = _f(v)
    if x is None:
        return '—'
    return f'{x:+.1f}%' if signed else f'{x:.1f}%'


def format_push(row: Dict, *, cfg: Optional[Dict] = None) -> str:
    """固定 6 行（清单 T2.4 第 1 条）。缺失一律 "—"，不填 0、不编板块名。

    纯文本生成，不做推送 I/O。`cfg` 只补 row 缺失的展示默认值（`position_pct`），
    不参与判据；cfg 为空时输出与 T2.4 完全一致。
    """
    p = row or {}
    C = cfg or {}
    sym = str(p.get('symbol') or '')
    code = sym[-6:] if len(sym) >= 6 else sym
    arm_txt = {'pullback': '结构成立/回踩VWAP',
               'breakout': '结构成立/突破触发分钟'}.get(p.get('entry_arm'), '结构成立')
    sector = p.get('sector_rank')
    sector_txt = (f'影子排名{int(float(sector))}' if sector is not None else '未接入')
    stop_pct = float(p.get('stop_pct') or 0)
    stop_mode = p.get('stop_mode')
    stop_head = {'fixed_2pct': f'按成交价×{stop_pct:.0f}%',
                 'vwap': f'VWAP×{stop_pct:.0f}%',
                 'day_low': '按当日最低价'}.get(stop_mode, f'按成交价×{stop_pct:.0f}%')
    pos = p.get('position_pct')
    if pos is None:
        pos = C.get('position_pct')
    lines = [
        f"[龙头观察] {p.get('hm') or '--:--'} {code} {p.get('name') or ''}".rstrip(),
        (f"评分 {_score_txt(p.get('score_v2'))}（旧分 {_score_txt(p.get('score_legacy'))}）"
         f" | 较今开 {_pct_txt(p.get('chg_from_open'), True)}"
         f" | 现价 {_px(p.get('price'))} | VWAP {_px(p.get('vwap'))}"),
        (f"距涨停 {_pct_txt(p.get('room'))} | 量比 {_pct_txt(p.get('vol_ratio'))}"
         f" | 5分钟额 {_amount(p.get('amount_5m'))} | 板块 {sector_txt}"),
        (f"触发：{arm_txt} | 建议区间 {_px(p.get('entry_low'))}~{_px(p.get('entry_high'))}"
         f" | 止损 {stop_head}（示例 {_px(p.get('stop_price'))}）"),
        f"卖出：{(p.get('sell') or {}).get('text') or sell_plan()['text']}",
        (f"仓位：单笔≤{float(pos or 0):.0f}%（组合层另算）"
         f" | 仅供参考，不构成投资建议"),
    ]
    return '\n'.join(lines)


# ---------- 推送前复核 ----------

def parse_quote_ts(ts: str, today: Optional[str] = None) -> Optional[datetime]:
    """腾讯快照时间：14 位 `YYYYMMDDHHMMSS`，或 6 位 `HHMMSS`（补当天）。"""
    s = str(ts or '').strip()
    try:
        if len(s) == 14 and s.isdigit():
            return datetime.strptime(s, '%Y%m%d%H%M%S')
        if len(s) == 6 and s.isdigit():
            day = today or datetime.now().strftime('%Y%m%d')
            return datetime.strptime(day + s, '%Y%m%d%H%M%S')
    except ValueError:
        return None
    return None


def precheck(plan: Dict, now: datetime, cfg: Optional[Dict] = None) -> Dict:
    """报价复核：>60s 或偏离触发价 >1% → 不推。缺时间视为不可用（不拿触发价冒充）。"""
    C = cfg or {}
    max_age = float(C.get('quote_max_age', INTRADAY_DRAGON_PUSH_QUOTE_MAX_AGE))
    dev_max = float(C.get('deviation_max', INTRADAY_DRAGON_PUSH_DEVIATION_MAX))
    reasons: List[str] = []
    if _f(plan.get('entry_low')) is None:
        reasons.append('entry_missing')
    if _f(plan.get('stop_price')) is None:
        reasons.append('stop_missing')
    qp = _f(plan.get('quote_price'))
    age = None
    if qp is None:
        reasons.append('quote_unavailable')
    else:
        dt = parse_quote_ts(plan.get('quote_ts'), now.strftime('%Y%m%d'))
        if dt is None:
            reasons.append('quote_ts_missing')
        else:
            age = (now - dt).total_seconds()
            if age > max_age:
                reasons.append('quote_stale')
            elif age < -5:
                reasons.append('quote_clock_skew')
        dev = plan.get('deviation')
        if dev is not None and abs(float(dev)) > dev_max:
            reasons.append('price_moved')
    if 'stop_not_below_entry' in (plan.get('stop_warn') or []):
        reasons.append('stop_not_below_entry')
    return {'ok': not reasons, 'reasons': reasons, 'quote_age': age}


def fetch_quote(code: str, timeout: float = 5.0) -> Optional[Dict]:
    """推送前重取单只现价 + 报价时间（腾讯 qt；字段 3=价、30=时间）。失败返回 None。"""
    try:
        url = 'https://qt.gtimg.cn/q=' + str(code)
        raw = urllib.request.urlopen(url, timeout=timeout).read().decode('gbk', 'ignore')
        body = raw.split('="', 1)[1].rsplit('"', 1)[0] if '="' in raw else ''
        f = body.split('~')
        if len(f) < 6:
            return None
        price = _f(f[3])
        if price is None or price <= 0:
            return None
        return {'price': price, 'ts': f[30] if len(f) > 30 else '', 'src': 'qt'}
    except Exception as e:
        print(f'[龙头推送] 复核报价失败 {code}: {type(e).__name__}: {e}')
        return None


# ---------- 落库 ----------

_DDL = '''CREATE TABLE IF NOT EXISTS dragon_push_log (
    trade_date  TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    pushed_at   TEXT NOT NULL,
    entry_arm   TEXT NOT NULL,
    name        TEXT,
    push_mode   TEXT,
    decision    TEXT,
    reasons     TEXT,
    score_v2    REAL,
    score_legacy REAL,
    price       REAL,
    quote_price REAL,
    quote_ts    TEXT,
    deviation   REAL,
    chg_from_open REAL,
    gap         REAL,
    room        REAL,
    vwap        REAL,
    vol_ratio   REAL,
    amount_5m   REAL,
    sector_rank REAL,
    entry_low   REAL,
    entry_high  REAL,
    entry_basis TEXT,
    stop_price  REAL,
    stop_mode   TEXT,
    stop_basis  TEXT,
    stop_alt    TEXT,
    exit_sealed TEXT,
    exit_unsealed TEXT,
    exit_stop   TEXT,
    exit_reason TEXT,
    text        TEXT,
    push_version TEXT,
    created_at  TEXT,
    PRIMARY KEY (trade_date, symbol, pushed_at, entry_arm)
)'''

_ALERT_DDL = '''CREATE TABLE IF NOT EXISTS dragon_alert_log (
    trade_date TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    pushed_at  TEXT NOT NULL,
    score      REAL,
    stage      TEXT,
    PRIMARY KEY (trade_date, symbol, pushed_at)
)'''


def ensure_tables(conn=None) -> None:
    own = conn is None
    c = conn or get_db_conn()
    try:
        c.execute(_DDL)
        c.execute(_ALERT_DDL)
        c.commit()
    finally:
        if own:
            c.close()


def quota_check(conn, trade_date: str, symbol: str, mode: str,
                now: datetime, cfg: Optional[Dict] = None) -> Dict:
    """三条额度各自可关。只统计本模式的 push/dry 行（dry 不占 live 额度）。"""
    C = cfg or {}
    out = {'ok': True, 'reasons': [], 'day_symbols': 0, 'stock_rows': 0, 'last_at': None}
    daily_on = bool(C.get('cap_daily_on', INTRADAY_DRAGON_CAP_DAILY_ON))
    per_on = bool(C.get('cap_per_stock_on', INTRADAY_DRAGON_CAP_PER_STOCK_ON))
    cool_on = bool(C.get('cap_cooldown_on', INTRADAY_DRAGON_CAP_COOLDOWN_ON))
    daily_limit = int(C.get('daily_limit', INTRADAY_DRAGON_DAILY_LIMIT))
    per_limit = int(C.get('max_per_stock', INTRADAY_DRAGON_MAX_PER_STOCK))
    cool = float(C.get('cooldown_sec', INTRADAY_DRAGON_COOLDOWN_SEC))
    try:
        if daily_on:
            n = conn.execute(
                'SELECT COUNT(DISTINCT symbol) FROM dragon_push_log '
                'WHERE trade_date=? AND push_mode=? AND decision IN (?,?)',
                (trade_date, mode, 'push', 'dry')).fetchone()[0] or 0
            out['day_symbols'] = int(n)
            if int(n) >= daily_limit:
                out['ok'] = False
                out['reasons'].append('daily_limit')
        if per_on:
            # 同一次推送的两臂共享 pushed_at，按"推送次数"去重，不把两臂算成两次
            n = conn.execute(
                'SELECT COUNT(DISTINCT pushed_at) FROM dragon_push_log '
                'WHERE trade_date=? AND symbol=? AND push_mode=? AND decision IN (?,?)',
                (trade_date, symbol, mode, 'push', 'dry')).fetchone()[0] or 0
            out['stock_rows'] = int(n)
            if int(n) >= per_limit:
                out['ok'] = False
                out['reasons'].append('per_stock_limit')
        if cool_on:
            last = conn.execute(
                'SELECT MAX(pushed_at) FROM dragon_push_log '
                'WHERE trade_date=? AND symbol=? AND push_mode=? AND decision IN (?,?)',
                (trade_date, symbol, mode, 'push', 'dry')).fetchone()[0]
            out['last_at'] = last
            if last:
                try:
                    age = (now - datetime.strptime(last, '%Y-%m-%d %H:%M:%S')).total_seconds()
                    if age < cool:
                        out['ok'] = False
                        out['reasons'].append('cooldown')
                except ValueError:
                    pass
    except Exception as e:
        # 额度查不出来时按"不推"处理，避免重复轰炸
        print(f'[龙头推送] 额度检查失败，本轮不推: {type(e).__name__}: {e}')
        out['ok'] = False
        out['reasons'].append('quota_error')
    return out


def _save_log(conn, plan: Dict, mode: str, decision: str, reasons: List[str],
              text: str, now: datetime) -> None:
    today = now.strftime('%Y-%m-%d')
    ts = now.strftime('%Y-%m-%d %H:%M:%S')
    sell = plan.get('sell') or {}
    conn.execute(
        'INSERT OR IGNORE INTO dragon_push_log '
        '(trade_date,symbol,pushed_at,entry_arm,name,push_mode,decision,reasons,score_v2,'
        'score_legacy,price,quote_price,quote_ts,deviation,chg_from_open,gap,room,vwap,'
        'vol_ratio,amount_5m,sector_rank,entry_low,entry_high,entry_basis,stop_price,'
        'stop_mode,stop_basis,stop_alt,exit_sealed,exit_unsealed,exit_stop,exit_reason,'
        'text,push_version,created_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (today, plan.get('symbol'), ts, plan.get('entry_arm') or '-', plan.get('name'),
         mode, decision, json.dumps(reasons or [], ensure_ascii=False),
         plan.get('score_v2'), plan.get('score_legacy'), plan.get('price'),
         plan.get('quote_price'), plan.get('quote_ts'), plan.get('deviation'),
         plan.get('chg_from_open'), plan.get('gap'), plan.get('room'), plan.get('vwap'),
         plan.get('vol_ratio'), plan.get('amount_5m'), plan.get('sector_rank'),
         plan.get('entry_low'), plan.get('entry_high'), plan.get('entry_basis'),
         plan.get('stop_price'), plan.get('stop_mode'), plan.get('stop_basis'),
         json.dumps(plan.get('stop_alt') or {}, ensure_ascii=False),
         sell.get('sealed'), sell.get('unsealed'), sell.get('stop'),
         # exit_reason 由 T3.1 回测/跟踪引擎在真正卖出时写入；T2.4 只落三条出口定义
         None, text, PUSH_VERSION, ts))


# ---------- 入口 ----------

def dispatch(rows: List[Dict], now: Optional[datetime] = None,
             push_fn=None, quote_fn=None, cfg: Optional[Dict] = None) -> Dict:
    """T2.4 推送入口。返回统计；任何异常都不抛出（主链路优先）。

    rows：`scan_once()` 的返回；只有 `ev.verdict == 'candidate'` 的行会被处理。
    """
    C = dict(cfg or {})
    mode = ensure_mode(C.get('mode', INTRADAY_DRAGON_PUSH_MODE))
    stats = {'mode': mode, 'rows': 0, 'plans': 0, 'dry': 0, 'sent': 0,
             'observe': 0, 'blocked': 0, 'risk_blocked': 0, 'skipped': None, 'reasons': {}}
    if mode == 'legacy':
        stats['skipped'] = 'legacy'
        return stats

    cand = [r for r in (rows or []) if (r.get('ev') or {}).get('verdict') == 'candidate']
    stats['rows'] = len(cand)
    if not cand:
        stats['skipped'] = 'no_candidate'
        return stats

    now = now or datetime.now()
    if not in_window(now.strftime('%H:%M'), C):
        stats['skipped'] = 'out_of_window'
        return stats

    live = mode == 'v2_live'
    if live and not bool(C.get('push_enabled', INTRADAY_DRAGON_PUSH_ENABLED)):
        stats['skipped'] = 'push_disabled'
        return stats

    dry_send = bool(C.get('dry_send', INTRADAY_DRAGON_PUSH_DRY_SEND)) and not live
    if push_fn is None and (live or dry_send):
        from services.support.push import push_wecom_webhook
        push_fn = push_wecom_webhook
    if quote_fn is None:
        quote_fn = fetch_quote
    live_arm = str(C.get('live_arm', INTRADAY_DRAGON_ENTRY_LIVE_ARM))
    # T2.6：回踩买不是 live 时不拿它做实盘臂，退回突破臂（默认 live，行为不变）
    if live_arm == 'pullback':
        try:
            from services.pick import dragon_shadow
            if not dragon_shadow.enforce('pullback', C):
                live_arm = 'breakout'
        except Exception:
            pass
    today = now.strftime('%Y-%m-%d')

    conn = None
    try:
        conn = get_db_conn()
        ensure_tables(conn)
        for r in cand:
            sym = str(r.get('symbol') or '')
            # T2.5：推送前最后一道风控减法。闸在取报价之前，被挡的不浪费请求。
            try:
                from services.pick import dragon_risk
                rk = dragon_risk.gate(r, C)
            except Exception as rk_err:
                rk = {'ok': True, 'reasons': [], 'notes': [],
                      'error': f'{type(rk_err).__name__}: {rk_err}'}
                print(f'[龙头风控] 判定异常（按放行处理并留痕）{sym}: {rk["error"]}')
            if not rk.get('ok'):
                stats['risk_blocked'] += 1
                for x in rk.get('reasons') or ['risk_unknown']:
                    stats['reasons'][x] = stats['reasons'].get(x, 0) + 1
                feats = (r.get('feat') or {}).get('features') or {}
                minimal = {
                    'symbol': sym, 'name': r.get('name'),
                    'price': (r.get('ev') or {}).get('price'),
                    'score_v2': (r.get('ev') or {}).get('score_v2'),
                    'score_legacy': (r.get('ev') or {}).get('score_legacy'),
                    'chg_from_open': feats.get('chg_from_open'),
                    'gap': feats.get('gap'), 'room': feats.get('room'),
                    'sell': {}, 'stop_alt': {},
                }
                _save_log(conn, minimal, mode, 'observe', rk.get('reasons') or [], '', now)
                print(f"[龙头风控] {r.get('name')}({sym}) 挡住: {','.join(rk.get('reasons') or [])}")
                continue
            q = None
            try:
                q = quote_fn(sym)
            except Exception as e:
                print(f'[龙头推送] 重取报价异常 {sym}: {type(e).__name__}: {e}')
            plans = build_plans(r, q, now.strftime('%H:%M'), C)
            if not plans:
                stats['reasons']['no_plan'] = stats['reasons'].get('no_plan', 0) + 1
                continue
            quota_mode = mode if live else 'v2_dry'
            quota = quota_check(conn, today, sym, quota_mode, now, C)
            if not quota['ok']:
                stats['blocked'] += 1
                for x in quota['reasons']:
                    stats['reasons'][x] = stats['reasons'].get(x, 0) + 1
                print(f"[龙头推送] {r.get('name')}({sym}) 额度挡住: {','.join(quota['reasons'])}")
                continue
            if live:
                # live 只发拍板臂；那一臂缺参照价时记观察，不拿另一臂顶替
                plans = [p for p in plans if p.get('entry_arm') == live_arm]
                if not plans:
                    stats['observe'] += 1
                    stats['reasons']['live_arm_missing'] = (
                        stats['reasons'].get('live_arm_missing', 0) + 1)
                    print(f"[龙头推送] {r.get('name')}({sym}) 拍板臂 {live_arm} 缺参照价，不推")
                    continue

            for p in plans:
                stats['plans'] += 1
                text = format_push(p)
                chk = precheck(p, now, C)
                if not chk['ok']:
                    stats['observe'] += 1
                    for x in chk['reasons']:
                        stats['reasons'][x] = stats['reasons'].get(x, 0) + 1
                    _save_log(conn, p, mode, 'observe', chk['reasons'], text, now)
                    continue
                if live:
                    ok_send = bool(push_fn(text)) if push_fn else False
                    if ok_send:
                        stats['sent'] += 1
                        _save_log(conn, p, mode, 'push', [], text, now)
                        conn.execute(
                            'INSERT OR IGNORE INTO dragon_alert_log '
                            '(trade_date,symbol,pushed_at,score,stage) VALUES (?,?,?,?,?)',
                            (today, sym, now.strftime('%Y-%m-%d %H:%M:%S'),
                             p.get('score_v2'), 'v2'))
                    else:
                        stats['observe'] += 1
                        stats['reasons']['send_failed'] = stats['reasons'].get('send_failed', 0) + 1
                        _save_log(conn, p, mode, 'observe', ['send_failed'], text, now)
                else:
                    stats['dry'] += 1
                    _save_log(conn, p, mode, 'dry', [], text, now)
                    if dry_send and push_fn:
                        push_fn(text)
        conn.commit()
    except Exception as e:
        print(f'[龙头推送] 落库/发送异常（不影响主链路）: {type(e).__name__}: {e}')
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return stats
