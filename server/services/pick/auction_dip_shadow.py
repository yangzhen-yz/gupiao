# -*- coding: utf-8 -*-
"""auction_dip_shadow.py - 竞价低吸臂影子（2026-11-07，`docs/影子实验台账.md` 第 20 条 · 只记录，不改任何动作）

一句话：**每个交易日 9:31 把"昨日涨停池 × 今日竞价快照"按竞价缺口分档落库，收盘后回填
"竞价买→收盘卖 / 拿隔夜 / 次日开盘卖"三条腿，线上评分、门槛、推送一字不动。**

为什么建这条臂（数字都是 11-07 当天实测，探针脚本在 `analysis_out/`、不入库）：
  · 线上 251 条推荐里 **46% 在生成时已涨 ≥9.5%**——分数只在涨起来之后才响，次日开盘买
    等于赌延续溢价：净口径 −0.71%、对同日等权超额 −0.64pp（t=−1.98）。
  · 影子实验室（22,095 行 / 19 天）里唯一稳的正超额在封板族：一封到底隔夜 +2.80%（t=4.94）；
    但早板没炸的日内腿 −1.43%（t=−3.62）——溢价在隔夜腿，不在日内腿。
  · 反过来，"**昨日涨停 + 今日竞价深低开（>3%）**"的日内腿（开盘→收盘）**+1.36%（t=1.98、
    7/10 天正号）**，竞价 8 天样本 +1.87%——这是"低吸代替追涨"的候选入口，本影子就是它的前向验证。
  · 高开追涨（gap≥7%）隔夜 **−2.61%（t=−4.61）**："不追高开"这条止血规则的反事实也一起记
    （high/chase 档同样落库回填），裁决时直接回答"不追省了多少"，不用再另起一轮影子。

口径边界（如实写在这里，别等裁决时才发现）：
  · 样本内 t=1.98 是**同一份数据里挑出来的规则**，不够上线——所以跑前向影子。判据预注册在
    `evaluate()`：≥20 个交易日、深低开档（可成交）对同日范围等权超额 ≥+0.5pp、按日聚类 t≥2、
    ≥2/3 的天数正号，四条全过才提"接进动作链"，否则延长或回退，**不降门槛**。
    胜率只展示不进判据（实测最高胜率档 61.3% 反而亏钱，目标是单笔期望不是胜率）。
  · 入场价＝9:25 竞价撮合价（`auction_price`），不是 9:30 后的现价；`buyable`（gap<7%，
    保守可成交）与 `one_word`（gap≥涨停幅×0.985，一字板）分开标，裁决时含/不含 buyable 各算一遍。
  · 三条腿都用**涨幅比值**算，不拿原始价直接除前复权价（与 exit_seal_shadow 同一避坑法）：
    日内＝(1+收盘涨幅)/(1+竞价缺口)−1；隔夜＝T+1 开盘/当日收盘−1（相邻两根K线比值）；
    合计＝(1+日内)×(1+隔夜)−1。
  · 哪条腿算不出来就**留 NULL，绝不填 0**（填 0 会把"还没数据"混成"平盘"，均值直接失真）；
    晚间自愈能补的补、补不了的继续 NULL。
  · 基准＝同日 `auction_snapshot` 范围（主板非ST市值≥35亿，≈2464 只）的 open→close 等权，
    与臂同一入场/出场时点，分母是用户真能操作的范围；逐日记在 `bench_intraday_pct`。
  · 读写严格分段：先把池子/竞价/日K全读进内存并关连接，再开写连接即写即提交——
    11-03"事务内等外部 IO 锁死全库"的教训不在这条新链路上重演。
  · 样本钟是第 19 条的**例外**（同第 17 条炸板影子）：对照的是"涨停池×竞价快照"，跟当天
    有没有推荐批次无关，数"有可比行的日子"；硬套统一口径会把推荐链冻结但竞价正常的日子误判空日。

开关：`AUCTION_DIP_SHADOW_ENABLED`（默认 True，只写这张新表）；配置页 `auction_dip_shadow_enabled` 可热改。
关掉时一个字段都不写，逐字节等于改造前。
"""
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

