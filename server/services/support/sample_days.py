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

判"是不是交易日"走的是**仓里那份按交易所公告维护的官方日历**
`services/data/trading_calendar.py`（2026 休市表 + 调休补班 + 跨年守卫），不是我自己另造的。
这一段有两次更正，都留着：
  · **二次更正（11-07，用户反问"节假日不也没 K 线吗"）**：第一版第二条写"周一至周五"，
    工作日不等于交易日——2026 年官方休市表里就有 **19 个日子是周一至周五**
    （1/1、1/2、2/16~2/20、2/23、4/6、5/1、5/4、5/5、6/19、9/25、10/1~10/7），
    靠星期几会把这 19 天全算成样本。当时改成"看本地日K 有没有那一天的 bar"。
  · **三次更正（同日，自查发现）**：仓里**本来就有一张权威交易日历**，我拿日K 又造了一张——
    两套日历迟早分叉，而且日K 只有过去，判不了"今天/明天是不是交易日"（盘中告警正需要这个）。
    实测两套在重叠区间**零分歧**（688 个日K 日期 vs 官方表：双向都 0 处不一致），
    所以主路换成官方表，日K 退成交叉校验 + 官方表没覆盖年份（2024/2025 那 485 个日子）的降级。
    来源用 `calendar_source()` 标出来：`trading_calendar` / `kline_calendar` / `weekday_fallback`，
    最后一档是"跨年又没数据、只能猜星期几"，读数要能看出自己不可全信。

两条判据各堵一个实拍漏口（更正后依然成立，但分工变了）：
- **日历那一半**管住"手工产出的非交易日"：11-07 是周六，`daily_recommendations` 里有 1 行、
  而日K 表里没有 11-07 ⇒ 不算样本（国庆 10-05 那个周一同理，靠星期几就会漏）。
  **11-07 体检收回一处证据**：那 1 行（`sz000004 / 今日新票`）后来查明是
  `scripts/daily_recs_cache_selftest.py` 换库写法失效漏进真库的假批次，不是人手工点的
  （已清理，备份 `server/analysis_out/leak_backup_20261107.json`）。判据一字未动——
  非交易日的行照样排除；但"手工点一次也会留下行"这句目前**没有实拍样本**，
  等真在周末点一次生成时再把证据补回来。
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
    'RULE', 'enabled', 'batch_days', 'kline_days', 'trading_days',
    'is_trading_day', 'calendar_source', 'is_trading_weekday',
    'filter_sample_days', 'count', 'describe',
]

RULE = ('样本日 = 当天是 A 股交易日（以仓里现成的 `services/data/trading_calendar.is_trading_day()` 为准）'
        '且当天有推荐批次（`daily_recommendations` 有行）（修⑧，11-07 三次更正：不另造日历）')

REASON_NO_BATCH = '空日：是交易日，但当天 0 推荐批次（调度没跑/数据不可回补，如 11-06）'
REASON_NOT_TRADING = '非交易日：周末或法定节假日，日K 表里根本没有这一天（手工点出来的批次不算样本）'

