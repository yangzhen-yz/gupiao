"""正式自测：/api/daily-reviews 的 SWR + ETag/304 + 预压缩 + 服务端分页（进程内 ASGI）。

覆盖：
  ① 分页首访 200、响应体结构、弱 ETag、Cache-Control/X-Data-State/Vary、gzip 可解码
  ② 带 If-None-Match 命中 → 304 且空体
  ③ ETag 不匹配 → 200 且完整体
  ④ 缓存命中后不再重复查库（compute 调用次数不增）
  ⑤ 写后失效 → 下一次重新构建、ETag 随内容变化
  ⑥ 内容不变时 ETag 稳定
  ⑦ 通配符 If-None-Match 也命中 304
  ⑧ 不收 gzip 的客户端拿 identity 原文
  ⑨ 服务端分页：不同页不同内容/不同 ETag、totalPages/recentFocus 正确
  ⑩ 不带 page 仍返回全量（老脚本兼容）
  ⑪ 越界页返回空数组且不额外查库
"""
import asyncio
import gzip
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'server'))
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')

import httpx

PASS = 0
FAIL = 0


def check(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  PASS  {name} {extra}')
    else:
        FAIL += 1
        print(f'  FAIL  {name} {extra}')


def _mk(payload):
    """与 routes._reviews_entry 同口径，避免自测自己造一套假实现。"""
    raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':'),
                     default=str).encode('utf-8')
    return {'raw': raw, 'gz': gzip.compress(raw, compresslevel=6),
            'etag': 'W/"' + hashlib.md5(raw).hexdigest() + '"'}


