<template>
  <div class="modal" v-if="visible" style="display:flex;">
    <div class="modal-content ai-diag-modal">
      <div class="modal-header">
        <span class="modal-title">AI智能诊断</span>
        <button class="close-btn" @click="close">&times;</button>
      </div>
      <div class="ai-diag-body">
        <!-- 加载中 -->
        <div v-if="loading" class="ai-diag-loading">
          <div class="loading-spinner"></div>
          <div class="loading-text">AI正在分析中，请稍候...</div>
          <div class="loading-sub">正在获取行情数据、K线数据、大盘环境并调用AI模型</div>
        </div>
        <!-- 错误 -->
        <div v-else-if="error" class="ai-diag-error">
          <div class="error-icon">&#9888;</div>
          <div class="error-text">{{ error }}</div>
          <button class="btn-primary" @click="retry">重试</button>
        </div>
        <!-- 诊断结果 -->
        <template v-else-if="diagData">
          <!-- 股票头部 -->
          <div class="diag-stock-header">
            <div class="diag-stock-info">
              <span class="diag-stock-name">{{ diagData.stock.name }}</span>
              <span class="diag-stock-code">{{ (diagData.stock.symbol || '').toUpperCase() }}</span>
            </div>
            <div class="diag-stock-price" :style="{ color: diagData.stock.changePercent >= 0 ? 'var(--red)' : 'var(--green)' }">
              {{ diagData.stock.price }}
              {{ diagData.stock.changePercent >= 0 ? '+' : '' }}{{ diagData.stock.changePercent }}%
            </div>
          </div>

          <!-- 方向与置信度 -->
          <div class="diag-direction-section">
            <div class="diag-direction-badge" :class="directionClass">{{ diag.direction || '观望' }}</div>
            <div class="diag-confidence">
              <span class="diag-confidence-label">置信度</span>
              <div class="diag-confidence-bar">
                <div class="diag-confidence-fill" :style="{ width: (diag.confidence || 0) + '%', background: confidenceColor }"></div>
              </div>
              <span class="diag-confidence-value">{{ diag.confidence || 0 }}%</span>
            </div>
          </div>

          <!-- 核心观点 -->
          <div class="diag-summary">{{ diag.summary }}</div>

          <!-- 五维评分 -->
          <div v-if="diag.scores" class="diag-scores-section">
            <div class="diag-section-title">五维评分</div>
            <div class="diag-scores-grid">
              <div v-for="(label, key) in scoreLabels" :key="key" class="diag-score-item">
                <div class="diag-score-label">{{ label }}</div>
                <div class="diag-score-bar-track">
                  <div class="diag-score-bar-fill" :style="{ width: (diag.scores[key] || 0) + '%', background: getScoreColor(diag.scores[key] || 0) }"></div>
                </div>
                <div class="diag-score-value" :style="{ color: getScoreColor(diag.scores[key] || 0) }">{{ diag.scores[key] || 0 }}</div>
              </div>
            </div>
          </div>

          <!-- 多维分析 -->
          <div v-if="diag.analysis" class="diag-analysis-section">
            <div class="diag-section-title">多维分析</div>
            <div class="diag-analysis-list">
              <div v-for="(label, key) in analysisLabels" :key="key" class="diag-analysis-item">
                <div class="diag-analysis-title">{{ label }}</div>
                <div class="diag-analysis-text">{{ diag.analysis[key] }}</div>
              </div>
            </div>
          </div>

          <!-- 关键信号 -->
          <div v-if="diag.keySignals" class="diag-signals-section">
            <div class="diag-section-title">关键信号</div>
            <div class="diag-signals-grid">
              <div class="diag-signals-col bullish">
                <div class="diag-signals-col-title">看多信号</div>
                <div v-for="(s, i) in (diag.keySignals.bullish || [])" :key="'b'+i" class="diag-signal-item bullish">&#9650; {{ s }}</div>
                <div v-if="!diag.keySignals.bullish || diag.keySignals.bullish.length === 0" class="diag-signal-empty">暂无</div>
              </div>
              <div class="diag-signals-col bearish">
                <div class="diag-signals-col-title">看空信号</div>
                <div v-for="(s, i) in (diag.keySignals.bearish || [])" :key="'s'+i" class="diag-signal-item bearish">&#9660; {{ s }}</div>
                <div v-if="!diag.keySignals.bearish || diag.keySignals.bearish.length === 0" class="diag-signal-empty">暂无</div>
              </div>
            </div>
          </div>

          <!-- 触发条件 -->
          <div v-if="diag.triggerCondition" class="diag-trigger-section">
            <div class="diag-section-title">转为操作条件</div>
            <div class="diag-trigger-text">{{ diag.triggerCondition }}</div>
          </div>

          <!-- 风险提示 -->
          <div v-if="diag.risk" class="diag-risk-section">
            <div class="diag-section-title">风险提示</div>
            <div class="diag-risk-text">{{ diag.risk }}</div>
          </div>

          <!-- 操作建议 -->
          <div v-if="diag.suggestion" class="diag-suggestion-section">
            <div class="diag-section-title">操作建议</div>
            <div class="diag-suggestion-text">{{ diag.suggestion }}</div>
          </div>

          <!-- 原始数据（折叠） -->
          <details v-if="diag.raw" class="diag-raw-section">
            <summary>原始AI输出</summary>
            <pre class="diag-raw-content">{{ diag.raw }}</pre>
          </details>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, inject } from 'vue'
