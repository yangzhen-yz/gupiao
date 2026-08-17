<template>
  <div>
    <div class="section-title">趋势回测自检验</div>
    <div class="section-subtitle">回看历史趋势策略的实战表现：跨报告加权胜率与平均超额收益（个股 N 日涨跌 − 大盘 N 日涨跌）</div>
    <div class="refresh-controls">
      <div class="toggle-section">
        <span class="toggle-label"><span class="dot" :class="{ live: !loading }"></span>策略自检</span>
      </div>
      <button class="btn-secondary" style="padding:8px 16px;" @click="loadSummary" :disabled="loading">🔄 刷新</button>
      <div class="last-update">{{ lastUpdate }}</div>
    </div>

    <div v-if="loading && !summary" class="loading-wrap">
      <div class="loading-spinner"></div> 加载中...
    </div>

    <div v-else-if="!summary || !summary.hasData" class="empty-state">
      ✨ 暂无回测记录，趋势策略执行后将自动累计数据
    </div>

    <div v-else>
      <!-- 概览卡片 -->
      <div class="overview-grid">
        <div class="ov-card">
          <div class="ov-label">回测报告数</div>
          <div class="ov-value">{{ summary.reports }}</div>
        </div>
        <div class="ov-card">
          <div class="ov-label">累计样本</div>
          <div class="ov-value">{{ summary.totalSamples }}</div>
        </div>
        <div class="ov-card">
          <div class="ov-label">已验证(1/3/5日)</div>
          <div class="ov-value">{{ summary.verified['1d'] }} / {{ summary.verified['3d'] }} / {{ summary.verified['5d'] }}</div>
        </div>
      </div>

      <!-- 加权胜率 -->
      <div class="block-title">📈 跨报告加权胜率</div>
      <div class="metric-grid">
        <div class="metric-card" v-for="p in periods" :key="'wr-' + p">
          <div class="metric-label">{{ p.replace('d','') }}日胜率</div>
          <div class="metric-value" :style="{ color: winRateColor(summary.winRates[p]) }">{{ summary.winRates[p] }}%</div>
        </div>
      </div>

      <!-- 平均超额收益 -->
      <div class="block-title">💹 平均超额收益（跑赢大盘）</div>
      <div class="metric-grid">
        <div class="metric-card" v-for="p in periods" :key="'ex-' + p">
          <div class="metric-label">{{ p.replace('d','') }}日超额</div>
          <div class="metric-value" :style="{ color: summary.avgExcessReturn[p] >= 0 ? 'var(--accent-danger)' : 'var(--accent-success)' }">
            {{ summary.avgExcessReturn[p] >= 0 ? '+' : '' }}{{ summary.avgExcessReturn[p] }}%
          </div>
        </div>
      </div>

      <!-- 近期报告 -->
      <div class="block-title">📋 近期回测报告</div>
      <div class="stock-table backtest-table">
        <div class="stock-table-header">
          <span>预测日期</span><span>样本数</span><span>1日胜率</span><span>3日胜率</span><span>5日胜率</span>
        </div>
        <div v-for="(r, i) in summary.recent" :key="i" class="stock-table-row">
          <span class="bt-date">{{ r.predict_date }}</span>
          <span>{{ r.total_count }}</span>
          <span :style="{ color: winRateColor(r.win_rate_1d) }">{{ r.win_rate_1d?.toFixed(1) || 0 }}%</span>
          <span :style="{ color: winRateColor(r.win_rate_3d) }">{{ r.win_rate_3d?.toFixed(1) || 0 }}%</span>
          <span :style="{ color: winRateColor(r.win_rate_5d) }">{{ r.win_rate_5d?.toFixed(1) || 0 }}%</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, inject } from 'vue'
import { apiFetch } from '../../utils/helpers'

const showNotification = inject('showNotification')
const summary = ref(null)
const loading = ref(false)
const lastUpdate = ref('--')
const periods = ['1d', '3d', '5d']

function winRateColor(v) {
  if (v == null) return '#64748b'
  if (v >= 65) return '#dc2626'   // 高胜率（红=好，CN 习惯）
  if (v >= 50) return '#d97706'
  return '#16a34a'
}

async function loadSummary() {
  if (loading.value) return
  loading.value = true
  try {
    const res = await apiFetch('/api/trend/backtest-summary?limit=30')
    if (res && res.success && res.data) {
      summary.value = res.data
      lastUpdate.value = new Date().toLocaleString('zh-CN')
    } else {
      showNotification?.('回测汇总加载失败')
    }
  } catch (e) {
    showNotification?.('回测汇总加载失败: ' + e.message)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  loadSummary()
})
</script>

<style scoped>
.refresh-controls {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin-bottom: 1rem;
  flex-wrap: wrap;
}
.toggle-label {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.875rem;
  color: #475569;
  font-weight: 500;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #cbd5e1;
  display: inline-block;
}
.dot.live {
  background: #22c55e;
  box-shadow: 0 0 0 3px rgba(34, 197, 94, 0.15);
}
.last-update {
  font-size: 0.8125rem;
  color: #94a3b8;
}
.loading-wrap, .empty-state {
  text-align: center;
  padding: 3rem 1rem;
  color: #64748b;
  background: #f8fafc;
  border-radius: 16px;
  border: 2px dashed #e2e8f0;
}
.overview-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 1rem;
  margin-bottom: 1.5rem;
}
.ov-card {
  background: linear-gradient(135deg, #ffffff, #f8fafc);
  border: 1px solid #e2e8f0;
  border-radius: 12px;
  padding: 1rem 1.25rem;
}
.ov-label {
  font-size: 0.8125rem;
  color: #64748b;
  margin-bottom: 0.4rem;
}
.ov-value {
  font-size: 1.5rem;
  font-weight: 800;
  color: var(--text-primary);
}
.block-title {
  font-size: 0.9375rem;
  font-weight: 700;
  color: #334155;
  margin: 1.25rem 0 0.75rem;
}
.metric-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 1rem;
}
.metric-card {
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 12px;
  padding: 1rem 1.25rem;
  text-align: center;
}
.metric-label {
  font-size: 0.8125rem;
  color: #64748b;
  margin-bottom: 0.4rem;
}
.metric-value {
  font-size: 1.75rem;
  font-weight: 800;
  font-family: 'SF Mono', 'Courier New', monospace;
}
.backtest-table {
  margin-top: 0.5rem;
}
.backtest-table .stock-table-header,
.backtest-table .stock-table-row {
  grid-template-columns: 1.4fr 1fr 1fr 1fr 1fr;
}
.bt-date {
  font-weight: 600;
  color: var(--text-primary);
}
@media (max-width: 640px) {
  .overview-grid, .metric-grid {
    grid-template-columns: 1fr;
  }
}
</style>
