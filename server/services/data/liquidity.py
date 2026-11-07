"""liquidity.py - 流动性/价位下限（2026-10-16 用户追加的三个选股条件）

用户原话：**"近 20 个交易日日均成交额大于 8000 万，近 20 个交易日平均换手率大于 1.5%，
股价大于 3 元"**，目的是"减少无效的筛选"。

为什么这三条该由一个模块统一算
------------------------------
它们不是打分项，是**入场资格**：成交额和换手不够的票，形态再好也只是账面收益 —— 挂单进不去、
出不来，回测里的收盘价成交在实盘要打折；3 元以下的票集中在被边缘化的老股/庄股，跳空与单日
异动概率明显更高（"大跳水"最集中的那一类）。所以它必须和主板/ST/市值一样，在**百分位排名之前**
生效，否则名额会被根本买不动的票挤掉。

实测（2026-10-16，2462 只沪深主板·非ST·市值≥35亿里）
---------------------------------------------------
  · 日均成交额 > 0.8 亿：1882 只
  · 日均换手率 > 1.5% ：1234 只  ← 真正收口的一条
  · 最新收盘 > 3 元  ：2340 只
  · **三条同时通过 ：1094 只（44.4%）**

口径细节（都是踩过坑之后定的）
------------------------------
  · 成交额取本地日K `turnover` 列，单位是**元**：baostock 的 `amount` 与腾讯收盘快照的
    `成交额(万元)×1e4` 两边一致（今晚已逐价校验过）；
  · 换手率分母用**流通股本**（= 流通市值 / 最新价），不用流通市值本身：窗口内涨过一波的票
    用市值当分母会把过去的换手算轻（603200 市值法 7.55% vs 股本法 8.84%）；
  · 窗口内**一根K线都没有**（长期停牌/已退市/新股还没数据）= 剔除，这种票今天根本买不到；
  · 拿不到流通股本（市值表缺这只）= 换手这一条**放行**（fail-open，与 market_cap 同一策略），
    否则每周一次的市值刷新一旦限流，选票会整片空掉。

用法：
  python -m services.data.liquidity --stats      # 逐条统计：哪一条在收口、砍掉它会放回多少只

三条各有独立开关（2026-10-16 用户要求改成独立开关）：`LIQ_AMOUNT_ENABLED` /
`LIQ_TURNOVER_ENABLED` / `LIQ_PRICE_ENABLED`，外加总开关 `LIQUIDITY_FILTER_ENABLED`。
实测每条单独开的收口效果（2462 只选票基础上）：只留成交额 1880 只、只留换手 1234 只、
只留价格 2340 只、三条全过 1094 只；关掉换手回到 1792 只、关掉成交额 1180 只、
关掉价格 1142 只；三条全关 = 2462 只（等于回到改造前，连"没K线就剔除"也一起关）。
"""
from __future__ import annotations

import sqlite3
import sys
import time
from typing import Dict, Iterable, List, Optional

from app import config as sys_cfg      # 退路用门面本体（不是星号拷贝），见 _flag/_num
from app.config import (KLINE_HISTORY_DB_PATH, LIQ_AMOUNT_ENABLED, LIQ_MIN_AVG_AMOUNT,
                        LIQ_MIN_AVG_TURNOVER_PCT, LIQ_MIN_PRICE, LIQ_PRICE_ENABLED,
                        LIQ_STATS_WINDOW, LIQ_TURNOVER_ENABLED, LIQUIDITY_FILTER_ENABLED)


