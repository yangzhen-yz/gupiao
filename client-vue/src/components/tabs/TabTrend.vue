<template>
  <div>
    <div class="refresh-controls">
      <div class="toggle-section"><span class="toggle-label">📊 自动刷新中</span></div>
      <div class="last-update">{{ lastScan }}</div>
    </div>

    <!-- 市场温度计：每日趋势扫描数量历史 -->
    <div v-if="trendHistoryCounts.length > 0" class="thermometer-section">
      <!-- 顶部状态栏 -->
      <div class="thermometer-header" @click="showThermometer = !showThermometer">
        <div class="therm-left">
          <svg class="therm-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4v10.54a4 4 0 1 1-4 0V4a2 2 0 0 1 4 0Z"/></svg>
          <span class="therm-title">市场温度计</span>
        </div>
        <div class="therm-status-group" v-if="thermometerStatus">
          <span class="therm-status-pill" :class="'level-' + thermometerStatus.level">
            {{ thermometerStatus.label }}
          </span>
          <span class="therm-hint-text">{{ thermometerStatus.hint }}</span>
        </div>
        <div class="therm-trend-badge" :class="thermometerTrend?.direction" v-if="thermometerTrend">
          <span class="trend-dir">{{ thermometerTrend.direction === 'up' ? '↑' : thermometerTrend.direction === 'down' ? '↓' : '—' }}</span>
          <span>较上日 <strong>{{ thermometerTrend.diff > 0 ? '+' : '' }}{{ thermometerTrend.diff }}</strong> 只</span>
          <span class="trend-pct">({{ thermometerTrend.pct > 0 ? '+' : '' }}{{ thermometerTrend.pct.toFixed(0) }}%)</span>
        </div>
        <span class="therm-toggle">{{ showThermometer ? '收起' : '展开' }}
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M3 4.5L6 7.5L9 4.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" :style="{ transform: showThermometer ? 'rotate(180deg)' : '', transition: 'transform 0.2s' }"/></svg>
        </span>
      </div>

      <!-- 展开内容 -->
      <div v-show="showThermometer" class="therm-body">
        <!-- 迷你柱状图：当前页符合数趋势 -->
        <div class="therm-spark-row">
          <div class="spark-label">近{{ pagedTrendHistoryCounts.length }}个交易日</div>
          <div class="spark-bars">
            <div v-for="(item, idx) in pagedTrendHistoryCounts.slice().reverse()" :key="item.date"
                 class="spark-bar-wrap" :title="`${item.date}: ${item.result_count}只`">
              <div class="spark-bar" :class="getThermResultClass(item.result_count)"
                   :style="{ height: sparkBarHeight(item.result_count) + '%' }"></div>
            </div>
          </div>
        </div>

        <!-- 数据表格 -->
        <div class="therm-table-wrap">
          <div class="therm-table">
            <div class="therm-row therm-head">
              <span class="col-date">日期</span>
              <span class="col-num">扫描</span>
              <span class="col-result">符合</span>
              <span class="col-avg">均分</span>
              <span class="col-high">80+</span>
              <span class="col-delta">变化</span>
            </div>
            <div v-for="(item, idx) in pagedTrendHistoryCounts" :key="item.date"
                 class="therm-row" :class="{ 'row-latest': idx === 0 }">
              <span class="col-date" :class="{ 'date-latest': idx === 0 }">{{ item.date.slice(5) }}</span>
              <span class="col-num">{{ item.scanned }}</span>
              <span class="col-result" :class="getThermResultClass(item.result_count)">
                <strong>{{ item.result_count }}</strong>
              </span>
              <span class="col-avg">{{ item.avg_score }}</span>
              <span class="col-high">{{ item.count_80plus }}</span>
              <span class="col-delta">
                <template v-if="idx < pagedTrendHistoryCounts.length - 1">
                  <span class="delta-val" :class="getTrendArrowClass(item.result_count, pagedTrendHistoryCounts[idx + 1].result_count)">
                    {{ getTrendArrow(item.result_count, pagedTrendHistoryCounts[idx + 1].result_count) }}
                  </span>
                </template>
                <span v-else class="delta-flat">—</span>
              </span>
            </div>
          </div>
        </div>

        <!-- 分页：默认近21个交易日，超出翻页 -->
        <div v-if="thermTotalPages > 1" class="history-pagination" style="margin-top: 0.6rem;">
          <button class="page-btn" :disabled="thermPage <= 1" @click="thermPage--">上一页</button>
          <span class="page-info">第 {{ thermPage }} / {{ thermTotalPages }} 页</span>
          <button class="page-btn" :disabled="thermPage >= thermTotalPages" @click="thermPage++">下一页</button>
        </div>

        <!-- 图例 -->
        <div class="therm-legends">
          <span class="leg-item"><i class="leg-dot frozen"></i>&le;{{ THERMOMETER.FROZEN }} 冰点 · 减仓</span>
          <span class="leg-item"><i class="leg-dot cold"></i>{{ THERMOMETER.FROZEN+1 }}~{{ THERMOMETER.COLD }} 偏冷 · 谨慎</span>
          <span class="leg-item"><i class="leg-dot normal"></i>>{{ THERMOMETER.COLD }} 正常/活跃</span>
        </div>
      </div>
    </div>
    <div class="trend-section">
      <div class="section-header">
        <div class="section-subtitle-small">发现的趋势股票</div>
        <span class="section-stats">
          {{ filteredTrendStocks.length }} / {{ trendStocks.length }} 只
          <span v-if="trendStocks.length - filteredTrendStocks.length > 0" class="section-stats-hint">（已隐藏 {{ trendStocks.length - filteredTrendStocks.length }} 只在自选池中）</span>
        </span>
      </div>
      <div v-if="autoAddThreshold > 0" class="auto-add-hint">
        <span class="auto-add-dot"></span>
        自动加池阈值：≥{{ autoAddThreshold }}分
        <span v-if="lastAutoAdded.length > 0" class="auto-add-count">（本次已自动加入 {{ lastAutoAdded.length }} 只）</span>
      </div>
      <!-- P4-1: 昨日回测准确率徽章 -->
      <div v-if="latestBacktest" class="backtest-accuracy-badge">
        <span class="backtest-badge-label">🎯 昨日趋势回测</span>
        <span class="backtest-badge-stat">
          1日胜率 <strong :class="getWinRateClass(latestBacktest.winRate1d)">{{ (latestBacktest.winRate1d * 100).toFixed(0) }}%</strong>
          <span class="backtest-badge-detail">({{ latestBacktest.win1dCount }}/{{ latestBacktest.verified1dCount }})</span>
        </span>
        <span class="backtest-badge-stat" v-if="latestBacktest.verified3dCount > 0">
          · 3日 <strong :class="getWinRateClass(latestBacktest.winRate3d)">{{ (latestBacktest.winRate3d * 100).toFixed(0) }}%</strong>
        </span>
        <span class="backtest-badge-stat" v-if="latestBacktest.verified5dCount > 0">
          · 5日 <strong :class="getWinRateClass(latestBacktest.winRate5d)">{{ (latestBacktest.winRate5d * 100).toFixed(0) }}%</strong>
        </span>
        <span class="backtest-badge-date">{{ latestBacktest.predictDate }}</span>
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
              <!-- P4-1: 昨测结果标签 -->
              <span v-if="getStockBacktestTag(stock)" class="yesterday-backtest-tag" :class="getStockBacktestTag(stock).cls">
                {{ getStockBacktestTag(stock).text }}
              </span>
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
                    <span class="metric-icon">{{ fmtNum(stock.details.sD1) }}分</span>
                    <span class="metric-sub">{{ stock.details.ma20 ? 'MA20=' + fmtNum(stock.details.ma20) : '—' }}</span>
                  </span>
                </div>
                <div class="trend-metric-item" :title="metricTip('d2')">
                  <span class="trend-metric-label">资金异动</span>
                  <span class="trend-metric-value" :class="(stock.details.sD2 || 0) >= 20 ? 'metric-on' : (stock.details.sD2 || 0) >= 10 ? 'metric-warn' : 'metric-off'">
                    <span class="metric-icon">{{ fmtNum(stock.details.sD2) }}分</span>
                    <span class="metric-sub">{{ stock.details.limitUpCount20d || 0 }}板/{{ stock.details.consecutiveYangCount || 0 }}阳</span>
                  </span>
                </div>
                <div class="trend-metric-item" :title="metricTip('d3')">
                  <span class="trend-metric-label">量能配合</span>
                  <span class="trend-metric-value" :class="(stock.details.sD3 || 0) >= 15 ? 'metric-on' : (stock.details.sD3 || 0) >= 8 ? 'metric-warn' : 'metric-off'">
                    <span class="metric-icon">{{ fmtNum(stock.details.sD3) }}分</span>
                    <span class="metric-sub">{{ stock.details.volRatio ? '倍率=' + fmtNum(stock.details.volRatio) : '—' }}</span>
                  </span>
                </div>
                <div class="trend-metric-item" :title="metricTip('d4')">
                  <span class="trend-metric-label">风险回撤</span>
                  <span class="trend-metric-value" :class="(stock.details.sD4 || 0) >= 15 ? 'metric-on' : (stock.details.sD4 || 0) >= 10 ? 'metric-warn' : 'metric-off'">
                    <span class="metric-icon">{{ fmtNum(stock.details.sD4) }}分</span>
                    <span class="metric-sub">{{ fmtNum(stock.details.currentDrawdown) }}%</span>
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
                <div v-if="(stock.details?.earlySignals || []).length > 0" class="trend-early-signal-tags">
                  <span class="trend-early-signal-label">早期信号</span>
                  <span v-for="s in (stock.details?.earlySignals || [])" :key="s.name"
                        class="trend-early-signal-tag" :title="s.desc">
                    +{{ s.score }} {{ signalNameMap[s.name] || s.name }}
                  </span>
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
      <!-- P4-1: 回测历史区（可折叠） -->
      <div v-if="backtestHistory.length > 0" class="backtest-history-section">
        <div class="backtest-history-header" @click="toggleBacktestHistory">
          <span class="backtest-history-title">📈 趋势评分回测历史（{{ backtestHistory.length }} 期）</span>
          <span class="backtest-history-toggle">{{ showBacktestHistory ? '收起 ▲' : '展开 ▼' }}</span>
        </div>
        <div v-show="showBacktestHistory" class="backtest-history-content">
          <div v-for="report in backtestHistory" :key="report.predictDate" class="backtest-history-item">
            <div class="backtest-item-date">{{ report.predictDate }}</div>
            <div class="backtest-item-grid">
              <div class="backtest-item-cell">
                <div class="cell-label">1日胜率</div>
                <div class="cell-value" :class="getWinRateClass(report.winRate1d)">
                  {{ report.verified1dCount > 0 ? (report.winRate1d * 100).toFixed(0) + '%' : '—' }}
                </div>
                <div class="cell-detail">{{ report.win1dCount }}/{{ report.verified1dCount }}</div>
              </div>
              <div class="backtest-item-cell">
                <div class="cell-label">3日胜率</div>
                <div class="cell-value" :class="getWinRateClass(report.winRate3d)">
                  {{ report.verified3dCount > 0 ? (report.winRate3d * 100).toFixed(0) + '%' : '—' }}
                </div>
                <div class="cell-detail">{{ report.win3dCount }}/{{ report.verified3dCount }}</div>
              </div>
              <div class="backtest-item-cell">
                <div class="cell-label">5日胜率</div>
                <div class="cell-value" :class="getWinRateClass(report.winRate5d)">
                  {{ report.verified5dCount > 0 ? (report.winRate5d * 100).toFixed(0) + '%' : '—' }}
                </div>
                <div class="cell-detail">{{ report.win5dCount }}/{{ report.verified5dCount }}</div>
              </div>
            </div>
            <div class="backtest-item-segments" v-if="report.score80PlusTotal > 0 || report.score7080Total > 0 || report.score6070Total > 0">
              <span class="segment-tag seg-60-70">60-70分: {{ report.score6070Win1d }}/{{ report.score6070Total }}</span>
              <span class="segment-tag seg-70-80">70-80分: {{ report.score7080Win1d }}/{{ report.score7080Total }}</span>
              <span class="segment-tag seg-80-plus">80+分: {{ report.score80PlusWin1d }}/{{ report.score80PlusTotal }}</span>
            </div>
          </div>
        </div>
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
const autoAddThreshold = ref(80)
const lastAutoAdded = ref([])

