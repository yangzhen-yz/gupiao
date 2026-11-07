"""Regression guard for the routes.py split.

Ensures the per-domain router split preserved EVERY endpoint: the set of
(method, path) exposed by ``app.api.routes.router`` must equal the baseline
captured when routes.py was a single file.

2026-11-07 加第二节（修⑩-1 的机制兜底）：路由/config 两次拆分留下的同一类病是
**引用改错**——端点函数搬走了，别处还在按老位置调。`py_compile` 与 import 都测不出
（语法合法、模块能导入），只有真走到那一行才 NameError，而且多半被 except 吞成一行
日志。实拍两处：`core/scheduler.py` 的 `_prewarm_hot_scan`（11-06 已修）与
`app/api/routes_state.py:212` 的批量扫描后台刷新（坏了整整一周，11-07 才发现）。
所以这一节用 AST 把 `app/api` 里"本模块 globals 和运行时都没有的名字"点出来，
并自带反证（探测器不能只报绿）。

Run: .venv/Scripts/python.exe server/scripts/routes_split_selftest.py
"""
import ast
import builtins
import os
import sys

# Make the ``app`` package importable when run as a standalone script.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api import routes as R

EXPECTED = [('GET', '/api/health'), ('GET', '/api/data-sources/health'), ('POST', '/api/data-sources/eastmoney-pause'), ('POST', '/api/data-sources/eastmoney-resume'), ('GET', '/api/hot_search_ranking'), ('GET', '/api/tab-scan-config'), ('GET', '/api/api-usage'), ('POST', '/api/api-usage/limits'), ('GET', '/api/eval-lab/latest'), ('POST', '/api/eval-lab/run'), ('GET', '/api/auction/latest'), ('GET', '/api/update-hot_search_ranking'), ('GET', '/api/stock/{symbol}'), ('GET', '/api/stock/{symbol}/analysis'), ('GET', '/api/stock/{symbol}/full'), ('GET', '/api/stocks'), ('GET', '/api/daily_recommendations'), ('GET', '/api/recommend-review'), ('GET', '/api/recommend-sim'), ('GET', '/api/recommend-batches'), ('GET', '/api/recommend-sim-batch'), ('GET', '/api/recommend-excess'), ('GET', '/api/shadow-eval'), ('GET', '/api/stock-search'), ('POST', '/api/auth/register'), ('POST', '/api/auth/login'), ('POST', '/api/auth/logout'), ('GET', '/api/auth/me'), ('GET', '/api/auth/users'), ('GET', '/api/auth/status'), ('POST', '/api/auth/change-password'), ('GET', '/api/positions'), ('POST', '/api/positions'), ('POST', '/api/positions/capital'), ('GET', '/api/push-channels'), ('POST', '/api/push-channels'), ('GET', '/api/push-channels/discover'), ('POST', '/api/push-channels/search-user'), ('POST', '/api/push-channels/aibot'), ('GET', '/api/push-channels/agent/status'), ('POST', '/api/push-channels/agent'), ('POST', '/api/push-channels/{ch_id}/test'), ('DELETE', '/api/push-channels/{ch_id}'), ('POST', '/api/positions/adjust'), ('DELETE', '/api/positions/{symbol}'), ('GET', '/api/sim/state'), ('POST', '/api/sim/generate'), ('POST', '/api/sim/execute'), ('POST', '/api/sim/sell'), ('POST', '/api/sim/reset'), ('POST', '/api/daily_recommendations'), ('DELETE', '/api/daily_recommendations/{date}'), ('POST', '/api/generate-recommendations'), ('POST', '/api/scan-ma10'), ('GET', '/api/trend-stocks'), ('GET', '/api/trend/history-counts'), ('GET', '/api/scan-trend-stocks'), ('POST', '/api/trend/backtest'), ('GET', '/api/trend/backtest-history'), ('GET', '/api/trend/backtest-latest'), ('GET', '/api/trend/backtest-records'), ('GET', '/api/stock-diagnose/{symbol}'), ('GET', '/api/stock-map'), ('POST', '/api/update-stock-map'), ('GET', '/api/custom-scan-pool'), ('GET', '/api/custom-scan-pool/symbols'), ('POST', '/api/custom-scan-pool'), ('PUT', '/api/custom-scan-pool/add'), ('PUT', '/api/custom-scan-pool/remove'), ('DELETE', '/api/custom-scan-pool'), ('GET', '/api/hot-stocks-scan'), ('GET', '/api/hot-stocks'), ('POST', '/api/hot-stocks'), ('PUT', '/api/hot-stocks/add'), ('PUT', '/api/hot-stocks/remove'), ('GET', '/api/stock-tags'), ('GET', '/api/stock-tags/{symbol}'), ('POST', '/api/stock-tags/{symbol}'), ('DELETE', '/api/stock-tags/{symbol}'), ('GET', '/api/stock-concepts/{symbol}'), ('GET', '/api/market-predict'), ('GET', '/api/market-alert'), ('GET', '/api/market-index'), ('GET', '/api/action-desk'), ('GET', '/api/theme-index'), ('GET', '/api/cls-ladder'), ('GET', '/api/cls-plate-stocks'), ('GET', '/api/kline/{symbol}'), ('GET', '/api/kline/{symbol}/minute'), ('POST', '/api/daily-review/generate'), ('GET', '/api/daily-reviews'), ('GET', '/api/strategy/weights'), ('POST', '/api/strategy/weights/reset'), ('POST', '/api/strategy/backtest'), ('GET', '/api/strategy/backtest-history'), ('GET', '/api/strategy/predictions'), ('GET', '/api/fund-flow'), ('GET', '/api/rps'), ('POST', '/api/rps/refresh'), ('GET', '/api/kline-history/stats'), ('GET', '/api/internal/ops-notify-limit'), ('GET', '/api/settings'), ('GET', '/api/settings/preview'), ('POST', '/api/settings/{key}'), ('POST', '/api/settings/{key}/reset'), ('POST', '/api/kline-history/sync'), ('POST', '/api/kline-history/backfill'), ('POST', '/api/fundamental/refresh'), ('GET', '/api/dragon-tiger'), ('GET', '/api/northbound'), ('POST', '/api/valuation/refresh'), ('GET', '/api/valuation'), ('GET', '/api/trend/backtest-summary'), ('GET', '/eastmoney/{path:path}'), ('POST', '/eastmoney/{path:path}'), ('GET', '/api/optimization/history'), ('GET', '/api/optimization/status'), ('POST', '/api/optimization/trigger'), ('GET', '/api/optimization/trend')]


