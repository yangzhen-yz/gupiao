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
    东财：按**主机家族**的熔断状态（2026-11-06 起）。每个 scope 一行 + families 汇总，
    三种"停"分得清：人工暂停（manual）、本家族当日停调（familyStoppedToday）、
    IP 级全域闸门（domainCooling）。页面"数据为什么旧/为什么标了新浪兜底"能直接答。
    """
    from services.data import source_health, eastmoney_guard
    from app.config import (EASTMONEY_DAY_STOP_ENABLED, EASTMONEY_DAY_STOP_PROBE_SEC,
                            EASTMONEY_FAMILY_GATE_ENABLED, EASTMONEY_IP_GATE_FAMILIES)
    # 2026-11-06 修正：原来这里写死的 'datacenter' 是幽灵 scope（当时没有任何调用方
    # 用它计数，永远返回空状态），而真实存在的 holder/pools/appdata 反而读不到。
    scopes = ('clist', 'ulist', 'pools', 'datacenter', 'holder', 'appdata', 'ann', 'push2')
    return {
        "success": True,
        "data": {
            "quotes": {
                "tencent": source_health.status('quote_tencent'),
                "sina": source_health.status('quote_sina'),
                "route": quote_route_status(),
            },
            "eastmoney": {
                "policy": {"familyGate": EASTMONEY_FAMILY_GATE_ENABLED,
                           "dayStop": EASTMONEY_DAY_STOP_ENABLED,
                           "dayStopProbeSec": EASTMONEY_DAY_STOP_PROBE_SEC,
                           "ipGateFamilies": EASTMONEY_IP_GATE_FAMILIES},
                "scopes": {s: eastmoney_guard.status(s) for s in scopes},
                "families": eastmoney_guard.families_status(),
            },
        },
    }


@router.post("/api/data-sources/eastmoney-pause", tags=["运维"])
async def eastmoney_pause_api(payload: Dict, user: Dict = Depends(current_user)):
    """人工暂停东财取数（2026-11-06，管理员）。

    `{"hours": 48, "target": "all", "reason": "东财不让用，先用备源"}`
    target 可以是 'all'（默认）、家族名（main/pools/datacenter/appdata/notice/push2）
    或具体 scope（clist/ulist/…）。写进 `data/eastmoney_guard_state.json`，
    **看护重启后仍然是停的**；期间各消费方走已有的备源/旧值分支。
    hours<=0 等于不暂停。人工暂停优先级最高：一次请求成功也不会把它清掉。
    """
    from services.support import app_settings as A
    if not A.can_write(user):
        raise HTTPException(status_code=403, detail='只有管理员能暂停数据源')
    from services.data import eastmoney_guard
    try:
        hours = float(payload.get('hours') or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail='hours 必须是数字（小时）')
    if not (-1000 <= hours <= 1000):
        raise HTTPException(status_code=400, detail='hours 只接受 -1000~1000（0=不暂停）')
    target = str(payload.get('target') or 'all').strip() or 'all'
    reason = str(payload.get('reason') or '')[:120]
    res = eastmoney_guard.pause(hours=hours, target=target, reason=reason,
                                 actor=user.get('username', ''))
    print(f'[东财熔断] {user.get("username", "?")} 人工暂停 target={target} '
          f'hours={hours}（{reason}）')
    return {"success": True, "data": res}


@router.post("/api/data-sources/eastmoney-resume", tags=["运维"])
async def eastmoney_resume_api(payload: Dict, user: Dict = Depends(current_user)):
    """解除人工暂停（只解人工；自动冷却仍要自己到期或等一次成功）。"""
    from services.support import app_settings as A
    if not A.can_write(user):
        raise HTTPException(status_code=403, detail='只有管理员能恢复数据源')
    from services.data import eastmoney_guard
    target = str(payload.get('target') or 'all').strip() or 'all'
    res = eastmoney_guard.resume(target=target)
    print(f'[东财熔断] {user.get("username", "?")} 解除人工暂停 target={target}（原先{'在停' if res['wasPaused'] else '本就没停'}）')
    return {"success": True, "data": res}


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

