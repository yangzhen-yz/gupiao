"""anti_drop_shadow.py - 抗跌因子（2026-11-07，修⑥①，**只记录、不进评分/门槛/推送**）

补的是哪一块
------------
选股 alpha≈0 已经证过（10-23 真推荐开盘口径 1/3/5/10 日超额全负），卖出侧更值得做，
而卖出侧第一块就是"这票在大盘跌的时候扛不扛"。11-03 那次把四大指数日线本地化进了
`index_daily`（702 行×4 指数，实测到 2026-11-05），但**没有任何读取方** —— 前置做完了，
因子本体一直没写。这个模块就是那个本体，先挂影子：攒够对照数据再谈要不要进动作。

口径（三条，逐条能报数）
------------------------
窗口 = 最近 `ANTI_DROP_WINDOW_DAYS`（默认 20）个**指数已收盘日**，日期轴以指数为准
（个股与指数按同一批日期对齐，缺一天就少一天，不补数）。

1. `relMaxDdPp` = 个股区间最大回撤 − 指数同期最大回撤，单位百分点。**负 = 比大盘抗跌**。
   最大回撤 = 按时间顺序 `max((峰 - 价) / 峰)`。
2. `downBeta` = 在指数跌幅 ≤ −0.3% 的那些日子里，`mean(个股日收益) / mean(指数日收益)`。
   两个都是负数，比值 <1 = 大盘跌 1 个点它只跌 0.6 个。**分母为零（窗口里没有下跌日）
   就记 None，不假装是 0**——0 会被读成"完全抗跌"。
3. `holdRate` = 同样那批下跌日里，"个股跌幅小于指数跌幅一半"的天数占比。

分位 `antiDropPct` 用当日打分集算（`-relMaxDdPp` 越大越抗跌），与 rev20 影子同一套路数；
分母与体检的全市场截面不同，裁决时要注明。

板块口径为什么先不接
--------------------
`docs/后续计划.md` 原话是"相对大盘/板块回撤"。板块那一半现在算不出来：
`cls_plate_daily` 实测只有 **4 个交易日**（2026-11-02 ~ 11-05，1808 行），20 日窗口拼不满，
而且它缺 10-30 与 11-06 两天（11-06 是调度器整体冻结事故那天）。所以：
**基准只有大盘（`ANTI_DROP_INDEX_SYMBOL`=sh000001），代码里 `bench` 字段如实写 sh000001**，
等板块日收益攒够 20 天再叠第二条腿（接的时候是 `plate_daily_return` 的活儿，不是这里改口径）。

本地日K 的新鲜度是个真坑，这里当门槛用
--------------------------------------
11-06 那次炸板影子踩过的坑：`stock_daily`（在 `history_kline.db`）里 5222 只票只有约 2460 只
同步到最新日，其余整片停在旧日期。抗跌因子要是拿旧序列算，会算出"这票 20 日没跌"这种假抗跌。
所以这里**只算最后一根 bar 的日期 ≥ 窗口末日往前 `ANTI_DROP_MIN_LAST_LAG_DAYS` 天的票**，
其余进 `stale` 计数并记原因 —— 覆盖率是产数质量的一部分，不是可忽略的实现细节。

关掉开关必须等于改造前
----------------------
`ANTI_DROP_SHADOW_ENABLED=False`（或覆盖表 `anti_drop_shadow_enabled=0`）时 `anti_drop_labs()`
直接返回空 dict，接线处一行都不写，快照与推荐结果逐字节不变。
"""
import os
import sqlite3
from typing import Dict, List, Optional, Tuple

_WIN_DEFAULT = 20
_INDEX_DEFAULT = 'sh000001'


def _cfg(name: str, default):
    try:
        from app import config
        return getattr(config, name, default)
    except Exception:
        return default


def enabled() -> bool:
    """独立开关：先热读覆盖表，读不到退 config 默认（True=只记录）。"""
    try:
        from services.support import app_settings
        v = app_settings.value('anti_drop_shadow_enabled')
        if v is not None:
            return bool(v)
    except Exception:
        pass
    return bool(_cfg('ANTI_DROP_SHADOW_ENABLED', True))


def window_days() -> int:
    return max(5, int(_cfg('ANTI_DROP_WINDOW_DAYS', _WIN_DEFAULT)))


def index_symbol() -> str:
    return str(_cfg('ANTI_DROP_INDEX_SYMBOL', _INDEX_DEFAULT))


