"""sentiment.py - 盘中短线情绪温度计（2026-10-14 新增）

为什么要有这个模块
------------------
系统本来已经有五维情绪周期（涨停家数/炸板率/连板高度/昨日涨停溢价/涨跌家数比），
但它只在 15:10 收评里算一次（services/review.py `_calc_sentiment_cycle`），
消费方只有持仓页的环境横幅。选股链路从头到尾只用得到一个市场变量——
上证指数当日涨跌幅。于是冰点日和高压日，同一只票用同一套阈值算出同一个分、
同样进 Top3，"先看情绪周期、再做板块、最后选个股"这条顺序在代码里是反的。

本模块做三件事：
  1. 把同一套情绪口径搬到盘中（3~5 分钟一次，复用 review.py 已有的取数与打分函数）；
  2. 从涨停池自带字段挖出以前没人用的三个维度：hybk（行业板块）→ 当日主线与梯队，
     fund/ltsz（封单占流通市值比）→ 封板强度，zbc（炸板次数）→ 烂板率；
  3. 把周期翻译成"门槛/否决"而不是"扣分"——不改 13 因子权重，
     既不污染 15:35 的 auto_optimizer，也让"退潮不接力"这类纪律不会被高涨幅平均掉。

硬约束（务必保持）
----------------
  1. 东财 push2ex 池接口是"当日盘面"接口，被高频轮询有 429/封 IP 风险，
     因此一次刷新只发 4 类请求（涨停池/炸板池/跌停池/宽度）+ 昨日涨停溢价的批量行情，
     并由 TTL + singleflight 合并并发调用；
  2. 任何一路取数失败都**绝不降级成"冰点"**——情绪源失效时退回昨日收评口径并打 stale，
     消费方对 stale 只做提示、不做硬否决（把接口故障当成市场恐慌而清仓是更贵的错误）；
  3. 本模块不写 strategy_factor_weights，也不改 daily_recommendations 的替换语义。
"""
import asyncio
import json
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from app.config import (SENTIMENT_ENABLED, SENTIMENT_REFRESH_TTL_SEC,
                        SENTIMENT_SNAPSHOT_SAVE_SEC, SENTIMENT_ZT_POOL_PAGESIZE,
                        SENTIMENT_PREMIUM_MAX_CODES, SENTIMENT_MIN_SCORE,
                        SENTIMENT_VETO_MIN_WEAK_SIGNALS, SENTIMENT_VETO_AFTER_MIN,
                        SENTIMENT_LADDER_COLLAPSE_RATE, SENTIMENT_LEADER_FAIL_CHANGE,
                        SENTIMENT_LADDER_MIN_BASE, CYCLE_RULES,
                        SENTIMENT_SNAPSHOT_FALLBACK_MAX_AGE_SEC)
from db.database import get_db_conn
from services.ops.review import (_em_zt_pool_get, _em_zb_pool_get, _em_batch_change_percent,
                             _fetch_market_breadth, _calc_lianban_ladder,
                             _calc_sentiment_cycle)
from services.data.trading_calendar import is_trading_day, get_session_phase
from services.data.limit_pool import save_pools

# 进程内缓存：情绪是"全市场一个答案"，所有用户/所有 Tab 共用一份即可
_CACHE: Dict = {'ts': 0.0, 'data': None}
_REFRESH_FLAG = threading.Lock()      # singleflight：同一时刻只允许一条线程打接口
_LAST_SAVE_TS = 0.0
# 只有交易日盘中才值得打接口取情绪（2026-10-17）：与调度器预热窗口一致
_COLLECT_PHASES = {'auction', 'morning', 'lunch', 'mid', 'tail'}
_EM_GUARD = None


def _em_guard():
    """东财共享熔断器（懒加载避免循环导入）"""
    global _EM_GUARD
    if _EM_GUARD is None:
        from services.data import eastmoney_guard
        _EM_GUARD = eastmoney_guard
    return _EM_GUARD
