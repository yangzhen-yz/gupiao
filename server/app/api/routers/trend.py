"""Domain router: trend."""

from fastapi import APIRouter
from ..routes_state import *

router = APIRouter()

@router.post("/api/scan-ma10", tags=["趋势发现"])
async def scan_ma10_api():
    """手动触发10日线检查（同时检查用户池和趋势池），便于立即清理跌破10日线的股票"""
    try:
        await scan_and_remove_below_ma10()
        return {"success": True, "message": "已触发10日线检查任务，请查看服务器日志"}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"触发失败: {str(error)}")


@router.get("/api/trend-stocks", tags=["趋势发现"])
async def get_trend_scan_results():
    try:
        trend_data = load_trend_scan_results()
        trend_data = _ensure_today_date(trend_data)
        # V4.6: 为每只趋势股票注入买卖点（位置感知，trend_details 精确度最高）
        stocks = trend_data.get('stocks', [])
        for s in stocks:
            # 涨跌幅（2026-10-23）：新快照写在 details.changePercent；
            # 旧快照没有该字段时用 recent5Days[-2]（昨收）与 latestPrice 回退，
            # 二者同属一次扫描快照，保证价格与涨跌幅口径一致。
            change_pct = (s.get('details') or {}).get('changePercent')
            if change_pct is None:
                recent = s.get('recent5Days') or []
                price = s.get('latestPrice')
                try:
                    prev_close = float(recent[-2]) if len(recent) >= 2 else 0
                    if prev_close > 0 and price:
                        change_pct = (float(price) - prev_close) / prev_close * 100
                except (TypeError, ValueError):
                    change_pct = None
            try:
                s['changePercent'] = round(float(change_pct), 2) if change_pct is not None else None
            except (TypeError, ValueError):
                s['changePercent'] = None
            try:
                details = s.get('details', {}) or {}
                score_result = {'total': s.get('score', 0), 'base_scores': {}, 'factor_values': {}}
                s['buySellPoints'] = _recommend_svc.calc_buy_sell_points(s, score_result=score_result, trend_details=details)
            except Exception as _e:
                print(f'[趋势] 买卖点计算失败 {s.get("symbol")}: {_e}')
        return {"success": True, "data": trend_data}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取趋势股票失败: {str(error)}")


@router.get("/api/trend/history-counts", tags=["趋势发现"])
async def get_trend_history_counts(days: int = 30):
    """趋势扫描历史数量统计（市场温度计：每日扫描数/符合数，辅助判断减仓加仓时机）"""
    try:
        conn = get_db_conn()
        c = conn.cursor()
        c.execute('''
            SELECT
                scan_date,
                SUM(CASE WHEN symbol != '__SCAN_EMPTY__' THEN 1 ELSE 0 END) as result_count,
                MIN(total_scanned) as scanned_count,
                ROUND(AVG(CASE WHEN symbol != '__SCAN_EMPTY__' THEN score ELSE NULL END), 1) as avg_score,
                MAX(CASE WHEN symbol != '__SCAN_EMPTY__' THEN score ELSE 0 END) as max_score,
                SUM(CASE WHEN symbol != '__SCAN_EMPTY__' AND score >= 80 THEN 1 ELSE 0 END) as count_80plus,
                SUM(CASE WHEN symbol != '__SCAN_EMPTY__' AND score >= 55 THEN 1 ELSE 0 END) as count_55plus
            FROM trend_scan_results
            WHERE scan_date IS NOT NULL AND scan_date != ''
            GROUP BY scan_date
            ORDER BY scan_date DESC
            LIMIT ?
        ''', (days * 3,))
        rows = c.fetchall()
        conn.close()
        data = [{
            'date': r['scan_date'],
            'scanned': r['scanned_count'] or 0,
            'result_count': r['result_count'],
            'avg_score': r['avg_score'] or 0,
            'max_score': r['max_score'] or 0,
            'count_80plus': r['count_80plus'] or 0,
            'count_55plus': r['count_55plus'] or 0,
        } for r in rows]
        # 仅保留交易日（排除周末与法定节假日），再截取所需条数
        data = [d for d in data if is_trading_day(d['date'])][:days]
        return {"success": True, "data": data}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取趋势历史统计失败: {str(error)}")


@router.get("/api/scan-trend-stocks", tags=["趋势发现"])
async def scan_trend_stocks(force: bool = False):
    try:
        result = await scan_trend_scan_results(force=force)
        return {"success": True, "data": _ensure_today_date(result.get("data", result))}
    except Exception as error:
        # 2026-07-14 修复：500 时打印完整 traceback，否则只看到 "扫描趋势股票失败: xxx" 无法定位
        import traceback as _tb
        print(f'[API] /api/scan-trend-stocks 500: {type(error).__name__}: {error}')
        print(f'[API] /api/scan-trend-stocks traceback:\n{_tb.format_exc()}')
        raise HTTPException(status_code=500, detail=f"扫描趋势股票失败: {str(error)}")


