"""方案 A 线上验收：重启加载新代码后，量收评 SWR/304 与扫描评分 N+1 的实际收益。

用法（在项目根、清代理）：
  .venv/Scripts/python.exe -u scripts/verify_plan_a.py
前置：先 touch server/data/backend_restart_requested 让看护重启后端。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
BASE = 'http://127.0.0.1:3003'
FLAG = ROOT / 'server' / 'data' / 'backend_restart_requested'
LOG = ROOT / 'server' / 'server_supervised.log'
OUT = ROOT / 'server' / 'analysis_out' / 'verify_plan_a.json'

S = requests.Session()
S.trust_env = False


def log_tail(n=250):
    try:
        return LOG.read_text(encoding='utf-8', errors='ignore').replace('\x00', '')[-n * 200:]
    except Exception:
        return ''


def get(path, headers=None):
    t = time.perf_counter()
    r = S.get(BASE + path, headers=headers or {}, timeout=60)
    return r, (time.perf_counter() - t) * 1000


def wait(label, pred, cap=420, interval=5):
    t0 = time.time()
    while time.time() - t0 < cap:
        try:
            if pred():
                print(f'[WAIT] {label} OK ({time.time() - t0:.0f}s)', flush=True)
                return True
        except Exception:
            pass
        time.sleep(interval)
    print(f'[WAIT] {label} 超时 {cap}s', flush=True)
    return False


def main():
    report = {}
    print('=== ① 等看护消费重启标志 ===', flush=True)
    wait('重启标志被消费', lambda: not FLAG.exists(), cap=420)

    print('=== ② 等新代码生效（收评响应带 ETag） ===', flush=True)
    def new_code():
        r, _ = get('/api/daily-reviews')
        return r.status_code == 200 and 'etag' in r.headers
    wait('新后端已起且带 ETag', new_code, cap=420, interval=4)

    wait('收评预热完成',
         lambda: '收评缓存完成' in log_tail(400), cap=420, interval=5)

    print('\n=== ③ 收评接口实测 ===', flush=True)
    r1, ms1 = get('/api/daily-reviews')
    etag = r1.headers.get('etag', '')
    dr = {
        'first': {'status': r1.status_code, 'ms': round(ms1, 1),
                  'bytes': len(r1.content),
                  'etag': etag,
                  'cacheControl': r1.headers.get('cache-control'),
                  'state': r1.headers.get('x-data-state'),
                  'processMs': r1.headers.get('x-process-time')},
        'rows': len(r1.json().get('data', [])),
    }
    print('  首次  %s ms=%sms size=%sB state=%s XPT=%s' % (
        dr['first']['status'], dr['first']['ms'], dr['first']['bytes'],
        dr['first']['state'], dr['first']['processMs']), flush=True)

    r2, ms2 = get('/api/daily-reviews', {'If-None-Match': etag})
    dr['conditional'] = {'status': r2.status_code, 'ms': round(ms2, 1),
                         'bytes': len(r2.content),
                         'processMs': r2.headers.get('x-process-time')}
    print('  条件  %s ms=%sms size=%sB XPT=%s' % (
        r2.status_code, dr['conditional']['ms'], dr['conditional']['bytes'],
        dr['conditional']['processMs']), flush=True)

    r3, ms3 = get('/api/daily-reviews', {'If-None-Match': '"deadbeef"'})
    dr['mismatch'] = {'status': r3.status_code, 'ms': round(ms3, 1),
                      'bytes': len(r3.content)}
    print('  不匹配 %s ms=%sms size=%sB' % (
        r3.status_code, dr['mismatch']['ms'], dr['mismatch']['bytes']), flush=True)
    report['dailyReviews'] = dr

    print('\n=== ④ 新组合冷扫描（看评分阶段是否从 0.78s 降下来） ===', flush=True)
    scans = []
    combos = [
        'sh600519,sz000001,sh601318,sh600036,sz000858,sh601888',
        'sh600030,sz000333,sh601166,sh600887,sz002594,sh601012',
    ]
    for combo in combos:
        path = '/api/hot-stocks-scan?codes=' + combo
        r, ms = get(path)
        scans.append({'combo': combo[:30], 'status': r.status_code,
                      'ms': round(ms, 1), 'bytes': len(r.content)})
        print('  %-32s %s %sms' % (combo[:30], r.status_code, round(ms, 1)), flush=True)
    report['coldScans'] = scans
    tail = log_tail(60).splitlines()
    print('  --- 最近扫描阶段日志 ---', flush=True)
    for line in [x for x in tail if '批量扫描' in x][-12:]:
        print('   ', line.strip()[:150], flush=True)
    report['scanLog'] = [x.strip()[:150] for x in tail if '批量扫描' in x][-12:]

    print('\n=== ⑤ 热态基准（53 端点，确认无回归） ===', flush=True)
    env = {**os.environ, 'GP_ROUND': 'warm', 'GP_TOKEN': os.environ.get('GP_TOKEN', '')}
    for k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'all_proxy'):
        env.pop(k, None)
    try:
        p = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'api_bench.py')],
                           cwd=str(ROOT), env=env, capture_output=True,
                           text=True, timeout=300)
        print(p.stdout[-2500:], flush=True)
        report['benchTail'] = p.stdout[-2500:]
    except Exception as e:
        print('  基准脚本失败（不阻断）:', e, flush=True)

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('\n报告:', OUT, flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