FEE_PCT = 0.13            # 双边费用口径（与 rec_sim / 复盘 / exit_seal 一致）
BUYABLE_GAP_MAX = 7.0     # 保守可成交上界：gap≥7% 常买不到竞价价（与 analysis_out 探针同口径）
KLINE_PAD_DAYS = 20       # 取K线前后各垫 20 个自然日，跨节假日也够得着昨收与 T+1
ARMS = ('deep_dip', 'mild_dip', 'flat', 'high', 'chase')
# 判据预注册（裁决时不许事后改门槛）
MIN_DAYS = 20
MIN_EXCESS_PP = 0.5       # 深低开档（可成交）日内净均值 − 同日范围等权，≥ 此值才算有超额
MIN_T = 2.0               # 按日聚类 t（每天一个点，不是每笔一个点）
MIN_POS_RATIO = 2.0 / 3.0  # 正号天数占比


def enabled() -> bool:
    """独立开关：先热读覆盖表，读不到退回 config 默认。"""
    try:
        from services.support import app_settings
        v = app_settings.value('auction_dip_shadow_enabled')
        if v is not None:
            return bool(v)
    except Exception:
        pass
    from app.config import AUCTION_DIP_SHADOW_ENABLED
    return bool(AUCTION_DIP_SHADOW_ENABLED)


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def arm_of(gap_pct) -> Optional[str]:
    """竞价缺口 → 档位。边界：≤−3 深低开 / (−3,0] 浅低开 / (0,3] 平开 / (3,4) 高开 / ≥4 追涨。
    缺数据返回 None，**不猜档**。"""
    g = _f(gap_pct)
    if g is None:
        return None
    if g <= -3.0:
        return 'deep_dip'
    if g <= 0.0:
        return 'mild_dip'
    if g <= 3.0:
        return 'flat'
    if g < 4.0:
        return 'high'
    return 'chase'


def limit_pct(code) -> float:
    """涨停幅度：创业/科创 20%、北交所 30%、其余主板 10%（与 exit_seal_shadow 同判据）。"""
    c = str(code)[-6:]
    if c.startswith(('30', '68')):
        return 20.0
    if c.startswith(('43', '83', '87', '92')):
        return 30.0
    return 10.0


def is_buyable(gap_pct) -> Optional[int]:
    """保守可成交标记：gap<7% 记 1。缺数据 None，不当 0。"""
    g = _f(gap_pct)
    return None if g is None else (1 if g < BUYABLE_GAP_MAX else 0)


def is_one_word(gap_pct, code) -> Optional[int]:
    """一字板标记：竞价缺口达涨停幅×0.985（与 kline_forward 同容差）。"""
    g = _f(gap_pct)
    return None if g is None else (1 if g >= limit_pct(code) * 0.985 else 0)


def is_sealed(chg_pct: Optional[float], code) -> Optional[int]:
    v = _f(chg_pct)
    return None if v is None else (1 if v >= limit_pct(code) * 0.985 else 0)


def is_dt_close(chg_pct: Optional[float], code) -> Optional[int]:
    """收盘封跌停＝收盘卖不出去（卖点没有对手盘），执行性审查要用。"""
    v = _f(chg_pct)
    return None if v is None else (1 if v <= -limit_pct(code) * 0.985 else 0)


def intraday_ret_pct(close_chg_pct: Optional[float], gap_pct: Optional[float]) -> Optional[float]:
    """竞价价 → 当日收盘（%）。两个输入都是"相对昨收的涨幅%"，比值相除绕开复权因子
    （与 exit_seal_shadow.close_ret_pct 同一手法）。gap=−100 这种坏值直接 None。"""
    a, b = _f(close_chg_pct), _f(gap_pct)
    if a is None or b is None or 1 + b / 100.0 == 0:
        return None
    return ((1 + a / 100.0) / (1 + b / 100.0) - 1) * 100.0