// 趋势回测相关
const latestBacktest = ref(null)
const backtestHistory = ref([])
const showBacktestHistory = ref(false)
const backtestRecordsMap = ref({})  // { symbol: { isCorrect1d, isCorrect3d, isCorrect5d } }

// 市场温度计：每日趋势扫描数量历史
const trendHistoryCounts = ref([])
const showThermometer = ref(false)
const THERM_PAGE_SIZE = 21
const thermPage = ref(1)
const thermTotalPages = computed(() => Math.max(1, Math.ceil(trendHistoryCounts.value.length / THERM_PAGE_SIZE)))
const pagedTrendHistoryCounts = computed(() => {
  const start = (thermPage.value - 1) * THERM_PAGE_SIZE
  return trendHistoryCounts.value.slice(start, start + THERM_PAGE_SIZE)
})

// 温度等级判定
const THERMOMETER = {
  FROZEN: 8,    // ≤8只：冰点，强烈建议减仓
  COLD: 15,     // ≤15只：偏冷，谨慎
  NORMAL_LOW: 20, // ≤20只：正常偏低
}
const thermometerStatus = computed(() => {
  if (trendHistoryCounts.value.length === 0) return null
  const latest = trendHistoryCounts.value[0]
  const count = latest.result_count
  if (count <= THERMOMETER.FROZEN) return { level: 'frozen', label: '冰点', icon: '⚠️', hint: '市场极冷，建议减仓/观望', color: '#dc2626' }
  if (count <= THERMOMETER.COLD) return { level: 'cold', label: '偏冷', icon: '🥶', hint: '市场偏冷，控制仓位', color: '#f59e0b' }
  if (count <= THERMOMETER.NORMAL_LOW) return { level: 'normal-low', label: '温和', icon: '🟢', hint: '市场温和，正常操作', color: '#16a34a' }
  return { level: 'warm', label: '活跃', icon: '🔥', hint: '市场活跃，可适当加仓', color: '#059669' }
})

