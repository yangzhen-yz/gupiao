<template>
  <div>
    <!-- 大盘紧急提醒 -->
    <div v-if="marketAlert" class="market-alert-container" v-html="marketAlert"></div>

    <div class="control-section">
      <div class="input-group">
        <div class="input-wrapper">
          <label>股票代码/名称</label>
          <div class="input-row">
            <input type="text" v-model="stockInput" class="stock-input"
              placeholder="输入6位代码：603985, 600487..." @keyup.enter="onFetch">
            <button class="btn-primary" @click="onFetch">查询</button>
            <button class="btn-secondary" @click="manualRefresh">手动刷新</button>
          </div>
        </div>
      </div>
      <div class="quick-stocks">
        <div class="quick-stocks-header">
          <div class="quick-stocks-title">热门股票快捷查询</div>
          <button class="btn-icon" @click="loadQuickStocks" title="刷新热门股票">🔄</button>
        </div>
        <div class="quick-buttons">
          <button v-for="s in quickStocks" :key="s.code" class="quick-btn"
            :class="{ active: currentSymbol === s.code }"
            @click="selectQuickStock(s.code)"
            @contextmenu.prevent="confirmRemoveQuickStock(s)">{{ s.name }}</button>
        </div>
      </div>
      <div class="refresh-controls">
        <div class="toggle-section">
          <span class="toggle-label">{{ tradingStatusText }}</span>
        </div>
        <div class="last-update">{{ lastUpdateTime }}</div>
      </div>
    </div>

    <!-- 行情卡片 -->
    <div class="quote-section">
      <div v-if="loading" class="price-card" style="text-align:center;">
        <div class="loading-spinner"></div>
        <div style="font-size:0.9rem;font-weight:500;color:var(--text-2);">正在加载数据...</div>
      </div>
      <div v-else-if="error" class="price-card" style="text-align:center;padding:2rem;">
        <div style="font-size:1.1rem;color:var(--red);margin-bottom:0.5rem;">{{ error }}</div>
        <button class="btn-primary" @click="fetchStockQuote(currentSymbol)">重试</button>
      </div>
      <template v-else-if="quoteData">
        <div class="price-card">
          <div class="stock-header">
            <div class="stock-name-code">
              <div class="stock-name">{{ quoteData.name }}</div>
              <div class="stock-code">{{ quoteData.symbol.toUpperCase() }}</div>
            </div>
            <div class="stock-price-section">
              <div class="stock-price" :class="quoteData.isUp ? 'up' : 'down'">{{ quoteData.price }}</div>
              <div class="stock-change" :class="quoteData.isUp ? 'up' : 'down'">
                {{ quoteData.isUp ? '+' : '' }}{{ quoteData.change }} · {{ quoteData.changePercent }}%
              </div>
            </div>
          </div>
          <div style="display:flex;gap:0.5rem;margin-bottom:0.8rem;padding:0 0.25rem;">
            <button class="btn-secondary" style="flex:1;display:flex;align-items:center;justify-content:center;gap:0.4rem;background:var(--blue-bg);color:var(--blue);border-color:var(--blue-border);" @click="$emit('open-kline', quoteData.symbol)">
              K线图
            </button>
            <button class="btn-secondary" style="flex:1;display:flex;align-items:center;justify-content:center;gap:0.4rem;background:var(--orange-bg);color:var(--orange);border-color:var(--orange-border);" @click="startAiDiagnose(quoteData.symbol)">
              AI诊断
            </button>
          </div>
          <div class="stock-card-actions" style="padding:0 0.25rem;">
            <button class="add-to-hot-btn" @click="toggleHotStock">{{ isInHot ? '⭐ 移出热门' : '⭐ 添加到热门股票' }}</button>
          </div>

          <!-- 详细数据 -->
          <div :class="['collapsible-section', { collapsed: collapsed.detail }]">
            <div class="collapsible-header" @click="collapsed.detail = !collapsed.detail">
              <span class="collapsible-title">📋 详细数据</span>
              <span class="collapsible-arrow">▼</span>
            </div>
            <div class="collapsible-content">
              <div class="stock-grid">
                <div class="grid-item"><div class="grid-label">今开</div><div class="grid-value">{{ quoteData.open || '--' }}</div></div>
                <div class="grid-item"><div class="grid-label">最高</div><div class="grid-value">{{ quoteData.high || '--' }}</div></div>
                <div class="grid-item"><div class="grid-label">最低</div><div class="grid-value">{{ quoteData.low || '--' }}</div></div>
                <div class="grid-item"><div class="grid-label">成交量</div><div class="grid-value">{{ formatNumber(quoteData.volumeRaw) }}</div></div>
                <div class="grid-item"><div class="grid-label">成交额</div><div class="grid-value">{{ formatNumber(quoteData.turnover * 10000) }}</div></div>
                <div class="grid-item"><div class="grid-label">换手率</div><div class="grid-value">{{ (quoteData.turnoverRate || 0).toFixed(2) }}%</div></div>
                <div class="grid-item"><div class="grid-label">量比</div><div class="grid-value" :style="{ color: quoteData.volumeRatio > 1.5 ? 'var(--red)' : quoteData.volumeRatio < 0.5 ? 'var(--green)' : 'var(--text)' }">{{ (quoteData.volumeRatio || 0).toFixed(2) }}</div></div>
                <div class="grid-item"><div class="grid-label">市盈率</div><div class="grid-value">{{ quoteData.pe > 0 ? quoteData.pe.toFixed(2) : '亏损' }}</div></div>
                <div class="grid-item"><div class="grid-label">振幅</div><div class="grid-value">{{ (quoteData.amplitude || 0).toFixed(2) }}%</div></div>
                <div class="grid-item"><div class="grid-label">委比</div><div class="grid-value" :style="{ color: quoteData.weibi > 0 ? 'var(--red)' : 'var(--green)' }">{{ quoteData.weibi > 0 ? '+' : '' }}{{ (quoteData.weibi || 0).toFixed(2) }}%</div></div>
                <div class="grid-item"><div class="grid-label">均价</div><div class="grid-value">{{ (quoteData.avgPrice || 0).toFixed(2) }}</div></div>
                <div class="grid-item"><div class="grid-label">买一</div><div class="grid-value">{{ quoteData.bid1 }}</div></div>
                <div class="grid-item"><div class="grid-label">卖一</div><div class="grid-value">{{ quoteData.ask1 }}</div></div>
                <div class="grid-item" v-if="quoteData.circulateMarketCap > 0"><div class="grid-label">流通市值</div><div class="grid-value">{{ formatNumber(quoteData.circulateMarketCap * 10000) }}</div></div>
              </div>
            </div>
          </div>
        </div>

        <!-- 综合评分 -->
        <div :class="['collapsible-section', { collapsed: collapsed.score }]">
          <div class="collapsible-header" @click="collapsed.score = !collapsed.score">
            <span class="collapsible-title">📊 综合评分模型</span>
            <span class="collapsible-arrow">▼</span>
          </div>
          <div class="collapsible-content">
            <div class="score-card" style="margin-bottom:0;border-left:none;">
              <div class="score-header">
                <span class="score-title"></span>
                <span class="score-badge" :style="{ background: scoreBarColor }">{{ scoreLabel }}</span>
              </div>
              <div class="score-bar-track">
                <div class="score-bar-fill" :style="{ width: scoreResult.score + '%', background: scoreBarColor }"></div>
              </div>
              <div class="score-value">{{ scoreResult.score }}分 · {{ scoreResult.label }}</div>
              <div class="score-details">
                <div v-for="d in scoreResult.details" :key="d.label" class="score-detail-item">
                  <span>{{ d.label }}</span>
                  <span :style="{ color: d.score >= d.max * 0.6 ? '#dc2626' : d.score >= d.max * 0.4 ? '#f59e0b' : '#16a34a' }">{{ d.score }}/{{ d.max }}</span>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- 内外盘 -->
        <div v-if="quoteData.outerPlateRaw > 0 || quoteData.innerPlateRaw > 0" :class="['collapsible-section', { collapsed: collapsed.plate }]">
          <div class="collapsible-header" @click="collapsed.plate = !collapsed.plate">
            <span class="collapsible-title">📊 内外盘数据</span>
            <span class="collapsible-arrow">▼</span>
          </div>
          <div class="collapsible-content">
            <div class="orderbook-card" style="margin-bottom:0;border-left:none;">
              <div class="orderbook-grid">
                <div class="orderbook-side buy">
                  <div class="side-label buy">外盘 · 主动买入</div>
                  <div class="side-value buy">{{ formatNumber(quoteData.outerPlateRaw) }}</div>
                  <div class="side-sub">看多力量</div>
                </div>
                <div class="orderbook-side sell">
                  <div class="side-label sell">内盘 · 主动卖出</div>
                  <div class="side-value sell">{{ formatNumber(quoteData.innerPlateRaw) }}</div>
                  <div class="side-sub">看空力量</div>
                </div>
              </div>
              <div class="orderbook-stats">
                <span>差值：<strong :class="quoteData.outerPlateRaw > quoteData.innerPlateRaw ? 'buy' : 'sell'">{{ (quoteData.outerPlateRaw - quoteData.innerPlateRaw).toLocaleString() }}</strong></span>
                <span>外盘占比：<strong class="buy">{{ quoteData.outerRatio }}%</strong></span>
              </div>
            </div>
          </div>
        </div>

        <!-- 分析 -->
        <div v-if="analysis && !analysis.disabled" :class="['collapsible-section', { collapsed: collapsed.analysis }]">
          <div class="collapsible-header" @click="collapsed.analysis = !collapsed.analysis">
            <span class="collapsible-title">{{ analysis.title }}</span>
            <span class="collapsible-arrow">▼</span>
          </div>
          <div class="collapsible-content">
            <div :class="['analysis-card', analysis.type]" style="margin-bottom:0;">
              <div class="analysis-desc">
                <div style="margin-bottom:6px;font-weight:700;">{{ analysis.desc }}</div>
                <div v-if="analysis.reason" style="font-size:0.75rem;color:var(--text-3);margin-bottom:4px;">{{ analysis.reason }}</div>
                <div v-if="analysis.position" style="font-size:0.75rem;color:var(--text-3);margin-bottom:4px;">位置：{{ analysis.position }}</div>
                <div v-if="analysis.volumeDesc" style="font-size:0.75rem;color:var(--text-3);margin-bottom:4px;">量能：{{ analysis.volumeDesc }}</div>
                <div style="font-size:0.8rem;color:var(--text-2);margin-top:8px;padding:8px 10px;background:var(--bg-muted);border-radius:8px;">{{ analysis.action }}</div>
              </div>
            </div>
          </div>
        </div>
        <div v-if="analysis && analysis.disabled" :class="['collapsible-section']">
          <div class="collapsible-header" @click="collapsed.analysis = !collapsed.analysis">
            <span class="collapsible-title">⛔ 策略不可用</span>
            <span class="collapsible-arrow">▼</span>
          </div>
          <div class="collapsible-content">
            <div class="analysis-card hold" style="margin-bottom:0;">
              <div class="analysis-desc">{{ analysis.reason }}</div>
            </div>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, inject } from 'vue'
