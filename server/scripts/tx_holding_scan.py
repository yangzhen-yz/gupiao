# -*- coding: utf-8 -*-
"""事务持锁扫描（2026-11-03 上线）：找"拿到写锁之后、提交之前"夹了外部 IO / 慢操作的代码。

为什么要这个脚本
    2026-11-03 页面 8~30 秒抖动、前端报"网络错误"的根因不是接口慢，而是
    `db/database.py` 的 `get_db_conn()`（`isolation_level='IMMEDIATE'` +
    `busy_timeout=30000`）在循环里第一条 DML 就抢到 WAL 写锁，之后整轮循环都在锁内
    `await` 外部 K 线（`backtest_yesterday_predictions` 的补验段）。WAL 只允许一个写者，
    于是全库写排队超时。症状特征：`X-Process-Time` 只有 1~2ms，而请求耗时几十秒——
    耗时发生在进入 HTTP 中间件之前，按"接口慢"去查一定查偏。
    这个坑早在 `analysis_out/sim_sell_selftest.py` 的注释里被认识过一次（"IMMEDIATE 事务
    不释放写锁，cleanup 会在另一条连接上卡到 database is locked"），但没沉淀成拦截。
    按"规则必须有机制兜底"的约定，脚本进版本控制（`server/scripts/`），由
    `.githooks/pre-commit` 的 B6 项在每次提交时调用（只提醒、不阻断）。

判据（v2，整段比字符偏移，不逐行）
    按函数体切，找第一条 DML 的偏移，再找它之后第一个 `commit()` 的偏移；两者之间出现
    await / 网络 / sleep / 已知慢函数调用即命中。
    v1 按单行匹配，`cursor.execute(` 与 `'''UPDATE` 分行时匹配不到，对已知有 bug 的旧版
    strategy.py 也报 0 命中；另外注释里写的"await"字样会被当成真实调用误报。两处都已修，
    并保留 `--selfcheck` 反证。

已知边界（不要把它当"扫过就安全"）
    - 只认"同一函数体内 conn + DML + 慢调用"；跨函数把 conn 传出去、`with conn:` 上下文、
      显式 `BEGIN` 都漏。
    - 慢操作是白名单，以后新写的重函数不认得（宁可漏报不误拦）。
    - `--staged` 检的是工作树内容 + 本次新增行号区间；部分暂存时可能与索引内容不一致。

用法
    python server/scripts/tx_holding_scan.py                 # 全仓扫描（人工排查用）
    python server/scripts/tx_holding_scan.py --staged        # pre-commit 用：只报本次新增行
    python server/scripts/tx_holding_scan.py --selfcheck <文件>   # 反证：已知有 bug 的旧版必须命中
"""
import io
import os
import re
import subprocess
import sys

# 输出必须是 UTF-8：Windows 原生 python 默认按控制台代码页（本机 GBK）写 stdout，
# 被 sh 版 pre-commit 捕获后中文就成了乱码（首跑实测）。
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(SERVER_DIR)
SKIP_DIRS = ('.venv', 'analysis_out', 'static', '__pycache__', '.git', 'node_modules')

# DML：允许 execute( 与引号、引号与关键字之间任意换行
DML = re.compile(
    r'''execute\w*\(\s*[uU]?['"]{1,3}\s*(INSERT|UPDATE|DELETE|REPLACE)\b'''
    r'''|['"]{3}\s*(INSERT|UPDATE|DELETE|REPLACE)\b''', re.I)
COMMIT = re.compile(r'\bcommit\(\)')
CONN = re.compile(r'get_db_conn\(|sqlite3\.connect\(')
FUNC = re.compile(r'^(?:async\s+)?def\s+(\w+)\s*\(', re.M)
SLOW = re.compile(
    r'(\bawait\s|\brequests\.(?:get|post)\b|\bhttpx\b|urlopen|fetch_with_retry'
    r'|fetch_stock_data|get_kline_data|_get_next_day_change|\btime\.sleep\('
    r'|asyncio\.sleep\(|_analyze_misjudgments|open_hold_excess|run_auto_optimization'
    r'|qt\.gtimg|push2|ifzq)')


def _strip_comments(src):
    """把 # 注释替换成等长空格：保住字符偏移（行号要对得上），又不让注释里的
    "await" 这类字样被当成真实调用（v2 首跑就被自己写的注释误报过一次）。"""
    return re.sub(r'#[^\n]*', lambda m: ' ' * len(m.group(0)), src)


