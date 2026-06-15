<template>
  <div>
    <div class="section-title">AI智能选股推荐</div>
    <div class="section-subtitle">基于综合评分模型，每日精选3支最具上涨潜力的股票，附精准买卖点提示</div>
    <div class="scan-controls" style="background:#f8fafc;padding:20px;border-radius:12px;border:1px solid #e2e8f0;margin-bottom:24px;">
      <button class="btn-scan" style="padding:12px 24px;display:flex;align-items:center;gap:8px;" @click="generateRecommendations" :disabled="generating">
        <span>🎯</span> {{ generating ? '生成中...' : '生成今日推荐' }}
      </button>
      <button v-if="todayRecommend.length > 0" class="btn-secondary" style="padding:12px 24px;" @click="saveRecommendations">💾 保存推荐记录</button>
    </div>
    <div class="refresh-controls">
      <div class="toggle-section"><span class="toggle-label">推荐成功率</span></div>
      <div class="last-update">{{ recommendStats }}</div>
    </div>
    <div class="recommend-section">
      <div class="section-header">
        <div class="section-subtitle-small">今日推荐股票</div>
        <span class="section-stats">{{ todayDate }}</span>
      </div>
      <div v-if="todayRecommend.length === 0" style="text-align:center;padding:4rem 2rem;color:#94a3b8;background:#f8fafc;border-radius:16px;border:2px dashed #e2e8f0;margin:1rem 0;">✨ 准备就绪，点击上方按钮生成今日潜力股</div>
      <div v-for="(r, idx) in todayRecommend" :key="r.symbol" :class="['recommend-card', `top-${idx + 1}`]">
        <div class="recommend-header">
          <div class="recommend-rank-badge">{{ idx === 0 ? '🥇' : idx === 1 ? '🥈' : '🥉' }}</div>
          <div class="recommend-info-main">
            <div class="recommend-name-row">
              <span class="recommend-name-text">{{ r.name }}</span>
              <span class="recommend-code-text">{{ r.symbol.toUpperCase() }}</span>
            </div>
          </div>
          <div class="recommend-price-large" :style="{ color: r.change >= 0 ? 'var(--accent-danger)' : 'var(--accent-success)' }">{{ r.price }}</div>
        </div>
        <div class="recommend-stats-grid">
          <div class="recommend-stat-box">
            <span class="recommend-stat-label">今日涨跌</span>
            <span class="recommend-stat-value" :style="{ color: r.change >= 0 ? 'var(--accent-danger)' : 'var(--accent-success)' }">{{ r.change >= 0 ? '+' : '' }}{{ r.changePercent?.toFixed(2) || 0 }}%</span>
          </div>
          <div class="recommend-stat-box">
            <span class="recommend-stat-label">AI 综合评分</span>
            <span class="recommend-stat-value" :style="{ color: r.score >= 70 ? 'var(--accent-danger)' : r.score >= 50 ? 'var(--accent-warning)' : 'var(--accent-success)' }">{{ r.score }} 分</span>
          </div>
        </div>
        <div v-if="r.buySellPoints || r.buyPoint" class="recommend-points-row">
          <div class="recommend-point-card buy">
            <div class="recommend-point-label">买入参考</div>
            <div class="recommend-point-value" style="color:var(--accent-success)">{{ r.buySellPoints?.buy || r.buyPoint }}</div>
          </div>
          <div class="recommend-point-card current">
            <div class="recommend-point-label">推荐时价</div>
            <div class="recommend-point-value" style="color:var(--accent-primary)">{{ r.buySellPoints?.current || r.price }}</div>
          </div>
          <div class="recommend-point-card sell">
            <div class="recommend-point-label">目标卖点</div>
            <div class="recommend-point-value" style="color:var(--accent-danger)">{{ r.buySellPoints?.sell || r.sellPoint }}</div>
          </div>
        </div>
        <div class="recommend-reason-box">
          <div class="recommend-reason-text">{{ r.reason }}</div>
        </div>
      </div>
    </div>
    <div class="history-section">
      <div class="section-subtitle-small">历史推荐记录</div>
      <div v-if="historyRecommend.length === 0" style="text-align:center;padding:2rem;color:#64748b;">暂无历史记录</div>
      <div v-for="record in pagedHistoryRecommend" :key="record.date" class="history-item">
        <div class="history-date-header">📅 {{ record.date }}</div>
        <div class="history-stock-grid">
          <div v-for="r in record.daily_recommendations || record.stocks" :key="r.symbol" class="history-stock-card">
            <div class="history-stock-name-row">
              <div>
                <div style="font-weight:700;font-size:0.95rem;color:var(--text-primary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100px;">{{ r.name }}</div>
                <div style="font-size:0.7rem;color:var(--text-tertiary);">{{ r.symbol?.toUpperCase() }}</div>
              </div>
              <div class="history-profit-badge" :class="getProfitClass(r)">{{ getProfitText(r) }}</div>
            </div>
            <div class="history-price-table">
              <span class="history-price-label">买入</span>
              <span class="history-price-val" style="color:var(--accent-danger)">{{ r.buySellPoints?.buy || r.buyPoint || '--' }}</span>
              <span class="history-price-label">现价</span>
              <span class="history-price-val">{{ r.currentPrice || r.price || '--' }}</span>
              <span class="history-price-label">盈利</span>
              <span class="history-price-val" :style="{ color: getProfitValue(r) >= 0 ? 'var(--accent-danger)' : 'var(--accent-success)' }">{{ getProfitText(r) }}</span>
            </div>
          </div>
        </div>
      </div>
      <div v-if="historyTotalPages > 1" class="history-pagination">
        <button class="page-btn" :disabled="historyPage <= 1" @click="historyPage--">上一页</button>
        <span class="page-info">第 {{ historyPage }} / {{ historyTotalPages }} 页</span>
        <button class="page-btn" :disabled="historyPage >= historyTotalPages" @click="historyPage++">下一页</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, inject } from 'vue'
