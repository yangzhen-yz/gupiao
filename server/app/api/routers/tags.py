"""Domain router: tags."""

from fastapi import APIRouter
from ..routes_state import *

router = APIRouter()

@router.get("/api/stock-tags", tags=["股票标签"])
async def get_stock_concept_tags_api():
    try:
        tags = load_stock_concept_tags()
        return {"success": True, "data": {"tags": tags, "count": len(tags)}}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取股票标签失败: {str(error)}")


@router.get("/api/stock-tags/{symbol}", tags=["股票标签"])
async def get_stock_concept_tags_by_symbol_api(symbol: str):
    try:
        tags = load_stock_concept_tags()
        return {"success": True, "data": {"symbol": symbol, "tags": tags.get(symbol.lower(), [])}}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取股票标签失败: {str(error)}")


@router.post("/api/stock-tags/{symbol}", tags=["股票标签"])
async def add_stock_concept_tags_api(symbol: str, data: Dict):
    try:
        tags_to_add = data.get('tags', [])
        if not isinstance(tags_to_add, list):
            tags_to_add = [str(tags_to_add)]
        
        updated_tags = add_stock_concept_tags(symbol, tags_to_add)
        return {"success": True, "data": {"symbol": symbol, "tags": updated_tags}}
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@router.delete("/api/stock-tags/{symbol}", tags=["股票标签"])
async def remove_stock_tag_api(symbol: str, data: Optional[Dict] = None):
    try:
        # 11-07 修⑩-6：原来写的是 `data: Dict = {}`——可变默认参数在 FastAPI 里是
        # "请求体可省"的老写法，默认值那个 dict 跨请求共享。本函数只读不改它，
        # 所以没造成过实际错误，但留着就是给下一个"顺手 data['x']=..."的人挖坑。
        # 换成 None，并补 isinstance：这样非法 body（字符串/列表）由 422 兜住，
        # 而不是在 data.get(...) 上抛 AttributeError 再被下面的 except 包成 500。
        if not isinstance(data, dict):
            data = {}
        tag_to_remove = data.get('tag') if data else None
        if not tag_to_remove:
            # 删除该股票的所有标签
            all_tags = load_stock_concept_tags()
            if symbol.lower() in all_tags:
                del all_tags[symbol.lower()]
                save_stock_concept_tags(all_tags)
            return {"success": True, "data": {"symbol": symbol, "tags": []}}
        else:
            # 删除单个标签
            updated_tags = remove_stock_tag(symbol, tag_to_remove)
            return {"success": True, "data": {"symbol": symbol, "tags": updated_tags}}
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@router.get("/api/stock-concepts/{symbol}", tags=["股票标签"])
async def fetch_stock_concepts_api(symbol: str, name: str = None):
    try:
        if not name:
            # 尝试从股票映射表获取名称
            stock_basic_info = load_stock_basic_info()
            name = stock_basic_info.get(symbol.lower(), symbol)
        
        result = await fetch_stock_concepts(symbol, name)
        return {"success": result.get('success', True), "data": result}
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))

