# -*- coding: utf-8 -*-
"""push_gate_shadow.py - 推送超价闸门影子（2026-11-11，`docs/影子实验台账.md` 第 22 条 · 只记录，不改任何动作）

一句话：**每条盘中达标推送成功后，把"推送时现价 vs 买入参考价上限"的超价幅度记下来，
收盘后回填"按推送价买→次日开盘卖"的净收益；裁决时回答"现价已超买入上限的票，是不是真的更差"。
线上推送一只都不拦，评分/门槛/文案一字不动。**

为什么建这条臂（数字都是 11-11 当天实测，探针在 `analysis_out/`、不入库）：
  · 方向 1 把"默认出口＝次日开盘卖"写进了每张推荐卡与每条推送；方向 2 是它的入口侧对照——
    推荐卡早就算了 `overPct`（现价比买入上限高多少），实测扫描接口 sz000700 现价 17.09、
    买入上限 16.75、overPct=2.06%（type=tail），正是"现价已追过低吸区间"的形态。
  · 复盘实测：现价追入口径 T+1 胜率 46.8%，低吸单真正成交口径 42.1%（纸面 60.4% 里三成是
    未成交的幻觉胜利）。"超价就不推"听起来对，但**这是同一份数据里挑出来的规则**，
    样本内不算数——所以先挂影子攒前向对照，不直接改推送过滤。

口径边界（如实写在这里，别等裁决时才发现）：
  · 入场价＝**推送那一刻的现价**（`price`），不是次日开盘、也不是买入上限。闸门假设是
    "现价已超上限＝你在追高"，反事实就是"真按这个追高价买、次日开盘卖"亏多少。
  · `would_block`：overPct>0 记 1（闸门会拦）、=0 或 <0 记 0（放行）、缺值记 NULL（不当 0）。
    用的是推荐卡同一核心口径 `buyTiming.overPct`（已 round 2），**不用 overPctDyn**（动态带宽是
    并列影子，负结果，见台账）。NULL 行只统计覆盖率，不进裁决两组。
  · 净收益＝(次日开盘/推送现价−1)×100 − 双边费用；费用走 `rec_sim._rt_cost_pct`（10 万本金口径，
    避免最低 5 元佣金扭曲小单），与可成交模拟同一把尺子，分母才可比。
  · 哪条腿算不出来就**留 NULL，绝不填 0**（填 0 会把"还没数据"混成"平盘"，均值直接失真）；
    晚间自愈能补的补、补不了的继续 NULL。
  · 裁决是**按日配对**：只数"放行组与被拦组当天都有样本"的交易日，逐日算
    放行组净均值 − 被拦组净均值，再按日聚类。这样控住行情日效应（两组同一天比），
    不会把"被拦组恰好都在大跌日推"误当成闸门有效。判据预注册在 `evaluate()`：
    ≥20 个配对交易日、差值≥+0.5pp、按日聚类 t≥2，三条全过才提"接进推送过滤"，
    否则延长或回退，**不降门槛**。胜率只展示不进判据（目标是单笔期望不是胜率）。
  · 读写严格分段：先把待补行/日K全读进内存并关连接，再开写连接即写即提交——
    11-03"事务内等外部 IO 锁死全库"的教训不在这条新链路上重演。
  · 样本钟慢：盘中达标推送每天最多 6 只（push_daily_limit），还要两组当天都有样本才计一个配对日，
    所以攒满 20 个配对日可能要一两个月。这是如实的代价，不为了快点裁决去放宽推送只数。

开关：`PUSH_GATE_SHADOW_ENABLED`（默认 True，只写这张新表）；配置页 `push_gate_shadow_enabled` 可热改。
关掉时一个字段都不写，推送链路逐字节等于改造前。
"""
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional

# 复用竞价低吸臂的日K只读与按日聚类（同一把尺子，不另起一套）
from services.pick.auction_dip_shadow import _read_bars, cluster_stats, KLINE_PAD_DAYS

# 判据预注册（裁决时不许事后改门槛）
MIN_DAYS = 20             # 放行组与被拦组"当天都有样本"的配对交易日数
MIN_GAP_PP = 0.5          # 放行组净均值 − 被拦组净均值，≥ 此值才算闸门有效
MIN_T = 2.0               # 按日聚类配对差值 t（每天一个点，不是每笔一个点）