import { apiFetch } from '../utils/helpers'

const props = defineProps({
  visible: Boolean,
  symbol: String,
})
const emit = defineEmits(['close'])
const showNotification = inject('showNotification')

const loading = ref(false)
const error = ref('')
const diagData = ref(null)

const diag = computed(() => diagData.value?.diagnosis || {})
const directionClass = computed(() => {
  const d = diag.value.direction || ''
  if (d.includes('强烈买入') || d.includes('买入')) return 'direction-buy-strong'
  if (d.includes('轻仓关注')) return 'direction-buy-light'
  if (d.includes('减仓') || d.includes('卖出')) return 'direction-sell'
  return 'direction-hold'
})
const confidenceColor = computed(() => {
  const c = diag.value.confidence || 0
  if (c >= 70) return '#dc2626'
  if (c >= 50) return '#f59e0b'
  return '#64748b'
})

const scoreLabels = { volume: '成交量', capital: '主力资金', technique: '技术面', market: '大盘环境', fundamental: '基本面' }
const analysisLabels = { volume: '成交量分析', capital: '主力资金分析', technique: '技术面分析', market: '大盘环境分析', fundamental: '基本面分析' }

function getScoreColor(score) {
  if (score >= 70) return '#dc2626'
  if (score >= 40) return '#f59e0b'
  return '#16a34a'
}

async function fetchDiagnosis(symbol) {
  if (!symbol) return
  loading.value = true
  error.value = ''
  diagData.value = null
  try {
    const result = await apiFetch(`/api/ai-diagnose/${symbol}`)
    if (result.success) {
      diagData.value = result.data
    } else {
      error.value = result.error || result.detail || 'AI诊断失败'
    }
  } catch (e) {
    error.value = 'AI诊断请求失败，请检查网络或API配置'
  } finally {
    loading.value = false
  }
}

function retry() {
  if (props.symbol) fetchDiagnosis(props.symbol)
}

function close() {
  emit('close')
}

watch(() => props.visible, (val) => {
  if (val && props.symbol) {
    fetchDiagnosis(props.symbol)
  }
})
</script>

<style scoped>
.ai-diag-modal {
  max-width: 640px;
  width: 95vw;
  max-height: 90vh;
  display: flex;
  flex-direction: column;
}
.ai-diag-body {
  overflow-y: auto;
  flex: 1;
  padding: 16px 18px;
}
.ai-diag-loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 2.5rem 1rem;
}
.loading-text {
  margin-top: 0.8rem;
  font-size: 1rem;
  font-weight: 600;
  color: var(--text);
}
.loading-sub {
  margin-top: 0.3rem;
  font-size: 0.78rem;
  color: var(--text-3);
}
.ai-diag-error {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 1.5rem 1rem;
}
.error-icon { font-size: 1.8rem; color: var(--red); }
.error-text { margin: 0.6rem 0; color: var(--text-2); font-size: 0.84rem; text-align: center; }

.diag-stock-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 12px;
  background: var(--bg-muted);
  border-radius: var(--radius);
  margin-bottom: 12px;
}
.diag-stock-info { display: flex; align-items: baseline; gap: 6px; }
.diag-stock-name { font-size: 1rem; font-weight: 700; color: var(--text); }
.diag-stock-code { font-size: 0.78rem; color: var(--text-3); }
.diag-stock-price { font-size: 1.1rem; font-weight: 700; font-variant-numeric: tabular-nums; }

