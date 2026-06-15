<template>
  <div class="review-page">
    <div class="section-title">每日收评</div>
    <div v-if="reviews.length === 0" style="text-align:center;padding:2rem;color:#64748b;">暂无收评数据</div>
    <div v-else>
      <div v-for="review in pagedReviews" :key="review.date" class="review-card" :class="{ latest: currentPage === 1 }">
        <div class="review-header">
          <span class="review-date">{{ review.date }}</span>
          <span v-if="currentPage === 1" class="tag-bull">最新</span>
          <div class="review-tabs">
            <button v-for="tab in reviewTabs" :key="tab.key" class="review-tab" :class="{ active: activeTab === tab.key }" @click="activeTab = tab.key">{{ tab.label }}</button>
          </div>
        </div>

        <!-- 顶部摘要带：大盘情绪条 -->
        <div class="market-sentiment-bar">
          <div class="sentiment-item">
            <span class="sentiment-label">大盘</span>
            <span class="sentiment-value" :class="getMarketSentiment(review) > 0 ? 'up' : 'down'">{{ getMarketSentiment(review) > 0 ? '普涨' : '普跌' }}</span>
          </div>
          <div class="sentiment-item">
            <span class="sentiment-label">涨停</span>
            <span class="sentiment-value up">{{ getTotalLimitUp(review) }}</span>
          </div>
          <div class="sentiment-item">
            <span class="sentiment-label">跌停</span>
            <span class="sentiment-value down">{{ getTotalLimitDown(review) }}</span>
          </div>
          <div class="sentiment-item">
            <span class="sentiment-label">行业领涨</span>
            <span class="sentiment-value up">{{ getLeadIndustry(review) }}</span>
          </div>
          <div class="sentiment-item">
            <span class="sentiment-label">行业领跌</span>
            <span class="sentiment-value down">{{ getLagIndustry(review) }}</span>
          </div>
        </div>

        <!-- 大盘指数 -->
        <div v-if="review.market && Object.keys(review.market).length" class="review-section">
          <div class="section-title-small">大盘指数</div>
          <div class="market-grid-3">
            <div v-for="(idx, code) in review.market" :key="code" class="index-card">
              <div class="index-name">{{ idx.name }}</div>
              <div class="index-price" :class="idx.changePercent >= 0 ? 'up' : 'down'">{{ idx.price }}</div>
              <div class="index-change-row">
                <span class="index-change" :class="idx.changePercent >= 0 ? 'up' : 'down'">{{ idx.changePercent >= 0 ? '+' : '' }}{{ idx.changePercent?.toFixed(2) }}%</span>
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

        <!-- 行业板块 Tab -->
        <div v-if="activeTab === 'industry' && (getIndustryTop(review).length || getIndustryLag(review).length)" class="review-section">
          <div class="section-title-small">行业板块</div>
          <div class="industry-row">
            <div class="industry-col">
              <div class="industry-col-label up">领涨板块</div>
              <div class="industry-list">
                <div v-for="s in getIndustryTop(review)" :key="s.code" class="industry-pill up">
                  <span class="industry-name">{{ s.name }}</span>
                  <span class="industry-change">+{{ s.changePercent?.toFixed(2) }}%</span>
                </div>
              </div>
            </div>
            <div class="industry-col">
              <div class="industry-col-label down">领跌板块</div>
              <div class="industry-list">
                <div v-for="s in getIndustryLag(review)" :key="s.code" class="industry-pill down">
                  <span class="industry-name">{{ s.name }}</span>
                  <span class="industry-change">{{ s.changePercent?.toFixed(2) }}%</span>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- 概念板块 -->
        <div v-if="activeTab === 'industry' && (review.conceptSectors || []).length" class="review-section">
          <div class="section-title-small">概念板块（涨）</div>
          <div class="industry-row">
            <div class="industry-list" style="flex-direction:row;flex-wrap:wrap;gap:8px;">
              <div v-for="s in review.conceptSectors" :key="s.code" class="industry-pill up">
                <span class="industry-name">{{ s.name }}</span>
                <span class="industry-change">+{{ s.changePercent?.toFixed(2) }}%</span>
              </div>
            </div>
          </div>
        </div>

        <!-- 个股排行 Tab -->
        <div v-if="activeTab === 'stocks' && ((review.topGainers || []).length || (review.topLosers || []).length)" class="review-section">
          <div class="section-title-small">个股涨幅榜</div>
          <div class="stock-rank-list">
            <div v-for="(s, i) in review.topGainers" :key="s.code" class="stock-rank-item up">
              <span class="rank-num">{{ i + 1 }}</span>
              <div class="rank-info">
                <div class="rank-name">{{ s.name }}</div>
                <div class="rank-code">{{ s.code }}</div>
              </div>
              <div class="rank-data">
                <div class="rank-change">+{{ s.changePercent?.toFixed(2) }}%</div>
                <div class="rank-meta" v-if="s.turnoverRate">换手 {{ s.turnoverRate?.toFixed(2) }}%</div>
              </div>
            </div>
          </div>
        </div>

        <div v-if="activeTab === 'stocks' && (review.topLosers || []).length" class="review-section">
          <div class="section-title-small">个股跌幅榜</div>
          <div class="stock-rank-list">
            <div v-for="(s, i) in review.topLosers" :key="s.code" class="stock-rank-item down">
              <span class="rank-num">{{ i + 1 }}</span>
              <div class="rank-info">
                <div class="rank-name">{{ s.name }}</div>
                <div class="rank-code">{{ s.code }}</div>
              </div>
              <div class="rank-data">
                <div class="rank-change">{{ s.changePercent?.toFixed(2) }}%</div>
                <div class="rank-meta" v-if="s.turnoverRate">换手 {{ s.turnoverRate?.toFixed(2) }}%</div>
              </div>
            </div>
          </div>
        </div>

        <!-- 涨跌停明细 Tab -->
        <div v-if="activeTab === 'limit' && ((review.limitUpStocks || []).length || (review.limitDownStocks || []).length)" class="review-section">
          <div class="limit-row">
            <div class="limit-col">
              <div class="limit-col-label up">涨停股 ({{ (review.limitUpStocks || []).length }})</div>
              <div class="stock-tags">
                <span v-for="s in review.limitUpStocks" :key="s.code" class="stock-tag up">{{ s.name }}</span>
                <span v-if="!(review.limitUpStocks || []).length" class="empty-tip">无</span>
              </div>
            </div>
            <div class="limit-col">
              <div class="limit-col-label down">跌停股 ({{ (review.limitDownStocks || []).length }})</div>
              <div class="stock-tags">
                <span v-for="s in review.limitDownStocks" :key="s.code" class="stock-tag down">{{ s.name }}</span>
                <span v-if="!(review.limitDownStocks || []).length" class="empty-tip">无</span>
              </div>
            </div>
          </div>
        </div>

        <!-- 涨停/跌停 数值（从summary解析） -->
        <div v-if="activeTab === 'limit' && parsedData(review).limitUpCount" class="review-section">
          <div class="section-title-small">市场情绪指标</div>
          <div class="sentiment-grid">
            <div class="metric-card up">
              <div class="metric-label">涨停数量</div>
              <div class="metric-value">{{ parsedData(review).limitUpCount }}</div>
              <div class="metric-bar"><div class="metric-bar-fill" :style="{ width: getLimitMetricWidth(parsedData(review).limitUpCount, 100) + '%', background: 'var(--accent-danger)' }"></div></div>
            </div>
            <div class="metric-card down">
              <div class="metric-label">跌停数量</div>
              <div class="metric-value">{{ parsedData(review).limitDownCount }}</div>
              <div class="metric-bar"><div class="metric-bar-fill" :style="{ width: getLimitMetricWidth(parsedData(review).limitDownCount, 100) + '%', background: 'var(--accent-success)' }"></div></div>
            </div>
            <div class="metric-card neutral">
              <div class="metric-label">涨停/跌停比</div>
              <div class="metric-value">{{ getLimitRatio(parsedData(review)) }}</div>
              <div class="metric-sub">{{ getMarketMood(parsedData(review)) }}</div>
            </div>
            <div class="metric-card neutral">
              <div class="metric-label">板块赚钱效应</div>
              <div class="metric-value">{{ getProfitEffect(review) }}</div>
              <div class="metric-sub">基于领涨领跌计算</div>
            </div>
          </div>
        </div>

        <!-- 明日关注 Tab -->
        <div v-if="activeTab === 'tomorrow' && (review.tomorrowFocus || parsedData(review).tomorrowFocus.length)" class="review-section">
          <div class="section-title-small">明日重点关注</div>
          <div class="stock-tags">
            <span v-for="s in (review.tomorrowFocus || []).map(x => x.name || x)" :key="s" class="stock-tag blue">{{ s }}</span>
            <span v-for="s in (review.tomorrowFocus && review.tomorrowFocus.length ? [] : parsedData(review).tomorrowFocus)" :key="'p-'+s" class="stock-tag blue">{{ s }}</span>
          </div>
          <div v-if="review.tomorrowFocus && review.tomorrowFocus[0] && review.tomorrowFocus[0].reason" class="focus-reason">
            <span class="focus-reason-title">推荐理由：</span>
            <span class="focus-reason-text">{{ review.tomorrowFocus[0].reason }}</span>
          </div>
        </div>

        <!-- 收评摘要 -->
        <details class="summary-details" v-if="review.summary">
          <summary>查看完整收评文本</summary>
          <div class="review-summary" style="white-space:pre-line;line-height:1.8;font-size:0.85rem;margin-top:8px;padding:12px;background:var(--bg-tertiary);border-radius:var(--radius-md);">{{ review.summary }}</div>
        </details>
      </div>
      <div v-if="totalPages > 1" class="history-pagination">
        <button class="page-btn" :disabled="currentPage <= 1" @click="currentPage--">上一页</button>
        <span class="page-info">第 {{ currentPage }} / {{ totalPages }} 页</span>
        <button class="page-btn" :disabled="currentPage >= totalPages" @click="currentPage++">下一页</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { apiFetch } from '../../utils/helpers'

