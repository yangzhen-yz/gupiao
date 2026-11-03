"""hot_chains.py - 动态热门板块链（2026-10-31）

背景：行动台主题卡原先是三条人工固定链（机器人/商业航天/算力），不随主线切换。
本模块每天从**财联社概念板块榜**选出最热的前 3 个板块，按成分股强度挑核心 20~40 只，
交给 `theme_index` 用同一套算法算"近 3 日等权涨幅 + 温度 + 状态"，排序逻辑完全不变。

选板口径（可复现）：
  1. 来源：优先 `cls_plate_daily` 最近交易日（15:10 落库）；盘中或库里没有当天数据时
     直接读财联社实时接口（`cls_plate.load('concept')` 自身有 60s 缓存与熔断）。
  2. 过滤：宽概念黑名单（融资融券/昨日涨停/指数成分…）+ 统计类（业绩预增/高送转/增减持…）
     + **板块宽度 15~200 只**（上涨+下跌家数）。原因是实测 2026-10-30 按涨停数排序，
     前四名是业绩预增(357 只/24 涨停)、国企改革(1373/14)、食品饮料(230/12)、人工智能(932/9)——
     这类"什么都能装"的板块会把真正的主升稀释掉；固定链 39~40 只的粒度才可比。
  3. 排序：(涨停数, 涨幅) 降序；涨停数 < 2 的不进候选，不足 3 个时按涨幅补齐。

成分口径（核心股）：
  - 涨停 +100 分、财联社 is_core +20、成交额占比 ×30、主力净流入分位 ×15、当日涨幅 ×10（封顶 5）；
  - 先按前四项的粗分取 60 只做日线可用性校验，再按成交额占比精排取前 40；
  - 日线：主板成员读 `stock_daily`；创业板/科创板本地只到 10-15/10-16，
    用腾讯 qfq 补最近 40 个交易日并写 `theme_member_daily`（chain_id 作 theme），
    收盘后每天由 `theme_index.sync_today` 续写当日行情。
  - 可用成员不足 20 只的板块直接跳过、顺延下一个候选（`_aggregate` 需要 ≥20 只才出点）。

更新节奏：
  - 盘中 300s / 休市 1800s 后台刷新（线程，不阻塞行动台热路径）；
  - 15:12 / 15:47 调度强制刷新 + 补日线 + 落库 `theme_chain_daily`；
  - 重启先读库里的最近一次链，再后台刷新，接口抖动不会让卡片空白。

边界：只做展示，不进选股评分/门槛/推送；财联社不提供成交额，非主板成员的成交额是
"收盘×成交量"近似值（与固定链回填口径一致），只用于板块内部相对权重，不对外当成交额引用。
"""
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import requests

from app.config import KLINE_HISTORY_DB_PATH
from db.database import (
    get_db_conn,
    load_theme_chain_daily,
    save_theme_chain_daily,
    save_theme_member_daily,
)
from services.data.fundflow import _BROAD_CONCEPT_BLOCKLIST
from services.ops import cls_plate
from services.ops.cls_pool import _norm_symbol

# 回滚开关：置 False 即回到三条固定链（theme_index 会自动兜底），不需要改其它文件
ENABLED = True

TOP_N = 3                 # 每天最多展示 3 条动态链
MAX_MEMBERS = 40          # 每条链最终等权成员上限（与固定链 39~40 只对齐）
PRE_SCORE_POOL = 60       # 补日线前先按粗分截断，避免为一个 500 只的板块拉 500 次
MIN_USABLE = 20           # `_aggregate` 要求每日 ≥20 只才出点
MIN_HISTORY = 60          # 至少 60 根日线（温度分位需要 60 日窗口）
BREADTH_MIN = 15          # 板块宽度（上涨+下跌家数）下限
BREADTH_MAX = 200         # 上限：超过 200 只视为过宽概念
MIN_ZT = 2                # 至少 2 家涨停才算"热门"
OVERLAP_MAX = 0.6         # 与已选板块的成分重叠率上限（交集/较小集合），防嵌套板块凑数
MEMBER_FETCH_LIMIT = 500  # 财联社成分接口一次返回全部，这里只是防御性上限
BACKFILL_DAYS = 40        # 腾讯 qfq 补最近 40 个交易日
BACKFILL_CAP = 150        # 单次刷新最多补多少只（首跑保护）
REFRESH_LIVE = 300        # 盘中刷新间隔
REFRESH_IDLE = 1800       # 休市刷新间隔

