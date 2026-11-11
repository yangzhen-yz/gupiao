"""focus_reasons.py - 收评「明日重点关注」买入逻辑卡（2026-11-10，A 方案第 1 层）

为什么：卡片 reason 原先只有「融合分 XX 分 + 量比/委比」这类技术指标，回答不了
「为什么是它 / 属于当前哪条主线 / 有什么题材」。本模块把系统里**已经落库**的
结构化事实拼成一张只读逻辑卡，让每只票的逻辑可见、可核对。

硬约束（改这个文件前先读）：
  1. **纯展示**：不进评分、不进选票、不进门槛、不进推送、不参与任何收益判断。
     `mainline.py:17-18` 明确排除「政策催化/产业链/业绩支撑」——本仓库无新闻 NLP，
     把主观包装成分数不可接受。这里只摆**已有事实**，不构成对该票的背书。
     尤其：`cls_pool_daily.reason` 是财联社的「题材/涨停原因归因」，不是「利好事件」；
     系统当前没有新闻/研报/公告利好数据源，本模块绝不生成这类文字。
  2. **缺数如实**：任何字段拿不到就写进 gaps，绝不补 0、绝不编理由。
     实测覆盖（2026-11-09）：`plates` 100%，`reason`/`themes` 约 6 成；
     非涨停票根本不在四池，只能标「未上四池」。
  3. **零外部请求**：只读 cls_pool_daily / theme_chain_daily / market_mainline_snapshots
     三张本地表，全部走本地 SQLite；不触发新浪全市场扫描、不发任何 HTTP。
  4. **当日数据未落库要标注**：四池由 15:10 / 15:45 任务落库，收评同在 15:10 生成，
     当天可能还没数据 —— 退回最近一天并把 poolDay/chainDay 一起带出去，别装成实时。
  5. 失败一律静默返回 0，绝不让收评生成中断（调用方必须包 try）。

口径说明：热门链用 `theme_chain_daily.members_json` 按股票代码**精确命中**；主线板块
是行业名，只能用 `plates`/`themes` 做**标签包含匹配**，属近似口径，文案已注明
「板块标签匹配口径」，不得写成「主线成分股确认」。

时间维度（2026-11-10 修正）：本卡里的「今日封板」只是 **T 日盘中**的执行约束
（此刻涨停买不进），**不等于「T+1 不可介入」**。明日 9:25 竞价是独立决策，
由 `review.py` 的条件单 + `auctionGuard` 负责；本卡不下「可买 / 不可买」结论。
那个 −0.28% / −5.15% 是「T 日摸板按涨停价买→次日开盘卖」的实测，
否定的是当天打板，不能拿来否定次日竞价接力。
"""
from typing import Dict, List, Optional
import json

_POOL_LABELS = {
    'up_pool': '涨停池',
    'continuous_up_pool': '连板池',
    'up_open_pool': '炸板池',
    'down_pool': '跌停池',
}
_SEALED_POOLS = {'up_pool', 'continuous_up_pool'}
_LIMIT_PCT = 9.8
_MAX_REASON = 140
_MAX_PLATES = 8


def _plain(code) -> str:
    """统一成 6 位纯代码：sz000525 / 000525 / SZ000525 → 000525。"""
    s = str(code or '').lower().strip()
    if s[:2] in ('sh', 'sz', 'bj'):
        s = s[2:]
    return s[-6:] if len(s) >= 6 else s


def _trunc(text, n: int = _MAX_REASON) -> str:
    t = ' '.join(str(text or '').split())
    return t if len(t) <= n else t[:n] + '…'


