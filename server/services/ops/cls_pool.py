"""cls_pool.py - 财联社四池与题材梯队（2026-10-31，影子）

用户原话："我看财联社上面关于这个板块梯队，这块不是挺全的嘛？你能爬取财联社的数据吗？"

财联社 App 的「涨停梯队」不是独立接口，但它网页端的四个池子已经把梯队所需字段给齐了：
  · 涨停池 up_pool：每只票的连板数 limit_up_days、首封时间 time、题材归因 up_reason；
  · 连板池 continuous_up_pool：同上，只含 2 板及以上；
  · 炸板池 up_open_pool / 跌停池 down_pool：只做情绪旁证，不参与梯队。

所以这里按 **up_reason 的 `|` 前缀**（如 "商业航天+机器人|..."）做题材归因，
每个题材内计算：龙头（最高板，并列看首封更早）→ 先锋（剩余首封最早）→
中军（剩余流通市值最大）→ 跟风（其余）。一只票可以同时出现在多个题材里，
这与盘面的实际理解一致（上海沪工既是商业航天又是机器人）。

接口与口径（2026-10-30 实测）：
  · 主机 https://x-quote.cls.cn，无需登录；签名 = md5(sha1(按 key 排序的 query))；
  · 公共参数 app=CailianpressWeb&os=web&sv=8.7.9；
  · 单次 0.28~0.61s，六路并行约 0.6~0.9s —— 因此不上页面热路径，
    15:10 收盘落库 + 盘中缓存 60s / 休市 600s，失败回退上一次成功快照。
  · **网页接口不支持历史日期**（传 date=20261029 仍返回最新交易日），
    历史只能从今天起逐日留档，本模块不假装能回填。
  · 与本地东财涨停池口径不同：10-30 财联社 49（含 2 只 *ST）、本地 51，47 只重合。
    展示必须标源，**不能拿两边混算比例**。

边界（影子第一周）：
  · 只展示，不进选股评分、不进买入门槛、不进推送；
  · 题材归因来自财联社的编辑口径，不是官方申万行业；
  · 没有自建的历史回测，也就没有"梯队选股胜率"这种数字可写。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests

from db.database import get_db_conn, save_cls_pool_daily, load_cls_pool_daily

BASE = 'https://x-quote.cls.cn'
_COMMON = {'app': 'CailianpressWeb', 'os': 'web', 'sv': '8.7.9'}
_TIMEOUT = (2, 5)
_POOLS = {
    'up_pool': '涨停池',
    'continuous_up_pool': '连板池',
    'up_open_pool': '炸板池',
    'down_pool': '跌停池',
}
# 题材归因里这些前缀说明"为什么涨"而不是"涨在哪个题材"，不能拿来组梯队
_GENERIC_THEMES = {
    '业绩', '业绩超预期', '业绩预增', '预盈预增', '扭亏为盈', '业绩增长',
    '昨日涨停', '昨日连板', '昨日高换手', '连板', '涨停',
    '股权转让', '回购', '回购增持', '增持', '举牌', '并购重组', '重大资产重组',
    '资产重组', '资产注入', '历史新高', '百日新高', '次新股', '新股与次新股',
    '高股息', '破净', '低价股', '国企改革', '央企改革', '中字头', '参股金融',
    '创投', '融资融券', '深股通', '沪股通', '转融券标的', 'MSCI概念',
    '富时罗素', '标普道琼斯A股', '摘帽', 'ST摘帽', '股东减持', '减持',
    '股权激励', '员工持股', '定增', '可转债', '送转', '高送转', '分红',
    '注销式回购', '增持计划', '公告', '公司公告', '利好', '中标', '中标公告',
}

_SESSION = requests.Session()
_SESSION.headers.update({
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'),
    'Referer': 'https://www.cls.cn/',
    'Accept': 'application/json, text/plain, */*',
})

# 缓存与熔断（进程内；服务重启后第一次请求会现抓一次）
_CACHE: Dict[str, object] = {'ts': 0.0, 'data': None}
_LOCK = threading.Lock()
_BREAK = {'fails': 0, 'until': 0.0}
_LAST_SAVE = {'ts': 0.0, 'day': ''}   # 落库节流：同一天 180s 内只写一次
_BREAK_AFTER = 3          # 连续失败 3 次
_BREAK_SEC = 300          # 暂停 5 分钟
_TTL_LIVE = 60
_TTL_IDLE = 600


# ========== 请求层 ==========
def _sign(params: Dict[str, str]) -> str:
    raw = '&'.join(f'{k}={v}' for k, v in sorted(params.items()))
    return hashlib.md5(hashlib.sha1(raw.encode()).hexdigest().encode()).hexdigest()


def _signed_get(path: str, params: Optional[Dict] = None) -> Dict:
    p = dict(_COMMON)
    for k, v in (params or {}).items():
        p[str(k)] = str(v)
    p['sign'] = _sign(p)
    resp = _SESSION.get(BASE + path, params=p, timeout=_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    if not isinstance(body, dict):
        raise ValueError(f'非预期返回: {str(body)[:80]}')
    if body.get('code') not in (200, 0) or body.get('error'):
        raise ValueError(f"接口 {path} 返回 code={body.get('code')} msg={body.get('msg')}")
    return body


def _norm_symbol(code: str) -> str:
    """sh605338 / sz001219 → 605338 / 001219（本地库口径）。"""
    c = str(code or '').strip().lower()
    if len(c) == 8 and c[:2] in ('sh', 'sz', 'bj'):
        return c[2:]
    return c


def _pct(v) -> Optional[float]:
    """接口给 0.1001 表示 10.01%。"""
    try:
        return round(float(v) * 100, 2)
    except (TypeError, ValueError):
        return None


def _reason_prefix(text: str) -> List[str]:
    head = str(text or '').split('|', 1)[0].strip()
    if not head:
        return []
    out = []
    for part in head.replace('，', '+').replace('、', '+').split('+'):
        t = part.strip()
        if t and t not in _GENERIC_THEMES and len(t) <= 12 and t not in out:
            out.append(t)
    return out


def _fetch_pool(kind: str) -> List[Dict]:
    body = _signed_get('/quote/index/up_down_analysis',
                       {'type': kind, 'way': 'last_px', 'rever': 1})
    data = body.get('data')
    return data if isinstance(data, list) else []


def _normalize_pool(kind: str, rows: List[Dict]) -> List[Dict]:
    out = []
    for r in rows:
        if not isinstance(r, dict) or not r.get('secu_code'):
            continue
        code = str(r.get('secu_code')).strip()
        plates = [p.get('secu_name') for p in (r.get('plate') or []) if isinstance(p, dict)]
        # 落库重建时传入的是已经算好的最终题材（含板块兜底），优先使用，
        # 保证"重启后从库里重建"与"直连抓取"得到同一份梯队，不因板块榜缺失而漂移。
        preset = r.get('themes')
        out.append({
            'symbol': _norm_symbol(code),
            'code': code,
            'name': str(r.get('secu_name') or r.get('name') or '').strip() or None,
            'changePct': _pct(r.get('change')),
            'lastPx': r.get('last_px'),
            'sealTime': str(r.get('time') or ''),
            'days': int(r.get('limit_up_days') or 1) if kind != 'down_pool' else None,
            'isSt': 1 if r.get('is_st') else 0,
            'reason': str(r.get('up_reason') or ''),
            'themes': list(preset) if isinstance(preset, list) and preset
            else _reason_prefix(r.get('up_reason')),
            'plates': [p for p in plates if p],
            'numStocks': r.get('num_stocks'),
            'pool': kind,
        })
    return out


def _local_caps(day: str) -> Dict[str, Dict]:
    """本地东财涨停池的流通市值/成交额，用于补"中军"（财联社池不带市值）。

    只补不覆盖：财联社与本地口径不同（10-30 重合 47/51），补不到的票只是
    ltsz 为空，不会伪造数字。读不到整表也不影响主流程。
    """
    caps: Dict[str, Dict] = {}
    try:
        conn = get_db_conn()
        try:
            rows = conn.execute(
                "SELECT symbol, raw FROM limit_pool_daily WHERE trade_date=? AND pool='zt'",
                (day,)).fetchall()
        finally:
            conn.close()
        for symbol, raw in rows:
            try:
                it = json.loads(raw or '{}')
            except Exception:
                continue
            if not isinstance(it, dict):
                continue
            ltsz = it.get('ltsz') or it.get('tshare')
            try:
                ltsz = float(ltsz) if ltsz not in (None, '') else None
            except (TypeError, ValueError):
                ltsz = None
            caps[str(symbol)] = {
                'ltsz': round(ltsz / 1e8, 1) if ltsz else None,   # 亿元
                'amount': (float(it['amount']) / 1e8) if it.get('amount') else None,
            }
    except Exception as error:
        print(f'[财联社梯队] 本地市值补齐失败 day={day}: {error}')
    return caps


def _fetch_plates(rn: int = 30) -> List[Dict]:
    body = _signed_get('/web_quote/plate/plate_list', {'rn': rn})
    data = body.get('data') or {}
    rows = data.get('plate_data') if isinstance(data, dict) else None
    return rows if isinstance(rows, list) else []


def _fetch_dingpan() -> Dict:
    body = _signed_get('/v2/quote/a/web/dingpan', {})
    data = body.get('data')
    return data if isinstance(data, dict) else {}


# ========== 组装修剪 ==========
def _dedupe_pool(*pools: List[Dict]) -> List[Dict]:
    """涨停池 + 连板池去重（连板池票也在涨停池里，连板数取大）。"""
    seen: Dict[str, Dict] = {}
    for rows in pools:
        for row in rows or []:
            key = row['symbol']
            old = seen.get(key)
            if not old:
                seen[key] = dict(row)
                continue
            old['days'] = max(int(old.get('days') or 1), int(row.get('days') or 1))
            if len(row.get('themes') or []) > len(old.get('themes') or []):
                old['themes'] = row['themes']
            if not old.get('reason') and row.get('reason'):
                old['reason'] = row['reason']
    return list(seen.values())


def _fallback_themes(member: Dict, limits: Dict[str, float]) -> List[str]:
    """up_reason 里没有可用题材时，退回板块标签（过滤通用标签，取当日涨幅最高者）。"""
    out = []
    for name in member.get('plates') or []:
        n = str(name)
        if n in _GENERIC_THEMES or any(g in n for g in ('昨日', '新高', '持股', '重仓')):
            continue
        if n not in out:
            out.append(n)
    out.sort(key=lambda n: limits.get(n, -99), reverse=True)
    return out[:1]


def _role_of(members: List[Dict]) -> List[Dict]:
    """按 龙头 → 先锋 → 中军 → 跟风 排角色。

    规则一句话：龙头看板高（并列看谁先封），先锋看剩下谁封得最早，
    中军看剩下谁盘子最大，其余都是跟风。缺失市值就不硬设中军。
    """
    members = sorted(members, key=lambda m: (-(m.get('days') or 1), m.get('sealTime') or '99:99:99'))
    if not members:
        return []
    roles: Dict[str, str] = {}
    rest = list(members)
    leader = rest.pop(0)
    roles[leader['symbol']] = '龙头'
    if rest:
        rest.sort(key=lambda m: m.get('sealTime') or '99:99:99')
        pioneer = rest.pop(0)
        roles[pioneer['symbol']] = '先锋'
    if rest:
        cap_sorted = sorted(rest, key=lambda m: -(m.get('ltsz') or 0))
        if cap_sorted and (cap_sorted[0].get('ltsz') or 0) > 0:
            core = cap_sorted[0]
            roles[core['symbol']] = '中军'
    for m in members:
        roles.setdefault(m['symbol'], '跟风')
    for m in members:
        m['role'] = roles[m['symbol']]
    return members


def build_ladder(pools: Dict[str, List[Dict]], day: str) -> List[Dict]:
    """由四池生成题材梯队（只取涨停/连板，炸板跌停只用来看情绪）。"""
    up = _normalize_pool('up_pool', pools.get('up_pool') or [])
    cont = _normalize_pool('continuous_up_pool', pools.get('continuous_up_pool') or [])
    members = _dedupe_pool(up, cont)
    caps = _local_caps(day)
    limits = {}
    for b in (pools.get('_plates') or []):
        try:
            limits[str(b.get('secu_name'))] = float(b.get('change') or 0)
        except (TypeError, ValueError):
            pass
    # 归因：先用 up_reason 前缀；没有就用板块标签兜底
    groups: Dict[str, List[Dict]] = {}
    for m in members:
        m['ltsz'] = (caps.get(m['symbol']) or {}).get('ltsz')
        themes = m.get('themes') or _fallback_themes(m, limits)
        m['themes'] = themes[:2]
        for t in m['themes']:
            groups.setdefault(t, []).append(m)
    # 记录每只票最终归属的题材（含板块兜底），供落库；重建时原样读回，
    # 避免"从库里重建"时板块榜缺失导致 fallback 题材变化、梯队组跟着漂移。
    theme_map: Dict[str, List[str]] = {}
    for m in members:
        theme_map[m['symbol']] = list(m.get('themes') or [])
    pools['_themeMap'] = theme_map
    ladders = []
    for theme, rows in groups.items():
        # 一只票一天算一次：同题材内去重
        uniq: Dict[str, Dict] = {}
        for r in rows:
            uniq.setdefault(r['symbol'], r)
        items = list(uniq.values())
        if len(items) < 2:
            continue          # 单票题材组不成梯队，只是个股新闻
        items = _role_of(items)
        first_seal = min((i.get('sealTime') or '99') for i in items)
        ladders.append({
            'theme': theme,
            'count': len(items),
            'maxDays': max(int(i.get('days') or 1) for i in items),
            'firstSeal': first_seal[11:16] if len(first_seal) > 11 else first_seal,
            'members': [{
                'symbol': i['symbol'],
                'name': i.get('name'),
                'role': i.get('role'),
                'days': i.get('days'),
                'sealTime': (i.get('sealTime') or '')[11:16] or '—',
                'changePct': i.get('changePct'),
                'ltsz': i.get('ltsz'),
                'isSt': i.get('isSt'),
            } for i in items[:8]],
        })
    ladders.sort(key=lambda x: (-x['count'], -x['maxDays'], x['firstSeal']))
    return ladders[:3]


def build_summary(pools: Dict[str, List[Dict]], dingpan: Dict, plates: List[Dict]) -> Dict:
    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    plate_rows = []
    for p in (plates or [])[:30]:
        if not isinstance(p, dict):
            continue
        plate_rows.append({
            'name': p.get('secu_name'),
            'limitUpNum': _int(p.get('limit_up_num')) or 0,
            'limitDownNum': _int(p.get('limit_down_num')) or 0,
            'changePct': _pct(p.get('change')),
            'fundYi': round((p.get('main_fund_diff') or 0) / 1e8, 2),
            'first': ((p.get('first_stock') or {}).get('secu_name') or ''),
        })
    plate_rows.sort(key=lambda x: (-x['limitUpNum'], -(x['changePct'] or -99)))
    return {
        'counts': {
            'up': len(pools.get('up_pool') or []),
            'continuous': len(pools.get('continuous_up_pool') or []),
            'open': len(pools.get('up_open_pool') or []),
            'down': len(pools.get('down_pool') or []),
        },
        'dingpan': {
            'marketDegree': _int(dingpan.get('market_degree')),
            'turnover': dingpan.get('shsz_balance') or '',
            'turnoverDelta': dingpan.get('shsz_balance_change_px') or '',
            'sealRate': dingpan.get('up_ratio') or '',
            'limitUpNum': _int(dingpan.get('up_ratio_num')),
            'openNum': _int(dingpan.get('up_open_num')),
            'prevZtPerf': dingpan.get('performance') or '',
            'gapUpRate': dingpan.get('up_open_ratio') or '',
            'profitRate': dingpan.get('profit_ratio') or '',
        },
        'plates': plate_rows[:6],
    }


def _day_of(pools: Dict[str, List[Dict]]) -> str:
    for kind in ('up_pool', 'continuous_up_pool', 'up_open_pool', 'down_pool'):
        for row in pools.get(kind) or []:
            t = str(row.get('time') or '')
            if len(t) >= 10 and t[4] == '-':
                return t[:10]
    return datetime.now().strftime('%Y-%m-%d')


# ========== 快照 ==========
def _assemble(pools: Dict[str, List[Dict]], dingpan: Dict, plates: List[Dict]) -> Dict:
    day = _day_of(pools)
    pools = dict(pools)
    pools['_plates'] = plates
    ladder = build_ladder(pools, day)
    summary = build_summary(pools, dingpan, plates)
    return {
        'date': day,
        'source': 'cls_x_quote',
        'ladders': ladder,
        'summary': summary,
        'themeMap': pools.get('_themeMap') or {},
        'meta': {
            'source': '财联社 x-quote 网页接口（非官方开放平台，未登录）',
            'caliber': '题材=up_reason 前缀；龙头=最高板（并列看首封），先锋=剩余最早封板，'
                       '中军=剩余流通市值最大，跟风=其余；一只票可属多个题材。',
            'dayDiff': '财联社涨停数与本地东财涨停池口径不同（10-30：49 vs 51，47 只重合），'
                       '两边比例不可混算。',
            'disclaimer': '影子观察：只展示，不进选股评分/买入门槛/推送，先用一周看是否可信。',
        },
    }


async def snapshot(live: bool = True, force: bool = False) -> Dict:
    """行动台/接口入口。缓存 60s（盘中）/600s（休市）；失败回退上一次成功快照。

    force=True 跳过缓存：15:10 收盘定版必须重抓，不能拿盘中最后一次刷新的快照充数。
    """
    now = time.time()
    ttl = _TTL_LIVE if live else _TTL_IDLE
    with _LOCK:
        cached = _CACHE.get('data')
        if not force and cached and now - float(_CACHE.get('ts') or 0) < ttl:
            return cached
        if now < _BREAK['until']:
            if cached:
                return {**cached, 'stale': True, 'break': True}
            raise RuntimeError('财联社接口冷却中')

    try:
        pools: Dict[str, List[Dict]] = {}
        async def _one(kind):
            return kind, await asyncio.to_thread(_fetch_pool, kind)
        results = await asyncio.gather(*(_one(k) for k in _POOLS), return_exceptions=True)
        failed = []
        for item in results:
            if isinstance(item, Exception):
                failed.append(item)
                continue
            kind, rows = item
            pools[kind] = rows
        if not pools.get('up_pool'):
            raise RuntimeError(f'涨停池抓取失败: {failed[:1]}')
        # 情绪旁证与板块榜失败只降级，不让整张卡消失
        dingpan, plates = {}, []
        aux = await asyncio.gather(asyncio.to_thread(_fetch_dingpan),
                                   asyncio.to_thread(_fetch_plates),
                                   return_exceptions=True)
        if not isinstance(aux[0], Exception):
            dingpan = aux[0]
        else:
            print(f'[财联社梯队] 看盘头部失败: {aux[0]}')
        if not isinstance(aux[1], Exception):
            plates = aux[1]
        else:
            print(f'[财联社梯队] 板块榜失败: {aux[1]}')

        data = _assemble(pools, dingpan, plates)
        # themeMap 只用于落库（保证重建与直连一致），不下发给前端
        theme_map = data.pop('themeMap', {}) or {}
        data['fetchedAt'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        data['stale'] = False
        with _LOCK:
            _CACHE['data'] = data
            _CACHE['ts'] = time.time()
            _BREAK['fails'] = 0
            _BREAK['until'] = 0.0
        # 盘中 60s 缓存会带来每 60s 一次的抓取；落库不必跟着这个频率走：
        # 同一交易日 180s 内只写一次，换日或 force（15:10 定版）立即写。
        should_save = (force or data['date'] != _LAST_SAVE.get('day')
                       or time.time() - float(_LAST_SAVE.get('ts') or 0) >= 180)
        try:
            if not should_save:
                data['_saved'] = 0
                return data
            rows = []
            for kind in _POOLS:
                for row in _normalize_pool(kind, pools.get(kind) or []):
                    # 落库写"最终题材"（含板块兜底），重建时才能还原同一份梯队
                    if theme_map.get(row['symbol']):
                        row['themes'] = theme_map[row['symbol']]
                    rows.append(row)
            data['_saved'] = save_cls_pool_daily(data['date'], rows)
            if data['_saved']:
                _LAST_SAVE['ts'] = time.time()
                _LAST_SAVE['day'] = data['date']
        except Exception as error:
            print(f'[财联社梯队] 落库失败: {error}')
        return data
    except Exception as error:
        with _LOCK:
            _BREAK['fails'] += 1
            if _BREAK['fails'] >= _BREAK_AFTER:
                _BREAK['until'] = time.time() + _BREAK_SEC
                _BREAK['fails'] = 0
            cached = _CACHE.get('data')
        if cached:
            return {**cached, 'stale': True, 'error': str(error)[:120]}
        raise


def peek(max_age_sec: Optional[float] = None) -> Optional[Dict]:
    """同步读最近一次成功快照（行动台超时兜底用），不触发任何请求。"""
    with _LOCK:
        data = _CACHE.get('data')
        if not data:
            return None
        if max_age_sec is not None and time.time() - float(_CACHE.get('ts') or 0) > max_age_sec:
            return None
        return data


def _latest_db_day() -> str:
    try:
        conn = get_db_conn()
        try:
            row = conn.execute('SELECT MAX(trade_date) FROM cls_pool_daily').fetchone()
        finally:
            conn.close()
        return str(row[0]) if row and row[0] else ''
    except Exception:
        return ''


def peek_from_db(day: Optional[str] = None) -> Optional[Dict]:
    """内存没有时从落库数据重建（服务刚重启、盘中接口超时）。

    取库中最近一个交易日（周末打开时就是上一个交易日），不假设"今天有数据"。
    """
    day = day or _latest_db_day()
    if not day:
        return None
    try:
        groups = load_cls_pool_daily(day)
    except Exception:
        return None
    if not (groups.get('up_pool') or []):
        return None
    back = {
        k: [{**r, 'time': r.get('sealTime'),
             'change': (r.get('changePct') or 0) / 100,
             'plate': [{'secu_name': p} for p in (r.get('plates') or [])],
             'up_reason': r.get('reason'), 'limit_up_days': r.get('days'),
             'secu_name': r.get('name'), 'themes': r.get('themes'),
             'secu_code': r.get('code') or r.get('symbol'), 'is_st': r.get('isSt')}
            for r in rows]
        for k, rows in groups.items() if k in _POOLS
    }
    data = _assemble(back, {}, [])
    data['fetchedAt'] = ''
    data['stale'] = True
    data['fromDb'] = True
    return data


async def sync_today() -> int:
    """收盘落库（15:10 / 15:45 重试）：强制重抓一次、写 cls_pool_daily，返回写入行数。

    force 不能省：15:10 时盘中轮询的 60s 缓存里可能还是 14:5x 的数据，
    直接返回缓存就会把"盘中某次快照"误当收盘定版落库。
    """
    data = await snapshot(live=False, force=True)
    return int(data.get('_saved') or 0)
