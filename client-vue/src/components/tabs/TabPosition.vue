<template>
  <div>
    <div class="section-title">持仓管理与仓位配置</div>
    <div class="section-subtitle">记录持仓，自动诊断止损/止盈/减仓信号，按大盘环境给出总仓位建议——大赚小亏靠纪律</div>

    <!-- 操作指南 -->
    <div class="guide-box">
      <div class="guide-header" @click="guideOpen = !guideOpen">
        <span>📖 实战操作指南（两个Tab怎么用 · 四时段挂单 · 买卖纪律）</span>
        <span class="guide-toggle">{{ guideOpen ? '收起 ▲' : '展开 ▼' }}</span>
      </div>
      <div v-show="guideOpen" class="guide-body">
        <div class="guide-section">
          <div class="guide-title">① 看哪个Tab？</div>
          <div class="guide-item"><b>智能推荐</b>（主决策）：每日精选≤3只，已带买点/卖点/时机徽章/风控止损——<b>买它，并按卡片时机执行</b></div>
          <div class="guide-item"><b>股票筛选</b>（发现机会）：全市场盘中实时打分排序，用来扩大视野；看到高分票可先观察，等它进推荐榜或自行按同等纪律参与</div>
          <div class="guide-item"><b>持仓管理</b>（本页）：管已买入的票，止损止盈减仓一句话说清</div>
        </div>
        <div class="guide-section">
          <div class="guide-title">② 什么时候挂单？（按卡片「买入时机」徽章）</div>
          <table class="guide-table">
            <tr><th>徽章</th><th>时段</th><th>适合的票</th><th>挂法</th></tr>
            <tr><td>🔔 次日竞价</td><td>9:15–9:25</td><td>昨日涨停封板、强势龙头</td><td>9:20后挂竞价单；<b>高开5%以上不追</b>，没成交就撤</td></tr>
            <tr><td>⚡ 早盘买入</td><td>9:30–10:00</td><td>强势情绪股（龙头/先锋）</td><td>开盘回踩不破分时均价线再买，<b>不追直线拉升</b>；10点后不再新开早盘仓</td></tr>
            <tr><td>📉 回踩低吸</td><td>10:00–14:00</td><td>低位趋势股（中军/补涨）</td><td>按「买入参考」价挂低吸单等回踩，<b>跌破均价线或涨幅转负就撤单</b></td></tr>
            <tr><td>🌙 尾盘买入</td><td>14:30–15:00</td><td>高振幅/冲高回落/高位票</td><td>14:30后确认全天量价形态再买，<b>避开尾盘急拉追高</b></td></tr>
          </table>
        </div>
        <div class="guide-section">
          <div class="guide-title">③ 买卖纪律（大赚小亏的核心）</div>
          <div class="guide-item">💰 <b>买</b>：只按卡片「买入参考」价低吸或对应时段买；复盘132次证明现价追入胜率仅45%，低吸才有63%</div>
          <div class="guide-item">🎯 <b>盈</b>：到「目标卖点」且当日走弱→止盈；浮盈≥5%止损上移成本价，浮盈≥10%上移成本+5%（移动止盈，本页自动算）</div>
          <div class="guide-item">🛑 <b>亏</b>：现价跌破「止损价」（-6%~-12%，按振幅缩放）<b>无条件执行</b>，不抢反弹、不摊平</div>
          <div class="guide-item">⚖️ <b>仓位</b>：单票≤20%（高分龙头≤30%），总仓位跟随本页「建议总仓位」随大盘调整；持仓超5只或单票超30%会告警</div>
        </div>
      </div>
    </div>

    <!-- 交易环境提示（今日适不适合交易，60s刷新及时可见） -->
    <div v-if="data.envAdvice" class="env-banner" :class="'env-' + data.envAdvice.level">
      <div class="env-main">
        <span class="env-emoji">{{ data.envAdvice.emoji }}</span>
        <div class="env-text">
          <div class="env-title">
            今日交易环境：{{ data.envAdvice.title }}
            <small>大盘 {{ data.envAdvice.marketChange >= 0 ? '+' : '' }}{{ data.envAdvice.marketChange }}%<template v-if="data.envAdvice.openChange != null"> · 开盘 {{ data.envAdvice.openChange >= 0 ? '+' : '' }}{{ data.envAdvice.openChange }}%</template><template v-if="data.envAdvice.sentiment"> · 情绪{{ data.envAdvice.sentiment.cycle }}期({{ data.envAdvice.sentiment.cycleScore }}分)</template></small>
          </div>
          <div class="env-detail">{{ data.envAdvice.detail }}</div>
          <div v-if="data.envAdvice.notes?.length" class="env-notes">修正因素：{{ data.envAdvice.notes.join('；') }}</div>
        </div>
        <div class="env-pct">建议总仓位<br><b>{{ data.envAdvice.pct }}%</b></div>
      </div>
    </div>

    <!-- 组合总览 -->
    <div v-if="data.has_positions" class="portfolio-box">
      <div class="portfolio-row">
        <div class="portfolio-metric">
          <em>大盘涨跌</em>
          <b :style="{ color: data.market_change >= 0 ? 'var(--accent-danger)' : 'var(--accent-success)' }">{{ data.market_change >= 0 ? '+' : '' }}{{ data.market_change }}%</b>
        </div>
        <div class="portfolio-metric">
          <em>持仓总盈亏</em>
          <b :style="{ color: (data.portfolio?.total_pnl || 0) >= 0 ? 'var(--accent-danger)' : 'var(--accent-success)' }">
            {{ (data.portfolio?.total_pnl || 0) >= 0 ? '+' : '' }}{{ data.portfolio?.total_pnl }} 元（{{ (data.portfolio?.total_pnl_pct || 0) >= 0 ? '+' : '' }}{{ data.portfolio?.total_pnl_pct }}%）
          </b>
        </div>
      </div>
      <div v-if="data.portfolio?.warnings?.length" class="portfolio-warnings">
        <span v-for="w in data.portfolio.warnings" :key="w" class="warn-chip">⚠ {{ w }}</span>
      </div>
      <div class="portfolio-updated">诊断时间 {{ data.generated_at }} · 交易时段每60秒自动刷新</div>
    </div>

    <!-- 添加/编辑持仓 -->
    <div class="pos-form-box">
      <div class="form-title">{{ editingSymbol ? '✏️ 编辑持仓' : '➕ 添加持仓' }}</div>
      <div class="form-grid">
        <label class="form-field wide">
          <span class="form-label">股票（输代码或名称，支持模糊搜索）</span>
          <div class="combo-wrap">
            <input v-model="stockQuery" class="form-input" placeholder="如：美诺 / 603538 / sh600000"
                   @input="onStockQueryInput" @focus="onStockQueryInput">
            <div v-if="searchResults.length > 0" class="combo-dropdown">
              <div v-for="s in searchResults" :key="s.symbol" class="combo-item" @mousedown.prevent="selectStock(s)">
                <b class="combo-name">{{ s.name }}</b>
                <span class="combo-code">{{ s.symbol.toUpperCase() }}</span>
                <span v-if="s.price" class="combo-price">现价 {{ s.price }}</span>
              </div>
            </div>
            <div v-else-if="stockQuery && searchLoading" class="combo-dropdown">
              <div class="combo-item combo-searching">搜索中...</div>
            </div>
          </div>
        </label>
        <label class="form-field">
          <span class="form-label">成本价（元）</span>
          <input v-model="form.cost_price" type="number" step="0.01" min="0" placeholder="如 27.00" class="form-input">
        </label>
        <label class="form-field">
          <span class="form-label">持仓数（股，默认100）</span>
          <input v-model="form.shares" type="number" step="100" min="0" placeholder="100" class="form-input">
        </label>
        <div class="form-field">
          <span class="form-label">现价（自动获取）</span>
          <div class="form-input readonly" :style="{ color: priceColor }">{{ currentPrice ? currentPrice : '选择股票后自动显示' }}</div>
        </div>
        <div class="form-actions">
          <button class="btn-scan" @click="submitPosition" :disabled="saving">{{ saving ? '保存中...' : '保存' }}</button>
          <button v-if="editingSymbol" class="btn-secondary" @click="resetForm">取消</button>
        </div>
      </div>
    </div>

    <!-- 持仓列表 -->
    <div v-if="!data.has_positions" class="empty-box">📭 还没有持仓记录，先在上方添加一笔（或用智能推荐买入后记在这里）</div>
    <div v-else class="pos-table">
      <div class="pos-table-header">
        <span>名称</span><span>成本/现价</span><span>盈亏</span><span>止损价</span><span>目标价</span><span>信号</span><span>建议</span><span style="text-align:center;">操作</span>
      </div>
      <div v-for="p in data.positions" :key="p.symbol" class="pos-table-row">
        <span class="pos-name">
          <b>{{ p.name }}</b>
          <small>{{ p.symbol.toUpperCase() }}</small>
        </span>
        <span class="pos-price">
          {{ p.cost }}<br><b :style="{ color: p.price >= p.cost ? 'var(--accent-danger)' : 'var(--accent-success)' }">{{ p.price }}</b>
        </span>
        <span class="pos-pnl" :style="{ color: (p.pnl_pct || 0) >= 0 ? 'var(--accent-danger)' : 'var(--accent-success)' }">
          {{ (p.pnl_pct || 0) >= 0 ? '+' : '' }}{{ p.pnl_pct }}%<br><small>{{ (p.pnl_amt || 0) >= 0 ? '+' : '' }}{{ p.pnl_amt }}元</small>
        </span>
        <span class="pos-stop">{{ p.stop_price ?? '--' }}<small v-if="p.trailing?.enabled">移动线{{ p.trailing.line }}</small></span>
        <span class="pos-target">{{ p.target_price ?? '--' }}</span>
        <span class="pos-flags"><span v-for="f in p.flags" :key="f" class="flag-chip">{{ f }}</span><span v-if="!p.flags?.length" class="flag-chip ok">无异动</span></span>
        <span class="pos-action">
          <span class="action-badge" :class="'action-' + p.action">{{ p.action_label }}</span>
          <small class="pos-detail">{{ p.detail }}</small>
        </span>
        <span class="pos-ops">
          <button class="op-btn" @click="editPosition(p)">编辑</button>
          <button class="op-btn danger" @click="removePosition(p)">删除</button>
        </span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, inject, onMounted, onUnmounted } from 'vue'