def compose_total(intraday_pct: Optional[float], overnight_pct: Optional[float]) -> Optional[float]:
    """合计腿＝(1+日内)×(1+隔夜)−1；任一腿 NULL 则合计 NULL。"""
    a, b = _f(intraday_pct), _f(overnight_pct)
    if a is None or b is None:
        return None
    return ((1 + a / 100.0) * (1 + b / 100.0) - 1) * 100.0


_DDL = '''CREATE TABLE IF NOT EXISTS auction_dip_shadow (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pool_date TEXT NOT NULL,
    prev_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT DEFAULT '',
    arm TEXT NOT NULL,
    gap_pct REAL,
    auction_price REAL,
    auction_amount REAL,
    bid1 REAL,
    lianban INTEGER,
    zhaban INTEGER,
    first_seal TEXT,
    seal_fund REAL,
    turnover REAL,
    buyable INTEGER,
    one_word INTEGER,
    close_ret_pct REAL,
    sealed_close INTEGER,
    dt_close INTEGER,
    intraday_ret_pct REAL,
    next_open_ret_pct REAL,
    total_open_ret_pct REAL,
    next_date TEXT DEFAULT '',
    bench_intraday_pct REAL,
    bench_close_pct REAL,
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(pool_date, symbol)
)'''


def _ensure_table() -> bool:
    try:
        from db.database import get_db_conn
        conn = get_db_conn(wait_for_lock=True)
        try:
            conn.execute(_DDL)
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        print(f'[竞价低吸影子] 建表失败: {type(e).__name__}: {e}')
        return False


# ── 只读段 ────────────────────────────────────────────────────────────────────
def _read_zt(conn, today: str) -> Tuple[str, List[Dict]]:
    """today 之前最近一个有涨停池的日子（自然处理周末/节假日/停调缺日）。"""
    prev = conn.execute(
        "SELECT MAX(trade_date) FROM limit_pool_daily WHERE pool='zt' AND trade_date<?",
        (today,)).fetchone()[0]
    if not prev:
        return '', []
    cols = ['symbol', 'name', 'lianban', 'zhaban', 'first_seal', 'seal_fund', 'turnover']
    rows = [dict(zip(cols, r)) for r in conn.execute(
        'SELECT symbol, name, lianban, zhaban, first_seal, seal_fund, turnover '
        'FROM limit_pool_daily WHERE trade_date=? AND pool=?', (prev, 'zt')).fetchall()]
    return str(prev), rows


def _read_auction(conn, day: str) -> List[Dict]:
    """某天竞价快照；表还没建（当天没采过）返回空，不报错。"""
    try:
        cols = ['symbol', 'name', 'gap_pct', 'auction_price', 'amount', 'bid1']
        return [dict(zip(cols, r)) for r in conn.execute(
            'SELECT symbol, name, gap_pct, auction_price, amount, bid1 '
            'FROM auction_snapshot WHERE trade_date=?', (day,)).fetchall()]
    except Exception:
        return []


def _read_pending(conn) -> List[tuple]:
    """三条腿或基准还缺的行（表没建时返回空）。形状：
    (pool_date, symbol, gap_pct, intraday_ret_pct, next_open_ret_pct, total_open_ret_pct, bench_intraday_pct)
    后四列既判"哪条还缺"，也供合计腿复用已填的日内值。"""
    try:
        return conn.execute(
            'SELECT pool_date, symbol, gap_pct, intraday_ret_pct, next_open_ret_pct, '
            'total_open_ret_pct, bench_intraday_pct FROM auction_dip_shadow '
            'WHERE intraday_ret_pct IS NULL OR next_open_ret_pct IS NULL '
            'OR total_open_ret_pct IS NULL OR bench_intraday_pct IS NULL').fetchall()
    except Exception:
        return []


