"""recommend_review.py - 智能推荐复盘闭环（2026-10-12 新增）

背景：用户反馈"推荐的股票买入就吃面"。对 2026-05~10 共 132 条历史推荐的复盘
（本地历史K线库计算）发现：
  - 从"买入参考价"（低吸价）算 T+1 胜率 62.9%、均值 +1.52%
  - 从"推荐时现价"算 T+1 胜率仅 45.5%、均值 -1.12%（用户实际体验）
  - 隔日平均低开 -1.29%，"现价-3%"的低吸单成交率低
  - 半山腰（推荐时涨幅 4.5%~9.8% 未涨停）T+1 胜率 44%、均值 -2.31%（死亡区）
  - 三祥新材被连续推荐两次、各亏 26%+（无黑榜机制）

本模块提供：
  1. compute_recommendation_performance()  逐条计算 T+1/T+3/T+5 收益 + 5日最大回撤
  2. run_recommendation_review()           每日复盘任务：聚合并持久化
  3. get_recent_loser_symbols()            近N天推荐且亏损的黑名单符号（选股避让）
  4. get_review_summary()                  复盘汇总（API/前端展示）

数据源：本地历史日K线库（data/history_kline.db，baostock 前复权，无网络依赖），
        T+N 按 rec_date 之后第 N 个交易日收盘价计算。
"""
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

from app.config import KLINE_HISTORY_ENABLED, KLINE_HISTORY_DB_PATH
from db.database import get_db_conn


def _local_history_conn():
    """本地历史K线库只读连接（独立库，避免与主库事务互相影响）"""
    conn = sqlite3.connect(f'file:{KLINE_HISTORY_DB_PATH}?mode=ro', uri=True)
    return conn


def compute_recommendation_performance(recs: List[Dict]) -> List[Dict]:
    """对推荐记录列表计算 T+1/T+3/T+5 收益与5日最大回撤

    Args:
        recs: [{'date','symbol','name','price','chg','score','buy'}, ...]
              buy 为数值买入价（'竞价关注'等非数值记录应提前过滤）

    Returns:
        与原记录等长的列表（无后续K线的记录被剔除），新增 r1/r3/r5/dd5/exec_r1 字段：
        r1/r3/r5: 从买入参考价的 T+N 收益%
        exec_r1:  从推荐时现价立即买入的 T+1 收益%（用户实际体验口径）
        dd5:      5日内最低价相对买入价的最大回撤%
    """
    if not KLINE_HISTORY_ENABLED:
        return []
    out = []
    try:
        hconn = _local_history_conn()
    except Exception:
        return []
    try:
        hcur = hconn.cursor()
        for rec in recs:
            sym = (rec.get('symbol') or '').lower()
            code = sym.replace('sh', '').replace('sz', '').replace('bj', '')
            buy = rec.get('buy')
            rec_price = rec.get('rec_price')
            if not isinstance(buy, (int, float)) or buy <= 0:
                continue
            try:
                hcur.execute(
                    "SELECT date, open, close, low FROM stock_daily WHERE symbol=? AND date>? ORDER BY date LIMIT 5",
                    (code, rec['date']))
                bars = hcur.fetchall()
            except Exception:
                continue
            if not bars:
                continue
            r = dict(rec)
            r['r1'] = round((bars[0][2] / buy - 1) * 100, 2)
            r['r3'] = round((bars[2][2] / buy - 1) * 100, 2) if len(bars) >= 3 else None
            r['r5'] = round((bars[4][2] / buy - 1) * 100, 2) if len(bars) >= 5 else None
            r['dd5'] = round((min(x[3] for x in bars) / buy - 1) * 100, 2)
            if isinstance(rec_price, (int, float)) and rec_price > 0:
                r['exec_r1'] = round((bars[0][2] / rec_price - 1) * 100, 2)
            out.append(r)
    finally:
        hconn.close()
    return out


