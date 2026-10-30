"""推荐链路集成自测：把 auto_generate_recommendations_task 整条跑一遍（临时库 + 假行情）

为什么值得跑：决策快照的写入点在 recommend.py 里，靠 py_compile 只能证明"没有语法错"，
证明不了 Monday 09:45 那一刻 `blocked`/`src_map`/`shadow_payload` 这些现场变量真的凑得齐。
所有网络入口都换成假数据，DB 指到临时文件，生产 gupiao.db 一个字节都不碰。
"""
import io
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

_TMP = os.path.join(tempfile.gettempdir(), f'rec_test_{os.getpid()}.db')
for suf in ('', '-wal', '-shm'):
    try:
        os.remove(_TMP + suf)
    except OSError:
        pass

import db.database as dbmod
dbmod.SQLITE_DB_PATH = _TMP

import app.config as cfg
dbmod.init_db()          # 临时库建好既有 schema（推荐落库要用）
from services.pick import recommend as R
from services.pick import candidate_pool

FAILS = []


def ck(cond, msg):
    print(('  ✓ ' if cond else '  ✗ ') + msg)
    if not cond:
        FAILS.append(msg)


CODES = ['sh600001', 'sz000002', 'sh600000', 'sh601111', 'sz300999', 'sh605555']


def _stock(sym, chg, price=10.5, vol=1.8):
    return {'symbol': sym, 'name': '测试' + sym[-2:], 'price': price, 'priceRaw': price,
            'change': round(price * chg / 100, 2), 'changePercent': chg,
            'high': price * 1.05, 'highRaw': price * 1.05,
            'prevClose': price / (1 + chg / 100), 'prevCloseRaw': price / (1 + chg / 100),
            'volumeRatio': vol, 'turnoverRate': 4.0, 'weibi': 30.0, 'outerRatio': 60.0,
            'amplitude': 5.0, 'pe': 20, 'amount': 2e8}


_WANTED = []


async def _fake_fetch(codes):
    _WANTED.clear()
    for _c in codes.split(','):
        if _c:
            _WANTED.append(_c)
    return 'raw'


def _fake_parse(_raw):
    chg = {'sh600001': 7.5, 'sz000002': 6.0, 'sh600000': 2.0,
           'sh601111': 1.0, 'sz300999': 3.0, 'sh605555': 4.5}
    return [_stock(sym, chg.get(sym, 1.0)) for sym in _WANTED]


async def _fake_idx(sym):
    return {'parsed': {'changePercent': 0.3}}


async def _fake_ff(codes):
    return {}


def _fake_trend(sym, price):        # asyncio.to_thread 跑的是同步函数，这里不能用 async
    return {'score': 72, 'details': {'checkPrice': price}}


async def _fake_sent():
    return {'cycle': '回暖', 'cycleScore': 61, 'stale': False, 'detail': '测试',
            'indicators': {'limitUpCount': 38, 'limitDownCount': 2, 'zhabanCount': 9,
                           'promotionRate': 28.0},
            'weakSignals': [], 'rule': {'allow_chase': True,
                                        'allow_relay': True},
            'minScore': 60}


def _fake_gate(_sent):
    # 2026-10-28 角色体系下线：vetoRoles 恒空，自测只为保持返回体形状
    return {'advisoryMinScore': 55, 'advisoryChase': True, 'vetoRoles': (),
            'allowChase': True, 'allowRelay': True}


# ---- 打桩 ----
R.load_hot_stock_buttons = lambda: [{'code': c} for c in CODES[:4]]
R._load_trend_score_map = lambda: {CODES[4]: {'score': 70, 'details': {}},
                                   CODES[5]: {'score': 68, 'details': {}}}
