"""theme_index.py - 主题篮子温度（2026-10-31）

用户诉求："篮子指数能显示在页面吗？方便在高潮的次日减仓。"
但同时提了一个更要紧的质疑："当天涨停加速特别多你觉得是高潮，第二天涨停更多，
那第二天算高潮还是前一天？"

**实测把"当天判顶"这条路否掉了**（2024-01~2026-10，39 只等权篮子，682 个交易日）：
  - 温度进前 20% 且（涨停≥3 或上涨≥90%）的"过热日"共 42 次，之后 3/5/10/20 日
    篮子中位 +2.7%/+2.9%/+2.2%/+7.5%，胜率 62%~68% —— **过热之后继续涨是常态**；
  - "过热次日收在过热日收盘之下"共 26 次，之后 1 日中位 +0.7%、胜率 69%
    —— 单看一根阴线确认不了任何事；
  - 自近 20 日高点首次回撤 8%（34 次）之后 10 日中位 −0.9%、胜率 41%；
    首次跌破 20 日线（13 次）之后 20 日中位 −4.6%、胜率 38% —— 弱偏空，只能减仓。

所以本模块**只做两件事，且都明确不是预测**：
  1. 温度（0-100）= 5 日涨幅 / 离 20 日线偏离 / 涨停家数 / 量能比 四项在
     近 250 个交易日的分位均值。回答"现在有多热"，不回答"是不是顶"。
  2. 转弱确认 = 自峰值回撤 ≥8% 或 收盘跌破 20 日线。回答"是否已经走坏"。
页面按"温度减一部分、确认再减一部分"给建议，永不宣称"这里是顶"。

数据：主路读 history_kline.db.stock_daily（前复权，主板到最新）；
创业板/科创板本地停更的那几天，读 theme_member_daily（盘后腾讯批量行情快照，
带交易所口径昨收，返回按"同源昨收"计算，不会因为复权口径拼接而失真）。

2026-10-31 多链：新增商业航天、算力两条人工链（成分见 THEMES，均非官方概念）。
最近约 10 个交易日由腾讯 qfq 日线回填（source=tencent_qfq，amount 为 close×volume
近似，只喂量能比），只写本地最后日期之后的日期——两套前复权基准不在同一行上拼接，
日收益按各自 prev_close 算（实测腾讯 prev_close 与本地前一日收盘一致）。
链排名按近 3 日等权涨幅（同幅看今日涨停家数/涨幅），温度只展示、不参与排序。

边界：本模块在行动台热路径上，快照有内存缓存（盘中 60s / 休市 600s），
计算只做一次全量聚合（约 2.7 万行，实测 <100ms）；不写任何全市场扫描。
"""
import asyncio
import sqlite3
import time
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional, Tuple

from app.config import KLINE_HISTORY_DB_PATH
from db.database import load_theme_member_daily
from services.ops import hot_chains

