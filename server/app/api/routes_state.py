"""Shared state, caches and helper functions extracted from the former monolithic routes.py.

This module intentionally exposes everything via ``__all__`` so the per-domain
router modules can do ``from ..routes_state import *`` and keep their handlers
verbatim. Do not put APIRouter instances here.
"""

"""routes - 所有 API 路由定义"""
from fastapi import HTTPException, APIRouter, Request, Depends
from fastapi.responses import Response
from typing import List, Dict, Optional
import json, asyncio, time, re, inspect, threading, hashlib, gzip
from datetime import date, datetime
from app.config import (STRATEGY_BACKTEST_PREDICTION_LIMIT, STRATEGY_BACKTEST_HISTORY_DAYS, DEFAULT_WEIGHTS, INDEX_SYMBOLS, TENCENT_QUOTE_API, SCAN_CONCURRENCY, HOT_STOCK_POOL, STRATEGY_INDUSTRY_NEUTRALIZATION_ENABLED, EASTMONEY_HEADERS, STRATEGY_SCORE_VALUES, FUND_FLOW_STOCK_TOP_N, FUND_FLOW_SECTOR_TOP_N, FUND_FLOW_HIDDEN_CHANGE_THRESHOLD, FUND_FLOW_HIDDEN_HOT_TOP_N, FUND_FLOW_HIDDEN_TOP_N, RPS_PERIOD_DAYS, SETTINGS_WRITE_USER_IDS, RPS_TOP_N, SMART_MONEY_DRAGON_TIGER_DAYS, PREDICT_SAVE_QUALITY_GATE, PREDICT_SAVE_LIMIT_UP_MIN, PREDICT_SAVE_MIN_SCORE, RECOMMEND_REVIEW_CACHE_ENABLED)
from db.database import (get_db_conn, load_daily_recommendations, save_daily_recommendations, delete_daily_recommendations_by_date, load_trend_scan_results, load_stock_basic_info, save_stock_basic_info, load_stock_alias_map, load_hot_search_ranking, save_hot_search_ranking, load_user_scan_pool, save_user_scan_pool, load_hot_stock_buttons, save_hot_stock_buttons, load_stock_concept_tags, save_stock_concept_tags, load_daily_market_reviews, save_daily_market_reviews, load_strategy_factor_weights, save_strategy_factor_weights, add_stock_concept_tags, remove_stock_tag, FACTOR_DESCRIPTIONS, save_stock_industry, load_trend_backtest_reports, load_latest_trend_backtest_report, load_trend_backtest_records, daily_recs_write_version)
from services.data.stock import (fetch_stock_data, fetch_eastmoney_hot_list, get_stock_name, get_kline_data, parse_tencent_batch_data, fetch_stock_concepts, is_forbidden_stock, calculate_ma, fetch_with_retry, _sync_get, has_delisting_risk, detect_quote_source, quote_route_status)
from services.strategy.strategy import backtest_yesterday_predictions, save_predictions, calc_stock_score_v2, apply_industry_neutralization
from services.strategy.trend import scan_trend_scan_results, scan_and_remove_below_ma10, calc_pool_stock_indicators
from services.strategy.trend_backtest import backtest_trend_predictions
from services.data.trading_calendar import is_trading_day
# 2026-10-29：推荐模块一律按模块属性取用。10-29 那次 500
# （"_build_expectation_note() takes 3 positional arguments but 4 were given"）的根因是
# app/main.py 的 _reload_config() 曾 importlib.reload(services.pick.recommend)，
# 而本模块启动时 from-import 的旧函数对象不会更新，旧函数体却读到新的全局表。
# 该 reload 已移除；这里仍按属性取用，避免将来有人恢复 reload 时再踩同一个坑。
from services.pick import recommend as _recommend_svc
from services.ops.review import generate_daily_review
from services.strategy.market import predict_market_risk, check_market_crash, load_market_alert
from services.data.fundflow import get_fund_flow_summary
from services.strategy.rps import get_rps_map, get_rps_leaderboard
from services.data.kline_history import normalize_local_symbol  # 将带前缀 symbol 规整为本地库纯代码，匹配 RPS 缓存 key
from services.data.fundamental import get_fundamental_batch, update_fundamental_task
from services.data.smartmoney import get_dragon_tiger, get_northbound_market, get_dragon_tiger_symbol_map
from services.data.valuation import get_valuation_batch, update_valuation_task
from services.strategy.risk_control import calc_risk_control
from services.data.utils import ensure_today_date
from services.support.auth import (current_user, create_user, authenticate, logout as auth_logout,
                           get_user_by_token, list_users, has_any_user, change_password)
from services.ops.action_desk import build_action_desk

TENCENT_API = TENCENT_QUOTE_API
MAX_RETRIES = 2

