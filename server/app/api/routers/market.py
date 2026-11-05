"""Domain router: market."""

from fastapi import APIRouter
from ..routes_state import *

router = APIRouter()

@router.get("/api/auction/latest", tags=["复盘"])
async def auction_latest_api(date_str: str = ''):
    """09:25 竞价快照（只读）。date_str 为空取今天；用于核对定时任务是否真的采到。"""
    from services.data.auction import load_day
    day = date_str or datetime.now().strftime('%Y-%m-%d')
    try:
        rows = await asyncio.to_thread(load_day, day)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'竞价快照读取失败: {type(e).__name__}: {e}')
    return {"success": True, "data": {'date': day, 'n': len(rows), 'items': rows[:300]}}


@router.get("/api/market-predict", tags=["大盘预测"])
async def get_market_predict_api():
    """获取大盘预测数据（基于主力资金流向的预测）"""
    try:
        result = await predict_market_risk()
        return {"success": True, "data": result}
    except Exception as error:
        print(f'[大盘预测API] 错误: {str(error)}')
        raise HTTPException(status_code=500, detail=f"获取大盘预测失败: {str(error)}")


@router.get("/api/market-alert", tags=["大盘预警"])
async def get_market_alert_api():
    """获取最新大盘风险预警（前端轮询用，轻量级）"""
    try:
        alert = load_market_alert()
        return {"success": True, "data": alert}
    except Exception as error:
        return {"success": True, "data": None}


@router.get("/api/market-index", tags=["大盘指数"])
async def get_market_index_api():
    """获取大盘指数数据（SWR：60s 新鲜 + 后台刷新，2026-11-04）"""
    try:
        result, state = await _swr.async_swr_get(
            _MISC_SWR_CACHE, ('market-index',), lambda: check_market_crash(),
            ttl=60, stale_max=3600, label='market-index', gap=20, max_keys=16)
        return {"success": True, "data": result, "stale": state == 'stale'}
    except Exception as error:
        print(f'[大盘指数API] 错误: {str(error)}')
        raise HTTPException(status_code=500, detail=f"获取大盘指数失败: {str(error)}")


@router.get("/api/action-desk", tags=["今日行动台"])
async def get_action_desk_api(phase: str = '', user: Dict = Depends(current_user)):
    """今日行动台：按当前交易时段聚合"现在该做什么"（前端首页唯一数据源）。

    只读聚合，全部走 DB / 文件 / 5 秒行情缓存，不触发任何全市场扫描。
    phase 参数仅供联调与截图脚本强制时段（如 ?phase=tail），线上不传即可。
    """
    try:
        data = await build_action_desk(user_id=int(user.get('id') or 1),
                                       phase_override=(phase or '').strip())
        return {"success": True, "data": data}
    except Exception as error:
        print(f'[行动台API] 错误: {str(error)}')
        raise HTTPException(status_code=500, detail=f"生成行动台数据失败: {str(error)}")


@router.get("/api/theme-index", tags=["今日行动台"])
async def get_theme_index_api(theme: str = 'auto', days: int = 0, user: Dict = Depends(current_user)):
    """主题篮子温度（2026-10-31）。只做"现在有多热 + 是否已走坏"，不做顶底预测。

    theme=auto（默认）：先算齐所有链，返回近 5 日最强的那条，并在 chains 里带各链摘要；
    theme=<id>：指定链。2026-10-31 起链由 `hot_chains` 按财联社板块榜每天动态选出，
    所以 id 是板块代码（如 cls80014），固定链 robot/space/compute 只在动态链不可用时兜底。
    days>0 时把返回的序列裁到最近 N 个交易日；默认 120。行情只在盘中相位拉，
    其它时段直接读本地日K与 theme_member_daily（有内存缓存，60s/600s）。
    """
    from services.ops import theme_index
    theme = (theme or 'auto').strip().lower()
    if theme != 'auto' and not theme_index.known(theme):
        raise HTTPException(status_code=400, detail=f"未知主题: {theme}")
    try:
        data, state = await _swr.async_swr_get(
            _MISC_SWR_CACHE, ('theme-index', theme, days),
            lambda: theme_index.snapshot(theme, live=True),
            ttl=60, stale_max=3600, label='theme-index', gap=60, max_keys=16)
        if days and data.get('series'):
            data = {**data, 'series': data['series'][-max(1, min(days, 250)):]}
        if state == 'stale':
            data = {**data, 'stale': True}
        return {"success": True, "data": data}
    except Exception as error:
        print(f'[主题温度API] 错误: {str(error)}')
        raise HTTPException(status_code=500, detail=f"生成主题温度失败: {str(error)}")


@router.get("/api/cls-ladder", tags=["今日行动台"])
async def get_cls_ladder_api(user: Dict = Depends(current_user)):
    """财联社题材梯队（2026-10-31，影子）：四池 + 看盘头部 + 板块榜。

    只展示，不进选股评分/买入门槛/推送。外部接口 0.6~0.9s，盘中 60s / 休市 600s 缓存；
    失败时回退上一次成功快照，并带 stale 标记。网页接口不支持历史日期，只能逐日留档。
    """
    from services.ops import cls_pool
    try:
        data, state = await _swr.async_swr_get(
            _MISC_SWR_CACHE, ('cls-ladder',), lambda: cls_pool.snapshot(live=True),
            ttl=60, stale_max=3600, label='cls-ladder', gap=60, max_keys=16)
        if state == 'stale' and isinstance(data, dict):
            data = {**data, 'stale': True}
        return {"success": True, "data": data}
    except Exception as error:
        backup = cls_pool.peek(86400) or cls_pool.peek_from_db()
        if backup:
            return {"success": True, "data": {**backup, 'stale': True, 'error': str(error)[:120]}}
        print(f'[财联社梯队API] 错误: {str(error)}')
        raise HTTPException(status_code=503, detail=f"财联社梯队暂不可用: {str(error)}")


@router.get("/api/cls-plate-stocks", tags=["收评"])
async def get_cls_plate_stocks_api(code: str, user: Dict = Depends(current_user)):
    """财联社板块成分股（2026-10-31，收评「板块轮动」弹窗用）。

    只接受 clsXXXXX（东财 BK 代码走原有 `/eastmoney/...` 代理）；按涨跌幅降序，最多 60 只。
    财联社不提供换手率，这里不造该字段，前端按“有则显示”处理。
    """
    from services.ops import cls_plate
    try:
        rows = await asyncio.to_thread(cls_plate.members, code, 60)
        return {"success": True, "data": rows}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    except Exception as error:
        print(f'[财联社板块成分API] 错误: {str(error)}')
        raise HTTPException(status_code=503, detail=f"财联社板块成分暂不可用: {str(error)}")