# ========== 篮子定义（固定链：仅作动态链不可用时的兜底） ==========
# 2026-10-31 起主口径改为 hot_chains 动态链（财联社概念榜前 3），下面三条固定链只在
# 财联社接口连续失败且库里也没有上一次的链时才使用，卡片 note 会写明"固定链兜底"。
# 2026-10-31：从单链扩到三条链，用户诉求是"首页要随市场主线变化"。
# 为什么不直接吃东财/新浪概念成分：实测两者都极宽（东财"人形机器人"328 只、
# "商业航天"327 只；新浪 488/383 只），混入风电（金风科技）、锂矿（中矿资源）、
# 传媒（人民网）等；按成交额取前 40 等权后，商业航天 10-27~10-29 反而跑出 -4.45%，
# 与上海沪工三连板、中国卫星 +10% 的盘面相反——宽概念会稀释掉真正的主升。
# 所以三条链都按"产业链上真正有营收/订单的公司"人工整理，并如实标注这个偏差。
THEMES: Dict[str, Dict] = {
    'robot': {
        'name': '人形机器人链',
        'note': '39 只等权（减速器/丝杠/电机/轴承/整机/传感），人工整理，非官方概念指数',
        'members': [
            ('sz300580', '贝斯特'), ('sz300100', '双林股份'), ('sz300953', '震裕科技'),
            ('sz301261', '恒工精密'), ('sz300718', '长盛轴承'), ('sz301368', '丰立智能'),
            ('sz003021', '兆威机电'), ('sz002472', '双环传动'), ('sz002896', '中大力德'),
            ('sh603915', '国茂股份'), ('sh688017', '绿的谐波'), ('sz002050', '三花智控'),
            ('sz300660', '江苏雷利'), ('sh688160', '步科股份'), ('sz002979', '雷赛智能'),
            ('sh688698', '伟创电气'), ('sz300124', '汇川技术'), ('sz300024', '机器人'),
            ('sz002527', '新时达'), ('sz002031', '巨轮智能'), ('sz300503', '昊志机电'),
            ('sz300161', '华中数控'), ('sz300007', '汉威科技'), ('sz300354', '东华测试'),
            ('sz002779', '中坚科技'), ('sz301550', '斯菱智驱'), ('sz300984', '金沃股份'),
            ('sz300258', '精锻科技'), ('sz300680', '隆盛科技'), ('sz300695', '兆丰股份'),
            ('sz300969', '恒帅股份'), ('sh603667', '五洲新春'), ('sh601100', '恒立液压'),
            ('sh601689', '拓普集团'), ('sh603728', '鸣志电器'), ('sh603416', '信捷电气'),
            ('sh603662', '柯力传感'), ('sh605488', '福莱新材'), ('sh603119', '浙江荣泰'),
        ],
    },
    'space': {
        'name': '商业航天链',
        'note': '40 只等权（火箭总装/卫星制造/星载芯片/连接器/航天材料），人工整理，非官方概念指数',
        'members': [
            ('sh600118', '中国卫星'), ('sh600879', '航天电子'), ('sz000901', '航天科技'),
            ('sz000547', '航天发展'), ('sh603131', '上海沪工'), ('sz003009', '中天火箭'),
            ('sh600501', '航天晨光'), ('sh601698', '中国卫通'), ('sh688568', '中科星图'),
            ('sz300627', '华测导航'), ('sz001270', '铖昌科技'), ('sz301050', '雷电微力'),
            ('sz300762', '上海瀚讯'), ('sh688387', '信科移动'), ('sh688311', '盟升电子'),
            ('sh688592', '司南导航'), ('sz300045', '华力创通'), ('sz002151', '北斗星通'),
            ('sz002465', '海格通信'), ('sz002829', '星网宇达'), ('sz002935', '天奥电子'),
            ('sz300342', '天银机电'), ('sz300455', '航天智装'), ('sz002025', '航天电器'),
            ('sz002179', '中航光电'), ('sh688629', '华丰科技'), ('sz000733', '振华科技'),
            ('sh603267', '鸿远电子'), ('sh603678', '火炬电子'), ('sh688523', '航天环宇'),
            ('sh688239', '航宇科技'), ('sh605123', '派克新材'), ('sh688102', '斯瑞新材'),
            ('sh688122', '西部超导'), ('sh600456', '宝钛股份'), ('sh688333', '铂力特'),
            ('sz002625', '光启技术'), ('sh600990', '四创电子'), ('sz300629', '新劲刚'),
            ('sh688282', '理工导航'),
        ],
    },
    'compute': {
        'name': '算力链',
        'note': '40 只等权（光模块/服务器/交换机/PCB/IDC液冷/算力芯片），人工整理，非官方概念指数',
        'members': [
            ('sz300308', '中际旭创'), ('sz300502', '新易盛'), ('sz300394', '天孚通信'),
            ('sz002281', '光迅科技'), ('sz300570', '太辰光'), ('sz301205', '联特科技'),
            ('sh688205', '德科立'), ('sh688498', '源杰科技'), ('sh688313', '仕佳光子'),
            ('sz300620', '光库科技'), ('sh603083', '剑桥科技'), ('sh600487', '亨通光电'),
            ('sh600522', '中天科技'), ('sh600498', '烽火通信'), ('sz000988', '华工科技'),
            ('sz002902', '铭普光磁'), ('sz000977', '浪潮信息'), ('sh603019', '中科曙光'),
            ('sz000938', '紫光股份'), ('sh601138', '工业富联'), ('sz002396', '星网锐捷'),
            ('sz301165', '锐捷网络'), ('sh603296', '华勤技术'), ('sz002463', '沪电股份'),
            ('sz300476', '胜宏科技'), ('sz002916', '深南电路'), ('sh600183', '生益科技'),
            ('sz300442', '润泽科技'), ('sz300383', '光环新网'), ('sh603881', '数据港'),
            ('sz300738', '奥飞数据'), ('sz002837', '英维克'), ('sz301018', '申菱环境'),
            ('sz300499', '高澜股份'), ('sh688041', '海光信息'), ('sh688256', '寒武纪'),
            ('sh688008', '澜起科技'), ('sh688047', '龙芯中科'), ('sh688668', '鼎通科技'),
            ('sz300679', '电连技术'),
        ],
    },
}