# 统计/筹码类概念：不是题材主线，成分股之间没有产业关联
_EXTRA_BLOCK_SUBSTR = (
    '业绩', '预增', '预减', '预亏', '扭亏', '高送转', '送转', '分红', '摘帽',
    '增持', '减持', '回购', '解禁', '股权激励', '机构调研', '股权转让',
)

_QFQ_URL = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
_UA = {'User-Agent': 'Mozilla/5.0'}

_LOCK = threading.RLock()
_CHAINS: Dict[str, Dict] = {}
_META = {'built_at': 0.0, 'day': '', 'source': '', 'refreshing': False,
         'error': '', 'plates': [], 'next_try': 0.0}
_BACKFILL_FAIL: Dict[str, set] = {}     # day -> 已失败过的 symbol，避免反复重试
_REF_DAY_CACHE: Dict[str, tuple] = {}   # day -> (日线基准日, 取数时刻)；TTL 内不重查（max(date) 无索引，一次 4.7s）
REF_DAY_TTL_SEC = 1800              # 30 分钟重查一次，18:15 日K同步后当天内就能跟上
_REF_DAY_WARNED: set = set()            # 已提示过"日K未同步到当天"的归属日
_SKIP_LOGGED: set = set()               # (day, chain_id) -> 已打过"跳过"日志，避免每轮刷屏


# ---------- 基础工具 ----------
def _last_trade_day():
    from services.data.trading_calendar import is_trading_day
    d = datetime.now().date()
    for _ in range(30):
        if is_trading_day(d):
            return d
        d -= timedelta(days=1)
    return datetime.now().date()


def _live_phase() -> bool:
    try:
        from services.data.trading_calendar import get_session_phase
        return get_session_phase().get('phase') in ('auction', 'morning', 'lunch', 'mid', 'tail')
    except Exception:
        return False


def _prefixed(code: str) -> str:
    """纯代码 → sh/sz 前缀（与 theme_index 的成员口径一致）；北交所返回空。"""
    c = str(code or '').strip().lower()
    if len(c) == 8 and c[:2] in ('sh', 'sz', 'bj'):
        c = c[2:]
    if len(c) != 6 or not c.isdigit():
        return ''
    if c[0] == '6':
        return 'sh' + c
    if c[0] in ('0', '3'):
        return 'sz' + c
    return ''


def _limit_pct(sym: str) -> float:
    return 20.0 if sym[-6:].startswith(('300', '301', '688')) else 10.0


def _num(v, default=0.0) -> float:
    try:
        if v in (None, ''):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _overlap(a: set, b: set) -> float:
    """重叠率 = 交集 / 较小集合（10-30：白酒 39 只全在酿酒 62 只里 → 1.0）。"""
    if not a or not b:
        return 0.0
    return len(a & b) / float(min(len(a), len(b)))


def _pct_rank(values: List[float]) -> List[float]:
    """把一组数值换成 0~1 分位（同值取平均位次），给打分用。"""
    if not values:
        return []
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 / max(1, len(values) - 1) if len(values) > 1 else 0.5
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


# ---------- 选板 ----------
def _plate_ok(row: Dict) -> bool:
    name = str(row.get('name') or '').strip()
    if not name or name in _BROAD_CONCEPT_BLOCKLIST:
        return False
    upper = name.upper()
    if 'ST' in upper or '退' in name:
        return False
    if any(s in name for s in _EXTRA_BLOCK_SUBSTR):
        return False
    breadth = (_num(row.get('upCount')) + _num(row.get('downCount')))
    return BREADTH_MIN <= breadth <= BREADTH_MAX