import { apiFetch, isTradingTime, getNextRefreshDelay } from '../../utils/helpers'
import { usePolling } from '../../composables/usePolling'

// 轮询治理（2026-10-13）：持仓 60s 价格刷新只在 Tab 可见时跑
const props = defineProps({ active: { type: Boolean, default: true } })

const showNotification = inject('showNotification', () => {})

const data = ref({ has_positions: false, positions: [], portfolio: null, market_change: null })
const guideOpen = ref(true)
const saving = ref(false)
const editingSymbol = ref('')
const form = ref({ symbol: '', name: '', cost_price: '', shares: '100' })
// 股票智能搜索（2026-10-12：名称→代码反查 + 模糊匹配）
const stockQuery = ref('')
const searchResults = ref([])
const searchLoading = ref(false)
const currentPrice = ref(null)
let searchTimer = null
// 交易时段 60s 自动刷新（"及时建议"）；2026-10-13 起仅在 Tab 可见时轮询
const { start: startPositionPolling } = usePolling(
  () => loadPositions(),
  {
    interval: () => (isTradingTime() ? 60000 : getNextRefreshDelay()),
    active: () => props.active,
    immediate: false,
  }
)

const priceColor = computed(() => {
  const p = parseFloat(currentPrice.value)
  const c = parseFloat(form.value.cost_price)
  if (!p || !c) return 'var(--text-primary)'
  return p >= c ? 'var(--accent-danger)' : 'var(--accent-success)'
})

