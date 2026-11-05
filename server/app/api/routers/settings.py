"""Domain router: settings."""

from fastapi import APIRouter
from ..routes_state import *

router = APIRouter()

@router.get("/api/internal/ops-notify-limit", tags=["内部"])
async def ops_notify_limit_api(request: Request):
    """仓库外守护脚本（backend_watchdog / tunnel_watch）发运维通知前调用：取"每天最多几条"上限。

    只接受本机调用；后端挂了时脚本用默认值兜底，不依赖本接口。返回页面对应的
    ops_notify_daily_limit（live 即时生效），读不到时回落 2。
    """
    host = (request.client.host if request.client else '') or ''
    if host not in ('127.0.0.1', '::1', 'localhost'):
        raise HTTPException(status_code=403, detail='localhost only')
    from services.support import app_settings as A
    try:
        limit = int(A.value('ops_notify_daily_limit'))
    except Exception:
        limit = 2
    return {"success": True, "limit": limit}


@router.get("/api/settings", tags=["配置"])
async def get_settings_api(user: Dict = Depends(current_user)):
    """列出可改项（当前值/默认值/是否被覆盖/生效方式）+ 口径摘要与改动记录。

    2026-10-17 提速：口径摘要四项（范围/选票/预览一致性/陈旧数）改为 to_thread 并行，
    热路径全命中时 <50ms。
    2026-10-30 再提速：摘要整份缓存化（settings_preview.compute_summary），调度器
    启动预热 + 每 9 分钟保活刷新，改「选股范围」配置后由 app_settings 触发后台重算。
    请求只读快照，实测冷路径 27.8s / 过期 5.3s 从此不再出现在请求路径上；
    只有快照缺失（重启后预热未完成的窗口）才由本请求现算兜底。
    """
    from services.support import app_settings as A, settings_preview as P
    items = A.list_items()

    _snap = P.summary_snapshot()
    if _snap is None:
        summary = await asyncio.to_thread(P.compute_summary)
    else:
        summary = dict(_snap)
        if P.summary_age() > P.SUMMARY_REFRESH_SEC:
            P.refresh_summary_async()
    return {"success": True, "data": {'items': items, 'summary': summary,
                                      'history': A.history(15),
                                      'canWrite': A.can_write(user),
                                      'writeIds': list(A.write_ids())}}


@router.get("/api/settings/preview", tags=["配置"])
async def preview_setting_api(key: str, value: str, user: Dict = Depends(current_user)):
    """"改成这个值会怎样" —— 界面切换/改数时按需调一次（约 0.3~0.8 秒，不做全量预取）。"""
    from services.support import app_settings as A, settings_preview as P
    if key not in [s['key'] for s in A.SPEC]:
        raise HTTPException(status_code=404, detail=f'不允许修改的配置项: {key}')
    incoming = (value.lower() in ('1', 'true', 'on', 'yes', '开')) if A.spec(key)['kind'] == 'bool' else value
    ok, val = A.coerce(key, incoming)
    if not ok:
        raise HTTPException(status_code=400, detail=val)
    return {"success": True, "data": P.preview_for(key, val)}


@router.post("/api/settings/{key}", tags=["配置"])
async def set_setting_api(key: str, body: Dict, user: Dict = Depends(current_user)):
    from services.support import app_settings as A
    if not A.can_write(user):
        raise HTTPException(status_code=403, detail=(
            f'当前账号无权修改口径（可改账号 id={A.write_ids()}，默认是最早注册的账号）；'
            '这些开关一改就影响所有账号的选票范围'))
    res = A.set_value(key, body.get('value'), user=str(user.get('username') or user.get('id')))
    if not res.get('ok'):
        raise HTTPException(status_code=400, detail=res.get('error') or '写入失败')
    print(f"[配置] {user.get('username') or user.get('id')} 改了 {key} = {res['value']}"
          f"（{'即时生效' if res['live'] else '需重启才生效'}）")
    return {"success": True, "data": res}


@router.post("/api/settings/{key}/reset", tags=["配置"])
async def reset_setting_api(key: str, user: Dict = Depends(current_user)):
    from services.support import app_settings as A
    if not A.can_write(user):
        raise HTTPException(status_code=403, detail='当前账号无权修改口径（默认只有最早注册的账号能改）')
    res = A.reset_value(key, user=str(user.get('username') or user.get('id')))
    if not res.get('ok'):
        raise HTTPException(status_code=400, detail=res.get('error') or '清除失败')
    return {"success": True, "data": res}

