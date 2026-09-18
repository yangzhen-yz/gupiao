"""scheduler - 定时任务调度"""
import asyncio, schedule, time
from datetime import datetime, timedelta

from app.config import (SCHEDULE_UPDATE_HOT_SEARCH_TIMES, SCHEDULE_MA10_CHECK_TIME, SCHEDULE_DAILY_REVIEW_TIME, SCHEDULE_BACKTEST_TIME, SCHEDULE_RECOMMENDATION_TIMES, SCHEDULE_TREND_SCAN_INTERVAL_MIN, SCHEDULE_TREND_SCAN_START, SCHEDULE_TREND_SCAN_END, SCHEDULE_TREND_BACKTEST_TIME, SHAREHOLDER_UPDATE_TIME, FUNDAMENTAL_UPDATE_TIME, VALUATION_UPDATE_TIME, SCHEDULE_MARKET_ALERT_TIMES, SMART_MONEY_UPDATE_TIME, WECOM_PUSH_INTERVAL_SEC, WECOM_PUSH_START, WECOM_PUSH_END, WECOM_PUSH_PREVIEW_TIME)

from services.stock import fetch_eastmoney_hot_list
from db.database import save_hot_search_ranking, save_daily_market_reviews
from services.trend import (scan_and_remove_below_ma10, scan_trend_task)
from services.trend_backtest import backtest_trend_predictions
from services.review import generate_daily_review
from services.strategy import backtest_yesterday_predictions
from services.recommend import auto_generate_recommendations_task
from services.shareholder import update_holder_num_task
from services.fundamental import update_fundamental_task
from services.valuation import update_valuation_task
from services.market import run_scheduled_market_alert
from services.smartmoney import refresh_smart_money_task
from services.push import push_candidates_task, push_daily_preview_task
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


