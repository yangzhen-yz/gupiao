"""anti_drop_overnight_selftest.py - 卖出侧两块（抗跌因子 / 过夜默认出口提醒）的回归自测
（2026-11-07，修⑥；用户"对第二类依次修吧"→"修吧"）

守的四条不变量：

1. **抗跌因子只是影子**：开关关掉时一个字段都不写、结果逐字节等于改造前；打开时也只填
   快照的 `antiDrop`，不改候选、不改分数、不改动作。
2. **指标口径不许把"算不出"读成"好"**：窗口里没有指数下跌日 → `downBeta`/`holdRate` 必须是
   `None`，不能是 0（0 会被下游读成"完全抗跌"）；本地日K落后的票一律不算并进 `stale` 计数
   （11-06 炸板影子那次就是拿停在 10-15 的旧序列算，涨停价整片算错）。
3. **过夜提醒默认关**：`OVERNIGHT_EXIT_ADVICE_ENABLED=False` 时 `run()` 一行库都不写；
   推送另有开关，关掉照样落库攒对照；发送失败必须撤回/不写去重标记，否则当天这条永久丢。
4. **接线不能漂**：scheduler 注册、快照写入点、cfg 的 `__all__` 三处静态核对。

全程临时库 + 假日K库 + 假推送，不写真库、不发企微。
"""
import asyncio
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

_TMP = os.path.join(tempfile.gettempdir(), f'antidrop_{os.getpid()}.db')
_KLINE = os.path.join(tempfile.gettempdir(), f'antidrop_kline_{os.getpid()}.db')
for _p in (_TMP, _KLINE):
    for _suf in ('', '-wal', '-shm'):
        try:
            os.remove(_p + _suf)
        except OSError:
            pass

import db.database as dbmod                      # noqa: E402
dbmod.use_db_path(_TMP)   # 换库必须走这个函数：直接赋值 SQLITE_DB_PATH 自 11-05 拆分起静默无效
dbmod.init_db()

import app.config as CFG                         # # noqa: E402
_REAL_KLINE = CFG.KLINE_HISTORY_DB_PATH          # 先记下真库位置，第 [6] 组要临时切回去
CFG.KLINE_HISTORY_DB_PATH = _KLINE               # 假日K库（模块内是 `from app.config import ...` 现取）

from services.pick import anti_drop_shadow as AD  # noqa: E402
from services.portfolio import overnight_exit as OE  # noqa: E402
from services.support.push_channel import _ensure_table as _ensure_alert_table  # noqa: E402

PASS, FAIL = [], []


