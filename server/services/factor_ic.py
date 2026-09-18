"""factor_ic.py - 因子 IC/IC_IR 评价引擎（2026-09-04，对标多因子体系最佳实践）

背景：原 strategy._adjust_weights 用"中位数分组区分度"（高组胜率-低组胜率）衡量因子
预测力，仅提取 1 bit 信息且对分组边界敏感。市面主流多因子体系（华泰金工《因子合成方法
实证分析》、米筐因子研究、果仁网）均以 Rank IC 序列为核心指标：
  - IC：每个截面日，因子值与次日收益的 Spearman 秩相关系数
  - IC 均值：因子方向性预测力
  - IC 标准差：预测力稳定性
  - IC_IR = IC均值 / IC标准差：风险调整后的因子质量（华泰实证中"最大化IC_IR"合成最优）
  - IC>0 占比：因子效果一致性

本模块职责：
  1. compute_factor_ic_stats：从 strategy_predict_records 计算各因子滚动窗口 IC 统计
     （收益用"次日超额收益"= actual_change - market_change，剔除大盘 beta）
  2. adjust_weights_by_ic_ir：基于 IC_IR 的渐进式权重自适应（保留原步长/上下限防护）
  3. 与原"区分度"逻辑互为补充：样本不足时上层回退旧逻辑，避免冷启动不可用

Rank IC 对因子值的单调变换不变，因此库中存的是原始值还是档位分数均可计算。
"""
from typing import Dict, List, Optional


# 参与评价的因子列（strategy_predict_records 已持久化的因子）
FACTOR_COLUMNS = [
    'outer_ratio', 'volume_ratio', 'turnover_rate', 'change_percent',
    'weibi', 'avg_price_deviation', 'amplitude', 'position',
    'main_capital_inflow', 'holder_structure',
]

# IC 计算参数
IC_MIN_CROSS_SECTION = 10   # 单个截面日最少样本数（少于则该日不参与IC）
IC_MIN_DAYS = 5             # 最少截面日数（少于则整体回退旧逻辑）

# IC_IR 权重自适应参数（与原 _adjust_weights 的防护边界一致）
IC_IR_THRESHOLD = 0.30      # |IC_IR| 超过该值才调整权重
IC_IR_STEP = 0.05           # 每次调整步长（×1.05 / ×0.95）
IC_POS_RATIO_CONFIRM = 0.40 # IC 方向一致占比低于该值时，同向下调加码确认
WEIGHT_MIN = 0.3            # 权重下限（与原逻辑一致）
WEIGHT_MAX = 2.0            # 权重上限（与原逻辑一致）


def _rank(values: List[float]) -> List[float]:
    """平均秩（tie 取平均），Spearman 用"""
    indexed = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and values[indexed[j + 1]] == values[indexed[i]]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_rank
        i = j + 1
    return ranks


def spearman_corr(xs: List[float], ys: List[float]) -> Optional[float]:
    """Spearman 秩相关（手写，无 scipy 依赖）。样本<3 或零方差返回 None"""
    n = len(xs)
    if n != len(ys) or n < 3:
        return None
    rx, ry = _rank(xs), _rank(ys)
    mean_x = sum(rx) / n
    mean_y = sum(ry) / n
    cov = sum((rx[i] - mean_x) * (ry[i] - mean_y) for i in range(n))
    var_x = sum((rx[i] - mean_x) ** 2 for i in range(n))
    var_y = sum((ry[i] - mean_y) ** 2 for i in range(n))
    if var_x <= 0 or var_y <= 0:
        return None
    return cov / (var_x * var_y) ** 0.5


def compute_factor_ic_stats(rows: List[Dict], lookback_days: int = 30) -> Dict[str, Dict]:
    """计算各因子滚动窗口 Rank IC 统计

    Args:
        rows: 已验证记录，须含 predict_date, actual_change, market_change + 因子列。
              收益使用超额收益（actual_change - market_change）剔除大盘 beta。
        lookback_days: 仅用于结果记录，过滤由调用方 SQL 完成

    Returns:
        {factor: {ic_mean, ic_std, ic_ir, ic_positive_ratio, n_days, n_total}}
    """
    # 按截面日分组
    by_date: Dict[str, List[Dict]] = {}
    for r in rows:
        d = r.get('predict_date')
        if d and r.get('actual_change') is not None:
            by_date.setdefault(d, []).append(r)

    dates = sorted(by_date.keys())
    factor_stats: Dict[str, Dict] = {}

    for factor in FACTOR_COLUMNS:
        ic_series: List[float] = []
        n_total = 0
        for d in dates:
            group = by_date[d]
            xs, ys = [], []
            for r in group:
                fv = r.get(factor)
                if fv is None or fv == '':
                    continue
                try:
                    fv = float(fv)
                except (TypeError, ValueError):
                    continue
                actual = float(r['actual_change'])
                market = float(r.get('market_change') or 0)
                xs.append(fv)
                ys.append(actual - market)  # 超额收益
            if len(xs) < IC_MIN_CROSS_SECTION:
                continue
            ic = spearman_corr(xs, ys)
            if ic is not None:
                ic_series.append(ic)
                n_total += len(xs)

        if len(ic_series) < IC_MIN_DAYS:
            continue  # 截面日不足，上层回退旧逻辑

        n = len(ic_series)
        ic_mean = sum(ic_series) / n
        ic_std = (sum((v - ic_mean) ** 2 for v in ic_series) / n) ** 0.5
        ic_ir = ic_mean / ic_std if ic_std > 0 else 0.0
        factor_stats[factor] = {
            'ic_mean': round(ic_mean, 4),
            'ic_std': round(ic_std, 4),
            'ic_ir': round(ic_ir, 4),
            'ic_positive_ratio': round(sum(1 for v in ic_series if v > 0) / n, 4),
            'n_days': n,
            'n_total': n_total,
        }

    return factor_stats


