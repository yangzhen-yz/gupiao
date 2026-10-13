<template>
  <!-- 未登录时只显示登录页：持仓等私有数据按账号隔离，行情/推荐/收评登录后才可见入口 -->
  <LoginPage v-if="!authReady || !currentUser" @success="onLoginSuccess" />
  <div v-else class="main-container">
    <!-- 大盘指数栏 -->
    <IndexBar />
    <!-- 顶部功能标签栏（交易终端风格） -->
    <div class="app-body">
      <header class="top-header">
        <!-- 标签栏可横向滚动 -->
        <nav class="tab-bar">
          <button
            v-for="tab in tabs"
            :key="tab.id"
            :class="['tab-bar-item', { active: activeTab === tab.id }]"
            @click="activateTab(tab.id)"
          >{{ tab.label }}</button>
        </nav>
      </header>
      <div class="app-main">
        <div :class="['tab-content', { active: activeTab === 'tab-quote' }]" v-show="activeTab === 'tab-quote'" v-if="mountedTabs.has('tab-quote')">
          <TabQuote ref="tabQuoteRef" @open-kline="openKline" @open-ai-diag="openAiDiag" />
        </div>
        <div :class="['tab-content', { active: activeTab === 'tab-scan' }]" v-show="activeTab === 'tab-scan'" v-if="mountedTabs.has('tab-scan')">
          <TabScan />
        </div>
        <div :class="['tab-content', { active: activeTab === 'tab-trend' }]" v-show="activeTab === 'tab-trend'" v-if="mountedTabs.has('tab-trend')">
          <TabTrend @open-kline="openKline" />
        </div>
        <div :class="['tab-content', { active: activeTab === 'tab-recommend' }]" v-show="activeTab === 'tab-recommend'" v-if="mountedTabs.has('tab-recommend')">
          <TabRecommend />
        </div>
        <div :class="['tab-content', { active: activeTab === 'tab-position' }]" v-show="activeTab === 'tab-position'" v-if="mountedTabs.has('tab-position')">
          <TabPosition ref="tabPositionRef" />
        </div>
        <div :class="['tab-content', { active: activeTab === 'tab-flow' }]" v-show="activeTab === 'tab-flow'" v-if="mountedTabs.has('tab-flow')">
          <TabFlow />
        </div>
        <div :class="['tab-content', { active: activeTab === 'tab-review' }]" v-show="activeTab === 'tab-review'" v-if="mountedTabs.has('tab-review')">
          <TabReview @open-kline="openKline" />
        </div>
        <div :class="['tab-content', { active: activeTab === 'tab-sim' }]" v-show="activeTab === 'tab-sim'" v-if="mountedTabs.has('tab-sim')">
          <TabSimTrade />
        </div>
        <div :class="['tab-content', { active: activeTab === 'tab-optimization' }]" v-show="activeTab === 'tab-optimization'" v-if="mountedTabs.has('tab-optimization')">
          <TabOptimization />
        </div>
      </div>
    </div>
    <!-- 当前登录账号（固定在左下角浮层，不占用顶部空间）：持仓等私有数据按账号隔离 -->
    <div class="user-zone">
      <span class="user-avatar">{{ (currentUser.displayName || currentUser.username).slice(0, 1) }}</span>
      <span class="user-name" :title="`当前账号：${currentUser.username}`">
        {{ currentUser.displayName || currentUser.username }}
      </span>
      <button class="user-btn" @click="logout">切换账号</button>
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
import { ref, provide, nextTick, onMounted, onUnmounted, defineAsyncComponent } from 'vue'
import { useNotification, isTradingTime, apiFetch, getToken, getAuthUser, saveAuth, clearAuth, setUnauthorizedHandler } from './utils/helpers'
import LoginPage from './components/LoginPage.vue'
import IndexBar from './components/IndexBar.vue'
import TabQuote from './components/tabs/TabQuote.vue'

