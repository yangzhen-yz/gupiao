"""fundamental.py - 基本面质量数据服务（2026-07-16 P1a）

数据源:东方财富 datacenter API RPT_LICO_FN_CPD（核心财务指标）
- 季度披露，filter=(ISNEW="1") 只取每只股票最新一期
- 全市场约 11000+ 条（含 A/B 股/多报告类型），按 symbol 去重取最新报告期
- 关键字段:WEIGHTAVG_ROE(加权平均ROE%), SJLTZ(归母净利润同比增长率%), REPORTDATE, BASIC_EPS
- 每日 18:00 定时拉取最新一期存 DB（stock_fundamental 表），评分时从 DB 批量查
- 参考模式:services/shareholder.py（同端点、同 ISNEW 过滤、同分页拉取）
"""
import asyncio
from typing import List, Dict, Tuple, Optional
from datetime import datetime
import requests

from db.database import save_fundamental_batch, load_fundamental_batch
from services.shareholder import _code_to_symbol


FUND_API = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
FUND_HEADERS = {
    'User-Agent': 'Mozilla/5.0',
    'Referer': 'https://data.eastmoney.com/'
}
FUND_REPORT = 'RPT_LICO_FN_CPD'
# 只取最新一期；字段:代码/名称/报告期/加权ROE/归母净利/净利同比/营收同比/基本EPS
FUND_COLUMNS = 'SECURITY_CODE,SECURITY_NAME_ABBR,REPORTDATE,WEIGHTAVG_ROE,PARENT_NETPROFIT,SJLTZ,YSTZ,BASIC_EPS'


def _fetch_page(pn: int) -> Tuple[List[Dict], int]:
    """同步拉取单页(100条)基本面数据,返回 (rows, total_count)

    用 requests + asyncio.to_thread 包装(项目硬约束:禁用 httpx 直连东方财富)
    """
    params = {
        'reportName': FUND_REPORT,
        'columns': FUND_COLUMNS,
        'filter': '(ISNEW="1")',
        'pageNumber': pn,
        'pageSize': 100,
        'ut': 'b2884a393a59ad64002292a3e90d46a5',
    }
    r = requests.get(FUND_API, params=params, headers=FUND_HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not data.get('success'):
        return [], 0
    res = data.get('result') or {}
    return res.get('data') or [], res.get('count') or 0


def _parse_row(row: Dict) -> Optional[Tuple[str, Dict]]:
    """解析单条记录,返回 (symbol, info_dict) 或 None(不支持的代码/解析失败)"""
    code = str(row.get('SECURITY_CODE', ''))
    symbol = _code_to_symbol(code)
    if not symbol:
        return None
    try:
        roe = float(row['WEIGHTAVG_ROE']) if row.get('WEIGHTAVG_ROE') not in (None, '') else None
    except (TypeError, ValueError):
        roe = None
    try:
        yoy = float(row['SJLTZ']) if row.get('SJLTZ') not in (None, '') else None
    except (TypeError, ValueError):
        yoy = None
    try:
        eps = float(row['BASIC_EPS']) if row.get('BASIC_EPS') not in (None, '') else None
    except (TypeError, ValueError):
        eps = None
    report_date = (str(row.get('REPORTDATE') or '') or '')[:10]
    return (symbol, {
        'symbol': symbol,
        'report_date': report_date,
        'roe': roe,
        'net_profit_yoy': yoy,
        'eps': eps,
    })


async def fetch_all_fundamentals() -> Dict[str, Dict]:
    """全市场分页拉取最新一期基本面(按 symbol 去重,保留最新报告期)

    Returns:
        {symbol_lower: {symbol, report_date, roe, net_profit_yoy, eps}}
    """
    result: Dict[str, Dict] = {}

    first_page, count = await asyncio.to_thread(_fetch_page, 1)
    if not first_page:
        return {}

    def _upsert(rows):
        for row in rows:
            parsed = _parse_row(row)
            if not parsed:
                continue
            sym, info = parsed
            # 去重:同 symbol 保留报告期更晚的一条
            if sym in result:
                if info['report_date'] > result[sym].get('report_date', ''):
                    result[sym] = info
            else:
                result[sym] = info

    _upsert(first_page)

    if count <= 100:
        return result

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
            _upsert(rows)

    return result


def get_fundamental_batch(symbols: List[str]) -> Dict[str, Dict]:
    """从 DB 批量查最新基本面(供评分注入,同步函数)

    Args:
        symbols: 股票代码列表(小写带前缀 sh600519)
    Returns:
        {symbol: {report_date, roe, net_profit_yoy, eps}}
    """
    return load_fundamental_batch(symbols)


async def update_fundamental_task():
    """定时任务:全市场拉取最新基本面并存 DB(每日 18:00 触发)"""
    try:
        print('[定时任务] 正在更新基本面质量数据...')
        data = await fetch_all_fundamentals()
        if not data:
            print('[定时任务] 基本面数据为空,跳过')
            return
        updated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        records = []
        for symbol, info in data.items():
            info['updated_at'] = updated_at
            records.append(info)
        saved = save_fundamental_batch(records)
        if saved:
            print(f'[定时任务] 基本面质量数据更新完成: {len(records)} 条')
        else:
            print('[定时任务] 基本面质量数据保存失败')
    except Exception as error:
        print(f'[定时任务] 更新基本面失败: {str(error)}')
