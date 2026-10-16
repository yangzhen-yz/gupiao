"""minute_history.py - 本地 5 分钟K线库（baostock 前复权），用来回答"早盘买到底值多少钱"

为什么需要它：日K只有 开/高/低/收 四个锚点，所以"9:30~10:00 买入"这种口径一直没数；
而 `成交额/成交量` 算出来的是**未复权**均价，跟前面复权的 OHLC 不是一个尺度
（实测只有 71.5% 的行落在当日最高最低之间），拿来当买入成本会系统性骗人。

baostock 的 5 分钟线带 adjustflag="2"（前复权），实测与本地日K**完全对齐**
（末根收盘 == 日K收盘，首根开盘 == 日K开盘，逐价相同），所以早盘/盘中/尾盘三段
第一次可以有真实成交价。

注意：分钟行的 volume/amount 是**未复权原始量**，价格是前复权价，两者不同尺度 ——
所以段均价一律用 `sum(价格×成交量)/sum(成交量)`（价格加权），不直接用 amount/volume。

用法：
  python minute_history.py --ensure sh600519 2025-09-01     # 补一天
  python minute_history.py --stats                          # 库里有多少
"""
import argparse
import os
import sqlite3
import sys
import threading
from datetime import datetime
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services.data.kline_history import _to_baostock_code

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(SERVER_DIR, 'data', 'history_minute.db')
BARS_PER_DAY = 48           # 5 分钟 × 4 小时
_login_lock = threading.Lock()
_logged_in = False


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=10000')
    conn.execute('''CREATE TABLE IF NOT EXISTS minute_5m (
        symbol TEXT NOT NULL, date TEXT NOT NULL, time TEXT NOT NULL,
        open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
        fetched_at TEXT, PRIMARY KEY (symbol, date, time))''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_minute_symbol_date ON minute_5m(symbol, date)')
    return conn


_FAIL_STREAK = 0      # 连续失败计数：到 20 次就判定被限流/拉黑，立即收工并进入冷却


def _login() -> bool:
    global _logged_in
    import baostock as bs
    with _login_lock:
        if _logged_in:
            return True
        _logged_in = bs.login().error_code == '0'
        return _logged_in


def logout() -> None:
    global _logged_in
    import baostock as bs
    with _login_lock:
        if _logged_in:
            try:
                bs.logout()
            except Exception:
                pass
            _logged_in = False


def fetch_day(symbol: str, date: str, retries: int = 3) -> Optional[List[tuple]]:
    """拉某只票某天的 5 分钟前复权分钟线并落库。返回落库的行数；失败返回 None。

    会话策略（2026-10-16 改）：`_login()` 本身是"一次进程一次登录"，但旧版在每次失败后
    `logout()`，于是失败一只就重登一次 —— 分钟线是逐只逐天拉的，一次分析上万请求，
    这正是 baostock 按登录频率判黑（10001011）的触发方式。现在：进入前先查封禁冷却，
    失败累计到 20 次直接收工并记冷却，只有拿不到数据才断会话，绝不为了"自愈"去敲门。
    """
    global _FAIL_STREAK
    import socket
    socket.setdefaulttimeout(60)
    code = symbol.lower()
    from services.data import market_cap
    if not market_cap.passes(code):     # 总市值下限：<35亿 不拉分时线（防量化操控小微盘）
        return []
    bs_code = _to_baostock_code(code[-6:])
    import baostock as bs
    # baostock 冷却门禁（2026-10-16）：分钟线是"逐只逐天"拉的，一次分析要打上万个请求，
    # 比日K更容易触发它的登录频率判黑。日K那边已经被封过一次，这条路必须先看同一份冷却状态。
    try:
        from services.data.kline_history import ban_cooldown_remaining
        _left = ban_cooldown_remaining()
    except Exception:
        _left = 0.0
    if _left > 0:
        print(f'[分钟K] baostock 冷却中（还剩 {int(_left)} 分钟），本次不拉分钟线'
              f'（本地已缓存的部分照常可用）')
        return None
    for attempt in range(retries):
        if not _login():
            return None
        try:
            rs = bs.query_history_k_data_plus(
                bs_code, 'date,time,open,high,low,close,volume,amount',
                start_date=date, end_date=date, frequency='5', adjustflag='2')
            if rs.error_code != '0':
                raise RuntimeError(rs.error_msg)
            rows = []
            while rs.next():
                r = rs.get_row_data()
                if len(r) < 8 or r[0] != date:
                    continue
                try:
                    rows.append((code, r[0], r[1], float(r[2]), float(r[3]), float(r[4]),
                                 float(r[5]), float(r[6]), float(r[7])))
                except ValueError:
                    continue
            if not rows:
                return []
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            with _conn() as c:
                c.executemany('INSERT OR REPLACE INTO minute_5m VALUES (?,?,?,?,?,?,?,?,?,?)',
                              [r + (now,) for r in rows])
                c.commit()
            _FAIL_STREAK = 0
            return rows
        except Exception as e:
            _FAIL_STREAK += 1
            print(f'[分钟K] {symbol} {date} 第{attempt + 1}次失败: {e}')
            try:
                from services.data.kline_history import _is_blocked_msg, note_ban
            except Exception:
                _is_blocked_msg = lambda m: '黑名单' in m or '10001011' in m
                note_ban = lambda r, minutes=None: None
            if _is_blocked_msg(str(e)):
                note_ban(f'分钟线被 baostock 拒绝: {e}')
                return None
            if _FAIL_STREAK >= 20:
                note_ban(f'分钟线连续 {_FAIL_STREAK} 次失败，判定限流，立即收工', minutes=60)
                return None
            logout()
    return None


def get_day(symbol: str, date: str) -> List[tuple]:
    """取某天分钟线（先查库，没有再拉）。返回按时间升序的 (time,o,h,l,c,vol,amount)。"""
    code = symbol.lower()
    with _conn() as c:
        rows = c.execute('SELECT time,open,high,low,close,volume,amount FROM minute_5m '
                         'WHERE symbol=? AND date=? ORDER BY time', (code, date)).fetchall()
    if len(rows) >= BARS_PER_DAY - 4:
        return rows
    fetched = fetch_day(code, date)
    if fetched is None:
        return rows
    with _conn() as c:
        return c.execute('SELECT time,open,high,low,close,volume,amount FROM minute_5m '
                         'WHERE symbol=? AND date=? ORDER BY time', (code, date)).fetchall()


def session_price(rows: List[tuple], start_hhmm: str, end_hhmm: str) -> Optional[float]:
    """一段时间内的价格加权均价（≈ 在这段时间内下单的真实成交均价）。

    baostock 的 time 是 'YYYYMMDDHHMMSS000'，**标的是这一段结束的时刻**：
    '093500' 这根覆盖 9:30~9:35 的成交，'150000' 这根覆盖 14:55~15:00。
    所以取"9:30~10:00"应该是 ('093500','100000')，两端都含 —— 写成
    ('093000','100000') 会一个根都取不到（这是最容易踩的错位，别改回去）。

    权重用成交量而不是 amount：价格是前复权、amount 是未复权原始额，混用会串尺度。
    """
    sel = []
    for r in rows:
        hm = str(r[0])[8:14]
        if start_hhmm <= hm <= end_hhmm:
            price, vol = r[4], r[5]
            if vol and vol > 0:
                sel.append((price, vol))
    if not sel:
        return None
    num = sum(p * v for p, v in sel)
    den = sum(v for _, v in sel)
    return num / den if den else None


def stats() -> dict:
    with _conn() as c:
        n = c.execute('SELECT COUNT(1) FROM minute_5m').fetchone()[0]
        s = c.execute('SELECT COUNT(DISTINCT symbol) FROM minute_5m').fetchone()[0]
        d = c.execute('SELECT COUNT(DISTINCT date) FROM minute_5m').fetchone()[0]
        lo = c.execute('SELECT MIN(date) FROM minute_5m').fetchone()[0]
        hi = c.execute('SELECT MAX(date) FROM minute_5m').fetchone()[0]
    return {'bars': n, 'symbols': s, 'days': d, 'from': lo, 'to': hi, 'db': DB_PATH}


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser()
    ap.add_argument('--ensure', nargs=2, metavar=('SYMBOL', 'DATE'))
    ap.add_argument('--stats', action='store_true')
    a = ap.parse_args()
    if a.ensure:
        r = fetch_day(a.ensure[0], a.ensure[1])
        print(f'落库 {len(r) if r is not None else "失败"} 行')
    s = stats()
    print(f"分钟库：{s['bars']:,} 行 / {s['symbols']} 只 / {s['days']} 天，{s['from']} ~ {s['to']}")
    logout()
