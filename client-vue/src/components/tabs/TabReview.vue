<template>
  <div class="review-page">
    <div class="page-header">
      <div class="page-title">每日收评</div>
      <div class="page-subtitle">AI 智能复盘 · 情绪雷达 · 操作建议</div>
    </div>

    <div v-if="reviews.length === 0" class="empty-state">
      <div class="empty-icon">📊</div>
      <div>暂无收评数据</div>
    </div>

    <div v-else>
      <div v-for="review in pagedReviews" :key="review.date" class="review-card" :class="{ latest: currentPage === 1 }">
        <!-- 头部日期 -->
        <div class="review-header">
          <div class="review-date-block">
            <span class="review-date">{{ review.date }}</span>
            <span v-if="currentPage === 1" class="tag-bull">最新</span>
          </div>
          <div class="ai-badge" v-if="review.aiAnalysis">
            <span class="ai-dot"></span>
            <span>AI 智能分析</span>
          </div>
        </div>

        <!-- 1) 顶部 KPI 卡条 -->
        <div class="kpi-grid" v-if="review.aiAnalysis">
          <!-- 情绪评分卡（带进度环） -->
          <div class="kpi-card kpi-sentiment" :class="getSentimentClass(review.aiAnalysis.sentimentScore)">
            <div class="kpi-label">市场情绪</div>
            <div class="kpi-sentiment-body">
              <div class="score-ring" :style="scoreRingStyle(review.aiAnalysis.sentimentScore)">
                <div class="score-ring-inner">
                  <div class="score-value">{{ review.aiAnalysis.sentimentScore }}</div>
                  <div class="score-unit">/100</div>
                </div>
              </div>
              <div class="sentiment-info">
                <div class="sentiment-tag">{{ review.aiAnalysis.sentimentLabel }}</div>
                <div class="sentiment-desc">{{ getSentimentDesc(review.aiAnalysis.sentimentScore) }}</div>
              </div>
            </div>
          </div>

          <!-- 涨停 -->
          <div class="kpi-card kpi-up" @click="openLimitList(review, 'up')">
            <div class="kpi-label">涨停家数</div>
            <div class="kpi-value up">{{ getTotalLimitUp(review) }}</div>
            <div class="kpi-meta">
              <span class="up-arrow">▲</span>
              <span>赚钱效应</span>
            </div>
          </div>

          <!-- 跌停 -->
          <div class="kpi-card kpi-down" @click="openLimitList(review, 'down')">
            <div class="kpi-label">跌停家数</div>
            <div class="kpi-value down">{{ getTotalLimitDown(review) }}</div>
            <div class="kpi-meta">
              <span class="down-arrow">▼</span>
              <span>亏钱效应</span>
            </div>
          </div>

          <!-- 涨跌停比 + 赚钱效应 -->
          <div class="kpi-card kpi-ratio">
            <div class="kpi-label">涨跌停比</div>
            <div class="kpi-value" :class="getTotalLimitUp(review) >= getTotalLimitDown(review) ? 'up' : 'down'">
              {{ getLimitRatioSimple(review) }}
            </div>
            <div class="kpi-meta">
              <span :class="['effect-tag', 'effect-' + getProfitEffect(review)]">
                赚钱效应 · {{ getProfitEffect(review) === '强' ? '强' : getProfitEffect(review) === '中' ? '中' : '弱' }}
              </span>
            </div>
          </div>
        </div>

        <!-- 2) AI 智能分析三栏 -->
        <div class="ai-analysis-section" v-if="review.aiAnalysis">
          <div class="section-title-row">
            <div class="section-title-small">
              <span class="ai-icon">🤖</span>
              <span>AI 智能分析结论</span>
            </div>
            <div class="ai-tag">基于 {{ review.aiAnalysis.highlights.length }} 项数据综合评估</div>
          </div>

          <div class="ai-grid">
            <!-- 操作建议 -->
            <div class="ai-card" :class="['ai-card-' + review.aiAnalysis.operationAdvice.level]">
              <div class="ai-card-header">
                <span class="ai-card-icon">{{ getAdviceIcon(review.aiAnalysis.operationAdvice.level) }}</span>
                <span class="ai-card-title">操作建议</span>
                <span class="ai-card-badge" :class="review.aiAnalysis.operationAdvice.level">
                  {{ review.aiAnalysis.operationAdvice.title.split('：')[0] }}
                </span>
              </div>
              <div class="ai-card-title-main">{{ review.aiAnalysis.operationAdvice.title }}</div>
              <div class="ai-card-detail">{{ review.aiAnalysis.operationAdvice.detail }}</div>
            </div>

            <!-- 明日展望 -->
            <div class="ai-card ai-card-outlook">
              <div class="ai-card-header">
                <span class="ai-card-icon">🔭</span>
                <span class="ai-card-title">明日展望</span>
              </div>
              <div class="ai-card-title-main">{{ review.aiAnalysis.tomorrowOutlook.title }}</div>
              <div class="ai-card-detail">{{ review.aiAnalysis.tomorrowOutlook.detail }}</div>
            </div>

            <!-- 风险提示 -->
            <div class="ai-card" :class="['ai-card-risk-' + review.aiAnalysis.riskWarning.level]">
              <div class="ai-card-header">
                <span class="ai-card-icon">{{ getRiskIcon(review.aiAnalysis.riskWarning.level) }}</span>
                <span class="ai-card-title">风险提示</span>
                <span class="ai-card-badge" :class="review.aiAnalysis.riskWarning.level">
                  {{ getRiskBadgeText(review.aiAnalysis.riskWarning.level) }}
                </span>
              </div>
              <div class="ai-card-title-main">{{ review.aiAnalysis.riskWarning.title }}</div>
              <div class="ai-card-detail">{{ review.aiAnalysis.riskWarning.detail }}</div>
            </div>
          </div>

          <!-- 亮点摘要 chips -->
          <div v-if="review.aiAnalysis.highlights.length" class="highlights-row">
            <span class="highlights-label">关键数据：</span>
            <span v-for="(h, i) in review.aiAnalysis.highlights" :key="i" class="highlight-chip">{{ h }}</span>
          </div>
        </div>

        <!-- 3) 大盘指数 -->
        <div v-if="review.market && Object.keys(review.market).length" class="review-section">
          <div class="section-title-row">
            <div class="section-title-small">📈 大盘指数</div>
          </div>
          <div class="market-grid-3">
            <div v-for="(idx, code) in review.market" :key="code" class="index-card">
              <div class="index-name">{{ idx.name }}</div>
              <div class="index-price" :class="idx.changePercent >= 0 ? 'up' : 'down'">{{ idx.price }}</div>
              <div class="index-change-row">
                <span class="index-change" :class="idx.changePercent >= 0 ? 'up' : 'down'">
                  {{ idx.changePercent >= 0 ? '+' : '' }}{{ idx.changePercent?.toFixed(2) }}%
                </span>
                <span class="index-detail" v-if="idx.open">开 {{ idx.open }}</span>
              </div>
              <div class="index-range" v-if="idx.high && idx.low">
                <div class="range-bar">
                  <div class="range-dot" :style="{ left: getRangePercent(idx) + '%' }"></div>
                </div>
                <div class="range-meta">
                  <span class="low">{{ idx.low }}</span>
                  <span class="high">{{ idx.high }}</span>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- 4) 板块 + 个股热度（左右两栏） -->
        <div class="dual-section">
          <!-- 左：板块轮动 -->
          <div class="dual-col">
            <div class="section-title-row">
              <div class="section-title-small">🔄 板块轮动</div>
            </div>
            <div v-if="getIndustryTop(review).length" class="mini-section">
              <div class="mini-label up">领涨行业</div>
              <div class="mini-pill-list">
                <div v-for="(s, i) in getIndustryTop(review)" :key="s.code" class="mini-pill up" @click="openSectorModal(s)">
                  <span class="mini-pill-rank">{{ i + 1 }}</span>
                  <span class="mini-pill-name">{{ s.name }}</span>
                  <span class="mini-pill-change">+{{ s.changePercent?.toFixed(2) }}%</span>
                </div>
              </div>
            </div>
            <div v-if="getIndustryLag(review).length" class="mini-section">
              <div class="mini-label down">领跌行业</div>
              <div class="mini-pill-list">
                <div v-for="(s, i) in getIndustryLag(review)" :key="s.code" class="mini-pill down" @click="openSectorModal(s)">
                  <span class="mini-pill-rank">{{ i + 1 }}</span>
                  <span class="mini-pill-name">{{ s.name }}</span>
                  <span class="mini-pill-change">{{ s.changePercent?.toFixed(2) }}%</span>
                </div>
              </div>
            </div>
            <div v-if="(review.conceptSectors || []).length" class="mini-section">
              <div class="mini-label purple">热门概念（涨）</div>
              <div class="mini-pill-list">
                <div v-for="(s, i) in review.conceptSectors.slice(0, 5)" :key="s.code" class="mini-pill purple" @click="openSectorModal(s)">
                  <span class="mini-pill-rank">{{ i + 1 }}</span>
                  <span class="mini-pill-name">{{ s.name }}</span>
                  <span class="mini-pill-change">+{{ s.changePercent?.toFixed(2) }}%</span>
                </div>
              </div>
            </div>
            <div v-if="!getIndustryTop(review).length && !getIndustryLag(review).length && !(review.conceptSectors || []).length" class="empty-tip">暂无板块数据</div>
          </div>

          <!-- 右：个股热度（仅显示热度前5） -->
          <div class="dual-col">
            <div class="section-title-row">
              <div class="section-title-small">🔥 个股热度</div>
              <div class="tab-switch">
                <button :class="['tab-btn', stockTab === 'gainer' ? 'active' : '']" @click="stockTab = 'gainer'">涨幅榜</button>
                <button :class="['tab-btn', stockTab === 'loser' ? 'active' : '']" @click="stockTab = 'loser'">跌幅榜</button>
                <button :class="['tab-btn', stockTab === 'funds' ? 'active' : '']" @click="stockTab = 'funds'">主力资金</button>
              </div>
            </div>

            <div v-if="stockTab === 'gainer'" class="stock-list">
              <template v-if="getTop5Gainers(review).length">
                <div v-for="(s, i) in getTop5Gainers(review)" :key="s.code" class="stock-row up" @click="openStockKline(s)">
                  <span class="stock-rank">{{ i + 1 }}</span>
                  <div class="stock-info">
                    <div class="stock-name">{{ s.name }}</div>
                    <div class="stock-code">{{ s.code }}</div>
                  </div>
                  <div class="stock-data">
                    <div class="stock-change">+{{ s.changePercent?.toFixed(2) }}%</div>
                    <div class="stock-meta" v-if="s.turnoverRate">换手 {{ s.turnoverRate?.toFixed(2) }}%</div>
                  </div>
                  <span class="stock-arrow">›</span>
                </div>
              </template>
              <div v-else class="empty-tip">暂无数据</div>
            </div>

            <div v-else-if="stockTab === 'loser'" class="stock-list">
              <template v-if="getTop5Losers(review).length">
                <div v-for="(s, i) in getTop5Losers(review)" :key="s.code" class="stock-row down" @click="openStockKline(s)">
                  <span class="stock-rank">{{ i + 1 }}</span>
                  <div class="stock-info">
                    <div class="stock-name">{{ s.name }}</div>
                    <div class="stock-code">{{ s.code }}</div>
                  </div>
                  <div class="stock-data">
                    <div class="stock-change">{{ s.changePercent?.toFixed(2) }}%</div>
                    <div class="stock-meta" v-if="s.turnoverRate">换手 {{ s.turnoverRate?.toFixed(2) }}%</div>
                  </div>
                  <span class="stock-arrow">›</span>
                </div>
              </template>
              <div v-else class="empty-tip">暂无数据</div>
            </div>

            <div v-else-if="stockTab === 'funds'" class="stock-list">
              <template v-if="getTop5Funds(review).length">
                <div v-for="(s, i) in getTop5Funds(review)" :key="s.code" class="stock-row up" @click="openStockKline(s)">
                  <span class="stock-rank">{{ i + 1 }}</span>
                  <div class="stock-info">
                    <div class="stock-name">{{ s.name }}</div>
                    <div class="stock-code">{{ s.code }}</div>
                  </div>
                  <div class="stock-data">
                    <div class="stock-change" :class="(s.changePercent || 0) >= 0 ? '' : 'down-c'">{{ (s.changePercent || 0) >= 0 ? '+' : '' }}{{ (s.changePercent || 0).toFixed(2) }}%</div>
                    <div class="stock-meta" v-if="s.mainNetInflow">净流入 {{ formatInflow(s.mainNetInflow) }}</div>
                  </div>
                  <span class="stock-arrow">›</span>
                </div>
              </template>
              <div v-else class="empty-tip">暂无主力资金数据</div>
            </div>

            <div v-else class="empty-tip">暂无数据</div>
          </div>
        </div>

        <!-- 5) 明日重点关注 -->
        <div v-if="(review.tomorrowFocus && review.tomorrowFocus.length)" class="review-section">
          <div class="section-title-row">
            <div class="section-title-small">🎯 明日重点关注</div>
            <span class="section-sub">{{ review.tomorrowFocus.length }} 只</span>
          </div>
          <div class="focus-grid">
            <div v-for="(s, i) in review.tomorrowFocus.slice(0, 6)" :key="s.code" class="focus-card" @click="openStockKline(s)">
              <div class="focus-rank">{{ i + 1 }}</div>
              <div class="focus-body">
                <div class="focus-name">{{ s.name }}</div>
                <div class="focus-code">{{ s.code }}</div>
                <div class="focus-reason" v-if="s.reason">{{ s.reason }}</div>
              </div>
              <div class="focus-score" v-if="s.score > 0">
                <div class="focus-score-val">{{ s.score }}</div>
                <div class="focus-score-lbl">策略分</div>
              </div>
            </div>
          </div>
        </div>

        <!-- 6) 收评摘要 -->
        <div v-if="review.summary" class="summary-section">
          <div class="section-title-row">
            <div class="section-title-small">📝 收评摘要</div>
          </div>
          <div class="review-summary">{{ review.summary }}</div>
        </div>
      </div>

      <div v-if="totalPages > 1" class="history-pagination">
        <button class="page-btn" :disabled="currentPage <= 1" @click="currentPage--">上一页</button>
        <span class="page-info">第 {{ currentPage }} / {{ totalPages }} 页</span>
        <button class="page-btn" :disabled="currentPage >= totalPages" @click="currentPage++">下一页</button>
      </div>
    </div>

    <!-- 板块成分股 Modal -->
    <div v-if="sectorModal.show" class="modal-mask" @click.self="closeSectorModal">
      <div class="modal-content">
        <div class="modal-header">
          <div class="modal-title">
            <span>{{ sectorModal.name }}</span>
            <span class="modal-change" :class="sectorModal.change >= 0 ? 'up' : 'down'">
              {{ sectorModal.change >= 0 ? '+' : '' }}{{ sectorModal.change?.toFixed(2) }}%
            </span>
          </div>
          <button class="modal-close" @click="closeSectorModal">×</button>
        </div>
        <div class="modal-body">
          <div v-if="sectorModal.loading" class="loading-tip">加载成分股中...</div>
          <div v-else-if="sectorModal.error" class="error-tip">{{ sectorModal.error }}</div>
          <div v-else-if="sectorModal.stocks.length" class="sector-stock-list">
            <div v-for="(s, i) in sectorModal.stocks" :key="s.code" class="sector-stock-row" @click="openStockKline(s); closeSectorModal()">
              <span class="stock-rank">{{ i + 1 }}</span>
              <div class="stock-info">
                <div class="stock-name">{{ s.name }}</div>
                <div class="stock-code">{{ s.code }}</div>
              </div>
              <div class="stock-data">
                <div class="stock-change" :class="s.changePercent >= 0 ? 'up' : 'down'">
                  {{ s.changePercent >= 0 ? '+' : '' }}{{ s.changePercent?.toFixed(2) }}%
                </div>
                <div class="stock-meta" v-if="s.mainNetInflow != null">
                  {{ s.mainNetInflow > 0 ? '净流入' : '净流出' }} {{ formatInflow(Math.abs(s.mainNetInflow)) }}
                </div>
              </div>
              <span class="stock-arrow">›</span>
            </div>
          </div>
          <div v-else class="empty-tip">暂无成分股数据</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, reactive } from 'vue'
