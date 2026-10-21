"""api_usage.py - 第三方接口调用用量统计与上限预警（2026-10-19）

背景：东财 push2/push2delay 一封 IP 就是几小时（10-17、10-19 两次实锤），根因是请求频率。
系统现在每天会调东财 / 腾讯 / 新浪 / westock / 企业微信 / DeepSeek 等多家第三方接口，
但**没有任何地方统计"到底调了多少次"**——改代码时很容易不小心把某个接口的日调用量
推过平台的封禁阈值，等被封了才从页面变空白倒推。

本模块做三件事：
  1. **自动计数**：install() 在进程启动早期 monkeypatch requests / httpx / urllib 的发送入口，
     任何出站 HTTP 调用都按 主机→(provider:interface) 自动分类并 +1（localhost / 内网 IP 不计数）。
     对未来的新接口零侵入：只要走 requests 等标准库，就自动被统计。
  2. **显式计数**：westock CLI 是子进程、不走 HTTP，由调用方在 westock_crosscheck.py 显式 record()。
  3. **可视化 + 上限预警**：get_stats() 给出每个接口今日调用数 / 上限 / 使用率 / 是否预警，
     get_history() 给出近 14 天趋势，落库到 api_usage_daily；管理员可改上限（POST /api/api-usage/limits）。

设计要点：
  · 内存计数器按"自然日"累计；跨天自动把旧天数落库再清零（flush 每 60s 兜底，防丢）。
  · 计数与失败率分开：count = 总调用；fail = HTTP>=400 或异常；ok = 其余。
    被封禁最典型的信号就是 fail 暴涨 + count 逼近上限，两列一起看最清楚。
  · 重启后从 DB 读回今日已计数，继续累计（不会把当天从 0 开始）。
  · 全部加 threading.Lock；record() 是纯内存写，同步/async 上下文都能安全调。
"""
import threading
import time
import ipaddress
from datetime import datetime
from urllib.parse import urlparse

# ========== 第三方主机 → (provider, 默认 interface) ==========
# interface 为 None 时按 path 细分（东财 push2delay 上挂了 clist/ulist/stock 多个业务）。
_HOST_EXACT = {
    'qt.gtimg.cn': ('tencent', 'quote'),        # 腾讯即时行情
    'ifzq.gtimg.cn': ('tencent', 'kline'),      # 腾讯K线
    'hq.sinajs.cn': ('sina', 'quote'),          # 新浪行情备源
    'push2delay.eastmoney.com': ('eastmoney', None),   # 人气榜/涨停池/个股 等同主机不同 path
    'push2.eastmoney.com': ('eastmoney', 'push2'),     # 行情推送主接口
    'push2.ex.eastmoney.com': ('eastmoney', 'pools'), # 涨停/跌停/炸板池
    'datacenter-web.eastmoney.com': ('eastmoney', 'datacenter'),  # 龙虎榜等
    'np-anotice-stock.eastmoney.com': ('eastmoney', 'ann'),       # 公告
    'emappdata.eastmoney.com': ('eastmoney', 'appdata'),          # App数据
    'qyapi.weixin.qq.com': ('wecom', 'push'),    # 企业微信推送
    # 2026-10-20：豆包/ARK/硅基流动/DeepSeek/Agnes 等 AI 源随 AI 诊断功能整体下线。
    # 以后若接入新 AI 供应商，在这里登记 主机→(provider,'chat') 即可被用量页统计。
}
_HOST_SUFFIX = [
    ('.baostock.com', ('baostock', 'data')),
]

# 东财 push2delay 同一主机按 path 落到不同接口计数
def _em_path_interface(path: str) -> str:
    p = (path or '').lower()
    if '/clist' in p:
        return 'clist'
    if '/ulist' in p:
        return 'ulist'
    if '/stock' in p:
        return 'stock'
    return 'clist'   # 兜底：绝大多数调用都是 clist


