"""scheduler - 定时任务调度"""
import asyncio, os, schedule, time, threading
from datetime import datetime, timedelta

from app.config import (SCHEDULE_UPDATE_HOT_SEARCH_TIMES, SCHEDULE_MA10_CHECK_TIME, SCHEDULE_DAILY_REVIEW_TIME, SCHEDULE_BACKTEST_TIME, SCHEDULE_RECOMMENDATION_TIMES, SCHEDULE_PRE_CLOSE_SNAPSHOT_TIME, RECOMMEND_REVIEW_TIME, SCHEDULE_TREND_SCAN_INTERVAL_MIN, SCHEDULE_TREND_SCAN_START, SCHEDULE_TREND_SCAN_END, SCHEDULE_TREND_BACKTEST_TIME, SHAREHOLDER_UPDATE_TIME, FUNDAMENTAL_UPDATE_TIME, VALUATION_UPDATE_TIME, SCHEDULE_MARKET_ALERT_TIMES, SMART_MONEY_UPDATE_TIME, WECOM_PUSH_INTERVAL_SEC, REC_SIM_DAILY_TIME, SHADOW_EVAL_TIME, SHADOW_EVAL_MIN_DAYS, WECOM_PUSH_START, WECOM_PUSH_END, WECOM_PUSH_DAILY_LIMIT, WECOM_PUSH_PREVIEW_TIME, KLINE_HISTORY_SYNC_TIME, KLINE_HISTORY_SYNC_TIMES, KLINE_HISTORY_SYNC_LAST,
                        KLINE_HISTORY_ENABLED, RPS_PERIOD_DAYS,
                        KLINE_RESTATE_TIME, KLINE_RESTATE_LIMIT, ZHABAN_SHADOW_TIME)

from services.data.stock import fetch_eastmoney_hot_list
from db.database import save_hot_search_ranking, save_daily_market_reviews
from services.strategy.trend import (scan_and_remove_below_ma10, scan_trend_task)
from services.strategy.trend_backtest import backtest_trend_predictions
from services.ops.review import generate_daily_review
from services.strategy.strategy import backtest_yesterday_predictions
from services.pick.recommend import auto_generate_recommendations_task
from services.data.shareholder import update_holder_num_task
from services.data.fundamental import update_fundamental_task
from services.data.valuation import update_valuation_task
from services.strategy.market import run_scheduled_market_alert
from services.data.smartmoney import refresh_smart_money_task
from services.support.push import push_candidates_task, push_daily_preview_task
from services.data.trading_calendar import is_trading_day
# 持仓快反推送（2026-10-14）：止损/止盈/炸板不能等用户打开页面才看见
# 2026-10-15 补 T+1：当天建仓的票当天卖不掉，改成次日竞价推"可卖预案"
from services.portfolio.position_watch import watch_all_users, push_t1_plan, watch_targets
# 盘后数据自检的告警出口（按账号发，不再共用全局群）
from services.support.push_channel import send_to_user
from services.strategy.sentiment import get_sentiment
# 盘前全市场海选候选池（2026-10-15）：替代"只看东财人气榜"的窄入口
from services.pick.candidate_pool import build_pool as build_candidate_pool
from app.config import CANDIDATE_POOL_TIME
from app.config import SCHED_HEARTBEAT_STALE_MIN, SCHED_CHAIN_CHECK_TIMES
from app.config import MATRIX_RERUN_ENABLED, MATRIX_RERUN_TIME
from app.config import SENTIMENT_FINAL_SNAPSHOT_TIME
from app.config import INDEX_DAILY_SYNC_TIME
from app.config import (MARKET_CAP_REFRESH_TIME, MARKET_CAP_REFRESH_WEEKDAY,
                        MARKET_CAP_REFRESH_WEEKDAY2,
                        INDUSTRY_SYNC_TIME, INDUSTRY_SYNC_WEEKDAY,
                        CANDIDATE_POOL_MAX, KLINE_POOL_MIN_COVERAGE)
from app.config import (POSITION_WATCH_ENABLED, POSITION_WATCH_INTERVAL_SEC,
                        POSITION_WATCH_START, POSITION_WATCH_END, PUSH_T1_PLAN_TIME)
# 盘后慢变数据刷新节奏（2026-10-19 降频专项，见 config.py 同名片段）
from app.config import (SLOW_DATA_HOLDER_NUM_WEEKDAYS, SLOW_DATA_FUNDAMENTAL_WEEKDAYS,
                        SLOW_DATA_DAILY_MONTHS, SLOW_DATA_VALUATION_TRADING_ONLY)
# 双源数据交叉校验（2026-10-19，westock 权威源）
from app.config import WESTOCK_CROSSCHECK_TIME
from app.config import EVAL_LAB_ENABLED, EVAL_LAB_TIME, EVAL_LAB_WEEKDAY
from app.config import AUCTION_SNAPSHOT_ENABLED, AUCTION_SNAPSHOT_TIME, AUCTION_SNAPSHOT_BATCH
from app.config import (AUCTION_DIP_SHADOW_ENABLED, AUCTION_DIP_MORNING_TIME,
                        AUCTION_DIP_RETRY_TIME, AUCTION_DIP_EVENING_TIME)
from app.config import MINUTE5_FORWARD_ENABLED, MINUTE5_FORWARD_TIME
# 盘中龙头通道（2026-11-01）：默认关，影子期只落库不推送
from app.config import (INTRADAY_DRAGON_ENABLED, INTRADAY_DRAGON_START,
                        INTRADAY_DRAGON_END, INTRADAY_DRAGON_INTERVAL_SEC,
                        INTRADAY_DRAGON_RATE_GOVERNOR_ON,
                        INTRADAY_DRAGON_SHADOW)


# 盘后数据自检的告警去重：同一天+同一"库内最新日期"只轰一次，否则每轮重跑都发一条
_KLINE_ALERTED = set()


_WEEKDAY_CN = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']


def _weekday_names(weekdays) -> str:
    """把 [0, 3] 这种人能读的形式写成 '周一/周四'；空列表 = 每日（=改造前）。"""
    if not weekdays:
        return '每日'
    return '/'.join(_WEEKDAY_CN[w] for w in sorted(weekdays) if 0 <= w <= 6)


def _slow_data_due(weekdays) -> bool:
    """盘后慢变数据（股东人数/基本面）今天该不该刷。

    判据只看日历，不发任何网络请求；返回 False 时任务打一行日志就退出。
    财报季（SLOW_DATA_DAILY_MONTHS）恢复每日——那几个月周末公告密集，新披露不能等一周。
    weekdays 传空列表 = 每个自然日都刷，行为与改造前完全一致（关掉开关就是老样子）。
    """
    if not weekdays:
        return True
    now = datetime.now()
    if now.month in SLOW_DATA_DAILY_MONTHS:
        return True
    return now.weekday() in weekdays


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


def _run_pending_guarded() -> None:
    """跑一轮定时任务，但**绝不让一个坏 job 冻结整条调度**。

    为什么不能直接 `schedule.run_pending()`：它的实现是
    `for job in sorted(runnable_jobs): self._run_job(job)`，而 `Job.run()` 是
    「先执行 job_func、执行完才推进 next_run」。所以 job_func 一抛异常：
      · next_run 没推进 ⇒ 这个 job 下一轮仍然"到点"，而且按 next_run 排序永远排第一；
      · for 循环被异常打断 ⇒ 排在它后面的 job **这一轮一个都轮不到**；
      · 下一轮 10 秒后再来一遍 —— 于是整条定时任务链被这一个坏 job 永久卡死。

    11-06 实拍：03:30 的 `job_db_backup` 缺一个 `import os`，从 03:30:01 起每 10 秒抛一次，
    结果 09:00 盘前海选、09:15 竞价快照、09:45/10:00/13:30/14:05 智能推荐、15:35 自动优化、
    每 60s 接口用量落库**全天一条都没执行**（`daily_candidate_pool`/`daily_recommendations`/
    `strategy_snapshot`/`auction_snapshot`/`cls_plate_daily` 当天全是 0 行），当天既没推票、
    也没推大盘提醒；只有主循环里的收评自愈、回测自愈在 15:10/15:20 把两项补了回来
    （`[定时任务自愈 15:10:09] 检测到今日(2026-11-06)收评缺失`）。日志里那 4115 条
    run_pending 异常只是这座冰山的露尖部分。

    做法：自己按 `next_run` 顺序遍历到点的 job，**每个 job 单独 try**——坏 job 抛异常时
    照样推进它自己的 `next_run`（当天跳过它，不再每 10 秒重复抛），后面的 job 继续执行。
    语义与 `run_pending()` 一致的地方：同样尊重 `CancelJob` 返回值；不一致的地方只有一处
    ——异常不再打断这一轮。这样"忘了 import"这类错误的代价从"整天没有推荐"降级成
    "那一个任务当天没跑，并且日志里点名是谁"。
    """
    now = datetime.now()
    try:
        due = [j for j in schedule.get_jobs() if j.should_run]
    except Exception as e:
        print(f'[定时任务 {now:%H:%M:%S}] 取到期任务失败（本轮跳过）: {type(e).__name__}: {e}')
        return
    _ran = 0
    _errs = 0
    for job in sorted(due, key=lambda j: (j.next_run or now)):
        name = getattr(getattr(job, 'job_func', None), '__name__', None) or repr(job.job_func)[:60]
        try:
            ret = job.run()
            if isinstance(ret, schedule.CancelJob) or ret is schedule.CancelJob:
                schedule.cancel_job(job)
            _ran += 1
        except Exception as e:
            _errs += 1
            # job.run() 里 job_func 抛异常时 next_run 不会被推进，这里补一刀把它推到下一次；
            # 推进失败就退回按周期加，绝不能让这个 job 留在"永远到点"的状态
            try:
                job._schedule_next_run()
            except Exception:
                try:
                    job.next_run = datetime.now() + timedelta(hours=24)
                except Exception:
                    pass
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 定时 job「{name}」抛异常（已隔离，'
                  f'本轮其余任务继续执行）: {type(e).__name__}: {e}；'
                  f'该任务已跳过一次，下次执行时间 {job.next_run:%m-%d %H:%M}')
    # 调度心跳（2026-11-07）：跑完一轮就记一次，给 `services/ops/sched_liveness.py` 的观察线程看。
    # 记在这里而不是观察者那边，是因为"循环还转着但什么都不执行"这种 11-06 形态，
    # 光有心跳抓不到——所以心跳只用来抓"循环彻底不转"，另一条判据去看产出行数。
    # 整块包 try：记心跳失败绝不能反过来影响定时任务本身。
    try:
        from services.ops.sched_liveness import mark_loop_beat
        mark_loop_beat(ran=_ran, errors=_errs)
    except Exception:
        pass


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
        # 2026-11-04：收评接口已上 SWR 缓存，写后立刻失效，别等 5 分钟 TTL
        try:
            from app.api.routes import _invalidate_daily_reviews_cache
            _invalidate_daily_reviews_cache()
        except Exception as _ce:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 收评缓存失效失败（不影响主流程）: {_ce}')
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
        from services.ops.auto_optimizer import run_auto_optimization
        # 网格搜索每周一运行一次
        is_monday = datetime.now().weekday() == 0
        result = await run_auto_optimization(run_grid=is_monday)

        if result.get('dimension_weights'):
            dim = result['dimension_weights']
            for adj in dim.get('adjustments', []):
                if adj.get('action') != 'keep':
                    print(f'  D1-D4权重: {adj["dimension"]} {adj["action"]} → {adj.get("new_weight", "N/A")} ({adj.get("reason", "")})')

        # 2026-10-28：角色门槛优化随角色体系删除，不再有 role_thresholds 结果

        if result.get('grid_search'):
            gs = result['grid_search']
            if gs.get('best_params'):
                print(f'  网格搜索: 胜率{gs["current_winrate"]:.1f}%→{gs["best_winrate"]:.1f}%, {"已采纳" if gs.get("adopted") else "未采纳"}')

        print(f'[定时任务 {datetime.now():%H:%M:%S}] 自动优化完成')
    except Exception as error:
        print(f'[定时任务 {datetime.now():%H:%M:%S}] 自动优化失败: {str(error)}')
        import traceback
        print(f'  堆栈: {traceback.format_exc()[:500]}')


# 启动预热串行闸门（2026-11-04，根治「重启后 12 分钟全站不可用」）
# 事故：15:10 看护重启后，5 个预热/保活线程同时跑全市场计算，抢 SQLite 写锁与 GIL。
# 日志实锤：[批量扫描] 阶段 候选+范围 670.36s（正常 2~8s）、预热链累计 769.5s；
# 期间 /api/stock-map 22s、stock/full 60s 超时、/api/health 排队 9.4s（X-Process-Time
# 只有 4ms，说明时间全花在排队而非计算），用户页面卡在 spinner 三分钟。
# 修法：同一时刻只允许一个预热/保活在跑；启动先让出 15s 给 uvicorn 完全就绪，
# 项与项之间再留 2s 空窗让主事件循环处理用户请求。各项业务逻辑一字未动。
_PREWARM_GATE = threading.Lock()
_PREWARM_START_DELAY = 15   # 秒：启动后先不预热，保证重启瞬间页面可访问
_PREWARM_GAP = 2            # 秒：相邻预热项之间的空窗


