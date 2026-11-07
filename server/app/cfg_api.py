import json
import os
from datetime import datetime

# ========== API 地址配置 ==========
TENCENT_QUOTE_API = 'https://qt.gtimg.cn/q='
TENCENT_KLINE_API = 'https://ifzq.gtimg.cn/appstock/app/fqkline/get'
SINA_QUOTE_API = 'https://hq.sinajs.cn/list='
EASTMONEY_HOT_LIST_API = 'https://push2delay.eastmoney.com/api/qt/clist/get'


# ========== HTTP 客户端配置 ==========
HTTP_TIMEOUT = 10.0  # HTTP 请求超时时间
HTTP_CONNECT_TIMEOUT = 5.0  # HTTP 连接超时时间
HTTP_MAX_CONNECTIONS = 50  # HTTP 最大连接数
HTTP_KEEPALIVE_CONNECTIONS = 25  # HTTP 保持连接数

# ========== 东财熔断策略（2026-11-06） ==========
# 起因：11-06 09:15:04 一次 push2delay/clist 连接被重置 → 全域闸门开 14400s，
# 把 14 天失败率只有 1.0% 的 push2ex 三池一起关到 13:16；情绪快照 33 行退潮/冰点
# 全部标 stale=1，于是"转弱推送/修复推送/减仓影子"整天静音（用户问"今天咋没推大盘"）。
# 三个开关都支持关掉回到改造前（逐字节等价），关掉即恢复 10-19 的"全域一起停手"。
EASTMONEY_FAMILY_GATE_ENABLED = True   # 按主机家族分闸：blocked 只关出事那条链路
EASTMONEY_IP_GATE_FAMILIES = 2         # 同时有几个家族 blocked 才判定"封的是 IP"→ 全域闸门
EASTMONEY_DAY_STOP_ENABLED = True      # blocked 熔断后不是一休 4 小时，而是停到当日 23:59:59
                                       # （次日自动恢复；期间每 PROBE_SEC 放行一次探测）
EASTMONEY_DAY_STOP_PROBE_SEC = 1800    # 当日停调期间的探测窗口（秒）：0 = 严格"当天不再调"
                                       # （探测请求成功会照旧解除停调，所以真解封不会白等到收盘）
                                       # 人工暂停另走 API：/api/data-sources/eastmoney-pause


__all__ = ['EASTMONEY_DAY_STOP_ENABLED', 'EASTMONEY_DAY_STOP_PROBE_SEC',
           'EASTMONEY_FAMILY_GATE_ENABLED', 'EASTMONEY_HOT_LIST_API',
           'EASTMONEY_IP_GATE_FAMILIES',
           'HTTP_CONNECT_TIMEOUT', 'HTTP_KEEPALIVE_CONNECTIONS', 'HTTP_MAX_CONNECTIONS',
           'HTTP_TIMEOUT', 'SINA_QUOTE_API', 'TENCENT_KLINE_API', 'TENCENT_QUOTE_API']