async def main():
    from app.api import routes as r
    from app.main import app

    # 用可控的假数据替换「查库 + 序列化」，验证缓存语义而非 DB 本身
    calls = {'n': 0}
    payload_src = {'v': 1}

    def fake_build():
        calls['n'] += 1
        focus = [{'code': 'sh600519', 'name': '贵州茅台'}]
        rows = [{'date': f'2026-11-{4 - i:02d}', 'v': payload_src['v'],
                 'tomorrowFocus': focus} for i in range(3)]
        common = {'total': len(rows), 'recentFocus': focus}
        return {
            'total': len(rows), 'recentFocus': focus,
            'all': _mk({'success': True, 'data': rows, 'page': 0,
                        'pageSize': len(rows), 'totalPages': 1, **common}),
            'pages': {
                i: _mk({'success': True, 'data': [row], 'page': i,
                        'pageSize': 1, 'totalPages': len(rows), **common})
                for i, row in enumerate(rows, 1)
            },
        }

    orig = r._build_daily_reviews_entry
    r._build_daily_reviews_entry = fake_build
    r._invalidate_daily_reviews_cache()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as c:
            r1 = await c.get('/api/daily-reviews?page=1')
            body1 = r1.json()
            check('① 分页首访 200', r1.status_code == 200, f'status={r1.status_code}')
            check('① 一页只返回 1 条', len(body1.get('data', [])) == 1,
                  f"len={len(body1.get('data', []))}")
            check('① 响应体结构不变',
                  body1.get('success') is True and body1['data'][0]['date'] == '2026-11-04',
                  f"body={str(body1)[:80]}")
            check('① 分页元信息齐备',
                  body1.get('page') == 1 and body1.get('total') == 3
                  and body1.get('totalPages') == 3 and body1.get('pageSize') == 1,
                  f"page={body1.get('page')} total={body1.get('total')} totalPages={body1.get('totalPages')}")
            etag = r1.headers.get('etag', '')
            check('① ETag 为弱校验器指纹',
                  etag.startswith('W/"') and etag.endswith('"') and len(etag) == 36,
                  f'ETag={etag}')
            check('① Cache-Control 允许存储但强制重验证',
                  'no-cache' in r1.headers.get('cache-control', ''),
                  f"CC={r1.headers.get('cache-control')}")
            check('① X-Data-State=cold', r1.headers.get('x-data-state') == 'cold',
                  f"state={r1.headers.get('x-data-state')}")
            check('① 预压缩直出 Content-Encoding=gzip',
                  r1.headers.get('content-encoding') == 'gzip',
                  f"CE={r1.headers.get('content-encoding')}")
            check('① Vary 含 Accept-Encoding',
                  'accept-encoding' in r1.headers.get('vary', '').lower(),
                  f"Vary={r1.headers.get('vary')}")
            check('① gzip 响应体可被客户端正常解码',
                  body1['data'][0]['v'] == 1, f"body={str(body1)[:80]}")
            check('① 响应体远小于全量',
                  len(r1.content) < 10000, f'bytes={len(r1.content)}')

            r2 = await c.get('/api/daily-reviews?page=1', headers={'If-None-Match': etag})
            check('② 命中返回 304', r2.status_code == 304, f'status={r2.status_code}')
            check('② 304 体为空', len(r2.content) == 0, f'len={len(r2.content)}')
            check('② 304 仍带 ETag', r2.headers.get('etag') == etag)

            r3 = await c.get('/api/daily-reviews?page=1',
                             headers={'If-None-Match': 'W/"deadbeef"'})
            check('③ ETag 不匹配返回 200', r3.status_code == 200, f'status={r3.status_code}')
            check('③ 200 带完整体', len(r3.content) > 20, f'len={len(r3.content)}')

            check('④ 三次请求只构建一次', calls['n'] == 1, f"calls={calls['n']}")

            r._invalidate_daily_reviews_cache()
            payload_src['v'] = 2
            r4 = await c.get('/api/daily-reviews?page=1')
            check('⑤ 失效后重新构建', calls['n'] == 2, f"calls={calls['n']}")
            check('⑤ 内容变化后 ETag 变化', r4.headers.get('etag') != etag,
                  f'{etag} -> {r4.headers.get("etag")}')

            r._invalidate_daily_reviews_cache()
            r5 = await c.get('/api/daily-reviews?page=1')
            check('⑥ 内容不变时 ETag 稳定', r5.headers.get('etag') == r4.headers.get('etag'))

            r6 = await c.get('/api/daily-reviews?page=1',
                             headers={'If-None-Match': '*'})
            check('⑦ 通配符 If-None-Match 也命中 304',
                  r6.status_code == 304, f'status={r6.status_code}')

            r7 = await c.get('/api/daily-reviews?page=1',
                             headers={'Accept-Encoding': 'identity'})
            # ⑤ 已把内容改成 v=2，比较基准要用当前缓存（r5），不能用首轮的 body1
            check('⑧ 不收 gzip 的客户端拿 identity 原文',
                  r7.status_code == 200 and 'content-encoding' not in r7.headers
                  and r7.json() == r5.json(),
                  f"CE={r7.headers.get('content-encoding')} body={str(r7.json())[:60]}")
            check('⑧ 预压缩不额外触发查库', calls['n'] == 3, f"calls={calls['n']}")

            # ---- ⑨ 服务端分页 ----
            p1 = await c.get('/api/daily-reviews?page=1')
            p2 = await c.get('/api/daily-reviews?page=2')
            p3 = await c.get('/api/daily-reviews?page=3')
            check('⑨ 不同页返回不同日期',
                  [x.json()['data'][0]['date'] for x in (p1, p2, p3)]
                  == ['2026-11-04', '2026-11-03', '2026-11-02'],
                  f"{[x.json()['data'][0]['date'] for x in (p1, p2, p3)]}")
            check('⑨ 各页 ETag 互不相同',
                  len({x.headers.get('etag') for x in (p1, p2, p3)}) == 3)
            check('⑨ 每页都带最近 3 天明日关注',
                  all(x.json().get('recentFocus') == [{'code': 'sh600519', 'name': '贵州茅台'}]
                      for x in (p1, p2, p3)))
            check('⑨ 翻页不额外查库', calls['n'] == 3, f"calls={calls['n']}")
            p2c = await c.get('/api/daily-reviews?page=2',
                              headers={'If-None-Match': p2.headers.get('etag')})
            check('⑨ 指定页的 304 也能命中', p2c.status_code == 304,
                  f'status={p2c.status_code}')

            # ---- ⑩ 全量兼容 ----
            allr = await c.get('/api/daily-reviews')
            check('⑩ 不带 page 仍返回全量 3 条',
                  allr.status_code == 200 and len(allr.json().get('data', [])) == 3,
                  f"len={len(allr.json().get('data', []))}")

            # ---- ⑪ 越界页 ----
            oob = await c.get('/api/daily-reviews?page=99999')
            check('⑪ 越界页返回空数组',
                  oob.status_code == 200 and oob.json().get('data') == []
                  and oob.json().get('total') == 3,
                  f"body={str(oob.json())[:80]}")
            check('⑪ 越界页不额外查库/不污染缓存',
                  calls['n'] == 3 and 'all' in r._DAILY_REVIEWS_CACHE,
                  f"calls={calls['n']} cache_keys={list(r._DAILY_REVIEWS_CACHE)}")
    finally:
        r._build_daily_reviews_entry = orig
        r._invalidate_daily_reviews_cache()

    print(f'\n结果: {PASS} 通过 / {FAIL} 失败')
    return 0 if FAIL == 0 else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
