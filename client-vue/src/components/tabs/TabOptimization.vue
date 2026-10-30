<template>
  <div class="opt-page">
    <div class="page-header">
      <div class="page-title">⚙️ 自动优化</div>
      <div class="page-subtitle">系统自进化面板 · D1-D4 权重 · 角色门槛 · 网格搜索</div>
    </div>

    <DataFreshness
      :updated="fetchedAt"
      label="数据读取于"
      stale-hint="距今超过一天，建议刷新"
      :loading="optimizing"
      @refresh="fetchData"
    />

    <!-- 概览统计 -->
    <div class="opt-stats-row">
      <div class="stat-card">
        <div class="stat-num">{{ stats.totalOptimizations || 0 }}</div>
        <div class="stat-label">累计优化次数</div>
      </div>
      <div class="stat-card">
        <div class="stat-num">{{ stats.weeklyOptimizations || 0 }}</div>
        <div class="stat-label">本周优化</div>
      </div>
      <div class="stat-card">
        <div class="stat-num">{{ pageSize }}</div>
        <div class="stat-label">历史回测样本</div>
      </div>
      <div class="stat-card clickable" @click="triggerOptimize">
        <div class="stat-num" v-if="!optimizing">▶ 运行</div>
        <div class="stat-num" v-else style="font-size: 0.85rem">⏳ 优化中...</div>
        <div class="stat-label">手动触发优化</div>
      </div>
    </div>

    <div class="opt-grid">
      <!-- 单列：D1-D4 权重（2026-10-28 角色面板删除后由双列改为单列） -->
      <div class="opt-panel">
        <div class="panel-header">
          <span>📐 D1-D4 维度权重</span>
          <span class="panel-hint">每日回测后自动微调 · 权重反映因子预测力</span>
        </div>

        <!-- 当前权重条 -->
        <div class="weight-bar-group">
          <div v-for="dim in dims" :key="dim.key" class="weight-bar-row">
            <div class="weight-bar-label">
              <span class="dim-name">{{ dim.label }}</span>
              <span class="dim-desc">{{ dim.desc }}</span>
            </div>
            <div class="weight-bar-track">
              <div
                class="weight-bar-fill"
                :style="{ width: weightPct(dim.key) + '%', backgroundColor: dim.color }"
              ></div>
            </div>
            <div class="weight-bar-val" :class="weightDir(dim.key)">
              {{ currentWeights[dim.key]?.weight?.toFixed(3) || '1.000' }}
              <span class="weight-dir-arrow">{{ weightArrow(dim.key) }}</span>
            </div>
          </div>
        </div>

        <!-- 权重调整历史 -->
        <div class="section-title">📈 权重变化日志</div>
        <div class="history-scroll">
          <div v-for="(h, idx) in weightHistory" :key="idx" class="history-item">
            <span class="history-time">{{ formatTime(h.created_at) }}</span>
            <span class="history-detail">{{ parseWeightDetail(h) }}</span>
          </div>
          <div v-if="weightHistory.length === 0" class="history-empty">暂无调整记录（系统运行后将自动生成）</div>
        </div>
      </div>
    </div>

    <!-- 网格搜索历史 -->
    <div class="opt-panel" style="margin-top: 1rem">
      <div class="panel-header">
        <span>🔍 网格搜索历史</span>
        <span class="panel-hint">每周一运行 · 搜索 D1/D2 最优权重组合（25种）</span>
      </div>
      <div class="grid-table">
        <div class="grid-row grid-head">
          <span>时间</span>
          <span>当前胜率</span>
          <span>最优胜率</span>
          <span>改善</span>
          <span>最优 D1</span>
          <span>最优 D2</span>
          <span>已采纳</span>
        </div>
        <div v-for="(g, idx) in gridHistory" :key="idx" class="grid-row">
          <span>{{ formatTime(g.created_at) }}</span>
          <span>{{ g.current_winrate?.toFixed(1) || '-' }}%</span>
          <span class="wr-high">{{ g.best_winrate?.toFixed(1) || '-' }}%</span>
          <span :class="g.improvement > 0 ? 'wr-high' : 'wr-low'">
            {{ g.improvement != null ? (g.improvement > 0 ? '+' : '') + g.improvement.toFixed(1) + '%' : '-' }}
          </span>
          <span>{{ parseGridParam(g.parameter_set, 'd1') }}</span>
          <span>{{ parseGridParam(g.parameter_set, 'd2') }}</span>
          <span>{{ g.improvement >= 2 ? '✅' : '⏸️' }}</span>
        </div>
        <div v-if="gridHistory.length === 0" class="history-empty">暂无网格搜索记录（每周一自动运行）</div>
      </div>
    </div>

    <!-- 触发反馈 -->
    <div v-if="triggerMsg" class="trigger-toast" :class="{ success: triggerSuccess, error: !triggerSuccess }">
      {{ triggerMsg }}
    </div>
  </div>
</template>