# ========== 全站 SWR 缓存基础设施（2026-11-04 全站巡检） ==========
# 统一口径：日级/分钟级只读数据过期先返旧值、重算放后台，绝不把冷算压在用户请求上。
from services.support import swr as _swr
_SWR_STALE_MAX = 86400.0           # 秒：旧值最长服役一天（后台刷新会持续把它更新）
# 大盘指数 / 主题链 / 财联社梯队 / 本地K库统计等分钟级数据源共用一份缓存。
# 键一律用 tuple，避免自由参数把缓存撑成可被远程清空的攻击面。
_MISC_SWR_CACHE: Dict = {}

# ========== 每日收评 SWR + ETag + 服务端分页（2026-11-04 P0-A / P0-B） ==========
# P0-A：实测每次返回 30 天 × 约 24KB = 518235B（gzip 105KB），服务端 XPT 133ms，
# 且无任何缓存头，而前端一页只渲染 1 条。→ 进程内缓存 + ETag/304 + 预压缩。
# P0-B（22:45）：304 已经零传输，但**首次开页仍要下 105KB**。改为服务端按页返回，
# 一页 1 条约 25KB（gzip 约 5KB），手机走隧道的开页成本再降一个数量级。
# 口径：page=0 / 不传 = 全量（兼容脚本与其它消费方），page>=1 = 第 N 页。
# **page_size 不给自由参数**：固定 1 条/页（前端本来就是 1 条），否则远程可用
# ?page_size=1..N 把缓存撑成无数份（11-04 复盘缓存被 days 撑爆是同一个教训）。
_DAILY_REVIEWS_CACHE: Dict = {}
_DAILY_REVIEWS_TTL = 300.0     # 秒：5 分钟内直接复用；过期先返旧值、后台刷新


def _reviews_entry(payload: dict) -> dict:
    """把一个响应体预序列化 + 预压缩成可直接吐给客户端的条目。

    ETag 用**弱校验器** `W/"..."`：同一 URL 会因 Accept-Encoding 返回 gzip /
    identity 两种表示，强 ETag 跨表示复用严格说违反 RFC 7232；弱校验器表示
    「语义等价」正是这个场景。Vary 仍保留。

    Args:
        payload: 待序列化的响应体字典。

    Returns:
        {'raw': bytes, 'gz': bytes, 'etag': str}
    """
    raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':'),
                     default=str).encode('utf-8')
    return {'raw': raw, 'gz': gzip.compress(raw, compresslevel=6),
            'etag': 'W/"' + hashlib.md5(raw).hexdigest() + '"'}


def _build_daily_reviews_entry():
    """一次查库，构建「全量 + 每页」全部预压缩条目（在 to_thread 里跑）。

    为什么整份 bundle 一次性构建而不是按页懒构建：懒构建会让第一次翻到某页
    的请求现场付序列化 + 压缩，等于把 P0-A 刚干掉的「每请求重压」又请回来。
    30 页 × (约 25KB raw + 约 5KB gz) ≈ 1MB 内存，换全部翻页零计算，划算。

    Returns:
        {'total': int, 'all': entry, 'pages': {page: entry}, 'recentFocus': list}
    """
    rows = load_daily_market_reviews()
    total = len(rows)
    # 前端 loadFocusQuotes 原来取 reviews.slice(0, 3) 的明日关注去重，
    # 分页后前端只拿得到 1 条，故由后端把「最近 3 天」的关注票一并下发，
    # 行为与改造前等价（甚至更准：翻到历史页时也仍拉最新 3 天的行情）。
    focus, seen = [], set()
    for row in rows[:3]:
        for s in (row.get('tomorrowFocus') or []):
            code = s.get('code') if isinstance(s, dict) else None
            if code and code not in seen:
                seen.add(code)
                focus.append(s)
    common = {'total': total, 'recentFocus': focus}
    bundle = {
        'total': total,
        'recentFocus': focus,
        'all': _reviews_entry({'success': True, 'data': rows,
                               'page': 0, 'pageSize': total,
                               'totalPages': 1, **common}),
        'pages': {
            i: _reviews_entry({'success': True, 'data': [row],
                               'page': i, 'pageSize': 1,
                               'totalPages': total, **common})
            for i, row in enumerate(rows, 1)
        },
    }
    return bundle


def _invalidate_daily_reviews_cache():
    """新收评落库后立刻失效，避免刚生成的收评要等 TTL 才可见。

    2026-11-04 复查：必须走 `_swr.invalidate` 而不是 `clear()`。收评缓存构建要
    序列化 518KB 再 gzip，后台刷新线程可能已拿着**旧快照**跑到一半；此时用户点
    「生成今日收评」→ clear() → 刷新线程 put() 回来，新收评就被旧值覆盖，且 ts
    是新的，接下来 5 分钟 fresh 全是旧数据（scripts/swr_selftest.py ⑭ 复现）。
    """
    _swr.invalidate(_DAILY_REVIEWS_CACHE)