import { apiFetch, isTradingTime, getNextRefreshDelay, getTradingStatusText, formatNowTime, formatNumber } from '../../utils/helpers'
import { calculateScore, getEnhancedAnalysis } from '../../utils/score'
import { useStockData } from '../../composables/useStockData'

const emit = defineEmits(['open-kline', 'open-ai-diag'])
const showNotification = inject('showNotification')

const { quickStocks, loadQuickStocks, loadScanPool, scanStockPool, stockNameMap, loadStockMap } = useStockData()

const stockInput = ref('sh603985')
const currentSymbol = ref('sh603985')
const quoteData = ref(null)
const loading = ref(false)
const error = ref('')
const lastUpdateTime = ref('--:--:--')
const marketAlert = ref('')
const collapsed = ref({ detail: true, score: false, plate: false, analysis: false })

const isInHot = computed(() => {
  if (!quoteData.value) return false
  return quickStocks.value.some(s => s.code === quoteData.value.symbol)
})

const scoreResult = computed(() => quoteData.value ? calculateScore(quoteData.value) : { score: 0, label: '', details: [] })
const scoreBarColor = computed(() => scoreResult.value.score >= 60 ? 'var(--red)' : scoreResult.value.score >= 40 ? 'var(--orange)' : 'var(--green)')
const scoreLabel = computed(() => scoreResult.value.score >= 60 ? '看涨' : scoreResult.value.score >= 40 ? '观望' : '看跌')
const analysis = computed(() => quoteData.value ? getEnhancedAnalysis(quoteData.value) : null)
const tradingStatusText = computed(() => getTradingStatusText())