.diag-direction-section {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}
.diag-direction-badge {
  padding: 4px 12px;
  border-radius: var(--radius-pill);
  font-size: 0.88rem;
  font-weight: 700;
  color: #fff;
  white-space: nowrap;
}
.direction-buy-strong { background: var(--red); }
.direction-buy-light { background: var(--orange); }
.direction-sell { background: var(--green); }
.direction-hold { background: var(--text-3); }
.diag-confidence {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
}
.diag-confidence-label { font-size: 0.78rem; color: var(--text-3); white-space: nowrap; }
.diag-confidence-bar {
  flex: 1;
  height: 6px;
  background: var(--border);
  border-radius: 3px;
  overflow: hidden;
}
.diag-confidence-fill { height: 100%; border-radius: 3px; transition: width 0.5s; }
.diag-confidence-value { font-size: 0.82rem; font-weight: 600; min-width: 2.2rem; }

.diag-summary {
  padding: 10px 12px;
  background: var(--blue-bg);
  border-left: 3px solid var(--blue);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  font-size: 0.84rem;
  color: var(--blue);
  line-height: 1.6;
  margin-bottom: 14px;
}

.diag-section-title {
  font-size: 0.82rem;
  font-weight: 700;
  color: var(--text-2);
  margin-bottom: 8px;
  padding-left: 8px;
  border-left: 3px solid var(--blue);
}

.diag-scores-section { margin-bottom: 14px; }
.diag-scores-grid { display: flex; flex-direction: column; gap: 6px; }
.diag-score-item { display: flex; align-items: center; gap: 8px; }
.diag-score-label { font-size: 0.78rem; color: var(--text-2); min-width: 4rem; }
.diag-score-bar-track {
  flex: 1;
  height: 5px;
  background: var(--border);
  border-radius: 3px;
  overflow: hidden;
}
.diag-score-bar-fill { height: 100%; border-radius: 3px; transition: width 0.5s; }
.diag-score-value { font-size: 0.78rem; font-weight: 600; min-width: 1.8rem; text-align: right; }

.diag-analysis-section { margin-bottom: 14px; }
.diag-analysis-list { display: flex; flex-direction: column; gap: 6px; }
.diag-analysis-item {
  padding: 8px 10px;
  background: var(--bg-muted);
  border-radius: var(--radius-sm);
}
.diag-analysis-title { font-size: 0.78rem; font-weight: 600; color: var(--text-2); margin-bottom: 3px; }
.diag-analysis-text { font-size: 0.8rem; color: var(--text); line-height: 1.5; }

.diag-signals-section { margin-bottom: 14px; }
.diag-signals-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.diag-signals-col { padding: 8px; border-radius: var(--radius-sm); }
.diag-signals-col.bullish { background: var(--red-bg); }
.diag-signals-col.bearish { background: var(--green-bg); }
.diag-signals-col-title { font-size: 0.78rem; font-weight: 600; margin-bottom: 4px; }
.bullish .diag-signals-col-title { color: var(--red); }
.bearish .diag-signals-col-title { color: var(--green); }
.diag-signal-item { font-size: 0.76rem; padding: 2px 0; line-height: 1.4; }
.diag-signal-item.bullish { color: var(--red); }
.diag-signal-item.bearish { color: var(--green); }
.diag-signal-empty { font-size: 0.76rem; color: var(--text-3); }

.diag-trigger-section { margin-bottom: 14px; }
.diag-trigger-text {
  padding: 8px 10px;
  background: var(--orange-bg);
  border-radius: var(--radius-sm);
  font-size: 0.8rem;
  color: var(--orange);
  line-height: 1.5;
}

.diag-risk-section { margin-bottom: 14px; }
.diag-risk-text {
  padding: 8px 10px;
  background: var(--red-bg);
  border-radius: var(--radius-sm);
  font-size: 0.8rem;
  color: var(--red);
  line-height: 1.5;
}

.diag-suggestion-section { margin-bottom: 14px; }
.diag-suggestion-text {
  padding: 10px 12px;
  background: var(--green-bg);
  border-left: 3px solid var(--green);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  font-size: 0.84rem;
  color: var(--green);
  line-height: 1.6;
}

.diag-raw-section {
  margin-top: 12px;
  border-top: 1px solid var(--border);
  padding-top: 10px;
}
.diag-raw-section summary {
  font-size: 0.78rem;
  color: var(--text-3);
  cursor: pointer;
}
.diag-raw-content {
  margin-top: 6px;
  font-size: 0.72rem;
  color: var(--text-2);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 180px;
  overflow-y: auto;
  background: var(--bg-muted);
  padding: 8px;
  border-radius: var(--radius-sm);
}
</style>