# ========== 显示用中文标签 ==========
PROVIDER_LABEL = {
    'tencent': '腾讯', 'sina': '新浪', 'eastmoney': '东财',
    'wecom': '企业微信', 'baostock': 'Baostock',
    'westock': 'Westock', 'other': '其他',
}
INTERFACE_LABEL = {
    'quote': '行情', 'kline': 'K线', 'clist': '人气榜', 'ulist': '自选列表',
    'stock': '个股', 'push2': '行情推送', 'pools': '涨停池', 'datacenter': '数据中心',
    'ann': '公告', 'appdata': 'App数据', 'push': '消息推送', 'chat': 'AI对话',
    'data': '行情数据', 'fundflow': '资金流',
}

# ========== 默认每日上限（可在页面改，改后持久化覆盖） ==========
# 取值依据：东财 clist 是封禁重灾区（10-19 实锤 840 次/小时峰值），给保守上限；
# 腾讯/新浪行情是主备源，量级大但平台宽松，给宽上限；AI/企微是低频源。
DEFAULT_LIMITS = {
    'eastmoney:clist': 8000,
    'eastmoney:ulist': 2000,
    'eastmoney:stock': 5000,
    'eastmoney:push2': 5000,
    'eastmoney:pools': 3000,
    'eastmoney:datacenter': 2000,
    'eastmoney:ann': 1000,
    'eastmoney:appdata': 1000,
    'tencent:quote': 50000,
    'tencent:kline': 10000,
    'sina:quote': 20000,
    'wecom:push': 1500,
    'baostock:data': 5000,
    'westock:quote': 4000,
    'westock:fundflow': 4000,
}

ALERT_THRESHOLD = 0.8   # 使用率 ≥ 80% 预警

# ========== 内存状态 ==========
_lock = threading.Lock()
_counters: dict = {}            # "provider:interface" -> {count, ok, fail, first_ts, last_ts}
_counters_date: str = ''
_limits: dict = {}              # "provider:interface" -> int（DB 覆盖值，否则用默认）
_installed = False


# ========== 出站 URL 分类 ==========
def _url_str(url) -> str:
    if isinstance(url, str):
        return url
    for attr in ('url', 'full_url'):
        v = getattr(url, attr, None)
        if isinstance(v, str) and v:
            return v
    return str(url)


def _is_internal(host: str) -> bool:
    if not host:
        return True
    h = host.lower()
    if h in ('localhost', '127.0.0.1', '0.0.0.0', '::1', ''):
        return True
    if h.endswith('.local') or h.endswith('.internal') or h.endswith('.corp'):
        return True
    try:
        ip = ipaddress.ip_address(h)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


def _provider_for_host(host: str):
    if host in _HOST_EXACT:
        return _HOST_EXACT[host]
    for suf, prov in _HOST_SUFFIX:
        if host.endswith(suf):
            return prov
    return ('other', host)


def classify(url) -> str | None:
    """把出站 URL 归到 provider:interface；内网/localhost 返回 None（不计入第三方）。"""
    s = _url_str(url)
    try:
        p = urlparse(s)
        host = (p.hostname or '').lower()
    except Exception:
        return None
    if _is_internal(host) or not host:
        return None
    provider, iface = _provider_for_host(host)
    if provider == 'eastmoney' and iface is None:
        iface = _em_path_interface(p.path)
    return f'{provider}:{iface}'


# ========== 计数核心 ==========
def record(name: str, n: int = 1, ok: bool = True, fail: bool = False) -> None:
    """记一次调用（或 n 次）。fail=True 表示该次失败（HTTP>=400 或异常）。"""
    global _counters_date
    now = time.time()
    today = datetime.now().strftime('%Y-%m-%d')
    with _lock:
        if today != _counters_date:
            # 跨天：先把旧天数落库，再开新天（落库在锁外做，避免持锁打 DB）
            old_rows = None
            if _counters:
                d0 = _counters_date
                old_rows = [(d0, *k.split(':', 1), c['count'], c['ok'], c['fail'])
                            for k, c in _counters.items()]
            _counters.clear()
            _counters_date = today
            if old_rows:
                _persist(old_rows)
        c = _counters.get(name)
        if c is None:
            c = {'count': 0, 'ok': 0, 'fail': 0, 'first_ts': now, 'last_ts': now}
        c['count'] += n
        if fail:
            c['fail'] += n
        else:
            c['ok'] += n
        c['last_ts'] = now
        _counters[name] = c


