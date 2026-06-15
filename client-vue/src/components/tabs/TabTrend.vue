<template>
  <div>
    <div class="refresh-controls">
      <div class="toggle-section"><span class="toggle-label">📊 自动刷新中</span></div>
      <div class="last-update">{{ lastScan }}</div>
    </div>
    <div class="trend-section">
      <div class="section-header">
        <div class="section-subtitle-small">发现的趋势股票</div>
        <span class="section-stats">{{ trendStocks.length }} 只</span>
      </div>
      <div v-if="trendStocks.length === 0" style="text-align:center;padding:2rem;color:#64748b;">暂无趋势股票，正在自动扫描中...</div>
      <div v-else class="trend-card-list">
        <div v-for="stock in pagedTrendStocks" :key="stock.symbol || stock.code" class="trend-card">
          <div class="trend-card-main" @click="toggleExpand(stock.symbol || stock.code)">
            <div class="trend-card-left">
              <div class="trend-card-name">{{ stock.name }}</div>
              <div class="trend-card-code">{{ (stock.symbol || stock.code || '').toUpperCase() }}</div>
            </div>
            <div class="trend-card-price">
              <span class="trend-price-val" style="color: #dc2626;">{{ stock.latestPrice || stock.price }}</span>
            </div>
            <div class="trend-card-consecutive">
              <span class="trend-consecutive-badge" :class="getConsecutiveClass(stock)">{{ stock.details?.consecutiveUpDays || 0 }}天</span>
            </div>
            <div class="trend-card-score">
              <div class="trend-score-ring" :style="getScoreStyle(stock)">
                <span :style="{ color: getScoreColor(stock), fontSize: '1rem', fontWeight: 800 }">{{ stock.score }}</span>
              </div>
            </div>
            <div class="trend-card-expand-icon" :class="{ expanded: expanded === (stock.symbol || stock.code) }">
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M4 6L8 10L12 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
            </div>
          </div>
          <div class="trend-card-detail" :class="{ open: expanded === (stock.symbol || stock.code) }">
            <div class="trend-detail-inner">
              <div v-if="stock.details" class="trend-metric-grid">
                <div class="trend-metric-item">
                  <span class="trend-metric-label">站上120线</span>
                  <span class="trend-metric-value" :style="{ color: stock.details.priceAboveMa120 ? '#16a34a' : '#dc2626' }">{{ stock.details.priceAboveMa120 ? '✓' : '✗' }}</span>
                </div>
                <div class="trend-metric-item">
                  <span class="trend-metric-label">120线向上</span>
                  <span class="trend-metric-value" :style="{ color: stock.details.ma120SlopeUp ? '#16a34a' : '#dc2626' }">{{ stock.details.ma120SlopeUp ? '✓' : '✗' }}</span>
                </div>
                <div class="trend-metric-item">
                  <span class="trend-metric-label">120日涨停</span>
                  <span class="trend-metric-value" :style="{ color: stock.details.limitUp120 ? '#16a34a' : '#dc2626' }">{{ stock.details.limitUp120 ? '✓' : '✗' }}</span>
                </div>
                <div class="trend-metric-item">
                  <span class="trend-metric-label">1年涨停</span>
                  <span class="trend-metric-value" :style="{ color: stock.details.limitUp250 ? '#16a34a' : '#dc2626' }">{{ stock.details.limitUp250 ? '✓' : '✗' }}</span>
                </div>
                <div class="trend-metric-item">
                  <span class="trend-metric-label">回撤&lt;30%</span>
                  <span class="trend-metric-value" :style="{ color: stock.details.drawdown30 ? '#16a34a' : '#dc2626' }">{{ stock.details.drawdown30 ? '✓' : '✗' }}</span>
                </div>
                <div class="trend-metric-item" v-if="stock.details.consecutiveUpDays">
                  <span class="trend-metric-label">连续上涨</span>
                  <span class="trend-metric-value" style="color:#dc2626;">{{ stock.details.consecutiveUpDays }}天</span>
                </div>
              </div>
              <div v-else style="color:var(--text-tertiary);font-size:0.85rem;">暂无详细数据</div>
            </div>
            <div class="trend-card-actions">
              <button class="trend-btn-kline" @click.stop="$emit('open-kline', stock.symbol || stock.code)">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
                K线
              </button>
              <button class="trend-btn-add" @click.stop="addToPool(stock.symbol || stock.code)">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
                添加
              </button>
            </div>
          </div>
        </div>
      </div>
      <div v-if="trendTotalPages > 1" class="history-pagination">
        <button class="page-btn" :disabled="trendPage <= 1" @click="trendPage--">上一页</button>
        <span class="page-info">第 {{ trendPage }} / {{ trendTotalPages }} 页</span>
        <button class="page-btn" :disabled="trendPage >= trendTotalPages" @click="trendPage++">下一页</button>
      </div>
    </div>
    <div class="pool-section">
      <div class="section-header">
        <div class="section-subtitle-small">股票池管理</div>
        <span class="section-stats">{{ poolStocks.length }} 只</span>
      </div>
      <div class="add-stock-container">
        <div class="add-stock-wrapper">
          <label class="form-label">添加股票到扫描池</label>
          <div class="add-stock-input-group">
            <input type="text" v-model="addInput" placeholder="输入代码(如 sh603985) 或 名称" class="form-input" @keyup.enter="addToPool(addInput)">
            <button class="btn-secondary" @click="addToPool(addInput)">添加</button>
          </div>
        </div>
      </div>
      <div class="pool-table" v-if="poolStocks.length > 0">
        <div class="stock-table-header">
          <span>名称</span>
          <span>最新价</span>
          <span>连涨/跌</span>
          <span>5日线</span>
          <span style="text-align:center;">操作</span>
        </div>
        <div v-for="s in poolStocks" :key="s.symbol || s.code" class="stock-table-row">
          <span class="pool-stock-name">{{ s.name || stockNameMap[s.symbol || s.code] || (s.symbol || s.code) }}</span>
          <span class="pool-stock-price" :class="getPriceClass(s)">
            {{ s.latestPrice > 0 ? s.latestPrice.toFixed(2) : '--' }}
          </span>
          <span class="pool-stock-trend">
            <span v-if="s.consecutiveUp > 0" class="trend-badge up">连涨{{ s.consecutiveUp }}天</span>
            <span v-else-if="s.consecutiveDown > 0" class="trend-badge down">连跌{{ s.consecutiveDown }}天</span>
            <span v-else class="trend-badge neutral">--</span>
          </span>
          <span class="pool-stock-ma5" :class="s.aboveMa5 ? 'above' : 'below'">
            <span v-if="s.aboveMa5" class="ma-badge up">站上 MA5 {{ s.ma5 ? s.ma5.toFixed(2) : '' }}</span>
            <span v-else class="ma-badge down">跌破 MA5 {{ s.ma5 ? s.ma5.toFixed(2) : '' }}</span>
          </span>
          <span class="pool-stock-actions">
            <button class="pool-btn pool-btn-kline" title="查看K线" @click="openKlineForPool(s.symbol || s.code)">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M7 14l4-4 4 4 5-5"/></svg>
              <span>K线</span>
            </button>
            <button class="pool-btn pool-btn-remove" title="移除" @click="removeFromPool(s.symbol || s.code)">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>
              <span>移除</span>
            </button>
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, inject } from 'vue'
import { apiFetch, isTradingTime, getNextRefreshDelay } from '../../utils/helpers'
import { useStockData } from '../../composables/useStockData'