def _load_all_recommendations() -> List[Dict]:
    """从主库加载全部推荐记录（含 price 作为推荐时现价）"""
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""SELECT rec_date, symbol, name, price, change_percent, score, buy_price,
                              COALESCE(mode, '')
                       FROM daily_recommendations ORDER BY rec_date""")
        recs = []
        for row in cur.fetchall():
            try:
                buy = float(row[6])
            except (TypeError, ValueError):
                continue  # '竞价关注'等非数值买入价无法计算
            if buy <= 0:
                continue
            try:
                price = float(row[3])
            except (TypeError, ValueError):
                price = 0
            recs.append({'date': row[0], 'symbol': (row[1] or '').lower(), 'name': row[2],
                         'chg': row[4] or 0, 'score': row[5] or 0,
                         'buy': buy, 'rec_price': price,
                         # 模式标签（2026-10-18）：老记录是空串，聚合成 '其他' 一档，不影响历史数字
                         'mode': row[7] or ''})
        conn.close()
        return recs
    except Exception as e:
        print(f'[推荐复盘] 加载推荐记录失败: {e}')
        return []


def _aggregate(perf: List[Dict]) -> Dict:
    """聚合一组复盘结果为汇总指标"""
    def _stats(key):
        vals = [r[key] for r in perf if r.get(key) is not None]
        if not vals:
            return None
        return {'n': len(vals),
                'win_rate': round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1),
                'avg': round(sum(vals) / len(vals), 2),
                'median': round(sorted(vals)[len(vals) // 2], 2)}
    dds = [r['dd5'] for r in perf if r.get('dd5') is not None]
    return {
        't1': _stats('r1'), 't3': _stats('r3'), 't5': _stats('r5'),
        'exec_t1': _stats('exec_r1'),
        'dd5_avg': round(sum(dds) / len(dds), 2) if dds else None,
        'noodle_count': sum(1 for r in perf if (r.get('r1') or 0) < -5),
    }


def run_recommendation_review() -> Dict:
    """每日推荐复盘任务：全量计算 + 按日聚合 + 持久化到 recommendation_review 表

    由 scheduler 每日 16:10 调用（本地K线库 16:00 同步后，保证当天数据就绪）。
    """
    recs = _load_all_recommendations()
    if not recs:
        print('[推荐复盘] 无推荐记录，跳过')
        return {}
    perf = compute_recommendation_performance(recs)
    if not perf:
        print('[推荐复盘] 本地K线库无可用数据，跳过')
        return {}

    # 按日期聚合
    by_date: Dict[str, List[Dict]] = {}
    for r in perf:
        by_date.setdefault(r['date'], []).append(r)

    summary = _aggregate(perf)
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS recommendation_review (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_date TEXT NOT NULL UNIQUE,
            total_count INTEGER DEFAULT 0,
            summary_json TEXT,
            by_date_json TEXT,
            created_at DATETIME DEFAULT (datetime('now', '+8 hours')))''')
        # 同一天重复跑则覆盖
        cur.execute('DELETE FROM recommendation_review WHERE review_date = ?',
                    (datetime.now().strftime('%Y-%m-%d'),))
        cur.execute('''INSERT INTO recommendation_review
                       (review_date, total_count, summary_json, by_date_json)
                       VALUES (?, ?, ?, ?)''',
                    (datetime.now().strftime('%Y-%m-%d'), len(perf),
                     json.dumps(summary, ensure_ascii=False),
                     json.dumps({d: _aggregate(v) for d, v in sorted(by_date.items())},
                                ensure_ascii=False)))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'[推荐复盘] 持久化失败: {e}')
        return summary

    t1 = summary.get('t1') or {}
    ex = summary.get('exec_t1') or {}
    print(f"[推荐复盘] 全量{len(perf)}条: T+1胜率{t1.get('win_rate')}% 均值{t1.get('avg')}%"
          f" | 现价追入T+1胜率{ex.get('win_rate')}% 均值{ex.get('avg')}%"
          f" | 吃面(>5%){summary.get('noodle_count')}只")
    return summary


_LOSER_CACHE = {'ts': 0.0, 'map': {}}


def get_loser_map(days: int = None, loss_threshold: float = None) -> Dict[str, Dict]:
    """当前仍在禁推期内的票 → {关多少天, 到期日, 最差收益, 推荐日, 判据}

    2026-10-17 重写（用户确认的三点优化）：
      · 判据可选 `t1`（老行为，只看 T+1 一根）或 `t1_or_t3`（T+1 或 T+3 任一亏超阈值）——
        只看 T+1 会被一根噪声左右：金安国纪 10-09 推荐 T+1 −7.22% 但 T+3 已回到 −2.47%，
        而利通电子 T+1 只 −3.52%、真正该警惕的是 T+5 的 −8.97%；
      · 可分级：轻亏（阈值~重亏线）只关 10 天、重亏（>5%）关 30 天，避免"−3.1% 和 −10.5%
        同罪"；
      · 结果按 `RECOMMEND_LOSER_CACHE_SEC` 缓存 —— 这张表要对全量推荐算 T+1/3/5，
        而盘中 60 秒扫描现在也要查它，不能每轮重算。
    默认配置（`criterion='t1'` + `graded=False`）与 2026-10-12 的老行为逐值一致。
    """
    import time as _t
    from app.config import (RECOMMEND_LOSER_LOOKBACK_DAYS, RECOMMEND_LOSER_LOSS_THRESHOLD,
                            RECOMMEND_LOSER_CRITERION, RECOMMEND_LOSER_GRADED,
                            RECOMMEND_LOSER_HEAVY_LOSS, RECOMMEND_LOSER_HEAVY_DAYS,
                            RECOMMEND_LOSER_LIGHT_DAYS, RECOMMEND_LOSER_CACHE_SEC)
    days = days if days is not None else _setting('loser_lookback_days', RECOMMEND_LOSER_LOOKBACK_DAYS)
    loss_threshold = (loss_threshold if loss_threshold is not None
                      else _setting('loser_loss_threshold', RECOMMEND_LOSER_LOSS_THRESHOLD))
    criterion = str(_setting('loser_criterion', RECOMMEND_LOSER_CRITERION))
    graded = bool(_setting('loser_graded', RECOMMEND_LOSER_GRADED))
    heavy = float(_setting('loser_heavy_loss', RECOMMEND_LOSER_HEAVY_LOSS))
    heavy_days = int(_setting('loser_heavy_days', RECOMMEND_LOSER_HEAVY_DAYS))
    light_days = int(_setting('loser_light_days', RECOMMEND_LOSER_LIGHT_DAYS))

    key = (days, loss_threshold, criterion, graded, heavy, heavy_days, light_days)
    if (_LOSER_CACHE.get('key') == key
            and _t.time() - _LOSER_CACHE['ts'] < RECOMMEND_LOSER_CACHE_SEC):
        return _LOSER_CACHE['map']

    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    recs = [r for r in _load_all_recommendations() if r['date'] >= cutoff]
    out: Dict[str, Dict] = {}
    if not recs:
        _LOSER_CACHE.update(ts=_t.time(), map=out, key=key)
        return out
    perf = compute_recommendation_performance(recs)
    today = datetime.now().date()
    for r in perf:
        r1 = r.get('r1')
        r3 = r.get('r3')
        worst, basis = None, ''
        if r1 is not None and r1 < -loss_threshold:
            worst, basis = r1, 'T+1'
        if criterion == 't1_or_t3' and r3 is not None and r3 < -loss_threshold:
            if worst is None or r3 < worst:
                worst, basis = r3, 'T+3'
        if worst is None:
            continue
        ban = (heavy_days if (graded and worst <= -heavy) else
               (light_days if graded else days))
        try:
            until = (datetime.strptime(r['date'], '%Y-%m-%d').date() + timedelta(days=ban)).isoformat()
        except ValueError:
            continue
        if until < today.isoformat():
            continue                       # 已过禁推期
        prev = out.get(r['symbol'])
        if not prev or (prev['until'] < until):   # 同一只票被推荐多次，取关得最久的那次
            out[r['symbol']] = {'ban_days': ban, 'until': until, 'worst': round(worst, 2),
                                'rec_date': r['date'], 'basis': basis}
    _LOSER_CACHE.update(ts=_t.time(), map=out, key=key)
    print(f"[推荐复盘] 亏损黑名单 {len(out)} 只（判据 {criterion}、{'分级' if graded else '一视同仁'}、"
          f"阈值 {loss_threshold}%、窗口 {days} 天）：" +
          ', '.join(f"{s}({v['basis']}{v['worst']:+.1f}% 至 {v['until']})" for s, v in sorted(out.items())))
    return out


def _setting(key: str, default):
    """覆盖表里能改的就现读（复盘子页"口径与开关"），读不到退回 config 默认值。"""
    try:
        from services.support import app_settings
        if key in [s['key'] for s in app_settings.SPEC]:
            return app_settings.value(key)
    except Exception:
        pass
    return default


def get_recent_loser_symbols(days: int = None, loss_threshold: float = None) -> Set[str]:
    """近N天被推荐过且 T+1 亏损超过阈值的股票代码集合（选股避让黑名单）

    背景：三祥新材 2026-06-10 / 06-29 连续两次被推荐、各亏 26%+——
    同一只票短期内推荐失败后不应再次出现。判据/分级见 `get_loser_map()`。
    返回集合是为了兼容老调用点（`in` 判断）；要看关到哪天用 `get_loser_map()`。
    """
    return set(get_loser_map(days, loss_threshold).keys())


def get_review_summary(days: int = 30) -> Dict:
    """复盘汇总（API 用）：全量 + 近N天 + 按日明细"""
    recs = _load_all_recommendations()
    perf = compute_recommendation_performance(recs)
    if not perf:
        # 键全部给齐：前端与调用方可以无脑读 by_mode/by_date，不用到处 .get()。
        # 临时库/新库跑自测时就会走到这里（假票号没有后续K线）。
        return {'available': False, 'total': {}, 'recent': {}, 'recent_days': days,
                'by_date': {}, 'by_mode': {}, 'worst': [], 'best': []}
    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    recent = [r for r in perf if r['date'] >= cutoff]
    by_date: Dict[str, List[Dict]] = {}
    for r in recent:
        by_date.setdefault(r['date'], []).append(r)
    # 按模式分组（2026-10-18，P2-2）：回答"哪种模式值得加码、哪种该停"。
    # 标签从 10-19 的批次才开始有内容，之前的老记录全部落 '其他' —— 这是诚实的起点，
    # 不是漏统计：没有标签的历史不能事后编一个模式出来。
    by_mode: Dict[str, List[Dict]] = {}
    try:
        from services.strategy.buy_rules import mode_label as _ml
    except Exception:
        _ml = lambda c: c or '其他'
    for r in perf:
        by_mode.setdefault(_ml(r.get('mode') or ''), []).append(r)
    return {
        'available': True,
        'total': _aggregate(perf),
        'recent': _aggregate(recent),
        'recent_days': days,
        'by_date': {d: _aggregate(v) for d, v in sorted(by_date.items(), reverse=True)},
        'by_mode': {m: _aggregate(v) for m, v in sorted(
            by_mode.items(), key=lambda kv: -len(kv[1]))},
        'worst': sorted(recent, key=lambda x: x.get('r1') or 0)[:5],
        'best': sorted(recent, key=lambda x: -(x.get('r1') or 0))[:5],
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
