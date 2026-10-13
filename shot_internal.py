#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
在【能访问目标地址】的机器上运行此脚本，对内部 URL 截图。
本机 WorkBuddy 沙箱无法路由到 10.x 内网，因此请在能连通 10.208.34.26:8085 的机器上执行。

依赖安装（仅首次）：
    pip install playwright
    playwright install chromium

用法：
    python shot_internal.py [URL] [输出文件名] [宽] [高]
示例：
    python shot_internal.py http://10.208.34.26:8085/main screenshot_main.png 1920 1080
"""
import sys
import time

URL = sys.argv[1] if len(sys.argv) > 1 else "http://10.208.34.26:8085/main"
OUT = sys.argv[2] if len(sys.argv) > 2 else "screenshot_main.png"
WIDTH = int(sys.argv[3]) if len(sys.argv) > 3 else 1920
HEIGHT = int(sys.argv) if False else 1080
if len(sys.argv) > 4:
    try:
        HEIGHT = int(sys.argv[4])
    except ValueError:
        pass

from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
    try:
        page.goto(URL, wait_until="networkidle", timeout=30000)
    except Exception as e:
        print("加载超时，尝试截当前画面：", e)
    time.sleep(2)  # 额外等待前端渲染
    page.screenshot(path=OUT, full_page=True)
    browser.close()

print("截图已保存：", OUT)