@router.post("/api/trend/backtest", tags=["趋势发现"])
async def run_trend_backtest():
    """手动触发趋势回测验证（扫描所有 verified=0 的历史记录）"""
    try:
        result = await backtest_trend_predictions()
        return {"success": True, "data": result}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"趋势回测失败: {str(error)}")


@router.get("/api/trend/backtest-history", tags=["趋势发现"])
async def get_trend_backtest_history(limit: int = 30):
    """获取趋势回测历史报告列表"""
    try:
        reports = load_trend_backtest_reports(limit=limit)
        return {"success": True, "data": reports}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取回测历史失败: {str(error)}")


@router.get("/api/trend/backtest-latest", tags=["趋势发现"])
async def get_trend_backtest_latest():
    """获取最新一条趋势回测报告（前端徽章用）"""
    try:
        report = load_latest_trend_backtest_report()
        return {"success": True, "data": report}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取最新回测报告失败: {str(error)}")


@router.get("/api/trend/backtest-records", tags=["趋势发现"])
async def get_trend_backtest_records(predict_date: str = None, symbol: str = None, limit: int = 100):
    """获取趋势回测明细记录（支持按日期/symbol 过滤）"""
    try:
        records = load_trend_backtest_records(predict_date=predict_date, symbol=symbol, limit=limit)
        return {"success": True, "data": records}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取回测明细失败: {str(error)}")


@router.get("/api/stock-diagnose/{symbol}", tags=["五层诊断"])
async def stock_diagnose_api(symbol: str):
    """五层本地诊断书（2026-10-20，替代已删除的 AI 诊断）：位置/资金/环境/系统历史/纪律价，
    纯本地库聚合＋规则判据，不接任何大模型；DB 密集读取放线程池，不压事件循环。"""
    if not re.match(r'^(sh|sz)?\d{6}$', symbol.lower()):
        raise HTTPException(status_code=400, detail="无效的股票代码格式")
    from services.support.diagnose import diagnose_stock
    try:
        return await asyncio.to_thread(diagnose_stock, symbol)
    except Exception as error:
        print(f'[五层诊断] 失败 {symbol}: {error}')
        raise HTTPException(status_code=500, detail=f"诊断失败: {str(error)}")


@router.get("/api/trend/backtest-summary", tags=["趋势发现"])
async def get_trend_backtest_summary(limit: int = 30):
    """趋势回测策略自检验汇总（P2b 增强）。

    聚合所有回测报告：总样本数、1/3/5 日加权胜率、平均超额收益
    （个股 N 日涨跌 − 大盘 N 日涨跌，来自 trend_backtest_records）、近期报告列表。
    让"回头看自己"的闭环对前端可见、可量化。
    """
    try:
        reports = load_trend_backtest_reports(limit=limit)
        if not reports:
            return {"success": True, "data": {"hasData": False}}
        # 跨报告加权胜率（按各周期验证样本数加权）
        agg = {'verified_1d': 0, 'win_1d': 0, 'verified_3d': 0, 'win_3d': 0,
               'verified_5d': 0, 'win_5d': 0, 'total_count': 0, 'reports': 0}
        for r in reports:
            agg['reports'] += 1
            agg['total_count'] += r.get('total_count', 0) or 0
            for p in ('1d', '3d', '5d'):
                agg[f'verified_{p}'] += r.get(f'verified_{p}_count', 0) or 0
                agg[f'win_{p}'] += r.get(f'win_{p}_count', 0) or 0

        def _wr(w, v):
            return round(w / v * 100, 1) if v > 0 else 0.0

        win_rates = {'1d': _wr(agg['win_1d'], agg['verified_1d']),
                     '3d': _wr(agg['win_3d'], agg['verified_3d']),
                     '5d': _wr(agg['win_5d'], agg['verified_5d'])}

        # 平均超额收益（个股 N 日涨跌 − 大盘 N 日涨跌），来自明细记录
        conn = get_db_conn()
        c = conn.cursor()
        excess = {}
        for p in ('1d', '3d', '5d'):
            c.execute(
                f'''SELECT AVG(actual_change_{p} - market_change_{p})
                    FROM trend_backtest_records WHERE verified_{p} = 1
                    AND actual_change_{p} IS NOT NULL AND market_change_{p} IS NOT NULL''')
            row = c.fetchone()
            val = row[0] if row and row[0] is not None else 0.0
            excess[p] = round(float(val), 2)
        conn.close()

        return {
            "success": True,
            "data": {
                "hasData": True,
                "reports": agg['reports'],
                "totalSamples": agg['total_count'],
                "verified": {'1d': agg['verified_1d'], '3d': agg['verified_3d'], '5d': agg['verified_5d']},
                "winRates": win_rates,
                "avgExcessReturn": excess,
                "recent": [
                    {
                        'predict_date': r.get('predict_date'),
                        'total_count': r.get('total_count', 0),
                        'win_rate_1d': round(r.get('win_rate_1d', 0) * 100, 1),
                        'win_rate_3d': round(r.get('win_rate_3d', 0) * 100, 1),
                        'win_rate_5d': round(r.get('win_rate_5d', 0) * 100, 1),
                    } for r in reports[:10]
                ],
            }
        }
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取回测汇总失败: {str(error)}")

