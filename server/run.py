"""run - 应用入口"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn
from fastapi.staticfiles import StaticFiles
from app.main import app

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, 'static')


class SPAStaticFiles(StaticFiles):
    """SPA 静态资源缓存策略（2026-10-19 修手机端拿到旧壳的坑）：

    · index.html / 任意 text/html：``no-cache`` —— 每次都向源站再验证，
      保证 cloudflared 隧道与手机浏览器永远拿到最新外壳（否则会卡在已被
      vite emptyOutDir 删掉的旧入口 JS，导致整个前端/某个懒加载 tab 打不开）。
    · 带内容 hash 的 JS/CSS（assets/xxx-<hash>.js）：``immutable`` 长缓存，
      内容变则文件名变，缓存永不失效也安全。
    """

    async def get_response(self, path: str, scope):
        resp = await super().get_response(path, scope)
        ctype = resp.headers.get('content-type', '')
        if 'text/html' in ctype or path == '' or path.endswith('.html'):
            resp.headers['Cache-Control'] = 'no-cache'
        else:
            resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        return resp


# 挂载 Vue3 前端构建产物
if os.path.exists(STATIC_DIR):
    app.mount("/", SPAStaticFiles(directory=STATIC_DIR, html=True), name="static")

if __name__ == "__main__":
    import os
    uvicorn.run(
        "run:app",
        host="0.0.0.0",
        port=3003,
        reload=True,
        reload_dirs=[os.path.join(BASE_DIR, 'app'), os.path.join(BASE_DIR, 'services'), os.path.join(BASE_DIR, 'db')],
    )