# ========== 链解析：动态链优先，固定链兜底（2026-10-31） ==========
def _cfg(theme: str) -> Optional[Dict]:
    """当前生效的链配置：先查 hot_chains（财联社概念榜前 3），没有再退回固定链。"""
    cfg = hot_chains.get(theme)
    if cfg:
        return cfg
    return THEMES.get(theme)


def active_ids() -> List[str]:
    """行动台要展示/计算的链 id；动态链不可用时返回固定三条。"""
    ids = hot_chains.ids()
    return ids if ids else list(THEMES)


def known(theme: str) -> bool:
    """theme 是否可算。接口校验用：动态链是财联社板块代码（clsXXXXX），每天都在变，
    不能再像 2026-10-31 之前那样只认写死的 THEMES 三个 id。"""
    return bool(_cfg(theme))


# 温度阈值（历史分位，不是涨跌幅）：≥80 过热、≥60 偏热、≤20 冰点
_TEMP_HOT = 80
_TEMP_WARM = 60
_TEMP_COLD = 20
# 转弱确认阈值
_DD_CONFIRM = -0.08
_DD_DEEP = -0.15

_LIVE_PHASES = ('morning', 'lunch', 'mid', 'tail')
_POST_CLOSE_HHMM = '15:02'   # 收盘后多久可以把当日行情当收盘价落库

_SERIES_DAYS = 120           # 页面展示的交易日数
_TEMP_WINDOW = 250           # 分位窗口（约一年）

_DISCLAIMER = ('这是一条"现在有多热"的参考线，不是顶底预测。历史上大涨日之后 94% 仍创新高，'
               '所以只按温度减一部分、按转弱确认再减一部分，别指望一次卖在最高点。')

_CACHE: Dict[str, Dict] = {}
_QUOTE_CACHE: Dict[str, Tuple[float, List[Dict]]] = {}


# ---------- 基础工具 ----------
def _limit_ratio(symbol: str) -> float:
    code = (symbol or '')[-6:]
    return 1.20 if code.startswith(('300', '301', '688')) else 1.10


def _limit_price(prev_close: float, ratio: float) -> float:
    """涨停价：Decimal 四舍五入到分（Python 内置 round 是银行家舍入，会差一分）。"""
    if prev_close <= 0:
        return 0.0
    v = Decimal(str(prev_close)) * Decimal(str(ratio))
    return float(v.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def _pct(vals: List[Optional[float]], window: int = _TEMP_WINDOW) -> List[Optional[float]]:
    """滚动分位（0~1）：当前值在"含自己在内的最近 window 个值"里的占比。

    用分位而不是绝对值，是因为不同阶段板块的成交额/涨停家数基数差一个量级
    （2024 年日成交几十亿，2026 年几百亿），绝对值不可比。
    """
    out: List[Optional[float]] = [None] * len(vals)
    for i, v in enumerate(vals):
        if v is None or i < 60:
            continue
        w = [x for x in vals[max(0, i - window + 1):i + 1] if x is not None]
        if len(w) < 30:
            continue
        out[i] = sum(1 for x in w if x <= v) / len(w)
    return out


# ---------- 数据装载 ----------
def _load_local_daily(codes: List[str]) -> Dict[str, List[Tuple]]:
    """history_kline.db 前复权日线：{裸代码: [(date, close, high, turnover)]}（升序）。"""
    out: Dict[str, List[Tuple]] = {c: [] for c in codes}
    try:
        conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=10)
        marks = ','.join('?' * len(codes))
        rows = conn.execute(
            f'SELECT symbol, date, close, high, turnover FROM stock_daily '
            f'WHERE symbol IN ({marks}) ORDER BY symbol, date', codes).fetchall()
        conn.close()
        for sym, d, close, high, turnover in rows:
            out.setdefault(sym, []).append((d, close, high, turnover))
    except Exception as error:
        print(f'[主题温度] 读取本地日K失败: {error}')
    return out


def _build_member_rows(theme: str) -> Dict[str, List[Dict]]:
    """把本地日K与 theme_member_daily 合并成每只票的日序列（升序）。

    同一日期两边都有时以 theme_member_daily 为准：它带交易所口径 prev_close，
    除权日不用自己拼（也是创业板/科创板本地停更后唯一的数据来源）。
    """
    cfg = _cfg(theme)
    if not cfg:
        raise KeyError(f'未知主题链: {theme}')
    codes = [s[-6:] for s, _ in cfg['members']]
    local = _load_local_daily(codes)
    snap = load_theme_member_daily(theme)
    by_sym: Dict[str, Dict[str, Dict]] = {s: {} for s, _ in cfg['members']}
    for sym, _name in cfg['members']:
        for d, close, high, turnover in local.get(sym[-6:], []):
            by_sym[sym][d] = {'date': d, 'close': close, 'high': high,
                              'amount': turnover, 'prev': None}
    for r in snap:
        sym = (r.get('symbol') or '').lower()
        if sym not in by_sym or not r.get('date'):
            continue
        by_sym[sym][r['date']] = {
            'date': r['date'], 'close': float(r.get('close') or 0),
            'high': float(r.get('high') or 0), 'amount': float(r.get('amount') or 0),
            'prev': float(r.get('prev_close') or 0) or None,
        }
    out: Dict[str, List[Dict]] = {}
    for sym, dates in by_sym.items():
        rows = [dates[d] for d in sorted(dates)]
        prev = None
        for r in rows:
            if r['prev'] is None:
                r['prev'] = prev
            prev = r['close'] or prev
        out[sym] = rows
    return out