import { apiFetch } from '../../utils/helpers'

const emit = defineEmits(['open-kline'])

const reviews = ref([])
const currentPage = ref(1)
const stockTab = ref('gainer')
const PAGE_SIZE = 1

const sectorModal = reactive({ show: false, name: '', change: 0, code: '', loading: false, error: '', stocks: [] })

const totalPages = computed(() => Math.ceil(reviews.value.length / PAGE_SIZE))
const pagedReviews = computed(() => {
  const start = (currentPage.value - 1) * PAGE_SIZE
  return reviews.value.slice(start, start + PAGE_SIZE)
})

const CACHE_KEY = 'review_enrich_cache'
const CACHE_TTL = 10 * 60 * 1000

function getCachedEnrichData() {
  try {
    const raw = localStorage.getItem(CACHE_KEY)
    if (!raw) return null
    const c = JSON.parse(raw)
    if (Date.now() - c.ts > CACHE_TTL) { localStorage.removeItem(CACHE_KEY); return null }
    return c.data
  } catch { return null }
}
function setCachedEnrichData(data) {
  try { localStorage.setItem(CACHE_KEY, JSON.stringify({ ts: Date.now(), data })) } catch {}
}

async function fetchEnrichData() {
  const cached = getCachedEnrichData()
  if (cached) return cached
  try {
    const [indJson, conJson, gainJson, loseJson] = await Promise.all([
      fetch('/eastmoney/api/qt/clist/get?fid=f3&po=1&pz=20&pn=1&np=1&fltt=2&invt=2&ut=b2884a393a59ad64002292a3e90d46a5&fs=m:90+t:2+f:!50&fields=f2,f3,f12,f14,f62').then(r => r.ok ? r.json() : null).catch(() => null),
      fetch('/eastmoney/api/qt/clist/get?fid=f3&po=1&pz=8&pn=1&np=1&fltt=2&invt=2&ut=b2884a393a59ad64002292a3e90d46a5&fs=m:90+t:3+f:!50&fields=f2,f3,f12,f14,f62').then(r => r.ok ? r.json() : null).catch(() => null),
      fetch('/eastmoney/api/qt/clist/get?fid=f3&po=1&pz=10&pn=1&np=1&fltt=2&invt=2&ut=b2884a393a59ad64002292a3e90d46a5&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048&fields=f2,f3,f8,f12,f14,f62').then(r => r.ok ? r.json() : null).catch(() => null),
      fetch('/eastmoney/api/qt/clist/get?fid=f3&po=0&pz=10&pn=1&np=1&fltt=2&invt=2&ut=b2884a393a59ad64002292a3e90d46a5&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048&fields=f2,f3,f8,f12,f14,f62').then(r => r.ok ? r.json() : null).catch(() => null),
    ])
    const result = { industrySectors: [], industrySectorsLag: [], conceptSectors: [], topGainers: [], topLosers: [] }
    if (indJson?.data?.diff) {
      for (const it of indJson.data.diff) {
        const e = { name: it.f14, code: it.f12, changePercent: it.f3, mainNetInflow: it.f62 }
        ;(it.f3 || 0) >= 0 ? result.industrySectors.push(e) : result.industrySectorsLag.push(e)
      }
    }
    if (conJson?.data?.diff) {
      for (const it of conJson.data.diff) result.conceptSectors.push({ name: it.f14, code: it.f12, changePercent: it.f3, mainNetInflow: it.f62 })
    }
    if (gainJson?.data?.diff) {
      for (const it of gainJson.data.diff) {
        if (Math.abs(it.f3 || 0) < 20) result.topGainers.push({ name: it.f14, code: it.f12, changePercent: it.f3, turnoverRate: it.f8, mainNetInflow: it.f62 })
      }
    }
    if (loseJson?.data?.diff) {
      for (const it of loseJson.data.diff) {
        if (Math.abs(it.f3 || 0) < 20) result.topLosers.push({ name: it.f14, code: it.f12, changePercent: it.f3, turnoverRate: it.f8, mainNetInflow: it.f62 })
      }
    }
    if (result.industrySectors.length || result.conceptSectors.length) setCachedEnrichData(result)
    return result
  } catch { return null }
}

