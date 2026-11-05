"""Domain router: recommend."""

from fastapi import APIRouter
from ..routes_state import *

router = APIRouter()

@router.get("/api/daily_recommendations", tags=["推荐记录"])
async def get_daily_recommendations():
    """历史推荐记录 + 实时盈亏（SWR：过期返旧档+后台刷新，2026-10-23）

    实测背景（本机直连）：30s 同步缓存时三次采样 14.6/183.8/518.2ms——
    命中 15ms，miss 要同步等全量历史票行情重算 500ms+。本接口无轮询
    （TabRecommend 只 onMounted 一次），用户体感 = 每次打开都可能转圈半秒。
    保存/删除推荐后由写接口调 _invalidate_daily_recs_cache 清档。
    """
    now = time.time()
    cached = _DAILY_RECS_CACHE.get('payload')
    if cached is not None and _daily_recs_cache_valid():
        age = now - _DAILY_RECS_CACHE['ts']
        if age < _DAILY_RECS_FRESH_TTL:
            return cached                      # 新鲜期：直接返
        _kick_daily_recs_refresh()             # 过期：返旧档 + 后台刷新
        return cached
    # 冷启动 / 写接口清档 / 版本或自然日已变：同步重建（唯一不可避免的等待路径）。
    # 2026-11-05：旧档失效时**绝不返旧档**——它缺当天记录，会骗用户去手动生成。
    try:
        payload = await _build_daily_recs_payload()
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取推荐记录失败: {str(error)}")
    with _DAILY_RECS_REFRESH_LOCK:
        _stamp_daily_recs_cache(payload)
    return payload


@router.get("/api/recommend-review", tags=["推荐记录"])
async def get_recommend_review_api(days: int = 30):
    """智能推荐复盘汇总（2026-10-12）：全量+近N天 T+1/T+3/T+5 胜率与盈亏、最好最差案例

    缓存与线程（2026-11-03 第 3 步）：实测原来**每次请求都现算**（11-03 夜连取三次
    1.82 / 1.16 / 0.88 秒，第二次不降＝根本没有缓存），而 `get_review_summary` 是同步
    函数、原来直接调 —— 那 0.8~1.8 秒全程压在事件循环上，正是 recommend-sim 上面注释
    写的"一进这页全站卡"那个教训的第三次重演。现在三件事一起做：TTL 命中直接返回
    （实测 0.010~0.012 秒）、未命中丢 `asyncio.to_thread` 算、**只有 `_REC_REVIEW_CACHE_DAYS`
    里那两档（前端真正在用的 30/120）进缓存**——自由参数 `days` 全收的话，缓存会被一串
    一次性 days 挤空（11-04 实测），白名单之外一律按请求现算。命中/写入都会刷新 `used`，
    超上限时按"最久没被访问"淘汰（`_remember_rec_review`/`_touch_rec_review`，兜底防线），
    并把数据时刻
    （cachedAt/cachedAgeSec）回给前端。
    关掉 `RECOMMEND_REVIEW_CACHE_ENABLED` = 回到每次现算、缓存表不写（仍走线程、仍回传时刻）。
    """
    try:
        from services.pick.recommend_review import get_review_summary
        key = int(days)
        now = time.time()
        hit = _REC_REVIEW_CACHE.get(key) if _rec_review_cacheable(key) else None
        if hit and (now - hit['ts']) < _REC_REVIEW_TTL:
            _touch_rec_review(key)
            return {"success": True, "data": _review_with_meta(hit['data'], hit['ts'])}
        if hit and (now - hit['ts']) < _SWR_STALE_MAX:
            # 过期但未超服役上限：立即返旧值，重算放后台线程。
            # 白名单外不进缓存的老行为保持不变。
            def _compute():
                return get_review_summary(days)

            def _remember(data):
                _remember_rec_review(key, data, time.time())

            _swr.try_refresh(_REC_REVIEW_CACHE, key, _compute,
                             label='rec-review', gap=120, max_keys=_REC_REVIEW_MAX_KEYS,
                             on_done=_remember)
            _touch_rec_review(key)
            return {"success": True, "data": _review_with_meta(hit['data'], hit['ts'])}
        data = await asyncio.to_thread(get_review_summary, days)
        ts = time.time()
        if _rec_review_cacheable(key):
            _remember_rec_review(key, data, ts)
        return {"success": True, "data": _review_with_meta(data, ts)}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取推荐复盘失败: {str(error)}")