def enabled() -> bool:
    """总开关开 **且** 至少一条子开关开，才算真的在过滤。

    三条子开关全关时一律放行 —— 包括"窗口内没K线就剔除"那条也一起关掉。
    这样"把开关关掉"确实等于回到加这三条之前，而不是留下一个看不见的剔除规则。

    2026-10-17 起走覆盖表热读（复盘子页"口径与开关"拨一下就生效），
    覆盖表读不到就退回 `app/config.py` 的默认值。
    """
    return (bool(_flag('liquidity_enabled', 'LIQUIDITY_FILTER_ENABLED'))
            and (_flag('liq_amount_enabled', 'LIQ_AMOUNT_ENABLED')
                 or _flag('liq_turnover_enabled', 'LIQ_TURNOVER_ENABLED')
                 or _flag('liq_price_enabled', 'LIQ_PRICE_ENABLED')))


def _flag(key: str, cfg_name: str) -> bool:
    """覆盖表优先、config 兜底。

    2026-11-07 修⑩-2：兜底那一行原来写的是 `getattr(sys_cfg, cfg_name)`，而 `sys_cfg`
    在这个模块里**从未定义过**（全仓 grep 只有这两处引用它）——意思是"覆盖表读不到"这个
    退路本身必炸：`app_settings.value()` 一旦抛异常（库被锁、app_settings 表还没建），
    这里抛的是 `NameError: name 'sys_cfg' is not defined`，而不是退回 config 默认值。
    成交额/换手/3 元这三条**入场资格**开关正好走这条路，等于在最需要降级的时候没降级。
    """
    try:
        from services.support import app_settings
        return bool(app_settings.value(key))
    except Exception:
        return bool(getattr(sys_cfg, cfg_name))


def _num(key: str, cfg_name: str) -> float:
    """取阈值并换算回 config 单位（覆盖表里 `value()` 给的是界面单位：万元 / % / 元）。

    退路同 `_flag`：以前也是 `getattr(sys_cfg, ...)` 的死引用，一并修掉。
    """
    try:
        from services.support import app_settings
        v = float(app_settings.value(key))
        s = app_settings.spec(key)
        if s.get('scale'):
            v = v * s['scale'] if s['scale'] != 100 else v / 100.0
        return v
    except Exception:
        return float(getattr(sys_cfg, cfg_name))


def invalidate_cache() -> None:
    """配置改动后让统计重算（阈值变了，通过名单就得重新算）。"""
    global _STATS_TS
    _STATS_TS = 0.0

_STATS: Optional[Dict[str, Dict[str, float]]] = None
_STATS_TS = 0.0
_STATS_TTL = 600.0      # 与 universe 的范围缓存同一节奏：一天只随行情变一次


def _pure(symbol: str) -> str:
    s = (symbol or '').strip().lower()
    return s[2:] if s[:2] in ('sh', 'sz', 'bj') else s


def _window_dates(conn, n: int) -> List[str]:
    return [r[0] for r in conn.execute(
        'SELECT DISTINCT date FROM stock_daily ORDER BY date DESC LIMIT ?', (n,)).fetchall()]


def last_stats_ts() -> float:
    """`_STATS` 最后一次重算完成的时刻（0.0 = 从未算过）。"""
    return _STATS_TS


def should_keepwarm_refresh(now: float, last_ts: float, max_age_sec: float,
                            force_flag: bool) -> bool:
    """保活循环这一轮要不要 force 重算流动性统计。抽成纯函数是为了能被自测直接跑，
    而不是在测试里另抄一遍条件表达式。

    `force_flag=True` 时无条件刷新——那是 2026-11-04 之前的行为，留作一键回滚。
    否则只在"距上次刷新超过 max_age_sec"时刷新：日K每天收盘后才变，正常路径由
    `job_sync_history` 同步完成后立刻刷，这个兜底负责"同步钩子没跑成"的日子。
    `last_ts=0`（进程刚起、从没算过）必然刷新。
    """
    if force_flag:
        return True
    if not last_ts:
        return True
    return (now - last_ts) >= max_age_sec


