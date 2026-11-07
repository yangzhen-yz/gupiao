"""smartmoney.py - 聪明钱信号服务（2026-07-16 P1b）

两路信号:
1. 龙虎榜 (RPT_DAILYBILLBOARD_DETAILS):个股上榜 + 龙虎榜净买入额(BILLBOARD_NET_AMT)
   → 代表游资/机构资金主动介入,是"聪明钱"最直接的个股级信号
2. 北向资金 (push2 kamt):沪深股通当日/月度净买额 → 市场级聪明钱情绪

设计要点:
- 龙虎榜按 symbol 聚合并缓存(默认近 5 个交易日),供扫描注入"聪明钱"评分因子
- 北向为市场级情绪指标,供策略页展示 + 可选大盘环境修饰
- 所有数据缺失均降级,不阻塞主流程
"""
import time
import threading
from typing import (Dict, List, Optional, Tuple)
from datetime import datetime, timedelta
import requests

from app.config import (SMART_MONEY_DRAGON_TIGER_DAYS, DRAGON_TIGER_PERSIST,
                        DRAGON_TIGER_DB_FALLBACK)
from db.database import load_dragon_tiger_rows_since, save_dragon_tiger_rows

DRAGON_API = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
DRAGON_HEADERS = {
    'User-Agent': 'Mozilla/5.0',
    'Referer': 'https://data.eastmoney.com/'
}
DRAGON_REPORT = 'RPT_DAILYBILLBOARD_DETAILS'
DRAGON_COLUMNS = ('TRADE_DATE,SECURITY_CODE,SECURITY_NAME_ABBR,SECUCODE,'
                  'BILLBOARD_NET_AMT,BILLBOARD_BUY_AMT,BILLBOARD_SELL_AMT,'
                  'EXPLANATION,CHANGE_RATE,CLOSE_PRICE,FREE_MARKET_CAP')

# 缓存(龙虎榜日级变动,1 小时 TTL 足够)
_dt_cache: Dict = {'ts': 0, 'days': 0, 'map': {}, 'list': [], 'source': ''}
# 2026-11-04：刷新后台化（SWR）。旧实现缓存一失效就同步拉东财，timeout=15s，
# 直接把 /api/stock/{symbol}/full 拖到 16.7~23.9s（线上实测）。龙虎榜是日级数据，
# 晚一两分钟更新对评分毫无影响，绝不该压在用户请求的关键路径上。
_dt_refresh_lock = threading.Lock()
_dt_last_attempt = {'ts': 0.0}
_DT_REFRESH_MIN_GAP = 120.0   # 秒：数据日期滞后于「今天」时，最多每 2 分钟试一次，防反复打东财
_NB_CACHE_TTL = 600
_nb_cache: Dict = {'ts': 0, 'data': None}

# 北向 kamt 配置
_NB_API = 'https://push2.eastmoney.com/api/qt/kamt/get'
_NB_HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://data.eastmoney.com/'}


def _latest_settled_trade_date() -> str:
    """返回"最近一个龙虎榜已出的交易日"。

    龙虎榜数据约 17:00 后陆续入库:
    - 若今天为交易日且当前时间 >= 18:00,今天数据已出 → 返回今天
    - 否则向前寻找最近一个交易日(周末/节假日自动跳过)
    """
    from services.data.trading_calendar import is_trading_day
    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    if is_trading_day(today) and now.hour >= 18:
        return today
    d = now.date()
    for _ in range(30):
        d = d - timedelta(days=1)
        if is_trading_day(d.strftime('%Y-%m-%d')):
            return d.strftime('%Y-%m-%d')
    return today


def _fetch_dragon_page(page_size: int = 1000) -> List[Dict]:
    """拉取最近 pageSize 条龙虎榜记录(按交易日期倒序)

    2026-11-06 接入共享熔断（scope 'datacenter'，域名 datacenter-web）：此前这里是
    闸门覆盖缺口，封禁日照样裸打。注意**停调时不能返回 []**——上层
    `_refresh_dt_cache` 会拿返回值整份覆盖内存缓存，返回空等于"把昨天的榜单清空"，
    所以跳过判定放在 `_refresh_dt_cache` 里（跳过就不动缓存）。
    """
    params = {
        'reportName': DRAGON_REPORT,
        'columns': DRAGON_COLUMNS,
        'pageSize': page_size,
        'pageNumber': 1,
        'sortColumns': 'TRADE_DATE',
        'sortTypes': -1,
        'ut': 'b2884a393a59ad64002292a3e90d46a5',
    }
    # 从 15s 单超时收紧为 (连接 3s, 读取 6s)：后台刷新线程可以慢慢等，请求链路不等
    from services.data import eastmoney_guard
    try:
        r = requests.get(DRAGON_API, params=params, headers=DRAGON_HEADERS, timeout=(3, 6))
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        eastmoney_guard.fail('datacenter', e)
        raise
    eastmoney_guard.ok('datacenter', clear_domain=False)
    if not data.get('success'):
        return []
    res = data.get('result') or {}
    return res.get('data') or []


