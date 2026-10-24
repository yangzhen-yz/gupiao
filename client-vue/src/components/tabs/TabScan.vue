<template>
  <div>
    <div class="refresh-controls">
      <div class="toggle-section"><span class="toggle-label">📊 自动刷新中</span></div>
      <div class="last-update">{{ lastUpdateTime }}</div>
    </div>
    <div class="filter-bar">
      <label class="filter-label">过滤模式：</label>
      <select v-model="filterMode" class="filter-select" @change="resetPage">
        <option v-for="mode in tabConfig.filterModes" :key="mode" :value="mode">
          {{ tabConfig.modeLabels[mode] || mode }}
        </option>
      </select>
      <input
        v-if="filterMode === 'min_score'"
        v-model.number="customMinScore"
        type="number"
        min="0"
        max="100"
        class="filter-input"
        title="改这个数会保存为默认最低分（需管理员），刷新与手机端一致"
        @change="onMinScoreChange"
      />
      <span v-if="filterMode !== 'all'" class="filter-info"> ({{ filteredCount }} / {{ scanResults.length }} 只) </span>
    </div>
    <div v-if="scanning" class="scan-progress">
      <div class="loading-spinner" style="width: 20px; height: 20px; border-width: 2px; margin: 0"></div>
      正在扫描股票池（含趋势池与热门股扩充，覆盖更广）...
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
          <span>名称</span><span>现价</span><span>涨跌幅</span><span>量比</span><span>换手率</span><span>RPS</span
          ><span>综合评分</span><span>目标价/涨幅</span><span>止损</span><span>风控/估值</span>
        </div>
        <div v-for="r in pagedResults" :key="r.symbol" class="stock-table-row clickable" @click="goToQuote(r.symbol)">
          <span class="name">
            <span class="name-line">{{ r.name }}</span>
            <span
              v-if="mainlineMap[r.symbol]"
              class="mainline-tag"
              :class="'ml-' + mainlineMap[r.symbol].kind"
              :title="mainlineMap[r.symbol].sector + ' · 当前市场主线'"
              >{{ mainlineMap[r.symbol].sector }}</span
            >
            <br />
            <small style="color: #94a3b8; font-weight: 400">{{ r.symbol.toUpperCase() }}</small>
          </span>
          <span class="price" :style="{ color: changeColor(r.change ?? r.changePercent) }">{{ r.price }}</span>
          <span class="change-pct" :style="{ color: changeColor(r.change ?? r.changePercent), fontWeight: 600 }">{{
            formatChangePct(r.changePercent)
          }}</span>
          <span class="volume-ratio">{{ r.volumeRatio.toFixed(2) }}</span>
          <span class="turnover">{{ r.turnoverRate.toFixed(2) }}%</span>
          <span
            class="rps-cell"
            :style="{
              color:
                r.rps != null
                  ? r.rps >= 90
                    ? 'var(--accent-danger)'
                    : r.rps >= 70
                      ? '#f59e0b'
                      : r.rps >= 50
                        ? '#64748b'
                        : '#94a3b8'
                  : '#94a3b8',
              fontWeight: 700,
            }"
            >{{ r.rps != null ? r.rps.toFixed(0) : '-' }}</span
          >
          <span
            class="score-cell"
            :style="{ color: r.score >= 70 ? 'var(--accent-danger)' : r.score >= 40 ? '#f59e0b' : 'var(--accent-success)' }"
          >
            {{ r.score }}<br /><span :class="r.score >= 70 ? 'tag-bull' : r.score < 40 ? 'tag-bear' : 'tag-hold'">{{
              r.score >= 70 ? '看涨' : r.score < 40 ? '看跌' : '观望'
            }}</span>
          </span>
          <span class="target-cell">
            <template v-if="expectedGain(r) !== null">
              <div class="target-price">{{ parseFloat(r.buySellPoints.sell).toFixed(2) }}</div>
              <div class="gain-val">+{{ expectedGain(r).toFixed(1) }}%</div>
              <div v-if="r.buySellPoints.stockType" class="stock-type-tag" :class="roleClass(r)">
                {{ r.buySellPoints.stockType }}
              </div>
            </template>
            <span v-else class="na-val">—</span>
          </span>
          <span class="stoploss-cell">
            <template v-if="r.riskControl && r.riskControl.stop_loss != null && r.riskControl.stop_loss > 0">
              <div class="stop-price">{{ r.riskControl.stop_loss.toFixed(2) }}</div>
              <div class="loss-val">-{{ Math.abs(r.riskControl.stop_loss_pct || 0).toFixed(1) }}%</div>
            </template>
            <span v-else class="na-val">—</span>
          </span>
          <span class="risk-cell">
            <span class="risk-badge" :class="riskLevelClass(r.riskControl?.risk_level)">{{
              riskLabel(r.riskControl?.risk_level)
            }}</span>
            <span
              v-if="r.valuation?.overvalued"
              class="val-badge over"
              :title="`PE百分位${r.valuation?.pe_pct}% / PB百分位${r.valuation?.pb_pct}%`"
              >估值{{ r.valuation?.pe_pct }}%</span
            >
          </span>
        </div>
      </div>

      <!-- 分页控件 -->
      <div v-if="totalPages > 1" class="pagination">
        <button class="page-btn" :disabled="currentPage === 1" @click="goPage(1)">« 首页</button>
        <button class="page-btn" :disabled="currentPage === 1" @click="goPage(currentPage - 1)">‹ 上一页</button>
        <span class="page-info">第 {{ currentPage }} / {{ totalPages }} 页 · 共 {{ sortedResults.length }} 条</span>
        <button class="page-btn" :disabled="currentPage === totalPages" @click="goPage(currentPage + 1)">
          下一页 ›
        </button>
        <button class="page-btn" :disabled="currentPage === totalPages" @click="goPage(totalPages)">末页 »</button>
      </div>
    </div>
    <div v-else-if="!scanning" style="text-align: center; padding: 2rem; color: #64748b">暂无扫描结果</div>
  </div>
