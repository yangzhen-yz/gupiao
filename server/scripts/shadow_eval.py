"""shadow_eval.py - 影子规则 vs 线上规则的 T+1 对照（第1批 P0-2 的上线判据，2026-10-18）

要回答的问题只有一个：**影子那三条市场级规则（周期门槛 + 角色否决 + 不追高）挡掉的票，
第二天是不是真的更差？** 如果是，把门槛搬到线上才有依据；如果不是（挡掉的票反而涨得好），
说明这套周期规则在削收益，第1批就不该上。这个问题不能靠感觉答，本仓库已经吃过两次
"近似口径造出漂亮假数"的亏（595 天回测里"竞价 T+1 胜率 90%"就是稀疏帧 shift 错位）。

数据来源：`recommend_shadow_log`（每天每个批次一行 JSON，10-14 起在写），
里面 `kept`（影子放行）/ `excluded`（影子挡掉，带理由）/ `live`（线上 Top3）/ `shadow`（影子 Top3）
四组用的是**同一份候选池、同一套分数**，唯一差别就是那三条规则 —— 所以差集可比。

口径（三套，别混成一个数）
--------------------------
  · 主口径 **当日复权收盘 → 次日复权收盘**：影子日志里没存当时的现价（`_shadow_item` 只有
    涨幅/量比/分数），所以两组只能用同一个成本基准才能对比。**收盘→收盘完全在前复权序列
    内部算，不受除权漂移影响**，是唯一每天必然可算的口径。
  · 附 **当日收盘 → 次日开盘**（开盘即走，接力/打板的真实退出口径）。
  · 附 **批次价 → 次日收盘**：10-17 起 `strategy_snapshot` 落了那一刻的真实现价，
    能凑齐就同时算这一列 —— 这才是"人真能成交的价"。快照价和复权序列对不上的行
    （|收盘/快照价-1| > 涨停幅度+1%）判为除权漂移，**从这一列剔除**并计数。

样本不足时只报进度、不下结论：交易日数 < `SHADOW_EVAL_MIN_DAYS`（默认 5，2026-11-05 由 10 降至 5）时，
输出里 `enough=False`、`verdict='样本不足'`，只打印"还差几个交易日"。
影子每天只对分数前 15 只候选复算（`SHADOW_EVAL_POOL`），样本本来就攒得慢，
拿 3 天的 5 次对比去决定要不要改线上门槛，是这个仓库最不能干的事。

两处先天缺口，都在输出里计数、不偷偷补：
  · `kept`（放行整份名单）是 10-18 才加进 payload 的，10-15/10-16 的老日志只能按
    "影子Top3 ∪ 线上Top3 − 被挡"反推，缺"过了门槛但没进前 3"的长尾 —— 所以判定只认
    名单来自 payload 的那些天（`realKeptDays`），反推的天数只报数不参与结论。
  · 矩阵标签当时只挂给 live/shadow/excluded 三组，放行名单要按模式分层只能读时补算
    （同一个 `classify`，但拿不到当天的涨停家数，极热档两条规则不参与），
    补算了多少只进 `gap` 计数。

用法：
  python scripts/shadow_eval.py                 # 全部已有影子日志，每天取最后批次
  python scripts/shadow_eval.py --days 10       # 只看最近 10 个交易日
  python scripts/shadow_eval.py --all-slots     # 一天 4 个批次全算（同一只票一天最多被数 4 次）
  python scripts/shadow_eval.py --json          # 机器读（接口/前端用）
"""
import argparse
import bisect
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
import statistics

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfg                       # noqa: E402
from db.database import get_db_conn            # noqa: E402
from services.strategy.buy_rules import (       # noqa: E402
    classify, mode_label, pick_mode)

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

GROUPS = ('影子放行', '影子挡掉', '线上Top3', '影子Top3')
# 每天 4 个批次，同一只票会被数 4 次；默认只取当天最后一个批次 = 当日最终决策，
# 与 snapshot_report 同口径（两边数对不上时，第一个被怀疑的应该是这里）
DEFAULT_SLOT_LAST = True


