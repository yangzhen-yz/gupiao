"""rec_sim_batch.py - 上午档 vs 下午档的拆批次回测（2026-10-21）

背景：`daily_recommendations` 同一天先删后插、只留最后一批（14:45），所以现有回测曲线
一直是"下午档"曲线——而下午档推的多是已涨停票、走次日竞价追买，和上午档的"盘中灵活
低吸"（如 10-21 的 10:26 上工申贝）根本是两种策略，混在一条曲线上互相污染。
`strategy_snapshot` 从 10-19 起每个批次都落了 stage/timing/快照价，拆档名单可以从
这张表还原（10-19 之前的上午名单已随推荐表覆盖丢失，无法回补——曲线只能从 10-19 攒起）。

口径（与现有曲线共用同一个 rec_sim 引擎，保证可比）：
  · 上午档 = 当天最后一个上午批次（<12:00，如 10:01/10:26）的 recommended 名单；
  · 下午档 = 14:45 名单（缺则取最后一个下午批次）；
  · 入口统一 'auction'（封板票次日竞价买·高开>5%不追；非封板按记录价当日买）——
    上午档的"记录价"是**推荐时点快照价**而非低吸撮合价，这是快照没存区间的近似，
    口径差异如实写进 labels.note，不与末批曲线争高下、只看两档相对强弱。
"""
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from app.config import KLINE_HISTORY_DB_PATH, SQLITE_DB_PATH
from services.pick import rec_sim

SIDE_LABEL = {'am': '上午档', 'pm': '下午档'}


def load_batch_recs(since: str) -> Tuple[List[Dict], List[Dict], Dict]:
    """从快照表还原两档名单。返回 (上午档recs, 下午档recs, 每日选批说明)。"""
    conn = sqlite3.connect(SQLITE_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT snap_date, batch, symbol, name, price, score, change_percent "
        "FROM strategy_snapshot WHERE stage='recommended' AND snap_date>=? "
        "ORDER BY snap_date, batch", (since,)).fetchall()
    conn.close()
    by_day = {}
    for r in rows:
        by_day.setdefault(r['snap_date'], {}).setdefault(r['batch'], []).append(dict(r))
    am_recs, pm_recs, labels = [], [], {}
    for d in sorted(by_day):
        batches = sorted(by_day[d])                       # 'HH:MM' 零填充，字符串序=时间序
        am_b = [b for b in batches if b < '12:00']
        pm_b = [b for b in batches if b >= '12:00']
        pick_am = am_b[-1] if am_b else None              # 上午取"最后一个上午批次"＝上午最终名单
        pick_pm = '14:45' if '14:45' in pm_b else (pm_b[-1] if pm_b else None)
        labels[d] = {'am': pick_am, 'pm': pick_pm}
        for side, pick in (('am', pick_am), ('pm', pick_pm)):
            for r in (by_day[d].get(pick) or []):
                rec = {'rec_date': d, 'symbol': r['symbol'], 'name': r['name'],
                       'price': float(r['price'] or 0), 'score': r['score'],
                       'change_percent': r['change_percent'], 'side': side}
                (am_recs if side == 'am' else pm_recs).append(rec)
    return am_recs, pm_recs, labels


def _bars_for(recs: List[Dict]) -> Dict[str, List[tuple]]:
    codes = sorted({(r['symbol'] or '')[2:] for r in recs})
    kconn = sqlite3.connect(KLINE_HISTORY_DB_PATH)
    bars = {}
    for code in codes:
        bars[code] = [tuple(x) for x in kconn.execute(
            "SELECT date, open, high, low, close FROM stock_daily WHERE symbol=? "
            "ORDER BY date", (code,)).fetchall()]
    kconn.close()
    return bars


def simulate_batch(months: int = 3, capital: float = 100000.0,
                   auction_max_gap: float = 5.0, whole_lots: bool = True) -> Dict:
    """两档 × 两卖法共四条曲线；引擎与末批曲线同一份（rec_sim._curve_stats）。"""
    since = (datetime.now() - timedelta(days=months * 31)).strftime('%Y-%m-%d')
    am_recs, pm_recs, labels = load_batch_recs(since)
    bars = _bars_for(am_recs + pm_recs)
    snap_min = min(labels, default='')
    out = {
        'window': {
            'since': max(since, snap_min) if snap_min else since,
            'until': max(labels, default='') if labels else '',
            'recDays': len(labels),
            'recCountAm': len(am_recs), 'recCountPm': len(pm_recs),
            'months': months,
            'snapshotSince': snap_min,
        },
        'capital': capital, 'labels': labels, 'curves': [],
        'note': ('上午档买入价=推荐时点快照价（近似，快照未存低吸区间）；下午档=14:45 名单；'
                 '入口/卖法/费用/封板判据与末批曲线完全同一引擎。快照 10-19 起才有，点数少只看方向。'),
        'generatedAt': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    for side, recs in (('am', am_recs), ('pm', pm_recs)):
        for mode in ('open', 'close'):
            c = rec_sim._curve_stats('auction', mode, recs, bars, capital,
                                     auction_max_gap, whole_lots)
            c['key'] = f'{side}_{mode}'
            c['side'] = side
            c['sideLabel'] = SIDE_LABEL[side]
            c['label'] = f"{SIDE_LABEL[side]} · {rec_sim.MODES[mode]}"
            c.pop('default', None)
            out['curves'].append(c)
    return out


def record_batch_daily(capital: float = 100000.0) -> Optional[Dict]:
    """16:26 定时任务：把两档四口径的当日摘要追加进 rec_sim_batch_daily（监控趋势用）。"""
    data = simulate_batch(months=12, capital=capital)
    rows = [(datetime.now().strftime('%Y-%m-%d %H:%M'),
             c['side'], c['mode'], c['retPct'], c['mdd'], c['trades'], c['skipped'],
             c['winRate'], c['end']) for c in data['curves']]
    conn = sqlite3.connect(SQLITE_DB_PATH, timeout=30)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS rec_sim_batch_daily (
            run_at TEXT NOT NULL, side TEXT NOT NULL, mode TEXT NOT NULL,
            ret_pct REAL, mdd REAL, trades INTEGER, skipped INTEGER,
            win_rate REAL, nav_end REAL,
            PRIMARY KEY (run_at, side, mode))""")
        conn.executemany("INSERT OR REPLACE INTO rec_sim_batch_daily VALUES (?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
    finally:
        conn.close()
    return {'rows': len(rows), 'window': data['window']}
