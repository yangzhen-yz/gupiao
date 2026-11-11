# -*- coding: utf-8 -*-
"""push_gate_shadow_selftest.py - 推送超价闸门影子的回归自测（2026-11-11，影子台账第 22 条）

守的不变量：**"现价 vs 买入上限"的超价判定必须边界精确（>0 才拦、=0 放行、缺值 NULL 不当 0）、
次日开盘净腿必须用推送现价做分母且扣双边费用、缺数据必须留 NULL 不许填 0、开关关掉必须一个字节都不写、
影子绝不影响推送本身（线上过滤链 never 引用 would_block）**，而且判据是预注册的三条
（配对日数/差值/按日 t），单日样本再好、零方差 t 算不出，都不许放行。

分组与反证：
  [1] would_block 边界：>0→1、=0→0、<0→0、None/坏值→None（把 >0 写成 >=0 必红）；
  [2] build_rows 纯计算：从 buySellPoints.buyTiming 取 overPct/buyHigh；缺价跳过、overPct 缺记 NULL、坏码跳过；
  [3] record_pushed 端到端 + 幂等重跑（UPSERT 同键，不碰已回填腿）；
  [4] build_heals 纯计算：毛＝次日open/推送现价−1、净＝毛−_rt_cost_pct；无 T+1 留 NULL（不是 0）；
  [5] run_evening 回填 + 自愈（补一根 T+1）+ COALESCE 不抹哨兵 999；
  [6] 开关关掉 → 不建表、不写行，逐字节等于改造前；
  [7] evaluate：配对日不足 ready=False；单日 t 算不出 pass=False；20 配对日变差值 pass=True；
      零方差 20 日 t=None 仍 pass=False（反证）；NULL 行只进覆盖率不进两组；单组日不算配对；forward_from 挡基线；
  [8] SPEC 注册：key/cfg/kind/live 对得上；
  [9] 源码守卫：线上 _pass_filter 不含 overPct 否决、push.py 不含 would_block、记录在推送成功之后、调度走交易日门控；
  [10] 真库泄漏反证：生产库里不许出现自测假票（9999xx/9998xx/9997xx）。
全程临时库（use_db_path）+ 临时日K库，真库只读打开做泄漏检查，一个字节都没写。
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

_TMP = os.path.join(tempfile.gettempdir(), f'push_gate_{os.getpid()}.db')
_KLINE = os.path.join(tempfile.gettempdir(), f'push_gate_kline_{os.getpid()}.db')
for _p in (_TMP, _KLINE):
    for _suf in ('', '-wal', '-shm'):
        try:
            os.remove(_p + _suf)
        except OSError:
            pass

import db.database as dbmod                            # noqa: E402
_ORIG = dbmod.SQLITE_DB_PATH                           # 真库路径只用于最后的只读泄漏检查
dbmod.use_db_path(_TMP)      # 换库只许走这个函数（11-05 拆分后直接赋值 SQLITE_DB_PATH 静默无效）
dbmod.init_db()

import app.config as cfg                               # noqa: E402
cfg.KLINE_HISTORY_DB_PATH = _KLINE                     # 日K读取在函数内 import，指到临时文件即生效
from services.pick import push_gate_shadow as pgs      # noqa: E402
from services.pick.rec_sim import _rt_cost_pct         # noqa: E402
from services.support import app_settings              # noqa: E402

PASS, FAIL = [], []


def ck(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(f"  [{'OK' if cond else 'XX'}] {msg}")


def near(a, b, tol=1e-6):
    return a is not None and b is not None and abs(a - b) <= tol


def _insert(date, symbol, wb, net, gross=None):
    """直接落一行已回填净腿的影子行（evaluate 测试用，绕过 heal）。"""
    c = sqlite3.connect(_TMP)
    c.execute('''INSERT INTO push_gate_shadow
        (push_date,symbol,name,push_time,price,buy_high,over_pct,would_block,score,
         next_date,next_open_ret_pct,net_ret_pct,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (date, symbol, '合成', '10:00:00', 10.0, 10.0,
         (1.0 if wb == 1 else -1.0 if wb == 0 else None), wb, 70.0,
         date, gross if gross is not None else net, net,
         '2026-11-11 00:00:00', '2026-11-11 00:00:00'))
    c.commit()
    c.close()


# ========== [1] would_block 边界 ==========
print('[1] would_block：>0 才拦、=0 放行、缺值 NULL（>=0 会把平盘误拦）')
ck(pgs.would_block(2.06) == 1, 'overPct=2.06>0 → 会拦（追高）')
ck(pgs.would_block(0.01) == 1, 'overPct=0.01>0 → 会拦（哪怕只超一点）')
ck(pgs.would_block(0.0) == 0, 'overPct=0 → 放行（现价恰好等于上限，不算追高）')
ck(pgs.would_block(-1.5) == 0, 'overPct=−1.5<0 → 放行（现价低于上限＝低吸区）')
ck(pgs.would_block(None) is None, '缺值 → None（不当 0，裁决时只统计覆盖率）')
ck(pgs.would_block('坏值') is None, '坏值 → None，不炸')

# ========== [2] build_rows 纯计算 ==========
print('[2] build_rows：从 buyTiming 取超价；缺价跳过、overPct 缺记 NULL、坏码跳过')
HITS = [
    {'symbol': 'sz999901', 'name': '自测甲', 'price': 17.09, 'score': 74,
     'buySellPoints': {'buyTiming': {'overPct': 2.06, 'buyHigh': 16.75, 'type': 'tail'}}},
    {'symbol': 'sh999902', 'name': '自测乙', 'price': 10.0, 'score': 80,
     'buySellPoints': {'buyTiming': {'overPct': -1.5, 'buyHigh': 10.15, 'type': 'pullback'}}},
    {'symbol': 'sz999903', 'name': '自测丙', 'price': 20.0, 'score': 71,
     'buySellPoints': {'buyTiming': {'overPct': 0.0, 'buyHigh': 20.0}}},
    {'symbol': 'sz999904', 'name': '自测丁', 'price': 0, 'score': 70,
     'buySellPoints': {'buyTiming': {'overPct': 3.0, 'buyHigh': 9.7}}},   # 缺价 → 跳过
    {'symbol': 'sz999905', 'name': '自测戊', 'price': 15.0, 'score': 72,
     'buySellPoints': {'buyTiming': {}}},                               # overPct 缺 → NULL，价在照记
    {'symbol': 'bad', 'name': '坏码', 'price': 5.0, 'score': 60, 'buySellPoints': {}},  # 坏码 → 跳过
]
rows = pgs.build_rows('2026-11-05', '10:39:00', HITS)
ck(len(rows) == 4, f'6 只里 4 只有效（缺价 999904、坏码 bad 跳过），实得 {len(rows)}')
by = {r['symbol']: r for r in rows}
ck(by['999901']['would_block'] == 1 and by['999901']['over_pct'] == 2.06
   and by['999901']['buy_high'] == 16.75, '甲：超价 2.06 会拦，上限/超价原样带')
ck(by['999902']['would_block'] == 0 and by['999903']['would_block'] == 0,
   '乙（−1.5）丙（0.0）都放行')
ck(by['999905']['would_block'] is None and by['999905']['over_pct'] is None
   and by['999905']['price'] == 15.0, '戊：overPct 缺记 NULL 但价在照记（覆盖率可审）')
ck(by['999901']['name'] == '自测甲' and by['999901']['score'] == 74.0
   and by['999901']['push_time'] == '10:39:00', '名称/评分/推送时刻带着')
ck(all(r['symbol'] == r['symbol'][-6:] for r in rows), 'symbol 统一裸 6 位（与日K库同键）')

# ========== [3] record_pushed 端到端 + 幂等 ==========
print('[3] record_pushed：落库 / 幂等重跑不重复 / 判定轮腿全 NULL')
r3 = pgs.record_pushed(HITS, push_date='2026-11-05')
ck(r3.get('written') == 4 and r3.get('blocked') == 1 and r3.get('allowed') == 2
   and r3.get('unknown') == 1, f'记 4 行：会拦1/放行2/缺值1，实得 {r3}')
conn = sqlite3.connect(_TMP)
n = conn.execute('SELECT COUNT(*) FROM push_gate_shadow').fetchone()[0]
legs = conn.execute('SELECT next_open_ret_pct, net_ret_pct FROM push_gate_shadow').fetchall()
conn.close()
ck(n == 4, f'表里 4 行，实得 {n}')
ck(all(v is None for row in legs for v in row), '判定轮次日腿全 NULL（留给晚间回填，不是 0）')
r3b = pgs.record_pushed(HITS, push_date='2026-11-05')
conn = sqlite3.connect(_TMP)
n2 = conn.execute('SELECT COUNT(*) FROM push_gate_shadow').fetchone()[0]
conn.close()
ck(r3b.get('written') == 4 and n2 == 4, f'幂等重跑不重复（UPSERT 同键 push_date+symbol），实得 {n2}')

# ========== [4] build_heals 纯计算 ==========
print('[4] build_heals：毛＝次日open/推送现价−1、净＝毛−双边费用；无 T+1 留 NULL')
# pending 形状 (push_date, symbol, price, next_open_ret_pct, net_ret_pct)
pending = [('2026-11-05', '999901', 17.09, None, None),
           ('2026-11-05', '999902', 10.0, None, None),
           ('2026-11-05', '999905', 15.0, None, None)]   # 999905 无 T+1
bars = {'2026-11-05': {
    '999901': [('2026-11-05', 16.8, 17.0, 16.5), ('2026-11-06', 17.5, 17.6, 17.0)],
    '999902': [('2026-11-05', 10.1, 10.0, 9.9), ('2026-11-06', 9.8, 9.9, 10.0)],
    '999905': [('2026-11-05', 15.1, 15.0, 14.9)],   # 只有一根，无次日
}}
heals = pgs.build_heals(pending, bars)
hb = {h['symbol']: h for h in heals}
ck(len(heals) == 2, f'999905 无 T+1 → 不产 heal 行（留 NULL），实得 {len(heals)}')
g1 = (17.5 / 17.09 - 1) * 100
ck(near(hb['999901']['next_open_ret_pct'], g1), f'甲 毛腿＝17.5/17.09−1={g1:.4f}%（分母是推送现价不是上限）')
ck(near(hb['999901']['net_ret_pct'], g1 - _rt_cost_pct(17.09, 17.5)),
   f'甲 净腿＝毛−双边费用（rec_sim 同一把尺子），实得 {hb["999901"]["net_ret_pct"]:.4f}')
ck(hb['999901']['net_ret_pct'] < hb['999901']['next_open_ret_pct'], '费用为正：净 < 毛')
ck(hb['999901']['next_date'] == '2026-11-06', 'T+1 日期带着')
g2 = (9.8 / 10.0 - 1) * 100
ck(near(hb['999902']['next_open_ret_pct'], g2) and g2 < 0, f'乙 毛腿 {g2:.2f}%（次日低开亏钱）')
ck('999905' not in hb, '无次日开盘的票绝不填 0（填 0 会把停牌混成平盘）')

# ========== [5] run_evening 回填 + 自愈 + 不抹值 ==========
print('[5] run_evening：端到端回填 / 补 T+1 自愈 / COALESCE 不抹哨兵')
kc = sqlite3.connect(_KLINE)
kc.execute('CREATE TABLE stock_daily (symbol TEXT, date TEXT, open REAL, close REAL, '
           'PRIMARY KEY(symbol, date))')
kc.executemany('INSERT INTO stock_daily VALUES (?,?,?,?)', [
    ('999901', '2026-11-05', 16.8, 17.0), ('999901', '2026-11-06', 17.5, 17.6),
    ('999902', '2026-11-05', 10.1, 10.0), ('999902', '2026-11-06', 9.8, 9.9),
    ('999903', '2026-11-05', 20.1, 20.0), ('999903', '2026-11-06', 20.5, 20.6),
    ('999905', '2026-11-05', 15.1, 15.0),   # 无 T+1（停牌模拟）
])
kc.commit()
kc.close()
r5 = pgs.run_evening()
ck(r5.get('healed') == 3, f'回填 3 行（999905 无 T+1 留 NULL），实得 {r5}')
conn = sqlite3.connect(_TMP)
d = {r[0]: r for r in conn.execute(
    'SELECT symbol, next_open_ret_pct, net_ret_pct, next_date FROM push_gate_shadow').fetchall()}
conn.close()
ck(near(d['999901'][1], (17.5 / 17.09 - 1) * 100) and d['999901'][3] == '2026-11-06',
   f'甲 次日腿对数，实得 {d["999901"]}')
ck(d['999905'][1] is None and d['999905'][2] is None and d['999905'][3] == '',
   f'戊 无 T+1 → 毛/净留 NULL、日期空（不是 0），实得 {d["999905"]}')
# 哨兵：已有值绝不被 COALESCE 抹掉
conn = sqlite3.connect(_TMP)
conn.execute("UPDATE push_gate_shadow SET net_ret_pct=999.0 WHERE symbol='999901'")
conn.commit()
conn.close()
pgs.run_evening()
conn = sqlite3.connect(_TMP)
sentinel = conn.execute("SELECT net_ret_pct FROM push_gate_shadow WHERE symbol='999901'").fetchone()[0]
conn.close()
ck(near(sentinel, 999.0), f'COALESCE 不抹已有值：哨兵 999 还在，实得 {sentinel}')
# 自愈：补一根 T+1，戊的腿回填
kc = sqlite3.connect(_KLINE)
kc.execute("INSERT INTO stock_daily VALUES ('999905','2026-11-06',15.3,15.4)")
kc.commit()
kc.close()
pgs.run_evening()
conn = sqlite3.connect(_TMP)
e5 = conn.execute("SELECT next_open_ret_pct, net_ret_pct, next_date FROM push_gate_shadow "
                  "WHERE symbol='999905'").fetchone()
conn.close()
ck(near(e5[0], (15.3 / 15.0 - 1) * 100) and e5[2] == '2026-11-06',
   f'戊 补上 T+1 后自愈回填，实得 {e5}')

# ========== [6] 开关关掉 = 改造前 ==========
print('[6] PUSH_GATE_SHADOW_ENABLED=False → 不建表、不写行')
conn = sqlite3.connect(_TMP)
conn.execute('DROP TABLE push_gate_shadow')
conn.commit()
conn.close()
cfg.PUSH_GATE_SHADOW_ENABLED = False
r6a = pgs.record_pushed(HITS, push_date='2026-11-05')
r6b = pgs.run_evening()
conn = sqlite3.connect(_TMP)
tbl = conn.execute("SELECT name FROM sqlite_master WHERE name='push_gate_shadow'").fetchone()
conn.close()
ck(r6a.get('enabled') is False and r6b.get('enabled') is False, f'两轮都认开关，实得 {r6a} / {r6b}')
ck(tbl is None, '表都没建（不是建了空表），逐字节等于改造前')
cfg.PUSH_GATE_SHADOW_ENABLED = True
pgs.record_pushed(HITS, push_date='2026-11-05')   # 恢复，给 [7] 用
pgs.run_evening()

# ========== [7] evaluate 判据 ==========
print('[7] evaluate：配对日不足不放行；单日/零方差 t 算不出也不放行；20 配对日变差值才放行')
ev = pgs.evaluate()
ck(ev['ready'] is False and ev['pass'] is False,
   f'真实数据只 1 个配对日 → ready=False/pass=False，实得 paired_days={ev["paired_days"]}')
ck('0.5' in ev['rule'] and '20' in ev['rule'], f'判据原文带预注册数字，实得 {ev["rule"]}')
ev1 = pgs.evaluate(min_days=1)
ck(ev1['ready'] is True and ev1['pass'] is False and ev1['checks']['t_ok'] is False,
   f'min_days=1 天数够但单日 t 算不出 → 仍不放行（反证：不许拿1天出结论），实得 {ev1["checks"]}')
ck(ev1['paired_days'] == 1, f'11-05 放行组(乙丙)与被拦组(甲)都有样本 → 1 个配对日，实得 {ev1["paired_days"]}')
# NULL 行只进覆盖率不进两组；单组日不算配对
_insert('2026-11-05', '999906', None, 5.0)     # 缺值行
_insert('2026-11-08', '999907', 0, 1.0)        # 只有放行组、无被拦组 → 不算配对日
ev2 = pgs.evaluate()
ck(ev2['unknown_n'] >= 1, f'NULL 行计入覆盖率 unknown_n，实得 {ev2["unknown_n"]}')
ck(ev2['paired_days'] == 1, f'11-08 单组日不算配对（仍只 11-05 一天），实得 {ev2["paired_days"]}')
# 20 个配对日、变差值（mean≈1.1、t 大）→ pass=True
for i in range(20):
    day = f'2026-12-{i + 1:02d}'
    _insert(day, f'9998{i * 2 + 1:02d}', 0, 1.0 + (i % 3) * 0.1)   # 放行组净均值更高
    _insert(day, f'9998{i * 2 + 2:02d}', 1, 0.0)                  # 被拦组净均值 0
evp = pgs.evaluate(forward_from='2026-12-01')
ck(evp['paired_days'] == 20 and evp['ready'] is True, f'20 配对日 → ready=True，实得 {evp["paired_days"]}')
ck(evp['pass'] is True and evp['checks']['gap_ok'] is True and evp['checks']['t_ok'] is True,
   f'差值≈1.1pp≥0.5 且按日 t≥2 → pass=True，实得 {evp["checks"]} / gap={evp["paired"]["gap_mean_pp"]}')
ck(evp['allowed']['net_mean_pct'] > evp['blocked']['net_mean_pct'],
   f'放行组净均值 > 被拦组（闸门假设方向对），实得 {evp["allowed"]} vs {evp["blocked"]}')
# 反证：20 个配对日但零方差（diff 恒 1.0）→ t=None → 仍 pass=False
for i in range(20):
    day = f'2027-01-{i + 1:02d}'
    _insert(day, f'9997{i * 2 + 1:02d}', 0, 1.0)
    _insert(day, f'9997{i * 2 + 2:02d}', 1, 0.0)
evz = pgs.evaluate(forward_from='2027-01-01')
ck(evz['paired_days'] == 20 and evz['checks']['days_ok'] is True
   and evz['checks']['gap_ok'] is True and evz['checks']['t_ok'] is False and evz['pass'] is False,
   f'零方差 20 日：天数/差值都够但 t 算不出 → 不放行（反证），实得 {evz["checks"]}')
# forward_from 把真实基线挡在钟外
evf = pgs.evaluate(forward_from='2027-02-01')
ck(evf['paired_days'] == 0 and evf['ready'] is False and evf['rows'] == 0,
   f'forward_from 之后无数据 → 空钟，实得 {evf["paired_days"]}')
cs = pgs.cluster_stats([0.5, 1.5, 1.0])
ck(cs['days'] == 3 and near(cs['mean'], 1.0, 1e-3) and near(cs['t'], 3.46, 0.01),
   f'按日聚类已知值对数（复用竞价低吸臂同一函数）：mean=1/t≈3.46，实得 {cs}')
ck(pgs.cluster_stats([])['days'] == 0 and pgs.cluster_stats([])['mean'] is None, '空样本不瞎算')

# ========== [8] SPEC 注册 ==========
print('[8] SPEC：key/cfg/kind/live 对得上')
spec = {s['key']: s for s in app_settings.SPEC}
s = spec.get('push_gate_shadow_enabled')
ck(s is not None and s.get('cfg') == 'PUSH_GATE_SHADOW_ENABLED'
   and s.get('kind') == 'bool' and s.get('live') is True,
   f'配置页能热改这个开关，实得 {s and {k: s.get(k) for k in ("cfg", "kind", "live")}}')
ck(getattr(cfg, 'PUSH_GATE_SHADOW_ENABLED', None) is True, 'cfg 默认开（只记录类影子惯例）')
ck(getattr(cfg, 'PUSH_GATE_SHADOW_EVENING_TIME', None) == '20:45', '回填时刻 20:45（竞价低吸臂之后）')
ck(app_settings.value('push_gate_shadow_enabled') is True, 'value() 现读默认值对')

# ========== [9] 源码守卫：影子没接进动作链 ==========
print('[9] 源码守卫：线上推送过滤 never 引用 would_block / overPct 否决；记录在推送成功之后')
_push_src = open(os.path.join(os.path.dirname(__file__), '..', 'services', 'support', 'push.py'),
                 encoding='utf-8').read()
ck('would_block' not in _push_src, 'push.py 全文不含 would_block（闸门判定只活在影子里，没接进推送）')
_pf = _push_src.split('def _pass_filter')[1].split('\ndef ')[0]
ck('overPct' not in _pf and 'over_pct' not in _pf,
   '线上 _pass_filter 不含超价否决（影子期一只都不拦）')
ck(_push_src.index('push_wecom_markdown(content)') < _push_src.index('record_pushed'),
   'record_pushed 在推送成功之后调（是记录器不是过滤器）')
ck('try:' in _push_src.split('record_pushed')[0].split('push_wecom_markdown(content)')[1],
   'record_pushed 被 try/except 兜底（影子坏了不影响推送）')
_sched_src = open(os.path.join(os.path.dirname(__file__), '..', 'core', 'scheduler.py'),
                  encoding='utf-8').read()
ck('_warp_trading_only(job_push_gate_shadow_evening)' in _sched_src,
   '回填 job 走交易日门控（非交易日不跑）')

# ========== [10] 真库泄漏反证 ==========
print('[10] 真库只读检查：自测假票（9999xx/9998xx/9997xx）一个字节都没漏进去')
prod = sqlite3.connect(f'file:{_ORIG}?mode=ro', uri=True)
try:
    has_tbl = prod.execute("SELECT 1 FROM sqlite_master WHERE name='push_gate_shadow'").fetchone()
    if has_tbl:
        leak = prod.execute("SELECT COUNT(*) FROM push_gate_shadow WHERE symbol LIKE '9999%' "
                            "OR symbol LIKE '9998%' OR symbol LIKE '9997%'").fetchone()[0]
        ck(leak == 0, f'生产表里无假票（真影子行允许存在），实得泄漏 {leak}')
    else:
        ck(True, '生产库还没建这张表（首次产数前）——自测全程在临时库，符合预期')
finally:
    prod.close()

print(f'\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败')
if FAIL:
    for m in FAIL:
        print(f'  [XX] {m}')
    sys.exit(1)
print('全部通过')