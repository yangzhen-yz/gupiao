"""quote_fallback_selftest.py - 行情备源（腾讯 → 新浪）自测（全程假 HTTP，不联网）

为什么要这份自测：降级链路平时不触发，等到腾讯真的挂了才第一次跑，而那时页面正在转圈，
没人有心思逐字段核对。这里把"挂了以后会发生什么"提前演一遍：

  1. 新浪 → 腾讯 50 字段转换：价格/涨跌/五档/成交量单位（个股是股、sh000* 是指数手数）；
  2. 解析器零改动：convert 后的字符串走原 parse_tencent_batch_data / parse_realtime_fields，
     quoteSource 必须是 sina，且价格等关键字段不能错位；
  3. 切换策略：腾讯连续 3 次失败后 60 秒冷却（冷却期不再白等腾讯两轮超时）；
     冷却期内新浪也挂了要回头试腾讯，不能直接放弃；
  4. 源健康台账：degraded/down/恢复要能读出来，给 /api/data-sources/health 用。
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import services.data.stock as S
from services.data import source_health
from services.data.stock import (
    parse_tencent_batch_data, parse_realtime_fields, detect_quote_source,
    _valid_symbols, _sina_raw_to_tencent, _sina_volume_lots,
)

FAILS = []


def ck(cond, msg):
    print(('  ✓ ' if cond else '  ✗ ') + msg)
    if not cond:
        FAILS.append(msg)


def near(a, b, tol=0.011):
    return abs(float(a) - float(b)) <= tol


class _Resp:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


def _sina_line(symbol, fields):
    return f'var hq_str_{symbol}="' + ','.join(fields) + '";'


def _stock_fields():
    """浦发银行假数据：33 个字段，量按「股」给，验证 /100。"""
    f = [''] * 33
    f[0] = '浦发银行'
    f[1] = '10.00'          # 今开
    f[2] = '9.90'           # 昨收
    f[3] = '10.10'          # 现价
    f[4] = '10.20'          # 最高
    f[5] = '9.95'           # 最低
    f[6] = '10.10'; f[7] = '10.11'
    f[8] = '12345600'       # 成交量（股）→ 123456 手
    f[9] = '124567890'      # 成交额（元）→ 12457 万元
    f[10] = '123400'; f[11] = '10.09'   # 买一量(股)/价
    f[12] = '200000'; f[13] = '10.08'
    f[14] = '300000'; f[15] = '10.07'
    f[16] = '400000'; f[17] = '10.06'
    f[18] = '500000'; f[19] = '10.05'
    f[20] = '600000'; f[21] = '10.11'   # 卖一量(股)/价
    f[22] = '700000'; f[23] = '10.12'
    f[24] = '800000'; f[25] = '10.13'
    f[26] = '900000'; f[27] = '10.14'
    f[28] = '1000000'; f[29] = '10.15'
    f[30] = '2026-10-19'; f[31] = '15:00:00'; f[32] = '00'
    return f


def _tencent_line(symbol, source='1'):
    """构造腾讯 50 字段格式，用于主源路径与来源判定。"""
    f = [''] * 50
    f[0] = source
    f[1] = '测试股'
    f[2] = symbol[2:]
    fill = {
        3: '10.10', 4: '9.90', 5: '10.00', 6: '1000', 7: '500', 8: '500',
        9: '10.09', 10: '100', 11: '10.08', 12: '200',
        19: '10.11', 20: '300', 21: '10.12', 22: '100',
        31: '0.20', 32: '2.02', 33: '10.20', 34: '9.95',
        36: '1000', 37: '1010', 38: '0.5', 39: '8.5', 43: '2.53', 49: '1.1',
    }
    for i, v in fill.items():
        f[i] = v
    return f'v_{symbol}="' + '~'.join(f) + '";'


print('[1] 新浪单行 → 腾讯 50 字段：价格/涨跌/成交额/五档')
stock_raw = _sina_line('sh600000', _stock_fields())
conv = _sina_raw_to_tencent(stock_raw)
fields = conv[conv.index('"') + 1:conv.rindex('"')].split('~')
ck(len(fields) == 50, f'转换后正好 50 字段（实得 {len(fields)}）')
ck(fields[0] == '2', '第 0 位写 "2" 作为新浪兜底标记')
ck(fields[1] == '浦发银行' and fields[2] == '600000', '名称/代码没有错位')
ck(fields[3] == '10.10' and fields[4] == '9.90' and fields[5] == '10.00',
   f'现价/昨收/今开 = 10.10/9.90/10.00（实得 {fields[3]}/{fields[4]}/{fields[5]}）')
ck(fields[6] == '123456' and fields[36] == '123456',
   f'成交量 12,345,600 股 → 123,456 手（实得 {fields[6]}）')
ck(fields[37] == '12457', f'成交额 124,567,890 元 → 12,457 万元（实得 {fields[37]}）')
ck(fields[31] == '0.20' and fields[32] == '2.02',
   f'涨跌/涨幅 = 0.20/2.02%（实得 {fields[31]}/{fields[32]}）')
ck(fields[9] == '10.09' and fields[10] == '1234',
   f'买一价/量 = 10.09/1,234 手（实得 {fields[9]}/{fields[10]}）')
ck(fields[19] == '10.11' and fields[20] == '6000',
   f'卖一价/量 = 10.11/6,000 手（实得 {fields[19]}/{fields[20]}）')
ck(fields[33] == '10.20' and fields[34] == '9.95' and fields[43] == '2.53',
   f'高/低/振幅 = 10.20/9.95/2.53%（实得 {fields[33]}/{fields[34]}/{fields[43]}）')

print('\n[2] 原解析器零改动：quoteSource=sina，数值取自转换结果')
parsed = parse_tencent_batch_data(conv)
ck(len(parsed) == 1, 'parse_tencent_batch_data 能解析')
p = parsed[0]
ck(p['quoteSource'] == 'sina', f"quoteSource=sina（实得 {p['quoteSource']}）")
ck(p['name'] == '浦发银行' and p['symbol'] == 'sh600000', '名称/代码正确')
ck(near(p['priceRaw'], 10.10) and near(p['prevCloseRaw'], 9.90) and near(p['change'], 0.20),
   f"价格 10.10 / 昨收 9.90 / 涨跌 0.20（实得 {p['priceRaw']}/{p['prevCloseRaw']}/{p['change']}）")
ck(near(p['changePercent'], 2.02), f"涨幅 2.02%（实得 {p['changePercent']}）")
ck(p['volumeRaw'] == 123456 and near(p['turnover'], 12457, tol=1),
   f"量 123456 手 / 额 12457 万（实得 {p['volumeRaw']}/{p['turnover']}）")
ck(9.9 < p['avgPrice'] < 10.2, f"均价落在合理区间（实得 {p['avgPrice']}）")
ck(detect_quote_source(conv) == 'sina', '原始串来源判定为 sina')
rt = parse_realtime_fields(conv)
ck(rt and rt['quoteSource'] == 'sina' and near(rt['price'], 10.10) and rt['volume'] == 123456,
   f"parse_realtime_fields 同样读出价格/量/来源（{rt}）")

print('\n[3] 指数成交量单位：sh000* 已是手，sz399* 要 /100')
sh_idx = _sina_line('sh000001', ['上证指数', '3200', '3190', '3210', '3220', '3180',
                                 '3210', '3211', '3456789', '450000000000',
                                 '0', '0', '0', '0', '0', '0', '0', '0', '0', '0',
                                 '0', '0', '0', '0', '0', '0', '0', '0', '0', '0',
                                 '2026-10-19', '15:00:00', '00'])
sz_idx = _sina_line('sz399001', ['深证成指', '10500', '10400', '10550', '10600', '10380',
                                 '10550', '10551', '345678900', '520000000000',
                                 '0', '0', '0', '0', '0', '0', '0', '0', '0', '0',
                                 '0', '0', '0', '0', '0', '0', '0', '0', '0', '0',
                                 '2026-10-19', '15:00:00', '00'])
idx_conv = _sina_raw_to_tencent(sh_idx + '\n' + sz_idx)
idx_parsed = {x['symbol']: x for x in parse_tencent_batch_data(idx_conv)}
ck(idx_parsed['sh000001']['volumeRaw'] == 3456789,
   f"sh000001 量保持 3456789 手（实得 {idx_parsed['sh000001']['volumeRaw']}）")
ck(idx_parsed['sz399001']['volumeRaw'] == 3456789,
   f"sz399001 3.456789 亿股 → 3456789 手（实得 {idx_parsed['sz399001']['volumeRaw']}）")
ck(_sina_volume_lots('sh000001', 100) == 100 and _sina_volume_lots('sz000001', 100) == 1,
   '单位换算函数本身：sh000*=1，个股/sz399*=1/100')

print('\n[3b] 停牌票（新浪现价=0）不能算成 -100%')
halt_fields = ['停牌股', '0', '8.50', '0', '0', '0', '8.50', '8.50', '0', '0'] + ['0'] * 23
halt_conv = _sina_raw_to_tencent(_sina_line('sz000002', halt_fields))
halt = parse_tencent_batch_data(halt_conv)[0]
ck(near(halt['priceRaw'], 8.50) and near(halt['change'], 0.0)
   and near(halt['changePercent'], 0.0),
   f"停牌按昨收 8.50 平盘展示（实得 {halt['priceRaw']}/{halt['change']}/{halt['changePercent']}%）")
ck(halt['volumeRaw'] == 0, '停牌成交量保持 0')

print('\n[4] 来源判定：腾讯串不能被误判成新浪')
t_line = _tencent_line('sh600519')
ck(detect_quote_source(t_line) == 'tencent', '腾讯串 → tencent')
ck(parse_tencent_batch_data(t_line)[0]['quoteSource'] == 'tencent', '解析器同样 → tencent')
ck(detect_quote_source('') == 'tencent' and detect_quote_source('v_sh600000="";') == 'tencent',
   '空串/空包按主源处理（不误报兜底）')
ck(_valid_symbols('sh600000,foo,sz000001, SH600519 ') == ['sh600000', 'sz000001', 'sh600519'],
   '非法 symbol 被过滤，大小写/空格被规整')

print('\n[5] 腾讯连挂：重试 2 次 → 新浪兜底 → 3 次后进入冷却')
source_health.reset()
S._realtime_cache.clear()
S._tencent_down_until = 0.0
S._last_fallback_log = 0.0
calls = {'tencent': 0, 'sina': 0}


def tencent_fail(url, **kwargs):
    calls['tencent'] += 1
    raise ConnectionError('Remote end closed connection without response')


def sina_ok(url, **kwargs):
    calls['sina'] += 1
    return _Resp(stock_raw.encode('gbk'))


S._REALTIME_SESSION.get = tencent_fail
S._SINA_SESSION.get = sina_ok

for i in (1, 2, 3):
    S._realtime_cache.clear()
    data = asyncio.run(S.fetch_with_retry('sh600000'))
    ck(detect_quote_source(data) == 'sina', f'第 {i} 次：腾讯挂 → 返回新浪数据')
    ck(source_health.status('quote_tencent')['streak'] == i,
       f'第 {i} 次：腾讯连续失败计数 = {i}')
ck(calls['tencent'] == 6, f'每次都试满 2 次腾讯（共 {calls["tencent"]} 次）')
ck(calls['sina'] == 3, f'新浪成功 3 次（共 {calls["sina"]} 次）')
ck(source_health.status('quote_tencent')['state'] == 'down', '腾讯状态 = down')
ck(source_health.status('quote_sina')['state'] == 'ok', '新浪状态 = ok')
route = S.quote_route_status()
ck(route['preferred'] == 'sina' and route['tencentCooldownRemainSec'] > 0,
   f'路由切到新浪，冷却剩余 {route["tencentCooldownRemainSec"]}s')
ck(route['cooldownAfterFailures'] == 3 and route['cooldownSec'] == 60,
   '冷却参数：连续 3 次、60 秒')

print('\n[6] 5 秒缓存仍生效：冷却期内重复请求不再打网络')
before = dict(calls)
data2 = asyncio.run(S.fetch_with_retry('sh600000'))
ck(detect_quote_source(data2) == 'sina' and calls == before,
   f'缓存命中，网络调用次数不变（{before}）')

print('\n[7] 冷却期内跳过腾讯：不再白等两轮超时')
S._realtime_cache.clear()
data3 = asyncio.run(S.fetch_with_retry('sh600000'))
ck(detect_quote_source(data3) == 'sina', '仍返回新浪数据')
ck(calls['tencent'] == before['tencent'], f'腾讯调用次数未增加（{calls["tencent"]}）')
ck(calls['sina'] == before['sina'] + 1, f'只走新浪（{calls["sina"]}）')

print('\n[8] 冷却期内新浪也挂：回头试腾讯，腾讯恢复了就正常返回')
def sina_fail(url, **kwargs):
    calls['sina'] += 1
    raise ConnectionError('sina refused')


def tencent_ok(url, **kwargs):
    calls['tencent'] += 1
    return _Resp(t_line.encode('gbk'))


S._SINA_SESSION.get = sina_fail
S._REALTIME_SESSION.get = tencent_ok
S._realtime_cache.clear()
data4 = asyncio.run(S.fetch_with_retry('sh600000'))
ck(detect_quote_source(data4) == 'tencent', '新浪失败后腾讯补上 → 来源回到 tencent')
ck(source_health.status('quote_tencent')['state'] == 'ok',
   '腾讯成功一次即清零 streak（状态 ok）')
ck(S._tencent_down_until == 0.0, '成功后退出生效中的冷却窗口')

print('\n[9] 双源同时不可用：报错要能看出两边都试过')
S._REALTIME_SESSION.get = tencent_fail
S._SINA_SESSION.get = sina_fail
S._tencent_down_until = time.time() + 60
source_health.reset('quote_sina')
S._realtime_cache.clear()
err = None
try:
    asyncio.run(S.fetch_with_retry('sh600000'))
except Exception as e:
    err = e
ck(err is not None and '腾讯/新浪均不可用' in str(err),
   f'异常写明双源均失败（{err}）')
ck('新浪' in str(err) and '腾讯' in str(err),
   f'冷却期先试新浪时，两边的错都要保留（{err}）')
ck(source_health.status('quote_tencent')['state'] in ('degraded', 'down')
   and source_health.status('quote_sina')['state'] in ('degraded', 'down'),
   '健康台账里两边都记了失败')
ck('腾讯' in source_health.status('quote_tencent')['lastError']
   or 'Remote' in source_health.status('quote_tencent')['lastError'],
   f"腾讯错误被保留（{source_health.status('quote_tencent')['lastError']}）")
ck('sina' in source_health.status('quote_sina')['lastError'].lower()
   or 'refused' in source_health.status('quote_sina')['lastError'].lower(),
   f"新浪错误被保留（{source_health.status('quote_sina')['lastError']}）")

print('\n[10] 健康台账状态机：unknown → degraded → down → 恢复')
from services.data.source_health import status
source_health.reset('demo')
ck(status('demo')['state'] == 'unknown', '没调用过 = unknown')
source_health.fail('demo', ValueError('x'))
ck(status('demo')['state'] == 'degraded' and status('demo')['streak'] == 1,
   '1 次失败 = degraded')
source_health.fail('demo', ValueError('x'))
source_health.fail('demo', ValueError('x'))
ck(status('demo')['state'] == 'down', '3 次失败 = down')
source_health.ok('demo', 12.3)
st = status('demo')
ck(st['state'] == 'ok' and st['streak'] == 0 and st['latencyMs'] == 12.3,
   f'成功即恢复，保留延迟（{st}）')

print('\n' + ('全部通过' if not FAILS else f'失败 {len(FAILS)} 项：\n- ' + '\n- '.join(FAILS)))
sys.exit(1 if FAILS else 0)