def select_plates(rows: List[Dict], limit: int = TOP_N) -> List[Dict]:
    """(涨停数, 涨幅) 取前 N；不足则按涨幅补齐。纯函数，便于自测。"""
    cands = [r for r in rows if _plate_ok(r)]
    strong = [r for r in cands if _num(r.get('limitUpNum')) >= MIN_ZT]
    strong.sort(key=lambda r: (_num(r.get('limitUpNum')), _num(r.get('changePct'))),
                reverse=True)
    picked = list(strong[:limit])
    if len(picked) < limit:
        used = {r.get('code') for r in picked}
        rest = [r for r in cands if r.get('code') not in used]
        rest.sort(key=lambda r: (_num(r.get('changePct')), _num(r.get('limitUpNum'))),
                  reverse=True)
        picked += rest[:limit - len(picked)]
    return picked


def _load_plates_from_db(day: str) -> List[Dict]:
    try:
        conn = get_db_conn()
        rows = conn.execute(
            'SELECT code, name, change_pct, main_fund_diff, up_count, down_count, '
            'limit_up_num FROM cls_plate_daily WHERE trade_date = ? AND plate_type = ?',
            (day, 'concept')).fetchall()
        conn.close()
    except Exception as error:
        print(f'[动态链] 读 cls_plate_daily 失败: {error}')
        return []
    return [{
        'code': r['code'], 'name': r['name'],
        'changePct': r['change_pct'], 'mainFundDiff': r['main_fund_diff'],
        'upCount': r['up_count'], 'downCount': r['down_count'],
        'limitUpNum': r['limit_up_num'],
    } for r in rows]


def _plate_rows() -> Tuple[List[Dict], str, str]:
    """返回 (板块行, 归属交易日, 来源标签)。盘中优先实时接口，休市优先当日落库。"""
    day = _last_trade_day().isoformat()
    if not _live_phase():
        rows = _load_plates_from_db(day)
        if rows:
            return rows, day, 'db'
    try:
        return cls_plate.load('concept'), day, 'live'
    except Exception as error:
        rows = _load_plates_from_db(day)
        return rows, day, 'db-retry' if rows else f'error: {error}'


# ---------- 成分与日线 ----------
def _fetch_members(plate_code: str) -> List[Dict]:
    rows = cls_plate.members(plate_code, limit=MEMBER_FETCH_LIMIT)
    out = []
    for m in rows:
        sym = _prefixed(_norm_symbol(m.get('code') or ''))
        name = str(m.get('name') or '').strip()
        if not sym or not name or 'ST' in name.upper() or '退' in name:
            continue
        out.append({**m, 'symbol': sym, 'name': name})
    return out


def _crude_score(m: Dict, ff_rank: float, cmc_rank: float) -> float:
    chg = _num(m.get('changePercent'))
    score = 100.0 if chg >= _limit_pct(m['symbol']) - 0.3 else 0.0
    score += 20.0 if m.get('isCore') else 0.0
    score += ff_rank * 15.0 + cmc_rank * 5.0
    return score


def _history_stats(symbols: List[str]) -> Dict[str, Tuple[str, int]]:
    """{纯代码: (最新日期, 历史根数)}，合并本地日K与主题明细。"""
    out: Dict[str, Tuple[str, int]] = {}
    pure = sorted({s[-6:] for s in symbols})
    if not pure:
        return out
    marks = ','.join('?' * len(pure))
    try:
        conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=10)
        for sym, last, n in conn.execute(
                f'SELECT symbol, MAX(date), COUNT(1) FROM stock_daily '
                f'WHERE symbol IN ({marks}) GROUP BY symbol', pure).fetchall():
            out[sym] = (last or '', int(n or 0))
        conn.close()
    except Exception as error:
        print(f'[动态链] 读本地日K失败: {error}')
    # theme_member_daily 存的是带前缀的 symbol，单独查一次
    try:
        conn = get_db_conn()
        pref = sorted({f'sh{s}' if s[0] == '6' else f'sz{s}' for s in pure})
        pmarks = ','.join('?' * len(pref))
        for sym, last, n in conn.execute(
                f'SELECT symbol, MAX(date), COUNT(1) FROM theme_member_daily '
                f'WHERE symbol IN ({pmarks}) GROUP BY symbol', pref).fetchall():
            key = str(sym)[-6:]
            old = out.get(key)
            if not old or (last or '') > old[0]:
                out[key] = (last or '', int(n or 0))
        conn.close()
    except Exception as error:
        print(f'[动态链] 读主题明细失败: {error}')
    return out


