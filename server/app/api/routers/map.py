"""Domain router: map."""

from fastapi import APIRouter
from ..routes_state import *

router = APIRouter()

@router.get("/api/stock-map", tags=["股票映射"])
async def get_stock_basic_info_api():
    try:
        stock_basic_info = load_stock_basic_info()
        name_search = load_stock_alias_map()
        return {"success": True, "data": stock_basic_info, "nameSearch": name_search}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取股票映射失败: {str(error)}")


@router.post("/api/update-stock-map", tags=["股票映射"])
async def update_stock_basic_info_api(data: Dict):
    # 2026-10-17 修复：services/stock.py 已搬到 services/data/stock.py，
    # 旧写法 `import services.data.stock` + `services.stock.x` 会抛
    # AttributeError: module 'services' has no attribute 'stock'。
    from services.data import stock as _stock_data
    try:
        symbol = data.get('symbol')
        name = data.get('name')
        if not symbol or not name:
            raise HTTPException(status_code=400, detail="参数缺失")
        
        if is_forbidden_stock(name):
            return {"success": False, "error": "该股票属于禁忌类别，不予添加"}
        
        stock_basic_info = load_stock_basic_info()
        stock_basic_info[symbol.lower()] = name
        save_stock_basic_info(stock_basic_info)
        _stock_data._stock_basic_info_cache = stock_basic_info
        
        return {"success": True}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))

