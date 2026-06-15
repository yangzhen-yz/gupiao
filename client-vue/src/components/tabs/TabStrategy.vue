<template>
  <div>
    <div class="section-title">策略优化</div>
    <div class="section-subtitle">自适应选股策略 · 每日回测验证 · 动态权重调整 · 误判分析</div>

    <!-- 大盘预测/崩盘预警 -->
    <div class="market-predict-section">
      <div class="market-predict-section-title" @click="marketPredictExpanded = !marketPredictExpanded">
        <span>大盘预测与崩盘预警</span>
        <span class="collapse-arrow" :style="{ transform: marketPredictExpanded ? 'rotate(180deg)' : '' }">▼</span>
      </div>
      <div class="market-predict-section-content" :style="{ display: marketPredictExpanded ? 'block' : 'none' }">
        <div style="display:flex;gap:8px;margin-bottom:10px;">
          <button class="btn-secondary" style="padding:4px 12px;font-size:0.76rem;" @click="loadMarketPredict">刷新</button>
        </div>
        <!-- 预测结果 -->
        <div v-if="marketPredict" class="strategy-card" style="margin-bottom:10px;" :style="{ borderLeft: `3px solid ${predictBorderColor}` }">
          <div class="strategy-card-header">
            <span class="strategy-card-title">{{ marketPredict.prediction?.title || '加载中...' }}</span>
            <span v-if="marketPredict.riskScore !== undefined" :style="{ color: predictBorderColor, fontWeight: 700 }">风险评分 {{ marketPredict.riskScore }}/100</span>
          </div>
          <div style="font-size:0.85rem;color:var(--text-secondary);margin-bottom:8px;">{{ marketPredict.prediction?.message }}</div>
          <div class="strategy-action-box" :style="{ background: predictBgColor, color: predictTextColor, padding: '8px 12px', borderRadius: 'var(--radius-sm)', fontSize: '0.85rem' }">
            建议: {{ marketPredict.prediction?.action }}
          </div>
          <!-- 四维信号 -->
          <div v-if="marketPredict.signals" class="market-predict-signals">
            <div v-for="(sig, key) in marketPredict.signals" :key="key" class="predict-signal-item">
              <div class="predict-signal-header">
                <span class="predict-signal-name">{{ sig.name }}</span>
                <span class="predict-signal-value" :style="{ color: sig.status === 'danger' ? 'var(--accent-danger)' : sig.status === 'warning' ? 'var(--accent-warning)' : 'var(--accent-success)' }">{{ sig.value }}</span>
              </div>
              <div class="predict-signal-bar">
                <div class="predict-signal-fill" :class="sig.status === 'danger' ? 'danger' : sig.status === 'warning' ? 'warning' : 'safe'" :style="{ width: (sig.score / sig.maxScore * 100) + '%' }"></div>
              </div>
              <div class="predict-signal-detail">{{ sig.detail }}</div>
            </div>
          </div>
          <div style="font-size:0.75rem;color:var(--text-tertiary);margin-top:8px;">更新时间: {{ marketPredict.updateTime }}</div>
        </div>
        <div v-else class="empty-state" style="padding:1.5rem;">
          <div class="loading-spinner" style="margin:0 auto 0.5rem;" v-if="marketPredictLoading"></div>
          <span>{{ marketPredictLoading ? '正在分析...' : '点击刷新加载大盘预测' }}</span>
        </div>

        <!-- 崩盘预警 -->
        <div v-if="marketCrash" class="strategy-card" style="margin-bottom:10px;" :style="{ borderLeft: `3px solid ${crashBorderColor}` }">
          <div class="strategy-card-header">
            <span class="strategy-card-title">{{ marketCrash.suggestion?.title || '崩盘检测' }}</span>
            <span :style="{ color: crashBorderColor, fontWeight: 700 }">{{ crashLevelText }}</span>
          </div>
          <div style="font-size:0.85rem;color:var(--text-secondary);margin-bottom:8px;">{{ marketCrash.suggestion?.message }}</div>
          <div v-if="marketCrash.indexes && marketCrash.indexes.length" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;">
            <div v-for="idx in marketCrash.indexes" :key="idx.symbol" style="background:var(--bg-tertiary);padding:4px 10px;border-radius:var(--radius-sm);font-size:0.8rem;">
              <span style="font-weight:600;">{{ idx.name }}</span>
              <span :style="{ color: idx.changePercent >= 0 ? 'var(--accent-danger)' : 'var(--accent-success)', fontWeight: 700 }">{{ idx.price }} ({{ idx.changePercent >= 0 ? '+' : '' }}{{ idx.changePercent?.toFixed(2) }}%)</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 操作按钮 -->
    <div class="scan-controls" style="margin-bottom:24px;">
      <button class="btn-scan" style="padding:10px 20px;" @click="loadWeights">刷新权重</button>
      <button class="btn-scan" style="padding:10px 20px;" @click="runBacktest" :disabled="backtesting">{{ backtesting ? '回测中...' : '手动回测' }}</button>
      <button class="btn-secondary" style="padding:10px 20px;" @click="resetWeights">重置权重</button>
    </div>

    <!-- 策略权重卡片 -->
    <div style="margin-bottom:24px;">
      <div class="section-subtitle-small" style="margin-bottom:12px;">当前策略权重</div>
      <div class="strategy-weights-grid">
        <div v-for="w in weights" :key="w.factor" class="strategy-card">
          <div class="strategy-card-header">
            <span class="strategy-card-title">{{ w.description || w.factor }}</span>
            <span class="strategy-card-subtitle">{{ w.factor }}</span>
          </div>
          <div class="strategy-bar-row">
            <span class="strategy-bar-label">权重</span>
            <div class="strategy-bar-track">
              <div class="strategy-bar-fill" :style="{ width: Math.min(100, Math.round(w.weight * 100)) + '%', background: w.weight > 1.2 ? 'var(--accent-danger)' : w.weight < 0.8 ? 'var(--accent-info)' : 'var(--accent-success)' }"></div>
            </div>
            <span class="strategy-bar-value" :style="{ color: w.weight > 1.2 ? 'var(--accent-danger)' : w.weight < 0.8 ? 'var(--accent-info)' : 'var(--accent-success)' }">{{ w.weight.toFixed(2) }}</span>
          </div>
          <div class="strategy-bar-row">
            <span class="strategy-bar-label">准确率</span>
            <div class="strategy-bar-track">
              <div class="strategy-bar-fill" :style="{ width: w.accuracy + '%', background: w.accuracy > 60 ? 'var(--accent-success)' : w.accuracy < 40 ? 'var(--accent-danger)' : 'var(--accent-warning)' }"></div>
            </div>
            <span class="strategy-bar-value" :style="{ color: w.accuracy > 60 ? 'var(--accent-success)' : w.accuracy < 40 ? 'var(--accent-danger)' : 'var(--accent-warning)' }">{{ w.accuracy }}%</span>
          </div>
          <div class="strategy-card-footer">样本: {{ w.sample_count }} | 正确: {{ w.correct_count }}</div>
        </div>
      </div>
    </div>

    <!-- 回测历史 -->
    <div style="margin-bottom:24px;">
      <div class="section-subtitle-small" style="margin-bottom:12px;">回测报告</div>
      <div v-if="backtestHistory.length === 0" class="empty-state">暂无回测数据，每日15:20自动回测，或点击"手动回测"</div>
      <div v-for="bt in pagedBacktestHistory" :key="bt.backtest_date" class="backtest-card">
        <div class="backtest-header">
          <span class="backtest-date">{{ bt.backtest_date }}</span>
          <span class="backtest-market" :style="{ color: bt.market_change > 0 ? 'var(--accent-danger)' : bt.market_change < 0 ? 'var(--accent-success)' : 'var(--text-secondary)' }">
            {{ bt.market_change > 0 ? '📈 上涨' : bt.market_change < 0 ? '📉 下跌' : '➡️ 平盘' }}
            {{ bt.market_change > 0 ? '+' : '' }}{{ parseFloat(bt.market_change).toFixed(2) }}%
          </span>
        </div>
        <div class="backtest-grid">
          <div class="backtest-grid-item">
            <div class="backtest-grid-label">总准确率</div>
            <div class="backtest-grid-value" :style="{ color: bt.accuracy > 60 ? 'var(--accent-success)' : bt.accuracy < 40 ? 'var(--accent-danger)' : 'var(--accent-warning)' }">{{ bt.accuracy }}%</div>
          </div>
          <div class="backtest-grid-item">
            <div class="backtest-grid-label">看涨准确率</div>
            <div class="backtest-grid-value" style="color:var(--accent-danger);">{{ bt.bull_accuracy }}%</div>
          </div>
          <div class="backtest-grid-item">
            <div class="backtest-grid-label">预测总数</div>
            <div class="backtest-grid-value" style="color:var(--text-primary);">{{ bt.total_predictions }}</div>
          </div>
        </div>
        <div v-if="bt.misjudge_analysis" class="backtest-analysis">
          <div v-if="bt.misjudge_analysis.market_context" style="margin-bottom:4px;">📊 大盘: <span :style="{ color: bt.market_change > 0 ? 'var(--accent-danger)' : 'var(--accent-success)' }">{{ bt.misjudge_analysis.market_context }}</span></div>
          <div v-if="bt.misjudge_analysis.bull_misjudge_analysis" style="margin-bottom:4px;color:var(--accent-danger);">🔴 误判分析: {{ bt.misjudge_analysis.bull_misjudge_analysis }}</div>
          <div v-if="bt.misjudge_analysis.suggestions && bt.misjudge_analysis.suggestions.length" style="margin-top:6px;"><strong>优化建议:</strong></div>
          <div v-for="s in (bt.misjudge_analysis.suggestions || [])" :key="s" style="color:#7c3aed;">💡 {{ s }}</div>
        </div>
        <div v-if="bt.weight_adjustments && bt.weight_adjustments.filter(a => a.action !== 'keep').length" class="backtest-adjustments">
          <div><strong>权重调整:</strong></div>
          <div v-for="a in bt.weight_adjustments.filter(a => a.action !== 'keep')" :key="a.factor" style="font-size:0.8rem;">
            {{ a.action === 'increase' ? '⬆️' : '⬇️' }} {{ a.factor }}: {{ a.reason }}
          </div>
        </div>
      </div>
      <div v-if="backtestTotalPages > 1" class="history-pagination">
        <button class="page-btn" :disabled="backtestPage <= 1" @click="backtestPage--">上一页</button>
        <span class="page-info">第 {{ backtestPage }} / {{ backtestTotalPages }} 页</span>
        <button class="page-btn" :disabled="backtestPage >= backtestTotalPages" @click="backtestPage++">下一页</button>
      </div>
    </div>

    <!-- 预测记录 -->
    <div>
      <div class="section-subtitle-small" style="margin-bottom:12px;">预测记录（含历史待验证）</div>
      <div v-if="predictions.length === 0" class="empty-state">暂无预测记录，扫描股票后自动生成</div>
      <div v-else>
        <div class="stock-table">
          <div class="stock-table-header">
            <span>日期</span><span>名称</span><span>评分</span><span>涨跌幅</span><span>外盘占比</span><span>量比</span><span>委比</span><span>验证</span>
          </div>
          <div v-for="r in pagedPredictions" :key="r.id" class="stock-table-row">
            <span style="font-size:0.75rem;color:var(--text-secondary);font-weight:500;">{{ r.predict_date ? r.predict_date.slice(5) : '' }}</span>
            <span class="name">{{ r.name }}<br><small style="color:var(--text-tertiary);">{{ (r.symbol || '').toUpperCase() }}</small></span>
            <span :style="{ fontWeight: 600, color: r.score >= 60 ? 'var(--accent-danger)' : r.score >= 40 ? 'var(--accent-warning)' : 'var(--accent-success)' }">{{ r.score }}</span>
            <span :style="{ color: parseFloat(r.change_percent) >= 0 ? 'var(--accent-danger)' : 'var(--accent-success)' }">{{ parseFloat(r.change_percent) >= 0 ? '+' : '' }}{{ parseFloat(r.change_percent).toFixed(2) }}%</span>
            <span>{{ parseFloat(r.outer_ratio).toFixed(1) }}%</span>
            <span>{{ parseFloat(r.volume_ratio).toFixed(2) }}</span>
            <span :style="{ color: parseFloat(r.weibi) >= 0 ? 'var(--accent-danger)' : 'var(--accent-success)' }">{{ parseFloat(r.weibi) >= 0 ? '+' : '' }}{{ parseFloat(r.weibi).toFixed(2) }}%</span>
            <span :style="{ color: r.verified ? (r.is_correct ? 'var(--accent-success)' : 'var(--accent-danger)') : 'var(--accent-tertiary)', fontWeight: 600 }">{{ r.verified ? (r.is_correct ? '✅正确' : '❌错误') : '⏳待验证' }}</span>
          </div>
        </div>
        <div v-if="predictionTotalPages > 1" class="history-pagination">
          <button class="page-btn" :disabled="predictionPage <= 1" @click="predictionPage--">上一页</button>
          <span class="page-info">第 {{ predictionPage }} / {{ predictionTotalPages }} 页</span>
          <button class="page-btn" :disabled="predictionPage >= predictionTotalPages" @click="predictionPage++">下一页</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, inject } from 'vue'
