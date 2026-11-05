import json
import os
from datetime import datetime

# ========== 策略决策快照（2026-10-17）==========
# 每批次把"扫了谁、每只评多少分、走到哪一步、为什么被挡、当天什么情绪档"落库，
# 目的只有一个：以后能用**真实决策**回测，而不是拿 r20 代理口径重建。
# 为什么不能回填历史：13 因子吃的是一盘中的实时字段（委比/外盘/量比/现价 vs 扫描价），
# 日K库里根本没有这些列，历史每一天只能近似、不能还原。所以从今天开始攒。
STRATEGY_SNAPSHOT_ENABLED = True        # 关掉=停写，推荐链路一字不动
STRATEGY_SNAPSHOT_MAX_ROWS = 900        # 每批次最多写多少只（候选与精选必留，长尾按分数截）
STRATEGY_SNAPSHOT_RETENTION_DAYS = 730  # 保留天数，0=永久（实测 900 行/批 × 4 批 × 250 交易日 ≈ 25 万行/年）
# 每日可成交性监控（2026-10-18，实施计划 P0-0）：16:10 推荐复盘之后跑一次 rec_sim 六种口径，
# 落 rec_sim_daily。存"每天一个点"而不是每次现算，是因为窗口会随日期漂移，
# 现算的两天的数字不可比 —— "放宽动作后 auction 口径有没有恶化"这条判据会当场失效。
REC_SIM_DAILY_ENABLED = True
REC_SIM_DAILY_TIME = '16:20'
REC_SIM_DAILY_MONTHS = 3            # 每天固定回看 3 个月（与用户最初那个问题同口径）


# ========== 预测验证口径（2026-11-03 修①）==========
# `backtest_yesterday_predictions` 主流程原来用**调用那一刻的实时涨跌**当"次日涨跌"。
# 15:20 定时跑是对的，但这个函数也被接口直接调；周末/节假日再调时，腾讯的 changePercent
# 还是上一个交易日的，于是把"预测日自己"的涨跌写成了"次日"。实测 2410 条已验证记录里
# 167 条（6.9%）对不上"次日收盘"：93 条等于预测日自己当天的涨跌（集中在周五那批：
# 06-05/06-26/07-03/07-17）、35 条与前后任何一根都对不上（盘中快照值）、39 条跨多根歧义。
# 判据自测 `analysis_out/verify_kline_selftest.py`。这些行同时喂给准确率统计与因子权重学习。
# True＝与补验路径同一口径（K 线里严格晚于预测日的第一根，取不到就不写）；
# False＝逐字节回到改造前（批量实时行情 + changePercent=0 跳过）。覆盖表可热改。
REC_VERIFY_USE_KLINE = True


__all__ = ['REC_SIM_DAILY_ENABLED', 'REC_SIM_DAILY_MONTHS', 'REC_SIM_DAILY_TIME', 'REC_VERIFY_USE_KLINE', 'STRATEGY_SNAPSHOT_ENABLED', 'STRATEGY_SNAPSHOT_MAX_ROWS', 'STRATEGY_SNAPSHOT_RETENTION_DAYS']