function applyEnrichData(review, data) {
  if (!data) return
  if (!review.industrySectors || !review.industrySectors.length) review.industrySectors = data.industrySectors
  if (!review.industrySectorsLag || !review.industrySectorsLag.length) review.industrySectorsLag = data.industrySectorsLag
  if (!review.conceptSectors || !review.conceptSectors.length) review.conceptSectors = data.conceptSectors
  if (!review.topGainers || !review.topGainers.length) review.topGainers = data.topGainers
  if (!review.topLosers || !review.topLosers.length) review.topLosers = data.topLosers
}

function getTotalLimitUp(review) {
  return (review.limitUpStocks || []).length || 0
}
function getTotalLimitDown(review) {
  return (review.limitDownStocks || []).length || 0
}
function getLimitRatioSimple(review) {
  const u = getTotalLimitUp(review), d = getTotalLimitDown(review)
  if (u === 0 && d === 0) return '--'
  if (d === 0) return `${u}:0`
  return `${u}:${d}`
}
function getIndustryTop(review) {
  if (review.industrySectors && review.industrySectors.length) return review.industrySectors.slice(0, 5)
  return []
}
function getIndustryLag(review) {
  if (review.industrySectorsLag && review.industrySectorsLag.length) return review.industrySectorsLag.slice(0, 5)
  return []
}
// 个股热度：优先用后端收评里的数据，取前5（涨幅榜/跌幅榜 > 涨停股降级补充）
function getTop5Gainers(review) {
  const src = review.topGainers && review.topGainers.length ? review.topGainers : (review.limitUpStocks || [])
  return src.slice(0, 5)
}
function getTop5Losers(review) {
  const src = review.topLosers && review.topLosers.length ? review.topLosers : (review.limitDownStocks || [])
  return src.slice(0, 5)
}
// 主力资金：全市场按主力净流入排序前5（后端 review.topFunds 按 f62 全市场排序取前20，直接 slice(0,5)）
function getTop5Funds(review) {
  return (review.topFunds || []).slice(0, 5)
}
function formatInflow(n) {
  if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(2) + '亿'
  if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(0) + '万'
  return n.toFixed(0)
}
function getProfitEffect(review) {
  const top = review.industrySectors?.[0]?.changePercent || 0
  const lag = review.industrySectorsLag?.[0]?.changePercent || 0
  const sum = top + Math.abs(lag)
  if (sum > 8) return '强'
  if (sum > 3) return '中'
  return '弱'
}
function getRangePercent(idx) {
  if (!idx.high || !idx.low || idx.high === idx.low) return 50
  const cur = parseFloat(idx.price)
  const lo = parseFloat(idx.low)
  const hi = parseFloat(idx.high)
  return Math.max(0, Math.min(100, ((cur - lo) / (hi - lo)) * 100))
}
function scoreRingStyle(score) {
  const color = score >= 60 ? '#16a34a' : score >= 40 ? '#f59e0b' : '#dc2626'
  return { '--ring-color': color, '--ring-pct': score + '%' }
}
function getSentimentClass(score) {
  if (score >= 60) return 'sentiment-bull'
  if (score >= 40) return 'sentiment-neutral'
  return 'sentiment-bear'
}
function getSentimentDesc(score) {
  if (score >= 75) return '资金活跃度高'
  if (score >= 60) return '结构性机会'
  if (score >= 45) return '多空均衡'
  if (score >= 30) return '亏钱效应扩散'
  return '系统性承压'
}
function getAdviceIcon(level) {
  return { bull: '🚀', hold: '⚖️', bear: '🛡️' }[level] || '⚖️'
}
function getRiskIcon(level) {
  return { low: '✅', mid: '⚠️', high: '🚨' }[level] || '✅'
}
function getRiskBadgeText(level) {
  return { low: '低', mid: '中', high: '高' }[level] || '低'
}

