# -*- coding: utf-8 -*-
"""log_encoding_guard.py - 「日志不能成为故障点」的静态检查器（2026-11-07，修⑨①；机制层）

要守的不变量只有一句：**运行期 print 打不崩，靠的是 `app/main.py` 入口那段编码兜底。**

线上实拍（见 `docs/工程知识库.md` 11-02 那条）：看护把后端 stdout 重定向进
`server/server_supervised.log`，那条管道是 **GBK**。于是一条带 `⚠` 的 `print` 不是难看，
而是抛 `UnicodeEncodeError` **把它的调用方一起打死**——`[推荐模式]` 接力票告警自 10-26 起
崩了 16 次，连带吞掉后面那条标签汇总。修法是入口一次性 `reconfigure` 成 UTF-8 +
`errors='replace'`（只在探到 GBK 家族时动手），**没有逐行改写那些文案**：一处入口治同类问题，
改 30 多处 print 反而把"能不能打日志"重新变成人工纪律。

于是风险很明确：**只要那块兜底被删掉／注释掉／改坏，全仓每一处 GBK 不安全 print 立刻变成
"下一次重启就打死它所在的任务"**，而且不会有任何静态信号——崩的是运行期，写它的人早下班了。
11-04 复核时文档里写着"下一步（未做）：把这条做成 .githooks 的机制拦截"，本文件就是那一步。

两条规则（11-07 用户拍板后 L2 也升级为阻断）
--------
- **L1 入口编码兜底必须在位**：`app/main.py` 里必须同时找得到
  ① 真的 `reconfigure(` 调用、② GBK 家族编码探测（gbk/cp936/gb2312/gb18030 至少一个）、
  ③ 它包在 `try` 里（兜底自身绝不影响启动）、④ 顶层真的调用了那个兜底函数。
  四者缺一 → 拦下，别提交。
- **L2 运行期 print 里不许出现 GBK 编不出的字符**：扫 `app/ core/ services/` 的
  `print(...)` **语句**（AST，不是整行匹配），逐条试 `encode('gbk')`，有命中就拦。
  这条以前只报数（当时存量 25 处，逐行改文案不在计划里，靠 L1 兜住）；
  11-07 把存量清成 0 之后改成阻断——**从此"新写一条 ⚠ 日志"在提交阶段就被挡住，
  而不是等下一次重启在线上崩一次**。改法是换等价文本，不是加转义：
  `⚠️ → [警告]`、`▸ → >`、`◂ → <`（都 GBK 编得出，视觉提示和可 grep 性都没丢）。
  注意口径：只管**会被打印的语句**。注释、docstring、以及要送到企微卡片/前端的业务文本
  （那些走 UTF-8、不经过这条 GBK 管道）都不在这条的射程里，误拦只会逼人把说明文字删掉。

判据为什么用 AST 而不是 grep
----------------------------
- 整行匹配会**漏**：多行 f-string 里 `print(` 在第一行、`⚠` 在第三行，那三行单独都能编进 GBK，
  整行扫就看不见（本仓 `core/scheduler.py` 有几条这种）。
- 整行匹配也会**误报**：注释里写"这条 print 含 ⚠ 会崩"并不是运行期日志。
- 所以取 `ast.Call` 且 callee 是 `print` 的节点，用 `get_source_segment` 拿到整条语句再试编码。
  正反用例都钉在 `scripts/log_encoding_guard_selftest.py` 里（垃圾输入之后还要命中才算测到）。

用法
----
    python server/scripts/log_encoding_guard.py --staged     # pre-commit 的 B8 用它
    python server/scripts/log_encoding_guard.py --all        # 全仓体检（报当前风险面）
    python server/scripts/log_encoding_guard.py --quiet      # 只出结论行
"""
import argparse
import ast
import os
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

# 检查器自己绝不能因为"打印一条含 ⚠ 的样本"而崩在 GBK 控制台上——这正好是被检查的那类毛病。
# 只收 errors='replace'（不改编码，显示照旧），保证它永远能说完话。
for _n in ('stdout', 'stderr'):
    _s = getattr(sys, _n, None)
    if _s is not None and hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(errors='replace')
        except Exception:
            pass

SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(SERVER)

LIVE_DIRS = ('app', 'core', 'services')          # 运行期代码；scripts/analysis_out 是离线跑的
MAIN_REL = 'server/app/main.py'                  # 兜底所在（仓库根相对路径）
GBK_FAMILY = ('gbk', 'cp936', 'gb2312', 'gb18030')

__all__ = ['scan_main', 'scan_print_sites', 'report', 'main']


def _callee_name(node: ast.Call) -> str:
    f = node.func
    return getattr(f, 'id', None) or getattr(f, 'attr', None) or ''


