"""Domain router: system."""

from fastapi import APIRouter
from ..routes_state import *

router = APIRouter()

@router.get("/api/health", tags=["健康检查"])
async def health_check():
    return {"status": "ok", "time": datetime.now().isoformat()}


@router.get("/api/data-sources/health", tags=["健康检查"])
async def data_sources_health():
    """数据源健康台账（2026-10-19，只读，不发网络请求）。

    行情：腾讯主源 / 新浪备源的连续失败计数与当前路由（是否处于腾讯冷却期）。
    东财：clist / ulist / datacenter 三个 scope 的熔断与全域闸门状态。
    页面“数据为什么旧/为什么标了新浪兜底”可以直接在这里找到答案。
    """
    from services.data import source_health, eastmoney_guard
    return {
        "success": True,
        "data": {
            "quotes": {
                "tencent": source_health.status('quote_tencent'),
                "sina": source_health.status('quote_sina'),
                "route": quote_route_status(),
            },
            "eastmoney": {
                # 2026-10-20：加 'ann'——公告接口当天也接入了共享熔断（此前是闸门覆盖缺口）
                scope: eastmoney_guard.status(scope)
                for scope in ('clist', 'ulist', 'datacenter', 'ann')
            },
        },
    }


@router.get("/api/api-usage", tags=["运维"])
async def api_usage_api(user: Dict = Depends(current_user)):
    """第三方接口每日调用计数与上限预警（只读）。管理员可改上限。

    用途：改代码前先看"哪个接口快撞上限"，避免把东财/腾讯/新浪等平台的日调用量推过封禁阈值。
    """
    from services.data import api_usage
    from services.support import app_settings as A
    items = api_usage.get_stats()
    history = api_usage.get_history(14)
    return {
        "success": True,
        "data": {
            "items": items,
            "history": history,
            "canWrite": A.can_write(user),
            "writeIds": list(A.write_ids()),
            "alertThreshold": api_usage.ALERT_THRESHOLD * 100,
        },
    }


@router.post("/api/api-usage/limits", tags=["运维"])
async def api_usage_set_limit_api(body: Dict, user: Dict = Depends(current_user)):
    """修改某接口的每日上限（管理员）。key 形如 'eastmoney:clist'。"""
    from services.data import api_usage
    from services.support import app_settings as A
    if not A.can_write(user):
        raise HTTPException(status_code=403, detail='当前账号无权修改接口调用上限（默认仅最早注册的账号可改）')
    key = (body or {}).get('key')
    limit = (body or {}).get('limit')
    if not key or ':' not in key:
        raise HTTPException(status_code=400, detail='key 格式应为 provider:interface，例如 eastmoney:clist')
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail='limit 必须是正整数')
    if limit <= 0:
        raise HTTPException(status_code=400, detail='limit 必须是正整数')
    api_usage.set_limit(key, limit, updated_by=str(user.get('username') or user.get('id')))
    print(f"[接口用量] {user.get('username') or user.get('id')} 改了 {key} 上限 = {limit}")
    return {"success": True, "data": {"key": key, "limit": limit}}

