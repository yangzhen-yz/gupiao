"""auto_optimizer.py - 系统自动优化引擎（V4.4）

两大自进化机制：
1. 趋势 D1-D4 维度权重反馈闭环（每日回测后运行）
2. 网格搜索最优参数组合（每周运行）

2026-10-28：角色体系整体下线，原"角色分类门槛自动优化"（Feature 2）随之删除。

设计原则：
- 所有优化都是渐进式的（每次微调，不突变）
- 保留优化历史，支持回滚
- 优化结果写入 DB，现有代码从 DB 加载（fallback 到 config 默认值）
"""
import json
from datetime import datetime
from typing import Dict
from db.database import (get_db_conn, load_trend_dimension_weights, save_trend_dimension_weights, save_optimization_history)
from app.config import (OPTIMIZER_OBJECTIVE_VERSION, OPTIMIZER_FORCE_WALK_FORWARD,
                        OPTIMIZER_DECAY_FACTOR, OPTIMIZER_DECAY_STREAK, OPTIMIZER_APPLY)
from services.ops.objective_v2 import (OBJECTIVE_V2_NAME, blended_spread_pp,
                                        has_usable_window, apply_decay_streak)

# 2026-11-01（T1.3 优化目标 C）：
#   v2 = 验证窗 D5−D1 分位差（0.6×1日 + 0.4×3日）+ 强制 walk-forward + 连续负向回落
#   v1 = 改造前的 discrimination_avg + top20 胜率口径（回滚用，行为与改造前一致）
USE_OBJECTIVE_V2 = (OPTIMIZER_OBJECTIVE_VERSION == 'v2')

# ========== 常量 ==========

# D1-D4 各维度满分（用于权重归一化）
DIMENSION_MAX_SCORES = {
    'D1': 30,  # TREND_D1_TREND_FULL(20) + max divergence penalty(10) → 实际满分30
    'D2': 30,  # max(consecutive_board_tiers[0][1], yang_line_tiers[0][1]) = 30
    'D3': 20,  # 10 + 5 + 5
    'D4': 20,  # drawdown_tiers[0][1]
}

# 权重调整参数
WEIGHT_ADJUST_DISCRIMINATION_THRESHOLD = 8.0   # 区分度阈值(%)
WEIGHT_ADJUST_STEP = 0.05                       # 每次调整步长(×1.05 或 ×0.95)
WEIGHT_MIN = 0.7                                # 权重下限
WEIGHT_MAX = 1.3                                # 权重上限
WEIGHT_MIN_SAMPLE = 20                          # 最小样本数

# 网格搜索参数
GRID_SEARCH_LOOKBACK_DAYS = 30                  # 回看天数
GRID_SEARCH_TOP_N = 20                          # 取前N只计算胜率
GRID_SEARCH_MIN_IMPROVEMENT = 2.0               # 最小改善幅度(%)才采纳新参数
GRID_SEARCH_3D_WEIGHT = 0.4                     # 目标函数中3日胜率权重（1日占1-该值）

# Walk-forward 样本外验证参数（2026-09-04，对标 IS-WFA-OOS 协议）
# 原实现用同一批30天数据既拟合又评估，存在过拟合风险。改为：
#   训练窗（较旧的20天）→ 产生候选调整；验证窗（最近10天）→ 检验调整是否泛化；
#   验证窗无改善则回滚不采纳。
WF_VALIDATION_DAYS = 10                          # 验证窗天数
WF_MIN_TRAIN_SAMPLES = 15                        # 训练窗最少样本
WF_MIN_VALID_SAMPLES = 8                         # 验证窗最少样本
WF_MIN_VALID_IMPROVEMENT = 1.0                   # 单项调整在验证窗的最少改善(%)


# ========== Walk-forward 工具 ==========

def _split_train_valid(rows, validation_days: int = WF_VALIDATION_DAYS):
    """按 predict_date 把记录切分为（训练窗, 验证窗）。验证窗为最近 validation_days 个交易日。"""
    dates = sorted({r['predict_date'] for r in rows if r['predict_date'] is not None})
    if len(dates) < 4:
        return rows, []
    n_valid = min(validation_days, len(dates) - 2)  # 训练窗至少保留2个交易日
    cutoff = dates[-n_valid]
    train = [r for r in rows if r['predict_date'] is not None and r['predict_date'] < cutoff]
    valid = [r for r in rows if r['predict_date'] is not None and r['predict_date'] >= cutoff]
    return train, valid


