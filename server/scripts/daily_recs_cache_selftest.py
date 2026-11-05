"""daily_recs_cache_selftest.py - 推荐记录缓存「写后失效 / 跨天自愈」自测（2026-11-05）

复现的事故：14:45 收盘前定稿已落库，但当晚首次打开推荐页仍拿到昨晚构建的旧档，
前端显示「今日推荐尚未生成」，用户被迫手动点生成，反而覆盖了收盘前定稿。

根因两条，都要证明被堵住：
  ① 定时批次(09:45~14:45)/手动生成是 services 层直接写库，不经过 POST/DELETE
     接口，缓存无人通知失效 → db 层写版本号兜住全部写入路径；
  ② 旧档里的 isToday 是构建时按当天算的，跨天后标签全过期 → built_date 校验兜住。

自测一律用临时 SQLite 库 + 打桩，绝不碰生产 gupiao.db。
"""
import os
import sys
import sqlite3
import tempfile
import asyncio
from datetime import date, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from db import database as db            # noqa: E402
from app.api import routes as r          # noqa: E402

FAILS = []


def ck(cond, msg):
    print(('  ✓ ' if cond else '  ✗ ') + msg)
    if not cond:
        FAILS.append(msg)


# ---------- 临时库：照搬生产表结构，保证 save/delete 的列名与真实一致 ----------
_tmpdir = tempfile.mkdtemp(prefix='recs_cache_selftest_')
_tmpdb = os.path.join(_tmpdir, 'test.db')
_prod = os.path.join(os.path.dirname(__file__), '..', 'gupiao.db')
_src = sqlite3.connect(_prod)
_ddls = [row[0] for row in _src.execute(
    "SELECT sql FROM sqlite_master WHERE type='table' "
    "AND name IN ('daily_recommendations','recommendation_batches')").fetchall()]
_src.close()
_c = sqlite3.connect(_tmpdb)
for _ddl in _ddls:
    if _ddl:
        _c.execute(_ddl)
_c.commit()
_c.close()


