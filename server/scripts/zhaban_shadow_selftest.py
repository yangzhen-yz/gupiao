"""zhaban_shadow_selftest.py - 炸板次数影子的回归自测（2026-11-06，影子台账第 17 条）

守的不变量：**"分时自算炸板"这套口径必须是"在板→离板的转换次数"，而且绝不允许
在没有东财真值、或分时日期不对的情况下把影子写进表**（前者会被误当成"已接成备源"，
后者会拿昨天的走势算今天的炸板）。

分组与反证：
  [1] 段/次数的区别：封-开-封-开-封 是 3 段 2 次炸板 —— 把次数写成段数，第 5 条必红；
  [2] 在板容差（±0.001 内算封住，差一分不算）；
  [3] 涨停幅度自动判（ST 5 优先于板块、创业/科创 20、北证 30、主板 10）与 Decimal 式舍入；
  [4] 昨收必须严格早于目标日：库里已有今天那根时，取的是昨天，不是今天（少 10% 的那个坑）；
  [5] run_day 端到端：两行并排落库、`zbc_em` 与 `zbc_tx` 都在；
  [6] 分时日期与目标日不符 → 该只跳过，一条不写；
  [7] 池子 0 行（pools 家族停调日）→ 跳过且**一个腾讯请求都不发**；
  [8] 开关关掉 → 不建表、不写行，逐字节等于改造前；
  [9] evaluate：天数不足必须 ready=False，不许拿 3 天样本出结论。
全程临时库 + 假 urllib + 假池子，真库与真腾讯一个字节都没碰。
"""
import json
import os
import sqlite3
import sys
import tempfile
import types
import urllib.request

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

_TMP = os.path.join(tempfile.gettempdir(), f'zbc_shadow_{os.getpid()}.db')
_KLINE = os.path.join(tempfile.gettempdir(), f'zbc_shadow_kline_{os.getpid()}.db')
for _p in (_TMP, _KLINE):
    for _suf in ('', '-wal', '-shm'):
        try:
            os.remove(_p + _suf)
        except OSError:
            pass

import db.database as dbmod                            # noqa: E402
dbmod.use_db_path(_TMP)      # 换库只许走这个函数（11-05 拆分后直接赋值 SQLITE_DB_PATH 静默无效）
dbmod.init_db()

import app.config as cfg                               # noqa: E402
cfg.KLINE_HISTORY_DB_PATH = _KLINE                     # 昨收取的是日K库，也指到临时文件
from services.data import limit_pool as lp             # noqa: E402
from services.data import zhaban_shadow as zs          # noqa: E402

PASS, FAIL = [], []
CALLS = {'minute': 0, 'quote': 0}


