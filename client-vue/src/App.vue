<template>
  <div class="main-container">
    <!-- 大盘指数栏 -->
    <IndexBar />
    <!-- 顶部功能标签栏（交易终端风格） -->
    <div class="app-body">
      <nav class="tab-bar">
        <button
          v-for="tab in tabs"
          :key="tab.id"
          :class="['tab-bar-item', { active: activeTab === tab.id }]"
          @click="activeTab = tab.id"
        >{{ tab.label }}</button>
      </nav>
      <div class="app-main">
        <div :class="['tab-content', { active: activeTab === 'tab-quote' }]" v-show="activeTab === 'tab-quote'">
          <TabQuote ref="tabQuoteRef" @open-kline="openKline" @open-ai-diag="openAiDiag" />
        </div>
        <div :class="['tab-content', { active: activeTab === 'tab-scan' }]" v-show="activeTab === 'tab-scan'">
          <TabScan />
        </div>
        <div :class="['tab-content', { active: activeTab === 'tab-trend' }]" v-show="activeTab === 'tab-trend'">
          <TabTrend @open-kline="openKline" />
        </div>
        <div :class="['tab-content', { active: activeTab === 'tab-recommend' }]" v-show="activeTab === 'tab-recommend'">
          <TabRecommend />
        </div>
        <div :class="['tab-content', { active: activeTab === 'tab-flow' }]" v-show="activeTab === 'tab-flow'">
          <TabFlow />
        </div>
        <div :class="['tab-content', { active: activeTab === 'tab-review' }]" v-show="activeTab === 'tab-review'">
          <TabReview @open-kline="openKline" />
        </div>
        <div :class="['tab-content', { active: activeTab === 'tab-backtest' }]" v-show="activeTab === 'tab-backtest'">
          <TabBacktest />
        </div>
        <div :class="['tab-content', { active: activeTab === 'tab-strategy' }]" v-show="activeTab === 'tab-strategy'">
          <TabStrategy @open-kline="openKline" />
        </div>
      </div>
    </div>
    <!-- K线弹窗 -->
    <KlineModal :visible="klineVisible" :symbol="klineSymbol" @close="klineVisible = false" />
    <!-- AI诊断弹窗 -->
    <AiDiagModal :visible="aiDiagVisible" :symbol="aiDiagSymbol" @close="aiDiagVisible = false" />
    <!-- 全局通知 -->
    <TransitionGroup name="toast" tag="div" class="notification-container">
      <div v-for="n in notifications" :key="n.id" :class="['notification', `toast-${n.type}`]">
        <span class="toast-icon">{{ TYPE_ICONS[n.type] || TYPE_ICONS.info }}</span>
        <span class="toast-msg">{{ n.message }}</span>
      </div>
    </TransitionGroup>
  </div>
</template>

<script setup>
import { ref, provide, nextTick, onMounted, onUnmounted } from 'vue'
import { useNotification, isTradingTime, apiFetch } from './utils/helpers'
import IndexBar from './components/IndexBar.vue'
import TabQuote from './components/tabs/TabQuote.vue'
import TabScan from './components/tabs/TabScan.vue'
import TabTrend from './components/tabs/TabTrend.vue'
import TabRecommend from './components/tabs/TabRecommend.vue'
import TabReview from './components/tabs/TabReview.vue'
import TabStrategy from './components/tabs/TabStrategy.vue'
import TabFlow from './components/tabs/TabFlow.vue'
import TabBacktest from './components/tabs/TabBacktest.vue'
import KlineModal from './components/KlineModal.vue'
import AiDiagModal from './components/AiDiagModal.vue'

const activeTab = ref('tab-quote')
const { notifications, showNotification, TYPE_ICONS } = useNotification()

provide('showNotification', showNotification)

// 跨Tab导航：其他Tab点击股票时跳转到实时行情并搜索
const tabQuoteRef = ref(null)
function navigateToQuote(symbol) {
  activeTab.value = 'tab-quote'
  // 等待DOM更新后调用TabQuote的搜索方法
  nextTick(() => {
    if (tabQuoteRef.value && tabQuoteRef.value.searchStock) {
      tabQuoteRef.value.searchStock(symbol)
    }
  })
}
provide('navigateToQuote', navigateToQuote)

// ========== 大盘风险预警轮询 ==========
const _lastAlertDate = ref('')
let _alertTimer = null

async function _checkMarketAlert() {
  if (!isTradingTime()) return
  try {
    const result = await apiFetch('/api/market-alert')
    if (!result.success || !result.data) return
    const alert = result.data
    // 同一天只推送一次
    if (alert.date === _lastAlertDate.value) return
    if (alert.riskScore >= 40) {
      _lastAlertDate.value = alert.date
      const pred = alert.prediction || {}
      const type = alert.riskScore >= 60 ? 'error' : 'warning'
      const msg = `${pred.title || '大盘风险预警'} (风险评分${alert.riskScore}) ${pred.action || ''}`
      showNotification(msg, type, 8000)
    }
  } catch (e) { /* 静默失败 */ }
}

onMounted(() => {
  _checkMarketAlert()
  _alertTimer = setInterval(_checkMarketAlert, 60000)
})
onUnmounted(() => {
  if (_alertTimer) clearInterval(_alertTimer)
})

// K线弹窗
const klineVisible = ref(false)
const klineSymbol = ref('')
function openKline(symbol) {
  klineSymbol.value = symbol
  klineVisible.value = true
}

// AI诊断弹窗
const aiDiagVisible = ref(false)
const aiDiagSymbol = ref('')
function openAiDiag(symbol) {
  aiDiagSymbol.value = symbol
  aiDiagVisible.value = true
}

const tabs = [
  { id: 'tab-quote', label: '实时行情' },
  { id: 'tab-scan', label: '股票筛选' },
  { id: 'tab-trend', label: '趋势发现' },
  { id: 'tab-recommend', label: '智能推荐' },
  { id: 'tab-flow', label: '资金流向' },
  { id: 'tab-review', label: '每日收评' },
  { id: 'tab-backtest', label: '策略回测' },
  { id: 'tab-strategy', label: '策略优化' },
]
</script>