# 日K 交叉日历缓存（只作校验/跨年降级；主日历是 services/data/trading_calendar）
_KLINE: Dict = {'day': '', 'set': None}


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
    11-07 第一版把它当主判据，被实测推翻；现在它只是**最末一级降级**（见 `is_trading_day()`）。解析不了则放行。
    """
    try:
        return date.fromisoformat(str(d)[:10]).weekday() < 5
    except Exception:
        return True


def kline_days(refresh: bool = False) -> frozenset:
    """本地日K 表里出现过的日期。**只作交叉校验与跨年降级，不当主日历。**

    11-07 三次更正的由来：第二版把这里当交易日历的主来源，理由是"没开市就没有 bar"——
    话是对的，但仓里**本来就有一份按交易所公告维护的权威日历**
    `services/data/trading_calendar.py`（含 2026 休市表、调休补班、跨年守卫），
    我另造一套等于留两个真相，迟早分叉。而且日K 有个硬缺陷：**它只有过去**，
    判不了"今天/明天是不是交易日"，而盘中告警要的恰恰是这个。
    实测两套日历在重叠区间零分歧（688 个日K 日期 vs 官方表，双向都 0 处不一致），
    所以主路走官方表、日K 退成交叉校验 + 官方表没覆盖年份（2024/2025 共 485 个日子）的降级，
    代价只是多一次只读查询，收益是日历只剩一个权威。
    """
    today = date.today().isoformat()
    if not refresh and _KLINE['set'] is not None and _KLINE['day'] == today:
        return _KLINE['set']
    ds: frozenset = frozenset()
    try:
        import os
        import sqlite3
        from app.config import KLINE_HISTORY_DB_PATH
        pth = os.path.abspath(KLINE_HISTORY_DB_PATH)
        conn = sqlite3.connect(f'file:{pth}?mode=ro', uri=True)
        try:
            if conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                            "AND name='stock_daily'").fetchone():
                cols = [r[1] for r in conn.execute('PRAGMA table_info(stock_daily)')]
                dcol = 'date' if 'date' in cols else ('trade_date' if 'trade_date' in cols else None)
                if dcol:
                    ds = frozenset(str(r[0])[:10] for r in
                                   conn.execute(f'SELECT DISTINCT {dcol} FROM stock_daily'))
        finally:
            conn.close()
    except Exception as e:
        print(f'[样本日] 日K 交叉日历读不到（只影响跨年降级，不影响主路）：'
              f'{type(e).__name__}: {e}')
    _KLINE.update({'day': today, 'set': ds})
    return ds


def calendar_source(d=None) -> str:
    """这天走的是哪一路日历，读数必须能看出来：
    `trading_calendar`（官方表覆盖的年份，主路）／`kline_calendar`（跨年降级，看日K）／
    `weekday_fallback`（跨年又没日K，只能猜星期几——已知会漏 19 个"工作日假期"/年）。
    """
    from services.data.trading_calendar import is_calendar_covered
    ds = str(d or date.today())[:10]
    if is_calendar_covered(ds):
        return 'trading_calendar'
    if ds in kline_days():
        return 'kline_calendar'
    return 'weekday_fallback'


def is_trading_day(d) -> bool:
    """是不是 A 股交易日。主路＝仓里那份按公告维护的官方日历（含调休补班）；
    官方表没维护到的年份才降级：过去看日K，日K 也没就看星期几（并在 `calendar_source()` 里可见）。
    """
    from services.data.trading_calendar import is_calendar_covered, is_trading_day as _official
    ds = str(d)[:10]
    if is_calendar_covered(ds):
        return bool(_official(ds))
    k = kline_days()
    if ds in k:
        return True
    if ds < date.today().isoformat():
        return False                       # 过去的日子：日K 里没有就是没开市
    return date.fromisoformat(ds).weekday() < 5 if len(ds) == 10 else True


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
    """把一批日期分成"算样本"与"不算"。

    batch=None 时自己去库里取批次日；cal=None 时逐日问 `is_trading_day()`（主路是官方日历）。
    自测里注入 cal（一批交易日清单）就能完全离线跑，不必碰真库。
    cal 注入成**空集合**表示"模拟日历不可用"，这时退回星期几近似，
    且 `calendarSource` 会是 `weekday_fallback`——降级要看得见。

    ⚠️ 批次集合读不到（空表/被清理）时**降级为不过滤**并打印警告，返回 `degraded=True`。
    理由是两种错的量级不一样：漏排一个空日只是分母虚高一天，而"批次表被保留策略清过"
    要是当成"所有日子都不是样本日"，样本钟当场归零，每个影子都显示"样本不足"，
    没人会去查是数据没了还是真没到期。这条不是假设——`shadow_eval_selftest` 第一轮
    就是这么报红的（假库没有推荐表 → 真名单天数从 8 掉到 0）。
    """
    uniq = sorted({str(d)[:10] for d in dates if d})
    if batch is None:
        batch = batch_days()
    bset = set(batch)
    injected_cal = cal is not None
    cset = {str(x)[:10] for x in cal} if injected_cal else None
    if cset:
        cal_src = 'injected'
    elif cset is not None:                      # 注入的是空集合＝模拟日历不可用
        cal_src = 'weekday_fallback'
    else:
        cal_src = '+'.join(sorted({calendar_source(d) for d in uniq})) or calendar_source()
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
        if cset is not None:
            in_cal = (d in cset) if cset else is_trading_weekday(d)   # 空集合→星期几近似
        else:
            in_cal = is_trading_day(d)
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

    日历来源是 `weekday_fallback`（官方表跨年没维护、日K 也没数据）时把来源打在行尾——
    会误算一天，这行日志要能让人当场看出这个读数不可全信。
    """
    counted = res.get('counted', res.get('sampleDays', []))
    src = res.get('calendarSource', '')
    tail = '' if src in ('trading_calendar', 'kline_calendar', 'injected', '') \
        else f'  ⚠ 日历来源={src}（跨年没数据，只能猜星期几；工作日期假的手工批次会误算，别当结论）'
    if not res.get('skipped'):
        return f'样本日 {len(counted)} 天（无排除）{tail}'
    ex = '、'.join(f"{s['date']}({s['reason'].split('：')[0]})" for s in res['skipped'])
    return f'样本日 {len(counted)} 天，排除 {len(res["skipped"])} 天：{ex}{tail}'
