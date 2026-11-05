"""全站接口基准计时（只读）。

从 ui_sweep_report.json 提取真实浏览器请求过的 API，串行冷/热两轮计时，
同时读 X-Process-Time 响应头区分「服务端真在处理」与「排队等线程池」。

用法：
  GP_TOKEN=<token> .venv/Scripts/python.exe scripts/api_bench.py
  GP_TOKEN=<token> GP_ROUND=cold .venv/Scripts/python.exe scripts/api_bench.py
可选 GP_EXTRA=1 追加一组巡检未覆盖、但用户会点到的只读端点。
"""
import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
BASE = os.environ.get('GP_BASE', 'http://127.0.0.1:3003')
TOKEN = os.environ.get('GP_TOKEN', '')
ROUND = os.environ.get('GP_ROUND', 'warm')
REPORT = ROOT / 'server' / 'analysis_out' / 'ui_sweep_report.json'
OUT = ROOT / 'server' / 'analysis_out' / f'api_bench_{ROUND}.json'

HEADERS = {'Authorization': f'Bearer {TOKEN}'} if TOKEN else {}
# 沙箱里必须清代理，否则 127.0.0.1 被透明代理拦成假 502
SESSION = requests.Session()
SESSION.trust_env = False

urls = []
if REPORT.exists():
    sweep = json.loads(REPORT.read_text(encoding='utf-8'))
    seen = set()
    for rec in sweep:
        for q in rec.get('reqs', []):
            u = q.get('full', '')
            if '/api/' in u and u not in seen:
                seen.add(u)
                urls.append(u)

if os.environ.get('GP_EXTRA') == '1':
    urls += [
        BASE + '/api/health',
        BASE + '/api/market-alert',
        BASE + '/api/market-predict',
        BASE + '/api/theme-index',
        BASE + '/api/cls-ladder',
        BASE + '/api/action-desk',
        BASE + '/api/rps',
        BASE + '/api/dragon-tiger',
        BASE + '/api/northbound',
        BASE + '/api/valuation',
        BASE + '/api/kline-history/stats',
        BASE + '/api/kline/sh600183',
        BASE + '/api/stock-diagnose/sh600183',
        BASE + '/api/stock/sh600183/analysis',
    ]

results = []
for u in urls:
    try:
        t0 = time.perf_counter()
        r = SESSION.get(u, headers=HEADERS, timeout=(3, 30))
        dt = (time.perf_counter() - t0) * 1000
        results.append({
            'url': u.replace(BASE, ''),
            'ms': round(dt),
            'serverMs': r.headers.get('X-Process-Time', ''),
            'status': r.status_code,
            'size': len(r.content),
        })
    except Exception as e:
        results.append({'url': u.replace(BASE, ''), 'ms': -1, 'status': 0,
                        'error': f'{type(e).__name__}: {e}'[:120]})

results.sort(key=lambda x: -x['ms'])
OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
print(f'=== {ROUND} round: {len(results)} endpoints ===')
for r in results:
    flag = 'SLOW ' if r['ms'] > 1000 else ('ERR  ' if r['ms'] < 0 else '     ')
    print(f"{flag}{r['ms']:>7}ms  xpt={str(r.get('serverMs','')):>6}  {r['status']:>3}  {r['url'][:110]}")
print(f'\n-> {OUT}')
