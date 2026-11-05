"""Domain router: data."""

from fastapi import APIRouter
from ..routes_state import *

router = APIRouter()

@router.get("/api/fund-flow", tags=["资金流向"])
async def get_fund_flow_api(
    stock_top_n: int = FUND_FLOW_STOCK_TOP_N,
    sector_top_n: int = FUND_FLOW_SECTOR_TOP_N,
    change_threshold: float = FUND_FLOW_HIDDEN_CHANGE_THRESHOLD,
    hot_top_n: int = FUND_FLOW_HIDDEN_HOT_TOP_N,
    hidden_top_n: int = FUND_FLOW_HIDDEN_TOP_N,
):
    """资金流向汇总：个股主力净流入/净流出排名 + 板块净流入/净流出排名 + 非热点上涨股。

    三部分数据各自独立获取、独立容错；结果带 60s 内存缓存，适配前端 60s 轮询。
    查询参数可覆盖默认阈值（前端“非热点上涨股”区块支持自定义涨幅阈值 / 热点排除排名）。
    """
    try:
        data = await get_fund_flow_summary(
            stock_top_n=stock_top_n,
            sector_top_n=sector_top_n,
            change_threshold=change_threshold,
            hot_top_n=hot_top_n,
            hidden_top_n=hidden_top_n,
        )
        return {"success": True, "data": data}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取资金流向失败: {str(error)}")


@router.get("/api/rps", tags=["RPS相对强度"])
async def get_rps_api(period_days: int = RPS_PERIOD_DAYS, top_n: int = RPS_TOP_N):
    """RPS 相对强度强势股榜（欧奈尔核心因子，P0，2026-07-16）

    返回全市场区间涨幅百分位排名前 top_n 的强势股（RPS + 区间涨跌幅）。
    数据来自东方财富 clist 区间涨幅，带 1 小时内存缓存，适配前端定时刷新。
    """
    try:
        data = await get_rps_leaderboard(period_days, top_n)
        return {"success": True, "data": data}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取RPS强势股失败: {str(error)}")


@router.post("/api/rps/refresh", tags=["RPS相对强度"])
async def refresh_rps_api(period_days: int = RPS_PERIOD_DAYS, limit_codes: int = None):
    """后台触发全市场 RPS 预计算（盘前/手动）。

    耗时较长（拉全市场 K 线），立即返回 accepted，结果写入 rps_cache 表；
    盘中扫描/分析读取缓存。可选 limit_codes 用于小范围验证。
    """
    from services.strategy.rps import refresh_rps
    async def _bg():
        try:
            res = await refresh_rps(period_days, limit_codes)
            print(f'[RPS] 预计算完成: {res}')
        except Exception as e:
            print(f'[RPS] 预计算失败: {e}')
    asyncio.create_task(_bg())
    return {"success": True, "message": "RPS 预计算已在后台启动", "periodDays": period_days}


@router.post("/api/fundamental/refresh", tags=["基本面质量"])
async def refresh_fundamental_api():
    """后台触发全市场基本面质量预计算（盘后/手动，P1a）。

    拉取最新一期 ROE/净利润同比，写入 stock_fundamental 表；立即返回 accepted。
    """
    async def _bg():
        try:
            await update_fundamental_task()
            print('[基本面] 预计算完成')
        except Exception as e:
            print(f'[基本面] 预计算失败: {e}')
    asyncio.create_task(_bg())
    return {"success": True, "message": "基本面质量预计算已在后台启动"}


@router.get("/api/dragon-tiger", tags=["聪明钱"])
async def get_dragon_tiger_api(days: int = SMART_MONEY_DRAGON_TIGER_DAYS, top_n: int = 30, force: bool = False):
    """龙虎榜聪明钱聚合榜（P1b）。返回近 days 日上榜个股净买入排名。force=true 时忽略缓存。"""
    try:
        data = get_dragon_tiger(days, top_n, force=force)
        return {"success": True, "data": data}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取龙虎榜失败: {str(error)}")


@router.get("/api/northbound", tags=["聪明钱"])
async def get_northbound_api(force: bool = False):
    """北向资金市场级净买额（P1b）。返回沪深股通当日/月度净买及情绪判断。force=true 时忽略缓存。"""
    try:
        data = get_northbound_market(force=force)
        return {"success": True, "data": data}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取北向资金失败: {str(error)}")


@router.post("/api/valuation/refresh", tags=["估值分位"])
async def refresh_valuation_api():
    """后台触发全市场估值分位预计算（盘后/手动，P2c）。

    拉取全市场 PE/PB 算百分位，写入 stock_valuation 表；立即返回 accepted。
    """
    async def _bg():
        try:
            res = update_valuation_task()
            print(f'[估值] 预计算完成: {res}')
        except Exception as e:
            print(f'[估值] 预计算失败: {e}')
    asyncio.create_task(_bg())
    return {"success": True, "message": "估值分位预计算已在后台启动"}


@router.get("/api/valuation", tags=["估值分位"])
async def get_valuation_api(symbols: str = ''):
    """批量查询估值分位（P2c）。symbols 逗号分隔，留空返回空。"""
    try:
        syms = [s.lower() for s in symbols.split(',') if re.match(r'^(sh|sz)\d{6}$', s.lower())]
        data = get_valuation_batch(syms)
        return {"success": True, "data": data}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取估值分位失败: {str(error)}")


@router.api_route("/eastmoney/{path:path}", methods=["GET", "POST"])
async def proxy_eastmoney(request: Request, path: str):
    """将 /eastmoney/* 请求转发到东方财富 API（requests Session + 重试，解决 RemoteDisconnected）

    2026-10-19：接入共享熔断 eastmoney_guard——前端唯一能任意路径直连东财的通道，
    冷却中直接 503，不再 3 次重试硬打（东财 clist IP 级风控实锤，频率是主因）。
    前端对非 2xx 均 catch(()=>null) 静默降级，行为兼容。
    """
    import asyncio, time as _time
    try:
        from services.data import eastmoney_guard
        if eastmoney_guard.skip('clist'):
            return Response(content=b'{"error":"eastmoney clist cooling"}',
                            status_code=503, media_type="application/json")
    except Exception:
        pass
    target_url = f"https://push2delay.eastmoney.com/{path}"
    query_params = dict(request.query_params)
    session = _get_eastmoney_session()

    def _do_request():
        last_err = None
        for attempt in range(3):
            try:
                if request.method == "GET":
                    return session.get(target_url, params=query_params, timeout=15)
                else:
                    body_bytes = request._body
                    return session.post(target_url, params=query_params, data=body_bytes, timeout=15)
            except Exception as e:
                last_err = e
                if attempt < 2:
                    _time.sleep(0.5 * (attempt + 1))  # 0.5s, 1.0s 退避
        raise last_err

    try:
        resp = await asyncio.to_thread(_do_request)
        try:
            from services.data import eastmoney_guard
            eastmoney_guard.ok('clist')
        except Exception:
            pass
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers={"Content-Type": resp.headers.get("content-type", "application/json")},
        )
    except Exception as e:
        print(f'[东方财富代理] 请求失败: {e}')
        try:
            from services.data import eastmoney_guard
            eastmoney_guard.fail('clist')
        except Exception:
            pass
        return Response(content=b"{}", status_code=502, media_type="application/json")

