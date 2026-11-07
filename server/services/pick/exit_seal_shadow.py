# -*- coding: utf-8 -*-
"""exit_seal_shadow.py - 出口分叉影子（2026-11-05，`docs/影子实验台账.md` 第 15 条 · 只记录，不改任何动作）

一句话：**每天收盘后把当天推荐的三条出口结果各算一遍存进表里，线上出口文案、推送、门槛一字不动。**

依据（`analysis_out/sealed_board_selection_check.py` 的 L9，样本 2026-05-14~11-04、201 条可算推荐、
成本一律用"推荐当时的价"、双边费用 0.13%）：

| 出口 | 单笔均值 | 胜率 | 亏超 5% | t |
|---|---|---|---|---|
| ① 统一拿到次日开盘卖（=现状默认） | +0.23% | 53.7% | 9.0% | 1.22 |
| ② 全部当日收盘卖 | −0.26% | 49.3% | 2.0% | −1.10 |
| ③ **封住就拿隔夜、没封住当天收盘走** | **+0.68%** | **57.7%** | **4.5%** | **3.17** |

③ 为什么可执行：它**不预测未来**，只需要在尾盘看一眼"现在封没封住"——那是当场可观测的状态，
本地日K/盘口就够判，不需要封单、逐笔或分钟库。分腿也自洽：封住那批拿隔夜 +2.40%（改当天走只
剩 +0.16%），没封住那批拿隔夜 −1.10%（改当天走 −0.33%）。

口径边界（如实写在这里，别等裁决时才发现）：
  · **那 201 条是同一份数据里挑出来的规则**，t=3.17 只说明"不是噪声主导"，不等于外推成立——
    所以要前向影子。判据在 `evaluate()`：影子期净口径单笔均值 ≥ 现状 +0.30pp 且"亏超 5%"不升高，
    且 ≥20 个交易日，才提"升为线上默认出口"，否则延长或回退，**不降门槛**。
  · 成本按"推荐那一刻的涨幅"（`daily_recommendations.change_percent`）。**低吸单没成交的日子这一条
    偏乐观**（没成交就没有这笔收益），所以把 `buy_timing`/`push_chg_pct` 原文一起存，裁决时能分开算。
  · 封住状态两个来源：`seal_last_batch`＝当天最后一个批次快照的涨幅是否达涨停（**当时看得到的东西**，
    批次时间存 `seal_batch_time`），`seal_close`＝当日收盘涨幅是否达涨停（事后确定值）。
    线上真要执行应看 14:57 现场，本影子用"最后一批"（多数是 14:45）当代理，那 12 分钟的差就在
    这两个字段不等行的行里，逐条可查。
  · 隔夜腿 `next_open_ret_pct` 要等 T+1 的日K同步后才回填，没回填**留 NULL**（填 0 会把"还没数据"
    混成"平盘"，均值直接失真）。
  · 读写严格分段：先把日K/快照全读进内存并关掉连接，再开写连接即写即提交。11-03 那次"事务内等外部
    IO 锁死全库"的教训不能在这条新链路上重演，哪怕这里等的只是本地 532MB 的日K库。

开关：`EXIT_SEAL_SHADOW_ENABLED`（默认 True，只写这张新表）；配置页 `exit_seal_shadow_enabled` 可热改。
关掉时一个字段都不写，逐字节等于改造前。
"""
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional

FEE_PCT = 0.13          # 双边费用口径（与 rec_sim / 复盘一致）
KLINE_FROM = '2024-01-01'    # 取数下界：要完整历史才不会把窗口首根算成"无昨收"


def enabled() -> bool:
    """独立开关：先热读覆盖表，读不到退回 config 默认。"""
    try:
        from services.support import app_settings
        v = app_settings.value('exit_seal_shadow_enabled')
        if v is not None:
            return bool(v)
    except Exception:
        pass
    from app.config import EXIT_SEAL_SHADOW_ENABLED
    return bool(EXIT_SEAL_SHADOW_ENABLED)


def limit_pct(code) -> float:
    """涨停幅度：创业/科创 20%、北交所 30%、其余主板 10%。本系统范围只有沪深主板，
    仍按板块分支是为了将来范围变了不用改这里。"""
    c = str(code)[-6:]
    if c.startswith(('30', '68')):
        return 20.0
    if c.startswith(('43', '83', '87', '92')):
        return 30.0
    return 10.0


