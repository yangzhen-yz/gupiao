"""candidate_pool.py - 盘前全市场海选候选池（2026-10-15）

为什么要有它
------------
改造前推荐池 = 东财**人气榜 444 只** + 固定底仓 28 只。人气榜是"散户此刻在搜什么"，
是滞后指标：一只票要先被散户搜到，才可能被系统看到。实测后果——

  九阳股份 002242：20 日 +103%、现价距历史高 -3.8%，
      不在热搜榜、不在趋势扫描、不在候选池 → 系统从头到尾没看过一眼。
  澳弘电子 605058：10-09 就被打分 72（当日第 3）、10-13 又 76（第 4），
      却因 Top3"概念分散"用一个只覆盖 55 只票的标签表去重而被挤掉，
      直到 10-15 涨停当天才成为推荐 —— 比它该被发现的时间晚了整整一个月。

本模块把入口从"热搜"换成"结构"：用本地日 K 库（**选股范围内 2462 只**：沪深主板 +
非ST/退 + 总市值≥35亿，盘后已同步）做一次向量化
海选，标准是欧奈尔趋势模板的 A 股短线版 —— 收盘 > MA20 > MA60、收盘距 60 日高点
8% 以内、当日上涨且量能不缩、20 日涨幅排进**当天选股范围**前 20%。

两个月的历史回放（2026-06-01 起）：每天选出 172 只（79~318），
九阳在 **2026-09-11** 命中（其后 +81.6%），澳弘在 **2026-09-14** 命中（其后 +74.9%）。

硬约束
------
  1. 只用本地库，不打任何外部接口 —— 盘前 9:00 是网络最脆的时间窗，不能把候选池
     挂在东方财富或腾讯上；
  2. **数据不新就不出池**：本地库最新交易日必须是"今天之前最近的交易日"，
     否则宁可用老的热搜池并大声告警。2026-10-14 那次 18:15 同步静默失败，
     如果当时有这条检查，前一晚就会被发现，而不是两周后查胜率才发现少一天；
  3. 只产出"进入评分的名单"，不改任何因子权重、不改 Top3 规则 —— 它是入口，不是判断。
"""
import os
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from app.config import (CANDIDATE_POOL_ENABLED, CANDIDATE_POOL_MAX, CANDIDATE_POOL_MIN_BARS,
                        CANDIDATE_POOL_RPS_TOP, CANDIDATE_POOL_NEAR_HIGH,
                        KLINE_HISTORY_DB_PATH, UNIVERSE_MAIN_BOARD_ONLY, KLINE_POOL_MIN_COVERAGE)
from db.database import get_db_conn
from services.data.trading_calendar import is_trading_day

_DDL = '''CREATE TABLE IF NOT EXISTS daily_candidate_pool (
    pool_date TEXT NOT NULL,
    symbol    TEXT NOT NULL,
    source    TEXT NOT NULL,
    r20       REAL,
    rank_pct  REAL,
    created_at TEXT,
    PRIMARY KEY (pool_date, symbol)
)'''


def _set(key: str, cfg_name: str, default: float) -> float:
    """配置项现读：覆盖表(app_settings) 优先，读不到退回 app/config.py 的默认值。

    2026-10-17：这些门槛以前是导入时绑定的常量，界面改了不生效；
    复盘子页"口径与开关"要能当场改当场见效，所以取用时才读。
    """
    try:
        from services.support import app_settings
        v = app_settings.value(key)
        return v if v is not None else default
    except Exception:
        import app.config as _cfg
        return float(getattr(_cfg, cfg_name, default))


def min_coverage() -> float:
    return float(_set('kline_pool_min_coverage', 'KLINE_POOL_MIN_COVERAGE', 0.9)) / 100.0


def pool_max() -> int:
    return int(_set('candidate_pool_max', 'CANDIDATE_POOL_MAX', 250))


def rps_top_pct() -> float:
    return float(_set('candidate_rps_top_pct', 'CANDIDATE_POOL_RPS_TOP', 80))


def near_high_ratio() -> float:
    return float(_set('candidate_near_high_pct', 'CANDIDATE_POOL_NEAR_HIGH', 0.92)) / 100.0


def _prefixed(code: str) -> str:
    """本地日K库用裸 6 位码，推荐链路用 sh/sz 前缀码。北交所一律排除
    （下游 `^(sh|sz)\\d{6}$` 的正则本来也不认 bj）。"""
    if code.startswith('6'):
        return 'sh' + code
    if code.startswith(('0', '3')):
        return 'sz' + code
    return ''


def _latest_bar_date() -> str:
    conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=30)
    try:
        return (conn.execute('SELECT MAX(date) FROM stock_daily').fetchone() or [''])[0] or ''
    finally:
        conn.close()


def _expected_trade_date(now: Optional[datetime] = None) -> str:
    """盘前海选应该用的数据日期 = 今天之前最近的交易日（盘前就是昨天）"""
    now = now or datetime.now()
    d = now.date()
    if now.hour < 15:      # 盘前/盘中：今天这根日线还没收
        d -= timedelta(days=1)
    for _ in range(12):
        try:
            if is_trading_day(d):
                return d.isoformat()
        except Exception:
            return d.isoformat()
        d -= timedelta(days=1)
    return ''


