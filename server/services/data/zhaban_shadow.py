# -*- coding: utf-8 -*-
"""zhaban_shadow.py - 炸板次数的"腾讯分时自算"影子留痕（2026-11-06，影子台账第 17 条）

一句话：东财涨停池里的 `zbc`（当日炸板次数）是**只有东财一家给**的字段，本模块用腾讯
1 分钟分时把它**另算一遍**，与东财真值并排存一张新表，攒 20 个交易日再裁决"东财停调那天
能不能用自算值顶上"。**只记录**——不改情绪评分、不改封板质量、不改门槛、不改推送。

为什么是它
----------
11-06 的熔断改造把"哪些字段真的没有备源"数清楚了（`docs/后续计划.md` 第 8 条）：涨停池附带
的 `fund`(封单额) / `ltsz`(流通市值) / `zbc`(炸板次数) / `hybk`(行业归因) 四列里，
`ltsz` 可由本地市值表近似、`hybk` 有 baostock 国标行业近似、**`zbc` 谁都给不了**——
因为"打开过几次"需要盘中逐分钟的状态序列，而我们有腾讯 1 分钟分时（`minute/query`，
零登录、当日可用）。这是唯一一条不依赖东财、又能自己把 zbc 造出来的免费路线。

口径（裁决时不许改）
--------------------
  · 涨停价 = 昨收 ×(1+pct%) 四舍五入到分，与 `intraday_dragon.limit_up_price` 同一式；
    **昨收取自腾讯批量行情的第 5 列，不是本地日K** —— `stock_daily` 5222 只里只有 2460 只
    同步到当天，其余 2762 只最近一根停在 2026-10-15/10-16（11-06 实测），而炸板池里
    ST/创业/北证占多数，拿三周前的收盘价算涨停价会整片算错（首跑一致率只有 46.7% 就是它）；
    pct 按板块自动判：主板 10 / 创业·科创 20 / ST 5 / 北证 30（universe 本只要主板，但
    东财池子给的是全市场，影子照单记，别让口径把样本偷偷筛掉）；
  · 某分钟算"在板上"：`price ≥ 涨停价 − 0.001`（分时给的是这一分钟的**即时价**，一分钟内
    "摸板又回落"看不见 —— 所以自算值天然只会**偏少**，这是本影子要量的头号偏差）；
  · `zbc_tx`（自算炸板次数）= 状态从"在板上"翻成"不在板上"的**转换次数**；
  · `seg_tx` = 连续在板段数（回封一次多一段），`zbc_tx` 与 `seg_tx` 相差 0 或 1；
  · `sealed_close_tx` = 最后一根分时是否在板上（收盘封住与否）；
  · `first_touch_tx` / `last_break_tx` = 首段起点 / 最后一次翻下的分钟；
  · **只在东财当天有真值时才跑**：`limit_pool_daily` 当天 0 行（pools 家族也停调的日子）
    就直接跳过，不发分时请求。理由：这张表的作用是"两源对照"，没有真值的自算值没有裁决价值，
    把它混进来会让人误以为已经接成备源了；
  · 分时返回的 `date` 必须等于目标交易日，否则整只跳过并记 `bar_date`（拿旧日分时算今天的
    炸板是最容易犯的错，宁缺毋假）。

开关：`ZHABAN_SHADOW_ENABLED`（默认 True，`app/cfg_market.py`，改后需重启）。
关掉 = 不建表、不写行、一个腾讯请求都不发，逐字节等于改造前。
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.request
from datetime import datetime
from typing import Dict, List, Optional, Tuple

_DDL = '''CREATE TABLE zhaban_shadow (
    trade_date      TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    pool            TEXT NOT NULL,        -- 东财当天在哪个池：zt / zb
    name            TEXT,
    zbc_em          INTEGER,              -- 东财真值（zhaban 列）
    zbc_tx          INTEGER,              -- 腾讯分时自算：在板→离板的转换次数
    seg_tx          INTEGER,              -- 连续在板段数
    touched_tx      INTEGER,              -- 当日是否摸到过涨停价（0/1）
    sealed_close_tx INTEGER,              -- 最后一根分时是否在板上（0/1）
    first_touch_tx  TEXT,
    last_break_tx   TEXT,
    prec            REAL,                 -- 昨收
    lim_price       REAL,                 -- 涨停价
    pct             REAL,                 -- 本次用的涨停幅度
    minutes         INTEGER,              -- 拿到的分时根数
    bar_date        TEXT,                 -- 分时接口报的日期（防拿旧数据）
    created_at      TEXT,
    PRIMARY KEY (trade_date, symbol, pool)
)'''


def ensure_table(conn) -> None:
    """建表 + **一次键迁移**（2026-11-07）。

    第一版把主键写成 `(trade_date, symbol)`，前提是"一只票当天要么在涨停池、要么在炸板池"。
    这个前提是错的：`limit_pool_daily` 由情绪快循环每 5 分钟落一次、每次 REPLACE，
    一只票盘中从炸板回到封住，就会在 `zb` 与 `zt` **各留一行**。实测 11-06：zt+zb 共 54 行，
    其中 8 只同码双池（002045/002088/002275/002488/600156/600410…），所以旧键把那 8 只吞了 ——
    `run_day` 打印"落库 54 只"、表里实际只有 46 行，这是文档与库对不上的真因。
    现在键改成 `(trade_date, symbol, pool)`（与 limit_pool_daily 同键）；旧结构的表直接重建，
    影子数据当天可重算（`--day` 能回补），不存在"丢历史"。
    """
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                       "AND name='zhaban_shadow'").fetchone()
    if row and 'pool' not in (row[0].split('PRIMARY KEY')[-1] if 'PRIMARY KEY' in row[0] else ''):
        conn.execute('DROP TABLE zhaban_shadow')
        print('[炸板影子] 旧主键 (trade_date,symbol) 会吞掉"同日既在 zt 又在 zb"的 8 只，'
              '重建为 (trade_date,symbol,pool)')
        row = None
    if row is None:                      # _DDL 故意不带 IF NOT EXISTS：表在就不重复建，
        conn.execute(_DDL)               # 否则第二次运行就报 "table already exists"（11-07 自测抓到）
    conn.commit()

_TOL = 0.001          # "在板上"的容差（元）
_REQUEST_GAP = 0.15   # 单只分时之间的小睡：一轮 ~54 只，慢 8 秒无所谓，别去刺腾讯


def enabled() -> bool:
    try:
        from app.config import ZHABAN_SHADOW_ENABLED
        return bool(ZHABAN_SHADOW_ENABLED)
    except Exception:
        return False


def pct_for(symbol: str, name: str = '') -> float:
    """涨停幅度：主板 10 / 创业·科创 20 / 北证 30 / ST 5（ST 优先于板块）。"""
    s = (symbol or '').strip().lower()
    code = s[2:] if s[:2] in ('sh', 'sz', 'bj') else s
    if 'ST' in (name or '').upper():
        return 5.0
    if s.startswith('bj') or code[:1] in ('4', '8', '9'):
        return 30.0
    if code[:2] in ('30', '68'):
        return 20.0
    return 10.0


def limit_up_price(prec: float, pct: float = 10.0) -> float:
    """与 `intraday_dragon.limit_up_price` 同式（+1e-9 避开浮点半分）。"""
    return round(prec * (1 + pct / 100.0) + 1e-9, 2)


def compute_from_bars(bars: List[Dict], lim: float) -> Dict:
    """分时序列 → 自算炸板口径。纯函数，不打网络、不碰库（自测直接喂构造序列）。"""
    at = [bool(b.get('price')) and float(b['price']) >= lim - _TOL for b in bars]
    segs, breaks = 0, 0
    first_touch, last_break, prev = '', '', False
    for i, on in enumerate(at):
        if on and not prev:
            segs += 1
            if not first_touch:
                first_touch = str(bars[i].get('t') or '')
        elif prev and not on:
            breaks += 1
            last_break = str(bars[i].get('t') or '')
        prev = on
    return {'zbc_tx': breaks, 'seg_tx': segs,
            'touched_tx': 1 if segs else 0,
            'sealed_close_tx': 1 if (at and at[-1]) else 0,
            'first_touch_tx': first_touch, 'last_break_tx': last_break,
            'minutes': len(bars)}


def fetch_minute_prices(code: str, timeout: float = 8.0) -> Tuple[str, List[Dict]]:
    """腾讯当日分时（与 intraday_dragon 同一个接口，这里只要 time/price 两列）。
    返回 (接口报的日期 YYYYMMDD, [{'t','price'}...])；失败 ('', [])。"""
    url = f'https://ifzq.gtimg.cn/appstock/app/minute/query?code={code}'
    try:
        d = json.loads(urllib.request.urlopen(url, timeout=timeout).read().decode('utf-8'))
        k = d['data'][code]
        date = str(k['data'].get('date') or '')
        rows = k['data'].get('data') or []
        bars = []
        for line in rows:
            parts = str(line).split()
            if len(parts) < 2:
                continue
            try:
                bars.append({'t': parts[0], 'price': float(parts[1])})
            except ValueError:
                continue
        return date, bars
    except Exception as e:
        print(f'[炸板影子] 分时失败 {code}: {type(e).__name__}: {e}')
        return '', []


def _prefixed(code6: str) -> str:
    """裸 6 位码 → 带市场前缀（腾讯两种接口都要带前缀的写法）。"""
    c = str(code6)
    if c[:2] in ('sh', 'sz', 'bj'):
        return c
    if c[:1] in ('4', '8', '9'):
        return 'bj' + c                     # 北交所：920xxx（新代码段）与 43/83/87 段
    return ('sh' + c) if c.startswith(('5', '6')) else ('sz' + c)


_TENCENT_QUOTE_URL = 'https://qt.gtimg.cn/q='
_QUOTE_BATCH = 60


def prev_closes(codes6: List[str]) -> Dict[str, float]:
    """昨收（元）：走腾讯批量行情的**第 5 列**（`~` 分隔：3=现价 / 4=昨收 / 5=今开），
    一次 60 只，54 只池子只要 1 个请求。

    为什么不用本地日K（这里曾经是日K，实测把它淘汰了）：`stock_daily` 里 5222 只票
    **只有 2460 只同步到当天**，另外 2762 只最近一根停在 2026-10-15/10-16（日K同步只按
    选股范围刷）。而炸板/涨停池里 ST、创业板、北证占多数，它们正好落在没刷新的那一堆里
    ⇒ 拿三周前的收盘价当"昨收"，涨停价整片算错。11-06 首跑自算一致率只有 46.7%、
    漏报 35.6%，逐只查下去根因就是这个（300237 真实昨收 2.35→涨停 2.82，日K给 2.37→2.84，
    于是"从未摸板"）。行情接口的昨收是当日实时的，不存在这个滞后。
    """
    out: Dict[str, float] = {}
    uniq = list(dict.fromkeys(str(c) for c in codes6 if c))
    for i in range(0, len(uniq), _QUOTE_BATCH):
        chunk = uniq[i:i + _QUOTE_BATCH]
        url = _TENCENT_QUOTE_URL + ','.join(_prefixed(c) for c in chunk)
        try:
            raw = urllib.request.urlopen(url, timeout=10).read().decode('gbk', 'replace')
        except Exception as e:
            print(f'[炸板影子] 昨收批量请求失败（这批 {len(chunk)} 只跳过）: '
                  f'{type(e).__name__}: {e}')
            continue
        for line in raw.strip().split(';'):
            line = line.strip()
            if '=' not in line:
                continue
            parts = line.split('=')[1].strip('"').split('~')
            if len(parts) < 6:
                continue
            code = str(parts[2])
            try:
                v = float(parts[4])           # 4＝昨收；写成 3 就拿到现价，5 是今开
            except ValueError:
                continue
            if v > 0:
                out[code] = v
    return out


def run_day(day: Optional[str] = None) -> Dict:
    """对当天的 zt+zb 池逐只自算炸板并与东财真值并排落库。

    网络取数全部在前（先算完再写），写库是最后一次性 executemany ——
    这是 11-03 整库写锁事故的规矩：不许在事务里等外部 IO。
    """
    if not enabled():
        return {'skipped': '开关关闭'}
    day = day or datetime.now().strftime('%Y-%m-%d')
    from services.data import limit_pool
    rows_em = limit_pool.load_day(day)
    pool_rows = [r for r in rows_em if r.get('pool') in ('zt', 'zb')]
    if not pool_rows:
        return {'skipped': f'{day} 的 limit_pool_daily 无行（pools 家族停调日不跑影子）',
                'day': day}
    codes = [str(r.get('symbol') or '') for r in pool_rows]
    codes = [c[2:] if c[:2] in ('sh', 'sz', 'bj') else c for c in codes]
    prev = prev_closes(codes)
    out, net_fail, no_prev = [], 0, 0
    for r in pool_rows:
        sym = str(r.get('symbol') or '')
        code = sym[2:] if sym[:2] in ('sh', 'sz', 'bj') else sym
        prec = prev.get(code)
        if not prec:
            no_prev += 1
            continue
        pct = pct_for(sym, str(r.get('name') or ''))
        lim = limit_up_price(prec, pct)
        bar_date, bars = fetch_minute_prices(_prefixed(code))
        time.sleep(_REQUEST_GAP)
        if not bars:
            net_fail += 1
            continue
        if bar_date and bar_date.replace('-', '') != day.replace('-', ''):
            print(f'[炸板影子] {sym} 分时日期是 {bar_date}，不是 {day} → 跳过（宁缺毋假）')
            net_fail += 1
            continue
        c = compute_from_bars(bars, lim)
        out.append((day, code, r.get('pool') or 'zt', r.get('name'), r.get('zhaban'),
                    c['zbc_tx'], c['seg_tx'], c['touched_tx'], c['sealed_close_tx'],
                    c['first_touch_tx'], c['last_break_tx'], prec, lim, pct,
                    c['minutes'], bar_date,
                    datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    if not out:
        return {'day': day, 'written': 0, 'poolRows': len(pool_rows),
                'netFail': net_fail, 'noPrevClose': no_prev}
    from db.database import get_db_conn
    conn = get_db_conn(wait_for_lock=True)
    try:
        ensure_table(conn)
        conn.executemany(
            'INSERT OR REPLACE INTO zhaban_shadow (trade_date,symbol,pool,name,zbc_em,'
            'zbc_tx,seg_tx,touched_tx,sealed_close_tx,first_touch_tx,last_break_tx,'
            'prec,lim_price,pct,minutes,bar_date,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            out)
        conn.commit()
    except Exception as e:
        print(f'[炸板影子] 落库失败 day={day}: {e}')
        return {'day': day, 'error': str(e), 'poolRows': len(pool_rows)}
    finally:
        conn.close()
    print(f'[炸板影子] {day} 落库 {len(out)} 只（东财池 {len(pool_rows)} 只 / '
          f'分时失败 {net_fail} / 无昨收 {no_prev}）；只记录，不进评分与门槛')
    return {'day': day, 'written': len(out), 'poolRows': len(pool_rows),
            'netFail': net_fail, 'noPrevClose': no_prev}


def evaluate(min_days: int = 20) -> Dict:
    """两源对照：自算 vs 东财真值。天数不够直接 ready=False，不拿小样本当结论。"""
    from db.database import get_db_conn
    conn = get_db_conn()
    try:
        try:
            rs = conn.execute('SELECT trade_date,zbc_em,zbc_tx,touched_tx,sealed_close_tx,'
                              'pool FROM zhaban_shadow WHERE zbc_em IS NOT NULL').fetchall()
            days = conn.execute('SELECT COUNT(DISTINCT trade_date) FROM zhaban_shadow').fetchone()[0]
        except sqlite3.Error:
            return {'ready': False, 'error': 'zhaban_shadow 表还不存在（一天都还没跑）', 'days': 0}
    finally:
        conn.close()
    n = len(rs)
    if not n:
        return {'ready': False, 'error': '还没有可比样本（东财真值与自算同时在场才算）',
                'days': days}
    same = sum(1 for r in rs if int(r[1] or 0) == int(r[2] or 0))
    diff = [int(r[2] or 0) - int(r[1] or 0) for r in rs]
    ae = sum(abs(d) for d in diff) / n
    xs = [float(r[1] or 0) for r in rs]
    ys = [float(r[2] or 0) for r in rs]
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    corr = sxy / (sxx * syy) ** 0.5 if sxx > 0 and syy > 0 else None
    miss = sum(1 for r in rs if int(r[1] or 0) > 0 and int(r[2] or 0) == 0)   # 东财有、自算漏
    ghost = sum(1 for r in rs if int(r[1] or 0) == 0 and int(r[2] or 0) > 0)  # 东财无、自算多
    em_seal = {'zt': 1, 'zb': 0}
    seal_ok = sum(1 for r in rs
                  if em_seal.get(str(r[5] or '')) is not None
                  and int(r[4] or 0) == em_seal[str(r[5] or '')])
    return {'ready': days >= min_days, 'days': days, 'n': n,
            'exact_rate_pct': round(same / n * 100, 2),
            'mean_abs_diff': round(ae, 3),
            'mean_bias_tx_minus_em': round(sum(diff) / n, 3),
            'pearson': round(corr, 4) if corr is not None else None,
            'missed_breaks': miss, 'missed_pct': round(miss / n * 100, 2),
            'extra_breaks': ghost, 'extra_pct': round(ghost / n * 100, 2),
            'sealed_vs_pool_pct': round(seal_ok / n * 100, 2),
            '判据': '要当备源用，需要 exact_rate ≥ 90% 且 missed_pct ≤ 5% 且 pearson ≥ 0.9'}


if __name__ == '__main__':
    import argparse
    import sys
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser()
    ap.add_argument('--day', help='YYYY-MM-DD，默认今天')
    ap.add_argument('--evaluate', action='store_true')
    a = ap.parse_args()
    if a.evaluate:
        print(json.dumps(evaluate(), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(run_day(a.day), ensure_ascii=False, indent=2))
