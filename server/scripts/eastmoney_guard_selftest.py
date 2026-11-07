"""eastmoney_guard_selftest.py - 东财熔断"分家族 + 当日停调 + 人工暂停"回归自测（2026-11-06）

守的三条线上不变量（都是 11-06 当天被真实故障逼出来的，见 docs/系统更新日志.md）：

1. **一条链路挂不得连带停全东财**。11-06 09:15:04 push2delay/clist 一次连接被重置就把
   push2ex 三池、datacenter 龙虎榜一起关到 13:16 —— 而实测失败率是 clist 47.5% vs
   pools 1.0% vs datacenter 0.4%（`api_usage_daily` 10-23~11-06）。代价不是"少几个字段"，
   是情绪快照 33 行退潮/冰点全标 stale=1，转弱推送/修复推送/减仓影子整天静音。
2. **blocked 熔断 = 当日停调**（抬到当日 23:59:59，次日自然过期），不再"每 4 小时戳一次"。
   但必须留探测窗口：真解封时（11-06 13:16 那次 ulist 成功）要能在几分钟内回到 fresh，
   否则"停一天"会把一次抖动放大成全天瞎。
3. **人工暂停优先于自动复位**：ok() 不得清掉人工暂停，重启必须从状态文件恢复。
   这条是用户说的"先暂停一段时间，等过一段时间再恢复"能不能落地的唯一判据。

全程不联网、不打真实库、不碰真实状态文件（`_STATE_FILE` 指向临时文件，
`_save_state_locked` 默认掐掉，只在"重启恢复"一节里显式让它写临时文件）。
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.data import eastmoney_guard as g   # noqa: E402

PASS, FAIL = [], []


def ck(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(f"  [{'✓' if cond else '✗'}] {msg}")


CON = ConnectionError('Remote end closed connection without response')
_TMP_STATE = os.path.join(tempfile.gettempdir(), f'emg_selftest_{os.getpid()}.json')
g._STATE_FILE = _TMP_STATE
g._save_state_locked = lambda: None       # 默认不写盘，别污染真实状态文件


def reset():
    g._state.clear()
    g._FAMILIES.clear()
    g._manual.clear()
    g._domain.update({'streak': 0, 'until': 0.0, 'trips': 0, 'probeAt': 0.0})


# ========== [1] 家族隔离：clist 挂不得带走三池/龙虎榜/公告 ==========
print('[1] 主机家族隔离（11-06 故障的直接成因）')
reset()
t0 = time.time()
g.fail('clist', CON, now=t0)
g.fail('clist', CON, now=t0 + 1)          # 家族 main 累计 2 次 → 跳闸
ck(g.skip('clist'), 'main 家族：clist 自己该停')
ck(g._skip_locked('ulist', t0 + 2), '同家族的 ulist 一起停（同域名=同风险，这是对的）')
for scope, name in (('pools', 'push2ex 三池'), ('datacenter', 'datacenter 龙虎榜/基本面'),
                    ('ann', 'np-anotice 公告'), ('appdata', 'emappdata 人气榜')):
    ck(not g._skip_locked(scope, t0 + 2), f'{name} 不受 clist 熔断牵连（反证：老行为会一起停）')
st = g.status('clist')
ck(st['family'] == 'main' and st['familyCooling'] and st['familyStoppedToday'],
   f'状态能分清是"家族当日停调"（family={st["family"]} stoppedToday={st["familyStoppedToday"]}）')

# ========== [2] 当日停调：抬到 23:59:59，次日自然恢复 ==========
print('[2] 当日停调与次日自动恢复')
ck(st['familyRecoverAt'] and st['familyRecoverAt'].endswith('23:59:59'),
   f'until 抬到当日 23:59:59（实得 {st["familyRecoverAt"]}）——不需要任何定时器/cron')
eod = g._eod_ts(t0)
ck(eod > t0 and time.strftime('%H:%M:%S', time.localtime(eod)) == '23:59:59',
   f'_eod_ts 落在当天末尾且不早于当前（实得 {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(eod))}）')
ck(not g._skip_locked('clist', eod + 5), '过了当日 23:59:59 → 次日自动放行（零恢复动作）')

# ========== [3] 探测窗口：停调期间仍能发现"真解封了" ==========
print('[3] 停调期间的探测窗口')
probe = g.EASTMONEY_DAY_STOP_PROBE_SEC
ck(probe > 0, f'默认留探测窗口（{probe}s）；配成 0 才是严格的"当天不再调"')
reset()
g.fail('clist', CON, now=t0)
g.fail('clist', CON, now=t0 + 1)
ck(g._skip_locked('clist', t0 + 60), '窗口未到：停')
ck(not g._skip_locked('clist', t0 + probe + 1), '窗口到点：放行一次探测')
ck(g._skip_locked('clist', t0 + probe + 2), '探测机会已被消耗：又回到停（每窗口只放一次）')
g.ok('ulist')
ck(not g._skip_locked('clist', t0 + probe + 3),
   '探测成功 → ok() 解除当日停调（11-06 13:16 现场：不通才停，通了就用）')
ck(g._skip_locked('clist', t0 + probe + 3) is False, '解除后同家族全部放行')

# 探测又失败：停调继续，但不能把窗口推回"永远不停"
reset()
g.fail('clist', CON, now=t0)
g.fail('clist', CON, now=t0 + 1)
g._skip_locked('clist', t0 + probe + 1)                    # 消耗一次探测
g.fail('clist', CON, now=t0 + probe + 2)                   # 探测仍然失败
ck(g._skip_locked('clist', t0 + probe + 3), '探测失败后继续停')
# 探测失败后本接口还有一层自己的指数退避（1800→3600s），所以下一次放行时刻
# 取"家族探测窗口"与"本接口冷却"里更晚的那个；断言的是**总会再探**，而不是永久停死
ck(not g._skip_locked('clist', max(g._FAMILIES['main']['probeAt'], g._state['clist']['until']) + 1),
   '探测失败后仍会在下一个窗口/退避到期后再探一次（不会永久停死）')

# ========== [4] 人工暂停：优先于一切、不被成功清掉 ==========
print('[4] 人工暂停（用户："先暂停一段时间，等过一段时间再恢复"）')
reset()
res = g.pause(hours=48, reason='自测：东财不让用')
ck(res['paused'] and abs(res['remainHours'] - 48) < 0.01, f'暂停 48h 生效（{res}）')
for scope in ('clist', 'ulist', 'pools', 'datacenter', 'ann', 'appdata'):
    ck(g.skip(scope), f'人工暂停 all 连 {scope}（含 ignore_domain 的边缘节点）也停')
g.ok('clist')
ck(g.skip('clist'), '一次请求成功不得解除人工暂停（人工说了算，与自动冷却分开）')
r2 = g.resume('all')
ck(r2['wasPaused'] and not g.skip('clist'), 'resume 解除后立刻放行')

# 只停一个家族：其它家族照常
reset()
g.pause(hours=6, target='main', reason='自测：只停 push2delay')
ck(g.skip('clist') and g.skip('ulist'), 'main 家族被人工停')
ck(not g.skip('pools') and not g.skip('datacenter'), '其它家族不受人工暂停影响')
ck(not g.skip('appdata', ignore_domain=True),
   'target=main 是精准停：appdata（另一个家族）不受牵连')
g.resume('main')
ck(not g.skip('clist'), '按 target 精准解除')

# ========== [5] 状态文件：重启不失忆 ==========
print('[5] 人工暂停/冷却跨进程恢复')
reset()
import json as _json


def _real_save():
    doc = {'saved_at': time.time(),
           'scopes': {k: dict(v) for k, v in g._state.items()},
           'domain': dict(g._domain),
           'families': {k: dict(v) for k, v in g._FAMILIES.items()},
           'manual': {k: dict(v) for k, v in g._manual.items()}}
    with open(_TMP_STATE, 'w', encoding='utf-8') as f:
        _json.dump(doc, f)


g._save_state_locked = _real_save
g.pause(hours=72, reason='自测：重启不该忘掉')
g.fail('clist', CON, now=time.time())
g.fail('clist', CON, now=time.time())
g._state.clear(); g._FAMILIES.clear(); g._manual.clear()
g._domain.update({'streak': 0, 'until': 0.0, 'trips': 0, 'probeAt': 0.0})
g._load_state()
ck(g.skip('clist') and g.skip('pools'), '重启后人工暂停仍在（72h>状态文件年龄上限也一样）')
ck(g.status('clist')['manualReason'] == '自测：重启不该忘掉',
   f'暂停原因可读回（实得 "{g.status("clist")["manualReason"]}"）')
ck(g._FAMILIES.get('main') is not None, '家族冷却也从磁盘恢复（重启不该从 0 重新累积）')
g.resume('all')
g._save_state_locked = lambda: None
try:
    os.remove(_TMP_STATE)
except OSError:
    pass

# ========== [6] 开关关闭 = 逐字节回到改造前 ==========
print('[6] 回滚等价（关掉开关必须回到 10-19 的老行为）')
reset()
_family_flag, _day_flag = g.EASTMONEY_FAMILY_GATE_ENABLED, g.EASTMONEY_DAY_STOP_ENABLED
try:
    g.EASTMONEY_FAMILY_GATE_ENABLED = False
    g.EASTMONEY_DAY_STOP_ENABLED = False
    g.fail('clist', CON, now=t0)
    g.fail('ulist', CON, now=t0 + 1)
    st = g.status('clist')
    ck(st['domainCooling'] and st['domainRemainSec'] > 0, '老行为：blocked 累计直接开全域闸门')
    ck(g._skip_locked('pools', t0 + 2), '老行为：三池被全域闸门连带停（新行为已不再这样）')
    ck(st['domainRemainSec'] <= 4 * 3600, f'老行为冷却封顶 4h（实得 {st["domainRemainSec"]}s）')
    reset()
    g.EASTMONEY_FAMILY_GATE_ENABLED = True
    g.EASTMONEY_DAY_STOP_ENABLED = False
    g.fail('clist', CON, now=t0)
    g.fail('clist', CON, now=t0 + 1)
    st2 = g.status('clist')
    ck(st2['familyCooling'] and not st2['familyStoppedToday'],
       '分家族但不开当日停调 → 家族指数退避（不是抬到 23:59）')
    ck(st2['familyRemainSec'] <= 4 * 3600, f'退避封顶 4h（实得 {st2["familyRemainSec"]}s）')
finally:
    g.EASTMONEY_FAMILY_GATE_ENABLED = _family_flag
    g.EASTMONEY_DAY_STOP_ENABLED = _day_flag
    reset()

# ========== [7] status 不得消耗探测窗口 ==========
print('[7] 查状态不等于探测一次')
reset()
g.fail('clist', CON, now=t0)
g.fail('clist', CON, now=t0 + 1)
probe_at = g._FAMILIES['main']['probeAt']
for _ in range(5):
    g.status('clist')
ck(abs(g._FAMILIES['main']['probeAt'] - probe_at) < 1e-6,
   '连续问 5 次 status，探测窗口时刻不变（否则"看一眼配置页"就吃掉一次探测）')
ck(g.status('clist')['blocksRequest'] is True and g.skip('clist') is True,
   'blocksRequest 与 skip 同判据')

# ========== [8] 端到端：clist 停调不得把情绪整份打成 stale ==========
print('[8] 端到端：main 停调 + pools 健康 → 情绪仍是 fresh 的盘中口径')
# 这就是 11-06 09:15~13:16 的真实处境。老行为下这一刻三条链路一起停手，新浪备源
# 给不出连板梯队/炸板率/昨日溢价，快照 stale=1，于是转弱推送、修复推送、减仓影子
# 全部静音；分家族之后三池照取，只有"昨日涨停溢价"（走 ulist）这一维如实置缺。
import os as _os
import sqlite3 as _sq3
import tempfile as _tf
import time as _t
import db.database as _dbmod

# ⚠️ 这一段会真的走 save_pools / 快照落库，必须先把连接指到临时库：
# 写这份自测时就踩过一次——只改了 db.database.SQLITE_DB_PATH（那是星号导入出来的副本，
# get_db_conn 读的是 db.dal_state 里那份），3 行假票直接进了真库 limit_pool_daily。
_DT_DB = _os.path.join(_tf.gettempdir(), f'emg_selftest_dt_{_os.getpid()}.db')
for _suf in ('', '-wal', '-shm'):
    try:
        _os.remove(_DT_DB + _suf)
    except OSError:
        pass
_dbmod.use_db_path(_DT_DB)
_dbmod.init_db()

from services.strategy import sentiment as S
reset()
_now = _t.time()
g.fail('clist', CON, now=_now)
g.fail('clist', CON, now=_now + 1)
ck(g.skip('clist') and not g.skip('pools'), '前置状态：main 停、pools 不停')
_POOL = [{'c': '600001', 'n': '甲', 'lbc': 3, 'zdp': 10.1, 'fund': 1e8, 'ltsz': 5e9,
          'hybk': 'BK1', 'zbc': 0},
         {'c': '600002', 'n': '乙', 'lbc': 1, 'zdp': 10.0, 'fund': 2e7, 'ltsz': 2e9,
          'hybk': 'BK2', 'zbc': 0}]
_hit = {'zt': 0, 'zb': 0, 'ulist': 0}
S._em_zt_pool_get = lambda date_str, pool_type='zt', **kw: (
    _hit.__setitem__('zt', _hit['zt'] + 1) or (_POOL if pool_type == 'zt' else []))
S._em_zb_pool_get = lambda date_str, **kw: (
    _hit.__setitem__('zb', _hit['zb'] + 1) or [{'c': '600003', 'n': '丙', 'zdp': 5.0}])
S._fetch_market_breadth = lambda: {'riseCount': 800, 'fallCount': 4000, 'riseFallRatio': 0.2}
S._yesterday_zt_pool = lambda today: _POOL
S._em_batch_change_percent = lambda codes, **kw: (_hit.__setitem__('ulist', _hit['ulist'] + 1) or {})
S._sync_collect.__globals__['get_session_phase'] = lambda *a, **k: {'phase': 'morning'}
S._CACHE.update({'ts': 0.0, 'data': None})
res = S._sync_collect()
ind = res.get('indicators') or {}
ck(res.get('source') == 'intraday' and res.get('stale') is False,
   f'情绪口径是盘中新鲜值（source={res.get("source")} stale={res.get("stale")}）——老行为这里是 alt_source+stale=True')
ck(_hit['zt'] >= 1 and _hit['zb'] >= 1, f'三池真的去取了（涨停/跌停池 {_hit["zt"]} 次、炸板池 {_hit["zb"]} 次）')
ck(ind.get('maxLianban') == 3 and ind.get('zhabanCount') == 1,
   f'连板高度/炸板数不再整维置缺（maxLianban={ind.get("maxLianban")} zhaban={ind.get("zhabanCount")}）')
ck(any('溢价' in str(x) for x in (res.get('dataGaps') or [])),
   f'唯一如实置缺的一维写进 dataGaps：{(res.get("dataGaps") or ["<空>"])[0][:40]}')
_saved = _sq3.connect(_DT_DB).execute('SELECT COUNT(*) FROM limit_pool_daily').fetchone()[0]
ck(_saved > 0, f'临时库收到池子明细 {_saved} 行（真库未参与）')
reset()

# ========== [9] classify / should_retry 未被改动 ==========
print('[9] 分级与"连接级失败不重试"仍成立（回归原有不变量）')
ck(g.classify(CON) == 'blocked', 'RemoteDisconnected → blocked')
ck(g.classify(ValueError('empty response')) == 'flaky', '空包 → flaky（抖动不当成封禁）')
ck(not g.should_retry(CON), 'blocked 不重试（不给 IP 续封）')
ck(g.should_retry(ValueError('rc=5')), 'flaky 可以重试')
reset()

print()
print(f'===== 结果：{len(PASS)} 通过 / {len(FAIL)} 失败 =====')
if FAIL:
    for f in FAIL:
        print(f'  ✗ {f}')
    sys.exit(1)
print('全部通过 ✓')