let autoRefreshTimer = null

function parseTencentData(raw) {
  try {
    const lines = raw.split(';')
    for (let line of lines) {
      if (line.startsWith('v_') && line.includes('=')) {
        const parts = line.split('=')
        const symbol = parts[0].substring(2)
        const content = parts[1].replace(/^"|"$/g, '')
        const fields = content.split('~')
        if (fields.length > 49) {
          const price = parseFloat(fields[3]) || 0
          const yesterdayClose = parseFloat(fields[4]) || 0
          const changeVal = parseFloat(fields[31]) || (price - yesterdayClose)
          const changePct = parseFloat(fields[32]) || (yesterdayClose > 0 ? ((changeVal / yesterdayClose) * 100) : 0)
          const outerPlateRaw = parseInt(fields[7]) || 0
          const innerPlateRaw = parseInt(fields[8]) || 0
          const volumeRaw = parseInt(fields[36]) || 0
          const turnoverRaw = parseFloat(fields[37]) || 0
          const turnoverRate = parseFloat(fields[38]) || 0
          const pe = parseFloat(fields[39]) || 0
          const amplitude = parseFloat(fields[43]) || 0
          const volumeRatio = parseFloat(fields[49]) || 0
          const highLimit = parseFloat(fields[47]) || 0
          const lowLimit = parseFloat(fields[48]) || 0
          const circulateMarketCap = parseFloat(fields[44]) || 0
          const totalMarketCap = parseFloat(fields[45]) || 0
          const bid1Vol = parseInt(fields[12]) || 0
          const ask1Vol = parseInt(fields[22]) || 0
          const weibi = (bid1Vol + ask1Vol) > 0 ? (((bid1Vol - ask1Vol) / (bid1Vol + ask1Vol)) * 100) : 0
          const avgPrice = volumeRaw > 0 ? ((turnoverRaw * 10000) / (volumeRaw * 100)) : price
          const avgPriceDeviation = avgPrice > 0 ? (((price - avgPrice) / avgPrice) * 100) : 0
          const outerRatio = (outerPlateRaw + innerPlateRaw) > 0 ? ((outerPlateRaw / (outerPlateRaw + innerPlateRaw)) * 100) : 50

          return {
            name: fields[1], symbol, price: fields[3], priceRaw: price,
            change: changeVal, changePercent: changePct, isUp: changeVal > 0,
            open: fields[5], high: fields[33], low: fields[34],
            volume: fields[36], volumeRaw, turnover: turnoverRaw, turnoverRate, pe, amplitude,
            volumeRatio, highLimit, lowLimit, circulateMarketCap, totalMarketCap,
            bid1: fields[11], bid1Vol: fields[12], ask1: fields[21], ask1Vol: fields[22],
            outerPlate: fields[7], innerPlate: fields[8], outerPlateRaw, innerPlateRaw,
            weibi: parseFloat(weibi.toFixed(2)), avgPrice: parseFloat(avgPrice.toFixed(2)),
            avgPriceDeviation: parseFloat(avgPriceDeviation.toFixed(2)),
            outerRatio: parseFloat(outerRatio.toFixed(1)),
          }
        }
      }
    }
  } catch (e) { console.error('解析数据失败:', e) }
  return null
}

