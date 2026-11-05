"""正式自测：批量评分的权重表 N+1 已消除（2026-11-04 代码层审查 P1）。

覆盖：
  ① 不传 weights 时，calc_stock_score_v2 内部确实会查一次权重表（证明问题真实存在）
  ② 传 weights 时，一次都不查（证明修复生效）
  ③ 两种调用对同一输入的评分结果完全一致（证明语义等价，没有改分数）
  ④ 60 次循环评分的调用次数从 60 降到 0，耗时量级下降
  ⑤ routes.py / recommend.py 两个循环调用点都显式传了 weights（防未来回归）
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'server'))

from db.database import load_strategy_factor_weights as real_load
from services.strategy import strategy as S

PASS = 0
FAIL = 0


def check(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  PASS  {name} {extra}')
    else:
        FAIL += 1
        print(f'  FAIL  {name} {extra}')


def main():
    weights = real_load()
    calls = {'n': 0}

    def counting_load():
        calls['n'] += 1
        return weights

    orig = S.load_strategy_factor_weights
    S.load_strategy_factor_weights = counting_load
    try:
        stock = {'symbol': 'sh600183', 'name': 'test', 'price': 10.0,
                 'changePercent': 1.2, 'volumeRatio': 1.1, 'turnoverRate': 2.0,
                 'amplitude': 3.0, 'pe': 20, 'weibi': 1, 'outerRatio': 1.2,
                 'outerPlateRaw': 100, 'innerPlateRaw': 80, 'volumeRaw': 1000}

        calls['n'] = 0
        r_default = S.calc_stock_score_v2(dict(stock))
        check('① 不传 weights 时内部查权重表 1 次', calls['n'] == 1, f"calls={calls['n']}")

        calls['n'] = 0
        r_reused = S.calc_stock_score_v2(dict(stock), weights=weights)
        check('② 传 weights 时零次查表', calls['n'] == 0, f"calls={calls['n']}")
        check('③ 两次评分结果完全一致', r_default == r_reused,
              f'total={r_default.get("total")} vs {r_reused.get("total")}')

        calls['n'] = 0
        t0 = time.perf_counter()
        for _ in range(60):
            S.calc_stock_score_v2(dict(stock), weights=weights)
        reused_ms = (time.perf_counter() - t0) * 1000
        check('④ 60 次循环零次查表', calls['n'] == 0, f"calls={calls['n']}")
        print(f'       60 次评分（复用权重）耗时 {reused_ms:.1f}ms')

        calls['n'] = 0
        for _ in range(60):
            S.calc_stock_score_v2(dict(stock))
        check('④ 对照：旧写法 60 次调用查表 60 次', calls['n'] == 60, f"calls={calls['n']}")

        # mock 不反映 14ms/次的真实 DB 开销，恢复真函数再量一次耗时才有意义
        S.load_strategy_factor_weights = orig
        t0 = time.perf_counter()
        for _ in range(60):
            S.calc_stock_score_v2(dict(stock))
        default_ms = (time.perf_counter() - t0) * 1000
        print(f'       60 次评分（真实 DB，旧写法）耗时 {default_ms:.1f}ms；'
              f'复用权重 {reused_ms:.1f}ms，快 {default_ms / max(reused_ms, 0.01):.0f}×')
        check('④ 真实 DB 下复用权重快 5 倍以上', default_ms > reused_ms * 5)
    finally:
        S.load_strategy_factor_weights = orig

    root = os.path.join(os.path.dirname(__file__), '..', 'server')
    for rel, needle in [('app/api/routes.py', 'weights=scan_weights'),
                        ('services/pick/recommend.py', 'weights=rec_weights')]:
        src = open(os.path.join(root, rel), encoding='utf-8').read()
        check(f'⑤ {rel} 已显式传权重', needle in src)

    print(f'\n结果: {PASS} 通过 / {FAIL} 失败')
    return 0 if FAIL == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