import { apiFetch } from '../../utils/helpers'

const showNotification = inject('showNotification')
const weights = ref([])
const backtestHistory = ref([])
const predictions = ref([])
const backtesting = ref(false)
const marketPredict = ref(null)
const marketPredictLoading = ref(false)
const marketCrash = ref(null)
const marketPredictExpanded = ref(true)

const PREDICTION_PAGE_SIZE = 5
const predictionPage = ref(1)
const predictionTotalPages = computed(() => Math.ceil(predictions.value.length / PREDICTION_PAGE_SIZE))
const pagedPredictions = computed(() => {
  const start = (predictionPage.value - 1) * PREDICTION_PAGE_SIZE
  return predictions.value.slice(start, start + PREDICTION_PAGE_SIZE)
})

const BACKTEST_PAGE_SIZE = 3
const backtestPage = ref(1)
const backtestTotalPages = computed(() => Math.ceil(backtestHistory.value.length / BACKTEST_PAGE_SIZE))
const pagedBacktestHistory = computed(() => {
  const start = (backtestPage.value - 1) * BACKTEST_PAGE_SIZE
  return backtestHistory.value.slice(start, start + BACKTEST_PAGE_SIZE)
})

const predictBorderColor = computed(() => {
  const s = marketPredict.value?.predictStatus
  if (s === 'severe_danger' || s === 'danger') return 'var(--accent-danger)'
  if (s === 'warning') return 'var(--accent-warning)'
  return 'var(--accent-success)'
})
const predictBgColor = computed(() => {
  const s = marketPredict.value?.predictStatus
  if (s === 'severe_danger' || s === 'danger') return 'rgba(220,38,38,0.08)'
  if (s === 'warning') return 'rgba(234,88,12,0.08)'
  return 'rgba(22,163,74,0.08)'
})
const predictTextColor = computed(() => {
  const s = marketPredict.value?.predictStatus
  if (s === 'severe_danger' || s === 'danger') return 'var(--accent-danger)'
  if (s === 'warning') return 'var(--accent-warning)'
  return 'var(--accent-success)'
})
const crashBorderColor = computed(() => {
  const s = marketCrash.value?.marketStatus
  if (s === 'severe_crash') return 'var(--accent-danger)'
  if (s === 'crash') return 'var(--accent-warning)'
  return 'var(--accent-success)'
})
const crashLevelText = computed(() => {
  const s = marketCrash.value?.crashLevel
  if (s === 'severe') return '暴跌'
  if (s === 'moderate') return '大跌'
  return '正常'
})