# ---------- 序列计算 ----------
def _aggregate(theme: str, member_rows: Dict[str, List[Dict]]) -> List[Dict]:
    """按交易日等权聚合：指数、当日涨幅、涨停/炸板家数、上涨占比、成交额。"""
    dates = sorted({r['date'] for rows in member_rows.values() for r in rows})
    pos: Dict[str, Dict[str, Dict]] = {
        sym: {r['date']: r for r in rows} for sym, rows in member_rows.items()}
    rat = {sym: _limit_ratio(sym) for sym in pos}
    level = 100.0
    series: List[Dict] = []
    for d in dates:
        rets, zt, zb, amt = [], 0, 0, 0.0
        for sym, day_map in pos.items():
            r = day_map.get(d)
            if not r or not r.get('prev') or not r.get('close'):
                continue
            prev, close = float(r['prev']), float(r['close'])
            if prev <= 0 or close <= 0:
                continue
            ret = close / prev - 1
            if abs(ret) > 0.30:      # 复权断点/数据异常，挡在均值外（单票最大 20% 不可能到 30%）
                continue
            rets.append(ret)
            amt += float(r.get('amount') or 0)
            lp = _limit_price(prev, rat[sym])
            if close >= lp - 0.005:
                zt += 1
            elif float(r.get('high') or 0) >= lp - 0.005:
                zb += 1
        if len(rets) < 20:           # 样本不足（本地停更/新票）就不出点，不拿 3 只票冒充指数
            continue
        r = sum(rets) / len(rets)
        level *= 1 + r
        series.append({'date': d, 'idx': round(level, 2), 'ret': round(r, 5),
                       'zt': zt, 'zb': zb, 'upRatio': round(sum(1 for x in rets if x > 0) / len(rets), 4),
                       'amount': round(amt / 1e8, 1), 'n': len(rets)})
    return series


def _enrich(series: List[Dict]) -> List[Dict]:
    """补齐 20 日线、偏离、5 日涨幅、量能比、峰值回撤、温度、状态。"""
    levels = [s['idx'] for s in series]
    amounts = [s['amount'] for s in series]
    ret5: List[Optional[float]] = []
    dev20: List[Optional[float]] = []
    amt5x: List[Optional[float]] = []
    peak = 0.0
    for i, s in enumerate(series):
        peak = max(peak, s['idx'])
        s['peak'] = round(peak, 2)                    # 历史峰值（只作背景）
        # 回撤口径：主口径用**近 20 日高点**（当前这一波的高点）。
        # 用历史最高点会永远显示 -40%（2026-01 见过 285），用近 60 日高点又会被
        # 两个月前的旧高点拖住——都对"高潮之后回撤多少"没有用。
        # 2026-10-31 实测（682 个交易日）：自近 20 日高点首次回撤 8% 共 34 次，
        # 之后 10 日篮子中位 −0.9%、胜率 41%，20 日中位 +1.0%——弱偏空，不是择时信号。
        lo20 = max(0, i - 19)
        w20 = levels[lo20:i + 1]
        pk20 = max(w20)
        s['peak20'] = round(pk20, 2)
        s['peak20Date'] = series[lo20 + w20.index(pk20)]['date']
        s['dd'] = round(s['idx'] / pk20 - 1, 5) if pk20 else 0.0
        lo60 = max(0, i - 59)
        w60 = levels[lo60:i + 1]
        pk60 = max(w60)
        s['peak60'] = round(pk60, 2)
        s['peak60Date'] = series[lo60 + w60.index(pk60)]['date']
        s['dd60'] = round(s['idx'] / pk60 - 1, 5) if pk60 else 0.0
        if i >= 19:
            ma = sum(levels[i - 19:i + 1]) / 20
            s['ma20'] = round(ma, 2)
            dev20.append(round(levels[i] / ma - 1, 5))
        else:
            s['ma20'] = None
            dev20.append(None)
        if i >= 5 and levels[i - 5] > 0:
            s['ret5'] = round(levels[i] / levels[i - 5] - 1, 5)
            ret5.append(s['ret5'])
        else:
            s['ret5'] = None
            ret5.append(None)
        # 近 3 日：链排名用这个。5 日太钝（今天刚退潮还排第一），1 日太抖。
        s['ret3'] = round(levels[i] / levels[i - 3] - 1, 5) if i >= 3 and levels[i - 3] > 0 else None
        prior = amounts[i - 5:i]
        amt5x.append(round(amounts[i] / (sum(prior) / len(prior)), 3)
                     if len(prior) == 5 and sum(prior) > 0 else None)
    p_ret5 = _pct(ret5)
    p_dev = _pct(dev20)
    p_zt = _pct([float(s['zt']) for s in series])
    p_amt = _pct(amt5x)
    for i, s in enumerate(series):
        comps = [x[i] for x in (p_ret5, p_dev, p_zt, p_amt) if x[i] is not None]
        s['temp'] = round(100 * sum(comps) / len(comps), 1) if len(comps) >= 2 else None
        s['state'] = _state(s)
    return series