def close_ret_pct(chg_close_pct: Optional[float], chg_push_pct: Optional[float]) -> Optional[float]:
    """推荐价 → 当日收盘 的收益（百分数）。

    两个输入都是"相对昨收的涨幅%"，所以 `(1+收盘涨)/(1+推荐时涨)-1` 就是推荐价到收盘的涨跌，
    **不涉及复权/未复权混算**（日K 存前复权价，`daily_recommendations.price` 是当时的原始价，
    两者直接相除会把复权因子算进去，那是错的；用涨幅比值就绕开了）。
    """
    a, b = _f(chg_close_pct), _f(chg_push_pct)
    if a is None or b is None or 1 + b / 100.0 == 0:
        return None
    return ((1 + a / 100.0) / (1 + b / 100.0) - 1) * 100.0


def next_open_ret_pct(chg_close_pct: Optional[float], chg_push_pct: Optional[float],
                      gap_pct: Optional[float]) -> Optional[float]:
    """推荐价 → 次日开盘 的收益（百分数）＝"拿到隔夜"那条腿。gap_pct＝次日开盘相对当日收盘的涨幅%。"""
    base = close_ret_pct(chg_close_pct, chg_push_pct)
    g = _f(gap_pct)
    if base is None or g is None:
        return None
    return ((1 + base / 100.0) * (1 + g / 100.0) - 1) * 100.0


def is_sealed(chg_pct: Optional[float], code) -> Optional[int]:
    """涨幅是否达涨停（0.985 容差，与 `kline_forward` 同一判据）。缺数据返回 None，**不当 0**。"""
    v = _f(chg_pct)
    if v is None:
        return None
    return 1 if v >= limit_pct(code) * 0.985 else 0


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ensure_table() -> bool:
    try:
        from db.database import get_db_conn
        conn = get_db_conn(wait_for_lock=True)
        try:
            conn.execute('''CREATE TABLE IF NOT EXISTS exit_seal_shadow (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rec_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT DEFAULT '',
                score REAL,
                push_price REAL,
                push_chg_pct REAL,
                buy_timing TEXT DEFAULT '',
                batch_ref TEXT DEFAULT '',
                seal_last_batch INTEGER,
                seal_batch_time TEXT DEFAULT '',
                seal_close INTEGER,
                close_ret_pct REAL,
                next_open_ret_pct REAL,
                next_open_date TEXT DEFAULT '',
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(rec_date, symbol)
            )''')
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        print(f'[出口分叉影子] 建表失败: {type(e).__name__}: {e}')
        return False


# ── 只读段：一次把主库和日K库要的东西都读进内存，读完就关连接 ──────────────────
def _read_recs(conn, rec_date: str) -> List[Dict]:
    cols = ['rec_date', 'symbol', 'name', 'price', 'change_percent', 'buy_timing', 'batch_ref', 'score']
    return [dict(zip(cols, row)) for row in conn.execute(
        'SELECT rec_date, symbol, name, price, change_percent, buy_timing, batch_ref, score '
        'FROM daily_recommendations WHERE rec_date=? ORDER BY sort_order', (rec_date,)).fetchall()]


def _read_seal_batch(conn, rec_date: str) -> Dict[str, tuple]:
    """symbol → (batch, change_percent)：当天**最后一个批次**的那一眼（可执行判据的代理）。"""
    out: Dict[str, tuple] = {}
    for sym, batch, chg in conn.execute(
            'SELECT symbol, batch, change_percent FROM strategy_snapshot WHERE snap_date=?',
            (rec_date,)).fetchall():
        k = str(sym)
        if k not in out or str(batch) > str(out[k][0]):
            out[k] = (batch, chg)
    return out


def _read_pending(conn) -> List[tuple]:
    """需要自愈的行：收盘腿或隔夜腿还是 NULL 的（表还没建时返回空，不报错）。

    为什么必须有这一步：本地日K是 18:15 才同步的，万一那天同步晚了、或推荐当天进程没起来，
    行就会带着 NULL 落库；只补"最新 rec_date"的话那些行永远不动。这里把
    "收盘腿缺"和"隔夜腿缺"都捞出来，K 线到了就补，没到的继续留 NULL。
    """
    try:
        return conn.execute(
            'SELECT rec_date, symbol, push_chg_pct, close_ret_pct, next_open_ret_pct '
            'FROM exit_seal_shadow WHERE close_ret_pct IS NULL OR next_open_ret_pct IS NULL').fetchall()
    except Exception:
        return []


