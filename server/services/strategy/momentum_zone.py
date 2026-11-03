"""momentum_zone.py - 推荐时"当日涨幅段"分区与闸门的唯一实现（2026-10-18，实施计划 B）

为什么要单独一个模块：分区这件事原来只以"半山腰罚分"的形态半躺在
`services/pick/recommend.py` 3.5 节 —— 只覆盖 4.5~9.8% 一段、用 abs() 把下跌股
也算进死亡区、还没有开关（想关只能改代码）。2026-10-18 的 218 条根因复盘
（`server/analysis_out/docs_backup_20261102/胜率低根因复盘-2026-10-18.md` §3）把区间补全了：当日涨幅 0~2% 与
4.5~9.8% 两段都是负期望（36.4%/-2.59%、40.0%/-2.27%），砍掉后剩 52.8%/+0.82%。
同一判据要被三个地方用：智能推荐主链（罚分或过滤）、影子 excluded（反事实留痕）、
盘中推送链（同一条闸门，不留"推荐不推、推送推"的漏口）——分三处各写一遍必然分叉，
所以收在这里，三边都调它。

口径要点（与复盘逐值对齐，勿凭感觉改区间）：
  · 弱势区   0 ≤ 涨幅 < weak_max(2.0)     没涨起来的票不是"安全"，是没人要
  · 甜点区   weak_max ≤ 涨幅 < chase_min(4.5) 不动
  · 死亡区   chase_min ≤ 涨幅 < chase_max(9.8) 接力资金最犹豫的半山腰
  · 涨停区   涨幅 ≥ chase_max(9.8)          按涨停对待，历史最强的一段，不拦
  · 负涨幅   一律不拦：复盘里负涨幅样本只有 2 条，没有统计意义。
    老代码用 abs() 把 -3% 也算进死亡区纯属误伤（-3% 与 +6% 不是一回事），
    本次显式修正为带符号判定。
  · 涨幅缺失（None/空串）不拦：老代码里缺失值 abs() 后是 0，同样不罚；
    没有数据不做判决，避免把接口抖动当成利空。

模式（app_settings 覆盖表现读，config 只是默认值）：
  off    不拦（老行为）
  demote 罚分：只压 score，票仍在候选里 —— 高分死亡区票照样能入选，这是 demote 的本意
  drop   过滤：不进候选；被闸票进影子 excluded，shadow_eval 自动算 T+1 反事实，
         配置改回 demote/off 即刻回退，不需要动代码、不需要重新训练什么。

默认 drop 而不是 demote：两段死亡区共 91 条、均值与超额双负，罚 4 分只是降低
入选概率、挡不死一只 85 分的死亡区票；而影子反事实是自动的（excluded 组照算 T+1），
两周后就有"闸门拦对了没有"的实测，不需要先相信这个结论。
"""
from typing import Dict, Optional

from app.config import (RECOMMEND_ZONE_GATE_ENABLED, RECOMMEND_ZONE_GATE_MODE,
                        RECOMMEND_ZONE_WEAK_MAX, RECOMMEND_ZONE_WEAK_PENALTY,
                        RECOMMEND_ZONE_CHASE_MIN, RECOMMEND_ZONE_CHASE_MAX,
                        RECOMMEND_ZONE_CHASE_PENALTY)

_ZONES: Dict[str, str] = {
    'unknown': '涨幅缺失（不闸）',
    'down':    '负涨幅（样本不足不闸）',
    'weak':    '弱势区',
    'sweet':   '甜点区',
    'chase':   '半山腰死亡区',
    'limit':   '涨停区',
}
# 只有这两个区是复盘实测的负期望段
_DEAD = ('weak', 'chase')


def _setting(key: str, default):
    """覆盖表里能改的就现读（复盘子页"口径与开关"），读不到退回 config 默认值。"""
    try:
        from services.support import app_settings
        if key in [s['key'] for s in app_settings.SPEC]:
            return app_settings.value(key)
    except Exception:
        pass
    return default


def cfg() -> Dict:
    """当前生效的涨幅段配置（每次调用现读，覆盖表改完即刻生效）。"""
    return {
        'enabled': bool(_setting('zone_gate_enabled', RECOMMEND_ZONE_GATE_ENABLED)),
        'mode': str(_setting('zone_gate_mode', RECOMMEND_ZONE_GATE_MODE) or 'off').lower(),
        'weak_max': float(_setting('zone_weak_max', RECOMMEND_ZONE_WEAK_MAX)),
        'chase_min': float(_setting('zone_chase_min', RECOMMEND_ZONE_CHASE_MIN)),
        'chase_max': float(_setting('zone_chase_max', RECOMMEND_ZONE_CHASE_MAX)),
        'weak_penalty': int(_setting('zone_weak_penalty', RECOMMEND_ZONE_WEAK_PENALTY)),
        'chase_penalty': int(_setting('zone_chase_penalty', RECOMMEND_ZONE_CHASE_PENALTY)),
    }


def is_drop_mode() -> bool:
    """是否处于过滤模式（总闸关着时一律不算）。"""
    c = cfg()
    return c['enabled'] and c['mode'] == 'drop'


def evaluate(chg, c: Optional[Dict] = None) -> Dict:
    """把一个当日涨幅值分到区里。返回 {'key','label','dead','penalty','chg','mode','enabled'}。

    `dead` = 落在负期望段（弱势区/死亡区）；`penalty` = demote 模式要罚的分（其他区为 0）。
    带符号判定：负涨幅与涨幅缺失都不算死亡区（见模块 docstring）。
    """
    c = c or cfg()
    try:
        v = float(chg)
    except (TypeError, ValueError):
        v = None
    if v is None:
        key = 'unknown'
    elif v < 0:
        key = 'down'
    elif v < c['weak_max']:
        key = 'weak'
    elif v < c['chase_min']:
        key = 'sweet'
    elif v < c['chase_max']:
        key = 'chase'
    else:
        key = 'limit'
    penalty = 0
    if key == 'weak':
        penalty = c['weak_penalty']
    elif key == 'chase':
        penalty = c['chase_penalty']
    return {'key': key, 'label': _ZONES[key], 'dead': key in _DEAD, 'penalty': penalty,
            'chg': v, 'mode': c['mode'], 'enabled': c['enabled']}


def gate_stock(stock: Dict, c: Optional[Dict] = None) -> Optional[Dict]:
    """drop 模式专用：票落在死亡区就返回分区 dict（调用方据此过滤），否则 None。

    非 drop 模式（off/demote）一律返回 None —— demote 的本意是"罚分但可选"，
    推送链没有评分可罚，硬拦就会跟主链口径不一致（见模块 docstring）。
    """
    c = c or cfg()
    if not c['enabled'] or c['mode'] != 'drop':
        return None
    z = evaluate(stock.get('changePercent'), c)
    return z if z['dead'] else None


def penalty_stock(stock: Dict, c: Optional[Dict] = None) -> Optional[Dict]:
    """demote 模式专用：票落在死亡区就返回分区 dict（调用方据此罚分），否则 None。"""
    c = c or cfg()
    if not c['enabled'] or c['mode'] != 'demote':
        return None
    z = evaluate(stock.get('changePercent'), c)
    return z if (z['dead'] and z['penalty']) else None