_STATE_LABEL = {'过热': '高位过热', '回调': '跌破20日线', '退潮': '深度回撤',
                '主升': '主升中', '震荡': '震荡'}


def _state(s: Dict) -> str:
    if (s.get('dd') or 0) <= _DD_DEEP:
        return '退潮'
    if s.get('ma20') and s['idx'] < s['ma20']:
        return '回调'
    if s.get('temp') is not None and s['temp'] >= _TEMP_HOT:
        return '过热'
    if (s.get('ret5') or 0) > 0.06:
        return '主升'
    return '震荡'


def _is_hot(s: Dict) -> bool:
    """过热日：温度进历史前 20% 且（涨停≥3 或 上涨占比≥90%）。"""
    return (s.get('temp') is not None and s['temp'] >= _TEMP_HOT
            and (s.get('zt', 0) >= 3 or s.get('upRatio', 0) >= 0.9))


def _banner(series: List[Dict]) -> Optional[Dict]:
    """横幅只回答"下一步怎么做"，不宣称顶。"""
    if len(series) < 2:
        return None
    last, prev = series[-1], series[-2]
    md = lambda d: (d or '')[5:]

    if _is_hot(last):
        return {
            'level': 'warn', 'kind': 'overheat_today',
            'title': f'{md(last["date"])} 板块过热 {last["temp"]:.0f}/100',
            'text': '明天不追高；高位持仓可先减 1/3，冲高不创新高再减。',
            'detail': ('过热≠顶：历史上这类过热日共 42 次，之后 3/5/10 日篮子中位 '
                       '+2.7%/+2.9%/+2.2%、胜率 62%~68%，过热后继续涨是常态。'
                       '所以这一步只减一部分、明天不追高，不一次清仓。'),
        }
    if (last.get('dd') is not None and last['dd'] <= _DD_CONFIRM
            and (prev.get('dd') is not None and prev['dd'] > _DD_CONFIRM)):
        return {
            'level': 'danger', 'kind': 'drawdown',
            'title': f'自 {md(last.get("peak20Date"))} 高点回撤 {abs(last["dd"]) * 100:.1f}%',
            'text': '转弱确认：高位持仓再减一档，但不建议清仓。',
            'detail': ('历史上自近 20 日高点首次回撤 8% 共 34 次，之后 10 日篮子中位 −0.9%、'
                       '胜率 41%，20 日中位 +1.0%——弱偏空，只能用来降一档仓位，不能当择时。'),
        }
    if (last.get('ma20') and last['idx'] < last['ma20']
            and prev.get('ma20') and prev['idx'] >= prev['ma20']):
        return {
            'level': 'warn', 'kind': 'ma20',
            'title': f'跌破 20 日线（{last["ma20"]:.1f}）',
            'text': '趋势转弱的第一道确认：高位持仓减一档，跌破不追。',
            'detail': ('历史上首次跌破 20 日线（此前连续 10 日在上方）共 13 次，'
                       '之后 20 日收益中位数 −4.6%，但均值 +2.5%——分歧很大，'
                       '所以它只用来减仓、不用来猜底。'),
        }
    if _is_hot(prev):
        return {
            'level': 'warn', 'kind': 'overheat_yesterday',
            'title': f'昨日（{md(prev["date"])}）板块过热，今日 {last["ret"] * 100:+.1f}%',
            'text': '按计划：过热时已减 1/3 的，不在这根阴线上加码；等结构确认再减下一档。',
            'detail': ('回测里"过热次日收阴"共 26 次，之后 1 日中位 +0.7%、胜率 69%——'
                       '单根阴线往往是回踩，不是顶。下一步看两件事：自近 20 日高点回撤 8%、'
                       '或收盘跌破 20 日线，出现任一再减。'),
        }
    hot_idx = [i for i, s in enumerate(series) if _is_hot(s)]
    if hot_idx and len(series) - 1 - hot_idx[-1] <= 5:
        hs = series[hot_idx[-1]]
        gap = last['idx'] / hs['idx'] - 1 if hs['idx'] else 0
        if _DD_CONFIRM < gap <= -0.03:
            line = (hs.get('peak20') or hs['idx']) * (1 + _DD_CONFIRM)
            return {
                'level': 'info', 'kind': 'cooling',
                'title': f'{md(hs["date"])} 过热（{hs["temp"]:.0f}）后已回落 {abs(gap) * 100:.1f}%',
                'text': '还没到确认线：已按过热减 1/3 的就够，等回撤 8% 或破 20 日线再减下一档。',
                'detail': (f'8% 确认线对应指数约 {line:.0f}，今天 {last["idx"]:.0f}；'
                           f'当前距近 20 日高点 {last.get("dd", 0) * 100:.1f}%。'),
            }
    return None


