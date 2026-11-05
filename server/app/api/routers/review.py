"""Domain router: review."""

from fastapi import APIRouter
from ..routes_state import *

router = APIRouter()

@router.post("/api/daily-review/generate", tags=["收评"])
async def api_generate_daily_review():
    try:
        print('[收评API] 开始生成今日收评...')
        review = await generate_daily_review()
        print('[收评API] 收评生成成功，正在保存...')
        
        save_daily_market_reviews([review])
        _invalidate_daily_reviews_cache()   # 2026-11-04：写后失效，别等 5 分钟 TTL
        print('[收评API] 收评保存完成！')
        
        return {"success": True, "data": review}
    except Exception as error:
        print(f'[收评API] 生成收评失败: {str(error)}')
        import traceback
        print(f'[收评API] 错误堆栈: {traceback.format_exc()}')
        return {"success": False, "error": str(error)}


@router.get("/api/daily-reviews", tags=["收评"])
async def get_daily_market_reviews(request: Request, page: int = 0):
    """收评列表（2026-11-04：SWR + ETag/304 + 预压缩 + 服务端分页）。

    Args:
        request: 用于读 `If-None-Match` / `Accept-Encoding`。
        page: 0 或不传 = 全量（兼容既有脚本）；>=1 = 第 N 页，固定 1 条/页。

    响应体在旧版 `{"success": True, "data": [...]}` 之上**只增不减**地追加
    `page/pageSize/total/totalPages/recentFocus`，老消费方读 `data` 不受影响。
    命中 If-None-Match 返回 304 空体；分页后首次开页从 105KB 降到约 5KB(gzip)。
    """
    try:
        bundle, state = await asyncio.to_thread(
            _swr.swr_get, _DAILY_REVIEWS_CACHE, 'all', _build_daily_reviews_entry,
            ttl=_DAILY_REVIEWS_TTL, stale_max=_SWR_STALE_MAX,
            label='daily-reviews', gap=60, max_keys=2)
        if page <= 0:
            entry = bundle['all']
        elif page > bundle['total']:
            # 越界页：不进缓存、现构一个极小响应，避免自由参数撑出无数条目
            entry = _reviews_entry({'success': True, 'data': [],
                                    'page': page, 'pageSize': 1,
                                    'total': bundle['total'], 'totalPages': bundle['total'],
                                    'recentFocus': bundle['recentFocus']})
        else:
            entry = bundle['pages'][page]
        etag = entry['etag']
        headers = {'ETag': etag, 'Cache-Control': 'private, no-cache',
                   'X-Data-State': state, 'Vary': 'Accept-Encoding'}
        inm = request.headers.get('if-none-match', '')
        if inm and (etag in [x.strip() for x in inm.split(',')] or inm.strip() == '*'):
            return Response(status_code=304, headers=headers)
        # 预压缩直出：GZipMiddleware 见已有 Content-Encoding 直接放行，不再重压。
        if 'gzip' in request.headers.get('accept-encoding', '').lower():
            headers['Content-Encoding'] = 'gzip'
            return Response(content=entry['gz'], media_type='application/json',
                            headers=headers)
        return Response(content=entry['raw'], media_type='application/json',
                        headers=headers)
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取收评失败: {str(error)}")

