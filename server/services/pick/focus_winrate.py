"""focus_winrate.py - 「明日重点关注」的历史胜率查找表（2026-10-14 新增）

背景：收评的明日关注原本是三层来源按顺序拼接、直接取前 10 只，既没排序也没分母。
用户反馈"10 只太多，挑预期最高的最多 6 只，最主要的是提示整体胜率"。
要回答"哪只预期更高"，不能拿三个来源的分数硬比 —— 它们量纲不同、分布不同
（实测 Pearson r：趋势 -0.017、策略 +0.055、推荐 -0.058，分数几乎不解释收益）。
所以这里用系统自己的回测数据算出「来源 × 分数段 → T+1 胜率 / 平均收益」查找表，
选票按预期胜率排，页面也能说清"这一篮子历史上大概几成"。

口径（三处统一到"次日绝对收益"，不含大盘 beta）：
  智能推荐 = recommend_review.compute_recommendation_performance 的 r1（次日收盘 / 买入参考价 - 1）
  趋势扫描 = trend_backtest_records.actual_change_1d
  策略预测 = strategy_predict_records.actual_change（bull 方向）

2026-10-14 实测（同库同口径，样本数随每日回测增长）：
  智能推荐 62.4%（n=133，平均 +1.45%）> 策略预测 49.3%（n=2171，+0.38%）> 趋势扫描 42.1%（n=1335，-0.40%）
  只有策略预测的分数档单调有效：60-70 47.2% / 70-80 54.3% / 80+ 59.6%
  趋势扫描各档平均收益全为负 —— 它进"明日关注"就是在拖低整体胜率，按负期望被过滤掉。
"""
from datetime import datetime
from typing import Dict, List, Optional

# 分箱边界与 review 里三层来源的中文名保持一致（source_tags 用的就是这三个词）
_BINS = [('<60', None, 60.0), ('60-70', 60.0, 70.0), ('70-80', 70.0, 80.0), ('80+', 80.0, None)]

SOURCE_REC = '智能推荐'
SOURCE_PREDICT = '策略预测'
SOURCE_TREND = '趋势扫描'

_TABLE: Optional[Dict[str, Dict]] = None
_TABLE_DATE = ''


def _bin_of(score: float) -> str:
    for name, lo, hi in _BINS:
        if (lo is None or score >= lo) and (hi is None or score < hi):
            return name
    return '80+'


def _stats(vals: List[float]) -> Dict:
    """一组 T+1 收益 → 胜率/平均收益/样本数。胜率口径：收益 > 0 的占比。"""
    n = len(vals)
    if not n:
        return {'winRate': None, 'avgRet': None, 'sample': 0}
    return {'winRate': round(sum(1 for x in vals if x > 0) / n * 100, 1),
            'avgRet': round(sum(vals) / n, 2), 'sample': n}


def _collect() -> Dict[str, Dict]:
    """从真实回测表现算查找表：{来源: {'bins': {档: 统计}, 'all': 统计}}"""
    from db.database import get_db_conn

    out: Dict[str, Dict] = {}
    conn = None
    try:
        conn = get_db_conn()
        cur = conn.cursor()

        # 趋势扫描：逐票次日绝对涨跌幅
        cur.execute("SELECT score, actual_change_1d AS r FROM trend_backtest_records "
                    "WHERE verified_1d = 1 AND actual_change_1d IS NOT NULL AND score IS NOT NULL")
        rows = cur.fetchall()
        out[SOURCE_TREND] = _bins_from_rows([(float(r[0]), float(r[1])) for r in rows])

        # 策略预测：只看看涨方向（明日关注只收 bull）
        cur.execute("SELECT score, actual_change AS r FROM strategy_predict_records "
                    "WHERE verified = 1 AND predict_direction = 'bull' "
                    "AND actual_change IS NOT NULL AND score IS NOT NULL")
        rows = cur.fetchall()
        out[SOURCE_PREDICT] = _bins_from_rows([(float(r[0]), float(r[1])) for r in rows])
    except Exception as e:
        print(f'[明日关注胜率表] 回测表读取失败: {e}')
    finally:
        if conn is not None:
            conn.close()

    # 智能推荐：走生产函数（要读本地历史K线库，是唯一有 I/O 开销的一段）
    try:
        from services.pick.recommend_review import _load_all_recommendations, compute_recommendation_performance
        perf = compute_recommendation_performance(_load_all_recommendations())
        out[SOURCE_REC] = _bins_from_rows([(float(p.get('score') or 0), float(p['r1']))
                                           for p in perf if p.get('r1') is not None])
    except Exception as e:
        print(f'[明日关注胜率表] 智能推荐样本计算失败: {e}')

    for src, tbl in list(out.items()):
        tbl['source'] = src
        tbl['all'] = _stats([v for b in tbl['bins'].values() for v in b['_vals']])
        for b in tbl['bins'].values():
            b.pop('_vals', None)
    return out


