<template>
  <div>
    <!-- 控制条 -->
    <div class="refresh-controls">
      <div class="toggle-section">
        <span class="toggle-label"
          >📊 资金流向 · {{ statusText }}<span v-if="sourceLabel" class="cooling-badge">{{ sourceLabel }}</span
          ><span v-if="stale" class="cooling-badge stale-badge">⚠ 数据截至 {{ lastUpdateTime }}（离线快照，今日未刷新）</span></span
        >
      </div>
      <div style="display: flex; gap: 0.5rem; align-items: center">
        <button class="btn-secondary" @click="refresh" :disabled="loading">手动刷新</button>
        <div class="last-update">数据时间 {{ lastUpdateTime }}</div>
      </div>
    </div>

    <div v-if="loading && !data" class="scan-progress">
      <div class="loading-spinner" style="width: 20px; height: 20px; border-width: 2px; margin: 0"></div>
      正在拉取资金流向数据...
    </div>
    <div v-if="error" class="scan-summary">
      <div class="scan-stat bear">{{ error }}</div>
    </div>

    <template v-if="data">
      <!-- 区块1：个股主力净流入/净流出 -->
      <h3 class="section-title">个股主力净流入 / 净流出排名</h3>
      <p class="section-subtitle">
        全市场沪深A股，按主力净流入（元）排序 · 红=流入 / 绿=流出 · 点击行查看行情 · 每页 {{ PAGE_SIZE }} 条
      </p>
      <div class="flow-grid">
        <div class="flow-col">
          <div class="flow-col-title inflow">🔴 主力净流入排名</div>
          <div class="stock-table">
            <div class="stock-table-header" :style="gridStock">
              <span>名称</span><span>现价</span><span>涨跌幅</span><span>主力净流入</span><span>行业</span>
            </div>
            <div
              v-for="r in paged(data.stocks.inflow, 'stockIn')"
              :key="r.symbol"
              class="stock-table-row clickable"
              :style="gridStock"
              @click="goQuote(r.symbol)"
            >
              <span class="name"
                >{{ r.name }}<br /><small class="muted">{{ r.code }}</small></span
              >
              <span class="price" :style="c(r.changePercent)">{{ r.price.toFixed(2) }}</span>
              <span :style="c(r.changePercent)">{{ sign(r.changePercent) }}{{ r.changePercent.toFixed(2) }}%</span>
              <span :style="moneyColor(r.mainNetInflow)">{{ formatMoney(r.mainNetInflow) }}</span>
              <span class="muted">{{ r.industry || '—' }}</span>
            </div>
            <div v-if="!data.stocks.inflow.length" class="empty-row">暂无数据</div>
          </div>
          <Pagination
            v-model="pageState.stockIn"
            :total="data.stocks.inflow.length"
            :total-pages="totalPages(data.stocks.inflow)"
          />
        </div>
        <div class="flow-col">
          <div class="flow-col-title outflow">🟢 主力净流出排名</div>
          <div class="stock-table">
            <div class="stock-table-header" :style="gridStock">
              <span>名称</span><span>现价</span><span>涨跌幅</span><span>主力净流入</span><span>行业</span>
            </div>
            <div
              v-for="r in paged(data.stocks.outflow, 'stockOut')"
              :key="r.symbol"
              class="stock-table-row clickable"
              :style="gridStock"
              @click="goQuote(r.symbol)"
            >
              <span class="name"
                >{{ r.name }}<br /><small class="muted">{{ r.code }}</small></span
              >
              <span class="price" :style="c(r.changePercent)">{{ r.price.toFixed(2) }}</span>
              <span :style="c(r.changePercent)">{{ sign(r.changePercent) }}{{ r.changePercent.toFixed(2) }}%</span>
              <span :style="moneyColor(r.mainNetInflow)">{{ formatMoney(r.mainNetInflow) }}</span>
              <span class="muted">{{ r.industry || '—' }}</span>
            </div>
            <div v-if="!data.stocks.outflow.length" class="empty-row">暂无数据</div>
          </div>
          <Pagination
            v-model="pageState.stockOut"
            :total="data.stocks.outflow.length"
            :total-pages="totalPages(data.stocks.outflow)"
          />
        </div>
      </div>

      <!-- 区块2：行业板块资金流（净流入左 / 净流出右，同一行） -->
      <h3 class="section-title">行业板块净流入 / 净流出排名</h3>
      <p class="section-subtitle">
        行业板块，按主力净流入（元）排序 · 红=流入 / 绿=流出 · 每页 {{ PAGE_SIZE }} 条
        <span v-if="sectorNote" class="cooling-badge">{{ sectorNote }}</span>
      </p>
      <div class="flow-grid">
        <div class="flow-col">
          <div class="flow-col-title inflow">🔴 行业板块净流入排名</div>
          <div class="stock-table">
            <div class="stock-table-header" :style="gridSector">
              <span>板块</span><span>涨跌幅</span><span>主力净流入</span><span>上涨/下跌</span>
            </div>
            <div
              v-for="s in paged(data.sectors.industry.inflow, 'indIn')"
              :key="'in' + s.code"
              class="stock-table-row"
              :style="gridSector"
            >
              <span class="name">{{ s.name }}</span>
              <span :style="c(s.changePercent)">{{ sign(s.changePercent) }}{{ s.changePercent.toFixed(2) }}%</span>
              <span :style="moneyColor(s.mainNetInflow)">{{ formatMoney(s.mainNetInflow) }}</span>
              <span class="muted">{{ s.riseCount }}/{{ s.fallCount }}</span>
            </div>
            <div v-if="!data.sectors.industry.inflow.length" class="empty-row">暂无数据</div>
          </div>
          <Pagination
            v-model="pageState.indIn"
            :total="data.sectors.industry.inflow.length"
            :total-pages="totalPages(data.sectors.industry.inflow)"
          />
        </div>
        <div class="flow-col">
          <div class="flow-col-title outflow">🟢 行业板块净流出排名</div>
          <div class="stock-table">
            <div class="stock-table-header" :style="gridSector">
              <span>板块</span><span>涨跌幅</span><span>主力净流入</span><span>上涨/下跌</span>
            </div>
            <div
              v-for="s in paged(data.sectors.industry.outflow, 'indOut')"
              :key="'out' + s.code"
              class="stock-table-row"
              :style="gridSector"
            >
              <span class="name">{{ s.name }}</span>
              <span :style="c(s.changePercent)">{{ sign(s.changePercent) }}{{ s.changePercent.toFixed(2) }}%</span>
              <span :style="moneyColor(s.mainNetInflow)">{{ formatMoney(s.mainNetInflow) }}</span>
              <span class="muted">{{ s.riseCount }}/{{ s.fallCount }}</span>
            </div>
            <div v-if="!data.sectors.industry.outflow.length" class="empty-row">暂无数据</div>
          </div>
          <Pagination
            v-model="pageState.indOut"
            :total="data.sectors.industry.outflow.length"
            :total-pages="totalPages(data.sectors.industry.outflow)"
          />
        </div>
      </div>

      <!-- 区块3：概念板块资金流（净流入左 / 净流出右，同一行） -->
      <h3 class="section-title">概念板块净流入 / 净流出排名</h3>
      <p class="section-subtitle">
        概念板块，按主力净流入（元）排序 · 红=流入 / 绿=流出 · 每页 {{ PAGE_SIZE }} 条
        <span v-if="conceptNote" class="cooling-badge">{{ conceptNote }}</span>
      </p>
      <div class="flow-grid">
        <div class="flow-col">
          <div class="flow-col-title inflow">🔴 概念板块净流入排名</div>
          <div class="stock-table">
            <div class="stock-table-header" :style="gridSector">
              <span>板块</span><span>涨跌幅</span><span>主力净流入</span><span>上涨/下跌</span>
            </div>
            <div
              v-for="s in paged(data.sectors.concept.inflow, 'conIn')"
              :key="'cin' + s.code"
              class="stock-table-row"
              :style="gridSector"
            >
              <span class="name">{{ s.name }}</span>
              <span :style="c(s.changePercent)">{{ sign(s.changePercent) }}{{ s.changePercent.toFixed(2) }}%</span>
              <span :style="moneyColor(s.mainNetInflow)">{{ formatMoney(s.mainNetInflow) }}</span>
              <span class="muted">{{ s.riseCount }}/{{ s.fallCount }}</span>
            </div>
            <div v-if="!data.sectors.concept.inflow.length" class="empty-row">暂无数据</div>
          </div>
          <Pagination
            v-model="pageState.conIn"
            :total="data.sectors.concept.inflow.length"
            :total-pages="totalPages(data.sectors.concept.inflow)"
          />
        </div>
        <div class="flow-col">
          <div class="flow-col-title outflow">🟢 概念板块净流出排名</div>
          <div class="stock-table">
            <div class="stock-table-header" :style="gridSector">
              <span>板块</span><span>涨跌幅</span><span>主力净流入</span><span>上涨/下跌</span>
            </div>
            <div
              v-for="s in paged(data.sectors.concept.outflow, 'conOut')"
              :key="'cout' + s.code"
              class="stock-table-row"
              :style="gridSector"
            >
              <span class="name">{{ s.name }}</span>
              <span :style="c(s.changePercent)">{{ sign(s.changePercent) }}{{ s.changePercent.toFixed(2) }}%</span>
              <span :style="moneyColor(s.mainNetInflow)">{{ formatMoney(s.mainNetInflow) }}</span>
              <span class="muted">{{ s.riseCount }}/{{ s.fallCount }}</span>
            </div>
            <div v-if="!data.sectors.concept.outflow.length" class="empty-row">暂无数据</div>
          </div>
          <Pagination
            v-model="pageState.conOut"
            :total="data.sectors.concept.outflow.length"
            :total-pages="totalPages(data.sectors.concept.outflow)"
          />
        </div>
      </div>

      <!-- 区块3：非热点上涨股 -->
      <h3 class="section-title">非近期热点 · 上涨不错的票</h3>
      <p class="section-subtitle">
        筛选逻辑：全市场涨幅榜剔除「热搜榜前 {{ hiddenHotTopN }} 名（近期热点）」与 ST/退市，不套用现有选股评分规则 ·
        每页 {{ PAGE_SIZE }} 条
      </p>
      <div class="filter-bar">
        <label class="filter-label">最低涨幅(%)：</label>
        <input v-model.number="hiddenChangeThreshold" type="number" step="0.5" min="0" class="filter-input" />
        <label class="filter-label">排除热搜前</label>
        <input v-model.number="hiddenHotTopN" type="number" min="0" class="filter-input" style="width: 4rem" />
        <label class="filter-label">名</label>
        <button class="btn-secondary" @click="refresh">应用并刷新</button>
        <span class="filter-info">共 {{ data.hiddenGems.length }} 只</span>
      </div>
      <div class="flow-col stock-table">
        <div class="stock-table-header" :style="gridStock">
          <span>名称</span><span>现价</span><span>涨跌幅</span><span>主力净流入</span><span>行业</span>
        </div>
        <div
          v-for="r in paged(data.hiddenGems, 'hidden')"
          :key="r.symbol"
          class="stock-table-row clickable"
          :style="gridStock"
          @click="goQuote(r.symbol)"
        >
          <span class="name"
            >{{ r.name }}<br /><small class="muted">{{ r.code }}</small></span
          >
          <span class="price" :style="c(r.changePercent)">{{ r.price.toFixed(2) }}</span>
          <span :style="c(r.changePercent)">{{ sign(r.changePercent) }}{{ r.changePercent.toFixed(2) }}%</span>
          <span :style="moneyColor(r.mainNetInflow)">{{ formatMoney(r.mainNetInflow) }}</span>
          <span class="muted">{{ r.industry || '—' }}</span>
        </div>
        <div v-if="!data.hiddenGems.length" class="empty-row">暂无符合条件股票（可下调涨幅阈值或排除排名）</div>
      </div>
      <Pagination
        v-model="pageState.hidden"
        :total="data.hiddenGems.length"
        :total-pages="totalPages(data.hiddenGems)"
      />
    </template>
  </div>
