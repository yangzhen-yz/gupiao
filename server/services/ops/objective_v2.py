"""objective_v2.py - 优化目标 C 的纯函数实现（2026-11-01，T1.3）

口径（与 `server/analysis_out/docs_backup_20261102/评分与龙头优化-开发任务清单-2026-11-01.md` T1.3 一致）：

1. 目标函数改为 **D5−D1 分位差**：按综合分从高到低分 5 桶，取
   `top_bucket_accuracy - bottom_bucket_accuracy`（单位 pp）。原来的
   `discrimination_avg` 是"中位数高/低两组差"，只用了 1 bit，这里改成 5 桶。
2. **强制 walk-forward**：没有验证窗就不采纳任何权重调整（由调用方执行，
   本模块只提供 `has_usable_window()`）。
3. **回落**：候选在验证窗上的改善 < 0 记一次负向；连续 `streak_trigger`
   次负向，权重向中性 1.0 收缩 `factor` 倍：`w ← 1 + (w-1) * 0.8`。
   收缩而不是直接乘 0.8，是为了让 1.0 以下的低权重也向中性回，而不是
   被"衰减"继续推低。

设计约束：
- 纯函数，不读库、不写库、不读全局配置；调用方传入所有输入。
- 旧口径 `discrimination_avg` 不在这里删除，由调用方并存写库做审计。
"""
from typing import Dict, List, Optional, Sequence

OBJECTIVE_V2_NAME = 'valid_improvement_pp'
NEUTRAL_WEIGHT = 1.0
DEFAULT_DECAY_FACTOR = 0.8
DEFAULT_DECAY_STREAK = 2
DEFAULT_BUCKETS = 5
MIN_BUCKET_SAMPLES = 10   # 单桶少于该数时判定"窗口不可用"，不产生结论

# 与 auto_optimizer 的 DIMENSION_MAX_SCORES 保持一致（此处只做归一化用）
_DIM_MAX = {'d1': 30.0, 'd2': 30.0, 'd3': 20.0, 'd4': 20.0}


def _get(row, key):
    """同时兼容 dict 与 sqlite3.Row（Row 缺列抛 IndexError）"""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _composite(r: Dict, weights: Sequence[float]) -> float:
    """按 D1-D4 权重算综合分（与线上同一归一化方式）"""
    w1, w2, w3, w4 = weights
    denom = (_DIM_MAX['d1'] * w1 + _DIM_MAX['d2'] * w2 +
             _DIM_MAX['d3'] * w3 + _DIM_MAX['d4'] * w4)
    if denom <= 0:
        return 0.0
    return (r['s_d1'] * w1 + r['s_d2'] * w2 + r['s_d3'] * w3 + r['s_d4'] * w4) / denom * 100


def bucket_spread_pp(rows: List[Dict], weights: Sequence[float],
                     correct_key: str = 'is_correct_1d',
                     buckets: int = DEFAULT_BUCKETS,
                     min_bucket: int = MIN_BUCKET_SAMPLES) -> Dict:
    """综合分分桶后的 D5−D1 准确率差（pp）。

    返回 {'ok': bool, 'n': int, 'spread_pp': float|None,
          'top_rate': float|None, 'bottom_rate': float|None,
          'per_bucket': [{'bucket': 1..5, 'n': int, 'rate': float}], 'reason': str}
    """
    usable = [r for r in rows
              if _get(r, 's_d1') is not None and _get(r, correct_key) is not None]
    out = {'ok': False, 'n': len(usable), 'spread_pp': None,
           'top_rate': None, 'bottom_rate': None, 'per_bucket': [], 'reason': ''}
    if len(usable) < buckets * min_bucket:
        out['reason'] = f'样本不足({len(usable)}<{buckets}×{min_bucket})'
        return out

    scored = sorted(((_composite(r, weights), r) for r in usable), key=lambda x: x[0], reverse=True)
    n = len(scored)
    base = n // buckets
    sizes = [base] * buckets
    for i in range(n - base * buckets):   # 余数给最前面的桶（高分桶）
        sizes[i] += 1

    per_bucket, idx = [], 0
    for b, size in enumerate(sizes, start=1):
        chunk = scored[idx:idx + size]
        idx += size
        wins = sum(1 for _, r in chunk if r[correct_key] == 1)
        per_bucket.append({'bucket': b, 'n': len(chunk), 'rate': round(wins / len(chunk) * 100, 2)})

    out.update({
        'ok': True,
        'spread_pp': round(per_bucket[0]['rate'] - per_bucket[-1]['rate'], 2),
        'top_rate': per_bucket[0]['rate'],
        'bottom_rate': per_bucket[-1]['rate'],
        'per_bucket': per_bucket,
    })
    return out


def blended_spread_pp(rows: List[Dict], weights: Sequence[float],
                      buckets: int = DEFAULT_BUCKETS,
                      weight_3d: float = 0.4) -> Dict:
    """1 日与 3 日分位差的加权目标（0.6×1日 + 0.4×3日）。

    3 日样本不足时只用 1 日，并在 `fell_back_to_1d` 标记，避免"看起来用了 3 日"。
    """
    one = bucket_spread_pp(rows, weights, 'is_correct_1d', buckets)
    if not one['ok']:
        return {'ok': False, 'value': None, 'spread_1d': one, 'spread_3d': None,
                'fell_back_to_1d': True, 'reason': one['reason']}
    three = bucket_spread_pp(rows, weights, 'is_correct_3d', buckets)
    if not three['ok']:
        return {'ok': True, 'value': one['spread_pp'], 'spread_1d': one, 'spread_3d': None,
                'fell_back_to_1d': True, 'reason': '3日样本不足，仅用1日'}
    value = one['spread_pp'] * (1 - weight_3d) + three['spread_pp'] * weight_3d
    return {'ok': True, 'value': round(value, 2), 'spread_1d': one, 'spread_3d': three,
            'fell_back_to_1d': False, 'reason': ''}


def has_usable_window(valid_rows: List[Dict], min_samples: int,
                      buckets: int = DEFAULT_BUCKETS) -> bool:
    """强制 walk-forward 的窗口判据：验证窗里 1 日标签足够分 5 桶。"""
    if not valid_rows or len(valid_rows) < max(min_samples, buckets * MIN_BUCKET_SAMPLES):
        return False
    return True


def decay_weight(weight: float, factor: float = DEFAULT_DECAY_FACTOR,
                 neutral: float = NEUTRAL_WEIGHT) -> float:
    """向中性收缩：w ← neutral + (w - neutral) * factor"""
    return round(neutral + (weight - neutral) * factor, 4)


def apply_decay_streak(prev_streak: int, improvement_pp: Optional[float],
                       weight: float, trigger: int = DEFAULT_DECAY_STREAK,
                       factor: float = DEFAULT_DECAY_FACTOR) -> Dict:
    """根据本次验证窗改善更新连续负向计数，必要时执行回落。

    improvement_pp:
      - None 表示本次没有产生候选（或没有可用验证窗）→ 计数保持不变
      - <0 记一次负向；>=0 清零
    """
    if improvement_pp is None:
        return {'streak': int(prev_streak), 'decayed': False,
                'new_weight': weight, 'triggered': False}
    streak = int(prev_streak) + 1 if improvement_pp < 0 else 0
    if streak >= trigger:
        return {'streak': 0, 'decayed': True,
                'new_weight': decay_weight(weight, factor), 'triggered': True}
    return {'streak': streak, 'decayed': False,
            'new_weight': weight, 'triggered': False}