# ========== 批量扫描结果内存缓存（2026-08-31 性能优化） ==========
_HOT_SCAN_CACHE: Dict[str, Dict] = {}
# 2026-10-24：15s→45s。前端 30s 轮询下单用户 15s TTL 永不命中（每次都过期），
# 每轮触发全量重扫（120 只行情+资金流+评分）；45s > 30s 轮询间隔，交替命中，
# 全量重扫频率减半，缓存数据最旧 45s（30s 一轮的筛选页可接受）。
_HOT_SCAN_CACHE_TTL = 45  # 秒
# 2026-10-30 陈旧兜底：45s TTL 一过，下一个请求就要白付全量重扫（实测冷 2.2s、
# 缓存过期叠并发时 8.7s 甚至 53.9s）。过期但未超上限的数据先原样返回、由后台
# 线程刷新；筛选页是 30s 轮询的大盘画面，秒级新鲜度足够，页面耗时从此 ≤ 缓存命中。
# 2026-11-04 全站巡检修正：原上限 600s 太短——**隔夜 / 长时间无人访问后首开**
# 仍会落进「无缓存可返」分支，同步重扫实测 10.008s，正好撞前端 10s 超时，
# 页面显示「暂无扫描结果」。日级扫描结果旧一点没有决策损失（前端有 X-Scan-As-Of
# 如实标注数据时刻，后台 20s 内刷新完、30s 轮询即拿新值），故放宽到 24 小时。
_HOT_SCAN_STALE_MAX = 86400  # 秒
_HOT_SCAN_REFRESHING: Dict[str, float] = {}   # {cache_key: 上次后台刷新时间}，20s 节流
_HOT_SCAN_CTX = threading.local()             # 后台刷新线程内置 compute=True，避免递归再进缓存分支

def _invalidate_hot_scan_cache():
    """清空批量扫描缓存（添加/移除自选股后调用，避免脏缓存）"""
    _HOT_SCAN_CACHE.clear()
    _HOT_SCAN_REFRESHING.clear()
    # 2026-11-04：扫描池详情也随池子变化，写后一并失效（60s SWR 缓存）。
    # 同样用 invalidate 而不是 pop：键不含池子内容，后台刷新会原样写回旧详情。
    _swr.invalidate(
        _MISC_SWR_CACHE,
        keys=[k for k in _MISC_SWR_CACHE
              if isinstance(k, tuple) and k[:1] == ('scan-pool',)],
    )


def _spawn_hot_scan_refresh(cache_key: str, codes_raw: str, pool_only: bool):
    """后台刷新一份过期的扫描缓存（2026-10-30 陈旧兜底的另一半）。

    20s 节流防止 30s 轮询叠并发时重复全量扫；子线程自带事件循环
    （asyncio.run）是安全的——扫描的网络调用全部走 requests + to_thread，
    不依赖主循环资源。失败只打日志，旧缓存留给下一个请求继续兜底。
    """
    _now = time.time()
    if _now - _HOT_SCAN_REFRESHING.get(cache_key, 0) < 20:
        return
    _HOT_SCAN_REFRESHING[cache_key] = _now

    def _run():
        _HOT_SCAN_CTX.compute = True
        try:
            asyncio.run(hot_stock_buttons_scan(codes=codes_raw, force=False, pool_only=pool_only))
            print(f'[批量扫描] 后台刷新完成 {cache_key[:40]}…')
        except Exception as e:
            print(f'[批量扫描] 后台刷新失败（旧缓存保留）: {e}')
    threading.Thread(target=_run, daemon=True, name='hot-scan-refresh').start()


def _scan_as_of_json(payload, as_of_ts: float):
    """扫描结果统一出口：附 X-Scan-As-Of = 这份数据是什么时候算出来的。

    2026-10-30 陈旧兜底上线后，"读取于 14:00:00"（前端客户端时钟）和
    "数据是 13:52 算的"（陈旧缓存）可能是两回事——老王量化那行"延迟 309 秒"
    治的就是这种误导。响应体结构不变（仍是裸数组），只多一个响应头。
    """
    from fastapi.responses import JSONResponse
    return JSONResponse(
        content=payload,
        headers={'X-Scan-As-Of': datetime.fromtimestamp(as_of_ts).strftime('%H:%M')})


# ========== 扫描宇宙扩展（2026-08-31 选股策略优化） ==========
# 背景：原「股票筛选」只扫前端传入的用户自选池（通常 20~30 只），覆盖面窄。
#       而趋势扫描已覆盖 ~477 只，热搜榜有全市场最活跃的 ~600 只。
# 方案：默认合并 用户池 ∪ 趋势池 ∪ 默认热门池 ∪ 热搜前 N，去重后封顶 SCAN_UNIVERSE_CAP，
#       让筛选 Tab 能看到更完整的市场机会；传入 pool_only=1 可退化为仅扫用户池。
SCAN_UNIVERSE_CAP = 120          # 扫描宇宙封顶（兼顾覆盖面与首扫耗时）
SCAN_UNIVERSE_HOT_TOP_N = 60     # 热搜榜补充数量
SCAN_UNIVERSE_MIN_POOL = 15      # 用户池 ≥ 此数量时不再补充热搜榜（尊重用户自选规模）