R.fetch_with_retry = _fake_fetch
R.parse_tencent_batch_data = _fake_parse
R.fetch_stock_data = _fake_idx
R.fetch_batch_fund_flow = _fake_ff
R.get_valuation_batch = lambda codes: {}
R.fetch_recent_placements = lambda codes, d: {}
R.load_stock_concept_tags = lambda: {CODES[0]: ['人工智能'], CODES[1]: ['人工智能']}
import services.strategy.trend as T
T.compute_trend_result = _fake_trend
import services.strategy.sentiment as S
S.get_sentiment = _fake_sent
S.cycle_gate = _fake_gate
import services.data.shareholder as SH
SH.get_holder_num_batch = lambda codes: {}
import services.data.fundamental as FD
FD.get_fundamental_batch = lambda codes: {}
import services.data.smartmoney as SM
SM.get_dragon_tiger_symbol_map = lambda: {}
import services.pick.recommend_review as RR
RR.get_recent_loser_symbols = lambda: {CODES[3]}     # 把 601111 拉黑，看快照有没有记下原因
candidate_pool_patch = candidate_pool
candidate_pool_patch.load_pool = lambda limit=None: [
    {'symbol': CODES[0], 'source': 'r20', 'r20': 41.2, 'rank_pct': 2.5},
    {'symbol': CODES[2], 'source': 'r20', 'r20': 33.0, 'rank_pct': 6.0}]
import services.data.universe as U
U.selectable_prefixed = lambda syms: [s for s in syms if not s.startswith('sz300')]
U.describe_selectable = lambda: '测试口径：主板非ST·市值≥35亿'
U.selectable_count = lambda: 1094

from datetime import datetime as _real_dt


class _DT:
    """把"今天"钉成 2026-10-19（周一）09:45：周末/非交易日任务本身就直接返回。"""

    @staticmethod
    def now():
        return _real_dt(2026, 10, 19, 9, 45, 3)

    @staticmethod
    def strptime(v, f):
        return _real_dt.strptime(v, f)


R.datetime = _DT

print('[跑] 完整推荐任务（假行情 6 只，含 1 只创业板应被范围过滤）')
import asyncio
res = asyncio.run(R.auto_generate_recommendations_task())
ck(res is not None, f'任务应正常返回（实得 {type(res).__name__}）')
recs = (res or {}).get('recommendations') or []
# 2026-10-18 B 涨幅段闸门上线后期望从 2 变 1：sh605555 当日 +4.5% 正落在死亡区
# 下界 [4.5, 9.8)，被 4.8 节整只拦掉（它的 blocked/影子留痕在下面断言）。
ck(len(recs) == 1, f'应推荐 1 只（5 只里 2 只炸板、1 只黑名单、1 只涨幅段闸门；实得 {len(recs)}）')

conn = sqlite3.connect(_TMP)
conn.row_factory = sqlite3.Row
rows = [dict(r) for r in conn.execute(
    'SELECT * FROM strategy_snapshot ORDER BY rank').fetchall()]
print('\n[快照]')
ck(len(rows) == 5, f'应落 5 行（第 6 只是创业板，被股票范围挡在扫描之外；实得 {len(rows)}）')
bysym = {r['symbol']: r for r in rows}
ck('sz300999' not in bysym, 'sz300999 被股票范围挡在扫描之外（不进快照是对的）')
ck(bysym['sh601111']['blocked_by'] == '亏损黑名单(近30天)',
   f'黑名单原因写进快照（实得 {bysym["sh601111"]["blocked_by"]}）')
ck(bysym['sh600001']['stage'] == 'scored' and bysym['sh600001']['blocked_by'] == '炸板',
   f'炸板那只停在 scored 且原因落库（{bysym["sh600001"]["stage"]}/{bysym["sh600001"]["blocked_by"]}）')
# B 涨幅段闸门（2026-10-18）：+4.5% 落在死亡区下界 → 整只拦下、原因落库、且进影子 excluded
ck(bysym['sh605555']['blocked_by'] == '涨幅段闸门·半山腰死亡区',
   f'死亡区票被涨幅段闸门拦下且原因落库（实得 {bysym["sh605555"]["blocked_by"]}）')
ck((bysym['sh605555']['shadow_note'] or '').startswith('影子挡:涨幅段闸门'),
   f'被闸票有影子挡留痕（反事实的入口，实得 {bysym["sh605555"]["shadow_note"]}）')
ck(bysym['sh600000']['blocked_by'] in (None, '', '未进精选(概念分散/名额3只)'),
   f'甜点区 +2.0% 不被误伤（实得 {bysym["sh600000"]["blocked_by"]}）')
