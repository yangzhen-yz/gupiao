"""settings_preview.py - 改配置前先看"会剩多少只票"（2026-10-17）

复盘子页"口径与开关"里每一项都要给出实时影响预览（用户选定的粒度是
"开关 + 阈值 + 实时影响预览"）。预览不能读当前开关去算 —— 那只能得到现状，
得不到"如果这样改会怎样"。所以这里把六条范围条件写成**以参数为准**的直接计算。

与真实范围的对照（`parity_check()`）
-----------------------------------
 duplicated logic 的风险是预览和真实实现跑偏，那比没有预览更糟。
 所以每次预览前都会检查"按当前配置的预览值 == universe.selectable_count()"，
 不一致就在返回里标注"预览可能未反映最新口径"，并把差异打到日志。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import app.config as cfg
import threading
import time


def _scope_params(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """当前生效的六条参数（覆盖表优先），overrides 用来模拟"改成这样"。"""
    from services.support import app_settings as A
    keys = ['universe_main_board_only', 'universe_exclude_st', 'market_cap_enabled',
            'min_market_cap_yi', 'liquidity_enabled', 'liq_amount_enabled',
            'liq_min_avg_amount_wan', 'liq_turnover_enabled', 'liq_min_avg_turnover_pct',
            'liq_price_enabled', 'liq_min_price']
    p = {k: A.value(k) for k in keys}
    p.update(overrides or {})
    return p


def scope_count(overrides: Optional[Dict[str, Any]] = None) -> Optional[int]:
    """按给定（可能尚未生效）的参数算选票范围只数。"""
    try:
        from services.data import universe, market_cap, liquidity
        p = _scope_params(overrides)
        codes = universe._db_symbols()
        if p['universe_main_board_only']:
            codes = [c for c in codes if c.startswith(universe.MAIN_BOARD_PREFIXES)]
        if p['universe_exclude_st']:
            ex = universe.excluded_symbols()
            codes = [c for c in codes if c not in ex]
        if p['market_cap_enabled']:
            cmap = market_cap.cap_map()
            cap = float(p['min_market_cap_yi']) * 1e8
            codes = [c for c in codes if c not in cmap or cmap[c] >= cap]
        if p['liquidity_enabled']:
            m = liquidity.stats_map()
            amt = float(p['liq_min_avg_amount_wan']) * 1e4
            keep = []
            for c in codes:
                d = m.get(c)
                if not d or not d.get('price'):
                    continue                      # 窗口内没K线 = 买不到，与 liquidity.passes 同口径
                if p['liq_price_enabled'] and d['price'] <= float(p['liq_min_price']):
                    continue
                if p['liq_amount_enabled'] and (d['amount'] or 0) <= amt:
                    continue
                if p['liq_turnover_enabled']:
                    shares = d.get('float_shares')
                    if shares and (d['volume'] or 0) / shares * 100 <= float(p['liq_min_avg_turnover_pct']):
                        continue                  # 算不出流通股本时放行（fail-open），与 passes 一致
                keep.append(c)
            codes = keep
        return len(set(codes))
    except Exception as e:
        print(f'[配置预览] 计算失败: {type(e).__name__}: {e}')
        return None


def parity_check() -> Dict[str, Any]:
    """预览逻辑与真实范围是否一致（不一致说明这边漏了某条条件，得改）。"""
    try:
        from services.data import universe
        actual = universe.selectable_count()
    except Exception as e:
        return {'ok': False, 'why': f'读不到真实范围: {e}'}
    preview = scope_count()
    return {'ok': preview == actual, 'actual': actual, 'preview': preview}


def stale_no_sync_count(overrides: Optional[Dict[str, Any]] = None) -> int:
    """"其余条件都过、但因为已停止日K同步（窗口最后一天没有K线）而不可能被选中"的只数。

    这一层必须单独报出来：2026-10-17 实测把市值下限从 35 亿调到 20 亿，预览显示 +0 只，
    看起来像"这条没作用"，真相是那 532 只 20~35 亿的票早已落在同步范围之外
    （`KLINE_SYNC_UNIVERSE_ONLY` 按 35 亿筛过），最后一条K线停在 10-15。
    判成"买不到"是对的（数据都不新了），但要让人看见原因，否则他会去调别的旋钮白费功夫。
    """
    try:
        from services.data import universe, market_cap, liquidity
        p = _scope_params(overrides)
        m = liquidity.stats_map()
        if not m:
            return 0
        window_latest = max((d.get('as_of') or '' for d in m.values() if d.get('as_of')), default='')
        codes = set(universe.filter_symbols(universe._db_symbols()))
        if p['universe_exclude_st']:
            codes -= universe.excluded_symbols()
        if p['market_cap_enabled']:
            cmap = market_cap.cap_map()
            cap = float(p['min_market_cap_yi']) * 1e8
            codes = {c for c in codes if c not in cmap or cmap[c] >= cap}
        n = 0
        for c in codes:
            d = m.get(c)
            if not d or not d.get('price') or (window_latest and d.get('as_of') != window_latest):
                # 最后一天没K线 = 已停止同步/长期停牌
                n += 1
        return n
    except Exception as e:
        print(f'[配置预览] 停同步只数统计失败: {type(e).__name__}: {e}')
        return 0


def preview_for(key: str, new_value: Any) -> Dict[str, Any]:
    """返回某一项改成 new_value 后的只数变化（带"为什么没变"的说明）。"""
    now = scope_count()
    after = scope_count({key: new_value})
    out = {'now': now, 'after': after,
           'delta': (after - now) if (now is not None and after is not None) else None}
    if out['delta'] == 0 and key in ('min_market_cap_yi', 'universe_main_board_only',
                                      'market_cap_enabled', 'universe_exclude_st'):
        stale = stale_no_sync_count({key: new_value})
        if stale:
            out['hint'] = (f'这一条放宽后本该放回 {stale} 只，但它们的日K已不在同步范围内'
                           f'（最后一条停在更早的交易日），所以实际仍是 {after} 只；'
                           f'要真的收回来得同时把它们纳入同步（config.KLINE_SYNC_UNIVERSE_ONLY 与市值口径），'
                           f'并等一次同步补齐历史。')
    return out


# ========== 口径摘要缓存（2026-10-30，页面 2s 内目标） ==========
# /api/settings 每次打开都现算四项摘要（数据范围/选票范围/预览一致性/停同步数），
# 实测：重启后首开 27.8s、universe 缓存过期后 5.3s、全热 <50ms。摘要只随
# "口径参数 / 日K库 / 范围缓存"变，没必要每次请求都算。整份摘要缓存化：
# 调度器启动预热 + 每 9 分钟随 universe 保活一起刷新；改「选股范围」组配置后
# 由 app_settings 触发一次后台重算；读取方永远拿缓存值（最多旧几秒~几分钟）。
_SUMMARY: Dict[str, Any] = {'ts': 0.0, 'data': None}
SUMMARY_REFRESH_SEC = 540.0   # 与调度器保活同节奏；超龄只触发后台重算，不阻塞请求


def compute_summary() -> Dict[str, Any]:
    """现算四项摘要并写入缓存（供 /api/settings 无快照时兜底、调度器与改配置后台调用）。"""
    from services.data import universe
    out: Dict[str, Any] = {}
    try:
        out.update({
            'dataScope': len(universe.eligible_symbols()),
            'selectableScope': universe.selectable_count(),
            'scopeText': universe.describe_selectable(),
        })
    except Exception as e:
        out['scopeError'] = str(e)[:120]
    try:
        p = parity_check()
        out['previewParity'] = p
        if not p.get('ok'):
            print(f"[配置] ⚠️ 预览逻辑与真实范围不一致（预览 {p.get('preview')} vs 真实 {p.get('actual')}）"
                  f" —— settings_preview 漏了条件，得补")
        out['staleOutOfSync'] = stale_no_sync_count()
    except Exception as e:
        out['previewError'] = f'{type(e).__name__}: {str(e)[:120]}'
    _SUMMARY.update(ts=time.time(), data=out)
    return out


def summary_snapshot() -> Optional[Dict[str, Any]]:
    """当前缓存的一份摘要；没有（重启后还没预热完）返回 None。"""
    return _SUMMARY['data']


def summary_age() -> float:
    return time.time() - _SUMMARY['ts']


def refresh_summary_async() -> None:
    """后台线程重算摘要（失败只打日志，读方继续用旧值）。"""
    def _run():
        try:
            compute_summary()
            print('[配置] 口径摘要后台重算完成')
        except Exception as e:
            print(f'[配置] 口径摘要后台重算失败: {e}')
    threading.Thread(target=_run, daemon=True, name='settings-summary').start()
