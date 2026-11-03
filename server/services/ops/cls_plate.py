"""cls_plate.py - 财联社板块榜（2026-10-31，展示兜底）

为什么有它：东财 clist 被封期间，资金流页的「概念板块」与收评的「板块轮动」一直是空的。
新浪没有概念板块分类（`fundflow._sina_sector_rankings` 只覆盖行业），收评也没有兜底。
财联社网页端板块榜免登录、一次 0.5s 能拿全量，字段正好够补这两块。

接口事实（2026-10-31 实测）：
  - `GET https://x-quote.cls.cn/web_quote/plate/plate_list`
    公共参数与签名沿用 `cls_pool`（app=CailianpressWeb&os=web&sv=8.7.9，sign=md5(sha1(排序后的 key=value))）。
  - `type=industry|concept`；`way=change|main_fund_diff`；`rever=1` 降序 / `0` 升序。
  - `page=N` 返回的是**累计** N×30 条，不是第 N 页：page=5 一次给全 54 个行业（is_all=1），
    page=20 一次给全 398 个概念（is_all=1）。所以全量只要一次请求。
  - 字段：`change` 小数、`main_fund_diff` 元、`limit_up/limit_down` 涨/跌家数、
    `limit_up_num/limit_down_num` 涨停/跌停家数、`first_stock` 当日领涨股（含涨幅）。
  - 不支持历史日期（传 date 无效），只能逐日落库 `cls_plate_daily`。

口径边界：
  - 财联社板块分类与东财/申万不是一套，成员数也不同（「人形机器人」cls82064 有 271 只成员），
    只适合看板块冷热与资金，**不用于选股、不参与评分/门槛/推送**。
  - 板块主力净流入与其成分股 `fundflow` 合计精确相等（白酒 24.29 亿、食品饮料 27.77 亿），
    所以不同板块之间可直接比大小；但与东财 f62 的板块数值不可混算。
"""
import json
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

from db.database import save_cls_plate_daily
from services.data.fundflow import _BROAD_CONCEPT_BLOCKLIST
from services.ops import cls_pool

_LIST_PATH = '/web_quote/plate/plate_list'
_STOCKS_PATH = '/web_quote/plate/stocks'
# 一次取全量所需的 page：返回累计 N×30 条，行业 54 / 概念 398
_KIND_PAGE = {'industry': 5, 'concept': 20}
_TTL_LIVE = 60
_TTL_IDLE = 600
_BREAK_AFTER = 3
_BREAK_SEC = 300

_CACHE: Dict[str, Dict] = {}          # kind -> {'ts': float, 'rows': List[Dict]}
_LOCK = threading.Lock()
_BREAK = {'fails': 0, 'until': 0.0}


