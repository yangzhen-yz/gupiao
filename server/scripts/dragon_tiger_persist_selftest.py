"""dragon_tiger_persist_selftest.py - 龙虎榜留档与停调回退的回归自测（2026-11-06）

守的不变量：**东财停调不能把龙虎榜变成空洞**。
此前龙虎榜只有内存 `_dt_cache`，是"只有东财一条路"的数据里唯一连旧值都留不住的一项：
进程重启即空、停调即整块没有，而它是评分的第 13 因子（smart_money，recommend/trend/scan
三处读）。11-06 起拉到就写 `dragon_tiger_daily`，取不到就读留档，并在返回里标 source。

自测要点（每条都带反证）：
  1. 落库幂等：同一批 rows 写两次不产生重复行，字段还原后聚合结果与直接喂东财 rows 一致；
  2. 停调时走留档：`skip('datacenter')` 为真 → `_dragon_rows` 返回 source='db'，不是空；
  3. 空结果不得覆盖缓存：接口和库都拿不到时 `_refresh_dt_cache` 保留上一份（老行为会把
     上次成功的榜单整份抹成空 map）；
  4. 北交所以外的 symbol 归一（600→sh、0/3→sz，其它跳过）与东财聚合口径一致；
  5. 开关 `DRAGON_TIGER_DB_FALLBACK=False` → 回到改造前（不读库、不标 db）。
全程临时库 + 假 HTTP，真库一个字节未动。
"""
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

_TMP = os.path.join(tempfile.gettempdir(), f'dt_persist_{os.getpid()}.db')
for _suf in ('', '-wal', '-shm'):
    try:
        os.remove(_TMP + _suf)
    except OSError:
        pass

import db.database as dbmod                      # noqa: E402
# 换库必须走 dbmod.use_db_path()：11-05 database.py 拆分后只赋值 dbmod.SQLITE_DB_PATH 是
# 静默无效的（get_db_conn 读的是 db.dal_state 里那份）。写这份自测时踩过一次——
# 3 行假龙虎榜直接进了生产库 server/gupiao.db，事后删干净才知道要收口。
dbmod.use_db_path(_TMP)
dbmod.init_db()
assert sqlite3.connect(_TMP).execute(
    "SELECT name FROM sqlite_master WHERE name='dragon_tiger_daily'").fetchone(), '临时库没建出新表'

from datetime import datetime, timedelta         # noqa: E402
import app.config as cfg                         # noqa: E402
from services.data import eastmoney_guard as g   # noqa: E402
from services.data import smartmoney as sm       # noqa: E402

g._save_state_locked = lambda: None              # 别污染真实熔断状态文件

PASS, FAIL = [], []