# 昨日涨停池按"今天"缓存（昨天的名单收盘后就是定版，不必每次刷新重拉）
_YEST_MEMO: Dict = {'date': '', 'pool': []}
# 昨日涨停溢价的批量行情偶尔整批失败（2026-10-14 实测 push2delay 晚间 RemoteDisconnected）。
# 它是"67 只票的均值"，日内变化很慢，所以失败时沿用当天早些时候的值，
# 而不是让周期从"退潮"漂到"分歧"——接口故障不该改变市场对当下的判断。
_PREMIUM_MEMO: Dict = {'date': '', 'quotes': {}}
# 三池明细落库节流（2026-10-22）：情绪刷新最快 20 秒，逐票列表没必要跟着刷。
# 收盘定版由 16:10 的收评任务再写一次并覆盖，那份才是因子回测要用的。
_POOL_SAVE_TS = 0.0
_POOL_SAVE_SEC = 180


def _ensure_table():
    """情绪快照表：每天约 48~96 行，供后续影子回测与"周期→次日表现"对照"""
    try:
        conn = get_db_conn()
        conn.execute('''CREATE TABLE IF NOT EXISTS market_sentiment_snapshots (
            sent_date TEXT NOT NULL,
            ts        TEXT NOT NULL,
            cycle     TEXT,
            score     INTEGER,
            stale     INTEGER DEFAULT 0,
            payload   TEXT,
            PRIMARY KEY (sent_date, ts)
        )''')
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'[情绪] 初始化快照表失败: {e}')


def _prev_trading_date(today: datetime) -> Optional[datetime]:
    d = today - timedelta(days=1)
    for _ in range(12):
        try:
            if is_trading_day(d):
                return d
        except Exception:
            return None
        d -= timedelta(days=1)
    return None


def _yesterday_zt_pool(today: datetime) -> List[Dict]:
    """昨日涨停池（定版名单，按今天日期缓存）：晋级率/梯队崩塌/龙头失势都以它为母数"""
    key = today.strftime('%Y%m%d')
    if _YEST_MEMO.get('date') == key:
        return _YEST_MEMO.get('pool') or []
    prev = _prev_trading_date(today)
    pool = []
    if prev:
        try:
            pool = _em_zt_pool_get(prev.strftime('%Y%m%d'),
                                   pagesize=SENTIMENT_ZT_POOL_PAGESIZE, retries=2) or []
        except Exception as e:
            print(f'[情绪] 昨日涨停池拉取失败: {e}')
    _YEST_MEMO.update({'date': key, 'pool': pool})
    return pool


def _lead_sectors(zt_pool: List[Dict]) -> List[Dict]:
    """当日主线：涨停池自带 hybk（行业板块）字段，以前收评只解析了 6 个字段、把它丢了。
    有了它，"今天的主流题材是谁、梯队完不完整"不必再依赖那张手维护的静态概念表。

    每个板块输出：涨停数、最高连板、封单额合计、梯队分布（几板各几只）、代表股。
    """
    buckets: Dict[str, Dict] = {}
    for item in zt_pool or []:
        bk = (item.get('hybk') or '').strip() or '其他'
        lbc = int(item.get('lbc') or 1)
        b = buckets.setdefault(bk, {'sector': bk, 'limitUp': 0, 'maxLianban': 0,
                                    'fundSum': 0, 'ladder': {}, 'leaders': []})
        b['limitUp'] += 1
        b['maxLianban'] = max(b['maxLianban'], lbc)
        b['fundSum'] += int(item.get('fund') or 0)
        b['ladder'][str(lbc)] = b['ladder'].get(str(lbc), 0) + 1
        b['leaders'].append({'name': item.get('n', ''), 'code': item.get('c', ''),
                             'lianban': lbc, 'sealFund': int(item.get('fund') or 0)})
    out = []
    for b in buckets.values():
        b['leaders'].sort(key=lambda x: (-x['lianban'], -x['sealFund']))
        b['leaders'] = b['leaders'][:3]
        # 梯队强度：有 1 只高标 + 有跟风补位才叫主线；单只独苗是孤军，第二天多半掉队
        tiers = sum(1 for k, v in b['ladder'].items() if int(k) >= 2 and v > 0)
        b['tierCount'] = tiers
        out.append(b)
    out.sort(key=lambda x: (-(x['maxLianban'] >= 3), -x['limitUp'], -x['maxLianban']))
    return out


