"""rec_sim_low_cash_selftest.py - 回测撮合在"现金不够付最低佣金"时不许卡死的回归自测（2026-11-07）

守的不变量：**任何参数组合都不允许把事件循环占住**。这条是当天实拍换来的：
07:08 有人打 `/api/recommend-sim?months=9&capital=1&gap=0.5&lots=false`，`rec_sim.run()` 里
那句"现金不够就缩股"的 `while cost > cash and shares > 0` 再也不出来，而它跑在事件循环上，
于是从那一刻起整个后端不响应任何请求（连 `/api/health` 都 15 秒读超时），只能靠看护重启恢复。

为什么偏偏是 `lots=False`：整股模式每次 `-100` 股，几步就归零退出；**非整股是乘法缩股
`shares *= 0.98`**，而佣金有最低 5 元（`FEE_MIN`），现金 1 元时 `cost` 永远 ≥5 > `cash`，
`shares` 只能一路缩进浮点次正规数区（缩到 1e-322 量级还剩 200 万圈没跑完）——
它不是"慢"，是**不会自己结束**。修法两道收手：`shares` 不再单调下降（下溢卡住）就清零，
现金连最低佣金都不够就直接放弃这一笔。语义不变：本来也买不进去，只是原来要靠死循环去发现。

自测的判据不是"跑得快不快"，而是**同一份老循环单独跑必须不退出**（第 [4] 组），
否则绿灯只说明用例没测到那个圈。全程临时库 + 手工造的订单，不读真库、不打任何外部源。
"""
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

_TMP = os.path.join(tempfile.gettempdir(), f'recsim_guard_{os.getpid()}.db')
for _suf in ('', '-wal', '-shm'):
    try:
        os.remove(_TMP + _suf)
    except OSError:
        pass

import db.database as dbmod                      # noqa: E402
dbmod.use_db_path(_TMP)   # 换库必须走这个函数：直接赋值 SQLITE_DB_PATH 自 11-05 拆分起静默无效
dbmod.init_db()

from services.pick import rec_sim as R           # noqa: E402

PASS, FAIL = [], []


