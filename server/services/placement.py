"""placement.py - 近期增发（定增/非公开发行等）公告识别（2026-09-30 新增）

数据源：东方财富公告接口（np-anotice-stock.eastmoney.com）。
用于给筛选/推荐结果打"近期增发"标签：按股票代码批量拉取近期公告，
过滤标题含增发/定增关键词且发生在窗口内的公告。

使用限制：
- 公告接口 page_size 上限 100，每页约覆盖 10 只股票 15~20 天，
  故采用「按批翻页」策略，直到该批的公告日期早于窗口边界。
- 接口对同一次请求的 q/searchkey 参数无效，只能客户端过滤标题。
"""
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests

# 东财公告接口（与收评/龙虎榜同源）
_ANNOUNCE_API = 'https://np-anotice-stock.eastmoney.com/api/security/ann'

# 命中关键词：覆盖广义"增发"（定向增发/公开增发/非公开发行/向特定对象发行）
# 注：注册制下定增公告标题常用"向特定对象发行A股股票"措辞，必须包含。
_HIT_KEYWORDS = ('增发', '定增', '非公开发行', '向特定对象发行')
# 排除词：可交换公司债券是大股东发债换股，不是上市公司增发；
# 终止/中止/撤回/取消 表示增发事项已取消或搁置，不应打"近期增发"标签。
_EXCLUDE_KEYWORDS = ('可交换公司债券', '可交换债', '换股期', '终止', '中止', '撤回', '取消')

_PAGE_SIZE = 100          # 接口单页上限
_MAX_PAGES = 4            # 单批最多翻页数（防御死循环）
_TIMEOUT = 10             # 单次请求超时（秒）
_BATCH_SIZE = 20          # 每次并发查询的股票数
_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://data.eastmoney.com/',
}


def _norm_symbol(symbol: str) -> str:
    """把 sh600519 / 600519 统一成 600519"""
    return symbol[-6:] if symbol else symbol


def _in_window(date_str: str, cutoff: datetime) -> bool:
    """判断公告日期是否在窗口内（notice_date 形如 '2026-09-05 00:00:00'）"""
    try:
        d = datetime.strptime(date_str[:10], '%Y-%m-%d')
    except (ValueError, TypeError):
        return False
    return cutoff <= d <= datetime.now()


def _fetch_batch_announcements(codes: List[str], cutoff: datetime) -> List[Dict]:
    """批量拉取指定股票在窗口内的公告（自动翻页直到覆盖窗口边界）。

    返回 [{stock_code, title, notice_date}]，仅含窗口内的记录。
    """
    stock_list = ','.join(codes)
    results: List[Dict] = []
    for page in range(1, _MAX_PAGES + 1):
        params = {
            'sr': '-1', 'page_size': str(_PAGE_SIZE), 'page_index': str(page),
            'ann_type': 'A', 'client_source': 'web', 'stock_list': stock_list,
            'f_node': '0', 's_node': '0',
        }
        try:
            resp = requests.get(_ANNOUNCE_API, params=params, headers=_HEADERS, timeout=_TIMEOUT)
            resp.raise_for_status()
            lst = (resp.json().get('data') or {}).get('list') or []
        except Exception:
            # 单页失败即放弃本批（降级为空），不影响推荐主流程
            break
        if not lst:
            break
        for it in lst:
            date = (it.get('notice_date') or '')[:10]
            if date and date < cutoff.strftime('%Y-%m-%d'):
                # 已早于窗口边界，说明本页数据越界；但后续条目只会更早
                return results
            codes_meta = it.get('codes') or []
            stock_code = codes_meta[0].get('stock_code', '') if codes_meta else ''
            results.append({
                'stock_code': stock_code,
                'title': it.get('title', ''),
                'notice_date': date,
            })
        # 若本页未填满（说明公告已取完），无需继续翻页
        if len(lst) < _PAGE_SIZE:
            break
    return results


def fetch_recent_placements(symbols: List[str], days: int = 30) -> Dict[str, Dict]:
    """获取最近 N 天内公布过增发（定增/非公开发行等）公告的股票。

    Args:
        symbols: 股票代码列表（支持 sh600519 或 600519 格式）
        days: 窗口天数（默认 30）

    Returns:
        {symbol: {'title': str, 'notice_date': 'YYYY-MM-DD'}}
        仅在命中的股票出现在结果中；接口异常时返回空 dict（优雅降级）。
    """
    if not symbols:
        return {}
    cutoff = datetime.now() - timedelta(days=days)
    hit_map: Dict[str, Dict] = {}
    # 去重并按批查询
    uniq = sorted({_norm_symbol(s) for s in symbols if s})
    for i in range(0, len(uniq), _BATCH_SIZE):
        batch = uniq[i:i + _BATCH_SIZE]
        announcements = _fetch_batch_announcements(batch, cutoff)
        for ann in announcements:
            title = ann['title'] or ''
            if not any(k in title for k in _HIT_KEYWORDS):
                continue
            if any(k in title for k in _EXCLUDE_KEYWORDS):
                continue
            code = ann['stock_code']
            if code and code not in hit_map:
                hit_map[code] = {'title': title, 'notice_date': ann['notice_date']}
    return hit_map