def _fetch_qfq(sym: str, n: int = BACKFILL_DAYS) -> List[Dict]:
    """腾讯前复权日线（[date, open, close, high, low, volume]，升序）。"""
    url = f'{_QFQ_URL}?param={sym},day,,,{n},qfq'
    for _ in range(2):
        try:
            resp = requests.get(url, headers=_UA, timeout=8)
            resp.raise_for_status()
            node = ((resp.json().get('data') or {}).get(sym) or {})
            rows = node.get('qfqday') or node.get('day') or []
            if rows:
                return rows
        except Exception:
            time.sleep(0.2)
    return []


def _backfill(chain_id: str, symbol: str, name: str) -> bool:
    raw = _fetch_qfq(symbol)
    if not raw:
        return False
    rows, prev = [], None
    for r in raw:
        if not isinstance(r, list) or len(r) < 6:
            continue
        try:
            d, _o, close, high, low, volume = r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
        except (TypeError, ValueError):
            continue
        rows.append({
            'symbol': symbol, 'date': str(d)[:10], 'name': name,
            'open': _o or None, 'high': high or None, 'low': low or None,
            'close': close or None, 'prev_close': prev,
            # 腾讯 qfq 只有成交量；amount 取 close×volume（与固定链回填口径一致）
            'amount': (close * volume) if close and volume else None,
            'volume': volume or None, 'source': 'tencent_qfq',
        })
        prev = close or prev
    if len(rows) < 4:
        return False
    return bool(save_theme_member_daily(rows, chain_id))


def _skip_once(key, msg: str) -> None:
    """同一天同一链同一原因只打一次日志：盘中每 5 分钟一轮，刷屏会把真正的问题埋掉。"""
    if key in _SKIP_LOGGED:
        return
    _SKIP_LOGGED.add(key)
    print(msg)


def _hist_ref_day(day: str) -> str:
    """日线基准日 = min(归属交易日, 本地日K已完成的最新一天)。

    2026-11-02 修：原来直接要求 `最新bar >= day`，而盘中 `day` 就是今天——今天的日K
    要到 18:15 之后才同步进来，于是盘中每一轮每个成员都算"缺日线"，触发 150 只/链的
    qfq 回填，实测今天一天打了 2743 次 `web.ifzq.gtimg.cn`（失败 1980 次，72%），
    链还是建不出来，日志上百条"可用日线仅 N 只，跳过"。温度分位要的是 60 日窗口的
    历史，不是当天那一根，所以基准日退回"已完成的一天"即可。
    """
    cached = _REF_DAY_CACHE.get(day)
    if cached and (time.time() - cached[1]) < REF_DAY_TTL_SEC:
        return cached[0]
    ref = day
    try:
        conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=10)   # 只读查询，与本模块其它处一致
        try:
            last = conn.execute('select max(date) from stock_daily').fetchone()[0]
        finally:
            conn.close()
        if last and str(last) < day:
            ref = str(last)
            if day not in _REF_DAY_WARNED:
                _REF_DAY_WARNED.add(day)
                print(f'[动态链] 本地日K只到 {ref}（归属日 {day} 尚未同步），'
                      f'本轮起按 {ref} 判可用，不再重复回填')
        elif not last:
            print('[动态链] 本地日K为空，日线可用性判定退回归属日')
    except Exception as error:
        print(f'[动态链] 读本地日K最新日期失败: {str(error)[:60]}')
    _REF_DAY_CACHE[day] = (ref, time.time())
    return ref


