"""Shared DAL state, connection/cache/migration helpers and cross-domain functions.

Extracted from the former monolithic database.py. Exposes everything via ``__all__``
so domain modules can do ``from ..dal_state import *`` and keep their functions verbatim.
"""

import sqlite3, json, time, threading


from typing import List, Dict, Optional


from datetime import date, datetime, timedelta


from app.config import SQLITE_DB_PATH, QUERY_DAILY_RECOMMENDATIONS_LIMIT, QUERY_TREND_RESULTS_LIMIT, QUERY_MARKET_REVIEWS_LIMIT, HOT_SEARCH_TOP_N, HOT_SEARCH_SNAPSHOT_CLEANUP_DAYS, DEFAULT_WEIGHTS


"""database.py - split from main.py"""


SQLITE_DB_PATH = SQLITE_DB_PATH


_DB_TTL_CACHE: Dict[str, Dict] = {}


_TS_MIGRATION_TABLES = {
    'daily_market_reviews': ['created_at'],
    'trend_scan_results': ['created_at'],
    'hot_stock_buttons': ['created_at', 'updated_at'],
    'stock_alias_map': ['created_at', 'updated_at'],
    'strategy_predict_records': ['created_at'],
    'strategy_factor_weights': ['updated_at'],
    'strategy_backtest_reports': ['created_at'],
    'hot_search_meta': ['updated_at'],
    'daily_recommendations': ['created_at'],
    'stock_basic_info': ['updated_at'],
    'user_scan_pool': ['created_at'],
    'stock_concept_tags': ['updated_at'],
    'trend_backtest_records': ['created_at'],
    'trend_backtest_reports': ['created_at', 'updated_at'],
}


_DAILY_RECS_WRITE_VERSION = 0


_DAILY_RECS_WRITE_LOCK = threading.Lock()


FACTOR_DESCRIPTIONS = {
    'outer_ratio': '外盘占比',
    'volume_ratio': '量比',
    'turnover_rate': '换手率',
    'change_percent': '涨跌幅',
    'weibi': '委比',
    'avg_price_deviation': '均价偏离',
    'amplitude': '振幅',
    'position': '位置评估',
    'main_capital_inflow': '主力净流入',  # （2026-06-30）：第 9 因子，配套展示中文
    'holder_structure': '筹码集中度',  # （2026-07-06）：第 10 因子，股东人数筹码面
    'rps': 'RPS相对强度',  # （2026-07-16）：第 11 因子，欧奈尔相对强度
    'fundamental': '基本面质量',  # （2026-07-16 P1a）：第 12 因子，ROE+净利润增速
    'smart_money': '聪明钱',  # （2026-07-16 P1b）：第 13 因子，龙虎榜净买+北向
}


_DEFAULT_HOT_STOCKS = [
    {'name': '深科技', 'code': 'sz000021'},
    {'name': '亨通光电', 'code': 'sh600487'},
    {'name': '中天科技', 'code': 'sh600522'},
    {'name': '东山精密', 'code': 'sz002384'},
    {'name': '光迅科技', 'code': 'sz002281'},
    {'name': '利通电子', 'code': 'sh603629'},
]