<script setup>
  import { ref, computed, onMounted } from 'vue'
  import DataFreshness from '../DataFreshness.vue'

  const emit = defineEmits(['open-kline'])

  // 新鲜度（2026-10-13）：本页不轮询，标出"读于何时"避免把旧权重当成本轮结果
  const fetchedAt = ref('')

  // 维度和角色常量
  const dims = [
    { key: 'd1', label: 'D1', desc: '短期均线趋势', color: '#dc2626' },
    { key: 'd2', label: 'D2', desc: '资金异动', color: '#ea580c' },
    { key: 'd3', label: 'D3', desc: '量能配合', color: '#2563eb' },
    { key: 'd4', label: 'D4', desc: '风险回撤', color: '#7c3aed' },
  ]
  // 2026-10-28 角色体系下线：删除 roleLabels（7 级角色名映射）

  const currentWeights = ref({ d1: { weight: 1.0 }, d2: { weight: 1.0 }, d3: { weight: 1.0 }, d4: { weight: 1.0 } })
  const stats = ref({})
  const weightHistory = ref([])
  const gridHistory = ref([])
  const pageSize = ref(0)
  const optimizing = ref(false)
  const triggerMsg = ref('')
  const triggerSuccess = ref(false)

  // 当前权重占比（归一化到 0-100%）
  function weightPct(key) {
    const w = currentWeights.value[key]?.weight || 1.0
    return Math.round((w / 1.5) * 100)
  }
  function weightDir(key) {
    const w = currentWeights.value[key]?.weight || 1.0
    if (w > 1.01) return 'up'
    if (w < 0.99) return 'down'
    return ''
  }
  function weightArrow(key) {
    const w = currentWeights.value[key]?.weight || 1.0
    if (w > 1.01) return '↑'
    if (w < 0.99) return '↓'
    return '→'
  }

  // 2026-10-28 角色体系下线：删除 roleList / thresholdList / roleName / wrClass / excessClass

  function formatTime(ts) {
    if (!ts) return ''
    return ts.slice(5, 16)
  }

  function parseWeightDetail(h) {
    try {
      const d = JSON.parse(h.details || '[]')
      const changed = d.filter(a => a.action !== 'keep')
      if (changed.length === 0) return '全部维持不变'
      return changed.map(a => `${a.dimension}: ${a.old_weight}→${a.new_weight}`).join('; ')
    } catch {
      return h.details || ''
    }
  }

  function parseGridParam(ps, key) {
    try {
      const p = JSON.parse(ps || '{}')
      return p[key]?.toFixed(2) || '-'
    } catch {
      return '-'
    }
  }

  async function fetchData() {
    try {
      const [statusRes, trendRes] = await Promise.all([
        fetch('/api/optimization/status').then(r => r.json()),
        fetch('/api/optimization/trend').then(r => r.json()),
      ])
      if (statusRes.success) {
        currentWeights.value = statusRes.data.dimensionWeights || {}
      }
      if (trendRes.success) {
        weightHistory.value = trendRes.data.weightHistory || []
        gridHistory.value = trendRes.data.gridHistory || []
        stats.value = trendRes.data.stats || {}
      }
      pageSize.value = statusRes.data?.trendBacktestSamples || 0
      fetchedAt.value = Date.now()
    } catch (e) {
      console.error('加载优化数据失败', e)
    }
  }

  async function triggerOptimize() {
    if (optimizing.value) return
    optimizing.value = true
    triggerMsg.value = ''
    try {
      const res = await fetch('/api/optimization/trigger', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      }).then(r => r.json())
      if (res.success) {
        triggerSuccess.value = true
        const r = res.data
        const parts = []
        if (r.dimension_weights) parts.push(r.dimension_weights.summary)
        if (r.grid_search) parts.push(r.grid_search.summary)
        triggerMsg.value = '✅ 优化完成: ' + parts.join(' | ')
        await fetchData()
      } else {
        triggerSuccess.value = false
        triggerMsg.value = '❌ 优化失败'
      }
    } catch (e) {
      triggerSuccess.value = false
      triggerMsg.value = '❌ 请求失败: ' + e.message
    }
    optimizing.value = false
    setTimeout(() => {
      triggerMsg.value = ''
    }, 6000)
  }

  onMounted(() => {
    fetchData()
  })
</script>

