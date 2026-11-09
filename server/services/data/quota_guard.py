"""quota_guard.py — 第三方接口「每日预算」主动治理（2026-11-09）

背景：2026-11-09 用量页发现 `web.ifzq.gtimg.cn`（腾讯 qfq 日K 的一个子域）在生产机被
腾讯按「并发突发 / 当日访问量」限流，单日失败率冲到 50%~72%，而同接口的 `ifzq.gtimg.cn`
同日 3479 次 0 失败。根因不是「封 IP」（那是 eastmoney_guard 管的），而是「这个子域当天
被打得太狠」。当时已把 qfq 回填切到健康的 ifzq（hot_chains._QFQ_URL），但这只是「事后换一个」，
没有机制保证以后 ifzq 自己也不会被打爆。

本模块与 `eastmoney_guard`（失败率熔断，事后 reactive）互补，做**事前 proactive** 的预算治理：
  · 给每个需要治理的「主机」设一个**每日调用预算**（DEFAULT_QUOTA）；
  · 调用方拼 URL 前调 `pick_host('tencent_kline_qfq')`，返回此刻该用的主机——
    首选主机用量 < 90% 预算 → 首选；否则用第一个还没到 90% 的备源；
    都到 90% → 返回首选（降级，但至少不主动超额；api_usage 仍会在 100% 预警）。
  · 计数由 `api_usage` 的 HTTP 接管在每次出站调用后 `bump(host)`（只统计登记过的主机，
    其它主机零开销），所以**任何**打到该主机的请求（不论哪个调用方）都计入预算。
  · 跨天清零、计数落盘持久化（重启不丢当天进度，避免重启后从头猛打）。

设计要点：
  · 预算按「主机」计——腾讯/东财的限流是按 host/IP 不是按 path，同主机不同 path 共享预算。
    比如腾讯日K（`/appstock/app/fqkline/get`）与分钟线（`/appstock/app/minute/query`）都走
    ifzq.gtimg.cn，它们共享 ifzq 的预算，这样预算才反映「这个主机当天被打了多少」。
  · 熔断(eastmoney_guard)管「失败」，预算(本模块)管「用量」——两条独立防线，互不替代。
  · 90% 阈值只是「切备源」信号，不是「禁止调用」；真到 100% 仍由 api_usage 的超限标记预警。
  · 接入新源只需在 SOURCE_HOSTS / DEFAULT_QUOTA 加一行；调用方改一行 `pick_host(...)` 即可。
"""
import os
import json
import threading
import time
from datetime import datetime

# ========== 逻辑源 → 主机优先级（[主, 备1, 备2...]） ==========
# 调用方只说逻辑源名，不直接写死主机；切源/加备源只改这里。
SOURCE_HOSTS = {
    'tencent_kline_qfq': ['ifzq.gtimg.cn', 'web.ifzq.gtimg.cn'],
}

# ========== 每主机每日预算上限（可调；低于 api_usage 的 interface 上限，留 headroom） ==========
# 取值依据：ifzq 单日 qfq 回填实测最高 ~3479 次（11-09），给 8000 留约 2.3 倍余量；
# web.ifzq 作为备源平时接近 0，给同等预算即可。
DEFAULT_QUOTA = {
    'ifzq.gtimg.cn': 8000,
    'web.ifzq.gtimg.cn': 8000,
}

# 达到预算的这个比例就切备源（用户要求：快到阈值 90% 时及时切换）
FAILOVER_THRESHOLD = 0.9

# 未登记主机用超大值：永不触发切源（只统计，不治理）
_GLOBAL_DEFAULT_QUOTA = 10 ** 9

# 落盘节流：最多每这么久写一次（bump 是每次出站调用都会进的，不能每次都写盘）
_PERSIST_MIN_SEC = 10

_lock = threading.Lock()
_counters: dict = {}            # host -> 当日累计调用次数
_date: str = ''
_last_persist = 0.0

_STATE_FILE = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', '..', 'data', 'quota_guard_state.json'))

# 所有需要治理的主机（SOURCE_HOSTS 里出现过的），bump 只对它们计数
_GOVERNED = set()
for _hs in SOURCE_HOSTS.values():
    _GOVERNED.update(_hs)


def _today() -> str:
    return datetime.now().strftime('%Y-%m-%d')


def _load() -> None:
    """进程启动时恢复当日计数（同日期才恢复，否则清零从今天重算）。"""
    global _counters, _date
    _date = _today()
    _counters = {}
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE, encoding='utf-8') as f:
                doc = json.load(f)
            if doc.get('date') == _date:
                _counters = {k: int(v) for k, v in (doc.get('counters') or {}).items()}
    except Exception:
        pass


def _persist() -> None:
    global _last_persist
    try:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        tmp = _STATE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'date': _date, 'counters': _counters}, f, ensure_ascii=False)
        os.replace(tmp, _STATE_FILE)
        _last_persist = time.time()
    except Exception:
        pass


_load()


def bump(host: str, n: int = 1) -> None:
    """记一次对 host 的调用（由 api_usage 的 HTTP 接管调用；只统计登记过的主机）。

    在请求**之前**调用也行、之后也行——这里只累加次数，不关心成败。
    调用方可能因为熔断/失败重试而多次打同一主机，每次都记，预算才真实。
    """
    if host not in _GOVERNED:
        return
    with _lock:
        global _counters, _date
        if _today() != _date:
            _counters = {}
            _date = _today()
        _counters[host] = _counters.get(host, 0) + n
        if time.time() - _last_persist > _PERSIST_MIN_SEC:
            _persist()


def _usage(host: str) -> dict:
    quota = DEFAULT_QUOTA.get(host, _GLOBAL_DEFAULT_QUOTA)
    cnt = _counters.get(host, 0)
    pct = (cnt / quota) if quota else 0.0
    return {'host': host, 'count': cnt, 'quota': quota,
            'pct': round(pct * 100, 1), 'hot': pct >= FAILOVER_THRESHOLD}


def usage(host: str) -> dict:
    """单个主机的当前用量（count/quota/pct/hot）。"""
    with _lock:
        return _usage(host)


def pick_host(source: str) -> str:
    """返回该逻辑源此刻该用的主机：首选 → 备源 →（都热）首选降级。

    调用方拿到返回值拼 URL 即可；返回空串说明该逻辑源未登记（调用方应回退到自己的默认主机）。
    """
    hosts = SOURCE_HOSTS.get(source)
    if not hosts:
        return ''
    with _lock:
        for h in hosts:
            if not _usage(h)['hot']:
                return h
    return hosts[0]


def flush() -> None:
    """把当前计数落盘（幂等；可由 api_usage 的定时 flush 顺带调用）。"""
    with _lock:
        _persist()


def reset(host: str = None) -> None:
    """测试/运维用：清空计数。host=None 清空全部。"""
    with _lock:
        if host is None:
            _counters.clear()
        else:
            _counters.pop(host, None)
        _persist()


def status() -> dict:
    """给诊断接口/日志看的实时状态：每个逻辑源下各主机的用量与是否过热。"""
    with _lock:
        return {src: [_usage(h) for h in hosts] for src, hosts in SOURCE_HOSTS.items()}