def _coverage_of(date_str: str) -> Tuple[int, int, float]:
    """某交易日的覆盖情况 → (当天有K线的可选股数, 选股范围总数, 比值)。

    分母是**选股范围**（主板 + 非ST/退 + 总市值≥下限），不是整库 5222。
    2026-10-16 用户原话："目前我不是增加了限制默认所有的选股条件：沪深主板、非ST和*ST、市值大于35亿"。
    用整库当分母有两个坏处：① 5222 里混着他永远买不了的创业板/科创/北证/ST，
    同一个"755 只"在两套分母下分别是 14% 和 31%，看日志的人判断不出严重程度；
    ② 同步清单收窄之后，整库分母只会让覆盖度永远上不了 90%，把正常状态报成故障。
    实测 2026-10-16：整库 5222 → 主板非ST 3051 → 市值≥35亿 **2462**，
    而 10-14/10-15 这两个完整交易日在 2462 口径下正好 100%（说明分母选得干净、没有退市股拖尾）。
    """
    try:
        conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=30)
        try:
            day = {r[0] for r in conn.execute(
                'SELECT DISTINCT symbol FROM stock_daily WHERE date=?', (date_str,)).fetchall()}
        finally:
            conn.close()
        from services.data import universe
        denom = set(universe.eligible_symbols())
        if not denom:
            return 0, 0, 0.0
        covered = len(day & denom)
        return covered, len(denom), covered / len(denom)
    except Exception:
        return 0, 0, 0.0


def pool_is_fresh() -> Dict:
    """本地日K是否新到能出池。返回 {'ok','latest','expected','coverage','covered','universe'}。

    2026-10-16 补上覆盖度这一条：只看 `MAX(date)` 会被**半截的一天**骗过去 ——
    盘后增量同步是逐只写入的（今晚 18:15 那次跑到一半时 10-16 在选股范围里只有 1109/2462 只），
    这时 `MAX(date)` 已经等于今天，`ok=True`，明早 9:00 就会拿"只有半天的市场"当全市场海选，
    池子会静默变薄（今天 59 只 vs 昨天 87 只这种差异，一半是范围收窄、一半可能就是这个）。
    覆盖度不够就当作数据不新：宁可用老的热搜池并大声告警，也不要悄悄用半截数据。
    """
    latest = _latest_bar_date()
    expected = _expected_trade_date()
    covered, universe_n, cov = _coverage_of(latest) if latest else (0, 0, 0.0)
    ok = bool(latest) and bool(expected) and latest >= expected and cov >= min_coverage()
    return {'ok': ok, 'latest': latest, 'expected': expected, 'coverage': round(cov, 3),
            'covered': covered, 'universe': universe_n}


