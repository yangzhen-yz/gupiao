"""valuation.py - 估值分位过滤（P2c，2026-07-16 新增）

全市场 PE(TTM)/PB 百分位排名，作为"估值分位过滤"维度：
- 复用 fundflow._clist 拉取全市场 f9(PE TTM) / f23(PB)
- 本地计算每只股票的 PE/PB 百分位（0~100，越高越贵）
- 落库 stock_valuation，每日 18:10 盘后更新
- 提供 get_valuation_batch() 供评分/推荐/扫描注入；is_overvalued() 用于过滤极端高估股

设计要点：
- 亏损股 PE<=0 无法排名，pe_pct 置 0（不会被 PE 维度判为"高估"）；
  极端高估需 PE 与 PB 百分位"双高"才触发，避免单维度误杀。
- 百分位 = 全市场中"估值不高于该股"的占比，与 RPS(越高越强) 相反（估值越高越贵）。
"""
import asyncio
from datetime import datetime
from typing import Dict, List, Optional

from app.config import (VALUATION_ENABLED, VALUATION_OVERVALUED_PE_PCT,
                        VALUATION_OVERVALUED_PB_PCT)
from db.database import save_valuation_batch, load_valuation_batch
# 复用 fundflow 的 clist 封装（已含 3 次重试 + 限流规避 + 全市场 fs 组合）
from services.fundflow import _clist, _FS_ALL_A

_VAL_FIELDS = 'f12,f13,f9,f23'   # 代码, 市场前缀, PE(TTM), PB
_VAL_PAGE = 100                  # clist 单页硬上限


def _normalize(item: Dict) -> Optional[Dict]:
    """将 clist 单行转为 {symbol, pe, pb}；字段缺失/异常返回 None。"""
    code = item.get('f12', '') or ''
    if not code:
        return None
    market = 'sh' if item.get('f13') == 1 else 'sz'
    symbol = f'{market}{code}'.lower()

    def _f(v):
        try:
            if v in (None, '', '-'):
                return 0.0
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    pe = _f(item.get('f9'))
    pb = _f(item.get('f23'))
    return {'symbol': symbol, 'pe': pe, 'pb': pb}


def fetch_all_valuation() -> List[Dict]:
    """分页拉取全市场 PE/PB（复用 _FS_ALL_A = 沪A+深A）。

    Returns:
        [{symbol, pe, pb}, ...]（未做百分位，原始值）
    """
    if not VALUATION_ENABLED:
        return []
    rows: List[Dict] = []
    pn = 1
    while True:
        try:
            diff = _clist('f9', 1, _FS_ALL_A, _VAL_FIELDS, _VAL_PAGE, pn)
        except Exception as e:
            print(f'[估值] 第 {pn} 页拉取失败: {e}')
            break
        if not diff:
            break
        for it in diff:
            norm = _normalize(it)
            if norm:
                rows.append(norm)
        if len(diff) < _VAL_PAGE:
            break
        pn += 1
        # 轻量限速，规避东方财富限流
        import time
        time.sleep(0.2)
    print(f'[估值] 全市场拉取完成，共 {len(rows)} 只（{pn} 页）')
    return rows


def _compute_percentiles(rows: List[Dict]) -> List[Dict]:
    """计算 PE/PB 百分位（0~100，越高越贵），亏损股 PE 置 0。

    百分位 = 全市场中估值 ≤ 该股 的比例。
    """
    # PE：仅对正 PE 排名；PB：对所有正 PB 排名（PB 恒正，亏损股也有 PB）
    pe_valid = [r['pe'] for r in rows if r['pe'] > 0]
    pb_valid = [r['pb'] for r in rows if r['pb'] > 0]
    pe_valid_sorted = sorted(pe_valid)
    pb_valid_sorted = sorted(pb_valid)
    pe_total = len(pe_valid_sorted)
    pb_total = len(pb_valid_sorted)

    def _pe_pct(v: float) -> float:
        if v <= 0 or pe_total == 0:
            return 0.0
        # bisect_right：大于 v 的数量，pct = (total - gt)/total*100
        import bisect
        gt = bisect.bisect_right(pe_valid_sorted, v)
        return round((pe_total - gt) / pe_total * 100, 1)

    def _pb_pct(v: float) -> float:
        if v <= 0 or pb_total == 0:
            return 0.0
        import bisect
        gt = bisect.bisect_right(pb_valid_sorted, v)
        return round((pb_total - gt) / pb_total * 100, 1)

    for r in rows:
        r['pe_pct'] = _pe_pct(r['pe'])
        r['pb_pct'] = _pb_pct(r['pb'])
    return rows


def update_valuation_task() -> Dict:
    """全市场估值预计算主入口（定时任务 / 手动触发）。

    拉取 → 算百分位 → 落库 stock_valuation。立即返回统计摘要。
    """
    if not VALUATION_ENABLED:
        return {'enabled': False}
    t0 = datetime.now()
    rows = fetch_all_valuation()
    if not rows:
        return {'updated': 0, 'message': '无估值数据'}
    rows = _compute_percentiles(rows)
    now = t0.strftime('%Y-%m-%d %H:%M:%S')
    for r in rows:
        r['updated_at'] = now
    saved = save_valuation_batch(rows)
    over = sum(1 for r in rows if r['pe_pct'] >= VALUATION_OVERVALUED_PE_PCT
               and r['pb_pct'] >= VALUATION_OVERVALUED_PB_PCT)
    print(f'[估值] 预计算完成：{saved} 只落库，极端高估(双90%) {over} 只，'
          f'耗时 {(datetime.now() - t0).total_seconds():.1f}s')
    return {
        'updated': saved,
        'total': len(rows),
        'overvalued': over,
        'pe_pct_threshold': VALUATION_OVERVALUED_PE_PCT,
        'pb_pct_threshold': VALUATION_OVERVALUED_PB_PCT,
        'cost_seconds': round((datetime.now() - t0).total_seconds(), 1),
    }


def get_valuation_batch(symbols: List[str]) -> Dict[str, Dict]:
    """批量从 DB 加载估值分位（{symbol: {pe, pb, pe_pct, pb_pct, overvalued}}）。"""
    if not symbols:
        return {}
    raw = load_valuation_batch([s.lower() for s in symbols])
    result: Dict[str, Dict] = {}
    for r in raw:
        sym = r.get('symbol', '')
        pe_pct = r.get('pe_pct', 0) or 0
        pb_pct = r.get('pb_pct', 0) or 0
        result[sym] = {
            'pe': r.get('pe'),
            'pb': r.get('pb'),
            'pe_pct': pe_pct,
            'pb_pct': pb_pct,
            'overvalued': (pe_pct >= VALUATION_OVERVALUED_PE_PCT
                           and pb_pct >= VALUATION_OVERVALUED_PB_PCT),
        }
    return result


def is_overvalued(val: Optional[Dict]) -> bool:
    """判断单只股票是否极端高估（PE 与 PB 百分位双高）。"""
    if not val:
        return False
    return bool(val.get('overvalued'))