def _latest_mainline() -> Dict:
    """最近一档主线快照（main/sub 为板块对象，main[].leaders[] 带个股代码）。"""
    try:
        from db.database import get_db_conn
        conn = get_db_conn()
        try:
            row = conn.execute(
                "SELECT main_date, payload FROM market_mainline_snapshots "
                "ORDER BY main_date DESC, ts DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        if not row:
            return {}
        try:
            day, payload = row['main_date'], row['payload']
        except (TypeError, KeyError, IndexError):
            day, payload = row[0], row[1]
        p = json.loads(payload or '{}') if isinstance(payload, str) else (payload or {})
        return {'date': day, 'main': p.get('main') or [], 'sub': p.get('sub') or []}
    except Exception:
        return {}


def _pool_data(trade_date: str):
    """返回 (按代码索引的四池行, 实际数据日期)。当日未落库则退回最近一天。"""
    from db.database import get_db_conn, load_cls_pool_daily
    day = trade_date
    pools = load_cls_pool_daily(day) or {}
    if not any(pools.values()):
        latest = ''
        try:
            conn = get_db_conn()
            r = conn.execute('SELECT MAX(trade_date) FROM cls_pool_daily').fetchone()
            conn.close()
            latest = (r[0] if r else '') or ''
        except Exception:
            pass
        if latest and latest != day:
            day = latest
            pools = load_cls_pool_daily(day) or {}
    idx: Dict[str, Dict] = {}
    for _pool, rows in (pools or {}).items():
        for r in rows or []:
            k = _plain(r.get('symbol') or r.get('code'))
            if k:
                idx.setdefault(k, r)
    return idx, (day if any(pools.values()) else '')


def _chain_index():
    """返回 (代码 → 命中的链列表, 链数据日期, 链条数)。day 为空时 DAL 取最近一天。"""
    from db.database import load_theme_chain_daily
    chains = load_theme_chain_daily() or []
    idx: Dict[str, List[Dict]] = {}
    day = ''
    for c in chains:
        day = day or str(c.get('trade_date') or '')
        for m in (c.get('members') or []):
            k = ''
            if isinstance(m, (list, tuple)) and m:
                k = _plain(m[0])
            elif isinstance(m, dict):
                k = _plain(m.get('code') or m.get('symbol'))
            if k:
                idx.setdefault(k, []).append(c)
    return idx, day, len(chains)


def _match_sector(names: List[str], plates: List[str], themes: List[str]) -> str:
    """板块标签匹配（近似口径，非成分股确认）。返回首个命中的板块名。"""
    tags = [str(x).strip() for x in (list(plates) + list(themes)) if x]
    for n in names:
        n = str(n or '').strip()
        if len(n) < 2:
            continue
        for t in tags:
            if len(t) >= 2 and (n == t or n in t or t in n):
                return n
    return ''


def build_card(code: str, pool_row: Optional[Dict], chain_hits: List[Dict],
               ml: Dict, chain_day: str, pool_day: str) -> Dict:
    """拼一张票的逻辑卡。所有字段都可缺，缺的进 gaps，绝不编。"""
    key = _plain(code)
    gaps: List[str] = []
    risk: List[str] = []
    plates: List[str] = []
    themes: List[str] = []
    card: Dict = {
        'chainText': '', 'chainHit': False, 'chainDay': chain_day,
        'themeText': '', 'upReason': '', 'plates': [], 'poolText': '',
        'poolDay': pool_day, 'mainlineText': '', 'mainlineHit': None,
        'mainlineStatus': 'no_data', 'mainlineDay': ml.get('date') or '',
        'sealed': False, 'blowup': False,
        'risk': risk, 'gaps': gaps,
    }

    # 1) 热门链：members_json 按代码精确命中，是三路数据里最可靠的一路
    if not chain_day:
        gaps.append('热门链未落库')
        card['chainText'] = '热门链：暂无数据'
    elif chain_hits:
        c = chain_hits[0]
        card['chainHit'] = True
        extra = f"，另命中 {len(chain_hits) - 1} 条链" if len(chain_hits) > 1 else ''
        card['chainText'] = (
            f"今日热门链：{c.get('name') or '未命名'}"
            f"（涨停 {c.get('limit_up_num') or 0} 家 · 板块 {c.get('change_pct') or 0}% · "
            f"成员 {c.get('member_count') or 0} 只{extra}）")
    else:
        card['chainText'] = '不在今日热门链'

    # 2) 财联社四池：题材/涨停原因/板块标签/连板数/首封时间
    if pool_row:
        pool = pool_row.get('pool') or ''
        label = _POOL_LABELS.get(pool, pool or '未知池')
        try:
            days_i = int(pool_row.get('days')) if pool_row.get('days') is not None else None
        except (TypeError, ValueError):
            days_i = None
        bits = [label]
        if days_i and pool == 'continuous_up_pool':
            bits.append(f'{days_i} 连板')
        seal = str(pool_row.get('sealTime') or '').strip()
        if seal:
            bits.append(f'首封 {seal}')
        card['poolText'] = ' · '.join(bits)
        card['sealed'] = pool in _SEALED_POOLS
        card['blowup'] = pool == 'up_open_pool'
        themes = [str(x) for x in (pool_row.get('themes') or []) if x][:4]
        plates = [str(x) for x in (pool_row.get('plates') or []) if x][:_MAX_PLATES]
        reason = str(pool_row.get('reason') or '').strip()
        card['themeText'] = '、'.join(themes) if themes else ''
        card['upReason'] = _trunc(reason) if reason else ''
        card['plates'] = plates
        if not themes:
            gaps.append('题材归因缺失')
        if not reason:
            gaps.append('涨停原因缺失')
        if not plates:
            gaps.append('板块标签缺失')
    else:
        card['poolText'] = '未上四池（非涨停票）'
        gaps.append('四池无该票，无题材归因/连板数据')

    # 3) 主线：leaders 代码精确命中 > 板块标签匹配 > 未形成/不相关
    main = ml.get('main') or []
    sub = ml.get('sub') or []
    if not ml:
        gaps.append('主线快照缺失')
        card['mainlineText'] = '主线判定：暂无数据'
    else:
        leader_codes = {
            _plain(l.get('code')) for m in main
            for l in (m.get('leaders') or []) if isinstance(l, dict) and l.get('code')
        }
        main_names = [str(m.get('name') or '') for m in main if m.get('name')]
        sub_names = [str(s.get('name') or '') for s in sub if s.get('name')]
        if key in leader_codes:
            card['mainlineHit'] = True
            card['mainlineStatus'] = 'leader'
            card['mainlineText'] = f"主线龙头：{'、'.join(main_names[:2]) or '当日主线'}"
        else:
            hit_main = _match_sector(main_names, plates, themes)
            hit_sub = _match_sector(sub_names, plates, themes)
            if hit_main:
                card['mainlineHit'] = True
                card['mainlineStatus'] = 'main'
                card['mainlineText'] = f'主线板块相关：{hit_main}（板块标签匹配口径）'
            elif hit_sub:
                card['mainlineHit'] = False
                card['mainlineStatus'] = 'sub'
                card['mainlineText'] = f'仅支线相关：{hit_sub}（板块标签匹配口径）'
            elif not main_names:
                # 盘前/弱市主线档为空：是「没判出来」，不是「判了不在」，两者不可混写
                card['mainlineHit'] = None
                card['mainlineStatus'] = 'no_main'
                card['mainlineText'] = '今日主线未形成（无板块入选主线档）'
            else:
                card['mainlineHit'] = False
                card['mainlineStatus'] = 'unrelated'
                card['mainlineText'] = f"与今日主线（{'、'.join(main_names[:2])}）不相关"

    # 4) 票级风险：封板/炸板必须讲清楚，不把负期望包装成机会。
    # 2026-11-10 修正：这些数字测的是「T 日打板」，不是「T+1 竞价接力」，必须标清口径，
    # 否则会被读成「明天也不能买」——push.py:558 矩阵里「昨日封板→可竞价低吸」是另一笔决策。
    if card['sealed']:
        risk.append('今日已封板：T 日盘中已无买入空间（不追）；T+1 竞价是独立决策，按条件单另判')
        risk.append('T 日摸板按涨停价买整体 −0.28%（扣费 −0.41%）、炸板批 −5.15% —— 这是当天打板口径，不是次日竞价的证据')
    elif card['blowup']:
        risk.append('今日炸板（曾涨停未回封）：T 日尾盘接该批 −5.15% / 胜率 5.2%，不接')
    if card['mainlineStatus'] == 'unrelated':
        risk.append('不在当前主线，题材持续性缺少当日数据支撑')
    elif card['mainlineStatus'] == 'no_main':
        risk.append('当日主线未形成，主线维度不可判（不等于「不在主线」）')
    return card


def attach_focus_logic(focus_items: List[Dict], trade_date: str, *,
                       code_key: str = 'code', pct_key: str = 'pctChg',
                       out_key: str = 'logic') -> int:
    """给每只票挂结构化买入逻辑卡。返回挂上的只数；失败返回 0 且不抛。

    收评 `review.py` 用默认参数（写 `logic`）。行动台 `action_desk.py` 的「可介入」
    卡片自带一条字符串 `logic`（评分/执行理由），故用 `out_key='themeLogic'` +
    `code_key='symbol'` / `pct_key='changePercent'` 复用本函数，避免覆盖原字段。

    三个 key 参数只为复用同一套聚合口径，不改变任何判定逻辑。
    """
    if not focus_items:
        return 0
    try:
        pool_idx, pool_day = _pool_data(trade_date)
        chain_idx, chain_day, _n = _chain_index()
        ml = _latest_mainline()
        for f in focus_items:
            code = f.get(code_key) or ''
            key = _plain(code)
            card = build_card(code, pool_idx.get(key), chain_idx.get(key) or [],
                              ml, chain_day, pool_day)
            # 行情涨跌幅兜底：四池 15:10 后才落库，非四池票也可能当日封板
            try:
                pct = float(f.get(pct_key))
            except (TypeError, ValueError):
                pct = None
            if pct is not None and pct >= _LIMIT_PCT and not card['sealed']:
                card['sealed'] = True
                card['risk'].insert(0, '当日涨幅 ≥9.8%：T 日盘中不追封板；T+1 竞价按条件单另判')
            f[out_key] = card
        return len(focus_items)
    except Exception as e:
        print(f'[买入逻辑卡] 生成失败（不影响名单与评分）: {e}')
        return 0
