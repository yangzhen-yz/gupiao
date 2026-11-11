"""focus_logic_selftest.py - 买入逻辑卡 / 封板票 T+1 竞价条件单 / 主线裁决闸门的长期回归自测

守的线上不变量（2026-11-10 修正）：
  1. 逻辑卡只摆已落库事实，缺数据只写 gaps，绝不编「利好」、绝不补 0；
  2. 逻辑卡**不改选票结果**（code/score/expectedWinRate 原样，只新增 logic 键）；
  3. **T 日与 T+1 必须分开**：今日封板＝今日盘中买不进（不追），
     不等于明日 9:25 竞价不可介入。封板票必须保留条件式买入计划，
     不许再出现「不可执行·仅观察 / blocked」一刀切；
  4. 竞价分档卡只作影子方向参考：必须标「未裁决」、不得下「可买/放行」指令、
     不得把主线当放行条件（主线判据 INSUFFICIENT）；
  5. 主线影子判据没到时闸门必须拒绝上线，且**不是永远拒绝**（样本够要能 READY）；
  6. **行动台不得再裁字段**：收评 focus_json 里的 `logic` / `reason` / `auctionGuard`
     必须透传到「明日计划」卡片；「可介入」卡片复用同一套聚合但不得覆盖已有的
     字符串 `logic`（评分理由）。

合成数据 + 打桩，不碰生产库、不发网络请求（`_auction_guard` 一律注入 basis，
`attach_focus_logic` 的三张表读取打桩）。
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


print('=== 买入逻辑卡 / T+1 竞价条件单 / 主线闸门 / 行动台透传 回归自测 ===')

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
          and any('T+1' in r for r in card['risk'])
          and not any('不提供买入动作' in r or '仅观察' in r for r in card['risk']))
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

# [11] 封板票必须保留完整 T+1 条件式计划（反证：不许再一刀切拦掉）
q_sealed = {'changePercent': 10.01, 'amplitude': 2.0, 'highRaw': 60.9, 'priceRaw': 60.9}
bt = rv._simple_buy_timing(q_sealed)
plan = rv._build_tomorrow_plan(q_sealed, bt)
plan['auctionGuard'] = rv._auction_guard(basis={})
f = {'buyTiming': bt, 'tomorrowPlan': plan}
check(11, 'execBlocked' not in f and 'blocked' not in plan
      and all(k in plan for k in ('buyLow', 'buyHigh', 'takeProfit', 'stopLoss', 'position', 'veto'))
      and '次日竞价挂单' in plan['buyNote']
      and plan['stopLossPct'] <= rv._TP_STOP_MAX_LIMIT_UP * 100 + 1e-6
      and any('一字板' in v for v in plan['veto']),
      json.dumps(plan, ensure_ascii=False)[:300])

# [12] 时机判定：T 日/T+1 分开说，非封板不受影响（反证）
bt2 = rv._simple_buy_timing({'changePercent': 3.0, 'amplitude': 2.0,
                             'highRaw': 10.3, 'priceRaw': 10.3})
check(12, bt['type'] == 'auction' and '条件单' in bt['label']
      and '今日' in bt['note'] and '明日' in bt['note']
      and '已封板·不追' not in bt['label'] and '仅观察' not in bt['note']
      and bt2['type'] != 'auction' and '条件单' not in bt2['label'])

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

# ---- 17~23：竞价分档卡的边界与反证（2026-11-10 新增） ----
BASIS = {
    'deep_dip': {'n': 38, 'mean': 1.559},
    'mild_dip': {'n': 145, 'mean': 0.691},
    'flat': {'n': 132, 'mean': 0.379},
    'high': {'n': 23, 'mean': -0.207},
    'chase': {'n': 135, 'mean': -1.425},
}
g = rv._auction_guard(basis=BASIS)

# [17] 五档齐全、方向与注入数据一致、绝不把影子方向写成放行指令
check(17, [a['key'] for a in g['arms']] == ['deep_dip', 'mild_dip', 'flat', 'high', 'chase']
      and [a['direction'] for a in g['arms']] ==
      ['positive', 'positive', 'positive', 'negative', 'negative']
      and all('仅参考' in a['action'] and '买' not in a['action'] for a in g['arms'])
      and not any(k in json.dumps(g, ensure_ascii=False)
                  for k in ('可以买入', '建议买入', '可介入即买', '已上线')))

# [18] 影子未裁决 + 主线判据不足，两者都不得写成放行依据
check(18, '未裁决' in g['shadowStatus'] and '不构成买入依据' in g['shadowStatus']
      and 'INSUFFICIENT' in g['mainlineNote'] and '不作放行条件' in g['mainlineNote'])

# [19] 时间维度必须明确分离，且现行条件单与影子方向的矛盾要摆出来（不掩盖、不偷改参数）
check(19, '今日' in g['timeSplit'] and '独立决策' in g['timeSplit']
      and '不覆盖条件单' in g['rule']
      and '口径矛盾' in g['conflict'] and '未改参数' in g['conflict']
      and '按现行条件单执行' in g['conflict'])

# [20] basis 缺失优雅降级：不编数、不填 0、不抛异常
g0 = rv._auction_guard(basis={})
check(20, g0['basisAvailable'] is False
      and all(a['mean'] is None and a['direction'] == 'unknown' for a in g0['arms'])
      and '不填数' in g0['basisNote'])

# [21] 小样本档不许给出方向（防止 n=1 的极端值被当成结论）
g_small = rv._auction_guard(basis={'deep_dip': {'n': 3, 'mean': 99.0},
                                   'chase': {'n': 2, 'mean': -99.0}})
check(21, all(a['direction'] == 'unknown' and a['mean'] is None
              for a in g_small['arms']))

# [22] 反证：源码里不许再出现一刀切封板拦截（防止回退到旧口径）
import inspect
_src = inspect.getsource(rv)
check(22, '_mark_sealed_blocked' not in _src
      and "'blocked': True" not in _src
      and '已封板·不追' not in _src)

# [23] 非封板票绝不挂竞价分档卡（不越界）
q_normal = {'changePercent': 3.0, 'amplitude': 2.0, 'highRaw': 10.3, 'priceRaw': 10.3}
plan_n = rv._build_tomorrow_plan(q_normal, bt2)
check(23, 'auctionGuard' not in plan_n and 'blocked' not in plan_n
      and plan_n['stopLossPct'] > 0)

# ---- 24~31：行动台买入逻辑卡透传（2026-11-10 新增） ----
from services.ops import action_desk as ad  # noqa: E402

_FAKE_FOCUS = [{
    'code': 'sz000525', 'name': '红太阳', 'score': 78,
    'expectedWinRate': 60.3, 'expectedAvgRet': 1.48, 'pctChg': 10.01,
    'price': 8.88, 'reason': '融合分78分，委比为正',
    'buyTiming': {'type': 'auction', 'label': '次日竞价·条件单'},
    'logic': {'chainText': '今日热门链：农药链', 'poolText': '涨停池', 'gaps': []},
    'tomorrowPlan': {'buyLow': 8.5, 'auctionGuard': {'title': '竞价分档'}},
}]

# [24] 投影层不许再裁掉买入逻辑 / 评分理由 / T+1 竞价条件单（本批修的正是这一层）
p = ad._project_tomorrow_plan(_FAKE_FOCUS)[0]
check(24, p['symbol'] == 'sz000525' and p['winRate'] == 60.3
      and p['score'] == 78 and p['pctChg'] == 10.01
      and p['logic']['chainText'] == '今日热门链：农药链'
      and p['reason'] == '融合分78分，委比为正'
      and p['timingLabel'] == '次日竞价·条件单'
      and p['plan']['buyLow'] == 8.5
      and p['plan']['auctionGuard']['title'] == '竞价分档')

# [25] 旧收评没有逻辑卡时如实留空，绝不编理由（前端会标注「下一份收评起提供」）
p_old = ad._project_tomorrow_plan([{'code': 'sh600000', 'name': 'X', 'reason': None}])[0]
check(25, p_old['logic'] == {} and p_old['reason'] == '' and p_old['plan'] == {}
      and ad._project_tomorrow_plan([]) == [])

# [26][27] 复用聚合时的 key 参数：不覆盖评分理由字符串；默认参数行为不变
_orig = (fr._pool_data, fr._chain_index, fr._latest_mainline)
fr._pool_data = lambda d: ({}, '')
fr._chain_index = lambda: ({}, '', 0)
fr._latest_mainline = lambda: {}
try:
    items = [{'symbol': 'sz000525', 'changePercent': 10.01,
              'logic': '评分理由原文', 'score': 78}]
    n = fr.attach_focus_logic(items, '2026-11-10', code_key='symbol',
                              pct_key='changePercent', out_key='themeLogic')
    card = items[0]['themeLogic']
    check(26, n == 1 and items[0]['logic'] == '评分理由原文'
          and card['sealed'] is True  # changePercent 10.01 触发封板兜底
          and card['chainText'] == '热门链：暂无数据'
          and any('四池无该票' in g for g in card['gaps']),
          json.dumps(card, ensure_ascii=False)[:240])

    items2 = [{'code': 'sz000525', 'pctChg': 2.0}]
    n2 = fr.attach_focus_logic(items2, '2026-11-10')
    check(27, n2 == 1 and 'logic' in items2[0] and 'themeLogic' not in items2[0]
          and not items2[0]['logic']['sealed'])
finally:
    fr._pool_data, fr._chain_index, fr._latest_mainline = _orig

# [28] 空名单不读库、不抛异常
check(28, ad._attach_buy_theme_logic([], '2026-11-10') == 0)

# [29] 聚合失败必须静默：可介入名单不能因为逻辑卡挂掉而整块消失
_orig_attach = fr.attach_focus_logic


def _boom(*a, **kw):
    raise RuntimeError('db down')


fr.attach_focus_logic = _boom
try:
    items3 = [{'symbol': 'sz000525'}]
    check(29, ad._attach_buy_theme_logic(items3, '2026-11-10') == 0
          and 'themeLogic' not in items3[0])
finally:
    fr.attach_focus_logic = _orig_attach

# [30] 反证：源码里不许回退到内联投影（历史上正是它裁掉了逻辑卡）
_ad_src = inspect.getsource(ad)
check(30, "'tomorrowPlan': _project_tomorrow_plan(" in _ad_src
      and "'tomorrowPlan': [{'symbol'" not in _ad_src
      and "'logic': f.get('logic') or {}" in _ad_src
      and "'reason': f.get('reason') or ''" in _ad_src
      and '_attach_buy_theme_logic(buy_now + deferred' in _ad_src)

# [31] 前端两处卡片必须都渲染逻辑卡，且评分理由要标明「非买入理由」
_vue = open(os.path.join(os.path.dirname(_SERVER), 'client-vue', 'src',
                         'components', 'tabs', 'TabActionDesk.vue'),
            encoding='utf-8').read()
check(31, all(k in _vue for k in (
    'logicHas(t.logic)', 'logicHas(b.themeLogic)', '评分依据（非买入理由）',
    '不构成买入背书', 'T 日不追 + T+1 条件化竞价单',
    't.plan?.auctionGuard', 't.plan?.blocked')))

# [32] 「稍后时机」投影不许再把刚挂上的 themeLogic 裁掉（与 [30] 同类回归）
_deferred = [{'symbol': 'sz000525', 'name': 'X', 'timingLabel': '尾盘',
              'score': 71, 'vetoReason': '情绪退潮·涨幅已过5%不追高',
              'themeLogic': {'chainText': '主线链条命中', 'poolDay': '2026-11-10'}}]
_proj_d = ad._project_deferred_buys(_deferred)[0]
check(32, _proj_d['themeLogic']['chainText'] == '主线链条命中'
      and _proj_d['vetoReason'] == '情绪退潮·涨幅已过5%不追高'
      and _proj_d['symbol'] == 'sz000525'
      and ad._project_deferred_buys([]) == [])

# [33] 反证：不许回退到内联投影；前端稍后时机 chip 必须真的显示一行理由
check(33, "'deferredBuys': _project_deferred_buys(deferred)" in _ad_src
      and "'deferredBuys': [{'symbol'" not in _ad_src
      and 'logicBrief(d.themeLogic)' in _vue
      and 'deferred-logic' in _vue)

_TOTAL = 33
assert len(_PASSED) == _TOTAL, f'用例数不对: {len(_PASSED)} != {_TOTAL}'
print(f'\n=== 全部通过 {len(_PASSED)}/{_TOTAL} ===')