def _read_bars(codes: List[str]) -> Dict[str, List[tuple]]:
    """裸码 → 升序 [(date, open, close, prev_close)]。日K库只读打开（mode=ro）。"""
    if not codes:
        return {}
    from app.config import KLINE_HISTORY_DB_PATH
    raw: Dict[str, List[tuple]] = {}
    conn = sqlite3.connect(f'file:{KLINE_HISTORY_DB_PATH}?mode=ro', uri=True, timeout=30)
    try:
        ph = ','.join('?' * len(codes))
        for code, date, o, c in conn.execute(
                f'SELECT symbol, date, open, close FROM stock_daily '
                f'WHERE symbol IN ({ph}) AND date>=? ORDER BY symbol, date',
                tuple(codes) + (KLINE_FROM,)).fetchall():
            raw.setdefault(str(code), []).append((date, o, c))
    except Exception as e:
        print(f'[出口分叉影子] 日K读取失败: {type(e).__name__}: {e}')
    finally:
        conn.close()
    out: Dict[str, List[tuple]] = {}
    for code, seq in raw.items():
        prev = None
        for date, o, c in seq:
            out.setdefault(code, []).append((date, o, c, prev))
            prev = c
    return out


def _day_of(bars: Dict[str, List[tuple]], code: str, date: str):
    for row in bars.get(code, []):
        if row[0] == date:
            return row
    return None


def _chg_pct(day) -> Optional[float]:
    """当日收盘相对昨收的涨幅%（前复权价相除，同一天内比例不受复权因子影响）。"""
    if not day or not day[3]:
        return None
    return (day[2] / day[3] - 1) * 100.0


def build_rows(rec_date: str, recs: List[Dict], seal_batch: Dict[str, tuple],
               bars: Dict[str, List[tuple]]) -> List[Dict]:
    """纯计算：把当天的推荐算成待落库的行（无 IO，自测直接打这里）。"""
    rows = []
    for x in recs:
        code = str(x['symbol'])[-6:]
        day = _day_of(bars, code, rec_date)
        push_chg = _f(x.get('change_percent'))
        chg_close = _chg_pct(day)
        sb = seal_batch.get(str(x['symbol']))
        rows.append({
            'rec_date': rec_date,
            'symbol': str(x['symbol']),
            'name': x.get('name') or '',
            'score': _f(x.get('score')),
            'push_price': _f(x.get('price')),
            'push_chg_pct': push_chg,
            'buy_timing': str(x.get('buy_timing') or ''),
            'batch_ref': str(x.get('batch_ref') or ''),
            'seal_last_batch': is_sealed(_f(sb[1]) if sb else None, code),
            'seal_batch_time': str(sb[0]) if sb else '',
            'seal_close': is_sealed(chg_close, code),
            'close_ret_pct': close_ret_pct(chg_close, push_chg),
            'next_open_ret_pct': None,
            'next_open_date': '',
            'note': '' if day else '当日K未同步（明天自愈回补）',
        })
    return rows


def build_heals(pending: List[tuple], bars: Dict[str, List[tuple]]) -> List[Dict]:
    """纯计算：把"收盘腿或隔夜腿还是 NULL"的行能补的补上（K 线还没到就继续留 NULL）。

    pending 行形状 = (rec_date, symbol, push_chg_pct, close_ret_pct, next_open_ret_pct)，
    后两列只用来判断"哪条腿还缺"，不参与计算。
    """
    out = []
    for rec_date, symbol, push_chg, have_close, have_open in pending:
        code = str(symbol)[-6:]
        seq = bars.get(code, [])
        idx = next((i for i, r in enumerate(seq) if r[0] == rec_date), None)
        if idx is None:
            continue
        day = seq[idx]
        chg_close = _chg_pct(day)
        heal = {'rec_date': rec_date, 'symbol': str(symbol),
                'close_ret_pct': None if have_close is not None else close_ret_pct(chg_close, push_chg),
                'seal_close': None if have_close is not None else is_sealed(chg_close, code),
                'next_open_ret_pct': None, 'next_open_date': ''}
        if have_open is None and idx + 1 < len(seq) and day[2]:
            nxt = seq[idx + 1]
            gap = (nxt[1] / day[2] - 1) * 100.0
            nr = next_open_ret_pct(chg_close, push_chg, gap)
            if nr is not None:
                heal['next_open_ret_pct'] = nr
                heal['next_open_date'] = nxt[0]
        if any(heal[k] is not None for k in ('close_ret_pct', 'seal_close', 'next_open_ret_pct')):
            out.append(heal)
    return out