const reviews = ref([])
const currentPage = ref(1)
const PAGE_SIZE = 1
const activeTab = ref('industry')
const reviewTabs = [
  { key: 'industry', label: '板块' },
  { key: 'stocks', label: '个股' },
  { key: 'limit', label: '涨跌停' },
  { key: 'tomorrow', label: '明日关注' }
]
const totalPages = computed(() => Math.ceil(reviews.value.length / PAGE_SIZE))
const pagedReviews = computed(() => {
  const start = (currentPage.value - 1) * PAGE_SIZE
  return reviews.value.slice(start, start + PAGE_SIZE)
})

// 缓存：避免重复请求东方财富
const CACHE_KEY = 'review_enrich_cache'
const CACHE_TTL = 10 * 60 * 1000 // 10分钟

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

// 前端从东方财富API补全数据
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

// 解析summary
function parsedData(review) {
  const summary = review.summary || ''
  const result = { industryLead: null, industryLag: null, conceptLead: null, limitUpCount: 0, limitDownCount: 0, tomorrowFocus: [] }
  const leadMatch = summary.match(/行业板块方面，(.+?)领涨[（(]([+-]?\d+\.?\d*)%[）)]/)
  if (leadMatch) result.industryLead = { name: leadMatch[1], change: parseFloat(leadMatch[2]) }
  const lagMatch = summary.match(/(.+?)领跌[（(]([+-]?\d+\.?\d*)%[）)]/)
  if (lagMatch) result.industryLag = { name: lagMatch[1], change: parseFloat(lagMatch[2]) }
  const conceptMatch = summary.match(/概念板块中，(.+?)表现最强[（(]([+-]?\d+\.?\d*)%[）)]/)
  if (conceptMatch) result.conceptLead = { name: conceptMatch[1], change: parseFloat(conceptMatch[2]) }
  const luMatch = summary.match(/涨停(\d+)只/)
  if (luMatch) result.limitUpCount = parseInt(luMatch[1])
  const ldMatch = summary.match(/跌停(\d+)只/)
  if (ldMatch) result.limitDownCount = parseInt(ldMatch[1])
  const focusMatch = summary.match(/明日重点关注[：:]\s*(.+?)(?:等)?[。\n]?$/)
  if (focusMatch) result.tomorrowFocus = focusMatch[1].split(/[、,，]/).map(s => s.trim()).filter(Boolean)
  return result
}