def _read_bars(codes: List[str], d0: str, d1: str) -> Dict[str, List[tuple]]:
    """裸码 → 升序 [(date, open, close, prev_close)]。日K库只读（mode=ro），
    500 只一批走 (symbol,date) 索引，不做全表 date 扫描。"""
    if not codes:
        return {}
    from app.config import KLINE_HISTORY_DB_PATH
    raw: Dict[str, List[tuple]] = {}
    conn = sqlite3.connect(f'file:{KLINE_HISTORY_DB_PATH}?mode=ro', uri=True, timeout=30)
    try:
        for i in range(0, len(codes), 500):
            chunk = codes[i:i + 500]
            ph = ','.join('?' * len(chunk))
            for code, date, o, c in conn.execute(
                    f'SELECT symbol, date, open, close FROM stock_daily '
                    f'WHERE symbol IN ({ph}) AND date>=? AND date<=? ORDER BY symbol, date',
                    tuple(chunk) + (d0, d1)).fetchall():
                raw.setdefault(str(code), []).append((date, o, c))
    except Exception as e:
        print(f'[竞价低吸影子] 日K读取失败: {type(e).__name__}: {e}')
    finally:
        conn.close()
    out: Dict[str, List[tuple]] = {}
    for code, seq in raw.items():
        prev = None
        for date, o, c in seq:
            out.setdefault(code, []).append((date, o, c, prev))
            prev = c
    return out


# ── 纯计算段（无 IO，自测直接打这里）──────────────────────────────────────────
def build_rows(pool_date: str, prev_date: str, zt_rows: List[Dict],
               auction_rows: List[Dict]) -> List[Dict]:
    """昨日涨停池 ∩ 今日竞价快照 → 待落库行。不在快照里的（范围外/采集漏）不写。"""
    auc: Dict[str, Dict] = {}
    for a in auction_rows:
        code = str(a.get('symbol') or '')[-6:]
        if len(code) == 6:
            auc[code] = a
    rows = []
    for z in zt_rows:
        code = str(z.get('symbol') or '')[-6:]
        a = auc.get(code)
        if a is None:
            continue
        gap = _f(a.get('gap_pct'))
        arm = arm_of(gap)
        if arm is None:
            continue
        rows.append({
            'pool_date': pool_date, 'prev_date': prev_date, 'symbol': code,
            'name': str(a.get('name') or z.get('name') or ''),
            'arm': arm, 'gap_pct': gap,
            'auction_price': _f(a.get('auction_price')),
            'auction_amount': _f(a.get('amount')), 'bid1': _f(a.get('bid1')),
            'lianban': z.get('lianban'), 'zhaban': z.get('zhaban'),
            'first_seal': str(z.get('first_seal') or ''),
            'seal_fund': _f(z.get('seal_fund')), 'turnover': _f(z.get('turnover')),
            'buyable': is_buyable(gap), 'one_word': is_one_word(gap, code),
            'note': '',
        })
    return rows


def _day_index(seq: List[tuple], date: str) -> Optional[int]:
    for i, r in enumerate(seq):
        if r[0] == date:
            return i
    return None


