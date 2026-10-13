# -*- coding: utf-8 -*-
"""截图本服务指定 Tab

用法:
  python shot_review.py              # 默认截「每日收评」
  python shot_review.py 智能推荐      # 截指定 Tab
"""
import sys, os, time
from playwright.sync_api import sync_playwright

TAB = sys.argv[1] if len(sys.argv) > 1 else "每日收评"
SAFE = {"每日收评": "daily_review", "智能推荐": "recommend", "趋势发现": "trend",
        "股票筛选": "scan", "持仓管理": "position", "资金流向": "flow", "实时行情": "quote"}
URL = "http://127.0.0.1:3003/"
OUTDIR = r"D:\Desktop\guizhou\gupiao\screenshots"
OUT = os.path.join(OUTDIR, SAFE.get(TAB, TAB) + ".png")

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1920, "height": 1080})
    pg.goto(URL, wait_until="networkidle", timeout=60000)
    time.sleep(1.5)
    pg.get_by_role("button", name=TAB).click()
    pg.wait_for_timeout(3500)
    pg.wait_for_timeout(2500)
    pg.screenshot(path=OUT, full_page=True)
    print("saved:", OUT, "| tab:", TAB)
    b.close()
