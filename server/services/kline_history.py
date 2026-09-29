"""kline_history.py - 本地历史日K线库（A 级优化：根治东方财富限流 + 向量化 RPS）

设计目标（借鉴 Sequoia-X V2）：
  1. 用 baostock（免费/免注册/无限流）把全市场前复权日K线落到本地 SQLite，绕开东方财富反爬/限流；
  2. RPS / 趋势 / 评分需要的"历史区间涨幅"全部从本地库向量化计算，秒级，不再逐股现拉腾讯K线；
  3. 选前复权（非后复权）：前复权最新交易日价格 = 实盘价，与实时行情拼接时无缝衔接；
     后复权最新价会被"累计股息因子"放大（≠实盘价），趋势扫描把实盘 check_price 与后复权 MA 比较会误判破位。
     前/后复权对"区间涨幅(收益率)"只差一个常数倍，RPS 百分位排名不受影响，故统一用前复权最稳妥。

为什么不用东方财富 clist 区间参数：实测 push2delay 在带 k1/k2/k3 时 f3 仍返回当日涨跌幅，接口不支持区间统计；
腾讯前复权K线虽能算，但全市场逐股拉会触发限流并把 167s 扫描/流畅度拖垮——本地库是根治方案。

本模块特点：
  - 独立数据库 server/data/history_kline.db（不膨胀主库 gupiao.db）；
  - 纯 Python + sqlite3，不引入 pandas（保持 Docker 镜像精简）；
  - backfill 单线程保守拉取，自动重试 + 定期重连 + 断点续传；
  - sync_today_bulk() 每日增量补齐，耗时 1~2 分钟。
"""
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from app.config import (
    KLINE_HISTORY_DB_PATH,
    KLINE_HISTORY_START_DATE,
)


def _to_baostock_code(symbol: str) -> str:
    """纯数字代码 → baostock 格式：6/9 开头→sh，8/4 开头→bj，其余→sz。"""
    if symbol.startswith(("6", "9")):
        return f"sh.{symbol}"
    if symbol.startswith(("8", "4")):
        return f"bj.{symbol}"
    return f"sz.{symbol}"


def _to_pure_code(bs_code: str) -> str:
    """baostock 格式 sh.600000 → 600000。"""
    return bs_code.split(".")[-1]


def _normalize_local_symbol(symbol: str) -> str:
    """本地库以纯代码存储（600756）；但调用方（趋势扫描/评分）传入的多是带交易所前缀的
    Tencent 格式（sh600756 / sz000063 / bj8xxxxx，带或不带点号）。统一剥掉前缀再查库，
    纯代码原样返回，保证两种格式都能命中本地库（否则全部误判为"本地缺"掉进腾讯实时兜底）。

    注意顺序：先匹配带点号 'sh.'/'sz.'/'bj.'，再匹配无点号 'sh'/'sz'/'bj'，避免误切。
    """
    s = symbol.strip().lower()
    for p in ("sh.", "sz.", "bj.", "sh", "sz", "bj"):
        if s.startswith(p):
            return s[len(p):]
    return s


# 公共别名：调用方（routes.py 等）用它把带前缀 symbol 规整为本地库纯代码，做 RPS 等取数/查表匹配
normalize_local_symbol = _normalize_local_symbol


