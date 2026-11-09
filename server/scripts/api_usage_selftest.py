"""api_usage_selftest.py - 接口用量统计与腾讯 qfq 域名分类回归自测（2026-11-09）

守住本次修复的不变量：
  [1] 接口用量分类：web.ifzq.gtimg.cn 与 ifzq.gtimg.cn 都归 tencent:kline（不再掉进 other 不被治理）
  [2] hot_chains qfq 回填不再用被限流的 web.ifzq 子域（改用已验证健康的 ifzq.gtimg.cn）
  [3] classify 对 localhost / 内网 / 非 http 返回 None（不入第三方计数）

背景：2026-11-09 用量页 other:web.ifzq.gtimg.cn 失败率持续 50%~72%，
而同机 ifzq.gtimg.cn 同日 3479 次 0 失败。根因是 web. 子域在生产机出口被腾讯对
并发突发限流；两者接口路径 appstock/app/fqkline/get 与返回结构完全一致，故切换域名。
本自测防止这两处回归（改回 web. 或分类表漏登记）。

运行：.venv/Scripts/python.exe scripts/api_usage_selftest.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.data import api_usage as au

PASS = []
FAIL = []


def ck(cond, msg, detail=''):
    if cond:
        PASS.append(msg)
    else:
        FAIL.append(f'{msg} | {detail}')


# ========== [1] 接口用量分类 ==========
print('[1] 接口用量分类：web.ifzq / ifzq 都归 tencent:kline')
_samples = [
    ('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519,day,,,40,qfq',
     'tencent:kline'),
    ('https://ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519,day,,,40,qfq',
     'tencent:kline'),
    ('https://qt.gtimg.cn/q=sh600519', 'tencent:quote'),
    ('https://push2.eastmoney.com/api/qt/clist/get?fid=f62', 'eastmoney:push2'),
]
for url, expect in _samples:
    got = au.classify(url)
    ck(got == expect, f'classify({url[:34]}…) == {expect}', f'实得 {got}')


# ========== [2] hot_chains qfq 回填域名 ==========
print('[2] hot_chains._QFQ_URL 不再命中被限流的 web.ifzq')
try:
    from services.ops import hot_chains as hc
    qfq = getattr(hc, '_QFQ_URL', '')
    ck('web.ifzq' not in qfq, 'hot_chains._QFQ_URL 不含 web.ifzq', f'实得 {qfq}')
    ck(qfq.startswith('https://ifzq.gtimg.cn/appstock/app/fqkline/get'),
        'hot_chains._QFQ_URL 走 ifzq.gtimg.cn', f'实得 {qfq}')
    # 用同模块拼接的请求串也须归 tencent:kline
    sample_req = f'{qfq}?param=sh600519,day,,,40,qfq'
    ck(au.classify(sample_req) == 'tencent:kline',
        'hot_chains 实际请求串归 tencent:kline', f'实得 {au.classify(sample_req)}')
except Exception as e:
    ck(False, 'hot_chains 导入或读取 _QFQ_URL 失败', repr(e))


# ========== [3] 内部/异常 URL 不计入第三方 ==========
print('[3] localhost / 内网 / 非 http 返回 None')
_internals = [
    'http://localhost:3003/api/x',
    'http://127.0.0.1:8080/y',
    'https://10.203.4.10:25000/z',
    'not-a-url',
]
for u in _internals:
    ck(au.classify(u) is None, f'classify({u[:28]}…) == None', f'实得 {au.classify(u)}')
# 外部非 http 方案：urlparse 成功解析出外部 host，按现有逻辑归 other（不入内网不计 None）
ck(au.classify('ftp://example.com/file') == 'other:example.com',
   'classify(ftp://example.com) == other:example.com（非内网、非 http 也计第三方）',
   f'实得 {au.classify("ftp://example.com/file")}')


# ========== 汇总 ==========
print('\n' + '=' * 60)
print(f'api_usage 自测：{len(PASS)} 通过 / {len(FAIL)} 失败')
for m in PASS:
    print(f'  ✓ {m}')
for m in FAIL:
    print(f'  ✗ {m}')
print('=' * 60)
sys.exit(1 if FAIL else 0)
