"""Domain router: kline."""

from fastapi import APIRouter
from ..routes_state import *

router = APIRouter()

@router.get("/api/kline/{symbol}", tags=["K线数据"])
async def get_kline(symbol: str):
    if not re.match(r'^(sh|sz)\d{6}$', symbol.lower()):
        raise HTTPException(status_code=400, detail="无效的股票代码格式")

    try:
        kline_data = await get_kline_data(symbol.lower())
        if not kline_data:
            return {"success": False, "error": "获取K线数据失败或数据不足"}

        dates = [k[0] for k in kline_data]
        values = [[float(k[1]), float(k[2]), float(k[4]), float(k[3])] for k in kline_data]
        vols = [float(k[5]) if len(k) > 5 and k[5] else 0 for k in kline_data]
        ma5 = calculate_ma(kline_data, 5)
        ma10 = calculate_ma(kline_data, 10)
        ma20 = calculate_ma(kline_data, 20)

        name = await get_stock_name(symbol) or symbol

        return {
            "success": True,
            "data": {
                "symbol": symbol,
                "name": name,
                "dates": dates,
                "values": values,
                "vols": vols,
                "ma5": ma5,
                "ma10": ma10,
                "ma20": ma20
            }
        }
    except Exception as error:
        print(f'[K线API] 错误: {str(error)}')
        raise HTTPException(status_code=500, detail=f"获取K线数据失败: {str(error)}")


@router.get("/api/kline/{symbol}/minute", tags=["K线数据"])
async def get_minute_kline(symbol: str):
    if not re.match(r'^(sh|sz)\d{6}$', symbol.lower()):
        raise HTTPException(status_code=400, detail="无效的股票代码格式")
    
    try:
        symbol_lower = symbol.lower()
        url = f'https://ifzq.gtimg.cn/appstock/app/minute/query?_var=min_data&code={symbol_lower}'

        # -fix: 改用 requests + asyncio.to_thread（去 httpx）
        response = await asyncio.to_thread(_sync_get, url)
        response.raise_for_status()
        text = response.text
        
        if not text.startswith('min_data='):
            return {"success": False, "error": "分时数据格式异常"}
        
        json_str = text[len('min_data='):]
        data = json.loads(json_str)
        
        if data.get('code') != 0:
            return {"success": False, "error": f"获取分时数据失败: code={data.get('code')}"}
        
        stock_data = data.get('data', {}).get(symbol_lower, {})
        minute_data = stock_data.get('data', {}).get('data', [])
        qt_data = stock_data.get('qt', {}).get(symbol_lower, [])
        
        if not minute_data:
            return {"success": False, "error": "暂无分时数据"}
        
        times = []
        prices = []
        vols = []
        amounts = []
        
        prev_close = 0
        if len(qt_data) > 4:
            prev_close = float(qt_data[4]) if qt_data[4] else 0
        
        for item in minute_data:
            parts = item.split(' ')
            if len(parts) >= 3:
                time_str = parts[0]
                formatted_time = f'{time_str[:2]}:{time_str[2:4]}'
                price = float(parts[1])
                vol = int(parts[2])
                amt = float(parts[3]) if len(parts) > 3 else 0
                times.append(formatted_time)
                prices.append(price)
                vols.append(vol)
                amounts.append(amt)
        
        name = await get_stock_name(symbol) or symbol
        
        return {
            "success": True,
            "data": {
                "symbol": symbol,
                "name": name,
                "times": times,
                "prices": prices,
                "vols": vols,
                "prevClose": prev_close
            }
        }
    except Exception as error:
        print(f'[分时K线API] 错误: {str(error)}')
        raise HTTPException(status_code=500, detail=f"获取分时数据失败: {str(error)}")


@router.get("/api/kline-history/stats", tags=["本地历史K线库"])
async def kline_history_stats_api():
    """查看本地历史日K线库统计（股票数/K线行数/数据区间）。

    A 级优化：RPS/趋势/评分的区间涨幅从此本地库向量化计算，绕开东方财富限流。
    若 symbolCount=0 表示尚未回填，需先执行 `python scripts/backfill_kline.py --mode backfill`。
    """
    try:
        from services.data.kline_history import get_engine, history_available

        def _compute():
            eng = get_engine()
            s = eng.get_db_stats(with_scope=True)
            s["available"] = history_available()
            return s

        # 346 万行全表统计实测 1.44s，原先直接压在事件循环上会拖住全站。
        # 丢线程池 + 5 分钟 SWR 缓存；过期先返旧值、后台刷新。
        stats, state = await asyncio.to_thread(
            _swr.swr_get, _MISC_SWR_CACHE, ('kline-stats',), _compute,
            ttl=300, stale_max=3600, label='kline-stats', gap=60, max_keys=16)
        return {"success": True, "data": stats, "stale": state == 'stale'}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取历史库统计失败: {str(error)}")


@router.post("/api/kline-history/sync", tags=["本地历史K线库"])
async def kline_history_sync_api():
    """后台触发本地历史日K线库每日增量同步（baostock 前复权），完成后向量化刷新 RPS。

    轻量（1~2 分钟），对已有数据幂等；立即返回 accepted。
    """
    async def _bg():
        try:
            from services.data.kline_history import get_engine, history_available
            eng = get_engine()
            eng.sync_today_bulk()
            if history_available():
                from services.strategy.rps import refresh_rps
                await refresh_rps(RPS_PERIOD_DAYS)
            print("[历史K线] 增量同步 + RPS 刷新完成")
        except Exception as e:
            print(f"[历史K线] 增量同步失败: {e}")
    asyncio.create_task(_bg())
    return {"success": True, "message": "本地历史库增量同步已在后台启动"}


@router.post("/api/kline-history/backfill", tags=["本地历史K线库"])
async def kline_history_backfill_api(start_date: str = None, limit: int = 0):
    """后台触发全市场历史日K线回填（baostock 前复权，单线程断点续传，约 12 分钟）。

    仅首次部署需要；日常用 /sync 即可。立即返回 accepted。
    """
    async def _bg():
        try:
            from services.data.kline_history import get_engine
            eng = get_engine()
            from app.config import KLINE_HISTORY_START_DATE
            symbols = eng.get_all_symbols()
            if not symbols:
                print("[历史K线] 无法获取股票列表（baostock 登录失败？），回填终止")
                return
            if limit and limit > 0:
                symbols = symbols[:limit]
            eng.backfill(symbols=symbols, start_date=start_date or KLINE_HISTORY_START_DATE)
            print("[历史K线] 全量回填完成")
        except Exception as e:
            print(f"[历史K线] 全量回填失败: {e}")
    asyncio.create_task(_bg())
    return {"success": True, "message": "全市场历史K线回填已在后台启动（约12分钟）"}

