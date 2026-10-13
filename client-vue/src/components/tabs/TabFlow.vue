<template>
  <div>
    <!-- 控制条 -->
    <div class="refresh-controls">
      <div class="toggle-section"><span class="toggle-label">📊 资金流向 · {{ statusText }}</span></div>
      <div style="display:flex; gap:0.5rem; align-items:center;">
        <button class="btn-secondary" @click="refresh" :disabled="loading">手动刷新</button>
        <div class="last-update">{{ lastUpdateTime }}</div>
      </div>
    </div>

    <div v-if="loading && !data" class="scan-progress">
      <div class="loading-spinner" style="width:20px;height:20px;border-width:2px;margin:0;"></div>
      正在拉取资金流向数据...
    </div>
    <div v-if="error" class="scan-summary"><div class="scan-stat bear">{{ error }}</div></div>

    <template v-if="data">
      <!-- 区块1：个股主力净流入/净流出 -->
      <h3 class="section-title">个股主力净流入 / 净流出排名</h3>
      <p class="section-subtitle">全市场沪深A股，按主力净流入（元）排序 · 红=流入 / 绿=流出 · 点击行查看行情 · 每页 {{ PAGE_SIZE }} 条</p>
      <div class="flow-grid">
        <div class="flow-col">
          <div class="flow-col-title inflow">🔴 主力净流入排名</div>
          <div class="stock-table">
            <div class="stock-table-header" :style="gridStock">
              <span>名称</span><span>现价</span><span>涨跌幅</span><span>主力净流入</span><span>行业</span>
            </div>
            <div v-for="r in paged(data.stocks.inflow, 'stockIn')" :key="r.symbol" class="stock-table-row clickable" :style="gridStock" @click="goQuote(r.symbol)">
              <span class="name">{{ r.name }}<br><small class="muted">{{ r.code }}</small></span>
              <span class="price" :style="c(r.changePercent)">{{ r.price.toFixed(2) }}</span>
              <span :style="c(r.changePercent)">{{ sign(r.changePercent) }}{{ r.changePercent.toFixed(2) }}%</span>
              <span :style="moneyColor(r.mainNetInflow)">{{ formatMoney(r.mainNetInflow) }}</span>
              <span class="muted">{{ r.industry || '—' }}</span>
            </div>
            <div v-if="!data.stocks.inflow.length" class="empty-row">暂无数据</div>
          </div>
          <Pagination v-model="pageState.stockIn" :total="data.stocks.inflow.length" :total-pages="totalPages(data.stocks.inflow)" />
        </div>
        <div class="flow-col">
          <div class="flow-col-title outflow">🟢 主力净流出排名</div>
          <div class="stock-table">
            <div class="stock-table-header" :style="gridStock">
              <span>名称</span><span>现价</span><span>涨跌幅</span><span>主力净流入</span><span>行业</span>
            </div>
            <div v-for="r in paged(data.stocks.outflow, 'stockOut')" :key="r.symbol" class="stock-table-row clickable" :style="gridStock" @click="goQuote(r.symbol)">
              <span class="name">{{ r.name }}<br><small class="muted">{{ r.code }}</small></span>
              <span class="price" :style="c(r.changePercent)">{{ r.price.toFixed(2) }}</span>
              <span :style="c(r.changePercent)">{{ sign(r.changePercent) }}{{ r.changePercent.toFixed(2) }}%</span>
              <span :style="moneyColor(r.mainNetInflow)">{{ formatMoney(r.mainNetInflow) }}</span>
              <span class="muted">{{ r.industry || '—' }}</span>
            </div>
            <div v-if="!data.stocks.outflow.length" class="empty-row">暂无数据</div>
          </div>
          <Pagination v-model="pageState.stockOut" :total="data.stocks.outflow.length" :total-pages="totalPages(data.stocks.outflow)" />
        </div>
      </div>

      <!-- 区块2：行业板块资金流（净流入左 / 净流出右，同一行） -->
      <h3 class="section-title">行业板块净流入 / 净流出排名</h3>
      <p class="section-subtitle">行业板块，按主力净流入（元）排序 · 红=流入 / 绿=流出 · 每页 {{ PAGE_SIZE }} 条</p>
      <div class="flow-grid">
        <div class="flow-col">
          <div class="flow-col-title inflow">🔴 行业板块净流入排名</div>
          <div class="stock-table">
            <div class="stock-table-header" :style="gridSector">
              <span>板块</span><span>涨跌幅</span><span>主力净流入</span><span>上涨/下跌</span>
            </div>
            <div v-for="s in paged(data.sectors.industry.inflow, 'indIn')" :key="'in'+s.code" class="stock-table-row" :style="gridSector">
              <span class="name">{{ s.name }}</span>
              <span :style="c(s.changePercent)">{{ sign(s.changePercent) }}{{ s.changePercent.toFixed(2) }}%</span>
              <span :style="moneyColor(s.mainNetInflow)">{{ formatMoney(s.mainNetInflow) }}</span>
              <span class="muted">{{ s.riseCount }}/{{ s.fallCount }}</span>
            </div>
            <div v-if="!data.sectors.industry.inflow.length" class="empty-row">暂无数据</div>
          </div>
          <Pagination v-model="pageState.indIn" :total="data.sectors.industry.inflow.length" :total-pages="totalPages(data.sectors.industry.inflow)" />
        </div>
        <div class="flow-col">
          <div class="flow-col-title outflow">🟢 行业板块净流出排名</div>
          <div class="stock-table">
            <div class="stock-table-header" :style="gridSector">
              <span>板块</span><span>涨跌幅</span><span>主力净流入</span><span>上涨/下跌</span>
            </div>
            <div v-for="s in paged(data.sectors.industry.outflow, 'indOut')" :key="'out'+s.code" class="stock-table-row" :style="gridSector">
              <span class="name">{{ s.name }}</span>
              <span :style="c(s.changePercent)">{{ sign(s.changePercent) }}{{ s.changePercent.toFixed(2) }}%</span>
              <span :style="moneyColor(s.mainNetInflow)">{{ formatMoney(s.mainNetInflow) }}</span>
              <span class="muted">{{ s.riseCount }}/{{ s.fallCount }}</span>
            </div>
            <div v-if="!data.sectors.industry.outflow.length" class="empty-row">暂无数据</div>
          </div>
          <Pagination v-model="pageState.indOut" :total="data.sectors.industry.outflow.length" :total-pages="totalPages(data.sectors.industry.outflow)" />
        </div>
      </div>

      <!-- 区块3：概念板块资金流（净流入左 / 净流出右，同一行） -->
      <h3 class="section-title">概念板块净流入 / 净流出排名</h3>
      <p class="section-subtitle">概念板块，按主力净流入（元）排序 · 红=流入 / 绿=流出 · 每页 {{ PAGE_SIZE }} 条</p>
      <div class="flow-grid">
        <div class="flow-col">
          <div class="flow-col-title inflow">🔴 概念板块净流入排名</div>
          <div class="stock-table">
            <div class="stock-table-header" :style="gridSector">
              <span>板块</span><span>涨跌幅</span><span>主力净流入</span><span>上涨/下跌</span>
            </div>
            <div v-for="s in paged(data.sectors.concept.inflow, 'conIn')" :key="'cin'+s.code" class="stock-table-row" :style="gridSector">
              <span class="name">{{ s.name }}</span>
              <span :style="c(s.changePercent)">{{ sign(s.changePercent) }}{{ s.changePercent.toFixed(2) }}%</span>
              <span :style="moneyColor(s.mainNetInflow)">{{ formatMoney(s.mainNetInflow) }}</span>
              <span class="muted">{{ s.riseCount }}/{{ s.fallCount }}</span>
            </div>
            <div v-if="!data.sectors.concept.inflow.length" class="empty-row">暂无数据</div>
          </div>
          <Pagination v-model="pageState.conIn" :total="data.sectors.concept.inflow.length" :total-pages="totalPages(data.sectors.concept.inflow)" />
        </div>
        <div class="flow-col">
          <div class="flow-col-title outflow">🟢 概念板块净流出排名</div>
          <div class="stock-table">
            <div class="stock-table-header" :style="gridSector">
              <span>板块</span><span>涨跌幅</span><span>主力净流入</span><span>上涨/下跌</span>
            </div>
            <div v-for="s in paged(data.sectors.concept.outflow, 'conOut')" :key="'cout'+s.code" class="stock-table-row" :style="gridSector">
              <span class="name">{{ s.name }}</span>
              <span :style="c(s.changePercent)">{{ sign(s.changePercent) }}{{ s.changePercent.toFixed(2) }}%</span>
              <span :style="moneyColor(s.mainNetInflow)">{{ formatMoney(s.mainNetInflow) }}</span>
              <span class="muted">{{ s.riseCount }}/{{ s.fallCount }}</span>
            </div>
            <div v-if="!data.sectors.concept.outflow.length" class="empty-row">暂无数据</div>
          </div>
          <Pagination v-model="pageState.conOut" :total="data.sectors.concept.outflow.length" :total-pages="totalPages(data.sectors.concept.outflow)" />
        </div>
      </div>

      <!-- 区块3：非热点上涨股 -->
      <h3 class="section-title">非近期热点 · 上涨不错的票</h3>
      <p class="section-subtitle">
        筛选逻辑：全市场涨幅榜剔除「热搜榜前 {{ hiddenHotTopN }} 名（近期热点）」与 ST/退市，不套用现有选股评分规则 · 每页 {{ PAGE_SIZE }} 条
      </p>
      <div class="filter-bar">
        <label class="filter-label">最低涨幅(%)：</label>
        <input v-model.number="hiddenChangeThreshold" type="number" step="0.5" min="0" class="filter-input" />
        <label class="filter-label">排除热搜前</label>
        <input v-model.number="hiddenHotTopN" type="number" min="0" class="filter-input" style="width:4rem;" />
        <label class="filter-label">名</label>
        <button class="btn-secondary" @click="refresh">应用并刷新</button>
        <span class="filter-info">共 {{ data.hiddenGems.length }} 只</span>
      </div>
      <div class="stock-table">
        <div class="stock-table-header" :style="gridStock">
          <span>名称</span><span>现价</span><span>涨跌幅</span><span>主力净流入</span><span>行业</span>
        </div>
        <div v-for="r in paged(data.hiddenGems, 'hidden')" :key="r.symbol" class="stock-table-row clickable" :style="gridStock" @click="goQuote(r.symbol)">
          <span class="name">{{ r.name }}<br><small class="muted">{{ r.code }}</small></span>
          <span class="price" :style="c(r.changePercent)">{{ r.price.toFixed(2) }}</span>
          <span :style="c(r.changePercent)">{{ sign(r.changePercent) }}{{ r.changePercent.toFixed(2) }}%</span>
          <span :style="moneyColor(r.mainNetInflow)">{{ formatMoney(r.mainNetInflow) }}</span>
          <span class="muted">{{ r.industry || '—' }}</span>
        </div>
        <div v-if="!data.hiddenGems.length" class="empty-row">暂无符合条件股票（可下调涨幅阈值或排除排名）</div>
      </div>
      <Pagination v-model="pageState.hidden" :total="data.hiddenGems.length" :total-pages="totalPages(data.hiddenGems)" />
    </template>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onUnmounted, inject } from 'vue'