ck(bysym['sh600001']['shadow_note'] is None,
   '影子只评估过门槛的 candidates，被炸板挡掉的没有影子判定（NULL=未评估）')
ck((bysym['sh600000']['shadow_note'] or '').startswith(('影子放行', '影子挡')),
   f'过门槛的票带影子判定（实得 {bysym["sh600000"]["shadow_note"]}）')
ck(bysym['sh600001']['source'] == '热搜+海选',
   f'入口叠加标记（实得 {bysym["sh600001"]["source"]}）')
ck(abs((bysym['sh600001']['r20'] or 0) - 41.2) < 1e-6, '海选 r20 落进快照')
ck(bysym['sh600000']['score'] is not None, '分数落库')
factors = bysym['sh600001']['factors']
ck(factors and factors.startswith('{'), f'13 因子 JSON 落库（{str(factors)[:40]}…）')
hdrs = [dict(r) for r in conn.execute('SELECT * FROM strategy_snapshot_batch').fetchall()]
ck(len(hdrs) == 1 and hdrs[0]['scope_size'] == 1094 and hdrs[0]['cycle'] == '回暖',
   f'批次头：分母 {hdrs[0]["scope_size"]} / 情绪 {hdrs[0]["cycle"]}')
ck(hdrs[0]['limit_up'] == 38, f'当日涨停数 {hdrs[0]["limit_up"]} 进批次头')
ck(hdrs[0]['recommended'] == len(recs), '批次头精选只数与推荐一致')

# 影子日志仍在（不能因为改了 return 值就把它写坏）
sh = conn.execute('SELECT payload FROM recommend_shadow_log').fetchall()
ck(len(sh) == 1, '影子日志照旧落库')
import json as _json
p = _json.loads(sh[0]['payload'])
ck('kept' in p, '影子 payload 新增 kept 名单（快照区分"放行/未评估"靠它）')
_zone_ex = [it for it in (p.get('excluded') or [])
            if '涨幅段闸门' in '；'.join(it.get('reasons') or [])]
ck(len(_zone_ex) == 1 and _zone_ex[0]['symbol'].endswith('605555'),
   f'影子 excluded 带涨幅段闸门挡掉的票（shadow_eval 的反事实测量对象，实得 {len(_zone_ex)} 只）')

# P2-2 模式标签：线上主链路要第一次真的算出 verdict 并落库
print('\n[模式标签]')
mode_rows = conn.execute(
    "SELECT rec_date, symbol, name, mode FROM daily_recommendations ORDER BY rec_date DESC LIMIT 6"
).fetchall()
ck(len(mode_rows) > 0, f'推荐记录能读到（{len(mode_rows)} 行）')
ck({r[3] for r in mode_rows} <= {'dixi', 'daban', 'chaodi', 'relay', 'chase', 'ban',
                                'reduce', 'other', None, ''},
   f'mode 取值都在枚举内（实得 {[r[3] for r in mode_rows]}）')
ck(any(r[3] for r in mode_rows), '至少一条推荐带上了模式标签（不是全空 = 没接上）')
from services.strategy.buy_rules import pick_mode, mode_label
ck(pick_mode('可低吸') == 'dixi' and pick_mode('可打板') == 'daban'
   and pick_mode('不接力') == 'relay' and pick_mode(None, {'limitClose': True}) == 'relay'
   and pick_mode('中性') == 'other',
   'verdict→mode 映射（含"昨日封板却没判定"要记成接力这条漏口告警）')
ck(mode_label('daban') == '打板' and mode_label('') == '其他', '模式中文名（老记录无标签归"其他"）')
from services.pick.recommend_review import get_review_summary
summ = get_review_summary(120)
ck('by_mode' in summ, f'复盘汇总透出 by_mode（available={summ.get("available")}）')

# P0-1 执行诚实化：区间与超价必须随 buy_timing JSON 一起落库（刷新后卡片还要能置灰）
print('\n[执行诚实化]')
conn9 = sqlite3.connect(_TMP)
conn9.row_factory = sqlite3.Row
bt_rows = [dict(r) for r in conn9.execute(
    "SELECT symbol, name, price, buy_timing FROM daily_recommendations "
    "WHERE rec_date='2026-10-19'").fetchall()]
