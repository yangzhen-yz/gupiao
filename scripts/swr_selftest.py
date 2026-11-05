"""services.support.swr 语义自测（零外部请求、零 DB）。

覆盖：fresh 不算 / stale 立即返旧值且后台刷新 / cold 同步算 / 超 stale_max 重算 /
gap 节流 / 刷新失败保留旧值 / max_keys 淘汰 / async 版三种状态 /
**写后失效 ABA 竞态**（2026-11-04 线上复查发现的真 bug）。
"""
import asyncio
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
from services.support import swr  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(f'{"OK  " if cond else "FAIL"} {name}{(" | " + detail) if detail else ""}')


def test_sync():
    store, calls = {}, []

    def compute():
        calls.append(1)
        return {'v': len(calls)}

    d, s = swr.swr_get(store, 'k', compute, ttl=0.20, stale_max=10,
                       label='selftest-sync', gap=0)
    check('① 无缓存 → cold 同步算', s == 'cold' and d == {'v': 1} and len(calls) == 1)

    d, s = swr.swr_get(store, 'k', compute, ttl=0.20, stale_max=10,
                       label='selftest-sync', gap=0)
    check('② TTL 内 → fresh 且不再算', s == 'fresh' and d == {'v': 1} and len(calls) == 1)

    time.sleep(0.22)
    t0 = time.time()
    d, s = swr.swr_get(store, 'k', compute, ttl=0.20, stale_max=10,
                       label='selftest-sync', gap=0)
    elapsed = time.time() - t0
    check('③ 过期 → stale 立即返旧值', s == 'stale' and d == {'v': 1} and elapsed < 0.05,
          f'{elapsed * 1000:.1f}ms')
    for _ in range(50):
        if len(calls) >= 2:
            break
        time.sleep(0.01)
    check('④ stale 后台刷新确实算了一次', len(calls) == 2, f'calls={len(calls)}')
    d, s = swr.swr_get(store, 'k', compute, ttl=0.20, stale_max=10,
                       label='selftest-sync', gap=0)
    check('⑤ 刷新后回到 fresh 且拿到新值', s == 'fresh' and d == {'v': 2})

    # 超 stale_max → 重新 cold
    swr.put(store, 'old', {'v': 99})
    store['old']['ts'] = time.time() - 20
    d, s = swr.swr_get(store, 'old', compute, ttl=0.2, stale_max=10,
                       label='selftest-old', gap=0)
    check('⑥ 超 stale_max → cold 重算', s == 'cold' and d == {'v': 3})

    # gap 节流
    s2, n = {}, []
    swr.put(s2, 'g', {'v': 0})
    s2['g']['ts'] = time.time() - 5
    a = swr.try_refresh(s2, 'g', lambda: n.append(1), label='selftest-gap', gap=60)
    b = swr.try_refresh(s2, 'g', lambda: n.append(1), label='selftest-gap', gap=60)
    check('⑦ gap 节流：第二次不起重复线程', a is True and b is False)

    # 刷新失败保留旧值
    s3 = {}
    swr.put(s3, 'e', {'v': 7})
    s3['e']['ts'] = time.time() - 5

    def boom():
        raise RuntimeError('mock upstream down')

    d, s = swr.swr_get(s3, 'e', boom, ttl=0.2, stale_max=10,
                       label='selftest-err', gap=0)
    time.sleep(0.05)
    check('⑧ 后台刷新失败仍保留旧值', d == {'v': 7} and s3['e']['data'] == {'v': 7})

    # max_keys 淘汰
    s4 = {}
    for i in range(10):
        swr.put(s4, i, {'v': i}, max_keys=4)
    check('⑨ max_keys 淘汰不超限', len(s4) <= 4, f'len={len(s4)}')


