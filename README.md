# A股实时行情查询系统

专门针对A股优化的实时行情查询工具，支持股票代码和名称查询，显示实时价格、买卖量等信息。

## 项目结构

```
gupiao/
├── client-vue/                  # 前端 (Vue 3 + Vite)
│   ├── index.html               # Vite 入口
│   ├── package.json             # 依赖
│   ├── package-lock.json        # 依赖锁定
│   ├── vite.config.js           # 构建配置（dev 代理到后端 3003）
│   └── src/
│       ├── main.js              # Vue 启动入口
│       ├── App.vue              # 根组件
│       ├── components/
│       │   ├── tabs/
│       │   │   ├── TabQuote.vue      # 实时行情
│       │   │   ├── TabTrend.vue      # 趋势发现
│       │   │   ├── TabScan.vue       # 股票筛选
│       │   │   ├── TabRecommend.vue  # 智能推荐
│       │   │   ├── TabReview.vue     # 每日收评
│       │   │   └── TabStrategy.vue   # 策略因子
│       │   ├── KlineModal.vue        # K线查看弹窗
│       │   ├── AiDiagModal.vue       # AI 诊断弹窗
│       │   └── IndexBar.vue          # 大盘指数栏
│       ├── composables/
│       │   └── useStockData.js       # 行情数据 composable
│       ├── utils/
│       │   ├── score.js              # 统一评级 getRating + 综合评分透传薄壳（权重由后端统一维护）
│       │   └── helpers.js            # 通用工具
│       └── assets/
│           └── main.css              # 全局样式
│
├── server/                      # FastAPI 后端
│   ├── run.py                   # 服务启动入口（uvicorn 启动 app.main:app）
│   ├── start.py                 # 跨平台统一启动入口（2026-06-18 新增）
│   ├── start.bat                # Windows 启动脚本（薄壳，调用 start.py）
│   ├── requirements.txt         # Python 依赖
│   ├── ai_config.json           # AI 诊断配置（字节方舟/豆包，OpenAI 兼容格式）
│   ├── gupiao.db                # SQLite 数据库文件（运行时生成，已从 MySQL 迁移）
│   ├── migrate_mysql_to_sqlite.py  # MySQL → SQLite 一次性迁移脚本
│   ├── app/
│   │   ├── main.py              # FastAPI 应用实例 & 启动生命周期 & /eastmoney 代理路由
│   │   ├── config.py            # 集中配置（所有硬编码参数 + 热加载）
│   │   ├── api/
│   │   │   └── routes.py        # 全部 HTTP 路由（/api/...，共 48 个端点）
│   │   └── core/
│   │       └── scheduler.py     # 定时任务注册（基于 schedule 库）
│   ├── services/                # 业务逻辑层
│   │   ├── stock.py             # 行情/K线/热搜榜（数据源）
│   │   ├── trend.py             # 趋势扫描 + 股票池指标 + P0/P2/P3 乘性修正系数族
│   │   ├── strategy.py          # 9 因子 AI 选股评分（calc_stock_score_v2）+ P3-2 行业中性化
│   │   ├── recommend.py         # 智能推荐
│   │   ├── market.py            # 大盘/板块数据
│   │   ├── review.py            # 每日收评生成
│   │   ├── ai.py                # AI 诊断（字节方舟/豆包）
│   │   ├── pattern.py           # P2-2 量价 6 形态分类（±4% 乘性修正）
│   │   ├── trend_backtest.py    # P4-1 趋势评分回测验证（1/3/5 日跑赢大盘胜率）
│   │   └── utils.py             # 服务层共享工具
│   ├── db/
│   │   └── database.py          # SQLite 连接 + 持久化（trend_scan_results / hot_search_daily_snapshot / trend_backtest_* 等）
│   └── static/                  # 前端构建产物（生产环境直接挂载到 /）
│       ├── index.html
│       └── assets/
│
└── README.md
```

> **历史变更提示**：
> - `client-vue/src/utils/stocks-config.js` 与 `forbidden-keywords.js` 已于 2026-07-02 C-2 清理时删除（用户自选/热点池改由后端 API 拉取，关键词过滤集中在 `config.py` 的 `FORBIDDEN_KEYWORDS`）
> - 数据库已从 MySQL 迁移到 SQLite（`server/gupiao.db`），`migrate_mysql_to_sqlite.py` 为一次性迁移脚本
> - 东方财富 HTTP 请求统一改为 `requests + asyncio.to_thread`，不再使用 `httpx.AsyncClient`（避免 Server disconnected）

## 快速开始

### 启动服务（端口 3003）

双击 `server/start.bat` 或：
```bash
cd server
sh start.bat   # Windows 直接双击即可
```

`start.bat` 启动顺序：
1. **构建前端**（自动 cd 到 `client-vue`，首次会执行 `pnpm install`，之后增量 `pnpm run build`）
2. 检查 Python 环境（自动识别 `python3` / `python`）
3. 安装/更新 Python 依赖（`requirements.txt`，清华源失败时回退到默认源）
4. 启动 FastAPI（`run.py`）

访问 http://localhost:3003 使用应用（生产环境由后端直接挂载 `static/` 下的前端构建产物）

访问 http://localhost:3003/docs 查看 API 文档

### 前端开发模式（可选）

如果需要热更新开发，可单独起前端 dev server：
```bash
cd client-vue
pnpm install
pnpm run dev
```

