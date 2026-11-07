"""overnight_exit.py - 过夜持仓"默认出口"次日提醒（2026-11-07，修⑥②；默认关，只落库不推）

补的是哪一块
------------
卡片与复盘里一直写着"过夜后按默认出口走"，但**没有任何代码在次日把这句话变成一条提醒**：
09:15 那一班（`position_watch.push_t1_plan`）推的是"昨天盘中已经触发过止损/止盈/炸板信号、
因为 T+1 当天卖不掉才压到今天"的票 —— 前提是昨天真的响过。三种情况它一条都不管：

1. **时间止损到期**（持有 ≥5 个交易日且收益 <3%）：昨天盘中没跌破线，所以从没响过，
   但按 `exit_rules` 今天就该走；
2. **逻辑止损线（MA20）就在头顶**：昨天没破、今天开盘可能一击即破，需要提前挂单；
3. 用户问的那条基线口径 —— **"次日开盘卖"**（P0 评估口径：推荐日次日开盘买、持 N 日开盘卖）。

这个模块把这三条变成 09:15 一条可执行清单。**它不改任何动作、不代下单**：
`exit_rules_live` 仍是关的，模拟盘的卖单仍由 `trade_sim` 那套规则生成；这里只出提醒 + 落库，
攒的就是"提醒了什么、用户做没做"的对照数据（`position_trade_log` 实测只有 2 行、
`exit_signal_log` 0 行，说明这块从来没有真数据）。

两个独立开关（关掉必须逐字节等于现状）
--------------------------------------
- `OVERNIGHT_EXIT_ADVICE_ENABLED`：总开关。关掉 → `run()` 一行都不写、一条都不推，
  现状的 09:15 只有 T+1 预案那一条腿。
- `OVERNIGHT_EXIT_PUSH_ENABLED`：只决定**推不推**；关掉时提醒照样落进 `position_alert_log`
  （kind='overnight_default'），先攒对照数据、不打扰用户。默认 False，就是先影子。

去重与失败处理沿用 `position_watch` 的既有规矩
--------------------------------------------
按 (日期, 账号) 一条汇总消息；`position_alert_log` 落库去重，发送失败就撤回标记
（否则当天这条提醒就永久丢了）。写库走 `get_db_conn(wait_for_lock=True)`（修⑤：
提醒是留痕类，宁可等锁，不要静默丢行）。
"""
import json
from datetime import datetime
from typing import Dict, List, Optional

from app.config import (OVERNIGHT_EXIT_ADVICE_ENABLED, OVERNIGHT_EXIT_PUSH_ENABLED)
from db.database import get_db_conn
from services.portfolio.position import get_portfolio_diagnosis
from services.support.push_channel import send_to_user
import services.support.push_channel as pc

_KIND = 'overnight_default'
_AGG_KEY = '__overnight__'          # 汇总消息的去重记录记在这个虚拟 symbol 上
# 时间止损/逻辑止损/固定止损三条默认出口里，"今天能执行"的那些动作
_URGENT = {'stop_loss', 'reduce', 'take_profit', 'giveback', 'exit'}


def enabled() -> bool:
    return bool(OVERNIGHT_EXIT_ADVICE_ENABLED)


def push_enabled() -> bool:
    return bool(OVERNIGHT_EXIT_PUSH_ENABLED)


def _hit_mechanisms(plan: Dict) -> List[Dict]:
    return [m for m in (plan.get('mechanisms') or []) if m.get('hit')]


def build_advice(diag: Dict, today: Optional[str] = None) -> List[Dict]:
    """从一份持仓诊断里挑出"今天该按默认出口动手"的过夜仓。

    判据全部复用 `exit_rules.plan_exit` 的结果（挂在 `pos['exit_plan']`），这里**不新建算法**：
    同一只票在持仓页、行动台、这里必须是同一个止损价，否则又回到"两个页面两个止损价"。
    """
    today = today or datetime.now().strftime('%Y-%m-%d')
    items: List[Dict] = []
    for p in (diag.get('positions') or []):
        sym = (p.get('symbol') or '').lower()
        if not sym:
            continue
        if not p.get('sellable', True):
            continue                       # 今天买的，今天卖不掉（T+1），明天再说
        plan = p.get('exit_plan') or {}
        hits = _hit_mechanisms(plan) if plan.get('enabled') else []
        act = p.get('action') or 'watch'
        why: List[str] = []
        if hits:
            why.extend([f"{m.get('label')}：{m.get('detail') or m.get('line')}" for m in hits])
        elif act in _URGENT:
            why.append(p.get('detail') or '持仓诊断已给出动作')
        if not why and not _near_line(p):
            continue
        items.append({
            'symbol': sym, 'name': p.get('name') or sym,
            'cost': p.get('cost'), 'price': p.get('price'),
            'pnl_pct': p.get('pnl_pct'), 'buy_date': p.get('buy_date'),
            'action': act, 'action_label': p.get('action_label'),
            'stop_price': p.get('stop_price'), 'ma20': _ma20(plan),
            'why': why, 'planEnabled': bool(plan.get('enabled')),
            # 第二条腿：P0 评估口径（推荐日次日开盘买 → 今天开盘卖）。
            # 只记录、不推送，等"提醒 vs 实际动作"攒够对照再比哪个更该当默认出口。
            'altCaliber': 'next_open_sell',
        })
    return items