// 趋势变化（对比上一交易日）
const thermometerTrend = computed(() => {
  if (trendHistoryCounts.value.length < 2) return null
  const curr = trendHistoryCounts.value[0].result_count
  const prev = trendHistoryCounts.value[1].result_count
  const diff = curr - prev
  const pct = prev > 0 ? ((curr - prev) / prev * 100) : 0
  return { diff, pct, direction: diff > 0 ? 'up' : diff < 0 ? 'down' : 'flat' }
})

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

const TREND_PAGE_SIZE = 10
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
  d1: '维度1：短期均线趋势（满分30分=趋势方向20+乖离惩罚0~-10）。趋势：20=近10日全在MA20上+MA20连升；15=近7日仅1日跌破次日收回；8=现价站稳MA20。乖离惩罚：偏离MA20≥15%扣10分；≥10%扣6分；≥5%扣3分。',
  d2: '维度2：短期资金异动（满分30分）。涨停次数与连阳数分别计分取高者：≥3板/2连板/≥6阳=30分；2板/4~5阳=20分；1板/3阳=10分。',
  d3: '维度3：量能配合（满分20分=量比10+量能趋势5+量能稳定性5）。量比：20日/60日均量≥1.5→10分；趋势：量能斜率陡升→5分；稳定性：CV≤0.3→5分（越小越健康）。',
  d4: '维度4：风险回撤（满分20分）。当前距近10日高点回撤<8%=20分；8~12%=15分；12~15%=10分；15~20%=5分；≥20%=0分。已创新高者回撤≈0得满分。',
  position: '年度分位：现价在近250日收盘价中的百分位。≥80%视为高位（扣-20分），60~80%中高位（扣-10分）。',
  consecutive: '从最新一天往回数连续阳线天数，与评分无关，仅作展示。',
}
function metricTip(key) { return METRIC_TIPS[key] || '' }

