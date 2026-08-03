<template>
  <div>
    <div class="refresh-controls">
      <div class="toggle-section"><span class="toggle-label">📊 自动刷新中</span></div>
      <div class="last-update">{{ lastUpdateTime }}</div>
    </div>
    <div class="filter-bar">
      <label class="filter-label">过滤模式：</label>
      <select v-model="filterMode" class="filter-select" @change="resetPage">
        <option v-for="mode in tabConfig.filterModes" :key="mode" :value="mode">{{ tabConfig.modeLabels[mode] || mode }}</option>
      </select>
      <input v-if="filterMode === 'min_score'" v-model.number="customMinScore" type="number" min="0" :max="tabConfig.bullMinScore" class="filter-input" @change="resetPage" />
      <span v-if="filterMode !== 'all'" class="filter-info">
        ({{ filteredCount }} / {{ scanResults.length }} 只)
      </span>
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
          <span>名称</span><span>现价</span><span>涨跌幅</span><span>量比</span><span>换手率</span><span>委比</span><span>RPS</span><span>综合评分</span><span>风控/估值</span>
        </div>
        <div v-for="r in pagedResults" :key="r.symbol" class="stock-table-row clickable" @click="goToQuote(r.symbol)">
          <span class="name">{{ r.name }}<br><small style="color:#94a3b8;font-weight:400;">{{ r.symbol.toUpperCase() }}</small></span>
          <span class="price" :style="{ color: r.change >= 0 ? '#dc2626' : '#16a34a' }">{{ r.price }}</span>
          <span class="change-pct" :style="{ color: r.change >= 0 ? '#dc2626' : '#16a34a', fontWeight: 600 }">{{ r.change >= 0 ? '+' : '' }}{{ r.changePercent.toFixed(2) }}%</span>
          <span class="volume-ratio">{{ r.volumeRatio.toFixed(2) }}</span>
          <span class="turnover">{{ r.turnoverRate.toFixed(2) }}%</span>
          <span class="weibi-val" :style="{ color: r.weibi >= 0 ? '#dc2626' : '#16a34a', fontWeight: 600 }">{{ r.weibi >= 0 ? '+' : '' }}{{ r.weibi.toFixed(2) }}%</span>
          <span class="rps-cell" :style="{ color: r.rps != null ? (r.rps >= 90 ? '#dc2626' : r.rps >= 70 ? '#f59e0b' : r.rps >= 50 ? '#64748b' : '#94a3b8') : '#94a3b8', fontWeight: 700 }">{{ r.rps != null ? r.rps.toFixed(0) : '-' }}</span>
          <span class="score-cell" :style="{ color: r.score >= 70 ? '#dc2626' : r.score >= 40 ? '#f59e0b' : '#16a34a' }">
            {{ r.score }}<br><span :class="r.score >= 70 ? 'tag-bull' : r.score < 40 ? 'tag-bear' : 'tag-hold'">{{ r.score >= 70 ? '看涨' : r.score < 40 ? '看跌' : '观望' }}</span>
          </span>
          <span class="risk-cell">
            <span class="risk-badge" :class="riskLevelClass(r.riskControl?.risk_level)">{{ riskLabel(r.riskControl?.risk_level) }}</span>
            <span v-if="r.valuation?.overvalued" class="val-badge over" :title="`PE百分位${r.valuation?.pe_pct}% / PB百分位${r.valuation?.pb_pct}%`">估值{{ r.valuation?.pe_pct }}%</span>
          </span>
        </div>
      </div>

      <!-- 分页控件 -->
      <div v-if="totalPages > 1" class="pagination">
        <button class="page-btn" :disabled="currentPage === 1" @click="goPage(1)">« 首页</button>
        <button class="page-btn" :disabled="currentPage === 1" @click="goPage(currentPage - 1)">‹ 上一页</button>
        <span class="page-info">第 {{ currentPage }} / {{ totalPages }} 页 · 共 {{ sortedResults.length }} 条</span>
        <button class="page-btn" :disabled="currentPage === totalPages" @click="goPage(currentPage + 1)">下一页 ›</button>
        <button class="page-btn" :disabled="currentPage === totalPages" @click="goPage(totalPages)">末页 »</button>
      </div>
    </div>
    <div v-else-if="!scanning" style="text-align:center;padding:2rem;color:#64748b;">暂无扫描结果</div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, inject } from 'vue'
import { apiFetch, isTradingTime, getNextRefreshDelay, formatNowTime, riskLabel, riskLevelClass } from '../../utils/helpers'
import { getRating } from '../../utils/score'
import { useStockData } from '../../composables/useStockData'

const showNotification = inject('showNotification')
const navigateToQuote = inject('navigateToQuote')
const { scanStockPool, loadScanPool } = useStockData()

const scanResults = ref([])
const scanning = ref(false)
const lastUpdateTime = ref('--:--:--')
const scanSummary = ref(null)
const scanPool = ref([])

// 过滤模式（2026-07-08 重构）：所有配置从后端 /api/tab-scan-config 拉取
// 后端单一源：TAB_SCAN_FILTER_MODES / TAB_SCAN_DEFAULT_MODE / TAB_SCAN_BULL_MIN_SCORE / TAB_SCAN_BEAR_MAX_SCORE
// 区间划分：score < bearMaxScore 看跌；bearMaxScore ≤ score < bullMinScore 观望；score ≥ bullMinScore 看涨
// "观望以上" = score ≥ bearMaxScore（包含 40-69 观望区 + ≥70 看涨区，排除 <40 看跌）
const tabConfig = ref({  // 后端配置缓存
  filterModes: ['all', 'bull_only', 'bull_and_hold', 'min_score'],
  defaultMode: 'all',
  bullMinScore: 70,
  bearMaxScore: 40,
  modeLabels: { all: '全部', bull_only: '仅看涨', bull_and_hold: '观望以上', min_score: '自定义最低分' },
})
const filterMode = ref('all')
const customMinScore = ref(50)  // 自定义模式默认 50 分

