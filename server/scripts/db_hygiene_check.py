"""db_hygiene_check.py - 自测换库的静态检查器（2026-11-07，修⑦；机制层，不靠自觉）

（原名 `selftest_db_hygiene.py`，11-07 改名：它是**检查器**不是自测，挂在
`scripts/*selftest*.py` 的 glob 下会被当成一份自测跑，也会被 pre-commit 的判据误读。）

为什么要有这个文件
------------------
`db/database.py` 在 11-05 拆成 `db/dal_state.py` + 各 DAL 之后，出现一类**静默失效**的写法：

    import db.database as dbmod
    dbmod.SQLITE_DB_PATH = 临时库          # ← 看着像换库，其实没用

`get_db_conn()` 的定义在 `db/dal_state.py`，读的是**那份模块里的全局**；`db.database` 里的同名
变量只是星号导入的副本，改它不改行为。后果不是报错，而是**自测照样跑绿、每一行 INSERT 却写进
生产库**（11-06 实拍过一次；更早 10-19 有人把 `2099-` 测试日期写进真实 gupiao.db，所以
`westock_crosscheck_selftest`/`fundflow_selftest` 一直留着"退出前连真库 DELETE 擦干净"的补丁——
那等于拿生产库当擦布，11-07 已把这两处改成只读核对 + 漏了就报 FAIL）。

三种合法隔离（都认）
--------------------
1. `dbmod.use_db_path(路径)` —— 同时改 `database` 与 `dal_state` 两份，推荐；
2. 把 `get_db_conn` 换成桩 —— **只算打在"真正执行写入的那个模块"上**（如
   `from db.dal import recommend as dr; dr.get_db_conn = 桩`）；
3. 所有 `sqlite3.connect(...)` 的目标都是临时路径（`tempfile`/`mkstemp`/`gettempdir`/`:memory:`）。

四条规则
--------
- **R1 会写库却没有上面任何一种隔离** —— 一定在写生产库。
- **R2 换库方式写错**：给 `config`/`database`/`dal_state` 这类门面或副本赋 `SQLITE_DB_PATH=`。
  赋给**被测模块自己**的那个全局是有效的（如 `eval_lab.SQLITE_DB_PATH`：
  `eval_lab.py:28` 是 `from app.config import SQLITE_DB_PATH`，`:46` 调用时才读），不算违规。
- **R3 直连生产库文件**（`gupiao.db`／`history_kline.db`／`SQLITE_DB_PATH`）且不是 `mode=ro`。
- **R4（11-07 新加）把 `get_db_conn` 的桩打在门面/共享层上**（`db.`、`dbmod.`、`database.`、
  `db.dal_state.` 这些别名）——和 R2 是同一件事的另一半：11-05 `database.py` 拆分后，
  各 DAL 模块用的是自己那份星号副本，赋在门面上的桩**对写入路径完全无效**。
  实拍漏口：`scripts/daily_recs_cache_selftest.py` 从 11-05 起每次跑都往真库
  `daily_recommendations`/`recommendation_batches` 写假批次，而检查器一直把那句赋值
  当成"已隔离"（老版第二条合法隔离写得太宽）。现在门面桩**既不算隔离、又单独报 R4**；
  同一文件里若已有 `use_db_path`，桩可以只是故障注入，不报。

判据为什么长这样（两条都是踩出来的）
------------------------------------
- **只看 AST 的 callee 名字判"写没写库"**：正则扫整行会被长名单字符串咬 ——
  `database_split_selftest` 的 EXPECTED 列表里全是 `save_*`/`delete_*` 字样，第一版就这么误报的。
- **路径只看 `connect` 的参数、SQL 只看 `execute` 的参数**：反过来，把整段调用原文拿来匹配，
  又会被"坏样本源码字符串"咬 —— 本仓的长期自测 `selftest_db_hygiene_selftest.py` 就是靠往里塞
  坏样本运行的，第二版在它的第 162/170 行报了两个假 R3。改成分工匹配后两个方向都不误报，
  正反用例都钉在那份自测里。

分级
----
`server/scripts/` 是入库的长期自测 → 违规**阻断**（pre-commit 的 B7 用它）；
`server/analysis_out/` 不入库、有些本就故意拿真库做实验 → 只**提示**。

用法
----
    python server/scripts/db_hygiene_check.py --staged          # pre-commit
    python server/scripts/db_hygiene_check.py --all             # 全仓体检
    python server/scripts/db_hygiene_check.py a.py --paths b.py # 指定文件（两种写法都行）
"""
import argparse
import ast
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional

SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(SERVER)