def _dragon_rows(days: int = 5) -> Tuple[List[Dict], str]:
    """取龙虎榜原始行：东财优先，取不到就回退 `dragon_tiger_daily` 留档。

    返回 (rows, source)，source ∈ {'eastmoney', 'db'}——前端与"为什么这页是 X 号的榜"
    都靠它，不能只在日志里说。

    2026-11-06 新增的这条回退是为了补一个真空洞：龙虎榜此前**只有内存缓存**，
    进程重启即空、东财停调即整块没有，而它是评分的第 13 因子（smart_money）。
    关掉 `DRAGON_TIGER_DB_FALLBACK` 即回到改造前（停调时保持旧缓存、不读库）。
    """
    from services.data import eastmoney_guard
    since = (datetime.now() - timedelta(days=days * 2 + 4)).strftime('%Y-%m-%d')
    if eastmoney_guard.skip('datacenter'):
        if not DRAGON_TIGER_DB_FALLBACK:
            return [], 'eastmoney'
        return load_dragon_tiger_rows_since(since), 'db'
    try:
        rows = _fetch_dragon_page(1000)
    except Exception as e:
        print(f'[聪明钱] 龙虎榜接口失败，回退库内留档: {type(e).__name__}: {e}')
        return (load_dragon_tiger_rows_since(since), 'db') if DRAGON_TIGER_DB_FALLBACK else ([], 'eastmoney')
    if rows and DRAGON_TIGER_PERSIST:
        saved = save_dragon_tiger_rows(rows)        # 即写即提交；失败只打印
        if saved:
            print(f'[聪明钱] 龙虎榜留档 {saved} 行（覆盖写，幂等）')
    return rows, 'eastmoney'


def _aggregate(days: int = 5, rows: Optional[List[Dict]] = None) -> Dict:
    """聚合近 days 个交易日龙虎榜,返回 (symbol_map, sorted_list, latest_trade_date)

    `rows` 传入时直接用（东财与库内留档字段名一致，聚合口径必须同一段代码走到底，
    写两份"看起来一样"的聚合迟早会漂）；不传时按老路自己拉接口。
    """
    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    if rows is None:
        rows = _fetch_dragon_page(1000)
    agg: Dict[str, Dict] = {}
    latest_trade_date = ''
    for row in rows:
        td = str(row.get('TRADE_DATE') or '')[:10]
        if not td or td < cutoff:
            continue
        if td > latest_trade_date:
            latest_trade_date = td
        code = str(row.get('SECURITY_CODE', ''))
        # 仅处理 A 股主板/创业板/科创板(symbol 前缀)
        if code.startswith('6'):
            sym = 'sh' + code
        elif code.startswith(('0', '3')):
            sym = 'sz' + code
        else:
            continue
        try:
            net = float(row.get('BILLBOARD_NET_AMT') or 0)
        except (TypeError, ValueError):
            net = 0.0
        reason = str(row.get('EXPLANATION') or '').strip()
        try:
            chg = float(row.get('CHANGE_RATE') or 0)
        except (TypeError, ValueError):
            chg = 0.0
        name = str(row.get('SECURITY_NAME_ABBR') or '')
        if sym not in agg:
            agg[sym] = {
                'symbol': sym, 'name': name, 'net_amt': 0.0,
                'appearances': 0, 'max_change': chg, 'reasons': set(),
            }
        agg[sym]['net_amt'] += net
        agg[sym]['appearances'] += 1
        if chg > agg[sym]['max_change']:
            agg[sym]['max_change'] = chg
        if reason:
            agg[sym]['reasons'].add(reason[:24])

    symbol_map = {s: {'net_amt': v['net_amt'], 'appearances': v['appearances']}
                  for s, v in agg.items()}
    sorted_list = sorted(agg.values(), key=lambda x: x['net_amt'], reverse=True)
    for v in sorted_list:
        v['reasons'] = list(v['reasons'])
    return symbol_map, sorted_list, latest_trade_date


