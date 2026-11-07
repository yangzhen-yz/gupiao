"""market_cap.py - 总市值下限过滤（2026-10-16，防量化操控小微盘）

用户口径：剔除总市值 < 35 亿的票。小盘股流通盘小、易被少量资金拉抬/砸盘做出形态，
把"被做出来"的 K 线当趋势信号很危险。落点覆盖全链路：
选票（候选池 / 智能推荐 / 趋势扫描 / 策略扫描）+ 同步K线 + 分时线。

数据来源的关键约束（2026-10-16 实测，别再去找批量接口了）：
  · 东财 clist 批量（push2/push2delay）、ulist.np 批量都不返回市值 —— f116/f117 恒为 '-'/0；
  · 只有单只 `push2 stock/get` 可靠返回 f116(总市值,元) / f117(流通市值,元) / f84(总股本)。
  · 2026-11-06 加备源：东财 push2 停调时改走新浪 `Market_Center.getHQNodeData?node=hs_a`
    （一页 100 只、实测 5578 只 / 56 页，带 mktcap/nmc，**单位是万元**，入库 ×1e4 换回元）。
    开关 MARKET_CAP_SINA_FALLBACK 关掉 = 完全回到"东财停调就整批跳过"的老行为。
  所以取数 = 按**可交易范围（主板 + 非ST/退）**的股票清单逐只拉，线程池并发，灌本地表
  `stock_market_cap`（放 history_kline.db，与 stock_basic_full 同库；盘前 9:00 候选池只读本地表、不外联，
  守住候选池"网络脆弱窗口不挂外部接口"的硬约束）。
  本地库全量约 5222（含创业板/科创/北证/ST），按 universe 口径刨完只剩约 3000 ——
  创业板/北证/ST 本就被 universe 排除、永不进选票，拉它们的市值是纯浪费，故只拉主板+非ST。

过滤策略（fail-open，避免刷表前把选票/同步全锁死）：
  · 开关关 或 表为空            → 不过滤（等同改造前行为）；
  · 表里有该票 且 cap<下限      → 剔除；
  · 表里没有该票（新股/漏刷/f116=0）→ 放行。刷新覆盖本地库全清单，实际≈全覆盖。

用法：
  python market_cap.py --refresh     # 按本地库清单灌表/刷新
  python market_cap.py --stats       # 看表里多少只、多少只在下限之下
"""
from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
import urllib.request
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Iterable, List, Optional

from app.config import (KLINE_HISTORY_DB_PATH, MARKET_CAP_AGE_REFRESH,
                        MARKET_CAP_FILTER_ENABLED, MARKET_CAP_MAX_PER_RUN,
                        MARKET_CAP_REFRESH_AGE_DAYS, MARKET_CAP_REQUEST_DELAY,
                        MARKET_CAP_WORKERS, MIN_TOTAL_MARKET_CAP)
from datetime import datetime, timedelta

_UT = 'b2884a393a59ad64002292a3e90d46a5'
_DDL = '''CREATE TABLE IF NOT EXISTS stock_market_cap (
    symbol       TEXT PRIMARY KEY,
    name         TEXT,
    total_cap    REAL,
    float_cap    REAL,
    total_shares REAL,
    updated_date TEXT
)'''

# 进程内缓存：市值一周才刷一次，没必要每次过滤都查库；refresh 后置 None 失效
_CAP_CACHE: Optional[Dict[str, float]] = None


def enabled_flag() -> bool:
    """总市值下限这一层是否生效（2026-10-17 起走覆盖表热读，读不到退回 config 默认值）。"""
    try:
        from services.support import app_settings
        return bool(app_settings.value('market_cap_enabled'))
    except Exception:
        return bool(MARKET_CAP_FILTER_ENABLED)


def min_cap() -> float:
    try:
        from services.support import app_settings
        return float(app_settings.value('min_market_cap_yi')) * 1e8    # 界面单位是亿元
    except Exception:
        return float(MIN_TOTAL_MARKET_CAP)


def invalidate_cache() -> None:
    """覆盖表变了要让下限判断重来；市值表本身不必重拉，这里只清"是否生效"的短路。"""
    global _CAP_CACHE
    _CAP_CACHE = None if _CAP_CACHE is not None else None