async function loadMarketPredict() {
  marketPredictLoading.value = true
  try {
    const [predictResult, crashResult] = await Promise.all([
      apiFetch('/api/market-predict'),
      apiFetch('/api/market-index')
    ])
    if (predictResult.success) marketPredict.value = predictResult.data
    if (crashResult.success) marketCrash.value = crashResult.data
  } catch (e) {
    showNotification('加载大盘预测失败')
  } finally {
    marketPredictLoading.value = false
  }
}

async function loadWeights() {
  try {
    const result = await apiFetch('/api/strategy/weights')
    if (result.success && result.data) {
      weights.value = result.data
    }
  } catch (e) { console.error('加载权重失败:', e) }
}

async function loadBacktestHistory() {
  try {
    const result = await apiFetch('/api/strategy/backtest-history?days=30')
    if (result.success && result.data) {
      backtestHistory.value = result.data
    }
  } catch (e) { console.error('加载回测历史失败:', e) }
}

async function loadPredictionRecords() {
  try {
    const result = await apiFetch('/api/strategy/predictions?include_today_and_pending=true')
    if (result.success && result.data) {
      predictions.value = result.data
      predictionPage.value = 1
    }
  } catch (e) { console.error('加载预测记录失败:', e) }
}

async function runBacktest() {
  backtesting.value = true
  try {
    const result = await apiFetch('/api/strategy/backtest', { method: 'POST' })
    if (result.success) {
      showNotification('回测完成')
      await Promise.all([loadWeights(), loadBacktestHistory(), loadPredictionRecords()])
    } else {
      showNotification(result.error || '回测失败')
    }
  } catch (e) {
    showNotification('回测请求失败')
  } finally {
    backtesting.value = false
  }
}

async function resetWeights() {
  if (!confirm('确定要重置所有策略权重为默认值吗？')) return
  try {
    const result = await apiFetch('/api/strategy/weights/reset', { method: 'POST' })
    if (result.success) {
      showNotification('策略权重已重置')
      await loadWeights()
    }
  } catch (e) { showNotification('重置失败') }
}

onMounted(() => {
  loadWeights()
  loadBacktestHistory()
  loadPredictionRecords()
  loadMarketPredict()
})
</script>
