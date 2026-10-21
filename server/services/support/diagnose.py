"""diagnose.py - 五层本地诊断书聚合（2026-10-20，替代已删除的 AI 诊断弹窗）

背景：豆包/ARK 等 AI 源整体下线后，"诊断"改为把会话里验证过的五层格式固化成代码——
**不接任何大模型，输出的是结构化事实**，每一层都指得到表；查不到的层如实标 blind，
"缺证据本身就是证据的一部分"。用户在金安国纪(002636)首案上确认了这个格式。

五层与数据源（全部本地优先）：
  L1 位置   本地日K库 stock_daily（MA10/20/60、60日高低与回撤、vol5、量能对比）
  L2 资金   westock_snapshot（权威，当日 16:31 落库）→ 东财 fund_flow_snapshot →
            逐票新浪 _fetch_sina_money_flow（唯一可能出网的一跳，仅在前两层都缺时调）
  L3 环境   daily_market_reviews（情绪分/周期/炸板率/建议原文）
  L4 历史   strategy_snapshot（历批次分数与 blocked_by）+ daily_recommendations
            历次入选后 T+1/T+3 实际兑现（日K现算）
  L5 纪律   规则生成的操作价位（反转确认=MA10、箱体底=近10日低、失效位、追高禁区）

stance（回避/观察/谨慎看多）由反对/支持信号计数推导，判据全部可解释；本模块不做
任何预测。_OFFLINE 标志供自测断言零网络（新浪那一跳被 mock 时不得真发请求）。
"""
import json
import os
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional

_OFFLINE = False   # 自测置 True：禁止新浪逐票请求出网

# 与 buy_range.recent_volatility 同口径：近5个已收盘交易日日收益标准差（%）
import statistics


def _recent_vol_pct(closes: List[float], bars: int = 5) -> Optional[float]:
    if len(closes) < bars + 1:
        return None
    seq = closes[-(bars + 1):]
    rets = [(seq[i] / seq[i - 1] - 1) for i in range(1, len(seq)) if seq[i - 1] > 0]
    if len(rets) < bars:
        return None
    return round(statistics.stdev(rets) * 100, 2)


def _norm(symbol: str) -> str:
    s = (symbol or '').lower().strip()
    if len(s) == 8 and s[:2] in ('sh', 'sz'):
        return s[2:]
    return s