def build_heals(pending: List[tuple], bars_by_date: Dict[str, Dict[str, List[tuple]]],
                universe_by_date: Dict[str, List[str]]) -> List[Dict]:
    """能补的补上，补不了的继续 NULL。基准按"同日竞价范围等权"逐日算一次，记到该日每行。"""
    out = []
    by_date: Dict[str, List[tuple]] = {}
    for p in pending:
        by_date.setdefault(str(p[0]), []).append(p)
    for day, plist in by_date.items():
        bars = bars_by_date.get(day, {})
        # 基准：范围内每只 open→close（同一根K线内比值，不受复权影响）与 昨收→close
        bi, bc = [], []
        for code in universe_by_date.get(day, []):
            idx = _day_index(bars.get(code, []), day)
            if idx is None:
                continue
            _, o, c, prev = bars[code][idx]
            if o and c and o > 0 and c > 0:
                bi.append((c / o - 1) * 100.0)
            if prev and c and prev > 0 and c > 0:
                bc.append((c / prev - 1) * 100.0)
        bench_i = sum(bi) / len(bi) if bi else None
        bench_c = sum(bc) / len(bc) if bc else None
        for (_, symbol, gap, have_i, have_n, have_t, have_b) in plist:
            code = str(symbol)[-6:]
            seq = bars.get(code, [])
            idx = _day_index(seq, day)
            heal = {'pool_date': day, 'symbol': code,
                    'close_ret_pct': None, 'sealed_close': None, 'dt_close': None,
                    'intraday_ret_pct': None, 'next_open_ret_pct': None,
                    'total_open_ret_pct': None, 'next_date': '',
                    'bench_intraday_pct': None if have_b is not None else bench_i,
                    'bench_close_pct': None if have_b is not None else bench_c}
            if idx is not None:
                _, o, c, prev = seq[idx]
                chg = (c / prev - 1) * 100.0 if (prev and c and prev > 0) else None
                if have_i is None:
                    heal['close_ret_pct'] = chg
                    heal['sealed_close'] = is_sealed(chg, code)
                    heal['dt_close'] = is_dt_close(chg, code)
                    heal['intraday_ret_pct'] = intraday_ret_pct(chg, gap)
                base_i = have_i if have_i is not None else heal['intraday_ret_pct']
                if have_n is None and idx + 1 < len(seq) and c and c > 0:
                    nxt = seq[idx + 1]
                    if nxt[1] and nxt[1] > 0:
                        nr = (nxt[1] / c - 1) * 100.0
                        heal['next_open_ret_pct'] = nr
                        heal['next_date'] = nxt[0]
                        if have_t is None:
                            heal['total_open_ret_pct'] = compose_total(base_i, nr)
            if any(heal[k] is not None for k in
                   ('close_ret_pct', 'intraday_ret_pct', 'next_open_ret_pct',
                    'total_open_ret_pct', 'bench_intraday_pct')):
                out.append(heal)
    return out


def cluster_stats(daily_vals: List[float]) -> Dict:
    """按日聚类的均值/t/正号天数（每天一个点，笔数再多也不许把 t 算大）。"""
    n = len(daily_vals)
    if n == 0:
        return {'days': 0, 'mean': None, 't': None, 'pos_days': 0, 'pos_ratio': None}
    mean = sum(daily_vals) / n
    var = sum((v - mean) ** 2 for v in daily_vals) / (n - 1) if n > 1 else 0.0
    se = (var / n) ** 0.5 if var else 0.0
    pos = sum(1 for v in daily_vals if v > 0)
    return {'days': n, 'mean': round(mean, 3),
            't': round(mean / se, 2) if se else None,
            'pos_days': pos, 'pos_ratio': round(pos / n, 3)}


# ── 写入段：拿到算好的行再开写连接，即写即提交 ────────────────────────────────
def _write_rows(conn, rows: List[Dict], ts: str) -> int:
    n = 0
    for r in rows:
        try:
            conn.execute('''INSERT INTO auction_dip_shadow (
                    pool_date, prev_date, symbol, name, arm, gap_pct, auction_price,
                    auction_amount, bid1, lianban, zhaban, first_seal, seal_fund, turnover,
                    buyable, one_word, note, created_at, updated_at)
                VALUES (:pool_date,:prev_date,:symbol,:name,:arm,:gap_pct,:auction_price,
                        :auction_amount,:bid1,:lianban,:zhaban,:first_seal,:seal_fund,:turnover,
                        :buyable,:one_word,:note,:ts,:ts)
                ON CONFLICT(pool_date, symbol) DO UPDATE SET
                    prev_date=excluded.prev_date, name=excluded.name, arm=excluded.arm,
                    gap_pct=excluded.gap_pct, auction_price=excluded.auction_price,
                    auction_amount=excluded.auction_amount, bid1=excluded.bid1,
                    lianban=excluded.lianban, zhaban=excluded.zhaban,
                    first_seal=excluded.first_seal, seal_fund=excluded.seal_fund,
                    turnover=excluded.turnover, buyable=excluded.buyable,
                    one_word=excluded.one_word, updated_at=excluded.updated_at''',
                dict(r, ts=ts))   # 重跑覆盖判定日字段，**不碰已回填的腿与基准**
            n += 1
        except Exception as e:
            print(f'[竞价低吸影子] 写 {r["pool_date"]} {r["symbol"]} 失败: {type(e).__name__}: {e}')
    return n