const emit = defineEmits(['open-kline'])
const showNotification = inject('showNotification')
const { stockNameMap, loadStockMap } = useStockData()

const trendStocks = ref([])
const poolStocks = ref([])
const lastScan = ref('--')
const expanded = ref(null)
const addInput = ref('')

const TREND_PAGE_SIZE = 15
const trendPage = ref(1)
const trendTotalPages = computed(() => Math.ceil(trendStocks.value.length / TREND_PAGE_SIZE))
const pagedTrendStocks = computed(() => {
  const start = (trendPage.value - 1) * TREND_PAGE_SIZE
  return trendStocks.value.slice(start, start + TREND_PAGE_SIZE)
})

function toggleExpand(symbol) {
  expanded.value = expanded.value === symbol ? null : symbol
}

function getConsecutiveClass(stock) {
  const days = stock.details?.consecutiveUpDays || 0
  if (days >= 3) return 'hot'
  if (days >= 1) return 'warm'
  return 'cool'
}

function getScoreColor(stock) {
  if (stock.score >= 80) return '#dc2626'
  if (stock.score >= 60) return '#f59e0b'
  return '#16a34a'
}

function getScoreStyle(stock) {
  if (stock.score >= 80) return { background: 'linear-gradient(135deg,#fef2f2,#fee2e2)', borderColor: '#fca5a5' }
  if (stock.score >= 60) return { background: 'linear-gradient(135deg,#fffbeb,#fef3c7)', borderColor: '#fde68a' }
  return { background: 'linear-gradient(135deg,#f0fdf4,#dcfce7)', borderColor: '#bbf7d0' }
}

