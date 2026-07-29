"""shareholder.py - 股东人数筹码面数据服务（2026-07-06）

数据源:东方财富 datacenter API RPT_F10_EH_HOLDERNUM
- 月度披露(每只股票每月1条),filter=(ISNEW="1") 只取最新一期
- 全市场约5000条,分页拉取,每日 18:00 定时更新
- 评分时从 DB 批量查(load_holder_num_batch),不实时拉取(月度数据无需实时)

参考模式:services/recommend.py 的 fetch_batch_fund_flow(requests + asyncio.to_thread)
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
    """同步拉取单页(100条)股东人数数据,返回 (rows, total_count)

    用 requests + asyncio.to_thread 包装(project_memory 硬约束:禁用 httpx 直连东方财富)
    """
    params = {
        'reportName': 'RPT_F10_EH_HOLDERNUM',
        'columns': 'ALL',
        'filter': '(ISNEW="1")',   # 只取每只股票最新一期
        'pageNumber': pn,
        'pageSize': 100,
    }
    r = requests.get(HOLDER_NUM_API, params=params, headers=HOLDER_NUM_HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not data.get('success'):
        return [], 0
    res = data.get('result') or {}
    return res.get('data') or [], res.get('count') or 0


def _parse_row(row: Dict):
    """解析单条记录,返回 (symbol, info_dict) 或 None(不支持的代码)"""
    code = str(row.get('SECURITY_CODE', ''))
    symbol = _code_to_symbol(code)
    if not symbol:
        return None
    return (symbol, {
        'symbol': symbol,
        'end_date': (row.get('END_DATE') or '')[:10],      # 截取 YYYY-MM-DD
        'holder_total_num': row.get('HOLDER_TOTAL_NUM'),
        'num_change': row.get('HOLDER_TOTAL_NUMCHANGE'),
        'total_num_ratio': row.get('TOTAL_NUM_RATIO'),       # 环比变化率%
        'avg_free_shares': row.get('AVG_FREE_SHARES'),
        'avg_hold_amt': row.get('AVG_HOLD_AMT'),
        'notice_date': (row.get('NOTICE_DATE') or '')[:10],
    })


async def fetch_all_latest_holder_num() -> Dict[str, Dict]:
    """全市场分页拉取最新一期股东人数(ISNEW=1)

    Returns:
        {symbol_lower: {symbol, end_date, holder_total_num, num_change,
                        total_num_ratio, avg_free_shares, avg_hold_amt, notice_date}}
    """
    result: Dict[str, Dict] = {}

    # 第一页(拿 count 算总页数)
    first_page, count = await asyncio.to_thread(_fetch_page, 1)
    if not first_page:
        return {}

    for row in first_page:
        parsed = _parse_row(row)
        if parsed:
            result[parsed[0]] = parsed[1]

    if count <= 100:
        return result

    # 并发拉取剩余页(每批5页,Semaphore 控并发,参考 fetch_batch_fund_flow 模式)
    total_pages = (count + 99) // 100
    remaining = list(range(2, total_pages + 1))
    batch_size = 5
    for i in range(0, len(remaining), batch_size):
        batch = remaining[i:i + batch_size]
        pages = await asyncio.gather(
            *[asyncio.to_thread(_fetch_page, pn) for pn in batch],
            return_exceptions=True
        )
        for page_result in pages:
            if isinstance(page_result, Exception):
                continue
            rows, _ = page_result
            for row in rows:
                parsed = _parse_row(row)
                if parsed:
                    result[parsed[0]] = parsed[1]

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