def adjust_weights_by_ic_ir(weights: Dict, ic_stats: Dict[str, Dict],
                            current_weights_meta: Optional[Dict] = None) -> List[Dict]:
    """基于 IC_IR 的因子权重自适应（渐进式，含防护边界）

    调整规则（华泰"IC_IR 加权"思想的保守落地）：
      - IC_IR > +0.30：因子有稳定正向预测力 → 权重 ×1.05（上限 WEIGHT_MAX）
      - IC_IR < -0.30：因子有稳定反向预测力（误导性） → 权重 ×0.95（下限 WEIGHT_MIN）
      - |IC_IR| ≤ 0.30：预测力不显著，保持
      - 附加确认：IC 正占比 < 40% 且 IC_IR < 0（反向且不稳定）→ 额外压低（×0.95 两次）
      - IC 均值为正但占比 < 50%（方向反复）→ 保持不调，避免噪声驱动

    Returns:
        adjustments 明细列表（与原 _adjust_weights 输出结构兼容，便于前端/历史记录复用）
    """
    adjustments = []

    for factor, stat in ic_stats.items():
        if factor not in weights:
            continue
        ic_ir = stat['ic_ir']
        ic_mean = stat['ic_mean']
        pos_ratio = stat['ic_positive_ratio']
        current_weight = weights[factor]['weight']
        old_weight = current_weight

        # 元数据回写（供前端展示因子画像；accuracy 列映射为 IC 方向一致占比，保持字段语义有效）
        weights[factor]['ic_mean'] = stat['ic_mean']
        weights[factor]['ic_ir'] = stat['ic_ir']
        weights[factor]['ic_positive_ratio'] = stat['ic_positive_ratio']
        weights[factor]['ic_days'] = stat['n_days']
        weights[factor]['accuracy'] = round(stat['ic_positive_ratio'] * 100, 2)
        weights[factor]['correct_count'] = stat['n_total']

        # 方向反复（占比<50%）时不信 IC 均值，只看强反向才动
        direction_unstable = pos_ratio < 0.5

        if ic_ir > IC_IR_THRESHOLD and not direction_unstable:
            new_weight = min(WEIGHT_MAX, current_weight * (1 + IC_IR_STEP))
            action = 'increase'
            reason = (f'IC_IR={ic_ir:.2f}(IC均值{ic_mean:.3f},方向一致{pos_ratio:.0%})，'
                      f'因子稳定有效，权重{old_weight:.3f}→{new_weight:.3f}')
        elif ic_ir < -IC_IR_THRESHOLD:
            new_weight = max(WEIGHT_MIN, current_weight * (1 - IC_IR_STEP))
            if pos_ratio < IC_POS_RATIO_CONFIRM:
                new_weight = max(WEIGHT_MIN, new_weight * (1 - IC_IR_STEP))
            action = 'decrease'
            reason = (f'IC_IR={ic_ir:.2f}(IC均值{ic_mean:.3f},方向一致{pos_ratio:.0%})，'
                      f'因子近期反向/失效，权重{old_weight:.3f}→{new_weight:.3f}')
        else:
            new_weight = current_weight
            action = 'keep'
            reason = f'IC_IR={ic_ir:.2f}在±{IC_IR_THRESHOLD}内或方向不稳定，权重保持{old_weight:.3f}'

        weights[factor]['weight'] = round(new_weight, 4)
        adjustments.append({
            'factor': factor, 'action': action,
            'old_weight': round(old_weight, 4), 'new_weight': round(new_weight, 4),
            'reason': reason,
            'ic_mean': stat['ic_mean'], 'ic_ir': stat['ic_ir'],
            'ic_positive_ratio': stat['ic_positive_ratio'],
            'n_days': stat['n_days'],
        })

    return adjustments
