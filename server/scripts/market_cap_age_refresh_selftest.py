"""market_cap_age_refresh_selftest.py - 市值"按年龄刷新"口径的回归自测（2026-11-07，修③）

守的不变量：**周刷新必须真的每周换水，而不是"表里有这只票就算刷新过了"**。
老默认 `only_missing=True` 只补"库里没有的票"，从不管库里的数多旧 ⇒ 2026-10-16 灌满 3052 只
之后，每一轮都走"目标清单已全部在表，无需刷新"返回 0，市值表从此冻结（实测 `updated_date`：
3052 行停在 10-16、2 行 11-01）。市值＝股本×当天价，用它做全链路第一道闸，冻结就等于
一边剔掉"已涨回 35 亿以上"的票、一边放行"已跌破下限"的票。

分组（每条都注明"写错什么会让它红"）：
  [1] 默认口径＝缺失或超龄：新鲜/超龄/缺失三只混在一起，只应拉后两只（年龄比较写反会反过来）
  [2] 单轮上限按**最旧优先**截断（排序写错会拉错那只）
  [3] 开关 `MARKET_CAP_AGE_REFRESH=False` → 默认调用逐字节回到"只补缺失"
  [4] 显式 `only_missing=True` 保持老语义（有超龄也不管）
  [5] 显式 `only_missing=False` 保持老语义（全表重拉，不许被年龄口径悄悄改掉）
  [6] 全部新鲜时零请求、直接返回（这是老代码唯一的正确行为，别改成每周猛拉 3000 次）
  [7] 跑完 `updated_date` 真的变成今天（否则"刷新成功"只是日志漂亮）
  [8] 新股（库里没有）在默认口径下必须被纳入——这正是 10-16 之后一直没补上的那批
全程临时库 + 假东财 + 假新浪（顺带证明默认路径不会误走新浪备源），真库零接触。
"""
import os
import sqlite3
import sys
import tempfile
import types
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

_TMP = os.path.join(tempfile.gettempdir(), f'mcap_age_{os.getpid()}.db')
for _suf in ('', '-wal', '-shm'):
    try:
        os.remove(_TMP + _suf)
    except OSError:
        pass

import app.config as cfg                        # noqa: E402
from services.data import market_cap as mc       # noqa: E402
from services.data import eastmoney_guard        # noqa: E402

mc.KLINE_HISTORY_DB_PATH = _TMP
mc._CAP_CACHE = None
TODAY = datetime.now().strftime('%Y-%m-%d')
OLD = (datetime.now() - timedelta(days=25)).strftime('%Y-%m-%d')
MID = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
NEWISH = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')

PASS, FAIL = [], []
PULLED, SINA = [], []


