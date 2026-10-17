"""universe.py - 可做交易的股票范围（沪深主板、剔除 ST/*ST/退市整理、总市值≥下限）

用户口径（2026-10-16 原话）：**"目前我暂时只针对沪深主板的股票，而且要排除掉 ST 和 *ST"**，
同日又加了一条：**"所有的选股条件默认：沪深主板、非ST和*ST、市值大于35亿"**。
这件事必须由一个地方统一说了算，原因不只是偏好：

  1. 涨跌停幅度不同 —— 主板 10%、创业板/科创板 20%、北交所 30%、ST 5%。
     海选与复盘里的"封板 / 炸板 / 准涨停"全按百分比阈值判定，混板等于混口径；
  2. ST 的 5% 板 + 退市风险，是"5 日内大面率"最高的一类，恰好是他最不想接的；
  3. 候选池、推荐、回测、推送如果各用各的范围，就会出现"回测说有效、
     推出来的却是他根本不买的板"。

名称与状态来自 `history_kline.db` 的 `stock_basic_full`，由 `python scripts/sync_stock_basic.py`
从 baostock 全量名录同步（那张表以前是空的，所以 ST 根本判不了）。
"""
from __future__ import annotations

import sqlite3
from functools import lru_cache
from typing import Iterable, List, Optional, Set

from app.config import KLINE_HISTORY_DB_PATH

# 这两个开关 2026-10-17 起改成**用的时候现读**（`_flag`）：界面在复盘子页拨一下就要立刻影响
# 选票范围，不能再依赖 `from app.config import X` 那种导入时绑定。
# config.py 里的值仍然是默认值 —— 覆盖表没记录时走它。


def _flag(key: str) -> bool:
    try:
        from services.support import app_settings
        return bool(app_settings.value(key))
    except Exception as e:
        # 覆盖表读不到就退回 config 默认值，绝不能让"范围判断"把选票链路整个打断
        print(f'[股票范围] 读覆盖表失败(退回默认值): {type(e).__name__}: {e}')
        import app.config as cfg
        return bool(getattr(cfg, _FLAG_TO_CFG[key]))


_FLAG_TO_CFG = {'universe_main_board_only': 'UNIVERSE_MAIN_BOARD_ONLY',
                'universe_exclude_st': 'UNIVERSE_EXCLUDE_ST'}


def invalidate_cache() -> None:
    """配置改动后打掉本进程缓存（范围清单 + ST 名录 + 选票范围）。"""
    _SCOPE_CACHE['value'] = []
    _SELECTABLE_CACHE['value'] = []
    try:
        excluded_symbols.cache_clear()
    except Exception:
        pass

MAIN_BOARD_PREFIXES = ('600', '601', '603', '605',        # 沪市主板
                       '000', '001', '002', '003')        # 深市主板（含并入的中小板）
MAIN_BOARD_RE = '^(600|601|603|605|000|001|002|003)'      # 给 pandas str.match 用


def _pure(symbol: str) -> str:
    s = (symbol or '').strip().lower()
    return s[2:] if s[:2] in ('sh', 'sz', 'bj') else s


def is_main_board(symbol: str) -> bool:
    return _pure(symbol).startswith(MAIN_BOARD_PREFIXES)


def is_st_name(name: str) -> bool:
    """ST / *ST / S*ST / 退市整理期（名称含"退"）一律算风险警示股。"""
    n = (name or '').upper()
    return ('ST' in n) or ('退' in n)


@lru_cache(maxsize=1)
def excluded_symbols() -> Set[str]:
    """当前该排除的裸 6 位码（ST/*ST/退市整理）。名录缺失时返回空集并提示去同步。"""
    if not _flag('universe_exclude_st'):
        return set()
    try:
        conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=30)
        try:
            rows = conn.execute("SELECT symbol, name FROM stock_basic_full").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        rows = []
    if not rows:
        print('[股票范围] 读不到 stock_basic_full，ST 判定失效 —— 先跑 python scripts/sync_stock_basic.py')
        return set()
    return {_pure(r[0]) for r in rows if is_st_name(r[1])}


