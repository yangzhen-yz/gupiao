"""重启后冷/热态验收编排（2026-11-04 全站巡检）。

等看护消费重启标志 → 等健康 → 等启动预热串行链跑完 → 连跑两轮接口基准。
冷态那轮在预热完成后采样，含义是「用户重启后首开」；热态那轮是「日常切页」。
"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLAG = ROOT / 'server' / 'data' / 'backend_restart_requested'
LOG = ROOT / 'server' / 'server_supervised.log'
PY = ROOT / '.venv' / 'Scripts' / 'python.exe'
TOKEN = os.environ.get('GP_TOKEN', '')


def log_tail(n=400):
    try:
        raw = LOG.read_bytes()[-400000:].decode('utf-8', errors='ignore').replace('\x00', '')
        return '\n'.join(raw.splitlines()[-n:])
    except Exception:
        return ''


def wait(label, fn, cap=480, step=3):
    t0 = time.time()
    while time.time() - t0 < cap:
        if fn():
            print(f'[WAIT] {label}: OK ({time.time() - t0:.0f}s)', flush=True)
            return True
        time.sleep(step)
    print(f'[WAIT] {label}: TIMEOUT after {cap}s（继续跑，结果按实测解读）', flush=True)
    return False


print('[STEP] 写重启标志', flush=True)
FLAG.touch()

wait('看护消费重启标志', lambda: not FLAG.exists(), cap=420)


def healthy():
    try:
        import requests
        s = requests.Session()
        s.trust_env = False
        r = s.get('http://127.0.0.1:3003/api/health', timeout=(2, 8))
        return r.status_code == 200
    except Exception:
        return False


wait('后端恢复健康', healthy, cap=180)
wait('预热串行链 _prewarm_heavy 结束',
     lambda: '◂ _prewarm_heavy 结束' in log_tail(400), cap=600)
# 让最后一项的 2s 让出窗口与后台刷新收尾
time.sleep(12)

env = dict(os.environ, GP_TOKEN=TOKEN)
env.pop('http_proxy', None); env.pop('https_proxy', None)
env.pop('HTTP_PROXY', None); env.pop('HTTPS_PROXY', None)
env.pop('ALL_PROXY', None); env.pop('all_proxy', None)

for rnd in ('cold', 'warm'):
    print(f'\n===== API BENCH {rnd} =====', flush=True)
    e = dict(env, GP_ROUND=rnd, GP_EXTRA='1')
    subprocess.run([str(PY), str(ROOT / 'scripts' / 'api_bench.py')],
                   cwd=str(ROOT), env=e, check=False)
    if rnd == 'cold':
        time.sleep(5)
print('\n[STEP] 基准完成，可继续跑 UI 巡检', flush=True)
