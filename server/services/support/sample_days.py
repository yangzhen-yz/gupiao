# -*- coding: utf-8 -*-
"""样本日口径（2026-11-07，修⑧）：影子"按交易日计数"的样本钟到底数哪些日子。

起因是 11-06 那天：调度链被冻 12 小时（03:30~15:52），`daily_recommendations`/
`strategy_snapshot*`/`daily_candidate_pool`/`auction_snapshot` **全天 0 行**，而实时数据
不可回补。凡是"攒够 20 个交易日就能裁决"的影子，只要把这天算进天数，就会出现两件事：
① 窗口里掺一个没有任何决策样本的空日，均值/胜率的分母跟着虚高；
② "第 20 个交易日 ≈ 某月某日"的判据日期整体往后漂，到期了其实样本还没到。

**这条口径只管"观测单位＝推荐批次"的三个钟**：海选层归因 `candidate_pool_shadow`、
出口分叉 `exit_seal_shadow`、影子规则对照 `shadow_eval` 的 `realKeptDays`。
口径一句话说清（**11-07 二次更正后的版本**）：

    **样本日 = 当天在交易日历里（本地日K `stock_daily` 有那一天的 bar）且当天有推荐批次（`daily_recommendations` 有行）。**

第一版写的是"且当天是周一至周五"，被用户一句反问推翻：**工作日不等于交易日**——
10-01（周四）、10-05（周一）这类法定节假日照样不开市，靠星期几会把它们当样本。
日K 表天然只在有行情的那些天写行，它就是这台机器上现成的、零第三方依赖的日历，
实测覆盖 688 个交易日（2024-01-02 ~ 2026-11-06），国庆 10-01~10-07 **一个都没有**。
日K 表读不到时才退回"周一至周五"近似，并把 `calendarSource` 标成 `weekday_fallback`
——降级期间节假日的手工批次会误算一天，所以这个来源必须跟着读数一起出来。

两条判据各堵一个实拍漏口（更正后依然成立，但分工变了）：
- **日历那一半**管住"手工产出的非交易日"：11-07 是周六，手工点一次"生成建议"也给
  `daily_recommendations` 留了 1 行，而日K 表里没有 11-07 ⇒ 不算样本；
- **批次那一半**管住"交易日但系统没出票"：11-06 是周五、日K 有 2460 只的 bar，
  但推荐/快照/候选池全 0 行（调度链被冻 12 小时且不可回补）⇒ 是"空日"，不算样本。
  同一类还有 10-08：日K、指数都正常，那天就是没出推荐——**这种日子该被诚实计入
  "交易日但 0 批次"而不是"非交易日"**，两种原因在返回值里分开写，别混成一个数。

11-07 落地当天在真库副本上的对账（新口径相对"数自己表里的日期"少掉的天）：
海选影子 19→19、出口分叉 2→2、四维闸门 3→3、策略快照 14→14 —— **今天一天都没少算**，
因为空日这些表自己就没写行。所以本模块的价值是把已经踩过的坑关进判据（下一次调度再冻结、
或周末手工点一次批次时不再掺样本），不是修正今天的数字，文档与台账的倒计时也不因此改动。

关掉开关（`SHADOW_SAMPLE_SKIP_EMPTY_DAYS=False`，配置页 `shadow_sample_skip_empty_days`
可热改）时 `count()` 逐字节等于改造前的 `len(set(dates))`，不做任何过滤；
`filter_sample_days()` 在任何取值下都把被排除的日期和原因一起返回，
**跳过是看得见的，不是静默少算**。

**两个钟故意不套这条口径**：`zhaban_shadow`（观测单位＝东财 zbc 真值与腾讯分时自算同时在场）
和盘中龙头特征 `dragon_features`/`intraday_dragon_shadow`（观测单位＝当天有特征行）。
11-06 实测它们分别有 54 只全部可比、1450 行特征，是**真样本**；硬按"有没有推荐批次"去数，
炸板影子的天数会当场从 1 掉到 0（真库副本实测就是这个数）——那是把判据改坏。
炸板影子真正修掉的是另一半：**原来数的是整张表的 DISTINCT 日期，东财停调日（那天只有自算行、
`zbc_em` 全 NULL）也会被计进 20 天的钟**，现在只数有可比行的日子。
两个例外的理由都写进了各自 evaluate() 返回的 `clockRule`，不靠人记。
"""
from datetime import date
from typing import Dict, Iterable, List, Optional, Sequence

__all__ = [
    'RULE', 'enabled', 'batch_days', 'trading_days', 'is_trading_day',
    'is_trading_weekday', 'filter_sample_days', 'count', 'describe',
]

RULE = ('样本日 = 当天在交易日历里（本地日K `stock_daily` 有那一天的 bar）'
        '且当天有推荐批次（`daily_recommendations` 有行）（修⑧，11-07 二次更正）')