SELFTEST_NAME = re.compile(r'(?:^|/)(?:selftest_[\w.]+|[\w.]*_selftest)\.py$')
FACADE = r'(?:cfg|config|CFG|app\.config|database|dbmod|db_mod|db|D|dal_state)'
FACADE_ASSIGN = re.compile(r'\b' + FACADE + r'\.SQLITE_DB_PATH\s*=(?!=)')
DAL_WRITE = re.compile(r'^(?:save|delete|add|upsert|record|merge)_[a-z0-9_]+$')
PROD_DB = re.compile(r'gupiao\.db|history_kline\.db|history_minute\.db|SQLITE_DB_PATH', re.I)
READ_ONLY = re.compile(r'mode=ro|mode%3Dro')
WRITE_TEXT = re.compile(r'INSERT\s|REPLACE\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM', re.I)
LEAF_CALLEE = lambda name: (name or '').split('.')[-1]     # noqa: E731
# `X.get_db_conn = 桩` 里的 X 落在门面/共享层 ⇒ 桩对写入路径无效（见文件头 R4）
FACADE_STUB_ROOT = re.compile(r'^(?:' + FACADE + r')(?:\.(?:database|dal_state))?$')


def _callee(node: ast.Call) -> str:
    """点号链的最左名字 + 最右属性，够判 `sqlite3.connect` / `dbmod.use_db_path` 这类。"""
    parts, cur = [], node.func
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    parts.reverse()
    return '.'.join(parts)


def _seg(src: str, node) -> str:
    try:
        return ast.get_source_segment(src, node) or ''
    except Exception:
        return ''


def _calls(src: str) -> List[Dict]:
    out = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            args = ' '.join(_seg(src, a) for a in node.args)
            args += ' ' + ' '.join(_seg(src, k.value) for k in node.keywords)
            out.append({'line': node.lineno, 'callee': _callee(node), 'args': args})
    return out


def _assign_targets(src: str) -> List[Dict]:
    out = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            for t in list(getattr(node, 'targets', []) or []) + (
                    [node.target] if isinstance(node, ast.AnnAssign) and node.target else []):
                seg = _seg(src, t).replace(' ', '')
                if seg:
                    out.append({'line': node.lineno, 'target': seg})
    return out


def _first_write_line(calls: List[Dict]) -> Optional[int]:
    """第一处"确实会写库"的调用行号。只读（SELECT／PRAGMA／load_*）不算——
    检查器误报一次，下一次就有人把它关掉，那比漏报更糟。"""
    for c in sorted(calls, key=lambda x: x['line']):
        leaf = LEAF_CALLEE(c['callee'])
        if DAL_WRITE.match(leaf) or leaf == 'init_db':
            return c['line']
        if re.fullmatch(r'(?:execute|executemany|executescript)', leaf) and WRITE_TEXT.search(c['args']):
            return c['line']
    return None


def scan_source(rel: str, src: str) -> List[Dict]:
    """返回 [{rule, line, detail}]。纯函数——长期自测直接喂假源码验证正反两种用例。"""
    rel = rel.replace(os.sep, '/')
    if not SELFTEST_NAME.search(rel):
        return []
    calls = _calls(src)
    assigns = _assign_targets(src)

    swapped = any(LEAF_CALLEE(c['callee']) == 'use_db_path' for c in calls)
    # 桩分两种：打在**被测模块自己**的名字上有效（真隔离），打在门面/共享层别名上无效。
    # 老版本把后者也算作"已隔离"，于是 `daily_recs_cache_selftest` 那种一路写进真库的
    # 脚本连续两周检查器全绿（11-07 体检实拍，见本文件头 R4）。
    stub_targets = [t for t in assigns
                    if t['target'].endswith('.get_db_conn') or t['target'] == 'get_db_conn']
    stub_facade = [t for t in stub_targets
                   if '.' in t['target']
                   and FACADE_STUB_ROOT.match(t['target'].rsplit('.', 1)[0])]
    stubbed = bool([t for t in stub_targets if t not in stub_facade])
    prod_conns = [c for c in calls
                  if LEAF_CALLEE(c['callee']) == 'connect'
                  and PROD_DB.search(c['args']) and not READ_ONLY.search(c['args'])]
    tempish = any(LEAF_CALLEE(c['callee']) in ('mkstemp', 'mkdtemp', 'gettempdir') for c in calls)
    isolated = swapped or stubbed or (tempish and not prod_conns)

    hits: List[Dict] = []
    wline = _first_write_line(calls)
    if wline and not isolated:
        hits.append({'rule': 'R1', 'line': wline,
                     'detail': '会写库但没有任何隔离（没 use_db_path、没把 get_db_conn 换成桩、'
                               '也不是全临时路径）→ 写的就是生产库'})
    if stub_facade and not swapped:
        hits.append({'rule': 'R4', 'line': stub_facade[0]['line'],
                     'detail': f'{stub_facade[0]["target"]} = 桩 打在门面/共享层上，'
                               '11-05 拆分后各 DAL 用的是自己那份星号副本 → 这行对写入路径无效，'
                               '别拿它当隔离；换库请调 dbmod.use_db_path(路径)，'
                               '要注桩就打在真正执行写入的那个模块上'})
    if not swapped:
        m = FACADE_ASSIGN.search(src)
        if m:
            hits.append({'rule': 'R2', 'line': src[:m.start()].count('\n') + 1,
                         'detail': f'{m.group(0).strip()} 改的是门面副本（11-05 拆分后静默无效）；'
                                   '换库请用 dbmod.use_db_path(路径)。'
                                   '（赋"被测模块自己"的那个全局是有效的，不算违规）'})
    for c in prod_conns:
        hits.append({'rule': 'R3', 'line': c['line'],
                     'detail': '自测直连生产库文件（`mode=ro` 只读除外）——"事后 DELETE 清理"'
                               '本身就说明前面写过真库'})
    seen, ded = set(), []
    for h in hits:
        if (h['rule'], h['line']) in seen:
            continue
        seen.add((h['rule'], h['line']))
        ded.append(h)
    return ded