def test_async():
    async def run():
        store, calls = {}, []

        async def compute():
            calls.append(1)
            await asyncio.sleep(0)
            return {'v': len(calls)}

        d, s = await swr.async_swr_get(store, 'a', compute, ttl=0.2, stale_max=10,
                                       label='selftest-async', gap=0)
        check('⑩ async cold', s == 'cold' and d == {'v': 1})
        d, s = await swr.async_swr_get(store, 'a', compute, ttl=0.2, stale_max=10,
                                       label='selftest-async', gap=0)
        check('⑪ async fresh 不再算', s == 'fresh' and len(calls) == 1)
        await asyncio.sleep(0.22)
        t0 = time.time()
        d, s = await swr.async_swr_get(store, 'a', compute, ttl=0.2, stale_max=10,
                                       label='selftest-async', gap=0)
        check('⑫ async stale 立即返旧值', s == 'stale' and d == {'v': 1}
              and time.time() - t0 < 0.05)
        await asyncio.sleep(0.05)
        check('⑬ async 后台刷新写回新值', len(calls) == 2)

    asyncio.run(run())


def test_invalidation():
    """写后失效必须让「失效前启动」的后台刷新结果作废，否则刚写的新数据被旧值覆盖。"""
    store = {}
    started, release = threading.Event(), threading.Event()
    db = {'v': 'OLD'}

    def compute():
        snapshot = db['v']           # 真实场景：查库已返回，后面还有序列化/压缩
        started.set()
        release.wait(5)
        return snapshot

    swr.put(store, 'all', 'OLD')
    store['all']['ts'] -= 999
    d, s = swr.swr_get(store, 'all', compute, ttl=1, stale_max=10000,
                       label='selftest-aba', gap=0)
    check('⑭ 过期 → stale 且后台刷新已启动', s == 'stale' and d == 'OLD')
    started.wait(2)

    db['v'] = 'NEW'                  # 业务侧写完库
    swr.invalidate(store)            # 写后失效（旧实现是 clear()，此处会复现 bug）
    release.set()
    time.sleep(0.15)
    check('⑮ 失效后旧快照不得写回缓存', 'all' not in store, f'store_keys={list(store)}')

    d, s = swr.swr_get(store, 'all', lambda: db['v'], ttl=300, stale_max=10000,
                       label='selftest-aba2', gap=0)
    check('⑯ 用户再开页看到的是新值', d == 'NEW' and s == 'cold', f'data={d} state={s}')

    # 按 key 局部失效：不能误伤同表里的其他键
    s2 = {}
    swr.put(s2, ('scan-pool', 1), 'P1')
    swr.put(s2, ('market-index',), 'M1')
    swr.invalidate(s2, keys=[('scan-pool', 1)])
    check('⑰ 局部失效只删目标键', ('scan-pool', 1) not in s2
          and s2.get(('market-index',), {}).get('data') == 'M1')

    # max_keys 淘汰必须排除代数表（它是 int/dict 之外的元数据，没有 ts）
    s3 = {}
    swr.invalidate(s3)
    for i in range(10):
        swr.put(s3, i, {'v': i}, max_keys=4)
    check('⑱ 代数表不参与 max_keys 淘汰且不被误删',
          len([k for k in s3 if k != swr._GEN_KEY]) <= 4
          and isinstance(s3.get(swr._GEN_KEY), dict), f'len={len(s3)}')

    # async 版同样要挡住旧快照
    async def run_async_aba():
        st, entered, go = {}, threading.Event(), threading.Event()
        cur = {'v': 'OLD'}

        async def ac():
            snap = cur['v']
            entered.set()
            while not go.is_set():
                await asyncio.sleep(0.005)
            return snap

        swr.put(st, 'k', 'OLD')
        st['k']['ts'] -= 999
        d, s = await swr.async_swr_get(st, 'k', ac, ttl=1, stale_max=10000,
                                       label='selftest-async-aba', gap=0)
        check('⑲ async 过期 → stale', s == 'stale' and d == 'OLD')
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        cur['v'] = 'NEW'
        swr.invalidate(st)
        go.set()
        await asyncio.sleep(0.05)
        check('⑳ async 失效后旧快照不得写回', 'k' not in st, f'store_keys={list(st)}')

    asyncio.run(run_async_aba())


if __name__ == '__main__':
    test_sync()
    test_async()
    test_invalidation()
    print(f'\n=== {len(PASS)} passed / {len(FAIL)} failed ===')
    if FAIL:
        print('FAILED:', ', '.join(FAIL))
        sys.exit(1)
