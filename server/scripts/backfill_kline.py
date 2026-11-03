"""backfill_kline.py - 本地历史日K线库 管理脚本（独立运行，不在 Web 进程内）

用法：
  # 首次回填全市场前复权日K（约 12 分钟，断点续传）
  python scripts/backfill_kline.py --mode backfill

  # 每日增量补齐（1~2 分钟，重复执行安全）
  python scripts/backfill_kline.py --mode sync

  # 补某一个漏掉的交易日（例如昨天 16:00 的同步没跑成）
  python scripts/backfill_kline.py --mode sync --date 2026-10-15

  # 查看本地库统计
  python scripts/backfill_kline.py --mode stats

说明：
  - 数据源 baostock（免费/免注册/无限流），彻底绕开东方财富反爬/限流；
  - 前复权存储使最新价=实盘价，与实时行情拼接无缝；RPS/趋势/评分的区间涨幅从本地库向量化计算；
  - backfill 单线程保守拉取，已入库的自动 skip，中断后可重跑续传。
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import KLINE_HISTORY_DB_PATH, KLINE_HISTORY_START_DATE
from services.data.kline_history import HistoryKlineEngine, _to_pure_code


def _print_stats(eng: HistoryKlineEngine) -> None:
    stats = eng.get_db_stats()
    print("── 本地历史K线库统计 ──")
    print(f"  路径       : {stats['dbPath']}")
    print(f"  股票数     : {stats['symbolCount']}")
    print(f"  K线总行数  : {stats['barCount']}")
    print(f"  最早交易日 : {stats['earliestDate']}")
    print(f"  最新交易日 : {stats['latestDate']}")


def main() -> None:
    # 手工跑这个脚本的终端默认是 GBK，而盘后自检的告警文案里有非 GBK 字符——2026-11-02 21:45
    # 实测：coverage_report 打印 "⚠" 时 print 自己抛 UnicodeEncodeError，把"看得见的告警"
    # 变成"看不见的崩溃"，收尾自检整段没跑完。stdout 统一收到 UTF-8 并 errors='replace'，
    # 打印本身永远不该是故障点。
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception as e:
        print(f"[启动] stdout 编码切换失败（不影响本次运行）: {type(e).__name__}: {e}")

    parser = argparse.ArgumentParser(description="本地历史日K线库管理（baostock 前复权）")
    parser.add_argument("--mode", choices=["backfill", "sync", "stats"], required=True,
                        help="backfill=全量回填; sync=每日增量; stats=查看统计")
    parser.add_argument("--db-path", default=KLINE_HISTORY_DB_PATH, help="历史库路径（默认 config.KLINE_HISTORY_DB_PATH）")
    parser.add_argument("--start-date", default=KLINE_HISTORY_START_DATE, help="回填起点 YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=0, help="仅回填前 N 只（调试用，0=全市场）")
    parser.add_argument("--date", default=None,
                        help="sync 模式专用：只补到这一个交易日（用于补昨天漏掉的一天），默认今天")
    parser.add_argument("--fill-dates", default=None,
                        help="sync 模式专用：点名回填这些交易日（逗号分隔，如 2026-11-03）。"
                             "判定是「这一天在不在」，不像 --date 那样只看每只票的最新日期，"
                             "所以能补中间漏掉的一天；非交易日（周末/节假日）会被交易日历"
                             "当场跳过，不会对着它发起全市场查询")
    args = parser.parse_args()

    eng = HistoryKlineEngine(args.db_path)
    try:
        if args.mode == "stats":
            _print_stats(eng)
        elif args.mode == "sync":
            fills = [x.strip() for x in (args.fill_dates or "").split(",") if x.strip()]
            written = eng.sync_today_bulk(args.date, fill_dates=fills or None)
            print(f"增量同步写入 {written} 条"
                  + (f"（点名回填 {','.join(fills)}）" if fills else
                     (f"（补到 {args.date}）" if args.date else "")))
            _print_stats(eng)
        elif args.mode == "backfill":
            symbols = eng.get_all_symbols()
            if not symbols:
                print("[回填] 无法获取股票列表（baostock 登录失败？），退出")
                sys.exit(1)
            if args.limit > 0:
                symbols = symbols[: args.limit]
            print(f"[回填] 待处理 {len(symbols)} 只，起点 {args.start_date} ...")
            res = eng.backfill(symbols=symbols, start_date=args.start_date)
            print(f"[回填] 结果: 成功{res['success']} 跳过{res['skipped']} 失败{res['failed']} 写入{res['written']}条")
            _print_stats(eng)
    finally:
        eng.close()


if __name__ == "__main__":
    main()