def ck(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(f"  [{'OK' if cond else 'XX'}] {msg}")


DATES = [f'2026-11-{d:02d}' for d in range(9, 16)] + [f'2026-11-{d:02d}' for d in range(2, 9)]
DATES = sorted(DATES)                      # 14 根：2026-11-02 ~ 11-15（当日历用，不较真伪交易日）

# 真 index_window 的原身先存下来：下面第 [1]~[3] 组要把它换成受控序列，第 [6] 组要还原
_ORIG_INDEX_WINDOW = AD.index_window


def _mk_kline(rows_by_code):
    """建一张和 `history_kline.db` 同构的假日K表。"""
    c = sqlite3.connect(_KLINE)
    c.execute('DROP TABLE IF EXISTS stock_daily')
    c.execute('CREATE TABLE stock_daily (symbol TEXT, date TEXT, open, high, low, close, volume REAL, '
              'PRIMARY KEY (symbol, date))')
    for code, closes in rows_by_code.items():
        if isinstance(closes, dict):       # {日期: 收盘} —— 用来造"日K停在某一天"的样本
            items = list(closes.items())
        else:
            items = list(zip(DATES[-len(closes):], closes))
        for d, v in items:
            c.execute('INSERT INTO stock_daily (symbol, date, open, high, low, close, volume) '
                      'VALUES (?,?,?,?,?,?,?)', (code, d, v, v, v, v, 1000.0))
    c.commit()
    c.close()


def _patch_index(closes):
    """把指数窗口换成受控序列（避免自测依赖真库新鲜度）。"""
    AD.index_window = lambda days=None: (DATES[-len(closes):], [float(x) for x in closes])
    return DATES[-len(closes):]


# ========== [1] 指标数学 ==========
print('[1] 抗跌三条口径：造已知序列，逐个数字核')
idx = [100, 99, 97, 94, 92, 90]                    # 一路跌，最大回撤 10%
_patch_index(idx)
# 个股 A：从 100 只跌到 98（dd 2%）→ relMaxDd = 2% − 10% = −8pp（抗跌）
# 个股 B：与指数逐日同比例（dd 10%）→ relMaxDd ≈ 0、beta ≈ 1
_mk_kline({'600001': [100, 99.5, 99, 98.5, 98, 98],
           '600002': [100, 99, 97, 94, 92, 90]})
vals, meta = AD.anti_drop_map(['sh600001', 'sh600002'])
a, b = vals.get('sh600001') or {}, vals.get('sh600002') or {}
ck(abs(a.get('relMaxDdPp', 99) - (-8.0)) <= 0.5,
   f"跟跌少的票：相对最大回撤 = {a.get('relMaxDdPp')}pp（期望 ≈ −8，负=抗跌）")
ck(abs(b.get('relMaxDdPp', 99)) <= 0.5,
   f"与指数同路径的票：相对回撤 ≈ 0（实得 {b.get('relMaxDdPp')}）")
ck(b.get('downBeta') is not None and abs(b['downBeta'] - 1.0) <= 0.15,
   f"同比例跟跌 → 下跌日 beta ≈ 1（实得 {b.get('downBeta')}）")
ck(a.get('downBeta') is not None and a['downBeta'] < 0.6,
   f"抗跌票 beta 明显 <1（实得 {a.get('downBeta')}）")
ck(a.get('holdRate') in (0.0, 1.0, 0.5) or 0.0 <= a.get('holdRate', -1) <= 1.0,
   f"抗跌日占比在 0~1 内（实得 {a.get('holdRate')}）")
ck(a.get('bars') == 6 and a.get('bench') == AD.index_symbol(),
   f"用了 {a.get('bars')} 根对齐 bar，基准如实记 {a.get('bench')}")

# ========== [2] 没有下跌日时不许写成 0 ==========
print('[2] 窗口里指数一直涨 → downBeta/holdRate 必须是 None（0 会被读成"完全抗跌"）')
_patch_index([100, 101, 102, 103, 104, 105])
_mk_kline({'600003': [100, 100, 101, 101, 102, 102]})
v, _m = AD.anti_drop_map(['sh600003'])
vv = v.get('sh600003') or {}
ck(vv.get('downBeta') is None and vv.get('holdRate') is None and vv.get('downDays') == 0,
   f"下跌日 0 天 → beta/hold 为 None、downDays=0（实得 {vv.get('downBeta')}/{vv.get('holdRate')}）")
ck(vv.get('relMaxDdPp') is not None, '相对回撤照样能算（与有没有下跌日无关）')

# ========== [3] 日K新鲜度是门槛 ==========
print('[3] 本地日K停在旧日期的票：不算，进 stale 计数（11-06 炸板影子的教训）')
_patch_index(idx)                          # 窗口 = DATES 后 6 天（11-10~11-15），容忍落后 2 天 → 末日须 ≥11-13
_stale_dates = {d: v for d, v in zip(DATES[-6:-3], [100, 99, 97])}   # 在窗口内、但停在 11-12
ck(min(_stale_dates) >= DATES[-6] and max(_stale_dates) < DATES[-3],
   f"造的落后样本确实落在窗口内（{min(_stale_dates)} ~ {max(_stale_dates)}，门槛 {DATES[-3]}）")
_mk_kline({'600004': [100, 99, 97, 94, 92, 90],          # 最后一根 = 窗口末日
           '600005': _stale_dates})                       # 停在窗口中间 → 该判 stale
vals3, meta3 = AD.anti_drop_map(['sh600004', 'sh600005'])
ck('sh600004' in vals3 and 'sh600005' not in vals3,
   f"落后的 600005 没被算（算出的只有 {sorted(vals3)}）")
ck(meta3.get('stale') == 1 and meta3.get('computed') == 1,
   f"stale/computed 计数如实（实得 stale={meta3.get('stale')} computed={meta3.get('computed')} "
   f"notEnough={meta3.get('notEnough')}）")
ck(abs(meta3.get('coverage', 0) - 0.5) < 1e-6, f"覆盖率 = {meta3.get('coverage')}")

# ========== [4] 代码前缀规则 ==========
print('[4] 本地纯代码 ↔ 线上带前缀：6→sh、8/4→bj、其余→sz')
ck(AD._to_full_symbol('600519') == 'sh600519'
   and AD._to_full_symbol('000876') == 'sz000876'
   and AD._to_full_symbol('300750') == 'sz300750',
   '沪/深主板的补法与 dragon_entry 那条规则一致')
ck(AD._to_full_symbol('830799') == 'bj830799',
   '8 开头归北交所，不许混进 sz（混进去会让"本地缺数据"被读成"这票跌了"）')
ck(AD._to_full_symbol('SH600519') == 'sh600519', '已带前缀的原样归一')
# 输入形态必须两种都吃：策略快照传带前缀，`universe.selectable_symbols()` 返回**裸 6 位码**。
# 第一版这里用 `s.lower()` 直接当键，裸码全部落到"没数据"分支 —— 真库 1102 只实测 computed=0，
# 而假库自测照样全绿（因为假样本都是带前缀喂进去的）。这条就是那个漏口的反证。
_patch_index(idx)
_mk_kline({'600001': [100, 99.5, 99, 98.5, 98, 98]})
_pref, _ = AD.anti_drop_map(['sh600001'])
_bare, _m_bare = AD.anti_drop_map(['600001'])
ck(_bare.get('sh600001') == _pref.get('sh600001') and _m_bare.get('computed') == 1,
   f"裸码与带前缀等价（computed={_m_bare.get('computed')}，两种形态指标一致）")
ck(AD.anti_drop_map(['']) [0] == {}, '空代码不进结果')

# ========== [5] 开关关掉 = 现状 ==========
print('[5] ANTI_DROP_SHADOW_ENABLED=False：labs 返回空，接线处一个字段都不写')
_saved = AD.enabled
AD.enabled = lambda: False
labs_off, meta_off = asyncio.run(AD.anti_drop_labs([{'symbol': 'sh600001'}]))
ck(labs_off == {} and meta_off.get('skipped'),
   f"关掉后 labs={{}}、meta={meta_off}")
AD.enabled = _saved
_mk_kline({'600001': [100, 99.5, 99, 98.5, 98, 98],
           '600002': [100, 99, 97, 94, 92, 90]})   # 第 [3] 组重建过假日K表，这里把样本放回去
labs_on, meta_on = asyncio.run(AD.anti_drop_labs([{'symbol': 'sh600001'}, {'symbol': 'sh600002'}]))
ck(len(labs_on) >= 1 and 'antiDrop' in (labs_on.get('sh600001') or {}),
   f"打开后填 antiDrop（实得 {sorted(labs_on)}）")
ck(all(isinstance(v.get('antiDropPct'), float) for v in labs_on.values()),
   '分位是 float（截面分位，与 rev20 影子同一套路数）')

# ========== [6] 真库 smoke（只读复核）==========
print('[6] 换到真库只读跑一次：证明这个模块就是 `index_daily` 的读取方（#54 的一半）')
AD.index_window = _ORIG_INDEX_WINDOW       # 还原真读取（第 [1]~[3] 组换的是受控序列）
_REAL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'gupiao.db')
dbmod.use_db_path(_REAL)                   # 只读复核（本组只 SELECT，不写；跑完立刻换回临时库）
CFG.KLINE_HISTORY_DB_PATH = _REAL_KLINE    # 日K也要换回真库，否则这里问的是假日K表里不存在的代码
try:
    dd, cc = AD.index_window()