def _simulate_top_winrate(rows, w1: float, w2: float, w3: float, w4: float,
                          top_n: int = GRID_SEARCH_TOP_N,
                          correct_key: str = 'is_correct_1d') -> float:
    """用给定 D1-D4 权重重算总分，返回 top N 的胜率(%)（与网格搜索同一口径）"""
    if not rows:
        return 0.0
    max_possible = (DIMENSION_MAX_SCORES['D1'] * w1 +
                    DIMENSION_MAX_SCORES['D2'] * w2 +
                    DIMENSION_MAX_SCORES['D3'] * w3 +
                    DIMENSION_MAX_SCORES['D4'] * w4)
    if max_possible <= 0:
        return 0.0
    scored = []
    for r in rows:
        if r['s_d1'] is None:
            continue
        raw = (r['s_d1'] * w1 + r['s_d2'] * w2 + r['s_d3'] * w3 + r['s_d4'] * w4)
        scored.append({'score': raw / max_possible * 100, 'is_correct': r[correct_key]})
    if not scored:
        return 0.0
    scored.sort(key=lambda x: x['score'], reverse=True)
    top = scored[:top_n]
    wins = sum(1 for s in top if s['is_correct'] == 1)
    return wins / len(top) * 100


def _blended_objective(rows, w1: float, w2: float, w3: float, w4: float,
                       top_n: int = GRID_SEARCH_TOP_N) -> float:
    """多周期目标：0.6×1日胜率 + 0.4×3日胜率（避免纯1日噪声主导参数选择）

    3日样本不足时退化为纯1日胜率。
    """
    wr_1d = _simulate_top_winrate(rows, w1, w2, w3, w4, top_n, 'is_correct_1d')
    rows_3d = [r for r in rows if r['is_correct_3d'] is not None]
    if len(rows_3d) < top_n:
        return wr_1d
    wr_3d = _simulate_top_winrate(rows_3d, w1, w2, w3, w4, top_n, 'is_correct_3d')
    return wr_1d * (1 - GRID_SEARCH_3D_WEIGHT) + wr_3d * GRID_SEARCH_3D_WEIGHT


def _blended_spread_value(rows, w1: float, w2: float, w3: float, w4: float):
    """目标 C：D1~D5 五等分的 D5−D1 准确率差（pp，0.6×1日 + 0.4×3日）。

    分桶样本不足时返回 None（调用方必须按"验证不通过"处理，不能当 0 分）。
    """
    result = blended_spread_pp(rows, (w1, w2, w3, w4))
    return result.get('value') if result.get('ok') else None


# ========== Feature 1: D1-D4 维度权重反馈闭环 ==========

