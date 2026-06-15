<template>
  <div>
    <div class="refresh-controls">
      <div class="toggle-section"><span class="toggle-label">📊 自动刷新中</span></div>
      <div class="last-update">{{ lastUpdateTime }}</div>
    </div>
    <div v-if="scanning" class="scan-progress">
      <div class="loading-spinner" style="width:20px;height:20px;border-width:2px;margin:0;"></div>
      正在扫描 {{ scanPool.length }} 只股票...
    </div>
    <div v-if="scanSummary" class="scan-summary">
      <div class="scan-stat bull">看涨: {{ scanSummary.bull }}只</div>
      <div class="scan-stat bear">看跌: {{ scanSummary.bear }}只</div>
      <div class="scan-stat neutral">观望: {{ scanSummary.neutral }}只</div>
      <div class="scan-stat neutral">共扫描: {{ scanSummary.total }}只</div>
    </div>
    <div v-if="scanResults.length">
      <div class="stock-table">
        <div class="stock-table-header">
          <span>名称</span><span>现价</span><span>涨跌幅</span><span>量比</span><span>换手率</span><span>委比</span><span>综合评分</span>
        </div>
        <div v-for="r in sortedResults" :key="r.symbol" class="stock-table-row clickable" @click="goToQuote(r.symbol)">
          <span class="name">{{ r.name }}<br><small style="color:#94a3b8;font-weight:400;">{{ r.symbol.toUpperCase() }}</small></span>
          <span class="price" :style="{ color: r.change >= 0 ? '#dc2626' : '#16a34a' }">{{ r.price }}</span>
          <span class="change-pct" :style="{ color: r.change >= 0 ? '#dc2626' : '#16a34a', fontWeight: 600 }">{{ r.change >= 0 ? '+' : '' }}{{ r.changePercent.toFixed(2) }}%</span>
          <span class="volume-ratio">{{ r.volumeRatio.toFixed(2) }}</span>
          <span class="turnover">{{ r.turnoverRate.toFixed(2) }}%</span>
          <span class="weibi-val" :style="{ color: r.weibi >= 0 ? '#dc2626' : '#16a34a', fontWeight: 600 }">{{ r.weibi >= 0 ? '+' : '' }}{{ r.weibi.toFixed(2) }}%</span>
          <span class="score-cell" :style="{ color: r.score >= 60 ? '#dc2626' : r.score >= 40 ? '#f59e0b' : '#16a34a' }">
            {{ r.score }}<br><span :class="r.score >= 60 ? 'tag-bull' : r.score < 40 ? 'tag-bear' : 'tag-hold'">{{ r.score >= 60 ? '看涨' : r.score < 40 ? '看跌' : '观望' }}</span>
          </span>
        </div>
      </div>
    </div>
    <div v-else-if="!scanning" style="text-align:center;padding:2rem;color:#64748b;">暂无扫描结果</div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, inject } from 'vue'
import { apiFetch, isTradingTime, getNextRefreshDelay, formatNowTime } from '../../utils/helpers'
import { useStockData } from '../../composables/useStockData'

const showNotification = inject('showNotification')
const navigateToQuote = inject('navigateToQuote')
const { scanStockPool, loadScanPool } = useStockData()

const scanResults = ref([])
const scanning = ref(false)
const lastUpdateTime = ref('--:--:--')
const scanSummary = ref(null)
const scanPool = ref([])

const sortedResults = computed(() => [...scanResults.value].sort((a, b) => b.score - a.score))

function goToQuote(symbol) {
  if (navigateToQuote) navigateToQuote(symbol)
}

async function startScan(silent = false) {
  if (scanning.value) return
  scanning.value = true
  try {
    const codes = scanPool.value.length > 0 ? scanPool.value : scanStockPool.value
    const result = await apiFetch(`/api/hot-stocks-scan?codes=${codes.join(',')}`)
    if (result.error) {
      if (!silent) showNotification(result.error)
      return
    }
    if (Array.isArray(result) && result.length > 0) {
      scanResults.value = result
      lastUpdateTime.value = formatNowTime()
      const bull = result.filter(r => r.score >= 60).length
      const bear = result.filter(r => r.score < 40).length
      scanSummary.value = { bull, bear, neutral: result.length - bull - bear, total: result.length }
    }
  } catch (e) {
    if (!silent) showNotification('扫描失败: ' + e.message)
  } finally {
    scanning.value = false
  }
}

let autoScanTimer = null
function scheduleScanRefresh() {
  if (autoScanTimer) clearTimeout(autoScanTimer)
  const tick = async () => {
    await startScan(true)
    autoScanTimer = setTimeout(tick, isTradingTime() ? 5000 : getNextRefreshDelay())
  }
  tick()
}

onMounted(async () => {
  await loadScanPool()
  scanPool.value = scanStockPool.value
  startScan(true)
  scheduleScanRefresh()
})

onUnmounted(() => {
  if (autoScanTimer) clearTimeout(autoScanTimer)
})
</script>