conn9.close()
ck(len(bt_rows) > 0, f'今日推荐读得到（{len(bt_rows)} 行）')
import json as _j2
timings = [(_j2.loads(r['buy_timing'] or '{}'), r) for r in bt_rows]
ck(all(t.get('buyLow') and t.get('buyHigh') for t, _r in timings),
   f'每只都带买入区间（实得 {[(t.get("buyLow"), t.get("buyHigh")) for t, _r in timings]}）')
ck(all(t.get('limitPrice') for t, _r in timings), '每只都带追价上限 limitPrice')
ck(all(t.get('overPct') is not None for t, _r in timings),
   f'超价幅度落库（实得 {[t.get("overPct") for t, _r in timings]}%）')
ck(all(t.get('execHint') for t, _r in timings), '文案不为空（超价警告或实测口径说明）')
ck(any(t.get('overPct', 0) > 0 for t, _r in timings),
   '这批测试行情里至少一只是"现价已越过买入上限"——否则置灰逻辑永远没被测到')

# 读端：报告脚本应能吃到这批数据
conn.close()
print('\n[读] snapshot_report')
import importlib.util
_spec = importlib.util.spec_from_file_location(
    'snapshot_report',
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 'scripts', 'snapshot_report.py'))
_rep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_rep)
sys.argv = ['snapshot_report.py', '--limit', '5']      # 本进程 DB 路径已指向临时库
import contextlib
buf, ERR = io.StringIO(), ['']


def _run():
    try:
        _rep.main()
    except SystemExit as e:
        ERR[0] = f'exit {e.code}'
    except Exception as e:
        ERR[0] = f'{type(e).__name__}: {e} @ line {e.__traceback__.tb_lineno}'


with contextlib.redirect_stdout(buf):
    _run()
txt = buf.getvalue()
ck('已攒快照' in txt, f'report 读到快照\n{txt[:400]}\n[err] {ERR[0]}')
ck('阶段分布' in txt, 'report 输出阶段分布')
print(txt[:1500])

# [9] 一只都不推的那一批：空仓分支必须照样落快照
# （modes 之类的变量如果在 `if not top3` 之后才声明，闭包读到就是 NameError，
#   而快照自己包了 try/except，会静默不落库 —— 恰恰丢掉最需要的空仓日样本）
print('\n[空仓分支]')
RR.get_recent_loser_symbols = lambda: {s for s in [
    'sh600001', 'sz000002', 'sh600000', 'sh601111', 'sh605555']}
conn2 = sqlite3.connect(_TMP)
conn2.execute("DELETE FROM daily_recommendations WHERE rec_date='2026-10-19'")
conn2.execute("DELETE FROM strategy_snapshot WHERE snap_date='2026-10-19'")
conn2.execute("DELETE FROM strategy_snapshot_batch WHERE snap_date='2026-10-19'")
conn2.commit()
conn2.close()
import datetime as _dmod
R.datetime.now = staticmethod(lambda: _dmod.datetime(2026, 10, 19, 13, 30, 5))
res2 = asyncio.run(R.auto_generate_recommendations_task())
ck(res2 is None or not (res2 or {}).get('recommendations'),
   f'全被黑名单挡住时应无推荐（实得 {len((res2 or {}).get("recommendations") or [])} 只）')
conn3 = sqlite3.connect(_TMP)
try:
    b = conn3.execute("SELECT batch, recommended, candidates FROM strategy_snapshot_batch "
                      "WHERE snap_date='2026-10-19'").fetchall()
except sqlite3.OperationalError:
    b = []
n_row = conn3.execute("SELECT COUNT(1) FROM strategy_snapshot WHERE snap_date='2026-10-19'").fetchone()[0]
conn3.close()
ck(len(b) == 1 and b[0][0] == '13:30', f'空仓批次照样落了批次头（实得 {b}）')
ck(b and b[0][1] == 0 and b[0][2] == 0, '头里 recommended=0、candidates=0（这才是"空仓是正式状态"）')
ck(n_row > 0, f'扫描过的票仍然有票级快照（{n_row} 行）')

print()
print('全部通过' if not FAILS else f'失败 {len(FAILS)} 项: {FAILS}')
sys.exit(0 if not FAILS else 1)
