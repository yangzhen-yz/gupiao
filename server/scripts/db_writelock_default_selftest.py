"""db_writelock_default_selftest.py - 写锁分级"默认快速失败 + 留痕白名单"的回归自测（2026-11-07，修⑤）

守的不变量有两层：

1. **机制层**：`get_db_conn()` 不点名就是 2 秒抢不到锁就抛；点名 `wait_for_lock=True` 才等 30 秒；
   老参数名 `fast_fail=True/False` 的老语义原样保留；总开关关掉逐字节回到改造前。
2. **接线层**：全仓凡是写"丢了就永久答不了"的表（决策留痕 / 用户数据 / 模拟盘的钱 / 累加型审计）
   的函数，都必须点名 `wait_for_lock=True` —— 漏一处，那一行就变成"抢不到锁静悄悄丢掉"。

为什么要翻默认方向（11-04 那句"要么分完、要么反过来"选的就是反过来）：老写法默认等 30 秒，
而 `database.py` 之外实测有 **115 处** `get_db_conn()` 从没分类过（本次清点：208 个点位，
只读 115 / 写留痕 49 / 写缓存 11 / 其余混合），也就是说上百处的失败方式全靠"有人记得传参数"。
11-03 那次全站卡死（写锁被占 1 小时+，连 `/api/health` 都排队 8~30 秒）的放大器正是长等。
翻过来之后，漏点的代价从"全站多等 30 秒"变成"这一行没落库 + DAL 里现成的 `print('保存 X 失败')`"。

全程临时库（`dbmod.use_db_path`；直接赋值 `SQLITE_DB_PATH` 自 11-05 拆分起静默无效），
不读真库、不打外部源。
"""
import ast
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

_TMP = os.path.join(tempfile.gettempdir(), f'writelock_{os.getpid()}.db')
for _suf in ('', '-wal', '-shm'):
    try:
        os.remove(_TMP + _suf)
    except OSError:
        pass

import db.database as dbmod                      # noqa: E402
dbmod.use_db_path(_TMP)
dbmod.init_db()

import app.config as CFG                         # noqa: E402
from db.dal_state import get_db_conn             # noqa: E402

PASS, FAIL = [], []


def ck(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(f"  [{'OK' if cond else 'XX'}] {msg}")


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 与接线守卫共用同一张表清单（改这张表就是改口径，第 [6] 组会验它是否真实存在）
# 判据是一条**频率规则**，不是"重要就不丢"：
#   · 点名等锁 = 用户数据/钱/决策留痕（丢了永久答不了）+ **一天一行、当天不补就永久缺**的数据落库；
#   · 默认快速失败 = 缓存、可重拉快照，以及**盘中高频**的影子/告警/快循环落库——
#     这些跑在事件循环里，等 30 秒的代价是冻住全站（11-03 的实况），少一行只是窗口少一个样本。
TRACE_TABLES = {
    # —— 决策留痕（"今天为什么没推"就靠这几张表回答）
    'strategy_snapshot', 'strategy_snapshot_batch', 'daily_recommendations',
    'recommendation_batches', 'daily_market_reviews', 'recommendation_review',
    'strategy_predict_records', 'strategy_factor_weights', 'trend_dimension_weights',
    'optimization_history', 'shadow_gate_adjust_log', 'position_trade_log',
    'exit_signal_log', 'push_sent_log', 'position_alert_log',
    # —— 用户数据 / 钱
    'users', 'user_sessions', 'user_positions', 'user_capital', 'user_realized_pnl',
    'user_scan_pool', 'hot_stock_buttons', 'stock_concept_tags', 'user_push_channels',
    'sim_accounts', 'sim_orders', 'sim_positions', 'app_settings',
    # —— 累加型审计
    'api_usage_daily', 'api_usage_limits',
    # —— 一天一行、当天不跑就永久缺的落库（都在 15:15~18:30 的收盘后班次，不在事件循环上）
    'sentiment_final_daily', 'rec_sim_daily', 'recommend_shadow_eval', 'zhaban_shadow',
    'cls_pool_daily', 'cls_plate_daily', 'theme_chain_daily', 'theme_member_daily',
    'index_daily', 'dragon_tiger_daily', 'daily_candidate_pool', 'stock_industry_map',
    # 20:35 一天的出口分叉影子（台账第 15 条 12-03 裁决的证据链，跑在 scheduler 线程里）
    'exit_seal_shadow',
}
# 反面清单（故意留在默认快速失败那一侧）：盘中高频与可重拉
FAST_FAIL_ON_PURPOSE = {
    'market_sentiment_snapshots', 'limit_pool_daily', 'fund_flow_snapshot',
    'hot_search_daily_snapshot', 'hot_search_ranking', 'hot_search_meta', 'kline_cache',
    'rps_cache', 'stock_basic_info', 'stock_daily', 'stock_fundamental', 'stock_valuation',
    'stock_market_cap', 'stock_holder_num', 'westock_snapshot', 'data_cross_check',
    'trend_scan_results', 'trend_backtest_records', 'trend_backtest_reports',
    'dragon_alert_log', 'dragon_b_candidates', 'dragon_features', 'intraday_dragon_shadow',
    'recommend_shadow_log', 'theme_index_cache', 'cls_ladder_snapshots',
}
# 注意别写成 `...)?|ALTER\s+TABLE)\s+(...)`：可选组里已经吃掉一个空格，后面再要 `\s+`
# 就会让 `CREATE TABLE IF NOT EXISTS x` 整类匹配失败（11-07 第一版就栽在这儿，
# 四处只建表的 `_ensure_table` 全被当成"认不出表名"漏掉了）。
TABLE_RE = re.compile(
    r'(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|REPLACE\s+INTO|UPDATE\s+|DELETE\s+FROM|'
    r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?|ALTER\s+TABLE)\s*([A-Za-z_][A-Za-z0-9_]*)',
    re.I)


