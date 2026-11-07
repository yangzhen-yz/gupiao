"""restate_queue.py - 消费 `kline_restate_queue`：把最后一场除权之后的历史前复权序列补齐

为什么需要这个模块（2026-11-01）
--------------------------------
`eod_snapshot.sync_snapshot()` 每天收盘比对"腾讯昨收 vs 库内最后一根"，不一致就把
(symbol, need_date) 写进 `kline_restate_queue`，注释写着"交给 baostock 逐只重灌"。
可 baostock 10-16 就被拉黑了，**这张表从此只有写入方、没有消费方**：截至 11-01 队列里
126 只、`done` 全是 NULL，等于每只除权票的历史序列都停在旧复权因子上。

后果不是"少一点精度"：库里前复权序列在除权日出现一个假跳空，跨过该日的 T+N 收益全被
污染（10-23 那次多数票的因子只差 0.2%，但 603629 这类大比例送转叠加后，账面收益能
差出几十个百分点——诊断文档里 −34.67% 那条就是这么来的）。

怎么重算（不依赖 baostock）
--------------------------
腾讯 `fqkline` 的前复权序列与本地库**同锚点**（2026-11-01 实测：600000/600519/000001
近 24 根逐值相等，差 0.000），所以它能当参照。做法不是"整段覆盖"（会丢掉本地更长的
历史和成交量口径），而是求一个**线性映射** `新值 = a×库值 + b`：

  · 纯现金分红 → a=1、b=−每股红利；
  · 送转股 → a=1/(1+送转比例)；
  · 两者叠加 → 一般线性式一次覆盖。

用除权日之前最近若干根（两边都有的日期）做最小二乘拟合，把断点之前的整段历史映射过去，
成交量/成交额不动（它们本来就不随复权变）。拟合完必须回验：断点前 10 根残差 ≤ 1 分钱、
断点处首尾相接，否则该票不写库、保持 done=NULL 等下次重试——宁可没修，也不能修歪。

断点由"逐日与腾讯前复权比对"自动定位（不是信 need_date）：need_date 只是**发现**日期，
真实除权日可能更早（那天没跑快照就漏记）。一轮修不干净（历史上还有更早的未重算事件）
就再跑一轮，最多 3 轮。

用法：
  python -m services.data.restate_queue --dry-run     # 只算不写，打印每只票的 a/b 与残差
  python -m services.data.restate_queue                # 写入并把结果记进 done
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import requests

from app.config import KLINE_HISTORY_DB_PATH

_UA = {'User-Agent': 'Mozilla/5.0'}
# 2026-11-01 实测：`web.ifzq.gtimg.cn` 被 WAF 封了整整一个下午（回 501 的 HTML 页面），
# 但同一个接口走 `proxy.finance.qq.com/ifzqgtimg/` 还能正常出数（126 只里最后 2 只就是这么放行的）。
# 两个域名轮着试，不把整条链路押在一个被限流的入口上。
_URLS = ('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get',
         'https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get',
         'https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get')

# 判定"这一根与腾讯前复权不一致"的容差：1.1 分钱。库内是浮点，腾讯保留三位小数，
# 正常同锚点序列的差在 0.001 量级；1 分钱以外才算真的口径不一致。
_TOL = 0.011
_MIN_PAIRS = 5          # 拟合至少要 5 个同日值，少于这个数不做线性映射
_MAX_PASSES = 3         # 同一只票最多修三轮（历史上可能有多个未重算事件）
# 拉前复权的窗口（自然日），由近到远逐个试。400 天覆盖绝大多数；但断点可能比 need_date
# 早得多（603373/000623 的断点在 2025-09-22/23，恰好落在 400 天窗口的最边上，凑不够
# 5 根同日值）——所以拿不够样本就自动加长窗口重取，而不是把票退回队列等人来捡。
_LOOKBACK_WINDOWS = (400, 900, 1500)
_MAX_BARS = 640         # 腾讯单次上限（约 2.5 年交易日）
# 回验时"还能补救"的历史窗口：残差全部落在这个天数以前，就不再每晚报错重试，
# 记 partial 落 done（近端不一致仍然是失败，必须修干净）。
_HISTORY_GRACE_DAYS = 365


def _prefixed(code: str) -> str:
    return ('sh' + code) if code.startswith('6') else ('sz' + code) if code[:2] in ('00', '30') else ''


def fetch_qfq(code: str, start: str, end: str) -> Dict[str, float]:
    """腾讯前复权收盘序列 {date: close}。失败返回空 dict（调用方跳过，不写库）。"""
    pre = _prefixed(code)
    if not pre:
        return {}
    for attempt in range(3):
        for url in _URLS:
            try:
                r = requests.get(url, params={'param': f'{pre},day,{start},{end},{_MAX_BARS},qfq'},
                                 headers=_UA, timeout=15)
                if r.status_code != 200 or not r.text.lstrip().startswith('{'):
                    continue          # 501/WAF 页面是 HTML，直接换下一个入口
                data = (r.json() or {}).get('data', {}).get(pre) or {}
                bars = data.get('qfqday') or data.get('day') or []
                out = {}
                for b in bars:
                    try:
                        out[str(b[0])] = float(b[2])
                    except (TypeError, ValueError, IndexError):
                        continue
                if out:
                    return out
            except Exception:
                continue
        time.sleep(1 + attempt)
    return {}


def _fit(pairs: List[Tuple[float, float]]) -> Tuple[float, float]:
    """最小二乘 y = a·x + b（x=库内, y=腾讯）。x 全相等时退化为纯平移。"""
    n = len(pairs)
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    sxx = sum((p[0] - mx) ** 2 for p in pairs)
    if sxx < 1e-12:
        return 1.0, my - mx
    a = sum((p[0] - mx) * (p[1] - my) for p in pairs) / sxx
    return a, my - a * mx


def _find_break(db: Dict[str, float], ref: Dict[str, float]) -> Optional[str]:
    """最后一个"库内与腾讯不一致"的日期；全一致返回 None。

    从最新一根往前扫，连续一致的那段视为已修好；第一个不一致的日子就是断点。
    要求连续 2 根不一致才认（单根可能是当天没同步/停牌的占位行）。
    """
    common = sorted(set(db) & set(ref), reverse=True)
    miss = 0
    for d in common:
        if abs(db[d] - ref[d]) > _TOL:
            miss += 1
            if miss >= 2:
                break
        else:
            miss = 0
    else:
        return None
    # 从第一根不一致处继续往前找，断点 = 该段最晚的那一天
    bad_dates = [d for d in common if abs(db[d] - ref[d]) > _TOL]
    return max(bad_dates) if bad_dates else None


def _load_db(conn, code: str, start: str) -> Dict[str, float]:
    return {d: float(c) for d, c in conn.execute(
        'SELECT date, close FROM stock_daily WHERE symbol=? AND date>=? AND close IS NOT NULL',
        (code, start)).fetchall()}


def restate_one(conn: sqlite3.Connection, code: str, need_date: str,
                dry_run: bool = False) -> Dict:
    """修复一只票的历史前复权序列。返回 {'ok','rows','a','b','verify','note'}。"""
    end = datetime.now().strftime('%Y-%m-%d')
    ref, db, start, last_err = {}, {}, '', ''

    # 窗口由近到远：断点比 need_date 早得多时（400 天窗口刚好切在断点前两三天），
    # 拟合样本凑不够 5 根，这时自动换更长的窗口重取，而不是直接把票退回队列。
    for days in _LOOKBACK_WINDOWS:
        start = (datetime.strptime(need_date, '%Y-%m-%d') - timedelta(days=days)).strftime('%Y-%m-%d')
        ref = fetch_qfq(code, start, end)
        if not ref:
            last_err = '腾讯前复权拿不到（网络/代码异常或限流），下次重试'
            continue
        db = _load_db(conn, code, start)
        if len(db) < _MIN_PAIRS:
            last_err = f'库内 {start} 之后只有 {len(db)} 根，无法拟合'
            continue
        brk = _find_break(db, ref)
        if brk is None or len([d for d in db if d <= brk and d in ref]) >= _MIN_PAIRS:
            break
        last_err = f'断点 {brk} 之前同日值不足 {_MIN_PAIRS} 根（窗口 {days} 天）'
    else:
        return {'ok': False, 'note': last_err or '无可用窗口'}
    if not ref or not db:
        return {'ok': False, 'note': last_err or '腾讯前复权拿不到，下次重试'}

    total_rows, last = 0, {'a': 1.0, 'b': 0.0, 'verify': None}
    for _ in range(_MAX_PASSES):
        brk = _find_break(db, ref)
        if brk is None:
            break
        # 断点是"最后一根还在旧因子上的K线"，它自己也要一起映射 —— 闭区间，
        # 写成 < brk 会把断点那天留在旧基准上，永远修不干净（11-01 首次预演就踩了）。
        pairs = [(db[d], ref[d]) for d in sorted(db) if d <= brk and d in ref][-_MIN_PAIRS * 2:]
        if len(pairs) < _MIN_PAIRS:
            return {'ok': False, 'note': f'断点 {brk} 之前同日值不足 {_MIN_PAIRS} 根'}
        a, b = _fit(pairs)
        resid = max(abs(a * x + b - y) for x, y in pairs)
        # 合理性闸门：系数离谱说明这不是除权，是脏数据/代码错位，别写
        med = sorted(p[0] for p in pairs)[len(pairs) // 2]
        if not (0.05 <= a <= 20) or abs(b) > 0.5 * max(med, 0.01):
            return {'ok': False, 'note': f'映射不合理 a={a:.6f} b={b:.4f}（断点 {brk}），不写'}
        if resid > 0.02:
            return {'ok': False, 'note': f'{brk} 前映射残差 {resid:.3f} 过大，不写'}

        if not dry_run:
            cur = conn.execute(
                "UPDATE stock_daily SET open=open*?+?, high=high*?+?, low=low*?+?, close=close*?+? "
                "WHERE symbol=? AND date<=? AND open IS NOT NULL",
                (a, b, a, b, a, b, a, b, code, brk))
            conn.commit()
            total_rows += cur.rowcount
        else:
            total_rows += sum(1 for d in db if d <= brk)

        # 内存序列同步映射，供下一轮定位断点（dry-run 也走，才能算出"要修几轮"）
        for d in list(db):
            if d <= brk:
                db[d] = a * db[d] + b
        last = {'a': a, 'b': b, 'verify': round(resid, 4), 'break': brk}

    # 回验：修完以后断点前后的衔接处必须与腾讯一致。
    # 写库后必须从库**重新读**——拿内存映射值自证等于自己给自己判卷。
    if not dry_run and total_rows:
        db = _load_db(conn, code, start)
    common = [d for d in sorted(db) if d in ref]
    bad = [d for d in common if abs(db[d] - ref[d]) > _TOL]
    if bad:
        # 连续两根以上不一致 = 还有更早的除权没修干净，不能标记完成；
        # 孤立单根（2025 年那种只有一天对不上）是历史落库的脏价，与复权无关，
        # 修不了也不该为它把整只票挡在队列里——记为 isolated 放着，等人工核。
        pos = {d: i for i, d in enumerate(common)}
        adjacent = any(pos[bad[i + 1]] == pos[bad[i]] + 1 for i in range(len(bad) - 1))
        if adjacent:
            # 但"更早"也可能早就超出任何对账需要的范围：000623 修完近两年全一致，
            # 只剩 2024-03~07 的 39 根差 1~8 分钱（同一笔分红两边取整不同，线性映射
            # 解释不了）。这种两年多以前的残差继续重试只是每晚白打一次腾讯接口，
            # 记为 partial 落 done，把"没修干净"写清楚，人工要查也有据可查。
            oldest = datetime.strptime(max(bad), '%Y-%m-%d')
            if (datetime.now() - oldest).days > _HISTORY_GRACE_DAYS:
                return {'ok': True, 'rows': total_rows, 'partial': True,
                        'note': f'近端已一致；历史 {min(bad)}~{max(bad)} 共 {len(bad)} 根仍差'
                                f'（超出 {_HISTORY_GRACE_DAYS} 天对账窗口，线性映射无法解释，待人工核）',
                        **last}
            return {'ok': False, 'note': f'仍有 {len(bad)} 根（含连续段）与腾讯不一致（最晚 {max(bad)}）', **last}
        return {'ok': True, 'rows': total_rows, 'isolated': len(bad),
                'note': f'孤立脏价 {len(bad)} 根（最晚 {max(bad)}），与复权无关，已记入 done', **last}
    if not total_rows:
        # 回验只覆盖本次加载的窗口（断点找不到就停在第一个窗口）。文案里带上窗口起点，
        # 免得"完全一致"被读成"整段历史都一致"——2024 年那截更早的口径差异不在范围内。
        return {'ok': True, 'rows': 0, 'a': 1.0, 'b': 0.0, 'verify': 0.0, 'window': start,
                'note': f'{start} 起 {len(common)} 根与腾讯前复权完全一致，无需调整'}
    return {'ok': True, 'rows': total_rows, **last}


def run(limit: int = 0, dry_run: bool = False, retry_done: bool = False) -> Dict:
    """消费队列（默认只处理 done 为 NULL 的行）。"""
    conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=60)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=30000')
    stats = {'ok': 0, 'skip': 0, 'fail': 0, 'rows': 0, 'noop': 0, 'aborted': '', 'details': []}
    consec_blocked = 0
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS kline_restate_queue (
            symbol TEXT NOT NULL, need_date TEXT NOT NULL, reason TEXT,
            created_at TEXT, done TEXT, PRIMARY KEY (symbol, need_date))''')
        where = '' if retry_done else 'WHERE done IS NULL'
        rows = conn.execute(
            f'SELECT symbol, need_date FROM kline_restate_queue {where} ORDER BY need_date').fetchall()
        if limit:
            rows = rows[:limit]
        for code, need_date in rows:
            try:
                r = restate_one(conn, code, need_date, dry_run=dry_run)
            except Exception as e:
                r = {'ok': False, 'note': f'{type(e).__name__}: {e}'}
            if r.get('ok') and not r.get('rows'):
                stats['noop'] += 1
            if r.get('ok'):
                consec_blocked = 0
                stats['ok'] += 1
                stats['rows'] += r.get('rows') or 0
                if not dry_run:
                    done = (f"{datetime.now():%Y-%m-%d %H:%M:%S} rows={r.get('rows')} "
                            f"a={r.get('a'):.6f} b={r.get('b'):.4f} verify={r.get('verify')} "
                            f"break={r.get('break')}"
                            + (f" note={r['note']}" if r.get('note') else ''))
                    conn.execute('UPDATE kline_restate_queue SET done=? WHERE symbol=? AND need_date=?',
                                 (done, code, need_date))
                    conn.commit()
            elif r.get('note', '').startswith('库内') or '拿不到' in r.get('note', ''):
                stats['skip'] += 1
            else:
                stats['fail'] += 1
            stats['details'].append({'symbol': code, 'need_date': need_date, **r})
            print(f"[除权重算] {code} {need_date}: {'OK' if r.get('ok') else 'SKIP/FAIL'} "
                  f"rows={r.get('rows')} {r.get('note') or ''}")
            # 腾讯对高频 fqkline 会回一个 501 的 WAF 页面（正文是 HTML，不是 JSON）。
            # 连着几只都拿不到就不是"这只票有问题"，是整条通路被限流了——继续跑只会
            # 把剩下的票全部标成 skip，还会把限流期拖长。停下来，剩下的下次再来。
            if not r.get('ok') and '拿不到' in (r.get('note') or ''):
                consec_blocked += 1
                if consec_blocked >= 3:
                    stats['aborted'] = f'连续 {consec_blocked} 只拿不到腾讯前复权（限流），剩余下次再跑'
                    print(f'[除权重算] [警告] {stats["aborted"]}')
                    break
            else:
                consec_blocked = 0
            time.sleep(0.3)      # 单线程节流：126 只连着打是最容易被 WAF 盯上的形态
    finally:
        conn.close()
    return stats


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='只算不写')
    ap.add_argument('--limit', type=int, default=0, help='只处理前 N 只（调试）')
    ap.add_argument('--retry-done', action='store_true', help='连 done 不为空的也重算一遍')
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    t0 = time.time()
    st = run(limit=a.limit, dry_run=a.dry_run, retry_done=a.retry_done)
    print(json.dumps({k: v for k, v in st.items() if k != 'details'}, ensure_ascii=False))
    print(f"完成：成功 {st['ok']}（其中无需调整 {st['noop']}）、跳过 {st['skip']}、失败 {st['fail']}，"
          f"共改 {st['rows']} 根K线，耗时 {int(time.time() - t0)}s")
    return 0


if __name__ == '__main__':
    sys.exit(main())