DEFAULT_STOCK_TAGS = {
    'sz000021': ['存储芯片', '半导体', '华为概念', '人工智能'],
    'sh600487': ['光通信', '海洋科技', '特高压'],
    'sh603618': ['光通信', '特高压', '智能电网'],
    'sz002281': ['光模块', '光纤通信', '华为概念'],
    'sh603629': ['消费电子', '新能源', '智能控制'],
    'sh600186': ['农业', '食品加工', '乡村振兴'],
    'sh603131': ['机器人', '工业4.0', '智能装备'],
    'sz002851': ['新能源', '工业自动化', '智能制造'],
    'sh603985': ['风电', '碳中和', '新能源'],
    'sh603738': ['消费电子', '芯片', '华为概念'],
    'sz003031': ['半导体', '芯片', '汽车电子'],
    'sh600884': ['锂电池', '新能源', '储能'],
    'sz002709': ['锂电池', '新能源', '储能'],
    'sh603256': ['PCB', '电子制造', '5G'],
    'sz002008': ['激光设备', '高端制造', '工业4.0'],
    'sz002636': ['PCB', '5G', '消费电子'],
    'sh601208': ['新材料', '化工', '半导体材料'],
    'sz002080': ['新材料', '玻纤', '风电'],
    'sh600584': ['芯片封测', '半导体', '华为概念'],
    'sz002156': ['芯片封测', '半导体', '算力'],
    'sz002185': ['芯片封测', '半导体', '5G'],
    'sz300014': ['锂电池', '储能', '新能源'],
    'sz002484': ['电容', '新能源', '光伏'],
    'sh600522': ['光通信', '海洋科技', '特高压'],
    'sh600118': ['航天军工', '北斗导航', '卫星通信'],
    'sz002384': ['消费电子', '精密制造', '汽车电子'],
    'sh603876': ['锂电池', '新能源', '储能'],
    'sz001309': ['存储芯片', '半导体', '消费电子'],
    'sz000063': ['5G', '通信设备', '数字经济'],
    'sz002241': ['VR/AR', '消费电子', '元宇宙'],
    'sz002475': ['消费电子', '苹果概念', '精密制造'],
    'sz300124': ['工业自动化', '机器人', '智能制造'],
    'sz002230': ['人工智能', 'AI', 'ChatGPT'],
    'sh688981': ['芯片', '半导体', '国产替代'],
    'sh601991': ['电力', '新能源发电', '绿色能源'],
    'sz002428': ['半导体材料', '锗', '稀缺资源'],
    'sz002222': ['激光', '光学', '半导体'],
    'sh600330': ['磁性材料', '稀土', '新能源'],
    'sz000988': ['激光', '5G', '光通信', '华为'],
    'sh600105': ['光通信', '光纤', '特高压'],
    'sz002015': ['新能源', '储能', '换电'],
    'sz000066': ['信创', '国产替代', '信息安全'],
    'sh605196': ['电线电缆', '新能源', '电网'],
    'sz000811': ['制冷设备', '碳中和', '氢能源', '冷链物流'],
    'sz002463': ['PCB', '汽车电子', '5G', '半导体'],
    'sz002916': ['PCB', '汽车电子', '服务器', '算力'],
    'sz002938': ['PCB', '消费电子', '苹果概念', '精密制造'],
    'sz300476': ['PCB', '新能源', '汽车电子', '5G'],
    'sz300657': ['PCB', '消费电子', 'MiniLED', 'VR/AR'],
    'sz002579': ['PCB', '消费电子', '汽车电子', '柔性电子'],
    'sh603920': ['PCB', '消费电子', '苹果概念', '5G'],
    'sz002913': ['PCB', '消费电子', '汽车电子', '高端制造'],
    'sz002815': ['PCB', '5G', '服务器', '汽车电子'],
    'sh603228': ['PCB', '消费电子', '汽车电子', '5G'],
    'sh600183': ['PCB', '5G', '服务器', '半导体材料']
}


_OPTIMIZER_V2_SCHEMA_READY = False


def get_db_conn(wait_for_lock: bool = False, fast_fail: Optional[bool] = None):
    """获取 SQLite 数据库连接 —— **默认是"2 秒抢不到就失败"，要等锁得点名**（2026-11-07 修⑤翻的）。

    两种意图：

    - 默认（什么都不传）：`busy_timeout = DB_CACHE_WRITE_BUSY_TIMEOUT_MS`（2 秒）。抢不到写锁就抛
      `database is locked`，调用方现成的 `except` 会打印"保存 X 失败"并把这次丢掉，下一轮重拉/重写。
      适用于缓存类、可重拉的数据快照，以及**盘中高频**的影子/告警落库（那种点一旦等 30 秒，
      卡住的是整个事件循环，见下）。
    - `wait_for_lock=True`：`busy_timeout = DB_TRACE_WRITE_BUSY_TIMEOUT_MS`（30 秒）。给"丢了就永久
      答不了"的行：决策留痕（`strategy_snapshot` / 推荐批次 / 收评 / 权重 / 预测记录）、用户数据
      （持仓 / 自选 / 标签 / 按钮 / 本金 / 推送绑定 / 账号）、模拟盘的钱与订单、接口用量累加。

    为什么把方向翻过来（11-04 那条"要么分完、要么反过来"选的就是反过来）：老写法是"默认等 30 秒、
    缓存类点名快速失败"，而 `database.py` 之外实测还有 **115 处** `get_db_conn()` 从没分类过 ——
    默认方向决定了这上百处**没点名**的点的失败方式，靠人记住传参数是守不住的。翻过来之后漏点儿的
    代价从"全站排队 30 秒"变成"这一行没落库、日志里看得见"：11-03 那次真正的全站卡死（写锁被占 1 小时+，
    连 `/api/health` 都排队 8~30 秒，`X-Process-Time` 只有 1~2ms 说明耗时全在中间件之前）放大器就是长等。

    `fast_fail` 是 11-03 那版的老参数名，**语义原样保留**（`True`=快速失败，跟现在默认一致；
    `False`=等 30 秒，等于现在的 `wait_for_lock=True`），所以历史调用点不会因为翻转而改变行为。
    新代码一律写 `wait_for_lock=`，`scripts/db_writelock_default_selftest.py` 里有静态守卫盯着：
    凡是写留痕表的点必须点名，漏一处就报红。

    为什么不走 `app_settings` 覆盖表：`app_settings.py` 顶层就 `from db.database import get_db_conn`，
    它的 `value()` 自己要用数据库连接，接进来会无限递归 —— 所以这几项改完要重启后端才生效。
    """
    if fast_fail is not None:
        wait_for_lock = not fast_fail        # 老名字的老语义：fast_fail=False 就是"要等锁"
    timeout_ms = 30000                       # 读不到配置时的兜底 = 改造前的行为，绝不因新开关而变短
    try:
        from app.config import (DB_FAST_FAIL_ENABLED, DB_CACHE_WRITE_BUSY_TIMEOUT_MS,
                                DB_TRACE_WRITE_BUSY_TIMEOUT_MS)
        if not DB_FAST_FAIL_ENABLED:
            # 总开关关掉：所有写入一律长等，逐字节等于分级改造之前
            timeout_ms = max(100, int(DB_TRACE_WRITE_BUSY_TIMEOUT_MS))
        elif wait_for_lock:
            timeout_ms = max(100, int(DB_TRACE_WRITE_BUSY_TIMEOUT_MS))
        else:
            timeout_ms = max(100, int(DB_CACHE_WRITE_BUSY_TIMEOUT_MS))
    except Exception:
        timeout_ms = 30000
    # 下限 100ms：防止配置页/手改成 0 之后变成"永远抢不到锁"
    # isolation_level='IMMEDIATE'：写语句隐式以 BEGIN IMMEDIATE 开启事务，一开始就抢写锁。
    # 避免 DEFERRED 事务「先读后写」升级锁时 SQLite 不重试 busy_timeout 直接返回 SQLITE_BUSY。
    conn = sqlite3.connect(SQLITE_DB_PATH, timeout=timeout_ms / 1000.0, isolation_level='IMMEDIATE')
    conn.row_factory = sqlite3.Row
    # busy_timeout 必须先于 journal_mode 设置：切换 WAL 本身也需要抢数据库锁。
    conn.execute("PRAGMA busy_timeout=%d" % timeout_ms)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _db_cache_get(key: str):
    """命中且在 TTL 内返回缓存数据，否则 None"""
    item = _DB_TTL_CACHE.get(key)
    if item and (time.time() - item['ts']) < item['ttl']:
        return item['data']
    return None


