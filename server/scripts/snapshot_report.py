"""snapshot_report.py - 用**真实决策**回放策略快照（2026-10-17）

配套 strategy_snapshot 表：那张表每批次记"扫了谁/多少分/走到哪一步/为什么被挡/当天什么情绪"，
这里就是它的读端。以前回测只能用事后重建的近似口径（pool_backtest 拿 20 日涨幅代理海选），
回答不了"我的系统到底值多少钱"；攒够真实样本后，这个脚本给的就是真答案。

三种口径，别混成一个数
----------------------
  · 主口径 **批次价 → 次日收盘**：入场价 = 快照里那一刻的现价（那是人真能成交的价），
    结果 = 下一交易日收盘。批次越早（09:45）越接近"看到推荐就买"。
  · 对照 **收盘 → 收盘**：比值完全在前复权序列内部算，**不受复权漂移影响**，
    用来复核主口径有没有被除权事件污染；也是"14:45 尾盘买入"这一档的天然口径。
  · 附 **批次价 → 次日开盘**：T+1 一开盘就走（接力/打板的退出口径）。

复权漂移为什么要单独挑出来：日K是前复权，快照价是当时的实盘价。若 D 日之后发生过除权，
`前复权收盘 / 快照价` 会明显偏离当日真实涨幅（主板 ±10%、创业/科创 ±20%）。
偏离超过 涨停幅度+1% 的行判定为"两个尺度对不上"，**从主口径剔除**、只进对照口径，
并计数报出来 —— 绝不把除权造成的假收益算进胜率。

其余剔除：当天日K缺失、未来还没走到（样本未成熟）、快照无价格。
默认每天只取**最后一个批次**（当日最终决策），`--all-batches` 才一天算 4 次。

样本不足时它只报"还差多少"，不硬给结论 —— 这个仓库吃过亏：
595 天近似回测里"竞价 T+1 胜率 90%"就是稀疏帧 shift 错位造出来的假数。

复现：python scripts/snapshot_report.py [--limit 60] [--batch 14:45] [--all-batches]
"""
import argparse
import bisect
import os
import sqlite3
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfg                       # noqa: E402
from db.database import get_db_conn            # noqa: E402

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

DIMS = ('stage', 'score带', '情绪', '入口', '时机', '矩阵', '影子判定', '被挡原因')


def _band(score):
    if score is None:
        return '无分数'
    for lo in (90, 80, 70, 60):
        if score >= lo:
            return f'{lo}+'
    return '<60'


