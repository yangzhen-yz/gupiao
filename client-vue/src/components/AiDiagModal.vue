<template>
  <div class="modal" v-if="visible" style="display:flex;">
    <div class="modal-content ai-diag-modal">
      <div class="modal-header">
        <div class="modal-header-left">
          <span class="modal-title">🤖 AI 智能诊断</span>
          <span v-if="diagData" class="modal-subtitle">DeepSeek 多维度量化分析</span>
        </div>
        <button class="close-btn" @click="close">&times;</button>
      </div>
      <div class="ai-diag-body">
        <!-- 加载中 -->
        <div v-if="loading" class="diag-loading">
          <div class="loading-spinner"></div>
          <div class="diag-loading-text">AI 正在分析中…</div>
          <div class="diag-loading-sub">获取行情数据 · K线数据 · 大盘环境 · 调用 AI 模型</div>
        </div>
        <!-- 错误 -->
        <div v-else-if="error" class="diag-error">
          <div class="diag-error-icon">⚠</div>
          <div class="diag-error-text">{{ error }}</div>
          <button class="btn-primary diag-retry-btn" @click="retry">🔁 重试</button>
        </div>
        <!-- 诊断结果 -->
        <template v-else-if="diagData">
          <!-- 股票头部 -->
          <div class="diag-header">
            <div class="diag-header-left">
              <span class="diag-header-name">{{ diagData.stock.name }}</span>
              <span class="diag-header-code">{{ (diagData.stock.symbol || '').toUpperCase() }}</span>
            </div>
            <div class="diag-header-right">
              <span class="diag-header-price">{{ diagData.stock.price }}</span>
              <span class="diag-header-change" :class="diagData.stock.changePercent >= 0 ? 'up' : 'down'">
                {{ diagData.stock.changePercent >= 0 ? '+' : '' }}{{ diagData.stock.changePercent }}%
              </span>
            </div>
          </div>

          <!-- 方向 + 置信度 -->
          <div class="diag-verdict">
            <span class="diag-verdict-badge" :class="directionClass">{{ diag.direction || '观望' }}</span>
            <div class="diag-verdict-conf">
              <span class="diag-verdict-conf-label">置信度</span>
              <div class="diag-verdict-conf-track">
                <div
                  class="diag-verdict-conf-fill"
                  :style="{ width: (diag.confidence || 0) + '%' }"
                  :class="confidenceLevel"
                ></div>
              </div>
              <span class="diag-verdict-conf-val">{{ diag.confidence || 0 }}%</span>
            </div>
          </div>

          <!-- 核心观点 -->
          <div class="diag-insight">
            <span class="diag-insight-icon">💡</span>
            <span class="diag-insight-text">{{ diag.summary }}</span>
          </div>

          <!-- 五维评分 -->
          <div v-if="diag.scores" class="diag-section">
            <div class="diag-section-head">
              <span>📊</span><span>五维评分</span>
            </div>
            <div class="diag-scores-box">
              <div v-for="(label, key) in scoreLabels" :key="key" class="diag-scores-row">
                <span class="diag-scores-name">{{ label }}</span>
                <div class="diag-scores-track">
                  <div
                    class="diag-scores-fill"
                    :style="{ width: (diag.scores[key] || 0) + '%' }"
                    :class="scoreLevel(diag.scores[key] || 0)"
                  ></div>
                </div>
                <span class="diag-scores-val" :class="scoreLevel(diag.scores[key] || 0)">
                  {{ diag.scores[key] || 0 }}
                </span>
              </div>
            </div>
          </div>

          <!-- 多维分析 -->
          <div v-if="diag.analysis" class="diag-section">
            <div class="diag-section-head">
              <span>🔍</span><span>多维分析</span>
            </div>
            <div class="diag-analysis">
              <div
                v-for="(label, key) in analysisLabels"
                :key="key"
                class="diag-analysis-card"
                :class="'diag-analysis-' + key"
              >
                <div class="diag-analysis-card-title">{{ label }}</div>
                <div class="diag-analysis-card-text">{{ diag.analysis[key] }}</div>
              </div>
            </div>
          </div>

          <!-- 关键信号 -->
          <div v-if="diag.keySignals" class="diag-section">
            <div class="diag-section-head">
              <span>📡</span><span>关键信号</span>
            </div>
            <div class="diag-signals">
              <div class="diag-signals-col bullish">
                <div class="diag-signals-col-head">
                  <span class="diag-signals-dot bull"></span>看多信号
                </div>
                <div
                  v-for="(s, i) in (diag.keySignals.bullish || [])"
                  :key="'b'+i"
                  class="diag-signals-item bull"
                >
                  <span class="diag-signals-marker">▲</span>{{ s }}
                </div>
                <div v-if="!diag.keySignals.bullish || diag.keySignals.bullish.length === 0" class="diag-signals-empty">— 暂无 —</div>
              </div>
              <div class="diag-signals-col bearish">
                <div class="diag-signals-col-head">
                  <span class="diag-signals-dot bear"></span>看空信号
                </div>
                <div
                  v-for="(s, i) in (diag.keySignals.bearish || [])"
                  :key="'s'+i"
                  class="diag-signals-item bear"
                >
                  <span class="diag-signals-marker">▼</span>{{ s }}
                </div>
                <div v-if="!diag.keySignals.bearish || diag.keySignals.bearish.length === 0" class="diag-signals-empty">— 暂无 —</div>
              </div>
            </div>
          </div>

          <!-- 触发条件 -->
          <div v-if="diag.triggerCondition" class="diag-section">
            <div class="diag-section-head">
              <span>⚡</span><span>转为操作条件</span>
            </div>
            <div class="diag-card warn">{{ diag.triggerCondition }}</div>
          </div>

          <!-- 风险提示 -->
          <div v-if="diag.risk" class="diag-section">
            <div class="diag-section-head">
              <span>🛡</span><span>风险提示</span>
            </div>
            <div class="diag-card danger">{{ diag.risk }}</div>
          </div>

          <!-- 操作建议 -->
          <div v-if="diag.suggestion" class="diag-section">
            <div class="diag-section-head">
              <span>🎯</span><span>操作建议</span>
            </div>
            <div class="diag-card success">{{ diag.suggestion }}</div>
          </div>

          <!-- 原始数据（折叠） -->
          <details v-if="diag.raw" class="diag-raw">
            <summary>📋 原始 AI 输出</summary>
            <pre class="diag-raw-content">{{ diag.raw }}</pre>
          </details>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { apiFetch } from '../utils/helpers'

