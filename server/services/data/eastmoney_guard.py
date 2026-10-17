"""eastmoney_guard.py - 东财 API 共享熔断器（2026-10-17）

背景：东财 push2/push2delay/datacenter 的 API 路径会做 IP 级封锁（403 / 连接重置），
一锁数小时。此前只有资金流向模块有自己的熔断（fundflow._CLIST_*），而情绪温度计、
AI 诊断的热点板块、龙虎榜、估值分位等热路径各自带 2 次重试 × 6~10 秒超时硬打，
封锁期间每个冷请求都要白等 2~10 秒 —— 行动台（默认首页）实测 9.2 秒就是这么来的。

本模块把熔断状态收敛成一处，所有东财调用方共用：
  · 连续失败达阈值即进入冷却窗口，期间 `skip()` 为真，调用方直接走兜底/缓存；
  · 一次成功即复位（解封后首轮恢复，不需要等冷却结束）；
  · 按 scope 分开计数（clist / datacenter / 其他），互不影响。

线程安全：调用方有的在事件循环里、有的在 asyncio.to_thread 的工作线程里，
状态更新用 threading.Lock 保护（持有时间极短，不会成为新瓶颈）。
"""
import threading
import time

_FAIL_THRESHOLD = 3          # 连续失败 3 次即熔断
_COOLDOWN_SEC = 300          # 冷却 5 分钟

_lock = threading.Lock()
_state: dict = {}            # scope -> {'streak': int, 'until': float}


def skip(scope: str = 'default') -> bool:
    """冷却中？为真时调用方应跳过东财、直接走兜底数据。"""
    with _lock:
        st = _state.get(scope)
        if not st:
            return False
        return time.time() < st['until']


def ok(scope: str = 'default') -> None:
    """一次成功：连续失败清零、冷却解除。"""
    with _lock:
        _state.pop(scope, None)


def fail(scope: str = 'default') -> None:
    """一次失败：累计连续失败，达阈值进入冷却。"""
    with _lock:
        st = _state.setdefault(scope, {'streak': 0, 'until': 0.0})
        st['streak'] += 1
        now = time.time()
        if st['streak'] >= _FAIL_THRESHOLD and now >= st['until']:
            st['until'] = now + _COOLDOWN_SEC
            print(f'[东财熔断] {scope} 连续失败 {st["streak"]} 次，进入冷却 '
                  f'{_COOLDOWN_SEC}s（期间各模块走兜底，不再请求东财）')


def reset(scope: str = 'default') -> None:
    """手动复位（自测/管理用）。"""
    with _lock:
        _state.pop(scope, None)


def status(scope: str = 'default') -> dict:
    with _lock:
        st = _state.get(scope) or {'streak': 0, 'until': 0.0}
        return {'scope': scope, 'streak': st['streak'],
                'cooling': time.time() < st['until'],
                'cooldownRemainSec': max(0, int(st['until'] - time.time()))}
