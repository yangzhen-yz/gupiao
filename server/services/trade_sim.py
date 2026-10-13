"""trade_sim.py - 模拟交易引擎（2026-10-12 新增）

定位：**纯模拟盘，绝不连接任何真实券商、绝不产生真金白银下单**。
目的：把系统已有的「信号」(智能推荐 / 收评明日买入计划 / 持仓诊断动作 / 筛选趋势选中)
     转化为可执行的「建议订单」，在隔离的模拟账户里撮合成交，验证策略闭环。
注：模拟账户最多持有 SIM_MAX_HOLD 只股票（用户规则）；智能推荐源按系统预期收益由高到低优先买入。

设计要点：
  - 每个 app 用户一个独立模拟账户 (sim_accounts)，与真实持仓/券商完全隔离。
  - 护栏用稳妥默认值（详见下方常量）：单票≤20% / 单笔≤10%总资金 /
    当日亏损≤3%封顶 / 价格偏离>5%拒单 / T+1（当日买入不可当日卖出）。
  - 所有动作留审计日志 (sim_orders)，status ∈ pending/filled/rejected。
  - 不下真单：execute_sim 只是在模拟账户里记账，不调用任何券商接口。
"""
import asyncio
import json
import sqlite3
from datetime import datetime, date
from typing import Dict, List, Optional

from db.database import get_db_conn, load_daily_recommendations
from services.stock import fetch_with_retry, parse_tencent_batch_data
from services.position import diagnose_position

# ========== 护栏常量（稳妥默认，可调） ==========
SIM_START_CASH = 1_000_000.0     # 模拟账户初始资金
MAX_POSITION_PCT = 0.20         # 单票 ≤20% 组合市值
MAX_ORDER_PCT = 0.10            # 单笔 ≤10% 总资金
DAILY_LOSS_CAP = 0.03           # 当日亏损 ≤3% 总资金 → 暂停新买
PRICE_DEVIATION = 0.05          # 成交价相对建议价偏离 >5% → 拒单
LOT = 100                       # A股一手=100股
SIM_MAX_HOLD = 3                # 模拟账户最多持有股票只数（2026-10-13 用户规则：最多持有3支票）
RECOMMEND_BUY_TOLERANCE = 0.03 # 智能推荐买入容差：实时价≤买入参考价×(1+此值) 才触发（回踩到买点附近才买，不追高）


def _today() -> str:
    return date.today().isoformat()


# ---------- 基础读写 ----------
def _ensure_sim_account(user_id: int) -> None:
    """确保每个用户有模拟账户行；并做每日基准重置（日亏封顶用）"""
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute('SELECT cash, day_start_value, day_start_date FROM sim_accounts WHERE user_id=?', (user_id,))
    row = cur.fetchone()
    today = _today()
    if not row:
        cur.execute('INSERT INTO sim_accounts (user_id, cash, day_start_value, day_start_date) VALUES (?,?,?,?)',
                    (user_id, SIM_START_CASH, SIM_START_CASH, today))
        conn.commit()
        conn.close()
        return
    cash, day_start_value, day_start_date = row
    # 跨日：把「当日基准」刷新为今天开盘时的账户总值（现金+持仓市值近似用现金+成本，
    # 这里用现金+持仓成本作为保守基准；精确市值在 get_sim_state 里用实时价算）
    if day_start_date != today:
        # 持仓市值用成本估算做日初基准（避免依赖行情）
        positions = cur.execute(
            'SELECT shares, cost_price FROM sim_positions WHERE user_id=?', (user_id,)).fetchall()
        cost_value = sum((p[0] or 0) * (p[1] or 0) for p in positions)
        new_start = cash + cost_value
        cur.execute('UPDATE sim_accounts SET day_start_value=?, day_start_date=? WHERE user_id=?',
                    (new_start, today, user_id))
        conn.commit()
    conn.close()