// ===== 交互：个股 → K线 =====
function openStockKline(s) {
  if (!s || !s.code) return
  // 东方财富 code 可能是 6 位（600519）或带前缀（sh600519），标准化
  let code = String(s.code)
  if (!/^(sh|sz|bj)/i.test(code)) {
    if (code.startsWith('6')) code = 'sh' + code
    else if (code.startsWith('0') || code.startsWith('3') || code.startsWith('1')) code = 'sz' + code
    else if (code.startsWith('4') || code.startsWith('8')) code = 'bj' + code
  }
  emit('open-kline', code)
}

// ===== 交互：板块 → 弹 Modal =====
async function openSectorModal(s) {
  if (!s || !s.code) return
  sectorModal.show = true
  sectorModal.name = s.name
  sectorModal.change = s.changePercent
  sectorModal.code = s.code
  sectorModal.loading = true
  sectorModal.error = ''
  sectorModal.stocks = []
  try {
    // 板块代码格式: BK0438 → 拼 fs=b:BK0438
    const url = `/eastmoney/api/qt/clist/get?fid=f3&po=1&pz=20&pn=1&np=1&fltt=2&invt=2&ut=b2884a393a59ad64002292a3e90d46a5&fs=b:${encodeURIComponent(s.code)}&fields=f2,f3,f8,f12,f14,f62`
    const r = await fetch(url)
    const j = await r.json()
    const diff = j?.data?.diff || []
    sectorModal.stocks = diff.map(it => ({
      name: it.f14, code: it.f12, changePercent: it.f3, turnoverRate: it.f8, mainNetInflow: it.f62,
    }))
  } catch (e) {
    sectorModal.error = '加载成分股失败: ' + (e?.message || e)
  } finally {
    sectorModal.loading = false
  }
}
function closeSectorModal() {
  sectorModal.show = false
}
function openLimitList(review, kind) {
  // 涨跌停数据已整合在涨幅榜/跌幅榜中，点击跳转到对应 tab
  stockTab.value = kind === 'up' ? 'gainer' : 'loser'
  setTimeout(() => {
    const el = document.querySelector('.stock-list')
    el?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }, 50)
}

