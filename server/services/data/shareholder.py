"""shareholder.py - 股东人数筹码面数据服务（2026-07-06，2026-10-31 换报表）

数据源:东方财富 datacenter API **RPT_HOLDERNUMLATEST**（"最新股东户数"聚合表）
- 每只股票只留最新一期，全市场 5577 行（2026-10-31 实测），pageSize=500 约 12 页
- 每日 18:00 定时更新；评分时从 DB 批量查(load_holder_num_batch)，不实时拉取

2026-10-31 为什么换表：原用 `RPT_F10_EH_HOLDERNUM` + `filter=(ISNEW="1")`，
该报表的 `ISNEW` 列已不存在（返回 code 9501），任务每天打印"数据为空,跳过"，
`stock_holder_num` 一直是 0 行 —— 筹码因子恒空。新表字段名也全部换了：
HOLDER_NUM / HOLDER_NUM_CHANGE / HOLDER_NUM_RATIO / AVG_HOLD_NUM(人均持股数)
/ AVG_MARKET_CAP(人均持股市值) / HOLD_NOTICE_DATE，入库列名保持不动。

东财封 IP 时不再硬打：接入共享熔断 eastmoney_guard（scope 'holder'），
冷却期直接返回空，失败只告警、不覆盖库里已有数据。
"""
import asyncio
from typing import List, Dict, Tuple
from datetime import datetime
import requests

from db.database import save_holder_num_batch, load_holder_num_batch


HOLDER_NUM_API = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
HOLDER_NUM_HEADERS = {
    'User-Agent': 'Mozilla/5.0',
    'Referer': 'https://data.eastmoney.com/'
}
_PAGE_SIZE = 500          # 新表 5577 行 → 12 页；旧代码 100 行要 56 页，平白多 44 次请求
_GUARD_SCOPE = 'holder'


def _code_to_symbol(code: str) -> str:
    """纯数字代码转 sh/sz 前缀(北交所 8/4 开头不支持,返回空)"""
    if not code or len(code) != 6:
        return ''
    if code.startswith('6'):           # 沪市主板/科创板
        return 'sh' + code
    if code.startswith('0') or code.startswith('3'):  # 深市主板/创业板
        return 'sz' + code
    return ''  # 8/4 开头(北交所)暂不支持