def _db_cache_set(key: str, data, ttl: float = 60.0):
    _DB_TTL_CACHE[key] = {'ts': time.time(), 'ttl': ttl, 'data': data}
    # 惰性清理：超过 200 条时全清（防止内存无限增长）
    if len(_DB_TTL_CACHE) > 200:
        _DB_TTL_CACHE.clear()


def _db_cache_invalidate(key: str):
    _DB_TTL_CACHE.pop(key, None)


def _payload_hash(recs: List[Dict]) -> str:
    """批次内容指纹：只看影响复盘结论的字段（代码/分数/现价/买卖点/理由）。"""
    import hashlib
    slim = []
    for r in recs or []:
        bsp = r.get('buySellPoints') or {}
        slim.append([r.get('symbol', ''), r.get('score', 0), str(r.get('price', '')),
                     str(bsp.get('buy', '')), str(bsp.get('current', '')),
                     str(bsp.get('sell', '')), r.get('mode', ''), r.get('reason', '')])
    return hashlib.md5(json.dumps(slim, ensure_ascii=False).encode('utf-8')).hexdigest()


def _bump_daily_recs_write_version():
    global _DAILY_RECS_WRITE_VERSION
    with _DAILY_RECS_WRITE_LOCK:
        _DAILY_RECS_WRITE_VERSION += 1


def _ensure_optimizer_v2_columns(cursor) -> None:
    """惰性补列（2026-11-01 T1.3）：只加列、不删不改旧列。

    - optimization_history：新 metric 与旧 discrimination_avg 并存，旧列继续写，
      新列只做审计与回填；objective_version 标明这一行由哪套口径产生。
    - trend_dimension_weights：valid_neg_streak 记录连续负向次数，用于 0.8 回落。
    """
    global _OPTIMIZER_V2_SCHEMA_READY
    if _OPTIMIZER_V2_SCHEMA_READY:
        return
    try:
        hist_cols = {row[1] for row in cursor.execute('PRAGMA table_info(optimization_history)')}
        for name, ddl in (
            ('metric_v2_name', "TEXT DEFAULT ''"),
            ('metric_v2_value', 'REAL DEFAULT NULL'),
            ('objective_version', "TEXT DEFAULT 'v1'"),
            ('walk_forward', 'INTEGER DEFAULT 0'),
            ('backfill_note', "TEXT DEFAULT ''"),
        ):
            if name not in hist_cols:
                cursor.execute(f'ALTER TABLE optimization_history ADD COLUMN {name} {ddl}')

        w_cols = {row[1] for row in cursor.execute('PRAGMA table_info(trend_dimension_weights)')}
        if 'valid_neg_streak' not in w_cols:
            cursor.execute('ALTER TABLE trend_dimension_weights ADD COLUMN valid_neg_streak INTEGER DEFAULT 0')
        _OPTIMIZER_V2_SCHEMA_READY = True
    except Exception as error:
        # 补列失败不阻断主流程：旧列仍可写，调用方会打印告警
        print(f'[优化器] 补列失败: {error}')



def daily_recs_write_version() -> int:
    """当前推荐表写版本号（进程内单调；缓存校验用，不落库）。"""
    return _DAILY_RECS_WRITE_VERSION


__all__ = [n for n in globals() if not n.startswith('__')]