def _ensure_history(chain_id: str, members: List[Dict], day: str) -> List[Dict]:
    """保证成员日线覆盖到基准日；返回可用成员（存在日线且历史 ≥ MIN_HISTORY）。"""
    ref = _hist_ref_day(day)
    stats = _history_stats([m['symbol'] for m in members])
    missing = [m for m in members
               if not stats.get(m['symbol'][-6:]) or stats[m['symbol'][-6:]][0] < ref]
    if missing:
        fails = _BACKFILL_FAIL.setdefault(day, set())
        todo = [m for m in missing if m['symbol'] not in fails][:BACKFILL_CAP]
        if todo:
            with ThreadPoolExecutor(max_workers=5) as pool:
                results = list(pool.map(
                    lambda m: (m, _backfill(chain_id, m['symbol'], m['name'])), todo))
            for m, ok in results:
                if not ok:
                    fails.add(m['symbol'])
            print(f'[动态链] {chain_id} 补日线 {sum(1 for _, ok in results if ok)}/{len(todo)} 只')
            stats = _history_stats([m['symbol'] for m in members])
    usable = []
    for m in members:
        st = stats.get(m['symbol'][-6:])
        if not st or st[0] < ref or st[1] < MIN_HISTORY:
            continue
        usable.append(m)
    return usable


def _latest_amounts(symbols: List[str], day: str) -> Dict[str, float]:
    """{纯代码: 当日成交额}，优先本地日K，缺的用主题明细（收盘×成交量近似）。"""
    out: Dict[str, float] = {}
    pure = sorted({s[-6:] for s in symbols})
    if not pure:
        return out
    marks = ','.join('?' * len(pure))
    try:
        conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=10)
        for sym, amt in conn.execute(
                f'SELECT symbol, turnover FROM stock_daily WHERE date = ? '
                f'AND symbol IN ({marks})', [day] + pure).fetchall():
            out[str(sym)] = float(amt or 0)
        conn.close()
    except Exception:
        pass
    try:
        conn = get_db_conn()
        pref = sorted({f'sh{s}' if s[0] == '6' else f'sz{s}' for s in pure})
        pmarks = ','.join('?' * len(pref))
        for sym, amt in conn.execute(
                f'SELECT symbol, MAX(amount) FROM theme_member_daily WHERE date = ? '
                f'AND symbol IN ({pmarks}) GROUP BY symbol', [day] + pref).fetchall():
            key = str(sym)[-6:]
            if not out.get(key):
                out[key] = float(amt or 0)
        conn.close()
    except Exception:
        pass
    return out


def _build_chain(plate: Dict, day: str, rank: int,
                 members: Optional[List[Dict]] = None) -> Optional[Dict]:
    chain_id = plate['code']
    if members is None:
        try:
            members = _fetch_members(chain_id)
        except Exception as error:
            print(f'[动态链] {chain_id} 成分拉取失败: {error}')
            return None
    if len(members) < MIN_USABLE:
        print(f'[动态链] {chain_id} {plate.get("name")} 成分仅 {len(members)} 只，跳过')
        return None

    ff = _pct_rank([_num(m.get('mainNetInflow')) for m in members])
    cmc = _pct_rank([_num(m.get('marketCap')) for m in members])
    for i, m in enumerate(members):
        m['_crude'] = _crude_score(m, ff[i], cmc[i])
    members.sort(key=lambda m: m['_crude'], reverse=True)
    pool = members[:PRE_SCORE_POOL]

    usable = _ensure_history(chain_id, pool, day)
    if len(usable) < MIN_USABLE:
        _skip_once((day, chain_id, 'hist'),
                   f'[动态链] {chain_id} {plate.get("name")} 可用日线仅 {len(usable)} 只，跳过'
                   f'（当日不再重试）')
        return None

    amounts = _latest_amounts([m['symbol'] for m in usable], day)
    total_amt = sum(amounts.values()) or 0.0
    ff2 = _pct_rank([_num(m.get('mainNetInflow')) for m in usable])
    for i, m in enumerate(usable):
        chg = _num(m.get('changePercent'))
        share = (amounts.get(m['symbol'][-6:], 0.0) / total_amt) if total_amt > 0 else 0.0
        m['_share'] = share
        m['_score'] = (
            (100.0 if chg >= _limit_pct(m['symbol']) - 0.3 else 0.0)
            + (20.0 if m.get('isCore') else 0.0)
            + share * 30.0
            + ff2[i] * 15.0
            + max(min(chg, 10.0), -10.0) * 0.5)
    usable.sort(key=lambda m: m['_score'], reverse=True)
    picked = usable[:MAX_MEMBERS]
    picked.sort(key=lambda m: m['_score'], reverse=True)

    chg = _num(plate.get('changePct'))
    zt = int(_num(plate.get('limitUpNum')))
    breadth = int(_num(plate.get('upCount')) + _num(plate.get('downCount')))
    note = (f'财联社概念「{plate.get("name")}」核心 {len(picked)} 只（{day} 涨停 {zt} 家、'
            f'板块 {chg:+.2f}%、宽度 {breadth} 只）；自动选链=当日涨停数/涨幅前 {TOP_N}，'
            f'非官方指数')
    return {
        'chain_id': chain_id,
        'id': chain_id,
        'rank': rank,
        'name': f'{plate.get("name")}链',
        'plateCode': chain_id,
        'plateName': plate.get('name'),
        'changePct': chg,
        'limitUpNum': zt,
        'breadth': breadth,
        'note': note,
        'source': 'cls',
        'day': day,
        'members': [[m['symbol'], m['name']] for m in picked],
        'core': [{'symbol': m['symbol'], 'name': m['name'],
                  'changePct': m.get('changePercent'),
                  'share': round(m['_share'], 4),
                  'isCore': bool(m.get('isCore'))} for m in picked[:10]],
    }


