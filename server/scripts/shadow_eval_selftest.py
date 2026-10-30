"""shadow_eval_selftest.py - 影子对照脚本的自测（临时库，不碰生产 gupiao.db）

为什么值得单独跑：这个脚本给的是**第1批要不要上线**的判据，它自己错了整个决策就错了。
最容易错的三处都在"分母"上，而分母错不会报错、只会把数变得好看：
  1. 老日志没有 kept 名单，只能反推 —— 反推的天数不能算进判定天数（realKeptDays）；
  2. 还没发生的 T+1 必须计成"样本未成熟"，不能当成 0% 拉低均值；
  3. 快照价与复权序列不同尺度（除权）的行要掉出批次价口径，不能混进胜率。
用四只假票把每种情况各造一遍，断言的是"数字对不对"，不是"跑没跑通"。
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

_TMP = os.path.join(tempfile.gettempdir(), f'shadow_test_{os.getpid()}.db')
_KLINE = os.path.join(tempfile.gettempdir(), f'shadow_kline_{os.getpid()}.db')
for p in (_TMP, _KLINE):
    for suf in ('', '-wal', '-shm'):
        try:
            os.remove(p + suf)
        except OSError:
            pass

import app.config as cfg
cfg.KLINE_HISTORY_DB_PATH = _KLINE
import db.database as dbmod
dbmod.SQLITE_DB_PATH = _TMP

from db.database import get_db_conn                     # noqa: E402

FAILS = []


def ck(cond, msg):
    print(('  ✓ ' if cond else '  ✗ ') + msg)
    if not cond:
        FAILS.append(msg)


def _item(sym, name, chg, score, verdict=None, reasons=None):
    it = {'symbol': sym, 'name': name, 'score': score, 'changePercent': chg,
          'volumeRatio': 1.2, 'timing': '', 'tag': '测试'}
    if verdict:
        it['matrix'] = {'verdict': verdict, 'why': '用例'}
    if reasons:
        it['reasons'] = reasons
    return it


# ---- 假日K：10-13 / 10-14 / 10-15 / 10-16 四天，10-16 就是 10-15 那批推荐的 T+1
#      注意 `stock_daily.symbol` 存的是 6 位裸代码（'600001'）而不是 'sh600001'：
#      日K库与主库的 symbol 口径本来就不一样，写错不会报错、只会全变成"当天日K缺失"，
#      很容易被误读成"数据没同步"。这个坑在自测里必须先踩一次。
kc = sqlite3.connect(_KLINE)
kc.execute('CREATE TABLE stock_daily (symbol TEXT, date TEXT, open REAL, high REAL, '
           'low REAL, close REAL, vol REAL, amount REAL, pct REAL)')
rows = [
    # 甲：10-15 收盘 12.0 → 10-16 开 11.5 / 收 11.5（收盘口径与开盘即走都是 -4.17%）
    ('600001', '2026-10-13', 10.0, 10.0, 10.0, 10.0, 0, 0, 0),
    ('600001', '2026-10-14', 10.0, 11.0, 10.0, 11.0, 0, 0, 0),
    ('600001', '2026-10-15', 11.0, 12.0, 11.0, 12.0, 0, 0, 0),
    ('600001', '2026-10-16', 11.5, 12.0, 11.0, 11.5, 0, 0, 0),
    # 乙：库里只到 10-14，10-15 那天没有它 → 必须落"当天日K缺失"
    ('600002', '2026-10-13', 5.0, 5.0, 5.0, 5.0, 0, 0, 0),
    ('600002', '2026-10-14', 5.0, 5.0, 5.0, 5.0, 0, 0, 0),
    # 丙：能成熟（收→收 0.00%），但快照价 20 与复权收盘 12 差 40% → 除权漂移，只许进主口径
    ('600003', '2026-10-13', 10.0, 10.0, 10.0, 10.0, 0, 0, 0),
    ('600003', '2026-10-14', 10.0, 11.0, 10.0, 11.0, 0, 0, 0),
    ('600003', '2026-10-15', 11.0, 12.0, 11.0, 12.0, 0, 0, 0),
    ('600003', '2026-10-16', 12.0, 12.5, 11.5, 12.0, 0, 0, 0),
]
kc.executemany('INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?)', rows)
kc.commit()
kc.close()

# ---- 影子日志：10-15 有 kept（真名单），10-16 没有 kept（老日志，只能反推）
conn = get_db_conn()
conn.execute('''CREATE TABLE IF NOT EXISTS recommend_shadow_log (
    rec_date TEXT NOT NULL, slot TEXT NOT NULL, cycle TEXT, min_score INTEGER,
    payload TEXT, PRIMARY KEY (rec_date, slot))''')
p1 = {'cycle': '分歧', 'liveMinScore': 60, 'shadowMinScore': 65,
      'live': [_item('sh600001', '甲', 8.0, 70, '半山腰')],
      'shadow': [_item('sh600001', '甲', 8.0, 70, '半山腰')],
      'kept': [_item('sh600001', '甲', 8.0, 70),                    # 放行但没挂矩阵标签
               _item('sh600002', '乙', 1.0, 68),
               _item('sh600003', '丙', 3.0, 66)],
      'excluded': [_item('sh600002', '乙', 1.0, 60, '低吸',
                         reasons=['低于分歧门槛65（现60分）'])]}
p2 = {'cycle': '退潮', 'liveMinScore': 60, 'shadowMinScore': 68,
      'live': [_item('sh600001', '甲', 2.0, 66, '中性'),
               _item('sh600002', '乙', 3.0, 61, '中性')],
      'shadow': [_item('sh600001', '甲', 2.0, 66, '中性')],
      'excluded': [_item('sh600002', '乙', 3.0, 61, '中性',
                         reasons=['低于退潮门槛68（现61分）'])]}
for d, slot, pl in (('2026-10-15', '14:45', p1), ('2026-10-15', '09:45', p1),
                    ('2026-10-16', '14:45', p2)):
    conn.execute('INSERT INTO recommend_shadow_log VALUES (?,?,?,?,?)',
                 (d, slot, pl['cycle'], pl['shadowMinScore'],
                  json.dumps(pl, ensure_ascii=False)))
# 快照价：甲 12.0（与复权同尺度，进批次价口径）；丙 20.0（漂移 40%，只进主口径）
conn.execute('''CREATE TABLE IF NOT EXISTS strategy_snapshot (
    snap_date TEXT, batch TEXT, symbol TEXT, price REAL, score REAL, stage TEXT)''')
conn.execute("INSERT INTO strategy_snapshot VALUES ('2026-10-15','14:45','sh600001',12.0,70,'candidate')")
conn.execute("INSERT INTO strategy_snapshot VALUES ('2026-10-15','14:45','sh600003',20.0,66,'candidate')")
conn.commit()
conn.close()

import importlib.util                                   # noqa: E402
_spec = importlib.util.spec_from_file_location(
    'shadow_eval', os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'scripts', 'shadow_eval.py'))
SE = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(SE)

print('\n[1] 批次选择：默认只取当天最后批次')
last = SE.load_logs(get_db_conn())
ck(len(last) == 2 and {x['slot'] for x in last} == {'14:45'},
   f'两天各一条 14:45（实得 {[(x["rec_date"], x["slot"]) for x in last]}）')
ck(len(SE.load_logs(get_db_conn(), slot_last=False)) == 3, '--all-slots 时 3 条')
ck(len(SE.load_logs(get_db_conn(), days=1)) == 1, 'days=1 只留最近一个交易日')

print('\n[2] 放行名单来源：payload 是真名单，老日志只能反推')
kept1, src1 = SE._kept_list(p1)
kept2, src2 = SE._kept_list(p2)
ck(src1 == 'payload' and len(kept1) == 3, f'真名单原样取（{len(kept1)} 只）')
ck(src2 == '推导' and {x['symbol'] for x in kept2} == {'sh600001'},
   f'反推＝影子Top3∪线上Top3−被挡（实得 {[x["symbol"] for x in kept2]}）')

print('\n[3] 三套口径各算各的，未成熟/缺失/漂移不许进胜率')
d = SE.evaluate()
g = d['groups']
ck(d['available'], '能读到影子日志')
ck(g['影子放行']['close']['n'] == 2,
   f'放行 3 只：甲、丙成熟，乙"当天日K缺失"（实得 {g["影子放行"]["close"]["n"]}）')
ck(abs(g['影子放行']['close']['avg'] - (-2.08)) < 0.02,
   f'主口径 收盘→收盘 = 甲 -4.17% / 丙 0.00% 的均值 -2.08%'
   f'（实得 {g["影子放行"]["close"]["avg"]}）')
ck(abs(g['影子放行']['open']['avg'] - (-2.08)) < 0.02,
   f'开盘即走：次日开盘同样 -4.17% / 0.00%（实得 {g["影子放行"]["open"]["avg"]}）')
ck(g['影子放行']['batch']['n'] == 1 and abs(g['影子放行']['batch']['avg'] + 4.17) < 0.02,
   f'批次价口径只剩甲（丙被漂移剔除）：{g["影子放行"]["batch"]}')
ck(d['gap'].get('当天日K缺失') == 4,
   f'乙两天都没有当天那根日K，在放行/被挡/线上各组各计一次（实得 {d["gap"].get("当天日K缺失")}）')
ck(d['gap'].get('批次价口径复权漂移(只进主口径)') == 1,
   f'丙 快照价20 vs 复权收盘12 → 漂移剔除（实得 {d["gap"]}）')
ck(d['gap'].get('样本未成熟(T+1 还没到)'),
   '10-16 那批的 T+1 是下周一、还没发生 → 计未成熟，绝不当 0% 算')
ck('矩阵标签读时补算(无当日涨停家数)' in d['gap'],
   f'放行名单没标签 → 读时补算并计数（实得 {d["gap"].get("矩阵标签读时补算(无当日涨停家数)")}）')
ck(d['snapCoverage'] == 4,
   f'批次价覆盖只数真有快照的事件（甲在放行/线上/影子三组各一次 + 丙一次；'
   f'10-16 那批没快照）：实得 {d["snapCoverage"]}')

print('\n[4] 判定只认真名单的天数，而且事件数不够也不许下结论')
ck(d['days'] == 2 and d['realKeptDays'] == 1,
   f'2 个交易日里只有 1 天有真放行名单（实得 {d["realKeptDays"]}/{d["days"]}）')
ck(d['enough'] is False and d['verdict'] == '样本不足',
   f'不足 SHADOW_EVAL_MIN_DAYS={d["minDays"]} 天 → 不给结论（实得 {d["verdict"]}）')
ck(d['needMore'] == d['minDays'] - 1, f'还差的天数按真名单算（实得 {d["needMore"]}）')
ck(d['keptSource'].get('payload') == 1 and d['keptSource'].get('推导') == 1,
   f'来源计数对得上（实得 {d["keptSource"]}）')
ck(d['groups']['线上独有']['n'] == 1 and d['groups']['影子多推']['n'] == 0,
   f'差集：10-16 线上有乙、影子没有（实得 独有{d["groups"]["线上独有"]["n"]}/'
   f'多推{d["groups"]["影子多推"]["n"]}）')
c1, k1 = get_db_conn(), sqlite3.connect(cfg.KLINE_HISTORY_DB_PATH)
ev, gp, ks = SE.build_events(SE.load_logs(c1), c1, k1)
d2 = SE.summarize(ev, gp, 2, ks, min_days=1)


def _n(grp):
    s = (d2['groups'].get(grp) or {}).get('close')
    return 0 if not s else s['n']


ck(d2['realKeptDays'] == 1 and d2['enough'] is False,
   f'把天数门槛降到 1 仍不下结论：放行 {_n("影子放行")} 次 / 被挡 {_n("影子挡掉")} 次，'
   f'两组各要 ≥{SE.MIN_EVENTS} 次（用例里被挡那只是"当天日K缺失"的乙，成熟样本 0 次）')
c1.close()
k1.close()

print('\n[5] 判定闸门本身（同样的数，四道闸门各挡一次）')
GOOD = {'n': 25, 'win': 60.0, 'avg': 1.0, 'med': 1.0, 'worst': -5.0, 'best': 6.0}
BAD = {'n': 25, 'win': 40.0, 'avg': -1.0, 'med': -1.0, 'worst': -8.0, 'best': 5.0}
a = SE._verdict(GOOD, BAD, 10, 10, 10)
ck(a[0] is True and a[1].startswith('支持上线'), f'放行优于被挡 → 支持上线（实得 {a}）')
b = SE._verdict(BAD, GOOD, 10, 10, 10)
ck(b[0] is True and b[1].startswith('不支持'), f'被挡反而更好 → 不支持（实得 {b}）')
c = SE._verdict(GOOD, dict(GOOD, win=61.0, avg=1.0), 10, 10, 10)
ck(c[0] is True and c[1].startswith('方向不明'), f'均值同、胜率反向 → 明说方向不明（实得 {c}）')
ck(SE._verdict(GOOD, BAD, 3, 10, 10) == (False, '样本不足'),
   '真名单天数 3 < 10 → 同样的数也不给结论')
ck(SE._verdict(dict(GOOD, n=5), BAD, 10, 10, 10) == (False, '样本不足'),
   f'放行只有 5 次（<{SE.MIN_EVENTS}）→ 一根票就能拧方向，不给结论')

print('\n[6] 每日落库：幂等 + 读回')
one = SE.record_daily()
ck(one.get('available'), '落库跑通')
SE.record_daily()
h = SE.history()
ck(len(h) == 1, f'同一天重复跑不翻倍（实得 {len(h)} 行）')
ck(h[0]['verdict'] == '样本不足' and h[0]['real_days'] == 1,
   f'表里带真名单天数与判定（实得 {h[0]["verdict"]}/{h[0]["real_days"]}）')

print('\n[7] 空库不炸（星期一之前它就该是这个样子）')
c2 = get_db_conn()
c2.execute('DELETE FROM recommend_shadow_log')
c2.execute('DELETE FROM recommend_shadow_eval')
c2.commit()
c2.close()
d4 = SE.evaluate()
ck(d4['available'] is False and d4['verdict'] == '没有影子日志',
   f'没日志时说"没有"，不编数（实得 {d4["verdict"]}）')
ck(SE.history() == [], '没落库时 history 返回空列表')

print()
print('全部通过' if not FAILS else f'失败 {len(FAILS)} 项: {FAILS}')
sys.exit(0 if not FAILS else 1)
