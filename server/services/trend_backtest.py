"""trend_backtest.py - 趋势评分回测验证闭环（2026-07-01）

让趋势评分系统能"回头看自己"：
1. 每次趋势扫描入选的趋势股作为"预测记录"落库（在 trend.py 中调用 save_trend_backtest_records）
2. 每日 15:30 定时任务对历史预测做 N 日（1/3/5 日）涨跌回测
3. 跑赢基准判定：is_correct_Nd = actual_change_Nd > market_change_Nd（严格大于）
   基准风格化（2026-09-04）：主板→上证指数，创业板(300/301)→创业板指，科创板(688)→科创50
4. 按 predict_date 生成聚合报告，含三周期总胜率 + 分数段胜率
5. 补验机制：每次回测扫描所有 verified_Nd=0 的记录（不限日期），避免漏验
"""
import asyncio
from datetime import (date, datetime)
from typing import List, Dict, Optional

from app.config import (SCAN_KLINE_TIMEOUT, TREND_BACKTEST_PERIODS,
                        TREND_BACKTEST_KLINE_CONCURRENCY, TREND_BACKTEST_MARKET_SYMBOL,
                        TREND_BACKTEST_SCORE_SEGMENTS)
from db.database import (load_trend_backtest_records_pending_verification,
                         load_trend_backtest_records, update_trend_backtest_verification,
                         upsert_trend_backtest_report)
from services.stock import get_kline_data


def _is_weekend(date_str: str) -> bool:
    """判断日期字符串是否为周末（周六=5, 周日=6）"""
    try:
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        return dt.weekday() >= 5
    except Exception:
        return False


def _get_style_benchmark_symbol(symbol: str) -> str:
    """按股票所属板块匹配风格化基准指数（2026-09-04）

    主板(60/00) → 上证指数；创业板(300/301) → 创业板指；科创板(688) → 科创50。
    同板块基准剔除板块风格涨跌，使"跑赢基准"反映个股真实alpha。
    """
    code = symbol[2:] if len(symbol) > 2 and symbol[:2] in ('sh', 'sz', 'bj') else symbol
    if code.startswith('300') or code.startswith('301'):
        return 'sz399006'
    if code.startswith('688'):
        return 'sh000688'
    return TREND_BACKTEST_MARKET_SYMBOL


def _calc_n_day_change(kline: List[List], predict_date: str, n: int) -> Optional[float]:
    """计算个股 N 日累计涨跌（%）

    Args:
        kline: K 线数据，格式 [[date, open, close, high, low, volume], ...]
        predict_date: 预测日期 'YYYY-MM-DD'
        n: 回测周期（交易日数）

    Returns:
        (close[D+n] / close[D] - 1) * 100，若数据不足返回 None
    """
    if not kline or n <= 0:
        return None

    # 在 K 线中查找 predict_date 的索引
    target_idx = None
    for i, k in enumerate(kline):
        if k[0] == predict_date:
            target_idx = i
            break

    if target_idx is None:
        # predict_date 不在 K 线中（可能停牌），尝试向前找最近交易日
        for i, k in enumerate(kline):
            if k[0] <= predict_date:
                target_idx = i
            else:
                break
        if target_idx is None:
            return None

    # 检查是否有足够的后续 K 线
    if target_idx + n >= len(kline):
        return None  # 未到验证时点（交易日数不足）

    try:
        close_d = float(kline[target_idx][2])
        close_dn = float(kline[target_idx + n][2])
        if close_d <= 0:
            return None
        return (close_dn / close_d - 1) * 100
    except (ValueError, IndexError, TypeError):
        return None


async def _fetch_kline_batch(symbols: List[str]) -> Dict[str, Optional[List[List]]]:
    """批量并发拉取 K 线数据

    复用 trend.py 的 Semaphore + wait_for + gather 模式。
    返回 {symbol: kline_data} 字典，失败的 symbol 值为 None。
    """
    if not symbols:
        return {}

    sem = asyncio.Semaphore(TREND_BACKTEST_KLINE_CONCURRENCY)
    result: Dict[str, Optional[List[List]]] = {}

    async def _fetch_one(symbol: str):
        async with sem:
            try:
                kline = await asyncio.wait_for(
                    get_kline_data(symbol), timeout=SCAN_KLINE_TIMEOUT
                )
                result[symbol] = kline
            except Exception as e:
                print(f'[趋势回测] 拉取 K 线失败 {symbol}: {e}')
                result[symbol] = None

    await asyncio.gather(*[_fetch_one(s) for s in symbols])
    return result


async def _generate_trend_backtest_report(predict_date: str) -> Dict:
    """为指定 predict_date 生成/更新聚合报告

    统计三周期总胜率 + 分数段（60-70/70-80/80+）胜率，调用 upsert_trend_backtest_report 落库。
    """
    records = load_trend_backtest_records(predict_date=predict_date, limit=10000)
    if not records:
        return {'predict_date': predict_date, 'total_count': 0}

    total_count = len(records)
    period_stats = {}
    for period in ['1d', '3d', '5d']:
        verified = sum(1 for r in records if r.get(f'verified_{period}') == 1)
        win = sum(1 for r in records if r.get(f'is_correct_{period}') == 1)
        win_rate = round(win / verified, 4) if verified > 0 else 0.0
        period_stats[period] = {'verified': verified, 'win': win, 'win_rate': win_rate}

    # 分数段统计
    score_segment_stats = {}
    for seg_name, (lo, hi) in zip(['60_70', '70_80', '80_plus'], TREND_BACKTEST_SCORE_SEGMENTS):
        seg_records = [r for r in records if lo <= r.get('score', 0) < hi]
        score_segment_stats[seg_name] = {
            'total': len(seg_records),
            'win_1d': sum(1 for r in seg_records if r.get('is_correct_1d') == 1),
            'win_3d': sum(1 for r in seg_records if r.get('is_correct_3d') == 1),
            'win_5d': sum(1 for r in seg_records if r.get('is_correct_5d') == 1),
        }

    upsert_trend_backtest_report(predict_date, total_count, period_stats, score_segment_stats)
    return {
        'predict_date': predict_date,
        'total_count': total_count,
        'period_stats': period_stats,
        'score_segment_stats': score_segment_stats,
    }