// 早期信号中文映射（第5维度）
const signalNameMap = {
  nr7_inside_bar: 'NR7缩量',
  vcp: 'VCP收缩',
  rsi_bottom_div: 'RSI底背离',
  bottom_first_limit: '底部首板',
  consolidation_break: '缩量后首放',
}

// 成交量格式化（万/亿）
function formatVol(v) {
  if (!v && v !== 0) return '—'
  if (v >= 1e8) return (v / 1e8).toFixed(1) + '亿'
  if (v >= 1e4) return (v / 1e4).toFixed(1) + '万'
  return String(v)
}

// 数值格式化：最多保留两位小数，去掉浮点尾数（如 19.679199999999998 → 19.68）
function fmtNum(v) {
  if (v === null || v === undefined || isNaN(Number(v))) return 0
  return Number(Number(v).toFixed(2))
}

function getConsecutiveClass(stock) {
  const days = stock.details?.consecutiveUpDays || 0
  if (days >= 3) return 'hot'
  if (days >= 1) return 'warm'
  return 'cool'
}

function getScoreColor(stock) {
  const c = getRating(stock.score).color
  if (c === 'var(--accent-danger)') return 'var(--accent-danger)'
  if (c === 'var(--accent-warning)') return 'var(--accent-warning)'
  return 'var(--accent-success)'
}