def _recent_events(series: List[Dict], n: int = 3) -> List[Dict]:
    out = []
    for s in series:
        if _is_hot(s):
            out.append({'date': s['date'], 'temp': s.get('temp'), 'ret': s.get('ret'),
                        'zt': s.get('zt'), 'zb': s.get('zb')})
    return out[-n:]


# ---------- 行情（盘中临时点 / 盘后落库） ----------
def _is_live_phase() -> bool:
    try:
        from services.data.trading_calendar import get_session_phase
        return get_session_phase().get('phase') in _LIVE_PHASES
    except Exception:
        return False


async def _fetch_quotes(theme: str, max_age: float = 60.0) -> List[Dict]:
    """39 只一次批量行情；模块内 60 秒缓存，避免行动台轮询把它变成高频请求。"""
    item = _QUOTE_CACHE.get(theme)
    if item and time.time() - item[0] < max_age:
        return item[1]
    from services.data.stock import fetch_with_retry, parse_tencent_batch_data
    symbols = ','.join(s for s, _ in (_cfg(theme) or {}).get('members', []) or [])
    try:
        raw = await fetch_with_retry(symbols)
        quotes = parse_tencent_batch_data(raw)
    except Exception as error:
        print(f'[主题温度] 批量行情失败: {error}')
        return []
    _QUOTE_CACHE[theme] = (time.time(), quotes)
    return quotes


def _quote_aggregate(quotes: List[Dict], series: List[Dict]) -> Optional[Dict]:
    """把盘中快照算成"今日临时一行"（不落库，收盘后再由 sync_today 写正式行）。"""
    if not quotes or not series:
        return None
    rets, zt, zb, amt = [], 0, 0, 0.0
    for q in quotes:
        sym = (q.get('symbol') or '').lower()
        prev = float(q.get('prevCloseRaw') or 0)
        price = float(q.get('priceRaw') or 0)
        high = float(q.get('highRaw') or 0)
        if prev <= 0 or price <= 0:
            continue
        ret = price / prev - 1
        if abs(ret) > 0.30:
            continue
        rets.append(ret)
        amt += float(q.get('turnover') or 0) * 10000  # 万元 → 元
        lp = _limit_price(prev, _limit_ratio(sym))
        if price >= lp - 0.005:
            zt += 1
        elif high >= lp - 0.005:
            zb += 1
    if len(rets) < 20:
        return None
    r = sum(rets) / len(rets)
    return {'date': datetime.now().strftime('%Y-%m-%d'),
            'idx': round(series[-1]['idx'] * (1 + r), 2), 'ret': round(r, 5),
            'zt': zt, 'zb': zb, 'upRatio': round(sum(1 for x in rets if x > 0) / len(rets), 4),
            'amount': round(amt / 1e8, 1), 'n': len(rets), 'live': True}