async function loadDailyReviews() {
  try {
    const result = await apiFetch('/api/daily-reviews')
    if (result.success && result.data) {
      reviews.value = result.data
      currentPage.value = 1
      // 尝试用实时数据补全（若后端数据缺失），但不阻塞渲染
      fetchEnrichData().then(data => {
        if (data) {
          reviews.value.slice(0, 3).forEach(r => applyEnrichData(r, data))
        }
      }).catch(() => {/* 实时补充失败时静默忽略 */})
      // 若 topGainers 为空，用热搜榜数据做降级
      const latest = reviews.value[0]
      if (latest && !(latest.topGainers?.length)) {
        apiFetch('/api/hot_search_ranking').then(hsr => {
          if (hsr.success && hsr.data?.stocks?.length) {
            const stocks = hsr.data.stocks
            // 按 hotRank 排序，涨幅>0为涨幅榜，<0为跌幅榜
            reviews.value.forEach(r => {
              if (!r.topGainers?.length) r.topGainers = stocks.filter(s => (s.changePercent || 0) > 0).sort((a, b) => (b.changePercent || 0) - (a.changePercent || 0)).slice(0, 5).map(s => ({ name: s.name, code: s.code, changePercent: s.changePercent, turnoverRate: 0 }))
              if (!r.topLosers?.length) r.topLosers = stocks.filter(s => (s.changePercent || 0) < 0).sort((a, b) => (a.changePercent || 0) - (b.changePercent || 0)).slice(0, 5).map(s => ({ name: s.name, code: s.code, changePercent: s.changePercent, turnoverRate: 0 }))
            })
          }
        }).catch(() => {})
      }
    }
  } catch (e) { console.error('加载收评失败:', e) }
}

onMounted(() => {
  loadDailyReviews()
})
</script>

