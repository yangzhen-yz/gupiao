"""mainline.py - 市场主线/支线板块判定（2026-10-22，计划 crisp-summit-newt）

为什么：`sentiment._lead_sectors` 只按"最高连板→涨停数"排序展示，没有资金持续性、
量能维度，也没有主线/支线/退潮的分层语义。本模块把四路**已在内存/本地**的数据合成
一个可回看的主线判定，行动台 sectors 卡展示、每 300s 落 market_mainline_snapshots
攒影子数据——第一期**不进 recommend、不进 cycle_gate、不推送**（影子优先）。

硬约束（改这个文件前先读）：
  1. **零新增外部请求**：涨停结构复用 sentiment 缓存（TTL 180s），板块资金流走
     `fundflow.peek_sector_flow()`（只读缓存/快照，一个请求都不发），持续性读本地
     日K×行业映射表。绝不在本模块触发新浪全市场扫描（12~18s，9.2s 事故同类）。
  2. 拿不到就 None＋dataGaps，**绝不把缺数折算成 0 分**（sentiment 同一铁律）。
  3. 各维在**数据源内做分位归一**：东财 f62（超大+大单）、新浪 r0（仅超大单）、
     几小时前的旧快照，三套绝对额不可跨源比——分位单调性可比，caliber 落库供回测分组。
  4. `MAINLINE_ENABLED=False` 时一行不执行、零查询，行动台返回体逐字节等于改造前。

排除维度（写死，防止后人"补上"）：政策催化/产业链/业绩支撑——本仓库无结构化数据源
（无新闻 NLP、概念无成员映射、fundamental 是季频），进分等于把主观包装成分数。
"""
import asyncio
import json
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from app.config import (
    MAINLINE_ENABLED, MAINLINE_REFRESH_TTL_SEC, MAINLINE_SNAPSHOT_SAVE_SEC,
    MAINLINE_WEIGHTS, MAINLINE_MIN_SCORE, MAINLINE_SUB_MIN_SCORE, MAINLINE_GAP,
    MAINLINE_JUDGE_AFTER_MIN, MAINLINE_FLOW_STALE_SEC, SECTOR_MIN_MEMBERS,
)

DISCLAIMER = '以上为量化模型对盘面资金/涨停结构/持续性的描述，仅供参考，不构成投资建议'
WEIGHTS_VERSION = 'v0.1'
_ENV_DAMP_CYCLES = ('冰点', '退潮')       # 这两档 main 门槛 +8 且一律标"退潮中"


def _setting(key: str, default):
    """覆盖表优先、config 兜底（与 rec_sim 同范式；value() 对 bool/int 返回原单位）"""
    try:
        from services.support import app_settings
        v = app_settings.value(key)
        return default if v is None else v
    except Exception:
        return default


# ========== 表 ==========
def _conn():
    from db.database import get_db_conn
    return get_db_conn()