def _persist(rows) -> None:
    if not rows:
        return
    try:
        from db.database import save_api_usage_daily
        save_api_usage_daily(rows)
    except Exception as e:
        print(f'[接口用量] 落库失败: {e}')


def flush() -> int:
    """把当前累计写入 api_usage_daily（幂等 upsert，可每 60s 调）。不重置计数器。"""
    with _lock:
        date = _counters_date
        rows = [(date, *k.split(':', 1), c['count'], c['ok'], c['fail'])
                for k, c in _counters.items()]
    if not rows:
        return 0
    _persist(rows)
    return len(rows)


# ========== 读取：今日统计 / 历史趋势 / 上限 ==========
def get_stats() -> list:
    """返回每个接口今日用量；按调用量降序。"""
    keys = set(DEFAULT_LIMITS) | set(_limits) | set(_counters)
    items = []
    with _lock:
        for key in keys:
            provider, interface = key.split(':', 1)
            c = _counters.get(key, {})
            count = c.get('count', 0)
            ok = c.get('ok', 0)
            fail = c.get('fail', 0)
            last_ts = c.get('last_ts', 0)
            limit = _limits.get(key, DEFAULT_LIMITS.get(key))
            pct = round(count / limit * 100, 1) if limit else None
            alert = bool(limit and pct is not None and pct >= ALERT_THRESHOLD * 100)
            label = f"{PROVIDER_LABEL.get(provider, provider)}·{INTERFACE_LABEL.get(interface, interface)}"
            items.append({
                'key': key, 'provider': provider, 'interface': interface, 'label': label,
                'count': count, 'ok': ok, 'fail': fail, 'limit': limit,
                'pct': pct, 'alert': alert,
                'over': bool(limit and count >= limit),
                'lastTs': int(last_ts * 1000) if last_ts else 0,
            })
    items.sort(key=lambda x: (-x['count'], x['key']))
    return items


def get_history(days: int = 14) -> dict:
    """近 N 天每日用量矩阵，供前端画趋势 sparkline。

    返回 {dates:[...], series:{key:[count...]}, okSeries:{...}, failSeries:{...}}，
    三个 series 都按 dates 对齐（缺天补 0）。
    """
    from db.database import load_api_usage_history
    try:
        rows = load_api_usage_history(days) or []
    except Exception as e:
        print(f'[接口用量] 读历史失败: {e}')
        rows = []
    dates = sorted({r['date'] for r in rows})
    s_map, o_map, f_map = {}, {}, {}
    for r in rows:
        key = f"{r['provider']}:{r['interface']}"
        s_map.setdefault(key, {})[r['date']] = r['count']
        o_map.setdefault(key, {})[r['date']] = r['ok']
        f_map.setdefault(key, {})[r['date']] = r['fail']
    out_s, out_o, out_f = {}, {}, {}
    for key, dmap in s_map.items():
        out_s[key] = [dmap.get(d, 0) for d in dates]
        out_o[key] = [o_map[key].get(d, 0) for d in dates]
        out_f[key] = [f_map[key].get(d, 0) for d in dates]
    return {'dates': dates, 'series': out_s, 'okSeries': out_o, 'failSeries': out_f}


def set_limit(key: str, limit: int, updated_by: str = '') -> None:
    provider, interface = key.split(':', 1)
    limit = int(limit)
    _limits[key] = limit
    try:
        from db.database import save_api_usage_limit
        save_api_usage_limit(provider, interface, limit, updated_by)
    except Exception as e:
        print(f'[接口用量] 保存上限失败: {e}')


