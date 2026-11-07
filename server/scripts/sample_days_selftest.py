# -*- coding: utf-8 -*-
"""sample_days_selftest.py — 修⑧「影子样本钟跳过空日」的长期回归自测（2026-11-07，含二次更正）

守的不变量只有一句：**任何"攒够 N 个交易日就能裁决"的影子，天数只能数
"当天在交易日历里、且当天确实生成过推荐批次"的日子。**

为什么值得长期守：11-06 调度链被冻 12 小时，`daily_recommendations` 全天 0 行、实时数据
不可回补。这种日子如果被计进 20 天的钟，后果不是"少一天"，而是
① 均值/胜率的分母虚高（窗口里掺一个没有决策样本的空日）；
② "第 20 个交易日 ≈ 某月某日"的判据日期整体往后漂，到期了其实样本没到，
   但账面写着"到期"——这是最容易被当成结论的那类假数。

**11-07 二次更正的由来（这条比口径本身更值得记住）**：第一版的第二条判据是"周一至周五"，
用户一句反问把它推翻了——工作日不等于交易日，10-01（周四）、10-05（周一）这类法定节假日
照样不开市。现在日历取自本地日K 表（`stock_daily` 的 DISTINCT date，实测 688 个交易日、
国庆 10-01~10-07 一个都没有），读不到才退回星期几近似，并把 `calendarSource=weekday_fallback`
打在返回值和日志里。第 [3] 组专门演这两个反证：**把节假日塞进日历它就混进样本**、
**日历不可用时 10-05 那个周一会混进样本**。

跑法：cd server && python scripts/sample_days_selftest.py
（[6][7][10] 三组会碰库：临时库走 `use_db_path`，真库/真日K 库只读 `mode=ro`）
"""
import ast
import io
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import app.config as cfg                       # noqa: E402
import db.database as dbmod                    # noqa: E402
from services.support import sample_days as SD  # noqa: E402

PASS, FAIL = [], []