def ck(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(f"  [{'✓' if cond else '✗'}] {msg}")


TODAY = datetime.now().strftime('%Y-%m-%d')
YDAY = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
ROWS = [
    {'TRADE_DATE': TODAY, 'SECURITY_CODE': '600123', 'SECURITY_NAME_ABBR': '甲',
     'EXPLANATION': '日涨幅偏离7%', 'BILLBOARD_NET_AMT': 1e8, 'BILLBOARD_BUY_AMT': 2e8,
     'BILLBOARD_SELL_AMT': 1e8, 'CHANGE_RATE': 9.9, 'CLOSE_PRICE': 10.0,
     'FREE_MARKET_CAP': 5e9},
    {'TRADE_DATE': TODAY, 'SECURITY_CODE': '600123', 'SECURITY_NAME_ABBR': '甲',
     'EXPLANATION': '日换手率达20%', 'BILLBOARD_NET_AMT': 0.5e8, 'BILLBOARD_BUY_AMT': 1e8,
     'BILLBOARD_SELL_AMT': 0.5e8, 'CHANGE_RATE': 9.9, 'CLOSE_PRICE': 10.0,
     'FREE_MARKET_CAP': 5e9},
    {'TRADE_DATE': YDAY, 'SECURITY_CODE': '000456', 'SECURITY_NAME_ABBR': '乙',
     'EXPLANATION': '日跌幅偏离7%', 'BILLBOARD_NET_AMT': -2e7, 'BILLBOARD_BUY_AMT': 1e7,
     'BILLBOARD_SELL_AMT': 3e7, 'CHANGE_RATE': -8.0, 'CLOSE_PRICE': 8.0,
     'FREE_MARKET_CAP': 3e9},
    {'TRADE_DATE': YDAY, 'SECURITY_CODE': '830123', 'SECURITY_NAME_ABBR': '北证',
     'EXPLANATION': 'xxx', 'BILLBOARD_NET_AMT': 1e7, 'BILLBOARD_BUY_AMT': 1e7,
     'BILLBOARD_SELL_AMT': 0, 'CHANGE_RATE': 1.0, 'CLOSE_PRICE': 5.0,
     'FREE_MARKET_CAP': 1e9},
    {'TRADE_DATE': YDAY, 'SECURITY_CODE': 'BADCODE', 'SECURITY_NAME_ABBR': '脏数据',
     'EXPLANATION': '', 'BILLBOARD_NET_AMT': None},
]

# ========== [1] 落库与幂等 ==========
print('[1] save_dragon_tiger_rows：字段归一 + 覆盖写幂等')
n1 = dbmod.save_dragon_tiger_rows(ROWS)
ck(n1 == 3, f'3 条 A 股记录入库（北证/脏代码被剔），实得 {n1}')
n2 = dbmod.save_dragon_tiger_rows(ROWS)
conn = sqlite3.connect(_TMP)
total = conn.execute('SELECT COUNT(*) FROM dragon_tiger_daily').fetchone()[0]
conn.close()
ck(n2 == 3 and total == 3, f'重跑不产生重复行（幂等键 trade_date+symbol+reason），实得 {total} 行')

# ========== [2] 读回口径与聚合一致 ==========
print('[2] load_dragon_tiger_rows_since 还原成东财字段，聚合结果必须与直接喂接口一致')
since = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
back = dbmod.load_dragon_tiger_rows_since(since)
ck(len(back) == 3, f'读回 3 行，实得 {len(back)}')
ck(all(str(r.get('TRADE_DATE'))[:10] in (TODAY, YDAY) for r in back), 'TRADE_DATE 原样带回')
ck({r['SECURITY_CODE'] for r in back} == {'600123', '000456'}, 'SECURITY_CODE 还原成 6 位裸码')
map_em, list_em, ltd_em = sm._aggregate(days=5, rows=ROWS)
map_db, list_db, ltd_db = sm._aggregate(days=5, rows=back)
ck(map_em == map_db, '东财 rows 与库内留档聚合出的 symbol_map 完全一致（同一份聚合代码）')
ck(ltd_em == ltd_db == TODAY, f'latest_trade_date 一致（实得 {ltd_db}）')
ck(abs(map_db.get('sh600123', {}).get('net_amt', 0) - 1.5e8) < 1,
   f'同一票两条原因 net_amt 求和 = 1.5 亿，实得 {map_db.get("sh600123")}')
ck(map_db.get('sh600123', {}).get('appearances') == 2, 'appearances 计两条上榜')

# ========== [3] 停调时走留档 ==========
print('[3] skip(datacenter) 为真 → _dragon_rows 读留档而不是返回空')
g.reset_all()
# 停调状态用"人工暂停"来表达，语义最干净：自动冷却还带探测窗口（到点会放一次真请求，
# 自测里就变成真打东财），人工暂停是无条件的停。
g.pause(hours=1, target='datacenter', reason='自测：停调中')
rows, source = sm._dragon_rows(days=5)
ck(source == 'db' and len(rows) == 3, f'停调中拿到留档（source={source}, n={len(rows)}）')

# ========== [4] 空结果不得覆盖缓存 ==========
print('[4] 接口与库都空时，_refresh_dt_cache 必须保留上一份缓存')
sm._dt_cache.update({'ts': time.time(), 'days': 5, 'map': {'sh600123': {'net_amt': 1.0}},
                     'list': [], 'latest_trade_date': TODAY, 'source': 'eastmoney'})
conn = sqlite3.connect(_TMP)
conn.execute('DELETE FROM dragon_tiger_daily')
conn.commit()
conn.close()
sm._fetch_dragon_page = lambda page_size=1000: []      # 接口空（停调已清，走真取数路径）
before = dict(sm._dt_cache['map'])
sm._refresh_dt_cache(days=5)
ck(sm._dt_cache['map'] == before, '缓存未被空结果覆盖（老行为会把上次成功的榜抹成空 map）')

# ========== [5] 开关关闭 = 回到改造前 ==========
print('[5] DRAGON_TIGER_DB_FALLBACK=False 时不读库')
dbmod.save_dragon_tiger_rows(ROWS)
_orig = sm.DRAGON_TIGER_DB_FALLBACK
try:
    sm.DRAGON_TIGER_DB_FALLBACK = False
    rows2, source2 = sm._dragon_rows(days=5)
    ck(rows2 == [] and source2 == 'eastmoney',
       f'关掉开关后停调即返回空、不读库（source={source2}）——逐字节回到改造前')
finally:
    sm.DRAGON_TIGER_DB_FALLBACK = _orig
    g.resume('datacenter')

# ========== [6] 落库开关独立 ==========
print('[6] DRAGON_TIGER_PERSIST=False 时不写库（读到也不留档）')
conn = sqlite3.connect(_TMP)
conn.execute('DELETE FROM dragon_tiger_daily')
conn.commit()
conn.close()
g._state.clear(); g._FAMILIES.clear(); g._domain.update({'until': 0})
_orig_p = sm.DRAGON_TIGER_PERSIST
g.resume('datacenter')
try:
    sm.DRAGON_TIGER_PERSIST = False
    sm._fetch_dragon_page = lambda page_size=1000: ROWS
    rows3, source3 = sm._dragon_rows(days=5)
    cnt = sqlite3.connect(_TMP).execute('SELECT COUNT(*) FROM dragon_tiger_daily').fetchone()[0]
    ck(len(rows3) == 5 and cnt == 0,
       f'关掉留档开关：接口结果照用（{len(rows3)} 行），库里 0 行（实得 {cnt}）')
finally:
    sm.DRAGON_TIGER_PERSIST = _orig_p

print()
print(f'===== 结果：{len(PASS)} 通过 / {len(FAIL)} 失败 =====')
if FAIL:
    for f in FAIL:
        print(f'  ✗ {f}')
    sys.exit(1)
print('全部通过 ✓')