except Exception as e:
    dd, cc = [], []
    print(f'     真库读取异常：{e}')
ck(len(dd) >= 21, f'真库 index_daily 里 {AD.index_symbol()} 有 {len(dd)} 个收盘日'
   f'（{dd[0] if dd else "-"} → {dd[-1] if dd else "-"}）')
ck(len(cc) == len(dd), f'日期与收盘数一一对应（{len(dd)}/{len(cc)}）')
if len(dd) >= 21:
    _vals, _meta = AD.anti_drop_map(['sh600519', 'sh600000', 'sz000876'])
    ck(_meta.get('computed', 0) >= 2 and _meta.get('bench') == 'sh000001',
       f"真数据能出指标（computed={_meta.get('computed')} 覆盖率={_meta.get('coverage')} "
       f"stale={_meta.get('stale')}）")
    _sample = _vals.get('sh600519') or {}
    print(f"     真数据一例：贵州茅台 相对回撤 {_sample.get('relMaxDdPp')}pp / "
          f"下跌日 beta {_sample.get('downBeta')} / 抗跌日占比 {_sample.get('holdRate')} / "
          f"{_sample.get('bars')} 根 bar")
dbmod.use_db_path(_TMP)                    # 换回临时库，后面的组继续用假库
CFG.KLINE_HISTORY_DB_PATH = _KLINE

