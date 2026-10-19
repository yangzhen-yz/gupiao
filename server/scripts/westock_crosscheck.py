"""westock_crosscheck.py - 双源数据交叉校验（权威源 = westock/腾讯自选股）

背景（2026-10-19）：东财 clist IP 级风控实锤后，westock-data 技能（腾讯自选股数据）
作为独立数据源就位。用户决策：**双源不一致时以 westock 为准**。

本脚本每日收盘后跑（scheduler 16:30，收评数据定型之后）：
  1. 定校验宇宙：当日资金流快照 top（inflow/outflow/hidden）+ 热搜 top + 当日推荐
     （去重，封顶 200 只）——不是全市场：fund flow 单次上限 10 只，全市场 5000+ 不现实；
     宇宙覆盖的是系统真正在用的股票（发现工具是全市场排名，权威校验是选股宇宙）。
  2. westock 拉取：fund flow（10 只/批并行）+ quote（60 只/批）。
  3. 双源比对：
     - 行情层（涨跌幅）：两源应一致（本地行情走腾讯 qt.gtimg.cn，westock 也是腾讯系，
       同源预期 100% 一致；不一致=某一侧接口异常，告警）；
     - 资金流层（主力净流入）：本地（东财 f62 / 新浪 r0+r1）vs westock（腾讯口径），
       三口径定义不同，只记录偏差与符号冲突，**westock 值为权威值**（authoritative='westock'）。
  4. 落库：westock_snapshot（每日权威快照）+ data_cross_check（比对结果）。

命令行：python scripts/westock_crosscheck.py [--date YYYY-MM-DD] [--dry-run]
scheduler：core/scheduler.py job_westock_crosscheck_daily 按路径加载本模块调 run()。
"""
import os
import sys
import time
import shutil
import argparse
import subprocess
from datetime import datetime
from typing import Dict, List, Optional

# westock CLI 定位：环境变量 > PATH > 本机默认安装位置（服务进程 PATH 可能不含 .local/bin）
_WESTOCK_CANDIDATES = [
    os.environ.get('WESTOCK_EXE', ''),
    shutil.which('westock') or '',
    r'C:\Users\Administrator\.local\bin\westock.exe',
]
_WESTOCK_EXE = next((p for p in _WESTOCK_CANDIDATES if p and os.path.exists(p)), None)

_UNIVERSE_CAP = 200          # 宇宙封顶
_FF_BATCH = 10               # fund flow 单批上限（CLI 硬限）
_QT_BATCH = 40               # quote 单批（60 只偶发空返回/超时，降到 40 稳）
_FF_WORKERS = 1              # 串行！2026-10-19 实测 4 线程并发触发 westock 服务端限频
_QT_WORKERS = 1              # （SERVER_RATE_LIMITED code=1620053006），与东财风控同一课
_FF_BATCH_SLEEP = 2.0        # fund flow 批间隔（秒）
_QT_BATCH_SLEEP = 1.0        # quote 批间隔（秒）
_RATE_LIMIT_RETRY = 3        # 空返回/限频批重试次数
_RATE_LIMIT_BACKOFF = 10.0   # 退避基数（秒）：10/20/30
_CLI_TIMEOUT = 45            # 单批 CLI 超时（大批次响应慢，30s 偏紧）
_QUOTE_TOL = 0.011           # 涨跌幅一致容差（百分点，防浮点/舍入）


# ========== CLI 调用与 Markdown 表格解析 ==========

def _westock(args: List[str], timeout: int = 30) -> str:
    """调 westock CLI，返回 stdout（含 stderr 合并文本，限频信息在 stderr）。

    2026-10-19：westock 服务端按频率限流（SERVER_RATE_LIMITED），失败列表走 stderr，
    合并返回便于调用方识别限频并退避。
    """
    if not _WESTOCK_EXE:
        raise RuntimeError('westock CLI 未找到（请先运行 skill 的 setup 或设 WESTOCK_EXE）')
    r = subprocess.run([_WESTOCK_EXE] + args, capture_output=True, text=True,
                       timeout=timeout, encoding='utf-8', errors='replace')
    return (r.stdout or '') + '\n' + (r.stderr or '')


def _is_rate_limited(text: str) -> bool:
    return 'SERVER_RATE_LIMITED' in (text or '') or '服务限频' in (text or '')


def _parse_md_table(text: str) -> List[Dict]:
    """解析 westock 的结构化 Markdown 表格输出为 dict 列表。

    形如：
      | code | name | ... |
      | --- | --- | ... |
      | sh600000 | 浦发银行 | ... |
    """
    lines = [ln.strip() for ln in (text or '').splitlines() if ln.strip().startswith('|')]
    if len(lines) < 3:
        return []
    header = [c.strip() for c in lines[0].strip('|').split('|')]
    rows = []
    for ln in lines[2:]:
        cells = [c.strip() for c in ln.strip('|').split('|')]
        if len(cells) != len(header):
            continue
        rows.append(dict(zip(header, cells)))
    return rows