REASON_NO_BATCH = '空日：是交易日，但当天 0 推荐批次（调度没跑/数据不可回补，如 11-06）'
REASON_NOT_TRADING = '非交易日：周末或法定节假日，日K 表里根本没有这一天（手工点出来的批次不算样本）'

# 交易日历缓存：{'day': 今天, 'set': {...}, 'source': 'stock_daily'|'weekday_fallback'}
_CAL: Dict = {'day': '', 'set': frozenset(), 'source': ''}


def enabled() -> bool:
    """开关当前是否生效（关掉时本模块只做"如实报告"，不改任何计数）。

    ⚠️ 这里**故意不在顶层 `from app.config import SHADOW_SAMPLE_SKIP_EMPTY_DAYS`**：
    那样星号进来的是一份值拷贝，配置页热改、测试里改 `cfg.XXX` 都看不见（11-05 拆分后
    门面副本静默无效是老毛病，自测第 [2] 组就是这么把它抓出来的）。按海选影子那条
    "先热读覆盖表、读不到再退回 config 现值"的写法来。
    """
    try:
        from services.support import app_settings
        if any(i['key'] == 'shadow_sample_skip_empty_days' for i in app_settings.SPEC):
            v = app_settings.value('shadow_sample_skip_empty_days')
            if v is not None:
                return bool(v)
    except Exception:
        pass
    import app.config as _cfg
    return bool(getattr(_cfg, 'SHADOW_SAMPLE_SKIP_EMPTY_DAYS', True))


def is_trading_weekday(d: str) -> bool:
    """按 ISO 日期判周1~周5。**只是兜底**，不是判据：节假日也是工作日。
    11-07 第一版把它当主判据，被实测推翻（见 `trading_days()` 顶部）。解析不了则放行。
    """
    try:
        return date.fromisoformat(str(d)[:10]).weekday() < 5
    except Exception:
        return True


def trading_days(refresh: bool = False) -> frozenset:
    """本地日K 表里出现过的日期集合 = 交易日历（只读，一天缓存一次）。

    为什么用日K 而不是"周一至周五"（11-07 二次更正，用户的反问是对的）：
    周末判据只管得住周六周日，管不住**法定节假日**——10-01（周四）、10-05（周一）这种
    工作日但不开市的日子，靠星期几会被当成交易日。而日K 表天然只在有行情的那些天写行，
    它就是现成的日历，不需要接第三方交易日历接口（实测 `history_kline.db` 的 `stock_daily`
    有 688 个不同日期、2024-01-02 ~ 2026-11-06，国庆 10-01~10-07 一个都没有）。
    日K 表读不到时退回"周一至周五"并标 `source='weekday_fallback'`——**这是降级，不是等价**：
    降级期间节假日的手工批次会被误算一天，所以读数要带出来。
    """
    today = date.today().isoformat()
    if not refresh and _CAL['day'] == today and _CAL['set']:
        return _CAL['set']
    ds: frozenset = frozenset()
    source = 'weekday_fallback'
    try:
        import os
        import sqlite3
        from app.config import KLINE_HISTORY_DB_PATH
        p = os.path.abspath(KLINE_HISTORY_DB_PATH)
        conn = sqlite3.connect(f'file:{p}?mode=ro', uri=True)
        try:
            row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                               "AND name='stock_daily'").fetchone()
            if row:
                cols = [r[1] for r in conn.execute('PRAGMA table_info(stock_daily)')]
                dcol = 'date' if 'date' in cols else ('trade_date' if 'trade_date' in cols else None)
                if dcol:
                    ds = frozenset(str(r[0])[:10] for r in
                                   conn.execute(f'SELECT DISTINCT {dcol} FROM stock_daily'))
                    if ds:
                        source = 'stock_daily'
        finally:
            conn.close()
    except Exception as e:
        print(f'[样本日] 交易日历读不到（退回"周一至周五"近似，读数会标 weekday_fallback）：'
              f'{type(e).__name__}: {e}')
    _CAL.update({'day': today, 'set': ds, 'source': source})
    return ds


def is_trading_day(d: str) -> bool:
    """这一天是不是交易日。日历可用时**只认日历**；日历不可用（空集合）才退回星期几。"""
    cal = trading_days()
    dd = str(d)[:10]
    if not cal:
        return is_trading_weekday(dd)
    return dd in cal