async function loadPositions() {
  try {
    const result = await apiFetch('/api/positions')
    if (result.success) data.value = result.data
  } catch (e) { console.error('加载持仓失败:', e) }
}

function onStockQueryInput() {
  clearTimeout(searchTimer)
  const q = stockQuery.value.trim()
  if (!q) { searchResults.value = []; return }
  searchTimer = setTimeout(async () => {
    searchLoading.value = true
    try {
      const r = await apiFetch(`/api/stock-search?q=${encodeURIComponent(q)}`)
      searchResults.value = (r.success && r.data) ? r.data : []
    } catch (e) { searchResults.value = [] }
    searchLoading.value = false
  }, 300)
}

function selectStock(s) {
  form.value.symbol = s.symbol
  form.value.name = s.name
  stockQuery.value = `${s.name}（${s.symbol.toUpperCase()}）`
  currentPrice.value = s.price || null
  searchResults.value = []
}

function onDocClick(e) {
  if (!e.target.closest('.combo-wrap')) searchResults.value = []
}

async function submitPosition() {
  if (!stockQuery.value || !form.value.cost_price) {
    showNotification('请先选择股票并填写成本价', 'warning')
    return
  }
  saving.value = true
  try {
    const result = await apiFetch('/api/positions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        symbol: form.value.symbol || stockQuery.value,
        name: form.value.name,
        cost_price: parseFloat(form.value.cost_price),
        shares: parseInt(form.value.shares) || 100,
        buy_date: new Date().toISOString().slice(0, 10)
      })
    })
    if (result.success) {
      showNotification(`持仓已保存：${result.data?.name || ''}`, 'success')
      resetForm()
      await loadPositions()
    } else {
      // 后端明确错误（代码格式/名称未匹配等）直接展示，不再"未知错误"
      const detail = typeof result.detail === 'string' ? result.detail
        : (Array.isArray(result.detail) ? '请求格式有误，请检查输入' : (result.error || '未知错误'))
      showNotification('保存失败：' + detail, 'error')
    }
  } catch (e) { showNotification('保存失败：网络或服务异常', 'error') }
  saving.value = false
}

