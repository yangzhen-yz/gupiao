"""scheduler - 定时任务调度"""
import asyncio, schedule, threading, time
from datetime import datetime, timedelta

from app.config import (SCHEDULE_UPDATE_HOT_SEARCH_TIMES, SCHEDULE_MA10_CHECK_TIME, SCHEDULE_DAILY_REVIEW_TIME, SCHEDULE_BACKTEST_TIME, SCHEDULE_RECOMMENDATION_TIMES, SCHEDULE_TREND_SCAN_INTERVAL_MIN, SCHEDULE_TREND_SCAN_START, SCHEDULE_TREND_SCAN_END, SCHEDULE_TREND_BACKTEST_TIME, SHAREHOLDER_UPDATE_TIME, FUNDAMENTAL_UPDATE_TIME, VALUATION_UPDATE_TIME, SCHEDULE_MARKET_ALERT_TIMES, SMART_MONEY_UPDATE_TIME)

from services.stock import fetch_eastmoney_hot_list
from db.database import save_hot_search_ranking, save_daily_market_reviews
from services.trend import scan_and_remove_below_ma10, scan_trend_task, scan_trend_scan_results
from services.trend_backtest import backtest_trend_predictions
from services.review import generate_daily_review
from services.strategy import backtest_yesterday_predictions
from services.recommend import auto_generate_recommendations_task
from services.shareholder import update_holder_num_task
from services.fundamental import update_fundamental_task
from services.valuation import update_valuation_task
from services.market import run_scheduled_market_alert
from services.smartmoney import refresh_smart_money_task
from services.trading_calendar import is_trading_day


def _today_is_trading() -> bool:
    """今日是否为 A 股交易日（带日志）。非交易日跳过盘中任务，避免浪费 API 调用拉取 0 条数据。"""
    today = datetime.now().date()
    ok = is_trading_day(today)
    return ok


def _generate_interval_times(start_hhmm: str, end_hhmm: str, interval_min: int) -> list:
    """生成 [start, end] 区间内每 interval_min 分钟一个的 'HH:MM' 时间列表（闭区间，含 end）。
    例: _generate_interval_times('09:00', '15:00', 30) → ['09:00', '09:30', ..., '15:00']
    """
    fmt = '%H:%M'
    t = datetime.strptime(start_hhmm, fmt)
    end = datetime.strptime(end_hhmm, fmt)
    result = []
    while t <= end:
        result.append(t.strftime(fmt))
        t += timedelta(minutes=interval_min)
    return result


async def update_hot_search_ranking_task():
    try:
        print(f'[定时任务 {datetime.now():%H:%M:%S}] 正在更新热搜榜数据...')
        data = await fetch_eastmoney_hot_list()
        save_hot_search_ranking(data)
        print(f'[定时任务 {datetime.now():%H:%M:%S}] 热搜榜数据更新完成')
    except Exception as error:
        print(f'[定时任务 {datetime.now():%H:%M:%S}] 更新热搜榜失败: {str(error)}')
        import traceback
        print(f'  堆栈: {traceback.format_exc()[:300]}')


async def generate_review_task():
    # 收评只在交易日执行；非交易日没有当日收盘数据，生成无意义
    if not _today_is_trading():
        print(f'[定时任务 {datetime.now():%H:%M:%S}] 今日非交易日，跳过收评生成')
        return
    try:
        print(f'[定时任务 {datetime.now():%H:%M:%S}] 正在生成今日收评...')
        review = await generate_daily_review()
        ok = save_daily_market_reviews([review])
        if not ok:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 收评保存返回 False（详见 db 日志）')
            return
        print(f'[定时任务 {datetime.now():%H:%M:%S}] 今日收评生成完成！ 情绪分={review.get("aiAnalysis",{}).get("sentimentScore") if review.get("aiAnalysis") else "N/A"}')
    except Exception as error:
        print(f'[定时任务 {datetime.now():%H:%M:%S}] 生成收评失败: {str(error)}')
        import traceback
        print(f'  堆栈: {traceback.format_exc()[:500]}')


async def backtest_task():
    if not _today_is_trading():
        print(f'[定时任务 {datetime.now():%H:%M:%S}] 今日非交易日，跳过策略回测')
        return
    try:
        print(f'[定时任务 {datetime.now():%H:%M:%S}] 正在回测验证昨日预测...')
        result = await backtest_yesterday_predictions()
        if 'error' in result:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 回测失败: {result["error"]}')
        elif 'message' in result:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] {result["message"]}')
        else:
            # 2026-07-14 修复：使用 .get() 兜底，避免 KeyError（之前 result['bear_accuracy'] KeyError）
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 回测完成: 准确率{result.get("accuracy", 0)}%, '
                  f'看涨准确率{result.get("bull_accuracy", 0)}%, '
                  f'看跌准确率{result.get("bear_accuracy", 0)}%')
            if result.get('weight_adjustments'):
                for adj in result['weight_adjustments']:
                    if adj['action'] != 'keep':
                        print(f'  权重调整: {adj["factor"]} {adj["action"]} ({adj["reason"]})')
    except Exception as error:
        print(f'[定时任务 {datetime.now():%H:%M:%S}] 回测验证失败: {str(error)}')
        import traceback
        print(f'  堆栈: {traceback.format_exc()[:500]}')


