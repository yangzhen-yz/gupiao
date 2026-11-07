"""market_cap_sina_selftest.py - 市值新浪备源的回归自测（2026-11-06）

守的不变量：**东财 push2 停调不能把市值刷不出数，也不能把单位刷错**。
市值下限是全链路第一道闸（候选池 / 智能推荐 / 趋势扫描 / 策略扫描都读 `stock_market_cap`），
而 11-06 之前 `refresh()` 在东财闸门关着时是**整批跳过**——表里的数就停在上一版，
新股永远补不上。现在改走新浪 `Market_Center.getHQNodeData`，风险点全在换算与覆盖上，
所以这份自测逐条盯住它们（每条都说明"什么写错会让它红"）：

  1. **单位换算**：新浪 mktcap/nmc 是万元，东财 f116/f117 是元 → 必须 ×1e4。
     反证靶：少乘一次，10.3 亿会变成 10.3 万元，35 亿下限会把这只票误剔；
  2. **total_shares 不能被抹成 NULL**：新浪没有股本字段，表里那列是东财 f84 刷的，
     直接 INSERT OR REPLACE 会把它清掉（pool_backtest 用它算市值）→ 写前先读旧值带着走；
  3. **拿不到就不落库**：mktcap 为 '-'/False/0 一律跳过，与东财 f116=0 的处理一致；
  4. **东财可用时零新浪请求**：备源只在停调时接管，正常周刷新一个新浪请求都不许多发；
  5. **开关关掉逐字节回到改造前**：MARKET_CAP_SINA_FALLBACK=False → 整批跳过、表一行不动；
  6. **全页失败不得写空表**：一页可用数据都没有时直接返回，不碰库。

全程临时库 + 假 HTTP + 假闸门（monkeypatch skip，绝不真去碰熔断状态文件），真库一个字节未动。
"""
import json
import os
import sqlite3
import sys
import tempfile
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

_TMP = os.path.join(tempfile.gettempdir(), f'mktcap_sina_{os.getpid()}.db')
for _suf in ('', '-wal', '-shm'):
    try:
        os.remove(_TMP + _suf)
    except OSError:
        pass

import db.database as dbmod                       # noqa: E402
dbmod.use_db_path(_TMP)   # 换库必须走这个函数；直接赋值 SQLITE_DB_PATH 自 11-05 拆分起静默无效
dbmod.init_db()

import app.config as cfg                          # noqa: E402
from services.data import eastmoney_guard          # noqa: E402
from services.data import market_cap as mc         # noqa: E402

mc.KLINE_HISTORY_DB_PATH = _TMP                    # 市值表在 history_kline.db，自测指到临时库
mc._CAP_CACHE = None

PASS, FAIL = [], []


def ck(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(f"  [{'✓' if cond else '✗'}] {msg}")


def q(sql, args=()):
    conn = sqlite3.connect(_TMP)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


# ========== 假 HTTP：只给"市值备源"用到的两个接口 ==========
P1 = [
    {'symbol': 'sh600000', 'name': '测试甲', 'mktcap': 103048.32, 'nmc': 64735.5717},
    {'symbol': 'sz000001', 'name': '测试乙', 'mktcap': '-', 'nmc': 1000000.0},
    {'symbol': 'sh601999', 'name': '测试丙', 'mktcap': False, 'nmc': 0},
    {'symbol': 'sh510300', 'name': '基金不该算个股', 'mktcap': 50000000.0, 'nmc': 50000000.0},
]
P2 = [{'symbol': 'sh600519', 'name': '测试丁', 'mktcap': 181636900.0, 'nmc': 181636900.0}]
PAGES = {1: P1, 2: P2}
CNT = {'sina_list': 0, 'sina_count': 0, 'broken': False}


class FakeResp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'http {self.status_code}')

    def json(self):
        return json.loads(self.text)


def fake_get(url, params=None, headers=None, timeout=None, proxies=None, **kw):
    # 计数放在最前面：broken（模拟备源全挂）分支也必须计数，否则 [8] 那条"请求数不超阈值"
    # 会因为"根本没计数"而假绿 —— 写自测时第一版就踩了这个坑，改完必须看它是几而不是看它绿。
    if 'getHQNodeStockCount' in url:
        CNT['sina_count'] += 1
    else:
        CNT['sina_list'] += 1
    if CNT['broken']:
        return FakeResp('', 500)
    if 'getHQNodeStockCount' in url:
        return FakeResp('"5578"')
    return FakeResp(json.dumps(PAGES.get(int((params or {}).get('page', 0)), [])))


mc.requests = types.SimpleNamespace(get=fake_get)

# ========== 假闸门：只改内存里的判定，不写熔断状态文件、不触发真实冷却 ==========
_real_skip = eastmoney_guard.skip
SKIP = {'v': False}
mc_skip_calls = []