def _db():
    from app.config import SQLITE_DB_PATH
    conn = sqlite3.connect(SQLITE_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _kline_db():
    from app.config import KLINE_HISTORY_DB_PATH
    conn = sqlite3.connect(KLINE_HISTORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ========== L1 位置 ==========
def layer_position(code: str) -> Dict:
    kh = _kline_db()
    rows = kh.execute(
        "SELECT date, open, high, low, close, volume FROM stock_daily "
        "WHERE symbol=? ORDER BY date DESC LIMIT 70", (code,)).fetchall()
    kh.close()
    if len(rows) < 21:
        return {'available': False, 'reason': f'本地日K仅 {len(rows)} 根（<21），均线结构不可算'}
    rows = rows[::-1]
    closes = [r['close'] for r in rows]
    last = rows[-1]
    ma10 = round(sum(closes[-10:]) / 10, 2)
    ma20 = round(sum(closes[-20:]) / 20, 2)
    ma60 = round(sum(closes[-60:]) / 60, 2) if len(closes) >= 60 else None
    win = rows[-60:] if len(rows) >= 60 else rows
    hi, lo = max(r['high'] for r in win), min(r['low'] for r in win)
    vol5 = _recent_vol_pct(closes)
    last5 = [round((closes[i] / closes[i - 1] - 1) * 100, 2) for i in range(len(closes) - 5, len(closes))]
    avg_vol60 = sum(r['volume'] for r in win) / len(win)
    return {
        'available': True, 'asOf': last['date'], 'close': last['close'],
        'ma10': ma10, 'ma20': ma20, 'ma60': ma60,
        'belowAllMa': last['close'] < ma10 and last['close'] < ma20 and (ma60 is None or last['close'] < ma60),
        'high60': hi, 'low60': lo,
        'low10': min(r['low'] for r in rows[-10:]),
        'drawdownPct': round((last['close'] / hi - 1) * 100, 1),
        'vol5Pct': vol5, 'last5Pct': last5,
        'volShrink': round(last['volume'] / avg_vol60, 2) if avg_vol60 else None,
        'vol3d': [round(r['volume'] / 1e4, 0) for r in rows[-3:]],   # 万手
    }


# ========== L2 资金 ==========
def layer_fundflow(code: str, full_symbol: str) -> Dict:
    db = _db()
    # ① westock 权威快照（当日 16:31 落库，宇宙约百只，不保证覆盖）
    r = db.execute("SELECT date, main_net_inflow, main_net_5d, main_net_20d "
                   "FROM westock_snapshot WHERE symbol=? ORDER BY date DESC LIMIT 1",
                   (full_symbol,)).fetchone()
    if r and r['date'] == datetime.now().strftime('%Y-%m-%d'):
        db.close()
        return {'available': True, 'source': 'westock', 'date': r['date'],
                'mainNetYi': round(r['main_net_inflow'] / 1e8, 2),
                'mainNet5dYi': round(r['main_net_5d'] / 1e8, 2),
                'mainNet20dYi': round(r['main_net_20d'] / 1e8, 2),
                'note': 'westock（腾讯自选股）口径，与东财 f62 定义不同，符号可能背离'}
    # ② 东财当日快照榜
    row = db.execute("SELECT snap_json, updated_at FROM fund_flow_snapshot WHERE snap_key='stocks'").fetchone()
    db.close()
    if row and row['snap_json']:
        try:
            data = json.loads(row['snap_json'])
            lst = data if isinstance(data, list) else data.get('rows', [])
            hit = [x for x in lst if str(x.get('code', '')).endswith(code)]
            if hit and row['updated_at'][:10] == datetime.now().strftime('%Y-%m-%d'):
                x = hit[0]
                # 快照行字段 mainNetInflow 单位是元（f62），榜行统一换算成亿
                return {'available': True, 'source': 'eastmoney', 'date': row['updated_at'],
                        'mainNetYi': round(float(x.get('mainNetInflow') or 0) / 1e8, 2),
                        'note': '东财 f62（超大单+大单）口径'}
        except (ValueError, TypeError, KeyError):
            pass
    # ③ 新浪逐票（唯一可能出网的一跳）
    if not _OFFLINE:
        try:
            from services.data.fundflow import _fetch_sina_money_flow
            v = _fetch_sina_money_flow(full_symbol)
            if v is not None:
                # 函数契约是"元"（r0+r1 净额），换算成亿
                return {'available': True, 'source': 'sina', 'date': datetime.now().strftime('%Y-%m-%d'),
                        'mainNetYi': round(float(v) / 1e8, 2),
                        'note': '新浪 r0+r1 口径（与东财 f62 不同）'}
        except Exception:
            pass
    return {'available': False,
            'reason': '东财被封（当日快照榜内无此票/榜为空）＋不在 westock 今日宇宙＋新浪备源无返回——本层无数据'}


# ========== L3 环境 ==========
def layer_environment() -> Dict:
    db = _db()
    rv = db.execute("SELECT review_date, ai_analysis_json, sentiment_cycle_json "
                    "FROM daily_market_reviews ORDER BY review_date DESC LIMIT 1").fetchone()
    db.close()
    if not rv:
        return {'available': False, 'reason': '无收评记录'}
    ai = json.loads(rv['ai_analysis_json'] or '{}')
    cyc = json.loads(rv['sentiment_cycle_json'] or '{}')
    ind = cyc.get('indicators') or {}
    return {'available': True, 'date': rv['review_date'],
            'sentimentScore': ai.get('sentimentScore'),
            'cycle': cyc.get('cycle'), 'cycleScore': cyc.get('cycleScore'),
            'advice': cyc.get('advice'), 'detail': cyc.get('detail'),
            'zhabanRate': ind.get('zhabanRate'), 'maxLianban': ind.get('maxLianban'),
            'isToday': rv['review_date'] == datetime.now().strftime('%Y-%m-%d')}


# ========== L4 系统历史账 ==========
def layer_history(full_symbol: str) -> Dict:
    db = _db()
    snaps = db.execute(
        "SELECT snap_date, batch, rank, score, stage, blocked_by FROM strategy_snapshot "
        "WHERE symbol=? ORDER BY snap_date DESC, batch LIMIT 12", (full_symbol,)).fetchall()
    recs = db.execute(
        "SELECT rec_date FROM daily_recommendations WHERE symbol=? ORDER BY rec_date DESC LIMIT 6",
        (full_symbol,)).fetchall()
    db.close()
    # 历次正式推荐后 T+1/T+3 兑现（日K现算）
    kh = _kline_db()
    rows = kh.execute("SELECT date, close FROM stock_daily WHERE symbol=? ORDER BY date",
                      (_norm(full_symbol),)).fetchall()
    kh.close()
    by_date = {r['date']: r['close'] for r in rows}
    dates = sorted(by_date)
    outcomes = []
    for rec in recs:
        d = rec['rec_date']
        if d not in by_date:
            continue
        i = dates.index(d)
        base = by_date[d]
        item = {'recDate': d}
        for n in (1, 3):
            item[f't{n}'] = round((by_date[dates[i + n]] / base - 1) * 100, 1) if i + n < len(dates) else None
        outcomes.append(item)
    blocked_all = bool(snaps) and all((s['blocked_by'] or s['stage'] != 'recommended') for s in snaps)
    return {
        'available': True,
        'snapshots': [dict(s) for s in snaps],
        'everRecommended': bool(recs),
        'allBlocked': blocked_all,
        'outcomes': outcomes,   # [{recDate, t1, t3}]
    }


# ========== L5 纪律价位（纯规则） ==========
def layer_discipline(pos: Dict) -> Dict:
    if not pos.get('available'):
        return {'available': False, 'reason': '依赖 L1 位置数据'}
    c, ma10 = pos['close'], pos['ma10']
    items = [
        {'name': '反转确认', 'price': ma10,
         'text': f'放量站稳 MA10（≈{ma10}）之上、回踩不破，结构才算修复'},
        {'name': '箱体底沿', 'price': pos['low10'],
         'text': f'近10日最低 {pos["low10"]} 一带是箱体下沿；跌破即失效、无条件离场'},
        {'name': '追高禁区', 'price': round(c * 1.03, 2),
         'text': f'现价上方 3%（{round(c * 1.03, 2)}）以上不追'
                 + (f'；5日波动 {pos["vol5Pct"]}% 属高波动票，追一根大阳明天可能全数还回' if (pos['vol5Pct'] or 0) >= 3 else '')},
    ]
    return {'available': True, 'items': items}


# ========== 汇总 stance ==========
def _verdicts(pos: Dict, flow: Dict, env: Dict, hist: Dict) -> List[Dict]:
    v = []
    if pos.get('available'):
        if pos['belowAllMa']:
            v.append({'layer': '位置', 'verdict': 'oppose', 'text': '现价在 MA10/20/60 全部下方'})
        elif pos['close'] > (pos['ma10'] or 0):
            v.append({'layer': '位置', 'verdict': 'support', 'text': '现价站上 MA10'})
        else:
            v.append({'layer': '位置', 'verdict': 'oppose', 'text': '均线之间拉锯，无明确多头结构'})
        if pos.get('volShrink') is not None and pos['volShrink'] < 0.7:
            v.append({'layer': '量能', 'verdict': 'oppose', 'text': f"缩量（当日量为60日均量 {pos['volShrink']:.0%}）"})
    if flow.get('available'):
        net = flow.get('mainNetYi')
        v.append({'layer': '资金', 'verdict': 'support' if (net or 0) > 0 else 'oppose',
                  'text': f"主力净{ '流入' if (net or 0) > 0 else '流出' } {abs(net or 0):.2f} 亿（{flow['source']} 口径）"})
    else:
        v.append({'layer': '资金', 'verdict': 'blind', 'text': flow.get('reason', '无数据')})
    if env.get('available'):
        cs = env.get('cycleScore') or 0
        v.append({'layer': '环境', 'verdict': 'support' if cs >= 55 else 'oppose',
                  'text': f"情绪周期「{env.get('cycle')}」{cs} 分｜{env.get('detail') or ''}"})
    if hist.get('available'):
        if hist.get('allBlocked'):
            v.append({'layer': '系统', 'verdict': 'oppose', 'text': '历次评分全部被门槛挡下（未达 60）'})
        neg = [o for o in hist.get('outcomes', []) if (o.get('t3') is not None and o['t3'] < 0)]
        if hist.get('outcomes') and len(neg) > len(hist['outcomes']) / 2:
            worst = min((o['t3'] for o in hist['outcomes'] if o['t3'] is not None), default=0)
            v.append({'layer': '系统', 'verdict': 'oppose',
                      'text': f"历次推荐 T+3 过半亏损（最差 {worst}%）"})
    return v


def diagnose_stock(symbol: str) -> Dict:
    """入口（同步，路由里用 asyncio.to_thread 包一层）。symbol 支持 sh600519/600519。"""
    code = _norm(symbol)
    full = symbol.lower() if symbol.lower()[:2] in ('sh', 'sz') else (
        ('sh' if code.startswith(('6', '9')) else 'sz') + code)
    db = _db()
    name_row = db.execute("SELECT name FROM stock_basic_info WHERE symbol=?", (full,)).fetchone()
    db.close()
    pos = layer_position(code)
    flow = layer_fundflow(code, full)
    env = layer_environment()
    hist = layer_history(full)
    disc = layer_discipline(pos)
    vs = _verdicts(pos, flow, env, hist)
    oppose = sum(1 for x in vs if x['verdict'] == 'oppose')
    support = sum(1 for x in vs if x['verdict'] == 'support')
    stance = '回避' if oppose >= 3 else ('谨慎看多' if support > oppose else '观察')
    return {
        'success': True,
        'data': {
            'symbol': full, 'name': name_row['name'] if name_row else code,
            'asOf': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'stance': stance, 'signals': vs,
            'layers': {'position': pos, 'fundflow': flow, 'environment': env,
                       'history': hist, 'discipline': disc},
            'disclaimer': '纯本地数据聚合与规则判据，不含模型预测；缺数据的层已标 blind',
        },
    }
