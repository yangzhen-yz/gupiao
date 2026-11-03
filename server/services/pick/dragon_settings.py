"""dragon_settings.py - 龙头通道 12 个配置项的范围校验（清单 T4.1）

职责
----
1. **12 项齐全**：清单 T4.1 的 12 个配置项逐项登记。清单名与 `app/config.py`
   变量名不同的五处（`TOP_GAIN_N` / `MAX_OPEN_GAIN` / `MIN_AMOUNT_5M` /
   `MAX_SPREAD` / `MAX_PUSH_PER_DAY`）在 `cfg` 字段里写死映射，**不新造第二套
   变量名**，避免改了一处忘了另一处。
2. **默认值不在这里**：唯一事实源是 `app/config.py`。本模块的 `spec_default`
   只用于"清单默认值 vs config 现值"对账；运行期回退一律用调用方传入的
   `defaults`（也就是 config 现值），不在模块里另存一份。
3. **非法值报错并给默认**：`validate()` 返回 `(values, errors)`，每条错误带
   `code` / `key` / `value` / `fallback`。调用方必须打印或抛错，不许静默吞掉；
   错误本身不做 I/O。
4. **热读边界写清楚**：`LIVE_KEYS=()` —— 12 项全部是"改 config.py 后重启
   生效"，没有一项走 `app_settings` 实时覆盖。要让某项实时生效，必须先在读取点
   改成 `value(key)` 现读，再把它挪进 `LIVE_KEYS`，两步缺一不可，只做一步就是
   "拨了开关没反应"。

硬约束
------
- 纯函数：不联网、不写库、不开文件、不 import `app.config`。现值与默认值都由
  调用方传入，单测可以喂非法值而不碰全局状态。
- `v2_live` / `ML_ENABLED=True` 不会被"自动修好"成能跑的状态：前者记
  `mode_not_ready`（T5.4 才允许），后者记 `not_implemented`（本机无
  sklearn/lightgbm 且 T1.5 未做），两者都回退到当前可运行值并留下错误码。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

SETTINGS_VERSION = 't4.1-2026-11-01'

# 12 项登记表。`key` 用清单里的名字；`cfg` 是 app/config.py 里的真实变量名。
# `spec_default` 只用于对账（运行期回退用调用方传入的 defaults）。
SETTINGS: List[Dict[str, Any]] = [
    {'key': 'INTRADAY_DRAGON_SCORE_MODE', 'cfg': 'INTRADAY_DRAGON_SCORE_MODE',
     'kind': 'enum', 'choices': ('legacy', 'v2_shadow', 'v2_live'),
     'ready': ('legacy', 'v2_shadow'), 'spec_default': 'v2_shadow', 'unit': '',
     'note': 'legacy=不算分；v2_shadow=只算分只记录；v2_live 要等 T5.4 小仓。'
             '2026-11-02 P1 影子开工，config 默认从 legacy 改成 v2_shadow，登记值跟着改'},
    {'key': 'INTRADAY_DRAGON_MIN_SCORE', 'cfg': 'INTRADAY_DRAGON_MIN_SCORE',
     'kind': 'float', 'min': 0.0, 'max': 100.0, 'spec_default': 0.0, 'unit': '分',
     'note': '0=不设下限；70 只做影子对照，不当线上线'},
    {'key': 'INTRADAY_DRAGON_TOP_GAIN_N', 'cfg': 'INTRADAY_DRAGON_B_TOP_N',
     'kind': 'int', 'min': 5, 'max': 50, 'spec_default': 20, 'unit': '只',
     'note': 'B 臂每轮取相对今开涨幅前 N'},
    {'key': 'INTRADAY_DRAGON_MAX_OPEN_GAIN', 'cfg': 'INTRADAY_DRAGON_B_GAIN_HI',
     'kind': 'float', 'min': 2.0, 'max': 10.0, 'spec_default': 8.5, 'unit': '%',
     'note': '相对今开涨幅上限；再高是追高不是起步'},
    {'key': 'INTRADAY_DRAGON_MAX_GAP', 'cfg': 'INTRADAY_DRAGON_GAP_MAX',
     'kind': 'float', 'min': 0.0, 'max': 10.0, 'spec_default': 7.0, 'unit': '%',
     'note': '高开上限：>7 不硬拒，只扣 15 分并留痕'},
    {'key': 'INTRADAY_DRAGON_MIN_AMOUNT_5M', 'cfg': 'INTRADAY_DRAGON_B_AMOUNT_5M',
     'kind': 'float', 'min': 500.0, 'max': 10000.0, 'spec_default': 3000.0, 'unit': '万元',
     'note': '最近 5 分钟成交额下限（流动性）'},
    {'key': 'INTRADAY_DRAGON_MAX_SPREAD', 'cfg': 'INTRADAY_DRAGON_B_SPREAD_MAX',
     'kind': 'float', 'min': 0.05, 'max': 1.0, 'spec_default': 0.3, 'unit': '%',
     'note': '买卖价差上限；影子分位只收紧不放松'},
    {'key': 'INTRADAY_DRAGON_ML_ENABLED', 'cfg': 'INTRADAY_DRAGON_ML_ENABLED',
     'kind': 'bool', 'spec_default': False, 'unit': '',
     'note': '本机无 sklearn/lightgbm 且 T1.5 未做；True 会被拒并回退 False'},
    {'key': 'INTRADAY_DRAGON_STOP_MODE', 'cfg': 'INTRADAY_DRAGON_STOP_MODE',
     'kind': 'enum', 'choices': ('none', 'fixed_2pct', 'vwap', 'day_low'),
     'ready': ('fixed_2pct', 'vwap', 'day_low'),
     'spec_default': 'fixed_2pct', 'unit': '',
     'note': '止损口径；none 在清单里合法但 T2.5 的 stop_plan 只实现三档，'
             '设 none 会被拒并回退（要支持需先在 dragon_push 加分支）'},
    {'key': 'INTRADAY_DRAGON_PULLBACK_WAIT_SEC', 'cfg': 'INTRADAY_DRAGON_PULLBACK_WAIT_SEC',
     'kind': 'int', 'min': 0, 'max': 600, 'spec_default': 180, 'unit': '秒',
     'note': '回踩限价最多等多久；与 dragon_backtest.PULLBACK_WAIT_SEC 同值'},
    {'key': 'INTRADAY_DRAGON_PUSH_MIN_INTERVAL_SEC', 'cfg': 'INTRADAY_DRAGON_PUSH_MIN_INTERVAL_SEC',
     'kind': 'int', 'min': 5, 'max': 300, 'spec_default': 20, 'unit': '秒',
     'note': '两条推送之间的最小间隔；P2 纸面时才接，当前没有读取点'},
    {'key': 'INTRADAY_DRAGON_MAX_PUSH_PER_DAY', 'cfg': 'INTRADAY_DRAGON_DAILY_LIMIT',
     'kind': 'int', 'min': 0, 'max': 10, 'spec_default': 4, 'unit': '只',
     'note': '龙头通道每天最多推几只；与主推送额度分账'},
]

# 已经做成"用的时候现读"、可以热改的项：本组 12 项一个都没有。
LIVE_KEYS: Tuple[str, ...] = ()

# 同票冷却 600s、同票 ≤2 次**不在**这 12 项里（清单 T4.1 第 5 条）：
# 要继续当常量用，参数化必须单独评审。这里只登记不扩键。
OUT_OF_SCOPE = (
    {'cfg': 'INTRADAY_DRAGON_COOLDOWN_SEC', 'value_note': '同票冷却 600s，保持常量'},
    {'cfg': 'INTRADAY_DRAGON_MAX_PER_STOCK', 'value_note': '同票 ≤2 次，保持常量'},
)

_ERROR_ORDER = ('bad_type', 'bad_enum', 'mode_not_ready', 'not_implemented', 'out_of_range')


def _is_bool(v: Any) -> bool:
    return isinstance(v, bool)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not _is_bool(v)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not _is_bool(v)


def _err(code: str, key: str, value: Any, fallback: Any, why: str) -> Dict[str, Any]:
    return {'code': code, 'key': key, 'value': value, 'fallback': fallback, 'why': why}


def _check_one(item: Dict[str, Any], value: Any,
               default: Any) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """单值校验：返回 (采用值, 错误或 None)。错误一律回退 default，不回退 spec_default。"""
    key, kind = item['key'], item['kind']
    if kind == 'bool':
        if not _is_bool(value):
            return default, _err('bad_type', key, value, default, '需要 True/False')
        if key == 'INTRADAY_DRAGON_ML_ENABLED' and value:
            return default, _err('not_implemented', key, value, default,
                                 '无 sklearn/lightgbm 且 T1.5 未做，打开等于空转')
        return value, None

    if kind == 'enum':
        if not isinstance(value, str):
            return default, _err('bad_type', key, value, default, '需要字符串')
        choices = item['choices']
        if value not in choices:
            return default, _err('bad_enum', key, value, default, f'只允许 {choices}')
        ready = item.get('ready') or choices
        if value not in ready:
            return default, _err('mode_not_ready', key, value, default,
                                 f'{value} 未开（{key} 当前只允许 {ready}）')
        return value, None

    if kind == 'int':
        if not _is_int(value):
            return default, _err('bad_type', key, value, default, '需要整数')
    elif kind == 'float':
        if not _is_num(value):
            return default, _err('bad_type', key, value, default, '需要数值')
    else:  # pragma: no cover - 登记表写错才会走到
        raise ValueError(f'未知 kind={kind!r}（key={key}）')

    lo, hi = item.get('min'), item.get('max')
    if lo is not None and value < lo:
        return default, _err('out_of_range', key, value, default, f'下限 {lo}')
    if hi is not None and value > hi:
        return default, _err('out_of_range', key, value, default, f'上限 {hi}')
    return value, None


def validate(values: Dict[str, Any],
             defaults: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], List[Dict]]:
    """逐项校验 12 个配置项。

    参数
    ----
    values   : 待校验的值，键用**清单名**（`SETTINGS[i]['key']`）。
    defaults : 非法值的回退来源，键同上；省略时用 `spec_default`（仅适合单测）。

    返回 `(采用值, 错误列表)`。错误列表为空表示全部合法；每条错误的 `fallback`
    就是最终采用值，调用方把它打印出来或直接抛错，都不能静默丢弃。
    """
    dv = defaults if defaults is not None else default_values()
    out: Dict[str, Any] = {}
    errors: List[Dict] = []
    for item in SETTINGS:
        key = item['key']
        default = dv.get(key, item['spec_default'])
        if key not in values:
            errors.append(_err('missing', key, None, default, '未提供，按默认值运行'))
            out[key] = default
            continue
        used, err = _check_one(item, values[key], default)
        out[key] = used
        if err:
            errors.append(err)
    errors.sort(key=lambda e: (_ERROR_ORDER.index(e['code'])
                               if e['code'] in _ERROR_ORDER else len(_ERROR_ORDER)))
    return out, errors


def default_values() -> Dict[str, Any]:
    """清单默认值（只用于对账/单测；运行期以 app/config.py 为准）。"""
    return {item['key']: item['spec_default'] for item in SETTINGS}


def spec_table() -> List[Dict[str, Any]]:
    """12 项元数据（对照文档表用）；不改动登记表本身。"""
    cols = ('key', 'cfg', 'kind', 'min', 'max', 'choices', 'spec_default', 'unit', 'note')
    return [{c: item.get(c) for c in cols} for item in SETTINGS]


def coverage() -> Dict[str, Any]:
    keys = [item['key'] for item in SETTINGS]
    return {'count': len(keys), 'unique': len(set(keys)) == len(keys),
            'names': keys, 'live': list(LIVE_KEYS),
            'restart_required': sorted(set(keys) - set(LIVE_KEYS))}


def check_defaults(values: Dict[str, Any]) -> List[Dict[str, Any]]:
    """对账：config 现值 vs 清单要求的默认值。返回差异列表（空=一致）。

    只做数值相等比较；int/float 允许 20 == 20.0（清单写 20、config 写 20.0 不算差异）。
    """
    diffs: List[Dict[str, Any]] = []
    for item in SETTINGS:
        key = item['key']
        if key not in values:
            diffs.append({'key': key, 'cfg': item['cfg'], 'have': None,
                          'want': item['spec_default'], 'why': 'config 缺这一项'})
            continue
        have, want = values[key], item['spec_default']
        same = (have == want) if not (isinstance(have, bool) or isinstance(want, bool)) \
            else (have is want)
        if not same:
            diffs.append({'key': key, 'cfg': item['cfg'], 'have': have,
                          'want': want, 'why': '与清单默认值不一致'})
    return diffs


def hot_reload_boundary() -> Dict[str, Any]:
    """热读边界：能实时改的为空，其余全部要重启。"""
    keys = [item['key'] for item in SETTINGS]
    return {'live': list(LIVE_KEYS),
            'restart_required': [k for k in keys if k not in LIVE_KEYS],
            'note': '改 app/config.py 后重启才生效；本组没有 app_settings 实时覆盖项'}


def cfg_names() -> Dict[str, str]:
    """清单名 → app/config.py 变量名（五处历史命名差异都在这里）。"""
    return {item['key']: item['cfg'] for item in SETTINGS}


def load_from_config(cfg_module: Any) -> Dict[str, Any]:
    """从已导入的 config 模块取现值（键用清单名）；缺失项不填默认，交给 validate 记 missing。

    这是唯一需要外部对象的一步，放在函数内，模块本身仍是纯函数。
    """
    out: Dict[str, Any] = {}
    for item in SETTINGS:
        if hasattr(cfg_module, item['cfg']):
            out[item['key']] = getattr(cfg_module, item['cfg'])
    return out
