"""cache_whitelist_selftest.py - 回测缓存参数白名单 + 行情缓存回收的回归自测（2026-11-07，修④）

守的两条不变量：

1. **三个公开回测接口的缓存槽，不能被远程垃圾参数挤掉前端在用的那一份**。
   `/api/recommend-sim`、`/api/recommend-sim-batch`、`/api/recommend-excess` 都不鉴权，
   键里是自由 int/float（`months`/`capital`/`gap`/`lots`/`bench`）。`swr.put(max_keys=…)` 的
   淘汰按 ts 扔最旧一半，所以垃圾参数清空不了整张表，但会把前端真正在用的那份挤出去，
   代价是一次 7.4 秒冷算（前端默认 10 秒超时）。正解是白名单外一律现算、不入表。
2. **`_realtime_cache` 有界**：键标准化（顺序无关）+ 写入时回收超龄条目 + 条数硬上限。
   原来只判龄不回收，条目永不 pop；键还是调用方的整串代码，同批票换个顺序就是两份。

验收判据按 `docs/后续计划.md` §8.3 第三条：不测"命中快不快"，测
**"一串垃圾参数之后还命中吗"**；并且每条都配反证（把改动撤掉必须报红），
否则绿灯只说明测试没测到东西。全程临时库 + 打桩，零外部请求、不写生产表。
"""
import asyncio
import inspect
import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

_TMP = os.path.join(tempfile.gettempdir(), f'cache_wl_{os.getpid()}.db')
for _suf in ('', '-wal', '-shm'):
    try:
        os.remove(_TMP + _suf)
    except OSError:
        pass

import db.database as dbmod                      # noqa: E402
dbmod.use_db_path(_TMP)   # 换库必须走这个函数：直接赋值 SQLITE_DB_PATH 自 11-05 拆分起静默无效
dbmod.init_db()

from app import config as C                      # noqa: E402
from app.api import routes_state as RS           # noqa: E402
from app.api.routers import recommend as REC     # noqa: E402
from services.data import stock as S             # noqa: E402
from services.support import swr as W            # noqa: E402

PASS, FAIL = [], []


