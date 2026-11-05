"""Domain router: auth."""

from fastapi import APIRouter
from ..routes_state import *

router = APIRouter()

@router.post("/api/auth/register", tags=["用户认证"])
async def api_register(payload: Dict):
    """注册账号（首个注册者 id=1，天然认领迁移过来的存量持仓）"""
    try:
        from app.config import AUTH_ALLOW_REGISTER
        if not AUTH_ALLOW_REGISTER:
            raise HTTPException(status_code=403, detail="系统未开放注册，请联系管理员开号")
        ok, message, user = create_user(
            payload.get('username', ''), payload.get('password', ''),
            payload.get('displayName', '') or payload.get('display_name', ''))
        if not ok:
            raise HTTPException(status_code=400, detail=message)
        # 注册成功直接签发会话，省去再登录一次
        user2, token, err = authenticate(payload.get('username', ''), payload.get('password', ''))
        if err:
            return {"success": True, "data": {"user": user, "token": None},
                    "message": f"{message}，请手动登录"}
        return {"success": True, "data": {"user": user2, "token": token}, "message": message}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"注册失败: {str(error)}")


@router.post("/api/auth/login", tags=["用户认证"])
async def api_login(payload: Dict):
    """账号密码登录，返回 token（默认有效期 30 天）"""
    try:
        user, token, err = authenticate(payload.get('username', ''), payload.get('password', ''))
        if err:
            raise HTTPException(status_code=401, detail=err)
        return {"success": True, "data": {"user": user, "token": token}, "message": "登录成功"}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"登录失败: {str(error)}")


@router.post("/api/auth/logout", tags=["用户认证"])
async def api_logout(request: Request):
    """注销当前会话"""
    try:
        token = request.headers.get('X-Auth-Token', '')
        auth_header = request.headers.get('Authorization', '')
        if not token and auth_header.lower().startswith('bearer '):
            token = auth_header[7:]
        auth_logout(token)
        return {"success": True, "message": "已退出登录"}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"退出失败: {str(error)}")


@router.get("/api/auth/me", tags=["用户认证"])
async def api_me(request: Request):
    """当前登录用户；未登录返回 success=false（前端据此显示登录页，不抛 401）"""
    try:
        token = request.headers.get('X-Auth-Token', '')
        auth_header = request.headers.get('Authorization', '')
        if not token and auth_header.lower().startswith('bearer '):
            token = auth_header[7:]
        user = get_user_by_token(token)
        if not user:
            return {"success": False, "data": None, "message": "未登录"}
        return {"success": True, "data": {"user": user}, "message": "ok"}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"校验失败: {str(error)}")


@router.get("/api/auth/users", tags=["用户认证"])
async def api_users(user: Dict = Depends(current_user)):
    """用户列表（供已登录账号之间切换用）。

    2026-10-13 加鉴权：原来免登录就能拿到全部用户名，等于把账号列表挂在登录页上。
    系统经 cloudflare 隧道短暂对外期间，这一步和 AUTH_ALLOW_REGISTER=False 一起做，
    陌生人打开链接只能看到一个空登录框。前端登录页的快捷切换已改读本机记录。
    """
    try:
        return {"success": True, "data": list_users()}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取用户列表失败: {str(error)}")


@router.get("/api/auth/status", tags=["用户认证"])
async def api_auth_status():
    """认证功能是否开启 / 是否已有账号 / 是否开放注册（登录页初始化用）"""
    from app.config import AUTH_ENABLED, AUTH_ALLOW_REGISTER
    return {"success": True, "data": {
        "enabled": AUTH_ENABLED,
        "hasUser": has_any_user(),
        "allowRegister": AUTH_ALLOW_REGISTER,
    }}


@router.post("/api/auth/change-password", tags=["用户认证"])
async def api_change_password(payload: Dict, user: Dict = Depends(current_user)):
    """修改当前用户密码（原密码校验，成功后吊销全部会话需重新登录）"""
    try:
        ok, message = change_password(user['id'], payload.get('oldPassword', ''),
                                      payload.get('newPassword', ''))
        if not ok:
            raise HTTPException(status_code=400, detail=message)
        return {"success": True, "message": message}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"修改密码失败: {str(error)}")

