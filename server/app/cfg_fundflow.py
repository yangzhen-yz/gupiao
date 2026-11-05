import json
import os
from datetime import datetime

# ========== 资金流向 Tab 配置（2026-07-16 新增） ==========
FUND_FLOW_STOCK_TOP_N = 50          # 个股主力净流入/净流出排名各取前 N 只
FUND_FLOW_SECTOR_TOP_N = 30         # 板块（行业/概念）净流入/净流出排名各取前 N 个（≥30 以支撑前端每页 10 条翻页）
FUND_FLOW_HIDDEN_CHANGE_THRESHOLD = 3.0   # “非热点但上涨不错”的最低涨幅阈值（%）
FUND_FLOW_HIDDEN_HOT_TOP_N = 30     # 排除热搜榜前 N 名（视为“近期热点”）
FUND_FLOW_HIDDEN_TOP_N = 50         # “非热点上涨股”最多返回 N 只
FUND_FLOW_CACHE_TTL = 20            # 资金流向汇总接口内存缓存有效期（秒）——按设计**只该管盘中**
FUND_FLOW_CLOSED_CACHE_TTL = 7200   # 收盘后的内存缓存有效期（秒，2 小时），与 fundflow._STALE_AFTER_SEC 对齐。
                                    # 不写成"无限"的两个理由：盘后资金流仍有修正数据；以及收盘后第一次
                                    # 取到的可能是盘中某一刻的数，2 小时后该重新走一遍取数判新鲜
FUND_FLOW_CACHE_TTL_FIX = True      # 2026-11-03 修「盘中/收盘后判据写反」的总开关。
                                    # False＝逐字节回到改造前（含盘中 TTL 不生效那个症状），见 fundflow.py 命中判断处



__all__ = ['FUND_FLOW_CACHE_TTL', 'FUND_FLOW_CACHE_TTL_FIX', 'FUND_FLOW_CLOSED_CACHE_TTL', 'FUND_FLOW_HIDDEN_CHANGE_THRESHOLD', 'FUND_FLOW_HIDDEN_HOT_TOP_N', 'FUND_FLOW_HIDDEN_TOP_N', 'FUND_FLOW_SECTOR_TOP_N', 'FUND_FLOW_STOCK_TOP_N']
