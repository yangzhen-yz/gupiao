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
    parser = argparse.ArgumentParser(description="本地历史日K线库管理（baostock 前复权）")
    parser.add_argument("--mode", choices=["backfill", "sync", "stats"], required=True,
                        help="backfill=全量回填; sync=每日增量; stats=查看统计")
    parser.add_argument("--db-path", default=KLINE_HISTORY_DB_PATH, help="历史库路径（默认 config.KLINE_HISTORY_DB_PATH）")
    parser.add_argument("--start-date", default=KLINE_HISTORY_START_DATE, help="回填起点 YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=0, help="仅回填前 N 只（调试用，0=全市场）")
    parser.add_argument("--date", default=None,
                        help="sync 模式专用：只补到这一个交易日（用于补昨天漏掉的一天），默认今天")
    args = parser.parse_args()

    eng = HistoryKlineEngine(args.db_path)
    try:
        if args.mode == "stats":
            _print_stats(eng)
        elif args.mode == "sync":
            written = eng.sync_today_bulk(args.date)
            print(f"增量同步写入 {written} 条" + (f"（补到 {args.date}）" if args.date else ""))
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
