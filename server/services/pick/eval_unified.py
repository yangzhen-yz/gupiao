"""eval_unified.py - 推荐统一评估（2026-11-01）

为什么再写一个评估模块
----------------------
`recommend_review.py` 是从"低吸参考价能不能成交"长出来的：它的主指标 `r1` 是
**低吸价 → 次日收盘**，未成交的记录照样按收盘价记一笔盈利（成交率 68% 之外的
幻觉胜利），所以纸面 T+1 一直比用户实际体验好十几个点。2026-11-01 的胜率诊断要
回答的却是另一个问题："这套选股到底有没有超额"，这需要一个**人人可复制、不依赖
挂单技巧**的口径：

    主口径 P0：rec_date 当日**收盘价买入** → 次日**开盘价卖出**（不加杠杆、不挂单、不择时）
    参考口径  ：收盘→次日收盘（T+1 收盘）、收盘→T+3、收盘→T+5

四件事一并在这里做（原模块只做了第一件的一半）：

  1. **评分有效性**：按 score 分 5 桶，输出各桶的胜率/均值/盈亏比/吃面率，
     再算分组单调性（桶序与均值的 Spearman ρ、D5−D1 差值）。分数如果排不出高低，
     后面所有调权重的工作都是空转。
  2. **低吸逆向选择的机会成本**：成交组（次日最低价触及挂单价）与未成交组分开统计；
     未成交组要算"如果当初按收盘价买，本来能拿到多少"——这才叫机会成本，
     只报成交组的胜率等于只报赢了的那半边。
  3. **可成交性净胜率**：剔除封板票（推荐时涨幅≥9.8%，当日实际买不到）、重复推荐
     （同一只票 30 天内推第 2 次及以后）、ST/退、复权口径无效的样本，
     给出"真能落地的钱"的胜率。这是与"纸面胜率"并列的第二个主数字。
  4. **前后对照**：`--compare` 会同时输出旧口径（r1/exec_t1，来自 recommend_review）
     与新主口径，用于对照表（原《优化说明》已并入备份 server/analysis_out/docs_backup_20261102/）。

数据源：本地前复权日K库（data/history_kline.db）+ 主库推荐表。无网络依赖。

用法：
  python -m services.pick.eval_unified                 # 全量评估并打印
  python -m services.pick.eval_unified --days 90       # 只评近 90 天
  python -m services.pick.eval_unified --json out.json --md out.md
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from app.config import KLINE_HISTORY_DB_PATH

# 吃面线：与复盘页"吃面"同一口径（单笔亏 5% 以上）
NOODLE_PCT = -5.0
# 封板线：推荐时涨幅到这个数，当日的成交价就已经不是你想买的价了
SEALED_CHG = 9.8
# 重复推荐的判定窗口（自然日）：同一只票在这个窗口内被推荐过，第 2 次及以后都算重复
DUP_WINDOW_DAYS = 30


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(f'file:{KLINE_HISTORY_DB_PATH}?mode=ro', uri=True)


def _load_recs(days: Optional[int] = None) -> List[Dict]:
    """读推荐记录（与 recommend_review 同一张表、同一套清洗：两个口径都算不了才丢）。"""
    from db.database import get_db_conn
    conn = get_db_conn()
    try:
        rows = conn.execute(
            "SELECT rec_date, symbol, name, price, change_percent, score, buy_price, "
            "COALESCE(mode, '') FROM daily_recommendations ORDER BY rec_date, sort_order"
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            buy = float(r[6])
        except (TypeError, ValueError):
            buy = None
        if buy is not None and buy <= 0:
            buy = None
        try:
            price = float(r[3])
        except (TypeError, ValueError):
            price = 0.0
        if buy is None and price <= 0:
            continue
        out.append({'date': r[0], 'symbol': (r[1] or '').lower(), 'name': r[2] or '',
                    'rec_price': price, 'chg': r[4] or 0, 'score': r[5] or 0,
                    'buy': buy, 'mode': r[7] or ''})
    if days:
        cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        out = [r for r in out if r['date'] >= cutoff]
    return out


def _load_series(codes: List[str]) -> Dict[str, List[Tuple[str, float, float, float]]]:
    """{裸码: [(date, open, close, low), ...] 升序}。一次 SQL 全捞，不逐票查。"""
    out: Dict[str, List[Tuple[str, float, float, float]]] = {}
    if not codes:
        return out
    conn = _conn()
    try:
        ph = ','.join('?' * len(codes))
        for sym, d, o, c, l in conn.execute(
                f'SELECT symbol, date, open, close, low FROM stock_daily '
                f'WHERE symbol IN ({ph}) ORDER BY symbol, date', tuple(codes)).fetchall():
            if c is None:
                continue
            out.setdefault(sym, []).append((d, float(o or 0), float(c), float(l or 0)))
    finally:
        conn.close()
    return out


def _idx_on_or_before(series: List[Tuple[str, float, float, float]], date_str: str) -> int:
    lo, hi, best = 0, len(series) - 1, -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= date_str:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    return best


def build_dataset(days: Optional[int] = None) -> List[Dict]:
    """构造统一评估数据集：每条推荐一行，带四种口径的收益与可成交性标记。"""
    from services.pick.recommend_review import (_exdiv_index, _mark_data_quality)
    recs = _load_recs(days)
    codes = sorted({(r['symbol'] or '')[-6:] for r in recs if r['symbol']})
    series = _load_series(codes)
    exdiv = _exdiv_index()
    out: List[Dict] = []
    for rec in recs:
        code = (rec['symbol'] or '')[-6:]
        bars = series.get(code) or []
        i = _idx_on_or_before(bars, rec['date'])
        if i < 0 or i + 1 >= len(bars):
            continue                       # 推荐日没有K线，或还没有次日（今天刚推的）
        rec_close = bars[i][2]
        if not rec_close or rec_close <= 0:
            continue
        r = dict(rec)
        r['rec_close'] = round(rec_close, 3)
        # 复权口径校验（与复盘同一判据）：无效样本不参与任何胜率，但保留在明细里
        _mark_data_quality(r, bars[i][0], rec_close, exdiv, code)
        nxt = bars[i + 1]
        r['next_open'] = nxt[1]
        r['next_close'] = nxt[2]
        r['next_low'] = nxt[3]
        r['ret_open'] = round((nxt[1] / rec_close - 1) * 100, 2)      # P0 主口径
        r['ret_t1'] = round((nxt[2] / rec_close - 1) * 100, 2)
        r['ret_t3'] = (round((bars[i + 3][2] / rec_close - 1) * 100, 2)
                       if i + 3 < len(bars) else None)
        r['ret_t5'] = (round((bars[i + 5][2] / rec_close - 1) * 100, 2)
                       if i + 5 < len(bars) else None)
        r['dd5'] = round((min(b[3] for b in bars[i + 1:i + 6]) / rec_close - 1) * 100, 2)
        # 低吸纪律：次日最低价触及挂单价才算成交；成交价按集合竞价规则取 min(挂单价, 开盘价)
        if rec.get('buy'):
            r['filled'] = 1 if nxt[3] <= rec['buy'] else 0
            if r['filled']:
                fp = min(rec['buy'], nxt[1]) if nxt[1] > 0 else rec['buy']
                r['fill_price'] = round(fp, 3)
                r['ret_fill'] = round((nxt[2] / fp - 1) * 100, 2)
            else:
                r['fill_price'] = None
                r['ret_fill'] = None
        r['sealed'] = 1 if float(rec.get('chg') or 0) >= SEALED_CHG else 0
        out.append(r)

    # 重复推荐：同一只票在 DUP_WINDOW_DAYS 天内第 2 次及以后标记 dup=1（保留第一条）
    last_seen: Dict[str, str] = {}
    for r in sorted(out, key=lambda x: (x['date'], x['symbol'])):
        prev = last_seen.get(r['symbol'])
        r['dup'] = 0
        if prev:
            try:
                gap = (datetime.strptime(r['date'], '%Y-%m-%d')
                       - datetime.strptime(prev, '%Y-%m-%d')).days
            except ValueError:
                gap = 99
            if 0 <= gap <= DUP_WINDOW_DAYS:
                r['dup'] = 1
        last_seen[r['symbol']] = r['date']
    return out


def _stats(vals: List[float]) -> Dict:
    """一组收益的基本统计：n/胜率/均值/中位/盈亏比/吃面率/最差。"""
    vals = [v for v in vals if v is not None]
    if not vals:
        return {'n': 0}
    n = len(vals)
    wins = [v for v in vals if v > 0]
    losses = [v for v in vals if v <= 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    return {
        'n': n,
        'win_rate': round(len(wins) / n * 100, 1),
        'avg': round(sum(vals) / n, 2),
        'median': round(sorted(vals)[n // 2], 2),
        'payoff': round(avg_win / avg_loss, 2) if avg_loss > 0 else None,
        'noodle_rate': round(sum(1 for v in vals if v <= NOODLE_PCT) / n * 100, 1),
        'worst': round(min(vals), 2),
    }


def _spearman(order_vals: List[float]) -> Optional[float]:
    """桶序(1..k)与桶均值的 Spearman ρ，无 scipy。并列取平均秩不影响 5 桶结论。"""
    k = len(order_vals)
    if k < 3:
        return None
    ranks = [0] * k
    sorted_idx = sorted(range(k), key=lambda i: order_vals[i])
    for rank, i in enumerate(sorted_idx):
        ranks[i] = rank + 1
    d2 = sum((ranks[i] - (i + 1)) ** 2 for i in range(k))
    return round(1 - 6 * d2 / (k * (k * k - 1)), 3)


def score_buckets(rows: List[Dict], ret_key: str = 'ret_open', k: int = 5) -> Dict:
    """按 score 分 k 桶（等频），输出每桶统计 + 单调性。

    D1 = 分数最低桶，D5 = 最高桶。若"分数高 → 收益高"成立，D5 均值应显著大于 D1。
    """
    rs = [r for r in rows if not r.get('data_invalid') and r.get(ret_key) is not None]
    if len(rs) < k * 5:
        return {'available': False, 'why': f'样本 {len(rs)} 条，不足 {k * 5}'}
    rs.sort(key=lambda x: (x.get('score') or 0))
    n = len(rs)
    buckets = []
    for b in range(k):
        part = rs[b * n // k:(b + 1) * n // k]
        st = _stats([r[ret_key] for r in part])
        st['label'] = f'D{b + 1}'
        st['score_lo'] = part[0].get('score')
        st['score_hi'] = part[-1].get('score')
        buckets.append(st)
    means = [b.get('avg', 0) for b in buckets]
    return {
        'available': True,
        'ret_key': ret_key,
        'buckets': buckets,
        'spearman': _spearman(means),
        'd5_minus_d1': round(means[-1] - means[0], 2),
        'spread_detail': {'d1_avg': means[0], 'd5_avg': means[-1]},
    }


def dixi_opportunity(rows: List[Dict]) -> Dict:
    """低吸挂单的成交/未成交分账 + 机会成本。

    未成交组的"机会成本"= 如果不等回落、直接按收盘价买入（P0 主口径）本该拿到的收益。
    它为正说明刚性低吸漏掉了强势延续；为负说明不等回落是对的。
    """
    rs = [r for r in rows if not r.get('data_invalid') and r.get('filled') is not None
          and r.get('ret_t1') is not None]
    if not rs:
        return {'available': False}
    filled = [r for r in rs if r['filled'] == 1]
    missed = [r for r in rs if r['filled'] == 0]
    f_stats = _stats([r['ret_fill'] for r in filled])
    m_close = _stats([r['ret_t1'] for r in missed])     # 未成交：若按收盘买
    m_open = _stats([r['ret_open'] for r in missed])
    fill_rate = round(len(filled) / len(rs) * 100, 1)
    ev = round(fill_rate / 100 * (f_stats.get('avg') or 0), 2)   # 未成交按 0，不幻想
    close_ev = _stats([r['ret_t1'] for r in rs]).get('avg')
    return {
        'available': True,
        'n': len(rs),
        'fill_rate': fill_rate,
        'filled': f_stats,
        'missed': {'n': len(missed),
                   'opportunity_close': m_close.get('avg'),      # 未成交但按收盘买的均值
                   'opportunity_open': m_open.get('avg'),
                   'detail_close': m_close},
        'dixi_ev': ev,
        'all_in_close_ev': close_ev,
        'ev_gap': round((ev - (close_ev or 0)), 2) if close_ev is not None else None,
    }


def tradeable_net(rows: List[Dict], ret_key: str = 'ret_open') -> Dict:
    """可成交口径净胜率：剔除"钱根本落不了地"的样本。

    剔除四类，逐类报数（关掉哪一类要能被看见，不做黑箱）：
      sealed   推荐时已封板（涨幅≥9.8%）—— 当天买不到，收益再高也只是纸面
      dup      30 天内重复推荐同一只 —— 复盘时它被统计了 N 次，等于给一只票加权
      st       名称含 ST/退
      invalid  复权口径无效（data_invalid）
    """
    base = [r for r in rows if r.get(ret_key) is not None]
    from services.data.universe import is_st_name
    bad_seal = [r for r in base if r.get('sealed')]
    bad_dup = [r for r in base if r.get('dup')]
    bad_st = [r for r in base if is_st_name(r.get('name'))]
    bad_inv = [r for r in base if r.get('data_invalid')]
    keep = [r for r in base
            if not r.get('sealed') and not r.get('dup')
            and not is_st_name(r.get('name')) and not r.get('data_invalid')]
    raw = _stats([r[ret_key] for r in base])
    net = _stats([r[ret_key] for r in keep])
    return {
        'available': True,
        'ret_key': ret_key,
        'raw': raw,
        'net': net,
        'dropped': {'sealed': len(bad_seal), 'dup': len(bad_dup),
                    'st': len(bad_st), 'invalid': len(bad_inv)},
        'keep_n': len(keep),
        'win_delta': (round((net.get('win_rate') or 0) - (raw.get('win_rate') or 0), 1)
                      if net.get('n') and raw.get('n') else None),
        'avg_delta': (round((net.get('avg') or 0) - (raw.get('avg') or 0), 2)
                      if net.get('n') and raw.get('n') else None),
    }


def summarize(days: Optional[int] = None) -> Dict:
    rows = build_dataset(days)
    valid = [r for r in rows if not r.get('data_invalid')]
    out = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'days': days,
        'n_all': len(rows),
        'n_invalid': len(rows) - len(valid),
        'period': {'from': min((r['date'] for r in rows), default=''),
                   'to': max((r['date'] for r in rows), default='')},
        # 主口径与三个参考口径并排：主口径不依赖挂单技巧，是"选股本身"的净效果
        'ret_open': _stats([r['ret_open'] for r in valid]),
        'ret_t1': _stats([r['ret_t1'] for r in valid]),
        'ret_t3': _stats([r['ret_t3'] for r in valid]),
        'ret_t5': _stats([r['ret_t5'] for r in valid]),
        'dd5': _stats([r['dd5'] for r in valid]),
        'buckets': score_buckets(valid, 'ret_open'),
        'buckets_t1': score_buckets(valid, 'ret_t1'),
        'dixi': dixi_opportunity(valid),
        'tradeable': tradeable_net(valid, 'ret_open'),
        'tradeable_t1': tradeable_net(valid, 'ret_t1'),
    }
    return out


def render_markdown(s: Dict) -> str:
    """把汇总渲染成对照表（出报告前先在这看一遍）。"""
    def row(label, st):
        if not st or not st.get('n'):
            return f'| {label} | 0 | - | - | - | - | - |'
        return (f"| {label} | {st['n']} | {st['win_rate']}% | {st['avg']:+.2f}% | "
                f"{st['median']:+.2f}% | {st['payoff'] if st['payoff'] is not None else '-'} | "
                f"{st['noodle_rate']}% |")

    L = []
    L.append(f"### 样本\n")
    L.append(f"- 区间：{s['period']['from']} ~ {s['period']['to']}"
             + (f"（近 {s['days']} 天）" if s.get('days') else ''))
    L.append(f"- 条目：{s['n_all']} 条，其中复权口径无效剔除 {s['n_invalid']} 条，"
             f"有效 {s['n_all'] - s['n_invalid']} 条\n")
    L.append('### 主口径与参考口径（同一批有效样本）\n')
    L.append('| 口径 | n | 胜率 | 均值 | 中位 | 盈亏比 | 吃面率(≤-5%) |')
    L.append('|---|---|---|---|---|---|---|')
    L.append(row('P0 当日收盘买→次日开盘卖（主）', s['ret_open']))
    L.append(row('收盘→次日收盘（T+1）', s['ret_t1']))
    L.append(row('收盘→T+3', s['ret_t3']))
    L.append(row('收盘→T+5', s['ret_t5']))
    L.append('')
    b = s.get('buckets') or {}
    L.append('### 评分有效性（P0 口径，按分数等频分 5 桶）\n')
    if b.get('available'):
        L.append('| 桶 | 分数区间 | n | 胜率 | 均值 | 中位 | 盈亏比 | 吃面率 |')
        L.append('|---|---|---|---|---|---|---|---|')
        for x in b['buckets']:
            L.append(f"| {x['label']} | {x.get('score_lo')}~{x.get('score_hi')} | {x['n']} | "
                     f"{x['win_rate']}% | {x['avg']:+.2f}% | {x['median']:+.2f}% | "
                     f"{x['payoff'] if x['payoff'] is not None else '-'} | {x['noodle_rate']}% |")
        L.append('')
        L.append(f"- 单调性：Spearman ρ = **{b['spearman']}**，D5−D1 = **{b['d5_minus_d1']:+.2f}pp**")
        L.append('')
    else:
        L.append(f"- 不可用：{b.get('why')}\n")
    d = s.get('dixi') or {}
    L.append('### 低吸挂单：成交与未成交分账\n')
    if d.get('available'):
        f_, m_ = d['filled'], d['missed']
        L.append(f"- 样本 {d['n']} 条，可成交率 **{d['fill_rate']}%**（{f_.get('n', 0)}/{d['n']}）")
        L.append(f"- 成交组（真实交易）：胜率 {f_.get('win_rate')}%，均值 {f_.get('avg'):+.2f}%"
                 f"（n={f_.get('n')}）")
        L.append(f"- 未成交组：若按当日收盘价买，次日收盘均值 **{m_['opportunity_close']:+.2f}%**，"
                 f"次日开盘卖 {m_['opportunity_open']:+.2f}%（n={m_['n']}）"
                 f"—— 这就是「没等到回落」的机会成本")
        L.append(f"- 低吸纪律 EV（成交率×成交组均值，未成交记 0）= **{d['dixi_ev']:+.2f}%**，"
                 f"全程按收盘买的 EV = **{d['all_in_close_ev']:+.2f}%**，差 {d['ev_gap']:+.2f}pp")
        L.append('')
    else:
        L.append('- 不可用（没有带数值低吸价的样本）\n')
    t = s.get('tradeable') or {}
    L.append('### 可成交口径（P0）\n')
    if t.get('available'):
        L.append(f"- 原始：n={t['raw'].get('n')}，胜率 {t['raw'].get('win_rate')}%，"
                 f"均值 {t['raw'].get('avg'):+.2f}%")
        L.append(f"- 剔除后：n={t['keep_n']}，胜率 **{t['net'].get('win_rate')}%**，"
                 f"均值 **{t['net'].get('avg'):+.2f}%**"
                 f"（胜率 {t['win_delta']:+.1f}pp，均值 {t['avg_delta']:+.2f}pp）")
        dd = t['dropped']
        L.append(f"- 剔除明细：封板 {dd['sealed']} 条、重复推 {dd['dup']} 条、"
                 f"ST/退 {dd['st']} 条、复权无效 {dd['invalid']} 条")
        L.append('')
    return '\n'.join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=None, help='只评最近 N 天')
    ap.add_argument('--json', default='', help='把汇总写成 JSON')
    ap.add_argument('--md', default='', help='把对照表写成 Markdown')
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    s = summarize(a.days)
    md = render_markdown(s)
    print(md)
    if a.json:
        with open(a.json, 'w', encoding='utf-8') as fh:
            json.dump(s, fh, ensure_ascii=False, indent=1, default=str)
        print(f'\nJSON 已写入 {a.json}')
    if a.md:
        with open(a.md, 'w', encoding='utf-8') as fh:
            fh.write(md + '\n')
        print(f'Markdown 已写入 {a.md}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
