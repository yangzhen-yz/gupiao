"""strategy_snapshot 自测（临时库，绝不碰生产 gupiao.db）"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

_TMP = os.path.join(tempfile.gettempdir(), f'snap_test_{os.getpid()}.db')
if os.path.exists(_TMP):
    os.remove(_TMP)

import db.database as dbmod
dbmod.use_db_path(_TMP)      # 换库必须走这个函数：11-05 db 拆分后只赋值 dbmod.SQLITE_DB_PATH 是静默无效的，
#                                    INSERT 会直接写进生产库 server/gupiao.db   # 写测试就先把连接指到临时库

from services.pick import strategy_snapshot as ss

FAILS = []


def ck(cond, msg):
    print(('  ✓ ' if cond else '  ✗ ') + msg)
    if not cond:
        FAILS.append(msg)


def _mk(sym, name, total, chg=2.0):
    return {
        'symbol': sym, 'name': name, 'priceRaw': 10.5, 'price': 10.5,
        'changePercent': chg, 'volumeRatio': 1.8, 'turnoverRate': 3.2,
        'weibi': 20.0, 'outerRatio': 55.0, 'amplitude': 4.0,
        'primary_tag': '人工智能',
        'score': {
            'total': total, 'label': '偏多看涨', 'intraday_score': total - 2,
            'trend_score': total + 1,
            'base_scores': {'outer_ratio': 10, 'volume_ratio': 8, 'change_percent': 12},
            'factor_values': {'outer_ratio': 55.0, 'volume_ratio': 1.8,
                              'main_capital_inflow': 1.2,
                              'holder_structure': {'holder_num': 30000, 'ratio': -5.0},
                              'rps': 88.0, 'smart_money': None},
        },
    }


def _payload(scored, cand, top3, batch='09:45', **kw):
    p = {
        'date_key': '2026-10-19', 'batch': batch,
        'generated_at': '2026-10-19T09:45:03',
        'scored': scored, 'candidates': cand, 'top3': top3,
        'blocked': {'sz000002': ['炸板'], 'sh600000': ['未进精选(概念分散/名额3只)'],
                    'sh601111': ['亏损黑名单(近30天)']},
        'sources': {'sz000002': '热搜+海选', 'sh600000': '趋势'},
        'pool_info': {'sz000002': {'r20': 41.2, 'rank_pct': 3.1}},
        'labels': {'sh600001': {'timing': '竞价低吸',
                                'matrix': '可接力', 'matrix_why': '昨日封板+今日高开'}},
        'market_change': 0.42,
        'sent': {'cycle': '主升', 'cycleScore': 78, 'stale': False,
                 'indicators': {'limitUpCount': 62, 'limitDownCount': 3,
                                'zhabanCount': 18, 'promotionRate': 31.0},
                 'weakSignals': []},
        'gate': {'advisoryMinScore': 65},
        'live_min_score': 60,
        'scope_desc': '沪深主板·非ST·市值≥35亿·日均额>0.8亿·换手>1.5%·价>3元',
        'scope_size': 1094, 'matrix_version': '2026-10-16',
    }
    p.update(kw)
    return p


print('[1] 正常写入')
a, b, c, d, e = (_mk('sh600001', '甲', 82), _mk('sz000002', '乙', 71, 6.5),
                 _mk('sh600000', '丙', 66), _mk('sh601111', '丁', 63),
                 _mk('sz000003', '戊', 41))
n = ss.write_snapshot(_payload([a, b, c, d, e], [a, b, c], [a]))
ck(n == 5, f'5 只全落库（实得 {n}）')

conn = sqlite3.connect(_TMP)
rows = conn.execute('SELECT symbol, stage, rank, blocked_by, score, trend_score, source, '
                    'timing, matrix, r20, factors, inputs, concept, price '
                    'FROM strategy_snapshot ORDER BY rank').fetchall()
ck([r[0] for r in rows] == ['sh600001', 'sz000002', 'sh600000', 'sh601111', 'sz000003'],
   'rank 按分数从高到低')
ck(rows[0][1] == 'recommended' and rows[1][1] == 'candidate' and rows[4][1] == 'scored',
   f'stage 分层正确（{[r[1] for r in rows]}）')
ck(rows[1][3] == '炸板', f'blocked_by 记录被挡原因（{rows[1][3]}）')
ck(rows[4][3] == '低于门槛60', f'门槛之下自动补原因（{rows[4][3]}）')
ck(rows[0][6] is None and rows[1][6] == '热搜+海选', '入口标记按 src_map 落库')
ck(rows[0][7] == '竞价低吸' and rows[0][8] == '可接力', '时机/矩阵标签写入')
ck(abs(rows[1][9] - 41.2) < 1e-6, '海选 r20 一并落库')
ck(rows[0][10].startswith('{"outer_ratio"') and 'main_capital_inflow' in rows[0][11],
   '13 因子得分与输入值以 JSON 落库')

hdr = conn.execute('SELECT * FROM strategy_snapshot_batch').fetchall()
cols = [x[1] for x in conn.execute('PRAGMA table_info(strategy_snapshot_batch)').fetchall()]
hd = dict(zip(cols, hdr[0]))
ck(hd['cycle'] == '主升' and hd['limit_up'] == 62 and hd['market_change'] == 0.42,
   f'批次头情绪档/涨停数/大盘涨幅（{hd["cycle"]}/{hd["limit_up"]}）')
ck(hd['scope_size'] == 1094 and hd['gate_min_score'] == 65,
   '批次头存当日分母与影子门槛')
ck(hd['scored'] == 5 and hd['candidates'] == 3 and hd['recommended'] == 1, '批次头计数')
conn.close()

print('[2] 同批次重跑覆盖自己')
n2 = ss.write_snapshot(_payload([a, b, c, d, e], [a, b, c], [a]))
conn = sqlite3.connect(_TMP)
ck(n2 == 5 and conn.execute('SELECT COUNT(1) FROM strategy_snapshot').fetchone()[0] == 5,
   '行数不翻倍（手动"立即生成"不该越点越多）')
conn.execute("DELETE FROM strategy_snapshot WHERE 1=0")
conn.close()

print('[3] 换批次共存')
ss.write_snapshot(_payload([a, b], [a], [a], batch='13:30'))
conn = sqlite3.connect(_TMP)
ck(conn.execute('SELECT COUNT(DISTINCT batch) FROM strategy_snapshot').fetchone()[0] == 2,
   '一天 4 个批次各自留底')
conn.close()

print('[4] 空入选也要落库（否则以后胜率缺空仓日）')
n4 = ss.write_snapshot(_payload([a, b], [], [], batch='14:05'))
conn = sqlite3.connect(_TMP)
ck(n4 == 2 and conn.execute("SELECT COUNT(1) FROM strategy_snapshot WHERE batch='14:05' "
                            "AND stage='candidate'").fetchone()[0] == 0,
   '0 只精选的批次照样有快照')
conn.close()

print('[5] 行数上限：候选必留，长尾按分数截')
big = [_mk(f'sh6{100000 + i}', f'X{i}', 90 - i * 0.1) for i in range(1200)]
keep_cand = [_mk('sh600999', '必留', 20)]     # 分数很低但过了门槛（模拟）
p5 = _payload(big + keep_cand, keep_cand, [], batch='14:45')
n5 = ss.write_snapshot(p5)
ck(n5 == ss.snap_max_rows(),
   f'截到 snapshot_max_rows={ss.snap_max_rows()}（实得 {n5}）')
conn = sqlite3.connect(_TMP)
ck(conn.execute("SELECT stage FROM strategy_snapshot WHERE symbol='sh600999' "
                "AND batch='14:45'").fetchone()[0] == 'candidate',
   '排名在截断线之外的候选仍然被保留')
conn.close()

print('[6] 保留期裁剪')
conn = sqlite3.connect(_TMP)
conn.execute("INSERT INTO strategy_snapshot (snap_date,batch,symbol,stage,created_at) "
             "VALUES ('2000-01-01','09:45','sh600001','scored','x')")
conn.commit()
conn.close()
ss.write_snapshot(_payload([a], [a], [a], batch='15:00'))
conn = sqlite3.connect(_TMP)
ck(conn.execute("SELECT COUNT(1) FROM strategy_snapshot WHERE snap_date='2000-01-01'").fetchone()[0] == 0,
   f'超过 {ss.snap_retention_days()} 天的快照被清掉')
conn.close()

print('[7] 读取接口')
items = ss.load_snapshot('2026-10-19')
ck(len(items) > 0 and items[-1]['batch'] == '15:00', 'load_snapshot 默认取当天最后批次')
ck(isinstance(items[0]['factors'], dict) and 'outer_ratio' in items[0]['factors'],
   'factors/inputs 读出来已反序列化')
one = ss.load_snapshot('2026-10-19', batch='09:45', stage='recommended')
ck(len(one) == 1 and one[0]['symbol'] == 'sh600001', '按批次+阶段过滤')
ck(ss.load_snapshot('1999-01-01') == [], '没快照的日期返回空列表而不是报错')
bl = ss.list_batches()
ck(len(bl) == 5 and bl[0]['batch'] == '15:00', f'list_batches 倒序（{len(bl)} 个批次）')

print('[8] 覆盖表开关（复盘·口径与开关页走的就是这条路径）')
from services.support import app_settings
ck(app_settings.value('snapshot_enabled') is True, 'SPEC 已注册，默认值从 config 读到')
res = app_settings.set_value('snapshot_enabled', False, user='selftest')
ck(res.get('ok'), f"界面把开关关掉应成功（{res}）")
ck(ss.snap_enabled() is False, '关掉后 snap_enabled() 不重启进程就能读到 False')
ck(ss.write_snapshot(_payload([a], [a], [a], batch='15:30')) == 0, '关掉后 write_snapshot 直接返回 0')
conn = sqlite3.connect(_TMP)
ck(conn.execute("SELECT COUNT(1) FROM strategy_snapshot WHERE batch='15:30'").fetchone()[0] == 0,
   '关掉后确实一行都没写')
conn.close()
app_settings.set_value('snapshot_enabled', True, user='selftest')
ck(ss.snap_enabled() is True, '改回默认值应删掉覆盖行（等于 config 默认）')

print()
print('全部通过' if not FAILS else f'失败 {len(FAILS)} 项: {FAILS}')
os.remove(_TMP)
sys.exit(0 if not FAILS else 1)