function getMarketSentiment(review) {
  const pd = parsedData(review)
  const gain = (review.topGainers || []).reduce((a, b) => a + (b.changePercent || 0), 0)
  const loss = (review.topLosers || []).reduce((a, b) => a + (b.changePercent || 0), 0)
  return pd.limitUpCount - pd.limitDownCount + (gain - loss)
}
function getTotalLimitUp(review) { return parsedData(review).limitUpCount || (review.limitUpStocks || []).length || 0 }
function getTotalLimitDown(review) { return parsedData(review).limitDownCount || (review.limitDownStocks || []).length || 0 }
function getLeadIndustry(review) {
  const pd = parsedData(review)
  if (pd.industryLead) return pd.industryLead.name
  if (review.industrySectors && review.industrySectors[0]) return review.industrySectors[0].name
  return '--'
}
function getLagIndustry(review) {
  const pd = parsedData(review)
  if (pd.industryLag) return pd.industryLag.name
  if (review.industrySectorsLag && review.industrySectorsLag[0]) return review.industrySectorsLag[0].name
  return '--'
}
function getIndustryTop(review) {
  if (review.industrySectors && review.industrySectors.length) return review.industrySectors.slice(0, 6)
  return []
}
function getIndustryLag(review) {
  if (review.industrySectorsLag && review.industrySectorsLag.length) return review.industrySectorsLag.slice(0, 6)
  return []
}
function getLimitMetricWidth(val, max) { return Math.min(100, (val / max) * 100) }
function getLimitRatio(pd) {
  if (pd.limitDownCount === 0 && pd.limitUpCount === 0) return '--'
  if (pd.limitDownCount === 0) return pd.limitUpCount + ':0'
  return (pd.limitUpCount / pd.limitDownCount).toFixed(2)
}
function getMarketMood(pd) {
  if (pd.limitUpCount > pd.limitDownCount * 2) return '情绪高涨'
  if (pd.limitUpCount > pd.limitDownCount) return '情绪偏暖'
  if (pd.limitUpCount < pd.limitDownCount) return '情绪偏冷'
  return '情绪平稳'
}
function getProfitEffect(review) {
  const top = review.industrySectors?.[0]?.changePercent || 0
  const lag = review.industrySectorsLag?.[0]?.changePercent || 0
  if (top + lag > 8) return '强'
  if (top + lag > 3) return '中'
  return '弱'
}
function getRangePercent(idx) {
  if (!idx.high || !idx.low || idx.high === idx.low) return 50
  const cur = parseFloat(idx.price)
  const lo = parseFloat(idx.low)
  const hi = parseFloat(idx.high)
  return Math.max(0, Math.min(100, ((cur - lo) / (hi - lo)) * 100))
}

