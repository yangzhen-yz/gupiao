"""app_settings.py - 运行时可改的配置覆盖表（2026-10-17）

用户要求：**"目前好多配置都是在代码中直接写死的"**，希望把这些开关放进复盘的子页面里改。

为什么不能简单地"UI 写 config.py"
--------------------------------
`app/config.py` 里的值在各模块是 `from app.config import X` **导入时绑定**的，
进程一启动就把数值抄进了自己的命名空间；UI 改了文件，正在跑的代码**读到的还是旧值**。
所以这里做成一张 DB 覆盖表：`config.py` 是**默认值**，`app_settings` 是**运行时覆盖**，
真正需要热生效的那几项改成"用的时候现读"（`value(key)`），其余仍然标"改完要重启"。

诚实的边界（界面也会照这个显示）
--------------------------------
  · `live=True` 的项：本进程改完立刻生效（同一进程内缓存会被 `invalidate()` 打掉）。
    另起的脚本进程（backfill、分析脚本）在**它下次启动时**才读到新值，这是进程模型的必然，
    不是 bug。
  · `live=False` 的项：改动只写进表里，等进程重启才生效 —— 绝不假装"已生效"，
    否则用户拨了开关、行为没变，比不给开关更糟。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

import app.config as cfg
from db.database import get_db_conn

_DDL = '''CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT,
    updated_by TEXT
)'''

# 注册表：只有列在这里的项能被界面改。每一项都说清"是什么、范围多少、改了影响谁"。
#   kind: bool | int | float
#   cfg : app/config.py 里的默认值变量名（改了默认值不用动这里）
#         没有 cfg 的项用 'default' 直接给出厂默认值（2026-10-24 起，纯界面项走这条）
#   live: 是否已经做成"用的时候现读"（见模块文档）
#   impact: 影响预览函数名（在 _preview_* 里实现），None 就只显示说明
SPEC: List[Dict[str, Any]] = [
    # 2026-10-24：用户要求"别把默认 60 写死在 config.py，直接在配置页改"。
    # 出厂默认留在这里（60），运行时以 app_settings 覆盖表为准；/api/tab-scan-config 每次现读。
    {
        'key': 'tab_scan_default_min_score', 'default': 60, 'kind': 'int',
        'label': '筛选页「自定义最低分」默认值', 'group': '股票筛选页', 'live': True,
        'min': 0, 'max': 100, 'step': 1, 'unit': '分',
        'note': '股票筛选 Tab 打开时，过滤条里的最低分取这个值；在筛选页直接改数字也会写回这里'
                '（只有管理员能改），刷新和手机端都按它显示。0~100，常用 40~70。',
    },
    {
        'key': 'universe_main_board_only', 'cfg': 'UNIVERSE_MAIN_BOARD_ONLY', 'kind': 'bool',
        'label': '只做沪深主板', 'group': '选股范围', 'live': True,
        'note': '关掉后创业板/科创板/北交所一起进入范围；注意它们的涨跌停是 20%/30%，'
                '封板类判定阈值口径会变',
        'impact': 'scope',
    },
    {
        'key': 'universe_exclude_st', 'cfg': 'UNIVERSE_EXCLUDE_ST', 'kind': 'bool',
        'label': '剔除 ST/*ST/退市整理', 'group': '选股范围', 'live': True,
        'note': 'ST 是 5% 板且带退市风险，历史上"5 日内大面率"最高的一类',
        'impact': 'scope',
    },
    {
        'key': 'market_cap_enabled', 'cfg': 'MARKET_CAP_FILTER_ENABLED', 'kind': 'bool',
        'label': '启用总市值下限', 'group': '选股范围', 'live': True,
        'note': '小盘流通盘小、易被少量资金拉出形态，这条是防被"做出来"的K线骗',
        'impact': 'scope',
    },
    {
        'key': 'min_market_cap_yi', 'cfg': 'MIN_TOTAL_MARKET_CAP', 'kind': 'float',
        'label': '总市值下限（亿元）', 'group': '选股范围', 'live': True,
        'min': 5, 'max': 500, 'step': 5, 'unit': '亿', 'scale': 1e8,
        'note': '市值表每周日刷一次；表里没有的票按放行处理',
        'impact': 'scope',
    },
    {
        'key': 'liquidity_enabled', 'cfg': 'LIQUIDITY_FILTER_ENABLED', 'kind': 'bool',
        'label': '启用流动性三条（总闸）', 'group': '选股范围', 'live': True,
        'note': '关掉总闸 = 下面三条一起不生效',
        'impact': 'scope',
    },
    {
        'key': 'liq_amount_enabled', 'cfg': 'LIQ_AMOUNT_ENABLED', 'kind': 'bool',
        'label': '近20日日均成交额这一条', 'group': '选股范围', 'live': True,
        'note': '成交额不够的票，形态再好也挂不进单、出不来',
        'impact': 'scope',
    },
    {
        'key': 'liq_min_avg_amount_wan', 'cfg': 'LIQ_MIN_AVG_AMOUNT', 'kind': 'float',
        'label': '日均成交额下限（万元）', 'group': '选股范围', 'live': True,
        'min': 1000, 'max': 200000, 'step': 1000, 'unit': '万', 'scale': 1e4,
        'note': '8000 万是 2026-10-16 定的口径',
        'impact': 'scope',
    },
    {
        'key': 'liq_turnover_enabled', 'cfg': 'LIQ_TURNOVER_ENABLED', 'kind': 'bool',
        'label': '近20日日均换手率这一条', 'group': '选股范围', 'live': True,
        'note': '六条里收口最紧的一条（1094 只就是它筛出来的）',
        'impact': 'scope',
    },
    {
        'key': 'liq_min_avg_turnover_pct', 'cfg': 'LIQ_MIN_AVG_TURNOVER_PCT', 'kind': 'float',
        'label': '日均换手率下限（%）', 'group': '选股范围', 'live': True,
        'min': 0.2, 'max': 20, 'step': 0.1, 'unit': '%',
        'note': '分母是流通股本（流通市值/最新价），不是流通市值',
        'impact': 'scope',
    },
    {
        'key': 'liq_price_enabled', 'cfg': 'LIQ_PRICE_ENABLED', 'kind': 'bool',
        'label': '股价下限这一条', 'group': '选股范围', 'live': True,
        'note': '3 元以下的票集中在被边缘化的老股/庄股，跳空与单日异动概率更高',
        'impact': 'scope',
    },
    {
        'key': 'liq_min_price', 'cfg': 'LIQ_MIN_PRICE', 'kind': 'float',
        'label': '最低股价（元）', 'group': '选股范围', 'live': True,
        'min': 1, 'max': 100, 'step': 0.5, 'unit': '元',
        'note': '看的是最后一根已收盘日K的收盘价（前复权最后一根=实盘价）',
        'impact': 'scope',
    },
    {
        'key': 'candidate_rps_top_pct', 'cfg': 'CANDIDATE_POOL_RPS_TOP', 'kind': 'float',
        'label': '海选要求的 20 日涨幅百分位（前百分之几）', 'group': '数据与门槛', 'live': True,
        'min': 50, 'max': 99, 'step': 1, 'unit': '%',
        'note': '80 = 只收 20 日涨幅排进范围前 20% 的票；调小=放宽入口，调大=只要最强的一批',
        'impact': None,
    },
    {
        'key': 'candidate_near_high_pct', 'cfg': 'CANDIDATE_POOL_NEAR_HIGH', 'kind': 'float',
        'label': '收盘距 60 日高点的最小位置（%）', 'group': '数据与门槛', 'live': True,
        'min': 70, 'max': 100, 'step': 1, 'unit': '%', 'scale': 100,
        'note': '92 = 收盘必须在 60 日最高价的 8% 以内（强势未破位）。这是欧奈尔模板里的'
                '"别买正在掉下来的票"，改太低会放进一批破位票',
        'impact': None,
    },
    {
        'key': 'kline_pool_min_coverage', 'cfg': 'KLINE_POOL_MIN_COVERAGE', 'kind': 'float',
        'label': '出池要求的日K覆盖度（%）', 'group': '数据与门槛', 'live': True,
        'min': 50, 'max': 100, 'step': 1, 'unit': '%', 'scale': 100,
        'note': '低于这个比例就当"数据不新"，退回热搜池并告警，避免拿半截的一天去海选。'
                '分母是选股范围（会随上面的开关一起变）',
        'impact': 'coverage',
    },
    {
        'key': 'candidate_pool_max', 'cfg': 'CANDIDATE_POOL_MAX', 'kind': 'int',
        'label': '海选池上限（只）', 'group': '数据与门槛', 'live': True,
        'min': 20, 'max': 400, 'step': 10, 'unit': '只',
        'note': '控制 9:45 那批的耗时，不是选股标准',
        'impact': None,
    },
    {
        'key': 'loser_criterion', 'cfg': 'RECOMMEND_LOSER_CRITERION', 'kind': 'enum',
        'label': '亏损黑名单判据', 'group': '推荐避让黑名单', 'live': True,
        'options': [{'value': 't1', 'label': '只看 T+1（老行为）'},
                    {'value': 't1_or_t3', 'label': 'T+1 或 T+3 任一亏超阈值'}],
        'note': '只看 T+1 会被一根噪声左右：金安国纪 T+1 −7.2% 但 T+3 已回到 −2.5%；'
                '而有的票 T+1 没到线、T+3 才跌出来（如 T+3 −10.2% 那只，老判据完全看不见）',
        'impact': None,
    },
    {
        'key': 'loser_graded', 'cfg': 'RECOMMEND_LOSER_GRADED', 'kind': 'bool',
        'label': '按亏损轻重分级关禁', 'group': '推荐避让黑名单', 'live': True,
        'note': '关掉=一视同仁（老行为：只要亏超阈值一律关满窗口天数）',
        'impact': None,
    },
    {
        'key': 'loser_loss_threshold', 'cfg': 'RECOMMEND_LOSER_LOSS_THRESHOLD', 'kind': 'float',
        'label': '进黑名单的亏损阈值（%）', 'group': '推荐避让黑名单', 'live': True,
        'min': 1, 'max': 20, 'step': 0.5, 'unit': '%',
        'note': 'T+1（或 T+3）收益低于 −此值 才算推荐失败',
        'impact': None,
    },
    {
        'key': 'loser_heavy_loss', 'cfg': 'RECOMMEND_LOSER_HEAVY_LOSS', 'kind': 'float',
        'label': '重亏线（%）', 'group': '推荐避让黑名单', 'live': True,
        'min': 2, 'max': 30, 'step': 0.5, 'unit': '%',
        'note': '亏过这条线才关满 HEAVY 天数；没过的只关 LIGHT 天数（需先打开上面的分级开关）',
        'impact': None,
    },
    {
        'key': 'loser_heavy_days', 'cfg': 'RECOMMEND_LOSER_HEAVY_DAYS', 'kind': 'int',
        'label': '重亏关禁天数', 'group': '推荐避让黑名单', 'live': True,
        'min': 5, 'max': 90, 'step': 5, 'unit': '天',
        'note': '从推荐日算起', 'impact': None,
    },
    {
        'key': 'loser_light_days', 'cfg': 'RECOMMEND_LOSER_LIGHT_DAYS', 'kind': 'int',
        'label': '轻亏关禁天数', 'group': '推荐避让黑名单', 'live': True,
        'min': 3, 'max': 60, 'step': 1, 'unit': '天',
        'note': '阈值~重亏线之间；设太短等于没有避让', 'impact': None,
    },
    {
        'key': 'loser_lookback_days', 'cfg': 'RECOMMEND_LOSER_LOOKBACK_DAYS', 'kind': 'int',
        'label': '黑名单回看窗口（天）', 'group': '推荐避让黑名单', 'live': True,
        'min': 7, 'max': 120, 'step': 1, 'unit': '天',
        'note': '不分级时这一项就是关禁天数', 'impact': None,
    },
    {
        'key': 'zone_gate_enabled', 'cfg': 'RECOMMEND_ZONE_GATE_ENABLED', 'kind': 'bool',
        'label': '启用涨幅段闸门', 'group': '推荐涨幅段闸门', 'live': True,
        'note': '复盘实测两个当日涨幅段是负期望：弱势区 0~2%（36.4%/−2.59%）与'
                '半山腰死亡区 4.5~9.8%（40.0%/−2.27%），两段共 91 条；砍掉后剩 52.8%/+0.82%。'
                '关掉=回到不拦的老行为',
        'impact': None,
    },
    {
        'key': 'zone_gate_mode', 'cfg': 'RECOMMEND_ZONE_GATE_MODE', 'kind': 'enum',
        'label': '闸门模式', 'group': '推荐涨幅段闸门', 'live': True,
        'options': [{'value': 'drop', 'label': '过滤（死亡区票不进候选）'},
                    {'value': 'demote', 'label': '罚分（压低分数但仍可选）'},
                    {'value': 'off', 'label': '关闭（不拦）'}],
        'note': '过滤模式下被挡的票会进影子记录，系统自动跟踪它们此后的表现来验证闸门是否有效；'
                '罚分模式更温和，但排名靠前的高分死亡区票仍可能入选',
        'impact': None,
    },
    {
        'key': 'zone_weak_max', 'cfg': 'RECOMMEND_ZONE_WEAK_MAX', 'kind': 'float',
        'label': '弱势区上限（%）', 'group': '推荐涨幅段闸门', 'live': True,
        'min': 0, 'max': 6, 'step': 0.5, 'unit': '%',
        'note': '当日涨幅在 [0, 此值) 算"没涨起来"：不是安全，是没人要（36.4% 胜率）',
        'impact': None,
    },
    {
        'key': 'zone_weak_penalty', 'cfg': 'RECOMMEND_ZONE_WEAK_PENALTY', 'kind': 'int',
        'label': '弱势区罚分', 'group': '推荐涨幅段闸门', 'live': True,
        'min': 0, 'max': 20, 'step': 1, 'unit': '分',
        'note': '仅罚分模式生效；过滤模式下整只拦掉，不需要罚分', 'impact': None,
    },
    {
        'key': 'zone_chase_min', 'cfg': 'RECOMMEND_ZONE_CHASE_MIN', 'kind': 'float',
        'label': '死亡区下限（%）', 'group': '推荐涨幅段闸门', 'live': True,
        'min': 2, 'max': 8, 'step': 0.5, 'unit': '%',
        'note': '甜点区上界：低于此值不拦', 'impact': None,
    },
    {
        'key': 'zone_chase_max', 'cfg': 'RECOMMEND_ZONE_CHASE_MAX', 'kind': 'float',
        'label': '死亡区上限（%）', 'group': '推荐涨幅段闸门', 'live': True,
        'min': 6, 'max': 15, 'step': 0.5, 'unit': '%',
        'note': '达到此值按涨停区对待（历史最强的一段，不拦）', 'impact': None,
    },
    {
        'key': 'zone_chase_penalty', 'cfg': 'RECOMMEND_ZONE_CHASE_PENALTY', 'kind': 'int',
        'label': '死亡区罚分', 'group': '推荐涨幅段闸门', 'live': True,
        'min': 0, 'max': 20, 'step': 1, 'unit': '分',
        'note': '仅罚分模式生效；过滤模式下整只拦掉，不需要罚分', 'impact': None,
    },
    {
        'key': 'buy_band_dynamic_enabled', 'cfg': 'BUY_RANGE_DYNAMIC_ENABLED', 'kind': 'bool',
        'label': '启用动态区间宽度（影子）', 'group': '买入区间带宽', 'live': True,
        'note': '现行上沿恒为「低吸价×1.01」，与个股波动无关；打开后按近 5 日已实现波动率'
                '放宽到 +band%，但**只写进新键 buyHighDyn**：线上挂单价、成交判定、主统计'
                '一律还是核心区间。关掉=逐字节等于改造前',
        'impact': None,
    },
    {
        'key': 'band_base_pct', 'cfg': 'BUY_RANGE_BAND_BASE_PCT', 'kind': 'float',
        'label': '核心带宽（%）', 'group': '买入区间带宽', 'live': True,
        'min': 0.5, 'max': 3, 'step': 0.1, 'unit': '%',
        'note': '就是现行那个 1%；它同时是扩展带宽的**下限**（扩展不会比核心更窄）',
        'impact': None,
    },
    {
        'key': 'band_max_pct', 'cfg': 'BUY_RANGE_BAND_MAX_PCT', 'kind': 'float',
        'label': '扩展带宽封顶（%）', 'group': '买入区间带宽', 'live': True,
        'min': 1, 'max': 6, 'step': 0.5, 'unit': '%',
        'note': '再怎么波动也不给超过低吸价 +3%：再往上就是替别人抬轿，不是"稍微贵一点"。'
                '扩展上沿另外还被追高线与涨停价双重封顶',
        'impact': None,
    },
    {
        'key': 'band_vol_k', 'cfg': 'BUY_RANGE_VOL_K', 'kind': 'float',
        'label': '带宽对波动的系数 K', 'group': '买入区间带宽', 'live': True,
        'min': 0, 'max': 3, 'step': 0.25, 'unit': '倍',
        'note': '扩展带宽 = 核心带宽 + K×5日波动率。K=0 等于不用动态，K 越大越宽',
        'impact': None,
    },
    {
        'key': 'band_vol_bars', 'cfg': 'BUY_RANGE_VOL_BARS', 'kind': 'int',
        'label': '波动率窗口（个交易日）', 'group': '买入区间带宽', 'live': True,
        'min': 3, 'max': 20, 'step': 1, 'unit': '天',
        'note': '只取**已收盘**交易日，数据来自本地日K库（零网络请求）；样本不足 3 个收益时'
                'vol5=None，带宽退回核心值——没有数据不做判决',
        'impact': None,
    },
    {
        'key': 'rec_sim_board_price_v2', 'cfg': 'REC_SIM_BOARD_PRICE_V2', 'kind': 'bool',
        'label': '回测封板改判"有无突破价成交"', 'group': '回测成交口径', 'live': True,
        'note': '买入：当日最低价低于涨停价就算存在过可成交价（炸板回封也可买），一字/T字全天'
                '没低下去过才剔；卖出：当日最高价高于跌停价就能卖，一字跌停卖不掉则顺延。'
                '关掉=用旧判据（收盘==当日最高价且涨幅≥97%限额视为封死）',
        'impact': 'backtest',
    },
    {
        'key': 'rec_sim_defer_limit_down', 'cfg': 'REC_SIM_DEFER_LIMIT_DOWN', 'kind': 'bool',
        'label': '一字跌停顺延也管尾盘卖', 'group': '回测成交口径', 'live': True,
        'note': '旧实现只对"开盘卖"判一字跌停顺延，尾盘卖连检查都没有——等于假设跌停也能按'
                '收盘价卖掉，回撤被低估。关掉=恢复只对开盘卖生效的老行为',
        'impact': 'backtest',
    },
    {
        'key': 'rec_sim_board_eps', 'cfg': 'REC_SIM_BOARD_EPS', 'kind': 'float',
        'label': '涨跌停价比较容差（元）', 'group': '回测成交口径', 'live': True,
        'min': 0.0, 'max': 0.05, 'step': 0.005, 'unit': '元',
        'note': '价格按四舍五入到分算，比较时留半分容差；调大只会让更多票被判成"能成交"',
        'impact': 'backtest',
    },
    {
        # 2026-11-03 修①：预测验证的"次日涨跌"原来取调用那一刻的实时涨跌，周末/接口重跑时
        # 会把预测日自己的涨跌写成次日（实测 2410 条里 93 条中招、集中在周五那批）。
        'key': 'rec_verify_use_kline', 'cfg': 'REC_VERIFY_USE_KLINE', 'kind': 'bool',
        'label': '预测验证改从日K取"次日"涨跌', 'group': '回测成交口径', 'live': True,
        'note': '开（默认）＝与补验路径同一口径：取日K里严格晚于预测日的第一根，取不到就不写、'
                '留给下一轮，绝不拿当天或盘中快照冒充次日；这批数同时喂给准确率统计与因子权重学习。'
                '关＝回到改造前的批量实时行情口径（含 changePercent=0 跳过），逐字节等于旧行为。',
        'impact': None,
    },
    {
        'key': 'rec_sim_sizing_v2', 'cfg': 'REC_SIM_SIZING_V2', 'kind': 'bool',
        'label': '启用仓位约束（风险预算＋上限）', 'group': '回测仓位规则', 'live': True,
        'note': '关掉就是现行规则「当天可用现金 ÷ 当天要买的只数」——满仓等分、不留缓冲；'
                '实测单笔占净值中位 32%、最大 99.5%（当天只推 1 只时一笔押满）。'
                '打开后每笔＝min(风险预算÷止损幅度, 单票上限)，再被当天剩余总敞口和现金夹住',
        'impact': None,
    },
    {
        'key': 'rec_sim_risk_per_trade', 'cfg': 'REC_SIM_RISK_PER_TRADE', 'kind': 'float',
        'label': '每笔账户风险预算（%）', 'group': '回测仓位规则', 'live': True,
        'min': 0.25, 'max': 3, 'step': 0.25, 'unit': '%', 'scale': 100,
        'note': '仓位＝这个值 ÷ 止损幅度。1.5%÷8% ≈ 19% 仓；它的前提是止损真会执行',
        'impact': None,
    },
    {
        'key': 'rec_sim_stop_pct', 'cfg': 'REC_SIM_STOP_PCT', 'kind': 'float',
        'label': '折算用止损幅度（%）', 'group': '回测仓位规则', 'live': True,
        'min': 4, 'max': 12, 'step': 0.5, 'unit': '%', 'scale': 100,
        'note': '线上三套止损口径还没统一（risk_control 振幅×1.5 钳[4,12]、position ×0.8 钳[4,8]），'
                '所以这里先当参数用，别当成"系统真会在这个价止损"',
        'impact': None,
    },
    {
        'key': 'rec_sim_max_position_pct', 'cfg': 'REC_SIM_MAX_POSITION_PCT', 'kind': 'float',
        'label': '单票占净值上限（%）', 'group': '回测仓位规则', 'live': True,
        'min': 5, 'max': 100, 'step': 5, 'unit': '%', 'scale': 100,
        'note': '专治"当天只推 1 只就 100% 押一只"；设成 100 等于不加这条约束',
        'impact': None,
    },
    {
        'key': 'rec_sim_max_exposure_pct', 'cfg': 'REC_SIM_MAX_EXPOSURE_PCT', 'kind': 'float',
        'label': '同日总敞口上限（%）', 'group': '回测仓位规则', 'live': True,
        'min': 20, 'max': 100, 'step': 5, 'unit': '%', 'scale': 100,
        'note': '持仓＋当天新买单占净值上限，剩下是现金缓冲；设成 100 等于不留缓冲（老行为）',
        'impact': None,
    },
    {
        'key': 'push_daily_limit', 'cfg': 'WECOM_PUSH_DAILY_LIMIT', 'kind': 'int',
        'label': '每天最多推送只数（去重后）', 'group': '推送', 'live': True,
        'min': 1, 'max': 30, 'step': 1, 'unit': '只',
        'note': '按"只"限（每只票一天只推一次）；用满之后本轮只记不推',
        'impact': None,
    },
    {
        'key': 'ops_notify_daily_limit', 'cfg': 'WECOM_OPS_NOTIFY_DAILY_LIMIT', 'kind': 'int',
        'label': '运维通知每天最多推送条数', 'group': '推送', 'live': True,
        'min': 0, 'max': 20, 'step': 1, 'unit': '条',
        'note': '仅限"服务重启/自愈/隧道拉起"类运维通知（由仓库外守护脚本发送）。默认 2 条/天，'
                '用满后当天不再推这类消息；业务推送（选股/盘前预览/T+1 预案/收评/市场情绪等）不受此限。'
                '改完即时生效（守护脚本每次发通知前会来读这个值，后端挂了时按默认 2 兜底）。',
        'impact': None,
    },
    {
        'key': 'push_apply_loser_blacklist', 'cfg': 'WECOM_PUSH_APPLY_LOSER_BLACKLIST', 'kind': 'bool',
        'label': '盘中推送也查亏损黑名单', 'group': '推送', 'live': True,
        'note': '2026-10-17 补的漏口：以前只有智能推荐查这张表，被拉黑的票还会从 60 秒扫描推送里回来',
        'impact': None,
    },
    {
        'key': 'push_pool_top_n', 'cfg': 'WECOM_PUSH_POOL_TOP_N', 'kind': 'int',
        'label': '纳入 60 秒连续扫描的海选只数', 'group': '推送', 'live': True,
        'min': 10, 'max': 250, 'step': 10, 'unit': '只',
        'note': '范围外存量已冻结不同步；这里太大去打腾讯批量接口会被限流',
        'impact': None,
    },
    {
        'key': 'snapshot_enabled', 'cfg': 'STRATEGY_SNAPSHOT_ENABLED', 'kind': 'bool',
        'label': '记录策略决策快照', 'group': '策略快照', 'live': True,
        'note': '每批次把"扫了谁/每只多少分/走到哪一步/为什么被挡/当天情绪档"落一张表。'
                '关掉后推荐照跑，但以后回测只能拿近似口径重建 —— 盘中的委比、外盘、量比'
                '日K库里没有，历史无法回填，只能现攒',
        'impact': 'snapshot',
    },
    {
        'key': 'snapshot_max_rows', 'cfg': 'STRATEGY_SNAPSHOT_MAX_ROWS', 'kind': 'int',
        'label': '每批次快照最多记多少只', 'group': '策略快照', 'live': True,
        'min': 100, 'max': 3000, 'step': 100, 'unit': '只',
        'note': '过门槛的候选与最终精选一定全留，剩下的按分数从高到低截。'
                '记满长尾才能做"分数带 × T+1 胜率"的分组统计',
        'impact': 'snapshot',
    },
    {
        'key': 'snapshot_retention_days', 'cfg': 'STRATEGY_SNAPSHOT_RETENTION_DAYS', 'kind': 'int',
        'label': '快照保留天数', 'group': '策略快照', 'live': True,
        'min': 0, 'max': 3650, 'step': 90, 'unit': '天',
        'note': '0=永久保留。每批 900 只 × 每天 4 批 ≈ 1 万行/天，两年约 500 万行',
        'impact': 'snapshot',
    },
    {
        'key': 'rec_sim_daily_enabled', 'cfg': 'REC_SIM_DAILY_ENABLED', 'kind': 'bool',
        'label': '每日跑可成交性监控（16:20）', 'group': '策略快照', 'live': True,
        'note': '把"同一批推荐在三种入口口径下各值多少钱"每天存一个点（rec_sim_daily）。'
                '关掉后回测曲线页仍能现算当天数字，但失去时间序列——'
                '任何"放宽过滤/多推/改模式"的提案都要靠这条序列证明 auction 口径没恶化',
        'impact': None,
    },
    {
        'key': 'shadow_eval_daily_enabled', 'cfg': 'SHADOW_EVAL_ENABLED', 'kind': 'bool',
        'label': '每日跑影子对照（16:25）', 'group': '策略快照', 'live': True,
        'note': '算"影子规则挡掉的票第二天是不是真的更差"（recommend_shadow_eval）。'
                '这是第1批把周期门槛搬到线上的判据：关掉只是少一个监控点，'
                '不影响影子日志本身（它由推荐任务在每个批次写入）',
        'impact': None,
    },
    {
        'key': 'eval_lab_enabled', 'cfg': 'EVAL_LAB_ENABLED', 'kind': 'bool',
        'label': '每周六跑因子体检（08:30）', 'group': '策略快照', 'live': True,
        'note': '本地日K上算 20 日反转/低波/缩量/60日动量/涨停拥挤度的 IC 与分组超额，'
                '落 eval_lab_runs。只读、不联网、不改推荐；关掉只是停止更新体检结果',
        'impact': None,
    },
    {
        'key': 'auction_snapshot_enabled', 'cfg': 'AUCTION_SNAPSHOT_ENABLED', 'kind': 'bool',
        'label': '09:25 采集集合竞价快照', 'group': '数据与门槛', 'live': True,
        'note': '腾讯批量行情，采选票范围的竞价匹配价/量/额，落 auction_daily。'
                '这是以后验证"弱转强"的前提数据；失败不阻塞 09:45 推荐',
        'impact': None,
    },
    {
        'key': 'minute5_forward_enabled', 'cfg': 'MINUTE5_FORWARD_ENABLED', 'kind': 'bool',
        'label': '收盘后采集活跃票 5 分钟线', 'group': '数据与门槛', 'live': True,
        'note': '只采当日推荐/持仓/候选，走腾讯 5 分钟接口（避开 baostock 登录封禁）。'
                '默认关：先确认字段与落库口径再加量',
        'impact': None,
    },
    {
        'key': 'exit_rules_shadow_enabled', 'cfg': 'EXIT_RULES_SHADOW_ENABLED', 'kind': 'bool',
        'label': '持仓页展示统一卖出建议（影子）', 'group': '卖出规则', 'live': True,
        'note': '在每只持仓上并列显示"固定止损/逻辑止损/时间止损"的统一结论，'
                '不改变现有动作、不影响推送。用来攒 20 日以上对照',
        'impact': None,
    },
    {
        'key': 'exit_rules_live_enabled', 'cfg': 'EXIT_RULES_LIVE_ENABLED', 'kind': 'bool',
        'label': '卖出规则接管页面动作与推送（实盘）', 'group': '卖出规则', 'live': True,
        'note': '⚠️ 打开后 exit_rules 的建议会覆盖现有 action/推送，属于实盘行为变更。'
                '默认关；需影子期 ≥20 个交易日、回撤不恶化才打开',
        'impact': None,
    },
    {
        'key': 'exit_seal_shadow_enabled', 'cfg': 'EXIT_SEAL_SHADOW_ENABLED', 'kind': 'bool',
        'label': '出口分叉影子：封住拿隔夜/没封住当天走', 'group': '卖出规则', 'live': True,
        'note': '每天 20:35（日K五批同步之后）把当天推荐的三条出口（拿隔夜 / 当天收盘走 / 分叉）各算一遍落 '
                'exit_seal_shadow 表。样本内 201 条：净口径 +0.23% / −0.26% / +0.68%，'
                '分叉那条把"亏超 5%"从 9.0% 压到 4.5%。**只记录，不改出口文案、不改推送**；'
                '≥20 个交易日后按判据裁决（影子台账第 15 条）',
        'impact': None,
    },
    {
        'key': 'westock_crosscheck_enabled', 'cfg': 'WESTOCK_CROSSCHECK_ENABLED', 'kind': 'bool',
        'label': '每日双源数据校验（16:30，westock 权威）', 'group': '策略快照', 'live': True,
        'note': '把本地资金流/行情与 westock（腾讯自选股）比对，落 westock_snapshot 与 '
                'data_cross_check（authoritative=westock）。行情层不一致=某一侧接口异常需告警；'
                '资金流层只记录偏差与符号冲突（三口径定义不同，不当场校正展示）。'
                '关掉只是少一个监控点，不影响任何选股/推送链路',
        'impact': None,
    },
    {
        'key': 'mainline_enabled', 'cfg': 'MAINLINE_ENABLED', 'kind': 'bool',
        'label': '市场主线/支线判定（行动台 sectors 卡）', 'group': '市场主线', 'live': True,
        'note': '五维评分（涨停梯队/主力资金/涨幅持续性/量能，源内分位归一）判主线≤2、支线≤5、'
                '退潮板块，300 秒一档并落 market_mainline_snapshots 攒影子数据。'
                '关掉=行动台不出这张卡、零执行，返回体与改造前逐字节相同；'
                '权重不在这里改（防止随手调参污染校准），在 config.MAINLINE_WEIGHTS',
        'impact': 'desk',
    },
    {
        'key': 'mainline_min_score', 'cfg': 'MAINLINE_MIN_SCORE', 'kind': 'int',
        'label': '主线最低分', 'group': '市场主线', 'live': True,
        'min': 40, 'max': 90, 'step': 2, 'unit': '分',
        'note': '冰点/退潮档自动 +8；与第二名分差≥断层值时只认一条主线',
        'impact': 'desk',
    },
    {
        'key': 'mainline_sub_min_score', 'cfg': 'MAINLINE_SUB_MIN_SCORE', 'kind': 'int',
        'label': '支线地板分（低于它=无主线）', 'group': '市场主线', 'live': True,
        'min': 20, 'max': 60, 'step': 2, 'unit': '分',
        'note': '全场最高分都够不着它 → 输出"无主线"+原因，对应游资心法的空仓哨兵',
        'impact': 'desk',
    },
    {
        'key': 'sector_min_members', 'cfg': 'SECTOR_MIN_MEMBERS', 'kind': 'int',
        'label': '板块持续性最少成员数', 'group': '市场主线', 'live': True,
        'min': 3, 'max': 30, 'step': 1, 'unit': '只',
        'note': '近10日窗口内每天都有 bar 的成员才计入交集（10-16 选股范围收窄的混合日靠它保口径），'
                '不足则该板块持续性=None、权重重归一并在 dataGaps 点名',
        'impact': 'desk',
    },
    {
        'key': 'desk_sector_chips', 'cfg': 'DESK_SECTOR_CHIPS_IN_SENTIMENT', 'kind': 'bool',
        'label': '情绪条保留涨停板块 chip', 'group': '市场主线', 'live': True,
        'note': '默认关：板块名单归一到 sectors 卡，避免同页两份"板块名单"打架；'
                '新卡出问题时打开可立刻回到旧观感',
        'impact': 'desk',
    },
    {
        'key': 'db_lock_sentinel_enabled', 'cfg': 'DB_LOCK_SENTINEL_ENABLED', 'kind': 'bool',
        'label': '盘中写锁哨兵', 'group': '数据库与写锁', 'live': True,
        'note': '独立连接试 BEGIN IMMEDIATE，等超阈值仍拿不到写锁就推企微，附「主库距最后一次成功写入'
                '几分钟」与最近任务锚点。哨兵拿到锁立刻 ROLLBACK，自己不会变成新的持锁者。'
                '关掉＝完全不探测也不推送，盘中锁死只能等用户撞上来',
    },
    {
        'key': 'db_lock_sentinel_threshold_sec', 'cfg': 'DB_LOCK_SENTINEL_THRESHOLD_SEC', 'kind': 'int',
        'label': '写锁等待告警阈值', 'group': '数据库与写锁', 'live': True,
        'min': 1, 'max': 10, 'step': 1, 'unit': '秒',
        'note': '超过这个秒数还拿不到写锁就告警。2026-11-03 那次锁被占了 1 小时以上，阈值定多小都抓得到；'
                '但定太小会在正常的并发写（多个定时任务同时落库）时误报，默认 2 秒',
    },
    {
        'key': 'db_lock_sentinel_cooldown_min', 'cfg': 'DB_LOCK_SENTINEL_COOLDOWN_MIN', 'kind': 'int',
        'label': '写锁告警冷却', 'group': '数据库与写锁', 'live': True,
        'min': 1, 'max': 60, 'step': 5, 'unit': '分钟',
        'note': '同一状态多久内只推一次。锁一直不放时哨兵每分钟都会探测到，不冷却就会推 60 条',
    },
    {
        'key': 'sentiment_snapshot_save_stale_fresh', 'cfg': 'SENTIMENT_SNAPSHOT_SAVE_STALE_FRESH',
        'kind': 'bool', 'label': '情绪快照允许落"当场采集但缺维"的 stale 行',
        'group': '策略快照', 'live': True,
        'note': '11-04 修：`_maybe_save` 原来对所有 stale 一律不落库，把"数字是此刻采的、只是东财'
                '涨停池/某一维取不到"（source=intraday/alt_source）也一起拒了——东财连续两天不可用'
                '就导致 market_sentiment_snapshots 两天 0 行，盘中情绪档断了审计链。现在只有'
                '"复用旧值"那类（snapshot_fallback/snapshot_final/review_fallback）继续拒绝。'
                '关掉＝逐字节回到"stale 一律不落库"',
        'impact': 'snapshot',
    },
    # 注意：DB_FAST_FAIL_ENABLED 与 DB_CACHE_WRITE_BUSY_TIMEOUT_MS **故意不注册在这里**——
    # 它们是 get_db_conn() 自己要用的值，而 app_settings.value() 内部就走 get_db_conn 读覆盖表，
    # 接进来就是无限递归。那两项只能改 app/config.py 并重启后端（写
    # server/data/backend_restart_requested 由看护重启），配置页看不到它们是有原因的。
]

_SPEC_INDEX = {s['key']: s for s in SPEC}
_OVERRIDES: Optional[Dict[str, str]] = None
_SCOPE_KEYS: Optional[set] = None


def _invalidates_scope(key: str) -> bool:
    """改这一项要不要清「选股范围」的进程内缓存。

    只有「选股范围」组的项会改变 universe/liquidity/market_cap 的输入；
    其他项（推送、快照、纯界面偏好）改了不影响范围，没必要跟着把热缓存打掉。
    """
    global _SCOPE_KEYS
    if _SCOPE_KEYS is None:
        _SCOPE_KEYS = {k for k, s in _SPEC_INDEX.items() if s.get('group') == '选股范围'}
    return key in _SCOPE_KEYS


def _ensure_table(conn) -> None:
    conn.execute(_DDL)


def _load_overrides() -> Dict[str, str]:
    global _OVERRIDES
    if _OVERRIDES is None:
        conn = get_db_conn()
        try:
            _ensure_table(conn)
            _OVERRIDES = {r[0]: r[1] for r in conn.execute(
                'SELECT key, value FROM app_settings').fetchall()}
        except sqlite3.Error as e:
            print(f'[配置覆盖] 读取失败(全部退回 config 默认值): {e}')
            _OVERRIDES = {}
        finally:
            conn.close()
    return _OVERRIDES


def invalidate() -> None:
    """让本进程下次重新读表（set 之后调用）。"""
    global _OVERRIDES
    _OVERRIDES = None


def owner_id() -> Optional[int]:
    """最早注册的账号 = 事实上的管理员（users 表没有角色列，只能这样定）。"""
    conn = get_db_conn()
    try:
        row = conn.execute('SELECT id FROM users ORDER BY id LIMIT 1').fetchone()
        return int(row[0]) if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def write_ids() -> tuple:
    ids = getattr(cfg, 'SETTINGS_WRITE_USER_IDS', ()) or ()
    if ids:
        return tuple(ids)
    o = owner_id()
    return (o,) if o is not None else ()


def can_write(user: Optional[Dict[str, Any]]) -> bool:
    """能不能改口径：白名单为空时按"最早注册的账号"算，避免写死 id 导致没人能改。"""
    if not user:
        return False
    return user.get('id') in write_ids()


def spec(key: str) -> Dict[str, Any]:
    s = _SPEC_INDEX.get(key)
    if not s:
        raise KeyError(f'不允许修改的配置项: {key}')
    return s


def default_value(key: str) -> Any:
    """出厂默认值（数值项会按 scale 换算成"界面上的单位"）。

    2026-10-24：优先读 config.py 的 cfg 变量；纯界面项没有 cfg，用 SPEC 里的 'default'。
    """
    s = spec(key)
    raw = getattr(cfg, s['cfg']) if s.get('cfg') else s['default']
    if s['kind'] == 'enum':
        return str(raw)
    if s.get('scale') == 100:          # 0.90 这种比例 → 显示成 90
        return round(float(raw) * 100, 2)
    if s.get('scale'):
        return round(float(raw) / s['scale'], 4)
    return raw


def value(key: str) -> Any:
    """**用的时候现读**：覆盖值优先，没有就用 config 默认值。返回值一律是**界面单位**
    （亿元、万元、百分数），这样 `default_value()` 和 `value()` 才是同一个尺度可比。

    ⚠️ 2026-10-17 差点踩进去的坑：落库走的是 config 单位（35e8 元、0.90 比例），
    读的时候必须除回 scale，否则界面上调"8000 万"、后端拿到的是"8000 元"，
    范围会整片空掉，而且看不出是单位问题。
    """
    s = spec(key)
    ov = _load_overrides().get(key)
    if ov is None:
        return default_value(key)
    if s['kind'] == 'bool':
        return ov == '1'
    if s['kind'] == 'enum':
        return str(ov)
    num = float(ov)
    if s.get('scale') == 100:
        num = num * 100.0
    elif s.get('scale'):
        num = num / s['scale']
    return int(num) if s['kind'] == 'int' else num


def coerce(key: str, incoming: Any):
    """校验 + 归一化。返回 (ok, 值或错误说明)。数值一律按界面单位存，落库时换算回 config 单位。"""
    s = spec(key)
    if s['kind'] == 'bool':
        if isinstance(incoming, bool):
            return True, incoming
        t = str(incoming).strip().lower()
        if t in ('1', 'true', 'on', 'yes', '开'):
            return True, True
        if t in ('0', 'false', 'off', 'no', '关'):
            return True, False
        return False, f'{s["label"]} 只能是开/关，收到的是「{incoming}」'
    if s['kind'] == 'enum':
        allowed = [o['value'] for o in s.get('options', [])]
        if str(incoming) not in allowed:
            return False, f'{s["label"]} 只能是 {"/".join(allowed)} 之一，收到的是「{incoming}」'
        return True, str(incoming)
    try:
        num = float(incoming)
    except (TypeError, ValueError):
        return False, f'{s["label"]} 需要数字，收到的是「{incoming}」'
    lo, hi = s.get('min'), s.get('max')
    if lo is not None and num < lo:
        return False, f'{s["label"]} 不能小于 {lo}{s.get("unit", "")}'
    if hi is not None and num > hi:
        return False, f'{s["label"]} 不能大于 {hi}{s.get("unit", "")}'
    if s['kind'] == 'int':
        if abs(num - round(num)) > 1e-9:
            return False, f'{s["label"]} 只能是整数'
        return True, int(num)
    return True, num


def _to_storage(key: str, val: Any) -> str:
    """界面单位 → config 单位（0.90 那种比例、35e8 那种大数都在这里还原）。"""
    s = spec(key)
    if s['kind'] == 'bool':
        return '1' if val else '0'
    if s['kind'] == 'enum':
        return str(val)
    if s.get('scale') == 100:
        return str(float(val) / 100.0)
    if s.get('scale'):
        return str(float(val) * s['scale'])
    return str(val)


def set_value(key: str, incoming: Any, user: str = '') -> Dict[str, Any]:
    if key not in _SPEC_INDEX:
        # 白名单之外一律拒绝：这个接口能改的是"选票口径"，不能变成任意配置写入
        return {'ok': False, 'error': f'不允许修改的配置项: {key}'}
    ok, val_or_err = coerce(key, incoming)
    if not ok:
        return {'ok': False, 'error': val_or_err}
    s = spec(key)
    conn = get_db_conn()
    try:
        _ensure_table(conn)
        # 改得跟默认值一样就删掉覆盖行：留着它，以后 config 默认值调整时会被旧覆盖"钉住"，
        # 而且排查的人看不出这个值到底是界面调过的还是默认的（2026-10-17 就留过一条 0.5 亿，
        # 直接把线上基线从 1094 只改成了 1168 只）。
        default = default_value(key)
        if s['kind'] == 'bool':
            same_as_default = bool(val_or_err) == bool(default)
        elif s['kind'] == 'enum':
            same_as_default = str(val_or_err) == str(default)
        else:
            same_as_default = abs(float(val_or_err) - float(default)) < 1e-9
        if same_as_default:
            conn.execute('DELETE FROM app_settings WHERE key=?', (key,))
        else:
            conn.execute('INSERT OR REPLACE INTO app_settings (key, value, updated_at, updated_by) '
                         'VALUES (?,?,?,?)',
                         (key, _to_storage(key, val_or_err),
                          datetime.now().strftime('%Y-%m-%d %H:%M:%S'), user or 'unknown'))
        conn.commit()
    except sqlite3.Error as e:
        return {'ok': False, 'error': f'写入失败: {e}'}
    finally:
        conn.close()
    invalidate()
    # 范围相关的进程内缓存必须一起打掉，否则界面改了、算出来的还是旧范围。
    # 2026-10-24：只有「选股范围」组的项才需要清——当天把「筛选页最低分」也照清了一次，
    # 结果下一次扫描丢掉热缓存，从 1.5s 变成 20.2s，同时打开的配置页被拖过 10s 超时。
    if _invalidates_scope(key):
        for mod in ('services.data.universe', 'services.data.liquidity', 'services.data.market_cap'):
            try:
                import importlib
                m = importlib.import_module(mod)
                for fn in ('invalidate_cache', 'clear_cache'):
                    if hasattr(m, fn):
                        getattr(m, fn)()
            except Exception as e:
                print(f'[配置覆盖] {mod} 缓存清理跳过: {type(e).__name__}: {e}')
        # 2026-10-30：口径摘要缓存随配置变化后台重算（/api/settings 只读快照，
        # 不在请求路径上付 5~28s 冷算；旧值在重算完成前继续兜底）
        try:
            from services.support import settings_preview as _sp
            _sp.refresh_summary_async()
        except Exception as _e:
            print(f'[配置覆盖] 口径摘要重算触发失败（不影响本次写入）: {type(_e).__name__}: {_e}')
    return {'ok': True, 'key': key, 'value': val_or_err, 'live': spec(key)['live']}


def reset_value(key: str, user: str = '') -> Dict[str, Any]:
    """删掉覆盖，回到 config.py 的默认值（界面上的"恢复默认"）。"""
    if key not in _SPEC_INDEX:
        return {'ok': False, 'error': f'不允许修改的配置项: {key}'}
    conn = get_db_conn()
    try:
        _ensure_table(conn)
        conn.execute('DELETE FROM app_settings WHERE key=?', (key,))
        conn.commit()
    except sqlite3.Error as e:
        return {'ok': False, 'error': f'清除失败: {e}'}
    finally:
        conn.close()
    invalidate()
    # 同 set_value：只有「选股范围」组才需要清范围缓存，纯界面项别把热缓存打掉
    if _invalidates_scope(key):
        for mod in ('services.data.universe', 'services.data.liquidity', 'services.data.market_cap'):
            try:
                import importlib
                m = importlib.import_module(mod)
                if hasattr(m, 'invalidate_cache'):
                    m.invalidate_cache()
            except Exception:
                pass
        # 2026-10-30：同 set_value，口径摘要后台重算
        try:
            from services.support import settings_preview as _sp
            _sp.refresh_summary_async()
        except Exception:
            pass
    return {'ok': True, 'key': key, 'value': default_value(key)}


def history(limit: int = 20) -> List[Dict[str, str]]:
    conn = get_db_conn()
    try:
        _ensure_table(conn)
        return [dict(zip(('key', 'value', 'updated_at', 'updated_by'), r)) for r in conn.execute(
            'SELECT key, value, updated_at, updated_by FROM app_settings '
            'ORDER BY updated_at DESC LIMIT ?', (limit,)).fetchall()]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def list_items() -> List[Dict[str, Any]]:
    out = []
    for s in SPEC:
        ov = _load_overrides().get(s['key'])
        out.append({
            'key': s['key'], 'label': s['label'], 'group': s['group'], 'kind': s['kind'],
            'value': value(s['key']), 'default': default_value(s['key']),
            'overridden': ov is not None,
            'min': s.get('min'), 'max': s.get('max'), 'step': s.get('step'),
            'unit': s.get('unit', ''), 'note': s.get('note', ''), 'live': s['live'],
            'options': s.get('options') or ([],)[0],
            'impact': s.get('impact'),
        })
    return out