@router.get("/api/recommend-sim", tags=["推荐记录"])
async def get_recommend_sim_api(months: int = 3, capital: float = 100000,
                                gap: float = 5.0, lots: bool = True, bench: bool = False):
    """历史推荐的钱曲线（2026-10-18，复盘 → 回测曲线子页用）。

    六种口径（三种入口 × 两种卖法）一次算完，实现只在 `services/pick/rec_sim.py` 一份 ——
    命令行 `scripts/rec_exit_sim.py` 调的是同一个函数，页面与日志不会出现两套数。

    同步的 SQLite 读取全部丢进 `to_thread`：这个接口一次要读 85 只票的完整日K，
    压在事件循环上就是"一进这页全站卡"（rps/趋势扫描那个教训）。

    缓存的只有"六种口径的模拟结果"（0.17s 一次，底层数据一天才变），
    `daily`（每日监控点）**每次都重读**：它是 16:20/16:25 定时任务往里追加的时间序列，
    跟着缓存走就会出"曲线命中缓存 → 监控点停在 30 分钟前"的假象
    （今天就是这样：点已经落库了，页面还是空的）。
    """
    import time as _t
    key = (months, capital, gap, lots, bench)
    from services.pick import rec_sim

    def _compute():
        return rec_sim.simulate(months=months, capital=capital,
                                auction_max_gap=gap, whole_lots=lots,
                                with_benchmark=bench)

    try:
        raw, state = _swr.swr_get(_REC_SIM_CACHE, key, _compute,
                                  ttl=_REC_SIM_TTL, stale_max=_SWR_STALE_MAX,
                                  label='rec-sim', gap=120, max_keys=12)
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"回测曲线计算失败: {str(error)}")
    data = dict(raw)                       # 浅拷贝：别把 daily 写回缓存里那份
    try:
        data['daily'] = await asyncio.to_thread(rec_sim.daily_history, 40)
    except Exception as e:
        data['daily'] = []
        data['dailyError'] = str(e)[:120]
    return {'success': True, 'data': data, 'cached': state != 'cold', 'stale': state == 'stale'}


@router.get("/api/recommend-batches", tags=["推荐记录"])
async def recommend_batches_api(date: str = ''):
    """当日各批次推荐回看（2026-10-21）：daily_recommendations 先删后插只留末批，
    上午推过什么（如 10-21 的 10:26 上工申贝）只有 strategy_snapshot 记得。
    按批次返回推荐档名单＋批次现场（周期/情绪分），供智能推荐页折叠区展示。"""
    from datetime import datetime as _dt
    d = date or _dt.now().strftime('%Y-%m-%d')
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', d):
        raise HTTPException(status_code=400, detail="日期格式应为 YYYY-MM-DD")

    def _read():
        from db.database import get_db_conn
        conn = get_db_conn()
        rows = conn.execute(
            "SELECT batch, symbol, name, rank, score, timing, mode, price, change_percent "
            "FROM strategy_snapshot WHERE snap_date=? AND stage='recommended' ORDER BY batch, rank",
            (d,)).fetchall()
        meta = conn.execute(
            "SELECT batch, cycle, cycle_score, recommended, market_change "
            "FROM strategy_snapshot_batch WHERE snap_date=? ORDER BY batch", (d,)).fetchall()
        m = {x['batch']: dict(x) for x in meta}
        groups = {}
        for r in rows:
            g = groups.setdefault(r['batch'], [])
            g.append({k: r[k] for k in ('symbol', 'name', 'rank', 'score',
                                        'timing', 'mode', 'price', 'change_percent')})
        return [{'batch': b, 'meta': m.get(b) or {}, 'stocks': groups.get(b, [])}
                for b in sorted(set(groups) | set(m))]
    try:
        batches = await asyncio.to_thread(_read)
        return {'success': True, 'data': {'date': d, 'batches': batches}}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"读取当日批次失败: {str(error)}")


