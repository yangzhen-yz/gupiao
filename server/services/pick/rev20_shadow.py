"""rev20_shadow.py - 新③：rev20+缩量 候选池影子臂（2026-11-03，只记录不改候选）

依据（README §9.6②，analysis_out/factor_lab.py 85 个截面体检）：
  `rev20+shrink`（20 日反转截面分位 ≥0.7 且 量能收缩分位 ≥0.6）是全部组合里
  唯一稳健为正的（5 日超额 +0.12pp）。在动它之前先落影子攒 20 个交易日样本，
  判据用可成交净期望 + 最深回撤，与修②同一批裁决。

口径必须说清的两点：
  1. 分位分母 = **当日打分集**（scored 全量），不是体检的全市场截面——
     后者是 2462 只的截面分位，前者是候选池内相对位置，裁决时如实注明口径差。
  2. 日K取本地库 `stock_daily` 最近 21 根已收盘 bar（18:15 同步），盘中即
     "昨收基准"，与 RPS 榜的既有口径一致；不足 21 根（次新）该票留空不硬算。

只记录：本模块不改候选、不改分、不改门槛——线上"推什么"逐字节等于现状。
"""
import sqlite3
from typing import Dict, List


def enabled() -> bool:
    """独立开关：热读覆盖表，读不到退 config 默认（True=只记录）。"""
    try:
        from services.support import app_settings
        v = app_settings.value('rev20_shadow_enabled')
        if v is not None:
            return bool(v)
    except Exception:
        pass
    from app.config import RECOMMEND_REV20_SHADOW_ENABLED
    return bool(RECOMMEND_REV20_SHADOW_ENABLED)


def _raw_map(symbols: List[str]) -> Dict[str, Dict]:
    """从本地日K算每票的 rev20 / vol_shrink 原始值（最近 21 根已收盘 bar）。

    rev20      = -(close[-1] / close[-21] - 1)   （20 日跌得越多值越大）
    vol_shrink = -(mean(vol[-5:]) / mean(vol[-20:]))  （越缩量值越大）
    与 factor_lab.py 的定义逐字一致；库内存纯 6 位代码，前缀用 s[-6:] 剥。
    """
    ph = ','.join('?' * len(symbols))
    sql = f'''SELECT symbol, close, volume FROM (
        SELECT symbol, close, volume,
               ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) rn
        FROM stock_daily WHERE symbol IN ({ph})
    ) WHERE rn<=21 ORDER BY symbol, rn'''
    out: Dict[str, Dict] = {}
    try:
        from app.config import KLINE_HISTORY_DB_PATH
        conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=30)
        try:
            rows = conn.execute(sql, [s[-6:] for s in symbols]).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(f'[rev20影子] 本地日K读取失败(列留空): {e}')
        return {}
    cur = None
    closes: List[float] = []
    vols: List[float] = []
    rows.append((None, None, None))  # 哨兵，触发最后一组结算
    for sym, close, vol in rows:
        if sym != cur:
            if cur and len(closes) >= 21 and len(vols) >= 21 \
                    and closes[-1] > 0 and closes[-21] > 0 \
                    and all(v and v > 0 for v in vols):
                v5 = sum(vols[:5]) / 5.0
                v20 = sum(vols[:20]) / 20.0
                if v20 > 0:
                    # 数组顺序：SQL 取 rn<=21 且 ORDER BY symbol, rn → 同 symbol 内
                    # rn 升序，closes[0]=最新(t)，closes[-1]=最旧(t-20)。
                    # 反转因子需 -(最新/最旧 - 1)：跌得越多(最新<最旧)→rev20 越大，
                    # 与 factor_lab.py:68 `-(close / close.shift(20) - 1)` 逐字一致。
                    # 旧公式 -(closes[-1]/closes[-21]-1) 算成了“涨幅(动量)”而非“跌幅反转”，符号反了。
                    out[cur] = {'rev20': -(closes[0] / closes[-1] - 1),
                                    'volShrink': -(v5 / v20)}
            cur, closes, vols = sym, [], []
        if sym is None:
            continue
        try:
            closes.append(float(close))
            vols.append(float(vol))
        except (TypeError, ValueError):
            pass
    return out


def _pct(value: float, values: List[float]) -> float:
    """截面百分位（值 ≤ 自己的占比，0~1）。"""
    if not values:
        return 0.0
    return sum(1 for x in values if x <= value) / len(values)


async def rev20_shrink_labs(all_results: List[Dict]) -> Dict[str, Dict]:
    """主入口：给打分集每只票生成 rev20/vol_shrink 影子标签。

    返回 {sym_lower: {'rev20': raw, 'vol_shrink': raw, 'rev20_shadow': {...}}}，
    由 recommend.py 并进 _labs，随 strategy_snapshot 落库。
    """
    import asyncio
    if not enabled() or not all_results:
        return {}
    syms: List[str] = []
    for s in all_results:
        sym = (s.get('symbol') or '').lower()
        if sym and sym not in syms:
            syms.append(sym)
    if not syms:
        return {}
    raw = await asyncio.to_thread(_raw_map, syms)
    if not raw:
        return {}
    rev_vals = [r['rev20'] for r in raw.values()]
    shr_vals = [r['volShrink'] for r in raw.values()]
    out: Dict[str, Dict] = {}
    for sym, r in raw.items():
        rev_pct = _pct(r['rev20'], rev_vals)
        shr_pct = _pct(r['volShrink'], shr_vals)
        tag = {'rev20': round(r['rev20'], 4),
               'volShrink': round(r['volShrink'], 4),
               'rev20Pct': round(rev_pct, 3),
               'shrinkPct': round(shr_pct, 3),
               'denom': len(rev_vals),
               'pass': bool(rev_pct >= 0.7 and shr_pct >= 0.6)}
        out[sym] = {'rev20': tag['rev20'], 'vol_shrink': tag['volShrink'],
                    'rev20_shadow': tag}
    return out
