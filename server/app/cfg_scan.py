import json
import os
from datetime import datetime

# ========== 扫描配置 ==========
SCAN_CONCURRENCY = 25  # 扫描并发数
SCAN_KLINE_TIMEOUT = 12.0  # 扫描K线超时时间
SCAN_CACHE_TTL = 300  # 扫描缓存过期时间（秒）默认300秒
# stale-while-revalidate（2026-11-03）：内存缓存是**今天的**但已过 TTL 时，不再让打开趋势页的
# 用户同步扛一轮现扫——实测现扫 6.6 / 17.8 / 41.7 秒（取决于外部行情源快慢），且扫描期间
# 事件循环被占，并发实验实测连 /api/health 都被拖到 4.8 秒。改成先返回旧数据（带 stale 标记
# 与 scanAt 扫描时刻，前端可见）+ 后台起一轮重扫。
# 注意 TTL(300s) 与定时扫描周期(1800s，09:00~15:00)本来就不匹配 → 一天里只有约 17% 的时间
# 缓存算"新鲜"，其余时间打开页面都要现扫；这也是为什么单靠延长 TTL 治不了收盘后的场景。
# 关掉本开关 = 逐字节回到改造前（过期即同步现扫）。
TREND_STALE_WHILE_REVALIDATE = True

# 2026-10-24（性能）：asyncio.to_thread 全项目共用 asyncio 默认线程池
# （min(32, CPU+4)，本机 12 核 = 16 线程），而趋势扫描并发 25、每票至少占
# 2 次 to_thread（本地日K + 行情），扫描期间线程池被打满，全站 to_thread
# 请求排队（实测扫描中连 401 拒绝都要 20s+）。此处为 lifespan 扩容上限，
# 扫描是 IO 密集（等腾讯/新浪响应），线程数放大不挤占 CPU。
TO_THREAD_POOL_MAX_WORKERS = 48


# ========== 股票筛选 Tab 过滤配置（2026-07-08）==========
# 第二个 Tab "股票筛选"（/api/hot-stocks-scan）的可选过滤模式
# 用户在前端可切换：all(全部) / bull_only(仅看涨) / bull_and_hold(观望以上) / min_score(自定义分)
# 注意：当前后端默认仍返回全量，前端按此配置做客户端过滤；如需服务端过滤可将 FILTER_MODE 设为 'all' 之外的任意值并改 hot-stocks-scan 路由
# 2026-09-10 默认改为"自定义最低分 60"，让用户一进页面就看到相对高分的股票（观望/看涨），避免弱票刷屏。
# 2026-10-24 该默认值移出本文件：改由「配置 → 股票筛选页」的 tab_scan_default_min_score 管理
# （注册表在 services/support/app_settings.py，live 即时生效，/api/tab-scan-config 每次现读）。
# 2026-10-28 删除 leaders_only（仅优质龙头）模式：角色体系整体下线，筛选只剩分档，不再按角色筛票。
TAB_SCAN_FILTER_MODES = ['all', 'bull_only', 'bull_and_hold', 'min_score']  # 前端下拉选项
TAB_SCAN_DEFAULT_MODE = 'min_score'  # 默认模式：自定义最低分
TAB_SCAN_BULL_MIN_SCORE = 70  # "看涨"区间的最低分（≥ 此值为看涨）
TAB_SCAN_BEAR_MAX_SCORE = 40  # "看跌"区间的最高分（< 此值为看跌）
# 区间划分：score < BEAR_MAX(40) 为看跌，BEAR_MAX(40) ≤ score < BULL_MIN(70) 为观望，score ≥ BULL_MIN(70) 为看涨
# "观望以上" 指 score ≥ BULL_MIN_SCORE，仅看涨也是 score ≥ BULL_MIN_SCORE



__all__ = ['SCAN_CACHE_TTL', 'SCAN_CONCURRENCY', 'SCAN_KLINE_TIMEOUT', 'TAB_SCAN_BEAR_MAX_SCORE', 'TAB_SCAN_BULL_MIN_SCORE', 'TAB_SCAN_DEFAULT_MODE', 'TAB_SCAN_FILTER_MODES', 'TO_THREAD_POOL_MAX_WORKERS', 'TREND_STALE_WHILE_REVALIDATE']
