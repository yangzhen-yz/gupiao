"""fundflow_selftest.py - 资金流向模块自测（2026-10-19 东财风控根治重构版）

覆盖本次重构的六个面：
  [1] 个股排名请求合并：inflow/outflow 1 次 _clist（原 2 次），正负拆分与截断正确
  [2] 板块排名请求合并：行业+概念共 2 次 _clist（原 4 次），正负拆分、概念黑名单过滤
  [3] 快照 key 去参数化：恒存 'stocks'，不同 top_n 读取截断一致（修"测试参数刷新成功、
      默认参数读旧快照"割裂）
  [4] 龄期诚实化：closed 分支 >2h 快照标 stale、<2h 不标；updateTime=数据真实时间
  [5] 板块兜底：_sina_sector_rankings 用新浪个股流×本地行业聚合，concept 诚实为空
  [6] 共享熔断指数退避：900→1800→3600 封顶，ok 复位（eastmoney_guard）

运行：.venv/Scripts/python.exe scripts/fundflow_selftest.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.data import fundflow as ff
from services.data import eastmoney_guard as guard

PASS = []
FAIL = []

# ---- 防御性第一件事：立即桩掉 DB 快照读写（在任何测试逻辑之前） ----
# 2026-10-19 事故：早期版本自测的盘中刷新路径把测试假数据（'流入0'等）写进了
# 真实 gupiao.db 的 fund_flow_snapshot 表（15:56 污染 stocks/sectors 两行）。
# 现在 import 后立刻把 save_/load_ 换成内存桩（写入根本不落库），退出时再**只读**扫一遍
# 真库做核对——漏了就报红，不再由自测去 DELETE（11-07 改，理由见 _cleanup_db 的说明）。
_store = {}


def _fake_save(key, data):
    _store[key] = data
    return True


def _fake_load(key):
    return _store.get(key)


ff.save_fund_flow_snapshot = _fake_save
ff.load_fund_flow_snapshot = _fake_load


def _cleanup_db():
    """退出前**只读**扫真库：发现测试假名泄漏就记一条 FAIL（11-07 从"DELETE 擦干净"改的）。

    为什么不再删：2026-10-19 那次假数据（'流入0'）漏进真库后，老版本的处置是"退出前
    DELETE 擦掉"——那等于拿生产库当擦布。`fund_flow_snapshot` 是收盘后判 `fresh_snap` 的
    "最后成功快照"（11-03 就是因为它被误判才每次重走补算），自测去删它是在改线上状态；
    而且擦完没人知道曾经漏过。现在只查不删：漏了就报红，怎么清由人决定。
    判据与规则见 `scripts/db_hygiene_check.py` 的 R3。
    """
    rows = []
    try:
        import sqlite3
        _db = os.path.join(_SRC_DIR, 'gupiao.db')
        con = sqlite3.connect(f'file:{_db}?mode=ro', uri=True, timeout=10)
        try:
            rows = con.execute('SELECT snap_key, snap_json FROM fund_flow_snapshot').fetchall()
        finally:
            con.close()
    except Exception as e:
        print(f'[自测护栏] 真库只读扫描失败（不判定，只提示）: {e}')
        return
    bad = [k for k, blob in rows
           if any(t in (blob or '') for t in ('"流入', '"流出', '"行业0"', '"弱行业'))]
    if bad:
        _ck = globals().get('ck')              # atexit 可能跑在 ck 定义之前（脚本早退时）
        if _ck is not None:
            _ck(False, f'真库发现自测假数据泄漏（本自测应全程走内存桩）：{bad}')
        else:
            print(f'[自测护栏] ✗ 真库发现自测假数据泄漏：{bad}')
    else:
        print('[自测护栏] 真库无泄漏行（只读核对通过）')


import atexit
atexit.register(_cleanup_db)


def ck(cond, name, detail=''):
    (PASS if cond else FAIL).append(name)
    mark = '✓' if cond else '✗'
    print(f'  [{mark}] {name}' + (f'  {detail}' if (detail and not cond) else ''))


def _stub_clist(monkey_target, calls, rows):
    """把 fundflow._clist 换成记录调用的桩，返回 rows。"""
    def _fake(fid, po, fs, fields, pz, pn=1):
        monkey_target.append({'fid': fid, 'po': po, 'fs': fs, 'pz': pz})
        return rows
    return _fake


# 全局桩：新浪全市场快照默认返回 None（模拟"计算中"）。
# 不桩会触发真实 _sina_compute_snapshot 后台任务逐票拉 1094 只新浪（约 1-2 分钟），
# asyncio.run 关闭时还要等线程池join——测试会被拖死。[4] 节换专用桩测聚合逻辑。
async def _sina_none():
    return None


ff._sina_market_snapshot = _sina_none

# _SRC_DIR 供退出清洗用（server 目录，gupiao.db 所在）
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ========== [1] 个股排名请求合并 ==========
print('[1] 个股排名请求合并（原 2 次 → 1 次）')
import asyncio

_calls = []
_rows = []
# f62 降序：正负混合 30 条（正 15 负 15）
for i in range(15):
    _rows.append({'f12': f'{600000 + i}', 'f13': 1, 'f14': f'流入{i}',
                  'f2': 10.0, 'f3': 1.0, 'f62': 1e8 - i * 1e6, 'f100': '行业A'})
for i in range(15):
    _rows.append({'f12': f'{600100 + i}', 'f13': 1, 'f14': f'流出{i}',
                  'f2': 10.0, 'f3': -1.0, 'f62': -(1e7 + i * 1e6), 'f100': '行业B'})

_orig_clist = ff._clist
ff._clist = _stub_clist(_calls, None, _rows)
_r = asyncio.run(ff.get_stock_fund_flow_rankings(10))
ff._clist = _orig_clist
ck(len(_calls) == 1, '个股排名只发 1 次 clist 请求', f'实际 {len(_calls)} 次')
ck(_calls and _calls[0]['pz'] == 100, '单次请求 pz=100（覆盖默认 top_n=50）')
ck(len(_r['inflow']) == 10, 'inflow 截断到 top_n=10', f"实得 {len(_r['inflow'])}")
ck(len(_r['outflow']) == 10, 'outflow 截断到 top_n=10', f"实得 {len(_r['outflow'])}")
ck(all(x['mainNetInflow'] > 0 for x in _r['inflow']), 'inflow 全为正')
ck(all(x['mainNetInflow'] < 0 for x in _r['outflow']), 'outflow 全为负')
ck(_r['inflow'][0]['mainNetInflow'] >= _r['inflow'][-1]['mainNetInflow'], 'inflow 降序')
ck(_r['outflow'][0]['mainNetInflow'] <= _r['outflow'][-1]['mainNetInflow'], 'outflow 从最负起')
ck(_r['inflow'][0]['name'] == '流入0', 'normalize 字段正确（名称/代码/行业）')
ck(_r['inflow'][0]['symbol'] == 'sh600000', 'symbol 带市场前缀')

# ========== [2] 板块排名请求合并 ==========
print('[2] 板块排名请求合并（原 4 次 → 2 次）')
_calls2 = []
_ind = []
_con = []
for i in range(10):
    _ind.append({'f12': f'BK{i:04d}', 'f14': f'行业{i}', 'f3': 1.0,
                 'f62': 1e9 - i * 1e8, 'f104': 30, 'f105': 5})
for i in range(8):
    _ind.append({'f12': f'BK{100 + i:04d}', 'f14': f'弱行业{i}', 'f3': -1.0,
                 'f62': -(1e8 + i * 1e7), 'f104': 5, 'f105': 30})
# 概念里混 2 个黑名单宽泛概念（应被过滤）+ 3 个真题材
_con.append({'f12': 'BK9001', 'f14': '融资融券', 'f3': 1.0, 'f62': 9e9, 'f104': 999, 'f105': 1})
_con.append({'f12': 'BK9002', 'f14': '机器人', 'f3': 2.0, 'f62': 5e8, 'f104': 40, 'f105': 3})
_con.append({'f12': 'BK9003', 'f14': '商业航天', 'f3': 1.5, 'f62': 4e8, 'f104': 30, 'f105': 4})
_con.append({'f12': 'BK9004', 'f14': 'MLCC', 'f3': 3.0, 'f62': 3e8, 'f104': 20, 'f105': 2})
_con.append({'f12': 'BK9005', 'f14': '弱势概念', 'f3': -2.0, 'f62': -3e8, 'f104': 3, 'f105': 40})


def _fake_sector_clist(fid, po, fs, fields, pz, pn=1):
    _calls2.append({'fid': fid, 'fs': fs, 'pz': pz})
    return _ind if 't:2' in fs else _con


ff._clist = _fake_sector_clist
_s = asyncio.run(ff.get_sector_fund_flow_rankings(5))
ff._clist = _orig_clist
ck(len(_calls2) == 2, '板块排名只发 2 次 clist 请求（行业+概念）', f'实际 {len(_calls2)} 次')
ck(all(c['pz'] == 100 for c in _calls2), '两次均 pz=100')
ck(len(_s['industry']['inflow']) == 5, '行业 inflow 截断 5')
ck(len(_s['industry']['outflow']) == 5, '行业 outflow 截断 5')
ck(all(x['mainNetInflow'] > 0 for x in _s['industry']['inflow']), '行业 inflow 全正')
ck(all(x['mainNetInflow'] < 0 for x in _s['industry']['outflow']), '行业 outflow 全负')
_names = [x['name'] for x in _s['concept']['inflow']]
ck('融资融券' not in _names, '概念黑名单过滤（融资融券 9e9 被滤掉）')
ck('机器人' in _names and '商业航天' in _names, '真题材概念保留')
ck('弱势概念' in [x['name'] for x in _s['concept']['outflow']], '概念 outflow 拆分正确')

# ========== [3] 快照 key 去参数化 ==========
print('[3] 快照 key 去参数化 + 读取截断')
# 注：save/load 桩已在文件开头全局 patch（防御性，勿删）
ff._LAST_GOOD.clear()

_now = time.time()
ff._LAST_GOOD['stocks'] = {'data': {'inflow': _r['inflow'], 'outflow': _r['outflow']}, 'ts': _now}
# 模拟 closed 分支用不同 top_n 读同一份快照
ff._in_trading_window = lambda: False  # closed
_d25 = asyncio.run(ff.get_fund_flow_summary(stock_top_n=25, sector_top_n=5,
                                            change_threshold=3.0, hot_top_n=30, hidden_top_n=20))
ck(_d25['stocks']['inflow'][0]['name'] == '流入0', 'closed 分支快照可用（stocks）')
ck(_d25['dataSource']['stocks'] == 'snapshot', 'closed 分支标记 snapshot')
ck(_d25['stale'] is False, '2 小时内的快照不标 stale')
ck(_d25['updateTime'] != '--' and _d25['updateTime'][11:13] != '', 'updateTime 是数据真实时间')
ck('fetchTime' in _d25, '新增 fetchTime 字段（调用时间）')
# 把快照时间推到 3 小时前 → stale
# 注意：新实现（2026-10-19 外部演进）closed+过期快照会**先走新浪补算**，补算成功则数据
# 新鲜、不 stale；只有补算也失败才回落过期快照并亮 stale。所以这里要桩掉新浪排行。
ff._LAST_GOOD['stocks']['ts'] = _now - 3 * 3600
ff._CACHE.clear()
_orig_sina_rank = ff._sina_stock_rankings


async def _sina_rank_none(top_n):
    return None


ff._sina_stock_rankings = _sina_rank_none
_d_stale = asyncio.run(ff.get_fund_flow_summary(stock_top_n=25, sector_top_n=5,
                                                change_threshold=3.0, hot_top_n=30, hidden_top_n=20))
ff._sina_stock_rankings = _orig_sina_rank
ck(_d_stale['stale'] is True, '超过 2h 的快照 + 新浪补算失败 → 标 stale')
# 补算成功路径：新浪排行有数据 → 数据新鲜、不 stale
_orig_sina_rank2 = ff._sina_stock_rankings


async def _sina_rank_ok(top_n):
    return {'inflow': _r['inflow'], 'outflow': _r['outflow']}


ff._sina_stock_rankings = _sina_rank_ok
ff._CACHE.clear()
_d_fresh = asyncio.run(ff.get_fund_flow_summary(stock_top_n=25, sector_top_n=5,
                                                change_threshold=3.0, hot_top_n=30, hidden_top_n=20))
ff._sina_stock_rankings = _orig_sina_rank2
ck(_d_fresh['stale'] is False and _d_fresh['dataSource']['stocks'] == 'sina',
   'closed+过期快照 → 新浪补算成功 → 数据新鲜不 stale')
# DB 里存的是去参数化 key（closed 分支不落盘，需切盘中刷新路径验证）
ff._in_trading_window = lambda: True  # 盘中
_calls3 = []
ff._clist = _stub_clist(_calls3, None, _rows)
ff._CACHE.clear()
_d_refresh = asyncio.run(ff.get_fund_flow_summary(stock_top_n=25, sector_top_n=5,
                                                  change_threshold=3.0, hot_top_n=30, hidden_top_n=20))
ff._clist = _orig_clist
ff._in_trading_window = lambda: False  # closed
ck('stocks' in _store, '盘中刷新后快照落盘 key 为 stocks（无参数后缀）',
   f"实际 keys={list(_store.keys())}")
ck(not any(':' in k for k in _store), '无旧式参数化 key 残留')
# 盘中 hidden 东财返回空（测试数据涨幅均 <3% 阈值）→ 新浪桩 None → 诚实降级为空
ck(_d_refresh['dataSource']['hidden'] == 'none' and _d_refresh['hiddenGems'] == [],
   'hidden 东财空+兜底无→诚实降级为空（不假装有数据）')

# ========== [4] 板块兜底：新浪个股流 × 本地行业聚合 ==========
print('[4] 板块兜底（新浪+本地行业聚合）')
# 新实现（2026-10-19 外部演进）：ind_map 从行内 industry 构建；**整表为空**才回退
# _sina_industry_map()（整表级回退，避免行内/本地混源）。两条路分开测。
_sina_rows = [
    {'symbol': 'sh600001', 'code': '600001', 'name': '甲', 'price': 10, 'changePercent': 1.0,
     'mainNetInflow': 5e8, 'industry': '电力'},
    {'symbol': 'sh600002', 'code': '600002', 'name': '乙', 'price': 10, 'changePercent': -1.0,
     'mainNetInflow': 3e8, 'industry': '电力'},
    {'symbol': 'sz000001', 'code': '000001', 'name': '丙', 'price': 10, 'changePercent': 2.0,
     'mainNetInflow': -2e8, 'industry': '银行'},
    {'symbol': 'sz000002', 'code': '000002', 'name': '丁', 'price': 10, 'changePercent': 0.5,
     'mainNetInflow': 1e8, 'industry': '房地产'},
]


async def _fake_snapshot():
    return _sina_rows


_orig_sina_snap = ff._sina_market_snapshot
ff._sina_market_snapshot = _fake_snapshot
_sb = asyncio.run(ff._sina_sector_rankings(10))
ff._sina_market_snapshot = _orig_sina_snap
ck(_sb is not None, '板块兜底返回非空')
_ind_in = _sb['industry']['inflow']
ck(len(_ind_in) == 2 and _ind_in[0]['name'] == '电力',
   '行业聚合正确（电力 8e8 居首，行内 industry 优先）',
   f"实得 {[(x['name'], x['mainNetInflow']) for x in _ind_in]}")
ck(_ind_in[0]['riseCount'] == 1 and _ind_in[0]['fallCount'] == 1, '涨跌家数聚合正确')
ck(_ind_in[1]['name'] == '房地产' and abs(_ind_in[1]['mainNetInflow'] - 1e8) < 1,
   '第二行业正确（房地产 1e8）')
ck(len(_sb['industry']['outflow']) == 1 and _sb['industry']['outflow'][0]['name'] == '银行',
   '行业 outflow 正确（银行 -2e8）')
ck(_sb['concept'] == {'inflow': [], 'outflow': []}, '概念板块诚实为空（无本地分类）')

# 整表回退路径：行内 industry 全空 → 回退 _sina_industry_map（打桩）
_sina_rows2 = [
    {'symbol': 'sh600001', 'code': '600001', 'name': '甲', 'price': 10, 'changePercent': 1.0,
     'mainNetInflow': 5e8, 'industry': ''},
    {'symbol': 'sz000002', 'code': '000002', 'name': '丁', 'price': 10, 'changePercent': 0.5,
     'mainNetInflow': 1e8, 'industry': ''},
]


async def _fake_snapshot2():
    return _sina_rows2


_orig_ind_map = ff._sina_industry_map
ff._sina_market_snapshot = _fake_snapshot2
ff._sina_industry_map = lambda: {'sh600001': '电力', 'sz000002': '房地产'}
_sb2 = asyncio.run(ff._sina_sector_rankings(10))
ff._sina_market_snapshot = _orig_sina_snap
ff._sina_industry_map = _orig_ind_map
_ind2 = _sb2['industry']['inflow'] if _sb2 else []
ck(len(_ind2) == 2 and _ind2[0]['name'] == '电力' and _ind2[1]['name'] == '房地产',
   '行内 industry 整表为空 → 回退本地行业表聚合',
   f"实得 {[(x['name'], x['mainNetInflow']) for x in _ind2]}")

# ========== [5] 共享熔断指数退避 ==========
print('[5] 共享熔断指数退避')
guard.reset('clist')
for _ in range(3):
    guard.fail('clist')
_st = guard.status('clist')
ck(_st['cooling'] and 890 <= _st['cooldownRemainSec'] <= 900,
   '第 1 次熔断冷却 900s', f"remain={_st['cooldownRemainSec']}")
ck(_st['trips'] == 1, 'trips 计数=1')
# 模拟冷却结束后再次失败 3 次 → 1800s
guard._state['clist']['until'] = 0.0
guard._state['clist']['streak'] = 0
for _ in range(3):
    guard.fail('clist')
_st2 = guard.status('clist')
ck(_st2['trips'] == 2 and 1790 <= _st2['cooldownRemainSec'] <= 1800,
   '第 2 次熔断冷却 1800s（指数退避）', f"trips={_st2['trips']} remain={_st2['cooldownRemainSec']}")
# 第三次 → 3600s（封顶）
guard._state['clist']['until'] = 0.0
guard._state['clist']['streak'] = 0
for _ in range(3):
    guard.fail('clist')
_st3 = guard.status('clist')
ck(_st3['cooldownRemainSec'] <= 3600 and _st3['cooldownRemainSec'] > 1800,
   '第 3 次熔断冷却 3600s（封顶）', f"remain={_st3['cooldownRemainSec']}")
# 第四次仍封顶 3600
guard._state['clist']['until'] = 0.0
guard._state['clist']['streak'] = 0
for _ in range(3):
    guard.fail('clist')
_st4 = guard.status('clist')
ck(_st4['cooldownRemainSec'] <= 3600, '第 4 次仍封顶 3600s', f"remain={_st4['cooldownRemainSec']}")
guard.ok('clist')
ck(not guard.status('clist')['cooling'] and guard.status('clist')['trips'] == 0,
   'ok 复位（冷却解除+trips 清零）')

# ========== [6] 接线静态断言 ==========
print('[6] 全项目东财调用方接线静态断言')

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(_SRC, rel), encoding='utf-8') as f:
        return f.read()


_wired = {
    'services/data/fundflow.py': '_clist_circuit_open',
    'services/data/stock.py': "eastmoney_guard.skip('clist')",
    'services/data/stock.py#2': "eastmoney_guard.skip('ulist')",
    'services/ops/review.py': "eastmoney_guard.skip('clist')",
    'services/strategy/rps.py': "eastmoney_guard.skip('clist')",
    'services/pick/recommend.py': "eastmoney_guard.skip('ulist')",
    # 2026-11-06：routes.py 拆分后代理端点在 routers/data.py（原来这条断言指向
    # app/api/routes.py，拆分当天就恒假了，只是没人重跑这份自测）
    'app/api/routers/data.py': "eastmoney_guard.skip('clist')",
    'app/main.py': "eastmoney_guard.skip('clist')",
    # 2026-11-06 补的六处漏接（11-06 用户要求"当天不通就别再戳"，这六处不接就停不住）：
    # 三池走 push2ex，自己一个 scope，不再被 clist 连带停
    'services/ops/review.py#pools': "eastmoney_guard.skip('pools')",
    'services/strategy/sentiment.py': "guard.skip('pools')",
    'services/data/valuation.py': "eastmoney_guard.skip('clist')",
    'services/data/smartmoney.py': "eastmoney_guard.skip('datacenter')",
    'services/data/smartmoney.py#push2': "eastmoney_guard.skip('push2')",
    'services/data/fundamental.py': "eastmoney_guard.skip('datacenter')",
    'services/data/market_cap.py': "eastmoney_guard.skip('push2')",
}
for rel, needle in _wired.items():
    path = rel.split('#')[0]
    ck(needle in _read(path), f'{path} 接入 guard（{needle}）')

# ========== 汇总 ==========
print()
print(f'===== 结果：{len(PASS)} 通过 / {len(FAIL)} 失败 =====')
if FAIL:
    for f in FAIL:
        print(f'  ✗ {f}')
    sys.exit(1)
print('全部通过 ✓')
