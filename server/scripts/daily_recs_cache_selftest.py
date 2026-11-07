"""daily_recs_cache_selftest.py - 推荐记录缓存「写后失效 / 跨天自愈」自测（2026-11-05）

复现的事故：14:45 收盘前定稿已落库，但当晚首次打开推荐页仍拿到昨晚构建的旧档，
前端显示「今日推荐尚未生成」，用户被迫手动点生成，反而覆盖了收盘前定稿。

根因两条，都要证明被堵住：
  ① 定时批次(09:45~14:45)/手动生成是 services 层直接写库，不经过 POST/DELETE
     接口，缓存无人通知失效 → db 层写版本号兜住全部写入路径；
  ② 旧档里的 isToday 是构建时按当天算的，跨天后标签全过期 → built_date 校验兜住。

自测一律用临时 SQLite 库 + 打桩，绝不碰生产 gupiao.db。

**11-07 大修三处（修⑩-3；改之前这份自测已经一文不值，而且一边装绿一边污染生产库）**：

  A. **脚本整个是死的**。11-06 路由拆分（`267cfb8`）后端点函数搬进
     `app/api/routers/recommend.py`，而聚合模块 `app/api/routes.py` 只 include 路由器、
     不再再导出端点函数 → `r.get_daily_recommendations()` 直接 AttributeError，
     崩在第 [6] 组：前五组跑完、后面一条没跑，这个不变量从此没有守卫。

  B. **换库写法静默无效，假数据一直写进生产库**。原来那句 `db.get_db_conn = _fake_conn`
     打在 `db.database` 上，而 11-05 的 database.py 拆分后真正写库的是
     `db/dal/recommend.py` 里那份**星号导入的副本** —— 赋值对调用侧完全无效（就是 B7
     拦的那类病）。真库实拍（11-07 体检）：`daily_recommendations` 里留着
     `rec_date=2026-11-07 / sz000004 / 今日新票` 一行，`recommendation_batches` 里
     **每跑一次留三条** `batch_time='14:45:10'`（测试专用值）的假批次，今天有 5 组。
     现在改走唯一合法写法 `db.use_db_path(临时库)`：开头断言路径已切、[2] 组断言假批次
     真落在临时库、收尾换回原库并用 `mode=ro` 核对生产库没有 `自测票%` 残留。
     假票名也换成一眼能认出来的 `自测票*` + `sz9999xx`，不再借用真实代码 sz000001/sz000002。

  C. **打桩要打在真正被调用的那一层**。`recommend.py` 是 `from ..routes_state import *`，
     桩只打 `routes_state._build_daily_recs_payload` 对端点函数不生效（同一类门面副本）。
     本测两个命名空间都打，并加第 [10] 组反证：只打 routes_state 时，端点命名空间里
     解析到的必须还是原函数（只看命名空间，不执行端点、不打网络）。
"""
import os
import sys
import sqlite3
import tempfile
import asyncio
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from db import database as db                       # noqa: E402
from db.dal import recommend as dal_rec             # noqa: E402  写入路径实际调的那份 get_db_conn
from app.api import routes_state as st              # noqa: E402  缓存状态与判据在这一层
from app.api.routers import recommend as rec        # noqa: E402  端点函数在这一层

FAILS = []


def ck(cond, msg):
    print(('  ✓ ' if cond else '  ✗ ') + msg)
    if not cond:
        FAILS.append(msg)


# ---------- 临时库：照搬生产表结构（只读 DDL、不带数据），列名与真实一致 ----------
_tmpdir = tempfile.mkdtemp(prefix='recs_cache_selftest_')
_tmpdb = os.path.join(_tmpdir, 'test.db')
_prod = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'gupiao.db'))
_src = sqlite3.connect(f'file:{_prod}?mode=ro', uri=True)      # 只读：只要 DDL，不碰数据
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
ck(len(_ddls) == 2, f'从真库取到两张表的 DDL（实得 {len(_ddls)}）')

_OLD_DB = db.current_db_path()
db.use_db_path(_tmpdb)              # 唯一合法写法：见 db/database.py 的说明与本文件头 B 条
ck(os.path.abspath(db.current_db_path()) == os.path.abspath(_tmpdb),
   f'换库确实生效（current_db_path={db.current_db_path()}）')

TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


