"""strategy_snapshot.py - 策略决策快照（2026-10-17 新增）

用户诉求："加个快照表吧，后续可随时根据历史数据进行复盘、优化"。

为什么非得单独建一张表（现有的都留不住现场）：
  · daily_candidate_pool 只保留最近 2 天（海选每天 DELETE 重算），
  · daily_recommendations 每天被 4 个批次覆盖成"最后一次"，中间三次决策消失，
  · recommend_shadow_log 一天一行 JSON 载荷，没法按分数/因子做 SQL 统计。
结果是：回测只能用**事后重建的近似口径**（scripts/pool_backtest.py 拿 r20 代理海选），
它回答不了"10 万按我的系统会变成多少钱"——因为系统真正的决策（13 因子评分 +
情绪门槛 + 时机/矩阵标签 + 被谁挡在门外）在盘后根本不存在。

从今天起每个批次给每只被扫到的票落一行，`stage` 记它最深走到哪一步，`blocked_by` 记没走下去的原因。
攒够 ~2 个月后，snapshot_report.py 就能用**真实决策**算 T+1/T+3 胜率、
按分数带/情绪档分组，以及"影子门槛 vs 线上门槛"谁更准。

三条设计约束：
  1. 只写不读、写失败不阻塞：调用方必须包 try/except，快照坏了推荐照跑。
  2. 分母要一致：批次头里存当日 `scope_desc`/`scope_size`（选票范围只数），
     否则以后统计"候选占池子多少"会又算回 5222。
  3. 口径可追溯：存 `matrix_version`、`min_score`、`gate_min_score`，
     以后评分权重改了就按版本号切开对比，不混为一谈。
"""
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional


def _set(key: str, cfg_name: str, default):
    """热读覆盖表（复盘·口径与开关页），读不到再退回 config 默认值。

    默认值不能在 import 时绑死：快照是每天跑的常驻任务，用户当天关掉就该当天停，
    而覆盖表改了不会重启进程。
    """
    try:
        from services.support import app_settings
        v = app_settings.value(key)
        return v if v is not None else default
    except Exception:
        import app.config as _cfg
        return getattr(_cfg, cfg_name, default)


def snap_enabled() -> bool:
    return bool(_set('snapshot_enabled', 'STRATEGY_SNAPSHOT_ENABLED', True))


def snap_max_rows() -> int:
    return int(_set('snapshot_max_rows', 'STRATEGY_SNAPSHOT_MAX_ROWS', 900))


def snap_retention_days() -> int:
    return int(_set('snapshot_retention_days', 'STRATEGY_SNAPSHOT_RETENTION_DAYS', 730))

STAGES = ('scored', 'candidate', 'recommended')

_DDL_ROWS = '''CREATE TABLE IF NOT EXISTS strategy_snapshot (
    snap_date      TEXT NOT NULL,
    batch          TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    name           TEXT,
    rank           INTEGER,
    stage          TEXT NOT NULL,
    blocked_by     TEXT,
    score          REAL,
    intraday_score REAL,
    trend_score    REAL,
    label          TEXT,
    change_percent REAL,
    price          REAL,
    volume_ratio   REAL,
    turnover_rate  REAL,
    weibi          REAL,
    outer_ratio    REAL,
    amplitude      REAL,
    factors        TEXT,
    inputs         TEXT,
    source         TEXT,
    mode           TEXT,
    role           TEXT,
    timing         TEXT,
    matrix         TEXT,
    matrix_why     TEXT,
    shadow_note    TEXT,
    concept        TEXT,
    r20            REAL,
    rank_pct       REAL,
    created_at     TEXT
)'''

_DDL_BATCH = '''CREATE TABLE IF NOT EXISTS strategy_snapshot_batch (
    snap_date      TEXT NOT NULL,
    batch          TEXT NOT NULL,
    generated_at   TEXT,
    cycle          TEXT,
    cycle_score    INTEGER,
    limit_up       INTEGER,
    limit_down     INTEGER,
    zhaban         INTEGER,
    promotion_rate REAL,
    market_change  REAL,
    min_score      INTEGER,
    gate_min_score INTEGER,
    scope_desc     TEXT,
    scope_size     INTEGER,
    scanned        INTEGER,
    scored         INTEGER,
    candidates     INTEGER,
    recommended    INTEGER,
    matrix_version TEXT,
    sentiment_stale INTEGER,
    weak_signals   TEXT,
    created_at     TEXT
)'''