def fake_skip(scope, *a, **k):
    mc_skip_calls.append(scope)
    return SKIP['v']


eastmoney_guard.skip = fake_skip

# ========== [0] 先放一行东财口径的老数据，用于验证覆盖与股本保留 ==========
print('[0] 预置：库里先有一行东财写的 600000（total_shares 有值、市值是旧版）')
conn = sqlite3.connect(_TMP)
conn.execute(mc._DDL)
conn.execute("INSERT OR REPLACE INTO stock_market_cap "
             "(symbol,name,total_cap,float_cap,total_shares,updated_date) "
             "VALUES ('600000','测试甲',500000000.0,400000000.0,29352000000.0,'2026-10-25')")
conn.commit()
conn.close()
ck(q("SELECT total_shares FROM stock_market_cap WHERE symbol='600000'")[0][0]
   == 29352000000.0, '预置的东财股本 293.52 亿股在场')

# ========== [1] 单位换算：万元 → 元 ==========
print('[1] refresh_via_sina：mktcap/nmc 必须从万元换算为元')
r1 = mc.refresh_via_sina()
row600 = q("SELECT name,total_cap,float_cap,total_shares,updated_date "
           "FROM stock_market_cap WHERE symbol='600000'")
ck(bool(row600), '600000 在表')
ck(abs(row600[0][1] - 103048.32 * 1e4) < 1, f'总市值 = 10.3048 亿（元），实得 {row600[0][1]:,.0f}')
ck(abs(row600[0][2] - 64735.5717 * 1e4) < 1, f'流通市值 = 6.4736 亿（元），实得 {row600[0][2]:,.0f}')
ck(row600[0][1] > 1e8 and row600[0][1] < 2e9, '换算后落在"亿元量级"，不是万元量级（少乘 1e4 会红）')

# ========== [2] total_shares 不能被抹 ==========
print('[2] 新浪无股本字段：东财刷进来的 total_shares 必须原样保留')
ck(row600[0][3] == 29352000000.0, f'股本仍是 293.52 亿，实得 {row600[0][3]}')
ck(row600[0][4] != '2026-10-25', f'updated_date 已刷新（{row600[0][4]}）')

# ========== [3] 脏数据不落库、茅台量级对得上实测 ==========
print('[3] mktcap 为 "-" / false / 0 的票不得入库；大市值票数量级要正确')
ck(not q("SELECT 1 FROM stock_market_cap WHERE symbol='000001'"), "mktcap='-' 的 000001 未落库")
ck(not q("SELECT 1 FROM stock_market_cap WHERE symbol='601999'"), 'mktcap=false 的 601999 未落库')
mt = q("SELECT total_cap FROM stock_market_cap WHERE symbol='600519'")
ck(bool(mt) and abs(mt[0][0] - 1.816369e12) < 1e6,
   f'茅台式数值 = 1.816 万亿（元），实得 {mt[0][0]:,.0f}' if mt else '茅台式数值没落库')
ck(r1['ok'] == 3, f'本轮入库 3 只（600000 / 510300 / 600519；两条脏数据被剔），实得 ok={r1["ok"]}')
ck(len(q("SELECT symbol FROM stock_market_cap WHERE symbol='510300'")) == 1,
   '场内基金 510300 也会入库（备源按新浪清单全刷，是否进选票由 universe 口径把关，不在此处剔）')

# ========== [4] 页数换算 ==========
print('[4] 页数：总数 5578 / 100 → 57 页（含一页余量），不超上限 70')
ck(mc._sina_total_pages() == 57, f'实得 {mc._sina_total_pages()}')

# ========== [5] 东财可用时：零新浪请求，走老路 ==========
print('[5] 东财可用（skip=False）→ 一个新浪请求都不许多发，仍走东财单票')
CNT['sina_list'] = CNT['sina_count'] = 0
SKIP['v'] = False
_real_fetch = mc._fetch_one
mc._fetch_one = lambda code, retries=2: (code, '东财名', 7.0e9, 6.0e9, 1.0e10)
r5 = mc.refresh(symbols=['sh600900'], only_missing=False)
ck(CNT['sina_list'] == 0 and CNT['sina_count'] == 0, f'新浪请求数 0（实得 list={CNT["sina_list"]}）')
ck(q("SELECT total_cap FROM stock_market_cap WHERE symbol='600900'")[0][0] == 7.0e9,
   '东财口径（元）直接落库，不乘 1e4')
mc._fetch_one = _real_fetch

# ========== [6] 停调 + 开关开 → 走新浪 ==========
print('[6] 东财停调 + 开关开 → 改走新浪（refresh 入口分流）')
SKIP['v'] = True
CNT['sina_list'] = 0
r6 = mc.refresh(symbols=['sh600000', 'sh600519'], only_missing=False)
ck(r6.get('source') == 'sina', f'refresh 返回 source=sina，实得 {r6.get("source")}')
ck(CNT['sina_list'] >= 57, f'翻满 57 页（实得 {CNT["sina_list"]} 次列表请求）')
ck(mc.LAST_SINA_RUN.get('badPages') == 0, '本轮无失败页')

