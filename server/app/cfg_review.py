import json
import os
from datetime import datetime

# ========== 数据查询限制 ==========
QUERY_DAILY_RECOMMENDATIONS_LIMIT = 300  # 历史推荐记录保留天数（load 取最新 N 天 / save 截断到 N 天，均按天数语义）
QUERY_TREND_RESULTS_LIMIT = 200  # 趋势结果数量限制
QUERY_MARKET_REVIEWS_LIMIT = 30  # 市场评论数量限制


# ========== 收评配置 ==========
REVIEW_TOP_GAINERS_COUNT = 15  # 顶部涨股票数量限制
REVIEW_TOP_LOSERS_COUNT = 15  # 顶部跌股票数量限制
REVIEW_TOP_SECTORS_COUNT = 5  # 顶部行业数量限制
REVIEW_TOMORROW_FOCUS_COUNT = 6  # 明日关注最多几只（2026-10-14：从 10 收到 6，按历史胜率选而非按来源顺序凑）
# 入选门槛：该来源该分数段的历史 T+1 胜率 ≥ 50% 且平均收益 > 0。
# 50% 就是抛硬币线 —— 低于它的票进名单只会把整篮子的胜率拉下去（用户明确要"提示整体胜率"）。
# 实测：趋势扫描各档平均收益全为负（整体 42.1% / -0.40%，n=1335），因此整层被挡在门外。
REVIEW_TOMORROW_FOCUS_MIN_WIN_RATE = 50.0
# 弱市里可能一只都过不了门槛，留个下限保证名单不空白（按胜率从高到低补）
REVIEW_TOMORROW_FOCUS_MIN_KEEP = 3



__all__ = ['QUERY_DAILY_RECOMMENDATIONS_LIMIT', 'QUERY_MARKET_REVIEWS_LIMIT', 'QUERY_TREND_RESULTS_LIMIT', 'REVIEW_TOMORROW_FOCUS_COUNT', 'REVIEW_TOMORROW_FOCUS_MIN_KEEP', 'REVIEW_TOMORROW_FOCUS_MIN_WIN_RATE', 'REVIEW_TOP_GAINERS_COUNT', 'REVIEW_TOP_LOSERS_COUNT', 'REVIEW_TOP_SECTORS_COUNT']