def ck(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(f"  [{'✓' if cond else '✗'}] {msg}")


def bars(seq):
    """价格序列 → 分时 bars，分钟从 09:30 起一根一根排。"""
    out, m = [], 0
    for off in seq:
        total = 9 * 60 + 30 + m
        out.append({'t': f'{total // 60:02d}{total % 60:02d}', 'price': round(off, 3)})
        m += 1
    return out


# ========== [1] 段数 vs 炸板次数 ==========
print('[1] compute_from_bars：炸板次数＝在板→离板的转换次数（不是段数）')
LIM = 11.0
one = bars([LIM] * 60)                                    # 全天封死
ck(zs.compute_from_bars(one, LIM)['zbc_tx'] == 0, '一字/全天封住 → 0 次炸板')
ck(zs.compute_from_bars(one, LIM)['seg_tx'] == 1, '全天封住 → 1 段')
ck(zs.compute_from_bars(one, LIM)['sealed_close_tx'] == 1, '全天封住 → 收盘在板上')

two = bars([LIM] * 30 + [10.8] * 20 + [LIM] * 10)         # 封→开→回封
r2 = zs.compute_from_bars(two, LIM)
ck(r2['zbc_tx'] == 1 and r2['seg_tx'] == 2, f'封-开-回封 = 1 次炸板 / 2 段，实得 {r2}')
ck(r2['sealed_close_tx'] == 1, '回封后收盘算封住')

three = bars([LIM] * 20 + [10.5] * 15)                    # 封→开，没回封
r3 = zs.compute_from_bars(three, LIM)
ck(r3['zbc_tx'] == 1 and r3['seg_tx'] == 1 and r3['sealed_close_tx'] == 0,
   f'封-开不回封 = 1 次炸板、收盘未封住，实得 {r3}')

never = bars([10.2] * 40)
r4 = zs.compute_from_bars(never, LIM)
ck(r4['zbc_tx'] == 0 and r4['seg_tx'] == 0 and r4['touched_tx'] == 0, '从未摸板 → 全 0')

quad = bars([LIM] * 10 + [10.6] * 10 + [LIM] * 10 + [10.7] * 10 + [LIM] * 10)
r5 = zs.compute_from_bars(quad, LIM)
ck(r5['zbc_tx'] == 2 and r5['seg_tx'] == 3,
   f'封-开-封-开-封 = 2 次炸板 / 3 段（写成段数就会得 3 → 这条就是反证），实得 {r5}')
ck(r5['first_touch_tx'] == '0930' and r5['last_break_tx'] == '1000',
   f'首触 0930 / 最后一次翻下 1000，实得 {r5["first_touch_tx"]} / {r5["last_break_tx"]}')

# ========== [2] 在板容差 ==========
print('[2] 容差：涨停价 −0.0005 算在板，−0.01 不算')
ck(zs.compute_from_bars(bars([LIM - 0.0005] * 5), LIM)['seg_tx'] == 1, '−0.0005 视为封住')
ck(zs.compute_from_bars(bars([LIM - 0.01] * 5), LIM)['seg_tx'] == 0, '−0.01 视为没封住')

# ========== [3] 涨停幅度与舍入 ==========
print('[3] pct_for 与 limit_up_price')
ck(zs.pct_for('sh600000', '浦发银行') == 10.0, '主板 10%')
ck(zs.pct_for('sz300308', '中际旭创') == 20.0, '创业板 20%')
ck(zs.pct_for('sh688001', '某科创') == 20.0, '科创板 20%')
ck(zs.pct_for('bj920000', '安徽凤凰') == 30.0, '北证 30%')
ck(zs.pct_for('sh600555', 'ST退股') == 5.0, 'ST 5% 优先于板块')
ck(zs.limit_up_price(3.37, 10.0) == 3.71, f'3.37×1.1=3.707→3.71（到分），实得 {zs.limit_up_price(3.37)}')
ck(zs.limit_up_price(10.0, 10.0) == 11.0, '10.00→11.00')
ck(zs.limit_up_price(9.87, 10.0) == 10.86, f'昨收 9.87 → 10.86，实得 {zs.limit_up_price(9.87)}')

# ========== 假 HTTP：分时 + 批量行情（昨收）==========
_POOL = [
    {'pool': 'zt', 'symbol': '600000', 'name': '测试甲', 'zhaban': 1},
    {'pool': 'zb', 'symbol': '600555', 'name': 'ST测试乙', 'zhaban': 3},
]
_MIN = {'sh600000': ('20261106', [11.0] * 30 + [10.8] * 10 + [11.0] * 10),   # 1 次炸板
        'sh600555': ('20261106', [10.9] * 20 + [10.4] * 5)}                   # 摸过又掉


def _fake_payload(code, prices, date='20261106'):
    rows = []
    for i, p in enumerate(prices):
        total = 9 * 60 + 30 + i
        rows.append(f'{total // 60:02d}{total % 60:02d} {p} {100 + i} 1000000.0')
    return {'data': {code: {'data': {'date': date, 'data': rows}}}}


_real_urlopen = urllib.request.urlopen

# 假腾讯批量行情：三列故意不同值（3=现价 / 4=昨收 / 5=今开），取错列就红
_PREV_LINE = {'sh600000': '1~测试甲~600000~10.03~9.87~9.91',
              'sh600555': '1~ST测试乙~600555~10.45~10.00~10.05'}


def fake_urlopen(req, timeout=None, *a, **kw):
    url = req if isinstance(req, str) else getattr(req, 'full_url', str(req))
    if 'qt.gtimg.cn' in url:                       # 昨收批量（11-06 起不再读本地日K）
        CALLS['quote'] += 1
        codes = url.split('q=')[-1].split(',')
        body = ';'.join(f'v_{c}="{_PREV_LINE[c]}"' for c in codes if c in _PREV_LINE)
        return types.SimpleNamespace(read=lambda: (body + ';').encode('gbk'))
    CALLS['minute'] += 1
    code = url.split('code=')[-1].split('&')[0]
    date, prices = _MIN.get(code, ('', []))
    payload = _fake_payload(code, prices, date) if prices else {'data': {}}
    return types.SimpleNamespace(read=lambda: json.dumps(payload).encode('utf-8'))


urllib.request.urlopen = fake_urlopen
_real_load_day = lp.load_day
lp.load_day = lambda day, kind=None: [dict(x) for x in _POOL]

# ========== [4] 昨收来源：腾讯行情的"昨收"列，不是本地日K ==========
print('[4] 昨收取腾讯批量第 5 列（现价/昨收/今开三列故意不同值，取错列必红）')
kc = sqlite3.connect(_KLINE)
kc.execute('CREATE TABLE stock_daily (symbol TEXT, date TEXT, close REAL, '
           'PRIMARY KEY(symbol, date))')
kc.executemany('INSERT INTO stock_daily VALUES (?,?,?)',
               [('600000', '2026-10-15', 12.34),        # 库里**只有这根三周前的陈旧值**
                ('600555', '2026-10-16', 8.88)])
kc.commit()
kc.close()
pc = zs.prev_closes(['600000', '600555'])
ck(pc == {'600000': 9.87, '600555': 10.00},
   f'昨收 = 9.87 / 10.00（误取现价列会是 10.03、今开列 9.91、日K陈旧列 12.34）实得 {pc}')
ck(zs.limit_up_price(pc['600000'], 10.0) == 10.86,
   f'600000 涨停价 10.86（用日K那根会算成 13.57）实得 {zs.limit_up_price(pc["600000"], 10.0)}')
ck(zs.prev_closes(['999999']) == {}, '行情没给这只票 → 返回空，不许瞎猜昨收')
ck(zs._prefixed('600000') == 'sh600000' and zs._prefixed('000001') == 'sz000001'
   and zs._prefixed('920000') == 'bj920000', '前缀归一：6→sh / 0→sz / 9(4/8)→bj')
ck(zs.pct_for('920000', '安徽凤凰') == 30.0, '裸码 920 开头也认成北证 30%（池子存的是裸码）')

# ========== [5] run_day 端到端 ==========
print('[5] run_day：与东财真值并排落库')
r5 = zs.run_day('2026-11-06')
conn = sqlite3.connect(_TMP)
rows = conn.execute('SELECT symbol,pool,zbc_em,zbc_tx,seg_tx,sealed_close_tx,prec,lim_price,'
                    'pct,minutes,bar_date FROM zhaban_shadow ORDER BY symbol').fetchall()
conn.close()
ck(r5.get('written') == 2, f'落库 2 行，实得 {r5}')
ck(len(rows) == 2, f'表里 2 行，实得 {len(rows)}')
by = {x[0]: x for x in rows}
ck(by['600000'][2] == 1 and by['600000'][3] == 1,
   f'测试甲 东财 1 次 / 自算 {by["600000"][3]} 次（两列并排存着才能对照）')
ck(by['600000'][4] == 2 and by['600000'][5] == 1, '测试甲 2 段、收盘封住')
ck(by['600555'][2] == 3 and by['600555'][3] == 1,
   f'测试乙 东财报 3 次、分时只能看到 1 次（这就是本影子要量的头号偏差，不许偷偷抹平）')
ck(by['600000'][6] == 9.87 and by['600000'][7] == 10.86,
   f'用的昨收/涨停价 = {by["600000"][6]} / {by["600000"][7]}')
ck(by['600000'][8] == 10.0 and by['600555'][8] == 5.0, 'ST 那只用 5% 档')
ck(by['600000'][10] == '20261106', 'bar_date 一并存着，事后可审')

# ========== [6] 分时日期不对 → 宁缺毋假 ==========
print('[6] 分时返回的日期不是目标日 → 该只跳过，一条都不写')
_MIN['sh600000'] = ('20261105', [11.0] * 40)          # 模拟拿到昨天的分时
conn = sqlite3.connect(_TMP)
conn.execute('DELETE FROM zhaban_shadow WHERE symbol=\'600000\'')
conn.commit()
conn.close()
r6 = zs.run_day('2026-11-06')
conn = sqlite3.connect(_TMP)
has600 = conn.execute('SELECT 1 FROM zhaban_shadow WHERE symbol=\'600000\'').fetchone()
n605 = conn.execute('SELECT COUNT(*) FROM zhaban_shadow WHERE symbol=\'600555\'').fetchone()[0]
conn.close()
ck(has600 is None, '拿旧分时的 600000 没被写进去')
ck(n605 == 1, f'另一只 600555 照常落库（一条坏数据不许连坐），实得 {n605}')
ck(r6.get('netFail', 0) >= 1, f'失败计数里有它，实得 {r6}')

# ========== [7] 池子 0 行 → 跳过且零请求 ==========
print('[7] 东财三池当天 0 行（pools 家族停调日）→ 跳过，且一个腾讯请求都不发')
lp.load_day = lambda day, kind=None: []
CALLS['minute'] = CALLS['quote'] = 0
r7 = zs.run_day('2026-11-06')
ck('skipped' in r7 and 'limit_pool_daily' in r7['skipped'], f'实得 {r7}')
ck(CALLS['minute'] == 0 and CALLS['quote'] == 0,
   f'零分时请求、零行情请求（没有真值的对照没有裁决价值），实得 {CALLS}')
lp.load_day = lambda day, kind=None: [dict(x) for x in _POOL]

# ========== [8] 开关关掉 = 改造前 ==========
print('[8] ZHABAN_SHADOW_ENABLED=False → 不建表、不写行、零请求')
conn = sqlite3.connect(_TMP)
conn.execute('DROP TABLE zhaban_shadow')
conn.commit()
conn.close()
cfg.ZHABAN_SHADOW_ENABLED = False
CALLS['minute'] = CALLS['quote'] = 0
r8 = zs.run_day('2026-11-06')
conn = sqlite3.connect(_TMP)
tbl = conn.execute("SELECT name FROM sqlite_master WHERE name='zhaban_shadow'").fetchone()
conn.close()
ck(r8.get('skipped') == '开关关闭', f'实得 {r8}')
ck(CALLS['minute'] == 0, '零请求')
ck(tbl is None, '表根本没被创建')
cfg.ZHABAN_SHADOW_ENABLED = True

# ========== [9] evaluate：天数不足不结论 ==========
print('[9] evaluate：只有 1 天数据时必须 ready=False')
r9 = zs.evaluate(min_days=20)
ck(r9['ready'] is False, f'天数不足不结论，实得 ready={r9["ready"]} days={r9["days"]}')
ck(zs.enabled() is True, '开关恢复为默认开')

urllib.request.urlopen = _real_urlopen
lp.load_day = _real_load_day
print(f'\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败')
if FAIL:
    for m in FAIL:
        print('  失败：', m)
    sys.exit(1)
print('临时库：', _TMP, '/', _KLINE, '（真库与真腾讯未触碰）')
