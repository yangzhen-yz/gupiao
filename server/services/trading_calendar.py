"""A股交易日历工具：判断某日期是否为交易日（排除周末与法定节假日）。

数据来源：沪深北交易所 2026 年部分节假日休市安排公告（2025-12-22 发布）。
每年需根据最新公告更新 CLOSED_DATES 与 MAKEUP_TRADING_DAYS。
"""

from datetime import datetime, date
from typing import Dict


# 2026 年 A股休市日期（含周末与法定节假日）
CLOSED_DATES_2026 = {
    # 元旦：1/1(四)-1/3(六) + 1/4(日)周末
    '2026-01-01', '2026-01-02', '2026-01-03', '2026-01-04',
    # 春节：2/14(六)周末 + 2/15(日)-2/23(一) + 2/28(六)周末
    '2026-02-14', '2026-02-15', '2026-02-16', '2026-02-17', '2026-02-18',
    '2026-02-19', '2026-02-20', '2026-02-21', '2026-02-22', '2026-02-23', '2026-02-28',
    # 清明节：4/4(六)-4/6(一)
    '2026-04-04', '2026-04-05', '2026-04-06',
    # 劳动节：5/1(五)-5/5(二) + 5/9(六)周末
    '2026-05-01', '2026-05-02', '2026-05-03', '2026-05-04', '2026-05-05', '2026-05-09',
    # 端午节：6/19(五)-6/21(日)
    '2026-06-19', '2026-06-20', '2026-06-21',
    # 中秋节：9/25(五)-9/27(日)，9/20(日)周末
    '2026-09-20', '2026-09-25', '2026-09-26', '2026-09-27',
    # 国庆节：10/1(四)-10/7(三) + 10/10(六)周末
    '2026-10-01', '2026-10-02', '2026-10-03', '2026-10-04', '2026-10-05',
    '2026-10-06', '2026-10-07', '2026-10-10',
}

# 调休补班交易日（周末但需交易），2026 年公告未安排，留空
MAKEUP_TRADING_DAYS_2026: set = set()

# 当前生效的交易日历（按年切换时在此扩展）
CLOSED_DATES = CLOSED_DATES_2026
MAKEUP_TRADING_DAYS = MAKEUP_TRADING_DAYS_2026


def is_trading_day(d) -> bool:
    """判断是否为 A股交易日。

    Args:
        d: date 对象或 'YYYY-MM-DD' 字符串。
    Returns:
        True=交易日, False=周末/节假日休市。
    """
    if isinstance(d, str):
        try:
            d = datetime.strptime(d, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return False
    elif not isinstance(d, date):
        return False

    # 调休补班：周末但正常交易
    if d.isoformat() in MAKEUP_TRADING_DAYS:
        return True
    # 周六、周日休市
    if d.weekday() >= 5:
        return False
    # 法定节假日休市
    if d.isoformat() in CLOSED_DATES:
        return False
    return True


# ========== 盘中时段划分（2026-10-13「今日行动台」） ==========
# 前端 helpers.js 的时段判断有两处已知缺口：把 9:15-9:25 集合竞价算作非交易时段、
# 没有 15:00 之后的盘后段。行动台需要按"现在该做什么"重排内容，因此时段以服务端为
# 唯一权威，前端只做倒计时展示。

# 各时段边界（当日分钟数，左闭右开）
PHASE_BOUNDS = [
    (0, 'pre'),
    (9 * 60 + 15, 'auction'),      # 集合竞价
    (9 * 60 + 30, 'morning'),      # 早盘
    (11 * 60 + 30, 'lunch'),       # 午间休市
    (13 * 60, 'mid'),              # 午后盘中
    (14 * 60 + 30, 'tail'),        # 尾盘确认窗口
    (15 * 60, 'post'),             # 盘后（收评/回测陆续产出）
    (18 * 60, 'closed'),           # 当日收尾，只剩明日计划
]

PHASE_LABELS = {
    'pre': '盘前',
    'auction': '集合竞价 9:15-9:25',
    'morning': '早盘 9:30-11:30',
    'lunch': '午间休市 11:30-13:00',
    'mid': '盘中 13:00-14:30',
    'tail': '尾盘确认 14:30-15:00',
    'post': '盘后复盘 15:00-18:00',
    'closed': '休市',
    'unknown': '交易日历未覆盖',
}

# 交易日历只硬编码到 2026 年：跨年时必须显式暴露，不能把未知年份静默当交易日
CALENDAR_YEARS = {2026}


def is_calendar_covered(d) -> bool:
    """当前日期是否落在已维护的日历年份内（跨年守卫）"""
    if isinstance(d, str):
        try:
            d = datetime.strptime(d, '%Y-%m-%d')
        except (ValueError, TypeError):
            return False
    return getattr(d, 'year', None) in CALENDAR_YEARS


def get_session_phase(now=None) -> Dict:
    """判定此刻处于哪个交易时段。

    Returns:
        {'phase': str, 'phaseLabel': str, 'isTradingDay': bool,
         'calendarCovered': bool, 'nextPhaseAt': 'HH:MM' | None,
         'minutesToNext': int | None}

    非交易日 ⇒ phase='closed'（周末/节假日）；
    交易日但日历未覆盖该年份（如 2027 年工作日）⇒ phase='unknown'，由前端挂横幅提示，
    而不是假装"交易中"给出误导性的行动建议。
    """
    now = now or datetime.now()
    trading = is_trading_day(now.date())
    covered = is_calendar_covered(now)

    if not trading:
        phase = 'closed'
    elif not covered:
        phase = 'unknown'
    else:
        minutes = now.hour * 60 + now.minute
        phase = 'pre'
        for start, name in PHASE_BOUNDS:
            if minutes >= start:
                phase = name

    # 下一时段边界（仅交易日内有意义）
    next_at, minutes_to_next = None, None
    if phase in ('pre', 'auction', 'morning', 'lunch', 'mid', 'tail', 'post'):
        minutes = now.hour * 60 + now.minute
        for start, _name in PHASE_BOUNDS[1:]:
            if start > minutes:
                next_at = f'{start // 60:02d}:{start % 60:02d}'
                minutes_to_next = start - minutes
                break

    return {
        'phase': phase,
        'phaseLabel': PHASE_LABELS.get(phase, phase),
        'isTradingDay': trading,
        'calendarCovered': covered,
        'nextPhaseAt': next_at,
        'minutesToNext': minutes_to_next,
    }

