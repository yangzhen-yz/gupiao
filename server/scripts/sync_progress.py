"""sync_progress.py - 看一眼"补某天的日K"到底跑到哪了、是不是挂了（2026-10-16）

为什么要有这个文件：2026-10-15 深夜那次手工同步写到 2000 只就再也不动，而 stdout 一个字
都没打印 —— 从外面看它"还在跑"，实际上早就僵在 baostock 的长连接上。补数据这件事一旦
看不见进度，就等于没有进度。

用法：
  python scripts/sync_progress.py              # 打一次当前状态
  python scripts/sync_progress.py --watch      # 每 60 秒打一行，同步进程一退出就停
  python scripts/sync_progress.py --date 2026-10-15 --watch
"""
import argparse
import glob
import os
import sqlite3
import subprocess
import sys
import time
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# 不要把本机绝对路径和本机 Python 写死：换机器/换账号就直接跑不起，
# 还把 Windows 用户名留在仓库里（sys.executable 就是"当前这个解释器"，最准）。
SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(SERVER, 'data', 'history_kline.db')
EXE = sys.executable


def sync_procs():
    """当前在跑的 backfill_kline 进程 pid 列表（只认命令行里带 backfill_kline.py 的）"""
    out = subprocess.run([EXE, '-c',
                          "import psutil;"
                          "print('|'.join(str(p.pid) for p in psutil.process_iter(['pid','name','cmdline'])"
                          " if (p.info['name'] or '').lower()=='python.exe'"
                          " and any('backfill_kline.py' in c for c in (p.info['cmdline'] or []))"
                          " and not any('psutil' in c for c in (p.info['cmdline'] or []))))"],
                         capture_output=True, text=True,
                         creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
    return [int(x) for x in out.split('|') if x.isdigit()]


def coverage(day):
    """(当天有K线的选股范围内只数, 选股范围总数, 库内最新日, 整库只数)

    分母从 2026-10-16 起改成**选股范围**（主板 + 非ST/退 + 总市值≥35亿）：
    用整库 5222 当分母，"755 只"会被读成 14%，而在真能买的 2462 只里其实是 31% ——
    同一个数字两套口径，看进度的人判断不出严重程度。范围外的票已不再增量同步、只冻结存量。
    """
    sys.path.insert(0, SERVER)
    import sqlite3 as _sql
    with _sql.connect(DB) as c:
        n_all = c.execute("select count(distinct symbol) from stock_daily where date=?", (day,)).fetchone()[0]
        mx = c.execute("select max(date) from stock_daily").fetchone()[0]
        tot = c.execute("select count(distinct symbol) from stock_daily").fetchone()[0]
        day_syms = {r[0] for r in c.execute(
            "select distinct symbol from stock_daily where date=?", (day,)).fetchall()}
    try:
        from services.data import universe
        scope = set(universe.eligible_symbols())
        return len(day_syms & scope), len(scope) or tot, mx, tot
    except Exception as e:
        print(f'（读不到选股范围，退回整库分母: {e}）')
        return n_all, tot, mx, tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=time.strftime('%Y-%m-%d'))
    ap.add_argument('--watch', action='store_true')
    ap.add_argument('--gap', type=int, default=60)
    a = ap.parse_args()
    while True:
        n, uni, mx, tot = coverage(a.date)
        pids = sync_procs()
        tail = ''
        logs = sorted(glob.glob(os.path.join(SERVER, 'sync_*.log')))
        if logs:
            log = logs[-1]
            with open(log, 'rb') as f:
                f.seek(max(0, os.path.getsize(log) - 400))
                tail = os.path.basename(log) + ' | ' + f.read().decode('gbk', 'replace').strip().splitlines()[-1]
        pct = (n / uni * 100) if uni else 0
        print(f'[{time.strftime("%H:%M:%S")}] {a.date} 选股范围(主板非ST·市值≥35亿)内覆盖 '
              f'{n}/{uni} = {pct:.1f}% | 库内最新 {mx} | 整库 {tot} 只（含范围外存量，已冻结）| '
              f'同步进程 {pids or "无"} | {tail}')
        if not a.watch:
            return 0
        if not pids:
            print('同步进程已退出，停止观察')
            return 0
        time.sleep(a.gap)


if __name__ == '__main__':
    sys.exit(main())