@router.get("/api/recommend-sim-batch", tags=["推荐记录"])
async def get_recommend_sim_batch_api(months: int = 3, capital: float = 100000, gap: float = 5.0):
    """上午档 vs 下午档拆批次回测（2026-10-21）：名单从 strategy_snapshot 还原
    （10-19 起才有），撮合/费用/封板判据与末批曲线同一引擎（rec_sim._curve_stats）。
    只读本地两个 SQLite、一次约 0.1s，丢线程池 + 30 分钟缓存，同 recommend-sim 的规矩。"""
    key = ('batch', months, capital, gap)
    from services.pick import rec_sim_batch

    def _compute():
        return rec_sim_batch.simulate_batch(months=months, capital=capital,
                                            auction_max_gap=gap)

    try:
        data, state = _swr.swr_get(_REC_SIM_CACHE, key, _compute,
                                   ttl=_REC_SIM_TTL, stale_max=_SWR_STALE_MAX,
                                   label='rec-sim-batch', gap=120, max_keys=12)
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"拆档回测计算失败: {str(error)}")
    return {'success': True, 'data': data, 'cached': state != 'cold', 'stale': state == 'stale'}


@router.get("/api/recommend-excess", tags=["推荐记录"])
async def get_recommend_excess_api(months: int = 6):
    """次日开盘买入、持有 1/3/5/10 日的**可执行超额**（2026-10-22，P0 评估基座）。

    为什么不塞进 /api/recommend-sim：主曲线 0.2 秒，这里要多算一遍选股范围等权基准
    （6 个月矩阵法**冷算实测 7.4 秒**，2026-10-27 单进程复测；早期写的 3.8 秒是推荐条数还少
    时量的，已不作数），同一个响应会让曲线白屏等它。页面并行请求，
    曲线先出来、这张表后补；30 分钟缓存，底层一天才变一次。

    口径固定为"推荐日下一交易日开盘买、持 N 个交易日开盘卖"，次日一字涨停买不到就跳过；
    基准是范围内**全部票**同一买入日、同一持有期等权，逐条配对后再平均。
    默认 6 个月而不是 3 个月：3 个月里 10 日样本不足 60 条，月度切片全是噪声。
    """
    key = (months,)
    from services.pick import rec_sim
    try:
        data, state = _swr.swr_get(_EXCESS_CACHE, key,
                                   lambda: rec_sim.open_hold_excess(months=months),
                                   ttl=_REC_SIM_TTL, stale_max=_SWR_STALE_MAX,
                                   label='rec-excess', gap=120, max_keys=6)
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"可执行超额计算失败: {str(error)}")
    return {'success': True, 'data': data, 'cached': state != 'cold', 'stale': state == 'stale'}