</template>

<script setup>
  import { ref, reactive, onMounted, inject } from 'vue'
  import Pagination from '../Pagination.vue'
  import {
    apiFetch,
    isTradingTime,
    getNextRefreshDelay,
    getTradingStatusText,
    formatNowTime,
  } from '../../utils/helpers'
  import { usePolling } from '../../composables/usePolling'
  import { goQuote } from '../../utils/navigation'

  const props = defineProps({ active: { type: Boolean, default: true } })

  const showNotification = inject('showNotification')

  const data = ref(null)
  const loading = ref(false)
  const error = ref('')
  const lastUpdateTime = ref('--:--:--')
  const statusText = ref(getTradingStatusText())
  const cooling = ref(false) // 2026-10-17：东财 clist 熔断冷却中，显示的是历史缓存
  const stale = ref(false) // 2026-10-19：数据龄期超阈值（今天从未成功刷新，页面显示的是旧快照）
  const sourceLabel = ref('') // 2026-10-17：数据来源提示（新浪兜底 / 冷却缓存）
  const sectorNote = ref('') // 2026-10-19：板块数据来源提示（兜底/快照/概念暂缺）
  const conceptNote = ref('') // 2026-10-31：概念板块来源提示（财联社兜底）

  const hiddenChangeThreshold = ref(3.0)
  const hiddenHotTopN = ref(30)

  // 分页：每种类型默认每页显示 10 条
  const PAGE_SIZE = 10
  const pageState = reactive({
    stockIn: 1,
    stockOut: 1,
    indIn: 1,
    indOut: 1,
    conIn: 1,
    conOut: 1,
    hidden: 1,
  })

  // 覆盖全局 stock-table 的 7 列网格，改为本页需要的列数
  const gridStock = { gridTemplateColumns: '1.6fr 0.9fr 1fr 1.3fr 1fr' }
  const gridSector = { gridTemplateColumns: '1.8fr 1fr 1.3fr 1.3fr' }

  function c(v) {
    return { color: v >= 0 ? '#dc2626' : '#16a34a', fontWeight: 600 }
  }
  function sign(v) {
    return v >= 0 ? '+' : ''
  }
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
        cooling.value = !!d.cooling
        stale.value = !!d.stale
        const ds = d.dataSource || {}
        // 只说"冷却中"没用，得说还剩多久；说"来自新浪"也必须带上口径差
        // （新浪排行只到超大单 r0，东财 f62 是超大单+大单，同一只票数值会不一样）
        const coolMin = Math.ceil((d.coolingRemainSec || 0) / 60)
        sourceLabel.value =
          ds.stocks === 'sina'
            ? '主力资金来自新浪·超大单口径（东财 clist 被封）'
            : ds.stocks === 'snapshot'
              ? '显示上次缓存数据'
              : d.cooling
                ? `东财冷却中·约 ${coolMin} 分钟后才再试`
                : ''
        // 2026-10-31：东财 clist 被封时，概念板块由财联社板块榜补；行业仍优先新浪×本地聚合。
        // 财联社分类与东财不是一套，数值不可混算，所以来源必须写在页面上。
        // 行级 source='cls' 比块级来源更准：合并后的快照（行业来自新浪/快照、概念来自财联社）
        // 落库后的 dataSource 只写 snapshot，但仍要如实告诉用户概念是财联社的。
        const _conRows = ((d.sectors || {}).concept || {})
        const _conCls = [...(_conRows.inflow || []), ...(_conRows.outflow || [])]
          .some(r => r && r.source === 'cls')
        sectorNote.value =
          ds.sectors === 'cls'
            ? '行业与概念板块均来自财联社（东财 clist 被封；财联社板块分类与东财不同，不可混算）'
            : ds.sectors === 'sina+cls'
              ? '行业为「新浪个股资金流 × 本地行业分类」聚合，概念板块来自财联社（两套分类不可混算）'
              : ds.sectors === 'snapshot+cls' || (ds.sectors === 'snapshot' && _conCls)
                ? '行业为历史快照，概念板块来自财联社'
                : ds.sectors === 'sina'
                  ? '板块为「新浪个股资金流 × 本地行业分类」聚合（行业来自 baostock 周更表，' +
                    '与东财板块名不是同一套分类；概念板块无本地分类，暂缺）'
                  : ds.sectors === 'snapshot'
                    ? '板块为历史快照'
                    : ''
        conceptNote.value =
          ds.sectors === 'cls' || ds.sectors === 'sina+cls' || ds.sectors === 'snapshot+cls' || _conCls
            ? '概念板块来自财联社（自有分类，与东财不可混算）'
            : ds.sectors === 'sina'
              ? '新浪无概念分类，概念板块暂缺'
              : ds.sectors === 'snapshot'
                ? '概念板块为历史快照'
                : ''
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
        const allEmpty =
          !d.stocks.inflow.length &&
          !d.stocks.outflow.length &&
          !d.sectors.industry.inflow.length &&
          !d.sectors.industry.outflow.length &&
          !d.sectors.concept.inflow.length &&
          !d.sectors.concept.outflow.length &&
          !d.hiddenGems.length
        if (prev && allEmpty) {
          console.warn('[资金流向] 本轮接口返回全空，保留上次数据')
          return
        }
        data.value = d
        // 2026-10-19 诚实化：显示数据真实时间（后端 updateTime），而非本次调用时间——
        // 封禁日静默返回旧快照时，旧时间戳是用户唯一能看出数据过期的线索。
        lastUpdateTime.value = (d.updateTime && d.updateTime !== '--') ? d.updateTime.slice(11) : formatNowTime()
        // 数据更新后重置所有分页到第 1 页
        Object.keys(pageState).forEach(k => {
          pageState[k] = 1
        })
      } else {
        error.value = (r && r.detail) || '获取资金流向失败'
      }
    } catch (e) {
      error.value = '请求失败: ' + e.message
    } finally {
      loading.value = false
    }
  }

  // 轮询治理（2026-10-19）：盘中 30s→120s。东财 clist IP 级风控实锤两次
  // （10-17/10-19 封禁均封数小时），根因是请求频率：原 30s×7次clist/轮=840次/小时，
  // 远超正常浏览器页面量级。治理后 fundflow 每轮 4 次 × 120s = 120次/小时（降 7 倍），
  // 资金流 2 分钟粒度对盘中决策无影响；休市仍自查一次长睡到下一个开盘点。
  const { start: startFlowPolling } = usePolling(
    async () => {
      statusText.value = getTradingStatusText()
      if (isTradingTime() || !data.value) await refresh()
    },
    {
      interval: () => (isTradingTime() ? 120000 : getNextRefreshDelay()),
      active: () => props.active,
      immediate: false,
    }
  )

  onMounted(() => {
    statusText.value = getTradingStatusText()
    refresh().finally(startFlowPolling)
  })
</script>

<style scoped>
  .cooling-badge {
    margin-left: 0.5rem;
    padding: 0.1rem 0.5rem;
    font-size: 0.72rem;
    font-weight: 600;
    color: #f0a020;
    background: rgba(240, 160, 32, 0.12);
    border: 1px solid rgba(240, 160, 32, 0.35);
    border-radius: 999px;
    vertical-align: middle;
    white-space: nowrap;
  }
  /* 2026-10-19：数据龄期超阈值（离线快照）——比冷却更醒目的警示色 */
  .stale-badge {
    color: #dc2626;
    background: rgba(220, 38, 38, 0.1);
    border-color: rgba(220, 38, 38, 0.4);
  }
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
  .flow-col .stock-table-header span:nth-child(n + 2),
  .flow-col .stock-table-row span:nth-child(n + 2) {
    text-align: right;
  }

  @media (max-width: 640px) {
    .flow-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
