"""scheduler - 定时任务调度"""
import asyncio, schedule, time, threading
from datetime import datetime, timedelta

from app.config import (SCHEDULE_UPDATE_HOT_SEARCH_TIMES, SCHEDULE_MA10_CHECK_TIME, SCHEDULE_DAILY_REVIEW_TIME, SCHEDULE_BACKTEST_TIME, SCHEDULE_RECOMMENDATION_TIMES, SCHEDULE_PRE_CLOSE_SNAPSHOT_TIME, RECOMMEND_REVIEW_TIME, SCHEDULE_TREND_SCAN_INTERVAL_MIN, SCHEDULE_TREND_SCAN_START, SCHEDULE_TREND_SCAN_END, SCHEDULE_TREND_BACKTEST_TIME, SHAREHOLDER_UPDATE_TIME, FUNDAMENTAL_UPDATE_TIME, VALUATION_UPDATE_TIME, SCHEDULE_MARKET_ALERT_TIMES, SMART_MONEY_UPDATE_TIME, WECOM_PUSH_INTERVAL_SEC, REC_SIM_DAILY_TIME, SHADOW_EVAL_TIME, SHADOW_EVAL_MIN_DAYS, WECOM_PUSH_START, WECOM_PUSH_END, WECOM_PUSH_DAILY_LIMIT, WECOM_PUSH_PREVIEW_TIME, KLINE_HISTORY_SYNC_TIME, KLINE_HISTORY_SYNC_TIMES, KLINE_HISTORY_SYNC_LAST,
                        KLINE_HISTORY_ENABLED, RPS_PERIOD_DAYS)

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
from app.config import MATRIX_RERUN_ENABLED, MATRIX_RERUN_TIME
from app.config import (MARKET_CAP_REFRESH_TIME, MARKET_CAP_REFRESH_WEEKDAY,
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
from app.config import MINUTE5_FORWARD_ENABLED, MINUTE5_FORWARD_TIME


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
        from services.ops.auto_optimizer import run_auto_optimization
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
    # 2026-10-17 启动预热：universe 选票范围首算要 ~5s（流动性聚合），
    # 不预热则重启后第一次开配置页白等 5s。放后台线程跑，不阻塞调度器注册。
    def _prewarm_universe():
        try:
            from services.data import universe
            universe.selectable_count()
            print(f'[预热 {datetime.now():%H:%M:%S}] universe 选票范围缓存完成')
        except Exception as e:
            print(f'[预热 {datetime.now():%H:%M:%S}] universe 预热失败（不致命，首次请求会现算）: {e}')
    threading.Thread(target=_prewarm_universe, daemon=True, name='prewarm-universe').start()

    # 区间带宽波动率预热（2026-10-19 动态区间宽度）：这份缓存是一次"全市场近 6 个已收盘
    # 交易日收盘序列"的**同步** SQLite 查询。不预热的话，09:45 批次里第一只票会在 asyncio
    # 事件循环上等它几百毫秒——偏偏那一刻正是"开盘后第一时间出推荐"的关键路径（同一个坑
    # 在 2026-10-17 的 history_available 上踩过一次）。开关关着就一行都不查：
    # 关掉的代价必须严格等于改造前，一次多余的库访问也不给留。
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
                print(f'[定时任务 {datetime.now():%H:%M:%S}] ⚠️ 盘前海选为空，今日推荐退回热搜池'
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
            asyncio.run(auto_generate_recommendations_task())
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
        # 每天重拉全市场 ~50 页只是把同样的行搬一遍，而东财 datacenter 正是 IP 风控的来源。
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
                  f'未取到 {r.get("fail")}（限流则下次续补）')
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
        print(f'[盘后自检] ⚠️ 本地日K库最新 {latest}，{why}')
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
                  + (f'｜⚠️ 拒写: {st.get("rejected")}' if st.get('rejected') else ''))
            if st.get('rejected'):
                sync_err = f"收盘快照与备路口径不符: {st['rejected']}"
        except Exception as e:
            sync_err = e
            print(f'[定时任务 {datetime.now():%H:%M:%S}] ⚠️ 收盘快照(主路)异常: {type(e).__name__}: {e}')

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
                    print(f'[定时任务 {datetime.now():%H:%M:%S}] ⚠️ 本地日K增量同步(备路)异常: {e}')
        try:
            from services.data.kline_history import history_available
            if history_available():
                from services.strategy.rps import refresh_rps
                res = asyncio.run(refresh_rps(RPS_PERIOD_DAYS))
                print(f'[定时任务 {datetime.now():%H:%M:%S}] 本地库同步后 RPS 刷新完成：{res}')
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] ⚠️ RPS 向量化刷新异常: {e}')
        # 名录（代码→名称、在市/退市、ST 标记）每天跟着刷一次：股票范围里"剔除 ST/*ST"
        # 完全依赖这张表，而 ST 帽子是随时戴随时摘的 —— 名录过期就等于范围失效。
        # 单独一段 try，且放在新鲜度自检之前：它失败只让范围变旧，不该拖住日K同步的结论。
        try:
            from sync_stock_basic import sync as sync_stock_names
            n = sync_stock_names()
            if n < 0:
                print(f'[定时任务 {datetime.now():%H:%M:%S}] ⚠️ 股票名录同步失败（ST 判定可能过期）')
        except Exception as e:
            print(f'[定时任务 {datetime.now():%H:%M:%S}] ⚠️ 股票名录同步异常: {e}')
        # 只有最后一个重试时刻还没等到数据才算真失败（16:05 那种"baostock 还没出当天EOD"
        # 不是故障，半夜叫警报只会让人以为系统坏了）
        _alert_kline_stale(sync_err, final=datetime.now().strftime('%H:%M') >= KLINE_HISTORY_SYNC_LAST)

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
            print(f'[定时任务 {datetime.now():%H:%M:%S}] ⚠️ 买法矩阵重跑拉起失败: {e}')

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

    # 板块持续性预热（2026-10-22，主线判定的分母）：09:05 供盘前用、20:25 收当天
    schedule.every().day.at('09:05').do(_warp_trading_only(job_sector_persistence))
    schedule.every().day.at('20:25').do(_warp_trading_only(job_sector_persistence))

    # 影子对照日常监控（2026-10-18，实施计划"工具"）：16:25 日K已同步，
    # 把「影子挡掉的票 T+1 是不是真的更差」存成每天一个点 —— 第1批 P0-2 的上线判据
    schedule.every().day.at(SHADOW_EVAL_TIME).do(_warp_trading_only(job_shadow_eval_daily))

    # 09:25 集合竞价快照（2026-10-22）：只在竞价窗口内写，晚跑宁可不写；
    # 采 2462 只约 41 次请求、10 秒内完成，9:45 推荐前早已结束
    schedule.every().day.at(AUCTION_SNAPSHOT_TIME).do(_warp_trading_only(job_auction_snapshot))

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

    # 总市值表每周刷新（2026-10-16）：选票/K线/分时线据此剔除 <35亿 小微盘，防量化操控；非交易日也跑
    getattr(schedule.every(), MARKET_CAP_REFRESH_WEEKDAY).at(MARKET_CAP_REFRESH_TIME).do(job_refresh_market_cap)

    # 行业分类周更（2026-10-19）：东财被封时，资金流向的行业列与板块聚合都靠这张本地表
    getattr(schedule.every(), INDUSTRY_SYNC_WEEKDAY).at(INDUSTRY_SYNC_TIME).do(job_sync_industry_map)

    # 买法矩阵按"选股范围 2462"重跑（一次性，跑成功写标记后自动不再触发）；非交易日也要跑
    schedule.every().day.at(MATRIX_RERUN_TIME).do(job_matrix_rerun)

    # 企业微信选股推送：开盘前 9:25 推送今日关注池预览（仅交易日）
    schedule.every().day.at(WECOM_PUSH_PREVIEW_TIME).do(job_push_daily_preview)

    # 数据库维护：每周六凌晨3点清理过期缓存+VACUUM（防 SQLite 膨胀）
    schedule.every().saturday.at("03:00").do(job_db_maintenance)

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
    print(f'[定时任务]   - 盘中 {POSITION_WATCH_START}~{POSITION_WATCH_END} 每{POSITION_WATCH_INTERVAL_SEC}秒: 持仓止损/止盈/炸板快反推送（仅交易日，避开竞价与午休）\n')

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



