"""services/utils.py - 跨模块共享的工具函数（2026-07-06 合并重复实现）

将 routes.py / trend.py / recommend.py 中重复定义的工具函数集中到此，
避免分叉和维护成本。仅放纯函数，不放有状态/有副作用的逻辑。
"""
from typing import Dict, Any
from datetime import date


def ensure_today_date(data: Dict) -> Dict:
    """将趋势扫描结果数据的 date 字段统一改为今天，原值保留到 scanDate。

    用于前端展示：scanDate 是实际扫描日期，date 是展示日期（始终为今天）。
    重复定义曾存在于 routes.py:258 和 trend.py:53，2026-07-06 合并到此。
    """
    if data:
        data['scanDate'] = data.get('date', '')
        data['date'] = date.today().isoformat()
    return data


def to_float(v: Any) -> float:
    """容错转 float：None / '' / '-' / 非数值 → 0.0

    东方财富 API 未公布字段常返回 '-' 或 None，直接 float() 会抛异常。
    重复定义曾存在于 recommend.py:55（局部函数），2026-07-06 合并到此。
    """
    try:
        return float(v) if v not in (None, '', '-') else 0.0
    except (TypeError, ValueError):
        return 0.0