def _pure(symbol: str) -> str:
    s = (symbol or '').strip().lower()
    return s[2:] if s[:2] in ('sh', 'sz', 'bj') else s


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(KLINE_HISTORY_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=10000')
    conn.execute(_DDL)
    return conn


def _secid(code6: str) -> Optional[str]:
    """裸 6 位码 → 东财 secid。只认沪深（6→沪 / 0,3→深），北交所 4/8 一律不取（universe 本就不要）。"""
    if code6.startswith('6'):
        return '1.' + code6
    if code6.startswith(('0', '3')):
        return '0.' + code6
    return None


def _fetch_one(code6: str, retries: int = 2) -> Optional[tuple]:
    """单只拉市值。返回 (code, name, total_cap, float_cap, total_shares)；拿不到返回 None。

    2026-11-06 接入共享熔断（scope 'push2'，域名 push2.eastmoney.com）：此前这里只有
    "连续失败 15 次退避 3s"的自救，整轮 2400+ 只照样打出去 —— 封禁日等于替 IP 续封。
    闸门判定放在 `refresh()` 入口（一轮问一次，不在 2400 次循环里问），本函数只负责把
    真实发生的成功/失败报给熔断器：连接层异常由 guard.classify 分成 blocked 计连击，
    停牌/指数那种 f116=0 属"取到但没数"，不算失败、也不报。
    """
    from services.data import eastmoney_guard
    secid = _secid(code6)
    if not secid:
        return None
    time.sleep(MARKET_CAP_REQUEST_DELAY)      # pacing：低并发+小睡，避开东财限流
    url = (f'https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&ut={_UT}'
           f'&fields=f57,f58,f116,f117,f84')
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0', 'Referer': 'https://quote.eastmoney.com/'})
            data = (__import__('json').loads(urllib.request.urlopen(req, timeout=10).read()
                                             .decode('utf-8', 'replace')) or {}).get('data') or {}
            eastmoney_guard.ok('push2', clear_domain=False)
            cap = data.get('f116')
            try:
                cap = float(cap)
            except (TypeError, ValueError):
                cap = 0.0
            if cap <= 0:                    # 停牌/指数/异常：f116 为空或 0，视为未知，不落库
                return None
            def _f(k):
                try:
                    return float(data.get(k))
                except (TypeError, ValueError):
                    return None
            return (code6, str(data.get('f58') or ''), cap, _f('f117'), _f('f84'))
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(1.0 + attempt)       # 重试退避
    eastmoney_guard.fail('push2', last_err)
    return None


def _local_symbols() -> List[str]:
    """本地日K库里的全部裸码（≈ 全市场，含创业板/科创/北证/ST）。"""
    conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=30)
    try:
        return [r[0] for r in conn.execute(
            'SELECT DISTINCT symbol FROM stock_daily').fetchall() if r[0]]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _universe_symbols() -> List[str]:
    """只拉"可交易范围"内的市值：主板 + 非ST/退（universe 口径，约 3000 只）。
    创业板/科创/北证/ST 本就被 universe 排除、永不进选票，不拉它们的市值。"""
    from services.data import universe
    return list(universe.filter_symbols(_local_symbols()))


# ========== 新浪市值备源（2026-11-06） ==========
# 为什么要它：`refresh()` 里东财 push2 一旦进入冷却/当日停调，原来整批 2400+ 只直接跳过，
# 库里市值就停在上一版 —— 新股永远补不上、停牌复牌的市值长期陈旧，而市值下限是全链路的
# 第一道闸（候选池/智能推荐/趋势扫描/策略扫描都读），陈旧会直接放错行。
# 实测（2026-11-06 22:4x，本机直连）：`Market_Center.getHQNodeData?node=hs_a` 一页 num=100
# 生效、`getHQNodeStockCount?node=hs_a` 回 5578，字段 mktcap/nmc 齐全；安徽凤凰(920000)
# mktcap=103048.32 对应约 10.3 亿 → **单位是万元**，东财 f116/f117 是元，差 1e4 倍。
_SINA_LIST_URL = ('https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php'
                  '/Market_Center.getHQNodeData')