def _build_scan_universe(codes_list: List[str], pool_only: bool = False) -> List[str]:
    """构建扫描宇宙：用户池 + 趋势池 + 默认热门池 + 热搜前 N（去重、过滤风险股）"""
    universe = [c.lower() for c in codes_list]
    if pool_only:
        return universe[:SCAN_UNIVERSE_CAP]

    # 合并默认热门池（HOT_STOCK_POOL 是经过人工挑选的备选池）
    for c in HOT_STOCK_POOL:
        if c.lower() not in universe:
            universe.append(c.lower())

    # 合并趋势扫描池（已通过趋势评分 ≥ TREND_MIN_SCORE 的强势股，跨系统联动）
    try:
        trend_data = load_trend_scan_results()
        for st in (trend_data.get('stocks') or []):
            sym = (st.get('symbol') or '').lower()
            if re.match(r'^(sh|sz)\d{6}$', sym) and sym not in universe:
                universe.append(sym)
    except Exception as _e:
        print(f'[扫描宇宙] 趋势池合并失败: {_e}')

    # 用户池规模较小时，补充热搜榜前排活跃股（扩大市场覆盖面）
    if len(codes_list) < SCAN_UNIVERSE_MIN_POOL:
        try:
            hot = load_hot_search_ranking()
            for st in (hot.get('stocks') or [])[:SCAN_UNIVERSE_HOT_TOP_N]:
                sym = (st.get('symbol') or '').lower()
                if not re.match(r'^(sh|sz)\d{6}$', sym):
                    continue
                if is_forbidden_stock(st.get('name') or '') or has_delisting_risk(st.get('name') or '', sym):
                    continue
                if sym not in universe:
                    universe.append(sym)
        except Exception as _e:
            print(f'[扫描宇宙] 热搜池合并失败: {_e}')

    return universe[:SCAN_UNIVERSE_CAP]







# ========== 第三方接口调用用量统计（2026-10-19） ==========




# ========== 因子体检（2026-10-22，P0 评估基座） ==========
# 结果每周六 08:30 由调度任务落 eval_lab_runs；这里只读，不重算。
# 手动重算给管理员一个按钮，但必须防重复点击：一次要读 140 万行日K、约 1 分钟。
_EVAL_LAB_STATE: Dict = {'running': False, 'startedAt': ''}


def _eval_lab_module():
    """轻量读写层（只读 sqlite + 子进程跑体检；不把 numpy/pandas 拉进 web 进程）。"""
    from services.support import eval_lab
    return eval_lab















async def _build_daily_recs_payload() -> Dict:
    """实际构建"历史推荐记录 + 实时盈亏"响应体（原 get_daily_recommendations 函数体抽出）

    抽出原因：SWR 模式下首次 miss 要同步算、过期后要在线程里 asyncio.run() 重算，
    两处必须复用同一份构建逻辑，否则必然漂移。
    """
    daily_recommendations = load_daily_recommendations()
    # 实时计算历史推荐盈利
    today_str = date.today().isoformat()

    # 性能优化（2026-07-02）：原实现每条历史记录串行 await client.get（最多29次，4-9s）
    # 改为：先收集所有历史 symbols，一次性批量拉取，再回填
    # Step 1: 标记 isToday + 收集所有历史 symbols
    all_history_symbols = []
    for record in daily_recommendations:
        rec_date = record.get('date', '')
        if rec_date >= today_str:
            record['isToday'] = True
            continue
        record['isToday'] = False
        stocks = record.get('daily_recommendations') or record.get('stocks') or []
        for s in stocks:
            sym = s.get('symbol', '')
            if sym:
                all_history_symbols.append(sym)

    # Step 2: 一次性批量拉取所有历史 symbols 的实时行情
    price_map = {}
    if all_history_symbols:
        try:
            # 去重
            unique_symbols = list(set(all_history_symbols))
            # 分批拉取（每批最多100只，避免URL过长）
            batch_size = 100
            async def _fetch_quote_batch(syms):
                try:
                    codes_str = ','.join(syms)
                    url = f"{TENCENT_QUOTE_API}{codes_str}"
                    # -fix: 改用 requests + asyncio.to_thread（去 httpx）
                    resp = await asyncio.to_thread(_sync_get, url)
                    return resp.text.strip()
                except Exception as _e:
                    print(f'[历史推荐] 行情批量拉取失败: {_e}')
                    return ''

            batch_tasks = [
                _fetch_quote_batch(unique_symbols[i:i + batch_size])
                for i in range(0, len(unique_symbols), batch_size)
            ]
            batch_texts = await asyncio.gather(*batch_tasks, return_exceptions=True)

            for text in batch_texts:
                if isinstance(text, Exception) or not text:
                    continue
                for line in text.split(';'):
                    line = line.strip()
                    if not line or '=' not in line:
                        continue
                    parts = line.split('~')
                    if len(parts) > 3:
                        raw_code = parts[2].lower().strip()
                        try:
                            cur_price = float(parts[3])
                        except (ValueError, IndexError):
                            cur_price = 0
                        if cur_price > 0:
                            prefix = 'sz' if raw_code.startswith(('0', '3')) else 'sh'
                            full_sym = prefix + raw_code
                            price_map[full_sym] = cur_price
                            price_map[raw_code] = cur_price
        except Exception as _e:
            print(f'[历史推荐] 行情解析失败: {_e}')

    # Step 3: 回填每条记录的 currentPrice + profitRatio
    for record in daily_recommendations:
        if record.get('isToday'):
            continue
        stocks = record.get('daily_recommendations') or record.get('stocks') or []
        for s in stocks:
            sym = s.get('symbol', '').lower()
            cur = price_map.get(sym, 0)
            if cur > 0:
                s['currentPrice'] = cur
                buy_price = 0
                bsp = s.get('buySellPoints')
                if bsp and bsp.get('buy'):
                    try:
                        buy_price = float(bsp['buy'])
                    except (ValueError, TypeError):
                        pass
                if buy_price <= 0:
                    try:
                        buy_price = float(s.get('price', 0))
                    except (ValueError, TypeError):
                        pass
                if buy_price > 0:
                    s['profitRatio'] = round((cur - buy_price) / buy_price * 100, 2)

    return {"success": True, "data": daily_recommendations}


