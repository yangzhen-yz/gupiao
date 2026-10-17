"""app_settings.py - 运行时可改的配置覆盖表（2026-10-17）

用户要求：**"目前好多配置都是在代码中直接写死的"**，希望把这些开关放进复盘的子页面里改。

为什么不能简单地"UI 写 config.py"
--------------------------------
`app/config.py` 里的值在各模块是 `from app.config import X` **导入时绑定**的，
进程一启动就把数值抄进了自己的命名空间；UI 改了文件，正在跑的代码**读到的还是旧值**。
所以这里做成一张 DB 覆盖表：`config.py` 是**默认值**，`app_settings` 是**运行时覆盖**，
真正需要热生效的那几项改成"用的时候现读"（`value(key)`），其余仍然标"改完要重启"。

诚实的边界（界面也会照这个显示）
--------------------------------
  · `live=True` 的项：本进程改完立刻生效（同一进程内缓存会被 `invalidate()` 打掉）。
    另起的脚本进程（backfill、分析脚本）在**它下次启动时**才读到新值，这是进程模型的必然，
    不是 bug。
  · `live=False` 的项：改动只写进表里，等进程重启才生效 —— 绝不假装"已生效"，
    否则用户拨了开关、行为没变，比不给开关更糟。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

import app.config as cfg
from db.database import get_db_conn

_DDL = '''CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT,
    updated_by TEXT
)'''

# 注册表：只有列在这里的项能被界面改。每一项都说清"是什么、范围多少、改了影响谁"。
#   kind: bool | int | float
#   cfg : app/config.py 里的默认值变量名（改了默认值不用动这里）
#   live: 是否已经做成"用的时候现读"（见模块文档）
#   impact: 影响预览函数名（在 _preview_* 里实现），None 就只显示说明
SPEC: List[Dict[str, Any]] = [
    {
        'key': 'universe_main_board_only', 'cfg': 'UNIVERSE_MAIN_BOARD_ONLY', 'kind': 'bool',
        'label': '只做沪深主板', 'group': '选股范围', 'live': True,
        'note': '关掉后创业板/科创板/北交所一起进入范围；注意它们的涨跌停是 20%/30%，'
                '封板类判定阈值口径会变',
        'impact': 'scope',
    },
    {
        'key': 'universe_exclude_st', 'cfg': 'UNIVERSE_EXCLUDE_ST', 'kind': 'bool',
        'label': '剔除 ST/*ST/退市整理', 'group': '选股范围', 'live': True,
        'note': 'ST 是 5% 板且带退市风险，历史上"5 日内大面率"最高的一类',
        'impact': 'scope',
    },
    {
        'key': 'market_cap_enabled', 'cfg': 'MARKET_CAP_FILTER_ENABLED', 'kind': 'bool',
        'label': '启用总市值下限', 'group': '选股范围', 'live': True,
        'note': '小盘流通盘小、易被少量资金拉出形态，这条是防被"做出来"的K线骗',
        'impact': 'scope',
    },
    {
        'key': 'min_market_cap_yi', 'cfg': 'MIN_TOTAL_MARKET_CAP', 'kind': 'float',
        'label': '总市值下限（亿元）', 'group': '选股范围', 'live': True,
        'min': 5, 'max': 500, 'step': 5, 'unit': '亿', 'scale': 1e8,
        'note': '市值表每周日刷一次；表里没有的票按放行处理',
        'impact': 'scope',
    },
    {
        'key': 'liquidity_enabled', 'cfg': 'LIQUIDITY_FILTER_ENABLED', 'kind': 'bool',
        'label': '启用流动性三条（总闸）', 'group': '选股范围', 'live': True,
        'note': '关掉总闸 = 下面三条一起不生效',
        'impact': 'scope',
    },
    {
        'key': 'liq_amount_enabled', 'cfg': 'LIQ_AMOUNT_ENABLED', 'kind': 'bool',
        'label': '近20日日均成交额这一条', 'group': '选股范围', 'live': True,
        'note': '成交额不够的票，形态再好也挂不进单、出不来',
        'impact': 'scope',
    },
    {
        'key': 'liq_min_avg_amount_wan', 'cfg': 'LIQ_MIN_AVG_AMOUNT', 'kind': 'float',
        'label': '日均成交额下限（万元）', 'group': '选股范围', 'live': True,
        'min': 1000, 'max': 200000, 'step': 1000, 'unit': '万', 'scale': 1e4,
        'note': '8000 万是 2026-10-16 定的口径',
        'impact': 'scope',
    },
    {
        'key': 'liq_turnover_enabled', 'cfg': 'LIQ_TURNOVER_ENABLED', 'kind': 'bool',
        'label': '近20日日均换手率这一条', 'group': '选股范围', 'live': True,
        'note': '六条里收口最紧的一条（1094 只就是它筛出来的）',
        'impact': 'scope',
    },
    {
        'key': 'liq_min_avg_turnover_pct', 'cfg': 'LIQ_MIN_AVG_TURNOVER_PCT', 'kind': 'float',
        'label': '日均换手率下限（%）', 'group': '选股范围', 'live': True,
        'min': 0.2, 'max': 20, 'step': 0.1, 'unit': '%',
        'note': '分母是流通股本（流通市值/最新价），不是流通市值',
        'impact': 'scope',
    },
    {
        'key': 'liq_price_enabled', 'cfg': 'LIQ_PRICE_ENABLED', 'kind': 'bool',
        'label': '股价下限这一条', 'group': '选股范围', 'live': True,
        'note': '3 元以下的票集中在被边缘化的老股/庄股，跳空与单日异动概率更高',
        'impact': 'scope',
    },
    {
        'key': 'liq_min_price', 'cfg': 'LIQ_MIN_PRICE', 'kind': 'float',
        'label': '最低股价（元）', 'group': '选股范围', 'live': True,
        'min': 1, 'max': 100, 'step': 0.5, 'unit': '元',
        'note': '看的是最后一根已收盘日K的收盘价（前复权最后一根=实盘价）',
        'impact': 'scope',
    },
    {
        'key': 'candidate_rps_top_pct', 'cfg': 'CANDIDATE_POOL_RPS_TOP', 'kind': 'float',
        'label': '海选要求的 20 日涨幅百分位（前百分之几）', 'group': '数据与门槛', 'live': True,
        'min': 50, 'max': 99, 'step': 1, 'unit': '%',
        'note': '80 = 只收 20 日涨幅排进范围前 20% 的票；调小=放宽入口，调大=只要最强的一批',
        'impact': None,
    },
    {
        'key': 'candidate_near_high_pct', 'cfg': 'CANDIDATE_POOL_NEAR_HIGH', 'kind': 'float',
        'label': '收盘距 60 日高点的最小位置（%）', 'group': '数据与门槛', 'live': True,
        'min': 70, 'max': 100, 'step': 1, 'unit': '%', 'scale': 100,
        'note': '92 = 收盘必须在 60 日最高价的 8% 以内（强势未破位）。这是欧奈尔模板里的'
                '"别买正在掉下来的票"，改太低会放进一批破位票',
        'impact': None,
    },
    {
        'key': 'kline_pool_min_coverage', 'cfg': 'KLINE_POOL_MIN_COVERAGE', 'kind': 'float',
        'label': '出池要求的日K覆盖度（%）', 'group': '数据与门槛', 'live': True,
        'min': 50, 'max': 100, 'step': 1, 'unit': '%', 'scale': 100,
        'note': '低于这个比例就当"数据不新"，退回热搜池并告警，避免拿半截的一天去海选。'
                '分母是选股范围（会随上面的开关一起变）',
        'impact': 'coverage',
    },
    {
        'key': 'candidate_pool_max', 'cfg': 'CANDIDATE_POOL_MAX', 'kind': 'int',
        'label': '海选池上限（只）', 'group': '数据与门槛', 'live': True,
        'min': 20, 'max': 400, 'step': 10, 'unit': '只',
        'note': '控制 9:45 那批的耗时，不是选股标准',
        'impact': None,
    },
    {
        'key': 'loser_criterion', 'cfg': 'RECOMMEND_LOSER_CRITERION', 'kind': 'enum',
        'label': '亏损黑名单判据', 'group': '推荐避让黑名单', 'live': True,
        'options': [{'value': 't1', 'label': '只看 T+1（老行为）'},
                    {'value': 't1_or_t3', 'label': 'T+1 或 T+3 任一亏超阈值'}],
        'note': '只看 T+1 会被一根噪声左右：金安国纪 T+1 −7.2% 但 T+3 已回到 −2.5%；'
                '而有的票 T+1 没到线、T+3 才跌出来（如 T+3 −10.2% 那只，老判据完全看不见）',
        'impact': None,
    },
    {
        'key': 'loser_graded', 'cfg': 'RECOMMEND_LOSER_GRADED', 'kind': 'bool',
        'label': '按亏损轻重分级关禁', 'group': '推荐避让黑名单', 'live': True,
        'note': '关掉=一视同仁（老行为：只要亏超阈值一律关满窗口天数）',
        'impact': None,
    },
    {
        'key': 'loser_loss_threshold', 'cfg': 'RECOMMEND_LOSER_LOSS_THRESHOLD', 'kind': 'float',
        'label': '进黑名单的亏损阈值（%）', 'group': '推荐避让黑名单', 'live': True,
        'min': 1, 'max': 20, 'step': 0.5, 'unit': '%',
        'note': 'T+1（或 T+3）收益低于 −此值 才算推荐失败',
        'impact': None,
    },
    {
        'key': 'loser_heavy_loss', 'cfg': 'RECOMMEND_LOSER_HEAVY_LOSS', 'kind': 'float',
        'label': '重亏线（%）', 'group': '推荐避让黑名单', 'live': True,
        'min': 2, 'max': 30, 'step': 0.5, 'unit': '%',
        'note': '亏过这条线才关满 HEAVY 天数；没过的只关 LIGHT 天数（需先打开上面的分级开关）',
        'impact': None,
    },
    {
        'key': 'loser_heavy_days', 'cfg': 'RECOMMEND_LOSER_HEAVY_DAYS', 'kind': 'int',
        'label': '重亏关禁天数', 'group': '推荐避让黑名单', 'live': True,
        'min': 5, 'max': 90, 'step': 5, 'unit': '天',
        'note': '从推荐日算起', 'impact': None,
    },
    {
        'key': 'loser_light_days', 'cfg': 'RECOMMEND_LOSER_LIGHT_DAYS', 'kind': 'int',
        'label': '轻亏关禁天数', 'group': '推荐避让黑名单', 'live': True,
        'min': 3, 'max': 60, 'step': 1, 'unit': '天',
        'note': '阈值~重亏线之间；设太短等于没有避让', 'impact': None,
    },
    {
        'key': 'loser_lookback_days', 'cfg': 'RECOMMEND_LOSER_LOOKBACK_DAYS', 'kind': 'int',
        'label': '黑名单回看窗口（天）', 'group': '推荐避让黑名单', 'live': True,
        'min': 7, 'max': 120, 'step': 1, 'unit': '天',
        'note': '不分级时这一项就是关禁天数', 'impact': None,
    },
    {
        'key': 'push_daily_limit', 'cfg': 'WECOM_PUSH_DAILY_LIMIT', 'kind': 'int',
        'label': '每天最多推送条数', 'group': '推送', 'live': True,
        'min': 1, 'max': 30, 'step': 1, 'unit': '条',
        'note': '用满之后只记日志不再推（避免盘中扫描把手机刷屏）',
        'impact': None,
    },
    {
        'key': 'push_apply_loser_blacklist', 'cfg': 'WECOM_PUSH_APPLY_LOSER_BLACKLIST', 'kind': 'bool',
        'label': '盘中推送也查亏损黑名单', 'group': '推送', 'live': True,
        'note': '2026-10-17 补的漏口：以前只有智能推荐查这张表，被拉黑的票还会从 60 秒扫描推送里回来',
        'impact': None,
    },
    {
        'key': 'push_pool_top_n', 'cfg': 'WECOM_PUSH_POOL_TOP_N', 'kind': 'int',
        'label': '纳入 60 秒连续扫描的海选只数', 'group': '推送', 'live': True,
        'min': 10, 'max': 250, 'step': 10, 'unit': '只',
        'note': '范围外存量已冻结不同步；这里太大去打腾讯批量接口会被限流',
        'impact': None,
    },
    {
        'key': 'snapshot_enabled', 'cfg': 'STRATEGY_SNAPSHOT_ENABLED', 'kind': 'bool',
        'label': '记录策略决策快照', 'group': '策略快照', 'live': True,
        'note': '每批次把"扫了谁/每只多少分/走到哪一步/为什么被挡/当天情绪档"落一张表。'
                '关掉后推荐照跑，但以后回测只能拿近似口径重建 —— 盘中的委比、外盘、量比'
                '日K库里没有，历史无法回填，只能现攒',
        'impact': 'snapshot',
    },
    {
        'key': 'snapshot_max_rows', 'cfg': 'STRATEGY_SNAPSHOT_MAX_ROWS', 'kind': 'int',
        'label': '每批次快照最多记多少只', 'group': '策略快照', 'live': True,
        'min': 100, 'max': 3000, 'step': 100, 'unit': '只',
        'note': '过门槛的候选与最终精选一定全留，剩下的按分数从高到低截。'
                '记满长尾才能做"分数带 × T+1 胜率"的分组统计',
        'impact': 'snapshot',
    },
    {
        'key': 'snapshot_retention_days', 'cfg': 'STRATEGY_SNAPSHOT_RETENTION_DAYS', 'kind': 'int',
        'label': '快照保留天数', 'group': '策略快照', 'live': True,
        'min': 0, 'max': 3650, 'step': 90, 'unit': '天',
        'note': '0=永久保留。每批 900 只 × 每天 4 批 ≈ 1 万行/天，两年约 500 万行',
        'impact': 'snapshot',
    },
]

_SPEC_INDEX = {s['key']: s for s in SPEC}
_OVERRIDES: Optional[Dict[str, str]] = None


def _ensure_table(conn) -> None:
    conn.execute(_DDL)


def _load_overrides() -> Dict[str, str]:
    global _OVERRIDES
    if _OVERRIDES is None:
        conn = get_db_conn()
        try:
            _ensure_table(conn)
            _OVERRIDES = {r[0]: r[1] for r in conn.execute(
                'SELECT key, value FROM app_settings').fetchall()}
        except sqlite3.Error as e:
            print(f'[配置覆盖] 读取失败(全部退回 config 默认值): {e}')
            _OVERRIDES = {}
        finally:
            conn.close()
    return _OVERRIDES


def invalidate() -> None:
    """让本进程下次重新读表（set 之后调用）。"""
    global _OVERRIDES
    _OVERRIDES = None


def owner_id() -> Optional[int]:
    """最早注册的账号 = 事实上的管理员（users 表没有角色列，只能这样定）。"""
    conn = get_db_conn()
    try:
        row = conn.execute('SELECT id FROM users ORDER BY id LIMIT 1').fetchone()
        return int(row[0]) if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def write_ids() -> tuple:
    ids = getattr(cfg, 'SETTINGS_WRITE_USER_IDS', ()) or ()
    if ids:
        return tuple(ids)
    o = owner_id()
    return (o,) if o is not None else ()


def can_write(user: Optional[Dict[str, Any]]) -> bool:
    """能不能改口径：白名单为空时按"最早注册的账号"算，避免写死 id 导致没人能改。"""
    if not user:
        return False
    return user.get('id') in write_ids()


def spec(key: str) -> Dict[str, Any]:
    s = _SPEC_INDEX.get(key)
    if not s:
        raise KeyError(f'不允许修改的配置项: {key}')
    return s


def default_value(key: str) -> Any:
    """config.py 里的默认值（数值项会按 scale 换算成"界面上的单位"）。"""
    s = spec(key)
    raw = getattr(cfg, s['cfg'])
    if s['kind'] == 'enum':
        return str(raw)
    if s.get('scale') == 100:          # 0.90 这种比例 → 显示成 90
        return round(float(raw) * 100, 2)
    if s.get('scale'):
        return round(float(raw) / s['scale'], 4)
    return raw


def value(key: str) -> Any:
    """**用的时候现读**：覆盖值优先，没有就用 config 默认值。返回值一律是**界面单位**
    （亿元、万元、百分数），这样 `default_value()` 和 `value()` 才是同一个尺度可比。

    ⚠️ 2026-10-17 差点踩进去的坑：落库走的是 config 单位（35e8 元、0.90 比例），
    读的时候必须除回 scale，否则界面上调"8000 万"、后端拿到的是"8000 元"，
    范围会整片空掉，而且看不出是单位问题。
    """
    s = spec(key)
    ov = _load_overrides().get(key)
    if ov is None:
        return default_value(key)
    if s['kind'] == 'bool':
        return ov == '1'
    if s['kind'] == 'enum':
        return str(ov)
    num = float(ov)
    if s.get('scale') == 100:
        num = num * 100.0
    elif s.get('scale'):
        num = num / s['scale']
    return int(num) if s['kind'] == 'int' else num


def coerce(key: str, incoming: Any):
    """校验 + 归一化。返回 (ok, 值或错误说明)。数值一律按界面单位存，落库时换算回 config 单位。"""
    s = spec(key)
    if s['kind'] == 'bool':
        if isinstance(incoming, bool):
            return True, incoming
        t = str(incoming).strip().lower()
        if t in ('1', 'true', 'on', 'yes', '开'):
            return True, True
        if t in ('0', 'false', 'off', 'no', '关'):
            return True, False
        return False, f'{s["label"]} 只能是开/关，收到的是「{incoming}」'
    if s['kind'] == 'enum':
        allowed = [o['value'] for o in s.get('options', [])]
        if str(incoming) not in allowed:
            return False, f'{s["label"]} 只能是 {"/".join(allowed)} 之一，收到的是「{incoming}」'
        return True, str(incoming)
    try:
        num = float(incoming)
    except (TypeError, ValueError):
        return False, f'{s["label"]} 需要数字，收到的是「{incoming}」'
    lo, hi = s.get('min'), s.get('max')
    if lo is not None and num < lo:
        return False, f'{s["label"]} 不能小于 {lo}{s.get("unit", "")}'
    if hi is not None and num > hi:
        return False, f'{s["label"]} 不能大于 {hi}{s.get("unit", "")}'
    if s['kind'] == 'int':
        if abs(num - round(num)) > 1e-9:
            return False, f'{s["label"]} 只能是整数'
        return True, int(num)
    return True, num


def _to_storage(key: str, val: Any) -> str:
    """界面单位 → config 单位（0.90 那种比例、35e8 那种大数都在这里还原）。"""
    s = spec(key)
    if s['kind'] == 'bool':
        return '1' if val else '0'
    if s['kind'] == 'enum':
        return str(val)
    if s.get('scale') == 100:
        return str(float(val) / 100.0)
    if s.get('scale'):
        return str(float(val) * s['scale'])
    return str(val)


def set_value(key: str, incoming: Any, user: str = '') -> Dict[str, Any]:
    if key not in _SPEC_INDEX:
        # 白名单之外一律拒绝：这个接口能改的是"选票口径"，不能变成任意配置写入
        return {'ok': False, 'error': f'不允许修改的配置项: {key}'}
    ok, val_or_err = coerce(key, incoming)
    if not ok:
        return {'ok': False, 'error': val_or_err}
    s = spec(key)
    conn = get_db_conn()
    try:
        _ensure_table(conn)
        # 改得跟默认值一样就删掉覆盖行：留着它，以后 config 默认值调整时会被旧覆盖"钉住"，
        # 而且排查的人看不出这个值到底是界面调过的还是默认的（2026-10-17 就留过一条 0.5 亿，
        # 直接把线上基线从 1094 只改成了 1168 只）。
        default = default_value(key)
        if s['kind'] == 'bool':
            same_as_default = bool(val_or_err) == bool(default)
        elif s['kind'] == 'enum':
            same_as_default = str(val_or_err) == str(default)
        else:
            same_as_default = abs(float(val_or_err) - float(default)) < 1e-9
        if same_as_default:
            conn.execute('DELETE FROM app_settings WHERE key=?', (key,))
        else:
            conn.execute('INSERT OR REPLACE INTO app_settings (key, value, updated_at, updated_by) '
                         'VALUES (?,?,?,?)',
                         (key, _to_storage(key, val_or_err),
                          datetime.now().strftime('%Y-%m-%d %H:%M:%S'), user or 'unknown'))
        conn.commit()
    except sqlite3.Error as e:
        return {'ok': False, 'error': f'写入失败: {e}'}
    finally:
        conn.close()
    invalidate()
    # 范围相关的进程内缓存必须一起打掉，否则界面改了、算出来的还是旧范围
    for mod in ('services.data.universe', 'services.data.liquidity', 'services.data.market_cap'):
        try:
            import importlib
            m = importlib.import_module(mod)
            for fn in ('invalidate_cache', 'clear_cache'):
                if hasattr(m, fn):
                    getattr(m, fn)()
        except Exception as e:
            print(f'[配置覆盖] {mod} 缓存清理跳过: {type(e).__name__}: {e}')
    return {'ok': True, 'key': key, 'value': val_or_err, 'live': spec(key)['live']}


def reset_value(key: str, user: str = '') -> Dict[str, Any]:
    """删掉覆盖，回到 config.py 的默认值（界面上的"恢复默认"）。"""
    if key not in _SPEC_INDEX:
        return {'ok': False, 'error': f'不允许修改的配置项: {key}'}
    conn = get_db_conn()
    try:
        _ensure_table(conn)
        conn.execute('DELETE FROM app_settings WHERE key=?', (key,))
        conn.commit()
    except sqlite3.Error as e:
        return {'ok': False, 'error': f'清除失败: {e}'}
    finally:
        conn.close()
    invalidate()
    for mod in ('services.data.universe', 'services.data.liquidity', 'services.data.market_cap'):
        try:
            import importlib
            m = importlib.import_module(mod)
            if hasattr(m, 'invalidate_cache'):
                m.invalidate_cache()
        except Exception:
            pass
    return {'ok': True, 'key': key, 'value': default_value(key)}


def history(limit: int = 20) -> List[Dict[str, str]]:
    conn = get_db_conn()
    try:
        _ensure_table(conn)
        return [dict(zip(('key', 'value', 'updated_at', 'updated_by'), r)) for r in conn.execute(
            'SELECT key, value, updated_at, updated_by FROM app_settings '
            'ORDER BY updated_at DESC LIMIT ?', (limit,)).fetchall()]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def list_items() -> List[Dict[str, Any]]:
    out = []
    for s in SPEC:
        ov = _load_overrides().get(s['key'])
        out.append({
            'key': s['key'], 'label': s['label'], 'group': s['group'], 'kind': s['kind'],
            'value': value(s['key']), 'default': default_value(s['key']),
            'overridden': ov is not None,
            'min': s.get('min'), 'max': s.get('max'), 'step': s.get('step'),
            'unit': s.get('unit', ''), 'note': s.get('note', ''), 'live': s['live'],
            'options': s.get('options') or ([],)[0],
            'impact': s.get('impact'),
        })
    return out
