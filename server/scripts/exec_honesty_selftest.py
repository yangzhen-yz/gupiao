"""exec_honesty_selftest.py - P0-1 执行诚实化的自测（区间算法搬家不能改行为）

两件必须证明的事：
  1. `services/ops/review.py` 的明日计划改成调 `buy_range()` 之后，**数字一个都不许变**
     —— 它一直在给用户的挂单价，搬个函数就变了价格是不可接受的；
  2. 推荐卡片这次真的算出了"现价比买入上限高多少"，并且超到阈值会给警告、
     打板票会带默认出口径 —— 而不是又变成一个永远为空的字段。

用假行情跑，不联网、不碰生产库。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.strategy.buy_range import (    # noqa: E402
    buy_range, chase_warning, limit_pct_of, over_pct)

FAILS = []


def ck(cond, msg):
    print(('  ✓ ' if cond else '  ✗ ') + msg)
    if not cond:
        FAILS.append(msg)


def old_plan_range(price, amp, ref_buy, ttype, is_limit_up):
    """改造前 `_build_tomorrow_plan` 里那段区间代码的原文（逐字抄来当基准）。"""
    CHASE_MAX = {'auction': 5.0, 'early': 3.0, 'tail': 3.0, 'pullback': 2.5, 'intraday': 3.0}
    chase = CHASE_MAX.get(ttype, 3.0)
    if ref_buy:
        buy_low = ref_buy
        buy_high = min(ref_buy * 1.01, price * (1 + chase / 100))
    elif is_limit_up:
        buy_low = price
        buy_high = price * 1.03
    else:
        pct = 0.03 if amp >= 5 else 0.025 if amp >= 3 else 0.02 if amp >= 2 else 0.015
        buy_low = price * (1 - pct)
        buy_high = price
    return buy_low, buy_high


print('[1] 区间算法与改造前逐格相等（明日计划不许因为搬函数变价格）')
bad = []
for price in (3.17, 10.0, 19.66, 88.88, 1234.5):
    for amp in (0.0, 1.2, 2.0, 3.0, 4.9, 5.0, 9.8, 15.0):
        for ttype in ('auction', 'early', 'tail', 'pullback', 'intraday', '未知'):
            for ref in (None, 0, 9.5, price * 0.97, price * 1.02):
                for lim in (False, True):
                    a = buy_range(price, amp, ref_buy=ref or None, timing_type=ttype,
                                  limit_up=lim)
                    b = old_plan_range(price, amp, ref or None, ttype, lim)
                    # 旧实现里 ref_buy 为 0 走 `_price_or_none` → None，这里对齐
                    if abs(a['buyLowRaw'] - b[0]) > 1e-9 or abs(a['buyHighRaw'] - b[1]) > 1e-9:
                        bad.append((price, amp, ttype, ref, lim, a['buyLowRaw'], b[0],
                                    a['buyHighRaw'], b[1]))
ck(not bad, f'共 {5*8*6*5*2} 格，逐格对比区间未变（不一致 {len(bad)} 格）')
if bad:
    print('   前 3 格：', bad[:3])

print('\n[2] 追价上限 limitPrice：封板票=次日涨停价，其余=现价×该时机追高幅度')
ck(buy_range(10.0, 6, timing_type='auction', limit_up=True, code='sh600001')['limitPrice']
   == 11.0, f'主板封板 10.00 → 次日涨停 11.00（实得 '
            f'{buy_range(10.0, 6, timing_type="auction", limit_up=True, code="600001")["limitPrice"]}）')
ck(buy_range(10.0, 6, timing_type='auction', limit_up=True, code='sz300999')['limitPrice']
   == 12.0, '创业板封板 → 20% 那档，别按主板算')
ck(buy_range(10.0, 6, timing_type='intraday', code='600001')['limitPrice'] == 10.30,
   '盘中灵活：追价上限＝现价×(1+3%)（实得 '
   f'{buy_range(10.0, 6, timing_type="intraday", code="600001")["limitPrice"]}）')
ck(limit_pct_of('sh600843') == 10.0 and limit_pct_of('301199') == 20.0
   and limit_pct_of('sz301199') == 20.0, '板块判定兼容带前缀与裸代码两种写法')

print('\n[3] 超价幅度与警告阈值')
ck(over_pct(10.0, 10.0) == 0.0 and over_pct(10.3, 10.0) == 3.0, '超价按 (现价/上限-1) 算')
ck(over_pct(10.0, None) is None and over_pct(0, 10.0) is None, '缺价时返回 None 而不是炸')
ck(chase_warning(10.05, 10.0) == '', '超价 0.5% 不说话（阈值 1%）')
w = chase_warning(10.15, 10.0)
ck('上限 10.00' in w and '46.8%' in w and '42.1%' in w and '不建议买入' not in w,
   f'超价 1.5% → 警告带上限价与两个胜率（兜底值=真实成交口径 42.1/追高 46.8），'
   f'但不到"不建议"：{w[:70]}…')
w3 = chase_warning(10.40, 10.0)
ck('超出 3% 以上不建议买入' in w3, f'超价 4% → 追加不建议买入：…{w3[-24:]}')
r = chase_warning(10.15, 10.0, {'dixiWin': 66.2, 'chaseWin': 45.5, 'filledChaseWin': 20.0})
ck('66.2%' in r and '45.5%' in r and '20.0%' in r,
   '传了当天复盘实测数就用实测数（含成交日追高反事实），不写死 42.1/46.8')
r_nofcw = chase_warning(10.15, 10.0, {'dixiWin': 66.2, 'chaseWin': 45.5})
ck('None' not in r_nofcw and '追高只有' not in r_nofcw,
   '缺 filledChaseWin 时不出现 None，也不硬凑反事实句')

print('\n[4] 推荐卡片：三种形态各挂一遍（涨停/半山腰/横盘）')
import app.config as cfg                                    # noqa: E402
from services.pick import recommend as R                    # noqa: E402


def _stock(sym, chg, price, amp, vol=1.5):
    return {'symbol': sym, 'name': '测试' + sym[-4:], 'price': price, 'priceRaw': price,
            'changePercent': chg, 'change': round(price * chg / 100, 2),
            'high': price * (1 + amp / 200), 'highRaw': price * (1 + amp / 200),
            'prevClose': price / (1 + chg / 100), 'prevCloseRaw': price / (1 + chg / 100),
            'volumeRatio': vol, 'turnoverRate': 4.0, 'weibi': 30.0, 'outerRatio': 60.0,
            'amplitude': amp, 'pe': 20, 'amount': 2e8}


cases = [
    ('封板票', _stock('sh600001', 10.0, 11.0, 8.0), 'auction'),
    ('半山腰', _stock('sh600002', 7.0, 17.4, 9.0), None),
    ('横盘缩量', _stock('sz000002', 0.4, 9.8, 1.8), None),
]
for label, st, want_type in cases:
    bsp = R.calc_buy_sell_points(st, score_result={'total': 72, 'label': 'B',
                                                   'base_scores': {'position': 50}},
                                 trend_details={'positionPct': 50})
    t = bsp.get('buyTiming') or {}
    ck(bool(t.get('buyLow') and t.get('buyHigh')), f'{label}：区间挂上了（{t.get("buyLow")}~{t.get("buyHigh")}）')
    ck(t.get('overPct') is not None, f'{label}：超价算出来了（{t.get("overPct")}%）')
    ck(bool(t.get('execHint')), f'{label}：文案不为空（{(t.get("execHint") or "")[:34]}…）')
    ck(t.get('limitPrice'), f'{label}：追价上限 {t.get("limitPrice")}')
    if want_type:
        ck(t.get('type') == want_type, f'{label}：时机={t.get("type")}')
        ck('次日 9:15-9:25 竞价挂卖' in (t.get('exitNote') or ''),
           f'{label}：打板票带默认出口径（不是只写"次日竞价"，而是给出为什么）')
        ck('竞价关注' in bsp.get('buy', ''), f'{label}：封板票买入价仍是"竞价关注"（没被区间覆盖掉）')
    else:
        ck('exitNote' not in t, f'{label}：非打板票不该冒出出口径')

print('\n[5] 低吸口径的票确实比现价便宜（否则"挂低吸单"这颗按钮没意义）')
bsp = R.calc_buy_sell_points(cases[1][1], score_result={'total': 72, 'label': 'B',
                                                        'base_scores': {'position': 50}},
                             trend_details={'positionPct': 50})
t = bsp['buyTiming']
ck(float(t['buyHigh']) <= float(bsp['current']) * 1.001,
   f'买入上限 {t["buyHigh"]} 不高于现价 {bsp["current"]}（低吸就是等更低，不是换个说法追高）')
ck(float(t['buyLow']) < float(t['buyHigh']), f'区间是开的：{t["buyLow"]} < {t["buyHigh"]}')
ck(t['overPct'] > 0 and '现价已高于买入参考价上限' in t['execHint'],
   f'半山腰票现价超上限 {t["overPct"]}% → 警告在前')

print('\n[6] 开关关掉要完全等于改造前（独立开关这条规矩）')
# 注意：常量是 `from app.config import ...` 复制进 recommend 命名空间的，
# 改 cfg 上的同名项没用 —— 必须打在 R 上（这也是为什么它不该是热读项：热读要走覆盖表）。
R.RECOMMEND_EXEC_HINT_ENABLED = False
bsp2 = R.calc_buy_sell_points(cases[1][1], score_result={'total': 72, 'label': 'B',
                                                         'base_scores': {'position': 50}},
                              trend_details={'positionPct': 50})
t2 = bsp2['buyTiming']
ck('execHint' not in t2, '关掉后没有追高警告文案')
ck(t2.get('buyLow') == t.get('buyLow'), '但区间数字仍在（区间是数据，警告才是文案开关管的）')
R.RECOMMEND_EXEC_HINT_ENABLED = True

print('\n[7] 胜率数字取当天复盘实测，不写死在常量里')
import time as _t
_NEW_RATES = {'dixiWin': 66.2, 'dixiAvg': 1.5, 'dixiN': 139,
              'filledChaseWin': 20.0, 'fillRate': 68.3, 'dixiEv': -0.72,
              'chaseWin': 45.5, 'chaseAvg': -1.1, 'chaseN': 218}
R._RATES_CACHE.update({'ts': _t.time() + 9999, 'rates': dict(_NEW_RATES)})
ck(R.exec_rates() == _NEW_RATES,
   '缓存新鲜时不重算（这函数每次推荐都要调，复盘表算全量 T+1/3/5）')
bsp3 = R.calc_buy_sell_points(cases[1][1], score_result={'total': 72, 'label': 'B',
                                                        'base_scores': {'position': 50}},
                              trend_details={'positionPct': 50})
h3 = bsp3['buyTiming']['execHint']
ck('66.2%' in h3 and '45.5%' in h3 and '20.0%' in h3 and '139' in h3
   and '真正成交' in h3,
   f'超价票：警告带实测数+样本条数+成交日反事实（{h3[:96]}…）')
bsp4 = R.calc_buy_sell_points(cases[2][1], score_result={'total': 72, 'label': 'B',
                                                        'base_scores': {'position': 50}},
                              trend_details={'positionPct': 50})
h4 = bsp4['buyTiming']['execHint']
ck('68.3%' in h4 and '躲诱多' in h4 and '66.2%' in h4 and '45.5%' in h4,
   f'未超价票：文案带成交率+"挂低吸是躲诱多不是保证赚"（{h4[:96]}…）')
ck('None' not in h3 and 'None' not in h4, '缺键不许渲染成 None')
ck('132次' not in h3 and '132次' not in h4 and '132' not in cfg.RECOMMEND_EXEC_HINT,
   '老常量里那句"复盘132次"已经不再出现在任何文案里')
R._RATES_CACHE.update({'ts': 0.0, 'rates': {}})

print()
print('全部通过' if not FAILS else f'失败 {len(FAILS)} 项: {FAILS}')
sys.exit(0 if not FAILS else 1)
