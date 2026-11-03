"""kline_history.py - 本地历史日K线库（A 级优化：根治东方财富限流 + 向量化 RPS）

设计目标（借鉴 Sequoia-X V2）：
  1. 用 baostock 把**选股范围内**（主板 + 非ST/退 + 总市值≥下限）的前复权日K线落到本地 SQLite，
     绕开东方财富反爬/限流；
     ⚠️ baostock 并非"无限流"：2026-10-16 19:40 被判定「黑名单用户」(10001011)，
     诱因是失败重试里逐只 logout/login。故本模块的纪律是**一次登录跑完一批**，
     连续失败达阈值立刻收工（见 BaostockBlocked）。
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
    BAOSTOCK_BAN_COOLDOWN_MIN,
    KLINE_HISTORY_DB_PATH,
    KLINE_HISTORY_START_DATE,
    KLINE_MAX_CONSECUTIVE_FAILS,
    KLINE_RECONNECT_EVERY,
    KLINE_SYNC_UNIVERSE_ONLY,
)


class BaostockBlocked(RuntimeError):
    """baostock 拒绝服务（黑名单/限流/登录失败）时抛出，用于**立刻终止整批**而不是逐只重试。

    2026-10-16 19:40 实测教训：旧版 `_fetch_one_bs` 每次失败都 `logout()+login()`，
    一只票最多 3 次 —— 一旦上游开始报错，2000 多只票就是几千次登录，正好踩中 baostock 的
    反滥用规则，错误码 10001011「黑名单用户，请与管理员联系」。之后每只票都在 login failed
    上空转，整晚进度归零。**被限流时继续敲门只会让门开得更晚**，所以这条必须早早退出。
    """


def _is_blocked_msg(msg: str) -> bool:
    m = str(msg or '')
    return ('黑名单' in m) or ('10001011' in m) or ('login failed' in m.lower())


# ───────────────────────── baostock 封禁冷却（跨进程） ─────────────────────────
# 为什么必须落文件而不是只记在内存里：调度器跑在 `run.py` 进程里，手工补数是另一个进程，
# 18:15/18:45/19:15/19:45/20:15 五个重试时刻又各自可能登录 —— 没有一份共享状态，
# 被封之后就是五个进程排队去撞同一扇锁上的门，每撞一次封禁就可能续一秒。
_BAN_FILE = os.path.join(os.path.dirname(os.path.abspath(KLINE_HISTORY_DB_PATH)), 'baostock_ban.json')


def note_ban(reason: str, minutes: Optional[float] = None) -> None:
    """记录"刚刚被 baostock 拒了"，让本机所有进程在冷却期内都别敲门。

    minutes=None 用配置的全量冷却（确认拉黑）；疑似限流（连续失败但没拿到黑名单字样）
    用短冷却，避免一次网络抖动就把备路锁 6 小时。
    """
    mins = BAOSTOCK_BAN_COOLDOWN_MIN if minutes is None else minutes
    try:
        import json
        with open(_BAN_FILE, 'w', encoding='utf-8') as f:
            json.dump({'ts': time.time(), 'cooldown_min': mins, 'reason': str(reason)[:300]},
                      f, ensure_ascii=False)
        print(f'[历史K线] 已记 baostock 冷却（{int(mins)} 分钟内不再登录）: {reason}')
    except OSError as e:
        print(f'[历史K线] 封禁状态写失败(不影响本次): {e}')


def ban_cooldown_remaining() -> float:
    """还在冷却期内则返回剩余分钟数，否则 0。文件里的时间戳过期就自动失效。"""
    try:
        import json
        with open(_BAN_FILE, encoding='utf-8') as f:
            d = json.load(f)
            ts = float(d.get('ts') or 0)
            mins = float(d.get('cooldown_min') or BAOSTOCK_BAN_COOLDOWN_MIN)
    except (OSError, ValueError, TypeError):
        return 0.0
    if not ts:
        return 0.0
    left = mins - (time.time() - ts) / 60.0
    return max(0.0, left)


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


def apply_selection_scope(symbols: List[str]) -> List[str]:
    """把待同步清单收窄到"选股范围"（主板 + 非ST/退 + 总市值≥下限）。

    2026-10-16 用户口径："所有的选股条件默认：沪深主板、非ST和*ST、市值大于35亿"。
    数据层以前只听市值那一层（5222 → 4632），所以每晚仍要花 ~2160 次查询去同步
    创业板/科创板/北交所/ST —— 那些票在选票环节 100% 被剔除，历史却是整库口径，
    于是"覆盖率 755/5222"和"池子从 2462 只里选"是两套分母，看日志的人判断不出严重程度。
    收窄后同步清单 = 覆盖率分母 = 选票范围，三处同一口径（实测 5222 → 2462）。

    范围外的存量数据不删、只冻结（分析脚本 --all-market 仍能回放历史，只是往后不再更新）。
    """
    if not symbols:
        return []
    try:
        from services.data import universe
        if KLINE_SYNC_UNIVERSE_ONLY:
            kept = universe.eligible_symbols(symbols)
        else:
            from services.data import market_cap
            kept = sorted(market_cap.filter_symbols(universe.filter_symbols(symbols)))
        if len(kept) != len(symbols):
            print(f'[历史K线] 选股范围 {universe.describe_full()}：{len(symbols)} → {len(kept)} 只'
                  f'（范围外不再增量同步，存量冻结）')
        return kept
    except Exception as e:
        print(f'[历史K线] 选股范围过滤失败(按原清单继续): {type(e).__name__}: {e}')
        return list(symbols)


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
                return apply_selection_scope(symbols)
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

        left = ban_cooldown_remaining()
        if left > 0:
            print(f'[历史K线] baostock 冷却中（还剩 {int(left)} 分钟），回填不登录。'
                  f'当天K线走主路：python -m services.eod_snapshot')
            return {"success": 0, "skipped": 0, "failed": 0, "written": 0,
                    "aborted": f'baostock 冷却中，剩余 {int(left)} 分钟'}

        start_date = start_date or KLINE_HISTORY_START_DATE
        today_str = date.today().strftime("%Y-%m-%d")

        if symbols is None:
            symbols = self.get_all_symbols()
        if not symbols:
            return {"success": 0, "skipped": 0, "failed": 0, "written": 0}

        max_retries = 3
        reconnect_every = KLINE_RECONNECT_EVERY   # 与增量同步同口径：重登太密会被 baostock 判黑名单

        def _login() -> bool:
            lg = bs.login()
            if lg.error_code != "0":
                print(f"[历史K线] baostock 登录失败: {lg.error_msg}")
                note_ban(f'登录失败 {lg.error_code} {lg.error_msg}')
                return False
            return True

        if not _login():
            return {"success": 0, "skipped": 0, "failed": len(symbols), "written": 0}

        success = skipped = failed = written = consec = 0
        since_reconnect = 0
        aborted = ''
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
                    try:
                        rows = self._fetch_one_bs(symbol, start, today_str, max_retries)
                    except BaostockBlocked as e:
                        aborted = f'baostock 拒绝服务：{e}'
                        note_ban(aborted)
                        break
                    if rows is None:
                        failed += 1
                        consec += 1
                    elif not rows:
                        skipped += 1
                        consec = 0
                    else:
                        written += self._bulk_insert(symbol, rows)
                        success += 1
                        consec = 0
                    if consec >= KLINE_MAX_CONSECUTIVE_FAILS:
                        aborted = (f'连续 {consec} 只拉取失败，判定为被限流/拉黑，立即收工'
                                   f'（已写入部分保留，稍后重跑自动续传）')
                        note_ban(aborted, minutes=60)
                        break
                since_reconnect += 1
                if since_reconnect >= reconnect_every:
                    bs.logout()
                    ok = False
                    for attempt in range(3):
                        time.sleep(10 * (attempt + 1))
                        if _login():
                            ok = True
                            break
                        print(f"[历史K线] 重连第{attempt + 1}次失败，10s×{attempt + 2} 后重试")
                    if not ok:
                        aborted = '重连多次失败，终止回填'
                        print(f"[历史K线] 告警 {aborted}")
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
        print(f"[历史K线] 回填{'中断' if aborted else '完成'} — 成功:{success} 跳过:{skipped} "
              f"失败:{failed} 写入:{written}条"
              + (f'｜本次范围外的 {total - i} 只未处理，稍后重跑自动续传' if aborted else ''))
        return {"success": success, "skipped": skipped, "failed": failed, "written": written,
                "aborted": aborted}

    def sync_today_bulk(self, end_date: Optional[str] = None,
                        fill_dates: Optional[List[str]] = None) -> int:
        """每日增量：对本地已有股票，从各自 last_date+1 补到 end_date（默认今天，前复权）。

        2026-10-15 深夜重写过，原因有两条实测教训：

        1. 旧版没有 socket 超时，baostock 长连接挂住时整个进程就永久停在那儿 —— 昨晚就是这样：
           23:50 起跑，写到 2000 只就再没动过，而 stdout 一个字都不打印，看起来"还在跑"。
           盘后任务最怕的不是失败，是失败得看不见。
        2. 旧版不重连、不报进度，而 backfill 那条路早就有"定期重连 + 进度打印"。
           现在两条路同一套口径，且同步清单先过选股范围（主板 + 非ST/退 + 市值≥下限）。

        end_date：指定要补到哪个交易日（用于补昨天漏掉的一天），None=今天。
        fill_dates：2026-11-02 新增。**按日期点名回填**，判定改成"这一天在不在"，
            而不是"这只票的最新日期到没到"。原来的判定有个静默缺陷：某天的同步整批漏掉
            （机器关机/被限流）之后，第二天起每只票的 max(date) 都已经越过那个洞，
            于是它永远不会被回填——这个缺陷是真的，保留。
            但触发它的那次排查结论是错的：11-02 白天以为"2026-10-31 全市场 0~5 只"
            是个洞，晚上 21:38 单进程点名回填跑满 2464 只、写入 0 条、失败 0 只，
            第二天核对发现 **10-31 是周六**，交易日历判非交易日，全库从来没有过周末的行
            （`select distinct date … weekday()>=5` 返回 0 条）。所以现在 fill_dates
            会先过一遍 trading_calendar，非交易日直接跳过并说明，绝不为了一个不存在的洞
            去做 2464 次查询 + 3~4 次登录（baostock 的登录频率本身就是风控信号）。
        """
        import socket
        import baostock as bs

        left = ban_cooldown_remaining()
        if left > 0:
            print(f'[历史K线] baostock 冷却中（还剩 {int(left)} 分钟），本次不登录。'
                  f'当天K线请走主路：python -m services.eod_snapshot')
            return 0
        # 所有 baostock 套接字默认 30 秒超时：宁可抛异常计入 failed，也不要整晚挂在这里
        socket.setdefaulttimeout(30)
        end_str = end_date or date.today().strftime("%Y-%m-%d")
        local = self._get_local_symbols()
        if not local:
            print("[历史K线] 本地无数据，请先执行 backfill")
            return 0
        # 选股范围（主板 + 非ST/退 + 总市值≥下限）之外的票不再增量同步：范围外的历史行保留、只冻结。
        # 2026-10-16：这一步同时把"覆盖率分母"从整库 5222 收到 2462，与选票/候选池同一口径。
        _scope = set(apply_selection_scope([s for s, _ in local]))
        local = [(s, d) for (s, d) in local if s in _scope]
        # 计算每只的待补区间
        tasks = []
        dropped: List[str] = []
        if fill_dates:
            from services.data.trading_calendar import is_calendar_covered, is_trading_day
            # 非交易日先剔掉：baostock 对周末/节假日返回"成功但 0 行"，而这里的判定是
            # "这一天在不在"，于是会对全市场 2464 只各发一次查询、登录 3~4 次，438 秒
            # 全部空转（2026-11-02 21:38 实测，点名回填 2026-10-31=周六）。
            # 登录频率本身就是 baostock 的风控信号，这种空转不只是浪费。
            real = []
            for d in sorted(set(fill_dates)):
                if is_calendar_covered(d) and not is_trading_day(d):
                    print(f"[历史K线] 点名回填跳过 {d}：交易日历判定为非交易日，库里本来就不该有")
                    dropped.append(d)
                    continue
                real.append(d)
            for d in real:
                have = self._symbols_on(d)
                todo = [s for s, _ in local if s not in have]
                print(f"[历史K线] 点名回填 {d}：已有 {len(have)} 只，待补 {len(todo)} 只")
                tasks += [(s, d, d) for s in todo]
            fill_dates = real
        else:
            for symbol, last_date in local:
                if last_date and last_date >= end_str:
                    continue
                start = end_str
                if last_date:
                    start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")
                tasks.append((symbol, start, end_str))

        if not tasks:
            if dropped:
                # 点名回填的日期全被判成非交易日，这里必须说清楚，别让"都已齐"冒充成功
                why = f'点名回填的 {",".join(dropped)} 都被判为非交易日，未发起任何 baostock 查询'
            elif fill_dates:
                why = '点名回填的日期都已齐'
            else:
                why = '所有股票已是最新（补到 %s）' % end_str
            print(f"[历史K线] {why}，无需增量更新")
            self.coverage_report()
            return 0

        lg = bs.login()
        if lg.error_code != "0":
            print(f"[历史K线] baostock 登录失败（{lg.error_code} {lg.error_msg}），增量同步终止")
            note_ban(f'登录失败 {lg.error_code} {lg.error_msg}')
            return 0

        written = failed = consec = 0
        since = 0
        t0 = time.time()
        total = len(tasks)
        aborted = ''
        try:
            for i, (symbol, start, end) in enumerate(tasks, start=1):
                try:
                    rows = self._fetch_one_bs(symbol, start, end, max_retries=2)
                except BaostockBlocked as e:
                    aborted = f'baostock 拒绝服务：{e}'
                    note_ban(aborted)
                    break
                except Exception as e:
                    # 单只异常（含 socket 超时）不该终止当晚整批同步
                    rows = None
                    print(f"[历史K线] {symbol} 拉取异常: {type(e).__name__}: {e}")
                if rows:
                    written += self._bulk_insert(symbol, rows)
                    consec = 0
                elif rows is None:
                    failed += 1
                    consec += 1
                else:
                    consec = 0        # 空列表=停牌/退市但链路正常，不能算失败
                since += 1
                if consec >= KLINE_MAX_CONSECUTIVE_FAILS:
                    aborted = (f'连续 {consec} 只拉取失败，判定为被限流/拉黑，立即收工'
                               f'（逐只继续重试只会让封禁更久）')
                    note_ban(aborted, minutes=60)     # 只是"疑似"限流，给 1 小时短冷却
                    break
                if i % 200 == 0 or i == total:
                    print(f"[历史K线] 增量进度 {i}/{total}（选股范围内待补 {total} 只）"
                          f"写入{written}条 失败{failed}只 耗时{int(time.time() - t0)}s")
                if since >= KLINE_RECONNECT_EVERY:
                    # 长连接会在几千次查询后僵死，要主动断开重连；但间隔从 200 提到 800、
                    # 且重登前先睡 10s/20s/30s —— 2026-10-16 被拉黑的教训：频繁 login 本身就是违规信号
                    bs.logout()
                    for attempt in range(3):
                        time.sleep(10 * (attempt + 1))
                        if bs.login().error_code == "0":
                            break
                    else:
                        aborted = f'重连 3 次仍失败，本次终止（已写入部分保留）'
                        break
                    since = 0
        finally:
            try:
                bs.logout()
            except Exception:
                pass
        if aborted:
            print(f"[历史K线] 告警 增量同步中断 → {aborted}")
        print(f"[历史K线] 增量同步{ '中断' if aborted else '完成' } → 补到 {end_str}：{total} 只待补，"
              f"写入 {written} 条，失败 {failed} 只，耗时 {int(time.time() - t0)}s")
        self.coverage_report()
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
                msg = f'{type(e).__name__}: {e}'
                if _is_blocked_msg(msg):        # 黑名单/登录失败：逐只重试只会更糟
                    raise BaostockBlocked(msg) from e
                if attempt < max_retries - 1:
                    wait = 2 ** (attempt + 1)
                    print(f"[历史K线] [{symbol}] 第{attempt+1}次失败: {msg}，{wait}s 后重试")
                    time.sleep(wait)
                    # 注意：这里**不做** logout/login。2026-10-16 旧版在这里重登，
                    # 上游开始报错时每只票 2~3 次登录、几千只 = 几千次，直接被封成"黑名单用户"。
                    # 链路真断了由调用方按连续失败阈值统一处理（重连或直接收工）。
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

    def get_db_stats(self, with_scope: bool = False) -> Dict:
        """库的基本盘（只数/行数/日期区间）。

        ⚠️ `with_scope` 默认 False：算"选股范围内有多少只 + 当天覆盖几只"要扫一遍
        `stock_daily` 的去重符号并过市值/流动性表，实测 **1.3~1.9 秒**。
        2026-10-17 的事故就是把这两层塞进了默认路径，而 `history_available()` 又在趋势扫描里
        被逐只调用 280 次 —— 同步查询压在 asyncio 主循环上，全量扫描 420 秒、所有页面一起卡。
        需要范围/覆盖的是盘后自检与 `kline-history/stats`，它们显式传 True。
        """
        symbols = self._get_conn().execute("SELECT COUNT(DISTINCT symbol) FROM stock_daily").fetchone()[0] or 0
        bars = self._get_conn().execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0] or 0
        latest = self.get_latest_trade_date()
        earliest = self._get_conn().execute("SELECT MIN(date) FROM stock_daily").fetchone()[0] or None
        stats = {
            "dbPath": self.db_path,
            "symbolCount": symbols,
            "barCount": bars,
            "latestDate": latest,
            "earliestDate": earliest,
        }
        if not with_scope:
            return stats
        universe_n = None
        day_covered = None
        try:
            from services.data import universe
            scope = set(universe.eligible_symbols())
            universe_n = len(scope)
            if latest:
                day = {r[0] for r in self._get_conn().execute(
                    "SELECT DISTINCT symbol FROM stock_daily WHERE date=?", (latest,)).fetchall()}
                day_covered = len(day & scope)
        except Exception:
            pass
        stats.update(universeCount=universe_n, latestDayCovered=day_covered)
        return stats

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

    def load_close_map(self, bars: int = 6) -> Dict[str, List[Tuple[str, float]]]:
        """取最近 bars 个**已收盘**交易日的收盘价：{symbol: [(date, close), ...] 升序}。

        给"买入区间动态带宽"用的近 5 日波动率取数。两条硬性要求：
          · 零网络——只读本地 stock_daily，绝不打行情接口（10-19 刚做完第三方调用降频，
            不能为了算波动率反而多打一轮）；
          · 一次 SQL 捞全市场，不按票循环（几百只票逐票查会把推荐批次拖慢一个量级）。
        """
        dates = [r[0] for r in self._get_conn().execute(
            "SELECT DISTINCT date FROM stock_daily ORDER BY date DESC LIMIT ?",
            (max(int(bars), 2),)).fetchall()]
        if not dates:
            return {}
        cutoff = dates[-1]
        rows = self._get_conn().execute(
            "SELECT symbol, date, close FROM stock_daily WHERE date >= ? ORDER BY symbol, date",
            (cutoff,)).fetchall()
        out: Dict[str, List[Tuple[str, float]]] = {}
        for sym, d, close in rows:
            if close is None:
                continue
            out.setdefault(sym, []).append((d, float(close)))
        return out

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

    def _symbols_on(self, day: str) -> set:
        """某天在本地库里有日线的股票集合（点名回填用它判"缺不缺这一天"）。"""
        rows = self._get_conn().execute(
            "SELECT symbol FROM stock_daily WHERE date=?", (day,)).fetchall()
        return {r[0] for r in rows}

    def coverage_report(self, recent_days: int = 20) -> List[Tuple[str, int]]:
        """最近 N 个交易日的覆盖只数 + "整日缺失"检查。

        同步完顺手打一遍，查两件事：

        1. **矮柱子**：库里出现了这一天但只数明显低于峰值（<90%）——当天同步部分失败；
        2. **整日缺失**（2026-11-02 补）：交易日历说是交易日，库里却一整天没有行。
           只看"库里出现过的日期"永远看不见这种洞，10-31 那次就是这么误判的（见下）。

        2026-11-02 实测更正：**2026-10-31 是周六、非交易日**，`stock_daily` 里
        从来没有过周末的行（全库 0 条），所谓"10-31 全市场 0 只"不是漏同步而是本来就不交易。
        但"整批漏掉一天、第二天起每只票的 max(date) 都已越过那个洞"的判定缺陷是真的，
        所以 `fill_dates` 与本报告都保留，只是都要先过一遍交易日历。
        """
        from services.data.trading_calendar import is_calendar_covered, is_trading_day

        conn = self._get_conn()
        days = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM stock_daily ORDER BY date DESC LIMIT ?",
            (recent_days,)).fetchall()]
        out = []
        for d in sorted(days):
            n = conn.execute("SELECT count(DISTINCT symbol) FROM stock_daily WHERE date=?",
                             (d,)).fetchone()[0]
            out.append((d, n))
        if len(out) >= 3:
            # 基线用**中位**而不是最大值：窗口里混着 10-16 之前的宽口径日子（5222 只），
            # 拿它当峰值会把 10-19 之后每一天真叫"覆盖不足"——2026-11-02 自测实锤。
            # 中位正好落在"现行选股范围"那一档，矮柱子才是真的矮。
            ns = sorted(n for _, n in out)
            base = ns[len(ns) // 2]
            holes = [d for d, n in out if n < base * 0.9]
            if holes:
                print(f"[历史K线] 告警 近 {recent_days} 个交易日里有 {len(holes)} 天覆盖不足 "
                      f"（窗口中位 {base} 只，缺口日 {holes[:5]}{'…' if len(holes) > 5 else ''}）"
                      f"，用 `--mode sync --fill-dates 日期` 点名回填")
        # 整日缺失：以最新一天为右端，往回取 recent_days 个"日历上的交易日"，看库里在不在。
        # 只在库已覆盖到的区间里查（min(days) 之前不算缺失），否则新库回填到一半
        # 会把"还没回填的历史"报成"漏同步的洞"。
        if days:
            newest = max(days)
            oldest = min(days)
            cur = date.fromisoformat(newest)
            expect, guard = [], 0
            while len(expect) < recent_days and guard < recent_days * 4:
                guard += 1
                if cur.strftime('%Y-%m-%d') < oldest:
                    break
                if is_calendar_covered(cur) and is_trading_day(cur):
                    expect.append(cur.strftime('%Y-%m-%d'))
                cur -= timedelta(days=1)
            missing = [d for d in expect if d not in set(days)]
            if missing:
                print(f"[历史K线] 告警 近 {len(expect)} 个交易日里 {len(missing)} 天一天都没有："
                      f"{sorted(missing)[:5]}（库里整日缺，用 "
                      f"`--mode sync --fill-dates {','.join(sorted(missing))}` 补）")
            elif expect:
                print(f"[历史K线] 覆盖自检：近 {len(expect)} 个交易日一天不缺，"
                      f"最少一天 {min(out, key=lambda x: x[1])[0]} 有 {min(n for _, n in out)} 只")
        return out


_VOL_MAP_CACHE: Dict[str, object] = {'ts': 0.0, 'bars': 0, 'map': {}}


def get_vol_close_map(bars: Optional[int] = None) -> Dict[str, List[float]]:
    """全市场「近 bars 个已收盘交易日收盘价序列」：{6位代码: [close 升序]}，进程内 TTL 缓存。

    给买入区间的动态带宽（`buy_range.recent_volatility`）喂数。三条纪律写在这里：
      · **零网络**：只读本地 stock_daily；10-19 刚把东财/新浪的请求量砍下来，
        不能为了算波动率再开一条每轮全量外部请求；
      · **一次查询捞全市场**，绝不逐票查（几百只票逐票查 = 把推荐批次拖慢一个量级，
        这正是 history_available 那次踩过的坑）；
      · 波动率是日级慢变量，一天只变一次 → 缓存 BUY_RANGE_VOL_CACHE_SEC，盘中各批共用。
    库不存在/没回填时返回 {}，调用方据此退回核心带宽（不硬造波动率）。
    """
    from app.config import BUY_RANGE_VOL_BARS, BUY_RANGE_VOL_CACHE_SEC
    n = int(bars or BUY_RANGE_VOL_BARS) + 1          # N 个收益要 N+1 个收盘
    now = time.time()
    # 命中条件只看"上次尝试过 + 窗口相同 + 未过期"，**不要求非空**：
    # 库里没回填时若按空值反复重建，批量链路逐票调用会把一次全表查询放大成几百次
    # （这正是 2026-10-17 history_available 那个坑的翻版——同步查询压在事件循环上）。
    if (_VOL_MAP_CACHE['bars'] == n
            and now - float(_VOL_MAP_CACHE['ts']) < BUY_RANGE_VOL_CACHE_SEC):
        return _VOL_MAP_CACHE['map']       # type: ignore[return-value]
    _VOL_MAP_CACHE['ts'] = now             # 先记时刻：失败也不允许下一只票再来一次
    try:
        raw = get_engine().load_close_map(bars=n)
    except Exception as e:
        print(f'[波动率] 本地日K读取失败（带宽退回核心值，10 分钟内不再重试）: {e}')
        return _VOL_MAP_CACHE['map'] or {}       # type: ignore[return-value]
    m = {sym: [c for _, c in seq] for sym, seq in raw.items()}
    _VOL_MAP_CACHE.update({'bars': n, 'map': m})
    return m


def get_vol_closes_for(symbol: str, bars: Optional[int] = None) -> List[float]:
    """单票收盘序列（升序）。传 sh600519/600519 都行；库里没有该票时返回空列表。

    走的是那份全市场缓存，所以单股分析页调用它同样不产生额外查询。
    """
    code = _normalize_local_symbol(symbol)      # sh600519/600519 都能命中（库里以纯代码存）
    return get_vol_close_map(bars).get(code) or []


# ───────────────────────── 模块级便捷函数（供 RPS / 调度器调用） ─────────────────────────

_engine_singleton: Optional[HistoryKlineEngine] = None


def get_engine() -> HistoryKlineEngine:
    global _engine_singleton
    if _engine_singleton is None:
        _engine_singleton = HistoryKlineEngine()
    return _engine_singleton


_AVAILABLE_CACHE = {'ts': 0.0, 'value': None}
_AVAILABLE_TTL = 300.0      # 本地库"有没有回填好"这种事实，5 分钟内不会变


def history_available() -> bool:
    """本地历史库是否已回填（有数据）。

    必须缓存：它在趋势扫描里被**逐只**调用（池子 280 只就是 280 次），
    而同步 SQLite 查询压在 asyncio 主循环上会拖慢整个服务（2026-10-17 实测 1.3~1.9 秒/次
    × 280 ≈ 420 秒）。按进程记 5 分钟，一天真正会变的时间点只有盘后同步那一下。
    """
    now = time.time()
    if _AVAILABLE_CACHE['value'] is not None and now - _AVAILABLE_CACHE['ts'] < _AVAILABLE_TTL:
        return _AVAILABLE_CACHE['value']
    try:
        eng = get_engine()
        stats = eng.get_db_stats()
        value = bool(stats["symbolCount"] > 0 and stats["barCount"] > 0)
    except Exception:
        value = False
    _AVAILABLE_CACHE.update(ts=now, value=value)
    return value