def batch_days(since: Optional[str] = None, until: Optional[str] = None,
               conn=None) -> List[str]:
    """有推荐批次的日期集合（升序）。只读，不写库；查不到就返回空列表并由调用方决定。"""
    own = conn is None
    if own:
        from db.dal_state import get_db_conn
        conn = get_db_conn()
    try:
        sql = 'SELECT DISTINCT rec_date FROM daily_recommendations'
        cond, args = [], []
        if since:
            cond.append('rec_date>=?'); args.append(since)
        if until:
            cond.append('rec_date<=?'); args.append(until)
        if cond:
            sql += ' WHERE ' + ' AND '.join(cond)
        sql += ' ORDER BY rec_date'
        return [str(r[0])[:10] for r in conn.execute(sql, args).fetchall()]
    except Exception as e:                       # 表还没建（全新库）也要能跑，不能抛
        print(f'[样本日] 读推荐批次日失败（按空集合处理，调用方会如实报 skipped）：{e}')
        return []
    finally:
        if own:
            conn.close()


def filter_sample_days(dates: Iterable[str], batch: Optional[Sequence[str]] = None,
                       cal: Optional[Sequence[str]] = None) -> Dict:
    """把一批日期分成"算样本"与"不算"。batch=None 时自己去库里取批次日；
    cal=None 时用本地日K 交易日历（自测里传假日历，不去碰真库）。

    ⚠️ 批次集合**读不到（空表/被清理）时降级为不过滤**，并打印一条警告、返回值里带
    `degraded=True`。理由是两种错的量级不一样：漏排一个空日只是分母虚高一天，
    而"批次表被保留策略清过"会让样本钟**当场归零**，于是每个影子都显示"样本不足"、
    没人去看是数据没了还是真没到期。宁可少过滤，也不静默清零（第 [7] 组用例反证过）。
    """
    uniq = sorted({str(d)[:10] for d in dates if d})
    if batch is None:
        batch = batch_days()
    bset = set(batch)
    injected_cal = cal is not None
    cset = {str(x)[:10] for x in cal} if injected_cal else set(trading_days())
    if not cset:
        # 日历不可用（真库读不到，或自测故意注入空日历模拟这个状态）→ 退回星期几近似
        cal_src = 'weekday_fallback'
    else:
        cal_src = 'injected' if injected_cal else (_CAL.get('source') or 'weekday_fallback')
    if not enabled():
        return {'counted': uniq, 'skipped': [], 'rule': RULE + '（开关关：不过滤，逐字节等于改造前）',
                'enabled': False, 'degraded': False, 'batchTotal': len(bset),
                'calendarSource': cal_src}
    if not bset:
        print(f'[样本日] daily_recommendations 一个批次日都读不到（空库/被清理？），'
              f'本轮**不过滤**——宁可少排一个空日，也不把 {len(uniq)} 天的样本钟清零')
        return {'counted': uniq, 'skipped': [], 'rule': RULE + '（降级：批次集合为空，不过滤）',
                'enabled': True, 'degraded': True, 'batchTotal': 0, 'calendarSource': cal_src}
    counted, skipped = [], []
    for d in uniq:
        # 日历可用只认日历；不可用时才退回星期几近似（来源已写在 calendarSource）
        in_cal = (d in cset) if cset else is_trading_weekday(d)
        if not in_cal:
            skipped.append({'date': d, 'reason': REASON_NOT_TRADING})
        elif d not in bset:
            skipped.append({'date': d, 'reason': REASON_NO_BATCH})
        else:
            counted.append(d)
    return {'counted': counted, 'skipped': skipped, 'rule': RULE,
            'enabled': True, 'degraded': False, 'batchTotal': len(bset),
            'calendarSource': cal_src}


def count(dates: Iterable[str], batch: Optional[Sequence[str]] = None,
          cal: Optional[Sequence[str]] = None) -> int:
    """给 evaluate() 用的天数。开关关掉时＝`len(set(dates))`（改造前口径）。"""
    return len(filter_sample_days(dates, batch=batch, cal=cal)['counted'])


def describe(res: Dict) -> str:
    """一行日志用的说明，含被排除的日期与原因，便于"今天天数怎么没涨"当场查。

    两种入参都吃：本模块 `filter_sample_days()` 的返回值（键 `counted`），以及各影子
    `evaluate()` 转出来的返回值（键 `sampleDays`）——`print_eval` 手里只有后者，
    只认一种就会在那儿抛 KeyError（11-07 真库副本实测撞过一次）。

    日历来源不是本地日K（`weekday_fallback`）时把来源打在行尾——降级期间节假日的手工批次
    会误算一天，这行日志要能让人当场看出这个读数不可全信。
    """
    counted = res.get('counted', res.get('sampleDays', []))
    src = res.get('calendarSource', '')
    tail = '' if src in ('stock_daily', 'injected', '') \
        else f'  ⚠ 日历来源={src}（靠星期几近似，节假日的手工批次会误算，别当结论）'
    if not res.get('skipped'):
        return f'样本日 {len(counted)} 天（无排除）{tail}'
    ex = '、'.join(f"{s['date']}({s['reason'].split('：')[0]})" for s in res['skipped'])
    return f'样本日 {len(counted)} 天，排除 {len(res["skipped"])} 天：{ex}{tail}'