// 2026-08-31 性能优化：非默认 Tab 与弹窗改为动态导入（配合懒挂载）。
// 效果：首屏只加载「实时行情」+ 核心依赖；ECharts（仅 KlineModal 使用，~1MB）
// 与其余 Tab 拆成独立 chunk，首次用到时才下载，首屏 JS 体积显著下降。
const TabScan = defineAsyncComponent(() => import('./components/tabs/TabScan.vue'))
const TabTrend = defineAsyncComponent(() => import('./components/tabs/TabTrend.vue'))
const TabRecommend = defineAsyncComponent(() => import('./components/tabs/TabRecommend.vue'))
const TabPosition = defineAsyncComponent(() => import('./components/tabs/TabPosition.vue'))
const TabReview = defineAsyncComponent(() => import('./components/tabs/TabReview.vue'))
const TabSimTrade = defineAsyncComponent(() => import('./components/tabs/TabSimTrade.vue'))
const TabFlow = defineAsyncComponent(() => import('./components/tabs/TabFlow.vue'))
const TabOptimization = defineAsyncComponent(() => import('./components/tabs/TabOptimization.vue'))
const KlineModal = defineAsyncComponent(() => import('./components/KlineModal.vue'))
const AiDiagModal = defineAsyncComponent(() => import('./components/AiDiagModal.vue'))

const activeTab = ref('tab-quote')

// 懒挂载：已激活过的 Tab 集合（2026-08-31 性能优化）。
// 初始只挂载「实时行情」，其余 Tab 在首次点击时挂载，挂载后保持不销毁以保留状态。
const mountedTabs = ref(new Set(['tab-quote']))
function activateTab(id) {
  mountedTabs.value.add(id)
  activeTab.value = id
}

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

// ========== 登录态（2026-10-12 多用户账号，持仓按账号隔离） ==========
const authReady = ref(false)
const currentUser = ref(getAuthUser())
const tabPositionRef = ref(null)

async function checkAuth() {
  if (!getToken()) {
    currentUser.value = null
    authReady.value = true
    return
  }
  try {
    const r = await apiFetch('/api/auth/me')
    if (r && r.success && r.data && r.data.user) {
      currentUser.value = r.data.user
      saveAuth(getToken(), r.data.user)   // 顺带刷新昵称等展示信息
    } else {
      currentUser.value = null
      clearAuth()
    }
  } catch (e) {
    currentUser.value = null
    clearAuth()
  } finally {
    authReady.value = true
  }
}

function onLoginSuccess(user) {
  currentUser.value = user
  authReady.value = true
  showNotification(`欢迎，${user.displayName || user.username}`, 'success')
  // 切换账号后立即重载持仓，避免残留上一个账号的数据
  nextTick(() => {
    if (tabPositionRef.value && tabPositionRef.value.loadPositions) {
      tabPositionRef.value.loadPositions()
    }
  })
}

async function logout() {
  try { await apiFetch('/api/auth/logout', { method: 'POST' }) } catch (e) { /* 服务端不可达时本地清理即可 */ }
  clearAuth()
  currentUser.value = null
}

// 私有接口返回 401（会话过期 / 账号被删）时，apiFetch 会调这个回调把界面切回登录页
setUnauthorizedHandler(() => {
  currentUser.value = null
  showNotification('登录已过期，请重新登录', 'warning')
})

onMounted(() => {
  checkAuth()
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
  { id: 'tab-position', label: '持仓管理' },
  { id: 'tab-flow', label: '资金流向' },
  { id: 'tab-review', label: '每日收评' },
  { id: 'tab-sim', label: '模拟交易' },
  { id: 'tab-optimization', label: '自动优化' },
]
</script>

<style scoped>
/* 当前登录账号区（2026-10-12 多用户）：固定在左下角浮层，不占用顶部空间 */
.user-zone {
  position: fixed;
  left: 14px;
  bottom: 14px;
  z-index: 50;
  white-space: nowrap;
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 6px 10px;
  background: var(--bg-secondary);
  border: 1px solid var(--border-medium);
  border-radius: 999px;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.18);
  backdrop-filter: blur(4px);
}
.user-avatar {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: linear-gradient(135deg, #6366f1, #a855f7);
  color: #fff;
  font-size: 0.72rem;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.user-name {
  font-size: 0.8rem;
  color: var(--text-primary);
  max-width: 90px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.user-btn {
  padding: 3px 9px;
  font-size: 0.72rem;
  font-family: inherit;
  color: var(--text-secondary);
  background: var(--bg-tertiary);
  border: 1px solid var(--border-subtle);
  border-radius: 5px;
  cursor: pointer;
  white-space: nowrap;
}
.user-btn:hover { border-color: var(--accent-primary); color: var(--text-primary); }
</style>
