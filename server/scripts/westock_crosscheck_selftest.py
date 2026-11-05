"""westock_crosscheck_selftest.py - 双源校验模块自测（2026-10-19）

覆盖：
  [1] Markdown 表格解析（正常表/空输出/限频文本/千分位/列数不齐）
  [2] _num 数值清洗（千分位/'-'/''/None）
  [3] 限频识别
  [4] 比对逻辑：quote_agreed 三态（一致/不一致/未比对）、direction_conflict、diff_pct
  [5] 落库打桩（save 被调用的次数与内容）
  [6] 宇宙构建与去重封顶
  [7] 限频退避重试（桩 _westock：第 1 次限频、第 2 次成功）

运行：python scripts/westock_crosscheck_selftest.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import westock_crosscheck as xc


def _cleanup_db():
    """退出前清掉测试日期数据（防漏写真库——2026-10-19 踩过：打桩方式错导致
    2099 测试日期写进真实 gupiao.db）。"""
    try:
        import sqlite3
        from db.database import SQLITE_DB_PATH
        con = sqlite3.connect(SQLITE_DB_PATH, timeout=10)
        con.execute("DELETE FROM westock_snapshot WHERE date LIKE '2099-%'")
        con.execute("DELETE FROM data_cross_check WHERE date LIKE '2099-%'")
        con.commit()
        con.close()
    except Exception as e:
        print(f'[自测清洗] 失败（不影响判定）: {e}')


import atexit
atexit.register(_cleanup_db)

PASS = []
FAIL = []


def ck(cond, name, detail=''):
    (PASS if cond else FAIL).append(name)
    print(f'  [{"✓" if cond else "✗"}] {name}' + (f'  {detail}' if (detail and not cond) else ''))


# ========== [1] Markdown 表格解析 ==========
print('[1] Markdown 表格解析')
_table = '''
[Batch] 状态: partial | 总数: 2 | 成功: 2 | 失败: 0

| code | name | price | change_percent |
| --- | --- | --- | --- |
| sh600000 | 浦发银行 | 9.78 | -0.87 |
| sh600519 | 贵州茅台 | 1,324.97 | -0.3 |
'''
rows = xc._parse_md_table(_table)
ck(len(rows) == 2, '解析出 2 行数据', f'实得 {len(rows)}')
ck(rows and rows[0]['code'] == 'sh600000' and rows[0]['name'] == '浦发银行', '字段映射正确')
ck(xc._parse_md_table('') == [], '空输出返回 []')
ck(xc._parse_md_table('some error text') == [], '无表格文本返回 []')
# 限频文本（失败列表不是 | 开头）
_rl = '''
[Batch] 状态: error | 总数: 1 | 成功: 0 | 失败: 1
失败列表:
  sh600000: [SERVER_RATE_LIMITED] 查询资金流向失败
'''
ck(xc._parse_md_table(_rl) == [], '限频文本解析为空行')
# 列数不齐的脏行被跳过
_dirty = '''| a | b |
| --- | --- |
| 1 | 2 |
| 3 | 4 | 5 |
'''
ck(len(xc._parse_md_table(_dirty)) == 1, '列数不齐的脏行被跳过')

# ========== [2] _num 数值清洗 ==========
print('[2] _num 数值清洗')
ck(xc._num('1,234.5') == 1234.5, '千分位逗号清理')
ck(xc._num('-0.52') == -0.52, '负数字符串')
ck(xc._num('') is None and xc._num('-') is None and xc._num(None) is None, '空值/占位/None → None')
ck(xc._num('abc') is None, '非数值 → None')

# ========== [3] 限频识别 ==========
print('[3] 限频识别')
ck(xc._is_rate_limited('[SERVER_RATE_LIMITED] xx'), '识别 SERVER_RATE_LIMITED')
ck(xc._is_rate_limited('msg=服务限频'), '识别中文"服务限频"')
ck(not xc._is_rate_limited('| sh600000 | 正常表格 |'), '正常输出不误判')

# ========== [4] 比对逻辑（run 的核心循环复刻） ==========
print('[4] 比对逻辑')
# 用 run() 打桩验证：构造两源数据比对
_fake_ff = {
    'sh600000': {'code': 'sh600000', 'name': '甲', 'MainNetFlow': '100000000',
                 'JumboNetFlow': '60000000', 'MidNetFlow': '20000000',
                 'RetailInFlow': '50000000', 'RetailOutFlow': '30000000',
                 'MainNetFlow5D': '1000000', 'MainNetFlow10D': '2000000',
                 'MainNetFlow20D': '3000000', 'MainInflowIndustryRank': '1'},
    'sh600001': {'code': 'sh600001', 'name': '乙', 'MainNetFlow': '-50000000'},
    'sh600002': {'code': 'sh600002', 'name': '丙', 'MainNetFlow': '10000000'},
}
_fake_qt = {
    'sh600000': {'code': 'sh600000', 'name': '甲', 'price': '9.78', 'change_percent': '1.00'},
    'sh600001': {'code': 'sh600001', 'name': '乙', 'price': '10.00', 'change_percent': '2.00'},
    'sh600002': {'code': 'sh600002', 'name': '丙', 'price': '10.00', 'change_percent': '3.00'},
}
# 本地值：甲涨幅一致+资金同向；乙涨幅不一致；丙资金符号冲突
_fake_our = {
    'sh600000': {'change_pct': 1.0, 'main_inflow': 8e7},    # 同向正
    'sh600001': {'change_pct': 2.5, 'main_inflow': -4e7},   # 涨幅不一致(2.0 vs 2.5)
    'sh600002': {'change_pct': 3.0, 'main_inflow': 2e7},    # 符号冲突(westock +1000万 vs our +2000万? 同向!)
}
# 修正丙：our 为负、westock 为正 → 符号冲突
_fake_our['sh600002'] = {'change_pct': 3.0, 'main_inflow': -2e7}

_saved_snap, _saved_check = [], []
# 注意：run() 内部是 `from db.database import save_*`，必须 patch db.database 模块属性，
# patch xc 模块属性无效（函数内 from import 每次从源模块取）。
import db.database as _db
_db.save_westock_snapshot = lambda d, rows: _saved_snap.extend(rows) or len(rows)
_db.save_cross_check = lambda d, rows: _saved_check.extend(rows) or len(rows)
xc.fetch_fund_flow = lambda syms, date=None: _fake_ff
xc.fetch_quotes = lambda syms, date=None: _fake_qt
_orig_collect_universe = xc.collect_universe  # 先存原函数，再打桩（顺序反了会存到桩）
xc.collect_universe = lambda: ['sh600000', 'sh600001', 'sh600002']
_orig_collect_our = xc.collect_our_values        # 先存真实现，下面才打桩（与 collect_universe 同一踩坑）
xc.collect_our_values = lambda *a, **k: dict(_fake_our)

_sum = xc.run(date_key='2099-01-02', dry_run=False)
ck(len(_saved_snap) == 3, '快照落库 3 条', f'实得 {len(_saved_snap)}')
ck(len(_saved_check) == 3, '比对落库 3 条')
_by = {r['symbol']: r for r in _saved_check}
ck(_by['sh600000']['quote_agreed'] is True, '甲：行情一致（容差内）')
ck(_by['sh600001']['quote_agreed'] is False, '乙：行情不一致（2.0 vs 2.5 超容差）')
ck(_by['sh600002']['direction_conflict'] is True, '丙：资金流符号冲突（our -2000万 vs westock +1000万）')
ck(_by['sh600000']['direction_conflict'] is False, '甲：资金流同向无冲突')
ck(abs(_by['sh600000']['inflow_diff'] - (1e8 - 8e7)) < 1, 'inflow_diff = westock - our',
   f"实得 {_by['sh600000']['inflow_diff']}")
ck(abs(_by['sh600000']['inflow_diff_pct'] - 25.0) < 0.01, 'inflow_diff_pct 相对 our 的百分比',
   f"实得 {_by['sh600000']['inflow_diff_pct']}")
ck(_sum['quote_agreed'] == 2 and _sum['quote_checked'] == 3,
   '摘要：行情 2/3 一致（甲丙一致、乙不一致）')
ck(len(_sum['direction_conflicts']) == 1, '摘要：符号冲突 1 只')
# 未比对三态：本地无值的票 quote_agreed=None
_fake_our2 = {'sh600000': {'change_pct': 1.0, 'main_inflow': 8e7}}
xc.collect_our_values = lambda *a, **k: dict(_fake_our2)
_saved_check.clear()
xc.run(date_key='2099-01-03', dry_run=False)
_by2 = {r['symbol']: r for r in _saved_check}
ck(_by2['sh600001']['quote_agreed'] is None, '本地无值 → quote_agreed=None（未比对，不与不一致混淆）')

# ---- 2026-11-05 回归：本地快照非当日必须判「不可校验」而不是写假冲突 ----
# 背景：原实现无条件读最新快照，--date 补跑历史日期时等于用今天行情比前天行情，
# `quote_agreed` 一致率在 1.1%～97.2% 之间乱跳、direction_conflict 被虚增。
from datetime import datetime as _dt
_ts_1104 = _dt(2026, 11, 4, 16, 30).timestamp()
_fake_snap = {'ts': _ts_1104, 'data': {'inflow': [{'symbol': 'sh600000', 'changePercent': 1.0,
                                                    'mainNetInflow': 8e7}], 'outflow': []}}
_db.load_fund_flow_snapshot = lambda k: (dict(_fake_snap) if k == 'stocks'
                                         else {'ts': _ts_1104, 'data': []})
ck(_orig_collect_our('2026-11-02') == {},
   '快照日 11-04 ≠ 校验日 11-02 → 本地侧不可校验（返回空表）')
ck(bool(_orig_collect_our('2026-11-04')),
   '快照日与校验日一致 → 正常返回本地值')
ck(_orig_collect_our() != {}, '不传日期 → 不做日期校验（向后兼容）')
ck(xc._snapshot_date({'ts': _ts_1104}) == '2026-11-04', '_snapshot_date 本地时区取日')
ck(xc._snapshot_date({}) is None and xc._snapshot_date(None) is None, '_snapshot_date 无 ts → None')

_saved_snap.clear(); _saved_check.clear()
xc.collect_our_values = lambda date_key=None: {}
_sum3 = xc.run(date_key='2026-11-02', dry_run=False)
ck(len(_saved_check) == 0, '不可校验 → data_cross_check 一行不写（全 NULL 行会撑大一致率分母）',
   f'实得 {len(_saved_check)} 行')
ck(len(_saved_snap) == 3, '不可校验 → westock 权威快照照常落库（数据不丢）',
   f'实得 {len(_saved_snap)} 行')
ck(_sum3.get('skipped_compare') == 'local_snapshot_not_same_day', '摘要标明跳过原因')
ck(_sum3.get('our_side_usable') is False, '摘要 our_side_usable=False')

# ========== [5] 快照字段映射 ==========
print('[5] 快照字段映射')
_snap_by = {r['symbol']: r for r in _saved_snap}
_r = _snap_by['sh600000']
ck(abs(_r['main_net_inflow'] - 1e8) < 1, 'MainNetFlow → main_net_inflow')
ck(abs(_r['jumbo_net_flow'] - 6e7) < 1, 'JumboNetFlow → jumbo_net_flow')
ck(abs(_r['retail_net_flow'] - 2e7) < 1, '散户净流 = RetailIn - RetailOut')
ck(_r['industry_rank'] == 1, 'industry_rank 转 int')
ck(abs(_r['main_net_5d'] - 1e6) < 1, 'MainNetFlow5D → main_net_5d')

# ========== [6] 宇宙构建 ==========
print('[6] 宇宙构建与封顶')
xc.collect_universe = _orig_collect_universe  # 恢复 [4] 节 patch 掉的真实函数
import db.database as _db
_db.load_fund_flow_snapshot = lambda k: {'data': {'inflow': [{'symbol': f'sh{i:06d}'} for i in range(60)],
                                                  'outflow': [{'symbol': f'sz{i:06d}'} for i in range(60)]}} if k == 'stocks' else None
_db.load_hot_search_ranking = lambda: {'stocks': [{'symbol': f'sh{i:06d}'} for i in range(50)]}
_db.load_daily_recommendations = lambda: [{'symbol': f'bj{i:06d}'} for i in range(30)]
_syms = xc.collect_universe()
ck(len(_syms) == 130, '宇宙去重合并（inflow前50+outflow前50+热搜50全重叠+推荐30bj = 130）',
   f'实得 {len(_syms)}')
ck(len(_syms) <= xc._UNIVERSE_CAP, '不超过封顶 200')
ck(len(set(_syms)) == len(_syms), '无重复')
ck(_syms == sorted(_syms), '排序稳定（输出确定）')

# ========== [7] 限频退避重试 ==========
print('[7] 限频退避重试（桩 _westock）')
_orig_westock = xc._westock
_calls = {'n': 0}


def _flaky(args, timeout=30):
    _calls['n'] += 1
    if _calls['n'] == 1:
        return '[SERVER_RATE_LIMITED] 服务限频'  # 第 1 次限频
    return '| code | name |\n| --- | --- |\n| sh600000 | 甲 |'  # 第 2 次成功


xc._westock = _flaky
xc._RATE_LIMIT_BACKOFF = 0.01  # 自测不真等
_out = xc._fetch_batched(['sh600000'], 10, ['fund', 'flow'], 0)
ck('sh600000' in _out, '限频后重试成功拿到数据')
ck(_calls['n'] == 2, '只调了 2 次（第 1 次限频、第 2 次成功）', f'实得 {_calls["n"]}')


def _always_limited(args, timeout=30):
    _calls['n'] += 1
    return '[SERVER_RATE_LIMITED] 服务限频'


_calls['n'] = 0
xc._westock = _always_limited
_out2 = xc._fetch_batched(['sh600000'], 10, ['fund', 'flow'], 0)
ck(_out2 == {} and _calls['n'] == xc._RATE_LIMIT_RETRY + 1,
   f'持续限频：重试 {xc._RATE_LIMIT_RETRY} 次后放弃（不无限打）', f'实得 {_calls["n"]} 次')
xc._westock = _orig_westock

# ========== 汇总 ==========
print()
print(f'===== 结果：{len(PASS)} 通过 / {len(FAIL)} 失败 =====')
if FAIL:
    for f in FAIL:
        print(f'  ✗ {f}')
    sys.exit(1)
print('全部通过 ✓')