</template>

<script setup>
  import { ref, computed, onMounted, inject, watch } from 'vue'
  import {
    apiFetch,
    isTradingTime,
    getNextRefreshDelay,
    formatNowTime,
    riskLabel,
    riskLevelClass,
  } from '../../utils/helpers'
  import { getRating } from '../../utils/score'
  import { matchMainLine } from '../../utils/mainline'
  import { useStockData } from '../../composables/useStockData'
  import { usePolling } from '../../composables/usePolling'

  // 轮询治理（2026-10-13）：本 Tab 的自动刷新是**全市场扫描**，最贵的一路。
  // Tab 隐藏时必须停掉，否则用户点开过一次之后，它就在后台每 30s 扫一遍。
  const props = defineProps({ active: { type: Boolean, default: true } })

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
  const tabConfig = ref({
    // 后端配置缓存
    filterModes: ['leaders_only', 'all', 'bull_only', 'bull_and_hold', 'min_score'],
    defaultMode: 'min_score',
    defaultMinScore: 60,
    bullMinScore: 70,
    bearMaxScore: 40,
    leadersMinGain: 3.0,
    modeLabels: {
      leaders_only: '仅优质龙头',
      all: '全部',
      bull_only: '仅看涨',
      bull_and_hold: '观望以上',
      min_score: '自定义最低分',
    },
  })
  const filterMode = ref('min_score')
  const customMinScore = ref(60) // 最低分：以服务端配置页的值为准，出厂兜底 60
  // 上次从服务端拿到的默认值：用来判断"配置页改过没有"，只有真变了才覆盖输入框
  const serverMinScore = ref(null)

  // 异步加载后端配置（不阻塞 onMounted 主流程；失败用默认值兜底）
  async function loadTabConfig() {
    try {
      const r = await apiFetch('/api/tab-scan-config')
      if (r && r.success && r.data) {
        tabConfig.value = { ...tabConfig.value, ...r.data }
        filterMode.value = r.data.defaultMode || 'min_score'
        const ms = Number(r.data.defaultMinScore)
        if (Number.isFinite(ms)) {
          serverMinScore.value = ms
          customMinScore.value = ms
        }
      }
    } catch (e) {
      console.warn('加载 tab-scan-config 失败，使用默认值:', e.message)
    }
  }

  // 最低分默认值由配置页管理（复盘 → 口径与开关 → 股票筛选页），存在服务端 app_settings。
  // 在筛选页直接改这个数字＝把默认值改掉：保存成功后刷新、手机端都一致；
  // 非管理员接口回 403，则只对本次浏览生效，并明确提示"没保存"，不假装成功。
  async function onMinScoreChange() {
    resetPage()
    const raw = Number(customMinScore.value)
    if (!Number.isFinite(raw)) return
    const val = Math.min(Math.max(Math.round(raw), 0), 100)
    if (val === serverMinScore.value) {
      customMinScore.value = val
      return
    }
    try {
      const res = await apiFetch('/api/settings/tab_scan_default_min_score', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value: val }),
      })
      if (res && res.success) {
        serverMinScore.value = val
        customMinScore.value = val
        showNotification(`最低分默认值已改为 ${val} 分（刷新与手机端一致）`, 'success')
      } else {
        // 403 的 detail 是给管理员看的排查说明（含账号 id），手机上会撑满一屏；
        // toast 只给结论，原文留在控制台。其他校验错误通常很短，照原样显示。
        const detail = String((res && res.detail) || '')
        if (detail) console.warn('保存最低分默认值失败:', detail)
        const reason = /无权修改/.test(detail) ? '当前账号无权修改（只有管理员能改）' : (detail || '保存失败')
        showNotification(`未能保存默认值：${reason}；本次仅临时生效`, 'error')
      }
    } catch (e) {
      showNotification(`未能保存默认值（${e.message || e}）；本次仅临时生效`, 'error')
    }
  }

  // 切回本 Tab 时同步一次配置页的改动（组件用 v-show 保活，不会重新挂载）。
  // 只在服务端值真的变了才覆盖输入框，避免冲掉用户刚敲的临时值。
  watch(
    () => props.active,
    async on => {
      if (!on) return
      try {
        const r = await apiFetch('/api/tab-scan-config')
        const ms = Number(r && r.success && r.data && r.data.defaultMinScore)
        if (Number.isFinite(ms) && ms !== serverMinScore.value) {
          serverMinScore.value = ms
          customMinScore.value = ms
        }
      } catch (e) {
        /* 同步失败不影响筛选，保持当前值 */
      }
    }
  )

  const PAGE_SIZE = 10
  const currentPage = ref(1)

  function resetPage() {
    currentPage.value = 1
  }

  // 过滤后的列表：先按分数倒序，再按过滤模式筛选
  // 阈值全部读自 tabConfig（后端单一源），不在前端硬编码
  const sortedResults = computed(() => {
    const sorted = [...scanResults.value].sort((a, b) => b.score - a.score)
    const { bullMinScore, bearMaxScore, leadersMinGain } = tabConfig.value
    if (filterMode.value === 'leaders_only') {
      // 仅优质龙头（2026-08-31 默认模式）：去掉 跟风/杂毛 弱势票，且预期涨幅达标。
      // 实测扫描宇宙中 93% 是跟风/杂毛，用户只需高预期龙头/先锋/中军。
      const minGain = leadersMinGain ?? 3.0
      return sorted.filter(r => {
        const role = r.buySellPoints?.stockType || ''
        if (/跟风|杂毛/.test(role)) return false
        const g = expectedGain(r)
        return g !== null && g >= minGain
      })
    }
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

  // 主线标记：按股票名称匹配当前主线板块词库，生成 symbol → 板块信息 映射
  const mainlineMap = computed(() => {
    const m = {}
    for (const r of sortedResults.value) {
      const hit = r.name ? matchMainLine(r.name) : null
      if (hit) m[r.symbol] = hit
    }
    return m
  })
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

  // 涨跌幅（2026-10-23）：字段偶发缺失时显示「—」，不再整行报错；红涨绿跌、平盘灰
  // 2026-10-24：红绿改用语义令牌（CSS 变量在暗色主题自动翻亮色变体）
  function changeColor(v) {
    const n = Number(v)
    if (!Number.isFinite(n) || n === 0) return '#64748b'
    return n > 0 ? 'var(--accent-danger)' : 'var(--accent-success)'
  }

  function formatChangePct(v) {
    if (v === null || v === undefined || v === '') return '—'
    const n = Number(v)
    if (!Number.isFinite(n)) return '—'
    return `${n > 0 ? '+' : ''}${n.toFixed(2)}%`
  }

  // 预期涨幅：(目标价 - 现价) / 现价 * 100
  // buySellPoints.sell 后端返回字符串（如 "12.34"），需 parseFloat；
  // buySellPoints 为 null/{} 或 sell 为空 → 返回 null，模板显示 "—"
  function expectedGain(r) {
    if (!r.buySellPoints || !r.buySellPoints.sell) return null
    const sell = parseFloat(r.buySellPoints.sell)
    if (isNaN(sell) || !r.price || r.price <= 0) return null
    return ((sell - r.price) / r.price) * 100
  }

  // 角色 CSS 类（7级分类）
  function roleClass(r) {
    const t = r.buySellPoints?.stockType || ''
    if (t.includes('总龙头')) return 'role-total-leader'
    if (t.includes('板块龙头')) return 'role-sector-leader'
    if (t.includes('中军')) return 'role-core-trend'
    if (t.includes('情绪先锋')) return 'role-vanguard'
    if (t.includes('补涨龙')) return 'role-catch-up'
    if (t.includes('跟风')) return 'role-follower'
    if (t.includes('杂毛')) return 'role-marginal'
    return 'role-marginal'
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

  // 自动扫描：交易时段 30s，休市时睡到下一个开盘点；仅本 Tab 可见时执行
  const { start: startScanPolling } = usePolling(() => startScan(true), {
    interval: () => (isTradingTime() ? 30000 : getNextRefreshDelay()),
    active: () => props.active,
    immediate: false,
  })

  onMounted(async () => {
    await loadScanPool()
    scanPool.value = scanStockPool.value
    // 2026-07-08：先加载后端配置（设置默认 filterMode），再启动扫描
    // 配置加载失败时使用默认 'all'，扫描仍可正常进行
    await loadTabConfig()
    await startScan(true)
    startScanPolling()
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

  /* 表格列（10 列）：名称/现价/涨跌幅/量比/换手率/RPS/综合评分/目标价涨幅/止损/风控估值 */
  /* 移除委比列（盘口瞬时数据，对批量筛选无决策价值）；新增目标价涨幅 + 止损列 */
  .stock-table-header,
  .stock-table-row {
    grid-template-columns: 2fr 1fr 1fr 0.8fr 0.8fr 0.7fr 1.1fr 1.1fr 1fr 1.1fr;
  }
  .rps-cell {
    font-family: 'SF Mono', 'Courier New', monospace;
    font-weight: 700;
  }
  /* 目标价/涨幅列 + 止损列：价格继承默认色，涨幅绿色/止损红色（CSS 变量兼容暗色模式） */
  .target-cell,
  .stoploss-cell {
    font-family: 'SF Mono', 'Courier New', monospace;
    font-size: 0.8125rem;
    line-height: 1.3;
  }
  .target-cell .gain-val {
    color: var(--scan-gain-color, #dc2626);
    font-weight: 600;
  }
  .stock-type-tag {
    display: inline-block;
    font-size: 0.6875rem;
    font-weight: 600;
    padding: 1px 6px;
    border-radius: 3px;
    margin-top: 2px;
    color: #475569;
    background: #f1f5f9;
    border: 0.5px solid #e2e8f0;
  }
  /* 7级角色配色 */
  .stock-type-tag.role-total-leader {
    color: #b91c1c;
    background: #fef2f2;
    border-color: #fecaca;
  }
  .stock-type-tag.role-sector-leader {
    color: #c2410c;
    background: #fff7ed;
    border-color: #fed7aa;
  }
  .stock-type-tag.role-core-trend {
    color: #1d4ed8;
    background: #eff6ff;
    border-color: #bfdbfe;
  }
  .stock-type-tag.role-vanguard {
    color: #7e22ce;
    background: #faf5ff;
    border-color: #e9d5ff;
  }
  .stock-type-tag.role-catch-up {
    color: #0e7490;
    background: #ecfeff;
    border-color: #a5f3fc;
  }
  .stock-type-tag.role-follower {
    color: #475569;
    background: #f1f5f9;
    border-color: #e2e8f0;
  }
  .stock-type-tag.role-marginal {
    color: #78716c;
    background: #f5f5f4;
    border-color: #d6d3d1;
  }
  /* 暗色模式 */
  :root[data-theme='dark'] .stock-type-tag {
    color: #94a3b8;
    background: rgba(71, 85, 105, 0.15);
    border-color: rgba(226, 232, 240, 0.2);
  }
  :root[data-theme='dark'] .stock-type-tag.role-total-leader {
    color: #fca5a5;
    background: rgba(185, 28, 28, 0.15);
    border-color: rgba(254, 202, 202, 0.3);
  }
  :root[data-theme='dark'] .stock-type-tag.role-sector-leader {
    color: #fdba74;
    background: rgba(194, 65, 12, 0.15);
    border-color: rgba(254, 215, 170, 0.3);
  }
  :root[data-theme='dark'] .stock-type-tag.role-core-trend {
    color: #93c5fd;
    background: rgba(29, 78, 216, 0.15);
    border-color: rgba(191, 219, 254, 0.3);
  }
  :root[data-theme='dark'] .stock-type-tag.role-vanguard {
    color: #d8b4fe;
    background: rgba(126, 34, 206, 0.15);
    border-color: rgba(233, 213, 255, 0.3);
  }
  :root[data-theme='dark'] .stock-type-tag.role-catch-up {
    color: #67e8f9;
    background: rgba(14, 116, 144, 0.15);
    border-color: rgba(165, 243, 252, 0.3);
  }
  :root[data-theme='dark'] .stock-type-tag.role-follower {
    color: #94a3b8;
    background: rgba(71, 85, 105, 0.15);
    border-color: rgba(226, 232, 240, 0.2);
  }
  :root[data-theme='dark'] .stock-type-tag.role-marginal {
    color: #a8a29e;
    background: rgba(120, 113, 108, 0.15);
    border-color: rgba(214, 211, 209, 0.2);
  }
  .stoploss-cell .loss-val {
    color: var(--scan-loss-color, #16a34a);
    font-weight: 600;
  }
  .na-val {
    color: var(--scan-na-color, #94a3b8);
  }

  /* 主线徽标：进攻主线暖色 / 防守主线冷色 */
  .name-line {
    display: inline-block;
    vertical-align: middle;
  }
  .mainline-tag {
    display: inline-block;
    vertical-align: middle;
    margin-left: 6px;
    font-size: 0.625rem;
    font-weight: 700;
    line-height: 1;
    padding: 2px 6px;
    border-radius: 3px;
    white-space: nowrap;
  }
  .mainline-tag.ml-attack {
    color: #b91c1c;
    background: #fef2f2;
    border: 0.5px solid #fecaca;
  }
  .mainline-tag.ml-defense {
    color: #1d4ed8;
    background: #eff6ff;
    border: 0.5px solid #bfdbfe;
  }
  :root[data-theme='dark'] .mainline-tag.ml-attack {
    color: #fca5a5;
    background: rgba(185, 28, 28, 0.15);
    border-color: rgba(254, 202, 202, 0.3);
  }
  :root[data-theme='dark'] .mainline-tag.ml-defense {
    color: #93c5fd;
    background: rgba(29, 78, 216, 0.15);
    border-color: rgba(191, 219, 254, 0.3);
  }
  /* 2026-10-23：断点从 640 提到 768，接管原先全局 nth-child 藏列的窄屏区间；
     手机/窄窗固定保留 名称/现价/涨跌幅/综合评分 四列，不再把涨跌幅整列藏掉。 */
  @media (max-width: 768px) {
    .stock-table-header,
    .stock-table-row {
      grid-template-columns: 2fr 1fr 1fr 1.2fr;
    }
    /* 移动端隐藏：量比(4)/换手(5)/RPS(6)/目标价涨幅(8)/止损(9)/风控估值(10)，保留 名称/现价/涨跌幅/评分 */
    .stock-table-header span:nth-child(4),
    .stock-table-header span:nth-child(5),
    .stock-table-header span:nth-child(6),
    .stock-table-header span:nth-child(8),
    .stock-table-header span:nth-child(9),
    .stock-table-header span:nth-child(10),
    .stock-table-row span:nth-child(4),
    .stock-table-row span:nth-child(5),
    .stock-table-row span:nth-child(6),
    .stock-table-row span:nth-child(8),
    .stock-table-row span:nth-child(9),
    .stock-table-row span:nth-child(10) {
      display: none;
    }
  }
</style>