def ensure_tables(conn):
    conn.execute(_DDL_ROWS)
    conn.execute(_DDL_BATCH)
    # 补列：早期版本建的表会缺新字段，INSERT 直接报错。
    # 本仓库常有另一路会话同时在改，加列必须自查而不是指望"大家都是从 DDL 建的"。
    have = {r[1] for r in conn.execute('PRAGMA table_info(strategy_snapshot)').fetchall()}
    for _col, _typ in (('shadow_note', 'TEXT'), ('mode', 'TEXT')):
        if _col not in have:
            conn.execute(f'ALTER TABLE strategy_snapshot ADD COLUMN {_col} {_typ}')
    # IF NOT EXISTS 覆盖不到"表已存在但要补索引"的情况，这里都得走一遍
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS ux_snap_key '
                 'ON strategy_snapshot (snap_date, batch, symbol)')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_snap_date '
                 'ON strategy_snapshot (snap_date, stage)')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_snap_sym '
                 'ON strategy_snapshot (symbol, snap_date)')


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v):
    x = _f(v)
    return None if x is None else int(x)


def _j(d):
    """因子明细/输入值可能是嵌套 dict，序列化失败也不能让整批快照写不进去。"""
    try:
        return json.dumps(d, ensure_ascii=False) if d else None
    except (TypeError, ValueError):
        return None


_COLS = ('snap_date', 'batch', 'symbol', 'name', 'rank', 'stage', 'blocked_by',
         'score', 'intraday_score', 'trend_score', 'label',
         'change_percent', 'price', 'volume_ratio', 'turnover_rate',
         'weibi', 'outer_ratio', 'amplitude', 'factors', 'inputs', 'source',
         'mode', 'role', 'timing', 'matrix', 'matrix_why', 'shadow_note', 'concept',
         'r20', 'rank_pct', 'created_at')

_BC = ('snap_date', 'batch', 'generated_at', 'cycle', 'cycle_score',
       'limit_up', 'limit_down', 'zhaban', 'promotion_rate', 'market_change',
       'min_score', 'gate_min_score', 'scope_desc', 'scope_size',
       'scanned', 'scored', 'candidates', 'recommended', 'matrix_version',
       'sentiment_stale', 'weak_signals', 'created_at')


def _row(s: Dict, date_key: str, batch: str, rank: int, stage: str,
         why: List[str], source: Optional[str], lab: Dict, pool: Dict,
         now_txt: str) -> tuple:
    """一行快照。**不改**传入的股票 dict —— 那些对象还在推荐链路里活着，往里塞临时键迟早出事。"""
    sc = s.get('score') or {}
    return (
        date_key, batch, (s.get('symbol') or '').lower(), s.get('name'), rank, stage,
        '；'.join(why) or None,
        _f(sc.get('total')), _f(sc.get('intraday_score')), _f(sc.get('trend_score')),
        sc.get('label'),
        _f(s.get('changePercent')), _f(s.get('priceRaw') or s.get('price')),
        _f(s.get('volumeRatio')), _f(s.get('turnoverRate')),
        _f(s.get('weibi')), _f(s.get('outerRatio')), _f(s.get('amplitude')),
        _j(sc.get('base_scores')), _j(sc.get('factor_values')), source,
        lab.get('mode'), lab.get('role'), lab.get('timing'), lab.get('matrix'), lab.get('matrix_why'),
        lab.get('shadow'),
        s.get('primary_tag'), _f(pool.get('r20')), _f(pool.get('rank_pct')), now_txt,
    )


def _batch_row(p: Dict, now_txt: str, counts: Dict) -> tuple:
    sent = p.get('sent') or {}
    ind = sent.get('indicators') or {}
    gate = p.get('gate') or {}
    return (
        p.get('date_key'), p.get('batch'), p.get('generated_at'), sent.get('cycle'),
        _i(sent.get('cycleScore')),
        _i(ind.get('limitUpCount')), _i(ind.get('limitDownCount')),
        _i(ind.get('zhabanCount')), _f(ind.get('promotionRate')),
        _f(p.get('market_change')), p.get('live_min_score'),
        _i(gate.get('advisoryMinScore')),
        p.get('scope_desc'), p.get('scope_size'),
        counts.get('scanned'), counts.get('scored'),
        counts.get('candidates'), counts.get('recommended'),
        p.get('matrix_version'), 1 if sent.get('stale') else 0,
        json.dumps(sent.get('weakSignals') or [], ensure_ascii=False), now_txt,
    )