function editPosition(p) {
  editingSymbol.value = p.symbol
  form.value = { symbol: p.symbol, name: p.name, cost_price: p.cost, shares: p.shares }
  stockQuery.value = `${p.name}（${p.symbol.toUpperCase()}）`
  currentPrice.value = p.price || null
}

function resetForm() {
  editingSymbol.value = ''
  form.value = { symbol: '', name: '', cost_price: '', shares: '100' }
  stockQuery.value = ''
  currentPrice.value = null
  searchResults.value = []
}

async function removePosition(p) {
  if (!confirm(`确认删除持仓 ${p.name}(${p.symbol.toUpperCase()})？`)) return
  try {
    const result = await apiFetch(`/api/positions/${p.symbol}`, { method: 'DELETE' })
    if (result.success) {
      showNotification('已删除', 'success')
      await loadPositions()
    }
  } catch (e) { showNotification('删除失败', 'error') }
}

onMounted(() => {
  loadPositions()
  document.addEventListener('click', onDocClick)
  startPositionPolling()
})

onUnmounted(() => {
  document.removeEventListener('click', onDocClick)
})

// 切换登录账号后，由 App 通过 ref 调用以立刻重载（否则会残留上一个账号的持仓）
defineExpose({ loadPositions })
</script>

<style scoped>
/* 交易环境横幅（2026-10-12） */
.env-banner {
  margin-bottom: 1rem;
  border-radius: 12px
  ;padding: 0.875rem 1rem;
  border: 1px solid;
}
.env-main { display: flex; align-items: center; gap: 0.875rem; }
.env-emoji { font-size: 1.75rem; flex-shrink: 0; }
.env-text { flex: 1; min-width: 0; }
.env-title { font-size: 1rem; font-weight: 800; margin-bottom: 0.25rem; }
.env-title small { font-weight: 400; font-size: 0.75rem; color: #64748b; margin-left: 0.5rem; }
.env-detail { font-size: 0.8125rem; line-height: 1.6; }
.env-notes { font-size: 0.75rem; color: #64748b; margin-top: 0.25rem; }
.env-pct {
  flex-shrink: 0;
  text-align: center;
  font-size: 0.6875rem;
  color: #64748b;
  padding-left: 0.875rem;
  border-left: 1px dashed rgba(100, 116, 139, 0.3);
}
.env-pct b { font-size: 1.375rem; }
.env-aggressive { background: #fef2f2; border-color: #fecaca; }
.env-aggressive .env-title, .env-aggressive .env-pct b { color: #b91c1c; }
.env-normal { background: #eff6ff; border-color: #bfdbfe; }
.env-normal .env-title, .env-normal .env-pct b { color: #1d4ed8; }
.env-cautious { background: #fff7ed; border-color: #fed7aa; }
.env-cautious .env-title, .env-cautious .env-pct b { color: #c2410c; }
.env-defensive { background: #fefce8; border-color: #fde68a; }
.env-defensive .env-title, .env-defensive .env-pct b { color: #a16207; }
.env-avoid { background: #fdf2f8; border-color: #fbcfe8; }
.env-avoid .env-title, .env-avoid .env-pct b { color: #be185d; }
.guide-box {
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 12px;
  margin-bottom: 1rem;
  overflow: hidden;
}
.guide-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0.875rem 1rem;
  cursor: pointer;
  font-weight: 700;
  font-size: 0.9375rem;
  color: #334155;
  user-select: none;
}
.guide-toggle { font-size: 0.75rem; color: #94a3b8; font-weight: 400; }
.guide-body { padding: 0 1rem 1rem; }
.guide-section { margin-bottom: 0.875rem; }
.guide-title { font-size: 0.875rem; font-weight: 700; color: #0f172a; margin-bottom: 0.375rem; }
.guide-item { font-size: 0.8125rem; color: #475569; line-height: 1.7; }
.guide-table { width: 100%; border-collapse: collapse; font-size: 0.75rem; }
.guide-table th, .guide-table td {
  border: 1px solid #e2e8f0;
  padding: 0.375rem 0.5rem;
  text-align: left;
  color: #475569;
  line-height: 1.5;
}
.guide-table th { background: #f1f5f9; font-weight: 700; color: #334155; }
.portfolio-box {
  background: linear-gradient(135deg, #eff6ff 0%, #dbeafe 100%);
  border: 1px solid #bfdbfe;
  border-radius: 12px;
  padding: 1rem;
  margin-bottom: 1rem;
}
.portfolio-row { display: flex; flex-wrap: wrap; gap: 1.5rem; }
.portfolio-metric { display: flex; flex-direction: column; }
.portfolio-metric em { font-style: normal; font-size: 0.75rem; color: #64748b; margin-bottom: 0.125rem; }
.portfolio-metric b { font-size: 1.125rem; }
.portfolio-metric small { font-size: 0.6875rem; color: #64748b; margin-top: 0.125rem; }
.portfolio-warnings { margin-top: 0.625rem; display: flex; flex-wrap: wrap; gap: 0.375rem; }
.warn-chip {
  font-size: 0.75rem;
  color: #b45309;
  background: #fef3c7;
  border: 1px solid #fde68a;
  border-radius: 999px;
  padding: 0.125rem 0.5rem;
}
.portfolio-updated { margin-top: 0.5rem; font-size: 0.6875rem; color: #94a3b8; }
.pos-form-box {
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 12px;
  padding: 0.875rem 1rem;
  margin-bottom: 1rem;
}
.form-title { font-size: 0.875rem; font-weight: 700; color: #334155; margin-bottom: 0.5rem; }
.form-grid {
  display: grid;
  grid-template-columns: 2fr 1fr 1fr 1fr auto;
  gap: 0.625rem;
  align-items: end;
}
.form-field { display: flex; flex-direction: column; gap: 0.25rem; }
.form-field.wide { grid-column: span 1; }
.form-label { font-size: 0.75rem; font-weight: 600; color: #64748b; }
.form-input {
  padding: 0.5rem 0.75rem;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  font-size: 0.875rem;
  background: #fff;
  width: 100%;
  box-sizing: border-box;
}
.form-input:focus { outline: none; border-color: #3b82f6; }
.form-input.readonly {
  background: #f1f5f9;
  color: #64748b;
  font-weight: 700;
  min-height: 35px;
  display: flex;
  align-items: center;
}
/* 股票智能搜索 combobox */
.combo-wrap { position: relative; }
.combo-dropdown {
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  right: 0;
  background: #fff;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  box-shadow: 0 8px 24px rgba(15, 23, 42, 0.12);
  z-index: 20;
  max-height: 260px;
  overflow-y: auto;
}
.combo-item {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
  padding: 0.5rem 0.75rem;
  cursor: pointer;
  font-size: 0.8125rem;
}
.combo-item:hover { background: #f1f5f9; }
.combo-item.combo-searching { color: #94a3b8; cursor: default; }
.combo-name { color: var(--text-primary); }
.combo-code { color: #94a3b8; font-size: 0.75rem; }
.combo-price { margin-left: auto; color: var(--accent-danger); font-size: 0.75rem; font-weight: 700; }
.form-actions { display: flex; gap: 0.375rem; }
@media (max-width: 900px) {
  .form-grid { grid-template-columns: 1fr 1fr; }
}
.empty-box {
  text-align: center;
  padding: 3rem 2rem;
  color: #94a3b8;
  background: #f8fafc;
  border-radius: 16px;
  border: 2px dashed #e2e8f0;
}
.pos-table { display: flex; flex-direction: column; gap: 0.5rem; }
.pos-table-header, .pos-table-row {
  display: grid;
  grid-template-columns: 1.1fr 0.9fr 1fr 0.9fr 0.8fr 1.2fr 2fr 0.9fr;
  gap: 0.5rem;
  align-items: center;
  font-size: 0.8125rem;
}
.pos-table-header {
  font-weight: 700;
  color: #64748b;
  padding: 0.5rem 0.75rem;
  background: #f1f5f9;
  border-radius: 8px;
}
.pos-table-row {
  padding: 0.75rem;
  background: #fff;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
}
.pos-name b { display: block; color: var(--text-primary); }
.pos-name small { color: var(--text-tertiary); font-size: 0.6875rem; }
.pos-price small, .pos-pnl small, .pos-stop small { font-size: 0.6875rem; color: #94a3b8; }
.pos-stop { color: var(--accent-success); font-weight: 700; }
.pos-target { color: var(--accent-danger); font-weight: 700; }
.pos-flags { display: flex; flex-wrap: wrap; gap: 0.25rem; }
.flag-chip {
  font-size: 0.6875rem;
  padding: 0.0625rem 0.375rem;
  border-radius: 999px;
  background: #fef2f2;
  color: #b91c1c;
  border: 1px solid #fecaca;
}
.flag-chip.ok { background: #f0fdf4; color: #15803d; border-color: #bbf7d0; }
.pos-action { display: flex; flex-direction: column; gap: 0.25rem; }
.action-badge {
  display: inline-block;
  width: fit-content;
  font-size: 0.75rem;
  font-weight: 700;
  padding: 0.1875rem 0.5rem;
  border-radius: 6px;
  border: 1px solid transparent;
}
.action-stop_loss { background: #fef2f2; color: #b91c1c; border-color: #fecaca; }
.action-take_profit { background: #fff1f2; color: #be123c; border-color: #fecdd3; }
.action-reduce { background: #fff7ed; color: #c2410c; border-color: #fed7aa; }
/* 冲高回落：比「减仓回避」轻一档、比「持有观察」重一档，用琥珀黄，颜色与行动台徽章保持一致 */
.action-giveback { background: #fef9c3; color: #854d0e; border-color: #fde047; }
.action-hold_trailing { background: #eff6ff; color: #1d4ed8; border-color: #bfdbfe; }
.action-hold { background: #f8fafc; color: #475569; border-color: #e2e8f0; }
.action-watch { background: #fefce8; color: #a16207; border-color: #fde68a; }
.pos-detail { font-size: 0.6875rem; color: #64748b; line-height: 1.4; }
.pos-ops { display: flex; gap: 0.25rem; justify-content: center; }
.op-btn {
  font-size: 0.75rem;
  padding: 0.25rem 0.5rem;
  border: 1px solid #cbd5e1;
  background: #fff;
  border-radius: 6px;
  cursor: pointer;
  color: #475569;
}
.op-btn:hover { background: #f1f5f9; }
.op-btn.danger { color: #b91c1c; border-color: #fecaca; }
.op-btn.danger:hover { background: #fef2f2; }
/* 窄窗口治理（2026-10-13）
   原实现在 ≤900px 时把表头 display:none、把行压成两列 —— 屏幕窄了不等于数据可以没有标题，
   那串无标题数字对上班族是最容易看错的一种（止损价和目标价长得几乎一样）。
   现在：900px 以下用横向滚动保留完整表格；只有窄到手机级（≤620px）才转成卡片，
   卡片里用 ::before 把每一格的列标题补回来。 */
@media (max-width: 980px) {
  .pos-table {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    padding-bottom: 4px;
  }
  .pos-table-header,
  .pos-table-row { min-width: 760px; }
}

@media (max-width: 620px) {
  .pos-table { overflow-x: visible; }
  .pos-table-header { display: none; }
  .pos-table-row {
    grid-template-columns: 1fr 1fr;
    min-width: 0;
    gap: 0.625rem 0.75rem;
  }
  .pos-table-row > span { display: flex; flex-direction: column; gap: 2px; }
  .pos-table-row > span::before {
    font-size: 0.6875rem;
    color: #94a3b8;
    letter-spacing: .02em;
    margin-bottom: 1px;
  }
  .pos-name::before { content: '名称'; }
  .pos-price::before { content: '成本 / 现价'; }
  .pos-pnl::before { content: '盈亏'; }
  .pos-stop::before { content: '止损价'; }
  .pos-target::before { content: '目标价'; }
  .pos-flags::before { content: '信号'; }
  .pos-action::before { content: '建议'; }
  .pos-ops::before { content: '操作'; }
  .pos-name, .pos-action, .pos-ops { grid-column: 1 / -1; }
  .pos-ops { flex-direction: row; }
}
</style>
