"""ann_unknown_selftest.py - 利空公告"三态"的回归自测（2026-11-07）

守的不变量：**"没查到公告"永远不能等于"这只票没有利空"**。
11-06 的熔断改造给公告接口接上闸门后，`skip('ann')` 走的是"返回空列表、不抛异常"的老契约
（原注释直接写着"调用方无感"），而趋势扫描那句注释写的是"失败不阻塞，仅视为无利空"——
两条合起来的结果是：**东财公告被停调的那天，利空否决整块失效，而且没有任何地方看得见它失效了**。

修法分两层，自测也按两层钉：
  · 判定层一字不变：unknown 时 `hasNegative` 仍是 False，**绝不把接口抖动判成利空**
    （与 `momentum_zone.py` 开头"没有数据不做判决"同口径）；
  · 动作层保守：`details.annStatus` 逐票留痕 → 本轮 unknown 只数进结果（`annUnknown`）→
    状态非 ok 的高分票**不进"自动加用户股票池"**（那是写进每个人自选池的不可逆动作）。

用例：
  [1] ok / ok-空列表 / cooling / error 四态的可观测差别（含"空列表≠没查到"）
  [2] 老契约不破：`fetch_stock_announcements` 三种情况都返回 list、不抛异常
  [3] 缓存分档：unknown 只缓存 60 秒（闸门一解除就回到真数据），ok 仍按 3600 秒
  [4] `ann_rollup` 的计数与"老数据没有该键按 ok 算"（不倒推历史说"当时也瞎了"）
  [5] 自动加池守卫：cooling 的高分票不加；开关关掉则照加（逐字节回到改造前）
  [6] 参数口径：`stock_list` 必须剥掉 sh/sz/bj 前缀 —— 这是"利空否决从来没生效过"的正主，
      带前缀时接口回 200 但恒空，所以这条断言直接钉住发出去的参数值，而不是钉返回值
  [7] 关键词校准：实测九类误命中放行、六类真利空一条不能放、`excludedHits` 必须留痕、
      关掉开关逐字节回到原关键词表
  [8] HTTP 200 但 `data` 结构不对 → error/unknown（"结构异常"与"确实没公告"不同形）
全程假 requests + 假闸门 + 假时钟，零外部请求、零真库写入（连股票池都是打桩的）。
"""
import asyncio
import json
import os
import sys
import types
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import services.data.stock as stk                    # noqa: E402
import services.strategy.trend as tr                 # noqa: E402
from services.data import eastmoney_guard as g       # noqa: E402

PASS, FAIL = [], []
CNT = {'req': 0}
NOW = [1_000_000.0]
TODAY = date.today().isoformat()

_real_save_state = g._save_state_locked
g._save_state_locked = lambda: None                  # 别污染真实熔断状态文件