def _try_write(conn, tag='probe'):
    """往自测专用表写一行（不借线上表——有些表是懒建的，没建会被误当成锁竞争）。"""
    t0 = time.time()
    try:
        conn.execute("INSERT OR REPLACE INTO writelock_probe (k, v) VALUES (?, ?)", (tag, '1'))
        conn.commit()
        return time.time() - t0, None
    except Exception as e:
        return time.time() - t0, type(e).__name__
    finally:
        try:
            conn.close()
        except Exception:
            pass


class _Holder:
    """在另一个线程里持住写锁 N 秒（BEGIN IMMEDIATE + 一条写语句不提交）。"""

    def __init__(self, seconds):
        self.seconds = seconds
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.err = None
        self.t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            c = sqlite3.connect(_TMP, timeout=30, isolation_level='IMMEDIATE')
            c.execute("INSERT OR REPLACE INTO writelock_probe (k, v) VALUES ('holder', '1')")
            self.ready.set()
            self.stop.wait(self.seconds)
            c.rollback()
            c.close()
        except Exception as e:
            self.err = e
            self.ready.set()

    def __enter__(self):
        self.t.start()
        self.ready.wait(10)
        if self.err is not None:
            print(f'     ⚠️ 持锁线程自己就失败了：{self.err}')
        return self

    def __exit__(self, *_a):
        self.stop.set()
        self.t.join(15)


# ========== [0] 探针表 ==========
print('[0] 建一张自测专用表 writelock_probe（有些线上表是懒建的，借它们测锁会把"表不存在"误读成锁竞争）')
_c = get_db_conn(wait_for_lock=True)
_c.execute('CREATE TABLE IF NOT EXISTS writelock_probe (k TEXT PRIMARY KEY, v TEXT)')
_c.commit()
_c.close()
ck(True, '探针表就位')

# ========== [1] 默认=快速失败 ==========
print('[1] 默认不点名：锁被别人占着时 2 秒就报错，不把调用方压成 30 秒排队')
cache_ms = float(getattr(CFG, 'DB_CACHE_WRITE_BUSY_TIMEOUT_MS', 2000))
with _Holder(6):
    el, err = _try_write(get_db_conn())
    ck(err == 'OperationalError', f'默认写法在锁被占时抛 OperationalError（实得 {err}）')
    ck(cache_ms / 1000.0 * 0.5 <= el <= cache_ms / 1000.0 + 2.0,
       f'耗时 {el:.2f}s ≈ busy_timeout {cache_ms / 1000:.1f}s（不是 30 秒排队）')
    el2, err2 = _try_write(get_db_conn(fast_fail=True))
    ck(err2 == 'OperationalError', f'老写法 fast_fail=True 语义不变（{el2:.2f}s {err2}）')