def _in_trading_window() -> bool:
    """粗判盘中（周一~周五 9:10-15:10），只用于挑缓存 TTL。"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 10) <= hm <= (15 * 60 + 10)


def _num(v, default=None):
    try:
        if v in (None, ''):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v):
    n = _num(v)
    return int(n) if n is not None else 0


def _pct(v) -> Optional[float]:
    """接口给 0.1001 表示 10.01%。"""
    n = _num(v)
    return round(n * 100, 2) if n is not None else None


def _norm(row: Dict) -> Optional[Dict]:
    if not isinstance(row, dict) or not row.get('secu_code'):
        return None
    first = row.get('first_stock') if isinstance(row.get('first_stock'), dict) else {}
    return {
        'code': str(row.get('secu_code') or '').strip(),
        'name': str(row.get('secu_name') or '').strip(),
        'changePct': _pct(row.get('change')),
        'mainFundDiff': _num(row.get('main_fund_diff'), 0.0),   # 元
        'upCount': _int(row.get('limit_up')),                   # 上涨家数
        'downCount': _int(row.get('limit_down')),               # 下跌家数
        'limitUpNum': _int(row.get('limit_up_num')),
        'limitDownNum': _int(row.get('limit_down_num')),
        'firstStock': ({
            'name': str(first.get('secu_name') or '').strip() or None,
            'code': str(first.get('secu_code') or '').strip() or None,
            'changePct': _pct(first.get('change')),
        } if first else None),
        'tradeStatus': str(row.get('trade_status') or ''),
    }


def _fetch(kind: str) -> List[Dict]:
    """抓某一类板块的全量（行业 54 / 概念 398），按涨跌幅降序。"""
    page = _KIND_PAGE.get(kind, 20)
    body = cls_pool._signed_get(_LIST_PATH, {
        'rn': 30, 'type': kind, 'way': 'change', 'rever': 1, 'page': page,
    })
    data = body.get('data') or {}
    raw = data.get('plate_data') if isinstance(data, dict) else None
    rows = [x for x in (_norm(r) for r in (raw or [])) if x and x.get('name')]
    # page 是累计语义，正常不会重复；这里仍按代码去重，避免接口行为变化时重复计数
    seen, out = set(), []
    for r in rows:
        if r['code'] in seen:
            continue
        seen.add(r['code'])
        out.append(r)
    out.sort(key=lambda r: r.get('changePct') if r.get('changePct') is not None else -999,
             reverse=True)
    return out


def load(kind: str, force: bool = False) -> List[Dict]:
    """带缓存与熔断的板块列表。失败且有旧缓存时返回旧缓存（不抛）。"""
    if kind not in _KIND_PAGE:
        raise ValueError(f'未知板块类型: {kind}')
    now = time.time()
    ttl = _TTL_LIVE if _in_trading_window() else _TTL_IDLE
    with _LOCK:
        cached = _CACHE.get(kind)
        if not force and cached and now - float(cached.get('ts') or 0) < ttl:
            return cached['rows']
        if now < _BREAK['until']:
            if cached:
                return cached['rows']
            raise RuntimeError('财联社板块接口冷却中')
    try:
        rows = _fetch(kind)
        if not rows:
            raise RuntimeError(f'{kind} 板块榜为空')
        with _LOCK:
            _CACHE[kind] = {'ts': time.time(), 'rows': rows}
            _BREAK['fails'] = 0
            _BREAK['until'] = 0.0
        return rows
    except Exception:
        with _LOCK:
            _BREAK['fails'] += 1
            if _BREAK['fails'] >= _BREAK_AFTER:
                _BREAK['until'] = time.time() + _BREAK_SEC
                _BREAK['fails'] = 0
            cached = _CACHE.get(kind)
        if cached:
            return cached['rows']
        raise


def _filtered(kind: str, rows: List[Dict]) -> List[Dict]:
    if kind != 'concept':
        return rows
    # 与东财口径保持一致：过滤融资融券/昨日涨停/指数成分这类宽泛概念
    return [r for r in rows if r.get('name') not in _BROAD_CONCEPT_BLOCKLIST]


def _side(rows: List[Dict], top_n: int, inflow: bool) -> List[Dict]:
    """按主力净流入排序截断，输出与 fundflow._normalize_sector 一致的形状（单位：元）。"""
    pool = [r for r in rows if (r.get('mainFundDiff') or 0) > 0] if inflow \
        else [r for r in rows if (r.get('mainFundDiff') or 0) < 0]
    pool.sort(key=lambda r: r.get('mainFundDiff') or 0, reverse=inflow)
    return [{
        'code': r['code'],
        'name': r['name'],
        'changePercent': r.get('changePct') or 0.0,
        'mainNetInflow': round(float(r.get('mainFundDiff') or 0), 2),
        'riseCount': r.get('upCount') or 0,
        'fallCount': r.get('downCount') or 0,
        'rank': i + 1,
        'source': 'cls',
    } for i, r in enumerate(pool[:top_n])]


def sector_rankings(top_n: int = 20) -> Optional[Dict]:
    """资金流页板块排名兜底：{industry:{inflow,outflow}, concept:{inflow,outflow}}。

    两类独立降级：某一类抓不到就返回空列表，另一类照常给。
    """
    out: Dict[str, Dict] = {}
    ok = False
    for kind in ('industry', 'concept'):
        try:
            rows = _filtered(kind, load(kind))
        except Exception as error:
            print(f'[财联社板块] {kind} 不可用: {error}')
            out[kind] = {'inflow': [], 'outflow': []}
            continue
        out[kind] = {'inflow': _side(rows, top_n, True),
                     'outflow': _side(rows, top_n, False)}
        ok = ok or bool(rows)
    return out if ok else None


def review_sectors() -> Dict[str, List[Dict]]:
    """收评「板块轮动」兜底：行业/概念都按涨跌幅降序，行内带 source='cls'。

    形状对齐 review._sync_fetch_sectors：name/code/changePercent/mainNetInflow/
    riseCount/fallCount；volume/amount 财联社不提供，落 0，绝不编数。
    """
    out: Dict[str, List[Dict]] = {'industry': [], 'concept': []}
    for kind in ('industry', 'concept'):
        try:
            rows = _filtered(kind, load(kind))
        except Exception as error:
            print(f'[财联社板块] {kind} 不可用: {error}')
            continue
        rows.sort(key=lambda r: r.get('changePct') if r.get('changePct') is not None else -999,
                  reverse=True)
        out[kind] = [{
            'name': r['name'],
            'code': r['code'],
            'changePercent': r.get('changePct') or 0.0,
            'mainNetInflow': round(float(r.get('mainFundDiff') or 0), 2),
            'volume': 0,
            'amount': 0,
            'riseCount': r.get('upCount') or 0,
            'fallCount': r.get('downCount') or 0,
            'source': 'cls',
        } for r in rows]
    return out


def members(code: str, limit: int = 60) -> List[Dict]:
    """板块成分股（收评弹窗用）。财联社无换手率，返回里不造这个字段。"""
    code = str(code or '').strip()
    if not code.lower().startswith('cls'):
        raise ValueError('只接受财联社板块代码（clsXXXXX）')
    body = cls_pool._signed_get(_STOCKS_PATH, {'secu_code': code})
    data = body.get('data') or {}
    raw = data.get('stocks') if isinstance(data, dict) else None
    rows = []
    for r in raw or []:
        if not isinstance(r, dict) or not r.get('secu_code'):
            continue
        rows.append({
            'code': cls_pool._norm_symbol(r.get('secu_code')),
            'name': str(r.get('secu_name') or '').strip(),
            'changePercent': _pct(r.get('change')),
            'mainNetInflow': _num(r.get('fundflow'), 0.0),
            'marketCap': _num(r.get('cmc')),
            'lastPx': _num(r.get('last_px')),
            'isCore': 1 if r.get('is_core') else 0,
        })
    rows.sort(key=lambda r: r.get('changePercent') if r.get('changePercent') is not None else -999,
              reverse=True)
    return rows[:limit]


def sync_today(day: Optional[str] = None) -> int:
    """收盘落库（15:10 / 15:45，仅交易日）。财联社无历史接口，只能逐日留档。

    行业 54 + 概念 398 一次请求各一份，整表覆盖写；`day` 由调度器传交易日，
    手工调用时不传则用当天（周末调用会把上一交易日的数据标成当天，别这么用）。
    """
    day = day or datetime.now().strftime('%Y-%m-%d')
    rows: List[Dict] = []
    for kind in ('industry', 'concept'):
        try:
            for r in load(kind, force=True):
                rows.append({**r, 'plateType': kind,
                             'firstStockJson': json.dumps(r.get('firstStock'), ensure_ascii=False)
                             if r.get('firstStock') else None})
        except Exception as error:
            print(f'[财联社板块] {kind} 落库前抓取失败 day={day}: {error}')
    if not rows:
        return 0
    return save_cls_plate_daily(day, rows)


def peek(max_age_sec: Optional[float] = None) -> Optional[Dict]:
    """同步读内存里已有的板块（自测/核对用），不触发请求。"""
    with _LOCK:
        out = {}
        for kind, item in _CACHE.items():
            if max_age_sec is not None and time.time() - float(item.get('ts') or 0) > max_age_sec:
                continue
            out[kind] = item.get('rows') or []
    return out or None


def snapshot() -> Dict:
    """接口自检用：两类板块的条数与头部，不落库。"""
    out = {'fetchedAt': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'plates': {}}
    for kind in ('industry', 'concept'):
        try:
            rows = _filtered(kind, load(kind))
            out['plates'][kind] = {
                'count': len(rows),
                'top': [{'code': r['code'], 'name': r['name'],
                         'changePercent': r.get('changePct'),
                         'mainNetInflow': r.get('mainFundDiff')} for r in rows[:5]],
            }
        except Exception as error:
            out['plates'][kind] = {'count': 0, 'error': str(error)[:120]}
    return out
