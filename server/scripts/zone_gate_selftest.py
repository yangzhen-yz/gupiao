"""zone_gate_selftest.py - B 涨幅段闸门的自测（分区/模式/覆盖表/推送链接线）

必须证明的四件事：
  1. 分区边界与 2026-10-18 复盘口径逐格一致（弱势区/甜点区/死亡区/涨停区/负涨幅/缺失）；
  2. 带符号判定 —— 老代码 abs() 把 -6% 也算死亡区，这是误伤，新行为显式修正；
  3. 三种模式行为正确且切换零成本（off=不拦 / demote=罚分 / drop=过滤），
     覆盖表改动能被 cfg() 现读（app_settings.value 打桩验证，不写生产库）；
  4. 推送链 `_pass_filter` 真的挂了同一道闸（构造一只只有涨幅段犯规的票，
     关闸放行、开闸拦掉 —— 用同一只票的前后对比，把矩阵/黑名单的干扰隔离掉）。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.strategy.momentum_zone import (   # noqa: E402
    cfg as zone_cfg, evaluate as zone_evaluate, gate_stock as zone_gate,
    penalty_stock as zone_penalty, is_drop_mode as zone_is_drop)

FAILS = []


def ck(cond, msg):
    print(('  ✓ ' if cond else '  ✗ ') + msg)
    if not cond:
        FAILS.append(msg)


print('[1] 分区边界逐格（默认口径：弱势 <2 / 甜点 <4.5 / 死亡 <9.8 / 涨停 ≥9.8）')
c = zone_cfg()
cases = [(-6.0, 'down'), (-0.01, 'down'), (0.0, 'weak'), (1.999, 'weak'),
         (2.0, 'sweet'), (4.0, 'sweet'), (4.499, 'sweet'), (4.5, 'chase'),
         (9.799, 'chase'), (9.8, 'limit'), (10.5, 'limit'), (20.0, 'limit'),
         (None, 'unknown'), ('', 'unknown'), ('abc', 'unknown')]
bad = [(v, zone_evaluate(v, c)['key'], want) for v, want in cases
       if zone_evaluate(v, c)['key'] != want]
ck(not bad, f'共 {len(cases)} 格（不一致 {len(bad)} 格：{bad[:3]}）')
ck(zone_evaluate(6.0, c)['dead'] and zone_evaluate(1.0, c)['dead']
   and not zone_evaluate(3.0, c)['dead'] and not zone_evaluate(10.0, c)['dead']
   and not zone_evaluate(-3.0, c)['dead'] and not zone_evaluate(None, c)['dead'],
   'dead 只落在弱势区与死亡区：负涨幅/缺失/甜点/涨停都不算')

print('\n[2] 带符号判定：老代码 abs() 的误伤已修正')
ck(zone_evaluate(-6.0, c)['key'] == 'down' and not zone_evaluate(-6.0, c)['dead'],
   '-6% 不再当死亡区（老规则 abs(-6)=6 会罚分/过滤，-6% 与 +6% 不是一回事）')
ck(abs(-6.0) >= 4.5 and abs(-6.0) < 9.8,
   '（对照）老口径确实会把 -6% 揽进死亡区 —— 所以这条修正是有意义的')

print('\n[3] drop 模式：gate_stock 只拦死亡区')
ck(zone_gate({'changePercent': 6.0}, c)['key'] == 'chase', '死亡区票返回分区 dict')
ck(zone_gate({'changePercent': 1.0}, c)['key'] == 'weak', '弱势区票同样拦')
ck(zone_gate({'changePercent': 3.0}, c) is None, '甜点区放行')
ck(zone_gate({'changePercent': 10.0}, c) is None, '涨停区放行')
ck(zone_gate({'changePercent': -3.0}, c) is None, '负涨幅放行')
ck(zone_gate({'changePercent': None}, c) is None, '缺失放行（没有数据不做判决）')
ck(zone_gate({'changePercent': 6.0}, dict(c, mode='demote')) is None
   and zone_gate({'changePercent': 6.0}, dict(c, mode='off')) is None
   and zone_gate({'changePercent': 6.0}, dict(c, enabled=False)) is None,
   '非 drop 模式 gate_stock 一律 None（demote 是"罚分但可选"，推送链硬拦会口径不一致）')

print('\n[4] demote 模式：penalty_stock 只罚死亡区')
ck(zone_penalty({'changePercent': 6.0}, dict(c, mode='demote'))['penalty'] == 4,
   '死亡区罚 4 分')
ck(zone_penalty({'changePercent': 1.0}, dict(c, mode='demote'))['penalty'] == 4,
   '弱势区罚 4 分')
ck(zone_penalty({'changePercent': 3.0}, dict(c, mode='demote')) is None, '甜点区不罚')
ck(zone_penalty({'changePercent': 10.0}, dict(c, mode='demote')) is None, '涨停区不罚')
ck(zone_penalty({'changePercent': 6.0}, c) is None, 'drop 模式不罚分（整只拦掉，罚分无意义）')
ck(zone_penalty({'changePercent': 6.0}, dict(c, mode='demote', weak_penalty=0))['key'] == 'chase'
   and zone_penalty({'changePercent': 1.0}, dict(c, mode='demote', weak_penalty=0)) is None,
   '罚分设为 0 等于该区不罚（区间可独立关）')

print('\n[5] 覆盖表现读：app_settings 改完即刻生效（打桩，不写生产库）')
import app.config as cfgmod                                     # noqa: E402
from services.support import app_settings                       # noqa: E402
ck(zone_cfg()['mode'] == cfgmod.RECOMMEND_ZONE_GATE_MODE
   and zone_cfg()['weak_max'] == cfgmod.RECOMMEND_ZONE_WEAK_MAX
   and zone_cfg()['chase_max'] == cfgmod.RECOMMEND_ZONE_CHASE_MAX,
   '无覆盖时 cfg() 默认值 == app.config 常量')
_orig_value = app_settings.value
try:
    _OVERRIDES = {'zone_gate_mode': 'demote', 'zone_weak_max': 3.0,
                  'zone_chase_min': 3.0, 'zone_chase_max': 8.0}
    # 真实 value(key) 无覆盖时回退 config 默认值，打桩要照这个语义来，
    # 否则没被覆盖的键会拿到 None，把默认值测试变成假阴性。
    app_settings.value = lambda key: _OVERRIDES.get(key, _orig_value(key))
    ck(zone_cfg()['mode'] == 'demote' and zone_cfg()['weak_max'] == 3.0,
       '覆盖表改模式/区间 → cfg() 现读拿到新值')
    ck(zone_is_drop() is False, '模式改 demote 后 is_drop_mode 立刻变 False（一键回退不需要重启）')
    ck(zone_evaluate(3.5, zone_cfg())['key'] == 'chase',
       '区间也跟着变：3.5% 在新口径下已进死亡区')
finally:
    app_settings.value = _orig_value
ck(zone_is_drop() is True, '还原后回到 drop（默认）')

print('\n[6] SPEC 注册表：7 个开关齐全、cfg 名对得上、enum 有选项')
_zone_keys = ['zone_gate_enabled', 'zone_gate_mode', 'zone_weak_max', 'zone_weak_penalty',
              'zone_chase_min', 'zone_chase_max', 'zone_chase_penalty']
_spec = {s['key']: s for s in app_settings.SPEC}
for k in _zone_keys:
    ck(k in _spec, f'{k} 已注册')
ck(all(_spec[k].get('live') for k in _zone_keys), '全部 live=True（用的时候现读，改完即刻生效）')
ck(all(hasattr(cfgmod, _spec[k]['cfg']) for k in _zone_keys),
   'cfg 字段指向的常量都存在于 app.config')
_mode = _spec['zone_gate_mode']
ck(_mode['kind'] == 'enum' and {o['value'] for o in _mode['options']} == {'off', 'demote', 'drop'},
   f'模式是 enum 且三选项齐全：{[o["value"] for o in _mode["options"]]}')
ck(all(_spec[k]['group'] == '推荐涨幅段闸门' for k in _zone_keys), '同组（界面聚在一起）')

print('\n[7] 推送链 _pass_filter 真的挂了同一道闸（同一只票，只差涨幅段）')
from services.support import push as P                          # noqa: E402


def _push_stock(chg):
    """一只除涨幅段以外全部合规的票（评分/量比/预期涨幅都过线）。"""
    price = 10.0
    return {'symbol': 'sh900001', 'name': '闸门测试', 'price': price,
            'changePercent': chg, 'volumeRatio': 2.0, 'gain20': 5.0,
            'score': 82,
            'buySellPoints': {'sell': price * 1.05}}


_dead = _push_stock(6.0)
_sweet = _push_stock(3.0)
_orig_cfg = zone_cfg
import services.strategy.momentum_zone as _mzmod                    # noqa: E402
try:
    _mzmod.cfg = lambda: dict(_orig_cfg(), mode='off')          # 关闸对照
    ck(P._pass_filter(_dead) is True, '关闸（off）时同一只票放行 —— 证明前面几道都没拦它')
    _sweet_off = P._pass_filter(_sweet)                        # 甜点区的关闸基准
finally:
    _mzmod.cfg = _orig_cfg
ck(P._pass_filter(_dead) is False, '开闸（drop）时 +6% 死亡区票被拦')
ck(P._pass_filter(_sweet) == _sweet_off,
   '同一只票改成甜点区 +3% 后，开闸与关闸判定一致（闸门只拦死亡区，不误伤其他段）')
try:
    _mzmod.cfg = lambda: dict(_orig_cfg(), mode='demote')       # demote 不该硬拦
    ck(P._pass_filter(_dead) is True, 'demote 模式推送链不硬拦（与主链"罚分但可选"一致）')
finally:
    _mzmod.cfg = _orig_cfg

print('\n[8] 主链接线（静态）：模块被引用、被闸票有影子留痕')
_src = open(os.path.join(os.path.dirname(__file__), '..', 'services', 'pick',
                         'recommend.py'), encoding='utf-8').read()
ck('zone_gate(s, _zone_c)' in _src, 'recommend.py 4.8 节调用 zone_gate 过滤')
ck('zone_penalty(s, _zone_c)' in _src, 'recommend.py 3.5 节调用 zone_penalty 罚分')
ck('_log_shadow_selection(now, date_key, candidates, top3,\n'
   '                                                          trend_map, market_change,\n'
   '                                                          MIN_RECOMMEND_SCORE, zone_dropped)'
   in _src, '影子落库调用传了 zone_dropped')
ck("f'涨幅段闸门·{_z[\"label\"]}'" in _src.replace("'涨幅段闸门·{_z['label']}'",
                                                   "f'涨幅段闸门·{_z[\"label\"]}'")
   or "涨幅段闸门·" in _src, '被闸票的拦截原因写进 blocked（快照能看到）')
ck('涨幅段闸门' in _src, '被闸票并入影子 excluded（反事实留痕）')
_push_src = open(os.path.join(os.path.dirname(__file__), '..', 'services', 'support',
                              'push.py'), encoding='utf-8').read()
ck('gate_stock as _zone_gate' in _push_src and '_zone_gate(stock)' in _push_src,
   'push.py _pass_filter 调用 gate_stock')

print('\n' + ('全部通过' if not FAILS else f'失败 {len(FAILS)} 项：{FAILS}'))
sys.exit(1 if FAILS else 0)
