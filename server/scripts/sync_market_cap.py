"""sync_market_cap.py - 手动刷新总市值表（2026-10-16，防量化操控小微盘）

用法：
  python scripts/sync_market_cap.py            # 断点续传刷新（只补缺失，主板+非ST 约3000只）
  python scripts/sync_market_cap.py --full     # 全量重刷（忽略已入库）
  python scripts/sync_market_cap.py --stats    # 只看表现状，不拉取

背景：选票/K线同步/分时线据此剔除总市值 < 35 亿的小微盘，防被少量资金操控形态。
市值只从东财单只 stock/get 拿（clist/ulist 批量都不返回 f116），故低并发+每请求小睡避开限流，
默认断点续传——被限流下次接着补，不丢进度。平时由 scheduler 每周自动跑；
这个脚本用于首灌 / 限流后手动补 / 看现状。数据层逻辑见 services/market_cap.py。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from services.data.market_cap import cap_map, count_below, describe, refresh
from app.config import MIN_TOTAL_MARKET_CAP


def main():
    full = '--full' in sys.argv
    stats = '--stats' in sys.argv
    if not stats:
        r = refresh(only_missing=not full)
        print(f"结果：新入库 {r['ok']}，未取到(停牌/指数/限流) {r['fail']}，本次清单 {r['total']}")
    cm = cap_map()
    print(f"市值表：{len(cm)} 只；下限 {MIN_TOTAL_MARKET_CAP/1e8:.0f} 亿之下 {count_below()} 只")
    print(describe())


if __name__ == '__main__':
    main()