def stats_map(force: bool = False) -> Dict[str, Dict[str, float]]:
    """{裸码: {'amount': 日均成交额元, 'volume': 日均成交量股, 'price': 最新收盘}}"""
    global _STATS, _STATS_TS
    if _STATS is not None and not force and time.time() - _STATS_TS < _STATS_TTL:
        return _STATS
    out: Dict[str, Dict[str, float]] = {}
    try:
        conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=60)
        try:
            dates = _window_dates(conn, LIQ_STATS_WINDOW)
            if not dates:
                return {}
            ph = ','.join('?' * len(dates))
            # as_of = 该票在窗口内**自己**最后一根K线的日期。用它区分"这只票还在同步"
            # 和"早就被排除在同步范围外（数据停在几天前）"，后者不能当可买的票。
            for code, amt, vol, as_of in conn.execute(
                    f'SELECT symbol, AVG(turnover), AVG(volume), MAX(date) FROM stock_daily '
                    f'WHERE date IN ({ph}) GROUP BY symbol', dates):
                if code:
                    out[code] = {'amount': amt or 0.0, 'volume': vol or 0.0, 'price': 0.0,
                                 'as_of': as_of or ''}
            latest = dates[0]
            for code, close in conn.execute(
                    'SELECT symbol, close FROM stock_daily WHERE date=?', (latest,)):
                if code in out:
                    out[code]['price'] = close or 0.0
            # 流通股本 ≈ 流通市值 / 最新价（缺流通市值就用总市值兜底）
            try:
                rows = conn.execute(
                    'SELECT symbol, float_cap, total_cap FROM stock_market_cap').fetchall()
            except sqlite3.Error:
                rows = []
            caps = {r[0]: (r[1] or r[2]) for r in rows if r[0]}
            for code, d in out.items():
                base = caps.get(code)
                d['float_shares'] = (base / d['price']) if (base and d['price'] > 0) else None
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(f'[流动性] 读本地日K失败(本次不过滤): {e}')
        return {}
    _STATS, _STATS_TS = out, time.time()
    return out


def metrics(code: str) -> Optional[Dict[str, float]]:
    """单只票的三项实测值；窗口内没有K线返回 None（= 今天买不到）。"""
    d = stats_map().get(_pure(code))
    if not d or not d.get('price'):
        return None
    shares = d.get('float_shares')
    return {'amount': d['amount'],
            'price': d['price'],
            'turnover_pct': (d['volume'] / shares * 100) if shares else None}


def passes(code: str) -> bool:
    if not enabled():
        return True
    m = metrics(code)
    if m is None:
        return False                              # 窗口内没K线 = 停牌/退市/没数据，本来也买不到
    if _flag('liq_price_enabled', 'LIQ_PRICE_ENABLED') and m['price'] <= _num('liq_min_price', 'LIQ_MIN_PRICE'):
        return False
    if _flag('liq_amount_enabled', 'LIQ_AMOUNT_ENABLED') and m['amount'] <= _num('liq_min_avg_amount_wan', 'LIQ_MIN_AVG_AMOUNT'):
        return False
    if _flag('liq_turnover_enabled', 'LIQ_TURNOVER_ENABLED') and m['turnover_pct'] is not None \
            and m['turnover_pct'] <= _num('liq_min_avg_turnover_pct', 'LIQ_MIN_AVG_TURNOVER_PCT'):
        return False
    return True                                   # 换手算不出（市值表缺）→ fail-open 放行


def filter_codes(symbols: Iterable[str]) -> List[str]:
    """批量筛，返回裸 6 位码（排序）。"""
    if not enabled():
        return sorted({_pure(s) for s in symbols if s})
    return sorted({_pure(s) for s in symbols if s and passes(s)})


def filter_prefixed(symbols: Iterable[str]) -> list:
    """带 sh/sz 前缀的清单原样进出（顺序不变、去重），供推荐/扫描链路用。"""
    if not enabled():
        seen, out = set(), []
        for s0 in symbols:
            k = (s0 or '').strip().lower()
            if k and k not in seen:
                seen.add(k)
                out.append(s0)
        return out
    seen, out = set(), []
    for s0 in symbols:
        k = (s0 or '').strip().lower()
        if not k or k in seen:
            continue
        if passes(k):
            seen.add(k)
            out.append(s0)
    return out


