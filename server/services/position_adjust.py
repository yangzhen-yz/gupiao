"""position_adjust.py - 持仓加仓 / 减仓的成本重算（2026-10-14）

用户在持仓页加/减仓位时最烦的是手算加权成本：买 400 股 @10.143 再补 300 股 @9.80，
新成本得自己乘来乘去。这里把口径固定下来：

  加仓  新成本 = (原成本 × 原股数 + 成交价 × 新增股数) ÷ 总股数
  减仓  剩余持仓的加权成本**不变**（券商/记账惯例），只减股数，
        同时算出本次实现盈亏 = (卖价 - 成本) × 卖出股数
  清仓  股数减到 0 → 整条持仓删除，由调用方处理

刻意不建交易流水表：note 里追加一行操作痕迹就够复盘用，
多一张表就多一处要和 user_positions 对齐的状态。
"""
from typing import Dict, Optional

NOTE_MAX_LEN = 200


def apply_adjust(cur: Dict, mode: str, delta: int, price: float, today: str) -> Dict:
    """返回 {shares, cost_price, note, cleared, realized_pnl, action}；非法输入抛 ValueError。"""
    if mode not in ('add', 'reduce'):
        raise ValueError("mode 只能是 add（加仓）或 reduce（减仓）")
    if delta <= 0:
        raise ValueError("股数要大于 0")
    if price <= 0:
        raise ValueError("成交价要大于 0")

    old_shares = int(cur.get('shares') or 0)
    old_cost = float(cur.get('cost_price') or 0)
    if old_shares <= 0:
        raise ValueError("原持仓股数缺失，请直接用「编辑」补全")
    base = (cur.get('note') or '').strip()

    if mode == 'add':
        if old_cost <= 0:
            raise ValueError("原持仓缺少成本价，请直接用「编辑」填写")
        shares = old_shares + delta
        cost = round((old_cost * old_shares + price * delta) / shares, 3)
        realized = None
        note = f'{base}；{today} 加仓{delta}股@{price}'
        action = '加仓'
    else:
        if delta > old_shares:
            raise ValueError(f'减仓 {delta} 股超过当前持仓 {old_shares} 股')
        shares = old_shares - delta
        cost = round(old_cost, 3)          # 减仓不改加权平均成本
        realized = round((price - old_cost) * delta, 2) if old_cost > 0 else None
        pnl_txt = f'（实现{realized:+.2f}元）' if realized is not None else ''
        note = f'{base}；{today} 减仓{delta}股@{price}{pnl_txt}'
        action = '减仓'

    return {'shares': shares,
            'cost_price': cost,
            'note': note.strip('；')[:NOTE_MAX_LEN],
            'cleared': mode == 'reduce' and shares == 0,
            'realized_pnl': realized,
            'action': action}


def describe(result: Dict, name: str = '') -> Optional[str]:
    """给人看的一句话（toast 用）"""
    prefix = f'{name} ' if name else ''
    if result.get('cleared'):
        r = result.get('realized_pnl')
        return f'{prefix}已全部卖出' + (f'，实现盈亏 {r:+.2f} 元' if r is not None else '')
    r = result.get('realized_pnl')
    txt = f'{prefix}{result["action"]}后 {result["shares"]} 股，成本 {result["cost_price"]}'
    if r is not None:
        txt += f'，本次实现盈亏 {r:+.2f} 元'
    return txt