async function fetchStockQuote(symbol, silent = false) {
  if (!symbol) return
  const formatted = symbol.toLowerCase()
  if (!/^(sh|sz)\d{6}$/.test(formatted)) {
    if (!silent) error.value = '请输入有效的A股代码'
    return
  }
  // 静默刷新时只更新数据，不触发loading状态，避免页面闪烁
  if (!silent) {
    loading.value = true
    error.value = ''
  }
  try {
    const resp = await fetch(`/api/stock/${formatted}`)
    const raw = await resp.text()
    const parsed = parseTencentData(raw)
    if (parsed) {
      if (silent && quoteData.value && quoteData.value.symbol === parsed.symbol) {
        // 静默刷新：只更新真正变化的字段，避免不必要的DOM重渲染
        let hasChange = false
        for (const key of Object.keys(parsed)) {
          if (quoteData.value[key] !== parsed[key]) {
            quoteData.value[key] = parsed[key]
            hasChange = true
          }
        }
        // 只有数据真正变化时才更新时间戳
        if (hasChange) {
          lastUpdateTime.value = formatNowTime()
        }
      } else {
        quoteData.value = parsed
        lastUpdateTime.value = formatNowTime()
      }
    } else {
      throw new Error('数据解析失败')
    }
  } catch (e) {
    if (!silent) error.value = e.message
  } finally {
    if (!silent) loading.value = false
  }
}