def _seal_quality(zt_pool: List[Dict]) -> Dict:
    """封板质量：fund/ltsz=封单占流通市值比（越高越硬），zbc=当日炸板次数（>0 即烂板）"""
    if not zt_pool:
        return {'sealRatioMedian': None, 'rottenRate': None, 'sample': 0}
    ratios, rotten = [], 0
    for item in zt_pool:
        ltsz = float(item.get('ltsz') or 0)
        fund = float(item.get('fund') or 0)
        if ltsz > 0 and fund > 0:
            ratios.append(fund / ltsz * 100)
        if int(item.get('zbc') or 0) > 0:
            rotten += 1
    ratios.sort()
    return {
        'sealRatioMedian': round(ratios[len(ratios) // 2], 2) if ratios else None,
        'rottenRate': round(rotten / len(zt_pool) * 100, 1),
        'sample': len(zt_pool),
    }


def _relay_stats(today_zt_pool: List[Dict], yest_pool: List[Dict],
                 quotes: Dict[str, float]) -> Dict:
    """接力结构：昨日涨停股今日还在涨停的比例（晋级率）、高位股存活率、龙头失势。

    这两个数是"退潮"最可靠的先行指标：炸板率飙升 + 晋级率腰斩同时出现才算退潮，
    单看炸板率会把"分歧日"误判成退潮（分歧日是换手出清，次日往往修复）。
    """
    y_codes = [str(i.get('c', '')) for i in yest_pool if i.get('c')]
    today_codes = {str(i.get('c', '')) for i in today_zt_pool or []}
    stats = {'promotionRate': None, 'highRelayRate': None, 'failedLeaders': [],
             'yesterdayPremium': None}
    if not y_codes:
        return stats
    promoted = [c for c in y_codes if c in today_codes]
    stats['promotionRate'] = round(len(promoted) / len(y_codes) * 100, 1)

    # 1 进 2（首板晋级）：短线接力最主流的一档
    first = [str(i.get('c', '')) for i in yest_pool if int(i.get('lbc') or 0) == 1]
    if first:
        stats['firstToSecondRate'] = round(
            sum(1 for c in first if c in today_codes) / len(first) * 100, 1)

    # 高位股（昨日 3 板及以上）存活率：母数太小就不下结论，避免单日噪声
    high = [str(i.get('c', '')) for i in yest_pool if int(i.get('lbc') or 0) >= 3]
    if len(high) >= SENTIMENT_LADDER_MIN_BASE:
        alive = sum(1 for c in high if c in today_codes)
        stats['highRelayRate'] = round(alive / len(high) * 100, 1)
        stats['highBase'] = len(high)

    # 龙头失势：昨日高位股今天跌到阈值以下（含断板后跳水）
    lbc_of = {str(i.get('c', '')): int(i.get('lbc') or 0) for i in yest_pool}
    for c, chg in (quotes or {}).items():
        if lbc_of.get(c, 0) >= 3 and chg <= SENTIMENT_LEADER_FAIL_CHANGE:
            name = next((i.get('n', '') for i in yest_pool if str(i.get('c', '')) == c), c)
            stats['failedLeaders'].append({'name': name, 'code': c,
                                           'lianban': lbc_of[c], 'change': chg})
    if quotes:
        vals = [v for v in quotes.values() if isinstance(v, (int, float))]
        if vals:
            stats['yesterdayPremium'] = round(sum(vals) / len(vals), 2)
    return stats


def _sync_collect() -> Dict:
    """同步取数（调用方用 asyncio.to_thread 包）：4 类东财请求 + 昨日溢价批量行情

    2026-10-17 两条提速（根因：东财 clist 被 IP 封锁时，6 个请求各带 2 次重试
    × 6s 超时串行硬打，冷路径实测 ~10s，把默认首页行动台拖到 9.2s）：
      1. **非交易时段短路**：只在交易日盘中（竞价~尾盘）才打接口。周末/节假日/
         盘后/盘前打开的页面，看到的情绪本就该是"上一交易日收评定版值"——
         与持仓页 _load_today_sentiment、行动台 headline 的既有语义一致。
      2. **共享熔断**：东财连续失败 3 次即冷却 5 分钟（eastmoney_guard），
         期间直接走收评兜底，不再白等重试。
    """
    now = datetime.now()
    # 1) 非交易时段：不碰东财，直接给"上一交易日"口径
    try:
        phase = get_session_phase(now).get('phase', '')
    except Exception:
        phase = ''
    if phase not in _COLLECT_PHASES:
        # 非交易时段不是"数据降级"，而是"收盘定版"：优先当日收评；
        # 收评还没生成（15:00~15:10）或当天失败时，用当日最近盘中快照兜住，
        # 绝不把上一个交易日的数字直接当成今天的。前端据此显示"收盘定版"。
        fb = _final_fallback(now)
        if fb:
            return fb
        return _empty_snapshot(f'非交易时段({phase or "未知"})')

    # 2) 东财熔断中：不硬打。优先沿用当日最近盘中快照，其次才退收评——
    #    直接退收评会把昨天的连板高度/炸板率当成今天的（2026-10-23 6板显示5板即此因）。
    if _em_guard().skip('clist'):
        fb = _fallback_snapshot(now)
        if fb:
            return fb
        return _empty_snapshot('东财冷却中')

    today_str = now.strftime('%Y%m%d')
    yest_pool = _yesterday_zt_pool(now)

    zt_pool = _em_zt_pool_get(today_str, pagesize=SENTIMENT_ZT_POOL_PAGESIZE,
                              retries=2, timeout=6.0) or []
    zb_pool = _em_zb_pool_get(today_str, retries=2, timeout=6.0) if zt_pool else []
    dt_pool = _em_zt_pool_get(today_str, pool_type='dt',
                              pagesize=SENTIMENT_ZT_POOL_PAGESIZE,
                              retries=2, timeout=6.0) or []
    breadth = _fetch_market_breadth()
    ladder = _calc_lianban_ladder(zt_pool)

    # 明细落库（2026-10-22）：聚合快照之外再存逐票名单，供首板/弱转强因子用。
    # 挂在熔断计数之前：池子拿到了就先存，情绪分算成什么样不影响这份原始数据。
    global _POOL_SAVE_TS
    if time.time() - _POOL_SAVE_TS >= _POOL_SAVE_SEC and (zt_pool or zb_pool or dt_pool):
        _POOL_SAVE_TS = time.time()
        save_pools(now.strftime('%Y-%m-%d'), zt_pool, zb_pool, dt_pool)

    # 熔断计数：涨停池空（重试后仍拿不到）记一次失败；正常拿到即复位。
    # 盘中全市场零涨停基本不存在，误判的代价也只是 5 分钟内走收评兜底，可自愈。
    if zt_pool:
        _em_guard().ok('clist')
    else:
        _em_guard().fail('clist')
        # 涨停池重试后仍为空：不要拿 0 涨停算出一个假的"冰点"周期。
        # 先退当日最近盘中快照，再退收评；两者都带 stale，只提示不拦。
        fb = _fallback_snapshot(now)
        if fb:
            return fb

    # 昨日涨停股今日表现（赚钱效应）：母数取昨日涨停池，限 200 只控制请求批数
    codes = [str(i.get('c', '')) for i in yest_pool if i.get('c')][:SENTIMENT_PREMIUM_MAX_CODES]
    quotes = _em_batch_change_percent(codes) if codes else {}
    gaps: List[str] = []
    _today_key = now.strftime('%Y%m%d')
    if quotes:
        _PREMIUM_MEMO.update({'date': _today_key, 'quotes': quotes})
    elif _PREMIUM_MEMO.get('date') == _today_key and _PREMIUM_MEMO.get('quotes'):
        quotes = _PREMIUM_MEMO['quotes']
        gaps.append('昨日涨停溢价沿用今日早前值（批量行情此刻失败）')
    else:
        gaps.append('昨日涨停溢价取不到（赚钱效应/龙头失势这一维缺数据）')
    if breadth.get('riseCount') is None:
        gaps.append('涨跌家数取不到（板块汇总此刻失败）')
    if not zt_pool:
        gaps.append('涨停池为空（此刻无数据或接口失败，情绪分不可用）')

    relay = _relay_stats(zt_pool, yest_pool, quotes)
    premium = relay.get('yesterdayPremium')
    # _calc_sentiment_cycle 的溢价入参口径是"昨日涨停今日平均涨跌幅"，与上面一致
    sent = _calc_sentiment_cycle(limit_up_count=len(zt_pool),
                                 limit_down_count=len(dt_pool),
                                 breadth=breadth, ladder=ladder,
                                 zhaban_count=(len(zb_pool) if zb_pool or zt_pool else None),
                                 yesterday_premium=premium)

    weak = list(sent.get('weakSignals') or [])
    if relay.get('highRelayRate') is not None and \
            relay['highRelayRate'] <= SENTIMENT_LADDER_COLLAPSE_RATE * 100:
        weak.append(f'昨日{relay.get("highBase")}只3板以上仅{int(relay["highRelayRate"] * relay["highBase"] / 100)}只晋级（梯队崩塌）')
    if relay.get('promotionRate') is not None and relay['promotionRate'] < 25:
        weak.append(f'涨停晋级率仅{relay["promotionRate"]}%')
    if relay.get('failedLeaders'):
        _fl = relay['failedLeaders'][0]
        weak.append(f'龙头失势：{_fl["name"]}({_fl["lianban"]}板){_fl["change"]:+.1f}%')

    sectors = _lead_sectors(zt_pool)
    seal = _seal_quality(zt_pool)

    return {
        'asOf': now.isoformat(timespec='seconds'),
        'date': now.strftime('%Y-%m-%d'),
        # 涨停池取不到时整个周期不可信：打 stale，让所有消费方退回"只提示、不否决"
        'stale': not zt_pool,
        'source': 'intraday',
        'final': False,                      # 盘中值：每 180 秒还会变
        'dataGaps': gaps,
        'cycle': sent['cycle'],
        'cycleScore': sent['cycleScore'],
        'advice': sent['advice'],
        'detail': sent['detail'],
        'weakSignals': weak,
        'indicators': dict(sent.get('indicators') or {},
                          **{'limitUpCount': len(zt_pool),
                             'limitDownCount': len(dt_pool),
                             'zhabanCount': len(zb_pool or []),
                             'promotionRate': relay.get('promotionRate'),
                             'firstToSecondRate': relay.get('firstToSecondRate'),
                             'highRelayRate': relay.get('highRelayRate'),
                             'sealRatioMedian': seal.get('sealRatioMedian'),
                             'rottenRate': seal.get('rottenRate')}),
        'leadSectors': sectors[:5],
        'failedLeaders': relay.get('failedLeaders') or [],
        'rule': CYCLE_RULES.get(sent['cycle'], {}),
        'minScore': SENTIMENT_MIN_SCORE.get(sent['cycle'], 60),
    }


def _fallback_from_review(final: bool = False) -> Optional[Dict]:
    """退回"最近一次收评"的情绪口径。

    final=False：盘中接口全挂，收评只是旧值兜底 → stale=True，只提示不否决；
    final=True ：非交易时段，收评就是当天的收盘定版 → 不标降级，由前端显示"收盘定版"。
                 仍带 stale=True 让下游照旧不给否决权（定版值不参与盘中拦截）。
    """
    try:
        from db.database import load_daily_market_reviews
        daily = load_daily_market_reviews()
        for rec in daily or []:
            sc = rec.get('sentimentCycle')
            if sc and sc.get('cycle'):
                rec_date = rec.get('date')
                # 定版不写 gap：来源日期由行动台的 finalLabel 承担，
                # 写进 dataGaps 会被前端渲染成"缺数：…"，反把正常定版说成缺数据。
                gap = '' if final else f'实时涨停池此刻不可用，数字取自 {rec_date} 收评'
                # 情绪 JSON 只存了炸板率/连板高度等，涨停/跌停总数在收评主表的
                # limit_up_count / limit_down_count 列；不并进来，定版卡会少最直观的两个数。
                inds = dict(sc.get('indicators') or {})
                if rec.get('limitUpCount') is not None:
                    inds.setdefault('limitUpCount', rec['limitUpCount'])
                if rec.get('limitDownCount') is not None:
                    inds.setdefault('limitDownCount', rec['limitDownCount'])
                return {'asOf': rec_date, 'date': rec_date,
                        'stale': True, 'source': 'review_fallback', 'final': bool(final),
                        'cycle': sc.get('cycle'), 'cycleScore': sc.get('cycleScore'),
                        'advice': sc.get('advice'), 'detail': sc.get('detail') or '',
                        'weakSignals': sc.get('weakSignals') or [],
                        'indicators': inds,
                        'leadSectors': [], 'failedLeaders': [],
                        'dataGaps': [gap] if gap else [],
                        'rule': CYCLE_RULES.get(sc.get('cycle'), {}),
                        'minScore': SENTIMENT_MIN_SCORE.get(sc.get('cycle'), 60)}
    except Exception as e:
        print(f'[情绪] 收评降级失败: {e}')
    return None


def _latest_live_snapshot(now: datetime, final: bool = False) -> Optional[Dict]:
    """实时涨停池不可用时，取当日最近一次成功落库的盘中快照。

    为什么不能直接退收评（2026-10-23 用户实拍）：服务在 13:45 重启后内存缓存为空，
    14:00 东财正处全域冷却，收评兜底把 **10-22 的「最高连板 5板」** 挂在了今天卡片上，
    而今天 13:35 的盘中快照是 6板。当日快照与此刻同一市场，最多旧几十分钟；
    超过 SENTIMENT_SNAPSHOT_FALLBACK_MAX_AGE_SEC 才退收评，并明确标注来源时间。

    final=True：收盘后当天收评还没生成时的定版兜底，来源标记为 snapshot_final。
    """
    try:
        _ensure_table()
        conn = get_db_conn()
        row = conn.execute(
            'SELECT ts, payload FROM market_sentiment_snapshots '
            'WHERE sent_date=? AND stale=0 ORDER BY ts DESC LIMIT 1',
            (now.strftime('%Y-%m-%d'),)).fetchone()
        conn.close()
        if not row:
            return None
        ts_str, payload = row['ts'], row['payload']
        snap_dt = datetime.fromisoformat(ts_str)
        age = (now - snap_dt).total_seconds()
        if age < 0 or age > SENTIMENT_SNAPSHOT_FALLBACK_MAX_AGE_SEC:
            return None
        snap = json.loads(payload)
        if not isinstance(snap, dict):
            return None
        snap = dict(snap)
        snap['stale'] = True                 # 兜底值一律只提示、不否决
        snap['source'] = 'snapshot_final' if final else 'snapshot_fallback'
        snap['final'] = bool(final)          # 盘中兜底是当日盘中值；收盘兜底才是定版
        snap['snapshotAgeSec'] = int(age)
        mins = int(age // 60)
        age_txt = f'（已 {mins} 分钟未更新）' if mins >= 1 else ''
        # 收盘定版兜底不写"实时涨停池不可用"——那会让用户以为数据是坏的；
        # 真正要说的是"今日收评还没生成，先用今天最接近收盘的快照"。
        # 也过滤掉快照里自带的旧 gap，避免同一句话叠两遍。
        gaps = [g for g in (snap.get('dataGaps') or [])
                if '盘中快照' not in str(g)]
        # 定版来源由行动台 finalLabel 说明（"收评未生成 · 沿用 HH:MM 快照"），
        # 不再塞进 dataGaps —— 那里会被渲染成"缺数"，语义正好反了。
        if not final:
            gaps.append(f'实时涨停池此刻不可用，沿用 {snap_dt.strftime("%H:%M")} 盘中快照{age_txt}')
        snap['dataGaps'] = gaps
        snap['rule'] = dict(snap.get('rule') or {})
        snap['rule']['veto_roles'] = ()
        return snap
    except Exception as e:
        print(f'[情绪] 盘中快照兜底失败: {e}')
        return None


def _final_fallback(now: datetime) -> Optional[Dict]:
    """非交易时段的定版口径：今日收评 → 今日最近的盘中快照 → 最近一次收评。

    第二条专治 15:00~15:10 收评尚未生成、或当天收评任务失败：宁可显示今天
    14:38 的快照并注明"收评尚未生成"，也不把昨天的连板高度当成今天的。
    """
    today = now.strftime('%Y-%m-%d')
    rev = _fallback_from_review(final=True)
    if rev and rev.get('date') == today:
        return rev
    snap = _latest_live_snapshot(now, final=True)
    if snap:
        return snap
    return rev


def _fallback_snapshot(now: datetime, final: bool = False) -> Optional[Dict]:
    """实时数据不可用时的兜底顺序：当日最近盘中快照 → 最近一次收评。

    盘中调用（默认 final=False）优先拿当日快照并标降级；非交易时段走
    `_final_fallback()`，不会经过这里。
    """
    return _latest_live_snapshot(now) or _fallback_from_review(final=final)


def _maybe_save(data: Dict):
    global _LAST_SAVE_TS
    # 兜底快照不回写：它的 asOf 是原快照时间，INSERT OR REPLACE 会把同一条 live 行
    # 覆盖成 stale=1，几次冷却之后当天快照就被自己吃光了。
    if not data or data.get('stale'):
        return
    if time.time() - _LAST_SAVE_TS < SENTIMENT_SNAPSHOT_SAVE_SEC:
        return
    _LAST_SAVE_TS = time.time()
    try:
        _ensure_table()
        conn = get_db_conn()
        conn.execute('INSERT OR REPLACE INTO market_sentiment_snapshots '
                     '(sent_date, ts, cycle, score, stale, payload) VALUES (?,?,?,?,?,?)',
                     (data['date'], data['asOf'], data['cycle'],
                      int(data.get('cycleScore') or 0), 1 if data.get('stale') else 0,
                      json.dumps(data, ensure_ascii=False)))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'[情绪] 快照落库失败: {e}')


def _empty_snapshot(reason: str) -> Dict:
    """拿不到情绪时的统一空壳：cycle='未知'，rule 为空，因此对所有消费方都是"只提示不拦" """
    return {'stale': True, 'source': reason, 'final': False,
            'cycle': '未知', 'cycleScore': None,
            'advice': f'情绪数据不可用（{reason}）', 'detail': '', 'weakSignals': [],
            'indicators': {}, 'leadSectors': [], 'failedLeaders': [], 'rule': {},
            'minScore': 60, 'asOf': '', 'date': datetime.now().strftime('%Y-%m-%d')}


def peek_cached(max_age_sec: Optional[int] = None) -> Optional[Dict]:
    """只读缓存，不打接口。

    给"预算只有 2 秒"的热路径（行动台/持仓诊断）当兜底：情绪是分钟级变化很慢的量，
    拿 5 分钟前的真实值远好于拿"未知"——后者会让该降的档没降、该提示的纪律消失。
    超过 max_age_sec 的值不再返回（避免收盘后把上午的周期当现在的周期）。
    """
    data = _CACHE.get('data')
    if not data:
        return None
    age = time.time() - _CACHE.get('ts', 0)
    if max_age_sec is not None and age > max_age_sec:
        return None
    out = dict(data)
    out['cacheAgeSec'] = int(age)
    return out


# 20 秒内不重开一轮取数：没有冷却的话 30 秒轮询 × 多个标签页会排出一串同时打东财的
# 重活 —— 恰是限流最怕的突发。（2026-10-17 纠正：wait_for 超时并不会取消 to_thread
# 里的刷新、也不会释放锁，"超时即释放 singleflight"的旧注释是错的，锁改非阻塞抢。）
_REFRESH_COOLDOWN_SEC = 20
_START_TS = 0.0


async def get_sentiment(force: bool = False) -> Dict:
    """盘中情绪（唯一对外入口）。TTL 内命中缓存；刷新失败退回旧值/收评值，绝不抛异常。"""
    global _START_TS
    if not SENTIMENT_ENABLED:
        return _empty_snapshot('已关闭')

    # 非交易时段：定版口径不参与缓存/singleflight 竞争。
    # 2026-10-23 实拍：行动台并发发起 sentiment 与 mainline，mainline 抢锁失败拿到
    # _empty_snapshot('刷新中')（没有 final 字段），于是把今天的结构判成"不可用"并
    # 缓存 180 秒。定版只读一次收评表，直接给，既快又不受并发态污染。
    now = datetime.now()
    try:
        phase = get_session_phase(now).get('phase', '')
    except Exception:
        phase = ''
    if phase not in _COLLECT_PHASES:
        fb = _final_fallback(now)
        return fb or _empty_snapshot(f'非交易时段({phase or "未知"})')

    cached = _CACHE.get('data')
    if cached and not force and time.time() - _CACHE['ts'] < SENTIMENT_REFRESH_TTL_SEC:
        return cached
    # 有旧值就宁可晚 20 秒也不重开一轮（情绪是分钟级慢变量，重复打接口的代价更高）
    if cached and not force and time.time() - _START_TS < _REFRESH_COOLDOWN_SEC:
        return cached

    # singleflight：已在刷新就返回旧值，不排队打第二拨接口。
    # 非阻塞抢锁（2026-10-17）：本函数在事件循环里被 await，threading.Lock 的
    # timeout 等待会把**整个事件循环冻住**（行动台冷路径实测白堵 2s，且 wait_for
    # 的取消对 to_thread 不生效，2s 预算形同虚设）。抢不到就说明有别人正在刷新，
    # 直接拿旧值/空壳，下轮轮询（30s）自然见到新值。
    if not _REFRESH_FLAG.acquire(blocking=False):
        return cached or _empty_snapshot('刷新中')
    _START_TS = time.time()
    try:
        data = await asyncio.to_thread(_sync_collect)
        _CACHE.update({'ts': time.time(), 'data': data})
        _maybe_save(data)
        return data
    except Exception as e:
        print(f'[情绪] 盘中情绪刷新失败，降级: {e}')
        fb = cached or _fallback_snapshot(datetime.now())
        if fb:
            fb = dict(fb)
            if cached and not cached.get('stale'):
                gaps = list(fb.get('dataGaps') or [])
                gaps.append('刚刚刷新失败，沿用上一次盘中值')
                fb['dataGaps'] = gaps
            fb['stale'] = True
            fb.setdefault('cycle', '未知')
            # 降级值不给否决权限：接口故障不该被当成"市场恐慌"而清空买入区
            fb['rule'] = dict(fb.get('rule') or {})
            fb['rule']['veto_roles'] = ()
            return fb
        return _empty_snapshot('接口失败')
    finally:
        _REFRESH_FLAG.release()


def cycle_gate(sentiment: Dict, now: Optional[datetime] = None) -> Dict:
    """把情绪翻译成可执行规则。

    返回 {'vetoRoles':..., 'allowChase':..., 'allowRelay':..., 'minScore':...,
          'envShift':..., 'vetoEnabled':..., 'reason':...}

    vetoEnabled 的四个前提（缺一个就只提示不拦）：
      1. 情绪源不是 stale（接口故障 ≠ 冰点），且用的是 5 分钟内的值；
      2. 已过 10:00 —— 9:35 涨停家数天然少，不满足这一条会把回暖日误判成冰点而清空买入区；
      3. 至少 SENTIMENT_VETO_MIN_WEAK_SIGNALS 个弱信号同向 —— 单指标假信号太多
         （炸板率高但涨停数也高是"分歧"，不是"退潮"）；
      4. 未走降级/收评兜底口径。
    """
    now = now or datetime.now()
    rule = dict(sentiment.get('rule') or CYCLE_RULES.get(sentiment.get('cycle'), {}))
    weak = sentiment.get('weakSignals') or []
    minutes = now.hour * 60 + now.minute
    age = int(sentiment.get('cacheAgeSec') or 0)
    enabled = (not sentiment.get('stale')) \
        and age <= 300 \
        and minutes >= SENTIMENT_VETO_AFTER_MIN \
        and len(weak) >= SENTIMENT_VETO_MIN_WEAK_SIGNALS
    # allow_chase / allow_relay 只在"确认成立"时才真的拦；未确认时退回 True，
    # 但 advisory* 保留原口径，供文案继续说"今天不该追高"。
    # 不这么做的话，9:35 的假冰点就会一边显示"不拦"、一边把追高票全挪出买入区。
    return {
        'cycle': sentiment.get('cycle'),
        'cycleScore': sentiment.get('cycleScore'),
        'vetoRoles': tuple(rule.get('veto_roles') or ()) if enabled else (),
        'allowChase': bool(rule.get('allow_chase', True)) or not enabled,
        'allowRelay': bool(rule.get('allow_relay', True)) or not enabled,
        'advisoryChase': bool(rule.get('allow_chase', True)),
        'advisoryRelay': bool(rule.get('allow_relay', True)),
        'minScore': sentiment.get('minScore') if enabled else 60,
        'advisoryMinScore': sentiment.get('minScore') or 60,
        'envShift': int(rule.get('env_shift', 0)) if enabled else 0,
        'vetoEnabled': enabled,
        'gatedBy': list(rule.get('veto_roles') or ()) if not enabled else (),
        'reason': rule.get('note') or '',
        'stale': bool(sentiment.get('stale')),
        'cacheAgeSec': age,
    }