def build_pool(now: Optional[datetime] = None, persist: bool = True) -> List[Dict]:
    """向量化海选全市场，产出当日候选池。失败/数据不新返回 []（调用方退回热搜池）。"""
    if not CANDIDATE_POOL_ENABLED:
        return []
    fresh = pool_is_fresh()
    if not fresh['ok']:
        _why = (f"最新 {fresh['latest'] or '空'} 落后于应有交易日 {fresh['expected']}"
                if not (fresh['latest'] and fresh['expected'] and fresh['latest'] >= fresh['expected'])
                else f"最新 {fresh['latest']} 在选股范围里只覆盖 "
                     f"{fresh['covered']}/{fresh['universe']} 只 = {fresh['coverage']*100:.1f}% "
                     f"（< {min_coverage()*100:.0f}%，同步像跑到一半）")
        print(f'[候选池] ⚠️ 本地日K不可用：{_why}，本次不出池（退回热搜池）。'
              f'请先补同步：python scripts/backfill_kline.py --mode sync')
        return []
    try:
        import pandas as pd
    except ImportError:
        print('[候选池] 无 pandas，跳过海选（退回热搜池）')
        return []

    try:
        conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=60)
        # 60 日高点 + MA60 + 20 日涨幅 + 5 日均量 → 至少需要 80 根；多取一些保证窗口完整
        since = (datetime.now() - timedelta(days=200)).strftime('%Y-%m-%d')
        df = pd.read_sql(
            'SELECT symbol, date, high, close, volume FROM stock_daily WHERE date >= ?',
            conn, params=(since,))
        conn.close()
        if df.empty:
            print('[候选池] 本地库区间内无数据')
            return []
        # 用户口径 2026-10-16：只做沪深主板、剔除 ST/*ST、市值≥35亿，再叠三条流动性/价位门槛
        # （近20日日均成交额>8000万、日均换手>1.5%、价>3元）。整层过滤必须排在百分位排名**之前** ——
        # "20 日涨幅排进当天前 20%" 如果连买不动的票也参与排名，池子就会被创业板/北交所的暴涨票
        # 和零换手的死票挤掉，等于用他能买的名额去看他买到也出不来的一半。
        from services.data import universe
        before = df['symbol'].nunique()
        scope = set(universe.selectable_symbols())      # 选票范围（≈1094 只，非数据范围 2462）
        df = df[df['symbol'].astype(str).str.lower().isin(scope)]
        n_scope = df['symbol'].nunique()
        print(f'[候选池] 股票范围 {universe.describe_selectable()}：{before} → {n_scope} 只'
              f'（数据范围 {universe.eligible_count()} 只，同步与覆盖率仍按它算）')
        if df.empty:
            print('[候选池] ⚠️ 过滤后一只都不剩（股票范围配置或名录缺失），本次不出池')
            return []
        df = df.sort_values(['symbol', 'date'])
        g = df.groupby('symbol', group_keys=False)
        df['ma20'] = g['close'].transform(lambda s: s.rolling(20).mean())
        df['ma60'] = g['close'].transform(lambda s: s.rolling(60).mean())
        df['hi60'] = g['high'].transform(lambda s: s.rolling(60).max())
        df['v5'] = g['volume'].transform(lambda s: s.rolling(5).mean())
        df['r20'] = g['close'].transform(lambda s: (s / s.shift(20) - 1) * 100)
        df['chg'] = g['close'].transform(lambda s: (s / s.shift(1) - 1) * 100)
        bars = g['close'].transform('count')
        d = df[df['date'] == fresh['latest']].copy()
        d = d[bars.reindex(d.index).fillna(CANDIDATE_POOL_MIN_BARS) >= CANDIDATE_POOL_MIN_BARS]
        full = df.dropna(subset=['ma60', 'hi60', 'v5', 'r20', 'chg']).copy()
        full['rank_pct'] = full.groupby('date')['r20'].rank(pct=True) * 100
        sel = full[full['date'] == fresh['latest']]
        n_base = len(sel)          # 当天指标齐全、进入海选的只数（= 选股范围内当天有K线的）
        sel = sel[(sel.close > sel.ma20) & (sel.ma20 > sel.ma60)
                  & (sel.close >= near_high_ratio() * sel.hi60)
                  & (sel.chg > 0) & (sel.volume >= sel.v5)
                  & (sel.rank_pct >= rps_top_pct())]
        sel = sel.sort_values('r20', ascending=False).head(pool_max())
        pool = []
        for row in sel.itertuples():
            sym = _prefixed(str(row.symbol))
            if not re.match(r'^(sh|sz)\d{6}$', sym):
                continue
            pool.append({'symbol': sym, 'source': 'pool',
                         'r20': round(float(row.r20), 2), 'rank_pct': round(float(row.rank_pct), 1)})
        print(f"[候选池] {fresh['latest']} 海选 {len(pool)} 只：选股范围 {n_scope} 只 → "
              f"当天指标齐全 {n_base} 只 → 过趋势/量能/RPS 阈值 {len(sel)} 只（上限 {pool_max()}）")
        if persist and pool:
            _save_pool(fresh['latest'], pool)
        return pool
    except Exception as e:
        print(f'[候选池] 海选异常（退回热搜池）: {type(e).__name__}: {e}')
        return []


def _save_pool(pool_date: str, pool: List[Dict]):
    conn = get_db_conn()
    try:
        conn.execute(_DDL)
        conn.execute('DELETE FROM daily_candidate_pool WHERE pool_date=?', (pool_date,))
        conn.executemany('INSERT OR REPLACE INTO daily_candidate_pool '
                         '(pool_date, symbol, source, r20, rank_pct, created_at) '
                         'VALUES (?,?,?,?,?,?)',
                        [(pool_date, p['symbol'], p['source'], p['r20'], p['rank_pct'],
                          datetime.now().strftime('%Y-%m-%d %H:%M:%S')) for p in pool])
        conn.commit()
    finally:
        conn.close()


def load_pool(limit: Optional[int] = None) -> List[Dict]:
    limit = limit or pool_max()      # 默认值不能写在签名里：那是导入时绑定的旧值
    """读最近一次海选结果（供 9:45/13:30/14:05 三次生成共用，避免重复计算）。

    只认"最近交易日"的池子：超过 5 个自然日没更新说明盘前任务又静默失败了，
    这种陈旧名单继续喂给推荐比不用更危险。
    """
    conn = get_db_conn()
    try:
        conn.execute(_DDL)
        latest = conn.execute('SELECT MAX(pool_date) FROM daily_candidate_pool').fetchone()[0]
        if not latest:
            return []
        try:
            age = (datetime.now() - datetime.strptime(latest, '%Y-%m-%d')).days
        except ValueError:
            return []
        if age > 5:
            print(f'[候选池] 最近一次海选是 {latest}（{age} 天前），视为过期不用')
            return []
        rows = conn.execute(
            'SELECT symbol, source, r20, rank_pct FROM daily_candidate_pool '
            'WHERE pool_date=? ORDER BY r20 DESC LIMIT ?', (latest, limit)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f'[候选池] 读取失败: {e}')
        return []
    finally:
        conn.close()
