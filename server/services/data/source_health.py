"""source_health.py - 通用数据源健康台账（2026-10-19）

东财已有 eastmoney_guard（熔断/冷却），但腾讯、新浪这些行情源没有统一台账：
出问题时页面只会转圈或报错，看不出是哪条链路挂了、从什么时候开始挂的。
本模块只记账、不做决策，调用方在每次真实网络请求后记 ok/fail，
运维接口 /api/data-sources/health 读出。

设计：
  · 线程安全：行情调用在 asyncio.to_thread 的工作线程里执行，更新用 threading.Lock；
  · 只在「首次失败 / 达到 down 阈值 / 恢复」时打印，避免 5 秒轮询把日志刷爆；
  · state：ok=正常、degraded=连续失败但未达阈值、down=连续失败 >= 3、unknown=还没调过。
"""
import threading
import time
from typing import Dict, Optional

_lock = threading.Lock()
_state: Dict[str, Dict] = {}

_DOWN_STREAK = 3          # 连续失败 3 次视为 down（与东财 flaky 阈值一致）
_LOG_AT = {1, _DOWN_STREAK}  # 只在首次失败/达到 down 阈值时打印


def _blank() -> Dict:
    return {
        'ok': 0, 'fail': 0, 'streak': 0,
        'lastOkTs': 0.0, 'lastErrorTs': 0.0,
        'lastError': '', 'latencyMs': 0.0,
    }


def ok(name: str, latency_ms: Optional[float] = None) -> None:
    """一次成功：清零连续失败，记录延迟。"""
    with _lock:
        st = _state.setdefault(name, _blank())
        was_down = st['streak'] >= _DOWN_STREAK
        st['ok'] += 1
        st['streak'] = 0
        st['lastOkTs'] = time.time()
        st['lastError'] = ''
        st['lastErrorTs'] = 0.0
        if latency_ms is not None:
            st['latencyMs'] = round(float(latency_ms), 1)
    if was_down:
        print(f'[数据源] {name} 已恢复')


def fail(name: str, err: object = None) -> None:
    """一次失败：累计连续失败，保留最后一次错误（截断，避免日志过长）。"""
    with _lock:
        st = _state.setdefault(name, _blank())
        st['fail'] += 1
        st['streak'] += 1
        st['lastErrorTs'] = time.time()
        st['lastError'] = (str(err) or repr(err) or '未知错误')[:160]
        streak = st['streak']
        last_error = st['lastError']
    if streak in _LOG_AT:
        print(f'[数据源] {name} 连续失败 {streak} 次：{last_error}')


def status(name: str) -> Dict:
    """单个源的状态快照。"""
    with _lock:
        st = dict(_state.get(name) or _blank())
    streak = st['streak']
    if not st['lastOkTs'] and not st['lastErrorTs']:
        state = 'unknown'
    elif streak == 0:
        state = 'ok'
    elif streak < _DOWN_STREAK:
        state = 'degraded'
    else:
        state = 'down'
    st.update({'name': name, 'state': state, 'downStreakThreshold': _DOWN_STREAK})
    return st


def all_status() -> Dict[str, Dict]:
    """所有登记过的源。"""
    with _lock:
        names = list(_state)
    return {name: status(name) for name in names}


def reset(name: Optional[str] = None) -> None:
    """手动复位（自测/运维用）。"""
    with _lock:
        if name is None:
            _state.clear()
        else:
            _state.pop(name, None)