def _fake_conn(*args, **kwargs):
    c = sqlite3.connect(_tmpdb, timeout=5, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


db.get_db_conn = _fake_conn

TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


def _rec(day, sym, name):
    return {'date': day, 'daily_recommendations': [
        {'symbol': sym, 'name': name, 'price': '10.00', 'changePercent': 1.0,
         'score': 66, 'buySellPoints': {'buy': '9.80', 'sell': '10.60'}}]}


def _reset_cache(payload=None, version=None, built=None, age=0.0):
    import time as _t
    r._DAILY_RECS_CACHE.update({
        'payload': payload,
        'ts': _t.time() - age,
        'generation': 0,
        'version': db.daily_recs_write_version() if version is None else version,
        'built_date': TODAY if built is None else built,
    })
    r._DAILY_RECS_REFRESHING = False


print('[1] 写版本号：save / delete 成功后自增')
v0 = db.daily_recs_write_version()
ck(isinstance(v0, int), f'版本号可读（初值 {v0}）')
ok = db.save_daily_recommendations([_rec(TODAY, 'sz000001', '平安银行')],
                                   batch_time='14:45:10', source='pre_close')
v1 = db.daily_recs_write_version()
ck(ok and v1 == v0 + 1, f'定时批次(pre_close)写库后版本 {v0}→{v1}')
ok = db.delete_daily_recommendations_by_date(YESTERDAY)
v2 = db.daily_recs_write_version()
ck(ok and v2 == v1 + 1, f'按日期删除后版本 {v1}→{v2}')

print('[2] 写失败不 bump（保存异常时不能让缓存白白多重建一次）')
_orig = db.get_db_conn
try:
    def _boom(*a, **k):
        raise RuntimeError('simulated write failure')
    db.get_db_conn = _boom
    bad = db.save_daily_recommendations([_rec(TODAY, 'sz000002', '万科A')])
    ck((not bad) and db.daily_recs_write_version() == v2,
       '写异常时版本号保持不变')
finally:
    db.get_db_conn = _orig

print('[3] 冷启动 / 清档后：旧档不可返')
_reset_cache(payload=None)
ck(not r._daily_recs_cache_valid(), 'payload 为空 → 判定失效')
_reset_cache(payload={'success': True, 'data': []}, version=db.daily_recs_write_version())
ck(r._daily_recs_cache_valid(), '版本一致且是今天构建 → 判定有效')

print('[4] ① 定时批次写库后，未通知接口的旧档必须失效（本次事故主因）')
_stale_payload = {'success': True, 'data': [_rec(YESTERDAY, 'sz000003', '老票')]}
_reset_cache(payload=_stale_payload, version=db.daily_recs_write_version())
ck(r._daily_recs_cache_valid(), '写库前旧档有效')
db.save_daily_recommendations([_rec(TODAY, 'sz000004', '今日新票')],
                              batch_time='14:45:10', source='pre_close')
ck(not r._daily_recs_cache_valid(),
   'services 层直接写库 → 旧档立即失效（不依赖 TTL / 不依赖 HTTP 清档）')

print('[5] ② 跨天后旧档失效（isToday 标签过期）')
_reset_cache(payload={'success': True, 'data': []},
             version=db.daily_recs_write_version(), built=YESTERDAY)
ck(not r._daily_recs_cache_valid(), 'built_date 是昨天 → 判定失效（即使版本未变）')

print('[6] 端到端：写库后首次 GET 必须重建，且返回值含当天记录')
_calls = {'n': 0}


async def _fake_build():
    _calls['n'] += 1
    groups = db.load_daily_recommendations()
    return {'success': True, 'data': groups}


_orig_build = r._build_daily_recs_payload
r._build_daily_recs_payload = _fake_build
try:
    # 模拟"昨晚构建的旧档，里面没有今天记录"
    _reset_cache(payload={'success': True, 'data': [_rec(YESTERDAY, 'sz000003', '老票')]},
                 version=db.daily_recs_write_version() - 1, built=TODAY, age=0.0)
    _calls['n'] = 0
    out = asyncio.run(r.get_daily_recommendations())
    days = [g.get('date') for g in out.get('data', [])]
    ck(_calls['n'] == 1, '旧档失效时确实同步重建（不是直接返旧档）')
    ck(TODAY in days, f'返回值含当天记录 {days}（前端能找到 isToday）')
    ck(r._daily_recs_cache_valid(), '重建后的档标记为当前版本/今天，下次可命中')

    print('[7] 新鲜期内命中缓存，不重复重建')
    _calls['n'] = 0
    out2 = asyncio.run(r.get_daily_recommendations())
    ck(_calls['n'] == 0, '300s 新鲜期内直接返缓存')
    ck(out2 is out, '返回的仍是同一份档')

    print('[8] 写接口清档后失效（原有 generation 机制未回归）')
    g0 = r._DAILY_RECS_CACHE['generation']
    r._invalidate_daily_recs_cache()
    ck(not r._daily_recs_cache_valid(), '清档后判定失效')
    ck(r._DAILY_RECS_CACHE['generation'] == g0 + 1, 'generation 仍会自增')
    ck(r._DAILY_RECS_CACHE['version'] == -1 and r._DAILY_RECS_CACHE['built_date'] == '',
       '清档同时重置 version / built_date')
finally:
    r._build_daily_recs_payload = _orig_build

print('[9] 后台刷新写回必须带上版本与日期（否则会被反复判成失效）')
_reset_cache(payload={'success': True, 'data': []}, version=-1)
r._stamp_daily_recs_cache({'success': True, 'data': [1]})
ck(r._DAILY_RECS_CACHE['version'] == db.daily_recs_write_version(),
   '写回记录当前写版本')
ck(r._DAILY_RECS_CACHE['built_date'] == TODAY, '写回记录构建自然日')
ck(r._daily_recs_cache_valid(), '写回后判定有效（不会无限重建）')

print()
if FAILS:
    print(f'❌ 失败 {len(FAILS)} 项：')
    for f in FAILS:
        print('   -', f)
    sys.exit(1)
print('✅ 全部通过（推荐缓存写后失效 / 跨天自愈）')
