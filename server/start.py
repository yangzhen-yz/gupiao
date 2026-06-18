"""一键启动脚本 - Windows / macOS / Linux 通用
用法: python start.py
"""
import os, sys, subprocess, shutil, platform

ROOT = os.path.dirname(os.path.abspath(__file__))
CLIENT = os.path.join(ROOT, '..', 'client-vue')

def run(cmd, cwd=None, shell=False):
    print(f'  -> {" ".join(cmd) if isinstance(cmd, list) else cmd}')
    result = subprocess.run(cmd, cwd=cwd or ROOT, shell=shell)
    if result.returncode != 0:
        print(f'\n[FAIL] 命令执行失败，退出码: {result.returncode}')
        sys.exit(1)

def find_pnpm():
    """查找 pnpm，优先使用项目自带的 node_modules/.bin"""
    local = os.path.join(CLIENT, 'node_modules', '.bin', 'pnpm.cmd' if platform.system() == 'Windows' else 'pnpm')
    if os.path.exists(local):
        return local
    return 'pnpm'

def find_python():
    for name in ['python3', 'python']:
        if shutil.which(name):
            return name
    print('[FAIL] 未找到 Python，请先安装 Python 3.9+')
    sys.exit(1)

def find_pip():
    python = find_python()
    # 尝试 pip3 / pip
    for name in ['pip3', 'pip']:
        if shutil.which(name):
            return name, python
    # fallback: python -m pip
    return f'{python} -m pip', python

# ---- 1. 构建前端 ----
print('[1/4] Building frontend...')
if not os.path.exists(os.path.join(CLIENT, 'node_modules')):
    print('  Installing frontend dependencies...')
    pnpm = find_pnpm()
    run([pnpm, 'install'], cwd=CLIENT, shell=(platform.system() == 'Windows'))

pnpm = find_pnpm()
run([pnpm, 'run', 'build'], cwd=CLIENT, shell=(platform.system() == 'Windows'))

# ---- 2. 检查 Python ----
print('\n[2/4] Checking Python...')
python = find_python()
run([python, '--version'])

# ---- 3. 安装 Python 依赖 ----
print('\n[3/4] Installing Python dependencies...')
pip, _ = find_pip()
if isinstance(pip, str) and ' ' in pip:
    run(f'{pip} install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple', shell=True)
else:
    try:
        run([pip, 'install', '-r', 'requirements.txt', '-i', 'https://pypi.tuna.tsinghua.edu.cn/simple'])
    except SystemExit:
        run([pip, 'install', '-r', 'requirements.txt'])

# ---- 4. 启动服务 ----
print('\n[4/4] Starting FastAPI...')
print('  http://localhost:3003')
print('  API docs: http://localhost:3003/docs')
print()

run([python, 'run.py'])