def in_universe(symbol: str, name: str = '') -> bool:
    """单只票是否在可做范围内（name 传了就用，省一次查库）。"""
    code = _pure(symbol)
    if _flag('universe_main_board_only') and not code.startswith(MAIN_BOARD_PREFIXES):
        return False
    if _flag('universe_exclude_st'):
        if name:
            return not is_st_name(name)
        return code not in excluded_symbols()
    return True


def filter_symbols(symbols: Iterable[str]) -> Set[str]:
    """批量筛（一次查库，别在循环里逐只查）。返回裸码集合。"""
    codes = {_pure(s) for s in symbols}
    if _flag('universe_main_board_only'):
        codes = {c for c in codes if c.startswith(MAIN_BOARD_PREFIXES)}
    return codes - excluded_symbols()


def _db_symbols() -> List[str]:
    """本地日K库里有历史数据的全部裸码（表缺失/库缺失返回空，交给调用方兜底）。"""
    try:
        conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=30)
        try:
            return [r[0] for r in conn.execute(
                'SELECT DISTINCT symbol FROM stock_daily').fetchall() if r[0]]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


_SCOPE_CACHE = {'ts': 0.0, 'value': []}
_SCOPE_TTL = 600.0      # 选股范围一天只会因"新股上市/市值刷新"变一次，10 分钟缓存足够新
_SELECTABLE_CACHE = {'ts': 0.0, 'value': []}
_SELECTABLE_TTL = 600.0  # 选票范围同节奏（2026-10-17：/api/settings 每次要算 5s，加结果缓存）


def eligible_symbols(symbols: Optional[Iterable[str]] = None) -> list:
    """**选股范围的唯一口径**：主板 + 非ST/退 + 总市值≥下限（裸 6 位码，已排序）。

    为什么要有这个函数：2026-10-16 之前的分工是"主板/ST 由本模块管、市值由 market_cap 管"，
    于是每个调用方要自己记得调两次，一旦漏一次，"选股范围"就退化成两种不同的东西 ——
    实测后果就是同步与覆盖率统计的分母用 5222（整库），而选票用 2462（真能买的那些），
    同一个"覆盖 755 只"在两套分母下分别是 14% 和 31%，看日志的人根本判断不出严重程度。
    现在任何需要"该有多少只票"的地方都走这里，不再各算各的。

    symbols=None 时用本地日K库的清单（≈已经攒了历史数据的那些），结果缓存 10 分钟：
    `get_db_stats` 之类的接口会反复问"范围里有多少只"，不必每次重扫 5222 行。
    """
    import time as _time
    if symbols is None:
        if _SCOPE_CACHE['value'] and _time.time() - _SCOPE_CACHE['ts'] < _SCOPE_TTL:
            return list(_SCOPE_CACHE['value'])
        result = _compute_eligible(_db_symbols())
        _SCOPE_CACHE.update(ts=_time.time(), value=result)
        return list(result)
    return _compute_eligible(symbols)


def _compute_eligible(symbols: Iterable[str]) -> list:
    codes = sorted(filter_symbols(symbols))
    try:
        from services.data import market_cap
        if market_cap.enabled_flag():
            codes = sorted(market_cap.filter_symbols(codes))
    except Exception as e:                                  # 市值表读不到时按主板+非ST放行
        print(f'[股票范围] 市值下限这层没接上(按不含市值处理): {e}')
    return codes


def eligible_count(symbols: Optional[Iterable[str]] = None) -> int:
    """选股范围有多少只（2026-10-16 实测 2462）—— 所有"分母"的唯一来源。"""
    return len(eligible_symbols(symbols))