import { apiFetch } from '../../utils/helpers'

const showNotification = inject('showNotification')
const todayRecommend = ref([])
const historyRecommend = ref([])
const todayDate = ref('--')
const recommendStats = ref('--')
const generating = ref(false)

const HISTORY_PAGE_SIZE = 3
const historyPage = ref(1)
const historyTotalPages = computed(() => Math.ceil(historyRecommend.value.length / HISTORY_PAGE_SIZE))
const pagedHistoryRecommend = computed(() => {
  const start = (historyPage.value - 1) * HISTORY_PAGE_SIZE
  return historyRecommend.value.slice(start, start + HISTORY_PAGE_SIZE)
})

function getProfitClass(r) {
  const val = r.profitRatio ?? r.result
  if (val === null || val === undefined) return ''
  return parseFloat(val) >= 0 ? 'up' : 'down'
}

function getProfitText(r) {
  const val = r.profitRatio ?? r.result
  if (val === null || val === undefined) return '--'
  const num = parseFloat(val)
  return (num >= 0 ? '+' : '') + num.toFixed(2) + '%'
}

function getProfitValue(r) {
  const val = r.profitRatio ?? r.result
  if (val === null || val === undefined) return 0
  return parseFloat(val)
}

async function generateRecommendations() {
  generating.value = true
  try {
    const result = await apiFetch('/api/generate-recommendations', { method: 'POST' })
    if (result.success) {
      todayRecommend.value = result.recommendations || []
      todayDate.value = new Date().toLocaleDateString('zh-CN')
      showNotification('推荐生成成功')
    } else {
      showNotification(result.error || '生成失败')
    }
  } catch (e) {
    showNotification('生成推荐失败')
  } finally {
    generating.value = false
  }
}

async function saveRecommendations() {
  if (todayRecommend.value.length === 0) {
    showNotification('请先生成推荐')
    return
  }
  try {
    const dateKey = new Date().toISOString().split('T')[0]
    const record = {
      date: dateKey,
      daily_recommendations: todayRecommend.value.map(r => ({
        name: r.name, symbol: r.symbol, price: r.price,
        change: r.change, changePercent: r.changePercent,
        score: r.score, buySellPoints: r.buySellPoints || { buy: r.buyPoint, current: r.price, sell: r.sellPoint },
        reason: r.reason
      }))
    }
    const result = await apiFetch('/api/daily_recommendations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(record)
    })
    if (result.success) {
      showNotification('保存成功！推荐记录已保存到数据库')
      await loadRecommendations()
    } else {
      showNotification('保存失败：' + (result.error || '未知错误'))
    }
  } catch (e) { showNotification('保存失败') }
}

async function loadRecommendations() {
  try {
    const result = await apiFetch('/api/daily_recommendations')
    if (result.success && result.data) {
      const today = result.data.find(d => d.isToday)
      if (today) {
        todayRecommend.value = today.stocks || today.daily_recommendations || []
        todayDate.value = today.date
      }
      historyRecommend.value = result.data.filter(d => !d.isToday)
    }
  } catch (e) { console.error('加载推荐失败:', e) }
}

onMounted(() => {
  loadRecommendations()
})
</script>
