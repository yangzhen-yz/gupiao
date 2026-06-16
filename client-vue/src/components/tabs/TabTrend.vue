<template>
  <div>
    <div class="refresh-controls">
      <div class="toggle-section"><span class="toggle-label">📊 自动刷新中</span></div>
      <div class="last-update">{{ lastScan }}</div>
    </div>
    <div class="trend-section">
      <div class="section-header">
        <div class="section-subtitle-small">发现的趋势股票</div>
        <span class="section-stats">
          {{ filteredTrendStocks.length }} / {{ trendStocks.length }} 只
          <span v-if="trendStocks.length - filteredTrendStocks.length > 0" class="section-stats-hint">（已隐藏 {{ trendStocks.length - filteredTrendStocks.length }} 只在自选池中）</span>
        </span>
      </div>
      <div v-if="trendStocks.length === 0" style="text-align:center;padding:2rem;color:#64748b;">暂无趋势股票，正在自动扫描中...</div>
      <div v-else-if="filteredTrendStocks.length === 0" style="text-align:center;padding:2rem;color:#64748b;">🎉 当前 {{ trendStocks.length }} 只趋势股已全部加入自选池</div>
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
                <div class="trend-metric-item" :title="metricTip('d1')">
                  <span class="trend-metric-label">MA趋势</span>
                  <span class="trend-metric-value" :class="(stock.details.sD1 || 0) >= 20 ? 'metric-on' : (stock.details.sD1 || 0) >= 10 ? 'metric-warn' : 'metric-off'">
                    <span class="metric-icon">{{ stock.details.sD1 || 0 }}分</span>
                    <span class="metric-sub">{{ stock.details.ma20 ? 'MA20=' + stock.details.ma20 : '—' }}</span>
                  </span>
                </div>
                <div class="trend-metric-item" :title="metricTip('d2')">
                  <span class="trend-metric-label">资金异动</span>
                  <span class="trend-metric-value" :class="(stock.details.sD2 || 0) >= 20 ? 'metric-on' : (stock.details.sD2 || 0) >= 10 ? 'metric-warn' : 'metric-off'">
                    <span class="metric-icon">{{ stock.details.sD2 || 0 }}分</span>
                    <span class="metric-sub">{{ stock.details.limitUpCount20d || 0 }}板/{{ stock.details.consecutiveYangCount || 0 }}阳</span>
                  </span>
                </div>
                <div class="trend-metric-item" :title="metricTip('d3')">
                  <span class="trend-metric-label">量能配合</span>
                  <span class="trend-metric-value" :class="(stock.details.sD3 || 0) >= 15 ? 'metric-on' : (stock.details.sD3 || 0) >= 8 ? 'metric-warn' : 'metric-off'">
                    <span class="metric-icon">{{ stock.details.sD3 || 0 }}分</span>
                    <span class="metric-sub">{{ stock.details.volRatio ? '倍率=' + stock.details.volRatio : '—' }}</span>
                  </span>
                </div>
                <div class="trend-metric-item" :title="metricTip('d4')">
                  <span class="trend-metric-label">风险回撤</span>
                  <span class="trend-metric-value" :class="(stock.details.sD4 || 0) >= 15 ? 'metric-on' : (stock.details.sD4 || 0) >= 10 ? 'metric-warn' : 'metric-off'">
                    <span class="metric-icon">{{ stock.details.sD4 || 0 }}分</span>
                    <span class="metric-sub">{{ stock.details.maxDrawdown20d || 0 }}%</span>
                  </span>
                </div>
                <div class="trend-metric-item" v-if="stock.details.consecutiveUpDays" :title="metricTip('consecutive')">
                  <span class="trend-metric-label">连续上涨</span>
                  <span class="trend-metric-value metric-on">
                    <span class="metric-icon">↑</span>
                    <span class="metric-sub">{{ stock.details.consecutiveUpDays }} 天</span>
                  </span>
                </div>
                <div class="trend-metric-item" v-if="stock.details.positionPct != null" :title="metricTip('position')">
                  <span class="trend-metric-label">年度分位</span>
                  <span class="trend-metric-value" :class="stock.details.positionPct >= 80 ? 'metric-off' : stock.details.positionPct >= 60 ? 'metric-warn' : 'metric-on'">
                    <span class="metric-icon">{{ stock.details.positionPct >= 80 ? '高' : stock.details.positionPct >= 60 ? '中' : '低' }}</span>
                    <span class="metric-sub">{{ stock.details.positionPct }}%</span>
                  </span>
                </div>
              </div>
              <div v-if="(stock.deductions || stock.bonuses) && (stock.deductions || stock.bonuses).length > 0" class="trend-score-breakdown">
                <div v-if="(stock.deductions || stock.deductions || []).length > 0" class="trend-deduct-tags">
                  <span v-for="d in (stock.deductions || [])" :key="d.name" class="trend-deduct-tag">-{{ d.points }} {{ d.name }}</span>
                </div>
                <div v-if="(stock.bonuses || []).length > 0" class="trend-bonus-tags">
                  <span v-for="b in (stock.bonuses || [])" :key="b.name" class="trend-bonus-tag">+{{ b.points }} {{ b.name }}</span>
                </div>
              </div>
              <div v-else style="color:var(--text-tertiary);font-size:0.85rem;">暂无详细数据</div>
              <div class="metric-legend">
                <span class="metric-legend-dot on"></span><span>优秀</span>
                <span class="metric-legend-dot warn"></span><span>中等</span>
                <span class="metric-legend-dot off"></span><span>较差</span>
              </div>
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
          <span class="sortable-header" @click="togglePoolSort('price')">
            最新价<span class="sort-arrow" :class="{ active: poolSortBy === 'price' }">{{ poolSortBy === 'price' ? (poolSortOrder === 'asc' ? '▲' : '▼') : '▽' }}</span>
          </span>
          <span class="sortable-header" @click="togglePoolSort('consecutive')">
            连涨/跌<span class="sort-arrow" :class="{ active: poolSortBy === 'consecutive' }">{{ poolSortBy === 'consecutive' ? (poolSortOrder === 'asc' ? '▲' : '▼') : '▽' }}</span>
          </span>
          <span>5日线</span>
          <span style="text-align:center;">操作</span>
        </div>
        <div v-for="s in sortedPoolStocks" :key="s.symbol || s.code" class="stock-table-row">
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
import { getRating } from '../../utils/score'

