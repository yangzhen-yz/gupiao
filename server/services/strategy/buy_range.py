"""buy_range.py - 买入价区间与"现在买是不是追高"的唯一算法（2026-10-18，实施计划 P0-1）

为什么要单独一个模块：同一件事（"这只票该挂在什么价、现价高出上限多少"）原来在两个地方
各写一遍 —— 收评的「明日买入计划」`services/ops/review.py:_build_tomorrow_plan` 里一套，
智能推荐的买卖点 `services/pick/recommend.py:calc_buy_sell_points` 里另一套（只有单值 buy）。
两套迟早分叉，而分叉之后"卡片上写的低吸价"和"明天计划里写的低吸价"不一样，
用户就再也不知道该挂哪个数。这里收在一处，两边都调它。

口径来源（不是拍脑袋）：`docs/短线买法-2026-10.md` §5 与推荐复盘的实测——
现价追入的 T+1 胜率 46.8%（218 条），低吸单**真正成交**的口径 42.1%（95 条成交，
纸面 60.4% 里三成是"没跌到买入价"的幻觉胜利）；且成交日如果追高只有 25.3% ——
"现价高于买入上限"不是措辞问题，是同一个日子里两种执行动作差 17 个百分点以上的
事实，必须按票算出来、摆在按钮上，而不是全局贴一句"建议低吸"了事。
"""
from typing import Dict, Optional

# 各买入时机允许的最高涨幅%（超过即视为追高，进否决清单）。
# 数值沿用 `_build_tomorrow_plan` 一直在用的那张表，本次只是搬过来，没有改。
CHASE_MAX: Dict[str, float] = {'auction': 5.0, 'early': 3.0, 'tail': 3.0,
                               'pullback': 2.5, 'intraday': 3.0}
# 各买入时机允许的最低开盘涨幅%（低于此值说明破位/情绪走坏，不接）
OPEN_FLOOR: Dict[str, float] = {'auction': 0.0, 'early': -2.0, 'tail': -3.0,
                                'pullback': -3.0, 'intraday': -3.0}
# 振幅→低吸回撤（波动大的票值得多等一点；与 calc_buy_sell_points 的 base_buy_pct 同源）
AMPLITUDE_BUY_PCT = ((5.0, 0.03), (3.0, 0.025), (2.0, 0.02))
DEFAULT_BUY_PCT = 0.015


def base_buy_pct(amplitude: float) -> float:
    for gate, pct in AMPLITUDE_BUY_PCT:
        if amplitude >= gate:
            return pct
    return DEFAULT_BUY_PCT


def limit_pct_of(code: str) -> float:
    """涨跌停幅度：创业/科创 20%、主板 10%（范围里已排除 ST 的 5% 档）。

    传进来的可能是 'sh600001' 也可能是 '600001'，一律取后 6 位再判板块。
    """
    c = str(code or '')[-6:]
    return 20.0 if c.startswith(('30', '68')) else 10.0