import Pagination from '../Pagination.vue'
import { apiFetch, isTradingTime, getNextRefreshDelay, getTradingStatusText, formatNowTime } from '../../utils/helpers'

const showNotification = inject('showNotification')
const navigateToQuote = inject('navigateToQuote')

const data = ref(null)
const loading = ref(false)
const error = ref('')
const lastUpdateTime = ref('--:--:--')
const statusText = ref(getTradingStatusText())

const hiddenChangeThreshold = ref(3.0)
const hiddenHotTopN = ref(30)

// 分页：每种类型默认每页显示 10 条
const PAGE_SIZE = 10
const pageState = reactive({
  stockIn: 1, stockOut: 1,
  indIn: 1, indOut: 1,
  conIn: 1, conOut: 1,
  hidden: 1,
})

// 覆盖全局 stock-table 的 7 列网格，改为本页需要的列数
const gridStock = { gridTemplateColumns: '1.6fr 0.9fr 1fr 1.3fr 1fr' }
const gridSector = { gridTemplateColumns: '1.8fr 1fr 1.3fr 1.3fr' }

function c(v) { return { color: v >= 0 ? '#dc2626' : '#16a34a', fontWeight: 600 } }
function sign(v) { return v >= 0 ? '+' : '' }
function moneyColor(v) {
  if (v > 0) return { color: '#dc2626', fontWeight: 600 }
  if (v < 0) return { color: '#16a34a', fontWeight: 600 }
  return { color: '#a8a29e' }
}
// f62 单位为元：>=1e8 显示亿，>=1e4 显示万
function formatMoney(v) {
  const n = Number(v) || 0
  const abs = Math.abs(n)
  if (abs >= 1e8) return (n / 1e8).toFixed(2) + '亿'
  if (abs >= 1e4) return (n / 1e4).toFixed(2) + '万'
  return n.toFixed(0)
}