def ck(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(f"  [{'✓' if cond else '✗'}] {msg}")


def _tmp_db(tag):
    p = os.path.join(tempfile.gettempdir(), f'sample_days_{tag}_{os.getpid()}.db')
    if os.path.exists(p):
        os.remove(p)
    return p


# 假日历：11-06（周五）与 10-08 都是交易日；11-07 周六、10-01 周四、10-05 周一**不是**
CAL = ['2026-09-30', '2026-10-08', '2026-10-09', '2026-11-03', '2026-11-04',
       '2026-11-05', '2026-11-06']
# 假批次：10-08 是交易日但那天没出票；11-07/10-01/10-05 是"手工点出来"的批次
BATCH = ['2026-10-09', '2026-11-03', '2026-11-04', '2026-11-05',
         '2026-11-07', '2026-10-01', '2026-10-05']
DATES = sorted(set(CAL) | set(BATCH))

# ========== [1] 口径本身：日历 AND 批次 ==========
print('[1] 样本日 = 在交易日历里 且 当天有推荐批次')
r = SD.filter_sample_days(DATES, batch=BATCH, cal=CAL)
ck(r['counted'] == ['2026-10-09', '2026-11-03', '2026-11-04', '2026-11-05'],
   f"只数四个『是交易日且有批次』的日子（实得 {r['counted']}）")
rs = {s['date']: s['reason'] for s in r['skipped']}
ck(rs.get('2026-11-07', '').startswith('非交易日'), '周六的手工批次被排除，理由是"非交易日"')
ck(rs.get('2026-10-01', '').startswith('非交易日'), '10-01（周四·国庆）被排除——**第一版靠星期几就会把它算进去**')
ck(rs.get('2026-10-05', '').startswith('非交易日'), '10-05（周一·国庆）同上')
ck(rs.get('2026-10-08', '').startswith('空日'),
   '10-08 的理由是"交易日但 0 批次"，不是"非交易日"——两种原因必须分开写')
ck(r['calendarSource'] == 'injected', f"日历来源跟着读数出来（实得 {r['calendarSource']}）")
ck(SD.count(DATES, batch=BATCH, cal=CAL) == 4, 'count() 与 filter 一致')
ck(SD.count(DATES + DATES[::-1], batch=BATCH, cal=CAL) == 4, '重复/乱序输入不影响结果')

# ========== [2] 开关关掉 = 逐字节回到改造前 ==========
print('[2] SHADOW_SAMPLE_SKIP_EMPTY_DAYS=False 必须完全等于改造前')
old = cfg.SHADOW_SAMPLE_SKIP_EMPTY_DAYS
cfg.SHADOW_SAMPLE_SKIP_EMPTY_DAYS = False
try:
    r2 = SD.filter_sample_days(DATES, batch=BATCH, cal=CAL)
    ck(r2['counted'] == sorted(set(DATES)), f"不过滤：{len(set(DATES))} 天全算（实得 {len(r2['counted'])}）")
    ck(r2['skipped'] == [], '关掉时一个都不排除（跳过是开关决定的，不是偷偷少算）')
    ck(SD.count(DATES, batch=BATCH, cal=CAL) == len(set(DATES)), 'count() 退回 len(set(dates))')
finally:
    cfg.SHADOW_SAMPLE_SKIP_EMPTY_DAYS = old
ck(SD.filter_sample_days(DATES, batch=BATCH, cal=CAL)['counted'] == r['counted'],
   '开关恢复后口径原样回来（没有全局态残留）')

# ========== [3] 反证：两条判据各自真的在管事 ==========
print('[3] 反证 A：把 10-01/10-05 塞回日历（模拟"用星期几当交易日"）→ 节假日就混进来了')
r3 = SD.filter_sample_days(DATES, batch=BATCH, cal=sorted(set(CAL) | {'2026-10-01', '2026-10-05'}))
ck('2026-10-01' in r3['counted'] and '2026-10-05' in r3['counted'],
   '节假日一旦进日历就被算成样本 ⇒ 证明这条漏口由"日历"那一半把住，不是写死日期')

print('[4] 反证 B：日历不可用（空集合）→ 退回星期几近似，10-05 那个周一会混进来，且来源必须标出来')
_old_wd = SD.is_trading_weekday
try:
    r4 = SD.filter_sample_days(DATES, batch=BATCH, cal=[])      # 注入空日历＝近似路径
    ck('2026-10-05' in r4['counted'],
       '降级期间节假日的手工批次确实会被误算一天（这是明说的已知误差，不是假装没事）')
    ck('2026-11-07' not in r4['counted'], '但周六仍然被拦住（近似至少管住周末）')
    SD.is_trading_weekday = lambda d: True
    r4b = SD.filter_sample_days(['2026-11-07'], batch=['2026-11-07'], cal=[])
    ck('2026-11-07' in r4b['counted'], '再把周末判据也摘掉 → 周六混进来（证明它真在管事）')
finally:
    SD.is_trading_weekday = _old_wd
r5 = SD.filter_sample_days(DATES, batch=BATCH, cal=CAL)
ck('2026-10-05' not in r5['counted'], '还原后 10-05 又回到"非交易日"')

print('[5] 反证 C：批次集合里没有 11-06 而日历里有 → 原因必须是"空日"')
r6 = SD.filter_sample_days(['2026-11-06'], batch=['2026-11-03'], cal=CAL)
ck(r6['skipped'][0]['reason'].startswith('空日'), f"实得分类：{r6['skipped']}")

# ========== [6] 脏输入不许炸 ==========
print('[6] 脏输入：None / 空串 / 坏格式 / 带时间戳，不抛异常且行为可解释')
try:
    r7 = SD.filter_sample_days(['2026-11-03 20:35:00', '', None, 'abc', '2026-11-04'],
                               batch=['2026-11-03', '2026-11-04', 'abc'],
                               cal=['2026-11-03', '2026-11-04', 'abc'])
    ck(True, '没抛异常')
    ck('2026-11-03' in r7['counted'], '带时间戳的日期按前 10 位对齐（影子表里两种写法都有）')
    ck('abc' in r7['counted'], '解析不了的日期按"日历里查得到"处理（宁可少过滤也不静默丢样本）')
    ck(all(len(d) == 10 for d in r7['counted'] if d != 'abc'),
       '能解析的都标准化成 10 位；解析不了的（abc）原样留着并被算进样本（fail-open，见下一条）')
except Exception as e:
    ck(False, f'脏输入抛异常：{type(e).__name__}: {e}')

# ========== [7] batch_days 真去库里读（临时库）==========
print('[7] batch_days() 读的是 daily_recommendations，且换库走 use_db_path')
_ORIGIN_DB = dbmod.current_db_path()          # 收尾原样换回去，别把临时库路径留在进程里
tmp = _tmp_db('batch')
dbmod.use_db_path(tmp)
try:
    conn = sqlite3.connect(tmp)
    conn.execute("CREATE TABLE daily_recommendations (rec_date TEXT, symbol TEXT)")
    conn.execute("INSERT INTO daily_recommendations VALUES ('2026-11-03','sh600000')")
    conn.execute("INSERT INTO daily_recommendations VALUES ('2026-11-04','sh600000'),"
                 "('2026-11-04','sz000001')")
    conn.commit()
    conn.close()
    bd = SD.batch_days()
    ck(bd == ['2026-11-03', '2026-11-04'], f"批次日按天去重升序（实得 {bd}）")
    ck(SD.batch_days(since='2026-11-04') == ['2026-11-04'], 'since 过滤生效')
    rr = SD.filter_sample_days(['2026-11-03', '2026-11-06'], batch=None, cal=CAL)
    ck(rr['counted'] == ['2026-11-03'] and rr['skipped'][0]['date'] == '2026-11-06',
       '库里真没有那天的批次时，样本钟真的不数它（不是靠测试里喂的假集合）')
finally:
    dbmod.use_db_path(_ORIGIN_DB)
    if os.path.exists(tmp):
        os.remove(tmp)

print('[8] 反证：批次表不存在时 batch_days 返回空集合，filter 降级为**不过滤**而不是把天数清零')
tmp2 = _tmp_db('empty')
dbmod.use_db_path(tmp2)
try:
    ck(SD.batch_days() == [], '空库 → []（打印一条说明，不抛）')
    r8 = SD.filter_sample_days(DATES)                  # batch=None → 自己去空库读
    ck(r8['degraded'] is True, 'degraded=True（降级是看得见的）')
    ck(r8['counted'] == sorted(set(DATES)), '批次读不到时全算：宁可少排一个空日也不清零')
    ck(len(r8['counted']) > 0, '样本钟没被清空（shadow_eval 自测第一轮报红就是这个坑）')
finally:
    dbmod.use_db_path(_ORIGIN_DB)
    if os.path.exists(tmp2):
        os.remove(tmp2)

# ========== [9] 接线层 ==========
print('[9] 接线层 AST 断言（改回去不改这里就是假绿）')


def _src(rel):
    return io.open(os.path.join(os.path.dirname(os.path.dirname(HERE)), rel),
                   encoding='utf-8').read()


def _func_body(src, name):
    t = ast.parse(src)
    for n in ast.walk(t):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return ast.dump(n)
    raise AssertionError(f'找不到函数 {name}')


for rel, fn in [('server/services/pick/candidate_pool_shadow.py', 'evaluate'),
               ('server/services/pick/exit_seal_shadow.py', 'evaluate')]:
    d = _func_body(_src(rel), fn)
    ck('filter_sample_days' in d, f'{os.path.basename(rel)}:{fn} 走统一样本日口径')
    ck('sampleDays' in d and 'skipped' in d,
       f'{os.path.basename(rel)}:{fn} 把 counted 与 skipped 都带回返回值（跳过要看得见）')

zd = _func_body(_src('server/services/data/zhaban_shadow.py'), 'evaluate')
ck('filter_sample_days' not in zd, '炸板影子**不**套推荐批次口径（它的对照与推荐无关，见 clockRule）')
ck("COUNT(DISTINCT trade_date) FROM zhaban_shadow" not in zd,
   '炸板影子不再用"整张表的 DISTINCT 日期"当天数（那会把东财停调日也算进 20 天）')
ck('clockRule' in zd, '炸板影子的口径写进返回值，不靠人记')

# ========== [10] describe() 给日志用 ==========
print('[10] describe()：一行日志要能回答"今天天数怎么没涨"，还要能看出日历是不是降级来的')
txt = SD.describe(SD.filter_sample_days(DATES, batch=BATCH, cal=CAL))
for d in ('2026-11-07', '2026-10-01', '2026-10-05', '2026-10-08'):
    ck(d in txt, f'被排除的 {d} 打印出来了')
ck('日历来源' not in txt, '日历走主路/injected 时不硬加一句警告（kline_calendar 也算可信，不警告）')
fallback = SD.filter_sample_days(['2026-11-07'], batch=['2026-11-07'], cal=[])
ck('日历来源=weekday_fallback' in SD.describe(fallback),
   f'降级时日志必须把来源打在行尾，让人当场看出这读数不可全信：{SD.describe(fallback)}')
# print_eval 手里的是 evaluate() 的返回值（键是 sampleDays），describe 只认 counted 就会 KeyError
ck(SD.describe({'sampleDays': ['2026-11-03', '2026-11-04'], 'skipped': [],
                'calendarSource': 'trading_calendar'}) == '样本日 2 天（无排除）',
   'describe() 也吃 evaluate() 那种返回值（sampleDays 键）——真库副本实测撞过这个 KeyError')

# ========== [11] 真日历只读 smoke：主路必须是仓里那份官方日历 ==========
print('[11] 真日历只读：主路=官方公告表(trading_calendar)；日K 只作交叉校验与跨年降级')
import datetime as _dt
from services.data.trading_calendar import CLOSED_DATES, is_calendar_covered
from services.data.trading_calendar import is_trading_day as _official

_kd = SD.kline_days(refresh=True)
ck(len(_kd) > 600, f'日K 交叉日历读到 {len(_kd)} 个交易日（2024-01-02 ~ 2026-11-06）')

_only_kline = sorted(d for d in _kd if is_calendar_covered(d) and not _official(d))
ck(_only_kline == [], f'① 日K 有 bar 但官方表判非交易日的日子（实得 {len(_only_kline)} 个）')

_cur, _miss = _dt.date(2026, 1, 1), []
while _cur.isoformat() < _dt.date.today().isoformat():
    if _official(_cur) and _cur.isoformat() not in _kd:
        _miss.append(_cur.isoformat())
    _cur += _dt.timedelta(days=1)
ck(_miss == [], f'② 官方表判交易日但日K 没 bar 的日子（2026 至今，实得 {len(_miss)} 个）'
   ' —— 双向零分歧，主路才敢换成官方表')

_hol_wd = sorted(d for d in CLOSED_DATES if _dt.date.fromisoformat(d).weekday() < 5
                 and d < _dt.date.today().isoformat())
ck(len(_hol_wd) >= 15,
   f'2026 官方休市表里"周一至周五"的假期有 {len(_hol_wd)} 天 —— 第一版靠星期几会把这 {len(_hol_wd)} 天'
   '全算成样本，这就是那条判据被推翻的量化代价')
for _d in _hol_wd[:4]:
    ck(not SD.is_trading_day(_d), f'{_d}（周{_dt.date.fromisoformat(_d).weekday() + 1}）'
       '官方表判休市，本模块也说不是交易日')

ck(SD.is_trading_day('2026-11-06'), '11-06 是交易日（官方表与日K 双向都认），只是那天没出推荐 ⇒ 归"空日"')
ck(not SD.is_trading_day('2026-11-07'), '11-07 周六不是交易日 —— 官方日历能判"今天/明天"，'
   '日K 判不了，这正是主路必须换成官方日历的原因')
ck(SD.calendar_source('2026-11-05') == 'trading_calendar', '2026 年走主路 trading_calendar')
ck(SD.calendar_source('2024-06-18') == 'kline_calendar', '官方表没覆盖 2024 ⇒ 降级到 kline_calendar')
ck(SD.calendar_source('2027-01-05') == 'weekday_fallback',
   '跨年又没有日K 数据 ⇒ weekday_fallback（读数必须自己承认在猜）')

_r11 = SD.filter_sample_days(['2026-11-06'], batch=['2026-11-03'])
ck(_r11['counted'] == [] and _r11['skipped'][0]['reason'].startswith('空日'),
   f'日历说是交易日、批次里没有 ⇒ 归『空日』（实得 {_r11["skipped"]}）')
_r11b = SD.filter_sample_days(['2026-10-05'], batch=['2026-10-05'])
ck(_r11b['skipped'][0]['reason'].startswith('非交易日'),
   '假期手工批次 ⇒ 归『非交易日』，不跟"空日"混成一个数')
ck(_r11b['calendarSource'] == 'trading_calendar',
   f'真日历路径下来源标注跟着出来（实得 {_r11b["calendarSource"]}）')


print(f'\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败')
if FAIL:
    for m in FAIL:
        print('  失败：', m)
    sys.exit(1)