async def sync_today(theme: str = 'robot', force: bool = False) -> int:
    """收盘后把 39 只的当日 OHLC 落 theme_member_daily（15:02 之后才允许，force 跳过时段判断）。"""
    from services.data.trading_calendar import is_trading_day
    now = datetime.now()
    if not is_trading_day(now.date()):
        return 0
    if not force and now.strftime('%H:%M') < _POST_CLOSE_HHMM:
        return 0
    quotes = await _fetch_quotes(theme, max_age=0)
    rows = []
    for q in quotes:
        sym = (q.get('symbol') or '').lower()
        price = float(q.get('priceRaw') or 0)
        prev = float(q.get('prevCloseRaw') or 0)
        if sym not in {s for s, _ in (_cfg(theme) or {}).get('members', []) or []} or price <= 0 or prev <= 0:
            continue
        rows.append({
            'symbol': sym, 'date': now.strftime('%Y-%m-%d'), 'name': q.get('name'),
            'open': float(q.get('open') or 0) or None,
            'high': float(q.get('highRaw') or 0) or None,
            'low': float(q.get('lowRaw') or 0) or None,
            'close': price, 'prev_close': prev,
            'amount': float(q.get('turnover') or 0) * 10000,
            'volume': float(q.get('volumeRaw') or 0) * 100,
            'source': 'tencent_quote',
        })
    if not rows:
        return 0
    from db.database import save_theme_member_daily
    ok = await asyncio.to_thread(save_theme_member_daily, rows, theme)
    if ok:
        _CACHE.pop(theme, None)      # 落库后让下次取数重新聚合
    return ok


# ---------- 对外接口 ----------
_CACHE_TTL_LIVE = 60
_CACHE_TTL_IDLE = 600


async def _compute(theme: str, live: bool) -> Dict:
    cfg = _cfg(theme)
    if not cfg:
        return {'theme': theme, 'name': theme, 'error': '主题链已更新，等待下一轮刷新',
                'memberCount': 0, 'series': [], 'banner': None}
    member_rows = await asyncio.to_thread(_build_member_rows, theme)
    series = _aggregate(theme, member_rows)
    if len(series) < 25:
        return {'theme': theme, 'name': cfg['name'], 'error': '成分日线不足，暂无法计算',
                'memberCount': len(cfg['members']), 'series': [], 'banner': None,
                'source': cfg.get('source', 'fixed'), 'plate': cfg.get('plateName'),
                'plateChangePct': cfg.get('changePct'),
                'plateLimitUp': cfg.get('limitUpNum'),
                'rank': cfg.get('rank')}
    _enrich(series)
    live_row = None
    if live and _is_live_phase():
        quotes = await _fetch_quotes(theme)
        cand = _quote_aggregate(quotes, series)
        if cand and cand['date'] != series[-1]['date']:
            series = _enrich(series + [cand])
            live_row = series[-1]
        elif cand and cand['date'] == series[-1]['date']:
            # 今日已有落库行（收盘后 sync 过），以落库为准，不再叠盘中点
            live_row = None
    completed = [s for s in series if not s.get('live')]
    last = completed[-1]
    peak = max(completed, key=lambda s: s['idx'])
    banner = _banner(completed)
    view = [{k: s.get(k) for k in ('date', 'idx', 'ret', 'zt', 'zb', 'upRatio', 'amount', 'temp', 'dd')}
            for s in completed[-_SERIES_DAYS:]]
    return {
        'theme': theme, 'name': cfg['name'], 'note': cfg['note'],
        'memberCount': len(cfg['members']),
        # 来源：cls=财联社当日热门概念（动态链），fixed=三条人工链兜底
        'source': cfg.get('source', 'fixed'),
        'plate': cfg.get('plateName'), 'rank': cfg.get('rank'),
        # 板块当日原始变动（财联社榜口径，只用于说明"为什么今天展示它"）
        'plateChangePct': cfg.get('changePct'),
        'plateLimitUp': cfg.get('limitUpNum'),
        'asOf': last['date'],
        'last': {k: last.get(k) for k in ('date', 'idx', 'ret', 'zt', 'zb', 'upRatio',
                                          'amount', 'temp', 'dd', 'dd60', 'ma20', 'ret5',
                                          'ret3', 'state', 'peak20', 'peak20Date')},
        # 盘中临时行（今日未收盘）：前端用它覆盖卡片头条（指数/当日/温度/距高点/状态），
        # 让盘中看到的是"今天"而不是昨天收盘。收盘后 sync_today 落库、live 自动为 None。
        'live': ({**{k: live_row.get(k) for k in ('date', 'idx', 'ret', 'zt', 'zb', 'upRatio',
                                                  'amount', 'temp', 'dd', 'state')},
                  'stateLabel': _STATE_LABEL.get(live_row.get('state') or '', ''),
                  'at': datetime.now().strftime('%H:%M')} if live_row else None),
        'peak': {'date': peak['date'], 'idx': peak['idx']},
        'recentPeak': {'date': last.get('peak20Date'), 'idx': last.get('peak20')},
        'dd': last.get('dd'),
        'dd60': last.get('dd60'),
        'state': last.get('state'),
        'stateLabel': _STATE_LABEL.get(last.get('state') or '', ''),
        'temp': last.get('temp'),
        'banner': banner,
        'events': _recent_events(completed),
        'series': view,
        'meta': {
            'caliber': cfg['note'],
            'tempBasis': '近 250 交易日分位均值：5 日涨幅 / 离 20 日线 / 涨停家数 / 量能比',
            'disclaimer': _DISCLAIMER,
        },
    }