function getScoreStyle(stock) {
  const c = getRating(stock.score).color
  if (c === 'var(--accent-danger)') return { background: 'var(--accent-danger-bg)', borderColor: 'var(--accent-danger-border)' }
  if (c === 'var(--accent-warning)') return { background: 'var(--accent-warning-bg)', borderColor: 'var(--accent-warning-border)' }
  return { background: 'var(--accent-success-bg)', borderColor: 'var(--accent-success-border)' }
}

async function loadTrendStocks() {
  try {
    const result = await apiFetch('/api/trend-stocks')
    if (result.success && result.data) {
      trendStocks.value = result.data.stocks || []
      if (typeof result.data.autoAddThreshold === 'number') {
        autoAddThreshold.value = result.data.autoAddThreshold
      }
      // 检查是否需要自动重新扫描
      if (!_autoRescanTriggered) {
        const today = new Date().toISOString().slice(0, 10)
        const scanDate = result.data.scanDate || ''
        // 条件1: 最新扫描日期不是今天 → 数据过期，自动重扫
        // 条件2: 旧数据无 consecutiveUpDays 字段 → 格式过旧，自动重扫
        const isStale = scanDate && scanDate !== today
        const isOldFormat = trendStocks.value.length > 0 &&
          trendStocks.value.every(s => !s.details || typeof s.details.consecutiveUpDays === 'undefined')
        if (isStale || isOldFormat) {
          _autoRescanTriggered = true
          scanTrendStocks().catch(() => { _autoRescanTriggered = false })
        }
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
      await loadPoolWithIndicators()
      if (typeof result.data.autoAddThreshold === 'number') {
        autoAddThreshold.value = result.data.autoAddThreshold
      }
      lastAutoAdded.value = result.data.autoAdded || []
      if (lastAutoAdded.value.length > 0) {
        showNotification(`已自动加入 ${lastAutoAdded.value.length} 只≥${autoAddThreshold.value}分股票到股票池`)
      }
      lastScan.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
      // 扫描完成后刷新温度计历史数据
      loadTrendHistoryCounts()
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

// ========== 趋势回测相关方法 ==========
async function loadLatestBacktest() {
  try {
    const result = await apiFetch('/api/trend/backtest-latest')
    if (result.success && result.data) {
      const d = result.data
      latestBacktest.value = {
        predictDate: d.predict_date,
        winRate1d: d.win_rate_1d || 0, win1dCount: d.win_1d_count || 0, verified1dCount: d.verified_1d_count || 0,
        winRate3d: d.win_rate_3d || 0, win3dCount: d.win_3d_count || 0, verified3dCount: d.verified_3d_count || 0,
        winRate5d: d.win_rate_5d || 0, win5dCount: d.win_5d_count || 0, verified5dCount: d.verified_5d_count || 0,
      }
    }
  } catch (e) { console.warn('加载最新回测失败', e) }
}

async function loadBacktestHistory() {
  try {
    const result = await apiFetch('/api/trend/backtest-history?limit=30')
    if (result.success && result.data) {
      backtestHistory.value = result.data.map(d => ({
        predictDate: d.predict_date,
        winRate1d: d.win_rate_1d || 0, win1dCount: d.win_1d_count || 0, verified1dCount: d.verified_1d_count || 0,
        winRate3d: d.win_rate_3d || 0, win3dCount: d.win_3d_count || 0, verified3dCount: d.verified_3d_count || 0,
        winRate5d: d.win_rate_5d || 0, win5dCount: d.win_5d_count || 0, verified5dCount: d.verified_5d_count || 0,
        score6070Total: d.score_60_70_total || 0, score6070Win1d: d.score_60_70_win_1d || 0,
        score7080Total: d.score_70_80_total || 0, score7080Win1d: d.score_70_80_win_1d || 0,
        score80PlusTotal: d.score_80_plus_total || 0, score80PlusWin1d: d.score_80_plus_win_1d || 0,
      }))
    }
  } catch (e) { console.warn('加载回测历史失败', e) }
}

async function loadYesterdayBacktestRecords() {
  try {
    // 性能优化（2026-07-02）：3 次串行 await 改 Promise.all 并发，3 RTT → 1 RTT
    const dates = [1, 2, 3].map(i => {
      const d = new Date(Date.now() - i * 86400000)
      return d.toISOString().slice(0, 10)
    })
    const results = await Promise.all(
      dates.map(dateStr => apiFetch(`/api/trend/backtest-records?predict_date=${dateStr}&limit=200`))
    )
    const records = {}
    results.forEach(result => {
      if (result.success && result.data) {
        result.data.forEach(r => {
          if (r.verified_1d === 1) {
            records[r.symbol] = { isCorrect1d: r.is_correct_1d }
          }
        })
      }
    })
    backtestRecordsMap.value = records
  } catch (e) { console.warn('加载昨测明细失败', e) }
}

function toggleBacktestHistory() {
  showBacktestHistory.value = !showBacktestHistory.value
}

// ========== 市场温度计 ==========
async function loadTrendHistoryCounts() {
  try {
    const result = await apiFetch('/api/trend/history-counts?days=90')
    if (result.success && result.data) {
      trendHistoryCounts.value = result.data
      thermPage.value = 1
    }
  } catch (e) { console.warn('加载趋势历史统计失败', e) }
}

function getWinRateClass(rate) {
  if (rate >= 0.6) return 'win-rate-high'
  if (rate >= 0.5) return 'win-rate-mid'
  return 'win-rate-low'
}

function getStockBacktestTag(stock) {
  const sym = (stock.symbol || stock.code || '').toLowerCase()
  const rec = backtestRecordsMap.value[sym]
  if (!rec || rec.isCorrect1d === undefined || rec.isCorrect1d === null) return null
  return rec.isCorrect1d === 1
    ? { text: '昨测✓', cls: 'tag-correct' }
    : { text: '昨测✗', cls: 'tag-wrong' }
}

// 温度计辅助
function getThermResultClass(count) {
  if (count <= THERMOMETER.FROZEN) return 'result-frozen'
  if (count <= THERMOMETER.COLD) return 'result-cold'
  return 'result-normal'
}
function getTrendArrow(curr, prev) {
  const d = curr - prev
  if (d > 0) return `↑${d}`
  if (d < 0) return `↓${Math.abs(d)}`
  return '—'
}
function getTrendArrowClass(curr, prev) {
  const d = curr - prev
  if (d > 3) return 'up-strong'
  if (d > 0) return 'up'
  if (d < -3) return 'down-strong'
  if (d < 0) return 'down'
  return 'flat'
}
// 迷你柱状图高度（百分比，基于当前页）
const sparkMaxCount = computed(() => {
  if (!pagedTrendHistoryCounts.value.length) return 40
  return Math.max(40, ...pagedTrendHistoryCounts.value.map(i => i.result_count))
})
function sparkBarHeight(count) {
  const max = sparkMaxCount.value
  if (max <= 0) return 8
  return Math.max(8, (count / max) * 100)
}

onMounted(async () => {
  // 性能优化（2026-07-02）：三个串行 await 改 Promise.all，首屏 = max(三者) 而非 sum
  await Promise.all([loadStockMap(), loadPoolWithIndicators(), loadTrendStocks()])
  scheduleTrendRefresh()
  // P4-1: 加载回测数据（非阻塞，不依赖趋势数据）
  loadLatestBacktest()
  loadBacktestHistory()
  loadYesterdayBacktestRecords()
  loadTrendHistoryCounts()
})

onUnmounted(() => {
  if (trendTimer) clearTimeout(trendTimer)
})
</script>

<style scoped>
/* ========== 市场温度计（Redesigned 2026-07-08） ========== */
.thermometer-section {
  background: #fff;
  border: 0.5px solid #e2e8f0;
  border-radius: 10px;
  margin-bottom: 1rem;
  overflow: hidden;
}

/* ---- 头部状态栏 ---- */
.thermometer-header {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  padding: 0.75rem 1.1rem;
  cursor: pointer;
  user-select: none;
  transition: background 0.15s ease;
  flex-wrap: wrap;
}
.thermometer-header:hover { background: #fafbfc; }

.therm-left {
  display: flex;
  align-items: center;
  gap: 0.4rem;
}
.therm-icon {
  width: 18px; height: 18px;
  color: #64748b;
  flex-shrink: 0;
}
.therm-title {
  font-size: 0.95rem;
  font-weight: 600;
  color: #334155;
  letter-spacing: -0.01em;
}

.therm-status-group {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
}
.therm-status-pill {
  display: inline-flex;
  align-items: center;
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0.02em;
  padding: 2px 8px;
  border-radius: 4px;
  line-height: 1.6;
  text-transform: uppercase;
}
.therm-status-pill.level-frozen  { background: #fef2f2; color: #dc2626; }
.therm-status-pill.level-cold    { background: #fffbeb; color: #d97706; }
.therm-status-pill.level-normal-low { background: #f0fdf4; color: #16a34a; }
.therm-status-pill.level-warm    { background: #ecfdf5; color: #059669; }
.therm-hint-text {
  font-size: 0.8rem;
  color: #94a3b8;
}

.therm-trend-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 0.82rem;
  color: #64748b;
  padding: 2px 7px;
  border-radius: 4px;
  background: #f8fafc;
  margin-left: auto;
}
.therm-trend-badge.up     { color: #16a34a; }
.therm-trend-badge.down   { color: #dc2626; }
.therm-trend-badge.flat   { color: #94a3b8; }
.trend-dir {
  font-weight: 700;
  font-size: 0.9rem;
}
.therm-trend-badge strong {
  font-weight: 700;
}
.trend-pct {
  font-size: 0.73rem;
  color: #94a3b8;
  margin-left: 1px;
}

.therm-toggle {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 0.76rem;
  color: #94a3b8;
  margin-left: 0.25rem;
  transition: color 0.15s;
}
.thermometer-header:hover .therm-toggle { color: #64748b; }

/* ---- 展开内容 ---- */
.therm-body {
  padding: 0 1.1rem 0.9rem;
  border-top: 0.5px solid #eef2f6;
}

/* 迷你柱状图 */
.therm-spark-row {
  display: flex;
  align-items: flex-end;
  gap: 1rem;
  padding: 0.85rem 0 0.55rem;
  border-bottom: 0.5px solid #f1f5f9;
}
.spark-label {
  font-size: 0.76rem;
  color: #94a3b8;
  white-space: nowrap;
  padding-bottom: 2px;
}
.spark-bars {
  display: flex;
  align-items: flex-end;
  gap: 2px;
  flex: 1;
  height: 40px;
}
.spark-bar-wrap {
  flex: 1;
  height: 100%;
  display: flex;
  align-items: flex-end;
  min-width: 0;
}
.spark-bar {
  width: 100%;
  border-radius: 2px 2px 0 0;
  min-height: 3px;
  transition: opacity 0.15s;
  opacity: 0.75;
}
.spark-bar-wrap:hover .spark-bar { opacity: 1; }
.spark-bar.result-frozen  { background: #fecaca; }
.spark-bar.result-cold    { background: #fde68a; }
.spark-bar.result-normal { background: #bbf7d0; }

/* ---- 数据表格 ---- */
.therm-table-wrap {
  margin-top: 0.5rem;
}
.therm-table {
  width: 100%;
}
.therm-row {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  align-items: center;
  justify-items: center;
  padding: 0.42rem 0.6rem;
  font-size: 0.88rem;
  transition: background 0.12s;
  border-bottom: 0.5px solid #f4f6f8;
}
.therm-row:last-child { border-bottom: none; }
.therm-row:hover:not(.therm-head) { background: #fafbfc; }

/* 表头 */
.therm-head {
  font-weight: 500;
  color: #94a3b8;
  font-size: 0.78rem;
  background: #fbfcfd;
  border-bottom: 0.5px solid #eef2f6;
  letter-spacing: 0.02em;
}
.therm-head:hover { background: #fbfcfd; }

/* 最新行高亮 */
.row-latest {
  position: relative;
  background: linear-gradient(90deg, #fefce8 0%, rgba(254,252,232,0) 60%);
  font-weight: 500;
}
.row-latest::before {
  content: '';
  position: absolute;
  left: 0; top: 0; bottom: 0;
  width: 3px;
  background: #f59e0b;
  border-radius: 0 2px 2px 0;
}

/* 列 */
.col-date { color: #64748b; font-variant-numeric: tabular-nums; justify-self: start; padding-left: 0.3rem; }
.col-date.date-latest { color: #d97706; font-weight: 700; }
.col-num { color: #94a3b8; font-variant-numeric: tabular-nums; font-size: 0.84rem; }
.col-result { font-weight: 700; font-family: 'SF Mono', Consolas, 'Cascadia Code', monospace; font-variant-numeric: tabular-nums; }
.col-result.result-frozen { color: #dc2626; }
.col-result.result-cold { color: #d97706; }
.col-result.result-normal { color: #16a34a; }
.col-avg { color: #94a3b8; font-variant-numeric: tabular-nums; font-size: 0.86rem; }
.col-high { color: #94a3b8; font-variant-numeric: tabular-nums; font-size: 0.84rem; }
.col-delta { }

/* 变化箭头 */
.delta-val {
  font-size: 0.82rem;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  padding: 1px 5px;
  border-radius: 3px;
}
.delta-val.up-strong { color: #166534; background: #f0fdf4; }
.delta-val.up        { color: #15803d; }
.delta-val.down-strong { color: #991b1b; background: #fef2f2; }
.delta-val.down      { color: #c2410c; }
.delta-flat          { color: #cbd5e1; font-size: 0.78rem; }

/* 图例 */
.therm-legends {
  display: flex;
  gap: 1.1rem;
  margin-top: 0.55rem;
  padding-top: 0.5rem;
  border-top: 0.5px dashed #eef2f6;
  font-size: 0.74rem;
  color: #94a3b8;
  flex-wrap: wrap;
}
.leg-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.leg-dot {
  width: 7px; height: 7px;
  border-radius: 2px;
  display: inline-block;
  flex-shrink: 0;
}
.leg-dot.frozen  { background: #fca5a5; }
.leg-dot.cold    { background: #fcd34d; }
.leg-dot.normal  { background: #86efac; }

/* ========== 早期信号 chips（橙色系，呼应"早期/未确认"语义） ========== */
.trend-early-signal-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
  align-items: center;
}
.trend-early-signal-label {
  font-size: 0.72rem;
  color: #ea580c;
  font-weight: 600;
  margin-right: 2px;
}
.trend-early-signal-tag {
  display: inline-flex;
  align-items: center;
  font-size: 0.72rem;
  font-weight: 600;
  padding: 1px 7px;
  border-radius: 4px;
  background: #fff7ed;
  color: #c2410c;
  border: 0.5px solid #fed7aa;
  cursor: help;
}
</style>
