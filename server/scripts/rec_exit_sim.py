"""rec_exit_sim.py - 历史推荐买卖口径的命令行壳子（实现只在 services/pick/rec_sim.py 一份）

为什么只剩壳子：页面（复盘 → 回测曲线）和命令行必须看同一个数。以前实现写在脚本里，
要把曲线搬上页面就得复制一遍 —— 两边一旦分叉，页面上的数字和日志里的数字对不上，
没人知道该信哪个。所以引擎抽到 `services/pick/rec_sim.py`，这里只负责打印。

口径、剔除规则、费用、以及"为什么尾盘封死的票按买不到处理"都写在引擎的文件头里。

用法：
  python scripts/rec_exit_sim.py                       # 近3个月，六种口径全打
  python scripts/rec_exit_sim.py --entry tailclose      # 只看某一种入口
  python scripts/rec_exit_sim.py --since 2026-08-01 --capital 50000
  python scripts/rec_exit_sim.py --bench                # 顺带算同期等权背景（慢 2.5s）
  python scripts/rec_exit_sim.py --detail               # 逐笔明细写 analysis_out/
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.pick import rec_sim as S          # noqa: E402

ENTRY_ALIAS = {
    'tailclose': 'tailclose', '尾盘': 'tailclose',
    'auction': 'auction', '竞价': 'auction',
    'filled': 'filled', '全成交': 'filled', 'allow-sealed': 'filled',
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--months', type=int, default=3)
    ap.add_argument('--since', default=None)
    ap.add_argument('--capital', type=float, default=100000)
    ap.add_argument('--entry', default=None, help='只看一种入口：tailclose/auction/filled')
    ap.add_argument('--auction-max-gap', type=float, default=5.0,
                    help='竞价买入允许的最大高开幅度%%（推荐原文"高开5%%以上不追"；999=不限制）')
    ap.add_argument('--no-lots', action='store_true',
                    help='不取整到 100 股（等权满仓的敏感性检验，看整股噪声贡献了多少）')
    ap.add_argument('--bench', action='store_true', help='算同期等权背景（多花约 2.5 秒）')
    ap.add_argument('--detail', action='store_true', help='逐笔明细写 analysis_out/')
    args = ap.parse_args()

    want = ENTRY_ALIAS.get(args.entry, args.entry) if args.entry else None
    d = S.simulate(months=args.months, capital=args.capital, since=args.since,
                   auction_max_gap=args.auction_max_gap, whole_lots=not args.no_lots,
                   with_benchmark=args.bench)
    w = d['window']
    print(f"窗口 {w['since']} .. {w['until']}：{w['recDays']} 个推荐日、{w['recCount']} 条推荐"
          f"（全部来自 14:45 收盘前快照）；日K最新到 {w['lastKlineDate']}")
    print(f"本金 {args.capital:,.0f} 元；分仓＝当日可用现金按推荐只数均分、100 股一手"
          f"{'（已放开整股）' if args.no_lots else ''}；"
          f"费用＝佣金万五(单边最低5元)+印花税千1(卖)+过户万0.1；竞价纪律＝高开≤{args.auction_max_gap:g}%才追")
    if d.get('benchmark', {}).get('available'):
        b = d['benchmark']
        print(f"同期背景（事后信息，只看行情冷热）：{b['from']}→{b['to']}，{b['count']} 只等权 {b['avgPct']:+.2f}%")
    print()
    ends = {}
    for c in d['curves']:
        if want and c['entry'] != want:
            continue
        ends[c['key']] = c['end']
        print(f"【{c['label']}】期末 {c['end']:,.0f} 元（{c['retPct']:+.2f}%）  "
              f"成交 {c['trades']} 笔 / 跳过 {c['skipped']} 笔  "
              f"胜率 {c['winRate']}%  单笔均(等权) {c['avgEqual']:+.2f}%  "
              f"单笔均(按投入资金加权) {c['avgWeighted']:+.2f}%  最大回撤 {c['mdd']:.1f}%")
        if c['worst']:
            b, bs = c['best'], c['worst']
            print(f"    最差 {bs['name']} {bs['rec_date']} {bs['ret']:+.2f}%   "
                  f"最好 {b['name']} {b['rec_date']} {b['ret']:+.2f}%")
        if c['skipReasons']:
            print(f"    跳过构成：{c['skipReasons']}")
        if c['notes']:
            print(f"    带口径提示的成交 {len(c['notes'])} 笔（除权越界/一字顺延/竞价买入）")
        if args.detail:
            out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   'analysis_out')
            os.makedirs(out_dir, exist_ok=True)
            path = os.path.join(out_dir, f"rec_exit_{c['key']}.json")
            orders, _sk = S.plan(*_recs_bars(d), c['mode'], entry=c['entry'],
                                 auction_max_gap=args.auction_max_gap)
            res = S.run(orders, args.capital, whole_lots=not args.no_lots)
            json.dump(res['trades'], open(path, 'w', encoding='utf-8'),
                      ensure_ascii=False, indent=1)
            print(f'    明细已写 {path}')
    if len(ends) >= 2:
        base = [k for k in ends if k.endswith('_open') and k.startswith('auction')]
        alt = [k for k in ends if k.endswith('_close') and k.startswith('auction')]
        if base and alt:
            diff = ends[base[0]] - ends[alt[0]]
            print(f"\n按推荐执行这一口径下，开盘卖比尾盘卖多 {diff:+,.0f} 元"
                  f"（{diff / args.capital * 100:+.2f} 个点）")


_CACHE = {}


def _recs_bars(d):
    """--detail 要逐笔明细：load 一次缓存着复用，避免每种口径都重读一遍日K。"""
    if not _CACHE:
        _CACHE['recs'], _CACHE['bars'] = S.load(d['window']['since'])
    return _CACHE['recs'], _CACHE['bars']


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    main()