def _load_positions(user_id: int) -> List[Dict]:
    conn = get_db_conn()
    rows = conn.execute(
        'SELECT symbol, name, shares, cost_price, buy_date FROM sim_positions WHERE user_id=?',
        (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _load_orders(user_id: int, status: Optional[str] = None, limit: int = 200) -> List[Dict]:
    conn = get_db_conn()
    if status:
        rows = conn.execute(
            'SELECT * FROM sim_orders WHERE user_id=? AND status=? ORDER BY id DESC LIMIT ?',
            (user_id, status, limit)).fetchall()
    else:
        rows = conn.execute(
            'SELECT * FROM sim_orders WHERE user_id=? ORDER BY id DESC LIMIT ?',
            (user_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


async def _quotes(symbols: List[str]) -> Dict[str, Dict]:
    if not symbols:
        return {}
    try:
        raw = await fetch_with_retry(','.join(s.lower() for s in symbols))
        return {p['symbol'].lower(): p for p in parse_tencent_batch_data(raw)}
    except Exception as e:
        print(f'[模拟交易] 行情拉取失败: {e}')
        return {}


# ---------- 组合估值 ----------
async def _portfolio(user_id: int, positions: List[Dict]):
    """返回 (cash, 持仓市值, 总值, 当日基准, 当日盈亏) 并记录行情快照"""
    _ensure_sim_account(user_id)
    conn = get_db_conn()
    row = conn.execute('SELECT cash, day_start_value FROM sim_accounts WHERE user_id=?', (user_id,)).fetchone()
    conn.close()
    cash_v = row['cash'] if hasattr(row, 'keys') else row[0]
    day_start = row['day_start_value'] if hasattr(row, 'keys') else row[1]
    qmap = await _quotes([p['symbol'] for p in positions])
    mv = 0.0
    for p in positions:
        q = qmap.get(p['symbol'].lower())
        price = float(q.get('priceRaw') or q.get('price') or p['cost_price']) if q else p['cost_price']
        mv += price * p['shares']
    total = cash_v + mv
    return cash_v, mv, total, day_start, total - day_start


# ---------- 护栏 ----------
def _check_buy(user_id: int, symbol: str, price: float, qty: int,
               total_capital: float, cash: float, positions: List[Dict],
               day_start: float, day_pnl: float) -> Optional[str]:
    if price <= 0 or qty <= 0:
        return '价格或数量非法'
    cost = price * qty
    if cost > cash + 1e-6:
        return f'资金不足（需{cost:.0f} > 可用{cash:.0f}）'
    if cost > total_capital * MAX_ORDER_PCT:
        return f'单笔金额超上限（{cost:.0f} > 总资金{MAX_ORDER_PCT:.0%} = {total_capital*MAX_ORDER_PCT:.0f}）'
    # 单票占比（含本次买入）
    existing = next((p for p in positions if p['symbol'].lower() == symbol.lower()), None)
    pos_after = (existing['shares'] * existing['cost_price'] if existing else 0) + cost
    if total_capital > 0 and pos_after / (total_capital + cost) > MAX_POSITION_PCT:
        return f'单票占比超{MAX_POSITION_PCT:.0%}上限'
    if day_pnl < -DAILY_LOSS_CAP * day_start:
        return f'触及当日亏损封顶（{day_pnl:.0f} < -{DAILY_LOSS_CAP:.0%}×{day_start:.0f}），暂停新买'
    return None


def _check_sell(user_id: int, symbol: str, qty: int, price: float,
                positions: List[Dict], today: str) -> Optional[str]:
    if price <= 0 or qty <= 0:
        return '价格或数量非法'
    pos = next((p for p in positions if p['symbol'].lower() == symbol.lower()), None)
    if not pos:
        return '无对应模拟持仓'
    if pos['shares'] < qty:
        return f'持仓不足（持有{pos["shares"]} < 卖出{qty}）'
    if pos.get('buy_date') == today:
        return 'T+1：当日买入不可当日卖出'
    return None


# ---------- 生成建议订单 ----------
async def generate_proposals(user_id: int, sources: List[str], symbols: Optional[List[str]] = None) -> Dict:
    """从信号源生成 pending 建议订单。sources ∈ recommend / tomorrow_plan / diagnosis / screener
    其中 recommend（智能推荐）按系统预期收益由高到低优先，且账户最多持有 SIM_MAX_HOLD 只股票。"""
    _ensure_sim_account(user_id)
    positions = _load_positions(user_id)
    cash_v, mv, total, day_start, day_pnl = await _portfolio(user_id, positions)
    qmap = await _quotes([p['symbol'] for p in positions] +
                         ([s for s in (symbols or [])]))
    created, rejected = [], []

    # 持仓只数上限（用户规则：最多持有 3 只）。加仓已有持仓不计入新增只数。
    held_symbols = set(p['symbol'].lower() for p in positions)
    queued_new = set()

    def _cap_reached(sym):
        s = (sym or '').lower()
        if s in held_symbols or s in queued_new:
            return False  # 加仓到已有持仓不增加只数
        return len(held_symbols | queued_new) >= SIM_MAX_HOLD

    def _mark_queued(sym):
        queued_new.add((sym or '').lower())

    def _add(side, sym, name, price, qty, source, reason):
        nonlocal created, rejected
        # 买入受「最多持有 N 只」约束
        if side == 'buy' and _cap_reached(sym):
            rejected.append({'symbol': sym, 'side': side,
                             'reason': f'已达最大持仓上限（{SIM_MAX_HOLD}只）'})
            return
        if side == 'buy':
            err = _check_buy(user_id, sym, price, qty, day_start, cash_v, positions, day_start, day_pnl)
        else:
            err = _check_sell(user_id, sym, qty, price, positions, _today())
        conn = get_db_conn()
        if err:
            conn.execute('''INSERT INTO sim_orders
                (user_id, symbol, name, side, proposed_price, proposed_qty, source, status, reject_reason)
                VALUES (?,?,?,?,?,?,?,?,?)''',
                (user_id, sym, name, side, price, qty, source, 'rejected', err))
            conn.commit(); conn.close()
            rejected.append({'symbol': sym, 'side': side, 'reason': err})
        else:
            conn.execute('''INSERT INTO sim_orders
                (user_id, symbol, name, side, proposed_price, proposed_qty, source, status, reason)
                VALUES (?,?,?,?,?,?,?,?,?)''',
                (user_id, sym, name, side, price, qty, source, 'pending', reason))
            conn.commit(); conn.close()
            created.append({'symbol': sym, 'name': name, 'side': side, 'price': price, 'qty': qty, 'source': source})
            if side == 'buy':
                _mark_queued(sym)

    # 0) 智能推荐（新增信号源，按系统预期收益由高到低优先；受最多持有 N 只约束）
    if 'recommend' in sources:
        try:
            rec_groups = load_daily_recommendations()
        except Exception:
            rec_groups = []
        recs = rec_groups[0].get('daily_recommendations', []) if rec_groups else []
        # 取最近一天推荐，计算每只预期收益并排序（预期最高优先）
        scored = []
        for r in recs:
            bsp = r.get('buySellPoints') or {}
            try:
                bp = float(bsp.get('buy') or 0)
                sp = float(bsp.get('sell') or 0)
            except (TypeError, ValueError):
                bp = sp = 0
            sym = (r.get('symbol') or '').lower()
            if bp > 0 and sp > 0 and sym:
                scored.append({'sym': sym, 'bp': bp, 'sp': sp,
                               'exp': (sp - bp) / bp, 'name': r.get('name', '')})
        if scored:
            # 补拉推荐票的实时行情（positions/symbols 之外的）
            rec_syms = [s['sym'] for s in scored if s['sym'] not in qmap]
            if rec_syms:
                qmap.update(await _quotes(rec_syms))
            scored.sort(key=lambda x: x['exp'], reverse=True)  # 预期最高优先
            for item in scored:
                if _cap_reached(item['sym']):
                    continue
                q = qmap.get(item['sym'])
                if not q:
                    continue
                price = float(q.get('priceRaw') or q.get('price') or 0)
                if price <= 0:
                    continue
                # 触发：实时价不高于买入参考价的容差范围（回踩到买点附近才买，不追高）
                if price > item['bp'] * (1 + RECOMMEND_BUY_TOLERANCE):
                    continue
                qty = max(LOT, int(total * MAX_ORDER_PCT / price / LOT) * LOT) if price > 0 else 0
                if qty > 0:
                    _add('buy', item['sym'], item['name'], round(price, 2), qty, 'recommend',
                         f"智能推荐：预期收益{item['exp']*100:.1f}%（买{item['bp']:.2f}→卖{item['sp']:.2f}）")

    # 1) 收评明日买入计划
    if 'tomorrow_plan' in sources:
        conn = get_db_conn()
        row = conn.execute(
            'SELECT focus_json FROM daily_market_reviews ORDER BY review_date DESC LIMIT 1').fetchone()
        conn.close()
        focus = []
        if row:
            fj = row['focus_json'] if hasattr(row, 'keys') else row[0]
            if fj:
                try:
                    focus = json.loads(fj) if isinstance(fj, str) else fj
                except Exception:
                    focus = []
        for f in focus:
            plan = f.get('tomorrowPlan')
            if not plan:
                continue
            sym = (f.get('code') or '').lower()
            q = qmap.get(sym)
            if not q:
                continue
            price = float(q.get('priceRaw') or q.get('price') or 0)
            buy_low, buy_high = float(plan.get('buyLow') or 0), float(plan.get('buyHigh') or 0)
            # 仅当实时价落入买入区间才生成（=明日开盘条件达成）
            if not (buy_low <= price <= buy_high) and buy_high > 0:
                continue
            qty = max(LOT, int(total * MAX_ORDER_PCT / price / LOT) * LOT) if price > 0 else 0
            _add('buy', sym, f.get('name', ''), round(price, 2), qty, 'tomorrow_plan',
                 f"收评明日计划：{plan.get('condition','')}")

    # 2) 持仓诊断动作（复用 position.diagnose_position）
    if 'diagnosis' in sources and positions:
        try:
            from services.recommend_review import get_recent_loser_symbols
            losers = get_recent_loser_symbols()
        except Exception:
            losers = set()
        market_change = 0.0
        try:
            m = (await _quotes(['sh000001'])).get('sh000001')
            if m:
                market_change = float(m.get('changePercent') or 0)
        except Exception:
            pass
        for p in positions:
            q = qmap.get(p['symbol'].lower())
            d = diagnose_position(p, q, market_change, losers)
            action = d.get('action')
            if action in ('stop_loss', 'take_profit'):
                _add('sell', p['symbol'], p['name'], round(d.get('price') or 0, 2), p['shares'],
                     'diagnosis', f"诊断动作：{d.get('action_label')}（{d.get('detail','')}）")
            elif action == 'reduce':
                half = int(p['shares'] / 2 / LOT) * LOT
                if half > 0:
                    _add('sell', p['symbol'], p['name'], round(d.get('price') or 0, 2), half,
                         'diagnosis', f"诊断动作：减仓（{d.get('detail','')}）")

    # 3) 筛选/趋势选中
    if 'screener' in sources and symbols:
        for sym in symbols:
            sym = sym.lower()
            q = qmap.get(sym)
            if not q:
                continue
            price = float(q.get('priceRaw') or q.get('price') or 0)
            if price <= 0:
                continue
            qty = max(LOT, int(total * MAX_ORDER_PCT / price / LOT) * LOT)
            _add('buy', sym, q.get('name', sym), round(price, 2), qty, 'screener', '筛选/趋势选中生成')

    return {'success': True, 'created': created, 'rejected': rejected,
            'created_count': len(created), 'rejected_count': len(rejected)}


# ---------- 执行模拟成交 ----------
async def execute_sim(user_id: int) -> Dict:
    """把 pending 建议订单按实时价撮合成交（仅在模拟账户记账，绝不连券商）"""
    _ensure_sim_account(user_id)
    pending = _load_orders(user_id, status='pending')
    if not pending:
        return {'success': True, 'filled': [], 'rejected': [], 'message': '无待执行建议'}
    positions = _load_positions(user_id)
    cash_v, mv, total, day_start, day_pnl = await _portfolio(user_id, positions)
    qmap = await _quotes([o['symbol'] for o in pending])
    filled, rejected = [], []

    for o in pending:
        sym = o['symbol'].lower()
        q = qmap.get(sym)
        live = float(q.get('priceRaw') or q.get('price') or 0) if q else 0
        # 偏离校验：实时价相对建议价偏离 >5% 拒单
        dev = abs(live - (o['proposed_price'] or 0)) / (o['proposed_price'] or 1)
        if live <= 0:
            _reject(o['id'], user_id, '行情缺失，无法成交')
            rejected.append({'symbol': sym, 'reason': '行情缺失'})
            continue
        if dev > PRICE_DEVIATION:
            _reject(o['id'], user_id, f'价格偏离{dev:.1%}超{PRICE_DEVIATION:.0%}拒单（建议{o["proposed_price"]}/实时{live:.2f}）')
            rejected.append({'symbol': sym, 'reason': f'偏离{dev:.1%}'})
            continue
        if o['side'] == 'buy':
            err = _check_buy(user_id, sym, live, o['proposed_qty'], day_start, cash_v, positions, day_start, day_pnl)
            if err:
                _reject(o['id'], user_id, err)
                rejected.append({'symbol': sym, 'reason': err}); continue
            _fill_buy(user_id, o, live)
            cash_v -= live * o['proposed_qty']
            filled.append({'symbol': sym, 'name': o['name'], 'side': 'buy',
                           'price': live, 'qty': o['proposed_qty']})
        else:
            err = _check_sell(user_id, sym, o['proposed_qty'], live, positions, _today())
            if err:
                _reject(o['id'], user_id, err)
                rejected.append({'symbol': sym, 'reason': err}); continue
            _fill_sell(user_id, o, live)
            cash_v += live * o['proposed_qty']
            filled.append({'symbol': sym, 'name': o['name'], 'side': 'sell',
                           'price': live, 'qty': o['proposed_qty']})

    return {'success': True, 'filled': filled, 'rejected': rejected,
            'filled_count': len(filled), 'rejected_count': len(rejected)}


def _reject(order_id: int, user_id: int, reason: str):
    conn = get_db_conn()
    conn.execute('UPDATE sim_orders SET status=?, reject_reason=? WHERE id=? AND user_id=?',
                 ('rejected', reason, order_id, user_id))
    conn.commit(); conn.close()


def _fill_buy(user_id: int, o: Dict, price: float):
    conn = get_db_conn()
    cur = conn.cursor()
    row = cur.execute('SELECT shares, cost_price, buy_date FROM sim_positions WHERE user_id=? AND symbol=?',
                      (user_id, o['symbol'])).fetchone()
    qty = o['proposed_qty']
    if row:
        old_s, old_c, old_date = row['shares'], row['cost_price'], row['buy_date']
        new_s = old_s + qty
        new_c = (old_s * old_c + qty * price) / new_s
        cur.execute('UPDATE sim_positions SET shares=?, cost_price=?, updated_at=datetime(\'now\',\'+8 hours\') WHERE user_id=? AND symbol=?',
                    (new_s, round(new_c, 2), user_id, o['symbol']))
    else:
        cur.execute('''INSERT INTO sim_positions (user_id, symbol, name, shares, cost_price, buy_date)
                       VALUES (?,?,?,?,?,?)''',
                    (user_id, o['symbol'], o['name'], qty, round(price, 2), _today()))
    cur.execute('UPDATE sim_accounts SET cash=cash-?, updated_at=datetime(\'now\',\'+8 hours\') WHERE user_id=?',
                (price * qty, user_id))
    cur.execute('UPDATE sim_orders SET status=?, filled_price=?, filled_qty=?, filled_at=datetime(\'now\',\'+8 hours\') WHERE id=?',
                ('filled', price, qty, o['id']))
    conn.commit(); conn.close()


def _fill_sell(user_id: int, o: Dict, price: float):
    conn = get_db_conn()
    cur = conn.cursor()
    qty = o['proposed_qty']
    row = cur.execute('SELECT shares, buy_date FROM sim_positions WHERE user_id=? AND symbol=?',
                      (user_id, o['symbol'])).fetchone()
    if row:
        new_s = row['shares'] - qty
        if new_s <= 0:
            cur.execute('DELETE FROM sim_positions WHERE user_id=? AND symbol=?', (user_id, o['symbol']))
        else:
            cur.execute('UPDATE sim_positions SET shares=?, updated_at=datetime(\'now\',\'+8 hours\') WHERE user_id=? AND symbol=?',
                        (new_s, user_id, o['symbol']))
    cur.execute('UPDATE sim_accounts SET cash=cash+?, updated_at=datetime(\'now\',\'+8 hours\') WHERE user_id=?',
                (price * qty, user_id))
    cur.execute('UPDATE sim_orders SET status=?, filled_price=?, filled_qty=?, filled_at=datetime(\'now\',\'+8 hours\') WHERE id=?',
                ('filled', price, qty, o['id']))
    conn.commit(); conn.close()


# ---------- 查询 ----------
async def get_sim_state(user_id: int) -> Dict:
    _ensure_sim_account(user_id)
    positions = _load_positions(user_id)
    cash_v, mv, total, day_start, day_pnl = await _portfolio(user_id, positions)
    qmap = await _quotes([p['symbol'] for p in positions])
    pos_view = []
    cost_total = 0.0
    for p in positions:
        q = qmap.get(p['symbol'].lower())
        price = float(q.get('priceRaw') or q.get('price') or p['cost_price']) if q else p['cost_price']
        mv_p = price * p['shares']
        cost_p = p['cost_price'] * p['shares']
        cost_total += cost_p
        pos_view.append({
            'symbol': p['symbol'], 'name': p['name'], 'shares': p['shares'],
            'costPrice': p['cost_price'], 'price': round(price, 2),
            'marketValue': round(mv_p, 2), 'pnl': round(mv_p - cost_p, 2),
            'pnlPct': round((mv_p - cost_p) / cost_p * 100, 2) if cost_p else 0,
            'weight': round(mv_p / total * 100, 1) if total else 0,
        })
    return {
        'success': True,
        'account': {
            'cash': round(cash_v, 2), 'marketValue': round(mv, 2),
            'totalValue': round(total, 2),
            'dayPnl': round(day_pnl, 2),
            'dayPnlPct': round(day_pnl / day_start * 100, 2) if day_start else 0,
            'totalPnl': round(total - SIM_START_CASH, 2),
            'totalPnlPct': round((total - SIM_START_CASH) / SIM_START_CASH * 100, 2),
        },
        'positions': pos_view,
        'pending': [o for o in _load_orders(user_id, 'pending')],
        'audit': [o for o in _load_orders(user_id)[:50]],
    }


def reset_sim(user_id: int) -> Dict:
    conn = get_db_conn()
    conn.execute('DELETE FROM sim_positions WHERE user_id=?', (user_id,))
    conn.execute('DELETE FROM sim_orders WHERE user_id=?', (user_id,))
    conn.execute('UPDATE sim_accounts SET cash=?, day_start_value=?, day_start_date=? WHERE user_id=?',
                 (SIM_START_CASH, SIM_START_CASH, _today(), user_id))
    conn.commit(); conn.close()
    return {'success': True, 'message': '模拟账户已重置'}