<style scoped>
.review-page { width: 100%; }
.page-header { margin-bottom: var(--spacing-lg); }
.page-title { font-size: 1.4rem; font-weight: 700; color: var(--text-primary); }
.page-subtitle { font-size: 0.85rem; color: var(--text-secondary); margin-top: 4px; }

.empty-state { text-align: center; padding: 3rem 1rem; color: var(--text-secondary); }
.empty-icon { font-size: 3rem; margin-bottom: 0.5rem; opacity: 0.5; }

.review-card { background: var(--bg-secondary); border: 1px solid var(--border-subtle); border-radius: var(--radius-lg); padding: var(--spacing-lg); margin-bottom: var(--spacing-lg); box-shadow: var(--shadow-soft); }
.review-card.latest { border-left: 4px solid var(--accent-danger); }
.review-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: var(--spacing-md); flex-wrap: wrap; gap: 8px; }
.review-date-block { display: flex; align-items: center; gap: 8px; }
.review-date { font-weight: 700; font-size: 1.1rem; color: var(--text-primary); font-family: 'SF Mono', 'JetBrains Mono', monospace; }
.tag-bull { background: var(--accent-danger); color: white; padding: 2px 8px; border-radius: var(--radius-pill); font-size: 0.7rem; font-weight: 600; }