_SINA_COUNT_URL = ('https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php'
                   '/Market_Center.getHQNodeStockCount')
_SINA_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                               '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                 'Referer': 'https://vip.stock.finance.sina.com.cn/',
                 'Accept': '*/*'}
_SINA_NO_PROXY = {'http': None, 'https': None}   # 数据源都在国内，走代理反而易断（同 fundflow）
_SINA_PAGE_SIZE = 100            # 实测单页上限 100（与资金流排行接口同一限制）
_SINA_MAX_PAGES = 70             # 5578 只 / 100 = 56 页，留余量
_SINA_PAGE_WORKERS = 4           # 与 fundflow 的新浪并发保持一致
_SINA_WAN_TO_YUAN = 1e4          # 万元 → 元
_SINA_MAX_FAIL_PAGES = 6         # 连续备源失败到此数就停止翻页（见 refresh_via_sina 的说明）

# 本进程最近一次走新浪的结果（观测/自测用；不影响选票）
LAST_SINA_RUN: Dict[str, object] = {}


def sina_fallback_enabled() -> bool:
    try:
        from app.config import MARKET_CAP_SINA_FALLBACK
        return bool(MARKET_CAP_SINA_FALLBACK)
    except Exception:
        return False


def _wan(v) -> Optional[float]:
    """新浪的万元 → 元；非数值/空回 None（'-' 与 false 都算拿不到）。"""
    if v is None or v is False or v == '-' or v == '':
        return None
    try:
        return float(v) * _SINA_WAN_TO_YUAN
    except (TypeError, ValueError):
        return None


def _sina_page(page: int, retries: int = 2) -> Optional[List[Dict]]:
    """单页（100 只）。返回 None = 这一页失败（区别于"这页真的没数据"的 []）。"""
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(_SINA_LIST_URL,
                             params={'page': page, 'num': _SINA_PAGE_SIZE,
                                     'sort': 'symbol', 'asc': 1, 'node': 'hs_a'},
                             headers=_SINA_HEADERS, timeout=10, proxies=_SINA_NO_PROXY)
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(1.0 + attempt)
    print(f'[市值-新浪] 第 {page} 页失败（重试 {retries} 次）: {last_err}')
    return None