def _serial_prewarm(fn):
    """启动预热项装饰器：延迟启动 + 全局串行 + 项间让出，不改原函数体。"""
    from functools import wraps

    @wraps(fn)
    def _wrapper(*args, **kwargs):
        time.sleep(_PREWARM_START_DELAY)
        with _PREWARM_GATE:
            print(f'[预热 {datetime.now():%H:%M:%S}] > {fn.__name__} 开始')
            try:
                return fn(*args, **kwargs)
            finally:
                print(f'[预热 {datetime.now():%H:%M:%S}] < {fn.__name__} 结束')
                time.sleep(_PREWARM_GAP)
    return _wrapper


def run_scheduler():
    # 2026-10-17 启动预热：universe 选票范围首算要 ~5s（流动性聚合），
    # 不预热则重启后第一次开配置页白等 5s。放后台线程跑，不阻塞调度器注册。
    # 2026-10-30 升级为**常驻保活**：universe 两层缓存与流动性统计的 TTL 都是 600s，
    # 只在启动预热一次的话，重启 ~10 分钟后缓存过期，下一个撞上的请求（批量扫描/
    # 配置页/海选）实测要再付 3~8s 冷算（10-30 16:13 筛选页 8.71s 即此因）。
    # 改为每 540s 后台刷新一次，永远赶在过期前；先算好再原子换入
    # （universe.refresh_caches），刷新窗口内请求继续用旧值，不撞冷路径。
    @_serial_prewarm
    def _prewarm_universe_first():
        import time as _time
        from services.data import universe
        _t = _time.time()
        try:
            universe.selectable_count()
            print(f'[预热 {datetime.now():%H:%M:%S}] universe 选票范围缓存完成（{_time.time() - _t:.1f}s）')
        except Exception as e:
            print(f'[预热 {datetime.now():%H:%M:%S}] universe 预热失败（不致命，首次请求会现算）: {e}')
        # 2026-10-30：配置页口径摘要预热（/api/settings 只读快照，缺快照才现算；
        # 这里先算好，重启后第一个打开配置页的人不再撞 27.8s 冷路径）
        try:
            from services.support import settings_preview
            _t2 = _time.time()
            settings_preview.compute_summary()
            print(f'[预热 {datetime.now():%H:%M:%S}] 配置页口径摘要缓存完成（{_time.time() - _t2:.1f}s）')
        except Exception as e:
            print(f'[预热 {datetime.now():%H:%M:%S}] 口径摘要预热失败（首次请求会现算兜底）: {e}')

    @_serial_prewarm
    def _keepwarm_round():
        import time as _time
        try:
            from app.config import (LIQ_STATS_KEEPWARM_MAX_AGE_SEC,
                                    LIQ_STATS_KEEPWARM_FORCE)
            from services.data import liquidity, universe
            from services.support import settings_preview
            _t = _time.time()
            # 流动性统计不再每轮 force 重算（2026-11-04）：它的输入是本地日K，盘中根本不变，
            # 而单算一次实测 14.9s（346.5 万行、无 date 前导索引），线上那一行日志因此是
            # 12~170s，每 9 分钟跟推送扫描/同步抢一轮资源。判据抽成纯函数便于自测。
            _need = liquidity.should_keepwarm_refresh(
                _time.time(), liquidity.last_stats_ts(),
                LIQ_STATS_KEEPWARM_MAX_AGE_SEC, LIQ_STATS_KEEPWARM_FORCE)
            _tl = 0.0
            if _need:
                _t0 = _time.time()
                liquidity.stats_map(force=True)
                _tl = _time.time() - _t0
            universe.refresh_caches()          # 再重算两层范围并原子换入
            settings_preview.compute_summary() # 配置页摘要随热缓存重算（热路径 <1s）
            _liq_txt = ('流动性重算 %.1fs' % _tl) if _need else '流动性跳过（日K未变）'
            print(f'[预热 {datetime.now():%H:%M:%S}] universe/口径摘要保活完成'
                  f'（{_time.time() - _t:.1f}s；{_liq_txt}）')
        except Exception as e:
            print(f'[预热 {datetime.now():%H:%M:%S}] universe 保活刷新失败（不影响请求，下轮再试）: {e}')

    def _keepwarm_universe():
        # 启动段与后续每 9 分钟的保活轮都进同一把串行闸门，不与其它预热叠加。
        _prewarm_universe_first()
        while True:
            time.sleep(540)  # 9 分钟 < 600s TTL
            _keepwarm_round()
    threading.Thread(target=_keepwarm_universe, daemon=True, name='prewarm-universe').start()

    # 批量扫描预热（2026-10-30）：重启后第一次打开筛选页要付一次全量冷算
    # （实测 2.2~2.9s，超"页面 2s 内"目标）。用**全部用户池的并集**构建宇宙
    # （与前端传参同源，load_user_scan_pool() 无参=所有人池子合并），启动后台
    # 预扫一次，首开即命中缓存；即使之后池子变了 key 不一致，也只是退回
    # "首开冷算一次"的旧行为，不会更差。失败不致命。
    @_serial_prewarm
    def _prewarm_hot_scan():
        import time as _time
        try:
            import asyncio as _aio
            # routes.py 拆分（2026-11-05）后端点函数不再从聚合模块再导出，
            # 这个预热必须直接取 routers.scan 里的实现，否则启动即 NameError 式失败
            from app.api.routers.scan import hot_stock_buttons_scan
            from db.database import load_user_scan_pool
            _codes = ','.join(load_user_scan_pool()) or 'sh600519'
            _t = _time.time()
            _aio.run(hot_stock_buttons_scan(codes=_codes, force=False, pool_only=False))
            print(f'[预热 {datetime.now():%H:%M:%S}] 批量扫描缓存完成（{_time.time() - _t:.1f}s）')
        except Exception as e:
            print(f'[预热 {datetime.now():%H:%M:%S}] 批量扫描预热失败（首开会现算）: {e}')
        # 行动台预热（2026-10-30）：重启后首开实测 2.2s（行情/情绪/主线冷算）。
        # build_action_desk 的公共部分与 user_id 无关（持仓段按用户现查，本来就轻），
        # 用 user_id=1 预热公共缓存即可；失败不致命。
        try:
            import asyncio as _aio
            from services.ops.action_desk import build_action_desk
            _t = _time.time()
            _aio.run(build_action_desk(user_id=1))
            print(f'[预热 {datetime.now():%H:%M:%S}] 行动台缓存完成（{_time.time() - _t:.1f}s）')
        except Exception as e:
            print(f'[预热 {datetime.now():%H:%M:%S}] 行动台预热失败（首开会现算）: {e}')
    threading.Thread(target=_prewarm_hot_scan, daemon=True, name='prewarm-hot-scan').start()

    # 可执行超额预热（2026-10-27）：`/api/recommend-excess` 冷算实测 7.4s，而前端默认 10s
    # 超时——重启后第一个开「回测曲线」页的人正好撞上（当天实拍：超额没算出来：请求超时（10s））。
    # 放后台线程预热一次，让第一个请求命中缓存；失败不致命（页面还有 30s 超时 + 一次重试兜底）。
    @_serial_prewarm
    def _prewarm_excess():
        try:
            from app.api import routes
            sec = routes.prewarm_recommend_excess()
            print(f'[预热 {datetime.now():%H:%M:%S}] 可执行超额缓存完成（冷算 {sec:.1f}s）')
        except Exception as e:
            print(f'[预热 {datetime.now():%H:%M:%S}] 超额预热失败（不致命，首次请求会现算）: {e}')
    threading.Thread(target=_prewarm_excess, daemon=True, name='prewarm-excess').start()

    # 区间带宽波动率预热（2026-10-19 动态区间宽度）：这份缓存是一次"全市场近 6 个已收盘
    # 交易日收盘序列"的**同步** SQLite 查询。不预热的话，09:45 批次里第一只票会在 asyncio
    # 事件循环上等它几百毫秒——偏偏那一刻正是"开盘后第一时间出推荐"的关键路径（同一个坑
    # 在 2026-10-17 的 history_available 上踩过一次）。开关关着就一行都不查：
    # 关掉的代价必须严格等于改造前，一次多余的库访问也不给留。
    @_serial_prewarm
    def _prewarm_vol():
        try:
            from services.support import app_settings
            if not app_settings.value('buy_band_dynamic_enabled'):
                return
            from services.data.kline_history import get_vol_close_map
            m = get_vol_close_map()
            print(f'[预热 {datetime.now():%H:%M:%S}] 区间带宽波动率缓存完成：'
                  f'{len(m)} 只票有收盘序列')
        except Exception as e:
            print(f'[预热 {datetime.now():%H:%M:%S}] 波动率预热失败（不致命，带宽退回核心值）: {e}')
    threading.Thread(target=_prewarm_vol, daemon=True, name='prewarm-vol').start()

    # 单票行情页依赖预热（2026-11-04）：/api/stock/{symbol}/full 首次访问会连付三笔冷启动
    # 成本：history_available() 全表统计 8.0s、compute_trend_result 首只 6.7s、龙虎榜东财
    # 1～15s，线上切票实测 16.7s / 23.9s。这些都是**进程级一次性初始化**，放启动后台
    # 预热即可；同一把 _PREWARM_GATE 保证不与其它预热抢 SQLite / 线程池。
    @_serial_prewarm
    def _prewarm_quote_deps():
        import time as _time
        try:
            from services.data.kline_history import history_available
            _t = _time.time()
            history_available()
            print(f'[预热 {datetime.now():%H:%M:%S}] 本地K线库可用性探测完成（{_time.time() - _t:.1f}s）')
        except Exception as e:
            print(f'[预热 {datetime.now():%H:%M:%S}] K线可用性探测失败（不致命，请求侧有超时闸门）: {e}')
        try:
            from services.strategy.trend import compute_trend_result
            _t = _time.time()
            compute_trend_result('sh603985', 0)   # 任意一只即可触发引擎单例 + 首条趋势 memo
            print(f'[预热 {datetime.now():%H:%M:%S}] 单票趋势引擎预热完成（{_time.time() - _t:.1f}s）')
        except Exception as e:
            print(f'[预热 {datetime.now():%H:%M:%S}] 趋势引擎预热失败（不致命）: {e}')
        try:
            from services.data.smartmoney import get_dragon_tiger_symbol_map
            from app.config import SMART_MONEY_DRAGON_TIGER_DAYS
            get_dragon_tiger_symbol_map(SMART_MONEY_DRAGON_TIGER_DAYS)  # 触发一次后台 SWR 刷新
            print(f'[预热 {datetime.now():%H:%M:%S}] 龙虎榜后台刷新已触发')
        except Exception as e:
            print(f'[预热 {datetime.now():%H:%M:%S}] 龙虎榜预热失败（不致命）: {e}')
    threading.Thread(target=_prewarm_quote_deps, daemon=True, name='prewarm-quote-deps').start()

    # 分钟级只读数据源预热（2026-11-04 全站巡检）：热态基准实测 /api/theme-index 2.92s、
    # /api/kline-history/stats 1.44s、/api/market-index 1.19s、/api/cls-ladder 1.16s。
    # 这四个接口已统一走 routes._MISC_SWR_CACHE（过期返旧值 + 后台刷新），但**重启后
    # 首次 cold 仍要现场付一次**。放启动串行闸门里提前算好，重启后首开即命中。
    # 与 _prewarm_hot_scan 同样用 asyncio.run 在预热线程自己的 loop 里跑，
    # 失败只打日志（请求侧 cold 兜底仍在），不影响其它预热项。
    @_serial_prewarm
    def _prewarm_misc():
        import asyncio as _aio
        import time as _time
        try:
            from app.api import routes as _r
            from services.support import swr as _swr
            from services.ops import theme_index, cls_pool
            jobs = [
                (('market-index',), lambda: _r.check_market_crash(), 'market-index'),
                (('theme-index', 'auto', 0), lambda: theme_index.snapshot('auto', live=True), 'theme-index'),
                (('cls-ladder',), lambda: cls_pool.snapshot(live=True), 'cls-ladder'),
            ]
            for key, factory, label in jobs:
                try:
                    t0 = _time.time()
                    _aio.run(_swr.async_swr_get(
                        _r._MISC_SWR_CACHE, key, factory, ttl=60, stale_max=3600,
                        label=label, gap=60, max_keys=16))
                    print(f'[预热 {datetime.now():%H:%M:%S}] {label} 缓存完成（{_time.time() - t0:.1f}s）')
                except Exception as e:
                    print(f'[预热 {datetime.now():%H:%M:%S}] {label} 预热失败（首开会现算）: {e}')
            try:
                from services.data.kline_history import get_engine, history_available
                t0 = _time.time()
                eng = get_engine()
                stats = eng.get_db_stats(with_scope=True)
                stats['available'] = history_available()
                _swr.put(_r._MISC_SWR_CACHE, ('kline-stats',), stats, 16)
                print(f'[预热 {datetime.now():%H:%M:%S}] 本地K库统计缓存完成（{_time.time() - t0:.1f}s）')
            except Exception as e:
                print(f'[预热 {datetime.now():%H:%M:%S}] K库统计预热失败（首开会现算）: {e}')
            # 2026-11-04 P0-A：收评 518KB 的查库+序列化+ETag 计算提前付掉。
            # SWR 只保证「缓存有值」时不阻塞，不预热的话重启后第一个打开收评页的
            # 用户仍要付一次 cold（实测 133ms，在 to_thread 里不堵主循环但自己要等）。
            try:
                t0 = _time.time()
                _swr.swr_get(_r._DAILY_REVIEWS_CACHE, 'all',
                             _r._build_daily_reviews_entry,
                             ttl=_r._DAILY_REVIEWS_TTL, stale_max=_r._SWR_STALE_MAX,
                             label='daily-reviews', gap=60, max_keys=2)
                print(f'[预热 {datetime.now():%H:%M:%S}] 收评缓存完成（{_time.time() - t0:.1f}s）')
            except Exception as e:
                print(f'[预热 {datetime.now():%H:%M:%S}] 收评预热失败（首开会现算）: {e}')
        except Exception as e:
            print(f'[预热 {datetime.now():%H:%M:%S}] 分钟级数据源预热整体失败（不致命）: {e}')
    threading.Thread(target=_prewarm_misc, daemon=True, name='prewarm-misc').start()

    # 重计算冷缓存预热（2026-11-04 全站巡检第 2 轮）：SWR 只保证「缓存里有值」时不阻塞，
    # 重启后第一次访问仍会撞 cold。冷/热态基准实测重启后首访：
    #   /api/recommend-sim?bench=1 15.645s（要算基准曲线）、/api/fund-flow 6.664s、
    #   /api/custom-scan-pool 1.55s、/api/daily_recommendations 1.43s、
    #   /api/recommend-review 1.33s；热态对应 31ms / 18ms / 10ms / 47ms / 15ms。
    # 这些全是本地 SQLite + 日级数据，一天才变，放启动串行闸门里一次算好即可。
    @_serial_prewarm
    def _prewarm_heavy():
        import asyncio as _aio
        import time as _time
        try:
            from app.api import routes as _r
            from services.support import swr as _swr
            from services.pick import rec_sim, rec_sim_batch, recommend_review
            from app.config import (FUND_FLOW_STOCK_TOP_N, FUND_FLOW_SECTOR_TOP_N,
                                    FUND_FLOW_HIDDEN_CHANGE_THRESHOLD,
                                    FUND_FLOW_HIDDEN_HOT_TOP_N, FUND_FLOW_HIDDEN_TOP_N)

            # 1) 回测曲线：bench=0 / bench=1 两个前端在用的口径 + 拆档
            for bench in (False, True):
                try:
                    t0 = _time.time()
                    key = (3, 100000.0, 5.0, True, bench)
                    data = rec_sim.simulate(months=3, capital=100000.0,
                                            auction_max_gap=5.0, whole_lots=True,
                                            with_benchmark=bench)
                    _swr.put(_r._REC_SIM_CACHE, key, data, 12)
                    print(f'[预热 {datetime.now():%H:%M:%S}] 回测曲线 bench={int(bench)} '
                          f'缓存完成（{_time.time() - t0:.1f}s）')
                except Exception as e:
                    print(f'[预热 {datetime.now():%H:%M:%S}] 回测曲线 bench={int(bench)} 预热失败: {e}')
            try:
                t0 = _time.time()
                key = ('batch', 3, 100000.0, 5.0)
                data = rec_sim_batch.simulate_batch(months=3, capital=100000.0,
                                                    auction_max_gap=5.0)
                _swr.put(_r._REC_SIM_CACHE, key, data, 12)
                print(f'[预热 {datetime.now():%H:%M:%S}] 拆档回测缓存完成（{_time.time() - t0:.1f}s）')
            except Exception as e:
                print(f'[预热 {datetime.now():%H:%M:%S}] 拆档回测预热失败: {e}')

            # 2) 复盘汇总（30 / 120 两档）+ 历史推荐
            for days in (30, 120):
                try:
                    t0 = _time.time()
                    data = recommend_review.get_review_summary(days)
                    _r._remember_rec_review(days, data, _time.time())
                    print(f'[预热 {datetime.now():%H:%M:%S}] 复盘汇总 days={days} '
                          f'缓存完成（{_time.time() - t0:.1f}s）')
                except Exception as e:
                    print(f'[预热 {datetime.now():%H:%M:%S}] 复盘汇总 days={days} 预热失败: {e}')
            try:
                t0 = _time.time()
                payload = _aio.run(_r._build_daily_recs_payload())
                _r._DAILY_RECS_CACHE.update(ts=_time.time(), payload=payload)
                print(f'[预热 {datetime.now():%H:%M:%S}] 历史推荐记录缓存完成（{_time.time() - t0:.1f}s）')
            except Exception as e:
                print(f'[预热 {datetime.now():%H:%M:%S}] 历史推荐预热失败: {e}')

            # 3) 资金流快照（外部源冷算 6.7s；休市时服务内部会优先用 DB 收盘快照）
            try:
                from services.data.fundflow import get_fund_flow_summary
                t0 = _time.time()
                _aio.run(get_fund_flow_summary(
                    stock_top_n=FUND_FLOW_STOCK_TOP_N, sector_top_n=FUND_FLOW_SECTOR_TOP_N,
                    change_threshold=FUND_FLOW_HIDDEN_CHANGE_THRESHOLD,
                    hot_top_n=FUND_FLOW_HIDDEN_HOT_TOP_N, hidden_top_n=FUND_FLOW_HIDDEN_TOP_N))
                print(f'[预热 {datetime.now():%H:%M:%S}] 资金流快照缓存完成（{_time.time() - t0:.1f}s）')
            except Exception as e:
                print(f'[预热 {datetime.now():%H:%M:%S}] 资金流预热失败（首开会现算）: {e}')
        except Exception as e:
            print(f'[预热 {datetime.now():%H:%M:%S}] 重计算预热整体失败（不致命）: {e}')
    threading.Thread(target=_prewarm_heavy, daemon=True, name='prewarm-heavy').start()

    def job_update_hot_search_ranking():
        asyncio.run(update_hot_search_ranking_task())

    def job_scan_ma10():
        if not _today_is_trading():
            return
        asyncio.run(scan_and_remove_below_ma10())

    def job_build_candidate_pool():
        """盘前 9:00 全市场海选候选池（2026-10-15）。

        为什么排在 9:00：本地日K由 18:15 同步维护，9:00 已含昨收；而 9:45 是第一次
        生成推荐，海选必须在那之前落库，让 9:45 / 10:00 / 13:30 / 14:05 四次共用同一份名单。
        失败不静默：池子为空时打警告，因为"入口静默退化回热搜池"和当年因子权重
        学习崩 40 天是同一类事故 —— 看起来一切正常，其实是能力悄悄少了大半。
        """
        if not _today_is_trading():
            return
        try:
            pool = build_candidate_pool()
            if not pool:
                print(f'[定时任务 {datetime.now():%H:%M:%S}] [警告] 盘前海选为空，今日推荐退回热搜池'
                      f'（请检查 18:15 日K同步是否成功：python scripts/backfill_kline.py --mode sync）')
            else:
                print(f'[定时任务 {datetime.now():%H:%M:%S}] 盘前海选完成：{len(pool)} 只入池')
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 盘前海选异常: {e}')

    def job_push_t1_plan():
        """9:15 集合竞价：推"昨日冻结·今日可卖"预案（2026-10-15）。

        A股 T+1 —— 当天买入的票当天卖不掉，那天推"立即止损"除了让人难受没有别的用处
        （用户原话：当天买的我怎么卖，推我卖有毛用）。所以信号照记，提醒改到
        第二天他真正能挂单的 9:15 竞价窗口。
        """
        if not _today_is_trading():
            return
        for uid in (watch_targets() or [1]):
            try:
                asyncio.run(push_t1_plan(user_id=uid))
            except Exception as e:
                print(f'[定时任务 {datetime.now():%H:%M:%S}] T+1 预案推送异常(账号{uid}): {e}')

    def job_overnight_exit_advice():
        """9:15 竞价前：过夜仓"今天按默认出口该做什么"（2026-11-07，修⑥②，默认关）。

        与上面那班 T+1 预案的分工：预案只管"昨天盘中真的响过、因 T+1 压到今天"的票；
        这一班管的是**时间止损到期 / MA20 就在头顶 / P0 基线次日开盘卖**这三条从没响过的。
        两个开关独立（`OVERNIGHT_EXIT_ADVICE_ENABLED` 总开关、`OVERNIGHT_EXIT_PUSH_ENABLED`
        只决定推不推），关掉时这行日志都不会有——现状逐字节不变。
        """
        if not _today_is_trading():
            return
        try:
            from services.portfolio import overnight_exit
            if not overnight_exit.enabled():
                return
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 过夜出口提醒模块加载失败: {e}')
            return
        for uid in (watch_targets() or [1]):
            try:
                r = asyncio.run(overnight_exit.run(user_id=uid))
                if r.get('advised'):
                    print(f'[定时任务 {datetime.now():%H:%M:%S}] 过夜出口提醒(账号{uid}): '
                          f'{r}')
            except Exception as e:
                print(f'[定时任务 {datetime.now():%H:%M:%S}] 过夜出口提醒异常(账号{uid}): {e}')

    def job_generate_review():
        asyncio.run(generate_review_task())

    def job_backtest():
        asyncio.run(backtest_task())

    def job_scan_trend():
        # 趋势扫描：非交易日也可允许（让前端看到历史缓存+标记休市），但 API 拉取不到实时数据
        # 为避免浪费，非交易日跳过
        if not _today_is_trading():
            return
        # 2026-11-04 修复（数据整天冻结）：调度器跑在**独立线程**，原来用 force=False 调用会命中
        # stale-while-revalidate 分支——那一支立即返回旧数据，把重扫挂到 `asyncio.create_task` 上，
        # 而任务挂在本函数 `asyncio.run` 的**临时 loop** 上：asyncio.run 一返回就把它取消，
        # **扫描没跑完就不会落库**。实测证据：`server_supervised.log` 里当天 12 条"缓存过期…已启动后台
        # 重扫"（没有一条"已在跑"），而 age 每 30 分钟单调增长 1776s→14372s，说明每次都真的起了任务、
        # 只是从没跑完 → 当天数据冻结在第一次扫描（09:00:31）。注意 `_swr_refresh_inflight` 本身没有
        # 卡死：asyncio.run 收尾会取消挂起任务并执行它的 finally，探针与上面那 12 条日志都印证标志正常复位，
        # 所以别按"标志卡住"去排查。调度器的职责本就是按时刷新落库，这里直接 force=True 现扫：
        # 跑在本线程 loop 上、扫完即落库刷新，不再依赖后台任务。
        asyncio.run(scan_trend_task(force=True))

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
        asyncio.run(auto_generate_recommendations_task(source='schedule'))

    def job_recommend_review():
        """智能推荐复盘（本地K线库计算，无网络依赖，非交易日也可跑）"""
        try:
            from services.pick.recommend_review import run_recommendation_review
            run_recommendation_review()
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 推荐复盘失败: {e}')

    def job_rec_sim_daily():
        """可成交性日常监控（2026-10-18，实施计划 P0-0）：推荐复盘之后跑，
        把"同一批推荐在三种入口口径下各值多少钱"存成每天一个点。

        非交易日也跑没意义（推荐记录不会新增、日K也没变），所以只交易日跑；
        但它**不依赖任何网络**，失败也只是少一个监控点，不影响任何链路。
        """
        try:
            from services.pick import rec_sim
            from app.config import REC_SIM_DAILY_MONTHS
            from services.support import app_settings
            if not app_settings.value('rec_sim_daily_enabled'):
                print('[可成交性监控] 开关已关闭（复盘·口径与开关），本轮跳过')
                return
            rec_sim.record_daily(months=REC_SIM_DAILY_MONTHS)
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 可成交性监控失败: {e}')

    def job_rec_sim_batch_daily():
        """上午档/下午档拆批次监控（2026-10-21）：现有末批曲线只反映 14:45 名单，
        上午档（盘中灵活低吸）与下午档（涨停票次日竞价）是两种策略，混在一条曲线上
        互相污染。这里把两档四口径的当日摘要落 rec_sim_batch_daily，攒对照数据。
        只读本地库不依赖网络；当天日K 18:15~20:15 分五批同步，所以排 20:30 跑——
        排 16:26 会把当天名单整批判成"无当日K线"白白跳过（16:20 末批监控就有这个滞后，
        靠次日重算追平，拆档没必要重蹈）。"""
        try:
            from services.pick import rec_sim_batch
            r = rec_sim_batch.record_batch_daily()
            if r:
                w = r['window']
                print(f"[拆档回测] 落库 {r['rows']} 行（窗口 {w.get('since')}→{w.get('until')}，"
                      f"上午 {w.get('recCountAm')} 条 / 下午 {w.get('recCountPm')} 条）")
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 拆档回测落库失败: {e}')

    def job_exit_seal_shadow():
        """出口分叉影子落库（2026-11-05，影子台账第 15 条）：把当天推荐的三条出口
        （拿隔夜 / 当日收盘走 / "封住拿隔夜、没封住当天走"）各算一遍存 exit_seal_shadow。
        **只记录**——不改出口文案、不改推送、不改门槛。
        排 20:35：当天日K 18:15~20:15 分五批同步，20:30 那条拆档回测也确认了这个前提；
        再早就会把当天名单整批判成"缺当日K"。真缺了也不要紧，模块里两条腿都留 NULL
        并在下一轮自愈回补（`close_ret_pct IS NULL OR next_open_ret_pct IS NULL` 都捞）。"""
        try:
            from services.pick import exit_seal_shadow as _ess
            if not _ess.enabled():
                return
            r = _ess.run_daily()
            if r.get('error'):
                print(f"[出口分叉影子] 未落库: {r['error']}")
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 出口分叉影子落库失败: {e}')

    def job_zhaban_shadow():
        """炸板次数影子（2026-11-06，影子台账第 17 条）：用腾讯 1 分钟分时把东财涨停池的
        `zbc` 另算一遍，与东财真值并排落 `zhaban_shadow`。**只记录**——不改情绪评分、
        不改封板质量、不改门槛、不改推送。
        东财三池当天 0 行（pools 家族也停调的日子）就直接跳过：没有真值的对照没有裁决价值，
        把它写进表里会让人误以为"已经接成备源了"。"""
        try:
            from services.data import zhaban_shadow as _zs
            if not _zs.enabled():
                return
            r = _zs.run_day()
            if r.get('skipped'):
                print(f"[炸板影子] 跳过：{r['skipped']}")
            elif r.get('error'):
                print(f"[炸板影子] 未落库: {r['error']}")
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 炸板影子落库失败: {e}')

    def job_sector_persistence():
        """板块持续性预热（2026-10-22，主线判定的分母）。

        一次 SQL 读本地日K近11个交易日全市场（~2.7万行，1-2s），按国标行业聚合
        截尾均值复利收益，落 sector_persistence_daily。排两个点：09:05（昨收日线
        已由昨晚 18:15~20:15 同步到位，盘前主线要用）与 20:25（末批同步 20:15 之后，
        当天数据进窗口，明早读到的就是含今天的）。失败只掉持续性维，不拖别的。"""
        if not _today_is_trading():
            return
        try:
            from services.strategy.mainline import compute_sector_persistence
            n = len(compute_sector_persistence(force=True))
            print(f'[板块持续性] 计算完成：{n} 个行业板块入库')
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 板块持续性预热失败: {e}')

    def job_theme_sync():
        """主题篮子收盘落库（2026-10-31，10-31 晚改为动态链）。

        链不再是固定的机器人/商业航天/算力，而是 `hot_chains` 按财联社板块榜每天选出的
        涨停数/涨幅前 3 热门板块（见 services/ops/hot_chains.py）。两条顺序不能反：
        ① 先按当日板块榜重建链（15:10 `cls_plate` 已落库，这里非盘中优先读库，零外部请求）；
        ② 再逐链把成分当日 OHLC 落 theme_member_daily——链里创业板/科创板本地日K
        停更在 10-15/10-16，只能靠腾讯批量行情的交易所昨收逐日续写。

        排 15:12（板块榜落库后 2 分钟，行情已定格）与 15:47（失败重试一次）；两个点都只在
        交易日跑，sync_today 内部还有 15:02 时段闸门。动态链重建失败时保留上一次的链，
        并用它继续落库，不会出现"今天没有主题卡"。
        """
        if not _today_is_trading():
            return
        try:
            from services.ops import hot_chains
            n = hot_chains.refresh(force=True)
            print(f'[动态链] 收盘重建 {n} 条链')
        except Exception as e:
            print(f'[动态链] 收盘重建失败（沿用上一次的链）: {e}')
        try:
            import asyncio as _aio
            from services.ops.theme_index import sync_today, active_ids
            ids = active_ids()
            total = 0
            for tid in ids:
                try:
                    total += _aio.run(sync_today(tid))
                except Exception as error:
                    print(f'[主题温度] {tid} 落库失败: {error}')
            print(f'[主题温度] 收盘落库 {total} 行（{len(ids)} 条链）')
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 主题温度落库失败: {e}')

    def job_cls_ladder_sync():
        """财联社四池 + 板块榜落库（2026-10-31，影子/展示兜底）。

        15:10 收盘定版抓一次，15:45 再抓一次当重试（接口偶发 503）。只交易日跑。
        四池与东财涨停池口径不同（10-30：财联社 49 / 本地 51，47 只重合），只落
        财联社自己的原始字段；板块榜（行业 54 / 概念 398）同时落 `cls_plate_daily`，
        供资金流页概念板块与收评板块轮动兜底。两者都只展示，不进选股。
        """
        if not _today_is_trading():
            return
        try:
            import asyncio as _aio
            from services.ops.cls_pool import sync_today as _cls_sync
            saved = _aio.run(_cls_sync())
            print(f'[财联社梯队] 收盘落库 {saved} 行')
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 财联社梯队落库失败: {e}')
        try:
            from services.ops.cls_plate import sync_today as _plate_sync
            plates = _plate_sync()
            print(f'[财联社板块] 收盘落库 {plates} 行')
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 财联社板块落库失败: {e}')

    def job_sentiment_final():
        """情绪收盘定版落库（2026-11-03，#11 减仓回测的前置）。

        把"当天的最终档位"写成 `sentiment_final_daily` 一行（一天一行、幂等）。
        只新增旁路留痕，**不改任何现有读取路径**——页面与行动台仍走原来的读时兜底，
        所以这一步对线上表现零影响。
        15:30 抓一次（收评 15:10 已生成、优化器 15:35 之前），16:05 再试一次当重试：
        `persist_final_snapshot` 内部宁缺不假（当天既无当日收评也无当日快照就不写），
        所以重复跑无害，也不会拿昨天的档位顶替今天。
        """
        if not _today_is_trading():
            return
        try:
            from services.strategy.sentiment import persist_final_snapshot
            r = persist_final_snapshot()
            if not r.get('written'):
                print(f'[情绪定版] 本轮未落库：{r.get("reason")}')
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 情绪定版落库失败: {e}')

    def job_index_daily():
        """大盘指数日线落库（2026-11-03，#12 抗跌因子的前置）。

        把上证/深成/创业板指/沪深300 的日K落到本地 `index_daily`（腾讯 fqkline，
        一次可取 700 根，所以首次就能回填全历史）。只新增本地表、不改任何现有读取路径——
        现在读指数的地方仍读它自己的 kline_cache，切过来是下一步的事。
        单只指数失败不影响其他（sync_all 内部逐只 try）。仅交易日跑。
        """
        if not _today_is_trading():
            return
        try:
            from services.data.index_daily import sync_all
            r = sync_all()
            if r.get('disabled'):
                return
            print(f'[指数日线] 本轮写入 {sum(v for v in r.values() if isinstance(v, int))} 行')
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 指数日线同步失败: {e}')

    def job_shadow_eval_daily():
        """影子对照日常监控（2026-10-18，实施计划"工具：影子对照脚本"）：
        把"影子挡掉的票 T+1 是不是真的更差"每天存一个点，第1批 P0-2 的上线判据。

        实现留在 `scripts/shadow_eval.py` 一份（计划指定的位置）：命令行要用、页面也要读，
        搬进 services 反而要多一个包外路径依赖，所以这里按文件路径加载，不复制第二份逻辑。
        样本不足 SHADOW_EVAL_MIN_DAYS 个交易日时脚本自己只报数、不下结论，这里不重复判断。
        """
        try:
            import os as _os
            import importlib.util as _ilu
            from services.support import app_settings
            if not app_settings.value('shadow_eval_daily_enabled'):
                print('[影子对照] 开关已关闭（复盘·口径与开关），本轮跳过')
                return
            _dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
            spec = _ilu.spec_from_file_location('shadow_eval',
                                                _os.path.join(_dir, 'scripts', 'shadow_eval.py'))
            mod = _ilu.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.record_daily()
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 影子对照失败: {e}')

    def job_eval_lab_weekly():
        """每周六跑一次本地因子体检（2026-10-22）。

        只读 history_kline.db / gupiao.db，不联网。选周六是因为它要读全市场 140 万行日K，
        约 1 分钟；放盘中会占调度线程，放周末零代价。结果落 eval_lab_runs，页面/接口
        直接读最近一次，不必每次现算。

        2026-10-23 改：不再在调度线程里 import factor_lab（它依赖 numpy/pandas，
        服务的 reload worker 解释器不一定带，曾实测 500 No module named numpy）。
        改走 services.support.eval_lab.run_analysis() 的**子进程**，解释器自动探测，
        web 进程不背 pandas，也不让周六任务静默失败。
        """
        if datetime.now().weekday() != EVAL_LAB_WEEKDAY:
            return
        try:
            from services.support import app_settings
            if not app_settings.value('eval_lab_enabled'):
                print('[因子体检] 开关已关闭，本轮跳过')
                return
            from services.support.eval_lab import run_analysis
            r = run_analysis()
            rec5 = ((r.get('recommendations') or {}).get('ret5') or {}).get('excessMean')
            print(f"[因子体检] {r.get('runDate')} 完成（{r.get('interpreter')}）："
                  f"范围 {r.get('universe')} 只 / {r.get('rebalances')} 个截面；"
                  f"推荐 5 日超额 {rec5}%")
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 因子体检失败: {type(e).__name__}: {e}')

    def job_auction_snapshot():
        """09:25 集合竞价快照（2026-10-22）。失败只少一天数据，不阻塞推荐。"""
        if not _today_is_trading():
            return
        try:
            from services.support import app_settings
            if not app_settings.value('auction_snapshot_enabled'):
                return
            from services.data.auction import snapshot_universe
            r = snapshot_universe(batch=AUCTION_SNAPSHOT_BATCH)
            print(f"[竞价快照] {r.get('date')} 落库 {r.get('rows')} 只"
                  f"（范围 {r.get('scope')}，失败批次 {r.get('failedBatches')}）")
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 竞价快照失败: {e}')

    def job_auction_dip_morning():
        """竞价低吸臂影子·判定轮（2026-11-07，影子台账第 20 条）：把"昨日涨停池×今日竞价快照"
        按缺口分五档落 auction_dip_shadow。**只记录**——不改评分、不改门槛、不改推送。
        纯本地读（池子+快照都已落库），不发网络请求、不阻塞 09:45 推荐；快照没采到就跳过并写明原因
        （竞价事后补不回，一张错表比没有表更坏）。09:40 还有一班幂等补跑。"""
        try:
            from services.pick import auction_dip_shadow as _ads
            if not _ads.enabled():
                return
            r = _ads.run_morning()
            if r.get('skipped'):
                print(f"[竞价低吸影子] 跳过：{r['skipped']}")
            elif r.get('error'):
                print(f"[竞价低吸影子] 未落库: {r['error']}")
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 竞价低吸影子判定轮失败: {e}')

    def job_auction_dip_evening():
        """竞价低吸臂影子·回填轮（2026-11-07，台账第 20 条）：日K五批（18:15~20:15）之后，
        把日内腿/隔夜腿/合计腿/同日范围等权基准能补的补上，历史缺行一起自愈。
        排 20:40：在出口分叉 20:35 之后，两条影子各写各的表、互不依赖。"""
        try:
            from services.pick import auction_dip_shadow as _ads
            if not _ads.enabled():
                return
            r = _ads.run_evening()
            if r.get('error'):
                print(f"[竞价低吸影子] 回填失败: {r['error']}")
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 竞价低吸影子回填轮失败: {e}')

    def job_minute5_forward():
        """收盘后采集活跃票 5 分钟线（默认关）。"""
        if not _today_is_trading():
            return
        try:
            from services.support import app_settings
            if not app_settings.value('minute5_forward_enabled'):
                return
            from services.data.minute_forward import collect_active
            r = collect_active()
            print(f"[5分钟前向] 落库 {r.get('rows')} 行 / {r.get('symbols')} 只")
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 5分钟前向采集失败: {e}')

    def job_westock_crosscheck_daily():
        """双源数据交叉校验（2026-10-19，westock 权威源）：每日收盘后跑。

        本地资金流/行情 vs westock（腾讯自选股），权威值=westock
        （用户决策"不一致的以 westock 为准"）。行情层不一致=接口异常告警；
        资金流层只记录偏差与符号冲突（三口径定义不同）。全流程 1-2 分钟
        （97 只宇宙实测），串行+限频退避，不阻塞调度循环（脚本内同步跑完即返回，
        单轮 2 分钟内，晚于它的任务只有 18:15 日K同步，无冲突）。
        """
        try:
            import os as _os
            import importlib.util as _ilu
            from services.support import app_settings
            if not app_settings.value('westock_crosscheck_enabled'):
                print('[双源校验] 开关已关闭（复盘·口径与开关），本轮跳过')
                return
            _dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
            spec = _ilu.spec_from_file_location('westock_crosscheck',
                                                _os.path.join(_dir, 'scripts', 'westock_crosscheck.py'))
            mod = _ilu.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.run()
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 双源校验失败: {e}')

    def job_sync_industry_map():
        """行业分类周更（2026-10-19）：baostock 一次登录把全市场行业落到 stock_industry_map。

        为什么以**子进程**跑：脚本要 bs.login()/logout() 并等一次全量查询，
        而调度循环是单线程的 asyncio —— 在里面同步等会把 09:00 海选/09:45 推荐一起拖住。
        为什么要这张表：10-19 东财 clist 被封一整天，资金流向页「行业」列全空、
        板块资金流聚合也没了分母。行业分类几个月才动一次，不该依赖一条会被封的行情接口。
        """
        try:
            import os as _os
            import subprocess
            import sys as _sys
            server_dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
            script = _os.path.join(server_dir, 'scripts', 'sync_stock_industry.py')
            log = _os.path.join(server_dir, 'analysis_out', 'industry_sync.log')
            _os.makedirs(_os.path.dirname(log), exist_ok=True)
            with open(log, 'a', encoding='utf-8') as fh:
                flags = 0
                if _os.name == 'nt':
                    flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
                p = subprocess.Popen([_sys.executable, '-u', script], cwd=server_dir,
                                     stdout=fh, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, creationflags=flags)
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 行业分类周更已拉起（pid {p.pid}，'
                  f'日志 server/analysis_out/industry_sync.log）')
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 行业分类周更拉起失败: {e}')

    def job_pre_close_snapshot():
        """收盘前推荐快照（2026-10-12）：14:45 用最新数据最终生成一次并自动保存。

        用户需求：不管定时生成还是手动生成，收盘前都要把"最后一次推荐"落库便于复盘。
        任务内部本就会保存（覆盖当日记录），这里额外做落库校验：
        - 生成成功 → 校验今日记录存在，打印最终推荐名单
        - 本次无合格候选 → 保留此前最后一份有效推荐（不删旧记录），日志说明
        - 今日完全无记录 → 打警告（本次与历史生成均未产出）
        """
        if not _today_is_trading():
            return
        print(f'[定时任务 {datetime.now():%H:%M:%S}] 收盘前推荐快照：开始最终生成...')
        try:
            asyncio.run(auto_generate_recommendations_task(source='pre_close'))
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 收盘前快照生成异常: {e}')
        # 落库校验
        try:
            from db.database import load_daily_recommendations
            today_str = datetime.now().strftime('%Y-%m-%d')
            daily = load_daily_recommendations()
            today_rec = next((r for r in daily if r.get('date') == today_str), None)
            recs = (today_rec or {}).get('daily_recommendations') or []
            if recs:
                names = '、'.join(f"{s.get('name')}({s.get('symbol')})" for s in recs)
                print(f'[定时任务 {datetime.now():%H:%M:%S}] 收盘前快照完成：今日推荐已保存 → {names}')
            elif today_rec is not None:
                print(f'[定时任务 {datetime.now():%H:%M:%S}] 收盘前快照：今日无合格推荐（沿用当日空记录）')
            else:
                print(f'[定时任务 {datetime.now():%H:%M:%S}] 收盘前快照警告：今日无任何推荐记录（本次与历史生成均未产出）')
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 收盘前快照校验失败: {e}')

    def job_update_holder_num():
        # 股东人数是月度数据；2026-10-19 降频：接口取的是"最新一期"，一期披露后内容不再变，
        # 每天重拉全市场 ~12 页（pageSize=500；旧表 100 条/页要 50+ 页）只是把同样的行搬一遍，
        # 而东财 datacenter 正是 IP 风控的来源。
        # 非刷新日直接退出（一行日志，零请求）；财报季自动恢复每日。原注释"非交易日也可拉取"
        # 的意图保留：刷新日不看是否交易日，周末披露的公告照样能在下一个刷新日入库。
        if not _slow_data_due(SLOW_DATA_HOLDER_NUM_WEEKDAYS):
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 股东人数：今天不是刷新日'
                  f'（节奏 {_weekday_names(SLOW_DATA_HOLDER_NUM_WEEKDAYS)}，'
                  f'财报季 {"、".join(str(m) for m in SLOW_DATA_DAILY_MONTHS)} 月每日），跳过')
            return
        asyncio.run(update_holder_num_task())

    def job_refresh_market_cap():
        """每周刷新总市值表（2026-10-16 防量化操控小微盘）。
        市值只从东财单只 stock/get 拿（clist/ulist 批量都不返回），按主板+非ST清单逐只拉；
        默认只补缺失（resumable），被限流下次继续。非交易日也跑（市值与是否交易日无关）。"""
        try:
            from services.data.market_cap import refresh as _refresh_cap
            r = _refresh_cap()
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 市值刷新完成：新入库 {r.get("ok")}，'
                  f'本轮尝试 {r.get("total")}，未取到 {r.get("fail")}'
                  + (f'（来源 {r.get("source")}）' if r.get('source') else '')
                  + (f'（{r.get("reason")}）' if r.get('reason') else ''))
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 市值刷新异常: {e}')

    def job_update_fundamental():
        # 基本面季度披露（ROE/净利同比/EPS），2026-10-19 降频：非财报季每周一拉一次即可，
        # 新披露的财报最多晚 6 天入库；财报季（1/3/4/7/8/10 月）自动恢复每日。
        if not _slow_data_due(SLOW_DATA_FUNDAMENTAL_WEEKDAYS):
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 基本面：今天不是刷新日'
                  f'（节奏 {_weekday_names(SLOW_DATA_FUNDAMENTAL_WEEKDAYS)}，'
                  f'财报季 {"、".join(str(m) for m in SLOW_DATA_DAILY_MONTHS)} 月每日），跳过')
            return
        asyncio.run(update_fundamental_task())

    def job_update_valuation():
        # 估值百分位（PE/PB）——PE/PB = 收盘价 × 报表数，只在有收盘价变化的日子才变，
        # 所以保持每日，只去掉周末/节假日的空跑（那两天拉回来的是同一份数据）。
        # 开关 SLOW_DATA_VALUATION_TRADING_ONLY=False 即退回 7 天无休（改造前行为）。
        if SLOW_DATA_VALUATION_TRADING_ONLY and not _today_is_trading():
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 估值分位：非交易日（没有新收盘价，'
                  f'拉回来还是同一份），跳过')
            return
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
        from services.ops.db_maintenance import cleanup_old_cache
        try:
            cleanup_old_cache()
        except Exception as e:
            print(f'[DB维护] 异常: {e}')

    def job_db_backup():
        """每日 03:30 自动备份 SQLite（2026-11-05，P0-3 资产不丢）。

        库是唯一不可再生产资产（日K库 507MB+），此前仅靠两个手动目录、scheduler 零调用。
        周六跑全量、平日跑增量（对 SQLite 都是整库在线拷贝，区别只在 manifest 标记）；
        保留最近 7 天、硬上限 30 份。用 sqlite3 在线备份 API，服务在写也不阻塞。
        以脱离进程方式拉起（与 job_matrix_rerun 同款），不阻塞调度线程。
        """
        import subprocess, sys
        server_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = os.path.join(server_dir, 'scripts', 'db_backup.py')
        if not os.path.exists(script):
            print(f'[定时任务 {datetime.now():%H:%M:%S}] [警告] 备份脚本缺失: {script}')
            return
        # 周六全量，其余增量
        mode = 'full' if datetime.now().weekday() == 5 else 'incremental'
        log = os.path.join(server_dir, 'analysis_out', 'db_backup.log')
        try:
            os.makedirs(os.path.dirname(log), exist_ok=True)
            flags = 0
            if os.name == 'nt':
                flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
            with open(log, 'a', encoding='utf-8') as fh:
                p = subprocess.Popen([sys.executable, '-u', script, '--mode', mode],
                                     cwd=server_dir, stdout=fh, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, creationflags=flags)
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 数据库自动备份已拉起'
                  f'（{mode}，pid {p.pid}，日志 analysis_out/db_backup.log）')
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] [警告] 数据库备份拉起失败: {e}')

    def job_api_usage_flush():
        """每 60s 把内存计数器落库（api_usage_daily），跨天自动归档，防丢当日计数。"""
        try:
            from services.data import api_usage
            n = api_usage.flush()
            if n:
                print(f'[定时任务 {datetime.now():%H:%M:%S}] 接口用量落库 {n} 个接口')
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 接口用量落库失败: {e}')

    def _alert_kline_stale(sync_err=None, final=True):
        """盘后自检：今天该有 K 线，库里却没有 —— 就别让它悄悄过去。

        为什么要专门写这条：2026-10-15 那次同步报 disk I/O error 后，日志只留下一行
        "本地库同步/RPS刷新异常"，没人注意到后果是 rps_cache 的更新时间还停在
        **2026-09-28 09:45**（当晚查出来的实测值），也就是评分用的 120 日相对强度已经
        十几天没换过水，而页面上一切看起来正常。这跟当年因子权重停摆 40 天是同一类事故。
        """
        if not _today_is_trading() or not final:
            return
        try:
            from services.data.kline_history import get_engine
            stats = get_engine().get_db_stats(with_scope=True) or {}
        except Exception as e:
            print(f'[盘后自检] 读不到本地日K库统计，跳过自检: {e}')
            return
        latest = stats.get('latestDate') or ''
        today = datetime.now().strftime('%Y-%m-%d')
        # 分母用"选股范围"（主板 + 非ST/退 + 市值≥35亿，实测 2462 只），不用整库 5222：
        # 范围外的票 2026-10-16 起不再增量同步（存量冻结），拿 5222 当分母会把正常状态永远报成故障。
        uni_n = stats.get('universeCount') or 0
        covered = stats.get('latestDayCovered') or 0
        cov = (covered / uni_n) if uni_n else 1.0
        if latest >= today and cov >= KLINE_POOL_MIN_COVERAGE:
            return
        if latest >= today:
            why = (f'当天在选股范围里只落了 {covered}/{uni_n} 只 = {cov*100:.1f}%'
                   f'（< {KLINE_POOL_MIN_COVERAGE*100:.0f}%，同步像跑到一半）')
        else:
            why = f'今天（{today}）这根K线没进来' + (f'（同步异常：{sync_err}）' if sync_err else '')
        print(f'[盘后自检] [警告] 本地日K库最新 {latest}，{why}')
        key = f'{today}|{latest}|{covered}'
        if key in _KLINE_ALERTED:
            return
        _KLINE_ALERTED.add(key)
        text = (f'⚠️ 盘后数据自检：本地日K库最新 {latest}，{why}。'
                + (f'\n同步异常：{sync_err}' if sync_err else '')
                + '\n影响：明早 09:00 盘前海选、RPS、趋势评分都缺最新一天'
                  '（候选池的覆盖度门槛会自动拒绝出池、退回热搜池，不会拿半截数据选票）。'
                + f'\n口径：分母＝选股范围（沪深主板·非ST/退·总市值≥35亿）{uni_n} 只，'
                  '整库 5222 只里范围外的已停止增量同步。'
                + '\n处理：在办公室电脑 cd D:\\Desktop\\guizhou\\gupiao\\server 后执行 '
                  'python scripts/backfill_kline.py --mode sync'
                  f'（补指定某天用 --date {today}；若日志出现「黑名单用户」说明被 baostock 限流，'
                  '等 1~2 小时再跑，越重试越久）')
        for uid in watch_targets():
            try:
                # 系统健康告警：personal=False 允许未绑通道的账号回落全局群，
                # 否则真实用户没绑推送时这条最关键的自检会彻底丢失。
                send_to_user(uid, text, personal=False)
            except Exception as e:
                print(f'[盘后自检] 账号{uid} 告警推送失败: {e}')

    def job_sync_history():
        """A 级优化：每日盘后增量同步本地历史日K线库 + 同步后向量化刷新 RPS。

        本地库由 baostock 前复权数据维护，最新价=实盘价与实时行情无缝；RPS 从此本地库向量化计算（秒级、免东方财富限流）；
        首次回填需手动跑 `python scripts/backfill_kline.py --mode backfill`，之后每日增量即可。

        2026-10-16 改动：同步与 RPS 刷新拆成两段 try，并在结尾做一次新鲜度自检。
        以前两者共用一个 try —— 同步一抛异常（10-15 就是 disk I/O error），RPS 连刷新都被
        跳过，结果就是"库没更新 + 评分口径也没更新"，而日志只有一行异常摘要。
        """
        if not KLINE_HISTORY_ENABLED:
            return
        today_str = datetime.now().strftime('%Y-%m-%d')

        def _cov_now():
            """(选股范围内已覆盖, 范围总数) —— 判断"今天这根K线到底齐不齐"的唯一依据。"""
            try:
                from services.data.kline_history import get_engine as _ge
                st = _ge().get_db_stats(with_scope=True) or {}
                u = st.get('universeCount') or 0
                c = st.get('latestDayCovered') or 0
                if st.get('latestDate') != today_str:
                    return 0, u or 1
                return c, u or 1
            except Exception:
                return 0, 1

        # 已经等到今天且覆盖度达标才跳过（18:15 成功的话，18:45/19:15 不必再跑一遍全市场）。
        # 只看 MAX(date) 会被半截的一天骗过去，所以这里问的是"范围内覆盖到几成"。
        c0, u0 = _cov_now()
        if c0 / u0 >= KLINE_POOL_MIN_COVERAGE:
            if datetime.now().strftime('%H:%M') != KLINE_HISTORY_SYNC_TIME:
                print(f'[定时任务 {datetime.now():%H:%M:%S}] 本地日K已是 {today_str}（覆盖 {c0}/{u0}），本轮跳过')
            return

        # ── 主路：腾讯批量收盘快照（零登录，41 次请求拿全市场） ──
        # 2026-10-16 换架构的原因：baostock 要会话、逐只查、失败还得重登，
        # 结果当天被自己拖进 `10001011 黑名单用户`，整晚停在 1109/2462 只。
        # 收盘快照这条路没有"登录频率"这个概念，被拉黑这件事从此与每日数据无关。
        sync_err = None
        try:
            from services.data import eod_snapshot
            st = eod_snapshot.sync_snapshot(today_str)
            c1, u1 = _cov_now()
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 收盘快照(主路)：拿到 {st.get("got")} 只、'
                  f'写入 {st.get("written")} 条、停牌跳过 {st.get("stale_stamp", 0) + st.get("zero_vol", 0)} 只、'
                  f'除权待重算 {st.get("restated")} 只 → 覆盖 {c1}/{u1}'
                  + (f'｜[警告] 拒写: {st.get("rejected")}' if st.get('rejected') else ''))
            if st.get('rejected'):
                sync_err = f"收盘快照与备路口径不符: {st['rejected']}"
        except Exception as e:
            sync_err = e
            print(f'[定时任务 {datetime.now():%H:%M:%S}] [警告] 收盘快照(主路)异常: {type(e).__name__}: {e}')

        # ── 备路：baostock 增量同步（仅在主路没把覆盖度喂够时才敲门；内部有封禁冷却门禁） ──
        c1, u1 = _cov_now()
        if c1 / u1 < KLINE_POOL_MIN_COVERAGE:
            left = 0.0
            try:
                from services.data.kline_history import ban_cooldown_remaining
                left = ban_cooldown_remaining()
            except Exception:
                pass
            if left > 0:
                print(f'[定时任务 {datetime.now():%H:%M:%S}] 主路覆盖 {c1}/{u1} 不足，'
                      f'但 baostock 仍在冷却（剩 {int(left)} 分钟）—— 本轮不敲门，'
                      f'剩余时刻与明早重试会继续补')
            else:
                try:
                    from services.data.kline_history import get_engine
                    get_engine().sync_today_bulk()
                except Exception as e:
                    sync_err = sync_err or e
                    print(f'[定时任务 {datetime.now():%H:%M:%S}] [警告] 本地日K增量同步(备路)异常: {e}')
        try:
            from services.data.kline_history import history_available
            if history_available():
                from services.strategy.rps import refresh_rps
                res = asyncio.run(refresh_rps(RPS_PERIOD_DAYS))
                print(f'[定时任务 {datetime.now():%H:%M:%S}] 本地库同步后 RPS 刷新完成：{res}')
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] [警告] RPS 向量化刷新异常: {e}')
        # 流动性统计跟着日K走（2026-11-04）：日K刚写进来，20 日窗口就该换新一天，否则保活兜底
        # 最长要 6 小时才补——选票范围的"流动性"那层会整天停在昨天的窗口上。单独一段 try：
        # 它失败只让范围变旧，不拖住日K同步的结论（与上面的 RPS、下面的名录同法）。
        try:
            from services.data import liquidity as _liq
            _t = time.time()
            _n = len(_liq.stats_map(force=True))
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 本地日K同步后 流动性统计刷新完成：'
                  f'{_n} 只（{time.time() - _t:.1f}s）')
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] [警告] 流动性统计刷新异常'
                  f'（选票范围会用旧窗口，最长到保活兜底 6 小时）: {e}')
        # 名录（代码→名称、在市/退市、ST 标记）每天跟着刷一次：股票范围里"剔除 ST/*ST"
        # 完全依赖这张表，而 ST 帽子是随时戴随时摘的 —— 名录过期就等于范围失效。
        # 单独一段 try，且放在新鲜度自检之前：它失败只让范围变旧，不该拖住日K同步的结论。
        try:
            # 2026-10-31 修：`from sync_stock_basic import sync` 从来就没成功过（模块在
            # server/scripts/ 下，不在 sys.path 里），每天只往日志里写一条 ImportError，
            # stock_basic_full 的 fetched_at 停在 2026-10-16 —— 而范围里"剔除 ST/*ST"
            # 全靠这张表。改为按文件路径加载（与 shadow_eval / westock_crosscheck 同法）。
            import os as _os
            import importlib.util as _ilu
            from services.data.kline_history import ban_cooldown_remaining
            left = ban_cooldown_remaining()
            if left > 0:
                print(f'[定时任务 {datetime.now():%H:%M:%S}] 股票名录同步跳过：'
                      f'baostock 仍在冷却（剩 {int(left)} 分钟），不为名录续封')
            else:
                _dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
                _spec = _ilu.spec_from_file_location(
                    'sync_stock_basic', _os.path.join(_dir, 'scripts', 'sync_stock_basic.py'))
                _mod = _ilu.module_from_spec(_spec)
                _spec.loader.exec_module(_mod)
                n = _mod.sync()
                if n == -2:
                    print(f'[定时任务 {datetime.now():%H:%M:%S}] 股票名录同步跳过（baostock 冷却）')
                elif n < 0:
                    print(f'[定时任务 {datetime.now():%H:%M:%S}] [警告] 股票名录同步失败（ST 判定可能过期）')
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] [警告] 股票名录同步异常: {e}')
        # 只有最后一个重试时刻还没等到数据才算真失败（16:05 那种"baostock 还没出当天EOD"
        # 不是故障，半夜叫警报只会让人以为系统坏了）
        _alert_kline_stale(sync_err, final=datetime.now().strftime('%H:%M') >= KLINE_HISTORY_SYNC_LAST)

    def job_restate_queue():
        """消费除权重算队列（2026-11-01）。

        这张表以前只有入口没有出口：收盘快照发现"腾讯昨收 ≠ 库内最后一根"就写一行，
        注释说"交给 baostock 逐只重灌"，可 baostock 10-16 就被拉黑了 —— 11-01 翻出来时
        126 只票的 done 全是 NULL，等于 126 只除权票的历史前复权序列一直停在旧因子上，
        跨除权日的 T+N 收益全是假数（诊断文档里 −34% 的"极端亏损"就是这么来的）。

        放在 21:30：晚于 18:15~20:15 五轮日K同步，夜里腾讯限流压力也最小。
        每晚最多 KLINE_RESTATE_LIMIT 只，处理不完的留在队里等下一晚；
        `restate_one` 自己会回验（连续两根与腾讯不一致就不写库、保持 done=NULL）。
        """
        try:
            from services.data import restate_queue
            st = restate_queue.run(limit=KLINE_RESTATE_LIMIT)
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 除权重算：成功 {st["ok"]}（无需调整 {st["noop"]}）、'
                  f'跳过 {st["skip"]}、失败 {st["fail"]}，改 {st["rows"]} 根K线'
                  + (f'｜[警告] {st["aborted"]}' if st.get('aborted') else ''))
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] [警告] 除权重算异常: {type(e).__name__}: {e}')

    def job_matrix_rerun():
        """一次性把买法四张表重跑成"选股范围 2462 只"口径（用户 2026-10-16 选定：冷却结束后自动跑）。

        为什么必须以**脱离进程**跑：四张表要几十分钟，而 `schedule` 是单线程轮询 ——
        直接在调度线程里等就等于推迟 09:00 海选和 09:45 推荐。
        为什么现在还要跑：今晚改分母（5222→2462）之后分时那张还没出数，而它要补的 5 分钟线
        依赖 baostock，本机 IP 又刚被判黑名单，所以排在冷却解除之后（脚本里会先看冷却状态）。
        幂等由 `analysis_out/.done_matrix_2462` 标记保证：四张都出数就不再重复。
        """
        if not MATRIX_RERUN_ENABLED:
            return
        import os
        import subprocess
        import sys
        server_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = os.path.join(server_dir, 'scripts', 'run_matrix_rerun.py')
        flag = os.path.join(server_dir, 'analysis_out', '.done_matrix_2462')
        if os.path.exists(flag):
            return                      # 已经完成，别每天再拉一次
        log = os.path.join(server_dir, 'analysis_out', 'rerun.log')
        try:
            os.makedirs(os.path.dirname(log), exist_ok=True)
            with open(log, 'a', encoding='utf-8') as fh:
                flags = 0
                if os.name == 'nt':
                    flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
                p = subprocess.Popen([sys.executable, '-u', script], cwd=server_dir,
                                     stdout=fh, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, creationflags=flags)
            print(f'[定时任务 {datetime.now():%H:%M:%S}] 买法矩阵重跑已拉起（pid {p.pid}，'
                  f'日志 server/analysis_out/rerun.log；脚本会自己等 baostock 冷却解除）')
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] [警告] 买法矩阵重跑拉起失败: {e}')

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

    # 盘前全市场海选候选池（2026-10-15）：9:00 出池，供 9:45/10:00/13:30/14:05 四次生成共用
    schedule.every().day.at(CANDIDATE_POOL_TIME).do(job_build_candidate_pool)

    # T+1 预案（2026-10-15）：昨天买的今天才能卖，提醒时点放在 9:15 竞价
    schedule.every().day.at(PUSH_T1_PLAN_TIME).do(job_push_t1_plan)
    # 过夜默认出口提醒（2026-11-07，修⑥②）：排在 T+1 预案之后，同一分钟两班各管各的
    schedule.every().day.at(PUSH_T1_PLAN_TIME).do(job_overnight_exit_advice)

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

    # 智能推荐复盘闭环（2026-10-12）：本地K线库16:00同步后跑，
    # 计算全部历史推荐的 T+1/T+3/T+5 胜率与盈亏并持久化，供 API/前端展示 + 黑名单避让
    schedule.every().day.at(RECOMMEND_REVIEW_TIME).do(job_recommend_review)

    # 可成交性日常监控（2026-10-18，实施计划 P0-0）：推荐复盘之后跑，
    # 把「同一批推荐在三种入口口径下各值多少钱」存成每天一个点（窗口固定才可比）
    schedule.every().day.at(REC_SIM_DAILY_TIME).do(_warp_trading_only(job_rec_sim_daily))

    # 上午档/下午档拆批次监控（2026-10-21）：排在日K同步批次（18:15~20:15）之后，
    # 当天名单才不会被判"无当日K线"；落 rec_sim_batch_daily
    schedule.every().day.at('20:30').do(_warp_trading_only(job_rec_sim_batch_daily))

    # 出口分叉影子（2026-11-05，台账第 15 条）：同样必须排在日K五批（18:15~20:15）之后
    schedule.every().day.at('20:35').do(_warp_trading_only(job_exit_seal_shadow))

    # 板块持续性预热（2026-10-22，主线判定的分母）：09:05 供盘前用、20:25 收当天
    schedule.every().day.at('09:05').do(_warp_trading_only(job_sector_persistence))
    schedule.every().day.at('20:25').do(_warp_trading_only(job_sector_persistence))

    # 影子对照日常监控（2026-10-18，实施计划"工具"）：16:25 日K已同步，
    # 把「影子挡掉的票 T+1 是不是真的更差」存成每天一个点 —— 第1批 P0-2 的上线判据
    schedule.every().day.at(SHADOW_EVAL_TIME).do(_warp_trading_only(job_shadow_eval_daily))

    # 09:25 集合竞价快照（2026-10-22）：只在竞价窗口内写，晚跑宁可不写；
    # 采 2462 只约 41 次请求、10 秒内完成，9:45 推荐前早已结束
    schedule.every().day.at(AUCTION_SNAPSHOT_TIME).do(_warp_trading_only(job_auction_snapshot))

    # 竞价低吸臂影子（2026-11-07，台账第 20 条）：9:31 判定（快照 9:25 采完之后）、9:40 幂等补一班，
    # 20:40 回填三条腿（日K五批 18:15~20:15 之后）；只写旁路表，不改评分/门槛/推送
    schedule.every().day.at(AUCTION_DIP_MORNING_TIME).do(_warp_trading_only(job_auction_dip_morning))
    schedule.every().day.at(AUCTION_DIP_RETRY_TIME).do(_warp_trading_only(job_auction_dip_morning))
    schedule.every().day.at(AUCTION_DIP_EVENING_TIME).do(_warp_trading_only(job_auction_dip_evening))

    # 收盘后 5 分钟线前向采集（2026-10-22，默认关）：只采当天推荐/持仓/海选池活跃票，
    # 15:05 跑完不耽误 15:10 收评；替代已被 baostock 判黑卡住的逐只分钟线
    schedule.every().day.at(MINUTE5_FORWARD_TIME).do(_warp_trading_only(job_minute5_forward))

    # 因子体检周任务（2026-10-22）：job 内部再按 EVAL_LAB_WEEKDAY 判断，
    # 注册在每天 08:30 是为了跨进程重启后不用重新算"下一个周六"
    schedule.every().day.at(EVAL_LAB_TIME).do(job_eval_lab_weekly)

    # 双源数据交叉校验（2026-10-19）：16:30 本地 vs westock（权威源），
    # 落 westock_snapshot + data_cross_check，行情不一致告警、资金流记录符号冲突
    schedule.every().day.at(WESTOCK_CROSSCHECK_TIME).do(_warp_trading_only(job_westock_crosscheck_daily))

    # 收盘前推荐快照（2026-10-12）：14:45 用最新数据最终生成一次并自动保存，
    # 保证"最后一次推荐"落库可复盘（用户明确需求）
    schedule.every().day.at(SCHEDULE_PRE_CLOSE_SNAPSHOT_TIME).do(job_pre_close_snapshot)

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

    # A 级优化：每日盘后增量同步本地历史日K线库 + 向量化刷新 RPS（避开估值 18:10 并发）
    # 时刻是一串而不是一个：baostock 当日 EOD 落地时间不固定（2026-10-16 实测 16:00 跑写 0 条），
    # 所以 18:15 起每 30 分钟重试，谁先等到数据谁做完，后面自动跳过；最后一个时刻仍没有才告警。
    if KLINE_HISTORY_ENABLED:
        # 仅交易日执行：非交易日无新 K 线，跑也是 0 条；且 RPS 重算无意义
        for _t in KLINE_HISTORY_SYNC_TIMES:
            schedule.every().day.at(_t).do(_warp_trading_only(job_sync_history))

    # 除权重算队列消费（2026-11-01）：排在五轮日K同步之后，每晚最多 KLINE_RESTATE_LIMIT 只。
    # 不挂交易日门控：周末/节假日跑也是在还旧账（队列里只有收盘快照发现的除权票）。
    schedule.every().day.at(KLINE_RESTATE_TIME).do(job_restate_queue)

    # 总市值表每周刷新（2026-10-16）：选票/K线/分时线据此剔除 <35亿 小微盘，防量化操控；非交易日也跑
    # 2026-11-07 起两班（周日 + 周三）：`refresh()` 的单轮上限是 1600 只（避开 10-16 那次
    # "猛拉 1100 只被限流"的形状），而待补口径是"缺失或超龄 7 天"≈ 全表 3054 只，
    # 一班拉不完，两班才保证每只票实际刷新周期 ≤7 天——否则又回到"表冻结在某一天的价格"。
    getattr(schedule.every(), MARKET_CAP_REFRESH_WEEKDAY).at(MARKET_CAP_REFRESH_TIME).do(job_refresh_market_cap)
    getattr(schedule.every(), MARKET_CAP_REFRESH_WEEKDAY2).at(MARKET_CAP_REFRESH_TIME).do(job_refresh_market_cap)

    # 行业分类周更（2026-10-19）：东财被封时，资金流向的行业列与板块聚合都靠这张本地表
    getattr(schedule.every(), INDUSTRY_SYNC_WEEKDAY).at(INDUSTRY_SYNC_TIME).do(job_sync_industry_map)

    # 买法矩阵按"选股范围 2462"重跑（一次性，跑成功写标记后自动不再触发）；非交易日也要跑
    schedule.every().day.at(MATRIX_RERUN_TIME).do(job_matrix_rerun)

    # 主题篮子收盘落库（2026-10-31）：15:12 在板块榜(15:10)之后重建动态链并落成分日K，
    # 15:47 失败重试一次（仅交易日）。盘中跟随见主循环的 hot_chains.tick()。
    schedule.every().day.at("15:12").do(job_theme_sync)
    schedule.every().day.at("15:47").do(job_theme_sync)
    # 财联社题材梯队（2026-10-31，影子）：15:10 收盘定版落库，15:45 跟着主题链一起重试
    schedule.every().day.at("15:10").do(job_cls_ladder_sync)
    schedule.every().day.at("15:45").do(job_cls_ladder_sync)
    # 炸板次数影子（2026-11-06，台账第 17 条）：15:52 排在 15:45/15:47 那两条之后，
    # 此时收盘分时已完整、东财三池盘中已落库；一轮 ~54 只 × 0.15s ≈ 20 秒，只写旁路表。
    schedule.every().day.at(ZHABAN_SHADOW_TIME).do(_warp_trading_only(job_zhaban_shadow))
    # 情绪收盘定版（2026-11-03，#11 减仓回测的前置）：15:30 抓一次、16:05 重试一次（仅交易日）。
    # 只写旁路表 sentiment_final_daily，不改任何读取路径；一天一行幂等，重复跑无害。
    schedule.every().day.at(SENTIMENT_FINAL_SNAPSHOT_TIME).do(job_sentiment_final)
    schedule.every().day.at("16:05").do(job_sentiment_final)
    # 大盘指数日线（2026-11-03，#12 前置）：15:15 收盘后同步（收评 15:10 后、回测 15:20 前，仅交易日）。
    # 一次可取 700 根，首日即回填全历史；只写本地新表，不改现有读取路径。
    schedule.every().day.at(INDEX_DAILY_SYNC_TIME).do(job_index_daily)

    # 企业微信选股推送：开盘前 9:25 推送今日关注池预览（仅交易日）
    schedule.every().day.at(WECOM_PUSH_PREVIEW_TIME).do(job_push_daily_preview)

    # 数据库维护：每周六凌晨3点清理过期缓存+VACUUM（防 SQLite 膨胀）
    schedule.every().saturday.at("03:00").do(job_db_maintenance)
    print(f'[定时任务]   - 03:30: 数据库自动备份（周六全量/平日增量，保留7天，analysis_out/db_backup_*）')

    # 数据库自动备份：每日 03:30（周六全量/平日增量，保留7天；2026-11-05 P0-3 资产不丢）
    schedule.every().day.at("03:30").do(job_db_backup)

    # 接口用量：每 60s 把内存计数器落库（2026-10-19，防第三方平台封禁的观测手段）
    schedule.every(60).seconds.do(job_api_usage_flush)
    print(f'[定时任务]   - 每 60s: 接口用量计数落库（第三方 API 调用观测，防封禁）')

    _now = datetime.now()
    print(f'\n[定时任务 {_now:%Y-%m-%d %H:%M:%S}] 已注册以下定时任务（今日交易日={_today_is_trading()}）：')
    print(f'[定时任务]   - {"/".join(SCHEDULE_UPDATE_HOT_SEARCH_TIMES)}: 更新热搜榜（仅交易日）')
    print(f'[定时任务]   - {",".join(trend_scan_times)} (每{SCHEDULE_TREND_SCAN_INTERVAL_MIN}分钟): 自动扫描趋势股（仅交易日）')
    print(f'[定时任务]   - {SCHEDULE_MA10_CHECK_TIME}: 检查10日线+连跌天数并清理股票池（仅交易日）')
    print(f'[定时任务]   - {CANDIDATE_POOL_TIME}: 盘前海选候选池（范围：沪深主板、剔除ST，'
          f'上限 {CANDIDATE_POOL_MAX} 只，喂给 {"/".join(SCHEDULE_RECOMMENDATION_TIMES)} 的推荐）')
    print(f'[定时任务]   - {PUSH_T1_PLAN_TIME}: T+1 可卖预案推送（昨日买入当天卖不掉的票，今天竞价提醒）')
    print(f'[定时任务]   - {PUSH_T1_PLAN_TIME}: 过夜仓"默认出口"提醒（时间止损到期/逼近MA20/'
          f'次日开盘卖基线；开关 overnight_exit_advice_enabled，推送另有 '
          f'overnight_exit_push_enabled，两个默认关）')
    print(f'[定时任务]   - {SCHEDULE_DAILY_REVIEW_TIME}: 自动生成每日收评（仅交易日，含 AI 情绪评分/操作建议）')
    print(f'[定时任务]   - {SCHEDULE_BACKTEST_TIME}: 回测验证昨日预测并调整策略权重（仅交易日）')
    print(f'[定时任务]   - {SCHEDULE_TREND_BACKTEST_TIME}: 趋势评分回测验证（1/3/5日跑赢大盘胜率，仅交易日）')
    print(f'[定时任务]   - 15:35: 自动优化（D1-D4权重+角色门槛+网格搜索，仅交易日，周一含全量搜索）')
    print(f'[定时任务]   - {"/".join(SCHEDULE_RECOMMENDATION_TIMES)}: 自动生成智能推荐股票（仅交易日）')
    print(f'[定时任务]   - {RECOMMEND_REVIEW_TIME}: 智能推荐复盘（本地K线库算T+1/T+3/T+5胜率）')
    print(f'[定时任务]   - {REC_SIM_DAILY_TIME}: 可成交性日常监控（三入口×两卖法落 rec_sim_daily，'
          f'一切"放宽/多推"提案的验收基线）')
    print(f'[定时任务]   - 20:30: 上午档/下午档拆批次回测（名单从 strategy_snapshot 还原，'
          f'落 rec_sim_batch_daily；10-19 起才有快照）')
    print(f'[定时任务]   - 20:35: 出口分叉影子落库（当天推荐算三条出口：拿隔夜/当日收盘走/分叉，'
          f'落 exit_seal_shadow；只记录，不改出口文案与推送）')
    print(f'[定时任务]   - 09:05/20:25: 板块持续性预热（本地日K×行业聚合近10日收益，'
          f'主线判定的分母，落 sector_persistence_daily）')
    print(f'[定时任务]   - {INDUSTRY_SYNC_WEEKDAY} {INDUSTRY_SYNC_TIME}: 行业分类周更'
          f'（baostock 全量落 stock_industry_map；东财被封时资金流向的行业列/板块聚合靠它）')
    print(f'[定时任务]   - {SHADOW_EVAL_TIME}: 影子对照日常监控（放行 vs 被挡 T+1，'
          f'落 recommend_shadow_eval；满 {SHADOW_EVAL_MIN_DAYS} 个交易日才给上线判据）')
    print(f'[定时任务]   - {WESTOCK_CROSSCHECK_TIME}: 双源数据校验（本地 vs westock 权威源，'
          f'落 westock_snapshot + data_cross_check；行情不一致告警、资金流记符号冲突）')
    print(f'[定时任务]   - {AUCTION_SNAPSHOT_TIME}: 集合竞价快照（腾讯批量，落 auction_snapshot；'
          f'仅竞价窗口 09:24~09:31 写入，晚跑拒写）')
    print(f'[定时任务]   - {AUCTION_DIP_MORNING_TIME}/{AUCTION_DIP_RETRY_TIME}: 竞价低吸臂影子判定'
          f'（昨日涨停池×今日竞价缺口分五档，落 auction_dip_shadow；只记录不改动作，'
          f'{"已启用" if AUCTION_DIP_SHADOW_ENABLED else "默认关"}）')
    print(f'[定时任务]   - {AUCTION_DIP_EVENING_TIME}: 竞价低吸臂影子回填（日内/隔夜/合计三条腿'
          f'+同日范围等权基准，历史缺行自愈；只记录）')
    print(f'[定时任务]   - {MINUTE5_FORWARD_TIME}: 5分钟线前向采集（活跃票，落 minute_5m；'
          f'{"已启用" if MINUTE5_FORWARD_ENABLED else "默认关"}）')
    print(f'[定时任务]   - 每周{"一二三四五六日"[EVAL_LAB_WEEKDAY]} {EVAL_LAB_TIME}: '
          f'因子体检（本地日K横截面 IC，落 eval_lab_runs；只读不联网）')
    print(f'[定时任务]   - {SCHEDULE_PRE_CLOSE_SNAPSHOT_TIME}: 收盘前推荐快照（最终生成并自动保存，仅交易日）')
    print(f'[定时任务]   - {SHAREHOLDER_UPDATE_TIME}: 更新股东人数筹码面数据'
          f'（月度披露；刷新节奏 {_weekday_names(SLOW_DATA_HOLDER_NUM_WEEKDAYS)}，'
          f'财报季 {"、".join(str(m) for m in SLOW_DATA_DAILY_MONTHS)} 月恢复每日）')
    print(f'[定时任务]   - {FUNDAMENTAL_UPDATE_TIME}: 更新基本面质量数据'
          f'（季度披露；刷新节奏 {_weekday_names(SLOW_DATA_FUNDAMENTAL_WEEKDAYS)}，'
          f'财报季恢复每日）')
    print(f'[定时任务]   - {VALUATION_UPDATE_TIME}: 更新估值分位数据（PE/PB 百分位，'
          f'{"仅交易日——周末没有新收盘价" if SLOW_DATA_VALUATION_TRADING_ONLY else "每日（含周末，改造前行为）"}）')
    print(f'[定时任务]   - {",".join(SCHEDULE_MARKET_ALERT_TIMES)}: 大盘风险预警自动扫描（仅交易日）')
    print(f'[定时任务]   - {SMART_MONEY_UPDATE_TIME}: 刷新龙虎榜+北向资金数据（刷新至最近已收盘交易日）')
    if KLINE_HISTORY_ENABLED:
        _sync_slots = ' / '.join(KLINE_HISTORY_SYNC_TIMES)
        print(f'[定时任务]   - {_sync_slots}: 增量同步本地历史日K线库 + 向量化刷新 RPS'
              f'（仅交易日；等到当天数据即停，最后一次仍没有才告警）')
    print(f'[定时任务]   - {WECOM_PUSH_PREVIEW_TIME}: 推送今日关注池预览（企业微信，仅交易日）')
    print(f'[定时任务]   - 盘中 {WECOM_PUSH_START}~{WECOM_PUSH_END} 每{WECOM_PUSH_INTERVAL_SEC}秒: '
          f'达标候选股实时推送企业微信（仅交易日；范围=用户池∪推荐池，日限 '
          f'{WECOM_PUSH_DAILY_LIMIT} 只）')
    print(f'[定时任务]   - 盘中 09:15~15:05 每180秒: 短线情绪温度计刷新（涨停池/炸板池/跌停池/宽度→周期+主线板块，仅交易日）')
    print(f'[定时任务]   - 盘中 09:40~15:05 每300秒: 动态热门板块链跟随（财联社板块榜→行动台前3链，仅交易日）')
    print(f'[定时任务]   - 15:12 / 15:47: 重建动态链 + 主题链成分当日OHLC落库（板块榜15:10之后，仅交易日）')
    # 下面四条是 2026-11-03 新加的。清单是硬编码 print（不是遍历 schedule.get_jobs()），
    # 所以**注册了就必须在这里补一行**，否则日志清单与实际不符、排查时会以为没挂上。
    # 时刻一律引用常量，避免改了 config 这里还在写死旧值。
    print(f'[定时任务]   - {SENTIMENT_FINAL_SNAPSHOT_TIME} / 16:05: 情绪收盘定版档位落库'
          f'（sentiment_final_daily，仅交易日；#11 回测前置——此前"当天最终档位"从来没落过库）')
    print(f'[定时任务]   - {INDEX_DAILY_SYNC_TIME}: 大盘指数日线同步（index_daily，4 指数×700 根，'
          f'仅交易日；只写本地新表，不改现有读指数的路径）')
    print(f'[定时任务]   - 观察线程（独立于本循环、每 60 秒一轮）: 调度心跳阈值 '
          f'{SCHED_HEARTBEAT_STALE_MIN} 分钟；决策链空档检查时点 '
          f'{"/".join(SCHED_CHAIN_CHECK_TIMES)}（要当天情绪快照有行而决策表全 0 才算，'
          f'免得把 9 月那种"功能还没上线"的 0 推荐日报成事故）；'
          f'开关 sched_liveness_enabled，关掉连线程都不起')
    print(f'[定时任务]   - 盘中 09:10~15:10 每60秒: 写锁哨兵探测（独立连接试 BEGIN IMMEDIATE，'
          f'等超阈值推企微；开关 db_lock_sentinel_enabled，拿到锁立刻回滚、自己不持锁）')
    print(f'[定时任务]   - 盘中持仓快反每{POSITION_WATCH_INTERVAL_SEC}秒附带: 情绪减仓影子'
          f'（exit_signal_log，退潮/冰点按先验比例算"该减多少"，**不执行**；'
          f'EXIT_SENTIMENT_SHADOW_ENABLED）')
    print(f'[定时任务]   - 盘中 {POSITION_WATCH_START}~{POSITION_WATCH_END} 每{POSITION_WATCH_INTERVAL_SEC}秒: 持仓止损/止盈/炸板快反推送（仅交易日，避开竞价与午休）')
    print(f'[定时任务]   - 盘中 {INTRADAY_DRAGON_START}~{INTRADAY_DRAGON_END} 每{INTRADAY_DRAGON_INTERVAL_SEC}秒: '
          f'龙头通道分钟级形态打分（仅交易日；'
          f'{"已启用" if INTRADAY_DRAGON_ENABLED else "默认关"}，'
          f'{"影子落库不推送" if INTRADAY_DRAGON_SHADOW else "推送已开"}；'
          f'频率熔断{"已接（20s 基准 / 失败率>5%退60s / 连败3次熔断300s）" if INTRADAY_DRAGON_RATE_GOVERNOR_ON else "默认关（保持现行 15s）"}；'
          f'主循环 10s 一轮，实际节奏约 20s）\n')

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
    # 持仓快反推送：盘中每 POSITION_WATCH_INTERVAL_SEC 秒扫一次持仓（2026-10-14）
    _last_watch_ts = 0.0
    # 盘中情绪刷新：主循环预热，避免用户第一次打开页面时现打接口
    _last_sentiment_ts = 0.0
    # 盘中动态链跟随（2026-10-31）：财联社板块榜变了就重建"今日热门板块前 3"，
    # 行情窗口内每 5 分钟看一次（TTL 判定在 hot_chains 内部，未到点直接返回）
    _last_chain_ts = 0.0
    # 盘中龙头通道（2026-11-01）：独立于 60s 主推送，开盘后 09:31~10:15 用更密的节奏看形态
    _last_dragon_ts = 0.0
    # 盘中写锁哨兵（2026-11-03）：每分钟探一次，独立 try 隔离，坏了不拖累推送与自愈
    _last_lock_sentinel_ts = 0.0

    # 调度链失活观察线程（2026-11-07）：**必须在主循环之前起**——放循环里就等于让被观察者
    # 判自己的死。11-06 那次循环一直在转、心跳根本不陈旧，靠的是这个独立线程
    # 加"决策链表全 0 行但情绪快照有行"的第二条判据。开关关掉时连线程都不起。
    try:
        from services.ops.sched_liveness import start_watchdog
        start_watchdog()
    except Exception as _lw_err:
        print(f'[定时任务 {datetime.now():%H:%M:%S}] 调度失活观察线程没起来（不致命，'
              f'只是当天再出 11-06 那种冻结不会自动通知）: {type(_lw_err).__name__}: {_lw_err}')

    while True:
        try:
            # 2026-09-18 稳定性修复：run_pending 单独 try 隔离。
            # 任何定时 job 抛异常只记录，不再拖累下方的企业微信推送轮询与自愈检查。
            # 2026-11-06 升级为 _run_pending_guarded()：只"记录"是不够的——schedule 在
            # job_func 抛异常时不推进 next_run，坏 job 会永远排在第一轮，把后面所有
            # 定时任务一起冻死（当天实拍：03:30 一个 NameError 卡掉整天海选/推荐/竞价）。
            _run_pending_guarded()
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

            # ---- 盘中写锁哨兵（2026-11-03）----
            # 独立连接试 BEGIN IMMEDIATE，等超阈值仍拿不到写锁就推企微，并附上「主库距最后一次
            # 成功写入几分钟」与最近任务锚点。为什么要有它：11-03 那次锁从 15:20 就坏了，用户
            # 16:5x 打不开页面才发现，定位又花约两小时、中间还误判过一次。哨兵改的是**发现时间**
            # 和**起点信息**，不替人处理。它拿到锁立刻 ROLLBACK，自己不会变成新的持锁者。
            # 只在盘中跑：收盘后锁竞争的表现是页面慢，用户自己会撞上，不值得为它发告警；
            # 盘中错过 09:45 / 14:45 批次才是真损失。整块单独 try 隔离，异常不拖累推送与自愈。
            if _hm >= '09:10' and _hm <= '15:10' and _today_is_trading():
                if time.time() - _last_lock_sentinel_ts >= 60:
                    _last_lock_sentinel_ts = time.time()
                    try:
                        from services.ops.db_sentinel import check_and_alert as _lock_sentinel
                        _lock_sentinel()
                    except Exception as _ls_err:
                        print(f'[定时任务 {_now:%H:%M:%S}] 写锁哨兵异常（已隔离，不影响其它任务）: {_ls_err}')

            # ---- 盘中龙头通道（2026-11-01，默认关；T2.7 接频率状态机）----
            # 与上面的 60s 主推送完全分开：只做分钟级形态打分，影子期写 intraday_dragon_shadow。
            # 关掉时整块不进入、一个请求都不发；单独 try 隔离，任何异常不拖累主推送与自愈。
            # T2.7：间隔与熔断由 dragon_risk.due() 决定——开关关＝现行 15s；开关开＝20s 基准，
            # 失败率 >5% 退 60s，连续 3 次失败熔断 300s；熔断期不进、也不刷新 _last_dragon_ts。
            if INTRADAY_DRAGON_ENABLED:
                _dragon_due = False
                try:
                    from services.pick import dragon_risk as _dr
                    _dragon_due = _dr.due(_hm, _today_is_trading(), _last_dragon_ts,
                                          time.time())['due']
                except Exception as _dr_err:
                    # 节奏判定自己坏了就退回改造前 15s，别让风控层拖住整条通道
                    print(f'[定时任务 {_now:%H:%M:%S}] 龙头节奏判定异常（退回 15s）: {_dr_err}')
                    _dragon_due = (_today_is_trading()
                                   and INTRADAY_DRAGON_START <= _hm <= INTRADAY_DRAGON_END
                                   and time.time() - _last_dragon_ts >= INTRADAY_DRAGON_INTERVAL_SEC)
                if _dragon_due:
                    _last_dragon_ts = time.time()
                    try:
                        from services.pick.intraday_dragon import run_scan as run_dragon_scan
                        run_dragon_scan()
                    except Exception as dragon_err:
                        print(f'[定时任务 {_now:%H:%M:%S}] 龙头通道异常: {dragon_err}')

            # ---- 盘中情绪刷新（2026-10-14，仅交易日 9:15~15:05）----
            # 主循环主动预热：情绪分是选股门槛/环境档位/快反文案的共同上游，
            # 若等页面请求触发，第一次打开会串行打 4 个东财接口、慢一拍。
            if (_hm >= '09:15' and _hm <= '15:05' and _today_is_trading()
                    and time.time() - _last_sentiment_ts >= 180):
                _last_sentiment_ts = time.time()
                try:
                    _s = asyncio.run(get_sentiment())
                    print(f'[情绪 {_now:%H:%M:%S}] {_s.get("cycle")} {_s.get("cycleScore")}分'
                          f'｜{_s.get("detail")}')
                except Exception as _sent_err:
                    print(f'[情绪 {_now:%H:%M:%S}] 刷新失败: {_sent_err}')
                # 主线判定预热（2026-10-22）：紧跟情绪之后，行动台请求永远命中缓存。
                # 全走内存/本地表（零外部请求），一次 ~50ms；开关关则整个跳过。
                try:
                    from services.strategy.mainline import get_mainline, _setting as _ml_setting
                    from app.config import MAINLINE_ENABLED
                    if _ml_setting('mainline_enabled', MAINLINE_ENABLED):
                        # 板块资金流保活（内部自判：clist 熔断中/快照<30min 直接跳过，
                        # 满足条件也只 2 次请求）——没人开资金流页时主线才不会永远"无主线"
                        try:
                            from services.data.fundflow import soft_refresh_sector_flow
                            asyncio.run(soft_refresh_sector_flow())
                        except Exception:
                            pass
                        _m = asyncio.run(get_mainline())
                        _mm = _m.get('main') or []
                        print(f'[主线 {_now:%H:%M:%S}] '
                              + ('｜'.join(f"{x['name']} {x['score']}分 {x.get('stage','')}" for x in _mm)
                                 if _mm else f"无主线：{(_m.get('none') or {}).get('reason','')}"))
                except Exception as _ml_err:
                    print(f'[主线 {_now:%H:%M:%S}] 预热失败: {_ml_err}')

            # ---- 盘中动态链跟随（2026-10-31，仅交易日 9:40~15:05）----
            # 行动台的"今日热门板块"按财联社板块榜动态选，盘中榜变了链也要跟着变；
            # hot_chains 自己按 TTL(盘中300s) 判重，主循环只负责按时戳叫醒它，不阻塞。
            if (_hm >= '09:40' and _hm <= '15:05' and _today_is_trading()
                    and time.time() - _last_chain_ts >= 300):
                _last_chain_ts = time.time()
                try:
                    from services.ops import hot_chains
                    hot_chains.tick()
                except Exception as _chain_err:
                    print(f'[动态链 {_now:%H:%M:%S}] 盘中跟随失败: {_chain_err}')

            # ---- 持仓快反推送（2026-10-14，止损/止盈不等用户打开页面）----
            # 时窗/交易日/午休判定在 watch_all_users 内部再做一次，这里只控频率。
            if (POSITION_WATCH_ENABLED and _hm >= POSITION_WATCH_START
                    and _hm <= POSITION_WATCH_END and _today_is_trading()):
                if time.time() - _last_watch_ts >= POSITION_WATCH_INTERVAL_SEC:
                    _last_watch_ts = time.time()
                    try:
                        asyncio.run(watch_all_users())
                    except Exception as _watch_err:
                        print(f'[定时任务 {_now:%H:%M:%S}] 持仓快反异常: {_watch_err}')

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
        # 2026-10-14：30s → 10s。原 30s 让"盘中每 20 秒扫持仓"变成一句空话——
        # 循环本身睡 30 秒，任何小于 30 秒的间隔都取决于运气。
        # run_pending/自愈都是纯内存与轻量 DB 读，10 秒一轮没有额外成本。
        time.sleep(10)