def selectable_symbols() -> list:
    """**选票范围** = 数据范围（主板+非ST/退+市值≥下限）再叠流动性/价位三条件。

    为什么要和 `eligible_symbols()` 分成两层（2026-10-16）：用户追加"日均成交额>8000万、
    日均换手>1.5%、价>3元"是为了**减少无效筛选**，收口后 2462 → 1094 只；但这三条绝不能拿去
    管日K同步，否则一只票今天缩量、明天放量就会在同步清单里来回进出，历史序列直接挖洞，
    MA60 / 60 日高点 / RPS 全被这个洞污染。所以：
      · `eligible_symbols()` = 数据层（同步清单 + 覆盖率分母，稳定）
      · `selectable_symbols()` = 选票层（海选百分位排名、推荐入口、趋势/批量扫描）

    2026-10-17：加 10 分钟结果缓存（流动性聚合一次约 5s，/api/settings 与预告接口
    每次打开都要问一遍）。配置改动时 `invalidate_cache()` 会一起打掉。
    """
    import time as _time
    if _SELECTABLE_CACHE['value'] and _time.time() - _SELECTABLE_CACHE['ts'] < _SELECTABLE_TTL:
        return list(_SELECTABLE_CACHE['value'])
    try:
        from services.data import liquidity
        result = liquidity.filter_codes(eligible_symbols())
    except Exception as e:
        print(f'[股票范围] 流动性这层没接上(按不含流动性处理): {e}')
        result = eligible_symbols()
    _SELECTABLE_CACHE.update(ts=_time.time(), value=list(result))
    return list(result)


def selectable_count() -> int:
    return len(selectable_symbols())


def selectable_prefixed(symbols: Iterable[str]) -> list:
    """选票层的入口过滤：主板 + 非ST/退 + 市值下限 + 流动性/价位三条件，一次过。"""
    kept = filter_prefixed(symbols)
    try:
        from services.data import liquidity
        return liquidity.filter_prefixed(kept)
    except Exception as e:
        print(f'[股票范围] 流动性这层没接上(按不含流动性处理): {e}')
        return kept


def describe_selectable() -> str:
    try:
        from services.data import liquidity
        return f'{describe_full()}、{liquidity.describe()}'
    except Exception:
        return describe_full()


def describe_full() -> str:
    """带市值下限的完整口径文案，用于日志与告警（describe() 只讲本模块负责的两条）。"""
    try:
        from services.data import market_cap
        return f'{describe()}、总市值≥{market_cap.min_cap() / 1e8:.0f}亿'
    except Exception:
        return describe()


def filter_prefixed(symbols: Iterable[str]) -> list:
    """**唯一的入口过滤**：保留"选股范围"内的票（主板 + 非ST/退 + 总市值≥下限）。

    推荐链路全程用 `sh600000 / sz000001` 这种带前缀的码，而库里存的是裸 6 位码，
    所以入口过滤必须"原样进出"，否则过滤一次就把下游的键名换掉了。

    2026-10-16 把市值下限也并进来：以前"主板/ST 归本模块、市值归 market_cap"，
    调用方要记得调两次，漏一次就出现"海选剔了小市值、趋势扫描还留着"这种分裂口径
    （实测 `trend.py` 与批量扫描两处就只调了市值）。现在凡走这个函数的三条件一次过。
    """
    try:
        from services.data import market_cap
        cap_on = market_cap.enabled_flag() and bool(market_cap.cap_map())
    except Exception:
        cap_on = False
    seen, out = set(), []
    for s0 in symbols:
        key = (s0 or '').strip().lower()
        if not key or key in seen:
            continue
        if not in_universe(key):
            continue
        if cap_on and not market_cap.passes(key):
            continue
        seen.add(key)
        out.append(s0)
    return out


def describe() -> str:
    parts = ['仅沪深主板(600/601/603/605/000/001/002/003)' if _flag('universe_main_board_only')
             else '全市场(含创业/科创/北交所)',
             '剔除 ST/*ST/退市整理' if _flag('universe_exclude_st') else '含 ST']
    return '、'.join(parts)


def apply_to_frame(df, symbol_col: str = 'symbol'):
    """按当前股票范围过滤 DataFrame（symbol 列可以是裸码或带 sh/sz 前缀）。"""
    import pandas as pd
    codes = df[symbol_col].astype(str).str.lower().str.replace(r'^(sh|sz|bj)', '', regex=True)
    keep = pd.Series(True, index=df.index)
    if _flag('universe_main_board_only'):
        keep &= codes.str.match(MAIN_BOARD_RE)
    ex = excluded_symbols()
    if _flag('universe_exclude_st') and ex:
        keep &= ~codes.isin(ex)
    return df[keep]