class HistoryKlineEngine:
    """本地历史日K线引擎（前复权）。

    仅依赖 baostock（回填/增量/股票列表）与 sqlite3；RPS 向量化计算走纯 Python，
    不依赖 pandas。
    """

    def __init__(self, db_path: str = KLINE_HISTORY_DB_PATH) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()  # 写操作串行化（读可并发）
        # 每线程独立连接（threading.local）：引擎单例被广泛复用（RPS 向量化、趋势扫描历史K线、
        # 调度器增量同步均在 asyncio.to_thread 工作线程中读取），若共用一条连接会触发 SQLite
        # 跨线程错误被静默吞掉 → 静默回退腾讯。每线程一条连接从根上规避。
        self._local = threading.local()
        self._init_db()

    # ───────────────────────── 库初始化 ─────────────────────────

    def _init_db(self) -> None:
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS stock_daily (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol  TEXT    NOT NULL,
                    date    TEXT    NOT NULL,
                    open    REAL,
                    high    REAL,
                    low     REAL,
                    close   REAL,
                    volume  REAL,
                    turnover REAL,
                    UNIQUE (symbol, date)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sd_symbol_date ON stock_daily (symbol, date)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS stock_basic ("
                "  symbol TEXT PRIMARY KEY,"
                "  name   TEXT"
                ")"
            )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")

    def _get_conn(self):
        """返回当前线程专属的 SQLite 连接（惰性创建，线程安全）。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    # ───────────────────────── 数据同步（baostock） ─────────────────────────

    def get_all_symbols(self) -> List[str]:
        """通过 baostock 获取全市场 A 股代码列表（纯数字代码）。失败返回 []。"""
        try:
            import baostock as bs

            if bs.login().error_code != "0":
                print("[历史K线] baostock 登录失败，无法获取股票列表")
                return []
            try:
                rs = bs.query_stock_basic(code_name="", code="")
                symbols: List[str] = []
                while rs.next():
                    row = rs.get_row_data()
                    code = row[0]            # "sh.600000"
                    status = row[4]          # "1" 上市
                    stock_type = row[5]       # "1" 股票
                    if status == "1" and stock_type == "1":
                        symbols.append(_to_pure_code(code))
                return symbols
            finally:
                bs.logout()
        except Exception as e:
            print(f"[历史K线] 获取股票列表失败: {e}")
            return []

    def backfill(self, symbols: Optional[List[str]] = None, start_date: Optional[str] = None) -> Dict:
        """首次/补数据：baostock 拉全市场前复权历史日K（单线程，断点续传 + 重试 + 定期重连）。

        Args:
            symbols: 指定代码列表；为 None 时自动拉全市场。
            start_date: 回填起点 YYYY-MM-DD；默认 KLINE_HISTORY_START_DATE。
        Returns:
            {"success", "skipped", "failed", "written"} 统计。
        """
        import baostock as bs

        start_date = start_date or KLINE_HISTORY_START_DATE
        today_str = date.today().strftime("%Y-%m-%d")

        if symbols is None:
            symbols = self.get_all_symbols()
        if not symbols:
            return {"success": 0, "skipped": 0, "failed": 0, "written": 0}

        max_retries = 3
        reconnect_every = 200  # 每处理 N 只重连一次，防长连接超时

        def _login() -> bool:
            lg = bs.login()
            if lg.error_code != "0":
                print(f"[历史K线] baostock 登录失败: {lg.error_msg}")
                return False
            return True

        if not _login():
            return {"success": 0, "skipped": 0, "failed": len(symbols), "written": 0}

        success = skipped = failed = written = 0
        since_reconnect = 0
        total = len(symbols)
        t0 = time.time()
        try:
            for i, symbol in enumerate(symbols, start=1):
                last_date = self._get_last_date(symbol)
                if last_date and last_date >= today_str:
                    skipped += 1
                else:
                    start = last_date or start_date
                    if last_date:
                        start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")
                    rows = self._fetch_one_bs(symbol, start, today_str, max_retries)
                    if rows is None:
                        failed += 1
                    elif not rows:
                        skipped += 1
                    else:
                        written += self._bulk_insert(symbol, rows)
                        success += 1
                since_reconnect += 1
                if since_reconnect >= reconnect_every:
                    bs.logout()
                    ok = False
                    for attempt in range(5):
                        time.sleep(2 + attempt)
                        if _login():
                            ok = True
                            break
                        print(f"[历史K线] 重连第{attempt + 1}次失败，{2 + attempt}s 后重试")
                    if not ok:
                        print("[历史K线] 重连多次失败，终止回填")
                        break
                    since_reconnect = 0
                # 进度日志（每 200 只）
                if i % 200 == 0 or i == total:
                    el = int(time.time() - t0)
                    print(f"[历史K线] 回填进度 {i}/{total}  成功{success} 跳过{skipped} 失败{failed} 写入{written}条  耗时{el}s")
        finally:
            try:
                bs.logout()
            except Exception:
                pass
        print(f"[历史K线] 回填完成 — 成功:{success} 跳过:{skipped} 失败:{failed} 写入:{written}条")
        return {"success": success, "skipped": skipped, "failed": failed, "written": written}

    def sync_today_bulk(self) -> int:
        """每日增量：对本地已有股票，从各自 last_date+1 补到今天（前复权）。返回写入条数。"""
        import baostock as bs

        today_str = date.today().strftime("%Y-%m-%d")
        local = self._get_local_symbols()
        if not local:
            print("[历史K线] 本地无数据，请先执行 backfill")
            return 0
        # 计算每只的待补区间
        tasks = []
        for symbol, last_date in local:
            if last_date and last_date >= today_str:
                continue
            start = today_str
            if last_date:
                start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")
            tasks.append((symbol, start, today_str))

        if not tasks:
            print("[历史K线] 所有股票已是最新，无需增量更新")
            return 0

        if bs.login().error_code != "0":
            print("[历史K线] baostock 登录失败，增量同步终止")
            return 0
        written = 0
        try:
            for symbol, start, end in tasks:
                rows = self._fetch_one_bs(symbol, start, end, max_retries=2)
                if rows:
                    written += self._bulk_insert(symbol, rows)
        finally:
            bs.logout()
        print(f"[历史K线] 增量同步完成，写入 {written} 条（{len(tasks)} 只待补）")
        return written

    def _fetch_one_bs(self, symbol: str, start: str, end: str, max_retries: int = 3) -> Optional[List[Tuple]]:
        """拉单只股票前复权日K。

        Returns:
            [(date, open, high, low, close, volume, turnover), ...]（升序）；失败返回 None。
        """
        import baostock as bs

        bs_code = _to_baostock_code(symbol)
        for attempt in range(max_retries):
            try:
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "date,open,high,low,close,volume,amount",
                    start_date=start,
                    end_date=end,
                    frequency="d",
                    adjustflag="2",  # 2=前复权（最新价=实盘价，与实时行情拼接无缝；1=后复权会因股息因子放大最新价）
                )
                if rs.error_code != "0":
                    raise RuntimeError(rs.error_msg)
                rows = []
                while rs.next():
                    r = rs.get_row_data()
                    # r: [date, open, high, low, close, volume, amount]
                    try:
                        rows.append((
                            r[0],
                            float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                            float(r[5]) if r[5] not in ("", None) else 0.0,
                            float(r[6]) if r[6] not in ("", None) else 0.0,
                        ))
                    except (ValueError, TypeError):
                        continue
                return rows
            except Exception as e:
                if attempt < max_retries - 1:
                    wait = 2 ** (attempt + 1)
                    print(f"[历史K线] [{symbol}] 第{attempt+1}次失败: {e}，{wait}s 后重试")
                    time.sleep(wait)
                    try:
                        bs.logout()
                        time.sleep(1)
                        bs.login()
                    except Exception:
                        pass
                else:
                    print(f"[历史K线] [{symbol}] {max_retries}次重试失败，跳过")
                    return None
        return None

    def _bulk_insert(self, symbol: str, rows: List[Tuple]) -> int:
        """批量写入单只股票的前复权日K（INSERT OR IGNORE，按 (symbol,date) 去重）。

        返回本次实际新增条数（已存在的不重复计）。
        rows: [(date, open, high, low, close, volume, turnover), ...]
        """
        if not rows:
            return 0
        payload = [(symbol, r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in rows]
        with self._lock:
            before = self._get_conn().execute(
                "SELECT COUNT(*) FROM stock_daily WHERE symbol = ?", (symbol,)
            ).fetchone()[0]
            self._get_conn().executemany(
                "INSERT OR IGNORE INTO stock_daily "
                "(symbol, date, open, high, low, close, volume, turnover) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                payload,
            )
            self._get_conn().commit()
            after = self._get_conn().execute(
                "SELECT COUNT(*) FROM stock_daily WHERE symbol = ?", (symbol,)
            ).fetchone()[0]
        return after - before

    # ───────────────────────── 查询（向量化 RPS 输入） ─────────────────────────

    def get_latest_trade_date(self) -> Optional[str]:
        row = self._get_conn().execute("SELECT MAX(date) FROM stock_daily").fetchone()
        return row[0] if row and row[0] else None

    def get_db_stats(self) -> Dict:
        symbols = self._get_conn().execute("SELECT COUNT(DISTINCT symbol) FROM stock_daily").fetchone()[0] or 0
        bars = self._get_conn().execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0] or 0
        latest = self.get_latest_trade_date()
        earliest = self._get_conn().execute("SELECT MIN(date) FROM stock_daily").fetchone()[0] or None
        return {
            "dbPath": self.db_path,
            "symbolCount": symbols,
            "barCount": bars,
            "latestDate": latest,
            "earliestDate": earliest,
        }

    def load_recent_closes(self, period_days: int) -> Tuple[Optional[str], Dict[str, List[Tuple[str, float]]]]:
        """返回 (最新交易日, {symbol: [(date, close), ...]})，仅取最近 period_days+45 天窗口。

        用于向量化 RPS：每个 symbol 取最近一段收盘序列，前端在 Python 内算区间涨幅 + 百分位排名。
        """
        latest = self.get_latest_trade_date()
        if not latest:
            return None, {}
        start = (datetime.strptime(latest, "%Y-%m-%d") - timedelta(days=int(period_days * 1.6) + 30)).strftime("%Y-%m-%d")
        rows = self._get_conn().execute(
            "SELECT symbol, date, close FROM stock_daily WHERE date >= ? ORDER BY symbol, date",
            (start,),
        ).fetchall()
        data: Dict[str, List[Tuple[str, float]]] = {}
        for sym, d, close in rows:
            data.setdefault(sym, []).append((d, close))
        return latest, data

    def get_kline_tencent_format(self, symbol: str, limit: int = 320) -> List[List]:
        """返回与腾讯K线同构的列表：[[date, open, close, high, low, volume], ...]（升序）。

        顺序与 get_kline_data 的 qfqday 完全一致（索引0=date,1=open,2=close,3=high,4=low,5=volume），
        可直接喂给趋势/评分等既存消费者，作为"本地历史"数据源（不含实时拼接，实时由上层追加）。
        """
        symbol = _normalize_local_symbol(symbol)
        rows = self._get_conn().execute(
            "SELECT date, open, high, low, close, volume FROM stock_daily "
            "WHERE symbol = ? ORDER BY date DESC LIMIT ?",
            (symbol, limit),
        ).fetchall()
        out = []
        for d, o, h, l, c, v in rows:
            out.append([d, o, c, h, l, v])
        out.reverse()
        return out

    # ───────────────────────── 内部工具 ─────────────────────────

    def _get_last_date(self, symbol: str) -> Optional[str]:
        row = self._get_conn().execute(
            "SELECT MAX(date) FROM stock_daily WHERE symbol = ?", (symbol,)
        ).fetchone()
        return row[0] if row and row[0] else None

    def _get_local_symbols(self) -> List[Tuple[str, Optional[str]]]:
        return self._get_conn().execute(
            "SELECT symbol, MAX(date) FROM stock_daily GROUP BY symbol"
        ).fetchall()


# ───────────────────────── 模块级便捷函数（供 RPS / 调度器调用） ─────────────────────────

_engine_singleton: Optional[HistoryKlineEngine] = None


def get_engine() -> HistoryKlineEngine:
    global _engine_singleton
    if _engine_singleton is None:
        _engine_singleton = HistoryKlineEngine()
    return _engine_singleton


def history_available() -> bool:
    """本地历史库是否已回填（有数据）。"""
    try:
        eng = get_engine()
        stats = eng.get_db_stats()
        return stats["symbolCount"] > 0 and stats["barCount"] > 0
    except Exception:
        return False
