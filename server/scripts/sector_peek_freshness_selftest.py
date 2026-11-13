# -*- coding: utf-8 -*-
"""sector_peek_freshness_selftest.py - 板块资金流「选最新一份」回归自测（2026-11-13）

守的线上不变量：`fundflow.peek_sector_flow()` 必须在 _CACHE 全部候选 + _LAST_GOOD +
DB 快照里选 **ts 最新** 的那份，而不是「遍历到第一个非空就 return」。

为什么这条值得长期守（11-13 实测事故链）：
  _CACHE 按完整参数 key 存多份、ts 各自独立。旧写法先命中先返回，可能拿到几小时前那份，
  而 DB sectors 快照明明 12:11:36／8905 字节含真实行业行。同一个旧 ts 被两处当判据：
    · mainline._sync_score 判 flow_age > 2h → 每 183s 一次全「无主线」；
    · soft_refresh_sector_flow 的 1800s 节流 → 永不成立 → 每 180s 重算新浪 70 页
      （实测 56 次／3 小时，本该 6 次），~18s 重活抢 GIL 把 uvicorn 事件循环饿死：
      /api/health 实测卡到 7.774s，自愈探活 11:49 连续 3 次失败写重启标志。

含反证：第 [12] 组复刻旧写法，证明同一批输入下旧代码**必须选错**——否则本自测没测到要害。
零网络、零真库写入：import 后立刻桩掉 save_/load_fund_flow_snapshot，收尾只读核对真库。

运行：PYTHONIOENCODING=utf-8 python scripts/sector_peek_freshness_selftest.py
"""
import os
import sys
import time
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.data import fundflow as ff

PASS = []
FAIL = []

# ---- 防御性第一件事：立刻桩掉 DB 快照读写（任何测试逻辑之前）----
# 沿用 fundflow_selftest 的教训：自测绝不写真库，收尾只读核对。
_store = {}


def _fake_save(key, data):
    _store[key] = data
    return True


def _fake_load(key):
    return _store.get(key)


ff.save_fund_flow_snapshot = _fake_save
ff.load_fund_flow_snapshot = _fake_load

# 真库 sectors 行的原始 updated_at，收尾只读比对（证明自测没污染）
_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'gupiao.db')
_orig_sectors_stamp = None
try:
    _c = sqlite3.connect('file:' + _DB + '?mode=ro', uri=True, timeout=10)
    _r = _c.execute("SELECT updated_at FROM fund_flow_snapshot WHERE snap_key='sectors'").fetchone()
    _orig_sectors_stamp = _r[0] if _r else None
    _c.close()
except Exception as _e:
    print(f'[提示] 真库只读基线没拿到（不影响本自测，收尾核对跳过）: {type(_e).__name__}: {_e}')