def _fetch_page(pn: int) -> Tuple[List[Dict], int]:
    """同步拉取单页(500条)股东人数数据,返回 (rows, total_count)

    用 requests + asyncio.to_thread 包装(project_memory 硬约束:禁用 httpx 直连东方财富)。
    失败抛异常，由上层计入失败页数。

    熔断只用**自己的 scope**（ignore_domain=True / clear_domain=False）：
    datacenter-web 与 push2/push2delay(clist) 不是同一条边缘，2026-10-31 实测
    clist 全域闸门还在冷却时 datacenter-web 仍 200 返回 5577 行 —— 跟着 clist 一起停
    等于把能用的数据源也停了，而不加隔离又会用它的成败去误开/误关全域闸门
    （同 appdata 人气榜 2026-10-23 的处理）。
    """
    from services.data import eastmoney_guard
    params = {
        'reportName': 'RPT_HOLDERNUMLATEST',   # 每只股票只留最新一期，无需 ISNEW 过滤
        'columns': 'ALL',
        'pageNumber': pn,
        'pageSize': _PAGE_SIZE,
    }
    try:
        r = requests.get(HOLDER_NUM_API, params=params, headers=HOLDER_NUM_HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        if not data.get('success'):
            raise RuntimeError(f"datacenter success=False: {data.get('message') or data.get('code')}")
    except Exception as e:
        eastmoney_guard.fail(_GUARD_SCOPE, e, kind='flaky')
        raise
    eastmoney_guard.ok(_GUARD_SCOPE, clear_domain=False)
    res = data.get('result') or {}
    return res.get('data') or [], res.get('count') or 0


def _parse_row(row: Dict):
    """解析单条记录,返回 (symbol, info_dict) 或 None(不支持的代码)

    字段映射（RPT_HOLDERNUMLATEST → 本地表列）：
      HOLDER_NUM → holder_total_num（股东总户数）
      HOLDER_NUM_CHANGE → num_change（户数增减）
      HOLDER_NUM_RATIO → total_num_ratio（环比%，负=户数减少=筹码集中）
      AVG_HOLD_NUM → avg_free_shares（人均持股数）
      AVG_MARKET_CAP → avg_hold_amt（人均持股市值，元）
      HOLD_NOTICE_DATE → notice_date（公告日）
    """
    code = str(row.get('SECURITY_CODE', ''))
    symbol = _code_to_symbol(code)
    if not symbol:
        return None
    return (symbol, {
        'symbol': symbol,
        'end_date': (row.get('END_DATE') or '')[:10],            # 截取 YYYY-MM-DD
        'holder_total_num': row.get('HOLDER_NUM'),
        'num_change': row.get('HOLDER_NUM_CHANGE'),
        'total_num_ratio': row.get('HOLDER_NUM_RATIO'),
        'avg_free_shares': row.get('AVG_HOLD_NUM'),
        'avg_hold_amt': row.get('AVG_MARKET_CAP'),
        'notice_date': (row.get('HOLD_NOTICE_DATE') or '')[:10],
    })


async def fetch_all_latest_holder_num() -> Dict[str, Dict]:
    """全市场分页拉取最新一期股东人数（RPT_HOLDERNUMLATEST）

    Returns:
        {symbol_lower: {symbol, end_date, holder_total_num, num_change,
                        total_num_ratio, avg_free_shares, avg_hold_amt, notice_date}}
    """
    from services.data import eastmoney_guard
    if eastmoney_guard.skip(_GUARD_SCOPE, ignore_domain=True):
        print('[股东人数] 东财 datacenter 冷却中，本轮跳过（库里旧数据保留）')
        return {}
    result: Dict[str, Dict] = {}

    # 第一页(拿 count 算总页数)
    first_page, count = await asyncio.to_thread(_fetch_page, 1)
    if not first_page:
        return {}

    for row in first_page:
        parsed = _parse_row(row)
        if parsed:
            result[parsed[0]] = parsed[1]

    if count <= _PAGE_SIZE:
        return result

    # 并发拉取剩余页(每批5页,Semaphore 控并发,参考 fetch_batch_fund_flow 模式)
    total_pages = (count + _PAGE_SIZE - 1) // _PAGE_SIZE
    remaining = list(range(2, total_pages + 1))
    batch_size = 5
    failed_pages = 0
    for i in range(0, len(remaining), batch_size):
        batch = remaining[i:i + batch_size]
        pages = await asyncio.gather(
            *[asyncio.to_thread(_fetch_page, pn) for pn in batch],
            return_exceptions=True
        )
        for page_result in pages:
            if isinstance(page_result, Exception):
                failed_pages += 1
                continue
            rows, _ = page_result
            for row in rows:
                parsed = _parse_row(row)
                if parsed:
                    result[parsed[0]] = parsed[1]
    if failed_pages:
        print(f'[股东人数] {failed_pages}/{total_pages} 页拉取失败，本轮只入库 {len(result)} 条'
              f'（下次成功会按最新一期覆盖，不影响表结构）')
    return result


def get_holder_num_batch(symbols: List[str]) -> Dict[str, Dict]:
    """从 DB 批量查最新一期股东人数(供评分注入,同步函数)

    Args:
        symbols: 股票代码列表(小写带前缀 sh600519)
    Returns:
        {symbol: {holder_total_num, num_change, total_num_ratio,
                  avg_free_shares, avg_hold_amt, end_date}}
    """
    return load_holder_num_batch(symbols)


async def update_holder_num_task():
    """定时任务:全市场拉取最新股东人数并存 DB(每日 18:00 触发)"""
    try:
        print('[定时任务] 正在更新股东人数数据...')
        data = await fetch_all_latest_holder_num()
        if not data:
            print('[定时任务] 股东人数数据为空,跳过')
            return
        updated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        records = []
        for symbol, info in data.items():
            info['updated_at'] = updated_at
            records.append(info)
        saved = save_holder_num_batch(records)
        if saved:
            print(f'[定时任务] 股东人数数据更新完成: {len(records)} 条')
        else:
            print('[定时任务] 股东人数数据保存失败')
    except Exception as error:
        print(f'[定时任务] 更新股东人数失败: {str(error)}')