const props = defineProps({
  visible: Boolean,
  symbol: String,
})
const emit = defineEmits(['close'])

const loading = ref(false)
const error = ref('')
const diagData = ref(null)

const diag = computed(() => diagData.value?.diagnosis || {})

const directionClass = computed(() => {
  const d = diag.value.direction || ''
  if (d.includes('强烈买入')) return 'dir-buy-strong'
  if (d.includes('买入')) return 'dir-buy'
  if (d.includes('轻仓关注')) return 'dir-light'
  if (d.includes('减仓') || d.includes('卖出')) return 'dir-sell'
  return 'dir-hold'
})

const confidenceLevel = computed(() => {
  const c = diag.value.confidence || 0
  if (c >= 70) return 'conf-high'
  if (c >= 45) return 'conf-mid'
  return 'conf-low'
})

const scoreLabels = { volume: '成交量', capital: '主力资金', technique: '技术面', market: '大盘环境', fundamental: '基本面' }
const analysisLabels = { volume: '成交量分析', capital: '主力资金分析', technique: '技术面分析', market: '大盘环境分析', fundamental: '基本面分析' }

function scoreLevel(v) {
  if (v >= 70) return 'sc-hi'
  if (v >= 40) return 'sc-md'
  return 'sc-lo'
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
/* ===== 弹窗容器 ===== */
.ai-diag-modal {
  max-width: 680px;
  width: 95vw;
  max-height: 88vh;
  display: flex;
  flex-direction: column;
}
.modal-header-left {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.modal-subtitle {
  font-size: 0.7rem;
  font-weight: 400;
  color: var(--text-tertiary);
  letter-spacing: 0.02em;
}
.ai-diag-body {
  overflow-y: auto;
  flex: 1;
  padding: 20px 22px;
}

/* ===== 加载态 ===== */
.diag-loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 3rem 1rem;
  gap: 12px;
}
.diag-loading-text {
  font-size: 1rem;
  font-weight: 600;
  color: var(--text-primary);
}
.diag-loading-sub {
  font-size: 0.76rem;
  color: var(--text-tertiary);
}

/* ===== 错误态 ===== */
.diag-error {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 2.5rem 1rem;
  gap: 10px;
}
.diag-error-icon {
  font-size: 2.4rem;
  line-height: 1;
  margin-bottom: 4px;
}
.diag-error-text {
  color: var(--text-secondary);
  font-size: 0.84rem;
  text-align: center;
  line-height: 1.5;
  max-width: 360px;
}
.diag-retry-btn { margin-top: 4px; padding: 7px 20px; }

/* ===== 股票头部 ===== */
.diag-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 14px 16px;
  background: linear-gradient(135deg, #f0f4ff 0%, #f8fafc 100%);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  margin-bottom: 14px;
}
.diag-header-left {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.diag-header-name {
  font-size: 1.1rem;
  font-weight: 700;
  color: var(--text-primary);
}
.diag-header-code {
  font-size: 0.74rem;
  color: var(--text-tertiary);
  font-family: 'SF Mono', 'Consolas', monospace;
  background: rgba(0,0,0,0.04);
  padding: 2px 6px;
  border-radius: var(--radius-xs);
}
.diag-header-right {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.diag-header-price {
  font-size: 1.15rem;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: var(--text-primary);
}
.diag-header-change {
  font-size: 0.88rem;
  font-weight: 700;
  padding: 3px 10px;
  border-radius: var(--radius-pill);
  color: #fff;
  font-variant-numeric: tabular-nums;
}
.diag-header-change.up   { background: var(--accent-danger); }
.diag-header-change.down { background: var(--accent-success); }

/* ===== 方向 + 置信度 ===== */
.diag-verdict {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 14px;
  padding: 12px 16px;
  background: var(--bg-secondary);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
}
.diag-verdict-badge {
  padding: 6px 16px;
  border-radius: var(--radius-pill);
  font-size: 0.92rem;
  font-weight: 700;
  color: #fff;
  white-space: nowrap;
  box-shadow: 0 2px 6px rgba(0,0,0,0.1);
}
.dir-buy-strong { background: linear-gradient(135deg, #dc2626, #ef4444); }
.dir-buy        { background: linear-gradient(135deg, #f97316, #fb923c); }
.dir-light      { background: linear-gradient(135deg, #eab308, #facc15); color: #713f12 !important; }
.dir-sell       { background: linear-gradient(135deg, #16a34a, #22c55e); }
.dir-hold       { background: linear-gradient(135deg, #78716c, #a8a29e); }

.diag-verdict-conf {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
}
.diag-verdict-conf-label {
  font-size: 0.74rem;
  color: var(--text-tertiary);
  white-space: nowrap;
}
.diag-verdict-conf-track {
  flex: 1;
  height: 8px;
  background: var(--bg-tertiary);
  border-radius: 4px;
  overflow: hidden;
}
.diag-verdict-conf-fill {
  height: 100%;
  border-radius: 4px;
  transition: width 0.6s cubic-bezier(0.4, 0, 0.2, 1);
}
.conf-high { background: linear-gradient(90deg, #dc2626, #ef4444); }
.conf-mid  { background: linear-gradient(90deg, #f59e0b, #fbbf24); }
.conf-low  { background: linear-gradient(90deg, #94a3b8, #cbd5e1); }
.diag-verdict-conf-val {
  font-size: 0.82rem;
  font-weight: 700;
  color: var(--text-primary);
  min-width: 2.6rem;
  text-align: right;
}

/* ===== 核心观点 ===== */
.diag-insight {
  display: flex;
  gap: 10px;
  padding: 12px 14px;
  background: linear-gradient(135deg, #eff6ff 0%, #f0f9ff 100%);
  border: 1px solid #bfdbfe;
  border-radius: var(--radius-lg);
  margin-bottom: 18px;
}
.diag-insight-icon {
  font-size: 1rem;
  line-height: 1.4;
  flex-shrink: 0;
}
.diag-insight-text {
  font-size: 0.84rem;
  color: #1e3a5f;
  line-height: 1.65;
}

/* ===== Section 通用 ===== */
.diag-section { margin-bottom: 18px; }
.diag-section-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 0.84rem;
  font-weight: 700;
  color: var(--text-primary);
  margin-bottom: 10px;
}

/* ===== 五维评分 ===== */
.diag-scores-box {
  background: var(--bg-secondary);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.diag-scores-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.diag-scores-name {
  font-size: 0.78rem;
  color: var(--text-secondary);
  min-width: 4.2rem;
  font-weight: 500;
}
.diag-scores-track {
  flex: 1;
  height: 8px;
  background: var(--bg-tertiary);
  border-radius: 4px;
  overflow: hidden;
}
.diag-scores-fill {
  height: 100%;
  border-radius: 4px;
  transition: width 0.5s ease;
}
.sc-hi { background: linear-gradient(90deg, #dc2626, #f87171); }
.sc-md { background: linear-gradient(90deg, #f59e0b, #fbbf24); }
.sc-lo { background: linear-gradient(90deg, #16a34a, #4ade80); }
.diag-scores-val {
  font-size: 0.8rem;
  font-weight: 700;
  min-width: 2rem;
  text-align: right;
}
.diag-scores-val.sc-hi { color: var(--accent-danger); }
.diag-scores-val.sc-md { color: #d97706; }
.diag-scores-val.sc-lo { color: var(--accent-success); }

/* ===== 多维分析 ===== */
.diag-analysis {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.diag-analysis-card {
  padding: 12px 14px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-subtle);
  background: var(--bg-secondary);
  transition: box-shadow 0.15s;
}
.diag-analysis-card:hover {
  box-shadow: var(--shadow-soft);
}
.diag-analysis-volume      { border-left: 3px solid #6366f1; }
.diag-analysis-capital     { border-left: 3px solid #f59e0b; }
.diag-analysis-technique   { border-left: 3px solid #14b8a6; }
.diag-analysis-market      { border-left: 3px solid #0ea5e9; }
.diag-analysis-fundamental { border-left: 3px solid #84cc16; }
.diag-analysis-card-title {
  font-size: 0.76rem;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 4px;
}
.diag-analysis-card-text {
  font-size: 0.82rem;
  color: var(--text-primary);
  line-height: 1.55;
}

/* ===== 关键信号 ===== */
.diag-signals {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.diag-signals-col {
  padding: 12px 14px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-subtle);
}
.diag-signals-col.bullish { background: linear-gradient(180deg, #fef2f2 0%, #fff 100%); }
.diag-signals-col.bearish { background: linear-gradient(180deg, #f0fdf4 0%, #fff 100%); }
.diag-signals-col-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 0.78rem;
  font-weight: 700;
  color: var(--text-primary);
  margin-bottom: 8px;
}
.diag-signals-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.diag-signals-dot.bull { background: var(--accent-danger); }
.diag-signals-dot.bear { background: var(--accent-success); }
.diag-signals-item {
  font-size: 0.78rem;
  padding: 3px 0;
  line-height: 1.5;
  display: flex;
  align-items: flex-start;
  gap: 5px;
}
.diag-signals-marker {
  font-size: 0.64rem;
  flex-shrink: 0;
  margin-top: 3px;
}
.diag-signals-item.bull { color: #b91c1c; }
.diag-signals-item.bull .diag-signals-marker { color: var(--accent-danger); }
.diag-signals-item.bear { color: #15803d; }
.diag-signals-item.bear .diag-signals-marker { color: var(--accent-success); }
.diag-signals-empty {
  font-size: 0.74rem;
  color: var(--text-tertiary);
  padding: 4px 0;
}

/* ===== 通用卡片（触发条件/风险/建议） ===== */
.diag-card {
  padding: 12px 14px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-subtle);
  background: var(--bg-secondary);
  font-size: 0.84rem;
  line-height: 1.6;
  color: var(--text-primary);
}
.diag-card.warn {
  border-left: 3px solid var(--accent-warning);
  background: linear-gradient(135deg, #fff7ed 0%, #fff 100%);
}
.diag-card.danger {
  border-left: 3px solid var(--accent-danger);
  background: linear-gradient(135deg, #fef2f2 0%, #fff 100%);
}
.diag-card.success {
  border-left: 3px solid var(--accent-success);
  background: linear-gradient(135deg, #f0fdf4 0%, #fff 100%);
}

/* ===== 原始数据折叠 ===== */
.diag-raw {
  margin-top: 16px;
  border-top: 1px solid var(--border-subtle);
  padding-top: 12px;
}
.diag-raw summary {
  font-size: 0.76rem;
  color: var(--text-tertiary);
  cursor: pointer;
  user-select: none;
  padding: 4px 0;
  transition: color 0.15s;
}
.diag-raw summary:hover { color: var(--text-secondary); }
.diag-raw-content {
  margin-top: 8px;
  font-size: 0.72rem;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 180px;
  overflow-y: auto;
  background: var(--bg-tertiary);
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-subtle);
}
</style>
