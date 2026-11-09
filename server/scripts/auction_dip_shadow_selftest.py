# -*- coding: utf-8 -*-
"""auction_dip_shadow_selftest.py - 竞价低吸臂影子的回归自测（2026-11-07，影子台账第 20 条）

守的不变量：**"昨日涨停×今日竞价缺口"这套分档必须边界精确、三条腿必须用涨幅比值算
（不拿原始价除前复权价）、缺数据必须留 NULL 不许填 0、开关关掉必须一个字节都不写**，
而且判据是预注册的四条（天数/超额/按日t/正号占比），单日样本再好也不许放行。

分组与反证：
  [1] 档位边界：−3/0/3/4 四个界点各归哪档（把 ≤ 写成 < 必红）；
  [2] 收益公式：日内＝(1+收盘涨)/(1+缺口)−1、合计＝两腿复利；gap=−100 坏值 → None；
  [3] 执行性标记：buyable 7% 界、一字板按板块涨停幅（主板 10/创业 20）、收盘跌停标记；
  [4] build_rows 纯连接：不在竞价快照里的涨停票不写、zb 池不混入、封板质量字段原样带；
  [5] run_morning 端到端 + 幂等重跑 + 两条跳过路径（无昨日池 / 无今日快照）；
  [6] run_evening 回填：日内/隔夜/合计/基准四列逐个对数，无 T+1 的腿留 NULL（不是 0）；
  [7] 自愈与不抹值：补一根 T+1 就回填；已有值（哨兵 999）绝不被 COALESCE 抹掉；
  [8] 开关关掉 → 不建表、不写行，逐字节等于改造前；
  [9] evaluate：天数不足 ready=False；单日天大超额也 pass=False（t 算不出来）；
      cluster_stats 按日聚类已知值对数；
  [10] SPEC 注册：key/cfg/kind/live 对得上；
  [11] 真库泄漏反证：生产库里不许出现自测假票（9999xx）。
全程临时库（use_db_path）+ 临时日K库，真库只读打开做泄漏检查，一个字节都没写。
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

_TMP = os.path.join(tempfile.gettempdir(), f'auction_dip_{os.getpid()}.db')
_KLINE = os.path.join(tempfile.gettempdir(), f'auction_dip_kline_{os.getpid()}.db')
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
from services.data import limit_pool as lp             # noqa: E402
from services.pick import auction_dip_shadow as ads    # noqa: E402
from services.support import app_settings              # noqa: E402

PASS, FAIL = [], []


def ck(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(f"  [{'OK' if cond else 'XX'}] {msg}")


def near(a, b, tol=1e-6):
    return a is not None and b is not None and abs(a - b) <= tol


# ========== [1] 档位边界 ==========
print('[1] arm_of：−3/0/3/4 四个界点（≤ 写成 < 就会错档）')
ck(ads.arm_of(-3.0) == 'deep_dip', 'gap=−3.0 归深低开（边界含等号）')
ck(ads.arm_of(-2.99) == 'mild_dip', 'gap=−2.99 归浅低开')
ck(ads.arm_of(0.0) == 'mild_dip', 'gap=0 归浅低开（平盘不算高开）')
ck(ads.arm_of(0.01) == 'flat', 'gap=+0.01 归平开')
ck(ads.arm_of(3.0) == 'flat', 'gap=3.0 归平开（边界含等号）')
ck(ads.arm_of(3.01) == 'high', 'gap=3.01 归高开')
ck(ads.arm_of(3.99) == 'high', 'gap=3.99 仍归高开')
ck(ads.arm_of(4.0) == 'chase', 'gap=4.0 归追涨（边界含等号）')
ck(ads.arm_of(None) is None and ads.arm_of('坏值') is None, '缺数据不猜档，返回 None')

# ========== [2] 收益公式 ==========
print('[2] 涨幅比值公式（不拿原始价除前复权价）')
ck(near(ads.intraday_ret_pct(0.0, -5.0), (1 / 0.95 - 1) * 100),
   f'收盘平盘+竞价低开5% → 日内 +5.263%，实得 {ads.intraday_ret_pct(0.0, -5.0)}')
ck(near(ads.intraday_ret_pct(10.0, 5.0), (1.10 / 1.05 - 1) * 100),
   '收盘+10% / 高开5% → 日内 +4.762%（直接拿 10−5=5 就错了）')
ck(ads.intraday_ret_pct(None, -5.0) is None and ads.intraday_ret_pct(0.0, None) is None,
   '任一输入缺 → None，不当 0')
ck(ads.intraday_ret_pct(0.0, -100.0) is None, 'gap=−100 坏值（分母为0）→ None')
ck(near(ads.compose_total(5.2631578947, 2.0), (1.052631578947 * 1.02 - 1) * 100),
   '合计＝两腿复利，不是简单相加')
ck(ads.compose_total(None, 2.0) is None and ads.compose_total(5.0, None) is None,
   '任一腿 NULL → 合计 NULL')

# ========== [3] 执行性标记 ==========
print('[3] buyable / one_word / sealed / dt_close')
ck(ads.is_buyable(6.99) == 1 and ads.is_buyable(7.0) == 0, 'buyable 界在 7%（含等号归不可成交）')
ck(ads.is_buyable(-9.0) == 1, '深低开照样买得到（跌停开盘买单能成交）')
ck(ads.is_buyable(None) is None, '缺数据 None')
ck(ads.is_one_word(9.85, '600000') == 1 and ads.is_one_word(9.84, '600000') == 0,
   '主板一字板界＝10×0.985=9.85')
ck(ads.is_one_word(19.7, '300001') == 1 and ads.is_one_word(19.6, '300001') == 0,
   '创业板按 20% 档（19.7=20×0.985）')
ck(ads.is_sealed(9.85, '600000') == 1 and ads.is_sealed(9.84, '600000') == 0, '收盘封板同容差')
ck(ads.is_dt_close(-9.85, '600000') == 1 and ads.is_dt_close(-9.84, '600000') == 0,
   '收盘跌停标记（卖点没有对手盘）')
ck(ads.is_sealed(None, '600000') is None and ads.is_dt_close(None, '600000') is None,
   '缺数据 None，不当 0')

# ========== [4] build_rows 纯连接 ==========
print('[4] build_rows：涨停池∩竞价快照，字段原样带')
ZT = [
    {'symbol': '999901', 'name': '自测票甲', 'lianban': 2, 'zhaban': 0,
     'first_seal': '093000', 'seal_fund': 5e8, 'turnover': 12.5},
    {'symbol': '999902', 'name': '自测票乙', 'lianban': 1, 'zhaban': 1,
     'first_seal': '100000', 'seal_fund': 2e8, 'turnover': 8.0},
    {'symbol': '999903', 'name': '自测票丙', 'lianban': 1, 'zhaban': 0,
     'first_seal': '140000', 'seal_fund': 1e8, 'turnover': 5.0},   # 不在快照里 → 不写
]
AUC = [
    {'symbol': '999901', 'name': '自测票甲', 'gap_pct': -5.0, 'auction_price': 9.5,
     'amount': 1e8, 'bid1': 9.49},
    {'symbol': '999902', 'name': '自测票乙', 'gap_pct': 5.0, 'auction_price': 10.5,
     'amount': 2e8, 'bid1': 10.5},
    {'symbol': '999905', 'name': '自测票戊', 'gap_pct': 1.0, 'auction_price': 20.2,
     'amount': 3e8, 'bid1': 20.19},   # 范围股但非昨日涨停 → 不进影子
]
rows = ads.build_rows('2026-11-05', '2026-11-04', ZT, AUC)
ck(len(rows) == 2, f'交集只有 2 只（999903 无快照、999905 非涨停），实得 {len(rows)}')
by = {r['symbol']: r for r in rows}
ck(by['999901']['arm'] == 'deep_dip' and by['999902']['arm'] == 'chase', '两档各归各位')
ck(by['999901']['lianban'] == 2 and by['999901']['seal_fund'] == 5e8
   and by['999901']['first_seal'] == '093000', '封板质量字段原样带（裁决时分层用）')
ck(by['999901']['buyable'] == 1 and by['999902']['buyable'] == 1,
   '−5% 与 +5% 都 <7 → 可成交（+5 不是>7，别把 chase 一律当买不到）')
ck(by['999901']['prev_date'] == '2026-11-04', '昨日池日期带着，事后可审')

# ========== [5] run_morning 端到端 ==========
print('[5] run_morning：落库 / 幂等 / 两条跳过路径')
lp.ensure_table()
conn = sqlite3.connect(_TMP)
conn.execute('''CREATE TABLE IF NOT EXISTS auction_snapshot (
    trade_date TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT, auction_price REAL,
    prev_close REAL, gap_pct REAL, volume REAL, amount REAL, bid1 REAL, ask1 REAL,
    quote_time TEXT, fetched_at TEXT, PRIMARY KEY (trade_date, symbol))''')
conn.executemany(
    'INSERT INTO limit_pool_daily (trade_date,pool,symbol,name,lianban,zhaban,first_seal,'
    'seal_fund,turnover) VALUES (?,?,?,?,?,?,?,?,?)',
    [('2026-11-04', 'zt', z['symbol'], z['name'], z['lianban'], z['zhaban'],
      z['first_seal'], z['seal_fund'], z['turnover']) for z in ZT]
    + [('2026-11-04', 'zb', '999904', '自测炸板票', 1, 2, '100000', 1e8, 6.0)])
conn.executemany(
    'INSERT INTO auction_snapshot (trade_date,symbol,name,auction_price,prev_close,gap_pct,'
    'amount,bid1,quote_time) VALUES (?,?,?,?,?,?,?,?,?)',
    [('2026-11-05', a['symbol'], a['name'], a['auction_price'], 10.0 if a['symbol'] != '999905' else 20.0,
      a['gap_pct'], a['amount'], a['bid1'], '20261105092500') for a in AUC]
    + [('2026-11-05', '999906', '自测票己', 30.0, 30.0, 0.0, 4e8, 29.99, '20261105092500')])
conn.commit()
conn.close()
r5 = ads.run_morning('2026-11-05')
ck(r5.get('written') == 2 and r5.get('prev_date') == '2026-11-04', f'落库 2 行、昨日池取 11-04，实得 {r5}')
ck(r5.get('arms', {}).get('deep_dip') == 1 and r5.get('arms', {}).get('chase') == 1, '档位计数对')
conn = sqlite3.connect(_TMP)
n = conn.execute('SELECT COUNT(*) FROM auction_dip_shadow').fetchone()[0]
legs = conn.execute('SELECT intraday_ret_pct, next_open_ret_pct, bench_intraday_pct '
                    'FROM auction_dip_shadow ORDER BY symbol').fetchall()
has_zb = conn.execute("SELECT 1 FROM auction_dip_shadow WHERE symbol='999904'").fetchone()
conn.close()
ck(n == 2, f'表里 2 行（zb 池不混入、范围股不进影子），实得 {n}')
ck(has_zb is None, '炸板池那只没被当成涨停池写进来')
ck(all(v is None for row in legs for v in row), '判定轮三条腿与基准全 NULL（留给晚间回填，不是 0）')
r5b = ads.run_morning('2026-11-05')
conn = sqlite3.connect(_TMP)
n2 = conn.execute('SELECT COUNT(*) FROM auction_dip_shadow').fetchone()[0]
conn.close()
ck(r5b.get('written') == 2 and n2 == 2, f'幂等重跑不重复（UPSERT 同键），实得 {n2}')
r5c = ads.run_morning('2026-10-01')
ck('skipped' in r5c and 'limit_pool_daily' in r5c['skipped'], f'无昨日池 → 跳过写明原因，实得 {r5c}')
r5d = ads.run_morning('2026-11-07')
ck('skipped' in r5d and 'auction_snapshot' in r5d['skipped'],
   f'有昨日池但今日无快照 → 跳过（竞价事后补不回），实得 {r5d}')

# ========== [6] run_evening 回填 ==========
print('[6] run_evening：三条腿+基准逐个对数，无 T+1 留 NULL')
kc = sqlite3.connect(_KLINE)
kc.execute('CREATE TABLE stock_daily (symbol TEXT, date TEXT, open REAL, close REAL, '
           'PRIMARY KEY(symbol, date))')
kc.executemany('INSERT INTO stock_daily VALUES (?,?,?,?)', [
    # 999901：昨收10 / 今开9.5收10（平盘）/ T+1 开10.2收10.3
    ('999901', '2026-11-04', 9.8, 10.0),
    ('999901', '2026-11-05', 9.5, 10.0),
    ('999901', '2026-11-06', 10.2, 10.3),
    # 999902：昨收10 / 今开10.5收11（+10% 封板）/ **无 T+1**（停牌模拟）
    ('999902', '2026-11-04', 9.9, 10.0),
    ('999902', '2026-11-05', 10.5, 11.0),
    # 范围股 999905/999906：只参与基准
    ('999905', '2026-11-04', 19.8, 20.0),
    ('999905', '2026-11-05', 20.2, 20.0),
    ('999906', '2026-11-04', 29.8, 30.0),
    ('999906', '2026-11-05', 30.0, 30.6),
])
kc.commit()
kc.close()
r6 = ads.run_evening()
ck(r6.get('healed') == 2, f'回填 2 行，实得 {r6}')
conn = sqlite3.connect(_TMP)
d = {r[0]: r for r in conn.execute(
    'SELECT symbol, close_ret_pct, sealed_close, dt_close, intraday_ret_pct, '
    'next_open_ret_pct, total_open_ret_pct, next_date, bench_intraday_pct, bench_close_pct '
    'FROM auction_dip_shadow').fetchall()}
conn.close()
a = d['999901']
ck(near(a[1], 0.0) and a[2] == 0 and a[3] == 0, f'甲 收盘平盘/未封板/未跌停，实得 {a[1:4]}')
ck(near(a[4], (1 / 0.95 - 1) * 100), f'甲 日内 +5.263%（涨幅比值），实得 {a[4]}')
ck(near(a[5], 2.0) and a[7] == '2026-11-06', f'甲 隔夜 +2.0%、T+1 日期带着，实得 {a[5]} / {a[7]}')
ck(near(a[6], (1 / 0.95 * 1.02 - 1) * 100), f'甲 合计 +7.368%（复利），实得 {a[6]}')
b = d['999902']
ck(near(b[1], 10.0) and b[2] == 1, f'乙 收盘+10% 封住，实得 {b[1]} / {b[2]}')
ck(near(b[4], (1.10 / 1.05 - 1) * 100), f'乙 日内 +4.762%，实得 {b[4]}')
ck(b[5] is None and b[6] is None and b[7] == '',
   f'乙 无 T+1 → 隔夜/合计留 NULL（填 0 会把停牌混成平盘），实得 {b[5]} / {b[6]}')
bench_i = ((10.0 / 9.5 - 1) + (11.0 / 10.5 - 1) + (20.0 / 20.2 - 1) + (30.6 / 30.0 - 1)) * 100 / 4
ck(near(a[8], bench_i) and near(b[8], bench_i),
   f'基准＝同日范围4只 open→close 等权 {bench_i:.4f}%（含影子自己，范围就是分母），实得 {a[8]}')
ck(near(a[9], 3.0), f'基准收盘腿＝(0+10+0+2)/4=3.0%，实得 {a[9]}')

# ========== [7] 自愈与不抹值 ==========
print('[7] 补一根 T+1 就自愈；已有值（哨兵999）绝不被抹')
conn = sqlite3.connect(_TMP)
conn.execute("UPDATE auction_dip_shadow SET intraday_ret_pct=999.0 WHERE symbol='999901'")
conn.commit()
conn.close()
ads.run_evening()
conn = sqlite3.connect(_TMP)
sentinel = conn.execute("SELECT intraday_ret_pct FROM auction_dip_shadow "
                        "WHERE symbol='999901'").fetchone()[0]
conn.close()
ck(near(sentinel, 999.0), f'COALESCE 不抹已有值：哨兵 999 还在，实得 {sentinel}')
conn = sqlite3.connect(_TMP)
conn.execute("UPDATE auction_dip_shadow SET intraday_ret_pct=? WHERE symbol='999901'",
             ((1 / 0.95 - 1) * 100,))
conn.commit()
conn.close()
kc = sqlite3.connect(_KLINE)
kc.execute("INSERT INTO stock_daily VALUES ('999902','2026-11-06',10.6,10.7)")
kc.commit()
kc.close()
r7 = ads.run_evening()
conn = sqlite3.connect(_TMP)
b2 = conn.execute("SELECT next_open_ret_pct, total_open_ret_pct, next_date "
                  "FROM auction_dip_shadow WHERE symbol='999902'").fetchone()
conn.close()
ck(near(b2[0], (10.6 / 11.0 - 1) * 100) and b2[2] == '2026-11-06',
   f'乙 补上 T+1 后隔夜腿自愈回填，实得 {b2}')
ck(near(b2[1], ((1.10 / 1.05) * (10.6 / 11.0) - 1) * 100), f'合计腿跟着补上，实得 {b2[1]}')

# ========== [8] 开关关掉 = 改造前 ==========
print('[8] AUCTION_DIP_SHADOW_ENABLED=False → 不建表、不写行')
conn = sqlite3.connect(_TMP)
conn.execute('DROP TABLE auction_dip_shadow')
conn.commit()
conn.close()
cfg.AUCTION_DIP_SHADOW_ENABLED = False
r8a = ads.run_morning('2026-11-05')
r8b = ads.run_evening()
conn = sqlite3.connect(_TMP)
tbl = conn.execute("SELECT name FROM sqlite_master WHERE name='auction_dip_shadow'").fetchone()
conn.close()
ck(r8a.get('enabled') is False and r8b.get('enabled') is False, f'两轮都认开关，实得 {r8a} / {r8b}')
ck(tbl is None, '表都没建（不是建了空表），逐字节等于改造前')
cfg.AUCTION_DIP_SHADOW_ENABLED = True
ads.run_morning('2026-11-05')     # 恢复，给 [9] 用
ads.run_evening()

# ========== [9] evaluate 判据 ==========
print('[9] evaluate：天数不足不放行；单日天大超额也过不了（按日 t 算不出来）')
ev = ads.evaluate()
ck(ev['ready'] is False and ev['pass'] is False and ev['days'] == 1,
   f'1 天样本 → ready=False/pass=False，实得 days={ev["days"]}')
ck('0.5' in ev['rule'] and '20' in ev['rule'], f'判据原文带预注册数字，实得 {ev["rule"]}')
ev1 = ads.evaluate(min_days=1)
ck(ev1['ready'] is True and ev1['pass'] is False and ev1['checks']['t_ok'] is False,
   f'min_days=1 时天数够了但单日 t 算不出 → 仍不放行（反证：不许拿1天出结论），实得 {ev1["checks"]}')
ck(ev1['deep_dip']['excess_mean_pp'] is not None
   and ev1['deep_dip']['excess_mean_pp'] > 0.5,
   f'深低开(可成交)净超额确实 >0.5pp（所以卡住它的是 t 不是超额），实得 {ev1["deep_dip"]}')
cs = ads.cluster_stats([0.5, 1.5, 1.0])
ck(cs['days'] == 3 and near(cs['mean'], 1.0, 1e-3) and near(cs['t'], 3.46, 0.01)
   and cs['pos_ratio'] == 1.0, f'按日聚类已知值对数：mean=1/t≈3.46/全正，实得 {cs}')
cs2 = ads.cluster_stats([1.0, -1.0, 1.0, -1.0])
ck(near(cs2['mean'], 0.0) and near(cs2['t'], 0.0) and cs2['pos_ratio'] == 0.5,
   f'正负各半 → t=0、正号占比0.5，实得 {cs2}')
ck(ads.cluster_stats([])['days'] == 0 and ads.cluster_stats([])['mean'] is None, '空样本不瞎算')
ck(ev['chase_counterfactual']['days'] == 1, 'chase 反事实也在记（止血规则的对照证据）')
evf = ads.evaluate(forward_from='2026-11-09')
ck(evf['days'] == 0 and evf['ready'] is False and evf['rows'] == 0,
   f'forward_from 把回溯基线挡在钟外（自测数据是 11-05），实得 days={evf["days"]}')

# ========== [10] SPEC 注册 ==========
print('[10] SPEC：key/cfg/kind/live 对得上')
spec = {s['key']: s for s in app_settings.SPEC}
s = spec.get('auction_dip_shadow_enabled')
ck(s is not None and s.get('cfg') == 'AUCTION_DIP_SHADOW_ENABLED'
   and s.get('kind') == 'bool' and s.get('live') is True,
   f'配置页能热改这个开关，实得 {s and {k: s.get(k) for k in ("cfg", "kind", "live")}}')
ck(getattr(cfg, 'AUCTION_DIP_SHADOW_ENABLED', None) is True, 'cfg 默认开（只记录类影子惯例）')
ck(app_settings.value('auction_dip_shadow_enabled') is True, 'value() 现读默认值对')

# ========== [11] 真库泄漏反证 ==========
print('[11] 真库只读检查：自测假票（9999xx）一个字节都没漏进去')
prod = sqlite3.connect(f'file:{_ORIG}?mode=ro', uri=True)
try:
    has_tbl = prod.execute(
        "SELECT 1 FROM sqlite_master WHERE name='auction_dip_shadow'").fetchone()
    if has_tbl:
        leak = prod.execute(
            "SELECT COUNT(*) FROM auction_dip_shadow WHERE symbol LIKE '9999%'").fetchone()[0]
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