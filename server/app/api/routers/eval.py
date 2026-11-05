"""Domain router: eval."""

from fastapi import APIRouter
from ..routes_state import *

router = APIRouter()

@router.get("/api/eval-lab/latest", tags=["复盘"])
async def eval_lab_latest_api(user: Dict = Depends(current_user)):
    try:
        mod = _eval_lab_module()
        data = await asyncio.to_thread(mod.latest_run)
        data['running'] = _EVAL_LAB_STATE['running']
        data['startedAt'] = _EVAL_LAB_STATE['startedAt']
        return {"success": True, "data": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'因子体检结果读取失败: {type(e).__name__}: {e}')


@router.post("/api/eval-lab/run", tags=["复盘"])
async def eval_lab_run_api(user: Dict = Depends(current_user)):
    from services.support import app_settings as A
    if not A.can_write(user):
        raise HTTPException(status_code=403, detail='当前账号无权触发全量因子体检')
    if _EVAL_LAB_STATE['running']:
        return {"success": True, "data": {'started': False, 'why': '上一轮还在跑',
                                         'startedAt': _EVAL_LAB_STATE['startedAt']}}
    import threading

    def _work():
        try:
            mod = _eval_lab_module()
            r = mod.run_analysis()
            print(f"[因子体检] 手动触发完成: {r.get('runDate')} / {r.get('interpreter')} / "
                  f"{r.get('universe')} 只 / 5日超额 {((r.get('recommendations') or {}).get('ret5') or {}).get('excessMean')}%")
        except Exception as e:
            print(f'[因子体检] 手动触发失败: {type(e).__name__}: {e}')
        finally:
            _EVAL_LAB_STATE['running'] = False

    _EVAL_LAB_STATE.update(running=True, startedAt=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    threading.Thread(target=_work, daemon=True, name='eval-lab-run').start()
    return {"success": True, "data": {'started': True, 'startedAt': _EVAL_LAB_STATE['startedAt']}}