def _load_limits_into_mem() -> None:
    _limits.clear()
    try:
        from db.database import load_api_usage_limits
        for r in (load_api_usage_limits() or []):
            key = f"{r['provider']}:{r['interface']}"
            try:
                _limits[key] = int(r['daily_limit'])
            except (TypeError, ValueError):
                pass
    except Exception as e:
        print(f'[接口用量] 读取上限失败（用默认值）: {e}')


def _load_today_into_mem() -> None:
    global _counters_date
    today = datetime.now().strftime('%Y-%m-%d')
    _counters_date = today
    _counters.clear()
    try:
        from db.database import load_api_usage_today
        for r in (load_api_usage_today(today) or []):
            key = f"{r['provider']}:{r['interface']}"
            _counters[key] = {'count': r['count'], 'ok': r['ok'], 'fail': r['fail'],
                              'first_ts': 0, 'last_ts': 0}
    except Exception as e:
        print(f'[接口用量] 读取今日计数失败（从 0 开始）: {e}')


# ========== 安装：monkeypatch 标准 HTTP 库 ==========
def _patch_requests():
    import requests
    if getattr(requests.Session.request, '_api_usage_patched', False):
        return
    orig = requests.Session.request

    def _wrap(self, method, url, *a, **kw):
        name = classify(url)
        if name is None:
            return orig(self, method, url, *a, **kw)
        try:
            resp = orig(self, method, url, *a, **kw)
            ok = getattr(resp, 'status_code', 200) < 400
            record(name, 1, ok=ok, fail=not ok)
            return resp
        except Exception:
            record(name, 1, fail=True)
            raise

    _wrap._api_usage_patched = True
    requests.Session.request = _wrap


def _patch_httpx():
    try:
        import httpx
    except ImportError:
        return
    if getattr(httpx.Client.send, '_api_usage_patched', False):
        return

    orig_send = httpx.Client.send
    orig_asend = getattr(httpx.AsyncClient, 'send', None)

    def _sync(self, request, *a, **kw):
        name = classify(request.url)
        if name is None:
            return orig_send(self, request, *a, **kw)
        try:
            resp = orig_send(self, request, *a, **kw)
            ok = getattr(resp, 'status_code', 200) < 400
            record(name, 1, ok=ok, fail=not ok)
            return resp
        except Exception:
            record(name, 1, fail=True)
            raise

    async def _async(self, request, *a, **kw):
        name = classify(request.url)
        if name is None:
            return await orig_asend(self, request, *a, **kw)
        try:
            resp = await orig_asend(self, request, *a, **kw)
            ok = getattr(resp, 'status_code', 200) < 400
            record(name, 1, ok=ok, fail=not ok)
            return resp
        except Exception:
            record(name, 1, fail=True)
            raise

    _sync._api_usage_patched = True
    _async._api_usage_patched = True
    httpx.Client.send = _sync
    if orig_asend is not None:
        httpx.AsyncClient.send = _async


def _patch_urllib():
    import urllib.request
    if getattr(urllib.request.urlopen, '_api_usage_patched', False):
        return
    orig = urllib.request.urlopen

    def _wrap(url, *a, **kw):
        name = classify(url)
        if name is None:
            return orig(url, *a, **kw)
        try:
            resp = orig(url, *a, **kw)
            record(name, 1, ok=True)
            return resp
        except Exception:
            record(name, 1, fail=True)
            raise

    _wrap._api_usage_patched = True
    urllib.request.urlopen = _wrap


def install() -> None:
    """在进程启动早期调用一次：加载持久化上限/今日计数，并接管所有出站 HTTP。"""
    global _installed
    if _installed:
        return
    _installed = True
    _load_limits_into_mem()
    _load_today_into_mem()
    try:
        _patch_requests()
    except Exception as e:
        print(f'[接口用量] patch requests 失败: {e}')
    try:
        _patch_httpx()
    except Exception as e:
        print(f'[接口用量] patch httpx 失败: {e}')
    try:
        _patch_urllib()
    except Exception as e:
        print(f'[接口用量] patch urllib 失败: {e}')
    print('[接口用量] 统计已安装（自动统计 requests/httpx/urllib 出站调用）')