<style scoped>
  .opt-page {
    padding: 0.5rem 1rem 2rem;
    max-width: 1400px;
    margin: 0 auto;
  }
  .page-header {
    margin-bottom: 1.2rem;
  }
  .page-title {
    font-size: 1.3rem;
    font-weight: 700;
    color: var(--text-primary);
  }
  .page-subtitle {
    font-size: 0.85rem;
    color: var(--text-tertiary);
    margin-top: 2px;
  }

  /* 统计行 */
  .opt-stats-row {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 0.8rem;
    margin-bottom: 1rem;
  }
  /* 2026-10-17：补窄屏降级（该文件此前全文无 @media）。768px 以下 4 张统计卡挤在一行，
   数字+标签会被压成两行碎块，改 2 列；480px 以下进一步收窄内边距。 */
  @media (max-width: 768px) {
    .opt-stats-row {
      grid-template-columns: repeat(2, 1fr);
    }
  }
  @media (max-width: 480px) {
    .opt-stats-row {
      gap: 0.5rem;
    }
    .stat-card {
      padding: 0.6rem 0.5rem;
    }
    .stat-num {
      font-size: 1.1rem;
    }
  }
  .stat-card {
    background: var(--bg-card);
    border: 1px solid var(--border-color);
    border-radius: 10px;
    padding: 0.8rem 1rem;
    text-align: center;
  }
  .stat-card.clickable {
    cursor: pointer;
    transition: all 0.15s;
  }
  .stat-card.clickable:hover {
    border-color: var(--accent);
  }
  .stat-num {
    font-size: 1.3rem;
    font-weight: 700;
    color: var(--text-primary);
  }
  .stat-label {
    font-size: 0.75rem;
    color: var(--text-tertiary);
    margin-top: 2px;
  }

  /* 双列网格 */
  .opt-grid {
    display: grid;
    grid-template-columns: 1fr;
    gap: 1rem;
  }

  /* 面板 */
  .opt-panel {
    background: var(--bg-card);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 1rem;
  }
  .panel-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1rem;
  }
  .panel-header > span:first-child {
    font-weight: 600;
    font-size: 0.95rem;
    color: var(--text-primary);
  }
  .panel-hint {
    font-size: 0.75rem;
    color: var(--text-tertiary);
  }
  .section-title {
    font-size: 0.85rem;
    font-weight: 600;
    color: var(--text-secondary);
    margin-bottom: 0.5rem;
  }

  /* 权重条 */
  .weight-bar-group {
    margin-bottom: 1rem;
  }
  .weight-bar-row {
    display: flex;
    align-items: center;
    gap: 0.8rem;
    margin-bottom: 0.6rem;
  }
  .weight-bar-label {
    width: 140px;
    flex-shrink: 0;
  }
  .dim-name {
    font-weight: 600;
    font-size: 0.9rem;
    color: var(--text-primary);
  }
  .dim-desc {
    display: block;
    font-size: 0.7rem;
    color: var(--text-tertiary);
    margin-top: 1px;
  }
  .weight-bar-track {
    flex: 1;
    height: 18px;
    background: var(--bg-input);
    border-radius: 9px;
    overflow: hidden;
  }
  .weight-bar-fill {
    height: 100%;
    border-radius: 9px;
    transition: width 0.5s ease;
    min-width: 4px;
  }
  .weight-bar-val {
    width: 70px;
    text-align: right;
    font-weight: 600;
    font-size: 0.9rem;
    font-variant-numeric: tabular-nums;
  }
  .weight-bar-val.up {
    color: #dc2626;
  }
  .weight-bar-val.down {
    color: #2563eb;
  }
  .weight-dir-arrow {
    margin-left: 2px;
    font-size: 0.75rem;
  }

  /* 历史滚动 */
  .history-scroll {
    max-height: 200px;
    overflow-y: auto;
    font-size: 0.8rem;
  }
  .history-item {
    display: flex;
    gap: 0.6rem;
    padding: 0.3rem 0;
    border-bottom: 1px solid var(--border-muted);
  }
  .history-time {
    color: var(--text-tertiary);
    flex-shrink: 0;
    font-size: 0.75rem;
    white-space: nowrap;
  }
  .history-detail {
    color: var(--text-secondary);
    word-break: break-all;
  }
  .history-empty {
    text-align: center;
    color: var(--text-tertiary);
    padding: 1rem;
    font-size: 0.85rem;
  }

  /* 2026-10-28 角色体系下线：删除角色表格与角色门槛网格样式 */
  .wr-high {
    color: #dc2626;
  }
  .wr-mid {
    color: #d97706;
  }
  .wr-low {
    color: #6b7280;
  }

  /* 网格搜索表 */
  .grid-table {
    font-size: 0.8rem;
  }
  .grid-row {
    display: grid;
    grid-template-columns: 80px 70px 70px 65px 55px 55px 55px;
    gap: 0.3rem;
    padding: 0.3rem 0;
    border-bottom: 1px solid var(--border-muted);
    align-items: center;
  }
  .grid-head {
    font-weight: 600;
    color: var(--text-tertiary);
    font-size: 0.72rem;
  }

  /* 触发反馈 */
  .trigger-toast {
    position: fixed;
    bottom: 2rem;
    left: 50%;
    transform: translateX(-50%);
    padding: 0.7rem 1.5rem;
    border-radius: 8px;
    font-size: 0.85rem;
    z-index: 999;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
    max-width: 90%;
  }
  .trigger-toast.success {
    background: #dcfce7;
    color: #166534;
    border: 1px solid #bbf7d0;
  }
  .trigger-toast.error {
    background: #fef2f2;
    color: #991b1b;
    border: 1px solid #fecaca;
  }
</style>