function goQuote(symbol) {
  if (navigateToQuote) navigateToQuote(symbol)
}

// ====== 分页辅助 ======
function totalPages(list) {
  return Math.max(1, Math.ceil((list?.length || 0) / PAGE_SIZE))
}
function paged(list, key) {
  const arr = list || []
  const start = (pageState[key] - 1) * PAGE_SIZE
  return arr.slice(start, start + PAGE_SIZE)
}

async function refresh() {
  if (loading.value) return
  loading.value = true
  error.value = ''
  try {
    const params = new URLSearchParams({
      change_threshold: String(hiddenChangeThreshold.value),
      hot_top_n: String(hiddenHotTopN.value),
    })
    const r = await apiFetch(`/api/fund-flow?${params.toString()}`)
    if (r && r.success && r.data) {
      const d = r.data
      // 容错：任一部分失败返回空对象时补默认值，避免模板报错
      d.stocks = d.stocks || { inflow: [], outflow: [] }
      d.stocks.inflow = d.stocks.inflow || []
      d.stocks.outflow = d.stocks.outflow || []
      d.sectors = d.sectors || {}
      d.sectors.industry = d.sectors.industry || { inflow: [], outflow: [] }
      d.sectors.concept = d.sectors.concept || { inflow: [], outflow: [] }
      d.hiddenGems = d.hiddenGems || []
      // 防御：若接口整体返回空（东财偶发限流且历史也无数据），但本地已有数据，
      // 保留本地数据，避免「切 Tab / 30s 轮询」把页面闪成『暂无数据』。
      const prev = data.value
      const allEmpty = !d.stocks.inflow.length && !d.stocks.outflow.length &&
        !d.sectors.industry.inflow.length && !d.sectors.industry.outflow.length &&
        !d.sectors.concept.inflow.length && !d.sectors.concept.outflow.length &&
        !d.hiddenGems.length
      if (prev && allEmpty) {
        console.warn('[资金流向] 本轮接口返回全空，保留上次数据')
        return
      }
      data.value = d
      lastUpdateTime.value = formatNowTime()
      // 数据更新后重置所有分页到第 1 页
      Object.keys(pageState).forEach(k => { pageState[k] = 1 })
    } else {
      error.value = (r && r.detail) || '获取资金流向失败'
    }
  } catch (e) {
    error.value = '请求失败: ' + e.message
  } finally {
    loading.value = false
  }
}

