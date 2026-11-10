"""focus_logic_selftest.py - 买入逻辑卡 / 封板拦截 / 主线裁决闸门的长期回归自测

守的线上不变量：
  1. 逻辑卡只摆已落库事实，缺数据只写 gaps，绝不编「利好」、绝不补 0；
  2. 逻辑卡**不改选票结果**（code/score/expectedWinRate 原样，只新增 logic 键）；
  3. 已封板票在收评里不给任何可执行买入动作（无买点/止盈/仓位、文案不诱导）；
  4. 主线影子判据没到时闸门必须拒绝上线，且**不是永远拒绝**（样本够要能 READY）。

全部用合成数据 + 打桩，不碰生产库、不发网络请求。
用法：python server/scripts/focus_logic_selftest.py
"""
import json
import os
import sys

_SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SERVER)
sys.path.insert(0, os.path.join(_SERVER, 'scripts'))

from services.ops import focus_reasons as fr          # noqa: E402
from services.ops import review as rv                 # noqa: E402
import mainline_shadow_verdict as mv                 # noqa: E402

_PASSED = []


def check(n, cond, msg=''):
    assert cond, f'[{n}] FAILED: {msg}'
    _PASSED.append(n)
    print(f'[{n}] OK')


POOL_FULL = {
    'pool': 'continuous_up_pool', 'symbol': '000525', 'code': '000525',
    'name': '红太阳', 'days': 2, 'sealTime': '2026-11-09 13:56:48',
    'reason': '农药|公司产品范围涵盖除草剂、杀虫剂、杀菌剂' * 12,
    'themes': ['农药'], 'plates': ['化工', '农药', '草甘膦'],
}
CHAIN = {'name': '汽车链', 'limit_up_num': 4, 'change_pct': 1.23,
         'member_count': 40, 'trade_date': '2026-11-10'}
ML_LEADER = {'date': '2026-11-10',
             'main': [{'name': '软件开发', 'leaders': [{'code': '000525'}]}],
             'sub': [{'name': '电子'}]}
ML_NONE = {'date': '2026-11-10', 'main': [], 'sub': [{'name': '电子'}]}
ML_UNREL = {'date': '2026-11-10',
            'main': [{'name': '软件开发', 'leaders': []}], 'sub': []}


def mk_days(days, with_main=True):
    p = json.dumps({'main': ([{'name': '软件开发', 'leaders': []}] if with_main else []),
                    'sub': []})
    return {d: [(1, p)] for d in days}


print('=== 买入逻辑卡 / 封板拦截 / 主线闸门 回归自测 ===')

# [1] 代码归一化
check(1, fr._plain('sz000525') == '000525'
      and fr._plain('000525') == '000525'
      and fr._plain('SH600609') == '600609'
      and fr._plain('') == '')

# [2] 数据齐全：链条/题材/归因/板块/连板/封板/主线龙头全部到位，长文本要截断
c = fr.build_card('sz000525', POOL_FULL, [CHAIN], ML_LEADER, '2026-11-10', '2026-11-09')
check(2, c['chainHit'] and '汽车链' in c['chainText']
      and c['themeText'] == '农药' and len(c['upReason']) <= fr._MAX_REASON + 1
      and c['plates'] == ['化工', '农药', '草甘膦']
      and '2 连板' in c['poolText'] and '首封' in c['poolText']
      and c['sealed'] and not c['blowup']
      and c['mainlineHit'] is True and c['mainlineStatus'] == 'leader'
      and not c['gaps'] and c['poolDay'] == '2026-11-09',
      json.dumps(c, ensure_ascii=False)[:300])

# [3] 全缺：只标缺口，绝不编利好/补 0
c = fr.build_card('sh601599', None, [], {}, '', '')
check(3, c['poolText'] == '未上四池（非涨停票）'
      and c['chainText'] == '热门链：暂无数据'
      and c['mainlineText'] == '主线判定：暂无数据'
      and c['upReason'] == '' and c['themeText'] == '' and c['plates'] == []
      and not c['sealed'] and len(c['gaps']) >= 3
      and '利好' not in json.dumps(c, ensure_ascii=False),
      json.dumps(c, ensure_ascii=False)[:300])

# [4] 反证：主线「未形成」≠「不在主线」，风险文案不许混写
c1 = fr.build_card('000525', None, [], ML_NONE, '2026-11-10', '2026-11-09')
c2 = fr.build_card('000525', None, [], ML_UNREL, '2026-11-10', '2026-11-09')
check(4, c1['mainlineStatus'] == 'no_main' and c1['mainlineHit'] is None
      and any('不可判' in r for r in c1['risk'])
      and not any('不在当前主线' in r for r in c1['risk'])
      and c2['mainlineStatus'] == 'unrelated'
      and any('不在当前主线' in r for r in c2['risk']))