def _write_heals(conn, heals: List[Dict], ts: str) -> int:
    """COALESCE 回填：这次算不出来绝不会把已有值抹成 NULL。"""
    n = 0
    for h in heals:
        try:
            conn.execute('''UPDATE auction_dip_shadow
                    SET close_ret_pct=COALESCE(:close_ret_pct, close_ret_pct),
                        sealed_close=COALESCE(:sealed_close, sealed_close),
                        dt_close=COALESCE(:dt_close, dt_close),
                        intraday_ret_pct=COALESCE(:intraday_ret_pct, intraday_ret_pct),
                        next_open_ret_pct=COALESCE(:next_open_ret_pct, next_open_ret_pct),
                        total_open_ret_pct=COALESCE(:total_open_ret_pct, total_open_ret_pct),
                        next_date=CASE WHEN :next_open_ret_pct IS NULL THEN next_date
                                       ELSE :next_date END,
                        bench_intraday_pct=COALESCE(:bench_intraday_pct, bench_intraday_pct),
                        bench_close_pct=COALESCE(:bench_close_pct, bench_close_pct),
                        updated_at=:ts
                WHERE pool_date=:pool_date AND symbol=:symbol''', dict(h, ts=ts))
            n += 1
        except Exception as e:
            print(f'[竞价低吸影子] 回补 {h["pool_date"]} {h["symbol"]} 失败: {type(e).__name__}')
    return n


def run_morning(today: Optional[str] = None, now: Optional[datetime] = None) -> Dict:
    """9:31 判定落库（纯本地读，不发任何网络请求）。异常只打印不抛，不拖垮调度。"""
    if not enabled():
        return {'enabled': False, 'written': 0}
    now = now or datetime.now()
    today = today or now.strftime('%Y-%m-%d')
    ts = now.strftime('%Y-%m-%d %H:%M:%S')
    from db.database import get_db_conn
    try:
        if not _ensure_table():
            return {'enabled': True, 'written': 0, 'error': '建表失败'}
        conn = get_db_conn()
        try:
            prev_date, zt = _read_zt(conn, today)
            auc = _read_auction(conn, today)
        finally:
            conn.close()
        if not prev_date or not zt:
            return {'enabled': True, 'written': 0,
                    'skipped': f'limit_pool_daily 在 {today} 之前无 zt 行（东财停调或首日）'}
        if not auc:
            return {'enabled': True, 'written': 0,
                    'skipped': f'{today} auction_snapshot 无行（采集失败或不在窗口，竞价事后补不回）'}
        rows = build_rows(today, prev_date, zt, auc)
        if not rows:
            return {'enabled': True, 'written': 0, 'skipped': '涨停池与竞价快照交集为空'}
        conn = get_db_conn()
        try:
            written = _write_rows(conn, rows, ts)
            conn.commit()
        finally:
            conn.close()
        arms = {a: sum(1 for r in rows if r['arm'] == a) for a in ARMS}
        print(f'[竞价低吸影子] {today} 落库 {len(rows)} 只（昨日池 {prev_date}、'
              f'写 {written} 行，幂等键 pool_date+symbol）档位 {arms}')
        return {'enabled': True, 'written': written, 'symbols': len(rows),
                'pool_date': today, 'prev_date': prev_date, 'arms': arms}
    except Exception as e:
        print(f'[竞价低吸影子] 判定轮失败: {type(e).__name__}: {e}')
        return {'enabled': True, 'written': 0, 'error': f'{type(e).__name__}: {e}'}


