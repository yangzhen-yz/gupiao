"""Domain router: strategy."""

from fastapi import APIRouter
from ..routes_state import *

router = APIRouter()

@router.get("/api/strategy/weights", tags=["策略管理"])
async def get_strategy_factor_weights():
    """获取当前策略权重和各因子准确率"""
    try:
        weights = load_strategy_factor_weights()
        result = {}
        for factor, data in weights.items():
            result[factor] = {
                'weight': data['weight'],
                'accuracy': data['accuracy'],
                'sample_count': data['sample_count'],
                'correct_count': data['correct_count'],
                'description': FACTOR_DESCRIPTIONS.get(factor, factor),
            }
        return {"success": True, "data": result}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取策略权重失败: {str(error)}")


@router.post("/api/strategy/weights/reset", tags=["策略管理"])
async def reset_strategy_factor_weights():
    """重置策略权重为默认值"""
    try:
        weights = {k: {'weight': v, 'accuracy': 50.0, 'sample_count': 0, 'correct_count': 0}
                   for k, v in DEFAULT_WEIGHTS.items()}
        save_strategy_factor_weights(weights)
        return {"success": True, "message": "策略权重已重置为默认值"}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"重置策略权重失败: {str(error)}")


@router.post("/api/strategy/backtest", tags=["策略管理"])
async def run_backtest():
    """手动触发回测验证"""
    try:
        result = await backtest_yesterday_predictions()
        return {"success": True, "data": result}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"回测失败: {str(error)}")


@router.get("/api/strategy/backtest-history", tags=["策略管理"])
async def get_backtest_history(days: int = STRATEGY_BACKTEST_HISTORY_DAYS):
    """获取回测历史报告"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM strategy_backtest_reports ORDER BY backtest_date DESC LIMIT ?',
            (days,)
        )
        rows = cursor.fetchall()
        conn.close()
        # 解析 TEXT 字段
        import json
        result_rows = []
        for row in rows:
            d = dict(row)
            if d.get('misjudge_analysis'):
                try:
                    parsed = json.loads(d['misjudge_analysis'])
                    if isinstance(parsed, dict) and 'suggestions' in parsed:
                        parsed['suggestions'] = [
                            s.replace('建议增加委比验证条件', '已增加委比交叉验证：委比>30但股价下跌时降分，委比高但外盘占比<48%或量比<0.8时降分')
                            for s in parsed['suggestions']
                        ]
                    d['misjudge_analysis'] = parsed
                except Exception as _e:
                    print(f'[回测历史] misjudge_analysis JSON 解析失败(id={d.get("id")}): {_e}')
            if d.get('weight_adjustments'):
                try:
                    d['weight_adjustments'] = json.loads(d['weight_adjustments'])
                except Exception as _e:
                    print(f'[回测历史] weight_adjustments JSON 解析失败(id={d.get("id")}): {_e}')
            result_rows.append(d)
        return {"success": True, "data": result_rows}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取回测历史失败: {str(error)}")


@router.get("/api/strategy/predictions", tags=["策略管理"])
async def get_predictions(date_str: str = None, verified: int = None, include_today_and_pending: bool = False):
    """获取预测记录"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        sql = 'SELECT * FROM strategy_predict_records WHERE 1=1'
        params = []
        
        if include_today_and_pending:
            import datetime
            today = datetime.datetime.now().strftime('%Y-%m-%d')
            sql += ' AND (predict_date = ? OR verified = 0)'
            params.append(today)
        else:
            if date_str:
                sql += ' AND predict_date = ?'
                params.append(date_str)
            if verified is not None:
                sql += ' AND verified = ?'
                params.append(verified)
                
        sql += f' ORDER BY predict_date DESC, score DESC LIMIT {STRATEGY_BACKTEST_PREDICTION_LIMIT}'
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        return {"success": True, "data": rows}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取预测记录失败: {str(error)}")


@router.get("/api/optimization/history", tags=["自动优化"])
async def get_optimization_history(module: str = '', limit: int = 50):
    """查询自动优化历史"""
    from db.database import load_optimization_history
    module_param = module if module else None
    data = load_optimization_history(module=module_param, limit=limit)
    return {"success": True, "data": data}


@router.get("/api/optimization/status", tags=["自动优化"])
async def get_optimization_status():
    """查询当前优化状态（D1-D4权重）
    2026-10-28 角色体系下线：不再返回 roleThresholds / rolePerformance。"""
    from db.database import load_trend_dimension_weights
    dim_weights = load_trend_dimension_weights()
    return {
        "success": True,
        "data": {
            "dimensionWeights": dim_weights,
        }
    }


@router.post("/api/optimization/trigger", tags=["自动优化"])
async def trigger_optimization(run_grid: bool = False):
    """手动触发自动优化"""
    from services.ops.auto_optimizer import run_auto_optimization
    result = await run_auto_optimization(run_grid=run_grid)
    return {"success": True, "data": result}


@router.get("/api/optimization/trend", tags=["自动优化"])
async def get_optimization_trend():
    """优化趋势聚合数据（前端面板展示用）"""
    from db.database import get_db_conn
    conn = get_db_conn()
    cursor = conn.cursor()

    # 1. 权重变化趋势（按天聚合 optimization_history 中 trend_dimension_weights 模块）
    cursor.execute('''
        SELECT created_at, parameter_set, metric_value, details
        FROM optimization_history
        WHERE module = 'trend_dimension_weights'
        ORDER BY created_at DESC LIMIT 60
    ''')
    weight_history = [dict(r) for r in cursor.fetchall()]

    # 2. 网格搜索结果历史（2026-10-28 删掉"角色近期表现"：角色体系已下线）
    cursor.execute('''
        SELECT created_at, metric_value as best_winrate,
               old_value as current_winrate,
               new_value - old_value as improvement,
               parameter_set
        FROM optimization_history
        WHERE module = 'grid_search'
        ORDER BY created_at DESC LIMIT 10
    ''')
    grid_history = [dict(r) for r in cursor.fetchall()]

    # 3. 全量优化统计
    cursor.execute('SELECT count(*) as cnt FROM optimization_history')
    total_opt = cursor.fetchone()['cnt']

    cursor.execute('''
        SELECT count(*) as cnt FROM optimization_history
        WHERE created_at >= date('now', '-7 days')
    ''')
    week_opt = cursor.fetchone()['cnt']

    conn.close()

    return {
        "success": True,
        "data": {
            "weightHistory": weight_history,
            "gridHistory": grid_history,
            "stats": {
                "totalOptimizations": total_opt,
                "weeklyOptimizations": week_opt,
            }
        }
    }