# ========== daily_recommendations 缓存（SWR 模式，2026-10-23） ==========
# 沿革：2026-10-17 加 30s TTL 同步缓存，解决了"每次打开都重拉行情"（0.9~1.3s），
# 但 miss 时用户仍要白等 500ms+（实测 14.6/183.8/518.2ms 三次采样）。
# "历史推荐现在赚多少"是分钟级信息量，没有理由让请求方等一次全量行情重算。
# 现改为 stale-while-revalidate：
#   · fresh 期内（300s）直接返缓存
#   · 过期但有旧档：立即返旧数据（<20ms），同时后台线程重算，下一个请求就是新档
#   · 无旧档（冷启动/被写接口清掉）：同步算，这一次不可避免
# generation：写接口清缓存时 +1，后台刷新完成时若 generation 已变（说明期间有新写入）
# 则丢弃刷新结果，避免把用户刚保存/删除的档又被后台旧数据覆盖回去。
_DAILY_RECS_CACHE: Dict = {'ts': 0.0, 'payload': None, 'generation': 0,
                           'version': -1, 'built_date': ''}
# version / built_date（2026-11-05）：分别兜住"库被定时批次写过但没人通知缓存"和
# "跨天旧档里 isToday 标签已过期"两类静默错误。旧档缺当天记录时前端会显示
# 「今日推荐尚未生成」——这正是 11-05 那次误判的成因，不能只靠 TTL 收敛。
_DAILY_RECS_FRESH_TTL = 300.0          # 新鲜期秒数：历史推荐盈亏是分钟级信息量
_DAILY_RECS_REFRESH_LOCK = threading.Lock()
_DAILY_RECS_REFRESHING = False         # 防并发重复刷新（锁保护）


def _invalidate_daily_recs_cache():
    """写接口调用：清档 + 重置时间 + bump generation（作废进行中的后台刷新）"""
    with _DAILY_RECS_REFRESH_LOCK:
        _DAILY_RECS_CACHE['payload'] = None
        _DAILY_RECS_CACHE['ts'] = 0.0
        _DAILY_RECS_CACHE['generation'] += 1
        _DAILY_RECS_CACHE['version'] = -1
        _DAILY_RECS_CACHE['built_date'] = ''
    # 复盘汇总（/api/recommend-review）的数据源就是这张表，同一批写入必须一起作废，
    # 否则手工生成/删除推荐后，复盘页还会拿旧档再显示一轮（10 分钟 TTL）。
    # 定时批次（09:45~14:45）不经过 HTTP 写接口，改由 db 层写版本号 +
    # 下方 _daily_recs_cache_valid() 统一兜住，不再只靠 TTL 收敛（2026-11-05）。
    _REC_REVIEW_CACHE.clear()


def _daily_recs_cache_valid() -> bool:
    """这份缓存档还能不能返给前端（2026-11-05）。

    两个判据缺一不可：
      · version  == db 层当前写版本：定时批次/手动生成/保存/删除后必须重建，
        否则旧档里没有当天记录，前端会误显示「今日推荐尚未生成」；
      · built_date == 今天：跨天后旧档里的 isToday 标签是昨天算的，会把昨天
        的记录当成"今日推荐"（或反过来找不到今天），同样不能返。
    """
    if _DAILY_RECS_CACHE.get('payload') is None:
        return False
    try:
        ver = daily_recs_write_version()
    except Exception:
        ver = -1
    return (_DAILY_RECS_CACHE.get('version') == ver
            and _DAILY_RECS_CACHE.get('built_date') == date.today().isoformat())


def _stamp_daily_recs_cache(payload):
    """写回缓存时记录"这份档对应哪个写版本 / 哪一天"。"""
    try:
        ver = daily_recs_write_version()
    except Exception:
        ver = -1
    _DAILY_RECS_CACHE.update(ts=time.time(), payload=payload,
                             version=ver, built_date=date.today().isoformat())