def run_evening(now: Optional[datetime] = None) -> Dict:
    """20:40 回填（日K五批 18:15~20:15 之后）：日内腿/隔夜腿/合计腿/基准，能补的补。
    历史缺行一起自愈，不只补最新一天。"""
    if not enabled():
        return {'enabled': False, 'healed': 0}
    now = now or datetime.now()
    ts = now.strftime('%Y-%m-%d %H:%M:%S')
    from db.database import get_db_conn
    try:
        if not _ensure_table():
            return {'enabled': True, 'healed': 0, 'error': '建表失败'}
        conn = get_db_conn()
        try:
            pending = _read_pending(conn)
            dates = sorted({str(p[0]) for p in pending})
            universe = {d: [str(r[0])[-6:] for r in conn.execute(
                'SELECT symbol FROM auction_snapshot WHERE trade_date=?', (d,)).fetchall()]
                for d in dates}
        finally:
            conn.close()
        if not pending:
            return {'enabled': True, 'healed': 0, 'pending': 0}
        bars_by_date = {}
        for d in dates:
            base = datetime.strptime(d, '%Y-%m-%d')
            d0 = (base - timedelta(days=KLINE_PAD_DAYS)).strftime('%Y-%m-%d')
            d1 = (base + timedelta(days=KLINE_PAD_DAYS)).strftime('%Y-%m-%d')
            bars_by_date[d] = _read_bars(universe.get(d, []), d0, d1)
        heals = build_heals(pending, bars_by_date, universe)
        conn = get_db_conn()
        try:
            healed = _write_heals(conn, heals, ts)
            conn.commit()
        finally:
            conn.close()
        print(f'[竞价低吸影子] 回填 {healed} 行（待补 {len(pending)} 行 / {len(dates)} 天，'
              f'缺K线的继续留 NULL）')
        return {'enabled': True, 'healed': healed, 'pending': len(pending), 'dates': dates}
    except Exception as e:
        print(f'[竞价低吸影子] 回填轮失败: {type(e).__name__}: {e}')
        return {'enabled': True, 'healed': 0, 'error': f'{type(e).__name__}: {e}'}


