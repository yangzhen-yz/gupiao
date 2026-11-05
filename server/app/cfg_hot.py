import json
import os
from datetime import datetime

# ========== 热搜榜配置（3类合并）==========
# 数据来源：
#   1. 人气榜：东方财富真实个股人气榜 API（emappdata.eastmoney.com/stockrank/getAllCurrentList）
#      — API 最多返回前 100 名，取全部沪深主板
#   2. 涨幅榜：clist API fid=f3（涨跌幅排序），取前 N 只沪深主板
#   3. 成交额榜：clist API fid=f6（成交额排序），取前 N 只沪深主板
# 合并去重后，人气榜股票排前面（hotRank 从 1 开始），涨幅榜独有的排中间，成交额榜独有的排后面
POPULARITY_RANK_TOP_N = 100       # 人气榜取前 N 只（API 上限 100）
CHANGE_RANK_TOP_N = 100           # 涨幅榜取前 N 只沪深主板
TURNOVER_RANK_TOP_N = 400         # 成交额榜取前 N 只沪深主板
HOT_SEARCH_TOP_N = 600            # 热搜榜最大显示数量（3榜合并后上限）
HOT_SEARCH_SNAPSHOT_CLEANUP_DAYS = 60  # 热搜榜快照清理天数



__all__ = ['CHANGE_RANK_TOP_N', 'HOT_SEARCH_SNAPSHOT_CLEANUP_DAYS', 'HOT_SEARCH_TOP_N', 'POPULARITY_RANK_TOP_N', 'TURNOVER_RANK_TOP_N']