def _refresh_dt_cache(days: int = 5) -> None:
    """取数（东财优先、留档兜底）并原子替换缓存。只允许后台线程 / 手动 force 调用。

    两条规矩：
      · **拿到空结果不许覆盖缓存**（2026-11-06）——接口停调又恰好库里没留档时，
        继续往下走会算出空 map，把上次成功的榜单整份抹掉，第 13 因子当场归零；
      · 缓存里带 `source`（eastmoney/db）与 `asOf`，龙虎榜页与评分侧要能说清
        "这是今天的榜，还是 10-31 的留档"。
    """
    rows, source = _dragon_rows(days)
    if not rows:
        return
    symbol_map, sorted_list, latest_trade_date = _aggregate(days, rows=rows)
    _dt_cache.update({'ts': time.time(), 'days': days, 'map': symbol_map,
                      'list': sorted_list, 'latest_trade_date': latest_trade_date,
                      'source': source})


def _spawn_dt_refresh(days: int = 5) -> bool:
    """后台刷新（SWR）：已在刷新或距上次尝试过近时直接跳过，返回是否真的起了线程。"""
    now = time.time()
    if now - _dt_last_attempt['ts'] < _DT_REFRESH_MIN_GAP:
        return False
    if not _dt_refresh_lock.acquire(blocking=False):
        return False
    _dt_last_attempt['ts'] = now

    def _run():
        try:
            _refresh_dt_cache(days)
        except Exception as e:
            print(f'[聪明钱] 龙虎榜后台刷新失败（沿用旧缓存）: {type(e).__name__}: {e}')
        finally:
            _dt_refresh_lock.release()

    threading.Thread(target=_run, daemon=True, name='dragon-tiger-refresh').start()
    return True


def get_dragon_tiger(days: int = 5, top_n: int = 30, force: bool = False) -> Dict:
    """获取近 days 日龙虎榜聚合(带缓存)

    Args:
        days: 聚合交易日数
        top_n: 返回前 N 条
        force: 是否强制刷新缓存

    Returns:
        {'updateTime', 'days', 'latestTradeDate', 'stocks': [{symbol,name,net_amt,appearances,
         max_change,reasons}], 'totalOnBoard': int}
    """
    now = time.time()
    target = _latest_settled_trade_date()
    hit = (not force and _dt_cache['ts'] and _dt_cache['days'] == days
           and now - _dt_cache['ts'] < 3600
           and _dt_cache.get('latest_trade_date') == target)
    if not hit:
        # 展示页 force=手动刷新才同步等；否则后台刷新，先拿旧数据渲染（SWR）
        if force and _dt_refresh_lock.acquire(blocking=False):
            _dt_last_attempt['ts'] = now
            try:
                _refresh_dt_cache(days)
            except Exception as e:
                print(f'[聪明钱] 龙虎榜手动刷新失败（沿用旧缓存）: {type(e).__name__}: {e}')
            finally:
                _dt_refresh_lock.release()
        else:
            _spawn_dt_refresh(days)
    sorted_list = _dt_cache['list']
    latest_trade_date = _dt_cache.get('latest_trade_date', '')
    return {
        'updateTime': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'days': days,
        'latestTradeDate': latest_trade_date,
        'stocks': sorted_list[:top_n],
        'totalOnBoard': len(sorted_list),
        # 2026-11-06：'db' 表示这份榜来自 dragon_tiger_daily 留档（东财停调/失败时）
        'source': _dt_cache.get('source') or 'eastmoney',
    }


def get_dragon_tiger_symbol_map(days: int = 5, force: bool = False) -> Dict[str, Dict]:
    """扫描/单票评分注入用：返回 {symbol: {net_amt, appearances}}。

    2026-11-04 改 SWR：**永远立即返回现有缓存**，过期只触发后台刷新，绝不同步等东财。
    旧实现在 `latest_trade_date != 今天` 时缓存判定恒为假（18:00 后但当日榜单尚未入库），
    于是每换一只股票都重拉一次东财，单次最多 15s —— 这是单票行情页 16.7~23.9s 的主因。
    """
    now = time.time()
    target = _latest_settled_trade_date()
    hit = (not force and _dt_cache['map'] and _dt_cache['days'] == days
           and now - _dt_cache['ts'] < 3600
           and _dt_cache.get('latest_trade_date') == target)
    if not hit:
        if force and _dt_refresh_lock.acquire(blocking=False):
            _dt_last_attempt['ts'] = now
            try:
                _refresh_dt_cache(days)
            except Exception as e:
                print(f'[聪明钱] 龙虎榜手动刷新失败（沿用旧缓存）: {type(e).__name__}: {e}')
            finally:
                _dt_refresh_lock.release()
        else:
            _spawn_dt_refresh(days)
    return _dt_cache['map']


