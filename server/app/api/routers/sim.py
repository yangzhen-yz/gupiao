"""Domain router: sim."""

from fastapi import APIRouter
from ..routes_state import *

router = APIRouter()

@router.get("/api/sim/state", tags=["模拟交易"])
async def sim_state_api(user: Dict = Depends(current_user)):
    """获取模拟账户 + 持仓 + 待执行建议 + 审计日志"""
    try:
        from services.portfolio.trade_sim import get_sim_state
        return await get_sim_state(user['id'])
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取模拟账户失败: {str(error)}")


@router.post("/api/sim/generate", tags=["模拟交易"])
async def sim_generate_api(payload: Dict, user: Dict = Depends(current_user)):
    """从信号源生成建议订单。body: {sources:[...], symbols:[...]}"""
    try:
        from services.portfolio.trade_sim import generate_proposals
        from asyncio import wait_for as _wait_for
        sources = payload.get('sources') or ['tomorrow_plan', 'diagnosis', 'screener']
        symbols = payload.get('symbols') or None
        # 2026-11-03 修复：整体加 25s 硬超时兜底，超时返回 504 而非让前端干等/无限挂起。
        # 前端已放宽到 30s，这里更紧，保证后端先响应。
        return await _wait_for(
            generate_proposals(user['id'], sources, symbols), timeout=25)
    except TimeoutError:
        raise HTTPException(status_code=504, detail="生成耗时过长（已触发超时保护），请稍后重试")
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"生成建议失败: {str(error)}")


@router.post("/api/sim/execute", tags=["模拟交易"])
async def sim_execute_api(user: Dict = Depends(current_user)):
    """执行模拟成交（按实时价撮合 pending 建议，仅在模拟账户记账）"""
    try:
        from services.portfolio.trade_sim import execute_sim
        return await execute_sim(user['id'])
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"模拟成交失败: {str(error)}")


@router.post("/api/sim/sell", tags=["模拟交易"])
async def sim_sell_api(payload: Dict, user: Dict = Depends(current_user)):
    """模拟盘手动卖出（按实时价立即成交）。body: {symbol, qty?}；不传 qty = 全部"""
    try:
        from services.portfolio.trade_sim import manual_sell
        r = await manual_sell(user['id'], payload.get('symbol') or '', payload.get('qty'))
        if not r.get('success'):
            raise HTTPException(status_code=400, detail=r.get('message') or '卖出失败')
        return r
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"卖出失败: {str(error)}")


@router.post("/api/sim/reset", tags=["模拟交易"])
async def sim_reset_api(user: Dict = Depends(current_user)):
    """重置模拟账户（清空持仓/订单/现金回到初始）"""
    try:
        from services.portfolio.trade_sim import reset_sim
        return reset_sim(user['id'])
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"重置失败: {str(error)}")

