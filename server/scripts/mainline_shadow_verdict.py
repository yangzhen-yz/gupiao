"""mainline_shadow_verdict.py - P0-3 主线加分影子期裁决闸门（只读，可重跑）

守的不变量：**判据没到，主线加分 / 主线徽章 / 评分权重一律不许上线**。
README §9.5 定的判据是「≥15 个交易日后按 factor_ic 口径做分组单调性 / IC 校准」。
本脚本只做**闸门核查**（样本够不够、主线判定有没有值），不代做收益裁决——
判据满足后应按 README 的 factor_ic 口径另行跑分组/IC，再单独拍板。

为什么不顺手把 IC 也算出来：影子快照里 `main[].leaders[]` 全是当日涨停/连板龙头，
「主线归属」与「封板状态」完全共线；拿它们直接算 T+1 收益，量到的是封板票次日开盘
溢价（+2.01%/64.3%），不是主线效应。样本不足时硬凑一个噪声数字，比不裁决更危险。

用法：python server/scripts/mainline_shadow_verdict.py
输出：结构化 verdict（PASS / INSUFFICIENT / FAIL 语义见下）+ 缺口清单，退出码恒 0。
  INSUFFICIENT = 判据未满足，影子期继续，禁止改权重（当前状态）
  READY        = 交易日均够且主线判定有值，可以做 factor_ic 分组/IC 校准（仍需人工拍板）
只读打开 gupiao.db / history_kline.db，不写任何表、不发任何网络请求。
"""
import json
import os
import sqlite3
from collections import defaultdict
from datetime import date

MIN_DAYS = 15  # README §9.5：≥15 个交易日
_SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_DB = os.path.join(_SERVER, 'gupiao.db')
KLINE_DB = os.path.join(_SERVER, 'data', 'history_kline.db')


def _ro(path):
    return sqlite3.connect('file:' + path.replace('\\', '/') + '?mode=ro', uri=True, timeout=30)


def _trading_days():
    """以日K库真实交易日历为准（不用自然日，也不猜周末）。"""
    if not os.path.exists(KLINE_DB):
        return set()
    c = _ro(KLINE_DB)
    try:
        return {r[0] for r in c.execute('SELECT DISTINCT date FROM stock_daily')}
    finally:
        c.close()


def _payload(p):
    try:
        return json.loads(p or '{}') if isinstance(p, str) else (p or {})
    except Exception:
        return {}


def evaluate(by_day, calendar, today, kline_last):
    """纯逻辑闸门核查：给定按日分组的快照 + 交易日历，返回裁决。

    与 read 分离是为了可测——自测喂合成数据即可覆盖 READY / INSUFFICIENT 两个方向，
    不依赖生产库当前攒了多少天（样本攒够后这条自测也不会失效）。
    """
    snap_days = sorted(by_day)
    trade_days = [d for d in snap_days if (not calendar or d in calendar)]
    # 今天只有盘前 1 档，不算完整交易日；用日K最新日期兜底判断
    complete_days = [d for d in trade_days if d <= kline_last and d != today]

    main_days, sub_days, flip_days = [], [], []
    rows_total = sum(len(v) for v in by_day.values())
    for d in complete_days:
        snap = by_day[d]
        last = _payload(snap[-1][1])
        if last.get('main'):
            main_days.append(d)
        if last.get('sub'):
            sub_days.append(d)
        flags = [bool(_payload(p).get('main')) for _ts, p in snap]
        if sum(1 for i in range(1, len(flags)) if flags[i] != flags[i - 1]) > 0:
            flip_days.append(d)

    gaps = []
    if len(complete_days) < MIN_DAYS:
        gaps.append(f'完整交易日 {len(complete_days)} < {MIN_DAYS}（还差 {MIN_DAYS - len(complete_days)} 个）')
    if len(main_days) < MIN_DAYS:
        gaps.append(f'收盘档有主线的交易日 {len(main_days)} < {MIN_DAYS}（还差 {MIN_DAYS - len(main_days)} 个）')
    if len(main_days) < len(complete_days) * 0.8:
        gaps.append(f'主线判定覆盖仅 {len(main_days)}/{len(complete_days)} 个交易日：'
                    f'多数日子因子取值为空，分组/IC 无法计算')
    if flip_days:
        gaps.append(f'{len(flip_days)} 个交易日内主线判定发生盘中翻转（收盘档与盘中档不一致）')

    ready = not gaps
    return {
        'verdict': 'READY' if ready else 'INSUFFICIENT',
        'minDays': MIN_DAYS,
        'snapshotRows': rows_total,
        'snapshotDays': len(snap_days),
        'tradingDaysWithSnapshot': len(trade_days),
        'completeTradingDays': len(complete_days),
        'klineLastDate': kline_last,
        'mainDays': len(main_days),
        'mainDayList': main_days,
        'subDays': len(sub_days),
        'intradayFlipDays': len(flip_days),
        'gaps': gaps,
        'action': ('可以做 factor_ic 分组/IC 校准，但仍需人工拍板；本脚本不代裁决'
                   if ready else '影子期继续：禁止开 P0-3 主线加分 / 主线徽章 / 改评分权重'),
    }


def collect():
    """读生产库（只读）后交给 evaluate。"""
    c = _ro(MAIN_DB)
    try:
        rows = c.execute(
            'SELECT main_date, ts, payload FROM market_mainline_snapshots '
            'ORDER BY main_date, ts').fetchall()
    finally:
        c.close()
    by_day = defaultdict(list)
    for day, ts, payload in rows:
        by_day[day].append((ts, payload))
    calendar = _trading_days()
    return evaluate(by_day, calendar, date.today().isoformat(),
                    max(calendar) if calendar else '')


if __name__ == '__main__':
    print(json.dumps(collect(), ensure_ascii=False, indent=2))