def _ensure_tables(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sector_persistence_daily (
            sector TEXT NOT NULL, calc_date TEXT NOT NULL,
            chg5 REAL, chg10 REAL, up_days5 INTEGER, amount_ratio5 REAL,
            n_members INTEGER, caliber TEXT, payload TEXT,
            PRIMARY KEY (sector, calc_date));
        CREATE TABLE IF NOT EXISTS sector_taxonomy_vote (
            em_name TEXT NOT NULL, bs_name TEXT NOT NULL,
            votes INTEGER DEFAULT 0, total_votes INTEGER DEFAULT 0,
            share REAL DEFAULT 0, updated_at TEXT,
            PRIMARY KEY (em_name, bs_name));
        CREATE TABLE IF NOT EXISTS market_mainline_snapshots (
            main_date TEXT NOT NULL, ts INTEGER NOT NULL,
            top_sector TEXT, main_count INTEGER, stale INTEGER DEFAULT 0,
            weights_version TEXT, payload TEXT,
            PRIMARY KEY (main_date, ts));
    """)


# ========== 板块持续性（本地日K × 国标行业，一次算全市场） ==========
_PERSIST_MEMO: Dict = {'ts': 0.0, 'data': {}}
_PERSIST_TTL = 6 * 3600
# 收盘定版时若持续性表落后于收评日期，允许强制重算；同一日期 10 分钟内只试一次，
# 避免日K还没同步时每次轮询都全市场重算（20:25 定时任务仍是兜底）。
_FINAL_PERSIST_TRY: Dict[str, float] = {}
_FINAL_PERSIST_RETRY_SEC = 600


def _final_lead_sectors(day: str) -> List[Dict]:
    """收盘定版：用当日涨停池明细重建板块结构（收评只存总量指标，没有板块清单）。

    limit_pool_daily.raw 保留着东财原始字段（hybk/lbc/fund/ltsz…），正好够
    `sentiment._lead_sectors` 复用同一套口径。读不到就返回空，让调用方如实降级，
    绝不拿昨天/静态概念表凑一份"今天的主线"。
    """
    try:
        import sqlite3
        from app.config import SQLITE_DB_PATH
        from services.strategy.sentiment import _lead_sectors

        conn = sqlite3.connect(SQLITE_DB_PATH, timeout=30)
        try:
            rows = conn.execute(
                "SELECT raw FROM limit_pool_daily WHERE trade_date=? AND pool='zt'",
                (day,)).fetchall()
        finally:
            conn.close()
        pool = []
        for (raw,) in rows:
            try:
                it = json.loads(raw or '{}')
            except Exception:
                continue
            if isinstance(it, dict) and it.get('c'):
                pool.append(it)
        return _lead_sectors(pool) if pool else []
    except Exception as e:
        print(f'[主线] 收盘定版板块重建失败 day={day}: {e}')
        return []


def compute_sector_persistence(force: bool = False) -> Dict[str, Dict]:
    """近10日板块等权收益（截尾均值日收益复利）。同步函数，调用方放 to_thread。

    口径钉死（自测 D 节逐条断言）：
      · 成员 = 窗口内**每个日期都有 bar** 的交集——10-16 选股范围收窄（5222→2458）
        的混合日不能让分母漂移，交集 <SECTOR_MIN_MEMBERS 的板块 → 不出数；
      · 每日板块收益 = 成员日收益**截尾均值**（各去 10%、至少各去 1 只），
        防新股首日/连板票把等权拉爆；
      · chg5/chg10 用**复利** Π(1+r)−1，不是日收益求和；
      · 盘中 stock_daily 最新=昨收（18:15 才同步）→ 这是"截至昨日"的慢变量。
    """
    now = time.time()
    if not force and _PERSIST_MEMO['data'] and now - _PERSIST_MEMO['ts'] < _PERSIST_TTL:
        return _PERSIST_MEMO['data']
    import sqlite3
    from app.config import SQLITE_DB_PATH, KLINE_HISTORY_DB_PATH
    from services.data.fundflow import _clean_industry
    from services.data.kline_history import _normalize_local_symbol

    ind = {}
    c = sqlite3.connect(SQLITE_DB_PATH, timeout=30)
    try:
        _ensure_tables(c)
        for r in c.execute("SELECT symbol, industry FROM stock_industry_map WHERE industry<>''"):
            ind[_normalize_local_symbol(r[0])] = _clean_industry(r[1])
    finally:
        c.close()
    if not ind:
        return {}
    k = sqlite3.connect(KLINE_HISTORY_DB_PATH)
    try:
        dates = [r[0] for r in k.execute(
            "SELECT DISTINCT date FROM stock_daily ORDER BY date DESC LIMIT 11")]
        if len(dates) < 6:
            return {}
        cutoff = min(dates)
        per: Dict[str, Dict[str, Tuple]] = {}
        for r in k.execute(
                "SELECT symbol, date, close, turnover FROM stock_daily WHERE date>=? "
                "ORDER BY symbol, date", (cutoff,)):
            per.setdefault(r[0], {})[r[1]] = (r[2], r[3])
    finally:
        k.close()
    dates = sorted(dates)
    # 行业 → {date: [ret...], amount 窗口}
    acc: Dict[str, Dict] = {}
    for sym, daymap in per.items():
        bs = ind.get(sym)
        if not bs or len(daymap) < len(dates):      # 交集：缺一天就整个成员剔除
            continue
        seq = [daymap[d] for d in dates]
        a = acc.setdefault(bs, {'rets': [[] for _ in range(len(dates) - 1)],
                                'n': 0, 'amt5': 0.0, 'amt10': 0.0})
        a['n'] += 1
        for i in range(1, len(seq)):
            prev, cur = seq[i - 1][0], seq[i][0]
            if prev and prev > 0:
                a['rets'][i - 1].append(cur / prev - 1)
        a['amt5'] += sum(x[1] or 0 for x in seq[-5:])
        a['amt10'] += sum(x[1] or 0 for x in seq[-10:-5])

    def _trimmed(vals: List[float]) -> Optional[float]:
        if not vals:
            return None
        s = sorted(vals)
        cut = max(1, int(len(s) * 0.1))
        core = s[cut: len(s) - cut] if len(s) > 2 * cut else s
        return sum(core) / len(core)

    min_mem = int(_setting('sector_min_members', SECTOR_MIN_MEMBERS))
    out: Dict[str, Dict] = {}
    calc_date = dates[-1]
    for bs, a in acc.items():
        if a['n'] < min_mem:
            continue
        daily = [_trimmed(v) for v in a['rets']]
        daily = [d for d in daily if d is not None]
        if len(daily) < 5:
            continue
        chg5 = 1.0
        for r in daily[-5:]:
            chg5 *= (1 + r)
        chg10 = 1.0
        for r in daily[-10:]:
            chg10 *= (1 + r)
        out[bs] = {
            'chg5': round((chg5 - 1) * 100, 2),
            'chg10': round((chg10 - 1) * 100, 2),
            'upDays5': sum(1 for r in daily[-5:] if r > 0),
            'amountRatio5': round(a['amt5'] / a['amt10'], 2) if a['amt10'] else None,
            'nMembers': a['n'], 'asOf': calc_date,
            'caliber': '成员交集截尾均值复利',
        }
    if out:
        c = sqlite3.connect(SQLITE_DB_PATH, timeout=30)
        try:
            _ensure_tables(c)
            for bs, v in out.items():
                c.execute("INSERT OR REPLACE INTO sector_persistence_daily VALUES (?,?,?,?,?,?,?,?,'')",
                          (bs, calc_date, v['chg5'], v['chg10'], v['upDays5'],
                           v['amountRatio5'], v['nMembers'], v['caliber']))
            c.commit()
        finally:
            c.close()
        _PERSIST_MEMO['ts'] = now
        _PERSIST_MEMO['data'] = out
    return out


def load_persistence() -> Tuple[Dict[str, Dict], str]:
    """读当日（或最近一日）持续性表；表缺 → 现算一次。返回 ({sector: 指标}, asOfDate)。"""
    import sqlite3
    from app.config import SQLITE_DB_PATH
    c = sqlite3.connect(SQLITE_DB_PATH, timeout=30)
    try:
        _ensure_tables(c)
        row = c.execute("SELECT MAX(calc_date) FROM sector_persistence_daily").fetchone()
        d0 = row[0] if row else None
        if d0:
            out = {r[0]: {'chg5': r[1], 'chg10': r[2], 'upDays5': r[3],
                          'amountRatio5': r[4], 'nMembers': r[5], 'asOf': d0}
                   for r in c.execute(
                       "SELECT sector,chg5,chg10,up_days5,amount_ratio5,n_members "
                       "FROM sector_persistence_daily WHERE calc_date=?", (d0,))}
            if out:
                return out, d0
    finally:
        c.close()
    fresh = compute_sector_persistence()
    return fresh, (max((v.get('asOf') or '') for v in fresh.values()) if fresh else '')


# ========== 东财行业名 → 国标行业名（投票映射） ==========
def record_taxonomy_vote(lead_sectors: List[Dict]) -> None:
    """样本来源：每个东财行业板块的代表股（≤3 只）在 stock_industry_map 的国标名。
    累计 ≥3 票且 top share ≥60% 才认定映射——样本不足宁可 None（持续性缺维），
    不可瞎配（错映射比缺数毒得多：它会把别的板块的历史安在头上）。"""
    if not lead_sectors:
        return
    import sqlite3
    from app.config import SQLITE_DB_PATH
    from services.data.fundflow import _clean_industry
    from services.data.kline_history import _normalize_local_symbol
    c = sqlite3.connect(SQLITE_DB_PATH, timeout=30)
    try:
        _ensure_tables(c)
        pairs: List[Tuple[str, str]] = []
        for sec in lead_sectors:
            em = sec.get('sector')
            for ld in sec.get('leaders') or []:
                code = _normalize_local_symbol(str(ld.get('code') or ''))
                if not (em and code):
                    continue
                r = c.execute("SELECT industry FROM stock_industry_map WHERE symbol LIKE ?",
                              (f'%{code}',)).fetchone()
                if r and r[0]:
                    pairs.append((em, _clean_industry(r[0])))
        now_s = datetime.now().strftime('%Y-%m-%d %H:%M')
        for em, bs in pairs:
            c.execute("INSERT INTO sector_taxonomy_vote VALUES (?,?,1,1,1,?) "
                      "ON CONFLICT(em_name,bs_name) DO UPDATE SET "
                      "votes=votes+1, total_votes=total_votes+1, updated_at=excluded.updated_at",
                      (em, bs, now_s))
            c.execute("UPDATE sector_taxonomy_vote SET total_votes=("
                      "SELECT SUM(votes) FROM sector_taxonomy_vote v2 WHERE v2.em_name=sector_taxonomy_vote.em_name"
                      ") WHERE em_name=?", (em,))
            c.execute("UPDATE sector_taxonomy_vote SET share=ROUND(1.0*votes/NULLIF(total_votes,0),3) "
                      "WHERE em_name=?", (em,))
        c.commit()
    finally:
        c.close()


def load_taxonomy_votes() -> Dict[str, Tuple[str, int, float]]:
    """一次读全投票表 → {东财名: (国标名, votes, share)}，评分循环内零查询。"""
    try:
        import sqlite3
        from app.config import SQLITE_DB_PATH
        c = sqlite3.connect(SQLITE_DB_PATH, timeout=30)
        rows = c.execute("SELECT em_name, bs_name, votes, share FROM sector_taxonomy_vote "
                         "ORDER BY em_name, votes DESC").fetchall()
        c.close()
    except Exception:
        return {}
    out: Dict[str, Tuple[str, int, float]] = {}
    for em, bs, v, s in rows:
        out.setdefault(em, (bs, v, s or 0))   # ORDER BY votes DESC → 首个即 top
    return out


def resolve_persistence_name(em_name: str, persist: Dict[str, Dict],
                             votes: Dict[str, Tuple[str, int, float]]) -> Tuple[Optional[str], str]:
    """东财行业名 → 持续性表的键。直配 > 投票映射（≥3 票且 share≥60%）> None。"""
    if em_name in persist:
        return em_name, 'direct'
    v = votes.get(em_name)
    if v and v[1] >= 3 and v[2] >= 0.6 and v[0] in persist:
        return v[0], 'vote'
    return None, 'none'


# ========== 评分 ==========
def _percentile_map(pairs: List[Tuple[str, float]]) -> Dict[str, float]:
    """源内分位 [0,1]（并列取同名次）。空/单元素 → 0.5（不给免费分也不罚）。"""
    vals = [(k, v) for k, v in pairs if v is not None]
    if len(vals) <= 1:
        return {k: 0.5 for k, _ in vals}
    vals.sort(key=lambda x: x[1])
    n = len(vals) - 1
    return {k: round(i / n, 3) for i, (k, _v) in enumerate(vals)}


def _lianban_step(m: int) -> float:
    return {0: 0.0, 1: 0.2}.get(m) or (0.35 if m == 2 else 0.6 if m == 3 else 0.8 if m <= 4 else 1.0)


def _sync_score(senti: Dict, flow: Dict, persist: Dict, now_dt: datetime) -> Dict:
    """纯计算：输入四路、输出 main/sub/fading/candidates/none + meta。零 I/O，可单测。"""
    gaps: List[str] = []
    weights = dict(_setting('mainline_weights', None) or MAINLINE_WEIGHTS)
    min_score = int(_setting('mainline_min_score', MAINLINE_MIN_SCORE))
    sub_min = int(_setting('mainline_sub_min_score', MAINLINE_SUB_MIN_SCORE))
    gap = int(_setting('mainline_gap', MAINLINE_GAP))

    leads = {s['sector']: s for s in (senti.get('leadSectors') or [])}
    ind_rows = ((flow.get('rows') or {}).get('industry') or {}).get('inflow') or []
    flow_by_name = {r.get('name'): r for r in ind_rows if r.get('name')}
    flow_caliber = 'eastmoney_f62'
    if ind_rows and ind_rows[0].get('chgCaliber'):
        flow_caliber = 'sina_r0'
    out_names = {r.get('name') for r in
                 (((flow.get('rows') or {}).get('industry') or {}).get('outflow') or [])
                 if r.get('name')}

    cands: Dict[str, Dict] = {}
    for name in set(leads) | set(flow_by_name):
        cands[name] = {'name': name}
    # 跨分类合并（2026-10-22）：新浪兜底日的板块名是国标名、涨停池是东财名——同一板块
    # 会在候选里出现两次（一半有资金一半有涨停，两边都判不成主线）。用投票映射把
    # 国标名的资金行并到东财名的候选上（映射未成熟时宁可保持分裂+缺维，不瞎并）。
    if flow_caliber == 'sina_r0' and leads:
        votes = load_taxonomy_votes()
        bs2em = {bs: em for em, (bs, v, sh) in votes.items() if v >= 3 and sh >= 0.6}
        for em, bs in bs2em.items():
            if em in cands and bs in flow_by_name and bs not in leads:
                # 把国标名的资金行挂到东财名上（主循环按名字取资金/涨幅，这里改字典即可）
                flow_by_name[em] = dict(flow_by_name[bs], name=em, mergedFrom=bs)
                cands.pop(bs, None)

    # 各维原料
    limit_raw, fund_raw, fs_raw = [], [], []
    for name, c in cands.items():
        ld = leads.get(name)
        c['limitUp'] = ld['limitUp'] if ld else 0
        c['maxLianban'] = ld['maxLianban'] if ld else 0
        c['tierCount'] = ld.get('tierCount', 0) if ld else 0
        c['leaders'] = (ld or {}).get('leaders', [])[:3]
        c['fundSum'] = (ld or {}).get('fundSum', 0) if ld else 0
        fr = flow_by_name.get(name) or (flow_by_name.get(name.replace('Ⅱ', '').replace('Ⅲ', '')) or {})
        c['chg1d'] = fr.get('changePercent')
        c['fundInflow'] = fr.get('mainNetInflow')
        c['riseCount'] = fr.get('riseCount')
        c['fallCount'] = fr.get('fallCount')
        limit_raw.append((name, c['limitUp']))
        fs_raw.append((name, c['fundSum']))
        fund_raw.append((name, c['fundInflow'] if (c['fundInflow'] or 0) > 0 else None))
    p_limit, p_fs, p_fund = (_percentile_map(limit_raw), _percentile_map(fs_raw),
                             _percentile_map(fund_raw))

    # 持续性/量能（要过映射）
    votes = load_taxonomy_votes()
    p_chg5 = _percentile_map([(n, (persist.get(n) or {}).get('chg5')) for n in persist])
    p_amt = _percentile_map([(n, (persist.get(n) or {}).get('amountRatio5')) for n in persist])

    env = senti.get('cycle') or ''
    indics = senti.get('indicators') or {}
    promo = indics.get('promotionRate')
    zhaban = indics.get('zhabanRate')

    for name, c in cands.items():
        dims, wsum = {}, 0.0
        # limit：涨停数 0.45 + 最高连板 0.30 + 梯队 0.25；孤军（1 只独苗）×0.55
        lm = (0.45 * p_limit.get(name, 0.0) + 0.30 * _lianban_step(c['maxLianban'])
              + 0.25 * (1.0 if c['tierCount'] >= 2 else 0.5 if c['tierCount'] == 1 else 0.0))
        if c['limitUp'] == 1:
            lm *= 0.55
        dims['limit'] = lm
        # fund：净流入分位 0.7 + 封单分位 0.3；净流出 → 0
        fi = c['fundInflow']
        dims['fund'] = 0.0 if (fi is not None and fi <= 0) else (
            0.7 * p_fund.get(name, 0.0) + 0.3 * p_fs.get(name, 0.0))
        # persist + volume 走映射名
        bs, matched = resolve_persistence_name(name, persist, votes) if persist else (None, 'none')
        c['matchedVia'] = matched
        pv = persist.get(bs) or {}
        c['chg5d'] = pv.get('chg5')
        c['chg10d'] = pv.get('chg10')
        c['upDays5'] = pv.get('upDays5')
        c['amountRatio5'] = pv.get('amountRatio5')
        if pv:
            dims['persist'] = 0.4 * p_chg5.get(bs, 0.0) + 0.6 * (c['upDays5'] or 0) / 5
            dims['volume'] = p_amt.get(bs, 0.0)
        else:
            gaps.append(f'持续性缺分母（板块「{name}」未映射到本地行业口径）')
        # 缺维重归一
        w_present = {k: weights[k] for k in dims if k in weights}
        tot = sum(w_present.values())
        c['breakdown'] = {k: round(v, 3) for k, v in dims.items()}
        c['score'] = round(sum(w_present[k] * dims[k] for k in w_present) / tot * 100, 1) if tot else 0.0
        c['fundPct'] = p_fund.get(name)
        c['limitPct'] = p_limit.get(name)

    # 入池门槛：limit 或 fund 至少一维进源内前 30%
    pool = [c for c in cands.values()
            if (c.get('limitPct') or 0) >= 0.7 or (c.get('fundPct') or 0) >= 0.7 or c['limitUp'] >= 2]
    pool.sort(key=lambda x: -x['score'])

    # 时间/新鲜度闸门
    minutes = now_dt.hour * 60 + now_dt.minute
    # 收盘定版（2026-10-23）：final 的 stale 是"不做盘中否决"的纪律标记，不是数据坏了。
    # 今天已有收评 + 涨停池明细时，结构就是可用的；不把它当 stale，否则页面永远只剩候选。
    final = bool(senti.get('final'))
    # 定版数据没有"开盘累积"问题：周末/清晨打开也应直接给判定，而不是等 10:00。
    after_gate = final or minutes >= int(_setting('mainline_judge_after', MAINLINE_JUDGE_AFTER_MIN))
    senti_stale = bool(senti.get('stale')) and not final
    final_day = str(senti.get('date') or senti.get('asOf') or '')[:10]
    flow_ts = float(flow.get('ts') or 0)
    flow_age = now_dt.timestamp() - flow_ts if flow_ts else None
    flow_day = datetime.fromtimestamp(flow_ts).strftime('%Y-%m-%d') if flow_ts else ''
    # 收盘定版接受"同一交易日的最后一份资金快照"（14:38 之后不再刷新是正常的），
    # 但跨日旧资金仍然判 stale——旧资金不判今日主线。
    final_flow_ok = bool(final and flow_day and final_day and flow_day == final_day
                         and ind_rows)
    flow_stale = flow_age is None or (
        flow_age > int(_setting('mainline_flow_stale', MAINLINE_FLOW_STALE_SEC))
        and not final_flow_ok)
    if env in _ENV_DAMP_CYCLES:
        min_score += 8

    def _stage(c: Dict) -> str:
        if (promo is not None and promo < 25) or (zhaban is not None and zhaban >= 40):
            return '分歧'
        if (c.get('chg5d') or 0) > 0 and c['maxLianban'] >= 2:
            return '加速'
        return '一致'

    main, sub = [], []
    if after_gate and not senti_stale and not flow_stale:
        for c in pool:
            # 资金门槛"有数才卡"：sina 日映射未成熟时板块可能没有资金行（fundInflow None），
            # 评分已按缺维重归一；若此时还硬卡分位，主线会被饿死。但"资金已知且为负/垫底"必须拦。
            fund_known = c.get('fundInflow') is not None
            qualified = (c['score'] >= min_score and c['maxLianban'] >= 2
                         and (not fund_known or (c.get('fundPct') or 0) >= 0.70))
            if qualified and len(main) < 2:
                if main and main[0]['score'] - c['score'] >= gap:
                    continue   # 断层：只认一条主线
                c['tier'] = 'main'
                c['stage'] = '退潮中' if env in _ENV_DAMP_CYCLES else _stage(c)
                c['why'] = _why(c)
                main.append(c)
            elif 42 <= c['score'] < min_score or (c['score'] >= min_score and c['maxLianban'] < 2):
                if len(sub) < 5 and c not in main:
                    c['tier'] = 'sub'
                    c['why'] = _why(c)
                    sub.append(c)
    none_reason = None
    if not main:
        if not after_gate:
            none_reason = '开盘累积中，10:00 后给首个判定'
        elif final and not leads:
            none_reason = '今日涨停池明细缺失，无法重建板块结构'
        elif senti_stale:
            none_reason = '今日涨停结构不可用，以下仅资金/涨幅候选'
        elif flow_stale:
            none_reason = ('板块资金流不是今日快照，收盘定版不判主线' if final
                           else '板块资金流缺失或超过 2 小时，旧资金不判今日主线')
        elif pool and pool[0]['score'] < sub_min:
            top2 = sum(c['limitUp'] for c in pool[:2])
            none_reason = f'无主线：最高分 {pool[0]["score"]:.0f}＜{sub_min}，前二板块涨停合计仅 {top2} 家'
        else:
            none_reason = '暂无法判定（数据不足）'
            gaps.append('main/sub 均空但原因未归类——检查 sentiment/fundflow 缓存')

    # 退潮：昨日 main 今日掉线（读最近一次快照的 main 名单）
    fading = _fading_from_prev(main, pool)

    return {
        'main': main[:2], 'sub': sub[:5], 'fading': fading[:3],
        'candidates': ([] if main else sorted(pool, key=lambda x: -x['score'])[:6]),
        'none': {'reason': none_reason} if none_reason else None,
        'final': final,
        'meta': {
            'caliber': {'fund': flow_caliber, 'persist': 'local_daily_k' if persist else 'missing',
                        'persistAsOfDate': persist and max((v.get('asOf') or '') for v in persist.values()) or '',
                        'weightsVersion': WEIGHTS_VERSION},
            'sources': {'zt': ('close_final' if final else
                               'review_fallback' if senti_stale else 'intraday'),
                        'flow': flow.get('src'), 'flowAgeSec': round(flow_age) if flow_age else None,
                        'flowAt': (datetime.fromtimestamp(flow_ts).strftime('%m-%d %H:%M')
                                   if flow_ts else '')},
            'dataGaps': sorted(set(gaps)),
            'final': final,
            'finalAsOfDate': final_day if final else '',
            # 收盘后不再有"下次判定"：数据已定版，每 180 秒重算只会给人"还在变"的错觉。
            'nextJudgeAt': '' if final else _next_judge(now_dt, after_gate),
            'env': {'cycle': env, 'cycleScore': senti.get('cycleScore'),
                    'promotionRate': promo, 'zhabanRate': zhaban,
                    'minScoreUsed': min_score},
        },
    }


def _why(c: Dict) -> str:
    bits = []
    if c['limitUp']:
        bits.append(f"涨停{c['limitUp']}只·最高{c['maxLianban']}板")
    if c.get('fundInflow') is not None:
        bits.append(f"资金P{(c.get('fundPct') or 0) * 100:.0f}")
    if c.get('chg5d') is None:
        bits.append('持续性缺数')
    return '，'.join(bits) or '入池'


def _fading_from_prev(main: List[Dict], pool: List[Dict]) -> List[Dict]:
    try:
        import sqlite3
        from app.config import SQLITE_DB_PATH
        c = sqlite3.connect(SQLITE_DB_PATH, timeout=30)
        r = c.execute("SELECT payload FROM market_mainline_snapshots ORDER BY main_date DESC, ts DESC LIMIT 1").fetchone()
        c.close()
        prev = json.loads(r[0]) if r and r[0] else {}
    except Exception:
        prev = {}
    prev_main = {m['name']: m.get('score') for m in (prev.get('main') or [])}
    cur = {m['name'] for m in main}
    by_name = {p['name']: p for p in pool}
    out = []
    for name, ps in prev_main.items():
        c0 = by_name.get(name)
        if name in cur or c0 is None:
            continue
        if (c0['score'] < int(_setting('mainline_sub_min_score', MAINLINE_SUB_MIN_SCORE))
                or (c0.get('fundInflow') or 0) < 0
                or c0.get('maxLianban', 0) == 0):
            out.append({'name': name, 'scoreDropFrom': ps, 'scoreNow': c0['score'],
                        'reason': ('资金转负' if (c0.get('fundInflow') or 0) < 0
                                   else '今日无涨停' if c0.get('maxLianban', 0) == 0
                                   else '评分掉出支线线')})
    return out


def _next_judge(now_dt: datetime, after_gate: bool) -> str:
    if not after_gate:
        return '10:00'
    nxt = now_dt.timestamp() + MAINLINE_REFRESH_TTL_SEC
    return datetime.fromtimestamp(nxt).strftime('%H:%M')


# ========== 对外入口（TTL + 非阻塞 singleflight，照 sentiment 范式） ==========
_CACHE: Dict = {'ts': 0.0, 'data': None}
_LOCK = threading.Lock()
_last_save = 0.0


def peek_mainline_cached(max_age_sec: Optional[int] = None) -> Optional[Dict]:
    age = time.time() - _CACHE['ts']
    if _CACHE['data'] and (max_age_sec is None or age <= max_age_sec):
        d = dict(_CACHE['data'])
        d['asOf'] = datetime.fromtimestamp(_CACHE['ts']).strftime('%H:%M:%S')
        return d
    return None


async def get_mainline(force: bool = False) -> Dict:
    """行动台唯一入口。常态命中缓存（scheduler 每 180s 预热）；未命中现算 ~50ms。"""
    if not _setting('mainline_enabled', MAINLINE_ENABLED):
        return {'disabled': True}
    now = time.time()
    if not force and _CACHE['data'] and now - _CACHE['ts'] < MAINLINE_REFRESH_TTL_SEC:
        d = dict(_CACHE['data'])
        d['asOf'] = datetime.fromtimestamp(_CACHE['ts']).strftime('%H:%M:%S')
        return d
    acquired = _LOCK.acquire(blocking=False)
    if not acquired:
        # 别的线程在算：绝不同步等锁（async 上下文等锁=冻住事件循环，sentiment 同款教训）
        return peek_mainline_cached() or {'disabled': False, 'pending': True,
                                          'main': [], 'sub': [], 'fading': [],
                                          'none': {'reason': '判定进行中，稍后刷新'}}

    async def _compute():
        from services.strategy.sentiment import get_sentiment, peek_cached
        from services.data.fundflow import peek_sector_flow
        senti = await get_sentiment()
        # 行动台并发发起 sentiment / mainline 时，后者可能抢不到 singleflight 锁，
        # 拿到没有 leadSectors 的"刷新中"空壳。先用最近一次真实值兜，再接受降级；
        # 这种降级结果绝不写缓存（见下面的 _no_cache），否则会把 180 秒都锁成"不可用"。
        lose_race = senti.get('source') == '刷新中'
        if lose_race:
            senti = peek_cached(max_age_sec=900) or senti
        final = bool(senti.get('final'))
        final_day = str(senti.get('date') or senti.get('asOf') or '')[:10]
        senti_eff = dict(senti)
        if final and final_day:
            # 收评的 sentimentCycle 只有总量、没有板块清单；用当日涨停池明细重建。
            # 这一步是"今天的结构可用"的前提，失败则保持空表走如实降级。
            leads = await asyncio.to_thread(_final_lead_sectors, final_day)
            if leads:
                senti_eff['leadSectors'] = leads[:5]
        flow = await asyncio.to_thread(peek_sector_flow)
        persist, persist_asof = await asyncio.to_thread(load_persistence)
        if final and final_day and (not persist_asof or persist_asof < final_day):
            # 持续性表落后于收评日期：日K已同步就按今日重算一次（10 分钟节流）。
            # 2026-10-23 现象：日K已到 10-23，表还停在 10-22（20:25 任务未到点），
            # 页脚一直写"截至 10-22"，用户自然以为整页是昨天的。
            now_ts = time.time()
            if now_ts - _FINAL_PERSIST_TRY.get(final_day, 0) >= _FINAL_PERSIST_RETRY_SEC:
                _FINAL_PERSIST_TRY[final_day] = now_ts
                fresh = await asyncio.to_thread(compute_sector_persistence, True)
                if fresh:
                    persist = fresh
        elif not persist:
            persist = await asyncio.to_thread(compute_sector_persistence)
        await asyncio.to_thread(record_taxonomy_vote, senti_eff.get('leadSectors') or [])
        out = _sync_score(senti_eff, flow, persist, datetime.now())
        out.update({'asOf': datetime.now().strftime('%H:%M:%S'),
                    'date': datetime.now().strftime('%Y-%m-%d'),
                    'cycle': senti.get('cycle'), 'cycleScore': senti.get('cycleScore'),
                    'stale': bool(senti.get('stale')) and not final,
                    'final': final,
                    'disclaimer': DISCLAIMER})
        # 拿到的是并发空壳/非定版空壳：本次结果只用于这一屏的降级展示，
        # 不写 180 秒缓存，下一轮请求应重新尝试真值。
        out['_no_cache'] = bool(lose_race and senti.get('source') == '刷新中')
        return out

    try:
        data = await _compute()
        no_cache = bool(data.pop('_no_cache', False))
        if not no_cache:
            _CACHE['ts'] = time.time()
            _CACHE['data'] = data
            await asyncio.to_thread(_maybe_save, data)
        return dict(data)
    finally:
        _LOCK.release()


def _maybe_save(data: Dict) -> None:
    global _last_save
    now = time.time()
    if now - _last_save < MAINLINE_SNAPSHOT_SAVE_SEC:
        return
    _last_save = now
    slim = {k: data.get(k) for k in ('main', 'sub', 'fading', 'candidates', 'none', 'meta')}
    try:
        import sqlite3
        from app.config import SQLITE_DB_PATH
        c = sqlite3.connect(SQLITE_DB_PATH, timeout=30)
        _ensure_tables(c)
        c.execute("INSERT OR REPLACE INTO market_mainline_snapshots VALUES (?,?,?,?,?,?,?)",
                  (data['date'], int(now), (data.get('main') or [{}])[0].get('name', ''),
                   len(data.get('main') or []), 1 if data.get('stale') else 0,
                   WEIGHTS_VERSION, json.dumps(slim, ensure_ascii=False)))
        c.execute("DELETE FROM market_mainline_snapshots WHERE main_date < date('now','-90 day')")
        c.commit()
        c.close()
    except Exception as e:
        print(f'[主线判定] 落库失败: {e}')
