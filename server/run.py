"""run - 应用入口"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn
from fastapi.staticfiles import StaticFiles
from app.main import app

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, 'static')

# 挂载 Vue3 前端构建产物
if os.path.exists(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

if __name__ == "__main__":
    import os
    uvicorn.run(
        "run:app",
        host="0.0.0.0",
        port=3003,
        reload=True,
        reload_dirs=[os.path.join(BASE_DIR, 'app'), os.path.join(BASE_DIR, 'services'), os.path.join(BASE_DIR, 'db')],
    )