def _limit_pct(code: str) -> float:
    return 20.0 if code.startswith(('30', '68')) else 10.0


def _stats(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    n = len(vals)
    return {'n': n,
            'win': round(sum(1 for v in vals if v > 0) / n * 100, 1),
            'avg': round(sum(vals) / n, 2),
            'med': round(sorted(vals)[n // 2], 2),
            'worst': round(min(vals), 2), 'best': round(max(vals), 2)}


def _fmt(s):
    if not s:
        return '无样本'
    return (f'{s["n"]:>4} 次  胜率{s["win"]:5.1f}%  均值{s["avg"]:+6.2f}%  '
            f'中位{s["med"]:+6.2f}%  最差{s["worst"]:+6.2f}%')


def _brief(s):
    return '无样本' if not s else f'{s["n"]}次 {s["win"]:.1f}%/{s["avg"]:+.2f}%'


def load_logs(conn, days=0, slot_last=DEFAULT_SLOT_LAST):
    """读影子日志 → [{'date','slot','cycle','minScore','payload'}]，按日期倒序。"""
    try:
        rows = [dict(r) for r in conn.execute(
            'SELECT rec_date, slot, cycle, min_score, payload FROM recommend_shadow_log '
            'ORDER BY rec_date DESC, slot DESC').fetchall()]
    except sqlite3.OperationalError:          # 表还没建 = 影子机制没跑过
        return []
    out = []
    for r in rows:
        try:
            r['payload'] = json.loads(r['payload'] or '{}')
        except (TypeError, ValueError):
            continue
        out.append(r)
    dates = sorted({x['rec_date'] for x in out}, reverse=True)
    if days > 0:
        keep = set(dates[:days])
        out = [x for x in out if x['rec_date'] in keep]
        dates = sorted(keep, reverse=True)
    if slot_last:
        last = {}
        for x in out:                          # 已按 slot 倒序，第一条就是当天最后批次
            last.setdefault(x['rec_date'], x['slot'])
        out = [x for x in out if x['slot'] == last[x['rec_date']]]
    return out


def _bars_map(kconn, codes):
    """每只票的 (日期列表, 复权OHLC)。一次把窗口内用到的票读全，别每行查一次。"""
    m = {}
    for code in codes:
        m[code] = kconn.execute(
            'SELECT date, open, close FROM stock_daily WHERE symbol=? ORDER BY date',
            (code,)).fetchall()
    return m


def _prices_map(conn, keys):
    """(date, slot, symbol) → 快照当时的现价。没有快照的日期返回空，不猜。"""
    if not keys:
        return {}
    dates = sorted({k[0] for k in keys})
    dph = ','.join('?' * len(dates))
    got = {}
    try:
        for d, b, s, p in conn.execute(
                f'SELECT snap_date, batch, symbol, price FROM strategy_snapshot '
                f'WHERE snap_date IN ({dph})', dates).fetchall():
            got[(d, b, (s or '').lower())] = p
    except sqlite3.OperationalError:           # 快照表还没建
        return {}
    return {k: got.get((k[0], k[1], k[2])) for k in keys}


def _prev_at(ser, i, code):
    """日志那天**的前两根**算出"昨日涨幅/昨日是否封板"（矩阵里"昨日封板不接力"这类规则要用）。

    `ser[i]` 是日志当天的日K（复盘是事后跑的，这根已经收盘了），所以昨日 = `ser[i-1]`。
    不能照搬线上 `_prev_bars`：那个函数取库里最新两根，是因为盘中"今天这根还没同步"，
    拿它跑历史日志会取到未来那根。
    """
    if i < 2:
        return None
    p1, p2 = ser[i - 1][2], ser[i - 2][2]
    if not p2:
        return None
    chg = (p1 / p2 - 1) * 100
    lim = 0.20 if code.startswith(('30', '68')) else 0.10
    return {'changePercent': round(chg, 2), 'limitClose': chg >= lim * 98.5,
            'barDate': ser[i - 1][0]}


def _matrix_fallback(ser, i, code, chg, vol):
    """给当时没挂矩阵标签的票（放行名单就是这种）读时补算一遍，模式维度才有分母。

    补算与当时判定用的同一套 `classify`，只是当日涨停家数拿不到（极热档两条规则不参与），
    汇总里以 `矩阵标签读时补算` 计数说明有多少事件是补的。
    """
    try:
        return classify({'changePercent': chg, 'volumeRatio': vol}, _prev_at(ser, i, code))
    except Exception:
        return None


def _kept_list(pl):
    """影子放行名单。10-18 才把 `kept` 塞进 payload，老日志只能反推。

    反推口径（标出来、不与真名单混称）：放行 = 影子Top3 ∪ 线上Top3 − 被挡。
    它只是"活下来的那几只"，不含过门槛却没进前 3 的长尾，所以样本偏小、
    汇总里用 `keptSource` 分开计数，真名单（payload）天数没满 MIN_DAYS 前不出判定。
    """
    if 'kept' in pl:
        return list(pl.get('kept') or []), 'payload'
    ex = {(x.get('symbol') or '').lower() for x in (pl.get('excluded') or [])}
    out = {}
    for g in ('shadow', 'live'):
        for x in pl.get(g) or []:
            sym = (x.get('symbol') or '').lower()
            if sym and sym not in ex:
                out[sym] = x
    return list(out.values()), '推导'


def build_events(logs, conn, kconn):
    """把四组票摊平成事件行，每行带上三套口径的 T+1 收益（算不出的是 None，不是 0）。"""
    events = []
    gap = Counter()
    kept_src = Counter()                        # 放行名单来源：payload（真名单）/ 推导（老日志）
    seen = set()                                # (date, slot, group, symbol) 去重
    want = set()
    for lg in logs:
        pl = lg['payload']
        kept, src = _kept_list(pl)
        kept_src[src] += 1
        for g, arr in (('影子放行', kept), ('影子挡掉', pl.get('excluded') or []),
                       ('线上Top3', pl.get('live') or []), ('影子Top3', pl.get('shadow') or [])):
            for it in arr:
                sym = (it.get('symbol') or '').lower()
                if sym:
                    want.add((lg['rec_date'], lg['slot'], sym))
    used = {sym[-6:] for (_d, _s, sym) in want if sym}
    bars = _bars_map(kconn, used)
    snap_price = _prices_map(conn, want)

    for lg in logs:
        pl = lg['payload']
        date, slot = lg['rec_date'], lg['slot']
        kept, ksrc = _kept_list(pl)
        if ksrc == '推导':
            gap['放行名单是反推的(老日志无 kept，样本偏小)'] += len(kept)
        groups = {'影子放行': kept, '影子挡掉': pl.get('excluded') or [],
                  '线上Top3': pl.get('live') or [], '影子Top3': pl.get('shadow') or []}
        for g, arr in groups.items():
            for it in arr:
                sym = (it.get('symbol') or '').lower()
                if not sym or (date, slot, g, sym) in seen:
                    continue
                seen.add((date, slot, g, sym))
                code = sym[-6:]
                ser = bars.get(code) or []
                dates = [x[0] for x in ser]
                i = bisect.bisect_right(dates, date) - 1
                # 放行名单当年没挂矩阵标签（_attach_matrix 只跑了 live/shadow/excluded 三组），
                # 模式维度就没分母了 —— 这里按日志那天的日K补算，补了多少只在剔除计数里报出来。
                verdict = ((it.get('matrix') or {}).get('verdict'))
                if not verdict and i >= 0 and dates[i] == date:
                    fb = _matrix_fallback(ser, i, code, it.get('changePercent'),
                                          it.get('volumeRatio'))
                    if fb:
                        verdict = fb.get('verdict')
                        gap['矩阵标签读时补算(无当日涨停家数)'] += 1
                ev = {'date': date, 'slot': slot, 'group': g, 'symbol': sym, 'code': code,
                      'name': it.get('name'), 'score': it.get('score'),
                      'cycle': lg.get('cycle') or pl.get('cycle') or '未知',
                      'chg': it.get('changePercent'),
                      'verdict': verdict or '未判定',
                      'reasons': '；'.join(it.get('reasons') or []),
                      'rc': None, 'ro': None, 'rb': None}
                if i < 0 or dates[i] != date:
                    gap['当天日K缺失'] += 1
                elif i + 1 >= len(dates):
                    gap['样本未成熟(T+1 还没到)'] += 1
                else:
                    q0 = ser[i][2]
                    o1, c1 = ser[i + 1][1], ser[i + 1][2]
                    if q0 and o1 and c1:
                        ev['rc'] = round((c1 / q0 - 1) * 100, 2)      # 主口径：收盘→收盘
                        ev['ro'] = round((o1 / q0 - 1) * 100, 2)      # 开盘即走
                        p = snap_price.get((date, slot, sym))
                        if p and abs((q0 / p - 1) * 100) <= _limit_pct(code) + 1:
                            ev['rb'] = round((c1 / p - 1) * 100, 2)   # 批次价→次日收盘
                        elif p:
                            gap['批次价口径复权漂移(只进主口径)'] += 1
                    else:
                        gap['日K字段缺失'] += 1
                ev['mode'] = mode_label(pick_mode(ev['verdict']))
                ev['src'] = ksrc if g == '影子放行' else None
                ev['hasSnap'] = snap_price.get((date, slot, sym)) is not None
                events.append(ev)

    # 差集：影子多推的 / 线上独有的（同一批次内比，跨批次比的是选法不是门槛）
    for lg in logs:
        date, slot = lg['rec_date'], lg['slot']
        live = {(x.get('symbol') or '').lower() for x in (lg['payload'].get('live') or [])}
        sh = {(x.get('symbol') or '').lower() for x in (lg['payload'].get('shadow') or [])}
        for sym in sh - live:
            ev = next((e for e in events if e['date'] == date and e['slot'] == slot
                       and e['group'] == '影子Top3' and e['symbol'] == sym), None)
            if ev:
                ev = dict(ev)
                ev['group'] = '影子多推'
                events.append(ev)
        for sym in live - sh:
            ev = next((e for e in events if e['date'] == date and e['slot'] == slot
                       and e['group'] == '线上Top3' and e['symbol'] == sym), None)
            if ev:
                ev = dict(ev)
                ev['group'] = '线上独有'
                events.append(ev)
    return events, gap, kept_src


MIN_EVENTS = 20        # 单组少于 20 次就不配参与判定：放行/被挡各只有几只时，一根票就把方向拧过去


def _verdict(kc, ec, real_days, n_days, min_days):
    """判定闸门。天数按**真放行名单**的交易日算（real_days），不是按影子日志条数算。

    两条都要过：天数 ≥ min_days，且两组各自的事件数 ≥ MIN_EVENTS。
    返回值 (enough, verdict)，`enough=False` 时 verdict 恒为 '样本不足' —— 宁可不给答案。
    """
    enough = bool(kc and ec and real_days >= min_days
                  and kc['n'] >= MIN_EVENTS and ec['n'] >= MIN_EVENTS)
    if not enough:
        return False, '样本不足'
    if kc['avg'] > ec['avg'] and kc['win'] >= ec['win']:
        return True, '支持上线：放行票优于被挡票'
    if kc['avg'] < ec['avg']:
        return True, '不支持：被挡票反而更好（这套周期规则在削收益）'
    return True, '方向不明：均值与胜率不同向'


def summarize(events, gap, n_days, kept_src=None, min_days=None):
    min_days = cfg.SHADOW_EVAL_MIN_DAYS if min_days is None else min_days
    kept_src = dict(kept_src or {})
    base = [e for e in events if e['group'] in GROUPS]      # 差集行不参与分层（会重复计数）
    by = defaultdict(list)
    for e in events:
        by[e['group']].append(e)
    groups = {}
    for g in list(GROUPS) + ['影子多推', '线上独有']:
        v = by.get(g) or []
        groups[g] = {'n': len(v),
                     'close': _stats([x['rc'] for x in v]),
                     'open': _stats([x['ro'] for x in v]),
                     'batch': _stats([x['rb'] for x in v])}

    def _split(key):
        out = defaultdict(list)
        for e in base:
            out[e[key]].append(e)
        res = {}
        for k, v in out.items():
            res[k] = {'n': len(v),
                      'kept': _stats([x['rc'] for x in v if x['group'] == '影子放行']),
                      'excl': _stats([x['rc'] for x in v if x['group'] == '影子挡掉'])}
        return res

    reasons = Counter()
    for e in base:
        if e['group'] == '影子挡掉':
            for r in (e['reasons'].split('；') if e['reasons'] else ['未记理由']):
                reasons[r.split('（')[0][:24]] += 1

    kept, excl = groups.get('影子放行') or {}, groups.get('影子挡掉') or {}
    kc, ec = kept.get('close'), excl.get('close')
    # 判定只认**真放行名单**（payload 里有 kept 的那些天）：反推出来的名单不含
    # "过门槛但没进前 3"的长尾，拿它算出来的放行均值会偏，绝不能凑天数。
    real_days = len({e['date'] for e in base
                     if e['group'] == '影子放行' and e.get('src') == 'payload'})
    enough, verdict = _verdict(kc, ec, real_days, n_days, min_days)
    # 置信区间 + 置信度（2026-11-05：降到 5 天的同时必须附 n 与区间，避免小样本被当结论）。
    # 用「主口径次日收盘收益」的原始序列做两独立样本均值差的正态近似 95% CI。
    kept_rc = [x['rc'] for x in by['影子放行'] if isinstance(x.get('rc'), (int, float))]
    excl_rc = [x['rc'] for x in by['影子挡掉'] if isinstance(x.get('rc'), (int, float))]
    ci_gap = None
    if len(kept_rc) >= 2 and len(excl_rc) >= 2:
        m1, s1, n1 = statistics.mean(kept_rc), statistics.stdev(kept_rc), len(kept_rc)
        m2, s2, n2 = statistics.mean(excl_rc), statistics.stdev(excl_rc), len(excl_rc)
        se = (s1 * s1 / n1 + s2 * s2 / n2) ** 0.5
        diff = m1 - m2
        ci_gap = [round(diff - 1.96 * se, 2), round(diff + 1.96 * se, 2)]
    # 置信度随真实放行天数分级：MIN_DAYS=5 后 5~6 天仅「参考级」，不让它显得很稳
    if real_days >= 10:
        confidence = '高'
    elif real_days >= 7:
        confidence = '中'
    else:
        confidence = '低(参考)'
    if enough and ci_gap is not None:
        verdict = (f"{verdict}（n=放行{len(kept_rc)}/被挡{len(excl_rc)}，"
                   f"差值95%CI [{ci_gap[0]:+.1f},{ci_gap[1]:+.1f}]%）")
    return {
        'available': bool(events),
        'days': n_days,
        'realKeptDays': real_days,
        'keptSource': kept_src,
        'minDays': min_days,
        'enough': enough,
        'needMore': max(0, min_days - real_days),
        'events': len(events),
        'groups': groups,
        'byCycle': _split('cycle'),
        'byMode': _split('mode'),
        'byVerdict': _split('verdict'),
        'reasons': dict(reasons),
        'gap': dict(gap),
        'snapCoverage': sum(1 for e in events if e['hasSnap']),
        'verdict': verdict,
        'confidence': confidence,
        'ciGap': ci_gap,
        'keptN': len(kept_rc),
        'exclN': len(excl_rc),
        'gapAvg': round(kc['avg'] - ec['avg'], 2) if (kc and ec) else None,
        'gapWin': round(kc['win'] - ec['win'], 1) if (kc and ec) else None,
        'generatedAt': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


def evaluate(days=0, slot_last=DEFAULT_SLOT_LAST):
    conn = get_db_conn()
    kconn = sqlite3.connect(cfg.KLINE_HISTORY_DB_PATH, timeout=30)
    try:
        logs = load_logs(conn, days=days, slot_last=slot_last)
        if not logs:
            return {'available': False, 'days': 0, 'events': 0, 'realKeptDays': 0,
                    'keptSource': {}, 'groups': {}, 'byCycle': {}, 'byMode': {},
                    'verdict': '没有影子日志', 'reasons': {}, 'gap': {}, 'enough': False,
                    'needMore': cfg.SHADOW_EVAL_MIN_DAYS, 'minDays': cfg.SHADOW_EVAL_MIN_DAYS}
        events, gap, kept_src = build_events(logs, conn, kconn)
        return summarize(events, gap, len({x['rec_date'] for x in logs}), kept_src)
    finally:
        conn.close()
        kconn.close()


# ========== 每日落库（scheduler 16:25 调这里，也供 /api/shadow-eval 读） ==========

DDL = '''CREATE TABLE IF NOT EXISTS recommend_shadow_eval (
    eval_date   TEXT PRIMARY KEY,
    window_days INTEGER,
    real_days   INTEGER,
    events      INTEGER,
    kept_n      INTEGER, kept_win REAL, kept_avg REAL,
    excl_n      INTEGER, excl_win REAL, excl_avg REAL,
    gap_avg     REAL, gap_win REAL,
    verdict     TEXT,
    payload     TEXT,
    created_at  DATETIME DEFAULT (datetime('now', '+8 hours')))'''


def record_daily(days=0):
    """每天存一个点：窗口随日期漂移，现算的历史数字不可比，所以要留底。"""
    d = evaluate(days=days)
    g = d.get('groups') or {}
    kc = (g.get('影子放行') or {}).get('close') or {}
    ec = (g.get('影子挡掉') or {}).get('close') or {}
    today = datetime.now().strftime('%Y-%m-%d')
    conn = get_db_conn(wait_for_lock=True)
    try:
        conn.execute(DDL)
        conn.execute('DELETE FROM recommend_shadow_eval WHERE eval_date=?', (today,))
        conn.execute('INSERT INTO recommend_shadow_eval (eval_date, window_days, real_days, '
                     'events, kept_n, kept_win, kept_avg, excl_n, excl_win, excl_avg, '
                     'gap_avg, gap_win, verdict, payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (today, d.get('days'), d.get('realKeptDays'), d.get('events'),
                      kc.get('n'), kc.get('win'), kc.get('avg'),
                      ec.get('n'), ec.get('win'), ec.get('avg'),
                      d.get('gapAvg'), d.get('gapWin'), d.get('verdict'),
                      json.dumps(d, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()
    if d.get('available'):
        def _p(s):
            return '无成熟样本' if not s else f'{s.get("win")}%/{s.get("avg")}%（{s.get("n")}次）'
        print(f"[影子对照] {d['days']}个交易日（放行名单来自 payload 的有 {d['realKeptDays']} 天）"
              f"/{d['events']}个事件：放行 {_p(kc)} vs 被挡 {_p(ec)} → {d['verdict']}"
              + ('' if d.get('enough') else f"（真名单还差 {d.get('needMore')} 个交易日）"))
    else:
        print('[影子对照] 还没有影子日志可算（影子机制由智能推荐任务在盘中批次写入）')
    return d


def history(limit=30):
    conn = get_db_conn()
    try:
        conn.execute(DDL)
        rows = [dict(r) for r in conn.execute(
            'SELECT eval_date, window_days, real_days, events, kept_n, kept_win, kept_avg, '
            'excl_n, excl_win, excl_avg, gap_avg, gap_win, verdict '
            'FROM recommend_shadow_eval ORDER BY eval_date DESC LIMIT ?', (limit,)).fetchall()]
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    return rows


# ========== 打印 ==========

def print_report(d, as_json=False):
    if as_json:
        print(json.dumps(d, ensure_ascii=False))
        return
    if not d.get('available'):
        print('recommend_shadow_log 是空的。影子精选由智能推荐任务在 09:45/13:30/14:05/14:45 '
              '四个批次写入，非交易日不跑；先确认推荐任务在正常出批次。')
        return
    print(f'窗口 {d["days"]} 个交易日、{d["events"]} 个事件'
          f'（批次价口径覆盖 {d["snapCoverage"]} 个事件，其余只有收盘口径）')
    print(f'放行名单来源：{d["keptSource"]}　'
          f'其中 payload（当天真把过门槛的整份名单存下来的）{d["realKeptDays"]} 个交易日 —— '
          f'判定只认这些天')
    if d.get('gap'):
        print(f'剔除/补算：{d["gap"]}')
    print(f'\n【主口径：当日收盘 → 次日收盘】（两组同成本基准，这才是门槛的对照）')
    for g in list(GROUPS) + ['影子多推', '线上独有']:
        s = (d['groups'].get(g) or {})
        if not s.get('n'):
            continue
        print(f'  {g:<10} {_fmt(s.get("close"))}')
        if s.get('open'):
            print(f'      ├ 开盘即走 {_brief(s["open"])}')
        if s.get('batch'):
            print(f'      └ 批次价→次日收盘 {_brief(s["batch"])}（有快照价的那批）')
    print('\n【影子挡掉的理由分布】')
    for k, v in sorted(d['reasons'].items(), key=lambda x: -x[1]):
        print(f'  {k:<26} {v}')
    for dim, title in (('byCycle', '按情绪周期'), ('byMode', '按模式')):
        print(f'\n【{title}分层：放行 vs 被挡（主口径）】')
        for k, v in sorted(d[dim].items(), key=lambda x: -(x[1]['n'])):
            print(f'  {str(k)[:12]:<14} 样本{v["n"]:>4}  '
                  f'放行 {_brief(v["kept"])}  被挡 {_brief(v["excl"])}')
    print()
    if d.get('enough'):
        print(f'判定：{d["verdict"]}　放行-被挡 均值差 {d["gapAvg"]:+.2f}%、'
              f'胜率差 {d["gapWin"]:+.1f} 个百分点')
        print(f'　置信度：{d["confidence"]}（放行 n={d["keptN"]} / 被挡 n={d["exclN"]}，'
              f'差值 95%CI {d["ciGap"]}）')
        print('　（第1批 P0-2 的上线判据就是这一行；方向不支持就别上，代码留在原地等下一次窗口）')
    else:
        if d['realKeptDays'] < d['minDays']:
            reason = (f'还需 {d["needMore"]} 个「有真放行名单」的交易日'
                      f'（现 {d["realKeptDays"]}/{d["minDays"]}）')
        else:
            reason = (f'天数已够，但放行/被挡事件数各需 ≥{MIN_EVENTS}，'
                      f'实得 放行{d["keptN"]}/被挡{d["exclN"]}（差值95%CI {d["ciGap"]}、'
                      f'置信度{d["confidence"]}，仅作参考）')
        print(f'样本不足：{reason} —— 只报数，不下结论。')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=0, help='只看最近 N 个交易日（0=全部）')
    ap.add_argument('--all-slots', action='store_true',
                    help='一天 4 个批次全算（默认只取当天最后批次）')
    ap.add_argument('--json', action='store_true', help='输出 JSON')
    ap.add_argument('--save', action='store_true', help='同时落 recommend_shadow_eval 表')
    args = ap.parse_args()
    if args.save:
        d = record_daily(days=args.days)
    else:
        d = evaluate(days=args.days, slot_last=not args.all_slots)
    print_report(d, as_json=args.json)
    return 0 if d.get('available') else 1


if __name__ == '__main__':
    sys.exit(main())
