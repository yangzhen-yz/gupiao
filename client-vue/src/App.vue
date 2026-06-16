<template>
  <div class="main-container">
    <!-- 大盘指数栏 -->
    <IndexBar />
    <!-- Tab 导航 -->
    <div class="tab-nav">
      <button
        v-for="tab in tabs"
        :key="tab.id"
        :class="['tab-btn', { active: activeTab === tab.id }]"
        @click="activeTab = tab.id"
      >{{ tab.label }}</button>
    </div>
    <!-- Tab 内容 -->
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
    <div :class="['tab-content', { active: activeTab === 'tab-review' }]" v-show="activeTab === 'tab-review'">
      <TabReview @open-kline="openKline" />
    </div>
    <div :class="['tab-content', { active: activeTab === 'tab-strategy' }]" v-show="activeTab === 'tab-strategy'">
      <TabStrategy />
    </div>
    <!-- K线弹窗 -->
    <KlineModal :visible="klineVisible" :symbol="klineSymbol" @close="klineVisible = false" />
    <!-- AI诊断弹窗 -->
    <AiDiagModal :visible="aiDiagVisible" :symbol="aiDiagSymbol" @close="aiDiagVisible = false" />
    <!-- 全局通知 -->
    <div class="notification-container" v-if="notifications.length">
      <div v-for="n in notifications" :key="n.id" class="notification">{{ n.message }}</div>
    </div>
  </div>
</template>

<script setup>
import { ref, provide, nextTick } from 'vue'
import { useNotification } from './utils/helpers'
import IndexBar from './components/IndexBar.vue'
import TabQuote from './components/tabs/TabQuote.vue'
import TabScan from './components/tabs/TabScan.vue'
import TabTrend from './components/tabs/TabTrend.vue'
import TabRecommend from './components/tabs/TabRecommend.vue'
import TabReview from './components/tabs/TabReview.vue'
import TabStrategy from './components/tabs/TabStrategy.vue'
import KlineModal from './components/KlineModal.vue'
import AiDiagModal from './components/AiDiagModal.vue'

const activeTab = ref('tab-quote')
const { notifications, showNotification } = useNotification()

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
  { id: 'tab-review', label: '每日收评' },
  { id: 'tab-strategy', label: '策略优化' },
]
</script>