async function loadDailyReviews() {
  try {
    const result = await apiFetch('/api/daily-reviews')
    if (result.success && result.data) {
      reviews.value = result.data
      currentPage.value = 1
      // 异步补全数据，不阻塞渲染
      fetchEnrichData().then(data => {
        if (data) {
          reviews.value.slice(0, 3).forEach(r => applyEnrichData(r, data))
        }
      })
    }
  } catch (e) { console.error('加载收评失败:', e) }
}

onMounted(() => {
  loadDailyReviews()
})
</script>

<style scoped>
.review-page { width: 100%; }
.review-card { background: var(--bg-secondary); border: 1px solid var(--border-subtle); border-radius: var(--radius-lg); padding: var(--spacing-lg); margin-bottom: var(--spacing-lg); box-shadow: var(--shadow-soft); }
.review-card.latest { border-left: 4px solid var(--accent-danger); }
.review-header { display: flex; align-items: center; gap: var(--spacing-sm); margin-bottom: var(--spacing-md); flex-wrap: wrap; }
.review-date { font-weight: 700; font-size: 1.1rem; color: var(--text-primary); font-family: 'SF Mono', 'JetBrains Mono', monospace; }
.tag-bull { background: var(--accent-danger); color: white; padding: 2px 8px; border-radius: var(--radius-pill); font-size: 0.7rem; font-weight: 600; }
.review-tabs { margin-left: auto; display: flex; gap: 4px; background: var(--bg-tertiary); border-radius: var(--radius-md); padding: 2px; }
.review-tab { background: transparent; border: 0; padding: 4px 12px; border-radius: var(--radius-sm); font-size: 0.8rem; color: var(--text-secondary); cursor: pointer; transition: all 0.15s; }
.review-tab.active { background: var(--bg-secondary); color: var(--text-primary); font-weight: 600; box-shadow: var(--shadow-xs); }
.review-tab:hover { color: var(--text-primary); }