// 异步加载后端配置（不阻塞 onMounted 主流程；失败用默认值兜底）
async function loadTabConfig() {
  try {
    const r = await apiFetch('/api/tab-scan-config')
    if (r && r.success && r.data) {
      tabConfig.value = { ...tabConfig.value, ...r.data }
      filterMode.value = r.data.defaultMode || 'all'
    }
  } catch (e) {
    console.warn('加载 tab-scan-config 失败，使用默认值:', e.message)
  }
}

const PAGE_SIZE = 10
const currentPage = ref(1)

function resetPage() {
  currentPage.value = 1
}

// 过滤后的列表：先按分数倒序，再按过滤模式筛选
// 阈值全部读自 tabConfig（后端单一源），不在前端硬编码
const sortedResults = computed(() => {
  const sorted = [...scanResults.value].sort((a, b) => b.score - a.score)
  const { bullMinScore, bearMaxScore } = tabConfig.value
  if (filterMode.value === 'all') return sorted
  if (filterMode.value === 'bull_only') {
    // 仅看涨：score >= bullMinScore（看涨区）
    return sorted.filter(r => r.score >= bullMinScore)
  }
  if (filterMode.value === 'bull_and_hold') {
    // 观望以上：score >= bearMaxScore（观望区 + 看涨区，排除看跌）
    return sorted.filter(r => r.score >= bearMaxScore)
  }
  if (filterMode.value === 'min_score') {
    // 自定义最低分
    return sorted.filter(r => r.score >= (customMinScore.value || 0))
  }
  return sorted
})

const filteredCount = computed(() => sortedResults.value.length)
const totalPages = computed(() => Math.max(1, Math.ceil(sortedResults.value.length / PAGE_SIZE)))
const pagedResults = computed(() => {
  const start = (currentPage.value - 1) * PAGE_SIZE
  return sortedResults.value.slice(start, start + PAGE_SIZE)
})

function goPage(p) {
  const target = Math.min(Math.max(1, p), totalPages.value)
  if (target !== currentPage.value) currentPage.value = target
}

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
      currentPage.value = 1
      lastUpdateTime.value = formatNowTime()
      const bull = result.filter(r => r.score >= 70).length
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
    autoScanTimer = setTimeout(tick, isTradingTime() ? 30000 : getNextRefreshDelay())
  }
  tick()
}

onMounted(async () => {
  await loadScanPool()
  scanPool.value = scanStockPool.value
  // 2026-07-08：先加载后端配置（设置默认 filterMode），再启动扫描
  // 配置加载失败时使用默认 'all'，扫描仍可正常进行
  await loadTabConfig()
  startScan(true)
  scheduleScanRefresh()
})

onUnmounted(() => {
  if (autoScanTimer) clearTimeout(autoScanTimer)
})
</script>

<style scoped>
.filter-bar {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.5rem 0;
  margin-bottom: 0.5rem;
  flex-wrap: wrap;
}
.filter-label {
  font-size: 0.875rem;
  color: #475569;
  font-weight: 500;
}
.filter-select {
  padding: 0.375rem 0.75rem;
  border: 1px solid #cbd5e1;
  border-radius: 0.375rem;
  background: white;
  font-size: 0.875rem;
  color: #1e293b;
  cursor: pointer;
  outline: none;
  transition: border-color 0.15s;
}
.filter-select:hover {
  border-color: #94a3b8;
}
.filter-select:focus {
  border-color: #3b82f6;
  box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.1);
}
.filter-input {
  width: 5rem;
  padding: 0.375rem 0.5rem;
  border: 1px solid #cbd5e1;
  border-radius: 0.375rem;
  font-size: 0.875rem;
  color: #1e293b;
  outline: none;
}
.filter-input:focus {
  border-color: #3b82f6;
  box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.1);
}
.filter-info {
  font-size: 0.8125rem;
  color: #64748b;
  font-weight: 500;
}

/* RPS 列（P0，2026-07-16）+ 风控/估值列（P2a/P2c）：表格扩展为 9 列 */
.stock-table-header,
.stock-table-row {
  grid-template-columns: 2fr 1fr 1fr 0.8fr 0.8fr 0.8fr 0.8fr 1.2fr 1.1fr;
}
.rps-cell {
  font-family: 'SF Mono', 'Courier New', monospace;
  font-weight: 700;
}
@media (max-width: 640px) {
  .stock-table-header,
  .stock-table-row {
    grid-template-columns: 2fr 1fr 1fr 1.2fr;
  }
  /* 移动端隐藏：量比(4)/换手(5)/委比(6)/RPS(7)/风控估值(9)，保留 名称/现价/涨跌幅/评分 */
  .stock-table-header span:nth-child(4),
  .stock-table-header span:nth-child(5),
  .stock-table-header span:nth-child(6),
  .stock-table-header span:nth-child(7),
  .stock-table-header span:nth-child(9),
  .stock-table-row span:nth-child(4),
  .stock-table-row span:nth-child(5),
  .stock-table-row span:nth-child(6),
  .stock-table-row span:nth-child(7),
  .stock-table-row span:nth-child(9) {
    display: none;
  }
}
</style>