async function loadTrendStocks() {
  try {
    const result = await apiFetch('/api/trend-stocks')
    if (result.success && result.data) {
      trendStocks.value = result.data.stocks || []
      // 旧数据无 consecutiveUpDays 字段 → 自动触发一次重新扫描刷新数据
      const needRescan = trendStocks.value.length > 0 &&
        trendStocks.value.every(s => !s.details || typeof s.details.consecutiveUpDays === 'undefined')
      if (needRescan && !_autoRescanTriggered) {
        _autoRescanTriggered = true
        console.log('[TabTrend] 检测到旧数据（无连涨天数），自动触发一次重新扫描...')
        scanTrendStocks().catch(() => { _autoRescanTriggered = false })
      }
    }
  } catch (e) { console.error('加载趋势数据失败:', e) }
}

// 仅在组件生命周期内自动重扫一次，避免每次刷新都触发
let _autoRescanTriggered = false

async function scanTrendStocks() {
  try {
    const result = await apiFetch('/api/scan-trend-stocks')
    if (result.success) {
      await loadTrendStocks()
      lastScan.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
    }
  } catch (e) { showNotification('趋势扫描失败') }
}

async function loadPoolWithIndicators() {
  try {
    const result = await apiFetch('/api/custom-scan-pool')
    if (result.success) {
      poolStocks.value = result.data.stocks || []
    }
  } catch (e) { /* 静默失败 */ }
}

async function addToPool(input) {
  const val = (input || addInput.value || '').trim()
  if (!val) return
  try {
    const result = await apiFetch('/api/custom-scan-pool/add', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol: val })
    })
    if (result.success) {
      addInput.value = ''
      const symbol = val.toLowerCase()
      trendStocks.value = trendStocks.value.filter(s => (s.symbol || s.code || '').toLowerCase() !== symbol)
      await loadPoolWithIndicators()
      showNotification('添加成功')
    } else {
      showNotification(result.error || '添加失败')
    }
  } catch (e) { showNotification('添加失败') }
}

async function removeFromPool(symbol) {
  try {
    const result = await apiFetch('/api/custom-scan-pool/remove', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol })
    })
    if (result.success) {
      await loadPoolWithIndicators()
      showNotification('移除成功')
    } else {
      showNotification(result.error || '移除失败')
    }
  } catch (e) { showNotification('移除失败') }
}

function openKlineForPool(code) {
  emit('open-kline', code)
}

function getPriceClass(s) {
  if (s.consecutiveUp > 0) return 'price-up'
  if (s.consecutiveDown > 0) return 'price-down'
  return 'price-neutral'
}

let trendTimer = null
function scheduleTrendRefresh() {
  if (trendTimer) clearTimeout(trendTimer)
  const tick = async () => {
    await loadTrendStocks()
    trendTimer = setTimeout(tick, isTradingTime() ? 30000 : getNextRefreshDelay())
  }
  tick()
}

onMounted(async () => {
  await loadStockMap()
  await loadPoolWithIndicators()
  await loadTrendStocks()
  scheduleTrendRefresh()
})

onUnmounted(() => {
  if (trendTimer) clearTimeout(trendTimer)
})
</script>