def buy_range(price: float, amplitude: float = 0.0, ref_buy: Optional[float] = None,
              timing_type: str = 'intraday', limit_up: bool = False,
              code: str = '') -> Dict:
    """给出这只票的挂单区间。返回 {'buyLow','buyHigh','chasePct','buyNote','limitPrice'}。

    三个分支的优先级不能换：
      1. 有买入参考价（智能推荐按角色/位置算好的）→ 以它为准，上限不越过追高线；
      2. 已封板 → 当天买不到，区间是**次日竞价**的参与区间（涨停票没法提前定价）；
      3. 其余 → 按振幅算回撤幅度，上限就是"不高于现价"。

    `limitPrice` = 今天真想买最多付到这个价（再高就是替别人抬轿）：
    封板票给次日涨停价（打板唯一可能的成交价），其余给"现价 × 该时机的追高上限"。
    """
    chase = CHASE_MAX.get(timing_type, 3.0)
    if ref_buy and ref_buy > 0:
        branch = 'ref'
        buy_low = float(ref_buy)
        buy_high = min(buy_low * 1.01, price * (1 + chase / 100))
        note = f'低吸参考 {buy_low:.2f} 附近挂单'
    elif limit_up:
        branch = 'limitUp'
        buy_low = price
        buy_high = price * 1.03
        note = '次日竞价挂单（涨停票无法提前定价）'
    else:
        branch = 'amplitude'
        pct = base_buy_pct(amplitude)
        buy_low = price * (1 - pct)
        buy_high = price
        note = f'回踩 {buy_low:.2f} 附近挂单，不追高于现价'
    if limit_up:
        limit_price = price * (1 + limit_pct_of(code) / 100)
    else:
        limit_price = min(price * (1 + chase / 100), price * (1 + limit_pct_of(code) / 100))
    # branch 一起返回：调用方各自的文案不同（收评说明天，说的是"不追高于今收"；
    # 推荐卡片说的是当下，得写"不追高于现价"），数字则只有一份算法。
    # Raw 两个键是给下游算止盈止损用的：那边一直用未取整的价格，这里提前 round 到分
    # 就会让 buy_mid/stop/takeProfit 与改造前差 0.01 ——"重构不改行为"那条断言当场过不了。
    return {'buyLow': round(buy_low, 2), 'buyHigh': round(buy_high, 2),
            'buyLowRaw': buy_low, 'buyHighRaw': buy_high,
            'chasePct': chase, 'buyNote': note, 'limitPrice': round(limit_price, 2),
            'branch': branch}


def over_pct(price: float, buy_high: Optional[float]) -> Optional[float]:
    """现价高出买入上限多少个百分点；负数=现价还在区间内（可以按纪律买）。"""
    if not buy_high or buy_high <= 0 or not price:
        return None
    return round((float(price) / float(buy_high) - 1) * 100, 2)


def chase_warning(price: float, buy_high: Optional[float], rates: Optional[Dict] = None,
                  warn_at: float = 1.0, stop_at: float = 3.0) -> str:
    """按票算出来的追高警告；没超阈值返回空串（不硬凑一句废话）。

    rates 传推荐复盘的实测数 `{'dixiWin':…, 'chaseWin':…, 'dixiN':…, 'chaseN':…,
    'filledChaseWin':…}`（`recommend.exec_rates()` 的返回值），拿不到就退回文档口径
    （42.1% / 46.8%，`docs/胜率低根因复盘-2026-10-18.md`）——
    数字必须带出处，但出处可以是文档；有实测量时把样本条数一起写出来，
    几十笔的胜率本来就飘，让用户看得见"这只是 95 条成交给出的 42.1%"。

    2026-10-18 口径纠偏：`dixiWin` 现在是**低吸单真正成交**口径（纸面 60.4% 里
    31.7% 是未成交的幻觉胜利，不能往警告文案里放）。`filledChaseWin`（成交日追高
    胜率）是这句话的重点：挂低吸的意义是躲诱多，不是保证赚。
    """
    op = over_pct(price, buy_high)
    if op is None or op < warn_at:
        return ''
    r = rates or {}
    dw = r.get('dixiWin') if r.get('dixiWin') is not None else 42.1
    cw = r.get('chaseWin') if r.get('chaseWin') is not None else 46.8
    n_txt = ''
    if r.get('dixiN') and r.get('chaseN'):
        n_txt = f'（样本 {r["dixiN"]}/{r["chaseN"]} 条）'
    fcw = r.get('filledChaseWin')
    fcw_txt = f'，而这些成交日如果追高只有 {fcw}%' if fcw is not None else ''
    txt = (f'现价已高于买入参考价上限 {op:.1f}%（上限 {buy_high:.2f}）。'
           f'现价追入口径历史 T+1 胜率 {cw}%，低吸单真正成交的口径是 {dw}%'
           f'{fcw_txt}{n_txt} —— 建议挂单等回踩，不追盘中高价')
    if op >= stop_at:
        txt += f'；超出 {stop_at:g}% 以上不建议买入'
    return txt
