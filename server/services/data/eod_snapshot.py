"""eod_snapshot.py - 收盘后用**腾讯批量行情**一次拿全市场当日日K（零登录，2026-10-16）

为什么要有它
------------
2026-10-16 19:40 这个 IP 被 baostock 判定 `10001011 黑名单用户`，整晚的日K同步停在
1109/2462 只。根因不是"今天运气差"，是**架构把每天的数据压在了一个需要登录会话的服务上**：

  · baostock 取数前必须 `login()` 建会话，它没有配额、没有 key，防滥用只有一招 = 按登录频率判黑；
  · 而它的接口是**逐只**查历史（`query_history_k_data_plus` 一次一只），
    所以"补当天一根K线"这件事，天然是 2462 次查询 + 若干次重连重登 —— 只要上游开始报错，
    失败重试里的重登就会从"偶尔"变成"每只都登一次"，几千次登录几分钟内打完，必然封。

腾讯 `qt.gtimg.cn/q=sh600000,sz000001,...` 一条请求能带 **~60 只**的实时快照，收盘后这份快照
就是当天的日K（今开/最高/最低/收盘/成交量/成交额），**没有会话、没有登录、没有黑名单这回事**：
2462 只 ≈ 41 次 HTTP 请求，几秒钟跑完。所以从今天起：

  主路（每晚 18:15 起）= 本模块的批量快照；
  备路（只在主路失败/要补历史/要重算复权因子时才用）= baostock。

口径核对（2026-10-16 实测，脚本内已做交叉校验）
------------------------------------------------
  · 价格：前复权序列的**最后一根 = 不复权实盘价**（复权因子锚定最新交易日），
    所以拿腾讯的原始价补"今天这一根"与 baostock 的前复权口径一致 ——
    实测 600000 昨日收盘：库内(前复权) 9.64 == 腾讯昨收字段 9.64，差 0.00；
  · 成交量：腾讯是**手**，库内是**股** → ×100；
  · 成交额：腾讯是**万元**，库内 `turnover` 存的是**元** → ×10000；
  · 除权除息：如果腾讯给的"昨收"≠ 库内最后一根收盘（差过 0.01 元），说明今天正好除权，
    这根新K线本身仍然正确，但**它之前的整段前复权序列要重算** —— 这类票进 `_restate` 清单，
    交给 baostock 逐只重灌（每天通常只有几十只，正好把登录预算花在刀刃上）。

局限（说清楚，别当免费午餐）
--------------------------
  1. 只能拿"当前这一根"，补不了历史某一天（要补 10-15 那种漏一天，仍然走 baostock）；
  2. 停牌/退市票快照可能是 0 成交量或时间戳停在旧交易日 —— 按时间戳日期不符就丢弃，宁缺毋错；
  3. 腾讯字段位置是公开约定，接口改版就得改这里 —— 因此交叉校验（与 baostock 已落库的同日数据
     比对价格/量）每次运行都会打日志，偏差超阈值就**拒绝写库**，让备路 baostock 继续当权威。

用法：
  python -m services.eod_snapshot --dry-run     # 只比对不写
  python -m services.eod_snapshot               # 写入当日
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import requests

from app.config import (EASTMONEY_HEADERS, KLINE_HISTORY_DB_PATH, TENCENT_SNAPSHOT_BATCH_SIZE,
                        TENCENT_SNAPSHOT_ENABLED, TENCENT_SNAPSHOT_MAX_DIFF)

_UA = {'User-Agent': EASTMONEY_HEADERS.get('User-Agent', 'Mozilla/5.0')}
_URL = 'https://qt.gtimg.cn/q='

# 腾讯快照字段位置（~ 分隔，实测 2026-10-16）
_F_NAME, _F_PRICE, _F_PREV_CLOSE, _F_OPEN = 1, 3, 4, 5
_F_HIGH, _F_LOW, _F_VOL_HANDS, _F_AMT_WAN, _F_STAMP = 33, 34, 36, 37, 30


def _pure(symbol: str) -> str:
    s = (symbol or '').strip().lower()
    return s[2:] if s[:2] in ('sh', 'sz', 'bj') else s


def _prefixed(code: str) -> str:
    return ('sh' + code) if code.startswith('6') else ('sz' + code) if code[:2] in ('00', '30') else ''


def eligible_codes() -> List[str]:
    """该拿快照的清单 = 本地库有历史数据 且 在选股范围内（主板+非ST/退+市值≥下限，≈2462 只）。

    这里必须走 `universe.eligible_symbols()`（含市值层）而不是 `filter_symbols()`（只含主板/ST）：
    2026-10-16 的口径统一之后，只有 2462 只才是"会进选票范围的票"，多拉的 589 只小市值
    连海选都进不去，白占请求。
    """
    conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=30)
    try:
        raw = [r[0] for r in conn.execute('SELECT DISTINCT symbol FROM stock_daily').fetchall()]
    finally:
        conn.close()
    from services.data import universe
    return universe.eligible_symbols(raw)


def _parse_line(ln: str) -> Optional[Dict]:
    """一行 `v_sh600000="...";` → 结构化快照；拿不到有意义的数据返回 None。"""
    try:
        key = ln.split('=')[0].replace('v_', '').strip()
        body = ln.split('"', 1)[1].rsplit('"', 1)[0]
    except (IndexError, ValueError):
        return None
    f = body.split('~')
    if len(f) <= _F_STAMP:
        return None
    def _num(i):
        try:
            return float(f[i])
        except (TypeError, ValueError, IndexError):
            return None
    code = _pure(key)
    close, opn, high, low, vol, amt = (_num(_F_PRICE), _num(_F_OPEN), _num(_F_HIGH),
                                       _num(_F_LOW), _num(_F_VOL_HANDS), _num(_F_AMT_WAN))
    stamp = (f[_F_STAMP] or '')[:8]
    if not code or not close or close <= 0 or not stamp:
        return None
    return {'symbol': code, 'close': close, 'open': opn, 'high': high, 'low': low,
            'prev_close': _num(_F_PREV_CLOSE),
            'volume': (vol or 0) * 100,            # 手 → 股
            'turnover': (amt or 0) * 1e4,          # 万元 → 元
            'stamp': f'{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}', 'name': f[_F_NAME]}


def fetch_snapshot(codes: List[str], batch: int = TENCENT_SNAPSHOT_BATCH_SIZE,
                   pause: float = 0.15) -> Dict[str, Dict]:
    """批量拉快照。返回 {裸码: 快照}。整批失败重试 2 次后跳过（不阻塞其余批次）。"""
    out: Dict[str, Dict] = {}
    todo = [_prefixed(c) for c in codes]
    todo = [t for t in todo if t]
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        for attempt in range(3):
            try:
                r = requests.get(_URL + ','.join(chunk), headers=_UA, timeout=12)
                r.encoding = 'gbk'
                for ln in r.text.strip().split(';'):
                    ln = ln.strip()
                    if ln.startswith('v_'):
                        d = _parse_line(ln)
                        if d:
                            out[d['symbol']] = d
                break
            except Exception as e:
                if attempt == 2:
                    print(f'[收盘快照] 第 {i // batch + 1} 批失败: {type(e).__name__}: {e}')
                else:
                    time.sleep(1 + attempt)
        time.sleep(pause)
    return out


def sync_snapshot(target_date: Optional[str] = None, dry_run: bool = False,
                  codes: Optional[List[str]] = None) -> Dict:
    """用腾讯快照把 target_date（默认今天）这根K线写进本地库。

    写之前做一次**与备路口径的交叉校验**：库里当天若已有 baostock 写的行，逐价比对，
    偏差比例超 `TENCENT_SNAPSHOT_MAX_DIFF` 就说明两边口径不一致（字段改版/单位变了），
    此时拒绝写入并告警 —— 主路必须能被备路否掉，否则一个改版就能把整库日K写坏。
    """
    if not TENCENT_SNAPSHOT_ENABLED:
        return {'ok': False, 'why': '开关关闭'}
    target_date = target_date or date.today().strftime('%Y-%m-%d')
    codes = codes or eligible_codes()
    snap = fetch_snapshot(codes)
    stats = {'ok': True, 'date': target_date, 'scope': len(codes), 'got': len(snap),
             'written': 0, 'stale_stamp': 0, 'zero_vol': 0, 'restated': 0,
             'diff_rows': 0, 'max_price_diff': 0.0, 'rejected': ''}
    if not snap:
        return {'ok': False, 'why': '一条快照都没拿到（腾讯接口不通/字段改版）'}
    conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=60)
    conn.execute('PRAGMA journal_mode=WAL')
    try:
        have = {r[0]: r for r in conn.execute(
            'SELECT symbol, open, high, low, close FROM stock_daily WHERE date=?', (target_date,))}
        # 交叉校验：只比"当天已有行"的那批（baostock 写过的）。
        # 停牌票要排除：腾讯对停牌给 O/H/L=0（"没有成交"是事实），baostock 写的是平线，
        # 2026-10-16 实测这种差异正好 5 只 —— 不是口径不一致，别让它把主路判死。
        both = [s for s in snap if s in have and snap[s]['open'] and snap[s]['high'] and snap[s]['low']]
        if both:
            bad = 0
            for s in both:
                for k, idx in (('open', 1), ('high', 2), ('low', 3), ('close', 4)):
                    ref = have[s][idx]
                    if ref and abs(snap[s][k] - ref) / max(ref, 0.01) > 0.002:
                        bad += 1
                        break
                stats['max_price_diff'] = max(stats['max_price_diff'],
                                              abs(snap[s]['close'] - (have[s][4] or 0)))
            stats['diff_rows'] = bad
            if bad > max(3, len(both) * TENCENT_SNAPSHOT_MAX_DIFF):
                stats['rejected'] = (f'{bad}/{len(both)} 行与 baostock 口径不符'
                                     f'（阈值 {TENCENT_SNAPSHOT_MAX_DIFF*100:.1f}%）→ 本次拒写')
                return stats
        payload, restate = [], []
        for s, d in snap.items():
            if d['stamp'] != target_date:
                stats['stale_stamp'] += 1           # 时间戳不是今天 = 这天没交易/停牌，别硬写
                continue
            if not d['volume']:
                stats['zero_vol'] += 1
                continue
            payload.append((s, target_date, d['open'], d['high'], d['low'], d['close'],
                            d['volume'], d['turnover']))
            # 除权检测：腾讯昨收 ≠ 库内最后一根收盘 → 历史前复权序列要重算
            if d['prev_close']:
                last = conn.execute('SELECT close FROM stock_daily WHERE symbol=? '
                                    'AND date<? ORDER BY date DESC LIMIT 1', (s, target_date)).fetchone()
                if last and last[0] and abs(d['prev_close'] - last[0]) > 0.011:
                    restate.append(s)
        if not dry_run and payload:
            conn.executemany('INSERT OR REPLACE INTO stock_daily '
                             '(symbol,date,open,high,low,close,volume,turnover) VALUES (?,?,?,?,?,?,?,?)',
                             payload)
        if not dry_run:
            # 名称同步（2026-11-01）：ST 过滤唯一的数据源是名称，而 baostock 名录已不可用
            # （10-16 拉黑），`stock_basic_full.fetched_at` 一度停在 10-16 —— 此后新戴帽的票
            # 能一路混进推荐（10-15 的 *ST威领 就是这么漏的）。腾讯快照本身就带名称，
            # 顺手按天刷新，不增加任何请求；IPO 日期/状态等字段保持不动，只覆盖 name。
            # 与 payload 解耦：停牌/休市时 stamp 不是今天、K线不写，但名称仍是当天的，
            # 照样要刷——否则周末跑一次就整天不更新名录。
            _names = [(s, d['name'], datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                      for s, d in snap.items() if d.get('name')]
            if _names:
                conn.execute('CREATE TABLE IF NOT EXISTS stock_basic_full ('
                             'symbol TEXT PRIMARY KEY, name TEXT, ipo_date TEXT, out_date TEXT,'
                             ' type TEXT, status TEXT, fetched_at TEXT)')
                conn.executemany(
                    'INSERT INTO stock_basic_full (symbol,name,fetched_at) VALUES (?,?,?) '
                    'ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, fetched_at=excluded.fetched_at',
                    _names)
                stats['names'] = len(_names)
            conn.commit()
        stats['written'] = 0 if dry_run else len(payload)
        stats['restated'] = len(restate)
        stats['restate_symbols'] = restate[:50]
        _save_restate(restate, target_date)
    finally:
        conn.close()
    return stats


_RESTATE_DDL = '''CREATE TABLE IF NOT EXISTS kline_restate_queue (
    symbol TEXT NOT NULL, need_date TEXT NOT NULL, reason TEXT,
    created_at TEXT, done TEXT,
    PRIMARY KEY (symbol, need_date)
)'''


def _save_restate(symbols: List[str], need_date: str):
    """除权票进重算队列：这类票数量少（几十只/天），交给 baostock 逐只重灌整段前复权历史。"""
    if not symbols:
        return
    conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=30)
    try:
        conn.execute(_RESTATE_DDL)
        conn.executemany('INSERT OR REPLACE INTO kline_restate_queue (symbol,need_date,reason,created_at) '
                         'VALUES (?,?,?,?)',
                         [(s, need_date, '除权除息：昨收与库内收盘不一致',
                           datetime.now().strftime('%Y-%m-%d %H:%M:%S')) for s in symbols])
        conn.commit()
    except sqlite3.Error as e:
        print(f'[收盘快照] 重算队列写失败: {e}')
    finally:
        conn.close()


def coverage(date_str: str) -> Tuple[int, int]:
    from services.data import universe
    conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=30)
    try:
        day = {r[0] for r in conn.execute(
            'SELECT DISTINCT symbol FROM stock_daily WHERE date=?', (date_str,)).fetchall()}
        scope = set(universe.eligible_symbols())
    finally:
        conn.close()
    return len(day & scope), len(scope)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None, help='要补的交易日（默认今天）')
    ap.add_argument('--dry-run', action='store_true', help='只校验、只比对，不写库')
    ap.add_argument('--limit', type=int, default=0, help='只拉前 N 只（调试用）')
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    codes = eligible_codes()
    if a.limit:
        codes = codes[:a.limit]
    t0 = time.time()
    st = sync_snapshot(a.date, dry_run=a.dry_run, codes=codes)
    cov, uni = coverage(st.get('date') or (a.date or date.today().strftime('%Y-%m-%d')))
    print(json.dumps(st, ensure_ascii=False, indent=1))
    print(f'覆盖：选股范围内 {cov}/{uni} = {cov / uni * 100:.1f}%（耗时 {int(time.time() - t0)}s）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