def _bins_from_rows(rows) -> Dict:
    bins = {name: {'_vals': [], 'winRate': None, 'avgRet': None, 'sample': 0}
            for name, _, _ in _BINS}
    all_vals = []
    for score, ret in rows:
        b = bins[_bin_of(score)]
        b['_vals'].append(ret)
        all_vals.append(ret)
    for name, b in bins.items():
        s = _stats(b['_vals'])
        b.update(s)
    return {'bins': bins, 'all': _stats(all_vals)}


def get_table(force: bool = False) -> Dict[str, Dict]:
    """进程内按天缓存：收评每天生成一次，查找表没必要每次重算"""
    global _TABLE, _TABLE_DATE
    today = datetime.now().strftime('%Y-%m-%d')
    if force or _TABLE is None or _TABLE_DATE != today:
        _TABLE = _collect()
        _TABLE_DATE = today
    return _TABLE


def estimate(source: str, score, min_sample: int = 30) -> Dict:
    """(来源, 分数) → 预期胜率。

    档位样本不足 min_sample 时退回该来源整体：小样本分箱的胜率数字看着精确、其实噪声，
    宁可给一个粗但稳的数，也不要给一个 60% 但其实只有 5 笔的数。
    """
    tbl = get_table().get(source)
    try:
        sc = float(score or 0)
    except (TypeError, ValueError):
        sc = 0.0
    if not tbl:
        return {'winRate': None, 'avgRet': None, 'sample': 0,
                'bin': '', 'basis': '无历史样本', 'source': source}
    b = (tbl['bins'] or {}).get(_bin_of(sc)) or {}
    if b.get('sample', 0) >= min_sample and b.get('winRate') is not None:
        return {'winRate': b['winRate'], 'avgRet': b['avgRet'], 'sample': b['sample'],
                'bin': _bin_of(sc), 'basis': f'同来源 {_bin_of(sc)} 分档', 'source': source}
    a = tbl.get('all') or {}
    return {'winRate': a.get('winRate'), 'avgRet': a.get('avgRet'), 'sample': a.get('sample', 0),
            'bin': _bin_of(sc), 'basis': '样本不足，按该来源整体', 'source': source}


def basket_expect(items: List[Dict]) -> Dict:
    """整篮子的预期：等权平均各票预期胜率，并给出最保守的那只（短板决定体验）"""
    vals = [i.get('expectedWinRate') for i in items if i.get('expectedWinRate') is not None]
    rets = [i.get('expectedAvgRet') for i in items if i.get('expectedAvgRet') is not None]
    if not vals:
        return {'winRate': None, 'avgRet': None, 'worst': None, 'sample': 0}
    return {'winRate': round(sum(vals) / len(vals), 1),
            'avgRet': round(sum(rets) / len(rets), 2) if rets else None,
            'worst': round(min(vals), 1),
            'sample': sum(int(i.get('expectedSample') or 0) for i in items)}