def _sina_total_pages() -> int:
    """先问总数换算页数；问不到就按上限翻（末页为空是正常的，不算失败）。"""
    try:
        r = requests.get(_SINA_COUNT_URL, params={'node': 'hs_a'}, headers=_SINA_HEADERS,
                         timeout=10, proxies=_SINA_NO_PROXY)
        n = int(str(r.text).strip().strip('"').replace(',', '') or 0)
        if n > 0:
            return min(_SINA_MAX_PAGES, (n + _SINA_PAGE_SIZE - 1) // _SINA_PAGE_SIZE + 1)
    except Exception as e:
        print(f'[市值-新浪] 总数查询失败，按上限翻 {_SINA_MAX_PAGES} 页: {e}')
    return _SINA_MAX_PAGES


def _load_shares_map() -> Dict[str, float]:
    """表里已有的总股本（东财 f84 刷进来的）。新浪没有这列，回填时要带着走，
    否则 INSERT OR REPLACE 会把 total_shares 抹成 NULL（pool_backtest 靠它算市值）。"""
    conn = _conn()
    try:
        return {r[0]: r[1] for r in conn.execute(
            'SELECT symbol, total_shares FROM stock_market_cap '
            'WHERE total_shares IS NOT NULL').fetchall()}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def refresh_via_sina() -> Dict:
    """东财停调时把市值表整表补一遍（单位已从万元换成元，与东财同口径）。

    三条硬约束，改这里之前先读：
      · **必须 ×1e4**：不换算的话 35 亿的下限会被当成 35 万，全市场误剔除；
      · **total_shares 不能被抹**：新浪无此字段，先 `_load_shares_map()` 读旧值再写回；
      · **只 REPLACE 不 DELETE**：拿不到的票沿用库里旧值，宁旧不空。
    """
    global LAST_SINA_RUN, _CAP_CACHE
    pages = _sina_total_pages()
    raw: List[Dict] = []
    ok_pages, skipped = 0, 0
    st = {'bad': 0}
    lock = threading.Lock()
    abort = threading.Event()
    _SKIP = object()

    def _guarded(p):
        """翻页包装：备源自己连续失败到阈值就停止翻页。
        理由和 fundflow 那条教训一样 —— 备源也被刷爆过（逐票 1094 次/轮把新浪备源自己
        搞到关闭），所以"备源不通"绝不能变成"把备源打到更不通"，停下来沿用库里旧值。"""
        if abort.is_set():
            return _SKIP
        got = _sina_page(p)
        if got is None:
            with lock:
                st['bad'] += 1
                bad_now = st['bad']
            if bad_now >= _SINA_MAX_FAIL_PAGES:
                abort.set()
                print(f'[市值-新浪] 累计失败 {bad_now} 页 → 停止翻页'
                      f'（备源自身异常，继续打只是给新浪攒封禁；库里旧市值原样沿用）')
        return got

    with ThreadPoolExecutor(max_workers=_SINA_PAGE_WORKERS) as ex:
        futs = [ex.submit(_guarded, p) for p in range(1, pages + 1)]
        for fut in as_completed(futs):
            got = fut.result()
            if got is _SKIP:
                skipped += 1
            elif got is None:
                pass                       # 已在 _guarded 里计过失败页
            else:
                ok_pages += 1
                raw.extend(got)
    bad_pages = st['bad']
    shares = _load_shares_map()
    today = datetime.now().strftime('%Y-%m-%d')
    out = []
    for it in raw:
        code = _pure(str(it.get('symbol') or ''))
        if len(code) != 6 or not code.isdigit():
            continue
        total = _wan(it.get('mktcap'))
        if not total or total <= 0:
            continue                       # 与东财口径一致：cap<=0 视为未知，不落库
        flt = _wan(it.get('nmc'))
        out.append((code, str(it.get('name') or ''), total,
                    flt if flt and flt > 0 else None, shares.get(code), today))
    if not out:
        LAST_SINA_RUN = {'ok': 0, 'pages': pages, 'okPages': ok_pages, 'badPages': bad_pages}
        print(f'[市值-新浪] 一页可用数据都没拿到（{pages} 页里失败 {bad_pages}），'
              f'库里市值原样不动')
        return {'ok': 0, 'fail': bad_pages, 'skip': 0, 'total': pages, 'source': 'sina'}
    conn = _conn()
    try:
        conn.executemany(
            'INSERT OR REPLACE INTO stock_market_cap '
            '(symbol,name,total_cap,float_cap,total_shares,updated_date) VALUES (?,?,?,?,?,?)',
            out)
        conn.commit()
    finally:
        conn.close()
    _CAP_CACHE = None
    LAST_SINA_RUN = {'ok': len(out), 'pages': pages, 'okPages': ok_pages,
                     'badPages': bad_pages, 'at': today}
    print(f'[市值-新浪] 入库 {len(out)} 只（应翻 {pages} 页：成功 {ok_pages}、失败 {bad_pages}）'
          f'；单位已从万元换算为元，股本沿用库里旧值（{len(shares)} 只带 f84）')
    return {'ok': len(out), 'fail': bad_pages, 'skip': 0, 'total': pages, 'source': 'sina'}


def refresh(symbols: Optional[Iterable[str]] = None,
            workers: int = MARKET_CAP_WORKERS, only_missing: Optional[bool] = None,
            max_age_days: Optional[int] = None,
            max_per_run: Optional[int] = None) -> Dict:
    """按清单逐只拉市值灌表。symbols 为空则用「主板+非ST」清单（universe 口径，约3000只）。

    **待补口径（2026-11-07 改，修③）**：默认不再是"只补缺失"。
      · 老默认 `only_missing=True` 只把"表里没有的票"当待补，从不管表里的数多旧 ⇒
        10-16 灌满 3052 只之后，每周日那一轮都走"目标清单已全部在表，无需刷新"返回 0，
        市值表从此冻结（实测 `updated_date`：3052 行停在 2026-10-16、2 行 11-01）。
        市值＝股本×当天价，价格一动就把票推过/推回 35 亿这条线，用它做全链路第一道闸
        等于一边剔掉"已涨回范围内"的、一边放行"已跌破下限"的。
      · 现在默认按 `MARKET_CAP_REFRESH_AGE_DAYS`（7 天）挑"缺失或超龄"，并按
        `MARKET_CAP_MAX_PER_RUN`（1600）截断、**最旧优先**，剩下的下一轮继续；
        开关 `MARKET_CAP_AGE_REFRESH=False` 时默认回到老的"只补缺失"。
      · 显式传参一律保持老语义：`only_missing=True` 只看缺失、`only_missing=False` 全表重拉；
        年龄口径只在**默认调用**（不传 only_missing/max_age_days，即 scheduler 与 `--refresh`）
        生效，想显式启用就传 `max_age_days=N`。调度器与手动 `--refresh` 都走默认口径。

    2026-10-16 实测教训：12 线程猛拉单只 stock/get，约 1100 次后被东财限流，之后 1900 只全失败
    （顺序慢拉却能 12/12 成功 → 是限流不是数据问题）。故：① 默认只补缺失（resumable，重跑接着填）；
    ② 4 线程 + 每请求前小睡；③ 连续失败自适应退避。周刷新即便被限流，下次会继续，不会丢进度。
    """
    global _CAP_CACHE
    # 只有**默认调用**（scheduler / --refresh，即 only_missing 与 max_age_days 都不传）才走
    # 新的"缺失或超龄"口径；显式传 True/False 一律保持老语义（True＝只补缺失、
    # False＝全表重拉）——向后兼容优先，不能让一个既有参数换了含义。
    age_mode = (only_missing is None and max_age_days is None
                and bool(MARKET_CAP_AGE_REFRESH))
    if only_missing is None:
        only_missing = not MARKET_CAP_AGE_REFRESH
    if max_age_days is None:
        max_age_days = MARKET_CAP_REFRESH_AGE_DAYS if age_mode else 0
    if max_per_run is None:
        max_per_run = MARKET_CAP_MAX_PER_RUN if age_mode else 0
    codes = [_pure(s) for s in (symbols if symbols is not None else _universe_symbols())]
    codes = [c for c in codes if len(c) == 6 and c.isdigit()]
    if not codes:
        return {'ok': 0, 'fail': 0, 'skip': 0, 'total': 0}
    todo = codes
    if only_missing:
        have = set(_load_cap_map().keys())
        todo = [c for c in codes if c not in have]
    elif max_age_days > 0:
        upd = _load_updated_map()
        cutoff = (datetime.now() - timedelta(days=max_age_days)).strftime('%Y-%m-%d')
        overdue = [c for c in codes if (upd.get(c) or '') < cutoff]
        print(f'[市值刷新] 待补口径＝缺失或超龄（>{max_age_days} 天）：'
              f'{len(overdue)}/{len(codes)} 只（库里最新一批 updated_date={max(upd.values()) if upd else "无"}）')
        if max_per_run and len(overdue) > max_per_run:
            overdue.sort(key=lambda c: upd.get(c) or '0000-00-00')   # 最旧优先；新股无日期排最前
            print(f'[市值刷新] 单轮上限 {max_per_run} 只，本轮拉最旧的这些，'
                  f'其余 {len(overdue) - max_per_run} 只下一轮继续')
            todo = overdue[:max_per_run]
        else:
            todo = overdue
    if not todo:
        print('[市值刷新] 目标清单已全部在表且不超龄，无需刷新')
        _CAP_CACHE = None
        return {'ok': 0, 'fail': 0, 'skip': 0, 'total': len(codes)}
    # 2026-11-06：闸门在入口问一次（原来一轮 2400+ 只全部裸打，封禁日等于替 IP 续封）
    from services.data import eastmoney_guard
    if eastmoney_guard.skip('push2'):
        if sina_fallback_enabled():
            print(f'[市值刷新] 东财 push2 在冷却/当日停调中 → 改走新浪市值备源'
                  f'（本轮待补 {len(todo)} 只；新浪是按 node 全表刷，不挑清单）')
            return refresh_via_sina()
        print(f'[市值刷新] 东财 push2 在冷却/当日停调中，本轮整批跳过'
              f'（待补 {len(todo)} 只，库里旧市值原样不动，下次周日/手动再补）')
        return {'ok': 0, 'fail': 0, 'skip': len(todo), 'total': len(codes),
                'reason': '东财闸门关闭中'}
    rows, fail, done, consec_fail = [], 0, 0, 0
    aborted = None
    _BATCH = 25                    # 一批 25 只：批与批之间复查闸门（见下面注释）
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for _start in range(0, len(todo), _BATCH):
            # 2026-11-07 补的一课：闸门必须**在批量循环里复查**，只在入口问一次等于没问。
            # 11-06 给这里接熔断时只在 `refresh()` 入口 `skip('push2')` 判了一次，
            # 于是今天凌晨那一轮在东财真在封的状态下连打 **1244 次全失败**
            # （状态文件里 push2 家族 blocked 连击 1244、全域随之开闸到当日 24:00）——
            # 一只都没刷到，还替被封的 IP 续了命。
            # 为什么不一次 submit 全部再 cancel：那样"已排队的任务"能不能被取消取决于
            # 线程池当时的手速，测出来飘（自测第 [11] 组第一版就是这么飘的）；
            # 改成按 25 只一批、批前复查，最坏只多打一批，行为可预期也可断言。
            if _start and eastmoney_guard.skip('push2'):
                aborted = (f'第 {done} 只后闸门已关（东财 push2 停调中），'
                           f'其余 {len(todo) - _start} 只本轮放弃')
                break
            futs = {ex.submit(_fetch_one, c): c for c in todo[_start:_start + _BATCH]}
            for fut in as_completed(futs):
                done += 1
                r = fut.result()
                if r is None:
                    fail += 1
                    consec_fail += 1
                else:
                    rows.append(r)
                    consec_fail = 0
                if consec_fail >= 15:      # 连续失败=大概率被限流，退避让 IP 喘口气
                    print(f'[市值刷新] 连续失败 {consec_fail} 次，退避 3s（疑似限流）')
                    time.sleep(3.0)
                    consec_fail = 0
            if done % 300 < _BATCH:
                print(f'[市值刷新] 进度 {done}/{len(todo)}，新入库 {len(rows)}，失败 {fail}')
    today = datetime.now().strftime('%Y-%m-%d')
    conn = _conn()
    try:
        conn.executemany(
            'INSERT OR REPLACE INTO stock_market_cap '
            '(symbol,name,total_cap,float_cap,total_shares,updated_date) VALUES (?,?,?,?,?,?)',
            [(c, n, cap, fcap, sh, today) for (c, n, cap, fcap, sh) in rows])
        conn.commit()
    finally:
        conn.close()
    _CAP_CACHE = None                      # 失效进程内缓存
    if aborted:
        print(f'[市值刷新] 提前收手：{aborted}；已拿到的 {len(rows)} 只照常入库，'
              f'没拿到的沿用库里旧值（宁旧不空），闸门解除后下一轮继续')
        return {'ok': len(rows), 'fail': fail, 'skip': len(todo) - done,
                'total': len(todo), 'reason': aborted}
    print(f'[市值刷新] 完成：新入库 {len(rows)}，未取到(停牌/指数/限流) {fail}，本次清单 {len(todo)}')
    return {'ok': len(rows), 'fail': fail, 'skip': 0, 'total': len(todo)}


def _load_updated_map() -> Dict[str, str]:
    """symbol -> updated_date（'YYYY-MM-DD'）。年龄口径靠它挑"超龄"的票。

    日期用字符串直接比是安全的：库里写的是 `datetime.strftime('%Y-%m-%d')`，
    字典序＝时间序；`max()` 用来在日志里报"最新一批是哪天"（冻结排查时就靠这一眼）。
    """
    conn = _conn()
    try:
        return {r[0]: (r[1] or '') for r in conn.execute(
            'SELECT symbol, updated_date FROM stock_market_cap').fetchall()}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _load_cap_map() -> Dict[str, float]:
    conn = _conn()
    try:
        return {r[0]: r[1] for r in conn.execute(
            'SELECT symbol, total_cap FROM stock_market_cap '
            'WHERE total_cap IS NOT NULL').fetchall()}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


# 11-04 观测用：本进程第一次装载市值表花了多久、装了多少行（启动那 190~751 秒定位用）
LAST_LOAD_SEC = 0.0
LAST_LOAD_ROWS = 0


def cap_map() -> Dict[str, float]:
    global _CAP_CACHE, LAST_LOAD_SEC, LAST_LOAD_ROWS
    if _CAP_CACHE is None:
        import time as _time
        _t = _time.time()
        _CAP_CACHE = _load_cap_map()
        LAST_LOAD_SEC = round(_time.time() - _t, 3)
        LAST_LOAD_ROWS = len(_CAP_CACHE)
    return _CAP_CACHE


def passes(symbol: str) -> bool:
    """该票是否过市值下限。未知/未启用一律放行（fail-open）。"""
    if not enabled_flag():
        return True
    cap = cap_map().get(_pure(symbol))
    if cap is None:
        return True
    return cap >= min_cap()


def filter_symbols(symbols: Iterable[str]) -> List:
    """批量筛，保留原始写法（带不帶 sh/sz 前缀原样返回）与顺序，去重。"""
    seen, out = set(), []
    for s0 in symbols:
        key = (s0 or '').strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        if passes(key):
            out.append(s0)
    return out


def apply_to_frame(df, symbol_col: str = 'symbol'):
    """按市值下限过滤 DataFrame（symbol 列裸码/带前缀均可）。未启用时原样返回。"""
    if not enabled_flag() or df is None or df.empty:
        return df
    cmap = cap_map()
    if not cmap:
        return df
    caps = df[symbol_col].astype(str).str.lower().map(
        lambda s: cmap.get(_pure(s)))
    keep = caps.isna() | (caps >= min_cap())   # 未知放行
    return df[keep]


def count_below() -> int:
    """下限之下的只数——**只数选股范围内的那部分**（2026-11-07 收口径）。

    起因：新浪备源是按 `node=hs_a` 全市场刷的，11-07 凌晨一次接管把表从 3054 行灌到
    5578 行（含创业板/科创/北证/ST）。这些票本来就被 universe 排除、永不进选票，
    把它们算进"已排除 N 只小市值"会让用户看到一个对不上选票范围的数
    （实测同一时刻：全表口径 1737 只，universe 口径才是能影响选票的那部分）。
    """
    cm = cap_map()
    inside = None
    try:
        from services.data import universe
        prefixed = []
        for c in cm:
            prefixed.append(('sh' + c) if c.startswith(('5', '6')) else
                            (('bj' + c) if c[:1] in ('4', '8', '9') else ('sz' + c)))
        # 注意 `universe.filter_symbols()` 返回的是**裸 6 位码集合**（不是原样返回，
        # 也不含市值层——含市值层的是 `eligible_symbols()`，在这儿用它就自指循环了：
        # 数"被市值剔掉的只数"却先过一遍市值过滤，结果永远是 0。11-07 第一次就踩成这样）。
        inside = set(universe.filter_symbols(prefixed))
    except Exception as e:
        print(f'[市值] universe 范围过滤不可用，退化为全表口径: {type(e).__name__}: {e}')
    return sum(1 for c, v in cm.items()
               if v < min_cap() and (inside is None or c in inside))


def describe() -> str:
    if not enabled_flag():
        return '市值下限：关'
    if not cap_map():
        return f'市值下限≥{min_cap()/1e8:.0f}亿（表空，暂未生效，先跑 python -m services.data.market_cap --refresh）'
    return f'总市值≥{min_cap()/1e8:.0f}亿（已排除{count_below()}只小市值）'


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--refresh', action='store_true')
    ap.add_argument('--stats', action='store_true')
    a = ap.parse_args()
    if a.refresh:
        refresh()
    if a.stats or not a.refresh:
        cm = cap_map()
        print(f'市值表：{len(cm)} 只；下限 {MIN_TOTAL_MARKET_CAP/1e8:.0f} 亿之下 {count_below()} 只')
        print(describe())