def _collect():
    out = []
    for route in R.router.routes:
        methods = getattr(route, "methods", None) or set()
        for m in methods:
            out.append((m.upper(), route.path))
    return out


# ---------------- 第二节：app/api 里"用了但本模块没有"的名字 ----------------

def _bound_names(fn):
    """一个函数体里能看见的局部名：参数、赋值目标、内层 def/class、with/as、推导式、
    global/nonlocal、函数内 import（这正是修⑩-1 的写法——懒导入必须在作用域里算数）。"""
    names = set()
    a = fn.args
    for arg in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
        names.add(arg.arg)
    if a.vararg:
        names.add(a.vararg.arg)
    if a.kwarg:
        names.add(a.kwarg.arg)
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)          # 内层 def/class 的名字（`_bg`、`_run` 这类）
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for im in node.names:
                names.add((im.asname or im.name).split(".")[0])
    return names


def find_undef(tree, ns_contains):
    """返回 [(行号, 名字)]。`ns_contains(name)` 判"模块层面有没有这个名字"。

    作用域按函数链逐级合并（内层函数能看见外层函数的参数与局部名），
    所以 `codes_raw`、`pool_only` 这类闭包变量不会误报。
    """
    parent = {}
    for node in ast.walk(tree):
        for ch in ast.iter_child_nodes(node):
            parent[ch] = node
    top = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            tgts = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in tgts:
                for nm in ast.walk(t):
                    if isinstance(nm, ast.Name) and isinstance(nm.ctx, ast.Store):
                        top.add(nm.id)
    out = []
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        scope = set(top)
        cur = fn
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope |= _bound_names(cur)
            cur = parent.get(cur)
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                nm = node.id
                if nm in scope or nm in _BUILTIN or ns_contains(nm):
                    continue
                out.append((node.lineno, nm))
    return out


_BUILTIN = set(dir(builtins))

# 这次真正要守的那一行：后台刷新必须**在调用时**把端点函数取进来，
# 而不是依赖顶层有个同名全局（顶层永远不会有，scan 反向引 routes_state 会循环导入）。
_GUARDED = ("app/api/routes_state.py", "hot_stock_buttons_scan")


PROD_DIRS = ("app", "services", "core", "db")