def _num(v) -> Optional[float]:
    """'1,234.5' / '-0.52' / '' → float / None。"""
    if v is None:
        return None
    s = str(v).replace(',', '').strip()
    if s in ('', '-', '--'):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _fetch_batched(symbols: List[str], batch_size: int, cmd: List[str],
                   batch_sleep: float) -> Dict[str, Dict]:
    """串行分批拉取 + 空返回退避重试（2026-10-19：并发/密集触发服务端限频，
    空返回即退避重试，不限于限频标记——超时/空响应同样走重试）。"""
    out: Dict[str, Dict] = {}
    batches = [symbols[i:i + batch_size] for i in range(0, len(symbols), batch_size)]
    for idx, batch in enumerate(batches):
        args = cmd + [','.join(batch)]
        rows: List[Dict] = []
        for attempt in range(_RATE_LIMIT_RETRY + 1):
            try:
                text = _westock(args, timeout=_CLI_TIMEOUT)
            except Exception as e:
                print(f'[双源校验] 第 {idx + 1}/{len(batches)} 批 CLI 异常: {type(e).__name__}')
                text = ''
            rows = _parse_md_table(text)
            if rows:
                break
            if attempt < _RATE_LIMIT_RETRY:
                wait = _RATE_LIMIT_BACKOFF * (attempt + 1)
                tag = '限频' if _is_rate_limited(text) else '空返回'
                print(f'[双源校验] westock {tag}，{wait:.0f}s 后重试第 {idx + 1}/{len(batches)} 批'
                      f'（第 {attempt + 1} 次）')
                time.sleep(wait)
        for r in rows:
            code = (r.get('code') or '').lower()
            if code:
                out[code] = r
        if idx < len(batches) - 1:
            time.sleep(batch_sleep)
    return out


def fetch_fund_flow(symbols: List[str], date: Optional[str] = None) -> Dict[str, Dict]:
    """westock fund flow 批量拉取（10 只/批串行），返回 {symbol: 行dict}。"""
    cmd = ['fund', 'flow']
    if date:
        cmd += ['--date', date]
    return _fetch_batched(symbols, _FF_BATCH, cmd, _FF_BATCH_SLEEP)


def fetch_quotes(symbols: List[str], date: Optional[str] = None) -> Dict[str, Dict]:
    """westock quote 批量拉取（60 只/批串行），返回 {symbol: 行dict}。"""
    cmd = ['quote']
    if date:
        cmd += ['--date', date]
    return _fetch_batched(symbols, _QT_BATCH, cmd, _QT_BATCH_SLEEP)


# ========== 校验宇宙 ==========

def collect_universe() -> List[str]:
    """当日资金流 top + 热搜 top + 当日推荐（去重，封顶 200）。"""
    from db.database import (load_fund_flow_snapshot, load_hot_search_ranking,
                             load_daily_recommendations)
    syms = set()

    snap = load_fund_flow_snapshot('stocks')
    if snap and isinstance(snap, dict) and 'data' in snap:
        data = snap['data']
        for x in (data.get('inflow') or [])[:50] + (data.get('outflow') or [])[:50]:
            sym = (x.get('symbol') or '').lower()
            if sym:
                syms.add(sym)
    snap_h = load_fund_flow_snapshot('hidden')
    if snap_h and isinstance(snap_h, dict) and 'data' in snap_h:
        for x in (snap_h['data'] or [])[:50]:
            sym = (x.get('symbol') or '').lower()
            if sym:
                syms.add(sym)

    hot = load_hot_search_ranking() or {}
    for s in (hot.get('stocks') or [])[:50]:
        sym = (s.get('symbol') or '').lower()
        if sym:
            syms.add(sym)

    try:
        recs = load_daily_recommendations() or []
        for r in recs[:30]:
            sym = (r.get('symbol') or '').lower()
            if sym:
                syms.add(sym)
    except Exception:
        pass

    return sorted(syms)[:_UNIVERSE_CAP]


def collect_our_values() -> Dict[str, Dict]:
    """本地侧当日值（fund-flow 快照的 stocks 区块）：{symbol: {'change_pct','main_inflow'}}"""
    from db.database import load_fund_flow_snapshot
    out: Dict[str, Dict] = {}
    snap = load_fund_flow_snapshot('stocks')
    if snap and isinstance(snap, dict) and 'data' in snap:
        for side in ('inflow', 'outflow'):
            for x in (snap['data'].get(side) or []):
                sym = (x.get('symbol') or '').lower()
                if sym:
                    out[sym] = {'change_pct': x.get('changePercent'),
                                'main_inflow': x.get('mainNetInflow')}
    snap_h = load_fund_flow_snapshot('hidden')
    if snap_h and isinstance(snap_h, dict) and 'data' in snap_h:
        for x in (snap_h['data'] or []):
            sym = (x.get('symbol') or '').lower()
            if sym and sym not in out:
                out[sym] = {'change_pct': x.get('changePercent'),
                            'main_inflow': x.get('mainNetInflow')}
    return out


# ========== 主流程 ==========