# ========== [7] 开关关掉 = 改造前 ==========
print('[7] MARKET_CAP_SINA_FALLBACK=False → 逐字节回到"整批跳过"')
conn = sqlite3.connect(_TMP)
before = conn.execute('SELECT COUNT(*), SUM(total_cap) FROM stock_market_cap').fetchone()
conn.close()
cfg.MARKET_CAP_SINA_FALLBACK = False
CNT['sina_list'] = CNT['sina_count'] = 0
r7 = mc.refresh(symbols=['sh600000', 'sh600519'], only_missing=False)
conn = sqlite3.connect(_TMP)
after = conn.execute('SELECT COUNT(*), SUM(total_cap) FROM stock_market_cap').fetchone()
conn.close()
ck(CNT['sina_list'] == 0 and CNT['sina_count'] == 0, '开关关掉后一个新浪请求都不发')
ck(r7.get('reason') == '东财闸门关闭中' and r7.get('skip') == 2,
   f'返回仍是老的 skip 口径，实得 {r7}')
ck(before == after, f'表一行未动（{before[0]} 行 / 合计 {before[1]:,.0f}）')
cfg.MARKET_CAP_SINA_FALLBACK = True

# ========== [8] 全页失败不得写空表，且不能把备源打爆 ==========
print('[8] 所有页都 500 → 停止翻页、直接返回，不碰库')
conn = sqlite3.connect(_TMP)
before8 = conn.execute('SELECT COUNT(*) FROM stock_market_cap').fetchone()[0]
conn.close()
CNT['broken'] = True
CNT['sina_list'] = 0
r8 = mc.refresh_via_sina()
conn = sqlite3.connect(_TMP)
after8 = conn.execute('SELECT COUNT(*) FROM stock_market_cap').fetchone()[0]
conn.close()
ck(r8['ok'] == 0, f'ok=0，实得 {r8["ok"]}')
ck(before8 == after8, f'表行数不变（{before8} → {after8}）')
ck(CNT['sina_list'] <= 24,
   f'连续失败到阈值就停手：本轮只发 {CNT["sina_list"]} 个列表请求'
   f'（不封顶会打满 57 页 ×2 次重试 = 114 次，等于给新浪攒封禁）')
ck(r8['fail'] == mc._SINA_MAX_FAIL_PAGES or r8['fail'] <= 9,
   f'失败页计数 {r8["fail"]}（阈值 {mc._SINA_MAX_FAIL_PAGES}，并发下允许少量溢出）')
CNT['broken'] = False

# ========== [9] 换算错了会不会被下游抓到（后果链） ==========
print('[9] 下游后果：市值下限 35 亿用的必须是换算后的元')
ck(mc.sina_fallback_enabled() is True, '开关默认开')
mc._CAP_CACHE = None
cm = mc.cap_map()
ck(cm.get('600000') == 103048.32 * 1e4, 'cap_map 读到的就是元（35 亿下限才能正确比）')
ck(mc.min_cap() == 3.5e9, f'下限本身 = 35 亿（元），实得 {mc.min_cap():,.0f}')
ck(mc.passes('sh600519') is True, '茅台（1.8 万亿）过下限')
ck(mc.passes('sh600000') is False, '测试甲（10.3 亿）被下限剔除 —— 这正是单位错乘会误伤的那类票')

# ========== [10] 反证：换算系数写成 1 时，[1] 那条必须真的变红 ==========
print('[10] 反证（坏样本）：故意把万元→元的系数改成 1，确认 [1] 不是空转')
_saved = mc._SINA_WAN_TO_YUAN
mc._SINA_WAN_TO_YUAN = 1.0
mc.refresh_via_sina()
bad = q("SELECT total_cap FROM stock_market_cap WHERE symbol='600000'")[0][0]
ck(abs(bad - 103048.32) < 1,
   f'系数=1 时落库 {bad:,.2f}（万元当元）—— 这正是 [1] 会报红的形状，说明断言有牙')
ck(abs(bad - 103048.32 * 1e4) > 1, '同一条数值在 [1] 的判据下确实会失败')
mc._SINA_WAN_TO_YUAN = _saved
mc.refresh_via_sina()
good = q("SELECT total_cap FROM stock_market_cap WHERE symbol='600000'")[0][0]
ck(abs(good - 103048.32e4) < 1, f'恢复系数后回到 10.30 亿（实得 {good:,.0f}）')

# ========== 收口 ==========
eastmoney_guard.skip = _real_skip
print(f'\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败')
if FAIL:
    for m in FAIL:
        print('  失败：', m)
    sys.exit(1)
print('临时库：', _TMP, '（真库未触碰）')
