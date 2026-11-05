"""Domain router: hot."""

from fastapi import APIRouter
from ..routes_state import *

router = APIRouter()

@router.get("/api/hot_search_ranking", tags=["热搜榜数据"])
async def get_hot_search_ranking_api():
    try:
        data = load_hot_search_ranking()
        return {"success": True, "data": data}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取hot_search_ranking失败: {str(error)}")


@router.get("/api/update-hot_search_ranking", tags=["热搜榜数据"])
async def update_hot_search_ranking_api():
    try:
        print('[热搜榜] 手动触发更新...')
        data = await fetch_eastmoney_hot_list()
        save_hot_search_ranking(data)
        
        return {
            "success": True,
            "message": f"成功更新热搜榜数据，共 {data['filteredCount']} 只股票",
            "data": data
        }
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"更新热搜榜失败: {str(error)}")


@router.get("/api/hot-stocks", tags=["热门股票"])
async def get_hot_stock_buttons_api(user: Dict = Depends(current_user)):
    try:
        stocks = load_hot_stock_buttons(user_id=int(user.get('id') or 1))
        # 获取实时价格用于排序
        if stocks:
            try:
                symbols = ','.join([s['code'] for s in stocks])
                data = await fetch_with_retry(symbols)
                parsed = parse_tencent_batch_data(data)
                price_map = {p['symbol']: p.get('price', 0) for p in parsed}
                for s in stocks:
                    try:
                        s['price'] = float(price_map.get(s['code'], 0) or 0)
                    except (TypeError, ValueError):
                        s['price'] = 0.0
                # 按股价降序排序
                stocks.sort(key=lambda x: x.get('price', 0), reverse=True)
            except Exception as _e:
                print(f'[热门股票] 实时行情批量获取失败，price 置 0: {_e}')
                for s in stocks:
                    s['price'] = 0.0
        return {"success": True, "data": {"stocks": stocks, "count": len(stocks)}}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取热门股票失败: {str(error)}")


@router.post("/api/hot-stocks", tags=["热门股票"])
async def update_hot_stock_buttons_api(data: Dict, user: Dict = Depends(current_user)):
    try:
        stocks = data.get('stocks', [])
        if not isinstance(stocks, list):
            raise HTTPException(status_code=400, detail="stocks必须是数组")
        
        # 验证股票格式
        valid_stocks = []
        for s in stocks:
            if isinstance(s, dict) and s.get('code') and s.get('name'):
                if re.match(r'^(sh|sz)\d{6}$', s['code'].lower()):
                    valid_stocks.append({
                        'code': s['code'].lower(),
                        'name': s['name']
                    })
        
        save_hot_stock_buttons(valid_stocks, user_id=int(user.get('id') or 1))
        return {"success": True, "data": {"stocks": valid_stocks, "count": len(valid_stocks)}}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@router.put("/api/hot-stocks/add", tags=["热门股票"])
async def add_to_hot_stock_buttons_api(data: Dict, user: Dict = Depends(current_user)):
    try:
        code = data.get('code')
        name = data.get('name')
        
        if not code or not name:
            raise HTTPException(status_code=400, detail="参数缺失：需要code和name")
        
        if not re.match(r'^(sh|sz)\d{6}$', code.lower()):
            raise HTTPException(status_code=400, detail="股票代码格式无效")
        
        current_stocks = load_hot_stock_buttons(user_id=int(user.get('id') or 1))
        
        # 检查是否已存在
        exists = any(s['code'].lower() == code.lower() for s in current_stocks)
        if exists:
            return {"success": False, "error": "该股票已在热门股票中"}
        
        current_stocks.append({
            'code': code.lower(),
            'name': name
        })
        
        save_hot_stock_buttons(current_stocks, user_id=int(user.get('id') or 1))
        return {"success": True, "data": {"stocks": current_stocks, "count": len(current_stocks)}}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@router.put("/api/hot-stocks/remove", tags=["热门股票"])
async def remove_from_hot_stock_buttons_api(data: Dict, user: Dict = Depends(current_user)):
    try:
        code = data.get('code')
        if not code:
            raise HTTPException(status_code=400, detail="参数缺失：需要code")
        
        code_lower = code.lower()
        current_stocks = load_hot_stock_buttons(user_id=int(user.get('id') or 1))
        current_stocks = [s for s in current_stocks if s['code'].lower() != code_lower]
        
        save_hot_stock_buttons(current_stocks, user_id=int(user.get('id') or 1))
        return {"success": True, "data": {"stocks": current_stocks, "count": len(current_stocks)}}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))