async def snapshot(theme: str = 'robot', live: bool = True) -> Dict:
    """theme='auto' 时先算齐所有链，再返回排名第一的那条（见 _snapshot_auto）。"""
    if theme == 'auto':
        return await _snapshot_auto(live)
    cached = _CACHE.get(theme)
    ttl = _CACHE_TTL_LIVE if _is_live_phase() else _CACHE_TTL_IDLE
    if cached and time.time() - cached['ts'] < ttl:
        return cached['data']
    data = await _compute(theme, live)
    _CACHE[theme] = {'ts': time.time(), 'data': data}
    return data


def peek_cached(theme: str = 'robot') -> Optional[Dict]:
    item = _CACHE.get(theme)
    return item['data'] if item else None


# ---------- 多链：摘要、排名、自动选链 ----------
def _summary(theme: str) -> Optional[Dict]:
    """从缓存里取一条链的摘要；没算过就返回 None（不在这里触发计算）。"""
    item = _CACHE.get(theme)
    if not item:
        return None
    d = item['data']
    name = d.get('name') or theme
    if d.get('error'):
        return {'id': theme, 'name': name, 'error': d['error'],
                'asOf': d.get('asOf'), 'memberCount': d.get('memberCount')}
    last = d.get('last') or {}
    return {
        'id': theme, 'name': name,
        'temp': d.get('temp'), 'ret3': last.get('ret3'), 'ret5': last.get('ret5'),
        'ret': last.get('ret'), 'zt': last.get('zt'), 'zb': last.get('zb'),
        'state': d.get('state'), 'stateLabel': d.get('stateLabel'),
        'asOf': d.get('asOf'), 'memberCount': d.get('memberCount'),
    }


def _rank_key(s: Dict) -> Tuple[float, float, float]:
    """当前最热 = 近 3 日等权涨幅最大；同幅看今日涨停家数，再看今日涨幅。

    用近 3 日而不是 5 日：5 日会把"三天前那波"算进今天（10-31 实测商业航天
    10-28/29 走强、10-30 退潮，5 日仍为负），3 日更接近"这两天在炒什么"；
    也不用温度：温度是各自历史的相对位置，跨链可比但不是"谁在涨"。
    """
    ret3 = s.get('ret3')
    return (ret3 if ret3 is not None else -9.99,
            float(s.get('zt') or 0), float(s.get('ret') or 0))


def rank_chains() -> List[Dict]:
    """按近 3 日强度排序当前已缓存的链；未计算的链不出现在结果里（不假装有数据）。"""
    rows = [s for s in (_summary(t) for t in active_ids()) if s]
    rows.sort(key=_rank_key, reverse=True)
    for i, r in enumerate(rows):
        r['rank'] = i + 1
    return rows


def _wrap_auto(data: Dict, chains: List[Dict]) -> Dict:
    out = dict(data)
    out['auto'] = True
    out['chains'] = chains
    return out


def peek_auto() -> Optional[Dict]:
    """同步取"当前最热链"（只读缓存），给行动台超时兜底用。"""
    chains = rank_chains()
    if not chains:
        return None
    best = chains[0]['id']
    data = peek_cached(best)
    return _wrap_auto(data, chains) if data else None


async def _snapshot_auto(live: bool) -> Dict:
    """算齐所有链后选最热的一条返回。

    三条动态链冷算合计约 0.4~0.6s（每链读 683 天 × 40 只并聚合），行动台给 2s 预算；
    常态命中各自缓存后接近 0ms。链集合来自 hot_chains（财联社概念榜前 3）。
    """
    ids = active_ids()
    if not ids:
        return {}
    for tid in ids:
        await snapshot(tid, live=live)
    chains = rank_chains()
    best = chains[0]['id'] if chains else ids[0]
    data = peek_cached(best) or {}
    return _wrap_auto(data, chains)


async def warm_all(live: bool = False) -> int:
    """预热全部链（启动/盘后调度用），返回成功条数。"""
    ok = 0
    for tid in active_ids():
        try:
            data = await snapshot(tid, live=live)
            ok += 0 if data.get('error') else 1
        except Exception as error:
            print(f'[主题温度] 预热 {tid} 失败: {error}')
    return ok