def _kick_daily_recs_refresh():
    """后台线程重算 daily_recommendations（节流：已有刷新在跑就直接返回）"""
    global _DAILY_RECS_REFRESHING
    with _DAILY_RECS_REFRESH_LOCK:
        if _DAILY_RECS_REFRESHING:
            return
        _DAILY_RECS_REFRESHING = True
        gen = _DAILY_RECS_CACHE['generation']

    def _run():
        global _DAILY_RECS_REFRESHING
        try:
            payload = asyncio.run(_build_daily_recs_payload())
            with _DAILY_RECS_REFRESH_LOCK:
                if gen == _DAILY_RECS_CACHE['generation']:
                    _stamp_daily_recs_cache(payload)
                else:
                    # 刷新期间发生了写入：丢弃本次结果，让下一个请求同步重建
                    pass
        except Exception as _e:
            # 刷新失败不静默：旧档会继续服役（SWR 的容错面），但要有日志可查
            print(f'[历史推荐] 后台刷新失败（旧档继续服役）: {_e}')
        finally:
            with _DAILY_RECS_REFRESH_LOCK:
                _DAILY_RECS_REFRESHING = False

    threading.Thread(target=_run, daemon=True, name='daily-recs-refresh').start()



_REC_REVIEW_CACHE: Dict = {}         # {days: {'ts': 计算完成时刻, 'data': 汇总结果}}
_REC_REVIEW_TTL = 600                # 秒。数据源是推荐批次(09:45~14:45 五档)+日K，盘中确实会变，
                                     # 但复盘页是"事后看"的页，10 分钟延迟不影响任何决策；
                                     # 比 recommend-sim 的 1800s 保守，就是这个差别
_REC_REVIEW_MAX_KEYS = 8             # 只是兜底防线（万一以后白名单加宽）；正常只有下面两档会进来
_REC_REVIEW_CACHE_DAYS = (30, 120)   # **只有这两档进缓存**：`TabRecommend.vue` 用 days=30、
                                     # `TabBacktest.vue` 用 days=120（全仓 grep 就这两个消费方）。
                                     # 为什么不"任何 days 都留一份"：`days` 是自由参数、这接口不鉴权、
                                     # 隧道又公开。11-04 实测一串 `?days=1..10` 能把前端真正在用的
                                     # 两档从"最久未用"淘汰里挤光（此后每次都 0.3~0.5 秒现算），
                                     # 等于把缓存做成可被远程清空的对象。白名单之外一律按请求现算：
                                     # 代价就是回到改造前的耗时，但仍走 to_thread、不堵事件循环。


def _rec_review_cacheable(key: int) -> bool:
    return RECOMMEND_REVIEW_CACHE_ENABLED and key in _REC_REVIEW_CACHE_DAYS


def _remember_rec_review(key: int, data, ts: float) -> None:
    """写复盘缓存前先清掉已过期条目，仍超限就按"最久没被访问的先扔"淘汰。
    缓存换来的是响应速度，不能拿"任何查询参数都永久占一份"去交换。
    淘汰看 `used`（最后一次被请求读到的时刻）而不是 `ts`（数据算出的时刻）：
    11-04 实测按 ts 淘汰时，一串 `?days=1..10` 会把前端真正在用的 30/120 两档挤出去，
    回到"每次现算"；`ts` 还要留着算 cachedAgeSec，所以两个时间戳各管各的。"""
    # 2026-11-04：SWR 下「过期」不再等于「作废」，旧值还要服役到 stale_max，
    # 所以这里只能清真正超过服役上限的条目，否则会把自己刚返回的缓存删掉。
    for k in [k for k, v in _REC_REVIEW_CACHE.items() if ts - v['ts'] >= _SWR_STALE_MAX]:
        _REC_REVIEW_CACHE.pop(k, None)
    while len(_REC_REVIEW_CACHE) >= _REC_REVIEW_MAX_KEYS:
        least_used = min(_REC_REVIEW_CACHE,
                         key=lambda k: _REC_REVIEW_CACHE[k].get('used', _REC_REVIEW_CACHE[k]['ts']))
        _REC_REVIEW_CACHE.pop(least_used, None)
    _REC_REVIEW_CACHE[key] = {'ts': ts, 'used': ts, 'data': data}


def _touch_rec_review(key: int) -> None:
    """命中即标记"这个档刚被读过"，供上面的最久未用淘汰用。"""
    hit = _REC_REVIEW_CACHE.get(key)
    if hit:
        hit['used'] = time.time()


def _review_with_meta(data, ts: float):
    """给复盘汇总挂上"这份数是什么时候算的"（cachedAt / cachedAgeSec）。
    与趋势扫描的 scanAt 同一套语义：延长缓存就必须让用户看得见数据的时刻，
    否则等于拿看不见的新鲜度换响应速度。非 dict 原样返回，不猜。"""
    if not isinstance(data, dict):
        return data
    d = dict(data)
    d['cachedAt'] = datetime.fromtimestamp(ts).strftime('%H:%M:%S')
    d['cachedAgeSec'] = int(time.time() - ts)
    return d



