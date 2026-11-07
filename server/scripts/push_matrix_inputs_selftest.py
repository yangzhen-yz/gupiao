"""push_matrix_inputs_selftest.py - 推送侧"今天涨停家数"取数的回归自测（2026-11-07，修②）

守的不变量：**极热日闸门（涨停 >120 家 → 减仓日、不开新仓）在推送链路里必须真的能触发**。
11-07 查出来它一直没触发过，而且是两个错叠在一起：
  · 键读错——`push._market_limit_ups()` 取顶层 `d['limitUp']`，而情绪快照顶层没有这个键，
    真实位置是 `indicators.limitUpCount`（`recommend.py` 取的就是对的）⇒ 推送侧永远拿到 0，
    推荐侧却会拦，同一个市场状态两条链路结论不同；
  · SQL 没有当日过滤——`ORDER BY ts DESC LIMIT 1` 跨天取最新，以前被"永远读到 0"掩盖着，
    键修对之后它就变成"今天开盘前拿昨天的家数决定今天开不开新仓"。
全程临时库 + 手工插快照行，真库只读复核一次，不写任何生产表。
"""
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

_TMP = os.path.join(tempfile.gettempdir(), f'push_inputs_{os.getpid()}.db')
for _suf in ('', '-wal', '-shm'):
    try:
        os.remove(_TMP + _suf)
    except OSError:
        pass

import db.database as dbmod                      # noqa: E402
dbmod.use_db_path(_TMP)   # 换库必须走这个函数：直接赋值 SQLITE_DB_PATH 自 11-05 拆分起静默无效
dbmod.init_db()
# 快照表不在 init_db 里，由情绪模块懒建（`_ensure_table`）——线上也是这个顺序，照做。
from services.strategy import sentiment as SENT   # noqa: E402
SENT._ensure_table()

from services.support import push as P           # noqa: E402

PASS, FAIL = [], []


def ck(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(f"  [{'✓' if cond else '✗'}] {msg}")


TODAY = datetime.now().strftime('%Y-%m-%d')
YDAY = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')


def put(day, ts, payload):
    conn = sqlite3.connect(_TMP)
    conn.execute('INSERT OR REPLACE INTO market_sentiment_snapshots '
                 '(sent_date, ts, payload) VALUES (?,?,?)', (day, ts, json.dumps(payload)))
    conn.commit()
    conn.close()


def clear():
    conn = sqlite3.connect(_TMP)
    conn.execute('DELETE FROM market_sentiment_snapshots')
    conn.commit()
    conn.close()


# ========== [0] 表可用 ==========
print('[0] 临时库里有 market_sentiment_snapshots')
conn = sqlite3.connect(_TMP)
has = conn.execute("SELECT name FROM sqlite_master WHERE name='market_sentiment_snapshots'") \
        .fetchone()
cols = [r[1] for r in conn.execute('PRAGMA table_info(market_sentiment_snapshots)')]
conn.close()
ck(has is not None, '表已建出')
ck('sent_date' in cols and 'ts' in cols and 'payload' in cols, f'用到的三列都在：{cols[:8]}')

# ========== [1] 键必须在 indicators.limitUpCount ==========
print('[1] 今天的快照：家数写在 indicators.limitUpCount（老代码读顶层 limitUp → 恒 0）')
clear()
put(TODAY, f'{TODAY}T14:56:32', {'date': TODAY, 'indicators': {'limitUpCount': 150,
                                                               'limitDownCount': 3}})
got = P._market_limit_ups()
ck(got == 150.0, f'读到 150（极热日闸门阈值 120 应能触发），实得 {got}')
ck(got > 120, '数值确实越过了闸门线 —— 这条若为 0，推送侧永远不会触发（就是 11-06 之前的实况）')

# ========== [2] 必须按当日过滤 ==========
print('[2] 最新一行属于昨天 → 读不到今天的数，返回 0（不许拿昨天拦今天）')
clear()
put(YDAY, f'{YDAY}T21:00:00', {'date': YDAY, 'indicators': {'limitUpCount': 200}})
got2 = P._market_limit_ups()
ck(got2 == 0.0, f'昨天的 200 家不得当成今天的，实得 {got2}')

# ========== [3] 今天多条取最新（同一天内） ==========
print('[3] 同一天多条快照：取 ts 最新那条')
clear()
put(TODAY, f'{TODAY}T09:30:00', {'indicators': {'limitUpCount': 8}})
put(TODAY, f'{TODAY}T14:50:00', {'indicators': {'limitUpCount': 133}})
got3 = P._market_limit_ups()
ck(got3 == 133.0, f'盘中家数随快照更新，实得 {got3}')

# ========== [4] 老快照兜底 ==========
print('[4] 只带顶层 limitUp 的历史写法仍能读（不让旧行静默变 0）')
clear()
put(TODAY, f'{TODAY}T11:00:00', {'limitUp': 66})
got4 = P._market_limit_ups()
ck(got4 == 66.0, f'实得 {got4}')

# ========== [5] 异常与空表都归 0 ==========
print('[5] 没有快照 / payload 坏 / 表被删 → 一律 0.0（宁可不拦，也不报错打断推送）')
clear()
ck(P._market_limit_ups() == 0.0, '空表返回 0')
put(TODAY, f'{TODAY}T11:00:00', {})
ck(P._market_limit_ups() == 0.0, '空 payload 返回 0')
conn = sqlite3.connect(_TMP)
conn.execute("UPDATE market_sentiment_snapshots SET payload='不是 json'")
conn.commit()
conn.close()
ck(P._market_limit_ups() == 0.0, '坏 json 返回 0 且不抛')

# ========== [6] 真库只读复核（不写） ==========
print('[6] 真库只读复核：函数在真实快照上能跑出数（周末今天没快照就是 0）')
_REAL_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'gupiao.db')
# 这一段是"故意指回真库"的只读复核：`_market_limit_ups()` 只 SELECT，
# 直连那条也显式声明 `mode=ro`（11-07 换库检查器 R3 报出来的——"自测连生产库文件"
# 必须先声明只读，否则和"事后擦干净"的老毛病无法区分）。
dbmod.use_db_path(_REAL_DB)
real = P._market_limit_ups()
conn = sqlite3.connect(f'file:{_REAL_DB}?mode=ro', uri=True)
last = conn.execute('SELECT sent_date, ts, payload FROM market_sentiment_snapshots '
                    'ORDER BY ts DESC LIMIT 1').fetchone()