.ai-badge { display: flex; align-items: center; gap: 6px; padding: 4px 10px; background: linear-gradient(135deg, rgba(99, 102, 241, 0.1), rgba(168, 85, 247, 0.1)); border: 1px solid rgba(99, 102, 241, 0.3); border-radius: 999px; font-size: 0.75rem; color: #6366f1; font-weight: 600; }
.ai-dot { width: 6px; height: 6px; border-radius: 50%; background: #6366f1; animation: pulse 1.5s ease-in-out infinite; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }

/* ===== KPI 顶部卡条 ===== */
.kpi-grid { display: grid; grid-template-columns: 1.4fr 1fr 1fr 1fr; gap: 12px; margin-bottom: var(--spacing-md); }
.kpi-card { background: var(--bg-tertiary); border: 1px solid var(--border-subtle); border-radius: var(--radius-md); padding: 14px 16px; transition: all 0.2s; cursor: pointer; }
.kpi-card:hover { border-color: var(--accent-danger); transform: translateY(-1px); box-shadow: var(--shadow-soft); }
.kpi-label { font-size: 0.75rem; color: var(--text-secondary); margin-bottom: 6px; font-weight: 500; }
.kpi-value { font-size: 1.8rem; font-weight: 700; font-family: 'SF Mono', 'JetBrains Mono', monospace; line-height: 1.1; }
.kpi-value.up { color: var(--accent-danger); }
.kpi-value.down { color: var(--accent-success); }
.kpi-meta { display: flex; align-items: center; gap: 4px; font-size: 0.72rem; color: var(--text-secondary); margin-top: 4px; }
.kpi-meta .up-arrow { color: var(--accent-danger); }
.kpi-meta .down-arrow { color: var(--accent-success); }

.kpi-sentiment { cursor: default; }
.kpi-sentiment-body { display: flex; align-items: center; gap: 12px; }
.score-ring { width: 64px; height: 64px; border-radius: 50%; background: conic-gradient(var(--ring-color) var(--ring-pct), var(--border-subtle) 0); display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
.score-ring-inner { width: 52px; height: 52px; border-radius: 50%; background: var(--bg-tertiary); display: flex; flex-direction: column; align-items: center; justify-content: center; }
.score-value { font-size: 1.2rem; font-weight: 700; color: var(--ring-color); line-height: 1; }
.score-unit { font-size: 0.6rem; color: var(--text-secondary); }
.sentiment-info { flex: 1; min-width: 0; }
.sentiment-tag { font-size: 0.95rem; font-weight: 700; color: var(--ring-color); }
.sentiment-desc { font-size: 0.72rem; color: var(--text-secondary); margin-top: 2px; }

.effect-tag { padding: 2px 8px; border-radius: 4px; font-size: 0.7rem; font-weight: 600; }
.effect-强 { background: rgba(220, 38, 38, 0.12); color: #dc2626; }
.effect-中 { background: rgba(245, 158, 11, 0.12); color: #f59e0b; }
.effect-弱 { background: rgba(100, 116, 139, 0.12); color: #64748b; }

/* ===== AI 智能分析三栏 ===== */
.ai-analysis-section { background: linear-gradient(135deg, rgba(99, 102, 241, 0.04), rgba(168, 85, 247, 0.04)); border: 1px solid rgba(99, 102, 241, 0.2); border-radius: var(--radius-md); padding: var(--spacing-md); margin-bottom: var(--spacing-md); }
.ai-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; margin-bottom: 12px; }
.ai-card { background: var(--bg-secondary); border: 1px solid var(--border-subtle); border-radius: var(--radius-md); padding: 12px 14px; position: relative; overflow: hidden; }
.ai-card::before { content: ''; position: absolute; top: 0; left: 0; width: 3px; height: 100%; }
.ai-card-bull::before { background: var(--accent-danger); }
.ai-card-hold::before { background: #f59e0b; }
.ai-card-bear::before { background: var(--accent-success); }
.ai-card-outlook::before { background: #6366f1; }
.ai-card-risk-low::before { background: #16a34a; }
.ai-card-risk-mid::before { background: #f59e0b; }
.ai-card-risk-high::before { background: #dc2626; }
.ai-card-header { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; }
.ai-card-icon { font-size: 1rem; }
.ai-card-title { font-size: 0.78rem; color: var(--text-secondary); font-weight: 600; }
.ai-card-badge { padding: 1px 6px; border-radius: 3px; font-size: 0.65rem; font-weight: 700; margin-left: auto; }
.ai-card-badge.bull { background: rgba(220, 38, 38, 0.12); color: #dc2626; }
.ai-card-badge.hold { background: rgba(245, 158, 11, 0.12); color: #f59e0b; }
.ai-card-badge.bear { background: rgba(22, 163, 74, 0.12); color: #16a34a; }
.ai-card-badge.low { background: rgba(22, 163, 74, 0.12); color: #16a34a; }
.ai-card-badge.mid { background: rgba(245, 158, 11, 0.12); color: #f59e0b; }
.ai-card-badge.high { background: rgba(220, 38, 38, 0.12); color: #dc2626; }
.ai-card-title-main { font-size: 0.95rem; font-weight: 700; color: var(--text-primary); margin-bottom: 4px; }
.ai-card-detail { font-size: 0.78rem; color: var(--text-secondary); line-height: 1.5; }

.highlights-row { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; padding-top: 8px; border-top: 1px dashed var(--border-subtle); }
.highlights-label { font-size: 0.72rem; color: var(--text-secondary); font-weight: 600; }
.highlight-chip { padding: 3px 8px; background: var(--bg-tertiary); border: 1px solid var(--border-subtle); border-radius: 4px; font-size: 0.72rem; color: var(--text-primary); }

/* ===== 通用 ===== */
.review-section { margin-bottom: var(--spacing-md); }
.section-title-row { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.section-title-small { font-size: 0.85rem; font-weight: 700; color: var(--text-primary); display: flex; align-items: center; gap: 6px; }
.section-sub { font-size: 0.7rem; color: var(--text-secondary); }

/* 大盘指数 */
.market-grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; }
.index-card { background: var(--bg-tertiary); border: 1px solid var(--border-subtle); border-radius: var(--radius-md); padding: 12px 14px; }
.index-name { font-size: 0.78rem; color: var(--text-secondary); margin-bottom: 4px; }
.index-price { font-size: 1.3rem; font-weight: 700; font-family: 'SF Mono', 'JetBrains Mono', monospace; }
.index-change-row { display: flex; align-items: center; gap: 8px; margin-top: 4px; }
.index-change { font-size: 0.8rem; font-weight: 600; }
.index-detail { font-size: 0.7rem; color: var(--text-secondary); }
.index-range { margin-top: 8px; }
.range-bar { position: relative; height: 4px; background: var(--border-subtle); border-radius: 2px; }
.range-dot { position: absolute; top: 50%; transform: translate(-50%, -50%); width: 8px; height: 8px; border-radius: 50%; background: var(--accent-danger); }
.range-meta { display: flex; justify-content: space-between; font-size: 0.65rem; color: var(--text-secondary); margin-top: 4px; font-family: 'SF Mono', 'JetBrains Mono', monospace; }
.index-price.up, .index-change.up, .range-dot { color: var(--accent-danger); }
.index-price.down, .index-change.down { color: var(--accent-success); }

/* 左右两栏 */
.dual-section { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: var(--spacing-md); }
.dual-col { background: var(--bg-tertiary); border: 1px solid var(--border-subtle); border-radius: var(--radius-md); padding: 12px 14px; }

.mini-section { margin-bottom: 10px; }
.mini-section:last-child { margin-bottom: 0; }
.mini-label { font-size: 0.72rem; font-weight: 600; margin-bottom: 6px; }
.mini-label.up { color: var(--accent-danger); }
.mini-label.down { color: var(--accent-success); }
.mini-label.purple { color: #6366f1; }

.mini-pill-list { display: flex; flex-direction: column; gap: 4px; }
.mini-pill { display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-radius: 6px; cursor: pointer; transition: all 0.15s; border: 1px solid transparent; }
.mini-pill:hover { transform: translateX(2px); }
.mini-pill.up { background: rgba(220, 38, 38, 0.06); }
.mini-pill.up:hover { border-color: rgba(220, 38, 38, 0.3); }
.mini-pill.down { background: rgba(22, 163, 74, 0.06); }
.mini-pill.down:hover { border-color: rgba(22, 163, 74, 0.3); }
.mini-pill.purple { background: rgba(99, 102, 241, 0.06); }
.mini-pill.purple:hover { border-color: rgba(99, 102, 241, 0.3); }
.mini-pill-rank { width: 16px; font-size: 0.7rem; color: var(--text-secondary); font-weight: 600; }
.mini-pill-name { flex: 1; font-size: 0.8rem; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.mini-pill-change { font-size: 0.78rem; font-weight: 700; font-family: 'SF Mono', 'JetBrains Mono', monospace; }
.mini-pill.up .mini-pill-change { color: var(--accent-danger); }
.mini-pill.down .mini-pill-change { color: var(--accent-success); }
.mini-pill.purple .mini-pill-change { color: #6366f1; }

/* 个股列表 */
.tab-switch { display: flex; gap: 4px; }
.tab-btn { background: transparent; border: 1px solid var(--border-subtle); padding: 3px 10px; border-radius: 4px; font-size: 0.72rem; color: var(--text-secondary); cursor: pointer; transition: all 0.15s; }
.tab-btn.active { background: var(--accent-danger); color: white; border-color: var(--accent-danger); }
.tab-btn:hover:not(.active) { color: var(--text-primary); }

.stock-list { display: flex; flex-direction: column; gap: 4px; }
.stock-row { display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-radius: 6px; cursor: pointer; transition: all 0.15s; }
.stock-row:hover { background: var(--bg-secondary); }
.stock-rank { width: 18px; height: 18px; border-radius: 4px; background: var(--border-subtle); display: flex; align-items: center; justify-content: center; font-size: 0.7rem; color: var(--text-secondary); font-weight: 600; }
.stock-info { flex: 1; min-width: 0; }
.stock-name { font-size: 0.82rem; color: var(--text-primary); font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.stock-code { font-size: 0.68rem; color: var(--text-secondary); font-family: 'SF Mono', 'JetBrains Mono', monospace; }
.stock-data { text-align: right; }
.stock-change { font-size: 0.85rem; font-weight: 700; font-family: 'SF Mono', 'JetBrains Mono', monospace; }
.stock-change.up, .stock-row.up .stock-change { color: var(--accent-danger); }
.stock-change.down, .stock-row.down .stock-change, .stock-change.down-c { color: var(--accent-success); }
.stock-meta { font-size: 0.65rem; color: var(--text-secondary); }
.stock-arrow { color: var(--text-secondary); font-size: 1.2rem; }

/* 涨跌停 tags */
.stock-tags { display: flex; flex-wrap: wrap; gap: 4px; }
.stock-tag { padding: 3px 8px; border-radius: 4px; font-size: 0.75rem; cursor: pointer; transition: all 0.15s; border: 1px solid transparent; }
.stock-tag.up { background: rgba(220, 38, 38, 0.1); color: #dc2626; }
.stock-tag.down { background: rgba(22, 163, 74, 0.1); color: #16a34a; }
.stock-tag:hover { transform: translateY(-1px); }
.stock-tag.up:hover { border-color: rgba(220, 38, 38, 0.4); }
.stock-tag.down:hover { border-color: rgba(22, 163, 74, 0.4); }
.more-tag { padding: 3px 8px; font-size: 0.7rem; color: var(--text-secondary); background: var(--bg-secondary); border-radius: 4px; }

.empty-tip { padding: 12px; text-align: center; color: var(--text-secondary); font-size: 0.8rem; }

/* 明日关注 */
.focus-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 8px; }
.focus-card { display: flex; align-items: center; gap: 10px; padding: 10px 12px; background: var(--bg-tertiary); border: 1px solid var(--border-subtle); border-radius: var(--radius-md); cursor: pointer; transition: all 0.15s; }
.focus-card:hover { border-color: #6366f1; transform: translateY(-1px); }
.focus-rank { width: 22px; height: 22px; border-radius: 50%; background: linear-gradient(135deg, #6366f1, #a855f7); color: white; display: flex; align-items: center; justify-content: center; font-size: 0.75rem; font-weight: 700; flex-shrink: 0; }
.focus-body { flex: 1; min-width: 0; }
.focus-name { font-size: 0.85rem; font-weight: 600; color: var(--text-primary); }
.focus-code { font-size: 0.7rem; color: var(--text-secondary); font-family: 'SF Mono', 'JetBrains Mono', monospace; }
.focus-reason { font-size: 0.72rem; color: var(--text-secondary); margin-top: 2px; line-height: 1.4; overflow: hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-line-clamp: 1; -webkit-box-orient: vertical; }
.focus-score { text-align: center; padding-left: 8px; border-left: 1px solid var(--border-subtle); }
.focus-score-val { font-size: 1rem; font-weight: 700; color: #6366f1; font-family: 'SF Mono', 'JetBrains Mono', monospace; }
.focus-score-lbl { font-size: 0.65rem; color: var(--text-secondary); }

/* 收评摘要 */
.summary-section { background: var(--bg-tertiary); border: 1px solid var(--border-subtle); border-radius: var(--radius-md); padding: 12px 16px; }
.review-summary { font-size: 0.85rem; color: var(--text-primary); line-height: 1.7; white-space: pre-wrap; }

/* 分页 */
.history-pagination { display: flex; align-items: center; justify-content: center; gap: 12px; padding: 12px; }
.page-btn { background: var(--bg-secondary); border: 1px solid var(--border-subtle); padding: 6px 16px; border-radius: 4px; cursor: pointer; font-size: 0.85rem; color: var(--text-primary); transition: all 0.15s; }
.page-btn:hover:not(:disabled) { border-color: var(--accent-danger); color: var(--accent-danger); }
.page-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.page-info { font-size: 0.85rem; color: var(--text-secondary); }

/* Modal */
.modal-mask { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0, 0, 0, 0.5); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.modal-content { background: var(--bg-secondary); border-radius: var(--radius-lg); width: 90%; max-width: 480px; max-height: 80vh; display: flex; flex-direction: column; box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3); }
.modal-header { display: flex; align-items: center; justify-content: space-between; padding: 14px 18px; border-bottom: 1px solid var(--border-subtle); }
.modal-title { font-size: 1rem; font-weight: 700; color: var(--text-primary); display: flex; align-items: center; gap: 8px; }
.modal-change { font-size: 0.9rem; font-weight: 700; font-family: 'SF Mono', 'JetBrains Mono', monospace; }
.modal-change.up { color: var(--accent-danger); }
.modal-change.down { color: var(--accent-success); }
.modal-close { background: transparent; border: none; font-size: 1.5rem; color: var(--text-secondary); cursor: pointer; padding: 0; width: 28px; height: 28px; border-radius: 4px; transition: all 0.15s; }
.modal-close:hover { background: var(--bg-tertiary); color: var(--text-primary); }
.modal-body { padding: 8px; overflow-y: auto; flex: 1; }
.sector-stock-list { display: flex; flex-direction: column; gap: 2px; }
.sector-stock-row { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border-radius: 6px; cursor: pointer; transition: all 0.15s; }
.sector-stock-row:hover { background: var(--bg-tertiary); }
.loading-tip, .error-tip { padding: 24px; text-align: center; color: var(--text-secondary); font-size: 0.85rem; }
.error-tip { color: #dc2626; }

/* 响应式 */
@media (max-width: 768px) {
  .kpi-grid { grid-template-columns: 1fr 1fr; }
  .ai-grid { grid-template-columns: 1fr; }
  .dual-section { grid-template-columns: 1fr; }
  .market-grid-3 { grid-template-columns: 1fr; }
}
</style>