_REC_SIM_CACHE: Dict = {}          # {(months, capital, gap, lots, bench): {ts, data}}
_EXCESS_CACHE: Dict = {}           # 可执行超额单独一份：冷算实测 7.4s，别把 0.2 秒的曲线缓存挤掉
_REC_SIM_TTL = 1800                # 秒：底层一天才变一次（14:45 推荐 + 16:10 复盘），算一次 0.2s
# 2026-11-04 全站巡检：下面几个「TTL 过期即同步重算」是回测曲线页 23.5s /
# 智能推荐页 10s 的共同根因。统一走文件顶部导入的 services.support.swr：
# 过期先返旧值，重算放后台线程；24h 内绝不把冷算压在用户请求上。
# 启动预热仍在 scheduler 里提前付掉首次 cold。









def prewarm_recommend_excess(months: int = 6) -> float:
    """启动预热用：提前把这份 7.4 秒的冷算结果塞进 `_EXCESS_CACHE`，返回耗时秒数。

    为什么需要它（2026-10-27 实拍）：服务重启会清掉进程内缓存，重启后第一个打开
    「回测曲线」页的人就要现等冷算；前端默认 10s 超时，实测那次直接报
    「超额没算出来：请求超时（10s）」。预热放后台线程跑，失败不致命（首次请求仍会现算）。
    """
    import time as _t
    from services.pick import rec_sim
    t0 = _t.time()
    data = rec_sim.open_hold_excess(months=months)
    _EXCESS_CACHE[(months,)] = {'ts': _t.time(), 'data': data}
    return _t.time() - t0



async def _search_stocks_remote(q: str) -> List[Dict]:
    """腾讯 smartbox 远程股票搜索（本地库未命中时的回退，2026-10-12）

    接口：https://smartbox.gtimg.cn/s3/?v=2&q={关键词}&t=all
    返回：v_hint="sz~000949~新乡化纤~xxhx~GP-A;sh~600000~..."（市场~代码~名称~拼音~类型）
    只保留 A 股（GP-A）；中文为 \\uXXXX 转义需解码。
    """
    try:
        from urllib.parse import quote
        import codecs
        # smartbox 不认带交易所前缀的代码（sh600000→无结果），代码查询剥掉前缀再传
        prefix = q.lower()[:2] if q.lower()[:2] in ('sh', 'sz', 'bj') else ''
        remote_q = re.sub(r'^(sh|sz|bj)', '', q.lower()) if re.fullmatch(r'(sh|sz|bj)?\d{1,6}', q.lower()) else q
        url = f'https://smartbox.gtimg.cn/s3/?v=2&q={quote(remote_q)}&t=all'
        resp = await asyncio.to_thread(_sync_get, url, None, (5, 8))
        text = resp.text.strip()
        m = re.search(r'v_hint="([^"]*)"', text)
        if not m or not m.group(1):
            return []
        raw = codecs.decode(m.group(1), 'unicode_escape')
        out = []
        seen = set()
        for item in raw.split(';'):
            parts = item.split('~')
            if len(parts) < 3:
                continue
            mkt, code, name = parts[0].strip().lower(), parts[1].strip(), parts[2].strip()
            typ = parts[4].strip() if len(parts) > 4 else ''
            if not re.fullmatch(r'\d{6}', code) or not name:
                continue
            if typ and typ != 'GP-A':
                continue  # 只要 A 股
            if mkt not in ('sh', 'sz', 'bj'):
                continue
            sym = mkt + code
            if sym in seen:
                continue
            seen.add(sym)
            out.append({'symbol': sym, 'name': name})
        # 查询显式带了交易所前缀时，该市场的结果排前（sz000001 优先平安银行）
        if prefix:
            out.sort(key=lambda x: 0 if x['symbol'].startswith(prefix) else 1)
        return out[:10]
    except Exception as e:
        print(f'[股票搜索] 远程回退失败: {e}')
        return []


# ========== 多用户账号认证（2026-10-12） ==========
# 只对私有数据（持仓）强制登录；行情/推荐/收评等公共接口保持免登录，
# 以免影响收评定时任务、推荐快照与截图脚本等既有自动化。


















# ========== 企业微信推送通道（按账号自助绑定，2026-10-14） ==========
# 别人注册后要接推送，只需要在自己手机上建个群、加个自定义机器人、把地址贴进来点绑定；
# 不需要改代码、不需要重启服务。绑定动作会真的发一条确认消息，发不出去就不算绑定成功。






















# ========== 模拟交易（纯模拟盘·不下真单） ==========












def _ensure_today_date(data: Dict) -> Dict:
    # 2026-07-06 : 已合并到 services.utils.ensure_today_date，此处保留薄壳透传以最小化改动
    return ensure_today_date(data)







# ========== 趋势评分回测验证 API（2026-07-01）==========











