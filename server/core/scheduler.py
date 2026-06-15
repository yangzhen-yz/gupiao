"""scheduler - 定时任务调度"""
import asyncio, schedule, threading, time
from datetime import datetime

from app.config import (SCHEDULE_UPDATE_HOT_SEARCH_TIMES, SCHEDULE_MA10_CHECK_TIME, SCHEDULE_DAILY_REVIEW_TIME, SCHEDULE_BACKTEST_TIME, SCHEDULE_RECOMMENDATION_TIMES)

from services.stock import fetch_eastmoney_hot_list
from db.database import save_hot_search_ranking, save_daily_market_reviews
from services.trend import scan_and_remove_below_ma10, scan_trend_task
from services.review import generate_daily_review
from services.strategy import backtest_yesterday_predictions
from services.recommend import auto_generate_recommendations_task

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
            print(f'[定时任务] 回测完成: 准确率{result["accuracy"]}%, 看涨准确率{result["bull_accuracy"]}%, 看跌准确率{result["bear_accuracy"]}%')
            if result.get('weight_adjustments'):
                for adj in result['weight_adjustments']:
                    if adj['action'] != 'keep':
                        print(f'  权重调整: {adj["factor"]} {adj["action"]} ({adj["reason"]})')
    except Exception as error:
        print(f'[定时任务] 回测验证失败: {str(error)}')

async def scan_trend_task(force: bool = False):
    try:
        print('[趋势扫描] 定时任务开始...')
        await scan_trend_scan_results(force=force)
    except Exception as e:
        print(f'[趋势扫描] 定时任务失败: {e}')



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
    
    def job_generate_recommendations():
        asyncio.run(auto_generate_recommendations_task())
    
    # 原有的定时任务
    schedule.every().day.at(SCHEDULE_UPDATE_HOT_SEARCH_TIMES[0]).do(job_update_hot_search_ranking)
    schedule.every().day.at(SCHEDULE_UPDATE_HOT_SEARCH_TIMES[1]).do(job_update_hot_search_ranking)
    
    schedule.every().day.at(SCHEDULE_MA10_CHECK_TIME).do(job_scan_ma10)
    
    schedule.every().day.at(SCHEDULE_DAILY_REVIEW_TIME).do(job_generate_review)
    
    schedule.every().day.at(SCHEDULE_BACKTEST_TIME).do(job_backtest)
    
    # 新增：每天自动扫描趋势股
    schedule.every().day.at('09:30').do(job_scan_trend)
    schedule.every().day.at('11:30').do(job_scan_trend)
    schedule.every().day.at('13:00').do(job_scan_trend)
    schedule.every().day.at('15:00').do(job_scan_trend)
    
    # 新增：每天自动生成智能推荐股票
    for t in SCHEDULE_RECOMMENDATION_TIMES:
        schedule.every().day.at(t).do(job_generate_recommendations)
    
    print('[定时任务] 已设置定时任务：')
    print(f'[定时任务]   - {SCHEDULE_UPDATE_HOT_SEARCH_TIMES[0]}/{SCHEDULE_UPDATE_HOT_SEARCH_TIMES[1]}: 更新热搜榜')
    print(f'[定时任务]   - 09:30/11:30/13:00/15:00: 自动扫描趋势股')
    print(f'[定时任务]   - {SCHEDULE_MA10_CHECK_TIME}: 检查10日线并删除跌破股票')
    print(f'[定时任务]   - {SCHEDULE_DAILY_REVIEW_TIME}: 自动生成每日收评')
    print(f'[定时任务]   - {SCHEDULE_BACKTEST_TIME}: 回测验证昨日预测并调整策略权重')
    print(f'[定时任务]   - {"/".join(SCHEDULE_RECOMMENDATION_TIMES)}: 自动生成智能推荐股票')
    
    while True:
        schedule.run_pending()
        time.sleep(60)



