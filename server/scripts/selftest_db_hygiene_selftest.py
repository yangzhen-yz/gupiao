"""selftest_db_hygiene_selftest.py - 换库检查器 `db_hygiene_check.py` 自身的回归自测（2026-11-07，修⑦）

这里守的是**元层**：检查器本身误报或漏报，机制就等于没有。
用户定的规矩是"检查工具要先用坏样本反证"，所以每一条规则都配一组
"必须报" + "必须不报"的用例，另外把 11-07 当天真踩过的两种写法钉成断言：

1. `db.database.SQLITE_DB_PATH = 临时库` —— 11-05 拆分后**静默无效**，自测绿着写真库；
   检查器必须报（R2）。
2. `eval_lab.SQLITE_DB_PATH = 临时库` —— 赋的是**被测模块自己**的全局，`eval_lab.py:46`
   在调用时才读它，所以这种是**有效**的；检查器不许报（报了就是把正常写法当违规，
   下一步就是有人把这个开关关掉）。
3. 只读冒烟（`get_db_conn()` + `load_*`）不是写库 —— 第一版把它算成写，误报了
   `database_split_selftest`，这条也钉住。
4. `sqlite3.connect('...gupiao.db')` 做 DELETE 清理 —— 必须报（R3）；
   同一条加上 `mode=ro` 就必须放行。
5. 真机制：`use_db_path()` 必须同时改到 `db.database` 与 `db.dal_state` 两份，
   并且 `get_db_conn()` 随后连的就是新路径 —— 这是整套规则成立的前提。

跑法：`python server/scripts/selftest_db_hygiene_selftest.py`
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import db.database as dbmod                      # noqa: E402
import db.dal_state as dal                       # noqa: E402
from scripts import db_hygiene_check as H        # noqa: E402

PASS, FAIL = [], []


def ck(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(f"  [{'OK' if cond else 'XX'}] {msg}")


def rules(src, rel='server/scripts/foo_selftest.py'):
    return {h['rule'] for h in H.scan_source(rel, src)}


# ========== [1] 真机制：use_db_path 改的是两份，且 get_db_conn 跟着走 ==========
print('[1] `use_db_path` 必须同时改 database 与 dal_state（整套规则的立足点）')
_ORIG = dbmod.SQLITE_DB_PATH
tmp = os.path.join(tempfile.gettempdir(), f'hygiene_{os.getpid()}.db')
for suf in ('', '-wal', '-shm'):
    try:
        os.remove(tmp + suf)
    except OSError:
        pass
try:
    dbmod.use_db_path(tmp)
    ck(dbmod.SQLITE_DB_PATH == tmp and dal.SQLITE_DB_PATH == tmp,
       '两份都被改掉（只改 database 那份副本 = 11-05 之后那个静默无效的坑）')
    c = dbmod.get_db_conn()
    c.execute('CREATE TABLE IF NOT EXISTS probe (k TEXT)')
    c.execute("INSERT INTO probe VALUES ('x')")
    c.commit()
    c.close()
    ck(os.path.exists(tmp), 'get_db_conn 随后连的就是新库（写进去了，没落到生产）')
    prod = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'gupiao.db')
    _c = sqlite3.connect(f'file:{prod}?mode=ro', uri=True)
    has_probe = _c.execute("SELECT 1 FROM sqlite_master WHERE name='probe'").fetchone()
    _c.close()
    ck(has_probe is None, '生产库里没有这张 probe 表（证明写入没漏到真库）')
    # 反证：只赋值 database 那份（老写法）不会改到 dal_state
    dbmod.use_db_path(_ORIG)
    dbmod.SQLITE_DB_PATH = tmp + '.fake'
    ck(dal.SQLITE_DB_PATH == _ORIG,
       '反证成立：`dbmod.SQLITE_DB_PATH = …` 单独赋值改不动 dal_state，'
       '而 get_db_conn 读的正是 dal_state —— 这就是"绿着写真库"的成因')
    dbmod.SQLITE_DB_PATH = _ORIG
finally:
    dbmod.use_db_path(_ORIG)
    for suf in ('', '-wal', '-shm'):
        try:
            os.remove(tmp + suf)
        except OSError:
            pass
ck(dbmod.SQLITE_DB_PATH == _ORIG and dal.SQLITE_DB_PATH == _ORIG, '收尾：库路径已还原')

# ========== [2] R1：会写库又没隔离 ==========
print('[2] R1「会写库但没隔离」——必须报，三种合法隔离都必须放行')
BAD = '''
import db.database as dbmod
from db.database import get_db_conn
def main():
    conn = get_db_conn()
    conn.execute("INSERT INTO t VALUES (1)")
'''
ck('R1' in rules(BAD), '裸用 get_db_conn + INSERT → 报 R1')
ck('R1' not in rules(BAD.replace('import db.database as dbmod',
                                 'import db.database as dbmod\ndbmod.use_db_path("/tmp/x.db")')),
   '加了 use_db_path → 放行')
ck('R1' not in rules('''
import sqlite3
import services.x as m
m.get_db_conn = lambda: sqlite3.connect("/tmp/x.db")
def main():
    m.get_db_conn().execute("INSERT INTO t VALUES (1)")
'''), '把 get_db_conn 换成桩 → 放行')
ck('R1' not in rules('''
import sqlite3, tempfile
p = tempfile.mkstemp(suffix=".db")[1]
def main():
    c = sqlite3.connect(p)
    c.execute("INSERT INTO t VALUES (1)")
'''), '全临时路径（tempfile + 没有生产库连接）→ 放行')
ck('R1' not in rules('''
import db.database as dbmod
def main():
    c = dbmod.get_db_conn()
    rows = c.execute("SELECT 1 FROM t").fetchall()
    d = dbmod.load_stock_basic_info()
'''), '反误报：只读冒烟（get_db_conn + SELECT/load_*）不算写 —— '
   '第一版把它算成写，误报了 database_split_selftest')
ck('R1' not in rules('''
EXPECTED = ['save_daily_recommendations', 'delete_user_position', 'get_db_conn', 'init_db']
def main():
    return len(EXPECTED)
'''), '反误报：长名单字符串里出现函数名不算调用（必须走 AST）')
ck(rules(BAD, rel='server/services/pick/recommend.py') == set(),
   '业务代码不判（规则只管自测文件，否则天天误伤）')

# ========== [3] R2：换库方式写错 ==========
print('[3] R2「赋门面副本的 SQLITE_DB_PATH」——必须报；赋被测模块自己的全局必须放行')
ck('R2' in rules('''
import db.database as dbmod
dbmod.SQLITE_DB_PATH = "/tmp/x.db"
def main():
    dbmod.get_db_conn().execute("INSERT INTO t VALUES (1)")
'''), 'dbmod.SQLITE_DB_PATH = … → 报 R2（静默无效那种）')
for form in ('cfg.SQLITE_DB_PATH = P', 'database.SQLITE_DB_PATH = P', 'D.SQLITE_DB_PATH = P',
             'dal_state.SQLITE_DB_PATH = P'):
    src = f'''
import app.config as cfg
import db.database as database
import db.dal_state as dal_state
D = database
{form}
def main():
    database.get_db_conn().execute("INSERT INTO t VALUES (1)")
'''
    ck('R2' in rules(src), f'`{form.split("=")[0].strip()}` 这类门面/副本赋值都要报')
ck('R2' not in rules('''
import services.support.eval_lab as eval_lab
eval_lab.SQLITE_DB_PATH = "/tmp/empty.db"
def main():
    return eval_lab.latest_run()
'''), '反证要钉住：`eval_lab.SQLITE_DB_PATH = …` 是改被测模块自己的全局（eval_lab.py:46 '
   '调用时才读），有效，不许报')
ck('R2' not in rules('''
import db.database as dbmod
dbmod.use_db_path("/tmp/x.db")
cfg.SQLITE_DB_PATH = "/tmp/y.db"
'''), '已经 use_db_path 了，再补一份 config 赋值（双保险写法）只提示不阻断')

# ========== [4] R3：直连生产库文件 ==========
print('[4] R3「自测直连生产库文件」——DELETE 清理要报，mode=ro 放行')
ck('R3' in rules('''
import sqlite3, os
from db.database import SQLITE_DB_PATH
def cleanup():
    con = sqlite3.connect(SQLITE_DB_PATH, timeout=10)
    con.execute("DELETE FROM westock_snapshot WHERE date LIKE '2099-%'")
    con.commit()
'''), '连 SQLITE_DB_PATH 做 DELETE（历史"退出前擦干净"那种）→ 报 R3')
ck('R3' in rules('''
import sqlite3
def probe():
    con = sqlite3.connect('data/history_kline.db')
    con.execute("INSERT INTO kline_cache VALUES ('x')")
'''), '写死相对路径连 `history_kline.db` 也算直连生产库')
ck('R3' not in rules('''
import sqlite3, os
P = os.path.join('/srv', 'gupiao.db')
def probe():
    con = sqlite3.connect(f'file:{P}?mode=ro', uri=True)
    return con.execute("SELECT 1").fetchall()
'''), '同一个文件用 `mode=ro` 只读探测 → 放行（护栏不该挡住只读核对）')

# ========== [5] 真实仓库：scripts/ 必须 0 阻断 ==========
print('[5] 真仓体检：server/scripts 下 26+ 份自测必须 0 处阻断')
blocking = []
for fn in sorted(os.listdir(os.path.join(os.path.dirname(os.path.abspath(__file__))))):
    if not fn.endswith('.py'):
        continue
    rel = f'server/scripts/{fn}'
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), fn),
               encoding='utf-8', errors='replace').read()
    for h in H.scan_source(rel, src):
        blocking.append(f'{rel}:{h["line"]}[{h["rule"]}]')
ck(not blocking, f'scripts/ 阻断级 0 处（实得 {blocking}）')

# ========== [6] 已知历史违规仍在清单里（不许被"修好"成看不见） ==========
print('[6] 一次性自测（analysis_out）的旧账：只提示不阻断，但必须还能看见')
p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 'analysis_out', 'db_sentinel_selftest.py')
if os.path.isfile(p):
    src = open(p, encoding='utf-8', errors='replace').read()
    ck('R3' in rules(src, rel='server/analysis_out/db_sentinel_selftest.py'),
       'db_sentinel（故意连真库测锁）仍被列为提示级——不是删掉判据，是分清轻重')
else:
    ck(True, 'analysis_out/db_sentinel_selftest.py 不在（一次性产物可能已被清理），跳过')
ck(H.SELFTEST_NAME.search('server/analysis_out/db_sentinel_selftest.py'),
   'analysis_out 里的自测也要被扫到（文件名规则不能只认 scripts/）')

# ========== [7] CLI 退出码 ==========
print('[7] --paths 的退出码：干净=0、违规=1（pre-commit 就靠这个拦）')


def _run_cli(*argv):
    _saved = sys.argv
    sys.argv = ['selftest_db_hygiene.py', *argv]
    try:
        return H.main()
    finally:
        sys.argv = _saved


_here = os.path.dirname(os.path.abspath(__file__))
ck(_run_cli('--paths', os.path.join(_here, 'cache_whitelist_selftest.py'), '--quiet') == 0,
   '干净的自测（已 use_db_path）→ 退出码 0')

# 文件名必须长得像自测才会被管（这一点也顺带验一遍命名规则）
tmp_bad = os.path.join(tempfile.gettempdir(), 'dbhygiene_bad_selftest.py')
with open(tmp_bad, 'w', encoding='utf-8') as fh:
    fh.write(BAD)
try:
    rc_bad = _run_cli('--paths', tmp_bad, '--quiet')
finally:
    os.remove(tmp_bad)
ck(rc_bad == 1, f'违规文件 → 退出码 1（实得 {rc_bad}）；绝对路径也能判，换个目录绕不过去')

tmp_nots = os.path.join(tempfile.gettempdir(), 'dbhygiene_tool.py')
with open(tmp_nots, 'w', encoding='utf-8') as fh:
    fh.write(BAD)
try:
    rc_nots = _run_cli('--paths', tmp_nots, '--quiet')
finally:
    os.remove(tmp_nots)
ck(rc_nots == 0, '名字不像自测的文件不判（业务代码/运维脚本不该被这条拦住）')

# ========== [8] R4：门面桩不算隔离（11-07 体检实拍漏口） ==========
print('\n[8] R4「桩打在门面/共享层」：既不许被当成隔离，也要单独点名')
_FACADE_STUB = '''
from db import database as db
def main():
    db.get_db_conn = lambda: None
    db.save_daily_recommendations([{"date": "2026-11-07"}])
'''
_r = rules(_FACADE_STUB)
ck('R4' in _r, f'`db.get_db_conn = 桩` + 真写库函数 → 报 R4（实得 {sorted(_r)}）')
ck('R1' in _r,
   '同一份源码同时报 R1（老版本把这种赋值算成"已隔离"，'
   '于是 scripts/daily_recs_cache_selftest.py 一路往真库写假批次、检查器一路绿）')
ck('R4' not in rules('''
from db import database as db
def main():
    db.use_db_path("/tmp/x.db")
    db.get_db_conn = lambda: (_ for _ in ()).throw(RuntimeError("故障注入"))
    db.save_daily_recommendations([{"date": "2026-11-07"}])
'''), '同一文件里已有 use_db_path ⇒ 门面桩只算故障注入，不报 R4（sched_liveness 那种写法）')
ck('R4' not in rules('''
from db.dal import recommend as dal_rec
def main():
    dal_rec.get_db_conn = lambda: None
    dal_rec.save_daily_recommendations([{"date": "2026-11-07"}])
'''), '反误报：桩打在**真正执行写入的那个模块**上是有效隔离，不报')
ck('R1' not in rules('''
from db.dal import recommend as dal_rec
def main():
    dal_rec.get_db_conn = lambda: None
    dal_rec.save_daily_recommendations([{"date": "2026-11-07"}])
'''), '同上也不报 R1（否则本次修好的那份自测自己就会被拦）')
for form in ('dbmod.get_db_conn = f', 'database.get_db_conn = f', 'dal_state.get_db_conn = f',
             'db.dal_state.get_db_conn = f'):
    src = '''
import db.database as database
import db.database as dbmod
import db.dal_state as dal_state
def main():
    __STUB__
    database.save_strategy_snapshot([dict(a=1)])
'''.replace('__STUB__', form)
    ck('R4' in rules(src), f'门面别名也拦得住：{form}')
ck(rules(_FACADE_STUB, rel='server/services/pick/recommend.py') == set(),
   '业务代码不判 R4（这条只管自测文件，判据与 R1~R3 同一条文件名门槛）')

print(f'\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败')
if FAIL:
    for m in FAIL:
        print('  失败：', m)
    sys.exit(1)