def _rec(day, sym, name):
    return {'date': day, 'daily_recommendations': [
        {'symbol': sym, 'name': name, 'price': '10.00', 'changePercent': 1.0,
         'score': 66, 'buySellPoints': {'buy': '9.80', 'sell': '10.60'}}]}


def _reset_cache(payload=None, version=None, built=None, age=0.0):
    st._DAILY_RECS_CACHE.update({
        'payload': payload,
        'ts': time.time() - age,
        'generation': 0,
        'version': db.daily_recs_write_version() if version is None else version,
        'built_date': TODAY if built is None else built,
    })
    st._DAILY_RECS_REFRESHING = False


print('[1] 写版本号：save / delete 成功后自增')
v0 = db.daily_recs_write_version()
ck(isinstance(v0, int), f'版本号可读（初值 {v0}）')
ok = db.save_daily_recommendations([_rec(TODAY, 'sz999901', '自测票A')],
                                   batch_time='14:45:10', source='pre_close')
v1 = db.daily_recs_write_version()
ck(ok and v1 == v0 + 1, f'定时批次(pre_close)写库后版本 {v0}→{v1}')
ok = db.delete_daily_recommendations_by_date(YESTERDAY)
v2 = db.daily_recs_write_version()
ck(ok and v2 == v1 + 1, f'按日期删除后版本 {v1}→{v2}')

print('[2] 假批次落在临时库（换库写法失效的话，这一组就是照妖镜）')
_tmp = sqlite3.connect(_tmpdb)
_n = _tmp.execute("SELECT COUNT(*) FROM daily_recommendations WHERE name='自测票A'").fetchone()[0]
_b = _tmp.execute("SELECT COUNT(*) FROM recommendation_batches "
                  "WHERE batch_time='14:45:10'").fetchone()[0]
_tmp.close()
ck(_n == 1, f'临时库 daily_recommendations 有那一行（实得 {_n}）')
ck(_b >= 1, f'临时库 recommendation_batches 有 14:45:10 那批（实得 {_b}）')

print('[3] 写失败不 bump（保存异常时不能让缓存白白多重建一次）')
_orig_get = dal_rec.get_db_conn
try:
    def _boom(*a, **k):
        raise RuntimeError('simulated write failure')
    dal_rec.get_db_conn = _boom      # 打在 DAL 模块自己那份名字上才管得住写入路径
    bad = db.save_daily_recommendations([_rec(TODAY, 'sz999902', '自测票B')])
    ck((not bad) and db.daily_recs_write_version() == v2,
       '写异常时版本号保持不变')
finally:
    dal_rec.get_db_conn = _orig_get

print('[4] 冷启动 / 清档后：旧档不可返')
_reset_cache(payload=None)
ck(not st._daily_recs_cache_valid(), 'payload 为空 → 判定失效')
_reset_cache(payload={'success': True, 'data': []}, version=db.daily_recs_write_version())
ck(st._daily_recs_cache_valid(), '版本一致且是今天构建 → 判定有效')

print('[5] ① 定时批次写库后，未通知接口的旧档必须失效（本次事故主因）')
_stale_payload = {'success': True, 'data': [_rec(YESTERDAY, 'sz999903', '自测票C旧')]}
_reset_cache(payload=_stale_payload, version=db.daily_recs_write_version())
ck(st._daily_recs_cache_valid(), '写库前旧档有效')
db.save_daily_recommendations([_rec(TODAY, 'sz999904', '自测票D')],
                              batch_time='14:45:10', source='pre_close')
ck(not st._daily_recs_cache_valid(),
   'services 层直接写库 → 旧档立即失效（不依赖 TTL / 不依赖 HTTP 清档）')

print('[6] ② 跨天后旧档失效（isToday 标签过期）')
_reset_cache(payload={'success': True, 'data': []},
             version=db.daily_recs_write_version(), built=YESTERDAY)
ck(not st._daily_recs_cache_valid(), 'built_date 是昨天 → 判定失效（即使版本未变）')

print('[7] 端到端：写库后首次 GET 必须重建，且返回值含当天记录')
_calls = {'n': 0}


async def _fake_build():
    _calls['n'] += 1
    groups = db.load_daily_recommendations()
    return {'success': True, 'data': groups}