def ck(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(f"  [{'OK' if cond else 'XX'}] {msg}")


def _orders(price=10.0, n=1):
    """一天的买、次日卖，只此一笔——测的是撮合那一步，不是选股。"""
    return [{'r': {'symbol': 'sh600000', 'name': '测试票'}, 'entry_price': price,
             'exit_price': round(price * 1.1, 2), 'buy_date': '2026-01-05',
             'sell_date': '2026-01-06'} for _ in range(n)]


def _call(fn, secs=20):
    """在另一个线程里跑，超时就判"卡住"——测试本身绝不能被拖死。"""
    box = {}

    def _w():
        try:
            box['v'] = fn()
        except Exception as exc:            # 抛异常也算"回来了"，交给断言判
            box['e'] = exc
    t = threading.Thread(target=_w, daemon=True)
    t.start()
    t.join(secs)
    return (not t.is_alive()), box


# ========== [1] 出事那一组参数：必须自己结束 ==========
print('[1] capital=1（连最低佣金 5 元都不够）两种整股模式都得返回')
for lots, label in ((False, 'lots=False（今天卡死的就是这一组）'), (True, 'lots=True')):
    t0 = time.time()
    done, box = _call(lambda l=lots: R.run(_orders(), 1.0, whole_lots=l), 20)
    el = time.time() - t0
    res = box.get('v')
    ck(done and 'v' in box, f'{label}：{el:.2f}s 返回（不卡、不抛）')
    if done and isinstance(res, dict):
        ck(len(res['trades']) == 0 and len(res['unsold']) == 1,
           f'{label}：一笔都没成交、未成交原因如实记着（实得 trades={len(res["trades"])} '
           f'unsold={len(res["unsold"])}）')
        ck(isinstance(res['end'], (int, float)) and abs(res['end'] - 1.0) < 1e-6,
           f'{label}：期末还是本金 1 元，没被凭空扣钱（实得 {res["end"]}）')

# ========== [2] 边界：现金正好等于最低佣金，不许被守卫误杀 ==========
print('[2] capital=FEE_MIN（5.0）：能买就得照样买，守卫只该拦"根本买不进"')
done, box = _call(lambda: R.run(_orders(), R.FEE_MIN, whole_lots=False), 20)
res = box.get('v') if done else None
ck(done and isinstance(res, dict) and len(res['trades']) == 1,
   f'cash 恰好等于最低佣金时仍然成交 1 笔（实得 {len(res["trades"]) if res else "-"}）')

# ========== [3] 正常口径不许被改动影响 ==========
print('[3] 正常本金：两种整股模式各成交 1 笔，收益量级不变')
for lots in (True, False):
    done, box = _call(lambda l=lots: R.run(_orders(), 100000.0, whole_lots=l), 20)
    res = box.get('v') if done else None
    ok_ = bool(done and isinstance(res, dict) and len(res['trades']) == 1)
    tr = res['trades'][0] if ok_ else None
    ck(ok_ and tr and tr['ret'] > 5.0,
       f'whole_lots={lots}：+10% 的票扣完费仍该有 5% 以上（实得 '
       f'{tr["ret"]:.2f}%）' if ok_ and tr else f'whole_lots={lots}：应成交 1 笔')

# ========== [4] 反证：老写法单独跑必须不退出（否则本自测测不到东西） ==========
print('[4] 反证：把改造前那句 while 单独跑 200 万圈，必须还没结束')


def _old_style(cash=1.0, price=10.0, cap_iter=2_000_000):
    """逐字照抄改造前的循环（含最低佣金），只是加了圈数上限好让自测能结束。"""
    fb = lambda a: max(R.FEE_MIN, a * R.FEE_RATE) + a * R.TRANSFER
    shares = cash / price                    # whole_lots=False 的起始股数
    cost = shares * price + fb(shares * price)
    n = 0
    while cost > cash and shares > 0:
        shares = shares * 0.98
        cost = shares * price + fb(shares * price) if shares > 0 else 0
        n += 1
        if n >= cap_iter:
            return False, n, shares          # 还没出来 = 死循环
    return True, n, shares


finished, loops, shares = _old_style()
ck(finished is False,
   f'反证成立：老写法跑到 {loops:,} 圈仍未退出（shares 卡在 {shares:.1e}，'
   '次正规数区里 ×0.98 几乎不往下走）')
ck(_old_style(cash=1.0, price=10.0, cap_iter=100000)[0] is False,
   '连 10 万圈都跑不出来 —— 所以线上那次不是"慢"，是不会自己结束')
# 同一把尺子量新写法：单调下降 + 最低佣金两道收手
_new_shares, _n = 1.0 / 10.0, 0
while _new_shares > 0 and _n < 100000:
    _prev = _new_shares
    _new_shares *= 0.98
    if _new_shares >= _prev:
        break
    _n += 1
ck(_new_shares < 1.0 / 10.0, '乘法缩股本身是单调下降的（守卫只是不让它无限降下去）')

# ========== [5] 静态接线：两道收手都还在源码里 ==========
print('[5] 源码接线：删掉任一道守卫，这里必须报红')
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..',
                         'services', 'pick', 'rec_sim.py'), encoding='utf-8').read()
ck('if shares >= _prev or _fee_buy(0.0) > cash:' in _src,
   '单调性 + 最低佣金两道收手在同一行条件里')
ck(_src.count('shares = 0.0') >= 1, '收手时把股数清零（走"现金不足一手"那条已有分支，不新增口径）')
ck('while cost > cash and shares > 0:' in _src,
   '循环本体还在（本条改的是退出条件，不是把撮合逻辑删掉）')

print(f'\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败')
if FAIL:
    for m in FAIL:
        print('  失败：', m)
    sys.exit(1)
print('临时库：', _TMP, '（未读真库、未打外部源）')