def _decode(blob_or_text) -> str:
    if isinstance(blob_or_text, bytes):
        return blob_or_text.decode('utf-8', 'replace')
    return blob_or_text or ''


def _git_show_staged(rel: str) -> Optional[str]:
    """读暂存区里那份（pre-commit 必须审"将要提交的内容"，不是磁盘上的在建内容）。"""
    try:
        r = subprocess.run(['git', 'show', f':{rel}'], cwd=ROOT,
                           capture_output=True, timeout=20)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return _decode(r.stdout)


def _read(rel: str, staged: bool) -> Optional[str]:
    """rel 是仓库根相对路径。staged 模式优先读暂存区那份，没有（未暂存）就退回磁盘。"""
    if staged:
        got = _git_show_staged(rel)
        if got is not None:
            return got
    p = os.path.join(ROOT, rel.replace('/', os.sep))
    if not os.path.exists(p):
        return None
    try:
        return _decode(open(p, encoding='utf-8', errors='replace').read())
    except Exception:
        return None


def _top_level_call_names(tree: ast.Module) -> set:
    """顶层（会随 import 执行到的）语句里出现的被调用者名字。

    ⚠️ 不能用 `ast.walk(stmt)` 直接下钻：`try: def f(): ... ; f()` 这种写法里，
    walk 会钻进函数体，把体里的 `reconfigure` 当成"顶层调用过" —— 于是调用行被注释掉
    也照样判绿（11-07 第一版就在这儿翻车，反证② 报不出红）。这里显式跳过
    def/class 的子树，只认 import 那一刻真会跑到的语句。
    """
    out = set()
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue                        # 定义体里的调用是"等着被调"，不算被调
        if isinstance(node, ast.Call):
            out.add(_callee_name(node))
        stack.extend(ast.iter_child_nodes(node))
    return out


def scan_main(src: Optional[str]) -> List[Dict]:
    """L1：入口编码兜底是否仍在位。返回违规列表（空=在位）。"""
    if src is None:
        return [{'rule': 'L1', 'line': 0, 'detail': '读不到 app/main.py，无法确认编码兜底在位'}]
    hits: List[Dict] = []
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [{'rule': 'L1', 'line': e.lineno or 0, 'detail': f'app/main.py 解析不了：{e}'}]

    calls_reconfigure = []          # 真的调了 .reconfigure(...)
    in_try = set()                  # 包在 try 里的 reconfigure 调用行号
    gbk_probe = []                  # GBK 家族编码字面量出现的行
    guarded_block = []              # 兜底函数名/注释锚点

    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    in_try.add(sub.lineno)
        if isinstance(node, ast.Call) and _callee_name(node) == 'reconfigure':
            calls_reconfigure.append(node.lineno)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            low = node.value.lower().replace('-', '').replace('_', '')
            if any(g in low for g in GBK_FAMILY) and len(node.value) <= 40:
                gbk_probe.append(node.lineno)

    if 'reconfigure' not in src:
        hits.append({'rule': 'L1', 'line': 0,
                     'detail': 'app/main.py 里没有 reconfigure —— 全仓那些带 ⚠/✓ 的运行期 print '
                               '在 GBK 管道下会抛 UnicodeEncodeError 打死调用方（10-26 起崩 16 次那次）'})
    elif not calls_reconfigure:
        hits.append({'rule': 'L1', 'line': 0,
                     'detail': 'app/main.py 里 "reconfigure" 只剩字样、没有真的调用'
                               '（大概是整块被注释掉了——注释掉的兜底等于没有兜底）'})
    if not gbk_probe:
        hits.append({'rule': 'L1', 'line': 0,
                     'detail': '找不到 GBK 家族编码探测（gbk/cp936/gb2312/gb18030）——'
                               '无条件改写别人的 stdout 是越权，无条件不改是回到崩的那版；两者都要拦'})
    if calls_reconfigure and not (set(calls_reconfigure) & in_try):
        hits.append({'rule': 'L1', 'line': min(calls_reconfigure),
                     'detail': 'reconfigure 没包在 try 里 —— 兜底自身出问题会把启动带崩'})

    # 最容易漏的一种坏法：函数还定义着、**调用那一行被注释/删掉**。
    # 上面几项全都过（reconfigure 字样在、调用节点在、在 try 里、有编码探测），
    # 但它一次也不会执行 —— 于是 25 处 GBK 编不出的 print 全部裸奔。
    # 判据：含 reconfigure 调用的函数名（或模块级直接调用）必须出现在**顶层语句**里，
    # 顶层 = tree.body 里除去 def/class 定义体本身（定义体里的调用是"等着被调"，不算被调）。
    guard_names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(isinstance(s, ast.Call) and _callee_name(s) == 'reconfigure'
                   for s in ast.walk(node)):
                guard_names.add(node.name)
    top_calls = _top_level_call_names(tree)
    if calls_reconfigure and not (top_calls & guard_names) and 'reconfigure' not in top_calls:
        hits.append({'rule': 'L1', 'line': calls_reconfigure[0],
                     'detail': f'兜底函数 {sorted(guard_names) or "(未命名)"} 定义了但顶层没有调用它——'
                               '注释掉那一行等于没有兜底，而静态上前面几条全都还是绿的'})
    return hits