async function onFetch() {
  let input = stockInput.value.trim()
  if (!input) { showNotification('请输入股票代码'); return }
  // 名称查找
  const map = stockNameMap.value
  if (map[input]) {
    currentSymbol.value = map[input]
  } else if (/^\d{6}$/.test(input)) {
    currentSymbol.value = input.startsWith('6') ? 'sh' + input : 'sz' + input
  } else {
    currentSymbol.value = input.toLowerCase()
  }
  stockInput.value = currentSymbol.value
  await fetchStockQuote(currentSymbol.value)
  scheduleRefresh()
}

function manualRefresh() {
  if (currentSymbol.value) fetchStockQuote(currentSymbol.value)
}

function selectQuickStock(code) {
  currentSymbol.value = code
  stockInput.value = code
  fetchStockQuote(code)
  scheduleRefresh()
}

function scheduleRefresh() {
  if (autoRefreshTimer) clearTimeout(autoRefreshTimer)
  const tick = async () => {
    if (currentSymbol.value) await fetchStockQuote(currentSymbol.value, true)
    autoRefreshTimer = setTimeout(tick, isTradingTime() ? 3000 : getNextRefreshDelay())
  }
  tick()
}

async function startAiDiagnose(symbol) {
  emit('open-ai-diag', symbol)
}

async function handleAddToHot(code, name) {
  try {
    const result = await apiFetch('/api/hot-stocks/add', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code, name })
    })
    if (result.success) {
      showNotification(`已添加 ${name} 到热门股票`)
      await loadQuickStocks()
    } else if (result.error) {
      showNotification(result.error)
    }
  } catch (e) {
    showNotification('添加失败')
  }
}

async function removeFromHot(code) {
  try {
    const result = await apiFetch('/api/hot-stocks/remove', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code })
    })
    if (result.success) {
      showNotification('已移出热门股票')
      await loadQuickStocks()
    }
  } catch (e) {
    showNotification('移除失败')
  }
}

function toggleHotStock() {
  if (!quoteData.value) return
  if (isInHot.value) {
    removeFromHot(quoteData.value.symbol)
  } else {
    handleAddToHot(quoteData.value.symbol, quoteData.value.name)
  }
}

function confirmRemoveQuickStock(stock) {
  if (confirm(`确定移除热门股票「${stock.name}」？`)) {
    removeFromHot(stock.code)
  }
}

async function checkMarketPredict() {
  try {
    const result = await apiFetch('/api/market-predict')
    if (result.success && result.data && result.data.alert) {
      marketAlert.value = result.data.alert
    }
  } catch (e) { /* silent */ }
}

onMounted(async () => {
  await Promise.all([loadStockMap(), loadQuickStocks(), loadScanPool()])
  fetchStockQuote(currentSymbol.value)
  scheduleRefresh()
  checkMarketPredict()
})

onUnmounted(() => {
  if (autoRefreshTimer) clearTimeout(autoRefreshTimer)
})

// 暴露给父组件的方法：搜索指定股票
function searchStock(symbol) {
  currentSymbol.value = symbol
  stockInput.value = symbol
  fetchStockQuote(symbol)
  scheduleRefresh()
}
defineExpose({ searchStock })
</script>