async def _load_scan_pool_detail(uid: int):
    try:
        symbols = load_user_scan_pool(user_id=uid)
        # 获取每只股票的名称 + 实时价 + 连涨连跌天数 + 是否在5日线上方
        stocks_info = []
        if symbols:
            try:
                # 一次性获取所有实时行情
                symbols_str = ','.join(symbols)
                data = await fetch_with_retry(symbols_str)
                parsed = parse_tencent_batch_data(data)
                quote_map = {p['symbol']: p for p in parsed}
                # 获取每只股票的K线 + 计算指标（并发控制）
                sem = asyncio.Semaphore(8)

                async def fetch_one(sym: str) -> Dict:
                    async with sem:
                        # -fix: get_stock_name 是 async def，必须 await，否则 info['name'] 会是 coroutine 对象
                        # FastAPI jsonable_encoder 序列化时炸 TypeError: 'coroutine' object is not iterable
                        name_from_quote = (quote_map.get(sym, {}) or {}).get('name')
                        if name_from_quote and not inspect.iscoroutine(name_from_quote):
                            info = {'symbol': sym, 'code': sym, 'name': name_from_quote}
                        else:
                            info = {'symbol': sym, 'code': sym, 'name': await get_stock_name(sym)}
                        realtime = 0
                        try:
                            price_val = quote_map.get(sym, {}).get('price', 0) or 0
                            # 防御：若 price 是 coroutine 同样丢弃
                            if not inspect.iscoroutine(price_val):
                                realtime = float(price_val)
                        except (TypeError, ValueError):
                            pass
                        info['realtimePrice'] = realtime
                        try:
                            kline = await get_kline_data(sym)
                            indicators = calc_pool_stock_indicators(kline or [], realtime)
                            # 防御：剥离 coroutine（极端情况），递归处理嵌套 dict/list
                            def _strip_coro(obj):
                                if inspect.iscoroutine(obj):
                                    return None
                                if isinstance(obj, dict):
                                    return {k: _strip_coro(v) for k, v in obj.items()}
                                if isinstance(obj, (list, tuple)):
                                    return [_strip_coro(x) for x in obj]
                                return obj
                            info.update(_strip_coro(indicators))
                        except Exception as inner_err:
                            print(f'[custom-scan-pool] {sym} 指标计算失败: {inner_err}')
                            info.update({'latestPrice': realtime, 'consecutiveUp': 0, 'consecutiveDown': 0, 'aboveMa5': False, 'ma5': None})
                        return info

                stocks_info = await asyncio.gather(*[fetch_one(s) for s in symbols])
            except Exception as e:
                import traceback
                print(f'[custom-scan-pool] 拉取行情失败: {e}\n{traceback.format_exc()}')
                stocks_info = [{'symbol': s, 'code': s, 'name': get_stock_name(s), 'latestPrice': 0, 'consecutiveUp': 0, 'consecutiveDown': 0, 'aboveMa5': False, 'ma5': None} for s in symbols]
        return {"success": True, "data": {"symbols": symbols, "count": len(symbols), "stocks": stocks_info}}
    except Exception as error:
        import traceback
        print(f'[custom-scan-pool] 顶层异常: {error}\n{traceback.format_exc()}')
        raise HTTPException(status_code=500, detail=f"获取自定义扫描池失败: {error}")














# ========== 股票标签API ==========





















# ========== 策略管理 API ==========














# ========== 运行时配置覆盖（复盘子页"口径与开关"，2026-10-17） ==========
# 为什么读和写的权限不一样：这些开关一拨就改变**所有账号**的选票范围与推送量，
# 而 users 表没有角色列（自注册体系）—— 所以读=登录即可，写=只允许
# `SETTINGS_WRITE_USER_IDS` 里的账号 id，改动人会记进 app_settings.updated_by 便于回溯。


























# ========== 东方财富 API 代理（兼容生产模式） ==========
# 前端 fetchEnrichData() 需要通过 /eastmoney/* 调用东方财富接口
# Vite 开发模式下由 vite.config.js 代理；生产模式下由此路由代理
# 2026-07-02 : 请求头统一使用 config.EASTMONEY_HEADERS
# 2026-07-06 A-: 模块级 requests.Session 复用连接池，避免每次请求新建 TCP 连接
import requests as _req
_eastmoney_session: Optional[_req.Session] = None

def _get_eastmoney_session() -> _req.Session:
    """获取模块级 requests.Session（懒初始化，复用连接池）"""
    global _eastmoney_session
    if _eastmoney_session is None:
        _eastmoney_session = _req.Session()
        _eastmoney_session.headers.update(EASTMONEY_HEADERS)
        # 配置 HTTPAdapter 复用连接池（pool_connections=连接数, pool_maxsize=最大并发）
        adapter = _req.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=0)
        _eastmoney_session.mount('https://', adapter)
        _eastmoney_session.mount('http://', adapter)
    return _eastmoney_session



# ========== 自动优化引擎 API（V4.4）==========









# 最后挂载静态文件，这样API路由会先被匹配到


__all__ = [n for n in globals() if not n.startswith("__")]