def ck(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(f"  [{'✓' if cond else '✗'}] {msg}")


def seed(rows, cap=9.0e9):
    """rows: [(symbol, updated_date)]；市值默认给一个远大于下限的值，免得过滤层干扰本测试。"""
    conn = sqlite3.connect(_TMP)
    conn.execute(mc._DDL)
    for s, d in rows:
        c = cap.get(s, cap) if isinstance(cap, dict) else cap
        conn.execute('INSERT OR REPLACE INTO stock_market_cap '
                     '(symbol,name,total_cap,float_cap,total_shares,updated_date) '
                     'VALUES (?,?,?,?,?,?)', (s, s + '名', c, 0.8 * c, 1.0e10, d))
    conn.commit()
    conn.close()


def clear_tbl():
    conn = sqlite3.connect(_TMP)
    conn.execute(mc._DDL)
    conn.execute('DELETE FROM stock_market_cap')
    conn.commit()
    conn.close()


# 假东财：只记录被拉了谁，返回一个换算前口径的元值
_real_fetch = mc._fetch_one


def fake_fetch(code6, retries=2):
    PULLED.append(code6)
    return (code6, code6 + '名', 7.0e9, 6.0e9, 1.0e10)


mc._fetch_one = fake_fetch
# 假新浪：默认路径不该碰它
mc.requests = types.SimpleNamespace(get=lambda *a, **k: (_ for _ in ()).throw(
    AssertionError('默认路径不应发新浪请求')))
_real_skip = eastmoney_guard.skip
eastmoney_guard.skip = lambda scope, *a, **k: False      # 东财可用

SYMS = ['sh600001', 'sh600002', 'sh600003', 'sh600009']   # 600009 = 库里没有的新股


def run(symbols=None, **kw):
    PULLED.clear()
    return mc.refresh(symbols=symbols or SYMS, only_missing=kw.pop('only_missing', None),
                      max_age_days=kw.pop('max_age_days', None),
                      max_per_run=kw.pop('max_per_run', None), **kw)


try:
    mc.MARKET_CAP_AGE_REFRESH = True
    mc.MARKET_CAP_REFRESH_AGE_DAYS = 7
    mc.MARKET_CAP_MAX_PER_RUN = 1600

    # ========== [1] 默认口径 ==========
    print('[1] 默认调用＝缺失或超龄（新鲜那只不该被拉）')
    clear_tbl()
    seed([('600001', NEWISH), ('600002', OLD), ('600003', MID)])
    r1 = run()
    ck(sorted(PULLED) == ['600002', '600003', '600009'],
       f'待补＝超龄 2 只 + 缺失 1 只，实得 {sorted(PULLED)}')
    ck('600001' not in PULLED, '两天前刚刷新的那只不重复拉（年龄比较写反就会拉它、漏掉超龄的）')

    # ========== [2] 上限按最旧优先 ==========
    print('[2] 单轮上限：只拉最旧的那些，剩下的下一轮')
    clear_tbl()
    seed([('600001', NEWISH), ('600002', OLD), ('600003', MID), ('600009', '')])
    r2 = run(max_per_run=2)
    ck(len(PULLED) == 2, f'本轮恰好 2 只，实得 {len(PULLED)}')
    ck('600002' in PULLED, '最旧（25 天前）那只必在本轮')
    ck('600001' not in PULLED, '新鲜的绝不插队')
    ck('skipped' not in r2, f'走的是正常拉取分支，实得 {r2}')

    # ========== [3] 开关关掉回到老行为 ==========
    print('[3] MARKET_CAP_AGE_REFRESH=False → 默认调用＝只补缺失（逐字节回到改造前）')
    mc.MARKET_CAP_AGE_REFRESH = False
    clear_tbl()
    seed([('600001', NEWISH), ('600002', OLD), ('600003', MID)])
    r3 = run()
    ck(sorted(PULLED) == ['600009'], f'只拉库里没有的那只，实得 {sorted(PULLED)}')
    mc.MARKET_CAP_AGE_REFRESH = True

    # ========== [4]/[5] 显式传参保持老语义 ==========
    print('[4] 显式 only_missing=True：有超龄也不管（老语义）')
    clear_tbl()
    seed([('600001', NEWISH), ('600002', OLD)])
    r4 = run(only_missing=True)
    # SYMS 有四只、库只种了两只 ⇒ "只补缺失"本来就该补两只（600003/600009），
    # 而库里那只 25 天前的 600002 **不补**——这才是"老语义不受年龄口径影响"的意思。
    ck(sorted(PULLED) == ['600003', '600009'],
       f'只补缺失的两只（超龄那只照旧不管），实得 {sorted(PULLED)}')

    print('[5] 显式 only_missing=False：全表重拉（不许被年龄口径改掉老语义）')
    clear_tbl()
    seed([('600001', NEWISH), ('600002', OLD), ('600003', MID)])
    r5 = run(only_missing=False)
    ck(sorted(PULLED) == ['600001', '600002', '600003', '600009'],
       f'四只全拉（含新鲜的），实得 {sorted(PULLED)}')

    # ========== [6] 全部新鲜 → 零请求 ==========
    print('[6] 全部新鲜且不缺 → 零请求（别每周猛拉 3000 次）')
    clear_tbl()
    seed([('600001', NEWISH), ('600002', NEWISH), ('600003', NEWISH), ('600009', NEWISH)])
    r6 = run()
    ck(PULLED == [], f'本轮不发请求，实得 {PULLED}')
    ck(r6.get('ok') == 0 and r6.get('total') == 4, f'返回仍是老的 no-op 口径，实得 {r6}')

    # ========== [7] updated_date 真的变今天 ==========
    print('[7] 跑完之后 updated_date 必须落到今天（不然"刷新成功"只是日志好看）')
    clear_tbl()
    seed([('600001', NEWISH), ('600002', OLD)])
    run()
    conn = sqlite3.connect(_TMP)
    got = dict(conn.execute('SELECT symbol, updated_date FROM stock_market_cap').fetchall())
    conn.close()
    ck(got.get('600002') == TODAY, f'超龄那只已刷新到今天，实得 {got.get("600002")}')
    ck(got.get('600009') == TODAY, f'新股已入库，实得 {got.get("600009")}')
    ck(mc._load_updated_map().get('600001') == NEWISH, '新鲜那只没被顺手改动')

    # ========== [8] 年龄口径的边界：正好 7 天 ==========
    print('[8] 边界：恰好 7 天前**不**算超龄（严格小于 cutoff 才换水）；放宽阈值就该不拉')
    exact = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    clear_tbl()
    seed([('600001', exact)])
    mc.MARKET_CAP_REFRESH_AGE_DAYS = 7
    run(symbols=['sh600001'])
    ck(PULLED == [], f'{exact} 恰好等于阈值时**不算**超龄（判据是严格 `updated_date < 今天-7天`，'
                     f'即满 8 天才换水）——写反成 `<=` 就会每周多拉一整轮，实得 {PULLED}')
    mc.MARKET_CAP_REFRESH_AGE_DAYS = 30
    clear_tbl()
    seed([('600001', exact)])
    run(symbols=['sh600001'])
    ck(PULLED == [], f'阈值放宽到 30 天后同一只就不该拉，实得 {PULLED}')
    mc.MARKET_CAP_REFRESH_AGE_DAYS = 7

    print('[9] 阈值来自模块常量（改配置要重启才生效，不是每次调用现读——这点要写清，别以为热改有用）')
    import inspect
    sig = inspect.signature(mc.refresh)
    ck(sig.parameters['only_missing'].default is None,
       'refresh() 的 only_missing 默认值是 None（默认口径由开关决定，不是写死 True）')
    # ========== [10] 口径：数"被下限剔掉的只数"不许自指过滤 ==========
    print('[10] count_below()：只数 universe 范围内的票，且不许用 eligible_symbols 自指')
    clear_tbl()
    seed([('600001', TODAY), ('600002', TODAY)], cap={'600001': 9.0e9, '600002': 3.0e9})
    conn = sqlite3.connect(_TMP)
    conn.execute('INSERT OR REPLACE INTO stock_market_cap '
                 '(symbol,name,total_cap,float_cap,total_shares,updated_date) '
                 'VALUES (?,?,?,?,?,?)', ('300999', '300999名', 2.0e9, 1.6e9, 1.0e10, TODAY))
    conn.commit()
    conn.close()                                  # 300999＝创业板，本就被 universe 排除
    calls = []
    from services.data import universe as U       # noqa: E402
    _real_fs, _real_es = U.filter_symbols, U.eligible_symbols

    def fake_filter(symbols):
        calls.append('filter_symbols')
        return {'600001', '600002'}               # 只有这两只在选股范围内

    def fake_eligible(symbols=None):
        calls.append('eligible_symbols')
        raise AssertionError('不该在这里用 eligible_symbols')

    U.filter_symbols, U.eligible_symbols = fake_filter, fake_eligible
    try:
        n = mc.count_below()
        ck(n == 1, f'universe 口径只数范围内的 1 只（300999 不算），实得 {n}')
        ck(calls.count('filter_symbols') == 1 and 'eligible_symbols' not in calls,
           f'用的是范围过滤，不是含市值层的 eligible（用它就自指成 0），调用序列 {calls}')
    finally:
        U.filter_symbols, U.eligible_symbols = _real_fs, _real_es
    mc._CAP_CACHE = None

    # ========== [11] 闸门必须在批量循环里复查 ==========
    print('[11] 闸门在循环里复查：东财封着时不许把整轮 60 只打完（凌晨真打了 1244 次）')
    clear_tbl()
    many = [f'sh6000{i:02d}' for i in range(1, 61)]
    seed([(c[-6:], OLD) for c in many])
    calls25 = [0]

    def gate(scope, *a, **k):
        calls25[0] += 1
        return calls25[0] > 1          # 入口放行一次，到第 25 只（第二批前）闸门已关 → 该收手

    _g = eastmoney_guard.skip
    eastmoney_guard.skip = gate
    try:
        r11 = run(symbols=many, max_per_run=60)
        ck('闸门已关' in (r11.get('reason') or ''), f'返回里如实写明提前收手，实得 {r11}')
        ck(20 <= len(PULLED) <= 28,
           f'一批 25 只打完就停（不是 60 只全打完），实得 {len(PULLED)}')
        ck(r11.get('ok') and r11['ok'] > 0, f'已拿到的照常入库、不白跑，实得 {r11.get("ok")}')
        ck(r11.get('skip') and r11['skip'] > 0, f'放弃的只数如实报出，实得 {r11.get("skip")}')
    finally:
        eastmoney_guard.skip = _g

finally:
    mc._fetch_one = _real_fetch
    eastmoney_guard.skip = _real_skip

print(f'\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败')
if FAIL:
    for m in FAIL:
        print('  失败：', m)
    sys.exit(1)
print('临时库：', _TMP, '（真库未接触；假新浪在任何用例里都不该被调用）')
