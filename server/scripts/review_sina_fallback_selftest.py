"""收评个股榜「东财 → 新浪」兜底回归自测（2026-11-13）。

只验映射、口径换算、失败降级与源码接线，全部走 stub：
零外部请求、不碰任何数据库。真数据源可达性由线上收评与
``/api/daily-reviews`` 验收，不在自测里赌网络。

对应改动：services/ops/review.py::_fetch_stocks_from_sina 与三张榜单的降级分支，
前端 TabReview.vue 的来源角标。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.data import fundflow          # noqa: E402
from services.ops import review             # noqa: E402

PASS, FAIL = [], []


def ck(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(f"  [{'OK' if cond else 'XX'}] {msg}")


HERE = os.path.dirname(os.path.abspath(__file__))
REVIEW_SRC = os.path.join(HERE, '..', 'services', 'ops', 'review.py')
VUE_SRC = os.path.join(HERE, '..', '..', 'client-vue', 'src',
                       'components', 'tabs', 'TabReview.vue')

_orig = fundflow._sina_rank_rows
calls = []


def _rows(n=25):
    return [{'name': f'测试股{i}', 'code': f'sh6000{i:02d}',
             'changePercent': 1.5, 'price': 10.0,
             'turnover': 1630.31, 'mainNetInflow': 1.2e8}
            for i in range(n)]


def _stub(sort, asc, pages=1):
    calls.append((sort, asc, pages))
    return _rows()


try:
    print('[1] 三张榜各自映射到正确的新浪排序字段')
    fundflow._sina_rank_rows = _stub
    for kind, want in (('gainers', ('changeratio', 0)),
                       ('losers', ('changeratio', 1)),
                       ('funds', ('r0_net', 0))):
        calls.clear()
        review._fetch_stocks_from_sina(kind)
        ck(calls == [(want[0], want[1], 1)],
           f'{kind} → sort={want[0]} / asc={want[1]}（实得 {calls}）')
    calls.clear()
    ck(review._fetch_stocks_from_sina('unknown') == [] and not calls,
       '未知 kind 返回空且不发请求')

    print('[2] 口径换算与行字段（新浪 turnover 是换手率×100）')
    rows = review._fetch_stocks_from_sina('gainers')
    ck(len(rows) == 20, f'单页 25 行截到前 20（实得 {len(rows)}）')
    ck(all(set(r) == {'name', 'code', 'changePercent', 'price',
                      'turnoverRate', 'mainNetInflow', 'source'} for r in rows),
       '每行字段与收评 schema 一致')
    ck(abs(rows[0]['turnoverRate'] - 16.3031) < 1e-9,
       f'换手率 1630.31 → 16.3031%（实得 {rows[0]["turnoverRate"]}）')
    ck(all(r['source'] == 'sina' for r in rows), '每行都标 source=sina')
    ck(rows[0]['mainNetInflow'] == 1.2e8 and rows[0]['changePercent'] == 1.5,
       '净流入与涨跌幅原样透传，不做二次换算')
    fundflow._sina_rank_rows = lambda *a, **k: _rows(3)
    ck(len(review._fetch_stocks_from_sina('funds')) == 3,
       '备源只返回 3 行时如实给 3 行，不补假数据')

    print('[3] 备源异常必须降级成空表，不能掀翻整份收评')
    def _boom(*a, **k):
        raise RuntimeError('network down')
    fundflow._sina_rank_rows = _boom
    ck(review._fetch_stocks_from_sina('gainers') == [], '请求异常返回 []（不抛）')
    fundflow._sina_rank_rows = lambda *a, **k: [
        {'name': '缺字段票', 'code': 'sh600000', 'turnover': None}]
    r = review._fetch_stocks_from_sina('losers')[0]
    ck(r['turnoverRate'] == 0.0 and r['changePercent'] == 0 and r['source'] == 'sina',
       f'字段缺失/None 用 0 兜底且不炸（实得 {r}）')

    print('[4] 源码接线：东财有数时不降级、空了才降级，两侧都带来源')
    src = open(REVIEW_SRC, encoding='utf-8').read()
    for kind in ('gainers', 'losers', 'funds'):
        ck(f"_fetch_stocks_from_sina('{kind}')" in src,
           f"{kind} 的降级调用点仍在")
    em_count = src.count("'source': 'eastmoney'")
    ck(em_count == 3, f'东财侧三张榜都标 source=eastmoney（实得 {em_count} 处）')
    ck(all(f'if not _{v}:' in src for v in ('g', 'l', 'f')),
       '三处都是「东财结果为空才降级」，不是无脑覆盖')
    ck('_turnover = float(_turnover) / 100.0' in src,
       '换手率换算没被删（否则前端会显示 1630.31%）')

    print('[5] 前端角标：按当前 tab 显示来源，且不假装口径一致')
    vue = open(VUE_SRC, encoding='utf-8').read()
    ck('activeStockSource' in vue and '新浪口径' in vue and "'东财'" in vue,
       'TabReview 有来源角标函数与两种文案')
    ck("map = { gainer: getTop5Gainers, loser: getTop5Losers, funds: getTop5Funds }" in vue,
       '角标按 stockTab 选对应榜单，不是写死一个来源')
finally:
    fundflow._sina_rank_rows = _orig

print(f'\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败')
if FAIL:
    for f in FAIL:
        print('  FAIL:', f)
    sys.exit(1)
print('全部通过（零外部请求、未写任何库）')