async def backtest_trend_predictions() -> Dict:
    """趋势回测主入口

    流程：
    1. 周末跳过（非交易日不回测）
    2. 查询所有 verified_Nd=0 的记录，按 predict_date 分组
    3. 收集所有待验证 symbol，去重后批量并发拉 K 线
    4. 对每条记录计算 N 日累计涨跌 vs 大盘涨跌
    5. 判定 is_correct = actual_change > market_change（严格大于）
    6. 更新记录 + 生成聚合报告
    7. 返回统计摘要
    """
    today_str = date.today().isoformat()

    # 周末不执行回测（K 线无新数据）
    if _is_weekend(today_str):
        return {
            'skipped': True,
            'reason': '周末不执行回测',
            'today': today_str,
            'verified_total': 0,
        }

    # 1. 加载所有待验证记录
    pending_records = load_trend_backtest_records_pending_verification()
    if not pending_records:
        return {
            'skipped': True,
            'reason': '无待验证记录',
            'today': today_str,
            'verified_total': 0,
        }

    # 2. 按 predict_date 分组
    by_predict_date: Dict[str, List[Dict]] = {}
    for r in pending_records:
        pd = r.get('predict_date', '')
        if not pd:
            continue
        by_predict_date.setdefault(pd, []).append(r)

    # 3. 收集所有待验证 symbol + 风格化基准指数，去重后批量拉 K 线
    all_symbols = set()
    for records in by_predict_date.values():
        for r in records:
            sym = r.get('symbol', '')
            if sym:
                all_symbols.add(sym)
    # 风格化基准：主板→上证指数，创业板→创业板指，科创板→科创50
    benchmark_symbols = {_get_style_benchmark_symbol(s) for s in all_symbols}
    benchmark_symbols.add(TREND_BACKTEST_MARKET_SYMBOL)  # 兜底大盘基准
    all_symbols |= benchmark_symbols

    print(f'[趋势回测] 待验证 {len(pending_records)} 条记录，{len(by_predict_date)} 个预测日，拉取 {len(all_symbols)} 只 K 线...')
    kline_map = await _fetch_kline_batch(list(all_symbols))
    benchmark_kline_map = {bs: kline_map.get(bs) for bs in benchmark_symbols}
    market_kline = benchmark_kline_map.get(TREND_BACKTEST_MARKET_SYMBOL)

    # 4. 逐条验证
    verified_count = 0
    win_count = {'1d': 0, '3d': 0, '5d': 0}
    skip_count = 0

    for predict_date, records in by_predict_date.items():
        for r in records:
            symbol = r.get('symbol', '')
            stock_kline = kline_map.get(symbol)
            if not stock_kline:
                skip_count += 1
                continue

            # 对每个周期判断是否到验证时点
            for n in TREND_BACKTEST_PERIODS:
                period = f'{n}d'
                # 已验证的跳过
                if r.get(f'verified_{period}') == 1:
                    continue

                # 计算 N 日涨跌
                actual_change = _calc_n_day_change(stock_kline, predict_date, n)
                if actual_change is None:
                    continue  # 未到验证时点

                # 风格化基准 N 日涨跌（同板块指数，拉取失败降级大盘）
                bench_kline = benchmark_kline_map.get(_get_style_benchmark_symbol(symbol)) or market_kline
                market_change = 0.0
                if bench_kline:
                    mc = _calc_n_day_change(bench_kline, predict_date, n)
                    if mc is not None:
                        market_change = mc

                # 跑赢大盘判定（严格大于）
                is_correct = actual_change > market_change

                # 更新记录
                update_trend_backtest_verification(
                    predict_date, symbol, period,
                    actual_change, market_change, is_correct
                )
                verified_count += 1
                if is_correct:
                    win_count[period] += 1

    # 5. 为每个 predict_date 生成/更新聚合报告
    report_summaries = []
    for predict_date in by_predict_date.keys():
        try:
            rep = await _generate_trend_backtest_report(predict_date)
            report_summaries.append(rep)
        except Exception as e:
            print(f'[趋势回测] 生成报告失败 {predict_date}: {e}')

    summary = {
        'today': today_str,
        'pending_total': len(pending_records),
        'verified_total': verified_count,
        'skipped_no_kline': skip_count,
        'win_count': win_count,
        'win_rate': {
            '1d': round(win_count['1d'] / verified_count, 4) if verified_count > 0 else 0,
            '3d': round(win_count['3d'] / verified_count, 4) if verified_count > 0 else 0,
            '5d': round(win_count['5d'] / verified_count, 4) if verified_count > 0 else 0,
        },
        'reports_updated': len(report_summaries),
    }
    print(f'[趋势回测] 完成：验证 {verified_count} 条，跳过 {skip_count} 条，更新 {len(report_summaries)} 份报告')
    return summary
