"""fundflow_fallback_selftest.py - 东财封禁日的取数链路自测（全程假 HTTP，不联网）

为什么值得单独一份：10-19 东财 clist 被封一整天，资金流向页从 08:51 起就停在旧数据上，
而"备源"自己又慢又危险（逐票 1094 个请求、约 2 分钟才算完，这期间页面只能继续吃旧快照）。
这类东西用眼睛看是看不出问题的 —— 它不报错，只是安静地给你昨天的数。

这里断言的四件事都是"封禁日到底会发生什么"：
  1. 熔断分级：连接被重置=blocked（2 次就全域停手），空包/超时=flaky（3 次才熔断）；
     封的是 IP，所以一个接口被封时，涨停池/龙虎榜/公告也必须一起停 —— 否则就是替东财续封。
  2. 新浪排行：过滤掉场内基金/国债ETF/北交所（它们动辄几十亿，会把真正的个股挤没）；
     涨幅是 0.100044 这种小数要换算成 10.0%；行业从本地表补并且去掉国标码前缀。
  3. 翻到底就停：全市场扫描不能按 max_pages 硬翻（空页继续翻=白挨一次限流）。
  4. 收盘后快照过期时必须改用新浪补算；快照还是今天的就一个请求都不发（别整晚轮询）。
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

_TMP = os.path.join(tempfile.gettempdir(), f'ff_test_{os.getpid()}.db')
for suf in ('', '-wal', '-shm'):
    try:
        os.remove(_TMP + suf)
    except OSError:
        pass

import db.database as dbmod
dbmod.use_db_path(_TMP)      # 换库必须走这个函数：11-05 db 拆分后只赋值 dbmod.SQLITE_DB_PATH 是静默无效的，
#                                    INSERT 会直接写进生产库 server/gupiao.db
dbmod.init_db()

# 本地行业表：造三条（含一条带国标码前缀，验证去码）
_c = dbmod.get_db_conn()
_c.execute('''CREATE TABLE IF NOT EXISTS stock_industry_map (
    symbol TEXT PRIMARY KEY, code TEXT, industry_code TEXT, industry TEXT, updated_at TEXT)''')
_c.executemany('INSERT INTO stock_industry_map VALUES (?,?,?,?,?)', [
    ('sh600487', '600487', 'C38', 'C38电气机械和器材制造业', '2026-10-19'),
    ('sz002242', '002242', 'C38', 'C38电气机械和器材制造业', '2026-10-19'),
    ('sh600127', '600127', 'A01', 'A01农业', '2026-10-19'),
])
_c.commit()
_c.close()

import requests                                    # noqa: E402
from services.data import eastmoney_guard as G     # noqa: E402
# 本份自测会**真的**调用 ok/fail/reset（这是它要测的东西），而 guard 每次状态变化都原子落盘到
# `server/data/eastmoney_guard_state.json`。不落盘钩子就会把测试造的停调写进真实状态文件，
# 线上下一次重启时按那份文件恢复 → 白停一天（2026-11-07 复核时文件恰好已被写脏过，
# 只是最后被脚本自己的 reset 抹平了，不能指望运气）。
G._save_state_locked = lambda: None
from services.data import fundflow as F            # noqa: E402

FAILS = []
CALLS = []


def ck(cond, msg):
    print(('  ✓ ' if cond else '  ✗ ') + msg)
    if not cond:
        FAILS.append(msg)


print('[1] 错误分级')
import http.client as _hc
try:
    raise requests.exceptions.ConnectionError(
        ('Connection aborted.', _hc.RemoteDisconnected('Remote end closed connection without response')))
except Exception as e:
    ck(G.classify(e) == 'blocked', f'连接被重置 → blocked（实得 {G.classify(e)}）')
ck(G.classify(ValueError('empty response')) == 'flaky', '空 body → flaky（不该当封禁处理）')
ck(G.classify(requests.exceptions.ConnectTimeout('t')) == 'blocked',
   '连接超时 → blocked')
ck(G.classify(RuntimeError('rc=100')) == 'flaky', '业务 rc 异常 → flaky')

print('\n[2] 全域闸门：封的是 IP，不是某个接口')
G.reset_all()        # 段前也全清一次（scope + 家族 + 全域），别拿上一段残留的状态判这一段
G.reset_domain()
for _s in list(G._state):
    G.reset(_s)
G.fail('clist', ValueError('empty response'))   # 一次抖动：不该惊动任何闸门
ck(not G.skip('ztpool'), '一次抖动不该就把别的接口也关掉')
ck(G.status('clist')['streak'] == 1, '但 flaky 也在计数（streak=1）')
G.fail('clist', ConnectionError('RemoteDisconnected'))   # blocked 第 1 次
ck(not G.skip('ztpool'), '一次连接被重置还不该全域停手（阈值 2）')
ck(G.status('clist')['blockedStreak'] == 1,
   f'blocked 与 flaky 分开计数（实得 blockedStreak={G.status("clist")["blockedStreak"]}）')
G.fail('ulist', ConnectionError('RemoteDisconnected'))   # blocked 第 2 次（同家族 push2delay）
# 2026-11-06 改口径：blocked 只关**出事的主机家族**。ulise/clist 都在 push2delay，
# 一起停是对的；但涨停池(push2ex)/数据中心(datacenter-web)是另外两条链路——实测近 14 天
# 失败率 pools 1.0%、datacenter 0.4%，把它们一起关正是 11-06 "退潮没推"的根因。
ck(G.skip('clist') and G.skip('ulist'), '同家族（push2delay 的 clist/ulist）一起停')
ck(not G.skip('pools') and not G.skip('datacenter'),
   '其它主机家族不受牵连：push2ex 三池与 datacenter 照常请求')
st = G.status('clist')
ck(st['family'] == 'main' and st['familyCooling'] and st['familyRemainSec'] > 0,
   f'状态里查得到本家族还剩多久（{st["familyRemainSec"]}s，recoverAt={st["familyRecoverAt"]}）'
   f'——"为什么这页是旧的"要能一句话答')
G.fail('pools', ConnectionError('RemoteDisconnected'))
G.fail('pools', ConnectionError('RemoteDisconnected'))   # 第二个家族也挂 → 才判定封的是 IP
st = G.status('datacenter')
ck(st['domainCooling'] and st['domainRemainSec'] > 0,
   f'两个家族同时熔断才升级全域闸门（全域剩余 {st["domainRemainSec"]}s）；'
   f'此时连 datacenter/公告也一起停：{G.skip("datacenter") and G.skip("ann")}')
G.fail('clist', ConnectionError('RemoteDisconnected'))   # clist 自己累计到 2 次 blocked → 本接口也熔断
ck(G.status('clist')['cooling'], 'clist 自己的冷却独立于家族/全域闸门')
G.ok('pools', clear_domain=False)             # 三池成功只复位三池自己，不给 clist 提前探测
st = G.status('clist')
ck(st['familyCooling'], '边缘节点的成功不得替 main 家族解除停调')
G.ok('clist')                                 # main 自己通了 = IP 没被封
st = G.status('clist')
ck(not st['domainCooling'] and not st['familyCooling'],
   '一次成功即解除家族与全域闸门，不用等冷却到期')
for _s in list(G._state):
    G.reset(_s)
G.reset_domain()
# ⚠️ 11-06 熔断改造后这里必须走 `reset_all()`：blocked 熔断除了 scope 冷却，还会把**主机家族**
# 的 until 抬到当日 23:59:59（当日停调）。上面那两行只清 scope 与全域，家族层残留会让本进程
# 后续所有段落都处在"main 停调中"——于是 [6]/[7]/[7b] 那 5 条断言看到的是"新浪根本没被试"，
# 而这不是资金流逻辑坏了，是自测没复位干净（2026-11-07 就是这样误判过一次）。
G.reset_all()

print('\n[3] 假 HTTP：新浪排行映射')


def _rank_row(sym, name, r0, ratio=0.100044, trade=10.0, net=None):
    return {'symbol': sym, 'name': name, 'trade': str(trade), 'changeratio': str(ratio),
            'r0_net': str(r0), 'netamount': str(net if net is not None else r0),
            'amount': '1000', 'turnover': '5'}


def fake_get(url, params=None, headers=None, timeout=None, proxies=None, **kw):
    CALLS.append((url.split('/')[-1], dict(params or {})))
    p = dict(params or {})

    class R:
        status_code = 200
        text = ''

        def __init__(self, payload):
            self._p = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._p

    if 'ssl_bkzj_ssggzj' in url:
        asc = int(p.get('asc') or 0)
        page = int(p.get('page') or 1)
        if p.get('sort') == 'SWEEP':          # 全市场扫描用：3 页有数据、第 4 页起空
            if page <= 3:
                return R([_rank_row(f'sh6000{page:02d}', f'扫{page}', page * 1e7)])
            return R([])
        if asc == 0:
            return R([_rank_row('sh600487', '亨通光电', 22.5e8),
                      _rank_row('sh510500', '中证500ETF', 8.9e8),      # 基金，必须被过滤
                      _rank_row('bj920196', '北交所某票', 5e8),        # 北交所，必须被过滤
                      _rank_row('sz002242', '九阳股份', 8.2e8)])
        return R([_rank_row('sh600127', '金健米业', -5.0e8),
                  _rank_row('sh511010', '国债ETF', -47.0e8)])          # 同样要过滤
    return R([])


F.requests.get = fake_get
requests.get = fake_get
F._SINA_IND_CACHE = {'ts': 0.0, 'map': {}}
F._SINA_RANK_CACHE.clear()
F._SINA_SNAPSHOT_CACHE.clear()

rows = F._sina_rank_rows('r0_net', 0, 1)
ck([r['symbol'] for r in rows] == ['sh600487', 'sz002242'],
   f'只留沪深 A 股，ETF/北交所被滤掉（实得 {[r["symbol"] for r in rows]}）')
ck(abs(rows[0]['changePercent'] - 10.0) < 0.01,
   f'changeratio 0.100044 → 涨幅 10.0%（实得 {rows[0]["changePercent"]}%）')
ck(rows[0]['industry'] == '电气机械和器材制造业',
   f'行业来自本地表且去掉国标码（实得 {rows[0]["industry"]}）')
ck(rows[0]['flowCaliber'] == 'sina_r0' and rows[0]['mainNetInflow'] == 22.5e8,
   '口径标注随行走（新浪排行只有超大单，与东财 f62 不是一套）')

print('\n[4] 全市场扫描：翻到底就停，不硬翻页')
CALLS.clear()
sweep = F._sina_sweep('SWEEP')
pages = [c for c in CALLS if c[0].endswith('ssl_bkzj_ssggzj')]
ck(len(sweep) == 3, f'3 页数据 → 3 行（实得 {len(sweep)}）')
ck(len(pages) < 12,
   f'第 4 页空即收手，没有把 {F._SINA_RANK_MAX_PAGES} 页翻满（实际请求 {len(pages)} 页）')

print('\n[5] 个股排名只要 4 个请求（旧写法是 1094 次逐票 + 19 次行情）')
CALLS.clear()
F._SINA_RANK_CACHE.clear()
r = asyncio.run(F._sina_stock_rankings(50))
_n4 = len([c for c in CALLS if c[0].endswith('ssl_bkzj_ssggzj')])
ck(_n4 == 4, f'两侧各 2 页 = 4 请求（实得 {_n4}）')
ck([x['symbol'] for x in r['inflow']] == ['sh600487', 'sz002242'],
   f'净流入榜按 r0 降序（实得 {[x["symbol"] for x in r["inflow"]]}）')
ck([x['symbol'] for x in r['outflow']] == ['sh600127'],
   f'净流出榜只留个股（实得 {[x["symbol"] for x in r["outflow"]]}）')
CALLS.clear()
asyncio.run(F._sina_stock_rankings(50))
ck(not [c for c in CALLS if c[0].endswith('ssl_bkzj_ssggzj')], '5 分钟内第二次不重复请求（走缓存）')

print('\n[6] 收盘后：快照过期就补算，快照新鲜就一个请求都不发')
F.requests.get = fake_get
F._in_trading_window = lambda: False
F._clist_circuit_open = lambda: False
calls_now = []


async def _stub_rank(top_n):
    calls_now.append('sina')
    return {'inflow': [{'symbol': 'sh600487', 'name': '亨通光电', 'price': 50.25,
                        'changePercent': 10.0, 'mainNetInflow': 22.5e8, 'industry': '电气'}],
            'outflow': []}


async def _no_sector(top_n):
    return None


async def _no_gems(a, b, c):
    return []


F._sina_stock_rankings = _stub_rank
F._sina_sector_rankings = _no_sector
F._sina_hidden_gems = _no_gems

F._LAST_GOOD['stocks'] = {'data': {'inflow': [{'symbol': 'old'}], 'outflow': []},
                          'ts': time.time() - 6 * 3600}      # 6 小时前的旧快照
F._LAST_GOOD['sectors'] = {'data': {}, 'ts': time.time() - 6 * 3600}
F._LAST_GOOD['hidden'] = {'data': [], 'ts': time.time() - 6 * 3600}
F._CACHE.clear()
d = asyncio.run(F.get_fund_flow_summary())
ck('sina' in calls_now, '收盘后快照超龄 → 用新浪排行补算当日排名（不再拿昨天的数）')
ck(d['dataSource']['stocks'] == 'sina', f'来源标成 sina（实得 {d["dataSource"]}）')
ck(d['stale'] is False, '补算成功就不该再挂"离线快照"警示')

calls_now.clear()
F._LAST_GOOD['stocks'] = {'data': {'inflow': [{'symbol': 'fresh'}], 'outflow': []},
                          'ts': time.time() - 60}            # 1 分钟前的快照
F._CACHE.clear()
d2 = asyncio.run(F.get_fund_flow_summary())
ck(not calls_now, '快照还是今天的 → 收盘后一个备源请求都不发（别整晚轮询）')
ck(d2['dataSource']['stocks'] == 'snapshot' and d2['stocks']['inflow'][0]['symbol'] == 'fresh',
   f'直接用今天的快照（实得 {d2["dataSource"]}）')

print('\n[7] 熔断只关东财：东财冷却时备源必须继续顶上（10-19 就是这条把页面冻在两天前的数上）')
F._clist_circuit_open = lambda: True
F._LAST_GOOD['stocks'] = {'data': {'inflow': [{'symbol': 'old2'}], 'outflow': []},
                          'ts': time.time() - 8 * 3600}
calls_now.clear()
F._CACHE.clear()
d3 = asyncio.run(F.get_fund_flow_summary())
ck(d3['cooling'] is True, 'cooling 标志透出（前端据此说明"为什么不是实时"）')
ck('sina' in calls_now, '东财冷却中仍然要试新浪 —— 熔断关的是东财那条路，不是所有路')
ck(d3['dataSource']['stocks'] == 'sina' and d3['stale'] is False,
   f'备源顶上后不该再挂"离线快照"（实得 {d3["dataSource"]} / stale={d3["stale"]}）')

print('\n[7b] 备源也不通时：旧快照仍然给，但 stale 必须亮')


async def _dead_rank(top_n):
    return None


F._sina_stock_rankings = _dead_rank
F._LAST_GOOD['stocks'] = {'data': {'inflow': [{'symbol': 'old3'}], 'outflow': []},
                          'ts': time.time() - 8 * 3600}
F._CACHE.clear()
d4 = asyncio.run(F.get_fund_flow_summary())
ck(d4['stocks']['inflow'] and d4['stocks']['inflow'][0]['symbol'] == 'old3',
   f'旧数据仍然返回（空白页比旧页更糟：用户分不清"没数据"和"数据旧"），实得 {d4["stocks"]}')
ck(d4['stale'] is True, '但 stale 亮起，不拿旧数装新数')
ck(d4['updateTime'] != d4['fetchTime'],
   f'updateTime 是数据真实时间、fetchTime 才是本次调用时间（{d4["updateTime"]} / {d4["fetchTime"]}）')

print('\n[7c] 行业列：快照行没行业时按本地分类表补')
F._clist_circuit_open = lambda: False
F._sina_stock_rankings = _stub_rank
F._LAST_GOOD['stocks'] = {'data': {'inflow': [{'symbol': 'sh600487', 'name': '亨通光电',
                                               'mainNetInflow': 1e8, 'industry': ''}],
                                   'outflow': []},
                          'ts': time.time() - 60}
F._CACHE.clear()
d5 = asyncio.run(F.get_fund_flow_summary())
ck(d5['stocks']['inflow'][0]['industry'] == '电气机械和器材制造业',
   f'快照行也补上本地行业（实得 {d5["stocks"]["inflow"][0]["industry"]!r}）')

# ⚠️ 临时库不能在 [7c] 之后删：从 [7d] 起还要读写 `fund_flow_snapshot`，
# 原来这句 `os.remove(_TMP)` 夹在中间，等于把库文件抽走再判后面那段 —— sqlite 会顺手
# 新建一个**空文件**，于是日志出现 `no such table: fund_flow_snapshot`（11-07 复核时发现）。
# 清理挪到全部段落跑完之后。
print('\n[7d] 龄期按块判定：个股是今天的、非热点还是两天前的 → 整页不许挂"今日未刷新"')
F._clist_circuit_open = lambda: False
F._sina_stock_rankings = _stub_rank


async def _dead_gems(a, b, c):
    return []


F._sina_hidden_gems = _dead_gems
F._LAST_GOOD['stocks'] = {'data': {'inflow': [], 'outflow': []}, 'ts': time.time() - 60}
F._LAST_GOOD['hidden'] = {'data': [{'symbol': 'sh600001', 'name': '两天前的旧票'}],
                          'ts': time.time() - 2 * 24 * 3600}
F._CACHE.clear()
d6 = asyncio.run(F.get_fund_flow_summary())
ck(d6['stale'] is False,
   f'主口径（个股）本轮是新浪现取 → stale 不该被两天前的非热点拖累（实得 {d6["stale"]}）')
ck('hidden' in (d6.get('staleBlocks') or []),
   f'但旧的那一块必须被点名（实得 {d6.get("staleBlocks")} / 各块龄期 {d6.get("blockAges")}）')

for _suf in ('', '-wal', '-shm'):
    try:
        os.remove(_TMP + _suf)
    except OSError:
        pass

print()
print('全部通过' if not FAILS else f'失败 {len(FAILS)} 项: {FAILS}')
sys.exit(0 if not FAILS else 1)
