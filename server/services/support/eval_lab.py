"""eval_lab.py - 因子体检的轻量读写层（2026-10-23）

为什么不在 web 进程里 import factor_lab
--------------------------------------
`analysis_out/factor_lab.py` 依赖 numpy/pandas，而服务是由 `.venv` 的 uvicorn
以 reload 方式拉起、子 worker 的解释器与依赖路径不一定带 numpy（2026-10-23 实测
`GET /api/eval-lab/latest` 500：`No module named 'numpy'`，同一条命令用系统
Python312 独立跑却正常）。读结果只需要 sqlite3；跑体检是一次性的离线任务，
放在子进程里跑，解释器可配置、可探测——既不让 web 进程背 pandas，也避免
"周六 08:30 任务静默失败、直到有人点开才发现"。

约定
----
- `latest_run()`：只读 `eval_lab_runs`，不 import factor_lab，永不因缺依赖 500。
- `run_analysis()`：优先用能 `import numpy` 的解释器跑 `factor_lab.py --store`；
  `EVAL_LAB_PYTHON` 环境变量可显式指定解释器。找不到可用解释器时**显式抛错**，
  由调用方写日志/返回 500，不做静默降级。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Dict, List, Optional

from app.config import SQLITE_DB_PATH

_DDL = '''CREATE TABLE IF NOT EXISTS eval_lab_runs (
    run_date      TEXT PRIMARY KEY,
    generated_at  TEXT,
    universe      INTEGER,
    rebalances    INTEGER,
    window_from   TEXT,
    window_to     TEXT,
    payload       TEXT
)'''


def latest_run() -> Dict:
    """最近一次体检结果（接口用）。只读，不加载分析脚本。"""
    import sqlite3
    conn = None
    try:
        conn = sqlite3.connect(f'file:{SQLITE_DB_PATH}?mode=ro', uri=True, timeout=15)
        row = conn.execute(
            'SELECT run_date, generated_at, payload FROM eval_lab_runs '
            'ORDER BY run_date DESC LIMIT 1').fetchone()
        if not row:
            return {'available': False}
        out = json.loads(row[2])
        out['runDate'] = row[0]
        out['generatedAt'] = row[1]
        out['available'] = True
        return out
    except sqlite3.OperationalError:
        # 表还没建过 / 库不存在：第一次体检前就是"没有结果"，不是故障
        return {'available': False}
    finally:
        if conn is not None:
            conn.close()


def _script_path() -> str:
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, 'analysis_out', 'factor_lab.py')


def _can_import_numpy(exe: str) -> bool:
    try:
        r = subprocess.run([exe, '-c', 'import numpy'],
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def _candidates() -> List[str]:
    """按可信度排的解释器候选。去重、保序。"""
    raw = [os.environ.get('EVAL_LAB_PYTHON', '').strip(),
           sys.executable,
           getattr(sys, '_base_executable', '') or '',
           shutil.which('python') or '']
    out: List[str] = []
    for x in raw:
        if x and x not in out:
            out.append(x)
    return out


def pick_interpreter() -> Optional[str]:
    """第一个能 import numpy 的解释器；一个都没有则返回 None。"""
    for exe in _candidates():
        if os.path.exists(exe) and _can_import_numpy(exe):
            return exe
    return None


def run_analysis(timeout: int = 900) -> Dict:
    """子进程跑一次体检并落库。返回 {ok, interpreter, runDate, universe, ...}。

    失败显式抛 RuntimeError（找不到解释器）或带上子进程输出尾部（跑挂了），
    调用方据此打日志；绝不返回"成功"来掩盖没跑成。
    """
    script = _script_path()
    if not os.path.exists(script):
        raise RuntimeError(f'因子体检脚本不存在: {script}')
    exe = pick_interpreter()
    if not exe:
        raise RuntimeError('找不到能 import numpy 的 Python 解释器（可设 EVAL_LAB_PYTHON）')
    env = dict(os.environ)
    env.setdefault('PYTHONIOENCODING', 'utf-8')
    r = subprocess.run([exe, script, '--store'], cwd=os.path.dirname(script),
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=timeout, env=env)
    tail = ((r.stdout or '') + '\n' + (r.stderr or '')).strip()[-1200:]
    if r.returncode != 0:
        raise RuntimeError(f'因子体检子进程退出码 {r.returncode}：{tail}')
    latest = latest_run()
    return {'ok': True, 'interpreter': exe, 'output': tail[-500:], **latest}