def run_scheduler():
    def job_update_hot_search_ranking():
        asyncio.run(update_hot_search_ranking_task())

    def job_scan_ma10():
        if not _today_is_trading():
            return
        asyncio.run(scan_and_remove_below_ma10())

    def job_generate_review():
        asyncio.run(generate_review_task())

    def job_backtest():
        asyncio.run(backtest_task())

    def job_scan_trend():
        # 趋势扫描：非交易日也可允许（让前端看到历史缓存+标记休市），但 API 拉取不到实时数据
        # 为避免浪费，非交易日跳过
        if not _today_is_trading():
            return
        asyncio.run(scan_trend_task())

    def job_trend_backtest():
        if not _today_is_trading():
            return
        asyncio.run(backtest_trend_predictions())

    def job_generate_recommendations():
        if not _today_is_trading():
            return
        asyncio.run(auto_generate_recommendations_task())

    def job_update_holder_num():
        # 股东人数是月度数据，每日更新即可；非交易日也可拉取（上一交易日收盘后发布也有）
        asyncio.run(update_holder_num_task())

    def job_update_fundamental():
        # 基本面季度披露，非交易日可拉取
        asyncio.run(update_fundamental_task())

    def job_update_valuation():
        # 估值百分位（PE/PB），非交易日可拉取上一交易日数据
        asyncio.run(update_valuation_task())

    def job_market_alert():
        if not _today_is_trading():
            return
        asyncio.run(run_scheduled_market_alert())

    def job_update_smart_money():
        # 龙虎榜+北向：只有"最近已收盘交易日"有数据，非交易日也可以拉取（刷新到上一交易日）
        asyncio.run(refresh_smart_money_task())

    # ---- 注册定时任务 ----
    # 热搜榜：非交易日不跑，没意义
    def _warp_trading_only(fn):
        def _wrapped():
            if not _today_is_trading():
                return
            fn()
        return _wrapped

    for t in SCHEDULE_UPDATE_HOT_SEARCH_TIMES:
        schedule.every().day.at(t).do(_warp_trading_only(job_update_hot_search_ranking))

    schedule.every().day.at(SCHEDULE_MA10_CHECK_TIME).do(job_scan_ma10)

    schedule.every().day.at(SCHEDULE_DAILY_REVIEW_TIME).do(job_generate_review)

    schedule.every().day.at(SCHEDULE_BACKTEST_TIME).do(job_backtest)

    # 趋势发现自动扫描：交易日 9:00 ~ 15:00 每 30 分钟一次
    # 例：09:00, 09:30, 10:00, ..., 14:30, 15:00
    trend_scan_times = _generate_interval_times(
        SCHEDULE_TREND_SCAN_START,
        SCHEDULE_TREND_SCAN_END,
        SCHEDULE_TREND_SCAN_INTERVAL_MIN,
    )
    for t in trend_scan_times:
        schedule.every().day.at(t).do(job_scan_trend)

    # P4-1: 每天 15:30 对历史趋势预测做 N 日（1/3/5 日）回测验证
    schedule.every().day.at(SCHEDULE_TREND_BACKTEST_TIME).do(job_trend_backtest)

    # 新增：每天自动生成智能推荐股票
    for t in SCHEDULE_RECOMMENDATION_TIMES:
        schedule.every().day.at(t).do(job_generate_recommendations)

    # : 每天 18:00 更新股东人数筹码面数据（月度披露，每日刷新最新一期）
    schedule.every().day.at(SHAREHOLDER_UPDATE_TIME).do(job_update_holder_num)

    # : 每天 18:05 更新基本面质量数据（季度披露，每日刷新最新一期 ROE/净利润同比）
    schedule.every().day.at(FUNDAMENTAL_UPDATE_TIME).do(job_update_fundamental)

    # : 每天 18:10 更新估值分位数据（PE/PB 全市场百分位，每日盘后刷新）
    schedule.every().day.at(VALUATION_UPDATE_TIME).do(job_update_valuation)

    # 大盘风险预警：盘中多时段自动扫描，风险评分>=40 时保存预警供前端推送
    for t in SCHEDULE_MARKET_ALERT_TIMES:
        schedule.every().day.at(t).do(job_market_alert)

    # 交易日后定时拉取最新龙虎榜+北向资金数据，前端无需手动刷新
    schedule.every().day.at(SMART_MONEY_UPDATE_TIME).do(job_update_smart_money)

    _now = datetime.now()
    print(f'\n[定时任务 {_now:%Y-%m-%d %H:%M:%S}] 已注册以下定时任务（今日交易日={_today_is_trading()}）：')
    print(f'[定时任务]   - {"/".join(SCHEDULE_UPDATE_HOT_SEARCH_TIMES)}: 更新热搜榜（仅交易日）')
    print(f'[定时任务]   - {",".join(trend_scan_times)} (每{SCHEDULE_TREND_SCAN_INTERVAL_MIN}分钟): 自动扫描趋势股（仅交易日）')
    print(f'[定时任务]   - {SCHEDULE_MA10_CHECK_TIME}: 检查10日线并删除跌破股票（仅交易日）')
    print(f'[定时任务]   - {SCHEDULE_DAILY_REVIEW_TIME}: 自动生成每日收评（仅交易日，含 AI 情绪评分/操作建议）')
    print(f'[定时任务]   - {SCHEDULE_BACKTEST_TIME}: 回测验证昨日预测并调整策略权重（仅交易日）')
    print(f'[定时任务]   - {SCHEDULE_TREND_BACKTEST_TIME}: 趋势评分回测验证（1/3/5日跑赢大盘胜率，仅交易日）')
    print(f'[定时任务]   - {"/".join(SCHEDULE_RECOMMENDATION_TIMES)}: 自动生成智能推荐股票（仅交易日）')
    print(f'[定时任务]   - {SHAREHOLDER_UPDATE_TIME}: 更新股东人数筹码面数据（月度）')
    print(f'[定时任务]   - {FUNDAMENTAL_UPDATE_TIME}: 更新基本面质量数据（季度）')
    print(f'[定时任务]   - {VALUATION_UPDATE_TIME}: 更新估值分位数据（PE/PB 百分位）')
    print(f'[定时任务]   - {",".join(SCHEDULE_MARKET_ALERT_TIMES)}: 大盘风险预警自动扫描（仅交易日）')
    print(f'[定时任务]   - {SMART_MONEY_UPDATE_TIME}: 刷新龙虎榜+北向资金数据（刷新至最近已收盘交易日）\n')

    # 自愈机制：schedule 库的 .day.at() 在 reload/线程异常时可能漏触发，
    # 主循环每 60s 额外检查：交易日 15:10 后若今日收评仍缺失则自动补生成
    _review_healed_date = ''
    # 回测自愈：交易日 15:20 后若最近交易日回测报告缺失则自动补跑
    _backtest_healed_date = ''

    while True:
        try:
            schedule.run_pending()
            _now = datetime.now()
            _hm = _now.strftime('%H:%M')

            # ---- 收评自愈检查 ----
            if _hm >= SCHEDULE_DAILY_REVIEW_TIME:
                _today_str = _now.strftime('%Y-%m-%d')
                if _review_healed_date != _today_str and is_trading_day(_now.date()):
                    _review_healed_date = _today_str  # 先标记，避免重复进入
                    try:
                        from db.database import load_daily_market_reviews
                        _existing = load_daily_market_reviews()
                        _has_today = any(r.get('date') == _today_str for r in (_existing or [])[:3])
                        if not _has_today:
                            print(f'[定时任务自愈 {_now:%H:%M:%S}] 检测到今日({_today_str})收评缺失，自动补生成...')
                            asyncio.run(generate_review_task())
                    except Exception as heal_err:
                        print(f'[定时任务自愈 {_now:%H:%M:%S}] 补生成收评失败: {heal_err}')

            # ---- 回测自愈检查 ----
            if _hm >= SCHEDULE_BACKTEST_TIME and _backtest_healed_date != _now.strftime('%Y-%m-%d'):
                if is_trading_day(_now.date()):
                    _backtest_healed_date = _now.strftime('%Y-%m-%d')  # 先标记
                    try:
                        _today = _now.date()
                        if _today.weekday() == 0:
                            _latest_td = _today - timedelta(days=3)
                        elif _today.weekday() == 6:
                            _latest_td = _today - timedelta(days=2)
                        elif _today.weekday() == 5:
                            _latest_td = _today - timedelta(days=1)
                        else:
                            _latest_td = _today - timedelta(days=1)
                        _latest_str = _latest_td.isoformat()
                        from db.database import get_db_conn
                        _conn = get_db_conn()
                        _has_bt = _conn.execute(
                            'SELECT id FROM strategy_backtest_reports WHERE backtest_date = ?', (_latest_str,)
                        ).fetchone()
                        _conn.close()
                        if not _has_bt:
                            print(f'[定时任务自愈 {_now:%H:%M:%S}] 检测到最近交易日({_latest_str})回测报告缺失，自动补跑...')
                            asyncio.run(backtest_task())
                    except Exception as heal_err:
                        print(f'[定时任务自愈 {_now:%H:%M:%S}] 补跑回测失败: {heal_err}')
        except Exception as loop_err:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] schedule 循环异常（已捕获继续）: {loop_err}')
            import traceback
            print(f'  堆栈: {traceback.format_exc()[:400]}')
        time.sleep(60)