def _staged_py() -> List[str]:
    try:
        r = subprocess.run(['git', '-C', ROOT, 'diff', '--cached', '--name-only', '-z',
                            '--diff-filter=ACM'], capture_output=True)
    except OSError:
        return []
    names = [n.decode('utf-8', 'replace') for n in r.stdout.split(b'\x00') if n]
    return [n for n in names if n.endswith('.py') and '/scripts/' in n.replace(os.sep, '/')]


def iter_targets(mode: str, paths: Optional[List[str]] = None) -> List[str]:
    if mode == 'paths':
        return [p for p in (paths or []) if os.path.isfile(p)]
    if mode == 'staged':
        return [os.path.join(ROOT, p) for p in _staged_py()
                if os.path.isfile(os.path.join(ROOT, p))]
    files = []
    for sub in ('scripts', 'analysis_out'):
        base = os.path.join(SERVER, sub)
        if not os.path.isdir(base):
            continue
        for fn in sorted(os.listdir(base)):
            if fn.endswith('.py'):
                files.append(os.path.join(base, fn))
    return files


def main() -> int:
    ap = argparse.ArgumentParser(description='自测换库检查器（修⑦）')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--staged', action='store_true', help='只查暂存的自测（pre-commit 用）')
    g.add_argument('--all', action='store_true', help='全仓体检（scripts + analysis_out）')
    ap.add_argument('--paths', nargs='*', dest='paths_flag', default=[], help='指定文件（别名）')
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('paths_pos', nargs='*', default=[], help='要检查的文件')
    a = ap.parse_args()

    wanted = list(a.paths_pos) + list(a.paths_flag)
    targets = iter_targets('paths', wanted) if wanted else iter_targets(
        'staged' if a.staged else 'all')

    blocking, warnings, scanned = [], [], 0
    for fp in targets:
        try:
            rel = os.path.relpath(fp, ROOT).replace(os.sep, '/')
        except ValueError:
            # 跨盘符（临时库在 C:、仓库在 D:）时 relpath 会抛 ValueError，
            # 11-07 长期自测第 [7] 组就是这么把检查器弄崩的：崩了不等于没问题，
            # 所以退回绝对路径继续判。
            rel = fp.replace(os.sep, '/')
        try:
            src = open(fp, encoding='utf-8', errors='replace').read()
        except OSError:
            continue
        scanned += 1
        for h in scan_source(rel, src):
            row = f"[{h['rule']}] {rel}:{h['line']}  {h['detail']}"
            (warnings if 'analysis_out' in rel else blocking).append(row)
    for row in blocking:
        print('  ' + row)
    for row in warnings:
        print('  （不入库，仅提示）' + row)
    if not a.quiet:
        print(f'自测换库检查：扫 {scanned} 个文件，阻断级 {len(blocking)} 处、'
              f'提示级（analysis_out）{len(warnings)} 处')
    if blocking:
        print('  怎么改对：文件顶部 `import db.database as dbmod; dbmod.use_db_path(临时库)`，'
              '之后 get_db_conn/DAL 的写入全落临时库；或把 get_db_conn 换成桩。'
              '不要给 database/config/dal_state 的 SQLITE_DB_PATH 赋值（改了不生效）。')
    return 1 if blocking else 0


if __name__ == '__main__':
    sys.exit(main())