def _ma20(plan: Dict) -> Optional[float]:
    for m in (plan.get('mechanisms') or []):
        if m.get('id') in ('logic_ma', 'logic') or 'MA' in str(m.get('label', '')):
            try:
                return float(m.get('line')) if m.get('line') else None
            except (TypeError, ValueError):
                return None
    return None


def _lines(p: Dict) -> List[float]:
    """这票所有"今天能挂在屏幕上的线"：持仓自己的止损价 + 出口计划里各机制的线。

    为什么要两边都读：`diagnose_position` 会在 `pos['stop_price']` 放固定止损线，
    而 MA20/移动线只在 `exit_plan.mechanisms[].line` 里 —— 只读一边会漏（第一版就漏在
    这儿：逼近 MA20 的票一条都不提醒，自测第 [8] 组报红才发现）。
    """
    out: List[float] = []
    for v in [p.get('stop_price')] + [m.get('line') for m in
                                      ((p.get('exit_plan') or {}).get('mechanisms') or [])]:
        try:
            f = float(v or 0)
        except (TypeError, ValueError):
            continue
        if f > 0:
            out.append(f)
    return out


def _near_line(p: Dict) -> bool:
    """没触发但离线很近（≤1.5%）也要提醒 —— 竞价一开就破的情况，等盘中推送就晚了。"""
    price = p.get('price') or 0
    if not price:
        return False
    for line in _lines(p):
        # 只在"价在上方、正在逼近"时提醒；已经跌破上面 hits 分支就接住了
        if 0 < (price - line) / line <= 0.015:
            return True
    return False


def _fmt(items: List[Dict], diag: Dict) -> str:
    head = [f'【过夜持仓·今日默认出口】{datetime.now():%m-%d %H:%M}（竞价前，共 {len(items)} 只）']
    for i in items:
        seg = [f"{i['name']}({i['symbol']})"]
        if i.get('pnl_pct') is not None:
            seg.append(f"浮盈 {i['pnl_pct']:+.2f}%")
        if i.get('stop_price'):
            seg.append(f"止损线 {i['stop_price']}")
        if i.get('ma20'):
            seg.append(f"MA20 {i['ma20']}")
        if i.get('why'):
            seg.append('；'.join(i['why'][:2]))
        if i.get('action_label'):
            seg.append(f"动作：{i['action_label']}")
        head.append(' · ' + '｜'.join(str(x) for x in seg))
    head.append('—— 只是提醒，不自动下单。口径与持仓页/行动台同一套（exit_rules）。')
    return '\n'.join(head)


def _already(date_str: str, user_id: int) -> bool:
    pc._ensure_table()
    conn = get_db_conn()
    try:
        row = conn.execute("SELECT 1 FROM position_alert_log WHERE alert_date=? AND user_id=? "
                           "AND symbol=? AND kind=? LIMIT 1",
                           (date_str, user_id, _AGG_KEY, _KIND)).fetchone()
    finally:
        conn.close()
    return row is not None


def _mark(date_str: str, user_id: int, items: List[Dict], pushed: bool) -> None:
    conn = get_db_conn(wait_for_lock=True)
    try:
        conn.execute('INSERT OR REPLACE INTO position_alert_log '
                     '(alert_date, user_id, symbol, kind, pushed_at, price, note) '
                     'VALUES (?,?,?,?,?,?,?)',
                     (date_str, user_id, _AGG_KEY, _KIND,
                      datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 0.0,
                      json.dumps({'pushed': pushed,
                                  'symbols': [i['symbol'] for i in items]}, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()


async def run(user_id: int = 1, today: Optional[str] = None,
              push: Optional[bool] = None, diag: Optional[Dict] = None) -> Dict:
    """一班 09:15 的入口。返回 `{'advised': n, 'pushed': bool, ...}`，任何异常都吞成结构化结果，
    调用方（scheduler）不会因为一条提醒打断整轮。"""
    today = today or datetime.now().strftime('%Y-%m-%d')
    if not enabled():
        return {'skipped': 'OVERNIGHT_EXIT_ADVICE_ENABLED=False', 'advised': 0}
    try:
        diag = diag if diag is not None else await get_portfolio_diagnosis(user_id=user_id)
        items = build_advice(diag or {}, today=today)
    except Exception as e:
        print(f'[过夜出口] 诊断失败（本轮放弃）: {type(e).__name__}: {e}')
        return {'error': f'{type(e).__name__}: {str(e)[:120]}', 'advised': 0}
    if not items:
        return {'advised': 0, 'empty': True}
    if _already(today, user_id):
        return {'advised': len(items), 'deduped': True}
    do_push = push_enabled() if push is None else bool(push)
    pushed = False
    if do_push:
        res = send_to_user(user_id, _fmt(items, diag or {}))
        pushed = bool(res.get('ok'))
        if not pushed:
            print(f'[过夜出口] 推送失败，标记撤回以便下次重试: {res.get("errors")}')
            return {'advised': len(items), 'pushed': False, 'errors': res.get('errors')}
    try:
        _mark(today, user_id, items, pushed)
    except Exception as e:
        print(f'[过夜出口] 去重标记写失败（提醒已生成，别重复推）: {e}')
    print(f'[过夜出口] {today} 账号 {user_id}：{len(items)} 只过夜仓待按默认出口处理'
          f'{"（已推送）" if pushed else "（只落库，推送开关关着）"}')
    return {'advised': len(items), 'pushed': pushed,
            'symbols': [i['symbol'] for i in items]}