async def auto_optimization_task():
    """V4.4：自动优化任务（每日回测后执行）"""
    try:
        print(f'[定时任务 {datetime.now():%H:%M:%S}] 正在执行自动优化...')
        from services.auto_optimizer import run_auto_optimization
        # 网格搜索每周一运行一次
        is_monday = datetime.now().weekday() == 0
        result = await run_auto_optimization(run_grid=is_monday)

        if result.get('dimension_weights'):
            dim = result['dimension_weights']
            for adj in dim.get('adjustments', []):
                if adj.get('action') != 'keep':
                    print(f'  D1-D4权重: {adj["dimension"]} {adj["action"]} → {adj.get("new_weight", "N/A")} ({adj.get("reason", "")})')

        if result.get('role_thresholds'):
            role = result['role_thresholds']
            for adj in role.get('adjustments', []):
                print(f'  角色门槛: {adj.get("role", "")}.{adj.get("param", "")} {adj.get("direction", "")} ({adj.get("reason", "")})')

        if result.get('grid_search'):
            gs = result['grid_search']
            if gs.get('best_params'):
                print(f'  网格搜索: 胜率{gs["current_winrate"]:.1f}%→{gs["best_winrate"]:.1f}%, {"已采纳" if gs.get("adopted") else "未采纳"}')

        print(f'[定时任务 {datetime.now():%H:%M:%S}] 自动优化完成')
    except Exception as error:
        print(f'[定时任务 {datetime.now():%H:%M:%S}] 自动优化失败: {str(error)}')
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

    def job_auto_optimize():
        """V4.4：自动优化（回测后运行，非交易日跳过）"""
        if not _today_is_trading():
            return
        asyncio.run(auto_optimization_task())

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
        # 2026-09-02 修复：update_valuation_task 是同步普通函数，不能用 asyncio.run 包裹（会抛 a coroutine was expected）
        update_valuation_task()

    def job_market_alert():
        if not _today_is_trading():
            return
        asyncio.run(run_scheduled_market_alert())

    def job_update_smart_money():
        # 龙虎榜+北向：只有"最近已收盘交易日"有数据，非交易日也可以拉取（刷新到上一交易日）
        asyncio.run(refresh_smart_money_task())

    def job_push_daily_preview():
        # 开盘前推送"今日关注池"预览（仅交易日）
        if not _today_is_trading():
            return
        asyncio.run(push_daily_preview_task())

    def job_db_maintenance():
        # 2026-09-18：每周六凌晨清理过期 K线缓存/热搜快照并 VACUUM（非交易日，不锁库）
        from services.db_maintenance import cleanup_old_cache
        try:
            cleanup_old_cache()
        except Exception as e:
            print(f'[DB维护] 异常: {e}')

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

    # V4.4: 每天回测后（15:35）自动优化 D1-D4 权重 + 角色门槛 + 网格搜索（周一）
    schedule.every().day.at("15:35").do(job_auto_optimize)

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

    # 企业微信选股推送：开盘前 9:25 推送今日关注池预览（仅交易日）
    schedule.every().day.at(WECOM_PUSH_PREVIEW_TIME).do(job_push_daily_preview)

    # 数据库维护：每周六凌晨3点清理过期缓存+VACUUM（防 SQLite 膨胀）
    schedule.every().saturday.at("03:00").do(job_db_maintenance)

    _now = datetime.now()
    print(f'\n[定时任务 {_now:%Y-%m-%d %H:%M:%S}] 已注册以下定时任务（今日交易日={_today_is_trading()}）：')
    print(f'[定时任务]   - {"/".join(SCHEDULE_UPDATE_HOT_SEARCH_TIMES)}: 更新热搜榜（仅交易日）')
    print(f'[定时任务]   - {",".join(trend_scan_times)} (每{SCHEDULE_TREND_SCAN_INTERVAL_MIN}分钟): 自动扫描趋势股（仅交易日）')
    print(f'[定时任务]   - {SCHEDULE_MA10_CHECK_TIME}: 检查10日线+连跌天数并清理股票池（仅交易日）')
    print(f'[定时任务]   - {SCHEDULE_DAILY_REVIEW_TIME}: 自动生成每日收评（仅交易日，含 AI 情绪评分/操作建议）')
    print(f'[定时任务]   - {SCHEDULE_BACKTEST_TIME}: 回测验证昨日预测并调整策略权重（仅交易日）')
    print(f'[定时任务]   - {SCHEDULE_TREND_BACKTEST_TIME}: 趋势评分回测验证（1/3/5日跑赢大盘胜率，仅交易日）')
    print(f'[定时任务]   - 15:35: 自动优化（D1-D4权重+角色门槛+网格搜索，仅交易日，周一含全量搜索）')
    print(f'[定时任务]   - {"/".join(SCHEDULE_RECOMMENDATION_TIMES)}: 自动生成智能推荐股票（仅交易日）')
    print(f'[定时任务]   - {SHAREHOLDER_UPDATE_TIME}: 更新股东人数筹码面数据（月度）')
    print(f'[定时任务]   - {FUNDAMENTAL_UPDATE_TIME}: 更新基本面质量数据（季度）')
    print(f'[定时任务]   - {VALUATION_UPDATE_TIME}: 更新估值分位数据（PE/PB 百分位）')
    print(f'[定时任务]   - {",".join(SCHEDULE_MARKET_ALERT_TIMES)}: 大盘风险预警自动扫描（仅交易日）')
    print(f'[定时任务]   - {SMART_MONEY_UPDATE_TIME}: 刷新龙虎榜+北向资金数据（刷新至最近已收盘交易日）')
    print(f'[定时任务]   - {WECOM_PUSH_PREVIEW_TIME}: 推送今日关注池预览（企业微信，仅交易日）')
    print(f'[定时任务]   - 盘中 {WECOM_PUSH_START}~{WECOM_PUSH_END} 每{WECOM_PUSH_INTERVAL_SEC}秒: 达标候选股实时推送企业微信（仅交易日）\n')

    # 自愈机制：schedule 库的 .day.at() 可能在 60s 间隔下漏触发（run_pending 恰好跳过目标分钟），
    # 主循环每 30s 额外检查：交易日 15:10 后若今日收评仍缺失则自动补生成，最多重试 3 次
    _review_heal_date = ''       # 已成功补生成的日期
    _review_heal_attempts = 0    # 当天重试次数
    _review_heal_date_tracker = ''  # 用于检测日期变更时重置计数器
    # 回测自愈：交易日 15:20 后若最近交易日回测报告缺失则自动补跑，最多重试 3 次
    _backtest_heal_date = ''
    _backtest_heal_attempts = 0
    _backtest_heal_date_tracker = ''
    # 企业微信选股推送：盘中每 WECOM_PUSH_INTERVAL_SEC 秒扫描一次（1 分钟级准实时）
    _last_push_ts = 0.0

    while True:
        try:
            # 2026-09-18 稳定性修复：run_pending 单独 try 隔离。
            # 任何定时 job 抛异常只记录，不再拖累下方的企业微信推送轮询与自愈检查。
            try:
                schedule.run_pending()
            except Exception as _sched_err:
                print(f'[定时任务 {datetime.now():%H:%M:%S}] run_pending 异常（已隔离，不影响推送）: {_sched_err}')
            _now = datetime.now()
            _hm = _now.strftime('%H:%M')
            _today_str = _now.strftime('%Y-%m-%d')

            # ---- 企业微信选股推送（盘中准实时，仅交易日）----
            # 不注册几百个 .day.at() 任务，改用主循环内的时间窗口 + 间隔控制
            if _hm >= WECOM_PUSH_START and _hm <= WECOM_PUSH_END and _today_is_trading():
                if time.time() - _last_push_ts >= WECOM_PUSH_INTERVAL_SEC:
                    _last_push_ts = time.time()
                    try:
                        asyncio.run(push_candidates_task())
                    except Exception as push_err:
                        print(f'[定时任务 {_now:%H:%M:%S}] 选股推送异常: {push_err}')

            # ---- 收评自愈检查（最多重试 3 次，成功后停止）----
            if _hm >= SCHEDULE_DAILY_REVIEW_TIME and is_trading_day(_now.date()):
                # 日期变更时重置计数器
                if _review_heal_date_tracker != _today_str:
                    _review_heal_date_tracker = _today_str
                    _review_heal_attempts = 0
                # 未成功且未超过重试次数
                if _review_heal_date != _today_str and _review_heal_attempts < 3:
                    _review_heal_attempts += 1
                    try:
                        from db.database import load_daily_market_reviews
                        _existing = load_daily_market_reviews()
                        _has_today = any(r.get('date') == _today_str for r in (_existing or [])[:3])
                        if _has_today:
                            # DB 中已存在（可能是定时任务成功或手动生成），标记完成
                            _review_heal_date = _today_str
                        else:
                            print(f'[定时任务自愈 {_now:%H:%M:%S}] 检测到今日({_today_str})收评缺失，第{_review_heal_attempts}次补生成...')
                            asyncio.run(generate_review_task())
                            # 生成后再次检查 DB 确认成功
                            _existing2 = load_daily_market_reviews()
                            _has_today2 = any(r.get('date') == _today_str for r in (_existing2 or [])[:3])
                            if _has_today2:
                                _review_heal_date = _today_str
                                print(f'[定时任务自愈 {_now:%H:%M:%S}] 收评补生成成功')
                            else:
                                print(f'[定时任务自愈 {_now:%H:%M:%S}] 收评生成后 DB 未确认（第{_review_heal_attempts}次），将在下次循环重试')
                    except Exception as heal_err:
                        print(f'[定时任务自愈 {_now:%H:%M:%S}] 补生成收评失败(第{_review_heal_attempts}次): {heal_err}')
                        import traceback
                        print(f'  堆栈: {traceback.format_exc()[:300]}')

            # ---- 回测自愈检查（最多重试 3 次，成功后停止）----
            if _hm >= SCHEDULE_BACKTEST_TIME and is_trading_day(_now.date()):
                if _backtest_heal_date_tracker != _today_str:
                    _backtest_heal_date_tracker = _today_str
                    _backtest_heal_attempts = 0
                if _backtest_heal_date != _today_str and _backtest_heal_attempts < 3:
                    _backtest_heal_attempts += 1
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
                        if _has_bt:
                            _backtest_heal_date = _today_str
                        else:
                            print(f'[定时任务自愈 {_now:%H:%M:%S}] 检测到最近交易日({_latest_str})回测报告缺失，第{_backtest_heal_attempts}次补跑...')
                            asyncio.run(backtest_task())
                            _conn2 = get_db_conn()
                            _has_bt2 = _conn2.execute(
                                'SELECT id FROM strategy_backtest_reports WHERE backtest_date = ?', (_latest_str,)
                            ).fetchone()
                            _conn2.close()
                            if _has_bt2:
                                _backtest_heal_date = _today_str
                                print(f'[定时任务自愈 {_now:%H:%M:%S}] 回测补跑成功')
                            else:
                                print(f'[定时任务自愈 {_now:%H:%M:%S}] 回测补跑后 DB 未确认（第{_backtest_heal_attempts}次），将在下次循环重试')
                    except Exception as heal_err:
                        print(f'[定时任务自愈 {_now:%H:%M:%S}] 补跑回测失败(第{_backtest_heal_attempts}次): {heal_err}')
                        import traceback
                        print(f'  堆栈: {traceback.format_exc()[:300]}')
        except Exception as loop_err:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] schedule 循环异常（已捕获继续）: {loop_err}')
            import traceback
            print(f'  堆栈: {traceback.format_exc()[:400]}')
        time.sleep(30)