# ========== [7] 过夜提醒：默认关 ==========
print('[7] OVERNIGHT_EXIT_ADVICE_ENABLED=False：一行都不写、一条都不推')
_ensure_alert_table()
conn = sqlite3.connect(_TMP)
n0 = conn.execute("SELECT COUNT(*) FROM position_alert_log").fetchone()[0]
conn.close()
CFG.OVERNIGHT_EXIT_ADVICE_ENABLED = False
OE.OVERNIGHT_EXIT_ADVICE_ENABLED = False
r = asyncio.run(OE.run(user_id=1, today='2026-11-09', diag={'positions': []}))
ck('skipped' in r and r.get('advised') == 0, f"关掉时直接返回 skipped（实得 {r}）")
conn = sqlite3.connect(_TMP)
n1 = conn.execute("SELECT COUNT(*) FROM position_alert_log").fetchone()[0]
conn.close()
ck(n0 == n1, f'库里一条没写（前后 {n0} → {n1}）')

# ========== [8] 打开后：生成 + 落库 + 不推 ==========
print('[8] 打开总开关、推送开关关着：生成清单并落库，但一次推送都不发')
OE.OVERNIGHT_EXIT_ADVICE_ENABLED = True
OE.OVERNIGHT_EXIT_PUSH_ENABLED = False
CALLS = []
_orig_send = OE.send_to_user
OE.send_to_user = lambda uid, text: (CALLS.append(text), {'ok': True, 'errors': None})[1]


def _pos(sym, name, cost, price, action='watch', plan=None, sellable=True, buy='2026-11-03'):
    return {'symbol': sym, 'name': name, 'cost': cost, 'price': price, 'pnl_pct': None,
            'buy_date': buy, 'sellable': sellable, 'action': action,
            'action_label': {'stop_loss': '立即止损', 'watch': '持有观察'}.get(action, action),
            'stop_price': None, 'detail': '', 'exit_plan': plan or {'enabled': False}}