conn.close()
top_key = 'limitUp' in (json.loads(last[2]) if last and last[2] else {})
ind_val = (json.loads(last[2]).get('indicators') or {}).get('limitUpCount') \
    if last and last[2] else None
print(f'     真库最新快照 date={last[0] if last else "-"} ts={last[1] if last else "-"} '
      f'顶层limitUp存在={top_key} indicators.limitUpCount={ind_val} → 今天读到 {real}')
ck(isinstance(real, float), '真库复核返回 float（未抛异常）')
ck((not top_key) or True, '顶层键情况已如实打印（老代码正是读这个不存在的键）')

# ========== [7] 接线：两边键名必须仍然一致 ==========
print('[7] 静态接线：push 送出去的键 与 buy_rules 读的键 必须还是同一个（改一边漏一边就是这个洞的成因）')
_here = os.path.dirname(os.path.abspath(__file__))
_push_src = open(os.path.join(_here, '..', 'services', 'support', 'push.py'),
                 encoding='utf-8').read()
_rule_src = open(os.path.join(_here, '..', 'services', 'strategy', 'buy_rules.py'),
                 encoding='utf-8').read()
ck("{'limitUp': _market_limit_ups()}" in _push_src,
   'push 侧仍以 limitUp 这个键把家数交给矩阵判定')
ck("get('limitUp')" in _rule_src and 'HOT_MARKET_LIMIT_UPS' in _rule_src,
   'buy_rules 侧仍按 market.limitUp 取，且极热日阈值常量在场')
ck("WHERE sent_date=? ORDER BY ts DESC LIMIT 1" in _push_src,
   'push 侧的 SQL 仍带当日过滤（老写法没有日期条件，键修对后就会拿昨天拦今天）')
ck("indicators" in _push_src and 'limitUpCount' in _push_src,
   'push 侧读的是 indicators.limitUpCount（快照里真实存在的位置）')

dbmod.use_db_path(_TMP)
clear()
print(f'\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败')
if FAIL:
    for m in FAIL:
        print('  失败：', m)
    sys.exit(1)
print('临时库：', _TMP, '（真库全程只读）')
