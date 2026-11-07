"""通用 stale-while-revalidate 缓存（2026-11-04）。

背景（全站巡检实测）：多个重计算接口都是「进程内 TTL 缓存，过期即同步重算」。
用户随时可能撞上冷算：`/api/recommend-excess` 冷算 7.4s（并发时 23.5s）、
`/api/hot-stocks-scan` 冷算超前端 10s 超时导致「暂无扫描结果」、智能推荐页四个
接口首访 7.8~10s。冷算又占满 asyncio 默认线程池，连 28KB 的 TabTrend.js 都排队
14.2s。启动预热只顶得住重启后的几分钟，TTL 一过问题照旧。

口径：**日级/回测级数据的正确性是「最终一致」而不是「请求瞬间一致」**。
过期时立即返回上一次结果（stale），重算放后台线程；只有完全无缓存、或旧值
超过 stale_max 才同步算一次（这两种情况由启动预热与定时保活提前消掉）。
实时行情、持仓、下单等强一致数据不适用本模块。

用法::

    from services.support.swr import swr_get
    data, state = swr_get(_MY_CACHE, key, lambda: heavy_sync_compute(x),
                          ttl=600, stale_max=86400, label='my-cache', gap=120)

state ∈ {'fresh', 'stale', 'cold'}，可直接塞进响应的 ``stale`` 字段做诚实标注。
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional, Tuple

# 全局刷新登记表：同一 (label,key) 同时只跑一个刷新线程，且两次尝试至少间隔 gap 秒
_REG_LOCK = threading.Lock()
_RUNNING: Dict[str, float] = {}
_LAST_ATTEMPT: Dict[str, float] = {}

# ---- 写后失效代数（2026-11-04 复查补）----------------------------------
# 仅 store.clear() 不够：后台刷新线程可能已经拿到**旧数据快照**，正在做序列化 /
# 压缩（收评是 518KB JSON + gzip），此时业务侧写完库并 clear()，刷新线程随后
# put() 回来会把刚写入的新值覆盖成旧值，而且 ts 是全新的 —— 接下来整个 TTL 内
# 用户都看不到新数据。典型 ABA。故给每个 key 记一个失效代数：刷新启动时取一次，
# 写回前再核对，代数变了就丢弃这份旧快照。
_GEN_KEY = '__swr_gen__'


def generation(store: Dict, key: Any) -> int:
    """读某个 key 的失效代数（纯读，不往空 store 里塞东西）。

    Args:
        store: SWR 缓存字典。
        key: 缓存键。

    Returns:
        当前代数；从未失效过返回 0。
    """
    gens = store.get(_GEN_KEY)
    return gens.get(key, 0) if isinstance(gens, dict) else 0


def invalidate(store: Dict, keys: Optional[Iterable[Any]] = None) -> None:
    """写后失效：删数据并递增代数，让失效前启动的后台刷新结果作废。

    所有「写库后要立刻可见」的调用方都应走这里，不要直接 ``store.clear()`` /
    ``store.pop()``。代数表本身保留，不随数据一起清掉。

    Args:
        store: SWR 缓存字典。
        keys: 只失效这些键；None 表示整表（保留代数表）。

    Returns:
        None
    """
    gens = store.get(_GEN_KEY)
    if not isinstance(gens, dict):
        gens = {}
        store[_GEN_KEY] = gens
    targets = ([k for k in store if k != _GEN_KEY] if keys is None
               else list(keys))
    for k in targets:
        gens[k] = gens.get(k, 0) + 1
        store.pop(k, None)


def _cache_key(label: str, key: Any) -> str:
    return f'{label}:{key!r}'


def put(store: Dict, key: Any, data: Any, max_keys: Optional[int] = None) -> None:
    """原子写入一条缓存。max_keys 超限时按 ts 淘汰最旧一半（不整表清空）。

    计数与淘汰都排除代数表 `_GEN_KEY`：它是 int/dict 之外的元数据，既不能算作
    一条缓存、也没有 ts 字段可排序。
    """
    store[key] = {'ts': time.time(), 'data': data}
    if max_keys:
        items = [(k, v) for k, v in store.items()
                 if k != _GEN_KEY and isinstance(v, dict)]
        if len(items) > max_keys:
            oldest = sorted(items, key=lambda kv: kv[1].get('ts', 0))[: len(items) // 2]
            for k, _ in oldest:
                store.pop(k, None)


def try_refresh(store: Dict, key: Any, compute: Callable[[], Any], *,
                label: str, gap: float = 60.0,
                max_keys: Optional[int] = None,
                on_done: Optional[Callable[[Any], None]] = None) -> bool:
    """尝试起一个后台刷新线程。已在刷新 / 距上次尝试过近时返回 False（不重复打数据源）。

    返回是否真的起了线程。刷新失败保留旧缓存，不影响任何请求。
    """
    rk = _cache_key(label, key)
    now = time.time()
    with _REG_LOCK:
        if rk in _RUNNING or now - _LAST_ATTEMPT.get(rk, 0.0) < gap:
            return False
        _RUNNING[rk] = now
        _LAST_ATTEMPT[rk] = now
    gen = generation(store, key)

    def _run():
        try:
            data = compute()
            # 期间发生过写后失效 → 这份是旧快照，丢弃（否则覆盖刚写入的新值）
            if generation(store, key) != gen:
                return
            put(store, key, data, max_keys)
            if on_done:
                on_done(data)
        except Exception as e:
            print(f'[SWR:{label}] 后台刷新失败（沿用旧缓存）: {type(e).__name__}: {e}')
        finally:
            with _REG_LOCK:
                _RUNNING.pop(rk, None)

    threading.Thread(target=_run, daemon=True, name=f'swr-{label}').start()
    return True


def swr_get(store: Dict, key: Any, compute: Callable[[], Any], *,
            ttl: float, stale_max: float, label: str,
            gap: float = 60.0, max_keys: Optional[int] = None,
            on_done: Optional[Callable[[Any], None]] = None,
            cacheable: bool = True) -> Tuple[Any, str]:
    """取缓存命中或计算新值。

    - ``fresh``：age < ttl，直接返回。
    - ``stale``：ttl ≤ age < stale_max，立即返回旧值 + 后台刷新。
    - ``cold``：无缓存或 age ≥ stale_max，同步 compute（仅启动预热未覆盖时发生）。

    ``cacheable=False``（2026-11-07 加，修④）：**既不读也不写缓存，每次现算**。
    这是给"参数白名单"用的：像 `/api/recommend-excess?months=7` 这种前端根本不会发的组合，
    以前也会占一份缓存位，而 `max_keys` 的淘汰是按时间把**最旧的**扔掉——于是来一串
    垃圾参数就能把前端真正在用的那份挤出去，代价是一次 7.4 秒的冷算。
    白名单外的组合不入表，才真正把"任何人都能远程清空别人的缓存"这条关掉；
    光调大上限或改淘汰策略都不解决（键空间是自由参数时，有多少槽都会被占满）。
    """
    if not cacheable:
        return compute(), 'cold'
    now = time.time()
    hit = store.get(key)
    if isinstance(hit, dict):
        age = now - hit.get('ts', 0)
        if age < ttl:
            return hit['data'], 'fresh'
        if age < stale_max:
            try_refresh(store, key, compute, label=label, gap=gap,
                        max_keys=max_keys, on_done=on_done)
            return hit['data'], 'stale'
    gen = generation(store, key)
    data = compute()
    # 同步重算期间也可能被写后失效抢先；此时结果照常返回（请求本就早于写入完成），
    # 但不写缓存，避免把旧值固化成接下来一个 TTL 的 fresh 数据。
    if generation(store, key) == gen:
        put(store, key, data, max_keys)
        if on_done:
            on_done(data)
    return data, 'cold'


def keepwarm(store: Dict, key: Any, compute: Callable[[], Any], *,
             label: str, ttl: float, gap: float = 60.0,
             max_keys: Optional[int] = None) -> bool:
    """定时保活钩子：只在缓存缺失或已过 ttl 时才尝试后台刷新。

    供 scheduler 的串行闸门调用，保证缓存永远不衰减到需要用户同步付冷算的程度。
    """
    hit = store.get(key)
    if hit and time.time() - hit.get('ts', 0) < ttl:
        return False
    return try_refresh(store, key, compute, label=label, gap=gap, max_keys=max_keys)


# ========== 协程版（async 数据源，如 theme_index / cls_pool / 大盘指数） ==========
# 为什么不用线程版 + asyncio.run：这些服务内部可能持有 asyncio.Lock / 连接等绑定
# 当前 loop 的资源（工程知识库 §2 的「锁不跨 loop」教训）。后台刷新统一用
# create_task 在**同一个 uvicorn 主 loop** 里跑，零跨 loop 风险。
_ASYNC_RUNNING: Dict[str, float] = {}
_ASYNC_LAST_ATTEMPT: Dict[str, float] = {}


async def async_swr_get(store: Dict, key: Any,
                        coro_factory: Callable[[], Awaitable[Any]], *,
                        ttl: float, stale_max: float, label: str,
                        gap: float = 30.0,
                        max_keys: Optional[int] = None) -> Tuple[Any, str]:
    """协程版 SWR。必须在事件循环里 await；返回 (data, state)。

    用于「本来就是 async、单次 1~3 秒、数据分钟级变化」的只读接口：
    过期立即返旧值，刷新作为独立 task 挂在主 loop 上，请求本身不受影响。
    """
    now = time.time()
    hit = store.get(key)
    if isinstance(hit, dict):
        age = now - hit.get('ts', 0)
        if age < ttl:
            return hit['data'], 'fresh'
        if age < stale_max:
            rk = _cache_key(label, key)
            if rk not in _ASYNC_RUNNING and now - _ASYNC_LAST_ATTEMPT.get(rk, 0.0) >= gap:
                _ASYNC_RUNNING[rk] = now
                _ASYNC_LAST_ATTEMPT[rk] = now
                gen = generation(store, key)

                async def _refresh(gen=gen):
                    try:
                        data = await coro_factory()
                        if generation(store, key) != gen:
                            return
                        put(store, key, data, max_keys)
                    except Exception as e:
                        print(f'[SWR:{label}] 后台刷新失败（沿用旧缓存）: {type(e).__name__}: {e}')
                    finally:
                        _ASYNC_RUNNING.pop(rk, None)

                asyncio.get_running_loop().create_task(_refresh())
            return hit['data'], 'stale'
    gen = generation(store, key)
    data = await coro_factory()
    if generation(store, key) == gen:
        put(store, key, data, max_keys)
    return data, 'cold'