def ck(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(f"  [{'✓' if cond else '✗'}] {msg}")


HERE = os.path.dirname(os.path.abspath(__file__))
MONTHS_OK = C.REC_SIM_CACHE_MONTHS
CAP_OK = C.REC_SIM_CACHE_CAPITAL_DEFAULT
GAP_OK = C.REC_SIM_CACHE_GAP_DEFAULT
LOTS_OK = C.REC_SIM_CACHE_LOTS_DEFAULT

# ========== [0] 白名单判定本身 ==========
print('[0] rec_params_cacheable：只有前端真会发的组合算数')
ck(RS.rec_params_cacheable(3, CAP_OK, GAP_OK, LOTS_OK, 0) is True
   and RS.rec_params_cacheable(6, CAP_OK, GAP_OK, LOTS_OK, 1) is True,
   f'months∈{list(MONTHS_OK)} × bench∈{{0,1}} 且其余取默认 → 可缓存')
ck(RS.rec_params_cacheable(7, CAP_OK, GAP_OK, LOTS_OK, 0) is False,
   'months=7（前端按钮里没有）→ 不可缓存')
ck(RS.rec_params_cacheable(3, 1.0, GAP_OK, LOTS_OK, 0) is False
   and RS.rec_params_cacheable(3, CAP_OK, 0.5, LOTS_OK, 0) is False,
   'capital/gap 被改过 → 不可缓存')
ck(RS.rec_params_cacheable(3, CAP_OK, GAP_OK, False, 0) is False,
   'lots=False（整手开关关掉）→ 不可缓存')
ck(RS.rec_params_cacheable(3, CAP_OK, GAP_OK, LOTS_OK, True) is True,
   'bench 传 bool 也认（True 归一到 1）')
ck(RS.rec_params_cacheable(6) is True and RS.rec_params_cacheable(13) is False,
   'batch/excess 只传 months：None 的参数不参与判定')

# ========== 打桩：让 rec_sim 那几个计算函数变成"数得清的桩" ==========
CALLS = {'sim': [], 'batch': [], 'excess': []}


def _stub_sim(months=3, capital=100000.0, auction_max_gap=5.0, whole_lots=True,
              with_benchmark=False):
    CALLS['sim'].append((months, capital, auction_max_gap, whole_lots, with_benchmark))
    return {'curve': {'months': months}, 'benchmark': {'available': False}}


def _stub_batch(months=3, capital=100000.0, auction_max_gap=5.0):
    CALLS['batch'].append((months, capital, auction_max_gap))
    return {'batch': months}


def _stub_excess(months=6):
    CALLS['excess'].append(months)
    return {'rows': months}


import services.pick.rec_sim as RSIM                 # noqa: E402
import services.pick.rec_sim_batch as RBATCH         # noqa: E402
RSIM.simulate = _stub_sim
RBATCH.simulate_batch = _stub_batch
RSIM.open_hold_excess = _stub_excess


# ========== [1] swr 层：cacheable=False 既不读也不写 ==========
print('[1] swr_get(cacheable=False)：结果照样算出来，但一个字节都不进表')
_store = {}
n = 0


def _c1():
    global n
    n += 1
    return {'v': n}


d1, s1 = W.swr_get(_store, 'k', _c1, ttl=60, stale_max=600, label='t1')
d2, s2 = W.swr_get(_store, 'k', _c1, ttl=60, stale_max=600, label='t1')
ck(s1 == 'cold' and s2 == 'fresh' and len(_store) == 1, f'默认可缓存：{s1}→{s2}，表里 1 条')
d3, s3 = W.swr_get(_store, 'k2', _c1, ttl=60, stale_max=600, label='t1', cacheable=False)
ck(s3 == 'cold' and d3 == {'v': n}, 'cacheable=False 时返回的是真的新算结果（不是空/旧值）')
ck('k2' not in _store and len(_store) == 1, f'cacheable=False 不写表（表里仍 {len(_store)} 条）')
W.swr_get(_store, 'k', _c1, ttl=60, stale_max=600, label='t1', cacheable=False)
ck(n == 3, f'cacheable=False 连表里已有的同键也不读（每次现算，compute 共调用 {n} 次）')

# ========== [2] 真接口：一串垃圾参数之后，前端在用的那份还命中吗 ==========
print('[2] 前端在用的组合 → 灌 30 组垃圾参数 → 再打同一组合（判据来自 §8.3 第三条）')
REC._REC_SIM_CACHE.clear()
REC._EXCESS_CACHE.clear()
CALLS['sim'].clear()
FRONT = dict(months=3, capital=CAP_OK, gap=GAP_OK, lots=LOTS_OK, bench=False)
asyncio.run(REC.get_recommend_sim_api(**FRONT))
r_hit = asyncio.run(REC.get_recommend_sim_api(**FRONT))
ck(r_hit['cached'] is True and len(CALLS['sim']) == 1,
   f'基线：第二次命中缓存（compute 只跑 1 次，实得 {len(CALLS["sim"])}）')
for i in range(30):
    asyncio.run(REC.get_recommend_sim_api(months=1 + i, capital=1000.0 + i, gap=0.1 + i,
                                          lots=(i % 2 == 0), bench=(i % 3 == 0)))
ck(len(REC._REC_SIM_CACHE) == 1,
   f'灌 30 组白名单外参数后，表里仍只有前端那一份（实得 {len(REC._REC_SIM_CACHE)} 条）')
r_after = asyncio.run(REC.get_recommend_sim_api(**FRONT))
ck(r_after['cached'] is True, '垃圾参数之后，前端那一份仍然命中（没被挤掉）')
ck(len(CALLS['sim']) == 31, f'30 组垃圾参数每组都真的现算了（compute 共 {len(CALLS["sim"])} 次）')

# 反证 A：把白名单摘掉（等价于改造前），同一序列必须把前端那份挤出去
print('    [反证 A] 强制 cacheable=True（= 改造前）→ 同一序列必须能挤掉前端那份')
# 注意要改 recommend 模块里的那个名字：它是 `from ..routes_state import *` 拷进来的引用，
# 只改 routes_state 的不会生效（这条本身就是个易错点，写在这儿当样子）。
_saved = REC.rec_params_cacheable
REC.rec_params_cacheable = lambda *a, **k: True
REC._REC_SIM_CACHE.clear()
CALLS['sim'].clear()
asyncio.run(REC.get_recommend_sim_api(**FRONT))
for i in range(30):
    asyncio.run(REC.get_recommend_sim_api(months=1 + i, capital=1000.0 + i, gap=0.1 + i,
                                          lots=(i % 2 == 0), bench=(i % 3 == 0)))
r_evict = asyncio.run(REC.get_recommend_sim_api(**FRONT))
ck(r_evict['cached'] is False,
   '反证成立：没有白名单时，前端那份确实会被垃圾参数挤掉（说明这条测试测到了东西）')
REC.rec_params_cacheable = _saved

# ========== [3] excess 单独一份（被清空一次代价最大：冷算 7.4 秒） ==========
print('[3] /api/recommend-excess：months=7 现算不入表；months=6 命中')
CALLS['excess'].clear()
e6a = asyncio.run(REC.get_recommend_excess_api(months=6))
e6b = asyncio.run(REC.get_recommend_excess_api(months=6))
ck(e6a['cached'] is False and e6b['cached'] is True,
   f'months=6 第一次冷、第二次命中（{e6a["cached"]}/{e6b["cached"]}）')
e7 = asyncio.run(REC.get_recommend_excess_api(months=7))
ck(e7['success'] is True and e7['data']['rows'] == 7, 'months=7 结果照样对（不因为不缓存就报错）')
ck(e7['cached'] is False and list(REC._EXCESS_CACHE) == [(6,)],
   f'months=7 没进表：表内键 {list(REC._EXCESS_CACHE)}')
e6c = asyncio.run(REC.get_recommend_excess_api(months=6))
ck(e6c['cached'] is True and len(CALLS['excess']) == 2,
   f'months=7 之后 months=6 仍命中（compute 共 {len(CALLS["excess"])} 次，应为冷算那 1 次 + 7 的 1 次）')

# ========== [4] 拆档 batch 与主曲线共用一张表：白名单外不占槽 ==========
print('[4] /api/recommend-sim-batch：键形状不同（(\'batch\', …)）但不许牵连主曲线')
REC._REC_SIM_CACHE.clear()
CALLS['sim'].clear()
asyncio.run(REC.get_recommend_sim_api(**FRONT))     # 先把主曲线那份立起来
b3a = asyncio.run(REC.get_recommend_sim_batch_api(months=3))
b3b = asyncio.run(REC.get_recommend_sim_batch_api(months=3))
ck(b3a['cached'] is False and b3b['cached'] is True, 'batch months=3 冷→热')
_before = len(REC._REC_SIM_CACHE)
for i in range(20):
    asyncio.run(REC.get_recommend_sim_batch_api(months=2 + i, capital=7.0 + i))
_after = len(REC._REC_SIM_CACHE)
ck(_before == 2 and _after == 2,
   f'20 组垃圾 batch 一条都没入表（前后 {list(REC._REC_SIM_CACHE)}）')
r_front2 = asyncio.run(REC.get_recommend_sim_api(**FRONT))
ck(r_front2['cached'] is True, 'batch 侧灌垃圾同样挤不掉主曲线那一份（两入口共用一张表）')
ck(3 + 3 + 1 <= 12,
   '白名单总槽位数（sim 6 + batch 3 + 其它 1）≤ max_keys=12 ⇒ 正常流量下淘汰根本不会被触发')

# ========== [5] 漂移守卫：白名单必须跟接口签名/前端档位对齐 ==========
print('[5] 漂移守卫：改了签名或前端档位却没改白名单，这里必须报红')
_sig = inspect.signature(REC.get_recommend_sim_api)
_p = _sig.parameters
ck(float(_p['capital'].default) == float(C.REC_SIM_CACHE_CAPITAL_DEFAULT),
   f"sim 的 capital 默认值 {_p['capital'].default} == REC_SIM_CACHE_CAPITAL_DEFAULT")
ck(float(_p['gap'].default) == float(C.REC_SIM_CACHE_GAP_DEFAULT),
   f"sim 的 gap 默认值 {_p['gap'].default} == REC_SIM_CACHE_GAP_DEFAULT")
ck(bool(_p['lots'].default) == bool(C.REC_SIM_CACHE_LOTS_DEFAULT),
   f"sim 的 lots 默认值 {_p['lots'].default} == REC_SIM_CACHE_LOTS_DEFAULT")
ck(int(_p['months'].default) in C.REC_SIM_CACHE_MONTHS,
   f"sim 的 months 默认值 {_p['months'].default} ∈ {list(C.REC_SIM_CACHE_MONTHS)}")
_sig_b = inspect.signature(REC.get_recommend_sim_batch_api)
ck(float(_sig_b.parameters['capital'].default) == float(C.REC_SIM_CACHE_CAPITAL_DEFAULT)
   and float(_sig_b.parameters['gap'].default) == float(C.REC_SIM_CACHE_GAP_DEFAULT),
   'batch 的 capital/gap 默认值同样对白名单')
_sig_e = inspect.signature(REC.get_recommend_excess_api)
ck(int(_sig_e.parameters['months'].default) in C.REC_SIM_CACHE_MONTHS,
   f"excess 的 months 默认值 {_sig_e.parameters['months'].default} ∈ 白名单")
ck(0 in C.REC_SIM_CACHE_BENCH and 1 in C.REC_SIM_CACHE_BENCH,
   'bench 两档（0/1）都在白名单里')
# 反证 B：白名单常量与签名对不上时，前端在用的组合必须掉出表外
print('    [反证 B] 把 CAPITAL 默认值改一个数 → 前端在用的组合应当立刻不入表')
_saved_cap = RS.REC_SIM_CACHE_CAPITAL_DEFAULT
RS.REC_SIM_CACHE_CAPITAL_DEFAULT = 1.0     # 判定函数读的是自己模块（routes_state）的全局名
REC._REC_SIM_CACHE.clear()
g1 = asyncio.run(REC.get_recommend_sim_api(**FRONT))
g2 = asyncio.run(REC.get_recommend_sim_api(**FRONT))
ck(g1['cached'] is False and g2['cached'] is False and len(REC._REC_SIM_CACHE) == 0,
   '反证成立：常量一漂移，"缓存"这个功能当场消失（所以要靠上面那条守卫拦）')
RS.REC_SIM_CACHE_CAPITAL_DEFAULT = _saved_cap
ck(RS.REC_SIM_CACHE_CAPITAL_DEFAULT == C.REC_SIM_CACHE_CAPITAL_DEFAULT,
   'routes_state 里那份常量与 app.config 同源（star 拷引用，改一边就会漂移）')

# scheduler 的预热键必须也在白名单内，否则预热白跑
_sched_src = open(os.path.join(HERE, '..', 'core', 'scheduler.py'), encoding='utf-8').read()
_prewarm_keys = re.findall(r'key = \((.*?)\)', _sched_src)
ck(len(_prewarm_keys) >= 2, f'从 scheduler 里读到预热用的缓存键：{_prewarm_keys[:3]}')
ck(RS.rec_params_cacheable(3, CAP_OK, GAP_OK, LOTS_OK, 0)
   and RS.rec_params_cacheable(3, CAP_OK, GAP_OK, LOTS_OK, 1),
   '预热键 (3, 100000.0, 5.0, True, bench=0/1) 可缓存')
ck(RS.rec_params_cacheable(3, CAP_OK, GAP_OK), '拆档预热键 (batch, 3, 100000.0, 5.0) 可缓存')
ck('3054' not in _prewarm_keys[0].replace(' ', ''), '预热键仍是一行字面量（改成变量时记得回来核这条）')

# ========== [6] 前端档位对账 ==========
print('[6] TabBacktest.vue：前端会发的 months 必须都在白名单里')
_vue = os.path.join(HERE, '..', '..', 'client-vue', 'src', 'components', 'tabs', 'TabBacktest.vue')
if os.path.exists(_vue):
    _src = open(_vue, encoding='utf-8').read()
    _btn = re.search(r'v-for="m in \[(.*?)\]"', _src)
    _vals = [int(x) for x in _btn.group(1).split(',')] if _btn else []
    ck(bool(_btn) and all(v in C.REC_SIM_CACHE_MONTHS for v in _vals),
       f'档位按钮 {(_btn.group(1) if _btn else "?")} 全在白名单内')
    _urls = re.findall(r'/api/recommend-(sim|sim-batch|excess)\?[^`\'"]*', _src)
    ck(len(_urls) >= 3, f'找到三条回测 URL：{[u.split("?")[0] for u in _urls]}')
    ck(not any('capital=' in u or 'gap=' in u or 'lots=' in u for u in _urls),
       '前端不发自定义 capital/gap/lots ⇒ 走服务端默认值，白名单不会把它挡在表外')
else:
    ck(False, f'找不到前端文件 {_vue}（对账没做成，别当通过）')

# ========== [7] 行情缓存：键标准化 + 回收 + 上限 ==========
print('[7] _realtime_cache：顺序无关命中、超龄回收、条数上限')
ck(S._realtime_cache_key('sh600001,sz000002, SH600000')
   == S._realtime_cache_key('SH600000,sh600001 ,sz000002'),
   '同一批票换个顺序/大小写/空格 → 同一个键')
ck(S._realtime_cache_key('sh600000,sh600000') == 'sh600000', '重复代码去重')
ck(S._realtime_cache_key('') == '' and S._realtime_cache_key(',,') == '',
   '空请求的键是空串（既不查也不写）')
ck(S._realtime_cache_key('sh600000,bj430047') == 'bj430047,sh600000',
   '非 sh/sz 的代码也参与键（不能只按沪深过滤，否则少一行的数据会被当全量返回）')

S._realtime_cache.clear()
_K_AB = S._realtime_cache_key('sh600000,sz000001')
S._realtime_cache_put(_K_AB, 'A')
ck(S._realtime_cache_get(S._realtime_cache_key('sz000001,sh600000')) == 'A',
   '乱序读能命中（改造前整串做键必 miss）')
ck(S._realtime_cache_get('sz000001,sh600000') is None,
   '读写都只认标准化键：整串直接塞进去不认（契约见 _realtime_cache_put 的 docstring）')
ck(S._realtime_cache_get('') is None, '空键读不到东西')
S._realtime_cache_put('', 'B')
ck(len(S._realtime_cache) == 1, f'空键不写表（实得 {len(S._realtime_cache)} 条）')
S._realtime_cache['ancient'] = (time.time() - 10_000, 'OLD')
S._realtime_cache_put('sh600002', 'C')
ck('ancient' not in S._realtime_cache,
   f'写入时把超龄条目带走（窗口 {C.REALTIME_CACHE_RECLAIM_SEC}s）')
for i in range(C.REALTIME_CACHE_MAX_KEYS + 20):
    S._realtime_cache[f'k{i}'] = (time.time() - 30 - i, 'x')
S._realtime_cache_put('sh600003', 'D')
ck(len(S._realtime_cache) <= C.REALTIME_CACHE_MAX_KEYS,
   f'超上限按最旧淘汰到 {C.REALTIME_CACHE_MAX_KEYS} 条以内（实得 {len(S._realtime_cache)}）')
ck(S._realtime_cache_get('sh600003') == 'D', '被淘汰的是旧条目，新写的还在')


# ========== [8] 真走一遍取数：乱序第二次不再发请求 ==========
print('[8] fetch_with_retry 接上缓存（腾讯打桩，零真实请求）')


class _Resp:
    def __init__(self, text):
        self.content = text.encode('gbk', errors='ignore')

    def raise_for_status(self):
        pass


class _FakeSession:
    def __init__(self):
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        codes = url.split('q=', 1)[1].split(',')
        body = '~'.join(['1', '测试', 'x'] + ['0'] * 50)
        return _Resp(';'.join(f'v_{c}="{body}"' for c in codes) + ';')


S._realtime_cache.clear()
_fake = _FakeSession()
_orig_session = S._REALTIME_SESSION
S._REALTIME_SESSION = _fake
try:
    asyncio.run(S.fetch_with_retry('sh600000,sz000001'))
    asyncio.run(S.fetch_with_retry('sz000001,sh600000'))
    ck(len(_fake.urls) == 1,
       f'乱序第二次直接命中缓存，没再敲腾讯（实得请求 {len(_fake.urls)} 次）')
    ck(len(S._realtime_cache) == 1,
       f'表里只有一份（改造前整串做键会是两份，实得 {len(S._realtime_cache)}）')
    asyncio.run(S.fetch_with_retry('sh600000'))
    ck(len(_fake.urls) == 2 and len(S._realtime_cache) == 2,
       '单码与整批是不同集合：该发的还是要发（归一化没有把不同请求糊成同一个）')
finally:
    S._REALTIME_SESSION = _orig_session

# ========== [9] 接线静态核对 ==========
print('[9] 接线：三处 swr_get 都带上 cacheable，且热扫描用短键')
_r_src = open(os.path.join(HERE, '..', 'app', 'api', 'routers', 'recommend.py'),
              encoding='utf-8').read()
ck(_r_src.count('cacheable=rec_params_cacheable(') == 3,
   f'recommend.py 三处 swr_get 都接了白名单（实得 {_r_src.count("cacheable=rec_params_cacheable(")} 处）')
ck('cacheable: bool = True' in open(os.path.join(HERE, '..', 'services', 'support', 'swr.py'),
                                    encoding='utf-8').read(),
   'swr_get 有 cacheable 形参（默认 True，没接的调用点行为不变）')
_s_src = open(os.path.join(HERE, '..', 'app', 'api', 'routers', 'scan.py'),
              encoding='utf-8').read()
ck('hot_scan_key(' in _s_src and 'code_key(' not in _s_src.split('hot_scan_key')[0][-200:],
   '热扫描走标准化短键（§8.7 第 1 名，已收口）')
_n_calls = (_r_src.count('swr_get(_REC_SIM_CACHE') + _r_src.count('swr_get(_EXCESS_CACHE'))
ck(_r_src.count('cacheable=rec_params_cacheable(') == _n_calls,
   f'每一处 swr_get(缓存表 …) 都带白名单：{ _n_calls} 处调用 / '
   f'{_r_src.count("cacheable=rec_params_cacheable(")} 处接线')

print(f'\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败')
if FAIL:
    for m in FAIL:
        print('  失败：', m)
    sys.exit(1)
print('临时库：', _TMP, '（全程零外部请求、不写生产表）')