def check_refs():
    """生产代码全量：`app/ services/ core/ db/`（不碰 scripts/ —— 自测里那些桩名本来就允许悬空）。

    为什么范围要铺到全仓而不是只查 app/api：这类"引用改错"两次都发生在**别的模块**
    （scheduler 预热、routes_state），只查拆分目录等于只看案发地点之一。
    """
    here = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    files = []
    for d in PROD_DIRS:
        base = os.path.join(here, d)
        for root, dirs, names in os.walk(base):
            dirs[:] = [x for x in dirs if x not in ("__pycache__",)]
            files += [os.path.join(root, x) for x in sorted(names) if x.endswith(".py")]
    bad, checked = [], 0
    for f in files:
        rel = os.path.relpath(f, here).replace(os.sep, "/")
        mod_name = rel[:-3]
        if mod_name.endswith(".__init__"):
            mod_name = mod_name[:-9]
        mod_name = mod_name.replace("/", ".")
        try:
            mod = __import__(mod_name, fromlist=["__name__"])
        except Exception:
            mod = None
        try:
            tree = ast.parse(open(f, encoding="utf-8").read())
        except Exception as e:
            bad.append((rel, 0, f"解析失败 {type(e).__name__}"))
            continue
        checked += 1
        ns = (lambda n, m=mod: hasattr(m, n)) if mod is not None else (lambda n: n in _BUILTIN)
        seen_here = set()
        for lineno, nm in find_undef(tree, ns):
            if (rel, nm) in seen_here:
                continue            # 同一个名字在一处反复出现只报一次（第一处带行号）
            seen_here.add((rel, nm))
            bad.append((rel, lineno, nm))
    return checked, bad


def check_falsify():
    """反证：探测器必须真能看见这类漏口，否则"0 处"没有意义。"""
    fails = []
    src_bad = "def _run():\n    return this_name_is_not_anywhere_xyz()\n"
    src_ok = "def _run():\n    this_name_is_not_anywhere_xyz = 1\n    return this_name_is_not_anywhere_xyz()\n"
    hit = find_undef(ast.parse(src_bad), lambda n: False)
    if not any(nm == "this_name_is_not_anywhere_xyz" for _, nm in hit):
        fails.append(f"反证失败：坏样本没被点出来（实得 {hit}）")
    miss = find_undef(ast.parse(src_ok), lambda n: False)
    if miss:
        fails.append(f"误报：函数内已赋值仍被点出来（实得 {miss}）")
    # 闭包：外层参数在内层函数里可见，不该算未定义
    src_closure = ("def outer(codes_raw):\n"
                   "    def _run():\n"
                   "        return codes_raw\n"
                   "    return _run\n")
    if find_undef(ast.parse(src_closure), lambda n: False):
        fails.append("误报：闭包变量被当成未定义（作用域链没合并）")
    # 本次修的那一行：改成"必须有函数内 import"这条不变量
    # 本次修的两处，各留一条"不许退回事故写法"的不变量
    rs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                      "app", "api", "routes_state.py")
    txt = open(rs, encoding="utf-8").read()
    if "from app.api.routers.scan import hot_stock_buttons_scan" not in txt:
        fails.append("routes_state 的批量扫描后台刷新没有函数内 import（会退回 11-06 那次的死引用）")
    lq = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                      "services", "data", "liquidity.py")
    ltxt = open(lq, encoding="utf-8").read()
    if "from app import config as sys_cfg" not in ltxt:
        fails.append("liquidity 没有把 config 门面绑成 sys_cfg（覆盖表读不到时退路会抛 NameError）")
    return fails


def main():
    actual = sorted(set(_collect()))
    expected = sorted(set(EXPECTED))
    missing = [e for e in expected if e not in actual]
    extra = [a for a in actual if a not in expected]
    rc = 0
    if missing or extra:
        print("FAIL: route set mismatch")
        print("missing:", missing)
        print("extra:", extra)
        print("actual count", len(actual), "expected count", len(expected))
        rc = 1
    else:
        print(f"OK: all {len(actual)} routes preserved after split")

    checked, bad = check_refs()
    if checked < 150:
        print(f"FAIL: 引用检查只扫到 {checked} 个文件（生产代码应有 150+，目录走样了就是检查器瞎了）")
        rc = 1
    if bad:
        print(f"FAIL: 生产代码里有 {len(bad)} 处引用在本模块 globals / 运行时都找不到（扫 {checked} 个文件）")
        for rel, lineno, nm in bad:
            print(f"  - {rel}:{lineno}  {nm}")
        rc = 1
    elif checked >= 150:
        print(f"OK: no dangling name reference in production code ({checked} files)")

    fails = check_falsify()
    for f in fails:
        print("FAIL: 探测器反证不通过 ->", f)
        rc = 1
    if not fails:
        print("OK: 引用检查器有牙（坏样本命中、好样本与闭包不误报、后台刷新的懒导入在位）")
    return rc


if __name__ == "__main__":
    sys.exit(main())