def _mark_fail(reason: str, retry_after: float = 120.0):
    """记一次失败并设退避，避免"接口挂了 → 每个请求都起线程再打一遍"。"""
    with _LOCK:
        _META['error'] = reason[:200]
        _META['next_try'] = time.time() + retry_after


def build(force: bool = False) -> Dict[str, Dict]:
    """同步重建三条动态链（调度与自测用）。任何异常都返回上一次的链。

    2026-10-31 实测补齐：财联社概念榜的头部常常是**互相嵌套**的板块
    （10-30 前三名为 白酒 39 只 / 酿酒 62 只 / 消费精选 39 只，白酒全部落在酿酒里），
    直接取前 3 会展示三条几乎同成分的链。所以候选扩到 TOP_N×3，按成分重叠率
    （交集 / 较小集合）≥ OVERLAP_MAX 视为同一热点跳过，保证 3 条链是 3 个不同主线；
    候选耗尽仍不足 3 条就如实少给，不拿嵌套板块凑数。
    """
    rows, day, source = _plate_rows()
    if not rows:
        print(f'[动态链] 板块榜不可用（{source}），保留上一次的链')
        _mark_fail(f'plate rows unavailable: {source}')
        with _LOCK:
            return dict(_CHAINS)
    plates = select_plates(rows, limit=TOP_N * 3)
    if not plates:
        print('[动态链] 没有满足宽度/涨停条件的板块，保留上一次的链')
        _mark_fail('no plate passed breadth/limit-up filter')
        with _LOCK:
            return dict(_CHAINS)
    chains: Dict[str, Dict] = {}
    picked_sets: List[set] = []
    for p in plates:
        if len(chains) >= TOP_N:
            break
        try:
            members = _fetch_members(p['code'])
        except Exception as error:
            print(f'[动态链] {p.get("code")} 成分拉取失败: {error}')
            continue
        codes = {m['symbol'][-6:] for m in members}
        if any(_overlap(codes, s) >= OVERLAP_MAX for s in picked_sets):
            print(f'[动态链] {p.get("name")} 与已选板块成分重叠 ≥{int(OVERLAP_MAX * 100)}%，跳过')
            continue
        cfg = _build_chain(p, day, len(chains) + 1, members)
        if cfg:
            chains[cfg['chain_id']] = cfg
            picked_sets.append(codes)
    if not chains:
        print('[动态链] 本轮没有可算的板块链，保留上一次的链')
        _mark_fail('no chain buildable this round')
        with _LOCK:
            return dict(_CHAINS)
    save_theme_chain_daily(day, list(chains.values()))
    with _LOCK:
        _CHAINS.clear()
        _CHAINS.update(chains)
        _META.update({'built_at': time.time(), 'day': day, 'source': source,
                      'error': '', 'next_try': 0.0, 'plates': [
                          {'code': p.get('code'), 'name': p.get('name'),
                           'changePct': p.get('changePct'),
                           'limitUpNum': p.get('limitUpNum')} for p in plates]})
    print(f'[动态链] 重建完成（{source}，{day}）：' +
          '、'.join(f'{c["name"]}({len(c["members"])}只)' for c in chains.values()))
    return dict(chains)