def _stats(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    n = len(vals)
    return {'n': n, 'win': sum(1 for v in vals if v > 0) / n * 100,
            'avg': sum(vals) / n, 'med': sorted(vals)[n // 2],
            'worst': min(vals), 'best': max(vals)}


def _fmt(s):
    if not s:
        return '无样本'
    return (f'{s["n"]:>5} 次  胜率{s["win"]:5.1f}%  均值{s["avg"]:+6.2f}%  '
            f'中位{s["med"]:+6.2f}%  最差{s["worst"]:+6.2f}%  最好{s["best"]:+6.2f}%')


def _brief(s):
    if not s:
        return '无样本'
    return f'{s["n"]}次 {s["win"]:.1f}%/{s["avg"]:+.2f}%'


def _limit_pct(code: str) -> float:
    return 20.0 if code.startswith(('30', '68')) else 10.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=60, help='只看最近 N 个交易日')
    ap.add_argument('--batch', default=None, help='只看某个批次（如 14:45）')
    ap.add_argument('--all-batches', action='store_true',
                    help='一天 4 个批次全算（同一只票一天最多被数 4 次，只在比早晚批次时用）')
    args = ap.parse_args()

    conn = get_db_conn()
    kconn = sqlite3.connect(cfg.KLINE_HISTORY_DB_PATH, timeout=30)
    try:
        try:
            days = [r[0] for r in conn.execute(
                'SELECT DISTINCT snap_date FROM strategy_snapshot '
                'ORDER BY snap_date DESC LIMIT ?', (args.limit,)).fetchall()]
        except sqlite3.OperationalError:            # 表还没建 = 快照今天才上线
            days = []
        if not days:
            print('strategy_snapshot 还是空的。')
            print('它由智能推荐任务在 09:45 / 13:30 / 14:05 / 14:45 四个批次写入，'
                  '非交易日不跑 —— 上线后第一个批次是下一个交易日早上 09:45。')
            print('先确认开关：复盘 → 口径与开关 → 策略快照 → 记录策略决策快照。')
            return 1
        print(f'已攒快照 {len(days)} 个交易日：{days[-1]} .. {days[0]}')

        ph = ','.join('?' * len(days))
        rows = [dict(r) for r in conn.execute(
            f'SELECT * FROM strategy_snapshot WHERE snap_date IN ({ph})', days).fetchall()]
        hdr = {(r['snap_date'], r['batch']): dict(r) for r in conn.execute(
            f'SELECT * FROM strategy_snapshot_batch WHERE snap_date IN ({ph})', days).fetchall()}
        if args.batch:
            rows = [r for r in rows if r['batch'] == args.batch]
            label = f'批次 {args.batch}'
        elif args.all_batches:
            label = '全部批次（同一只票一天最多被数 4 次）'
        else:
            last = {}
            for r in rows:
                k = r['snap_date']
                if k not in last or r['batch'] > last[k]:
                    last[k] = r['batch']
            rows = [r for r in rows if r['batch'] == last[r['snap_date']]]
            label = '每天最后批次（当日最终决策）'
        print(f'参与统计的快照行：{len(rows)}（口径：{label}）')
        if not rows:
            print('没有可用行 —— 快照开关可能被关了（复盘·口径与开关·策略快照）')
            return 1

        bars_cache = {}
        groups = defaultdict(list)      # (维度, 值) -> [(主口径, 开盘即走, T+3, 收盘对照)]
        gap = Counter()
        stages = Counter()

        for r in rows:
            code = (r['symbol'] or '')[-6:]
            price = r['price']
            if not price:
                gap['快照无价格'] += 1
                continue
            if code not in bars_cache:
                bars_cache[code] = kconn.execute(
                    'SELECT date, open, close FROM stock_daily WHERE symbol=? ORDER BY date',
                    (code,)).fetchall()
            bars = bars_cache[code]
            dates = [b[0] for b in bars]
            i = bisect.bisect_right(dates, r['snap_date']) - 1
            if i < 0 or dates[i] != r['snap_date']:
                gap['当天日K缺失'] += 1
                continue
            if i + 1 >= len(dates):
                gap['样本未成熟(T+1 还没到)'] += 1
                continue
            q0 = bars[i][2]                              # 快照当日复权收盘
            c1, o1 = bars[i + 1][2], bars[i + 1][1]
            c3 = bars[i + 3][2] if i + 3 < len(bars) else None
            cc1 = (c1 / q0 - 1) * 100                    # 收盘→收盘：复权安全，永远可算
            drift = (q0 / price - 1) * 100               # 快照价与复权序列是否同一尺度
            ok = abs(drift) <= _limit_pct(code) + 1
            if not ok:
                gap['复权漂移(只进对照口径)'] += 1
            main1 = (c1 / price - 1) * 100 if ok else None
            open1 = (o1 / price - 1) * 100 if ok else None
            t3 = (c3 / price - 1) * 100 if (ok and c3) else None
            stage = r['stage'] or '?'
            stages[stage] += 1
            h = hdr.get((r['snap_date'], r['batch'])) or {}
            vals = {'stage': stage,
                    'score带': _band(r['score']),
                    '情绪': h.get('cycle') or '未知',
                    '入口': r['source'] or '未标记',
                    '时机': r['timing'] or '无标签',
                    '矩阵': r['matrix'] or '未判定',
                    '影子判定': r['shadow_note'] or '未评估(不在影子前15只里)',
                    '被挡原因': (r['blocked_by'] or '未被挡').split('；')[0]}
            for d in DIMS:
                groups[(d, vals[d])].append((main1, open1, t3, cc1))

        print(f'阶段分布：{dict(stages)}')
        if gap:
            print(f'剔除：{dict(gap)}（缺口/未成熟/漂移都不进主口径胜率，理由见文件头）')

        for dim in DIMS:
            keys = [k for k in groups if k[0] == dim]
            if not keys:
                continue
            keys.sort(key=lambda k: -len(groups[k]))
            print(f'\n【按{dim}】主口径＝批次价→次日收盘')
            for k in keys:
                v = groups[k]
                print(f'  {str(k[1])[:20]:<22} {_fmt(_stats([x[0] for x in v]))}')
                print(f'        ├ 开盘即走 {_brief(_stats([x[1] for x in v]))}'
                      f'   T+3 {_brief(_stats([x[2] for x in v]))}')
                print(f'        └ 对照(收盘→收盘) {_brief(_stats([x[3] for x in v]))}')
        return 0
    finally:
        conn.close()
        kconn.close()


if __name__ == '__main__':
    sys.exit(main())