def ck(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(f"  [{'✓' if cond else '✗'}] {msg}")


def resp(payload):
    return types.SimpleNamespace(
        status_code=200,
        raise_for_status=lambda: None,
        json=lambda: payload,
        text=json.dumps(payload))


ANN_HIT = {'data': {'list': [{'title': '关于2026年度业绩预亏的公告', 'art_code': 'x1',
                             'notice_date': TODAY, 'columns_name': '业绩预告'}]}}
ANN_NONE = {'data': {'list': []}}

MODE = {'kind': 'hit', 'skip': False}
LAST = {'stock_list': None, 'url': None}
_real_skip = g.skip
_real_time = stk.time


def fake_get(url, params=None, headers=None, timeout=None):
    CNT['req'] += 1
    LAST['stock_list'] = (params or {}).get('stock_list')
    if MODE['kind'] == 'boom':
        raise RuntimeError('connection reset by peer')
    if MODE['kind'] == 'badstruct':
        return resp({'data': None})          # HTTP 200 但结构不对：不能折算成"没有公告"
    return resp(ANN_HIT if MODE['kind'] == 'hit' else ANN_NONE)


g.skip = lambda scope, *a, **k: MODE['skip'] if scope == 'ann' else _real_skip(scope)
stk.requests = types.SimpleNamespace(get=fake_get)
stk.time = types.SimpleNamespace(time=lambda: NOW[0])        # 假时钟，只用于缓存分档


def run(coro):
    return asyncio.run(coro)


def reset(sym):
    stk._announcement_cache.pop(sym.lower(), None)
    CNT['req'] = 0


try:
    # ========== [1] 四态 ==========
    print('[1] announcement_check 四态：ok有 / ok空 / cooling / error')
    S = 'sh600001'
    reset(S)
    MODE['kind'], MODE['skip'] = 'hit', False
    r1 = run(stk.announcement_check(S))
    ck(r1['status'] == 'ok' and r1['hasNegative'] is True and len(r1['hits']) == 1,
       f'查到命中利空 → status=ok / hasNegative=True，实得 {r1["status"]}/{r1["hasNegative"]}')
    ck(r1['unknown'] is False and r1['reason'] == '', '命中利空时不算 unknown、不给 reason')

    reset(S)
    MODE['kind'] = 'none'
    r2 = run(stk.announcement_check(S))
    ck(r2['status'] == 'ok' and r2['hasNegative'] is False and r2['unknown'] is False,
       '查过、确实没有公告 → status=ok（这才是"没有利空"的正确形状）')

    reset(S)
    MODE['kind'], MODE['skip'] = 'none', True
    CNT['req'] = 0
    r3 = run(stk.announcement_check(S))
    ck(r3['status'] == 'cooling' and r3['unknown'] is True,
       f'我们自己停调 → status=cooling + unknown=True，实得 {r3["status"]}')
    ck(CNT['req'] == 0, '停调期间一个请求都不发（闸门语义保持）')
    ck(r3['hasNegative'] is False, '★ unknown 不等于利空：判定层没被改动（不拿抖动当利空）')
    ck('停调' in r3['reason'] or '冷却' in r3['reason'], f'reason 说清了原因：{r3["reason"]}')

    reset(S)
    MODE['kind'], MODE['skip'] = 'boom', False
    r4 = run(stk.announcement_check(S))
    ck(r4['status'] == 'error' and r4['unknown'] is True,
       f'发了但失败 → status=error + unknown=True，实得 {r4["status"]}')
    ck(r4['hasNegative'] is False, 'error 也不判成利空')
    ck(r4['status'] != r3['status'], 'cooling 与 error 可区分（谁停的、为什么停，事后能审）')

    # ========== [2] 老契约不破 ==========
    print('[2] 历史入口 fetch_stock_announcements：三态下都返回 list、不抛异常')
    for kind, skip in (('hit', False), ('none', True), ('boom', False)):
        reset('sz000002')
        MODE['kind'], MODE['skip'] = kind, skip
        out = run(stk.fetch_stock_announcements('sz000002'))
        ck(isinstance(out, list), f'kind={kind} 返回 list（长度 {len(out)}），老调用方无感')
    reset('sz000002')
    MODE['kind'], MODE['skip'] = 'hit', False
    legacy = run(stk.has_negative_announcement('sz000002'))
    ck(legacy.get('hasNegative') is True and 'hits' in legacy,
       'has_negative_announcement 的 hasNegative/hits 两个键一字未改（向后兼容）')
    ck(legacy.get('status') == 'ok' and legacy.get('unknown') is False,
       '同名入口也带上新键，消费方不用换函数')

    # ========== [3] 缓存分档 ==========
    print('[3] 缓存：unknown 只按 60 秒复用，ok 仍按 3600 秒')
    reset('sh600003')
    MODE['kind'], MODE['skip'] = 'none', True
    NOW[0] = 1_000_000.0
    run(stk.announcement_check('sh600003'))
    CNT['req'] = 0
    NOW[0] += 30
    r30 = run(stk.announcement_check('sh600003'))
    ck(CNT['req'] == 0 and r30['status'] == 'cooling', '30 秒内复用缓存（不给东财攒封禁）')
    NOW[0] += 40                                    # 距首次 70 秒 > 60
    MODE['skip'] = False                            # 闸门解除
    r70 = run(stk.announcement_check('sh600003'))
    ck(CNT['req'] == 1, '超过 60 秒就重新问（闸门一解除立刻回到真数据，不让一次抖动伪装一小时）')
    ck(r70['status'] == 'ok', f'重问后拿到 ok，实得 {r70["status"]}')

    CNT['req'] = 0
    NOW[0] += 1200
    run(stk.announcement_check('sh600003'))
    ck(CNT['req'] == 0, 'ok 状态 20 分钟内仍复用（正常 TTL 3600 秒没被改短）')
    NOW[0] += 2500                                  # 距上次 ok 写入 3700 秒
    run(stk.announcement_check('sh600003'))
    ck(CNT['req'] == 1, 'ok 超过 3600 秒才重取')

    # ========== [4] ann_rollup ==========
    print('[4] ann_rollup：unknown 只数要真数得出来，老数据不倒推成"当时也瞎"')
    rows = [{'details': {'annStatus': 'ok', 'annCount': 3}},
            {'details': {'annStatus': 'cooling'}},
            {'details': {'annStatus': 'error'}},
            {'details': {}}, {'score': 1}]
    a = tr.ann_rollup(rows)
    ck(a['checked'] == 5, f'查过只数 = 5（每条结果都计入分母），实得 {a}')
    ck(a['unknown'] == 2, f'没查到只数 = 2（cooling + error），实得 {a["unknown"]}')
    ck(a['withAnn'] == 1 and a['checkedOk'] == 1, f'成功只数与"有公告"只数 = 1/1，实得 {a}')
    ck(a['suspect'] is False, '成功只数不到 50 → 不下"接口改版"的可疑结论（分母太小）')
    b = tr.ann_rollup([{'details': {'annStatus': 'ok', 'annCount': 0}} for _ in range(60)])
    ck(b['suspect'] is True, '查过 60 只全部公告为 0 → 报可疑（11-07 那个"带前缀恒空"的形状）')
    c = tr.ann_rollup([{'details': {'annStatus': 'ok', 'annCount': 1}} for _ in range(60)])
    ck(c['suspect'] is False, '只要有一条查到公告就不报可疑')
    d = tr.ann_rollup([{'details': {'annStatus': 'cooling'}} for _ in range(60)])
    ck(d['suspect'] is False and d['checkedOk'] == 0,
       '全是停调（没有成功样本）不报可疑 —— 那是闸门的事，不是接口改版')
    ck(tr.ann_rollup([{'details': {'a': 1}}])['unknown'] == 0,
       '老结果没有 annStatus 键 → 不判 unknown（历史行不能倒推成"当时也瞎了"）')
    e = tr.ann_rollup([])
    ck(e['checked'] == 0 and e['suspect'] is False, '空名单不炸')

    # ========== [5] 自动加池守卫 ==========
    print('[5] 高分自动加池：公告没查到的票跳过；开关关掉照加（回到改造前）')
    _pool_load, _pool_save, _users = tr.load_user_scan_pool, tr.save_user_scan_pool, tr.list_user_ids
    _thr = tr.TREND_AUTO_ADD_POOL_THRESHOLD
    tr.list_user_ids = lambda: [1]
    tr.TREND_AUTO_ADD_POOL_THRESHOLD = 70
    ADDED = []

    def fake_load(user_id=None):
        return []

    def fake_save(pool, user_id=None):
        ADDED[:] = list(pool)
        return True

    tr.load_user_scan_pool = fake_load
    tr.save_user_scan_pool = fake_save
    hi_unknown = [{'symbol': 'sh600010', 'name': '未知票', 'score': 88,
                   'details': {'annStatus': 'cooling', 'annReason': '东财公告停调'}}]
    hi_ok = [{'symbol': 'sh600011', 'name': '正常票', 'score': 88,
              'details': {'annStatus': 'ok'}}]
    tr.ANN_UNKNOWN_POOL_GUARD = True
    added_unknown = tr.auto_add_high_score_to_pool(hi_unknown)
    ck(added_unknown == [], 'cooling 的高分票不进池（不可逆动作要等有数据）')
    added_ok = tr.auto_add_high_score_to_pool(hi_ok)
    ck(added_ok == ['sh600011'], f'ok 的高分票照常进池，实得 {added_ok}')
    tr.ANN_UNKNOWN_POOL_GUARD = False
    added_off = tr.auto_add_high_score_to_pool(hi_unknown)
    ck(added_off == ['sh600010'], f'开关关掉＝回到改造前（照样加），实得 {added_off}')
    tr.ANN_UNKNOWN_POOL_GUARD = True
    tr.load_user_scan_pool, tr.save_user_scan_pool, tr.list_user_ids = _pool_load, _pool_save, _users
    tr.TREND_AUTO_ADD_POOL_THRESHOLD = _thr

    # ========== [6] 参数口径：stock_list 必须是裸 6 位码 ==========
    # 这一条是 11-07 查出"利空否决从来没生效过"的正主：带前缀时接口回 200 但 list 恒空，
    # 老代码传的就是带前缀的 sym，于是每一次"成功"都返回空、被当成"这只票没有利空"。
    print('[6] stock_list 必须剥掉 sh/sz/bj 前缀（带前缀 200 但恒空，就是原始病灶）')
    for sym, bare in (('sh600519', '600519'), ('sz000001', '000001'), ('bj920000', '920000')):
        reset(sym)
        MODE['kind'], MODE['skip'] = 'hit', False
        r6 = run(stk.announcement_check(sym))
        ck(LAST['stock_list'] == bare,
           f'{sym} → stock_list={LAST["stock_list"]}（应为裸码 {bare}）')
        ck(r6['status'] == 'ok' and r6['annCount'] == 1 and r6['hasNegative'] is True,
           f'{sym} 剥前缀后取到公告并命中利空（老写法这里永远是 ok/0 条）')

    # ========== [7] 关键词校准（取数修好后，误命中就是新的伤害） ==========
    print('[7] NEG_ANN_CALIBRATED：九类实测误命中要放行，真利空一个不能放')
    FALSE_HITS = [
        'ST绝味:2026年半年度权益分派实施公告',
        'ST绝味:关于撤销其他风险警示暨临时停牌的公告',
        '上海环境集团股份有限公司重大事项报告制度',
        '人民网股份有限公司防范控股股东及关联人资金占用管理办法',
        '杉杉股份关于最近五年被证券监管部门和交易所采取监管措施或处罚情况的公告',
        '江苏长龄液压股份有限公司关于公司股东及董事、高级管理人员未减持公司股份并提前终止减持计划的公告',
        '佛慈制药:关于副总经理辞职的公告',
        '仁和药业:关于董事、副董事长辞职的公告',
        '南极电商:关于公司重大诉讼进展暨撤诉裁定的公告',
    ]
    REAL_HITS = [
        '天马科技关于控股股东、实际控制人股份被冻结的公告',
        '江苏龙蟠科技集团股份有限公司关于2026年前三季度计提减值准备的公告',
        '海南发展:关于收到深圳证监局警示函的公告',
        '佳力图控股股东减持股份结果公告',
        '格尔软件股份有限公司关于公司涉及诉讼的公告',
        '华塑控股:关于公司及相关责任人收到四川证监局行政监管措施决定书的公告',
    ]
    _cal = stk.NEG_ANN_CALIBRATED
    stk.NEG_ANN_CALIBRATED = False
    legacy_false = sum(1 for t in FALSE_HITS
                       if stk.check_negative_announcement([{'title': t}])['hasNegative'])
    legacy_real = sum(1 for t in REAL_HITS
                      if stk.check_negative_announcement([{'title': t}])['hasNegative'])
    stk.NEG_ANN_CALIBRATED = True
    cal_false = sum(1 for t in FALSE_HITS
                    if stk.check_negative_announcement([{'title': t}])['hasNegative'])
    r8 = [stk.check_negative_announcement([{'title': t}]) for t in REAL_HITS]
    cal_real = sum(1 for x in r8 if x['hasNegative'])
    ck(legacy_false == len(FALSE_HITS),
       f'关掉校准＝老表原样（{legacy_false}/{len(FALSE_HITS)} 条误命中全部拦人），逐字节回到改造前')
    ck(cal_false == 0, f'开启校准后误命中放行，实得仍拦 {cal_false} 条')
    ck(cal_real == len(REAL_HITS) and legacy_real == len(REAL_HITS),
       f'真利空一条没放（{cal_real}/{len(REAL_HITS)}）')
    _x = stk.check_negative_announcement([{'title': FALSE_HITS[0]}])
    ck(_x['excludedHits'] == 1, f'排掉的条数要留痕（excludedHits），实得 {_x["excludedHits"]}')
    ck(stk.check_negative_announcement([{'title': REAL_HITS[0]}])['excludedHits'] == 0,
       '真命中的 excludedHits 记 0（排错了能顺着这个数找到）')
    stk.NEG_ANN_CALIBRATED = _cal

    # ========== [8] 结构异常 ≠ 没有公告 ==========
    print('[8] HTTP 200 但 data 结构不对 → error/unknown，不许折算成"这只票没有公告"')
    reset('sh600004')
    MODE['kind'], MODE['skip'] = 'badstruct', False
    r7 = run(stk.announcement_check('sh600004'))
    ck(r7['status'] == 'error' and r7['unknown'] is True, f'实得 {r7["status"]}/{r7["unknown"]}')
    ck(r7['hasNegative'] is False, '结构异常同样不判利空（判定层没变）')
    ck('结构' in r7['reason'], f'reason 写清了是结构问题：{r7["reason"]}')
finally:
    g.skip = _real_skip
    stk.requests = __import__('requests')
    stk.time = _real_time
    g._save_state_locked = _real_save_state

print(f'\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败')
if FAIL:
    for m in FAIL:
        print('  失败：', m)
    sys.exit(1)
print('全程假 requests / 假闸门 / 假时钟，零外部请求、零真库写入')