def enabled() -> bool:
    """独立开关：先热读覆盖表，读不到退回 config 默认。"""
    try:
        from services.support import app_settings
        v = app_settings.value('push_gate_shadow_enabled')
        if v is not None:
            return bool(v)
    except Exception:
        pass
    from app.config import PUSH_GATE_SHADOW_ENABLED
    return bool(PUSH_GATE_SHADOW_ENABLED)


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def would_block(over_pct) -> Optional[int]:
    """超价闸门判定：overPct>0 记 1（会拦）、≤0 记 0（放行）、缺值 None（不当 0）。
    边界：overPct=0 是"现价恰好等于买入上限"，不算追高，放行。"""
    v = _f(over_pct)
    if v is None:
        return None
    return 1 if v > 0 else 0


_DDL = '''CREATE TABLE IF NOT EXISTS push_gate_shadow (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    push_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT DEFAULT '',
    push_time TEXT DEFAULT '',
    price REAL,
    buy_high REAL,
    over_pct REAL,
    would_block INTEGER,
    score REAL,
    next_date TEXT DEFAULT '',
    next_open_ret_pct REAL,
    net_ret_pct REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(push_date, symbol)
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
        print(f'[推送闸门影子] 建表失败: {type(e).__name__}: {e}')
        return False


# ── 纯计算段（无 IO，自测直接打这里）──────────────────────────────────────────
def build_rows(push_date: str, push_time: str, hits: List[Dict]) -> List[Dict]:
    """推送成功的候选 → 待落库行。缺入场价（无法回填次日腿）的跳过，不写半行；
    overPct 缺但价在的照记（would_block=NULL），裁决时只统计覆盖率、不进两组。"""
    rows = []
    for s in hits:
        code = str(s.get('symbol') or s.get('code') or '')[-6:]
        if len(code) != 6:
            continue
        price = _f(s.get('priceRaw') or s.get('price'))
        if price is None or price <= 0:
            continue
        bsp = s.get('buySellPoints') or {}
        t = bsp.get('buyTiming') or {}
        over = _f(t.get('overPct'))
        rows.append({
            'push_date': push_date, 'symbol': code,
            'name': str(s.get('name') or ''), 'push_time': push_time,
            'price': price, 'buy_high': _f(t.get('buyHigh')),
            'over_pct': over, 'would_block': would_block(over),
            'score': _f(s.get('score')),
        })
    return rows


def _day_index(seq: List[tuple], date: str) -> Optional[int]:
    for i, r in enumerate(seq):
        if r[0] == date:
            return i
    return None


def build_heals(pending: List[tuple], bars_by_date: Dict[str, Dict[str, List[tuple]]]) -> List[Dict]:
    """能补的补上，补不了的继续 NULL。次日开盘腿＝次日open/推送现价−1，净＝毛−双边费用。
    pending 形状：(push_date, symbol, price, next_open_ret_pct, net_ret_pct)。"""
    from services.pick.rec_sim import _rt_cost_pct
    out = []
    by_date: Dict[str, List[tuple]] = {}
    for p in pending:
        by_date.setdefault(str(p[0]), []).append(p)
    for day, plist in by_date.items():
        bars = bars_by_date.get(day, {})
        for (_, symbol, price, _have_g, _have_n) in plist:
            code = str(symbol)[-6:]
            seq = bars.get(code, [])
            idx = _day_index(seq, day)
            heal = {'push_date': day, 'symbol': code,
                    'next_open_ret_pct': None, 'net_ret_pct': None, 'next_date': ''}
            if idx is not None and price and price > 0 and idx + 1 < len(seq):
                nxt = seq[idx + 1]
                nxt_open = nxt[1]
                if nxt_open and nxt_open > 0:
                    gross = (nxt_open / price - 1) * 100.0
                    heal['next_open_ret_pct'] = gross
                    heal['net_ret_pct'] = gross - _rt_cost_pct(price, nxt_open)
                    heal['next_date'] = nxt[0]
            if heal['next_open_ret_pct'] is not None or heal['net_ret_pct'] is not None:
                out.append(heal)
    return out


# ── 只读段 ────────────────────────────────────────────────────────────────────
def _read_pending(conn) -> List[tuple]:
    """次日腿或净收益还缺的行（表没建时返回空）。"""
    try:
        return conn.execute(
            'SELECT push_date, symbol, price, next_open_ret_pct, net_ret_pct '
            'FROM push_gate_shadow '
            'WHERE next_open_ret_pct IS NULL OR net_ret_pct IS NULL').fetchall()
    except Exception:
        return []


# ── 写入段：拿到算好的行再开写连接，即写即提交 ────────────────────────────────
def _write_rows(conn, rows: List[Dict], ts: str) -> int:
    n = 0
    for r in rows:
        try:
            conn.execute('''INSERT INTO push_gate_shadow (
                    push_date, symbol, name, push_time, price, buy_high, over_pct,
                    would_block, score, created_at, updated_at)
                VALUES (:push_date,:symbol,:name,:push_time,:price,:buy_high,:over_pct,
                        :would_block,:score,:ts,:ts)
                ON CONFLICT(push_date, symbol) DO UPDATE SET
                    name=excluded.name, push_time=excluded.push_time, price=excluded.price,
                    buy_high=excluded.buy_high, over_pct=excluded.over_pct,
                    would_block=excluded.would_block, score=excluded.score,
                    updated_at=excluded.updated_at''',
                dict(r, ts=ts))   # 重跑覆盖判定日字段，**不碰已回填的次日腿**
            n += 1
        except Exception as e:
            print(f'[推送闸门影子] 写 {r["push_date"]} {r["symbol"]} 失败: {type(e).__name__}: {e}')
    return n


def _write_heals(conn, heals: List[Dict], ts: str) -> int:
    """COALESCE 回填：这次算不出来绝不会把已有值抹成 NULL。"""
    n = 0
    for h in heals:
        try:
            conn.execute('''UPDATE push_gate_shadow
                    SET next_open_ret_pct=COALESCE(:next_open_ret_pct, next_open_ret_pct),
                        net_ret_pct=COALESCE(:net_ret_pct, net_ret_pct),
                        next_date=CASE WHEN :next_open_ret_pct IS NULL THEN next_date
                                       ELSE :next_date END,
                        updated_at=:ts
                WHERE push_date=:push_date AND symbol=:symbol''', dict(h, ts=ts))
            n += 1
        except Exception as e:
            print(f'[推送闸门影子] 回补 {h["push_date"]} {h["symbol"]} 失败: {type(e).__name__}')
    return n


def record_pushed(hits: List[Dict], push_date: Optional[str] = None,
                  now: Optional[datetime] = None) -> Dict:
    """推送成功后记录（push.py 在 push_wecom_markdown 返回 True 后调）。
    异常只打印不抛——影子绝不影响推送本身。纯本地写，不发任何网络请求。"""
    if not enabled():
        return {'enabled': False, 'written': 0}
    now = now or datetime.now()
    push_date = push_date or now.strftime('%Y-%m-%d')
    push_time = now.strftime('%H:%M:%S')
    ts = now.strftime('%Y-%m-%d %H:%M:%S')
    from db.database import get_db_conn
    try:
        rows = build_rows(push_date, push_time, hits)
        if not rows:
            return {'enabled': True, 'written': 0, 'skipped': '无有效行（缺价或代码非法）'}
        if not _ensure_table():
            return {'enabled': True, 'written': 0, 'error': '建表失败'}
        conn = get_db_conn()
        try:
            written = _write_rows(conn, rows, ts)
            conn.commit()
        finally:
            conn.close()
        blocked = sum(1 for r in rows if r['would_block'] == 1)
        allowed = sum(1 for r in rows if r['would_block'] == 0)
        unknown = sum(1 for r in rows if r['would_block'] is None)
        print(f'[推送闸门影子] {push_date} 记 {len(rows)} 只（写 {written} 行，'
              f'幂等键 push_date+symbol）会拦 {blocked}/放行 {allowed}/缺值 {unknown}')
        return {'enabled': True, 'written': written, 'symbols': len(rows),
                'push_date': push_date, 'blocked': blocked, 'allowed': allowed,
                'unknown': unknown}
    except Exception as e:
        print(f'[推送闸门影子] 记录轮失败: {type(e).__name__}: {e}')
        return {'enabled': True, 'written': 0, 'error': f'{type(e).__name__}: {e}'}


def run_evening(now: Optional[datetime] = None) -> Dict:
    """20:45 回填（日K五批 18:15~20:15 之后、竞价低吸臂 20:40 之后）：
    按推送价买→次日开盘卖的毛/净腿，能补的补，历史缺行一起自愈。"""
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
        finally:
            conn.close()
        if not pending:
            return {'enabled': True, 'healed': 0, 'pending': 0}
        dates = sorted({str(p[0]) for p in pending})
        codes_by_date: Dict[str, List[str]] = {}
        for p in pending:
            codes_by_date.setdefault(str(p[0]), []).append(str(p[1])[-6:])
        bars_by_date = {}
        for d in dates:
            base = datetime.strptime(d, '%Y-%m-%d')
            d0 = (base - timedelta(days=KLINE_PAD_DAYS)).strftime('%Y-%m-%d')
            d1 = (base + timedelta(days=KLINE_PAD_DAYS)).strftime('%Y-%m-%d')
            bars_by_date[d] = _read_bars(sorted(set(codes_by_date.get(d, []))), d0, d1)
        heals = build_heals(pending, bars_by_date)
        conn = get_db_conn()
        try:
            healed = _write_heals(conn, heals, ts)
            conn.commit()
        finally:
            conn.close()
        print(f'[推送闸门影子] 回填 {healed} 行（待补 {len(pending)} 行 / {len(dates)} 天，'
              f'缺K线的继续留 NULL）')
        return {'enabled': True, 'healed': healed, 'pending': len(pending), 'dates': dates}
    except Exception as e:
        print(f'[推送闸门影子] 回填轮失败: {type(e).__name__}: {e}')
        return {'enabled': True, 'healed': 0, 'error': f'{type(e).__name__}: {e}'}


def _mean(vals: List[float]) -> Optional[float]:
    return sum(vals) / len(vals) if vals else None


def evaluate(min_days: int = MIN_DAYS, forward_from: Optional[str] = None) -> Dict:
    """裁决用只读汇总。判据预注册（模块头三常量），胜率只展示不进判据。
    配对日不足 ready=False——影子没到期就写"未裁决"。
    forward_from：只数该日及以后的日子（裁决时传前向起点，把回溯基线排除在钟外）。"""
    from db.database import get_db_conn
    conn = get_db_conn()
    out: Dict = {'ready': False, 'pass': False, 'paired_days': 0, 'rows': 0,
                 'min_days': min_days, 'forward_from': forward_from,
                 'allowed': {}, 'blocked': {}, 'unknown_n': 0, 'paired': {},
                 'rule': f'≥{min_days}个配对交易日 且 放行组净均值−被拦组净均值≥{MIN_GAP_PP}pp '
                         f'且 按日聚类配对差值 t≥{MIN_T}；胜率只展示',
                 'clockRule': '只数"放行组与被拦组当天都有样本"的配对日；盘中达标推送每天≤6只，'
                              '样本钟慢是如实代价，不为快点裁决放宽推送只数'}
    try:
        rows = conn.execute(
            'SELECT push_date, would_block, net_ret_pct FROM push_gate_shadow '
            'WHERE net_ret_pct IS NOT NULL').fetchall()
        if forward_from:
            rows = [r for r in rows if str(r[0]) >= forward_from]
        out['rows'] = len(rows)
        out['unknown_n'] = sum(1 for r in rows if r[1] is None)

        def _grp(wb):
            vals = [float(r[2]) for r in rows if r[1] == wb]
            if not vals:
                return {}
            n = len(vals)
            wins = sum(1 for v in vals if v > 0)
            return {'n': n, 'net_mean_pct': round(sum(vals) / n, 3),
                    'win_pct': round(wins / n * 100, 1),
                    'days': len({str(r[0]) for r in rows if r[1] == wb})}

        out['allowed'] = _grp(0)
        out['blocked'] = _grp(1)
        # 按日配对：只数两组当天都有样本的日子，逐日算 放行均值 − 被拦均值
        daily_a: Dict[str, List[float]] = {}
        daily_b: Dict[str, List[float]] = {}
        for d, wb, net in rows:
            if wb == 0:
                daily_a.setdefault(str(d), []).append(float(net))
            elif wb == 1:
                daily_b.setdefault(str(d), []).append(float(net))
        paired_days = sorted(set(daily_a) & set(daily_b))
        diffs = [_mean(daily_a[d]) - _mean(daily_b[d]) for d in paired_days]
        cs = cluster_stats(diffs)
        out['paired'] = dict(cs, paired_days=len(paired_days),
                             gap_mean_pp=cs['mean'])
        out['paired_days'] = len(paired_days)
        out['ready'] = len(paired_days) >= min_days
        gap_ok = cs['mean'] is not None and cs['mean'] >= MIN_GAP_PP
        t_ok = cs['t'] is not None and cs['t'] >= MIN_T
        out['pass'] = bool(out['ready'] and gap_ok and t_ok)
        out['checks'] = {'days_ok': out['ready'], 'gap_ok': gap_ok, 't_ok': t_ok}
    except Exception as e:
        out['error'] = f'{type(e).__name__}: {e}'
    finally:
        conn.close()
    return out


def main(argv=None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(description='推送超价闸门影子（只记录，不改动作）')
    ap.add_argument('--stage', choices=('evening', 'evaluate'), default='evaluate')
    ap.add_argument('--forward-from', default=None, help='evaluate 只数该日及以后（裁决传前向起点）')
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if a.stage == 'evening':
        print(run_evening())
    else:
        print(evaluate(forward_from=a.forward_from))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())