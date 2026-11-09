"""quota_guard_selftest.py — 每日预算治理（failover）回归自测（2026-11-09）

验证点（用真实模块，不另抄逻辑）：
  [1] 冷却期：首选主机未到 90% 预算 → pick_host 返回首选（ifzq.gtimg.cn）。
  [2] 切备源：首选 bump 到 ≥90% 预算 → pick_host 返回备源（web.ifzq.gtimg.cn）。
  [3] 计数真实：bump(n) 后 usage().count 精确反映累加（含多次累加）。
  [4] 降级不崩：首选与备源都到 90% → pick_host 仍返回首选（降级，不抛异常、不返回空）。
  [5] 未登记源：pick_host('不存在') 返回空串（调用方据此回退自身默认主机）。
  [6] 未治理主机：bump 对非 SOURCE_HOSTS 主机是 no-op（不污染计数、不影响其它主机）。
  [7] 持久化：bump 后状态文件存在且含当日日期（重启可恢复，不从头猛打）。
"""
import os
import sys
import tempfile

# 隔离：自测用临时状态文件，不碰生产 server/data/quota_guard_state.json
_tmp = os.path.join(tempfile.gettempdir(), f'quota_guard_selftest_{os.getpid()}.json')
if os.path.exists(_tmp):
    os.remove(_tmp)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # server/
import services.data.quota_guard as qg

qg._STATE_FILE = _tmp
qg.reset()  # 清空计数并落盘到临时文件

PASS = 0
FAIL = 0


def ck(cond, title, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  [PASS] {title}')
    else:
        FAIL += 1
        print(f'  [FAIL] {title}  {extra}')


print('[1] 冷却期首选返回')
ck(qg.pick_host('tencent_kline_qfq') == 'ifzq.gtimg.cn',
    '首选主机冷却时 pick_host 返回 ifzq.gtimg.cn', f'实得 {qg.pick_host("tencent_kline_qfq")!r}')

print('[2] 首选到 90% 切备源')
# ifzq 预算 8000，90% = 7200
qg.bump('ifzq.gtimg.cn', 7200)
ck(qg.usage('ifzq.gtimg.cn')['hot'] is True, 'ifzq 到 7200 标记 hot')
ck(qg.pick_host('tencent_kline_qfq') == 'web.ifzq.gtimg.cn',
    '首选 hot 时切到 web.ifzq.gtimg.cn 备源', f'实得 {qg.pick_host("tencent_kline_qfq")!r}')

print('[3] 计数真实（累加）')
qg.reset('ifzq.gtimg.cn')
qg.bump('ifzq.gtimg.cn', 5)
qg.bump('ifzq.gtimg.cn', 3)
ck(qg.usage('ifzq.gtimg.cn')['count'] == 8, 'bump(5)+bump(3) 计数=8', f'实得 {qg.usage("ifzq.gtimg.cn")["count"]}')

print('[4] 双热降级不崩')
qg.bump('ifzq.gtimg.cn', 7200)
qg.bump('web.ifzq.gtimg.cn', 7200)
try:
    got = qg.pick_host('tencent_kline_qfq')
    ck(got == 'ifzq.gtimg.cn', '双热时返回首选（降级而非空/异常）', f'实得 {got!r}')
except Exception as e:
    ck(False, '双热时 pick_host 不得抛异常', repr(e))

print('[5] 未登记源返回空串')
ck(qg.pick_host('no_such_source') == '', '未知逻辑源返回空串')

print('[6] 未治理主机 bump 是 no-op')
qg.reset()
before = dict(qg._counters)
qg.bump('example.com', 999)
ck(qg._counters == before, '非治理主机 bump 不改变任何计数', f'counters={qg._counters}')
# 也不影响已治理主机
qg.bump('ifzq.gtimg.cn', 10)
ck(qg.usage('ifzq.gtimg.cn')['count'] == 10, '治理主机计数独立正确')

print('[7] 持久化落盘')
qg.bump('ifzq.gtimg.cn', 1)
qg.flush()  # 强制落盘（api_usage 的定时 flush 也会调）
on_disk = False
try:
    import json
    if os.path.exists(_tmp):
        with open(_tmp, encoding='utf-8') as f:
            doc = json.load(f)
        on_disk = (doc.get('date') == qg._today() and 'ifzq.gtimg.cn' in doc.get('counters', {}))
except Exception:
    pass
ck(on_disk, '状态文件含当日日期与主机计数')

# 清理临时文件
try:
    os.remove(_tmp)
except Exception:
    pass

print(f'\n结果：{PASS} 通过 / {FAIL} 失败')
sys.exit(1 if FAIL else 0)