@router.get("/api/shadow-eval", tags=["推荐记录"])
async def get_shadow_eval_api(limit: int = 30):
    """影子对照的每日监控点（2026-10-18，实施计划"工具：影子对照脚本"）。

    这里**只读 16:25 落库的那些点**，不现算：现算一次要读整段日K、还会因为窗口随日期
    漂移而让"今天的数字"和"上周的数字"不可比。计算逻辑只在 `scripts/shadow_eval.py` 一份。
    没跑过（表还没建）时返回 available=False 并说清第一个点什么时候来，不给空数组让人猜。
    """
    def _read():
        from db.database import get_db_conn
        conn = get_db_conn()
        try:
            cur = conn.execute("SELECT eval_date, window_days, real_days, events, "
                               "kept_n, kept_win, kept_avg, excl_n, excl_win, excl_avg, "
                               "gap_avg, gap_win, verdict FROM recommend_shadow_eval "
                               "ORDER BY eval_date DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in cur]
        except Exception:
            return None                       # 表还没建 = 这个监控今天才上线
        finally:
            conn.close()
    rows = await asyncio.to_thread(_read)
    from app.config import SHADOW_EVAL_MIN_DAYS
    if rows is None:
        return {'success': True,
                'data': {'available': False, 'rows': [], 'minDays': SHADOW_EVAL_MIN_DAYS,
                         'note': '影子对照由定时任务在交易日 16:25 写入 recommend_shadow_eval；'
                                 f'判定要攒满 {SHADOW_EVAL_MIN_DAYS} 个有真放行名单的交易日'}}
    return {'success': True,
            'data': {'available': bool(rows), 'rows': rows, 'minDays': SHADOW_EVAL_MIN_DAYS}}


@router.post("/api/daily_recommendations", tags=["推荐记录"])
async def post_daily_recommendations(record: Dict):
    try:
        if not record or 'date' not in record or not isinstance(record.get('daily_recommendations'), list):
            raise HTTPException(status_code=400, detail="无效的推荐记录格式")
        
        record['timestamp'] = int(datetime.now().timestamp() * 1000)
        
        daily_recommendations = load_daily_recommendations()
        
        existing_index = -1
        for i, r in enumerate(daily_recommendations):
            if r.get('date') == record['date']:
                existing_index = i
                break
        
        if existing_index != -1:
            daily_recommendations[existing_index] = record
        else:
            daily_recommendations.insert(0, record)
        
        saved = save_daily_recommendations(daily_recommendations, source='api')
        
        if saved:
            _invalidate_daily_recs_cache()      # 写后清档，避免旧档多显示一轮
            return {"success": True, "message": "推荐记录保存成功"}
        else:
            raise HTTPException(status_code=500, detail="保存推荐记录失败: save_daily_recommendations 返回 False（详见服务端日志）")
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"保存推荐记录失败: {str(error)}")


@router.delete("/api/daily_recommendations/{date}", tags=["推荐记录"])
async def delete_recommendation(date: str):
    try:
        daily_recommendations = load_daily_recommendations()
        original_length = len(daily_recommendations)
        daily_recommendations = [r for r in daily_recommendations if r.get('date') != date]
        
        if len(daily_recommendations) == original_length:
            raise HTTPException(status_code=404, detail="未找到该日期的推荐记录")
        
        # 2026-10-12 修复：save_daily_recommendations 只替换列表内日期的行，
        # 被过滤掉的日期必须显式 DELETE，否则旧行残留（端点曾返回"删除成功"但记录仍在）。
        save_daily_recommendations(daily_recommendations)
        delete_daily_recommendations_by_date(date)
        _invalidate_daily_recs_cache()          # 写后清档
        
        return {"success": True, "message": "推荐记录删除成功"}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"删除推荐记录失败: {str(error)}")


@router.post("/api/generate-recommendations", tags=["推荐记录"])
async def generate_recommendations_api():
    """手动触发生成智能推荐并返回结果"""
    try:
        result = await _recommend_svc.auto_generate_recommendations_task()
        if result and result.get('recommendations'):
            return {"success": True, "recommendations": result['recommendations']}
        # 如果任务没有返回数据，尝试从数据库读取今天的推荐
        daily = load_daily_recommendations()
        today = date.today().isoformat()
        for r in daily:
            if r.get('date') == today:
                return {"success": True, "recommendations": r.get('daily_recommendations', [])}
        return {"success": False, "error": "生成推荐失败，请稍后重试"}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"生成推荐失败: {str(error)}")