def index_window(days: Optional[int] = None) -> Tuple[List[str], List[float]]:
    """最近 N 个指数收盘日：返回（日期升序，收盘价升序）。表是 15:15 落的，盘中即昨收基准。"""
    n = days or window_days()
    sym = index_symbol()
    conn = None
    try:
        from db.database import get_db_conn
        conn = get_db_conn()
        rows = conn.execute(
            'SELECT date, close FROM index_daily WHERE symbol=? ORDER BY date DESC LIMIT ?',
            (sym, n + 1)).fetchall()
    except sqlite3.Error as e:
        print(f'[抗跌影子] 指数日线读取失败: {type(e).__name__}: {e}')
        return [], []
    except Exception as e:
        print(f'[抗跌影子] 指数日线读取异常（不阻断主流程）: {type(e).__name__}: {e}')
        return [], []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    rows = list(reversed(rows))                     # 时间正序，回撤要按路径算
    dates = [str(r[0]) for r in rows]
    closes = [float(r[1]) for r in rows if r[1] is not None]
    if len(closes) != len(dates):
        return [], []
    return dates, closes


def _max_drawdown(closes_asc: List[float]) -> Optional[float]:
    """按时间顺序的最大回撤（0.08 = 从峰到谷跌了 8%）。不足两根给 None。"""
    if len(closes_asc) < 2:
        return None
    peak, mdd = closes_asc[0], 0.0
    for v in closes_asc[1:]:
        if v > peak:
            peak = v
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd


def _daily_returns(closes_asc: List[float]) -> List[float]:
    out = []
    for i in range(1, len(closes_asc)):
        prev = closes_asc[i - 1]
        out.append((closes_asc[i] / prev - 1.0) if prev else 0.0)
    return out


def metrics(stock_closes_by_date: Dict[str, float], idx_dates: List[str],
            idx_closes: List[float], down_threshold: float = -0.3) -> Optional[Dict]:
    """单票指标。`stock_closes_by_date` 是 {date: close}；缺的日子跳过（不补数、不前向填充）。"""
    pairs = [(d, stock_closes_by_date.get(d)) for d in idx_dates]
    have = [(d, float(v)) for d, v in pairs if v is not None and float(v) > 0]
    if len(have) < max(6, len(idx_dates) // 2):
        return None                                 # 对齐上的日子太少，算出来的分位没意义
    s_dates = [d for d, _ in have]
    s_closes = [v for _, v in have]
    s_pos = {d: i for i, d in enumerate(s_dates)}
    # 指数只在"个股也有数"的那些日子上的序列（两边同一段日期轴，回撤才可比）
    i_common = [idx_closes[idx_dates.index(d)] for d in s_dates]
    dd_s, dd_i = _max_drawdown(s_closes), _max_drawdown(i_common)
    if dd_s is None or dd_i is None:
        return None
    r_s, r_i = _daily_returns(s_closes), _daily_returns(i_common)
    down_idx = [(rs, ri) for rs, ri in zip(r_s, r_i) if ri * 100 <= down_threshold]
    down_beta: Optional[float] = None
    if down_idx:
        mi = sum(x[1] for x in down_idx) / len(down_idx)
        if mi < 0:
            ms = sum(x[0] for x in down_idx) / len(down_idx)
            down_beta = round(ms / mi, 3)
    hold = (sum(1 for rs, ri in down_idx if rs >= ri / 2.0) / len(down_idx)) if down_idx else None
    return {'relMaxDdPp': round((dd_s - dd_i) * 100, 2),
            'stockMaxDdPct': round(dd_s * 100, 2),
            'indexMaxDdPct': round(dd_i * 100, 2),
            'downBeta': down_beta,
            'holdRate': round(hold, 3) if hold is not None else None,
            'downDays': len(down_idx),
            'bars': len(s_closes),
            'firstDate': s_dates[0], 'lastDate': s_dates[-1],
            'bench': index_symbol()}


def anti_drop_map(symbols: List[str], days: Optional[int] = None) -> Tuple[Dict[str, Dict], Dict]:
    """批量算抗跌指标。返回（{小写代码: 指标}，覆盖率 meta）。零写入、纯读本地库。"""
    out: Dict[str, Dict] = {}
    meta = {'asked': len(symbols), 'computed': 0, 'stale': 0, 'notEnough': 0,
            'indexDays': 0, 'window': days or window_days(), 'bench': index_symbol()}
    if not symbols:
        return out, meta
    idx_dates, idx_closes = index_window(days)
    meta['indexDays'] = len(idx_dates)
    if len(idx_dates) < 6:
        meta['reason'] = f'指数日线不足（{len(idx_dates)} 天，需 ≥6）'
        return out, meta
    last_ok = idx_dates[-1 - int(_cfg('ANTI_DROP_MIN_LAST_LAG_DAYS', 2))]
    ph = ','.join('?' * len(symbols))
    try:
        from app.config import KLINE_HISTORY_DB_PATH
        conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=30)
        try:
            rows = conn.execute(
                f'SELECT symbol, date, close FROM stock_daily WHERE symbol IN ({ph}) '
                f'AND date>=? ORDER BY symbol, date',
                [s[-6:] for s in symbols] + [idx_dates[0]]).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(f'[抗跌影子] 本地日K读取失败(整列留空): {type(e).__name__}: {e}')
        meta['reason'] = f'本地日K读取失败: {type(e).__name__}'
        return out, meta
    series: Dict[str, Dict[str, float]] = {}
    latest: Dict[str, str] = {}
    for sym, d, close in rows:
        if close is None:
            continue
        try:
            c = float(close)
        except (TypeError, ValueError):
            continue
        full = _to_full_symbol(str(sym))
        series.setdefault(full, {})[str(d)] = c
        if str(d) > latest.get(full, ''):
            latest[full] = str(d)
    for s in symbols:
        full = _to_full_symbol(s)                   # 输入两种形态都要能吃：带前缀（策略快照）
        if not full:
            meta['notEnough'] += 1
            continue                                # 与裸 6 位码（universe.selectable_symbols）。
        if full not in series:                      # 第一版在这里写 `s.lower()`，于是裸码全部
            meta['notEnough'] += 1                  # 落到"没数据"分支：真库实测 1102 只 computed=0
            continue
        if latest.get(full, '') < last_ok:
            meta['stale'] += 1                     # 日K落后的票一律不算（11-06 炸板影子的教训）
            continue
        m = metrics(series[full], idx_dates, idx_closes)
        if m is None:
            meta['notEnough'] += 1
            continue
        out.setdefault(full, m)
        meta['computed'] += 1
    meta['coverage'] = round(meta['computed'] / max(1, meta['asked']), 3)
    return out, meta


