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



__all__ = ['EASTMONEY_HOT_LIST_API', 'HTTP_CONNECT_TIMEOUT', 'HTTP_KEEPALIVE_CONNECTIONS', 'HTTP_MAX_CONNECTIONS', 'HTTP_TIMEOUT', 'SINA_QUOTE_API', 'TENCENT_KLINE_API', 'TENCENT_QUOTE_API']