def _plan(hits, stop=None, ma20=None):
    ms = [{'id': 'fixed', 'label': '固定止损', 'hit': 'fixed' in hits, 'line': stop,
           'detail': f'跌破止损线 {stop}' if stop and 'fixed' in hits else ''},
          {'id': 'logic_ma', 'label': '逻辑止损(MA20)', 'hit': 'ma' in hits, 'line': ma20,
           'detail': f'收盘跌破 MA20 {ma20}' if ma20 and 'ma' in hits else ''},
          {'id': 'time', 'label': '时间止损', 'hit': 'time' in hits, 'line': None,
           'detail': '持有 6 天仍 <3%'}]
    return {'enabled': True, 'live': False, 'mechanisms': ms,
            'action': 'exit' if hits else 'hold'}


diag = {'positions': [
    _pos('sz000700', '模塑科技', 13.0, 12.0, plan=_plan(['fixed'], stop=12.35)),
    _pos('sh603667', '五洲新春', 55.0, 54.4, plan=_plan(['time'])),
    _pos('sh600000', '浦发银行', 7.0, 8.0, sellable=False, plan=_plan(['fixed'], stop=7.5)),
    _pos('sz000001', '平安银行', 10.0, 10.05, plan=_plan([], stop=10.0)),   # 距线 0.5% → 逼近
    _pos('sh601899', '紫金矿业', 20.0, 24.0, plan=_plan([]), ),              # 什么都没触发
]}
adv = OE.build_advice(diag, today='2026-11-09')
syms = sorted(i['symbol'] for i in adv)
ck(syms == ['sh603667', 'sz000001', 'sz000700'],
   f"三条都接住、T+1 与无信号的排除（实得 {syms}）")
ck(all(i.get('altCaliber') == 'next_open_sell' for i in adv),
   '每条都带第二条腿口径 next_open_sell（P0"次日开盘卖"基线，只记录不推送）')
near = next((i for i in adv if i['symbol'] == 'sz000001'), None)
ck(near is not None and not near['why'], '逼近止损线（差 0.5%）也算一条，但 why 留空不编原因')
r = asyncio.run(OE.run(user_id=1, today='2026-11-09', diag=diag))
ck(r.get('advised') == 3 and r.get('pushed') is False, f"落库不推送（实得 {r}）")
ck(not CALLS, f'推送一次都没发（实得 {len(CALLS)} 次）')
conn = sqlite3.connect(_TMP)
rows = conn.execute("SELECT symbol, kind, note FROM position_alert_log "
                    "WHERE alert_date='2026-11-09' AND kind='overnight_default'").fetchall()
conn.close()
ck(len(rows) == 1 and rows[0][0] == OE._AGG_KEY and '"pushed": false' in (rows[0][2] or ''),
   f"去重标记落了一条汇总行（实得 {rows}")

# ========== [9] 去重 / 失败 / 推送 ==========
print('[9] 同一天第二次不重复；推送开关打开才发；发送失败不留去重标记')
r2 = asyncio.run(OE.run(user_id=1, today='2026-11-09', diag=diag))
ck(r2.get('deduped') is True, f"第二次调用去重（实得 {r2}）")
OE.OVERNIGHT_EXIT_PUSH_ENABLED = True
CALLS.clear()
r3 = asyncio.run(OE.run(user_id=2, today='2026-11-09', diag=diag))
ck(r3.get('pushed') is True and len(CALLS) == 1 and '默认出口' in CALLS[0],
   f"账号 2 打开推送开关后发一条（实得 {r3}）")
conn = sqlite3.connect(_TMP)
n_users = conn.execute("SELECT COUNT(DISTINCT user_id) FROM position_alert_log "
                       "WHERE kind='overnight_default'").fetchone()[0]