async def optimize_trend_dimension_weights() -> Dict:
    """趋势评分 D1-D4 维度权重自动优化（2026-09-04 V2：Walk-forward 样本外验证）

    算法：
    1. 加载最近30天已验证的趋势回测记录（含 s_d1~s_d4 分数）
    2. 切分训练窗（较旧）与验证窗（最近10个交易日）
    3. 对每个维度 D1-D4（在训练窗上）：
       a. 按该维度分数的中位数分高/低两组
       b. 计算高组 vs 低组的1日胜率差异（区分度）
       c. 区分度 > 8% → 权重 ×1.05（上限1.3）；< -8% → ×0.95（下限0.7）
    4. 样本外验证（V2 新增，防过拟合）：
       - v2（2026-11-01 目标 C）：用验证窗的 D5−D1 分位差改善（pp）判定，
         改善 < 1pp 或验证窗不足以分 5 桶 → 回滚；**没有验证窗一律不采纳**
       - v1（回滚口径）：验证窗 top20 胜率改善 < 1% → 回滚
    5. 回落（目标 C 新增）：候选连续 2 次在验证窗为负，权重向中性 1.0
       收缩 0.8（w ← 1 + (w-1)*0.8），不再只有单向下限
    6. 保存通过验证的权重到 DB，并把新旧两套 metric 同时写进 optimization_history

    Returns:
        {'adjustments': [...], 'summary': str}
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()

        # 加载最近30天已验证1日的回测记录（含 s_d1~s_d4）
        cursor.execute('''
            SELECT predict_date, s_d1, s_d2, s_d3, s_d4, is_correct_1d, is_correct_3d, actual_change_1d, market_change_1d
            FROM trend_backtest_records
            WHERE verified_1d = 1
              AND predict_date >= date('now', '-30 days')
              AND s_d1 IS NOT NULL
            ORDER BY predict_date DESC
        ''')
        rows = cursor.fetchall()
        conn.close()

        if len(rows) < WEIGHT_MIN_SAMPLE:
            return {
                'adjustments': [],
                'summary': f'样本不足({len(rows)}/{WEIGHT_MIN_SAMPLE})，跳过D1-D4权重优化',
            }

        # Walk-forward 切分：训练窗产生候选，验证窗检验泛化
        train_rows, valid_rows = _split_train_valid(rows)
        has_validation = len(valid_rows) >= WF_MIN_VALID_SAMPLES and len(train_rows) >= WF_MIN_TRAIN_SAMPLES
        if USE_OBJECTIVE_V2:
            # 目标 C：5 桶每桶至少 10 条才够算 D5−D1；不够就视为没有验证窗
            has_validation = has_validation and has_usable_window(valid_rows, WF_MIN_VALID_SAMPLES)

        # 加载当前权重
        weights = load_trend_dimension_weights()
        current_w = {k: v['weight'] for k, v in weights.items()}
        adjustments = []

        for dim in ['D1', 'D2', 'D3', 'D4']:
            col = f's_{dim.lower()}'
            dim_key = dim.lower()  # 'd1', 'd2', 'd3', 'd4'

            # 提取该维度分数（跳过 None），区分度在训练窗上计算
            fit_rows = train_rows if has_validation else rows
            scored_rows = [r for r in fit_rows if r[col] is not None]
            if len(scored_rows) < WEIGHT_MIN_SAMPLE:
                adjustments.append({
                    'dimension': dim, 'action': 'keep',
                    'reason': f'有效样本不足({len(scored_rows)}/{WEIGHT_MIN_SAMPLE})',
                })
                continue

            # 按中位数分高/低两组
            dim_vals = sorted([r[col] for r in scored_rows])
            median_val = dim_vals[len(dim_vals) // 2]

            high_group = [r for r in scored_rows if r[col] > median_val]
            low_group = [r for r in scored_rows if r[col] <= median_val]

            if not high_group or not low_group:
                adjustments.append({
                    'dimension': dim, 'action': 'keep', 'reason': '分组后样本为空',
                })
                continue

            # 计算各组1日胜率
            high_win = sum(1 for r in high_group if r['is_correct_1d'] == 1)
            low_win = sum(1 for r in low_group if r['is_correct_1d'] == 1)
            high_rate = high_win / len(high_group) * 100
            low_rate = low_win / len(low_group) * 100

            # 区分度 = 高组胜率 - 低组胜率
            discrimination = high_rate - low_rate

            old_weight = weights[dim_key]['weight']
            sample_count = len(scored_rows)
            correct_count = high_win + low_win

            # 更新元数据
            weights[dim_key]['accuracy'] = round((high_rate + low_rate) / 2, 2)
            weights[dim_key]['discrimination'] = round(discrimination, 2)
            weights[dim_key]['sample_count'] = sample_count
            weights[dim_key]['correct_count'] = correct_count

            # 权重调整（训练窗结论）
            if discrimination > WEIGHT_ADJUST_DISCRIMINATION_THRESHOLD:
                new_weight = min(WEIGHT_MAX, old_weight * (1 + WEIGHT_ADJUST_STEP))
                action = 'increase'
                reason = (f'训练窗区分度{discrimination:.1f}%(高组{high_rate:.0f}%>低组{low_rate:.0f}%)，'
                          f'权重{old_weight:.3f}→{new_weight:.3f}')
            elif discrimination < -WEIGHT_ADJUST_DISCRIMINATION_THRESHOLD:
                new_weight = max(WEIGHT_MIN, old_weight * (1 - WEIGHT_ADJUST_STEP))
                action = 'decrease'
                reason = (f'训练窗区分度{discrimination:.1f}%(高组{high_rate:.0f}%<低组{low_rate:.0f}%)，'
                          f'权重{old_weight:.3f}→{new_weight:.3f}')
            else:
                new_weight = old_weight
                action = 'keep'
                reason = f'训练窗区分度{discrimination:.1f}%在±{WEIGHT_ADJUST_DISCRIMINATION_THRESHOLD}%内，权重保持{old_weight:.3f}'

            # ---- 样本外验证门控 ----
            # v2：验证窗 D5−D1 改善 pp；v1：旧的验证窗 top20 胜率改善（回滚用）
            valid_base_pp = None
            valid_new_pp = None
            valid_imp_pp = None
            if action != 'keep':
                cw = [current_w.get('d1', 1.0), current_w.get('d2', 1.0),
                      current_w.get('d3', 1.0), current_w.get('d4', 1.0)]
                nw = list(cw)
                nw[int(dim[1]) - 1] = new_weight

                if OPTIMIZER_FORCE_WALK_FORWARD and not has_validation:
                    # 目标 C：没有可用验证窗 → 一律不采纳，不做"训练窗直接上"
                    reason = (f'{reason}；无可用验证窗（强制 walk-forward），不采纳，回滚为{old_weight:.3f}')
                    new_weight = old_weight
                    action = 'keep'
                elif has_validation and USE_OBJECTIVE_V2:
                    base_obj = _blended_spread_value(valid_rows, *cw)
                    new_obj = _blended_spread_value(valid_rows, *nw)
                    valid_base_pp, valid_new_pp = base_obj, new_obj
                    if base_obj is None or new_obj is None:
                        reason = (f'{reason}；验证窗不足以分 5 桶，无法算 D5−D1，'
                                  f'不采纳，回滚为{old_weight:.3f}')
                        new_weight = old_weight
                        action = 'keep'
                    else:
                        valid_imp_pp = round(new_obj - base_obj, 2)
                        if valid_imp_pp < WF_MIN_VALID_IMPROVEMENT:
                            reason = (f'{reason}；验证窗 D5−D1 改善{valid_imp_pp:+.1f}pp'
                                      f'（{base_obj:.1f}→{new_obj:.1f}）未达{WF_MIN_VALID_IMPROVEMENT}pp，'
                                      f'不采纳，回滚为{old_weight:.3f}')
                            new_weight = old_weight
                            action = 'keep'
                        else:
                            reason = (f'{reason}；验证窗 D5−D1 改善{valid_imp_pp:+.1f}pp'
                                      f'（{base_obj:.1f}→{new_obj:.1f}）通过')
                elif has_validation:
                    base_obj = _blended_objective(valid_rows, *cw)
                    new_obj = _blended_objective(valid_rows, *nw)
                    if new_obj < base_obj + WF_MIN_VALID_IMPROVEMENT:
                        reason = (f'{reason}；样本外验证未通过(验证窗当前{base_obj:.1f}%→调整后{new_obj:.1f}%)，'
                                  f'不采纳，回滚为{old_weight:.3f}')
                        new_weight = old_weight
                        action = 'keep'

            # ---- 目标 C 回落：连续 N 次验证窗为负 → 向中性 1.0 收缩 ----
            prev_streak = int(weights[dim_key].get('valid_neg_streak', 0) or 0)
            decay = apply_decay_streak(prev_streak, valid_imp_pp, new_weight,
                                       trigger=OPTIMIZER_DECAY_STREAK, factor=OPTIMIZER_DECAY_FACTOR)
            weights[dim_key]['valid_neg_streak'] = decay['streak']
            if decay['decayed']:
                new_weight = decay['new_weight']
                action = 'decay'
                reason = (f'{reason}；连续{OPTIMIZER_DECAY_STREAK}次验证窗为负，'
                          f'权重向1.0收缩至{new_weight:.4f}')

            weights[dim_key]['weight'] = round(new_weight, 4)

            adjustments.append({
                'dimension': dim, 'action': action,
                'old_weight': round(old_weight, 4),
                'new_weight': round(new_weight, 4),
                'discrimination': round(discrimination, 2),
                'high_accuracy': round(high_rate, 2),
                'low_accuracy': round(low_rate, 2),
                'sample_count': sample_count,
                'walk_forward_validated': has_validation,
                'valid_base_pp': valid_base_pp,
                'valid_new_pp': valid_new_pp,
                'valid_improvement_pp': valid_imp_pp,
                'valid_neg_streak': weights[dim_key]['valid_neg_streak'],
                'objective': 'v2_d5_minus_d1' if USE_OBJECTIVE_V2 else 'v1_discrimination',
                'reason': reason,
            })

        # 保存权重（OPTIMIZER_APPLY=False：照算指标、优化历史照记，但一律不写权重；
        # 跑 5~10 个交易日观察期后再放开 —— 2026-11-03 新增闸门）
        if OPTIMIZER_APPLY:
            save_trend_dimension_weights(weights)
        else:
            for a in adjustments:
                a['applied'] = False
                a['reason'] += '；OPTIMIZER_APPLY=False 只评估不采纳，权重未写入'
            print('[自动优化] OPTIMIZER_APPLY=False：本轮只评估，权重未写入（历史照记供审计）')

        # 记录优化历史（旧 metric 照旧写，供审计；新口径写 metric_v2_*）
        changed = [a for a in adjustments if a['action'] != 'keep']
        v2_vals = [a['valid_improvement_pp'] for a in adjustments
                   if a.get('valid_improvement_pp') is not None]
        metric_v2 = round(sum(v2_vals) / len(v2_vals), 2) if v2_vals else None
        save_optimization_history(
            module='trend_dimension_weights',
            parameter_set=json.dumps({k: v['weight'] for k, v in weights.items()}, ensure_ascii=False),
            metric_name='discrimination_avg',
            metric_value=round(sum(a.get('discrimination', 0) for a in adjustments) / max(len(adjustments), 1), 2),
            old_value=None,
            new_value=len(changed),
            details=json.dumps(adjustments, ensure_ascii=False),
            metric_v2_name=OBJECTIVE_V2_NAME if USE_OBJECTIVE_V2 else '',
            metric_v2_value=metric_v2,
            objective_version=OPTIMIZER_OBJECTIVE_VERSION,
            walk_forward=int(has_validation),
        )

        summary = (f'D1-D4权重优化完成({OPTIMIZER_OBJECTIVE_VERSION}): '
                   f'{len(changed)}/{len(adjustments)} 个维度调整'
                   + ('' if OPTIMIZER_APPLY else '（只评估，权重未写入）'))
        print(f'[自动优化] {summary}')
        return {'adjustments': adjustments, 'summary': summary}

    except Exception as e:
        print(f'[自动优化] D1-D4权重优化失败: {e}')
        return {'adjustments': [], 'summary': f'优化失败: {e}', 'error': str(e)}


# ========== Feature 2: 角色门槛自动优化（2026-10-28 随角色体系删除） ==========

# ========== Feature 3: 网格搜索最优参数 ==========

async def run_grid_search() -> Dict:
    """网格搜索 D1-D4 最优权重组合（每周运行，2026-09-04 V2：walk-forward + 多周期目标）

    算法：
    1. 定义权重搜索空间：D1/D2 各5档 [0.8~1.2]，D3/D4 各3档 [0.9~1.1]（225种组合）
    2. Walk-forward 切分：训练窗（较旧20天）/ 验证窗（最近10个交易日）
    3. 训练窗目标函数：
       - v2（2026-11-01 目标 C）：D5−D1 分位差，0.6×1日 + 0.4×3日
       - v1（回滚口径）：0.6×1日胜率 + 0.4×3日胜率
    4. 样本外验证门控（V2）：最优组合必须在验证窗上也超过当前权重 + 2 才采纳
       （训练窗选优、验证窗确认，双门槛防过拟合）；强制 walk-forward 打开时，
       没有可用验证窗一律不采纳

    Returns:
        {'best_params': {...}, 'current_winrate': float, 'best_winrate': float, 'adopted': bool, 'summary': str}
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()

        # 加载已验证的回测记录
        cursor.execute('''
            SELECT symbol, name, predict_date, s_d1, s_d2, s_d3, s_d4, score,
                   is_correct_1d, is_correct_3d, actual_change_1d, market_change_1d
            FROM trend_backtest_records
            WHERE verified_1d = 1
              AND s_d1 IS NOT NULL
              AND predict_date >= date('now', f'-{GRID_SEARCH_LOOKBACK_DAYS} days')
            ORDER BY predict_date DESC
        ''')
        rows = cursor.fetchall()
        conn.close()

        if len(rows) < 30:
            return {
                'best_params': None,
                'summary': f'样本不足({len(rows)}/30)，跳过网格搜索',
            }

        # 当前权重
        current_weights = load_trend_dimension_weights()
        current_w = {k: v['weight'] for k, v in current_weights.items()}
        cw = [current_w.get('d1', 1.0), current_w.get('d2', 1.0),
              current_w.get('d3', 1.0), current_w.get('d4', 1.0)]

        # Walk-forward 切分
        train_rows, valid_rows = _split_train_valid(rows)
        has_validation = len(valid_rows) >= WF_MIN_VALID_SAMPLES and len(train_rows) >= WF_MIN_TRAIN_SAMPLES
        if USE_OBJECTIVE_V2:
            has_validation = has_validation and has_usable_window(valid_rows, WF_MIN_VALID_SAMPLES)
        fit_rows = train_rows if has_validation else rows

        # 搜索空间：D1/D2 各5档，D3/D4 各3档 → 225种组合（V2：扩展 D3/D4）
        space_12 = [0.8, 0.9, 1.0, 1.1, 1.2]
        space_34 = [0.9, 1.0, 1.1]

        # 目标函数：v2 = D5−D1 分位差；v1 = 0.6×1日胜率 + 0.4×3日胜率（回滚用）
        objective_fn = _blended_spread_value if USE_OBJECTIVE_V2 else _blended_objective

        # 当前权重的训练窗目标值
        current_obj = objective_fn(fit_rows, *cw)
        if current_obj is None:
            return {
                'best_params': None,
                'summary': '训练窗样本不足以分 5 桶（D5−D1 无法计算），跳过网格搜索',
            }

        # 网格搜索（训练窗）
        best_obj = current_obj
        best_params = {'d1': cw[0], 'd2': cw[1], 'd3': cw[2], 'd4': cw[3]}

        all_results = []
        for w1 in space_12:
            for w2 in space_12:
                for w3 in space_34:
                    for w4 in space_34:
                        obj = objective_fn(fit_rows, w1, w2, w3, w4)
                        if obj is None:
                            continue
                        all_results.append({'d1': w1, 'd2': w2, 'd3': w3, 'd4': w4,
                                            'objective': round(obj, 2)})
                        if obj > best_obj:
                            best_obj = obj
                            best_params = {'d1': w1, 'd2': w2, 'd3': w3, 'd4': w4}

        # 样本外验证门控（V2）：验证窗上也要优于当前权重 + 阈值才采纳
        improvement = best_obj - current_obj
        adopted = improvement >= GRID_SEARCH_MIN_IMPROVEMENT
        validation_note = '无有效验证窗，仅训练窗判定'
        valid_base_obj = None
        valid_best_obj = None

        if adopted and has_validation:
            bw = [best_params['d1'], best_params['d2'], best_params['d3'], best_params['d4']]
            valid_base_obj = objective_fn(valid_rows, *cw)
            valid_best_obj = objective_fn(valid_rows, *bw)
            if valid_base_obj is None or valid_best_obj is None:
                adopted = False
                validation_note = '验证窗样本不足以分 5 桶，无法确认泛化，不采纳'
            elif valid_best_obj < valid_base_obj + GRID_SEARCH_MIN_IMPROVEMENT:
                adopted = False
                validation_note = (f'样本外验证未通过：验证窗当前{valid_base_obj:.1f} → 最优{valid_best_obj:.1f}，'
                                   f'改善不足{GRID_SEARCH_MIN_IMPROVEMENT}，判定为过拟合，不采纳')
            else:
                validation_note = f'样本外验证通过：验证窗当前{valid_base_obj:.1f} → 最优{valid_best_obj:.1f}'

        if adopted and OPTIMIZER_FORCE_WALK_FORWARD and not has_validation:
            adopted = False
            validation_note = '无可用验证窗（强制 walk-forward），不采纳'

        if adopted:
            # 应用新权重（OPTIMIZER_APPLY=False：只评估，验证结论照记历史但不写入）
            if OPTIMIZER_APPLY:
                for dim_key, new_w in best_params.items():
                    current_weights[dim_key]['weight'] = round(new_w, 4)
                save_trend_dimension_weights(current_weights)
            else:
                validation_note += '；OPTIMIZER_APPLY=False 只评估不采纳，权重未写入'

        # 记录优化历史
        valid_imp = None
        if valid_base_obj is not None and valid_best_obj is not None:
            valid_imp = round(valid_best_obj - valid_base_obj, 2)
        save_optimization_history(
            module='grid_search',
            parameter_set=json.dumps(best_params, ensure_ascii=False),
            metric_name='quintile_spread_pp' if USE_OBJECTIVE_V2 else 'blended_winrate_top20',
            metric_value=round(best_obj, 2),
            old_value=round(current_obj, 2),
            new_value=round(best_obj, 2),
            details=json.dumps({
                'adopted': adopted,
                'applied': bool(adopted and OPTIMIZER_APPLY),
                'improvement': round(improvement, 2),
                'objective': ('0.6*spread_1d + 0.4*spread_3d (D5-D1)' if USE_OBJECTIVE_V2
                              else '0.6*winrate_1d + 0.4*winrate_3d'),
                'walk_forward': has_validation,
                'validation_note': validation_note,
                'current_objective': round(current_obj, 2),
                'best_objective': round(best_obj, 2),
                'valid_base_pp': valid_base_obj,
                'valid_best_pp': valid_best_obj,
                'valid_improvement_pp': valid_imp,
                'sample_size': len(rows),
                'top_results': sorted(all_results, key=lambda x: x['objective'], reverse=True)[:5],
            }, ensure_ascii=False),
            metric_v2_name=OBJECTIVE_V2_NAME if USE_OBJECTIVE_V2 else '',
            metric_v2_value=valid_imp,
            objective_version=OPTIMIZER_OBJECTIVE_VERSION,
            walk_forward=int(has_validation),
        )

        unit = 'pp' if USE_OBJECTIVE_V2 else '%'
        adopt_txt = ('已采纳' if (adopted and OPTIMIZER_APPLY)
                     else ('已验证·未写入（APPLY关）' if adopted
                           else '未达阈值/验证未通过，未采纳'))
        summary = (f'网格搜索完成({OPTIMIZER_OBJECTIVE_VERSION}): 训练窗目标{current_obj:.1f}{unit} → 最优{best_obj:.1f}{unit} '
                   f'(+{improvement:.1f}), {adopt_txt}')
        print(f'[自动优化] {summary}')
        return {
            'best_params': best_params,
            'current_winrate': round(current_obj, 2),
            'best_winrate': round(best_obj, 2),
            'improvement': round(improvement, 2),
            'adopted': adopted,
            'walk_forward': has_validation,
            'validation_note': validation_note,
            'sample_size': len(rows),
            'summary': summary,
        }

    except Exception as e:
        print(f'[自动优化] 网格搜索失败: {e}')
        return {'best_params': None, 'summary': f'网格搜索失败: {e}', 'error': str(e)}


# ========== 编排器 ==========

async def run_auto_optimization(run_grid: bool = False) -> Dict:
    """运行自动优化（每日回测后调用）

    Args:
        run_grid: 是否同时运行网格搜索（每周一次由 scheduler 控制）
    """
    print(f'[自动优化] 开始运行 ({datetime.now():%Y-%m-%d %H:%M:%S})')
    results = {}

    # Feature 1: D1-D4 权重反馈
    results['dimension_weights'] = await optimize_trend_dimension_weights()

    # Feature 2（角色门槛优化）已于 2026-10-28 随角色体系删除

    # Feature 3: 网格搜索（可选）
    if run_grid:
        results['grid_search'] = await run_grid_search()

    print(f'[自动优化] 全部完成 ({datetime.now():%H:%M:%S})')
    return results