def get_northbound_market(force: bool = False) -> Dict:
    """获取北向资金市场级净买额(沪深股通)

    Args:
        force: 是否强制刷新缓存

    Returns:
        {'date', 'sh_net', 'sz_net', 'total', 'month_sh', 'month_sz',
         'sentiment', 'updated'}
    """
    now = time.time()
    if not force and _nb_cache['ts'] and now - _nb_cache['ts'] < _NB_CACHE_TTL and _nb_cache['data']:
        return _nb_cache['data']
    from services.data import eastmoney_guard
    if eastmoney_guard.skip('push2'):
        # 停调中：有旧值就沿用旧值（宁旧不假），没有才给全 0 + error，口径由前端显示
        if _nb_cache['data']:
            return _nb_cache['data']
        return {'date': '', 'sh_net': 0, 'sz_net': 0, 'total': 0,
                'month_sh': 0, 'month_sz': 0, 'sentiment': 'neutral',
                'updated': datetime.now().strftime('%Y-%m-%d %H:%M'),
                'error': '东财 push2 冷却/当日停调中'}
    try:
        r = requests.get(_NB_API, params={
            'fields1': 'f1,f3',
            'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58',
            'ut': 'b2884a393a59ad64002292a3e90d46a5',
        }, headers=_NB_HEADERS, timeout=15)
        j = r.json()
        eastmoney_guard.ok('push2', clear_domain=False)
        d = (j.get('data') or {})
        sh = d.get('hk2sh') or {}
        sz = d.get('hk2sz') or {}
        sh_net = float(sh.get('dayNetAmtIn') or 0)
        sz_net = float(sz.get('dayNetAmtIn') or 0)
        month_sh = float(sh.get('monthNetAmtIn') or 0)
        month_sz = float(sz.get('monthNetAmtIn') or 0)
        total = sh_net + sz_net
        if abs(total) < 1e-6:
            sentiment = 'neutral'
        elif total > 0:
            sentiment = 'bullish'
        else:
            sentiment = 'bearish'
        result = {
            'date': sh.get('date2') or sh.get('date') or '',
            'sh_net': sh_net,
            'sz_net': sz_net,
            'total': total,
            'month_sh': month_sh,
            'month_sz': month_sz,
            'sentiment': sentiment,
            'updated': datetime.now().strftime('%Y-%m-%d %H:%M'),
        }
        _nb_cache.update({'ts': now, 'data': result})
        return result
    except Exception as e:
        eastmoney_guard.fail('push2', e)
        print(f'[聪明钱] 北向资金获取失败: {e}')
        # 失败也别把旧值丢掉：有旧缓存就继续给旧缓存（下面这行在 11-06 之前是直接给全 0）
        if _nb_cache['data']:
            return _nb_cache['data']
        return {'date': '', 'sh_net': 0, 'sz_net': 0, 'total': 0,
                'month_sh': 0, 'month_sz': 0, 'sentiment': 'neutral',
                'updated': datetime.now().strftime('%Y-%m-%d %H:%M'), 'error': str(e)}


async def refresh_smart_money_task():
    """定时任务：每日刷新龙虎榜+北向资金缓存，确保缓存为"最近一个已收盘交易日"的数据。

    不再限制交易日才执行——周末也会刷新到最近交易日(如周五),
    保证前端默认加载即可看到最新龙虎榜,无需手动刷新。
    """
    try:
        target = _latest_settled_trade_date()
        print(f'[定时任务] 正在刷新聪明钱数据（目标交易日 {target}）...')
        print('[定时任务] 正在刷新龙虎榜与北向资金数据...')
        # force=True 跳过缓存，直接拉取最新数据
        dragon = get_dragon_tiger(days=SMART_MONEY_DRAGON_TIGER_DAYS, top_n=30, force=True)
        north = get_northbound_market(force=True)
        # 同时刷新扫描注入用的 symbol_map 缓存
        get_dragon_tiger_symbol_map(days=SMART_MONEY_DRAGON_TIGER_DAYS, force=True)
        print(f'[定时任务] 聪明钱数据刷新完成：龙虎榜 {dragon.get("totalOnBoard", 0)} 只，'
              f'最新交易日期 {dragon.get("latestTradeDate", "")}，'
              f'北向数据日期 {north.get("date", "")}')
    except Exception as e:
        print(f'[定时任务] 刷新聪明钱数据失败: {e}')