const emit = defineEmits(['open-kline'])
const showNotification = inject('showNotification')
const { stockNameMap, loadStockMap } = useStockData()

const trendStocks = ref([])
const poolStocks = ref([])
const lastScan = ref('--')
const expanded = ref(null)
const addInput = ref('')

// 股票池排序
const poolSortBy = ref(null)       // 'price' | 'consecutive' | null
const poolSortOrder = ref('desc')  // 'asc' | 'desc'

function togglePoolSort(field) {
  if (poolSortBy.value === field) {
    poolSortOrder.value = poolSortOrder.value === 'asc' ? 'desc' : 'asc'
  } else {
    poolSortBy.value = field
    poolSortOrder.value = 'desc'   // 默认降序
  }
}

// 获取连涨/连跌天数（正值=连涨，负值=连跌）
function getConsecutiveDelta(s) {
  if (s.consecutiveUp > 0) return s.consecutiveUp
  if (s.consecutiveDown > 0) return -s.consecutiveDown
  return 0
}

const sortedPoolStocks = computed(() => {
  if (!poolSortBy.value) return poolStocks.value
  const order = poolSortOrder.value === 'asc' ? 1 : -1
  return [...poolStocks.value].sort((a, b) => {
    if (poolSortBy.value === 'price') {
      return ((a.latestPrice || 0) - (b.latestPrice || 0)) * order
    }
    if (poolSortBy.value === 'consecutive') {
      return (getConsecutiveDelta(a) - getConsecutiveDelta(b)) * order
    }
    return 0
  })
})