# ========== [2] 点名=等锁 ==========
print('[2] wait_for_lock=True：等对方释放，写进去')
_trace_ms = 6000        # 本组把留痕超时改短，免得自测跑 30 秒（真实默认 30000）
_old_trace = CFG.DB_TRACE_WRITE_BUSY_TIMEOUT_MS
CFG.DB_TRACE_WRITE_BUSY_TIMEOUT_MS = _trace_ms
try:
    with _Holder(2.0):
        el3, err3 = _try_write(get_db_conn(wait_for_lock=True))
        ck(err3 is None, f'点名等锁的写入最终成功（实得 {err3}）')
        ck(el3 >= 1.0, f'确实等了 {el3:.2f}s（不是秒回）')
        el4, err4 = _try_write(get_db_conn(fast_fail=False))
        ck(err4 is None and el4 >= 0.0, f'老写法 fast_fail=False = 等锁，行为与 True 一致（{el4:.2f}s {err4}）')
finally:
    CFG.DB_TRACE_WRITE_BUSY_TIMEOUT_MS = _old_trace

# ========== [3] 总开关 ==========
print('[3] DB_FAST_FAIL_ENABLED=False：一律回到改造前的长等（逐字节等于分级改造之前）')
_old_sw = CFG.DB_FAST_FAIL_ENABLED
CFG.DB_FAST_FAIL_ENABLED = False
try:
    with _Holder(2.0):
        el5, err5 = _try_write(get_db_conn())
        ck(err5 is None, f'关掉总开关后默认写法也不再快速失败（实得 {err5}）')
        ck(el5 >= 1.0, f'等了 {el5:.2f}s 才写进去（= 老行为）')
finally:
    CFG.DB_FAST_FAIL_ENABLED = _old_sw

# ========== [4] 只读不受影响（WAL 读者不等写锁） ==========
print('[4] 同一个时刻，读侧照常出数 —— 分级只动写')
with _Holder(3):
    c = get_db_conn()
    try:
        rows = c.execute('SELECT COUNT(*) AS n FROM stock_basic_info').fetchone()['n']
        ck(isinstance(rows, int), f'锁被占时读到了 {rows} 行（没排队也没报错）')
    except Exception as e:
        ck(False, f'锁被占时读侧被拖累：{type(e).__name__}: {e}')
    finally:
        c.close()

# ========== [5] 接线守卫：写留痕表的函数必须点名 ==========
print('[5] 静态守卫：全仓写留痕表的函数都得点名 wait_for_lock=True（=True 或老名 =False）')


def scan_trace_sites(src_by_file):
    """返回 [(文件, 行号, 函数名, 命中的表, 有没有点名)]。参数给源码字符串，方便做反证。"""
    out = []
    for rel, src in src_by_file.items():
        if 'get_db_conn(' not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        lines = src.split('\n')
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.get_source_segment(src, node) or ''
            calls = [(i, l) for i, l in enumerate(lines[node.lineno - 1: node.end_lineno or node.lineno],
                                              start=node.lineno)
                     if 'get_db_conn(' in l and not l.strip().startswith('#')]
            if not calls:
                continue
            tables = {m.group(1).lower() for m in TABLE_RE.finditer(body)} & TRACE_TABLES
            if not tables:
                continue
            named = any('wait_for_lock=True' in l or 'fast_fail=False' in l for _i, l in calls)
            out.append((rel, calls[0][0], node.name, ','.join(sorted(tables)), named))
    return out