let timer = null
function schedule() {
  if (timer) clearTimeout(timer)
  statusText.value = getTradingStatusText()
  // 非交易日 / 非交易时段：加载一次收盘后的缓存数据（如果还没有数据），然后仅定时唤醒自查
  if (!isTradingTime()) {
    if (!data.value) refresh()
    timer = setTimeout(schedule, getNextRefreshDelay())
    return
  }
  const tick = async () => {
    await refresh()
    statusText.value = getTradingStatusText()
    if (isTradingTime()) {
      timer = setTimeout(tick, 30000)
    } else {
      // 刚收盘：停止刷新，等下一交易时段再唤醒
      schedule()
    }
  }
  tick()
}

onMounted(() => { schedule() })
onUnmounted(() => { if (timer) clearTimeout(timer) })
</script>

<style scoped>
.flow-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1rem;
  margin-bottom: 1.5rem;
}
.flow-col {
  min-width: 0;
}
.flow-col-title {
  font-size: 0.875rem;
  font-weight: 700;
  padding: 0.5rem 0.75rem;
  border-radius: var(--radius-sm);
  margin-bottom: 0.5rem;
}
.flow-col-title.inflow {
  background: #fef2f2;
  color: var(--accent-danger);
  border: 1px solid #fecaca;
}
.flow-col-title.outflow {
  background: #f0fdf4;
  color: var(--accent-success);
  border: 1px solid #bbf7d0;
}
.muted {
  color: var(--text-tertiary);
  font-weight: 400;
  font-size: 0.75rem;
}
.empty-row {
  text-align: center;
  padding: 1.5rem;
  color: var(--text-tertiary);
  font-size: 0.8125rem;
}
/* 让板块/个股表头与行左对齐更好看 */
.flow-col .stock-table-header span,
.flow-col .stock-table-row span {
  text-align: left;
}
.flow-col .stock-table-header span:nth-child(n+2),
.flow-col .stock-table-row span:nth-child(n+2) {
  text-align: right;
}

@media (max-width: 640px) {
  .flow-grid {
    grid-template-columns: 1fr;
  }
}
</style>