def scan_file(path):
    """返回该文件里"第一条 DML 之后、下一个 commit 之前夹了慢操作"的命中列表。"""
    src = _strip_comments(io.open(path, encoding='utf-8', errors='replace').read())
    hits = []
    funcs = [(m.start(), m.group(1)) for m in FUNC.finditer(src)]
    for i, (pos, name) in enumerate(funcs):
        end = funcs[i + 1][0] if i + 1 < len(funcs) else len(src)
        body = src[pos:end]
        if not CONN.search(body):
            continue
        dml = DML.search(body)
        if not dml:
            continue
        dml_at = dml.start()
        cm = COMMIT.search(body[dml_at:])
        commit_at = dml_at + cm.start() if cm else len(body)
        slow = SLOW.search(body[dml_at:commit_at])
        if slow:
            abs_pos = pos + dml_at + slow.start()
            rel = os.path.relpath(path, REPO_DIR).replace('\\', '/')
            hits.append({
                'file': rel,
                'func': name,
                'line': src[:abs_pos].count('\n') + 1,
                'dml_line': src[:pos + dml_at].count('\n') + 1,
                'committed': bool(cm),
                'code': src[abs_pos:abs_pos + 90].split('\n')[0].strip(),
            })
    return hits


def _git(args):
    """调 git 拿文本输出。encoding 必须显式 utf-8：Windows 原生 python 在 text=True 下
    按 locale（本机 GBK）解码，diff 里只要有一行中文注释就会抛 UnicodeDecodeError，
    而异常发生在 reader 线程、外层 try 抓不住，结果是 stdout 为空——B6 首跑没提醒
    就是这个原因（判据本身命中，只是行号区间拿不到）。"""
    try:
        return subprocess.run(['git'] + args, cwd=REPO_DIR, capture_output=True,
                              text=True, encoding='utf-8', errors='replace').stdout or ''
    except Exception:
        return ''


def staged_py_files():
    out = _git(['diff', '--cached', '--name-only', '--diff-filter=ACM', '--', '*.py'])
    return [l.strip() for l in out.splitlines() if l.strip()]


def added_line_ranges(path):
    """从 `git diff --cached -U0` 取新文件侧的行号区间（只看本次新增/改动的行）。"""
    out = _git(['diff', '--cached', '-U0', '--', path])
    ranges = []
    for m in re.finditer(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@', out, re.M):
        start = int(m.group(1))
        count = int(m.group(2)) if m.group(2) else 1
        ranges.append((start, start + max(count, 1) - 1))
    return ranges


def in_ranges(line, ranges):
    return any(lo <= line <= hi for lo, hi in ranges)


def run_staged():
    files = staged_py_files()
    if not files:
        return 0
    reported = []
    for rel in files:
        abs_path = os.path.join(REPO_DIR, rel.replace('/', os.sep))
        if not os.path.exists(abs_path):
            continue
        ranges = added_line_ranges(rel)
        if not ranges:
            continue
        for h in scan_file(abs_path):
            if in_ranges(h['line'], ranges) or in_ranges(h['dml_line'], ranges):
                reported.append(h)
    for h in reported:
        print('%s:%d  %s()  第一条 DML 在 %d 行，锁还没交还就调了：%s'
              % (h['file'], h['line'], h['func'], h['dml_line'], h['code']))
    return len(reported)


def run_full():
    all_hits = []
    scanned = 0
    for dp, dn, fns in os.walk(SERVER_DIR):
        if any(s in dp for s in SKIP_DIRS):
            continue
        for fn in fns:
            if not fn.endswith('.py'):
                continue
            scanned += 1
            all_hits.extend(scan_file(os.path.join(dp, fn)))
    print('扫描 .py 文件数=%d  风险点=%d' % (scanned, len(all_hits)))
    for h in sorted(all_hits, key=lambda x: (x['file'], x['line'])):
        print('  %s:%d  in %s()  [第一条DML在%d行, %scommit, 锁内夹了:] %s'
              % (h['file'], h['line'], h['func'], h['dml_line'],
                 '之后有' if h['committed'] else '之后无', h['code']))
    return len(all_hits)


def main():
    args = sys.argv[1:]
    if args and args[0] == '--staged':
        return 0 if run_staged() == 0 else 2      # 提醒用：命中≠失败，退出码只作区分
    if args and args[0] == '--selfcheck':
        hits = scan_file(args[1])
        print('selfcheck %s -> 命中 %d' % (args[1], len(hits)))
        for h in hits:
            print('   %s() 行%d [第一条DML@行%d, 之后%s commit]'
                  % (h['func'], h['line'], h['dml_line'],
                     '有' if h['committed'] else '无'))
        return 0 if hits else 1
    run_full()
    return 0


if __name__ == '__main__':
    sys.exit(main())