# ── 写入段：拿到算好的行再开写连接，即写即提交 ────────────────────────────────
def _write_rows(conn, rows: List[Dict], ts: str) -> int:
    n = 0
    for r in rows:
        try:
            conn.execute('''INSERT INTO exit_seal_shadow (
                    rec_date, symbol, name, score, push_price, push_chg_pct, buy_timing, batch_ref,
                    seal_last_batch, seal_batch_time, seal_close, close_ret_pct, next_open_ret_pct,
                    next_open_date, note, created_at, updated_at)
                VALUES (:rec_date,:symbol,:name,:score,:push_price,:push_chg_pct,:buy_timing,:batch_ref,
                        :seal_last_batch,:seal_batch_time,:seal_close,:close_ret_pct,NULL,:next_open_date,
                        :note,:ts,:ts)
                ON CONFLICT(rec_date, symbol) DO UPDATE SET
                    name=excluded.name, score=excluded.score, push_price=excluded.push_price,
                    push_chg_pct=excluded.push_chg_pct, buy_timing=excluded.buy_timing,
                    batch_ref=excluded.batch_ref, seal_last_batch=excluded.seal_last_batch,
                    seal_batch_time=excluded.seal_batch_time, seal_close=excluded.seal_close,
                    close_ret_pct=excluded.close_ret_pct, note=excluded.note,
                    updated_at=excluded.updated_at''',   # 重跑覆盖当天行，但**不碰已回填的隔夜腿**
                dict(r, ts=ts))
            n += 1
        except Exception as e:
            print(f'[出口分叉影子] 写 {r["rec_date"]} {r["symbol"]} 失败: {type(e).__name__}: {e}')
    return n


def _write_heals(conn, heals: List[Dict], ts: str) -> int:
    """回填两条腿。用 COALESCE 所以"这次算不出来"绝不会把已有值抹成 NULL。"""
    n = 0
    for h in heals:
        try:
            conn.execute('''UPDATE exit_seal_shadow
                    SET close_ret_pct=COALESCE(:close_ret_pct, close_ret_pct),
                        seal_close=COALESCE(:seal_close, seal_close),
                        next_open_ret_pct=COALESCE(:next_open_ret_pct, next_open_ret_pct),
                        next_open_date=CASE WHEN :next_open_ret_pct IS NULL THEN next_open_date
                                       ELSE :next_open_date END,
                        note=CASE WHEN close_ret_pct IS NULL AND :close_ret_pct IS NOT NULL
                                  THEN '' ELSE note END,
                        updated_at=:ts
                WHERE rec_date=:rec_date AND symbol=:symbol''', dict(h, ts=ts))
            n += 1
        except Exception as e:
            print(f'[出口分叉影子] 回补 {h["rec_date"]} {h["symbol"]} 失败: {type(e).__name__}')
    return n