conn.close()
ck(n_users == 2, f'去重按账号独立（涉及 {n_users} 个账号）')
OE.send_to_user = lambda uid, text: {'ok': False, 'errors': ['模拟发送失败']}
r4 = asyncio.run(OE.run(user_id=3, today='2026-11-09', diag=diag))
ck(r4.get('pushed') is False and 'errors' in r4, f"发送失败如实返回（实得 {r4}）")
conn = sqlite3.connect(_TMP)
has3 = conn.execute("SELECT 1 FROM position_alert_log WHERE kind='overnight_default' "
                    "AND user_id=3").fetchone()
conn.close()
ck(has3 is None, '发送失败时不写去重标记（否则当天这条提醒永久丢）')
OE.send_to_user = _orig_send

# ========== [10] 反证：没有动作就不许编动作 ==========
print('[10] 反证：exit_plan 整块不可用 + 动作是 watch → 一条都不能生成')
diag_bad = {'positions': [_pos('sz000700', '模塑科技', 13.0, 12.0,
                               plan={'enabled': False, 'why': 'EXIT_RULES 关着'})]}
ck(OE.build_advice(diag_bad, today='2026-11-10') == [],
   '默认出口不可用时不假装有动作（提醒里宁可空）')
diag_err = {'positions': [{'symbol': 'sz000700'}]}
try:
    got = OE.build_advice(diag_err, today='2026-11-10')
    ck(isinstance(got, list), '缺字段的诊断结果不抛异常（返回空清单）')
except Exception as e:
    ck(False, f'缺字段时抛了：{type(e).__name__}: {e}')

# ========== [11] 静态接线 ==========
print('[11] 接线三处核对：scheduler 注册 / 快照写入 / cfg 的 __all__')
_root = os.path.dirname(os.path.abspath(__file__))
_sched = open(os.path.join(_root, '..', 'core', 'scheduler.py'), encoding='utf-8').read()
ck('job_overnight_exit_advice' in _sched
   and _sched.count('schedule.every().day.at(PUSH_T1_PLAN_TIME).do(job_overnight_exit_advice)') == 1,
   'scheduler 里注册且只注册一次')
ck('def job_overnight_exit_advice' in _sched, 'job 本体在 scheduler 里')
_rec = open(os.path.join(_root, '..', 'services', 'pick', 'recommend.py'), encoding='utf-8').read()
ck('anti_drop_labs' in _rec, '策略快照里有抗跌写入点')
ck(_rec.count('from services.pick.anti_drop_shadow import anti_drop_labs') == 1,
   '抗跌写入点只有一处（多处会重复算）')
for name, where in (('OVERNIGHT_EXIT_ADVICE_ENABLED', 'cfg_recommend'),
                    ('OVERNIGHT_EXIT_PUSH_ENABLED', 'cfg_recommend'),
                    ('ANTI_DROP_SHADOW_ENABLED', 'cfg_strategy'),
                    ('ANTI_DROP_WINDOW_DAYS', 'cfg_strategy'),
                    ('ANTI_DROP_INDEX_SYMBOL', 'cfg_strategy'),
                    ('ANTI_DROP_MIN_LAST_LAG_DAYS', 'cfg_strategy')):
    ck(hasattr(CFG, name), f'{name} 经 config facade 可见（{where}）')
ck(getattr(CFG, 'OVERNIGHT_EXIT_ADVICE_ENABLED', None) is False
   and getattr(CFG, 'OVERNIGHT_EXIT_PUSH_ENABLED', None) is False,
   '两个过夜开关的出厂默认都是 False（上线不改变现状）')
ck(getattr(CFG, 'ANTI_DROP_MIN_LAST_LAG_DAYS', 0) >= 1,
   f"日K落后容忍度 = {CFG.ANTI_DROP_MIN_LAST_LAG_DAYS} 天（不能是 0：那样等于要求当天同步）")

print(f'\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败')
if FAIL:
    for m in FAIL:
        print('  失败：', m)
    sys.exit(1)
print('临时库：', _TMP, '| 假日K库：', _KLINE, '（真库全程只读，未发企微）')