def scan_print_sites(src: Optional[str], rel: str) -> List[Tuple[int, str]]:
    """L2：该文件里 GBK 编不出的 print 语句 [(行号, 片段首 60 字)]。"""
    out = []
    if not src:
        return out
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _callee_name(node) == 'print':
            try:
                seg = ast.get_source_segment(src, node) or ''
            except Exception:
                seg = ''
            if not seg:
                continue
            try:
                seg.encode('gbk')
            except UnicodeEncodeError:
                out.append((node.lineno, seg.replace('\n', ' ')[:60]))
    return out


def _live_rels(paths: List[str]) -> List[str]:
    return [p for p in paths if p.startswith('server/') and
            p.split('/')[1] in LIVE_DIRS and p.endswith('.py')]


def _staged_rels() -> List[str]:
    try:
        r = subprocess.run(['git', 'diff', '--cached', '--name-only', '--diff-filter=ACMR'],
                           cwd=ROOT, capture_output=True, timeout=20)
    except Exception:
        return []
    return [x for x in _decode(r.stdout).split('\n') if x.strip()]


def _all_rels() -> List[str]:
    out = []
    for d in LIVE_DIRS:
        base = os.path.join(SERVER, d)
        for dp, dns, fns in os.walk(base):
            dns[:] = [x for x in dns if x != '__pycache__']
            for f in fns:
                if f.endswith('.py'):
                    rel = os.path.relpath(os.path.join(dp, f), ROOT).replace(os.sep, '/')
                    out.append(rel)
    return out


def report(staged: bool = False, paths: Optional[List[str]] = None,
           quiet: bool = False) -> Tuple[int, Dict]:
    """返回 (退出码, 明细)。退出码 0=放行，1=拦下。"""
    rels = _live_rels(paths if paths else (_staged_rels() if staged else _all_rels()))
    # 兜底是否在位要按"这次要提交的那份 main.py"判（index 里所有跟踪文件都读得到，
    # 没暂存 main.py 时它等于 HEAD 内容），审磁盘上的在建版本会漏掉"删掉兜底的这笔提交"。
    main_src = _read(MAIN_REL, staged=staged)
    l1 = scan_main(main_src)
    sites: List[Tuple[str, int, str]] = []
    for rel in rels:
        src = _read(rel, staged)
        for ln, seg in scan_print_sites(src, rel):
            sites.append((rel, ln, seg))

    n = len(sites)
    detail = {'l1': l1, 'unsafePrints': n, 'files': len(rels),
              'samples': sites[:8], 'blocking': bool(l1) or n > 0, 'mainHasFallback': not l1}
    if not quiet:
        if l1:
            print(f'[日志编码] L1 入口兜底缺失/改坏（{len(l1)} 条）：')
            for h in l1:
                print(f'  [L1] {MAIN_REL}'
                      + (f':{h["line"]}' if h['line'] else '') + f'  {h["detail"]}')
        print(f'[日志编码] L2 运行期 print 里 GBK 编不出的语句：{n} 处 / 扫 {len(rels)} 个文件'
              '（11-07 起这一条也阻断——存量已清成 0，新写的要拦住）')
        for rel, ln, seg in sites[:8]:
            print(f'      {rel}:{ln}  {seg}')
        if n > 8:
            print(f'      …另有 {n - 8} 处（--all 全量可见）')
    if l1 and not quiet:
        print('  上面 L1 说清了 app/main.py 那段兜底现在缺哪一块。')
    if (l1 or n) and not quiet:
        print('[日志编码] 结论：拦下。改法是把符号换成 GBK 编得出的等价文本'
              '（⚠️→[警告]、▸→>、◂→<），不是删日志说明、也不是加转义；'
              'L1 那几种则要把入口那块恢复回来。')
    if l1 or n:
        return 1, detail
    return 0, detail


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description='日志编码兜底守卫（pre-commit B8）')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--staged', action='store_true', help='只审暂存内容（pre-commit 用）')
    g.add_argument('--all', action='store_true', help='全仓体检')
    ap.add_argument('paths', nargs='*', help='指定仓库根相对路径')
    ap.add_argument('--quiet', action='store_true', help='只出结论')
    ns = ap.parse_args(argv)
    code, _ = report(staged=ns.staged, paths=ns.paths or None, quiet=ns.quiet)
    if not ns.quiet:
        print('  放行' if code == 0 else '  拦下')
    return code


if __name__ == '__main__':
    sys.exit(main())
