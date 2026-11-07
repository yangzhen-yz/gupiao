import json
import os
from datetime import datetime

# ========== 数据库配置 ==========
SQLITE_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'gupiao.db')

# ---- 写锁分级（2026-11-03）----
# 背景：get_db_conn() 用 isolation_level='IMMEDIATE' + busy_timeout=30000，而 WAL 同时只允许
# 一个写者。2026-11-03 实测：回测补验在事务里等外部 K 线，把写锁占了 1 小时以上，期间连
# /api/health 都要排队 8~30 秒（X-Process-Time 只有 1~2ms，耗时全在中间件之前）。根治办法是
# 别在事务里等 IO（已改），但锁竞争还会以别的形态出现（多个定时任务同时写），所以这里顺手把
# **失败方式**改掉：缓存类写入抢不到锁就快速失败（读侧有旧数据兜底，丢一次下次重拉），
# 留痕类继续长等（strategy_snapshot / 推荐批次 / 用户持仓这类写失败就是永久丢行）。
# 为什么不走 app_settings 覆盖表：app_settings.py 顶层就 `from db.database import get_db_conn`，
# 它的 value() 自己要用数据库连接——接进来会无限递归。所以这两项**改完要重启后端**才生效
# （写 server/data/backend_restart_requested 让看护重启），配置页里看不到它们是有原因的。
DB_FAST_FAIL_ENABLED = True            # 总开关。False=所有写入仍用 30000ms，逐字节等于改造前
DB_CACHE_WRITE_BUSY_TIMEOUT_MS = 2000  # 缓存/快照类写入的 busy_timeout（毫秒），留痕类不受影响

# ---- 默认方向翻过来（2026-11-07，修⑤）----
# 上面那套分级是"默认等 30 秒、缓存类点名快速失败"，问题在于**没点名的新写法自动落到长等**：
# 11-04 实测 database.py 之外还有 114 处 `get_db_conn()` 从没分类过，也就是说那 114 处的
# 失败方式全靠"没人记得改"来决定 —— 11-03 那次全站排队 8~30 秒，放大器正是这种长等。
# 现在反过来：**默认就是 2 秒快速失败**，只有点名 `wait_for_lock=True` 的留痕类才等
# `DB_TRACE_WRITE_BUSY_TIMEOUT_MS`。漏点的代价从"全站多等 30 秒"变成"这一行没落库"，
# 后者轻得多：留痕类每一处都在 DAL 里有 `print('保存 X 失败')` 的现成出口，看得见；
# 而长等是悄悄把整站拖死的（那次连 /api/health 都在排队）。
# 哪些算留痕、为什么这么分，见 `scripts/db_writelock_default_selftest.py` 的 TRACE_TABLES
# 与 `docs/工程知识库.md`「写锁分级：默认快速失败 + 留痕白名单」。
DB_TRACE_WRITE_BUSY_TIMEOUT_MS = 30000  # 点名要等锁的留痕/用户数据写入的 busy_timeout（毫秒）

# ---- 盘中写锁哨兵（2026-11-03）----
# 独立连接试 BEGIN IMMEDIATE，等超阈值就拿不到 → 推企微，附"最后写入几分钟前 + 最近任务锚点"。
# 这三项已接进 app_settings 覆盖表（配置页「数据库与写锁」，live=True 可盘中热改），
# 这里是厂默认值。哨兵拿到锁会立刻 ROLLBACK，它自己不会变成持锁者。
DB_LOCK_SENTINEL_ENABLED = True        # 关掉=完全不探测、不推送
DB_LOCK_SENTINEL_THRESHOLD_SEC = 2     # 等超过这么多秒仍拿不到写锁就告警（1~10）
DB_LOCK_SENTINEL_COOLDOWN_MIN = 10     # 同一状态多久内只推一次，防刷屏（1~60）


# ========== 东方财富请求头（统一常量） ==========
# 2026-07-02 优化：从 review.py/ai.py/routes.py 三处分散定义统一到此处
EASTMONEY_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Referer': 'https://quote.eastmoney.com/',
    'Origin': 'https://quote.eastmoney.com',
}


__all__ = ['DB_CACHE_WRITE_BUSY_TIMEOUT_MS', 'DB_FAST_FAIL_ENABLED', 'DB_LOCK_SENTINEL_COOLDOWN_MIN', 'DB_LOCK_SENTINEL_ENABLED', 'DB_LOCK_SENTINEL_THRESHOLD_SEC', 'DB_TRACE_WRITE_BUSY_TIMEOUT_MS', 'EASTMONEY_HEADERS', 'SQLITE_DB_PATH']