def apply_to_frame(df, symbol_col: str = 'symbol'):
    """按流动性下限过滤 DataFrame（symbol 列裸码或带前缀均可）。未启用时原样返回。"""
    if not enabled() or df is None or df.empty:
        return df
    keep = df[symbol_col].astype(str).str.lower().map(lambda s: passes(s))
    return df[keep]


def count_in_scope(codes: Iterable[str]) -> int:
    return len([c for c in codes if passes(c)])


def describe() -> str:
    """只列**当前真的生效**的条件，免得日志写着三条、实际只跑了两条。"""
    if not enabled():
        return '流动性下限：未生效' if not LIQUIDITY_FILTER_ENABLED else '流动性下限：三条子开关全关'
    parts = []
    if _flag('liq_amount_enabled', 'LIQ_AMOUNT_ENABLED'):
        parts.append(f'近{LIQ_STATS_WINDOW}日日均成交额>{_num("liq_min_avg_amount_wan", "LIQ_MIN_AVG_AMOUNT") / 1e8:.1f}亿')
    if _flag('liq_turnover_enabled', 'LIQ_TURNOVER_ENABLED'):
        parts.append(f'日均换手率>{_num("liq_min_avg_turnover_pct", "LIQ_MIN_AVG_TURNOVER_PCT")}%')
    if _flag('liq_price_enabled', 'LIQ_PRICE_ENABLED'):
        parts.append(f'股价>{_num("liq_min_price", "LIQ_MIN_PRICE")}元')
    offs = [_flag('liq_amount_enabled', 'LIQ_AMOUNT_ENABLED'),
            _flag('liq_turnover_enabled', 'LIQ_TURNOVER_ENABLED'),
            _flag('liq_price_enabled', 'LIQ_PRICE_ENABLED')].count(False)
    return '、'.join(parts) + (f'（另有 {offs} 条已关）' if offs else '')


def stats() -> Dict:
    """自检：整库与"选股范围"分开报，并给出**逐条单独开**会剩多少只，
    方便直接判断哪一条在收口、砍掉它会放回多少票。"""
    m = stats_map()
    try:
        from services.data import universe
        scope = set(universe.eligible_symbols())
    except Exception as e:
        scope = set(m)
        print(f'[流动性] 读不到选股范围，按整库统计: {e}')

    min_price = _num('liq_min_price', 'LIQ_MIN_PRICE')
    min_amount = _num('liq_min_avg_amount_wan', 'LIQ_MIN_AVG_AMOUNT')
    min_turn = _num('liq_min_avg_turnover_pct', 'LIQ_MIN_AVG_TURNOVER_PCT')

    def _ok(d, amount=True, turnover=True, price=True):
        if not d or not d.get('price'):
            return False
        if price and d['price'] <= min_price:
            return False
        if amount and d['amount'] <= min_amount:
            return False
        shares = d.get('float_shares')
        if turnover and shares and d['volume'] / shares * 100 <= min_turn:
            return False
        return True

    in_scope = {c: m[c] for c in scope if c in m}
    out = {'窗口内有K线': len(m), '选股范围内': len(scope),
           '当前生效条件': describe(),
           '仅成交额这一条': sum(1 for d in in_scope.values() if _ok(d, True, False, False)),
           '仅换手这一条': sum(1 for d in in_scope.values() if _ok(d, False, True, False)),
           '仅价格这一条': sum(1 for d in in_scope.values() if _ok(d, False, False, True)),
           '三条全过': sum(1 for d in in_scope.values() if _ok(d))}
    return out


if __name__ == '__main__':
    import argparse
    import json
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser()
    ap.add_argument('--stats', action='store_true')
    ap.parse_args()
    print(describe())
    print(json.dumps(stats(), ensure_ascii=False, indent=1))