const TREND_PAGE_SIZE = 15
const trendPage = ref(1)
const trendTotalPages = computed(() => Math.ceil(filteredTrendStocks.value.length / TREND_PAGE_SIZE))
const pagedTrendStocks = computed(() => {
  const start = (trendPage.value - 1) * TREND_PAGE_SIZE
  return filteredTrendStocks.value.slice(start, start + TREND_PAGE_SIZE)
})

// 自选池代码集合（小写），用于从趋势列表中过滤掉已加入自选池的股票
const poolSymbolSet = computed(() => new Set(poolStocks.value.map(s => (s.symbol || s.code || '').toLowerCase())))
// 已加入自选池的代码集合（前端层面持久过滤，跨刷新有效）
const locallyPooledSymbols = ref(new Set())
// 趋势列表过滤：剔除自选池 + 剔除本次会话内手动加入的
const filteredTrendStocks = computed(() => trendStocks.value.filter(s => {
  const code = (s.symbol || s.code || '').toLowerCase()
  return !poolSymbolSet.value.has(code) && !locallyPooledSymbols.value.has(code)
}))

function toggleExpand(symbol) {
  expanded.value = expanded.value === symbol ? null : symbol
}

// 指标说明（hover 提示）
const METRIC_TIPS = {
  d1: '维度1：短期均线趋势（满分30分）。30=近10日全在MA20上+MA20连续抬升；20=近7日仅1日短暂跌破次日收回；10=现价站稳MA20；0=跌破未收回。',
  d2: '维度2：短期资金异动（满分30分）。涨停次数与连阳数分别计分取高者：≥3板/2连板/≥6阳=30分；2板/4~5阳=20分；1板/3阳=10分。',
  d3: '维度3：量能配合（满分20分）。20日均量/60日均量≥1.5=20分；≥1.3=15分；≥1.0=8分；缩量=0分。20分档需价涨量增。',
  d4: '维度4：风险回撤（满分20分）。近20日最大回撤<8%=20分；8~12%=15分；12~15%=10分；15~20%=5分；≥20%=0分。',
  position: '年度分位：现价在近250日收盘价中的百分位。≥80%视为高位（扣-20分），60~80%中高位（扣-10分）。',
  consecutive: '从最新一天往回数连续阳线天数，与评分无关，仅作展示。',
}
function metricTip(key) { return METRIC_TIPS[key] || '' }

// 成交量格式化（万/亿）
function formatVol(v) {
  if (!v && v !== 0) return '—'
  if (v >= 1e8) return (v / 1e8).toFixed(1) + '亿'
  if (v >= 1e4) return (v / 1e4).toFixed(1) + '万'
  return String(v)
}

function getConsecutiveClass(stock) {
  const days = stock.details?.consecutiveUpDays || 0
  if (days >= 3) return 'hot'
  if (days >= 1) return 'warm'
  return 'cool'
}

function getScoreColor(stock) {
  const c = getRating(stock.score).color
  if (c === 'var(--red)') return '#dc2626'
  if (c === 'var(--orange)') return '#f59e0b'
  return '#16a34a'
}

function getScoreStyle(stock) {
  const c = getRating(stock.score).color
  if (c === 'var(--red)') return { background: 'linear-gradient(135deg,#fef2f2,#fee2e2)', borderColor: '#fca5a5' }
  if (c === 'var(--orange)') return { background: 'linear-gradient(135deg,#fffbeb,#fef3c7)', borderColor: '#fde68a' }
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
      // 立即从前端列表中隐藏（不等后端刷新），且跨定时刷新持续生效
      const symbol = val.toLowerCase()
      locallyPooledSymbols.value.add(symbol)
      locallyPooledSymbols.value = new Set(locallyPooledSymbols.value)  // 触发 ref 更新
      // 跳回第 1 页，避免当前页变成空白
      trendPage.value = 1
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
      // 解除本地隐藏标记，股票会重新出现在趋势列表（如果仍满足条件）
      const code = (symbol || '').toLowerCase()
      if (code) {
        locallyPooledSymbols.value.delete(code)
        locallyPooledSymbols.value = new Set(locallyPooledSymbols.value)
      }
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