def evaluate(min_days: int = MIN_DAYS, forward_from: Optional[str] = None) -> Dict:
    """裁决用只读汇总。判据预注册（模块头四常量），胜率只展示不进判据。
    天数不足 ready=False——影子没到期就写"未裁决"。
    forward_from：只数该日及以后的日子（裁决时传前向起点 '2026-11-09'，把回溯的
    9 天样本内基线排除在钟外——那 9 天与发现规则的探针重叠，只作基线不作证据）。"""
    from db.database import get_db_conn
    conn = get_db_conn()
    out: Dict = {'ready': False, 'pass': False, 'days': 0, 'rows': 0,
                 'min_days': min_days, 'forward_from': forward_from,
                 'arms': {}, 'deep_dip': {}, 'chase_counterfactual': {},
                 'rule': f'≥{min_days}交易日 且 深低开(可成交)日内净超额≥{MIN_EXCESS_PP}pp '
                         f'且 按日t≥{MIN_T} 且 正号天数≥{MIN_POS_RATIO:.0%}；胜率只展示',
                 'clockRule': '数有可比行（日内腿已回填）的日子；对照的是涨停池×竞价快照，'
                              '不依赖推荐批次（第19条统一口径的例外，同第17条炸板影子）'}
    try:
        rows = conn.execute(
            'SELECT pool_date, arm, buyable, intraday_ret_pct, next_open_ret_pct, '
            'bench_intraday_pct FROM auction_dip_shadow '
            'WHERE intraday_ret_pct IS NOT NULL').fetchall()
        if forward_from:
            rows = [r for r in rows if str(r[0]) >= forward_from]
        out['rows'] = len(rows)
        out['days'] = len({str(r[0]) for r in rows})
        for arm in ARMS:
            vals = [float(r[3]) - FEE_PCT for r in rows if r[1] == arm]
            if not vals:
                continue
            n = len(vals)
            mean = sum(vals) / n
            wins = [v for v in vals if v > 0]
            losses = [v for v in vals if v <= 0]
            payoff = ((sum(wins) / len(wins)) / abs(sum(losses) / len(losses))
                      if wins and losses and sum(losses) != 0 else None)
            buyable_n = sum(1 for r in rows if r[1] == arm and r[2] == 1)
            out['arms'][arm] = {
                'n': n, 'net_mean_pct': round(mean, 3),
                'win_pct': round(len(wins) / n * 100, 1),
                'payoff': round(payoff, 2) if payoff is not None else None,
                'bad5_pct': round(sum(1 for v in vals if v <= -5) / n * 100, 1),
                'buyable_n': buyable_n,
                'buyable_share': round(buyable_n / n, 3),
            }
        # 深低开档（可成交）：按日聚类超额 = 当日均值(净) − 当日范围等权
        daily: Dict[str, List[float]] = {}
        bench: Dict[str, float] = {}
        for d, arm, buyable, i_val, _n, b_val in rows:
            if arm != 'deep_dip' or buyable != 1:
                continue
            daily.setdefault(str(d), []).append(float(i_val) - FEE_PCT)
            if b_val is not None:
                bench[str(d)] = float(b_val)
        excess = [sum(v) / len(v) - bench[d] for d, v in sorted(daily.items()) if d in bench]
        cs = cluster_stats(excess)
        out['deep_dip'] = dict(cs, buyable_days=len(daily),
                               bench_days=len(bench),
                               excess_mean_pp=cs['mean'])
        # chase 档反事实（"不追高开"止血规则的对照证据，绝对收益按日聚类）
        chase_daily: Dict[str, List[float]] = {}
        for d, arm, _b, i_val, _n, _bv in rows:
            if arm == 'chase':
                chase_daily.setdefault(str(d), []).append(float(i_val) - FEE_PCT)
        out['chase_counterfactual'] = cluster_stats(
            [sum(v) / len(v) for _d, v in sorted(chase_daily.items())])
        out['ready'] = out['days'] >= min_days
        t_ok = cs['t'] is not None and cs['t'] >= MIN_T
        pos_ok = cs['pos_ratio'] is not None and cs['pos_ratio'] >= MIN_POS_RATIO
        ex_ok = cs['mean'] is not None and cs['mean'] >= MIN_EXCESS_PP
        out['pass'] = bool(out['ready'] and t_ok and pos_ok and ex_ok)
        out['checks'] = {'days_ok': out['ready'], 'excess_ok': ex_ok,
                         't_ok': t_ok, 'pos_ok': pos_ok}
    except Exception as e:
        out['error'] = f'{type(e).__name__}: {e}'
    finally:
        conn.close()
    return out


def main(argv=None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(description='竞价低吸臂影子（只记录，不改动作）')
    ap.add_argument('--stage', choices=('morning', 'evening', 'evaluate'), default='evaluate')
    ap.add_argument('--date', default=None, help='判定日（morning 用），默认今天')
    ap.add_argument('--forward-from', default=None, help='evaluate 只数该日及以后（裁决传前向起点）')
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if a.stage == 'morning':
        print(run_morning(a.date))
    elif a.stage == 'evening':
        print(run_evening())
    else:
        print(evaluate(forward_from=a.forward_from))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())