# ---------- 缓存与刷新 ----------
def _cfg_from_row(row: Dict) -> Optional[Dict]:
    members = [tuple(m) for m in (row.get('members') or []) if m and len(m) >= 2]
    if len(members) < MIN_USABLE:
        return None
    return {
        'chain_id': row.get('chain_id'), 'id': row.get('chain_id'),
        'rank': row.get('rank'), 'name': row.get('name'),
        'plateCode': row.get('plate_code'), 'plateName': row.get('plate_name'),
        'changePct': row.get('change_pct'), 'limitUpNum': row.get('limit_up_num'),
        'breadth': row.get('breadth'), 'note': row.get('note'),
        'source': 'cls', 'day': row.get('trade_date'), 'members': members,
    }


def _refresh_worker():
    try:
        build()
    except Exception as error:
        with _LOCK:
            _META['error'] = str(error)[:200]
        print(f'[动态链] 后台刷新失败: {error}')
    finally:
        with _LOCK:
            _META['refreshing'] = False


def _maybe_refresh(force: bool = False):
    ttl = REFRESH_LIVE if _live_phase() else REFRESH_IDLE
    with _LOCK:
        if _META['refreshing']:
            return False
        if not force:
            if _CHAINS and time.time() - _META['built_at'] < ttl:
                return False
            # 失败退避：CLS 挂掉时不要每个请求都起线程再打一遍（120s 内直接给旧链/固定链）
            if time.time() < float(_META.get('next_try') or 0):
                return False
        _META['refreshing'] = True
    threading.Thread(target=_refresh_worker, daemon=True, name='hot-chains').start()
    return True


def current() -> Dict[str, Dict]:
    """行动台热路径用：只读缓存；没有就尝试读库，再没有就后台跑一次。"""
    if not ENABLED:
        return {}
    with _LOCK:
        if _CHAINS:
            chains = dict(_CHAINS)
        else:
            chains = {}
    if chains:
        _maybe_refresh()
        return chains
    rows = load_theme_chain_daily()
    if rows:
        loaded = {}
        for r in rows:
            cfg = _cfg_from_row(r)
            if cfg:
                loaded[cfg['chain_id']] = cfg
        if loaded:
            with _LOCK:
                _CHAINS.update(loaded)
                _META.update({'day': rows[0].get('trade_date') or '',
                              'source': 'db-restart', 'built_at': 0.0})
            _maybe_refresh()
            return dict(loaded)
    # 空链：非 force 也会进来（内置失败退避，不会每个请求都打外部接口）
    _maybe_refresh()
    return {}


def refresh(force: bool = True) -> int:
    """同步刷新（15:12/15:47 调度与自测）。返回链数。"""
    if not ENABLED:
        return 0
    with _LOCK:
        if _META['refreshing'] and not force:
            return len(_CHAINS)
        _META['refreshing'] = True
    try:
        chains = build()
        return len(chains)
    finally:
        with _LOCK:
            _META['refreshing'] = False


def tick():
    """盘中定时 tick：到点就后台刷新一次（不做同步等待）。"""
    if ENABLED:
        _maybe_refresh()


def ids() -> List[str]:
    return list(current().keys())


def get(chain_id: str) -> Optional[Dict]:
    return current().get(chain_id)


def meta() -> Dict:
    with _LOCK:
        return {**_META, 'enabled': ENABLED, 'chains': list(_CHAINS.keys())}