def run_daily(rec_date: Optional[str] = None, now: Optional[datetime] = None) -> Dict:
    """落当天推荐的两条出口 + 回补历史行的隔夜腿。异常只打印不抛，不拖垮调度里的其他任务。"""
    if not enabled():
        return {'enabled': False, 'written': 0, 'backfilled': 0}
    now = now or datetime.now()
    ts = now.strftime('%Y-%m-%d %H:%M:%S')
    from db.database import get_db_conn
    try:
        if not _ensure_table():
            return {'enabled': True, 'written': 0, 'error': '建表失败'}
        # ① 只读
        conn = get_db_conn()
        try:
            if not rec_date:
                rec_date = (conn.execute('SELECT MAX(rec_date) FROM daily_recommendations')
                            .fetchone()[0] or '')
            if not rec_date:
                return {'enabled': True, 'written': 0, 'error': '没有推荐记录'}
            recs = _read_recs(conn, rec_date)
            seal_batch = _read_seal_batch(conn, rec_date)
            pending = _read_pending(conn)
        finally:
            conn.close()
        codes = sorted({str(x['symbol'])[-6:] for x in recs}
                       | {str(p[1])[-6:] for p in pending})
        bars = _read_bars(codes)
        # ② 纯算
        rows = build_rows(rec_date, recs, seal_batch, bars)
        heals = build_heals(pending, bars)
        # ③ 即写即提交
        conn = get_db_conn()
        try:
            written = _write_rows(conn, rows, ts)
            conn.commit()
            healed = _write_heals(conn, heals, ts)
            conn.commit()
        finally:
            conn.close()
        sealed_n = sum(1 for r in rows if r['seal_last_batch'] == 1)
        no_k = sum(1 for r in rows if r['note'])
        uniq = len({r['symbol'] for r in rows})
        print(f'[出口分叉影子] {rec_date} 落库 {uniq} 只（写 {written} 行：同日同票多批会覆盖同一行，'
              f'幂等键 rec_date+symbol）、批内判据封住 {sealed_n}、缺当日K {no_k}、'
              f'回补两条腿 {healed} 行')
        return {'enabled': True, 'written': written, 'symbols': uniq, 'healed': healed,
                'rec_date': rec_date, 'sealed': sealed_n, 'no_kline': no_k}
    except Exception as e:
        print(f'[出口分叉影子] 本轮失败: {type(e).__name__}: {e}')
        return {'enabled': True, 'written': 0, 'error': f'{type(e).__name__}: {e}'}


def evaluate(min_days: int = 20) -> Dict:
    """裁决用的只读汇总：三条出口各自的净口径单笔均值/胜率/亏超5% 与样本天数。
    天数不足直接 `ready=False`——本仓规矩，影子没到期就写"未裁决"。"""
    from db.database import get_db_conn
    conn = get_db_conn()
    out: Dict = {'ready': False, 'days': 0, 'rows': 0, 'legs': {},
                 'min_days': min_days, 'rule': '净口径 ≥ 现状+0.30pp 且亏超5%不升高'}
    try:
        sqls = (
            ('① 拿到次日开盘（现状默认）',
             'SELECT next_open_ret_pct FROM exit_seal_shadow WHERE next_open_ret_pct IS NOT NULL'),
            ('② 当日收盘走',
             'SELECT close_ret_pct FROM exit_seal_shadow WHERE close_ret_pct IS NOT NULL'),
            ('③ 分叉：封住拿隔夜/没封住当天走',
             'SELECT CASE WHEN seal_last_batch=1 THEN next_open_ret_pct ELSE close_ret_pct END '
             'FROM exit_seal_shadow WHERE close_ret_pct IS NOT NULL '
             'AND (seal_last_batch<>1 OR next_open_ret_pct IS NOT NULL)'),
        )
        for leg, sql in sqls:
            vals = [float(r[0]) for r in conn.execute(sql).fetchall() if r[0] is not None]
            if not vals:
                continue
            n = len(vals)
            mean = sum(vals) / n
            net = [v - FEE_PCT for v in vals]
            var = sum((v - mean) ** 2 for v in vals) / (n - 1) if n > 1 else 0.0
            se = (var / n) ** 0.5 if var else 0.0
            net_mean = mean - FEE_PCT
            out['legs'][leg] = {'n': n, 'net_mean_pct': round(net_mean, 3),
                                'win_pct': round(sum(1 for v in net if v > 0) / n * 100, 1),
                                'bad5_pct': round(sum(1 for v in net if v <= -5) / n * 100, 1),
                                'se_pct': round(se, 3),
                                't': round(net_mean / se, 2) if se else None}
        r = conn.execute('SELECT COUNT(DISTINCT rec_date), COUNT(*) FROM exit_seal_shadow').fetchone()
        out['days'], out['rows'] = int(r[0] or 0), int(r[1] or 0)
        out['ready'] = out['days'] >= min_days and bool(out['legs'])
    except Exception as e:
        out['error'] = f'{type(e).__name__}: {e}'
    finally:
        conn.close()
    return out