def ck(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(('  [OK] ' if cond else '  [FAIL] ') + name + (('  ' + detail) if detail else ''))


def reset():
    ff._CACHE.clear()
    ff._LAST_GOOD.clear()
    _store.clear()


def sec_row(name='软件和信息技术服务业', chg=0.57):
    """一份最小可辨的板块数据（industry.inflow 非空即算「有货」）。"""
    return {'industry': {'inflow': [{'name': name, 'changePercent': chg, 'mainNetInflow': 1.0}]},
            'concept': {'inflow': []}}


def put_cache(key, ts, rows=None):
    ff._CACHE[key] = {'ts': ts, 'data': {'sectors': rows if rows is not None else sec_row()}}


def old_peek():
    """复刻 2026-11-13 之前的旧写法（先命中先返回），仅用于反证，不上线。"""
    for entry in ff._CACHE.values():
        sec = (entry.get('data') or {}).get('sectors') or {}
        if (sec.get('industry') or {}).get('inflow'):
            return {'rows': sec, 'src': 'cache', 'ts': float(entry.get('ts') or 0)}
    lg = ff._LAST_GOOD.get('sectors')
    if not lg:
        snap = ff.load_fund_flow_snapshot('sectors')
        if snap and isinstance(snap, dict) and 'data' in snap:
            lg = snap
            ff._LAST_GOOD['sectors'] = lg
    if lg and lg.get('data'):
        return {'rows': lg['data'], 'src': 'lastgood', 'ts': float(lg.get('ts') or 0)}
    return {'rows': {}, 'src': 'none', 'ts': 0.0}


T = time.time()
OLD = T - 7200      # 2 小时前：正好越过 mainline 的 2h 判据
NEW = T - 60        # 1 分钟前：新鲜

print('[1] 空候选 → 诚实降级 none')
reset()
r = ff.peek_sector_flow()
ck('1.1 全空返回 src=none', r['src'] == 'none', str(r['src']))
ck('1.2 全空 ts=0.0', r['ts'] == 0.0)
ck('1.3 全空 rows={}', r['rows'] == {})

print('[2] 只有一份 _CACHE → 返回它')
reset()
put_cache('k1', NEW)
r = ff.peek_sector_flow()
ck('2.1 src=cache', r['src'] == 'cache', str(r['src']))
ck('2.2 ts 就是那份的 ts', abs(r['ts'] - NEW) < 1e-6)

print('[3] 核心正例：_CACHE 旧 + _LAST_GOOD 新 → 必须选新的 lastgood')
reset()
put_cache('k_old', OLD)
ff._LAST_GOOD['sectors'] = {'data': sec_row('新数据行业'), 'ts': NEW}
r = ff.peek_sector_flow()
ck('3.1 选 lastgood 而非旧 cache', r['src'] == 'lastgood', str(r['src']))
ck('3.2 ts 是新的', abs(r['ts'] - NEW) < 1e-6)
ck('3.3 rows 是新那份', r['rows']['industry']['inflow'][0]['name'] == '新数据行业')

print('[4] 反向：_CACHE 新 + _LAST_GOOD 旧 → 必须选 cache')
reset()
put_cache('k_new', NEW, sec_row('缓存新行业'))
ff._LAST_GOOD['sectors'] = {'data': sec_row('兜底旧行业'), 'ts': OLD}
r = ff.peek_sector_flow()
ck('4.1 选 cache', r['src'] == 'cache', str(r['src']))
ck('4.2 rows 是缓存新那份', r['rows']['industry']['inflow'][0]['name'] == '缓存新行业')

print('[5] _CACHE 多份不同 ts → 选最新，不是插入序第一个')
reset()
put_cache('a_first_old', OLD, sec_row('插入序第一但旧'))
put_cache('b_second_new', NEW, sec_row('插入序第二但新'))
put_cache('c_third_mid', T - 3600, sec_row('中间'))
r = ff.peek_sector_flow()
ck('5.1 选 ts 最新那份', abs(r['ts'] - NEW) < 1e-6, f"ts={r['ts']:.1f}")
ck('5.2 rows 对应最新那份', r['rows']['industry']['inflow'][0]['name'] == '插入序第二但新')

print('[6] industry.inflow 为空的 entry 必须跳过（即使 ts 最新）')
reset()
put_cache('empty_new', NEW, {'industry': {'inflow': []}, 'concept': {'inflow': []}})
put_cache('has_old', OLD, sec_row('有货但旧'))
r = ff.peek_sector_flow()
ck('6.1 不选空壳', r['rows']['industry']['inflow'][0]['name'] == '有货但旧')
ck('6.2 ts 是有货那份', abs(r['ts'] - OLD) < 1e-6)
reset()
put_cache('only_empty', NEW, {'industry': {'inflow': []}})
r = ff.peek_sector_flow()
ck('6.3 只有空壳 → none', r['src'] == 'none', str(r['src']))

print('[7] _LAST_GOOD 为空 → 从 DB 快照恢复（桩，不碰真库）')
reset()
_store['sectors'] = {'data': sec_row('快照恢复行业'), 'ts': NEW}
r = ff.peek_sector_flow()
ck('7.1 src=lastgood', r['src'] == 'lastgood', str(r['src']))
ck('7.2 rows 来自快照', r['rows']['industry']['inflow'][0]['name'] == '快照恢复行业')
ck('7.3 恢复后写回 _LAST_GOOD', 'sectors' in ff._LAST_GOOD)

print('[8] DB 快照比 _CACHE 新 → 选快照')
reset()
put_cache('k_old', OLD, sec_row('缓存旧'))
_store['sectors'] = {'data': sec_row('快照新'), 'ts': NEW}
r = ff.peek_sector_flow()
ck('8.1 选 lastgood(快照)', r['src'] == 'lastgood', str(r['src']))
ck('8.2 ts 是快照的新 ts', abs(r['ts'] - NEW) < 1e-6)

print('[9] ts 相同 → 保持原优先级（cache 先）')
reset()
put_cache('same', NEW, sec_row('同刻缓存'))
ff._LAST_GOOD['sectors'] = {'data': sec_row('同刻兜底'), 'ts': NEW}
r = ff.peek_sector_flow()
ck('9.1 同 ts 选 cache', r['src'] == 'cache', str(r['src']))

print('[10] 返回结构三键不变（调用方 mainline/soft_refresh 依赖）')
reset()
put_cache('k', NEW)
r = ff.peek_sector_flow()
ck('10.1 恰好 rows/src/ts 三键', set(r.keys()) == {'rows', 'src', 'ts'}, str(sorted(r.keys())))
ck('10.2 ts 是 float', isinstance(r['ts'], float))

print('[11] 节流判据：修复后 1800s 内不再重算（放大器消除）')
reset()
# 修复前的处境：_CACHE 旧 entry + 刚成功写入的新兜底
put_cache('k_old', OLD)
ff._LAST_GOOD['sectors'] = {'data': sec_row(), 'ts': NEW}
pf = ff.peek_sector_flow()
throttled = (pf['src'] != 'none' and time.time() - pf['ts'] < 1800)
ck('11.1 新 ts → 节流成立（本轮 return False，不重算 70 页）', throttled)
reset()
put_cache('k_old', OLD)          # 只有旧 entry、没有新兜底
pf = ff.peek_sector_flow()
throttled_old = (pf['src'] != 'none' and time.time() - pf['ts'] < 1800)
ck('11.2 只有旧 ts → 节流不成立（如实：确实该重算）', not throttled_old)

print('[12] 反证：旧写法在同一批输入下必须选错（证明本自测测到要害）')
reset()
put_cache('k_old', OLD)
ff._LAST_GOOD['sectors'] = {'data': sec_row('新数据行业'), 'ts': NEW}
o = old_peek()
ck('12.1 旧写法在[3]场景选错（拿到旧 cache）', o['src'] == 'cache' and abs(o['ts'] - OLD) < 1e-6,
   f"old src={o['src']}")
reset()
put_cache('a_first_old', OLD, sec_row('插入序第一但旧'))
put_cache('b_second_new', NEW, sec_row('插入序第二但新'))
o = old_peek()
ck('12.2 旧写法在[5]场景选错（拿到插入序第一的旧份）',
   o['rows']['industry']['inflow'][0]['name'] == '插入序第一但旧')
reset()
put_cache('k_old', OLD, sec_row('缓存旧'))
_store['sectors'] = {'data': sec_row('快照新'), 'ts': NEW}
o = old_peek()
ck('12.3 旧写法在[8]场景选错（旧 cache 挡住新快照）', o['src'] == 'cache', f"old src={o['src']}")
reset()
put_cache('k_old', OLD)
ff._LAST_GOOD['sectors'] = {'data': sec_row(), 'ts': NEW}
o = old_peek()
old_throttled = (o['src'] != 'none' and time.time() - o['ts'] < 1800)
ck('12.4 旧写法节流失效（正是 56 次/3h 重试风暴的成因）', not old_throttled)

print('[13] 收尾：真库 sectors 行只读核对（自测零污染）')
if _orig_sectors_stamp is not None:
    try:
        _c = sqlite3.connect('file:' + _DB + '?mode=ro', uri=True, timeout=10)
        _r = _c.execute("SELECT updated_at FROM fund_flow_snapshot WHERE snap_key='sectors'").fetchone()
        _c.close()
        ck('13.1 真库 sectors.updated_at 未被自测改动',
           _r and _r[0] == _orig_sectors_stamp, f'{_orig_sectors_stamp} → {_r[0] if _r else None}')
    except Exception as _e:
        ck('13.1 真库只读核对可执行', False, f'{type(_e).__name__}: {_e}')
else:
    print('  [跳过] 无基线（真库不可读或 sectors 行不存在）')

print()
print(f'结果: {len(PASS)} 通过 / {len(FAIL)} 失败')
if FAIL:
    print('失败项:')
    for f in FAIL:
        print('  - ' + f)
sys.exit(1 if FAIL else 0)