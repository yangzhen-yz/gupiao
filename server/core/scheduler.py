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
        print('[定时任务] 正在更新热搜榜数据...')
        data = await fetch_eastmoney_hot_list()
        save_hot_search_ranking(data)
        print('[定时任务] 热搜榜数据更新完成')
    except Exception as error:
        print(f'[定时任务] 更新热搜榜失败: {str(error)}')

async def generate_review_task():
    try:
        print('[定时任务] 正在生成今日收评...')
        review = await generate_daily_review()
        
        save_daily_market_reviews([review])
        
        print('[定时任务] 今日收评生成完成！')
    except Exception as error:
        print(f'[定时任务] 生成收评失败: {str(error)}')

async def backtest_task():
    try:
        print('[定时任务] 正在回测验证昨日预测...')
        result = await backtest_yesterday_predictions()
        if 'error' in result:
            print(f'[定时任务] 回测失败: {result["error"]}')
        elif 'message' in result:
            print(f'[定时任务] {result["message"]}')
        else:
            # 2026-07-14 修复：使用 .get() 兜底，避免 KeyError（之前 result['bear_accuracy'] KeyError）
            print(f'[定时任务] 回测完成: 准确率{result.get("accuracy", 0)}%, '
                  f'看涨准确率{result.get("bull_accuracy", 0)}%, '
                  f'看跌准确率{result.get("bear_accuracy", 0)}%')
            if result.get('weight_adjustments'):
                for adj in result['weight_adjustments']:
                    if adj['action'] != 'keep':
                        print(f'  权重调整: {adj["factor"]} {adj["action"]} ({adj["reason"]})')
    except Exception as error:
        print(f'[定时任务] 回测验证失败: {str(error)}')

def run_scheduler():
    def job_update_hot_search_ranking():
        asyncio.run(update_hot_search_ranking_task())
    
    def job_scan_ma10():
        asyncio.run(scan_and_remove_below_ma10())
    
    def job_generate_review():
        asyncio.run(generate_review_task())
    
    def job_backtest():
        asyncio.run(backtest_task())
    
    def job_scan_trend():
        asyncio.run(scan_trend_task())

    def job_trend_backtest():
        asyncio.run(backtest_trend_predictions())

    def job_generate_recommendations():
        asyncio.run(auto_generate_recommendations_task())

    def job_update_holder_num():
        asyncio.run(update_holder_num_task())

    def job_update_fundamental():
        asyncio.run(update_fundamental_task())

    def job_update_valuation():
        asyncio.run(update_valuation_task())

    def job_market_alert():
        asyncio.run(run_scheduled_market_alert())

    def job_update_smart_money():
        asyncio.run(refresh_smart_money_task())

    # 原有的定时任务
    schedule.every().day.at(SCHEDULE_UPDATE_HOT_SEARCH_TIMES[0]).do(job_update_hot_search_ranking)
    schedule.every().day.at(SCHEDULE_UPDATE_HOT_SEARCH_TIMES[1]).do(job_update_hot_search_ranking)
    
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

    print('[定时任务] 已设置定时任务：')
    print(f'[定时任务]   - {SCHEDULE_UPDATE_HOT_SEARCH_TIMES[0]}/{SCHEDULE_UPDATE_HOT_SEARCH_TIMES[1]}: 更新热搜榜')
    print(f'[定时任务]   - {",".join(trend_scan_times)} (每{SCHEDULE_TREND_SCAN_INTERVAL_MIN}分钟): 自动扫描趋势股')
    print(f'[定时任务]   - {SCHEDULE_MA10_CHECK_TIME}: 检查10日线并删除跌破股票')
    print(f'[定时任务]   - {SCHEDULE_DAILY_REVIEW_TIME}: 自动生成每日收评')
    print(f'[定时任务]   - {SCHEDULE_BACKTEST_TIME}: 回测验证昨日预测并调整策略权重')
    print(f'[定时任务]   - {SCHEDULE_TREND_BACKTEST_TIME}: 趋势评分回测验证（1/3/5日跑赢大盘胜率）')
    print(f'[定时任务]   - {"/".join(SCHEDULE_RECOMMENDATION_TIMES)}: 自动生成智能推荐股票')
    print(f'[定时任务]   - {SHAREHOLDER_UPDATE_TIME}: 更新股东人数筹码面数据')
    print(f'[定时任务]   - {VALUATION_UPDATE_TIME}: 更新估值分位数据（PE/PB 百分位）')
    print(f'[定时任务]   - {",".join(SCHEDULE_MARKET_ALERT_TIMES)}: 大盘风险预警自动扫描')
    print(f'[定时任务]   - {SMART_MONEY_UPDATE_TIME}: 刷新龙虎榜+北向资金数据（仅交易日）')
    
    while True:
        schedule.run_pending()
        time.sleep(60)