.market-sentiment-bar { display: flex; flex-wrap: wrap; gap: var(--spacing-md); padding: var(--spacing-md); background: linear-gradient(135deg, var(--bg-tertiary) 0%, var(--bg-canvas) 100%); border-radius: var(--radius-md); margin-bottom: var(--spacing-md); border: 1px solid var(--border-subtle); }
.sentiment-item { display: flex; align-items: baseline; gap: 6px; }
.sentiment-label { font-size: 0.75rem; color: var(--text-tertiary); }
.sentiment-value { font-weight: 700; font-size: 0.95rem; font-family: 'SF Mono', monospace; }
.sentiment-value.up { color: var(--accent-danger); }
.sentiment-value.down { color: var(--accent-success); }

.review-section { margin-bottom: var(--spacing-lg); }
.section-title-small { font-size: 0.85rem; font-weight: 700; color: var(--text-secondary); margin-bottom: var(--spacing-sm); letter-spacing: 0.5px; text-transform: uppercase; }

.market-grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--spacing-md); }
.index-card { padding: var(--spacing-md); background: var(--bg-tertiary); border-radius: var(--radius-md); border: 1px solid var(--border-subtle); }
.index-name { font-weight: 700; font-size: 0.9rem; color: var(--text-primary); margin-bottom: 6px; }
.index-price { font-size: 1.4rem; font-weight: 800; font-family: 'SF Mono', monospace; }
.index-price.up { color: var(--accent-danger); }
.index-price.down { color: var(--accent-success); }
.index-change-row { display: flex; gap: var(--spacing-sm); align-items: baseline; margin-top: 2px; }
.index-change { font-size: 0.85rem; font-weight: 700; font-family: 'SF Mono', monospace; }
.index-change.up { color: var(--accent-danger); }
.index-change.down { color: var(--accent-success); }
.index-detail { font-size: 0.7rem; color: var(--text-tertiary); }
.index-range { margin-top: var(--spacing-sm); }
.range-bar { position: relative; height: 4px; background: linear-gradient(90deg, var(--accent-success) 0%, var(--accent-danger) 100%); border-radius: 2px; }
.range-dot { position: absolute; top: -3px; width: 10px; height: 10px; background: white; border: 2px solid var(--text-primary); border-radius: 50%; transform: translateX(-50%); }
.range-meta { display: flex; justify-content: space-between; font-size: 0.7rem; color: var(--text-tertiary); font-family: 'SF Mono', monospace; margin-top: 2px; }

.industry-row { display: flex; gap: var(--spacing-md); }
.industry-col { flex: 1; min-width: 0; }
.industry-col-label { font-size: 0.75rem; font-weight: 600; margin-bottom: 6px; }
.industry-col-label.up { color: var(--accent-danger); }
.industry-col-label.down { color: var(--accent-success); }
.industry-list { display: flex; flex-direction: column; gap: 4px; }
.industry-pill { display: flex; justify-content: space-between; align-items: center; padding: 8px 12px; border-radius: var(--radius-sm); font-size: 0.85rem; }
.industry-pill.up { background: rgba(220,38,38,0.06); }
.industry-pill.down { background: rgba(22,163,74,0.06); }
.industry-name { font-weight: 600; color: var(--text-primary); }
.industry-change { font-family: 'SF Mono', monospace; font-weight: 700; }
.industry-pill.up .industry-change { color: var(--accent-danger); }
.industry-pill.down .industry-change { color: var(--accent-success); }