# [5] 支线 / 主线板块标签匹配必须标注「近似口径」
row = dict(POOL_FULL)
row['plates'], row['themes'] = ['电子', '光通信'], []
c = fr.build_card('000525', row, [], ML_NONE, '2026-11-10', '2026-11-09')
check(5, c['mainlineStatus'] == 'sub'
      and '板块标签匹配口径' in c['mainlineText'])
row2 = dict(row)
row2['plates'] = ['软件开发']
c = fr.build_card('000525', row2, [], ML_UNREL, '2026-11-10', '2026-11-09')
check(6, c['mainlineStatus'] == 'main' and c['mainlineHit'] is True)

# [7] 炸板池不是封板池，风险文案要分开
row = dict(POOL_FULL)
row['pool'] = 'up_open_pool'
c = fr.build_card('000525', row, [], ML_UNREL, '2026-11-10', '2026-11-09')
check(7, c['blowup'] and not c['sealed']
      and any('炸板' in r for r in c['risk']))

# [8][9] attach：行情兜底判封板 + 反证「绝不改选票结果」
_orig = (fr._pool_data, fr._chain_index, fr._latest_mainline)
try:
    fr._pool_data = lambda td: ({}, '2026-11-09')
    fr._chain_index = lambda: ({}, '2026-11-10', 0)
    fr._latest_mainline = lambda: {}
    items = [{'code': 'sh601599', 'name': '浙文影业', 'pctChg': 10.02,
              'score': 80, 'expectedWinRate': 59.5}]
    before = dict(items[0])
    n = fr.attach_focus_logic(items, '2026-11-09')
    card = items[0]['logic']
    check(8, n == 1 and card['sealed']
          and any('不提供买入动作' in r for r in card['risk']))
    check(9, all(items[0][k] == v for k, v in before.items())
          and set(items[0]) - set(before) == {'logic'},
          '逻辑卡偷偷改了选票字段')
finally:
    fr._pool_data, fr._chain_index, fr._latest_mainline = _orig

# [10] 数据源异常必须静默，绝不让收评生成中断
try:
    def _boom(_td):
        raise RuntimeError('boom')
    fr._pool_data = _boom
    items2 = [{'code': 'sz000525'}]
    check(10, fr.attach_focus_logic(items2, '2026-11-09') == 0
          and items2 == [{'code': 'sz000525'}])
finally:
    fr._pool_data = _orig[0]

# [11] 封板拦截对象不含任何可执行买点
f = {}
rv._mark_sealed_blocked(f)
check(11, f['execBlocked'] is True and f['tomorrowPlan']['blocked'] is True
      and not any(k in f['tomorrowPlan'] for k in ('buyLow', 'buyHigh', 'takeProfit', 'stopLoss', 'position'))
      and '次日竞价' not in json.dumps(f, ensure_ascii=False))

# [12] 时机判定：封板不诱导，非封板不受影响（反证）
bt = rv._simple_buy_timing({'changePercent': 10.0, 'amplitude': 2.0,
                            'highRaw': 11.0, 'priceRaw': 11.0})
bt2 = rv._simple_buy_timing({'changePercent': 3.0, 'amplitude': 2.0,
                             'highRaw': 10.3, 'priceRaw': 10.3})
check(12, bt['label'] == '已封板·不追' and '竞价参与' not in bt['note']
      and bt2['label'] != '已封板·不追' and '竞价参与' not in bt2['note'])

# [13][14] 闸门：样本不足拒绝 / 样本足够放行（反证，防止闸门永远拒绝）
days13 = [f'2026-01-{d:02d}' for d in range(1, 14)]
r = mv.evaluate(mk_days(days13), set(days13), '2099-01-01', max(days13))
check(13, r['verdict'] == 'INSUFFICIENT'
      and any('完整交易日' in g for g in r['gaps']) and '禁止' in r['action'])
days16 = [f'2026-02-{d:02d}' for d in range(1, 17)]
r = mv.evaluate(mk_days(days16), set(days16), '2099-01-01', max(days16))
check(14, r['verdict'] == 'READY' and not r['gaps'] and r['mainDays'] == 16)

# [15] 主线覆盖不足：交易日够但多数日子因子为空，仍不许裁决
by = mk_days(days16)
for d in days16[3:]:
    by[d] = [(1, json.dumps({'main': [], 'sub': []}))]
r = mv.evaluate(by, set(days16), '2099-01-01', max(days16))
check(15, r['verdict'] == 'INSUFFICIENT'
      and any('覆盖仅' in g for g in r['gaps']))

# [16] 盘中翻转要被记进缺口
by = mk_days(days16)
for d in days16[:2]:
    by[d] = [(1, json.dumps({'main': [], 'sub': []})),
             (2, json.dumps({'main': [{'name': 'X', 'leaders': []}], 'sub': []}))]
r = mv.evaluate(by, set(days16), '2099-01-01', max(days16))
check(16, any('翻转' in g for g in r['gaps']) and r['intradayFlipDays'] == 2)

print(f'\n=== 全部通过 {len(_PASSED)}/{16} ===')
