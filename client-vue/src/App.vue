<template>
  <!-- 未登录时只显示登录页：持仓等私有数据按账号隔离，行情/推荐/收评登录后才可见入口 -->
  <!-- 带着本地 token 启动时先走校验，不渲染登录页：否则每次刷新都会闪一下登录界面，
       登录页还会顺手打 /api/auth/status 与 /api/auth/users 两个无用的请求 -->
  <div v-if="!authReady" class="boot-splash"><span>正在恢复登录状态…</span></div>
  <LoginPage v-else-if="!currentUser" @success="onLoginSuccess" />
  <div v-else class="main-container">
    <!-- 大盘指数栏：行动台的 hero 卡已经带了「大盘风险 + 三大指数」，在默认页再挂一条
         既是重复信息，也会每 5s 打 3 个指数请求（收盘后全是 --）。只在明细页显示。 -->
    <IndexBar v-if="activeTab !== 'tab-desk'" />
    <!-- 顶部功能标签栏（2026-10-13 改为「行动台 + 3 组」，9 个平级 Tab 降级为明细） -->
    <div class="app-body">
      <header class="top-header">
        <nav class="tab-bar" @click.stop>
          <button
            :class="['tab-bar-item', 'desk-entry', { active: activeTab === 'tab-desk' }]"
            @click="activateTab('tab-desk')"
          >
            🎯 行动台
          </button>

          <!-- 宽屏：三个组胶囊，点开弹层选子项 -->
          <div v-for="g in tabGroups" :key="g.key" class="tab-group">
            <button
              :class="['tab-bar-item', 'group-chip', { active: g.ids.includes(activeTab) }]"
              @click="toggleMenu(g.key, $event)"
            >
              <span class="group-label">{{ g.label }}</span>
              <span class="group-current">{{ shortLabel(activeTab, g) }}</span>
              <i class="caret">▾</i>
            </button>
          </div>

          <!-- 窄屏（办公室 500–900px）：合并成一个「明细」菜单 -->
          <div class="tab-group all-group">
            <button class="tab-bar-item group-chip" @click="toggleMenu('all', $event)">
              <span class="group-label">明细</span><i class="caret">▾</i>
            </button>
          </div>
        </nav>
      </header>
      <!-- 弹层必须 Teleport 到 body：.tab-bar 为了窄屏横向滚动写了 overflow-x:auto，
           这会让 overflow-y 的计算值从 visible 变成 auto，绝对定位的下拉菜单被自己裁掉 ——
           2026-10-13 手机上点「明细」"没反应"就是这个原因（菜单开了，但看不见）。 -->
      <Teleport to="body">
        <div v-if="menuItems.length" class="group-menu nav-menu" :style="menuStyle" data-nav-menu>
          <button
            v-for="t in menuItems"
            :key="t.id"
            :class="['menu-item', { active: activeTab === t.id }]"
            @click="activateTab(t.id)"
          >
            {{ t.label }}
          </button>
        </div>
      </Teleport>
      <div class="app-main">
        <div
          :class="['tab-content', { active: activeTab === 'tab-desk' }]"
          v-show="activeTab === 'tab-desk'"
          v-if="mountedTabs.has('tab-desk')"
        >
          <TabActionDesk :active="activeTab === 'tab-desk'" />
        </div>
        <div
          :class="['tab-content', { active: activeTab === 'tab-quote' }]"
          v-show="activeTab === 'tab-quote'"
          v-if="mountedTabs.has('tab-quote')"
        >
          <TabQuote
            ref="tabQuoteRef"
            :active="activeTab === 'tab-quote'"
            @open-kline="openKline"
            @open-diag="openDiag"
          />
        </div>
        <div
          :class="['tab-content', { active: activeTab === 'tab-scan' }]"
          v-show="activeTab === 'tab-scan'"
          v-if="mountedTabs.has('tab-scan')"
        >
          <TabScan :active="activeTab === 'tab-scan'" />
        </div>
        <div
          :class="['tab-content', { active: activeTab === 'tab-trend' }]"
          v-show="activeTab === 'tab-trend'"
          v-if="mountedTabs.has('tab-trend')"
        >
          <TabTrend :active="activeTab === 'tab-trend'" @open-kline="openKline" />
        </div>
        <div
          :class="['tab-content', { active: activeTab === 'tab-recommend' }]"
          v-show="activeTab === 'tab-recommend'"
          v-if="mountedTabs.has('tab-recommend')"
        >
          <TabRecommend :active="activeTab === 'tab-recommend'" @open-kline="openKline" />
        </div>
        <div
          :class="['tab-content', { active: activeTab === 'tab-position' }]"
          v-show="activeTab === 'tab-position'"
          v-if="mountedTabs.has('tab-position')"
        >
          <TabPosition ref="tabPositionRef" :active="activeTab === 'tab-position'" />
        </div>
        <div
          :class="['tab-content', { active: activeTab === 'tab-flow' }]"
          v-show="activeTab === 'tab-flow'"
          v-if="mountedTabs.has('tab-flow')"
        >
          <TabFlow :active="activeTab === 'tab-flow'" />
        </div>
        <div
          :class="['tab-content', { active: activeTab === 'tab-review' }]"
          v-show="activeTab === 'tab-review'"
          v-if="mountedTabs.has('tab-review')"
        >
          <TabReview @open-kline="openKline" />
        </div>
        <div
          :class="['tab-content', { active: activeTab === 'tab-sim' }]"
          v-show="activeTab === 'tab-sim'"
          v-if="mountedTabs.has('tab-sim')"
        >
          <TabSimTrade />
        </div>
        <div
          :class="['tab-content', { active: activeTab === 'tab-optimization' }]"
          v-show="activeTab === 'tab-optimization'"
          v-if="mountedTabs.has('tab-optimization')"
        >
          <TabOptimization />
        </div>
        <!-- 口径与开关（复盘组第三个子页，2026-10-17）：把原先写死在 config.py 的选股条件
             搬到界面上改，配实时影响预览；读=登录即可，写=管理员账号 -->
        <div
          :class="['tab-content', { active: activeTab === 'tab-settings' }]"
          v-show="activeTab === 'tab-settings'"
          v-if="mountedTabs.has('tab-settings')"
        >
          <TabSettings />
        </div>
        <!-- 回测曲线（复盘组第四个子页，2026-10-18）：历史推荐按买卖口径重放的净值曲线
             与真实胜率走势。曲线口径与命令行 scripts/rec_exit_sim.py 共用一份实现 -->
        <div
          :class="['tab-content', { active: activeTab === 'tab-backtest' }]"
          v-show="activeTab === 'tab-backtest'"
          v-if="mountedTabs.has('tab-backtest')"
        >
          <TabBacktest />
        </div>
        <!-- 接口用量（运维组，2026-10-19）：第三方 API 每日调用计数 + 上限预警，防平台封禁 -->
        <div
          :class="['tab-content', { active: activeTab === 'tab-api-usage' }]"
          v-show="activeTab === 'tab-api-usage'"
          v-if="mountedTabs.has('tab-api-usage')"
        >
          <TabApiUsage />
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
    <!-- 暗色切换（2026-10-13）：右下角浮层，与账号区同一层级，不打扰数据区 -->
    <button class="theme-btn" :title="isDark ? '切到亮色' : '切到暗色（盯盘护眼）'" @click="toggleTheme">
      {{ isDark ? '☀️' : '🌙' }}
    </button>
    <!-- K线弹窗：⚠️ 必须带 v-if。这两个组件是 defineAsyncComponent，但模板里没有 v-if 时
         App 一渲染就会去下载 KlineModal 那个约 1MB 的 chunk（内含全量 import 的 echarts），
         懒加载等于白做 —— 2026-10-17 实测这是首屏最重的一笔。两个组件的 visible watcher
         都已加 `immediate: true`，条件挂载后仍能正常取数。 -->
    <KlineModal
      v-if="klineVisible"
      :visible="klineVisible"
      :symbol="klineSymbol"
      :before-date="klineBeforeDate"
      @close="onKlineClose"
    />
    <!-- 五层诊断弹窗（纯本地数据聚合，无 AI 源） -->
    <StockDiagModal v-if="diagVisible" :visible="diagVisible" :symbol="diagSymbol" @close="diagVisible = false" />
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
  import { ref, computed, provide, watch, nextTick, onMounted, onUnmounted, defineAsyncComponent } from 'vue'
  import {
    useNotification,
    apiFetch,
    getToken,
    getAuthUser,
    saveAuth,
    clearAuth,
    saveRecentUser,
    setUnauthorizedHandler,
    isOrderableNow,
  } from './utils/helpers'
  import { useHashRoute, navigate } from './composables/useHashRoute'
  import { usePolling } from './composables/usePolling'
  import { useTheme } from './composables/useTheme'
  import LoginPage from './components/LoginPage.vue'
  import IndexBar from './components/IndexBar.vue'

  // 2026-08-31 性能优化：非默认 Tab 与弹窗改为动态导入（配合懒挂载）。
  // 2026-10-13：默认页换成「行动台」，实时行情降为普通 Tab，首屏只下载 desk chunk。
  const TabQuote = defineAsyncComponent(() => import('./components/tabs/TabQuote.vue'))
  const TabActionDesk = defineAsyncComponent(() => import('./components/tabs/TabActionDesk.vue'))
  const TabScan = defineAsyncComponent(() => import('./components/tabs/TabScan.vue'))
  const TabTrend = defineAsyncComponent(() => import('./components/tabs/TabTrend.vue'))
  const TabRecommend = defineAsyncComponent(() => import('./components/tabs/TabRecommend.vue'))
  const TabPosition = defineAsyncComponent(() => import('./components/tabs/TabPosition.vue'))
  const TabReview = defineAsyncComponent(() => import('./components/tabs/TabReview.vue'))
  const TabSimTrade = defineAsyncComponent(() => import('./components/tabs/TabSimTrade.vue'))
  const TabFlow = defineAsyncComponent(() => import('./components/tabs/TabFlow.vue'))
  const TabOptimization = defineAsyncComponent(() => import('./components/tabs/TabOptimization.vue'))
  const TabSettings = defineAsyncComponent(() => import('./components/tabs/TabSettings.vue'))
  const TabBacktest = defineAsyncComponent(() => import('./components/tabs/TabBacktest.vue'))
  const TabApiUsage = defineAsyncComponent(() => import('./components/tabs/TabApiUsage.vue'))
  const KlineModal = defineAsyncComponent(() => import('./components/KlineModal.vue'))
  const StockDiagModal = defineAsyncComponent(() => import('./components/StockDiagModal.vue'))

  // ========== hash 路由（2026-10-13）：刷新停在本页、链接可分享 ==========
  const { activeTab, route } = useHashRoute()

  // 懒挂载：已激活过的 Tab 集合。挂载后**永不销毁**（保留状态，且跨组件 ref 调用依赖实例存活），
  // 隐藏时只是由 usePolling 停掉定时器，不再打接口。
  const mountedTabs = ref(new Set(['tab-desk']))

  const TAB_LABELS = {
    'tab-quote': '实时行情',
    'tab-scan': '股票筛选',
    'tab-trend': '趋势发现',
    'tab-recommend': '智能推荐',
    'tab-position': '持仓管理',
    'tab-flow': '资金流向',
    'tab-review': '每日收评',
    'tab-sim': '模拟交易',
    'tab-optimization': '自动优化',
    'tab-settings': '口径与开关',
    'tab-backtest': '回测曲线',
    'tab-desk': '行动台',
    'tab-api-usage': '接口用量',
  }
  const tabGroups = [
    {
      key: 'market',
      label: '盘面',
      tabs: ['tab-quote', 'tab-scan', 'tab-trend', 'tab-flow'].map(id => ({ id, label: TAB_LABELS[id] })),
    },
    {
      key: 'decide',
      label: '决策',
      tabs: ['tab-recommend', 'tab-position', 'tab-sim'].map(id => ({ id, label: TAB_LABELS[id] })),
    },
    {
      key: 'review',
      label: '复盘',
      tabs: ['tab-review', 'tab-optimization', 'tab-settings', 'tab-backtest'].map(id => ({ id, label: TAB_LABELS[id] })),
    },
    {
      key: 'ops',
      label: '运维',
      tabs: ['tab-api-usage'].map(id => ({ id, label: TAB_LABELS[id] })),
    },
  ]
  tabGroups.forEach(g => {
    g.ids = g.tabs.map(t => t.id)
  })
  const allTabs = tabGroups.reduce((acc, g) => acc.concat(g.tabs), [])

  const openMenu = ref('')
  const menuAnchorEl = ref(null)
  const MENU_W = 168
  const menuPos = ref({ left: 0, top: 0, maxH: 240 })
  function toggleMenu(key, ev) {
    if (openMenu.value === key) {
      closeMenu()
      return
    }
    menuAnchorEl.value = ev ? ev.currentTarget : null
    openMenu.value = key
    nextTick(placeMenu)
  }
  function closeMenu() {
    openMenu.value = ''
    menuAnchorEl.value = null
  }
  // fixed 定位（元素已 Teleport 到 body）：贴着按钮下沿，并夹在视口内不溢出
  function placeMenu() {
    const el = menuAnchorEl.value
    if (!el) return
    const a = el.getBoundingClientRect()
    const left = Math.max(8, Math.min(a.left, window.innerWidth - MENU_W - 8))
    const top = a.bottom + 6
    menuPos.value = {
      left: Math.round(left),
      top: Math.round(top),
      maxH: Math.max(140, Math.round(window.innerHeight - top - 12)),
    }
  }
  const menuStyle = computed(() => ({
    left: `${menuPos.value.left}px`,
    top: `${menuPos.value.top}px`,
    width: `${MENU_W}px`,
    maxHeight: `${menuPos.value.maxH}px`,
  }))
  const menuItems = computed(() => {
    if (!openMenu.value) return []
    if (openMenu.value === 'all') return allTabs
    const g = tabGroups.find(x => x.key === openMenu.value)
    return g ? g.tabs : []
  })
  // 组胶囊上直接显示当前所在子页，收起后也知道自己在哪
  function shortLabel(id, g) {
    if (!g.ids.includes(id)) return g.tabs[0].label
    return TAB_LABELS[id]
  }
  function activateTab(id) {
    closeMenu()
    navigate(id.replace(/^tab-/, ''))
  }
  // 点页面其他地方收起菜单
  function onDocClick(e) {
    if (!openMenu.value) return
    // 弹层在 body 下，点它自己不该收起（收起由菜单项的 activateTab 负责）
    if (e && e.target && e.target.closest && e.target.closest('[data-nav-menu]')) return
    closeMenu()
  }
  // 滚动 / 缩放时让弹层跟着按钮走。
  // 注意：这里**不能**直接收起 —— 数据到达后浏览器会做 scroll anchoring 自动微调
  // scrollTop，收起会把刚点开的菜单立刻关掉（2026-10-13 实测 6 次里 3 次"点了没反应"）。
  function onViewportChange() {
    if (openMenu.value) placeMenu()
  }

  // 跨Tab导航：其他Tab点击股票 → 走 hash 打开实时行情并定位该票
  const tabQuoteRef = ref(null)
  function navigateToQuote(symbol) {
    navigate('quote', symbol || '')
  }
  provide('navigateToQuote', navigateToQuote)

  // hash 变化时统一处理：挂载对应 Tab；带 symbol 的行情深链交给 TabQuote 定位
  watch(
    [activeTab, () => route.value.param],
    ([tab, param], old) => {
      if (!mountedTabs.value.has(tab)) mountedTabs.value = new Set([...mountedTabs.value, tab])
      if (tab === 'tab-quote' && param) {
        // 目标 Tab 的异步 chunk 可能还没下载完，重试直到 ref 就绪
        const trySearch = tries => {
          if (tabQuoteRef.value && tabQuoteRef.value.searchStock) {
            tabQuoteRef.value.searchStock(param)
          } else if (tries > 0) {
            setTimeout(() => trySearch(tries - 1), 150)
          }
        }
        nextTick(() => trySearch(20))
      }
    },
    { immediate: true }
  )

  // ========== 大盘风险预警轮询 ==========
  // 2026-10-13 去掉 isTradingTime() 门禁：上班族常常在收盘后才打开页面，
  //   原逻辑会让"盘中错过就永远没有汇总"。
  // 2026-10-15 收回一半：预警**数据**照样随时拉取（行动台/收评里都能看到），
  //   但**弹窗**只在能操作的时间、且预警是当天的时候弹。
  //   起因是早上 7:18 手机弹「紧急警报：主力资金正在疯狂出逃！立即清仓！」——
  //   那是昨天 14:00 扫描留下的快照，盘前既不能挂单也来不及反应，
  //   一条过期的命令式警报只会让人想在开盘前先做点什么，或者整天心神不宁。
  const _lastAlertDate = ref('')

  function _todayStr() {
    const d = new Date()
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  }

  async function _checkMarketAlert() {
    try {
      const result = await apiFetch('/api/market-alert')
      if (!result.success || !result.data) return
      const alert = result.data
      // 同一天只推送一次
      if (alert.date === _lastAlertDate.value) return
      if (alert.riskScore >= 40) {
        _lastAlertDate.value = alert.date
        const pred = alert.prediction || {}
        const msg = `${pred.title || '大盘风险预警'} (风险评分${alert.riskScore}) ${pred.action || ''}`
        // 过期的快照与非交易时段：不弹，只留在页面里
        if (alert.date !== _todayStr()) return
        if (!isOrderableNow()) return
        const type = alert.riskScore >= 60 ? 'error' : 'warning'
        // 3 秒：这是每天一次的提醒，不是需要读完的正文 —— 详细内容在收评/行动台里都有。
        // 原先写 8000，手机上悬浮在页面顶部会把「行动台」标题和第一张卡压住太久（2026-10-13 反馈）。
        showNotification(msg, type, 3000)
      }
    } catch (e) {
      /* 静默失败 */
    }
  }

  const { notifications, showNotification, TYPE_ICONS } = useNotification()

  provide('showNotification', showNotification)

  // ========== 登录态（2026-10-12 多用户账号，持仓按账号隔离） ==========
  const authReady = ref(false)
  const currentUser = ref(getAuthUser())
  const tabPositionRef = ref(null)

  // 轮询必须声明在 currentUser 之后：usePolling 建 watch 时会立刻读一次 active，
  // 提前引用处于 TDZ 的 const 会让整个 App setup 抛错（2026-10-13 首屏白屏的成因）。
  const { start: startAlertPoll } = usePolling(_checkMarketAlert, {
    interval: 120000,
    immediate: false,
    active: () => !!currentUser.value,
  })

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
        saveAuth(getToken(), r.data.user) // 顺带刷新昵称等展示信息
        saveRecentUser(r.data.user) // 登录页的「已有账号」快捷切换靠它（用户列表接口已改为需登录）
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
    // 登录后回到登录前想看的页面（门禁期间 hash 已经解析好了，这里只需补挂载）
    if (!location.hash) navigate('desk')
    // 切换账号后立即重载持仓，避免残留上一个账号的数据
    nextTick(() => {
      if (tabPositionRef.value && tabPositionRef.value.loadPositions) {
        tabPositionRef.value.loadPositions()
      }
    })
  }

  async function logout() {
    try {
      await apiFetch('/api/auth/logout', { method: 'POST' })
    } catch (e) {
      /* 服务端不可达时本地清理即可 */
    }
    clearAuth()
    currentUser.value = null
  }

  // 私有接口返回 401（会话过期 / 账号被删）时，apiFetch 会调这个回调把界面切回登录页
  setUnauthorizedHandler(() => {
    currentUser.value = null
    showNotification('登录已过期，请重新登录', 'warning')
  })

  // ========== 暗色主题（2026-10-13 真正启用） ==========
  // 此前整套暗色 token 写在 @media (prefers-color-scheme: dark) 里，而代码中
  // data-theme="dark" 的选择器没有任何地方会设置该属性 → 暗色是不可达的死代码。
  // 现在样式全部挂在 [data-theme="dark"] 上，主题由 useTheme 决定（未手动选择时跟随系统）。
  const { isDark, toggleTheme } = useTheme()

  onMounted(() => {
    checkAuth()
    startAlertPoll()
    document.addEventListener('click', onDocClick)
    window.addEventListener('scroll', onViewportChange, true)
    window.addEventListener('resize', onViewportChange)
  })
  onUnmounted(() => {
    document.removeEventListener('click', onDocClick)
    window.removeEventListener('scroll', onViewportChange, true)
    window.removeEventListener('resize', onViewportChange)
  })

  // K线弹窗
  const klineVisible = ref(false)
  const klineSymbol = ref('')
  const klineBeforeDate = ref('') // 2026-10-16：推荐日之前复盘视图，空=正常最新全量
  function openKline(symbol, beforeDate) {
    klineSymbol.value = symbol
    klineBeforeDate.value = beforeDate || ''
    klineVisible.value = true
  }
  function onKlineClose() {
    klineVisible.value = false
    klineBeforeDate.value = ''
  }

  // 五层诊断弹窗
  const diagVisible = ref(false)
  const diagSymbol = ref('')
  function openDiag(symbol) {
    diagSymbol.value = symbol
    diagVisible.value = true
  }