Vite dev server 会代理 `/api/*` 到后端 `http://localhost:3003`，同时也会代理 `/eastmoney/*` 到东方财富推送接口（[vite.config.js](file:///d:/Desktop/guizhou/gupiao/client-vue/vite.config.js)）。生产环境由 FastAPI 统一代理，避免跨域问题。

## 使用说明

### 查询股票

- 沪市：sh600519（贵州茅台）
- 深市：sz002384（东山精密）
- 创业板：sz300750（宁德时代）
- 科创板：sh688981（中芯国际）

---

## 趋势发现 - 完整逻辑说明（核心）

### 1. 扫描数据源

| 优先级 | 数据源 | 说明 |
|--------|--------|------|
| 主要 | 东方财富热搜榜（`hot_search_daily_snapshot` 表） | 实时更新，前500只热门股票 |
| 备用 | `HOT_STOCK_POOL` + `stock_basic_info` | 热搜榜为空时的本地股票池 |

### 2. 趋势股入选条件（中短线波段评分，V4.0）

> **关注周期**：未来 ~1 个月
> **评分结构**：基础分(100分) - 扣分项(含互斥衰减) + 加分项 + 板块动量修正 = 最终总分
> **强制排除项**：直接剔除，不进入评分

---

#### 2.1 基础分（100分，共4项）

| # | 维度 | 分值 | 得分档 | 判定标准 |
|---|------|------|--------|----------|
| 1 | 短期均线趋势 | 30 | 趋势20 + 乖离0~-10 | 趋势方向：近10日全在MA20上+MA20连升=20；近7日仅1日跌破次日收回=15；现价站稳=8。乖离惩罚：偏离MA20≥15%扣10分；≥10%扣6分；≥5%扣3分（追高风险惩罚） |
| 2 | 短期资金异动 | 30 | 30 / 25 / 20 / 12 / 0 | **P0-2 连板高度分层**（业界龙头战法核心）：5连板+=市场总龙头30；3-4连板=板块龙头25；2连板=替补龙20；首板=12；无涨停且阳线<3根=0（连板分与连阳分取高，无连板时fallback到涨停次数评分） |
| 3 | 量能配合 | 20 | 量比10+趋势斜率5+稳定性5 | 量比(20日/60日)：≥1.5→10分；趋势斜率(归一化)：≥0.3→5分；稳定性(CV)：≤0.3→5分。三个子维度分别评估量的大小、方向、健康度 |
| 4 | 风险回撤 | 20 | 20 / 15 / 10 / 5 / 0 | **当前距近10日高点回撤**<8%=20分；8%~12% =15分；12%~15% =10分；15%~20% =5分；≥20%=0分。已创新高则回撤≈0满分 |

---

#### 2.2 扣分项（12项，分3组互斥衰减，同组只取扣分最大值）

> **2026-06-17 新增**：扣分项分组互斥，防止同类风险重复惩罚。均线趋势组(MA20/MA60_MA120/中高位)、量价异常组(放量滞涨/量价背离/缩量阴跌)、极端事件组(跌停/天地板/单日大跌)各取最大值。

| 扣分 | 条件 | 说明 |
|------|------|------|
| -10（**可豁免**） | 现价250日分位≥80% **且近20日涨幅<20%** | 年度高位但非新高突破扣10分；新高突破（近20日涨≥20%）豁免 |
| -8 | MA60与MA120同步向下 | 中长期双空头 |
| -5 | 60%≤分位<80%或MA60↓/MA120↑ | 中期空头或高位震荡 |
| -6 | 近5日跌破MA20且3日未收回 | 短期破位 |
| -15（**可豁免**） | 近20日单日跌停，**豁免**：近3日≥2涨停（连板修复） | 洗盘/利空出尽后的强势修复可豁免 |
| -10 | 持续缩量阴跌（近5日量<60日均量50%） | 流动性枯竭 |
| -10 | 单日成交量创20日新高，涨幅-1%~1%（微涨/微跌+巨量） | 放量滞涨，排除大跌放量和涨停放量 |
| -6 | 近5日股价微涨但20日均量放大超30% | 量价顶背离 |
| -8 | 近20日振幅≥15%且收跌 | 天地板/炸板 |
| -8 | 单日大跌超-7%且次日未修复 | 破位下杀 |
| -10（**位置感知**） | 近20日振幅<5%：高位扣满10分、中位扣5分、**低位不扣反加3分** | 低位窄幅横盘是蓄力形态 |
| -5 | 非主板（创业板/科创板） | 波动大，降低权重 |

---

#### 2.3 加分项（6项+横盘蓄力，缩放后贡献 0~5 分）

> 加分项原始值合计最大 23 分（稳步上行与加速拉升互斥，取稳步上行 +3 作为代表），但不会直接叠加到基础分上，而是按 `(bonus_score / 23) * 5` 缩放到 0~5 分后附加，最终总分封顶 100。这样既保留"加分=质地更好"的区分度，又避免强势票全部拿到 100 分无法排序。

| 加分（原始值） | 缩放后上限 | 条件 |
|------|------|------|
| +6 | ~1.3 | MA20>MA60>MA120标准多头排列，且现价分位<60% |
| +4 | ~0.9 | 近3日倍量突破（日量≥20日均量×2，涨幅≥5%） |
| +4 | ~0.9 | 近5日连续阳线且每日涨幅≥1% |
| +3 | ~0.7 | 口袋支点：倍量(≥1.5倍)+收于日高80%以上+收盘突破前日高点 |
| +3 / +1（互斥） | ~0.7 / ~0.2 | **稳步上行 +3**：近20日涨幅5%-15% + 阳线占比≥60% + 最大单日涨幅<5%；**加速拉升 +1**：近5日涨幅≥15% + 量比≥2（警示透支风险，低加分）。两者互斥，加速拉升时给低分以示风险 |
| +3 | ~0.7 | 低位横盘蓄力：振幅<5%+250日分位<30%（加分替代扣分） |

---

#### 2.4 强制排除项（直接剔除，不评分）

| 条件 | 说明 |
|------|------|
| 近20日有重大利空/减持/立案调查公告 | 通过东方财富公告接口检测关键词 |
| ST / *ST / 退市 | 风险警示股 |
| 近20日出现连续3个跌停 | 极端流动性危机 |
| 总市值 < 20亿（可配置） | 排除小微盘庄股 |

---

#### 2.5 门槛判断

| 条件 | 值 | 说明 |
|------|----|------|
| **中期下跌过滤 ①** | MA20下方 **且** 60日累计跌幅 > 15% | **2026-06-18 新增**：避免"短线反弹但中长期下跌"的票误判为趋势向上，直接 `isUp=False` |
| **中期下跌过滤 ②（v5.3）** | MA 偏空（双空头/死亡交叉/现价<MA120 三选一）**且** 距全量历史高点回撤 > 20% | **2026-06-22 三阶迭代**：v5.1(60日窗口)→v5.2(全量高点+死亡交叉)→v5.3(+现价<MA120)，三层穿透覆盖任何时间跨度的下跌中继反弹 |
| **isUp 门槛** | ≥ 35分 | 认定为"趋势向上"，进入备选池 |
| **趋势池门槛** | ≥ 60分 | 从isUp中筛选，最终进入趋势发现列表 |
| **自动加入股票池门槛** | ≥ 80分 | 满足该分数的趋势股会自动写入 `user_scan_pool`，方便后续重点跟踪（阈值配置见 `TREND_AUTO_ADD_POOL_THRESHOLD`） |

---

#### 2.6 自动加入股票池（2026-06-25 新增）

趋势扫描完成后，系统会额外执行一次高分自动入池：

1. 取出本次扫描所有得分 ≥ `TREND_AUTO_ADD_POOL_THRESHOLD`（默认 **80 分**）的趋势股
2. 过滤掉已经在 `user_scan_pool` 中的股票
3. 批量插入 `user_scan_pool`，并记录日志
4. 前端趋势发现页顶部会显示本次自动加入的股票数量

> 阈值设为 0 或负数时，自动加池功能关闭；修改 `server/app/config.py` 后热加载即可生效，无需重启。

---

#### 2.7 板块动量修正（P0-1 已实现）

> **2026-06-30 升级**：基于个股概念标签（`stock_concept_tags`）的同题材平均涨幅做板块动量环境修正，个股评分最终乘以 `(1 + sectorFactor)`，热门板块加成、冷门板块折价。
>
> **数据来源**：扫描时复用 `load_stock_concept_tags()` 获取每只股票的概念标签，再批量获取扫描池内所有股票的当日涨幅，按概念标签聚合求平均，作为该板块当日动量代理指标。每只股票的 `sectorChange` = 其所属多个概念标签平均涨幅的算术平均。
>
> **修正系数分档**（`TREND_SECTOR_FACTOR_TIERS` / `TREND_SECTOR_FACTOR_COLD`）：
>
> | 板块平均涨幅 | sectorFactor | 说明 |
> |--------------|--------------|------|
> | ≥ 3% | +0.05 | 热门板块加成 5% |
> | ≥ 1% | +0.03 | 偏热板块加成 3% |
> | -1% ~ 1% | 0.0 | 中性板块不修正 |
> | ≤ -1% | -0.03 | 偏冷板块折价 3% |
> | ≤ -3% | -0.05 | 冷门板块折价 5% |
>
> **降级策略**：板块动量预计算失败时（如概念标签缺失、行情获取异常），`sector_change=None` → `sectorFactor=0.0` 不修正，不影响主评分流程。
>
> **乘性修正系数族（P0-1/P2-1/P2-2，2026-06-30）**：板块动量、筹码结构、量价形态三者为同族乘性修正，最终总分公式为 `total = raw_score × (1 + min(TREND_TOTAL_FACTOR_CAP, sectorFactor + chipFactor + patternFactor))`，三系数叠加总和封顶 `0.15`（防溢出），再 `min(100, …)`。各系数独立计算、互不干扰：
> - `sectorFactor`（P0-1，本节）：±5%，板块环境修正
> - `chipFactor`（P2-1）：±8%，筹码结构代理指标（MA成本+换手+主力净流入+量能CV）
> - `patternFactor`（P2-2）：±4%，量价6形态互斥识别
>
> 另有 **P3-1 动态阈值**（非乘性修正，而是按 `market_regime × pattern` 三级优先覆盖 D1/D3/D4 扣分阈值），与上述乘性修正并存：牛市放宽乖离、收紧破位；熊市收紧乖离/回撤；形态特化加重滞涨/背离/横盘扣分。

---

#### 2.8 评分流程示例

| 步骤 | 操作 | 得分变化 |
|------|------|----------|
| 1 | 计算基础分（满分 100）：D1=18(趋势20-乖离扣2),D2=20,D3=14(量比8+斜率3+稳定3),D4=20 | **72** |
| 2 | 扣分（互斥衰减后）：中高位-5, 跌破MA20-6（均线组已取最大） | 72 − 11 = **61** |
| 3 | 加分缩放：触发多头排列(5)+倍量突破(4)=9 → 缩放 (9/18)*5 = 2.5 | 61 + 2.5 = **63.5** |
| 4 | 三修正系数叠加（封顶 0.15）：板块+0.05 + 筹码+0.03 + 形态+0.04 = 0.12 | totalFactor = min(0.15, 0.12) = **0.12** |
| 5 | 乘性修正：raw × (1 + totalFactor) | 63.5 × 1.12 = **71.1** |
| 6 | 最终得分 71.1 ≥ 35 → **isUp = true**；≥ 60 → 进入趋势池 | — |


### 3. 趋势股排除条件

以下条件满足任一即**不进入**趋势池：

| 条件 | 说明 | 代码位置 |
|------|------|----------|
| K线数据不足 | K线天数 < 60 天 | `TREND_MIN_KLINE_DAYS` |
| 强制排除（ST / 主板 / 利空 / 横盘） | 见 §2.4 | [services/trend.py `is_up_trend`](file:///d:/Desktop/guizhou/gupiao/server/services/trend.py) |
| 退市风险 | 名称含 `ST`/`*ST`/`退市` 等关键词 | [services/stock.py `has_delisting_risk`](file:///d:/Desktop/guizhou/gupiao/server/services/stock.py) |
| 趋势评分不够 | 总分 < 60 分 | `TREND_MIN_SCORE` |

> **非沪深主板**（创业板/科创板）不直接排除，改为在评分中扣 `TREND_DEDUCT_NON_MAIN_BOARD`（默认 -5）分，见 §2.2 扣分项表末行。

### 4. 趋势股移除逻辑

#### 4.1 自动移除（每次扫描时）

趋势扫描是**全量替换**机制：每次 `/api/scan-trend-stocks` 执行时：
1. 先 `DELETE` 数据库中当天（同 `scan_date`）的所有旧记录
2. 重新全量扫描所有股票
3. 将符合条件的 `INSERT` 入数据库

→ **不满足条件的自动消失，新符合条件的自动出现**

#### 4.2 5日/10日线跌破移除（统一入口 + 盘中实时校准）

| 机制 | 触发时机 | 说明 |
|------|----------|------|
| **统一硬过滤** | 每次趋势扫描 | `is_up_trend()` 函数第一关就拒绝，价格源为**实时价**（盘中）或**收盘价**（盘后） |
| **实时价校准** | 09:30/11:30/13:00/15:00 定时扫描 | 扫描时优先取 `priceRaw` 实时价重算 MA5/MA10，盘中跌破立即移除 |
| **定时检查** | 每天 15:05 | `check_below_ma10()` 扫描**用户股票池**（非趋势池），跌破且未收回的直接删除（兜底） |

> **设计权衡说明**：系统采用盘中实时价判断是否跌破均线，目的是尽快识别转弱信号，避免用户看到已破位的股票。
> **可能副作用**：股票盘中短暂刺破均线但收盘收复，会被临时移出趋势池，需等待下一次全量扫描（通常次日）才会重新加入。
> **替代方案**：如需更稳健的判断，可在 `config.py` 中将 `USE_INTRADAY_BREAK_CHECK` 设为 `false`，改为仅用收盘价判断（或改为"连续两个收盘价跌破"再移除）。

**优化点**：
1. **数据滞后修复**：原逻辑只用 K 线收盘价判断，无法发现盘中跌破；现增加实时价校准
2. **检查入口统一**：5日/10日线检查从两处（`is_up_trend` + `check_below_ma10`）合并为一处
3. **冗余去除**：删除 `MA60>MA120` 重复评分项，分数重新分配给其他项

### 5. 趋势评分计算流程

```
扫描池中每只股票
  ├─ 获取K线 → is_up_trend(kline, realtime_price)
  │   ├─ K线天数 < 60?  →  拒绝 ❌
  │   ├─ 强制排除（ST/利空/连续跌停/小市值/行业关键词）  →  拒绝 ❌
  │   ├─ 4维度基础分（满分100）：
  │   │   D1 短期均线趋势 30分（趋势方向20 + 乖离惩罚0~-10）
  │   │   D2 短期资金异动 30分（连板高度分层/连阳数取高）
  │   │   D3 量能配合    20分（量比10 + 趋势斜率5 + 稳定性5）
  │   │   D4 风险回撤    20分（距近10日高点回撤，5档）
  │   ├─ P3-1 动态阈值：market_regime(bull/bear/range) × pattern 三级优先覆盖
  │   │   D1乖离/D3放量滞涨/D4回撤/破位/背离/横盘 扣分阈值
  │   ├─ 12项扣分（分3组互斥衰减，同组取最大值，阈值由 P3-1 动态调整）
  │   ├─ 5项加分（缩放到0~5分附加，总分封顶100）
  │   ├─ 乘性修正系数族（封顶 0.15）：
  │   │   total = raw × (1 + min(0.15, sectorFactor + chipFactor + patternFactor))
  │   │   • sectorFactor  P0-1 板块动量 ±5%
  │   │   • chipFactor    P2-1 筹码结构（MA成本+换手+主力净流入+量能CV）±8%
  │   │   • patternFactor P2-2 量价6形态 ±4%
  │   ├─ 中期下跌过滤①：MA20下方 + 60日跌幅>15%  →  isUp=false
  │   ├─ 中期下跌过滤②：MA偏空 + 距历史高点回撤>20%  →  isUp=false
  │   └─ 总分 ≥ 35?  →  isUp=true
  ├─ 获取股票名称 → 为空? → 跳过
  ├─ 检查退市风险 → 有风险? → 跳过
  ├─ 非沪深主板 → 扣5分（不跳过）
  ├─ 总分 ≥ 60?   → 进入趋势池
  ├─ 总分 ≥ 80?   → 自动加入用户股票池
  └─ 按分数降序排列 → 存入数据库
```

### 6. 前端加载策略

| 场景 | 行为 |
|------|------|
| 首次打开趋势发现 | 先展示缓存数据（秒开），同时检测 `scanDate` 是否过期 |
| 数据已过期 | 后台自动触发 `/api/scan-trend-stocks` 异步扫描 |
| 数据是今天的 | 直接展示，每隔30秒（盘中）/ 5分钟（盘后）自动刷新 |
| 缓存数据为空 | 显示"正在扫描"，然后触发完整扫描 |

**盘中 / 盘后定义**：

| 状态 | 时间范围 |
|------|----------|
| **盘中** | 交易日的 **09:30-11:30** 和 **13:00-15:00** |
| **盘后** | 交易日 **15:00 之后**，以及**所有非交易日**（周末、节假日） |

**节假日处理**：非交易日不触发自动刷新；前端检测到非交易日时，直接使用缓存数据并显示"休市"。

### 7. 关键配置项（`server/config.py`）

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `TREND_MIN_SCORE` | 60 | 进入趋势池的最低分数（V3 评分下从 85 调低） |
| `TREND_MIN_KLINE_DAYS` | 60 | 最少K线天数（覆盖MA20+60日量能） |
| `TREND_IS_UP_MIN_SCORE` | 35 | 认定为"趋势向上"的最低分数（V3 评分下从 60 调低） |
| `TREND_AUTO_ADD_POOL_THRESHOLD` | 80 | 趋势发现中得分 ≥ 该值的股票自动加入用户股票池；设为 0 关闭 |
| `TREND_D4_DRAWDOWN_TIERS` | [(8,20),(12,15),(15,10),(20,5)] | 维度4回撤分档（当前距近10日高点回撤, 得分） |
| `TREND_LIMIT_UP_THRESHOLD` | 9.8 | 涨停阈值（%） |
| `TREND_LIMIT_DOWN_THRESHOLD` | -9.8 | 跌停阈值（%） |
| `TREND_DEDUCT_LIMIT_DOWN` | 15 | 近20日单日跌停扣分 |
| `TREND_DEDUCT_VOLUME_SPIKE` | 10 | 放量滞涨扣分（仅-1%~1%涨幅+巨量触发） |
| `TREND_EXCLUDE_MARKET_CAP_MIN` | 20 | 总市值<20亿直接剔除 |
| `SCAN_CONCURRENCY` | 25 | 扫描并发数 |
| `SCAN_CACHE_TTL` | 300 | 缓存有效期（秒） |
| `HOT_SEARCH_TOP_N` | 500 | 热搜榜获取数量 |

### 8. 定时任务总览

| 时间 | 任务 | 作用对象 |
|------|------|----------|
| 10:00 | 更新东方财富热搜榜 | `hot_search_daily_snapshot` 表（用于趋势扫描的"热搜连续性检查"） |
| 14:00 | 更新东方财富热搜榜 | 同上 |
| **09:00 / 09:30 / 10:00 / 10:30 / 11:00 / 11:30 / 12:00 / 12:30 / 13:00 / 13:30 / 14:00 / 14:30 / 15:00** | **自动扫描趋势股** | **趋势池（全量替换，每 30 分钟一次，覆盖盘前预热/盘中实时价校准/收盘）** |
| 15:05 | 检查10日线，删除跌破股票 | **用户股票池**（非趋势池；趋势池的全量替换已覆盖该逻辑，此处为用户自选股的兜底检查） |
| 15:10 | 自动生成每日收评 | `daily_market_reviews` 表 |
| 15:20 | 回测验证昨日预测并调整策略权重 | 策略权重表（仅当启用机器学习权重时生效） |
| **15:30** | **P4-1 趋势评分回测验证** | `trend_backtest_records` / `trend_backtest_reports` 表（1/3/5 日跑赢大盘胜率） |

> 趋势扫描时点可通过 `config.py` 的 `SCHEDULE_TREND_SCAN_START` / `SCHEDULE_TREND_SCAN_END` / `SCHEDULE_TREND_SCAN_INTERVAL_MIN` 调整，修改后热加载即生效。

> 趋势回测时间可通过 `config.py` 的 `SCHEDULE_TREND_BACKTEST_TIME` 调整（默认 15:30）。

> **说明**：所有定时任务在 [server/core/scheduler.py](file:///d:/Desktop/guizhou/gupiao/server/core/scheduler.py) 的 `run_scheduler()` 中注册（[server/app/main.py:L87-L120](file:///d:/Desktop/guizhou/gupiao/server/app/main.py#L87-L120) 启动），通过 `schedule` 库在进程内直接调用异步函数，**不走 HTTP 接口**。

### 9. AI 选股推荐策略（2026-06-30 优化）

针对“AI智能选股推荐”板块，系统采用了一套结合量价协同、龙头溢价、主力资金和骗线过滤的综合评分算法。

#### 9.1 核心评分因子（9因子，总分 100）

> **2026-06-30 升级**：从 8 因子扩展到 9 因子，新增「主力净流入」因子（P1-1）；涨停质量从 2 档细化为 5 档（P1-2）。`max_possible` 改为从 `STRATEGY_SCORE_VALUES` 动态提取各因子最大档位分值计算，避免硬编码导致新增因子后归一化错误。
>
> **P3-2 行业中性化后处理（2026-06-30）**：9 因子加权得到基础 `total` 后，由共享 helper `apply_industry_neutralization` 按行业 P20/P80 分位数做 ±2 调整（见下表末行），避免热门行业股票集体虚高。三层调用对齐：`routes.py` 批量扫描、`recommend.py` 智能推荐、`trend.py` 趋势扫描均在 `calc_stock_score_v2` 之后调用同一 helper。

| 因子 | 权重 | 优化逻辑说明 |
|------|------|--------------|
| **外盘占比** | ~15% | 衡量主动买入意愿。>55% 给予高分奖励。大盘上涨时若 outer>60% 且 vol<1.5%，额外 +5 分（缩量稳步上涨优选）。 |
| **量比健康度** | ~10% | **分级放量出货检测**：涨停股用更高阈值（vol>8x + outer<45% 重罚-5；vol>5x + outer<48% 轻罚-3），非涨停股（vol>3x + outer<50% 或 vol>1.5x + outer<55%）扣分-5。正常封板（vol≤5x）不罚。 |
| **涨幅及龙头逻辑** | ~20% | **P1-2 涨停质量 5 档分层**（2026-06-30）：用委比+换手率+量比组合代理封板质量，业界共识"封板质量决定次日溢价"：<br>• **涨停(>9.5%)**：强力封板(委比>95%+换手2-8%+量比<3)=**25分**；优质封板(委比>80%+换手2-15%)=22分；普通封板(委比>50%+换手≤20%)=18分；弱封板(委比>0)=14分；烂板/开板(委比≤0)=8分。<br>• **7%-9.5%**：量比<2.5=18分，否则11分。<br>• **3%-7%**：健康量比(1.0-2.2)+强外盘(>56%)=18分，否则14分。<br>• **0-3%**：16分。 |
| **均价偏离度** | ~10% | 监测分时图价格与均线的关系，偏离过远（>1.5%）视为短线超买，适当降分。 |
| **委比真实性** | ~10% | **虚假封单过滤**：委比 > 99% 但外盘占比 < 60% 时，判定为虚假封单诱多，评分归零。 |
| **换手率** | ~10% | 辅助因子，确保个股活跃度处于安全区间（2-15% 为健康档）。 |
| **振幅** | ~5% | 辅助因子，过高振幅（>10%）扣分，过低（<2%）也适当降分。 |
| **PE/仓位** | ~10% | 辅助因子，PE 过高（>50）或涨幅过大时降档，平稳小幅上涨 + 低 PE 加分。 |
| **主力净流入**（**P1-1 新增**） | ~10% | **2026-06-30 新增第 9 因子**。基于流通市值的主力净流入占比（`main_net_inflow / circ_mv × 100%`），数据源为东方财富 clist 接口（f62=主力净流入，**f21=流通市值**；注意 f85 是市盈率 PE 不是流通市值）。接口走 `push2delay` 延迟端点 + 分页拉取（单页上限 100 条），未公布字段（`'-'`）容错为 0。<br>• ≥3% = 15分（强力吸筹）<br>• ≥1% = 10分（持续流入）<br>• ≥0% = 7分（小幅流入）<br>• ≥-1% = 7分（小幅流出，仍给 low 档）<br>• <-1% = 2分（主力出逃）<br>• 无数据 = 7分（中档分，避免拖累总分）<br>**三层调用对齐**：`routes.py` 批量扫描、`recommend.py` 智能推荐、`trend.py` 趋势扫描均预取 `fund_flow_map` 注入评分。 |
| **行业中性化**（**P3-2 后处理**） | ±2 分 | **2026-06-30 新增后处理层**（非独立因子，对 9 因子加权和 `total` 做调整）。数据源为东方财富 clist 接口 `f100` 字段（个股所属板块/行业），持久化到 `stock_basic_info.industry`。<br>• 单行业样本 < 5 → 不调整（统计无意义）<br>• 行业内 `total` ≥ P80 → **+2**（行业龙头溢价）<br>• 行业内 `total` ≤ P20 → **-2**（行业内相对偏弱折价）<br>• 中间档 → 0<br>**三层调用对齐**：`routes.py`/`recommend.py`/`trend.py` 在 `calc_stock_score_v2` 之后调用 `apply_industry_neutralization`；`trend_score` 已有 `sectorFactor`(P0-1) 做环境修正，不叠加本层，避免双重修正。 |

#### 9.2 选股硬性过滤规则

在生成推荐名单前，系统会执行以下硬性拦截，确保“实战可操作性”：

- **基本面安全**：自动过滤名称含 `ST` 或 `退` 的风险股。
- **成交真实性**：**分级软惩罚**（非硬过滤）。涨停股天量开板（vol>8x+outer<45%）重罚-5分，放量偏弱（vol>5x+outer<48%）轻罚-3分；非涨停股放量外盘不占优时扣-5分。正常封板不罚。
- **拥抱龙头**：**完全取消**对高涨幅股的硬过滤。允许涨停股入选，旨在发掘次日高开溢价或龙回头机会。

#### 9.3 P2/P3 优化说明（2026-06-30）

承接 P0（板块效应/连板梯队）/ P1（主力净流入/涨停质量），P2/P3 进一步补齐筹码结构、量价形态、动态阈值、行业中性化四项：

**P2-1 筹码结构修正系数（`trend.py _calc_chip_factor`）**
无真实 CYQ 筹码数据，用 4 个代理指标组合刻画筹码结构，作为乘性修正系数叠加到趋势总分（钳制 ±8%）：
- 子项1 成本压力位：现价 vs MA60/MA120（双线上方 +3% / 双线下方 -5%）
- 子项2 换手率松动度：1-3% 筹码锁定 +2% / >15% 筹码松动 -2%
- 子项3 主力筹码动向：主力净流入占比 ≥3% 吸筹 +3% / ≤-3% 派发 -3%
- 子项4 量能稳定性 CV：≤0.3 沉淀 +2% / >0.8 紊乱 -2%

**P2-2 量价6形态分类（`services/pattern.py detect_volume_price_pattern`）**
识别 6 种互斥量价形态（优先级：突破类 > 滞涨/背离类 > 回调类 > 横盘类），作为乘性修正系数（±4%）：
- 放量突破 +4% / 倍量反包 +3% / 缩量回调 +2%
- 放量滞涨 -4% / 量价背离 -3% / 缩量横盘 -1%

**P3-1 动态阈值（`trend.py _classify_market_regime` + `_get_dynamic_thresholds`）**
按 `market_regime(bull/bear/range) × pattern` 三级优先覆盖 D1/D3/D4 扣分阈值（精确匹配 > regime 默认 > 全局默认）：
- 牛市：放宽乖离惩罚（追高容忍），收紧破位扣分
- 熊市：收紧乖离/回撤档位（追高风险大、回撤容忍收紧）
- 形态特化：放量滞涨/量价背离/缩量横盘扣分加重
- 大盘状态由 sh000001 现价 vs MA20 + 当日涨跌联合判定

**P3-2 行业中性化（`strategy.py apply_industry_neutralization`）**
对 `calc_stock_score_v2` 输出的 `total` 按行业 P20/P80 分位数做 ±2 后处理调整（见 §9.1 末行），三层调用对齐。单行业样本 <5 不调整。

### 10. 统一评级阈值（`client-vue/src/utils/score.js` `getRating`）

为消除前端"左侧短标签（看涨/观望/看跌）"与"右侧详细标签（中性观望等）"显示矛盾，**全站统一使用一份阈值**：

| 分数区间 | 短标签 | 详细标签 | 颜色 |
|----------|--------|----------|------|
| **≥ 80** | 看涨 | 强烈看涨 | 红 (--red / #dc2626) |
| **70 – 79** | 看涨 | 建议关注 | 红 (--red / #dc2626) |
| **55 – 69** | 观望 | 中性观望 | 橙 (--orange / #f59e0b) |
| **40 – 54** | 回调 | 谨慎回调 | 橙 (--orange / #f59e0b) |
| **< 40** | 看跌 | 强烈避险 | 绿 (--green / #16a34a) |

使用方式：
```js
import { getRating } from '@/utils/score'
const r = getRating(stock.score)   // { label, short, color }
```

涉及统一阈值的页面：实时行情（TabQuote）、趋势发现（TabTrend）、股票筛选（TabScan）、策略因子（TabStrategy）、智能推荐（TabRecommend）、AI 诊断弹窗（AiDiagModal）。

---

## API 接口

> 完整路由定义见 [server/app/api/routes.py](file:///d:/Desktop/guizhou/gupiao/server/app/api/routes.py)（48 个 `/api/*` 端点）+ [server/app/main.py](file:///d:/Desktop/guizhou/gupiao/server/app/main.py)（`/eastmoney/*` 代理）。按功能模块分组如下：

### 健康检查

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/health` | GET | 健康检查 |

### 股票行情

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/stock/{symbol}` | GET | 单只股票行情（腾讯原始字符串，PlainTextResponse；IndexBar 等依赖 `.text()`） |
| `/api/stock/{symbol}/analysis` | GET | 单只股票分析 JSON（含 `score`/`label`/`details`，2026-07-06 新增；TabQuote 用 `Promise.all` 并行调用本端点 + 上一端点注入 score） |
| `/api/stocks` | GET | 批量股票行情 |

### K线数据

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/kline/{symbol}` | GET | K线数据（支持 `return_realtime=True` 复用实时行情） |
| `/api/kline/{symbol}/minute` | GET | 分时数据 |

### 大盘

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/market-index` | GET | 大盘指数（上证/深证/创业板） |
| `/api/market-predict` | GET | 大盘预测（60s 内存缓存，避免 TabQuote+TabStrategy 同时 mount 触发 500 股扫描两次） |

### 热搜榜

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/hot_search_ranking` | GET | 热搜榜数据 |
| `/api/update-hot_search_ranking` | GET | 手动更新热搜榜 |

### 热门股票

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/hot-stocks` | GET | 热门股票榜单（沪深北三市） |
| `/api/hot-stocks` | POST | 重置热门股票池 |
| `/api/hot-stocks/add` | PUT | 添加到热门池 |
| `/api/hot-stocks/remove` | PUT | 从热门池移除 |
| `/api/hot-stocks-scan` | GET | 热门股票批量扫描评分（batch=100 + `asyncio.gather` 并发） |

### 趋势发现

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/trend-stocks` | GET | 获取趋势股票（含 `scanDate` 实际扫描日期） |
| `/api/scan-trend-stocks` | GET | 扫描趋势股票（`?force=true` 强制重新扫描；Lock/Semaphore 懒初始化避免 --reload 跨 loop） |
| `/api/scan-ma10` | POST | 扫描 MA10 跌破（用户股票池兜底） |
| `/api/trend/backtest` | POST | 手动触发趋势回测（P4-1） |
| `/api/trend/backtest-history` | GET | 回测历史报告列表 |
| `/api/trend/backtest-latest` | GET | 最新回测报告（趋势页徽章用） |
| `/api/trend/backtest-records` | GET | 回测明细记录（支持 `predict_date`/`symbol` 过滤） |

### 推荐记录

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/daily_recommendations` | GET | 查询推荐记录（历史盈亏回填批量并发） |
| `/api/daily_recommendations` | POST | 新增推荐记录 |
| `/api/daily_recommendations/{date}` | DELETE | 删除某日推荐 |
| `/api/generate-recommendations` | POST | 手动触发推荐生成 |

### 每日收评

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/daily-reviews` | GET | 每日收评列表 |
| `/api/daily-review/generate` | POST | 手动生成每日收评 |

### 策略管理

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/strategy/weights` | GET | 策略因子权重（9 因子） |
| `/api/strategy/weights/reset` | POST | 重置为默认权重 |
| `/api/strategy/backtest` | POST | 手动触发策略回测 |
| `/api/strategy/backtest-history` | GET | 策略回测历史 |
| `/api/strategy/predictions` | GET | 策略预测记录 |

### AI 诊断

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/ai-diagnose/{symbol}` | GET | AI 智能诊断（字节方舟/豆包） |
| `/api/ai-config` | GET | AI 配置状态 |

### 自定义扫描池

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/custom-scan-pool` | GET | 获取用户扫描池（含详情） |
| `/api/custom-scan-pool/symbols` | GET | 轻量端点，仅返回代码列表（2026-07-02 新增） |
| `/api/custom-scan-pool` | POST | 批量替换扫描池 |
| `/api/custom-scan-pool/add` | PUT | 添加股票到扫描池 |
| `/api/custom-scan-pool/remove` | PUT | 从扫描池移除 |
| `/api/custom-scan-pool` | DELETE | 清空扫描池 |

### 热门股票映射 / 标签

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/stock-map` | GET | 股票代码→名称映射 |
| `/api/update-stock-map` | POST | 更新股票映射 |
| `/api/stock-tags` | GET | 全部标签 |
| `/api/stock-tags/{symbol}` | GET | 某股票的标签 |
| `/api/stock-tags/{symbol}` | POST | 设置标签 |
| `/api/stock-tags/{symbol}` | DELETE | 删除标签 |
| `/api/stock-concepts/{symbol}` | GET | 某股票的概念板块（东方财富，P0-1 板块动量数据源） |

### 东方财富代理

| 接口 | 方法 | 说明 |
|------|------|------|
| `/eastmoney/{path:path}` | GET | 东方财富推送接口代理到 `push2delay.eastmoney.com`（生产环境统一代理，避免跨域；定义在 `main.py`，用 `requests + asyncio.to_thread`） |

## 技术说明

- 前端：Vue 3 + Vite（`client-vue/`），Vite dev 代理到后端 `/api/*` + `/eastmoney/*`；生产由后端挂载 `static/` 构建产物
- 后端：Python + FastAPI（`server/app/main.py`）
  - 腾讯财经API：获取股票实时行情和K线数据
  - 东方财富API：获取热搜榜/板块数据，并通过 FastAPI 路由 `/eastmoney/*` 代理推送给前端（统一使用 `requests + asyncio.to_thread`，避免 `httpx.AsyncClient` 直连 `push2.eastmoney.com` 的 Server disconnected 问题）
  - 字节方舟/豆包 API：AI 智能诊断（通过 `server/ai_config.json` 配置，OpenAI 兼容格式）
- 数据库：SQLite（`server/gupiao.db`），使用 `sqlite3` 原生连接 + WAL 模式（定义在 `server/db/database.py`；已从 MySQL 迁移，迁移脚本 `migrate_mysql_to_sqlite.py`）
- 存储：通过 `trend_scan_results` 表持久化，内存缓存加速访问
- 热加载：修改 `config.py` 后自动生效，无需重启

## 注意事项

1. 数据仅供参考，不构成投资建议
2. 服务默认端口 3003
3. 所有配置集中在 `server/config.py`，修改后自动热加载
4. 修改前端代码后，只需重启 `server/start.bat` 即可（脚本会自动重新 `pnpm run build`）

## 评分体系架构

项目内存在**两套独立的评分系统**，各 Tab 页使用的评分来源不同：

| Tab 页 | 评分来源 | 依赖策略权重? |
|--------|---------|:----------:|
| **股票筛选** | `calc_stock_score_v2()` — 9 因子日内评分（含 P1-1 主力净流入 + P3-2 行业中性化后处理） | ✅ 是 |
| **智能推荐** | 50% 日内因子 + 50% 趋势因子融合 | ✅ 是 |
| **每日收评**（明日关注） | 三层来源：①智能推荐(50%日内+50%趋势) → ②策略预测(`strategy_predict_records`) → ③趋势扫描高分票 | ✅ 是 |
| **趋势发现** | `is_up_trend()` — 4 维度趋势评分（MA/资金异动/量能/回撤）+ P0-1/P2-1/P2-2 乘性修正 + P3-1 动态阈值 | ❌ 独立体系 |
| **实时行情**（综合评分） | 前端 `utils/score.js` — 纯客户端计算 | ❌ 独立体系 |

**策略权重的修改链路**：策略优化页调整权重 → `strategy_factor_weights` 表更新 → 回测自动微调 → 股票筛选/智能推荐/每日收评的评分实时变化。趋势发现和实时行情的评分完全不受策略权重影响。

详见 [评分函数对比与影响链路图](#评分函数对比) 章节。

### 评分函数对比

```
策略评分 (calc_stock_score_v2)               趋势评分 (is_up_trend)
┌─────────────────────────────────┐     ┌──────────────────────────────┐
│ 9 因子加权 — 日内实时           │     │ 4 维度评分 — 日K线 + 盘中价  │
│ • outer_ratio  外盘占比  权重可调 │     │ D1 MA趋势方向 + 乖离惩罚      │
│ • volume_ratio 量比      权重可调 │     │ D2 资金异动（涨停/连阳）      │
│ • turnover_rate 换手率  权重可调 │     │ D3 量能配合（量比+趋势+稳定） │
│ • change_percent 涨跌幅 权重可调 │     │ D4 风险回撤（距近10日高点）  │
│ • weibi         委比    权重可调 │     │ + 12项扣分(分组互斥)         │
│ • avg_price_dev 均价偏离权重可调 │     │ + 5项加分(多头/倍量/连阳/支点/蓄力)│
│ • amplitude     振幅    权重可调 │     │ + P0-1/P2-1/P2-2 乘性修正族  │
│ • position      仓位评估权重可调 │     │   (板块/筹码/形态, 封顶0.15)  │
│ • main_capital_inflow 主力净流入 │     │ + P3-1 动态阈值(市场×形态)   │
│ + P3-2 行业中性化后处理(±2)     │     │ + 多层强制过滤               │
│                                 │     │                              │
│ → 股票筛选 / 智能推荐 / 收评    │     │ → 趋势发现                   │
└─────────────────────────────────┘     └──────────────────────────────┘
```

## 趋势评分变更日志

| 版本 | 变更内容 | 变更原因 |
|------|----------|----------|
| v1（旧） | 6 项评分（价格站上 MA120 / MA60>MA120 / MA120 斜率 / 近 120 天涨停 / 近 250 天涨停 / 回撤控制）：20+15+20+15+10+10 = 90 | 初版 |
| v2（当前） | 5 项评分（去除 MA60>MA120 冗余项）：25+25+20+10+10 = 90 | MA60>MA120 与"MA120 站上 + MA120 斜率向上 + 收盘价 > MA120"高度重复，去除后原 15 分重新分配给"价格站上 120 日线"（20→25）和"MA120 斜率向上"（20→25） |
| v3（当前） | 4 维度（短期均线趋势/资金异动/量能配合/风险回撤）：30+30+20+20=100；TREND_MIN_SCORE 85→60，TREND_IS_UP_MIN_SCORE 60→35；4 项最重扣分减半（高位-20→-10，双空头-15→-8，破位-12→-6，顶背离-12→-6） | V2 评分（最大 90 分且无扣分项）下趋势池过松、V3 评分（100 分制 + 多档扣分）下趋势池过严（found=0）。经 268 只样本实测：阈值从 85→60、扣分减半后 found 约 10+，分布 60-80 区间合理 |
| v4（当前） | 全面重构评分体系：(1) D1 拆分为趋势方向(20)+乖离惩罚(0~-10)，偏离MA20过远扣分；(2) D3 拆分为量比(10)+量能趋势斜率(5)+量能稳定性(5)，三个子维度评估量的大小/方向/健康度；(3) 扣分项分组互斥（均线/量价/极端事件各组只取最大），防止同类风险重复惩罚；(4) 加分权重大幅提升（多头排列+5→+12，倍量+5→+10，连阳+3→+5），新增加口袋支点+8、低位横盘蓄力+3；(5) 年度分位≥80%增加新高突破豁免（近20日涨≥20%免扣）；(6) 横盘扣分结合250日分位：低位不扣加分、中位减半、高位扣满；(7) 板块动量因子占位（±20%环境修正）；(8) 修复D4/跌停/放量滞涨3个V型反转错杀缺陷 | 综合7项优化建议实施，评分体系从\"惩罚主导\"转为\"奖励优秀+惩罚风险\"的对称结构 |
| v5（当前） | 新增**中期下跌过滤**：在 `is_trend_up` 判定前，若现价在 MA20 下方 **且** 60 日累计跌幅 > 15%，直接 `isUp=False`（不进入评分门槛） | 修复"短线反弹但中长期下跌"的票（如东安动力 K 线 6 个月内 -30%）被误判为趋势向上 |
| v5.2 | **过滤规则 2 修订**：① 中长期均线偏空检测改为斜率+死亡交叉双保险 ② 回撤参照从60日高点改为**全量历史最高点**（覆盖任何时间跨度的下跌）。东安动力类（6 个月缓慢跌 35%+近期反弹 15%）在 v5.1 中因 60 日窗口数据太平凡（跌 5%、回撤 9%）全部绕过滤，v5.2 已修复 | 东安动力 force scan 后仍在趋势池——根因是 v5.1 的 60 日窗口无法覆盖超过 60 天的缓慢下跌 |
| v5.3（当前） | **过滤规则 2 三条件增强**：新增第三条 MA 偏空检测——**现价 < MA120**（股价尚未收复半年线）。和邦生物(sh603077) MA60>MA120 形成伪金叉+MA120 走平 → v5.2 双重检测全跳过，score=62 isUp=True，但距历史高点 3.54 回撤 29.4% 且现价 2.5 仍在 MA120(2.64) 下方。v5.3 通过"现价未收复 MA120"这条务实条件精准拦截此类伪金叉下跌中继 | 实测验证：远东(36.15>>MA120 16.12)、兆易(689>>MA120 327)不受影响 |
| v6（当前） | **P0/P1/P2/P3 四阶段优化**：① 乘性修正系数族（板块动量 ±5% + 筹码结构 ±8% + 量价形态 ±4%，叠加封顶 0.15）；② P3-1 动态阈值（market_regime × pattern 三级优先覆盖 D1/D3/D4 扣分阈值）；③ P0-2 连板高度分层替代单纯涨停次数（5 档识别龙头）；④ P1-1 主力净流入第 9 因子；⑤ P1-2 涨停质量 5 档分层；⑥ P3-2 行业中性化 ±2 后处理 | 解决三大痛点：强势票全 100 无区分度（封顶+缩放）；板块/行业共振无法体现（乘性修正）；牛熊市同一阈值不适配（动态阈值） |
| v7（当前） | **P4 趋势回测验证闭环 + 资金流并发优化**：① P4-1 趋势评分回测（1/3/5 日跑赢大盘胜率，按 predict_date 聚合 + 分数段统计）；② P4-2 `fetch_batch_fund_flow` 分批并发（每批5页 + Semaphore(5)，30s→6s） | 让评分系统能"回头看自己"，反向校验评分体系的区分度 |
| v8（当前） | **综合优化（性能 + 策略 + 清理 + 可观测性）**：① 性能：`/api/market-predict` 60s 内存缓存、`/api/hot-stocks-scan` 批量行情 batch=100 + `asyncio.gather`、`/api/daily_recommendations` 历史盈亏回填批量并发、`get_kline_data(return_realtime=True)` 复用实时行情、`auto_generate_recommendations_task`/`check_market_crash`/`backtest_yesterday_predictions` 三处 gather 化、`/api/custom-scan-pool/symbols` 轻量端点；② 策略：9 因子权重表（B-1，新增 `main_capital_inflow:1.3`）、趋势加分项上限 20（B-2，`TREND_BONUS_BULL_ALIGNMENT=6`/`CONSECUTIVE_YANG=4`）、涨停阈值统一常量 `TREND_LIMIT_UP_THRESHOLD`（B-3）、稳步上行/加速拉升形态区分（B-4，`TREND_BONUS_STEADY_UPTREND=3`/`ACCELERATING=1`）；③ 清理：删除孤儿函数/`stocks-config.js`/`forbidden-keywords.js`/`check_ma10_api` 调试端点（C-1/C-2/C-4）；④ 可观测性：删除 `/__force_reload__` + `/api/debug-eastmoney-review` 两个临时调试接口、8 处 bare except 加 print 日志、17 处 HTTPException 透传 `str(error)`、清理 routes.py 4 个未使用 import；⑤ Bug 修复：`strategy.py` 拆分时漏 `import asyncio` + `fetch_with_retry`/`parse_tencent_batch_data` 导致回测 `accuracy=0.0` | 解决性能瓶颈（500 股扫描串行 → 并发）、评分区分度（全 100 → 封顶+缩放）、可观测性盲点（静默吞异常 → 日志透传） |

## 11. P4 优化（2026-07-01）：趋势回测验证闭环 + 资金流并发优化

### 11.1 P4-1 趋势评分回测验证闭环

**目标**：让趋势评分系统能"回头看自己"，用历史预测 vs 实际涨跌反向校验评分体系的区分度。

**机制**：
1. **预测落库**：每次趋势扫描入选的趋势股，连同当时的修正系数快照（sectorFactor/chipFactor/pattern/marketRegime）落库到 `trend_backtest_records`
2. **N 日回测**：每日 15:30 定时任务扫描所有 `verified_Nd=0` 的历史记录，按 predict_date 分组，并发拉 K 线计算 1/3/5 日累计涨跌
3. **跑赢大盘判定**：`is_correct_Nd = actual_change_Nd > market_change_Nd`（严格大于，基准为上证指数 sh000001）
4. **聚合报告**：按 predict_date 生成 `trend_backtest_reports`，含三周期总胜率 + 分数段（60-70/70-80/80+）胜率
5. **补验机制**：每次回测扫描所有未验证记录（不限日期），避免漏验

**数据库设计**：
- `trend_backtest_records`：三周期字段并存于同一行，UNIQUE(predict_date, symbol)，补验时只 UPDATE 对应周期
- `trend_backtest_reports`：按 predict_date 聚合，三周期统计字段并存，1d 在 D+1 填充、3d 在 D+3、5d 在 D+5

**前端展示**：
- 趋势发现页顶部：昨日回测准确率徽章（1/3/5 日胜率）
- 趋势发现页底部：可折叠回测历史区（三宫格 + 分数段胜率）
- 每张趋势股卡片：昨测✓/✗ 标签

**API 端点**：
- `POST /api/trend/backtest` — 手动触发回测
- `GET /api/trend/backtest-history` — 历史报告列表
- `GET /api/trend/backtest-latest` — 最新报告（徽章用）
- `GET /api/trend/backtest-records` — 明细记录（支持 predict_date/symbol 过滤）

**配置项**（`config.py`）：
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `SCHEDULE_TREND_BACKTEST_TIME` | '15:30' | 回测定时任务时间 |
| `TREND_BACKTEST_PERIODS` | [1, 3, 5] | 回测周期（天） |
| `TREND_BACKTEST_KLINE_CONCURRENCY` | 5 | 批量拉 K 线并发度 |
| `TREND_BACKTEST_MARKET_SYMBOL` | 'sh000001' | 大盘基准 |
| `TREND_BACKTEST_SCORE_SEGMENTS` | [(60,70),(70,80),(80,101)] | 分数段 |

### 11.2 P4-2 主力净流入并发分页优化

**问题**：`fetch_batch_fund_flow` 原顺序分页拉取 60 页（每页 100 条），耗时约 30s，盘中扫描时拖慢整体节奏。

**优化**：改为分批并发（每批 5 页 + `asyncio.Semaphore(5)`），保留 `requests + asyncio.to_thread` 模式（避免 Server disconnected），预计耗时降至 ~6s。

**约束遵守**：
- HTTP 请求东方财富仍用 `requests + asyncio.to_thread`（非 httpx.AsyncClient）
- 单页 100 条限制不变（push2delay 接口硬限制）
- 字段 `f62`=主力净流入、`f21`=流通市值、`f100`=所属板块
- `_to_float()` 容错 `'-'`/`None`/`''`

## 系统更新日志（2026-06 ~ 07）

| 日期 | 模块 | 变更 |
|------|------|------|
| 2026-07-06 | 可观测性 | **P0 异常处理改造**：① 删除 `/__force_reload__` + `/api/debug-eastmoney-review` 两个临时调试接口；② 8 处 bare except 加 print 日志（`ai-diagnose` 大盘指数/标签/JSON 解析、`hot-stocks-scan` 大盘涨跌幅、`hot-stocks` 行情批量、`daily_recommendations` 历史行情、`backtest-history` JSON 解析 ×2）；③ 17 处 HTTPException detail 透传 `str(error)`（原固定文案）；④ `services/trend.py:1396` `scan_trend_scan_results` 大盘涨跌幅失败加日志；⑤ 清理 `routes.py` 4 个未使用 import（`is_up_trend`/`calc_buy_sell_points`/`parse_realtime_fields`/`has_delisting_risk`） |
| 2026-07-03 | Bug 修复 | **回测准确率为 0 修复**：`strategy.py` 从 main.py 拆分时漏 `import asyncio` + `fetch_with_retry`/`parse_tencent_batch_data`，导致 `backtest_yesterday_predictions` 调用 `asyncio.gather` 抛 `NameError` 被 except 吞掉，`actual_change_map` 始终为空，44 条预测全部 `verified=0`，报告 `accuracy=0.0`。补全 import + 增加批量拉取失败时的降级单只拉取兜底 |
| 2026-07-02 | 性能 | **批次 1（A-P0/A-P1）并发优化**：`/api/market-predict` 60s 内存缓存（避免 TabQuote+TabStrategy 同时 mount 触发 500 股扫描两次）；`/api/hot-stocks-scan` 批量行情 batch=100 + `asyncio.gather`；`/api/daily_recommendations` 历史盈亏回填批量并发（原 29 次串行 4-9s → 单次批量）；`get_kline_data(return_realtime=True)` 复用实时行情；`auto_generate_recommendations_task`/`check_market_crash`/`backtest_yesterday_predictions` 三处 gather 化；新增 `/api/custom-scan-pool/symbols` 轻量端点；前端 TabTrend Promise.all 并发、TabScan 30s 刷新 |
| 2026-07-02 | 策略 | **批次 2（B-1~B-4）评分优化**：B-1 9 因子权重表（新增 `main_capital_inflow:1.3`）；B-2 趋势加分项上限 20（`TREND_BONUS_BULL_ALIGNMENT=6`/`CONSECUTIVE_YANG=4`/`max_bonus=20`）；B-3 涨停阈值统一常量 `TREND_LIMIT_UP_THRESHOLD`（替代散落的 9.8 字面量）；B-4 稳步上行/加速拉升形态区分（`TREND_BONUS_STEADY_UPTREND=3`/`ACCELERATING=1`） |
| 2026-07-02 | 清理 | **批次 3（C-1/C-2/C-4）代码清理**：C-1 删除 3 个孤儿函数；C-2 删除 `stocks-config.js`/`forbidden-keywords.js`；C-4 删除 `check_ma10_api` 调试端点 |
| 2026-07-01 | P4-1/P4-2 | **趋势评分回测验证闭环 + 资金流并发优化**：P4-1 新建 `trend_backtest.py`，新增 `trend_backtest_records`/`trend_backtest_reports` 两表 + 7 个 DB 函数 + 4 个 API；趋势扫描后自动落库预测记录，每日 15:30 定时回测 1/3/5 日跑赢大盘胜率（按分数段统计）；前端趋势发现页新增回测徽章/历史区/卡片标签三处增强。P4-2 `fetch_batch_fund_flow` 顺序分页（30s）改为分批并发（每批5页+Semaphore(5)，~6s）。补齐 `main.py` 热加载白名单遗漏的 `SCHEDULE_TREND_SCAN_*` |
| 2026-06-30 | 趋势评分 | **P0/P1/P2/P3 四阶段优化全量落地**：P0-1 板块动量修正（`_calc_sector_factor`，±5%）；P0-2 连板高度分层（`_count_max_consecutive_boards`，5 档识别龙头）；P1-1 主力净流入第 9 因子（`_score_main_inflow`，三层调用对齐）；P1-2 涨停质量 5 档分层（委比+换手+量比代理封板质量）；P2-1 筹码结构修正（`_calc_chip_factor`，4 代理指标 ±8%）；P2-2 量价 6 形态分类（`pattern.py`，±4%）；P3-1 动态阈值（`_classify_market_regime` + `_get_dynamic_thresholds`，市场×形态三级优先）；P3-2 行业中性化（`apply_industry_neutralization`，±2 后处理）。乘性修正族三系数叠加封顶 0.15 防溢出 |
| 2026-06-30 | 数据层 | **`fetch_batch_fund_flow` 修复 4 个 bug**：① httpx 直连 `push2.eastmoney.com` 被断连 → 改用 `requests` + `push2delay` + `asyncio.to_thread`；② `f85` 字段误用为流通市值（实为 PE）→ 改用 `f21`；③ `pz=5000` 在 push2delay 上无效（单页上限 100）→ 分页拉取；④ 未公布字段 `'-'` 导致 float 异常 → `_to_float` 容错 |
| 2026-06-30 | 调度 | **scheduler.py 修复**：`from services.trend import` 漏 `scan_trend_scan_results`，导致定时任务持续 `name 'scan_trend_scan_results' is not defined` 报错。补齐 import 后定时扫描（09:00-15:00 每 30 分钟）恢复正常 |
| 2026-06-30 | 文档 | README §2.7/§5/§9.1/§9.3 同步 P0-P3 实现；§3 表格修正"非主板"为扣分项而非排除项；§评分体系架构 + §评分函数对比图从 8 因子更新为 9 因子；§9.1 第 9 因子字段从 f85 修正为 f21 |
| 2026-06-25 | 趋势发现 | **高分股自动入池**：扫描完成后，得分 ≥ `TREND_AUTO_ADD_POOL_THRESHOLD`（默认 80 分）的趋势股自动写入 `user_scan_pool`；阈值在 `server/app/config.py` 中配置，热加载生效 |
| 2026-06-24 | AI 诊断 | 修复 Agnes AI 域名错误（`api.agnes-ai.com` → `apihub.agnes-ai.com`），并修正豆包 endpoint ID（模型名需传入 `ep-...`） |
| 2026-06-23 | AI 诊断 | **AI 服务商切换与 URL 兼容**：从 DeepSeek 切换到 Agnes AI，代码中 `baseUrl` 支持末尾已含 `/v1` 的 URL，避免拼出 `/v1/v1/chat/completions` 导致 404；错误提示通用化为 "AI API错误" |
| 2026-06-22 | 策略评分 | **V4.2 涨停股分级防误杀**：两条放量出货检测规则不再对涨停股一刀切惩罚。涨停时按封板质量分级——天量开板(vol>8x+outer<45%)重罚 -5/+项、放量偏弱(vol>5x+outer<48%)轻罚 -3/项、温和封板(vol≤5x)不罚。非涨停股保持原规则 |
| 2026-06-22 | 趋势评分 | **v5.1 过滤规则 2**：新增三层条件联合过滤（MA60+MA120 双空头 + 60日跌幅>20% + 距60日高点回撤>15%）——已被v5.2取代 |
| 2026-06-22 | UI | 所有 Tab 页移除 `page-header` 标题行（实时行情/股票筛选/趋势发现/智能推荐/每日收评/策略优化），精简顶部空间 |
| 2026-06-22 | UI | 趋势发现股票池表格新增排序功能：最新价、连涨/跌列表头可点击切换升序/降序 |
| 2026-06-22 | UI | 趋势发现股票池表格名称列修复居中（CSS `text-align: left` → `center`） |
| 2026-06-22 | Bug | **服务层导入完整性全量排查**：4 个文件 7 处缺失导入修复（`ai.py`/`strategy.py`/`trend.py`/`stock.py`），根因为"从 main.py 拆分时未迁移 import" |
| 2026-06-22 | UI | **AI 诊断弹窗（AiDiagModal.vue）全面重设计**：股票头部蓝白渐变卡片、方向 badge 五档渐变色、置信度/五维评分条 8px 渐变、多维分析独立卡片左边框分色、核心观点 💡 卡片、关键信号渐变背景+▲▼标记、风险/建议统一 `.diag-card` 体系 |
| 2026-06-18 | 趋势评分 | **v5 中期下跌过滤**：MA20 下方 + 60 日跌幅 > 15% → isUp=False（修复东安动力类中长期下跌票误入榜） |
| 2026-06-18 | 配置 | FORBIDDEN_KEYWORDS 补充保险/银行/铁路/航空/航运等公司字面关键词（中国人寿/平安/太保/中铁/国航等） |
| 2026-06-18 | 部署 | 新增 `server/start.py` 跨平台统一启动入口；`start.bat` / `start.command` 简化为薄壳 |
| 2026-06-18 | 自选扫描池 | `isUp_trend_pool` 防御性剥离 coroutine + 详细 traceback |
| 2026-06-18 | 东方财富代理 | 修复 502 Bad Gateway（每次请求新建 AsyncClient + 反爬头） |
| 2026-06-15 | 部署脚本 | `server/start.bat` 新增前端构建步骤（`pnpm install` + `pnpm run build`），修改前端后无需手动构建 |
| 2026-06-15 | 评分 UI | 新增 `getRating()` 统一评级函数，消除"左侧看涨/右侧中性观望"矛盾；TabQuote/TabScan/TabTrend/TabStrategy/TabRecommend/AiDiagModal 全部接入同一阈值（70/55/40 边界） |
| 2026-06-15 | 后端代理 | FastAPI 新增 `/eastmoney/{path:path}` 代理路由，转发到 `https://push2.eastmoney.com`，解决生产环境东方财富接口 404 |
| 2026-06-15 | 行情 UI | "综合评分模型"卡片重做：左侧大号分数+短标签、右侧详细标签、进度条带百分比、分项卡片化、暗色模式适配 |
| 2026-06-15 | 收评 UI | 顶部情绪条文字对比度修复（默认色 + neutral 类）、0 值/空值占位优化、分组分隔线、收评摘要取消折叠直接展示 |
| 2026-06-15 | 趋势评分 | V3 评分阈值/扣分项调整：`TREND_MIN_SCORE` 85→60（先调到 55，再上调到 60）、`TREND_IS_UP_MIN_SCORE` 60→35；4 项最重扣分减半（高位-20→-10、双空头-15→-8、跌破MA20-12→-6、量价顶背离-12→-6）。修复"全市场 found=0 导致 4 维度显示 0"的 bug |