.stock-rank-list { display: flex; flex-direction: column; gap: 4px; }
.stock-rank-item { display: flex; align-items: center; gap: var(--spacing-md); padding: 8px 12px; border-radius: var(--radius-sm); border-left: 3px solid transparent; }
.stock-rank-item.up { background: rgba(220,38,38,0.04); border-left-color: var(--accent-danger); }
.stock-rank-item.down { background: rgba(22,163,74,0.04); border-left-color: var(--accent-success); }
.rank-num { width: 24px; height: 24px; display: flex; align-items: center; justify-content: center; background: var(--bg-tertiary); border-radius: 50%; font-weight: 700; font-size: 0.75rem; color: var(--text-secondary); }
.rank-info { flex: 1; min-width: 0; }
.rank-name { font-weight: 600; color: var(--text-primary); font-size: 0.9rem; }
.rank-code { font-size: 0.7rem; color: var(--text-tertiary); font-family: 'SF Mono', monospace; }
.rank-data { text-align: right; }
.rank-change { font-weight: 800; font-family: 'SF Mono', monospace; font-size: 0.95rem; }
.stock-rank-item.up .rank-change { color: var(--accent-danger); }
.stock-rank-item.down .rank-change { color: var(--accent-success); }
.rank-meta { font-size: 0.7rem; color: var(--text-tertiary); }

.limit-row { display: flex; gap: var(--spacing-md); }
.limit-col { flex: 1; }
.limit-col-label { font-size: 0.8rem; font-weight: 700; margin-bottom: var(--spacing-sm); }
.limit-col-label.up { color: var(--accent-danger); }
.limit-col-label.down { color: var(--accent-success); }
.stock-tags { display: flex; flex-wrap: wrap; gap: 6px; }
.stock-tag { padding: 4px 10px; border-radius: var(--radius-pill); font-size: 0.75rem; font-weight: 600; }
.stock-tag.up { background: rgba(220,38,38,0.1); color: var(--accent-danger); }
.stock-tag.down { background: rgba(22,163,74,0.1); color: var(--accent-success); }
.stock-tag.blue { background: rgba(15,23,42,0.08); color: var(--accent-primary); }
.empty-tip { font-size: 0.75rem; color: var(--text-tertiary); padding: 4px 0; }

.sentiment-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--spacing-md); }
.metric-card { padding: var(--spacing-md); background: var(--bg-tertiary); border-radius: var(--radius-md); border: 1px solid var(--border-subtle); position: relative; }
.metric-card.up { background: linear-gradient(135deg, rgba(220,38,38,0.04) 0%, var(--bg-tertiary) 100%); }
.metric-card.down { background: linear-gradient(135deg, rgba(22,163,74,0.04) 0%, var(--bg-tertiary) 100%); }
.metric-label { font-size: 0.75rem; color: var(--text-tertiary); }
.metric-value { font-size: 1.6rem; font-weight: 800; font-family: 'SF Mono', monospace; margin: 4px 0; color: var(--text-primary); }
.metric-card.up .metric-value { color: var(--accent-danger); }
.metric-card.down .metric-value { color: var(--accent-success); }
.metric-sub { font-size: 0.7rem; color: var(--text-tertiary); margin-top: 2px; }
.metric-bar { height: 4px; background: var(--bg-canvas); border-radius: 2px; overflow: hidden; margin-top: 6px; }
.metric-bar-fill { height: 100%; transition: width 0.3s; }

.focus-reason { padding: var(--spacing-md); background: var(--bg-tertiary); border-radius: var(--radius-md); margin-top: var(--spacing-sm); font-size: 0.85rem; }
.focus-reason-title { font-weight: 700; color: var(--text-secondary); }
.focus-reason-text { color: var(--text-primary); }

.summary-details { margin-top: var(--spacing-md); }
.summary-details summary { cursor: pointer; font-size: 0.85rem; color: var(--text-secondary); padding: 6px 0; user-select: none; }
.summary-details summary:hover { color: var(--text-primary); }
.summary-details[open] summary { margin-bottom: var(--spacing-sm); }
</style>
