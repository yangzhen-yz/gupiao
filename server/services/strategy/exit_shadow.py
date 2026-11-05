# -*- coding: utf-8 -*-
"""exit_shadow.py - 卖出侧信号影子（2026-11-03，#11 情绪转弱→减仓）

一句话：**只算、只落表，不执行、不改推送、不参与任何现有判定。**

为什么要它
    选股侧已经证明 alpha≈0（P0 收盘买→次日开盘卖 52.5%/+0.70%，可成交净口径仍
    45.6%/−0.35%），文档的判断是"卖法比选股更值得做"。但"情绪转弱该减多少"现在
    **没有任何实测依据**——比例是用户心法的先验值。所以先影子落表攒对照，
    ≥20 个交易日后再裁决要不要接进动作链。

为什么单独一张表而不复用 position_alert_log
    那张表记的是"推了什么"，去重口径是 (date,user,symbol,action)，且市场级那条走账号 0；
    减仓建议要按**每个账号的每个持仓**记，还要带比例、股数、估算盈亏。混在一张表里
    两边口径会互相污染，所以另起 exit_signal_log。

边界（如实）
    · 减仓比例是**先验值，未做参数寻优**；
    · 只在情绪 `stale=False` 时算——降级数据不配生成建议；
    · T+1 当天不可卖的持仓**不记**（记了也执行不了，只会虚增信号量）；
    · A股卖出按手：减仓股数 floor 到 100 的整数倍，不足一手记 0 并在 note 写明原因；
    · 一天一票只记第一条（UNIQUE 幂等），盘中 20 秒一轮的扫描不会刷出几十行；
    · `applied` 恒 0，转 live 是另一个决定，不由这张表偷偷生效。
"""
from datetime import datetime
from typing import Dict, Optional

LOT_SIZE = 100          # A股一手
TRIGGER_WEAKEN = 'sentiment_weaken'


def _ensure_table() -> bool:
    try:
        from db.database import get_db_conn
        conn = get_db_conn()
        conn.execute('''CREATE TABLE IF NOT EXISTS exit_signal_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sig_date TEXT NOT NULL,
            ts TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT DEFAULT '',
            trigger_key TEXT NOT NULL,
            cycle TEXT,
            cycle_score INTEGER,
            action TEXT NOT NULL,
            ratio REAL,
            shares_before INTEGER,
            shares_reduce INTEGER,
            price REAL,
            est_pnl REAL,
            applied INTEGER NOT NULL DEFAULT 0,
            note TEXT DEFAULT '',
            UNIQUE(sig_date, user_id, symbol, trigger_key)
        )''')
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f'[减仓影子] 建表失败: {e}')
        return False


def reduce_plan(shares: int, ratio: float) -> Dict:
    """按比例算减仓股数，floor 到整手。返回 {shares_reduce, ratio, note}。"""
    shares = int(shares or 0)
    ratio = float(ratio or 0)
    if shares <= 0:
        return {'shares_reduce': 0, 'ratio': ratio, 'note': '持仓股数为 0，不记建议'}
    if ratio <= 0:
        return {'shares_reduce': 0, 'ratio': ratio, 'note': '该周期未配置减仓比例'}
    raw = shares * ratio
    lots = int(raw // LOT_SIZE)
    n = lots * LOT_SIZE
    if n <= 0:
        return {'shares_reduce': 0, 'ratio': ratio,
                'note': f'按 {ratio:.0%} 应减 {raw:.0f} 股，不足一手({LOT_SIZE})，本轮不减'}
    return {'shares_reduce': n, 'ratio': ratio, 'note': ''}


def record_sentiment_reduce(user_id: int, pos: Dict, sentiment: Dict,
                            date_str: str, ts: Optional[str] = None) -> bool:
    """为单个持仓落一条"情绪转弱该减多少"的影子信号。返回是否新写了一行。

    不抛异常：调用点在持仓快反主链路里，影子写失败绝不能影响推送与现有判定。
    """
    try:
        from app.config import EXIT_SENTIMENT_SHADOW_ENABLED, EXIT_SENTIMENT_REDUCE_RATIO
        if not EXIT_SENTIMENT_SHADOW_ENABLED:
            return False
    except Exception:
        return False

    cycle = str(sentiment.get('cycle') or '')
    ratio = float((EXIT_SENTIMENT_REDUCE_RATIO or {}).get(cycle, 0) or 0)
    sym = (pos.get('symbol') or '').lower()
    if not sym or not cycle:
        return False
    # 该周期没配减仓比例 → **不是信号，不写**。别留一行"减 0 股"的噪声污染统计。
    # 注意与下面"比例算出来不足一手"区分：那是真实情况（小仓位确实减不动），要写并说明原因。
    if ratio <= 0:
        return False

    plan = reduce_plan(int(pos.get('shares') or 0), ratio)
    price = float(pos.get('price') or 0)
    cost = float(pos.get('cost') or 0)
    est_pnl = round((price - cost) * plan['shares_reduce'], 2) \
        if (price > 0 and cost > 0 and plan['shares_reduce']) else None

    try:
        if not _ensure_table():
            return False
        from db.database import get_db_conn
        conn = get_db_conn()
        cur = conn.execute(
            'INSERT OR IGNORE INTO exit_signal_log '
            '(sig_date, ts, user_id, symbol, name, trigger_key, cycle, cycle_score, '
            ' action, ratio, shares_before, shares_reduce, price, est_pnl, applied, note) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)',
            (date_str, ts or datetime.now().strftime('%Y-%m-%d %H:%M:%S'), int(user_id),
             sym, pos.get('name') or sym, TRIGGER_WEAKEN, cycle,
             int(sentiment.get('cycleScore') or 0), 'reduce', plan['ratio'],
             int(pos.get('shares') or 0), plan['shares_reduce'], price, est_pnl,
             plan['note']))
        wrote = cur.rowcount > 0
        conn.commit()
        conn.close()
        if wrote:
            print(f'[减仓影子] {date_str} 账号{user_id} {pos.get("name") or sym} '
                  f'{cycle} → 建议减 {plan["shares_reduce"]} 股'
                  f'（{plan["ratio"]:.0%}，现价 {price}）'
                  + (f'｜{plan["note"]}' if plan['note'] else ''))
        return wrote
    except Exception as e:
        print(f'[减仓影子] 落信号失败（不影响推送）: {e}')
        return False


def load_signals(date_str: str, user_id: Optional[int] = None) -> list:
    """读某天的减仓影子信号（回测/对照用；线上决策不读这张表）。"""
    try:
        from db.database import get_db_conn
        conn = get_db_conn()
        sql = 'SELECT * FROM exit_signal_log WHERE sig_date = ?'
        args = [date_str]
        if user_id is not None:
            sql += ' AND user_id = ?'
            args.append(int(user_id))
        sql += ' ORDER BY symbol'
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        conn.close()
        return rows
    except Exception as e:
        print(f'[减仓影子] 读取失败: {e}')
        return []
