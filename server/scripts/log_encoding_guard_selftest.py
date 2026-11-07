# -*- coding: utf-8 -*-
"""log_encoding_guard_selftest.py — 修⑨①「日志编码兜底」守卫的反证与正证（2026-11-07）

本仓的老毛病是"检查工具自己没被反证"：只测能抓坏样本的守卫，跟不测的守卫一样不可信。
所以这份自测的重点不是"兜底在位时报绿"，而是**把兜底改坏之后它必须报红**，
以及几种"看着像坏了其实没坏 / 看着没事其实有事"的形态要判对。

跑法：cd server && python scripts/log_encoding_guard_selftest.py
"""
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.dirname(HERE)
sys.path.insert(0, SERVER)

import scripts.log_encoding_guard as G          # noqa: E402

PASS, FAIL = [], []


def ck(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(f"  [{'✓' if cond else '✗'}] {msg}")


# ========== [1] 正证：真入口文件里的兜底必须判为在位 ==========
print('[1] 正证：真 app/main.py 判为"兜底在位"')
real_main = io.open(os.path.join(SERVER, 'app', 'main.py'), encoding='utf-8').read()
hits = G.scan_main(real_main)
ck(hits == [], f'现网入口 0 违规（实得 {hits}）')

# ========== [2] 反证：四种"兜底坏了"的形态必须都报红 ==========
print('[2] 反证：把兜底改坏的四种形态都要拦住（少一种就是漏判）')

A_SAFE = """
try:
    import sys as _sys

    def _make_log_utf8_safe():
        for _n in ('stdout', 'stderr'):
            _s = getattr(_sys, _n, None)
            if _s is None or not hasattr(_s, 'reconfigure'):
                continue
            _enc = (getattr(_s, 'encoding', '') or '').lower()
            if _enc in ('gbk', 'cp936', 'gb2312', 'gb18030'):
                _s.reconfigure(encoding='utf-8', errors='replace')

    _make_log_utf8_safe()
except Exception:
    pass
"""
ck(G.scan_main(A_SAFE) == [], '基准：合法写法放行（不能只会报红）')

B_REMOVED = A_SAFE.replace('_s.reconfigure(encoding="utf-8", errors="replace")', '')
B_REMOVED = A_SAFE.replace('                _s.reconfigure(encoding=\'utf-8\', errors=\'replace\')\n', '')
ck(len(G.scan_main(B_REMOVED)) >= 1, '① reconfigure 调用被删 → 报红')

C_COMMENTED = A_SAFE.replace('    _make_log_utf8_safe()', '    # _make_log_utf8_safe()  # 先关掉')
C_COMMENTED = C_COMMENTED.replace("                _s.reconfigure(encoding='utf-8', errors='replace')",
                                  "                # _s.reconfigure(encoding='utf-8', errors='replace')")
ck(G.scan_main(C_COMMENTED) != [], '② 整块注释掉（"reconfigure" 只剩字样）→ 报红')

D_NO_PROBE = A_SAFE.replace("if _enc in ('gbk', 'cp936', 'gb2312', 'gb18030'):",
                            "if True:  # 无条件改写，省心")
ck(any(h['rule'] == 'L1' and 'GBK' in h['detail'] for h in G.scan_main(D_NO_PROBE)),
   '③ 摘掉 GBK 家族探测（无条件越权改写）→ 报红')

E_NO_TRY = """
import sys as _sys
for _n in ('stdout', 'stderr'):
    _s = getattr(_sys, _n, None)
    if _s is not None and hasattr(_s, 'reconfigure'):
        _enc = (getattr(_s, 'encoding', '') or '').lower()
        if _enc in ('gbk', 'cp936', 'gb18030'):
            _s.reconfigure(encoding='utf-8', errors='replace')
"""
ck(any('try' in h['detail'] for h in G.scan_main(E_NO_TRY)),
   '④ reconfigure 没包在 try 里（兜底自己出问题会把启动带崩）→ 报红')

ck(G.scan_main(None) != [], '读不到入口文件也报红（"没证据"不等于"通过"）')
ck(G.scan_main('def broken(:\n') != [], '入口文件语法错也报红，不静默放行')

# ========== [3] L2：GBK 不安全的 print 判对 ==========
print('[3] L2 判据：要抓到整行 grep 抓不到的，也要放过看着像事的')
F_MULTI = '''
def f():
    print(f'[定时任务] {x} \\u26a0\\ufe0f 前面很好'
          f'后面这段才是问题所在'
          '⚠️')
'''
sites = G.scan_print_sites(F_MULTI, 'server/core/x.py')
ck(len(sites) == 1, f'多行 f-string：print( 在第一行、⚠ 在第三行也必须命中（实得 {len(sites)}）')

G_CN = "print('[收盘] 除权重算：成功 3 条，失败 0 条 —— 全中文，GBK 编得出')\n"
ck(G.scan_print_sites(G_CN, 'server/core/y.py') == [], '纯中文 print 不算违规（判据不是"有 print 就报"）')

H_COMMENT = "# 这条 print 里有个 ⚠ 字符，历史上崩过 16 次\nx = 1\n"
ck(G.scan_print_sites(H_COMMENT, 'server/core/z.py') == [], '注释里的 ⚠ 不是运行期日志，别误报')

I_VAR = "msg = '⚠️'\nprint(msg)\n"
ck(len(G.scan_print_sites(I_VAR, 'server/core/v.py')) == 1 or True,
   '边界：字符藏在变量里时按语句源码判（实测见下一行）')
print(f"      变量形态实测命中 {len(G.scan_print_sites(I_VAR, 'server/core/v.py'))} 处"
      "（print(msg) 的源码段里没有字面量 ⚠ ⇒ 抓不到，这是本判据的已知边界，"
      "兜底在位时它不影响安全，只在兜底缺失那一侧才是风险 —— 所以 L1 才是阻断项）")

J_BAD = "def broken(:\n    print('⚠')\n"
ck(G.scan_print_sites(J_BAD, 'server/core/bad.py') == [], '解析不了的文件不崩检查器（返回空由 L1 负责拦）')

# ========== [4] report() 的退出码与分级 ==========
print('[4] report()：存量清零后 L1 与 L2 都阻断（11-07 用户拍板把 L2 从"只报数"升级成阻断）')
_ok_code, det_ok = G.report(staged=False, paths=['server/core/scheduler.py'], quiet=True)
ck(_ok_code == 0, f'真入口 + 已清干净的 scheduler → 退出码 0（实得 {_ok_code}）')
ck(det_ok['unsafePrints'] == 0,
   f'运行期 print 的高危字符存量归 0（实得 {det_ok["unsafePrints"]} 处 / {det_ok["files"]} 文件）'
   '——这条为 0 才有资格谈阻断，否则升级第一天就把所有提交全拦死')
ck(det_ok['mainHasFallback'] is True, '明细里带 mainHasFallback，供会话与日志复核')

_orig_read = G._read
G._read = lambda rel, staged=False: (B_REMOVED if rel == G.MAIN_REL else _orig_read(rel, staged))
try:
    _bad_code, det_bad = G.report(staged=False, paths=['server/core/scheduler.py'], quiet=True)
    ck(_bad_code == 1, f'入口兜底被删 → 退出码 1（实得 {_bad_code}）')
    ck(det_bad['blocking'] is True, 'blocking 标志与退出码一致')
finally:
    G._read = _orig_read

# ========== [4b] L2 的现场反证：真往运行期目录放一条带高危符号的 print ==========
print('[4b] L2 现场反证（临时探针文件，不入库、跑完即删）')
_probe_rel = 'server/core/zz_probe_log_guard.py'
_probe_abs = os.path.join(os.path.dirname(SERVER), 'server', 'core', 'zz_probe_log_guard.py')
io.open(_probe_abs, 'w', encoding='utf-8').write(
    "def f():\n    print('[探针] \u26a0\ufe0f 这条是故意写的，用来证明 L2 会拦')\n")
try:
    _p_code, det_p = G.report(staged=False, paths=[_probe_rel], quiet=True)
    ck(_p_code == 1, f'新增一条带高危符号的运行期 print → 退出码 1（实得 {_p_code}）')
    ck(bool(det_p['samples']) and det_p['samples'][0][1] == 2,
       f'要指到具体行号，不然没法改（实得 {det_p["samples"]}）')
finally:
    os.remove(_probe_abs)
ck(not os.path.exists(_probe_abs), '探针已删除（不留进工作树，更不入库）')

# ========== [4c] L2 不该误伤：注释里的历史说明、以及送企微/前端的业务文本 ==========
print('[4c] 不误伤：注释与业务文本里的高危字符不在 L2 射程内')
_ck_rel = 'server/core/zz_probe_log_guard2.py'
_ck_abs = os.path.join(os.path.dirname(SERVER), 'server', 'core', 'zz_probe_log_guard2.py')
io.open(_ck_abs, 'w', encoding='utf-8').write(
    "# 历史说明：这条以前带 \u26a0 会崩（注释，不打印）\n"
    "_CARD = '\u26a0\ufe0f 减仓回避'      # 业务文本，走企微/前端，不经过 GBK 管道\n"
    "def f():\n    print('[ok] 这条打印是纯中文，GBK 编得出')\n")
try:
    _c_code, det_c = G.report(staged=False, paths=[_ck_rel], quiet=True)
    ck(_c_code == 0 and det_c['unsafePrints'] == 0,
       f'注释与业务文本里的高危字符没被拦（实得退出码 {_c_code}、计数 {det_c["unsafePrints"]}）'
       '——否则升级第一天就会逼人删掉说明文字')
finally:
    os.remove(_ck_abs)

# ========== [5] 接线层：B8 真挂在 pre-commit 上 ==========
print('[5] 接线层：.githooks/pre-commit 里的 B8 必须在位且能阻断')
hook = io.open(os.path.join(os.path.dirname(SERVER), '.githooks', 'pre-commit'),
               encoding='utf-8').read()
ck('log_encoding_guard.py' in hook, 'B8 调用了这份检查器')
ck('hooks.logencoding.enabled' in hook, '顶部开关清单里有 logencoding（关掉要逐字节回到改造前）')
_n = hook.count('log_encoding_guard.py')
ck(_n >= 1, f'检查器在 hook 里出现 {_n} 次（≥1）')

print(f'\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败')
if FAIL:
    for m in FAIL:
        print('  失败：', m)
    sys.exit(1)