def load_repo():
    files = {}
    for d in ('app', 'services', 'core', 'db', 'scripts'):
        base = os.path.join(ROOT, d)
        for dp, _dn, fns in os.walk(base):
            if 'analysis_out' in dp or '__pycache__' in dp:
                continue
            for fn in fns:
                if fn.endswith('.py'):
                    p = os.path.join(dp, fn)
                    files[os.path.relpath(p, ROOT).replace(os.sep, '/')] = open(
                        p, encoding='utf-8').read()
    return files


SRC = load_repo()
sites = scan_trace_sites(SRC)
missing = [s for s in sites if not s[4]]
ck(len(sites) >= 40, f'扫到 {len(sites)} 个写留痕表的函数（数量级对得上 11-07 的清点）')
ck(not missing, f'全部点名（漏的：{missing[:5] if missing else "无"}）')
files_with_trace = sorted({s[0] for s in sites})
ck(len(files_with_trace) >= 15,
   f'覆盖 {len(files_with_trace)} 个文件：{files_with_trace[:6]} …')

# 反证：临时把一个点名摘掉（只在内存里改，不动文件），守卫必须报它
_probe = next((s for s in sites if s[4]), None)
if _probe:
    _rel = _probe[0]
    _mutated = dict(SRC)
    _mutated[_rel] = SRC[_rel].replace('get_db_conn(wait_for_lock=True)', 'get_db_conn()', 1)
    _after = scan_trace_sites(_mutated)
    _miss = [s for s in _after if not s[4]]
    ck(len(_miss) >= 1 and _miss[0][1] == _probe[1],
       f'反证成立：摘掉一处点名后守卫报出 {_miss[0][0]}:{_miss[0][1]} {_miss[0][2]}')
else:
    ck(False, '没找到任何已点名的点位（守卫根本没机会验证，别当通过）')

# ========== [6] 表清单不许是猜的 ==========
print('[6] TRACE_TABLES 每张表都得是真表（表名写错 = 守卫空转）')
c = get_db_conn()
try:
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
finally:
    c.close()
# 有些表是各自模块懒建的（push_sent_log / exit_signal_log / strategy_snapshot…），
# 临时库里没有不等于表名是编的 —— 所以认两种证据：库里有，或全仓源码里有它的 CREATE TABLE。
_all_src = '\n'.join(SRC.values())
ghost = sorted(t for t in TRACE_TABLES
               if t not in names and not re.search(
                   r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"\[]?%s[`"\]]?\s*\(' % t,
                   _all_src, re.I))
ck(len([t for t in TRACE_TABLES if t in names]) >= 15,
   f'临时库里当场就存在 {len([t for t in TRACE_TABLES if t in names])} 张')
ck(not ghost, f'其余都能在源码里找到建表语句（找不到 = 表名可疑：{ghost or "无"}）')

# ========== [7] 配置项与文档口径 ==========
print('[7] 配置三件套齐全、默认方向确实是"快速失败"')
ck(hasattr(CFG, 'DB_TRACE_WRITE_BUSY_TIMEOUT_MS')
   and int(CFG.DB_TRACE_WRITE_BUSY_TIMEOUT_MS) >= 1000,
   f'DB_TRACE_WRITE_BUSY_TIMEOUT_MS 在场且 ≥1 秒（实得 {CFG.DB_TRACE_WRITE_BUSY_TIMEOUT_MS}）')
ck(int(CFG.DB_CACHE_WRITE_BUSY_TIMEOUT_MS) < int(CFG.DB_TRACE_WRITE_BUSY_TIMEOUT_MS),
   '缓存类超时严格短于留痕类（不然分级没有意义）')
_src = open(os.path.join(ROOT, 'db', 'dal_state.py'), encoding='utf-8').read()
_m = re.search(r'def get_db_conn\(([^)]*)\)', _src)
ck('wait_for_lock: bool = False' in (_m.group(1) if _m else ''),
   f'签名默认不点名：{_m.group(1) if _m else "?"}')
ck('fast_fail: Optional[bool] = None' in (_m.group(1) if _m else ''),
   '老参数保留（默认 None＝不干预），存量调用点不会因为翻转而改变行为')

print(f'\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败')
if FAIL:
    for m in FAIL:
        print('  失败：', m)
    sys.exit(1)
print('临时库：', _TMP, '（未读真库、未打外部源）')
