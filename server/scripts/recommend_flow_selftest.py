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
            'weakSignals': [], 'rule': {'veto_roles': ('跟风',), 'allow_chase': True,
                                        'allow_relay': True},
            'minScore': 60}


def _fake_gate(_sent):
    return {'advisoryMinScore': 55, 'advisoryChase': True, 'vetoRoles': ('跟风',),
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
ck(len(recs) == 2, f'应推荐 2 只（5 只里 2 只炸板、1 只黑名单；实得 {len(recs)}）')

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

print()
print('全部通过' if not FAILS else f'失败 {len(FAILS)} 项: {FAILS}')
sys.exit(0 if not FAILS else 1)
