<template>
  <div>
    <!-- 大盘紧急提醒 -->
    <div v-if="marketAlert" class="market-alert-container" v-html="marketAlert"></div>

    <div class="control-section">
      <div class="input-group">
        <div class="input-wrapper">
          <label>股票代码/名称</label>
          <div class="input-row">
            <input
              type="text"
              v-model="stockInput"
              class="stock-input"
              placeholder="输代码或名称：603985 / 美诺华 / 新乡化纤..."
              @keyup.enter="onFetch"
            />
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
          <div v-for="s in quickStocks" :key="s.code" class="quick-btn-wrapper">
            <button
              class="quick-btn"
              :class="{ active: currentSymbol === s.code }"
              @click="selectQuickStock(s.code)"
              @contextmenu.prevent="confirmRemoveQuickStock(s)"
            >
              {{ s.name }}
            </button>
            <button class="quick-btn-remove" title="移除该股票" @click.stop="confirmRemoveQuickStock(s)">×</button>
          </div>
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
      <div v-if="loading" class="price-card" style="text-align: center">
        <div class="loading-spinner"></div>
        <div style="font-size: 0.9rem; font-weight: 500; color: var(--text-2)">正在加载数据...</div>
      </div>
      <div v-else-if="error" class="price-card" style="text-align: center; padding: 2rem">
        <div style="font-size: 1.1rem; color: var(--accent-danger); margin-bottom: 0.5rem">{{ error }}</div>
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
          <div style="display: flex; gap: 0.5rem; margin-bottom: 0.8rem; padding: 0 0.25rem">
            <button
              class="btn-secondary"
              style="
                flex: 1;
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 0.4rem;
                background: var(--accent-info-bg);
                color: var(--accent-info);
                border-color: var(--accent-info-border);
              "
              @click="$emit('open-kline', quoteData.symbol)"
            >
              K线图
            </button>
            <button
              class="btn-secondary"
              style="
                flex: 1;
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 0.4rem;
                background: var(--accent-warning-bg);
                color: var(--accent-warning);
                border-color: var(--accent-warning-border);
              "
              @click="startAiDiagnose(quoteData.symbol)"
            >
              AI诊断
            </button>
          </div>
          <div class="stock-card-actions" style="padding: 0 0.25rem">
            <button class="add-to-hot-btn" @click="toggleHotStock">
              {{ isInHot ? '⭐ 移出热门' : '⭐ 添加到热门股票' }}
            </button>
          </div>

          <!-- 详细数据 -->
          <div :class="['collapsible-section', { collapsed: collapsed.detail }]">
            <div class="collapsible-header" @click="collapsed.detail = !collapsed.detail">
              <span class="collapsible-title">📋 详细数据</span>
              <span class="collapsible-arrow">▼</span>
            </div>
            <div class="collapsible-content">
              <div class="stock-grid">
                <div class="grid-item">
                  <div class="grid-label">今开</div>
                  <div class="grid-value">{{ quoteData.open || '--' }}</div>
                </div>
                <div class="grid-item">
                  <div class="grid-label">最高</div>
                  <div class="grid-value">{{ quoteData.high || '--' }}</div>
                </div>
                <div class="grid-item">
                  <div class="grid-label">最低</div>
                  <div class="grid-value">{{ quoteData.low || '--' }}</div>
                </div>
                <div class="grid-item">
                  <div class="grid-label">成交量</div>
                  <div class="grid-value">{{ formatNumber(quoteData.volumeRaw) }}</div>
                </div>
                <div class="grid-item">
                  <div class="grid-label">成交额</div>
                  <div class="grid-value">{{ formatNumber(quoteData.turnover * 10000) }}</div>
                </div>
                <div class="grid-item">
                  <div class="grid-label">换手率</div>
                  <div class="grid-value">{{ (quoteData.turnoverRate || 0).toFixed(2) }}%</div>
                </div>
                <div class="grid-item">
                  <div class="grid-label">量比</div>
                  <div
                    class="grid-value"
                    :style="{
                      color:
                        quoteData.volumeRatio > 1.5
                          ? 'var(--accent-danger)'
                          : quoteData.volumeRatio < 0.5
                            ? 'var(--accent-success)'
                            : 'var(--text-primary)',
                    }"
                  >
                    {{ (quoteData.volumeRatio || 0).toFixed(2) }}
                  </div>
                </div>
                <div class="grid-item">
                  <div class="grid-label">市盈率</div>
                  <div class="grid-value">{{ quoteData.pe > 0 ? quoteData.pe.toFixed(2) : '亏损' }}</div>
                </div>
                <div class="grid-item">
                  <div class="grid-label">振幅</div>
                  <div class="grid-value">{{ (quoteData.amplitude || 0).toFixed(2) }}%</div>
                </div>
                <div class="grid-item">
                  <div class="grid-label">委比</div>
                  <div
                    class="grid-value"
                    :style="{ color: quoteData.weibi > 0 ? 'var(--accent-danger)' : 'var(--accent-success)' }"
                  >
                    {{ quoteData.weibi > 0 ? '+' : '' }}{{ (quoteData.weibi || 0).toFixed(2) }}%
                  </div>
                </div>
                <div class="grid-item">
                  <div class="grid-label">均价</div>
                  <div class="grid-value">{{ (quoteData.avgPrice || 0).toFixed(2) }}</div>
                </div>
                <div class="grid-item" v-if="quoteData.circulateMarketCap > 0">
                  <div class="grid-label">流通市值</div>
                  <div class="grid-value">{{ formatNumber(quoteData.circulateMarketCap * 10000) }}</div>
                </div>
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
            <div class="score-card" style="margin-bottom: 0; border-left: none">
              <div class="score-header">
                <div class="score-main">
                  <div class="score-big" :style="{ color: scoreBarColor }">{{ scoreResult.score }}</div>
                  <div class="score-unit">
                    <div class="score-unit-text">分</div>
                    <div
                      class="score-tag"
                      :style="{
                        color: scoreBarColor,
                        background: `color-mix(in srgb, ${scoreBarColor} 10%, transparent)`,
                        borderColor: `color-mix(in srgb, ${scoreBarColor} 33%, transparent)`,
                      }"
                    >
                      {{ scoreLabel }}
                    </div>
                  </div>
                </div>
                <div
                  class="score-summary"
                  :style="{
                    color: rating.color,
                    background: `color-mix(in srgb, ${rating.color} 10%, transparent)`,
                    borderColor: `color-mix(in srgb, ${rating.color} 33%, transparent)`,
                  }"
                >
                  {{ rating.label }}
                </div>
              </div>
              <div class="score-bar-track">
                <div class="score-bar-fill" :style="{ width: scoreResult.score + '%', background: scoreBarColor }">
                  <span class="score-bar-text">{{ scoreResult.score }}%</span>
                </div>
              </div>
              <div class="score-details">
                <div v-for="d in scoreResult.details" :key="d.label" class="score-detail-item">
                  <span>{{ d.label }}</span>
                  <span
                    :style="{
                      color:
                        d.score >= d.max * 0.6
                          ? 'var(--accent-danger)'
                          : d.score >= d.max * 0.4
                            ? 'var(--accent-warning)'
                            : 'var(--accent-success)',
                    }"
                    >{{ d.score }}/{{ d.max }}</span
                  >
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- 交易决策卡（止损 / 买卖点 / 仓位建议） -->
        <div v-if="showDecisionCard" class="decision-card">
          <div class="decision-card-header">
            <span class="decision-card-title">🎯 交易决策参考</span>
            <span v-if="buySellPoints?.stockType" class="decision-type-badge" :class="stockRoleClass">{{
              buySellPoints.stockType
            }}</span>
            <span
              v-if="riskControl?.risk_level"
              class="decision-risk-tag"
              :style="{
                color: riskLevelMeta.color,
                background: `color-mix(in srgb, ${riskLevelMeta.color} 12%, transparent)`,
                borderColor: `color-mix(in srgb, ${riskLevelMeta.color} 33%, transparent)`,
              }"
              >{{ riskLevelMeta.text }}</span
            >
          </div>
          <div class="decision-grid">
            <!-- 买入参考价 -->
            <div class="decision-item">
              <div class="decision-label">买入参考</div>
              <div class="decision-value">{{ buySellPoints?.buy || '--' }}</div>
            </div>
            <!-- 目标卖点价 + 预期涨幅 -->
            <div class="decision-item">
              <div class="decision-label">目标卖点</div>
              <div class="decision-value">{{ buySellPoints?.sell || '--' }}</div>
              <div v-if="expectedGainPct !== null" class="decision-sub gain">
                预期 +{{ expectedGainPct.toFixed(2) }}%
              </div>
            </div>
            <!-- 止损价 + 止损幅度 -->
            <div class="decision-item">
              <div class="decision-label">止损价</div>
              <div class="decision-value loss">{{ riskControl?.stop_loss || '--' }}</div>
              <div v-if="stopLossPctText" class="decision-sub loss">幅度 {{ stopLossPctText }}</div>
            </div>
            <!-- 仓位建议 -->
            <div class="decision-item">
              <div class="decision-label">仓位建议</div>
              <div class="decision-value position-text">{{ riskControl?.position_suggest || '--' }}</div>
            </div>
          </div>
          <div v-if="buySellPoints?.expectationNote" class="decision-note expectation-note">
            {{ buySellPoints.expectationNote }}
          </div>
          <div v-if="riskControl?.risk_note" class="decision-note">{{ riskControl.risk_note }}</div>
        </div>

        <!-- 内外盘 -->
        <div
          v-if="quoteData.outerPlateRaw > 0 || quoteData.innerPlateRaw > 0"
          :class="['collapsible-section', { collapsed: collapsed.plate }]"
        >
          <div class="collapsible-header" @click="collapsed.plate = !collapsed.plate">
            <span class="collapsible-title">📊 内外盘数据</span>
            <span class="collapsible-arrow">▼</span>
          </div>
          <div class="collapsible-content">
            <div class="orderbook-card" style="margin-bottom: 0; border-left: none">
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
                <span
                  >差值：<strong :class="quoteData.outerPlateRaw > quoteData.innerPlateRaw ? 'buy' : 'sell'">{{
                    (quoteData.outerPlateRaw - quoteData.innerPlateRaw).toLocaleString()
                  }}</strong></span
                >
                <span
                  >外盘占比：<strong class="buy">{{ quoteData.outerRatio }}%</strong></span
                >
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
            <div :class="['analysis-card', analysis.type]" style="margin-bottom: 0">
              <div class="analysis-desc">
                <div style="margin-bottom: 6px; font-weight: 700">{{ analysis.desc }}</div>
                <div v-if="analysis.reason" style="font-size: 0.75rem; color: var(--text-3); margin-bottom: 4px">
                  {{ analysis.reason }}
                </div>
                <div v-if="analysis.position" style="font-size: 0.75rem; color: var(--text-3); margin-bottom: 4px">
                  位置：{{ analysis.position }}
                </div>
                <div v-if="analysis.volumeDesc" style="font-size: 0.75rem; color: var(--text-3); margin-bottom: 4px">
                  量能：{{ analysis.volumeDesc }}
                </div>
                <div
                  style="
                    font-size: 0.8rem;
                    color: var(--text-2);
                    margin-top: 8px;
                    padding: 8px 10px;
                    background: var(--bg-muted);
                    border-radius: 8px;
                  "
                >
                  {{ analysis.action }}
                </div>
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
            <div class="analysis-card hold" style="margin-bottom: 0">
              <div class="analysis-desc">{{ analysis.reason }}</div>
            </div>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup>
  import { ref, computed, onMounted, inject } from 'vue'
  import {
    apiFetch,
    isTradingTime,
    getNextRefreshDelay,
    getTradingStatusText,
    formatNowTime,
    formatNumber,
  } from '../../utils/helpers'
  import { calculateScore, getEnhancedAnalysis, getRating } from '../../utils/score'
  import { useStockData } from '../../composables/useStockData'
  import { usePolling } from '../../composables/usePolling'

  // 轮询治理（2026-10-13）：Tab 隐藏时不再每 2s 打行情，只 gate 定时器，组件永不销毁
  const props = defineProps({ active: { type: Boolean, default: true } })
  const emit = defineEmits(['open-kline', 'open-ai-diag'])

  // 自重排的行情轮询：交易时段 2s，休市时按下一个开盘点长睡；隐藏即停
  const { start: startQuotePolling } = usePolling(
    () => (currentSymbol.value ? fetchStockQuote(currentSymbol.value, true) : Promise.resolve()),
    {
      interval: () => (isTradingTime() ? 2000 : getNextRefreshDelay()),
      active: () => props.active,
      immediate: false,
    }
  )

  const showNotification = inject('showNotification')

  const { quickStocks, loadQuickStocks, stockNameMap, loadStockMap } = useStockData()

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

  const scoreResult = computed(() =>
    quoteData.value ? calculateScore(quoteData.value) : { score: 0, label: '', details: [] }
  )
  const rating = computed(() => getRating(scoreResult.value.score))
  const scoreBarColor = computed(() => rating.value.color)
  const scoreLabel = computed(() => rating.value.short)
  const analysis = computed(() => (quoteData.value ? getEnhancedAnalysis(quoteData.value) : null))
  const tradingStatusText = computed(() => getTradingStatusText())

  // ===== 交易决策卡（风控止损 + 买卖点） =====
  const buySellPoints = computed(() => quoteData.value?.buySellPoints || null)
  const riskControl = computed(() => quoteData.value?.riskControl || null)
  const showDecisionCard = computed(() => !!buySellPoints.value || !!riskControl.value)
  // 角色 CSS 类（7级分类：总龙头/板块龙头/中军/情绪先锋/补涨龙/跟风/杂毛）
  const stockRoleClass = computed(() => {
    const t = buySellPoints.value?.stockType || ''
    if (t.includes('总龙头')) return 'role-total-leader'
    if (t.includes('板块龙头')) return 'role-sector-leader'
    if (t.includes('中军')) return 'role-core-trend'
    if (t.includes('情绪先锋')) return 'role-vanguard'
    if (t.includes('补涨龙')) return 'role-catch-up'
    if (t.includes('跟风')) return 'role-follower'
    if (t.includes('杂毛')) return 'role-marginal'
    return 'role-marginal'
  })
  // 预期涨幅 = (目标卖点 - 当前价) / 当前价 * 100
  const expectedGainPct = computed(() => {
    const bsp = buySellPoints.value
    if (!bsp || !bsp.sell) return null
    const sellPrice = parseFloat(bsp.sell)
    const curPrice = quoteData.value?.priceRaw
    if (isNaN(sellPrice) || !curPrice || curPrice <= 0) return null
    return ((sellPrice - curPrice) / curPrice) * 100
  })
  // 风险等级 → 文本 + 颜色（high=红 / mid=橙 / low=绿）
  const riskLevelMeta = computed(() => {
    const level = riskControl.value?.risk_level
    const map = {
      high: { text: '高风险', color: 'var(--accent-danger)' },
      mid: { text: '中风险', color: 'var(--accent-warning)' },
      low: { text: '低风险', color: 'var(--accent-success)' },
    }
    return map[level] || { text: level || '--', color: 'var(--text-tertiary)' }
  })
  // 止损幅度文本（兼容数字 / 字符串）
  const stopLossPctText = computed(() => {
    const pct = riskControl.value?.stop_loss_pct
    if (pct === null || pct === undefined || pct === '') return ''
    if (typeof pct === 'number') return '-' + Math.abs(pct).toFixed(2) + '%'
    const s = String(pct)
    return s.includes('%') ? s : '-' + s + '%'
  })

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
            const changeVal = parseFloat(fields[31]) || price - yesterdayClose
            const changePct = parseFloat(fields[32]) || (yesterdayClose > 0 ? (changeVal / yesterdayClose) * 100 : 0)
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
            const weibi = bid1Vol + ask1Vol > 0 ? ((bid1Vol - ask1Vol) / (bid1Vol + ask1Vol)) * 100 : 0
            const avgPrice = volumeRaw > 0 ? (turnoverRaw * 10000) / (volumeRaw * 100) : price
            const avgPriceDeviation = avgPrice > 0 ? ((price - avgPrice) / avgPrice) * 100 : 0
            const outerRatio =
              outerPlateRaw + innerPlateRaw > 0 ? (outerPlateRaw / (outerPlateRaw + innerPlateRaw)) * 100 : 50

            return {
              name: fields[1],
              symbol,
              price: fields[3],
              priceRaw: price,
              change: changeVal,
              changePercent: changePct,
              isUp: changeVal > 0,
              open: fields[5],
              high: fields[33],
              low: fields[34],
              volume: fields[36],
              volumeRaw,
              turnover: turnoverRaw,
              turnoverRate,
              pe,
              amplitude,
              volumeRatio,
              highLimit,
              lowLimit,
              circulateMarketCap,
              totalMarketCap,
              bid1Vol: fields[12],
              ask1Vol: fields[22],
              outerPlate: fields[7],
              innerPlate: fields[8],
              outerPlateRaw,
              innerPlateRaw,
              weibi: parseFloat(weibi.toFixed(2)),
              avgPrice: parseFloat(avgPrice.toFixed(2)),
              avgPriceDeviation: parseFloat(avgPriceDeviation.toFixed(2)),
              outerRatio: parseFloat(outerRatio.toFixed(1)),
            }
          }
        }
      }
    } catch (e) {
      console.error('解析数据失败:', e)
    }
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
      // 2026-07-08 性能优化：合并行情+评分为单一接口，从 2 个并行请求 → 1 个请求
      const resp = await fetch(`/api/stock/${formatted}/full`).then(r => r.json())
      if (!resp || !resp.success) {
        throw new Error(resp?.detail || '数据获取失败')
      }
      const { raw, score, label, predictDirection, details, riskControl, buySellPoints } = resp.data
      const parsed = parseTencentData(raw)
      // 注入后端 score（后端 calc_stock_score_v2 是权威评分源）
      if (parsed) {
        parsed.score = score
        parsed.scoreDetails = details || []
        parsed.label = label
        // 风控止损 + 买卖点（散户最关心的止损位 / 预期涨幅 / 买入参考价）
        parsed.riskControl = riskControl || null
        parsed.buySellPoints = buySellPoints || null
      }
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
    if (!input) {
      showNotification('请输入股票代码或名称')
      return
    }
    // 名称查找（本地映射）
    const map = stockNameMap.value
    if (map[input]) {
      currentSymbol.value = map[input]
    } else if (/^(sh|sz|bj)?\d{6}$/i.test(input)) {
      // 纯6位或带前缀代码：自动补交易所前缀（6→sh，0/3→sz，4/8→bj）
      const pure = input.toLowerCase().replace(/^(sh|sz|bj)/, '')
      if (pure.startsWith('6')) currentSymbol.value = 'sh' + pure
      else if (pure.startsWith('0') || pure.startsWith('3')) currentSymbol.value = 'sz' + pure
      else currentSymbol.value = 'bj' + pure
    } else if (/^(sh|sz|bj)\d{6}$/i.test(input)) {
      currentSymbol.value = input.toLowerCase()
    } else {
      // 名称模糊查询（2026-10-12）：走后端搜索接口解析为代码
      // （本地 stock_basic_info + 腾讯 smartbox 远程回退，本地查不到也能搜）
      try {
        const r = await apiFetch(`/api/stock-search?q=${encodeURIComponent(input)}`)
        if (r.success && r.data && r.data.length > 0) {
          currentSymbol.value = r.data[0].symbol
          showNotification(`已匹配：${r.data[0].name}（${r.data[0].symbol.toUpperCase()}）`)
        } else {
          showNotification(`未找到「${input}」对应的股票，请换个名称或直接用6位代码`, 'warning')
          return
        }
      } catch (e) {
        showNotification('名称查询失败，请改用代码', 'error')
        return
      }
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
    startQuotePolling()
  }

  async function startAiDiagnose(symbol) {
    emit('open-ai-diag', symbol)
  }

  async function handleAddToHot(code, name) {
    try {
      const result = await apiFetch('/api/hot-stocks/add', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code, name }),
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
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code }),
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
    } catch (e) {
      /* silent */
    }
  }

  onMounted(async () => {
    await Promise.all([loadStockMap(), loadQuickStocks()])
    // 默认加载热门股票快捷查询中的第一只股票
    if (quickStocks.value && quickStocks.value.length > 0) {
      const first = quickStocks.value[0]
      const sym = first.code || first.symbol
      if (sym) {
        currentSymbol.value = sym
        stockInput.value = sym
      }
    }
    fetchStockQuote(currentSymbol.value)
    startQuotePolling()
    checkMarketPredict()
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

<style scoped>
  /* ===== 交易决策卡 ===== */
  .decision-card {
    background: linear-gradient(135deg, var(--bg-secondary) 0%, var(--bg-tertiary) 100%);
    border-radius: var(--radius-lg);
    padding: var(--spacing-lg);
    margin-bottom: var(--spacing-lg);
    border: 1px solid var(--border-subtle);
    border-left: 4px solid var(--accent-info);
    box-shadow: var(--shadow-soft);
  }

  .decision-card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: var(--spacing-md);
    gap: 8px;
    flex-wrap: wrap;
  }

  .decision-card-title {
    font-size: 1rem;
    font-weight: 700;
    color: var(--text-primary);
  }

  .decision-type-badge {
    display: inline-block;
    padding: 3px 10px;
    border-radius: var(--radius-pill);
    font-size: 0.8125rem;
    font-weight: 700;
    white-space: nowrap;
    margin-right: auto;
  }
  /* 7级角色配色（龙头系暖橙、中军蓝、先锋紫、补涨青、跟风灰、杂毛暗灰） */
  .decision-type-badge.role-total-leader {
    color: #b91c1c;
    background: #fef2f2;
    border: 1px solid #fecaca;
  }
  .decision-type-badge.role-sector-leader {
    color: #c2410c;
    background: #fff7ed;
    border: 1px solid #fed7aa;
  }
  .decision-type-badge.role-core-trend {
    color: #1d4ed8;
    background: #eff6ff;
    border: 1px solid #bfdbfe;
  }
  .decision-type-badge.role-vanguard {
    color: #7e22ce;
    background: #faf5ff;
    border: 1px solid #e9d5ff;
  }
  .decision-type-badge.role-catch-up {
    color: #0e7490;
    background: #ecfeff;
    border: 1px solid #a5f3fc;
  }
  .decision-type-badge.role-follower {
    color: #475569;
    background: #f1f5f9;
    border: 1px solid #e2e8f0;
  }
  .decision-type-badge.role-marginal {
    color: #78716c;
    background: #f5f5f4;
    border: 1px solid #d6d3d1;
  }
  /* 暗色模式 */
  :root[data-theme='dark'] .decision-type-badge.role-total-leader {
    color: #fca5a5;
    background: rgba(185, 28, 28, 0.15);
    border-color: rgba(254, 202, 202, 0.3);
  }
  :root[data-theme='dark'] .decision-type-badge.role-sector-leader {
    color: #fdba74;
    background: rgba(194, 65, 12, 0.15);
    border-color: rgba(254, 215, 170, 0.3);
  }
  :root[data-theme='dark'] .decision-type-badge.role-core-trend {
    color: #93c5fd;
    background: rgba(29, 78, 216, 0.15);
    border-color: rgba(191, 219, 254, 0.3);
  }
  :root[data-theme='dark'] .decision-type-badge.role-vanguard {
    color: #d8b4fe;
    background: rgba(126, 34, 206, 0.15);
    border-color: rgba(233, 213, 255, 0.3);
  }
  :root[data-theme='dark'] .decision-type-badge.role-catch-up {
    color: #67e8f9;
    background: rgba(14, 116, 144, 0.15);
    border-color: rgba(165, 243, 252, 0.3);
  }
  :root[data-theme='dark'] .decision-type-badge.role-follower {
    color: #94a3b8;
    background: rgba(71, 85, 105, 0.15);
    border-color: rgba(226, 232, 240, 0.2);
  }
  :root[data-theme='dark'] .decision-type-badge.role-marginal {
    color: #a8a29e;
    background: rgba(120, 113, 108, 0.15);
    border-color: rgba(214, 211, 209, 0.2);
  }

  .decision-risk-tag {
    display: inline-block;
    padding: 3px 12px;
    border-radius: var(--radius-pill);
    font-size: 0.8125rem;
    font-weight: 700;
    border: 1px solid;
    white-space: nowrap;
  }

  .decision-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: var(--spacing-sm);
  }

  @media (max-width: 768px) {
    .decision-grid {
      grid-template-columns: repeat(2, 1fr);
    }
  }

  .decision-item {
    background: var(--bg-primary);
    border-radius: var(--radius-md);
    padding: var(--spacing-sm) var(--spacing-md);
    border: 1px solid var(--border-subtle);
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-height: 72px;
  }

  .decision-label {
    font-size: 0.75rem;
    color: var(--text-tertiary);
    font-weight: 500;
  }

  .decision-value {
    font-size: 1.125rem;
    font-weight: 700;
    color: var(--text-primary);
    line-height: 1.3;
  }

  .decision-value.loss {
    color: var(--accent-success);
  }

  .decision-value.position-text {
    font-size: 0.9375rem;
  }

  .decision-sub {
    font-size: 0.75rem;
    font-weight: 600;
    margin-top: 2px;
  }

  .decision-sub.gain {
    color: var(--accent-danger);
  }

  .decision-sub.loss {
    color: var(--accent-success);
  }

  .decision-note {
    margin-top: var(--spacing-sm);
    padding: var(--spacing-xs) var(--spacing-sm);
    font-size: 0.75rem;
    color: var(--text-secondary);
    background: var(--bg-primary);
    border-radius: var(--radius-sm);
    border: 1px solid var(--border-subtle);
  }

  .decision-note.expectation-note {
    color: #c2410c;
    font-weight: 600;
    border-color: #fed7aa;
    background: #fff7ed;
  }
  :root[data-theme='dark'] .decision-note.expectation-note {
    color: #fdba74;
    background: rgba(194, 65, 12, 0.1);
    border-color: rgba(254, 215, 170, 0.25);
  }
</style>
