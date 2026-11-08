"""exit_rules_user4_selftest.py - 用户 4 条卖点（放量滞涨/尾盘偷袭/持仓跌停/逆势抗跌）影子逻辑自测

纯函数、无 IO、无 DB：直接调用 exit_rules.plan_exit 验证每条规则的命中/不命中/缺数据行为，
以及向后兼容（只传原必填入参不报错）。

运行：python server/scripts/exit_rules_user4_selftest.py
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from services.portfolio.exit_rules import plan_exit  # noqa: E402

TODAY = date(2026, 11, 8)
fails = []


def check(name, cond):
    print(('PASS ' if cond else 'FAIL ') + name)
    if not cond:
        fails.append(name)


def mech(plan, mid):
    return next((m for m in plan['mechanisms'] if m['id'] == mid), None)


# 1) 规则1 放量滞涨：昨涨停 + 今量2.5倍昨 + 涨幅3% → 命中
p = plan_exit(symbol='X', cost=10, price=10.3, buy_date='2026-11-06',
              yest_limit_up=True, vol_today=250000.0, vol_yest=100000.0,
              day_gain_pct=3.0, today=TODAY)
m = mech(p, 'u1')
check('u1 放量滞涨命中', bool(m and m['hit'] and m['available']))

# 2) 规则1 缺数据（无昨涨停标记）→ 不误触发
p = plan_exit(symbol='X', cost=10, price=10.3, buy_date='2026-11-06',
              vol_today=250000.0, vol_yest=100000.0, day_gain_pct=3.0, today=TODAY)
m = mech(p, 'u1')
check('u1 缺昨涨停标记→不可用不命中', bool(m and not m['available'] and not m['hit']))

# 3) 规则1 量够但涨幅≥5% → 不命中（非滞涨）
p = plan_exit(symbol='X', cost=10, price=10.8, buy_date='2026-11-06',
              yest_limit_up=True, vol_today=250000.0, vol_yest=100000.0,
              day_gain_pct=6.0, today=TODAY)
m = mech(p, 'u1')
check('u1 涨幅≥5%→不命中', bool(m and m['available'] and not m['hit']))

# 4) 规则2 尾盘偷袭：late_spike 且 14:30 → 命中
p = plan_exit(symbol='X', cost=10, price=10.3, buy_date='2026-11-06',
              late_spike=True, now_hm='14:30', day_gain_pct=4.0, today=TODAY)
m = mech(p, 'u2')
check('u2 尾盘拉升14:30→命中', bool(m and m['hit'] and m['available']))

# 5) 规则2 早于14:00 → 不命中
p = plan_exit(symbol='X', cost=10, price=10.3, buy_date='2026-11-06',
              late_spike=True, now_hm='13:30', today=TODAY)
m = mech(p, 'u2')
check('u2 13:30→不命中', bool(m and m['available'] and not m['hit']))

# 6) 规则3 持仓跌停（前日买入）→ 命中可卖
p = plan_exit(symbol='X', cost=10, price=9.0, buy_date='2026-11-06',
              limit_down=True, day_gain_pct=-10.0, today=TODAY)
m = mech(p, 'u3')
check('u3 前日买入跌停→命中', bool(m and m['hit'] and m['available']))

# 7) 规则3 当日买入跌停 → T+1 不可卖，不命中（不误杀）
p = plan_exit(symbol='X', cost=10, price=9.0, buy_date='2026-11-08',
              limit_down=True, day_gain_pct=-10.0, today=TODAY)
m = mech(p, 'u3')
check('u3 当日买入跌停→T+1 不命中', bool(m and m['available'] and not m['hit']))

# 8) 规则3 未跌停 → 不命中
p = plan_exit(symbol='X', cost=10, price=10.3, buy_date='2026-11-06',
              limit_down=False, day_gain_pct=3.0, today=TODAY)
m = mech(p, 'u3')
check('u3 未跌停→不命中', bool(m and m['available'] and not m['hit']))

# 9) 规则4 逆势抗跌：大盘跌 + 个股红盘3% → 命中（sector_down=None=板块未知，按大盘弱近似）
p = plan_exit(symbol='X', cost=10, price=10.3, buy_date='2026-11-06',
              market_down=True, day_gain_pct=3.0, sector_down=None, today=TODAY)
m = mech(p, 'u4')
check('u4 大盘跌+红盘小涨(板块未知近似)→命中', bool(m and m['hit'] and m['available']))

# 9b) 规则4 严格双跌：大盘跌+板块同跌+红盘3% → 命中
p = plan_exit(symbol='X', cost=10, price=10.3, buy_date='2026-11-06',
              market_down=True, day_gain_pct=3.0, sector_down=True, today=TODAY)
m = mech(p, 'u4')
check('u4 大盘跌+板块同跌→命中', bool(m and m['hit'] and m['available']))

# 9c) 规则4 大盘跌但板块明确未跌 → 不命中（与用户规则"都下跌"不符）
p = plan_exit(symbol='X', cost=10, price=10.3, buy_date='2026-11-06',
              market_down=True, day_gain_pct=3.0, sector_down=False, today=TODAY)
m = mech(p, 'u4')
check('u4 大盘跌+板块未跌→不命中', bool(m and m['available'] and not m['hit']))

# 10) 规则4 大盘涨 → 不命中
p = plan_exit(symbol='X', cost=10, price=10.3, buy_date='2026-11-06',
              market_down=False, day_gain_pct=3.0, today=TODAY)
m = mech(p, 'u4')
check('u4 大盘涨→不命中', bool(m and not m['available'] or (m['available'] and not m['hit'])))

# 11) 规则4 大盘跌但个股大跌 → 不命中（非"红盘小涨"）
p = plan_exit(symbol='X', cost=10, price=9.0, buy_date='2026-11-06',
              market_down=True, day_gain_pct=-3.0, today=TODAY)
m = mech(p, 'u4')
check('u4 大盘跌+个股大跌→不命中', bool(m and m['available'] and not m['hit']))

# 12) 向后兼容：只传原始必填入参，不报错且 u1..u4 均为不可用
p = plan_exit(symbol='X', cost=10, price=10.3, buy_date='2026-11-06', today=TODAY)
ok = all(not mech(p, mid)['hit'] for mid in ('u1', 'u2', 'u3', 'u4'))
check('向后兼容：仅必填入参不触发新规则', ok)

# 13) 影子模式：u4 命中 → action 建议为 exit，但 live=False（position.py 据此不越权接管）
p = plan_exit(symbol='X', cost=10, price=10.3, buy_date='2026-11-06',
              market_down=True, day_gain_pct=3.0, sector_down=None, today=TODAY)
check('影子模式：建议exit但live=False不接管',
      p.get('action') == 'exit' and p.get('hit') == 'u4' and p.get('live') is False)

print('\n' + ('ALL PASS' if not fails else f'FAILED: {fails}'))
sys.exit(1 if fails else 0)