</script>

<style scoped>
  /* ===== 顶栏分组导航 ===== */
  .tab-bar {
    align-items: center;
    gap: 6px;
  }
  .desk-entry {
    font-weight: 700;
  }

  .tab-group {
    position: relative;
    display: flex;
  }
  .group-chip {
    display: inline-flex;
    align-items: baseline;
    gap: 5px;
    white-space: nowrap;
  }
  .group-label {
    font-weight: 700;
  }
  .group-current {
    font-size: 0.75rem;
    font-weight: 500;
    color: var(--text-tertiary);
    max-width: 5.5em;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .group-chip.active .group-current {
    color: var(--text-secondary);
  }
  .caret {
    font-style: normal;
    font-size: 0.6875rem;
    color: var(--text-tertiary);
  }

  .group-menu {
    /* fixed：弹层已 Teleport 到 body，坐标按视口算（见 menuStyle） */
    position: fixed;
    z-index: 120;
    min-width: 132px;
    padding: 5px;
    display: flex;
    flex-direction: column;
    gap: 2px;
    overflow-y: auto;
    overscroll-behavior: contain;
    background: var(--bg-elevated);
    border: 1px solid var(--border-medium);
    border-radius: var(--radius-md);
    box-shadow: var(--shadow-large);
  }
  .menu-item {
    padding: 7px 10px;
    font-family: inherit;
    font-size: 0.8125rem;
    text-align: left;
    color: var(--text-secondary);
    background: transparent;
    border: none;
    border-radius: var(--radius-sm);
    cursor: pointer;
    white-space: nowrap;
  }
  .menu-item:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }
  .menu-item.active {
    background: var(--bg-tertiary);
    color: var(--accent-primary);
    font-weight: 700;
  }
  /* 触摸设备上 30px 高的菜单项太难点中，手机上加高到 ~44px */
  @media (max-width: 699px) {
    .menu-item {
      padding: 12px 14px;
      font-size: 0.875rem;
    }
  }

  /* 宽屏用组胶囊，窄屏（办公室常见窄窗口/手机）合并成单个「明细」菜单 */
  .all-group {
    display: none;
  }
  @media (max-width: 980px) {
    .tab-group:not(.all-group) {
      display: none;
    }
    .all-group {
      display: flex;
    }
  }

  /* 暗色切换按钮 */
  .theme-btn {
    position: fixed;
    right: 14px;
    bottom: 14px;
    z-index: 50;
    width: 34px;
    height: 34px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.9rem;
    background: var(--bg-secondary);
    border: 1px solid var(--border-medium);
    border-radius: 50%;
    box-shadow: 0 2px 10px rgba(0, 0, 0, 0.14);
    cursor: pointer;
  }
  .theme-btn:hover {
    border-color: var(--accent-strong, var(--accent-primary));
  }

  /* 启动占位：校验本地 token 期间（通常 <200ms）显示，避免闪登录页 */
  .boot-splash {
    min-height: 240px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.875rem;
    color: var(--text-tertiary);
    animation: bootPulse 1.1s ease-in-out infinite;
  }
  @keyframes bootPulse {
    0%,
    100% {
      opacity: 0.45;
    }
    50% {
      opacity: 1;
    }
  }

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
  .user-btn:hover {
    border-color: var(--accent-primary);
    color: var(--text-primary);
  }
</style>