_ORIG_ST = st._build_daily_recs_payload
_ORIG_REC = rec._build_daily_recs_payload
st._build_daily_recs_payload = _fake_build
rec._build_daily_recs_payload = _fake_build
try:
    # 模拟"昨晚构建的旧档，里面没有今天记录"
    _reset_cache(payload={'success': True, 'data': [_rec(YESTERDAY, 'sz999903', '自测票C旧')]},
                 version=db.daily_recs_write_version() - 1, built=TODAY, age=0.0)
    _calls['n'] = 0
    out = asyncio.run(rec.get_daily_recommendations())
    days = [g.get('date') for g in out.get('data', [])]
    ck(_calls['n'] == 1, '旧档失效时确实同步重建（不是直接返旧档）')
    ck(TODAY in days, f'返回值含当天记录 {days}（前端能找到 isToday）')
    ck(st._daily_recs_cache_valid(), '重建后的档标记为当前版本/今天，下次可命中')

    print('[8] 新鲜期内命中缓存，不重复重建')
    _calls['n'] = 0
    out2 = asyncio.run(rec.get_daily_recommendations())
    ck(_calls['n'] == 0, f'{st._DAILY_RECS_FRESH_TTL:g}s 新鲜期内直接返缓存')
    ck(out2 is out, '返回的仍是同一份档')

    print('[9] 写接口清档后失效（原有 generation 机制未回归）')
    g0 = st._DAILY_RECS_CACHE['generation']
    st._invalidate_daily_recs_cache()
    ck(not st._daily_recs_cache_valid(), '清档后判定失效')
    ck(st._DAILY_RECS_CACHE['generation'] == g0 + 1, 'generation 仍会自增')
    ck(st._DAILY_RECS_CACHE['version'] == -1 and st._DAILY_RECS_CACHE['built_date'] == '',
       '清档同时重置 version / built_date')
finally:
    st._build_daily_recs_payload = _ORIG_ST
    rec._build_daily_recs_payload = _ORIG_REC

print('[10] 反证：桩只打 routes_state 时端点吃不到（门面副本这一类）')
_g = rec.get_daily_recommendations.__globals__
ck(_g is vars(rec), '端点函数的自由名字解析到 routers.recommend 自己的命名空间')
ck(_g['_build_daily_recs_payload'] is _ORIG_REC, '未打桩时端点那份是原函数（前提成立）')
st._build_daily_recs_payload = _fake_build
try:
    ck(_g['_build_daily_recs_payload'] is _ORIG_REC,
       '只改 routes_state，端点命名空间里仍是原函数 ⇒ 光打这一层等于没打')
finally:
    st._build_daily_recs_payload = _ORIG_ST
rec._build_daily_recs_payload = _fake_build
try:
    ck(_g['_build_daily_recs_payload'] is _fake_build,
       '打在端点命名空间那层才真被调用（第 [7] 组用的就是它）')
finally:
    rec._build_daily_recs_payload = _ORIG_REC

print('[11] 后台刷新写回必须带上版本与日期（否则会被反复判成失效）')
_reset_cache(payload={'success': True, 'data': []}, version=-1)
st._stamp_daily_recs_cache({'success': True, 'data': [1]})
ck(st._DAILY_RECS_CACHE['version'] == db.daily_recs_write_version(),
   '写回记录当前写版本')
ck(st._DAILY_RECS_CACHE['built_date'] == TODAY, '写回记录构建自然日')
ck(st._daily_recs_cache_valid(), '写回后判定有效（不会无限重建）')

# ---------- 收尾：换回原库 + 只读核对真库没有本次的假数据 ----------
db.use_db_path(_OLD_DB)
ck(os.path.abspath(db.current_db_path()) == os.path.abspath(_OLD_DB),
   f'收尾换回原库（{db.current_db_path()}）')
_leak = -1
try:
    _p = sqlite3.connect(f'file:{_prod}?mode=ro', uri=True)
    _leak = _p.execute("SELECT COUNT(*) FROM daily_recommendations "
                       "WHERE name LIKE '自测票%'").fetchone()[0]
    _p.close()
except Exception as e:
    ck(False, f'真库核对异常: {type(e).__name__}: {e}')
ck(_leak == 0, f'[自测护栏] 真库无 "自测票" 残留行（只读核对，实得 {_leak}）')

print()
if FAILS:
    print(f'❌ 失败 {len(FAILS)} 项：')
    for f in FAILS:
        print('   -', f)
    sys.exit(1)
print('✅ 全部通过（推荐缓存写后失效 / 跨天自愈 / 换库真的换到了）')