def run(date_key: Optional[str] = None, dry_run: bool = False) -> Dict:
    """跑一遍双源校验，返回摘要 dict。"""
    date_key = date_key or datetime.now().strftime('%Y-%m-%d')
    t0 = time.time()
    syms = collect_universe()
    if not syms:
        print(f'[双源校验] 宇宙为空（无资金流快照/热搜/推荐），跳过')
        return {'date': date_key, 'total': 0, 'skipped': True}

    print(f'[双源校验] {date_key} 宇宙 {len(syms)} 只，开始拉取 westock...')
    try:
        ff = fetch_fund_flow(syms, date_key)
        qt = fetch_quotes(syms, date_key)
    except Exception as e:
        print(f'[双源校验] westock 拉取失败: {type(e).__name__}: {e}')
        return {'date': date_key, 'total': 0, 'error': str(e)}

    our = collect_our_values()
    print(f'[双源校验] westock 资金流 {len(ff)}/{len(syms)} 只、行情 {len(qt)}/{len(syms)} 只')

    snap_rows: List[Dict] = []
    check_rows: List[Dict] = []
    n_quote_ok = 0
    n_quote_checked = 0
    conflicts: List[Dict] = []

    for sym in syms:
        f, q = ff.get(sym), qt.get(sym)
        if not f and not q:
            continue
        name = (f or q or {}).get('name') or ''
        close = _num(q.get('price')) if q else None
        chg = _num(q.get('change_percent')) if q else None
        main_in = _num(f.get('MainNetFlow')) if f else None
        jumbo = _num(f.get('JumboNetFlow')) if f else None
        mid = _num(f.get('MidNetFlow')) if f else None
        r_in, r_out = _num(f.get('RetailInFlow')) if f else None, _num(f.get('RetailOutFlow')) if f else None
        retail = (r_in - r_out) if (r_in is not None and r_out is not None) else None

        snap_rows.append({
            'symbol': sym, 'name': name, 'close_price': close, 'change_pct': chg,
            'main_net_inflow': main_in, 'jumbo_net_flow': jumbo, 'mid_net_flow': mid,
            'retail_net_flow': retail,
            'main_net_5d': _num(f.get('MainNetFlow5D')) if f else None,
            'main_net_10d': _num(f.get('MainNetFlow10D')) if f else None,
            'main_net_20d': _num(f.get('MainNetFlow20D')) if f else None,
            'industry_rank': int(_num(f.get('MainInflowIndustryRank')) or 0) if f else None,
        })

        # 行情层比对（两源都有才判）
        o = our.get(sym) or {}
        agreed = None
        if chg is not None and o.get('change_pct') is not None:
            n_quote_checked += 1
            agreed = abs(float(o['change_pct']) - chg) <= _QUOTE_TOL
            if agreed:
                n_quote_ok += 1
        # 资金流层比对
        o_in = o.get('main_inflow')
        diff = diff_pct = None
        direction_conflict = None
        if main_in is not None and o_in is not None and o_in != 0:
            diff = main_in - float(o_in)
            diff_pct = diff / abs(float(o_in)) * 100
            direction_conflict = (main_in > 0) != (float(o_in) > 0)
            if direction_conflict:
                conflicts.append({'symbol': sym, 'name': name,
                                  'our': round(float(o_in) / 1e8, 2),
                                  'westock': round(main_in / 1e8, 2)})
        check_rows.append({
            'symbol': sym, 'name': name,
            'our_change_pct': o.get('change_pct'), 'westock_change_pct': chg,
            'quote_agreed': agreed,
            'our_main_inflow': o_in, 'westock_main_inflow': main_in,
            'inflow_diff': diff, 'inflow_diff_pct': diff_pct,
            'direction_conflict': direction_conflict,
        })

    summary = {
        'date': date_key, 'total': len(syms),
        'westock_ff': len(ff), 'westock_qt': len(qt),
        'quote_checked': n_quote_checked, 'quote_agreed': n_quote_ok,
        'quote_agreed_rate': round(n_quote_ok / n_quote_checked * 100, 1) if n_quote_checked else None,
        'direction_conflicts': conflicts,
        'cost_sec': round(time.time() - t0, 1),
    }

    if not dry_run:
        from db.database import save_westock_snapshot, save_cross_check
        n1 = save_westock_snapshot(date_key, snap_rows)
        n2 = save_cross_check(date_key, check_rows)
        summary['saved_snapshot'] = n1
        summary['saved_check'] = n2
        print(f'[双源校验] 落库 westock_snapshot {n1} 条、data_cross_check {n2} 条')

    rate = summary['quote_agreed_rate']
    print(f"[双源校验] 完成：行情一致 {n_quote_ok}/{n_quote_checked}"
          f"（{rate}%）、资金流符号冲突 {len(conflicts)} 只、耗时 {summary['cost_sec']}s")
    for c in conflicts:
        print(f"  ⚠ 符号冲突 {c['name']}({c['symbol']}): 本地 {c['our']}亿 vs westock {c['westock']}亿（以 westock 为准）")
    return summary


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='双源数据交叉校验（权威源 westock）')
    ap.add_argument('--date', default=None, help='交易日 YYYY-MM-DD，默认今天')
    ap.add_argument('--dry-run', action='store_true', help='只比对不落库')
    args = ap.parse_args()
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    run(date_key=args.date, dry_run=args.dry_run)