def write_snapshot(payload: Dict, conn=None) -> int:
    """落一批决策快照，返回写入的票数（0 表示开关关了或没数据）。

    payload 由 recommend.auto_generate_recommendations_task 现场组装：
      date_key/batch/generated_at/scored/candidates/top3/blocked/sources/pool_info/
      labels/market_change/sent/gate/live_min_score/scope_desc/scope_size/matrix_version
    """
    if not snap_enabled():
        return 0
    scored = payload.get('scored') or []
    if not scored:
        return 0

    now = datetime.now()
    now_txt = now.strftime('%Y-%m-%d %H:%M:%S')
    date_key = payload['date_key']
    batch = payload['batch']

    # stage：最深走到哪一步。candidates 是过完全部门槛的名单，top3 是最终精选。
    cand = {(s.get('symbol') or '').lower() for s in (payload.get('candidates') or [])}
    top3 = {(s.get('symbol') or '').lower() for s in (payload.get('top3') or [])}
    blocked = payload.get('blocked') or {}
    sources = payload.get('sources') or {}
    pool_info = payload.get('pool_info') or {}
    labels = payload.get('labels') or {}
    min_score = payload.get('live_min_score') or 0

    # 行数上限：候选与精选一定留，剩下的按分数从高到低补，保证长尾不至于无限膨胀。
    keep = list(top3 | cand)
    rest = [(s.get('symbol') or '').lower() for s in scored
            if (s.get('symbol') or '').lower() not in (top3 | cand)]
    budget = max(0, snap_max_rows() - len(keep))
    chosen = set(keep) | set(rest[:budget])

    rows = []
    for i, s in enumerate(scored, 1):
        sym = (s.get('symbol') or '').lower()
        if not sym or sym not in chosen:
            continue
        stage = 'recommended' if sym in top3 else ('candidate' if sym in cand else 'scored')
        why = list(blocked.get(sym) or [])
        total = _f((s.get('score') or {}).get('total'))
        if not why and stage == 'scored' and total is not None and total < min_score:
            why = [f'低于门槛{int(min_score)}']
        rows.append(_row(s, date_key, batch, i, stage, why,
                         sources.get(sym), labels.get(sym) or {},
                         pool_info.get(sym) or {}, now_txt))

    if not rows:
        return 0

    close = conn is None
    conn = conn or _conn()
    try:
        ensure_tables(conn)
        # 同批次重跑（手动点"立即生成"）覆盖自己，不影响别的批次
        conn.execute('DELETE FROM strategy_snapshot WHERE snap_date=? AND batch=?',
                     (date_key, batch))
        conn.executemany('INSERT INTO strategy_snapshot (' + ','.join(_COLS) + ') VALUES (' +
                         ','.join('?' * len(_COLS)) + ')', rows)
        conn.execute('DELETE FROM strategy_snapshot_batch WHERE snap_date=? AND batch=?',
                     (date_key, batch))
        conn.executemany('INSERT INTO strategy_snapshot_batch (' + ','.join(_BC) + ') VALUES (' +
                         ','.join('?' * len(_BC)) + ')',
                         [_batch_row(payload, now_txt, {
                             'scanned': len(scored), 'scored': len(scored),
                             'candidates': len(cand), 'recommended': len(top3)})])
        prune_days = snap_retention_days()
        if prune_days > 0:
            cutoff = (now - timedelta(days=prune_days)).strftime('%Y-%m-%d')
            conn.execute('DELETE FROM strategy_snapshot WHERE snap_date<?', (cutoff,))
            conn.execute('DELETE FROM strategy_snapshot_batch WHERE snap_date<?', (cutoff,))
        conn.commit()
    finally:
        if close:
            conn.close()
    return len(rows)


def _conn():
    from db.database import get_db_conn
    return get_db_conn()


def load_snapshot(date_key: str, batch: Optional[str] = None,
                  stage: Optional[str] = None) -> List[Dict]:
    """读某天快照；batch 省略时取当天最后一个批次。"""
    conn = _conn()
    try:
        ensure_tables(conn)
        if not batch:
            batch = conn.execute('SELECT MAX(batch) FROM strategy_snapshot_batch '
                                 'WHERE snap_date=?', (date_key,)).fetchone()[0]
            if not batch:
                return []
        q = 'SELECT * FROM strategy_snapshot WHERE snap_date=? AND batch=?'
        args: list = [date_key, batch]
        if stage:
            q += ' AND stage=?'
            args.append(stage)
        q += ' ORDER BY rank'
        out = []
        for r in conn.execute(q, args).fetchall():
            d = dict(r)
            for k in ('factors', 'inputs'):
                try:
                    d[k] = json.loads(d[k]) if d[k] else {}
                except (TypeError, ValueError):
                    d[k] = {}
            out.append(d)
        return out
    finally:
        conn.close()


def list_batches(limit: int = 60) -> List[Dict]:
    """已攒了多少个决策现场（复盘前先确认数据在不在）。"""
    conn = _conn()
    try:
        ensure_tables(conn)
        return [dict(r) for r in conn.execute(
            'SELECT * FROM strategy_snapshot_batch ORDER BY snap_date DESC, batch DESC '
            'LIMIT ?', (limit,)).fetchall()]
    finally:
        conn.close()