def _to_full_symbol(raw: str) -> str:
    """history_kline.db 里存的是纯 6 位码；这里补回 sh/sz 前缀与线上代码口径一致。

    规则与 `universe`/`kline_history` 用的一致：60/9x → sh，其余 → sz。北交所（8/4 开头）
    在选股范围外，出现就原样返回小写，不猜。
    """
    r = str(raw).strip().lower()
    if r[:2] in ('sh', 'sz', 'bj'):
        return r
    if r[:1] == '6':
        return 'sh' + r
    if r[:1] in ('8', '4'):
        return 'bj' + r
    return 'sz' + r


def _pct(value: float, values: List[float]) -> float:
    """截面百分位（值 ≤ 自己的占比，0~1）。"""
    if not values:
        return 0.0
    return round(sum(1 for x in values if x <= value) / len(values), 3)


async def anti_drop_labs(all_results: List[Dict]) -> Tuple[Dict[str, Dict], Dict]:
    """给策略快照用的异步入口：传当日打分集，返回（{symbol: {'antiDrop': {...}, 'antiDropPct': x}}，meta）。

    与 `rev20_shrink_labs` 同一用法：调用方 `_labs.setdefault(sym, {}).update(...)`，
    **只填快照字段，不改候选、不改分数、不改动作**。
    """
    if not enabled():
        return {}, {'skipped': '开关关闭'}
    syms = [(r.get('symbol') or '').lower() for r in (all_results or []) if r.get('symbol')]
    if not syms:
        return {}, {'asked': 0}
    vals, meta = anti_drop_map(syms)
    keys = [-m['relMaxDdPp'] for m in vals.values()]          # 越抗跌值越大
    labs: Dict[str, Dict] = {}
    for sym, m in vals.items():
        labs[sym] = {'antiDrop': m, 'antiDropPct': _pct(-m['relMaxDdPp'], keys)}
    meta['labs'] = len(labs)
    return labs, meta
