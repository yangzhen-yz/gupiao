<template>
  <div class="opt-page">
    <div class="page-header">
      <div class="page-title">⚙️ 自动优化</div>
      <div class="page-subtitle">系统自进化面板 · D1-D4 权重 · 角色门槛 · 网格搜索</div>
    </div>

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
        <div class="stat-num" v-else style="font-size:0.85rem;">⏳ 优化中...</div>
        <div class="stat-label">手动触发优化</div>
      </div>
    </div>

    <div class="opt-grid">
      <!-- 左列：D1-D4权重 -->
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
              <div class="weight-bar-fill" :style="{ width: weightPct(dim.key) + '%', backgroundColor: dim.color }"></div>
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

      <!-- 右列：角色表现 + 网格搜索 -->
      <div class="opt-panel">
        <div class="panel-header">
          <span>🏷️ 角色分类表现</span>
          <span class="panel-hint">近7天各角色胜率与超额收益</span>
        </div>

        <div class="role-table">
          <div class="role-row role-head">
            <span class="rc-role">角色</span>
            <span class="rc-samples">样本</span>
            <span class="rc-wr">胜率</span>
            <span class="rc-excess">超额收益</span>
          </div>
          <div v-for="r in roleList" :key="r.role" class="role-row">
            <span class="rc-role">{{ roleName(r.role) }}</span>
            <span class="rc-samples">{{ r.total }}</span>
            <span class="rc-wr" :class="wrClass(r.win_rate)">{{ r.win_rate?.toFixed(1) || '-' }}%</span>
            <span class="rc-excess" :class="excessClass(r.excess_return)">
              {{ r.excess_return != null ? (r.excess_return > 0 ? '+' : '') + r.excess_return.toFixed(1) + '%' : '-' }}
            </span>
          </div>
          <div v-if="roleList.length === 0" class="history-empty">暂无数据（运行后将自动生成）</div>
        </div>

        <!-- 角色门槛当前值 -->
        <div class="section-title" style="margin-top:1.2rem;">🎯 角色门槛（自动调优）</div>
        <div class="threshold-grid">
          <div v-for="t in thresholdList" :key="t.role" class="threshold-item">
            <span class="th-role">{{ roleName(t.role) }}</span>
            <span class="th-param">{{ t.desc }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 网格搜索历史 -->
    <div class="opt-panel" style="margin-top:1rem;">
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

const emit = defineEmits(['open-kline'])

// 维度和角色常量
const dims = [
  { key: 'd1', label: 'D1', desc: '短期均线趋势', color: '#dc2626' },
  { key: 'd2', label: 'D2', desc: '资金异动', color: '#ea580c' },
  { key: 'd3', label: 'D3', desc: '量能配合', color: '#2563eb' },
  { key: 'd4', label: 'D4', desc: '风险回撤', color: '#7c3aed' },
]
const roleLabels = {
  total_leader: '总龙头', sector_leader: '板块龙头', core_trend: '中军',
  sentiment_vanguard: '情绪先锋', catch_up: '补涨龙', follower: '跟风', marginal: '杂毛',
}

const currentWeights = ref({ d1: { weight: 1.0 }, d2: { weight: 1.0 }, d3: { weight: 1.0 }, d4: { weight: 1.0 } })
const stats = ref({})
const weightHistory = ref([])
const rolePerformance = ref([])
const roleThresholds = ref({})
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

// 角色列表
const roleList = computed(() => {
  const map = {}
  for (const r of rolePerformance.value) {
    if (!map[r.role]) map[r.role] = { role: r.role, total: 0, win_rate: 0, excess_return: 0 }
    map[r.role].total += r.total
    map[r.role].win_rate = Math.max(map[r.role].win_rate, r.win_rate)
    map[r.role].excess_return = Math.max(map[r.role].excess_return, r.excess_return)
  }
  return Object.values(map).sort((a, b) => (roleLabels[a.role] || a.role).localeCompare(roleLabels[b.role] || b.role))
})

// 门槛列表
const thresholdList = computed(() => {
  const items = []
  const tl = roleThresholds.value
  if (tl.total_leader) items.push({ role: 'total_leader', desc: `连板≥${tl.total_leader.min_boards} 涨停≥${tl.total_leader.min_limitups}` })
  if (tl.sector_leader) items.push({ role: 'sector_leader', desc: `连板≥${tl.sector_leader.min_boards} 涨停≥${tl.sector_leader.min_limitups}` })
  if (tl.core_trend) items.push({ role: 'core_trend', desc: `市值≥${(tl.core_trend.min_mv / 1e8).toFixed(0)}亿 评分≥${tl.core_trend.min_score}` })
  if (tl.sentiment_vanguard) items.push({ role: 'sentiment_vanguard', desc: `换手率≥${tl.sentiment_vanguard.min_turnover}%` })
  if (tl.catch_up) items.push({ role: 'catch_up', desc: `位置分位<${tl.catch_up.max_position}` })
  if (tl.follower) items.push({ role: 'follower', desc: `涨幅≥${tl.follower.min_change}% 量比≥${tl.follower.min_vol_ratio}` })
  return items
})

function roleName(r) { return roleLabels[r] || r }

function wrClass(v) {
  if (v >= 60) return 'wr-high'
  if (v >= 45) return 'wr-mid'
  return 'wr-low'
}
function excessClass(v) {
  if (v == null) return ''
  if (v > 2) return 'wr-high'
  if (v < -2) return 'wr-low'
  return 'wr-mid'
}

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
  } catch { return h.details || '' }
}

function parseGridParam(ps, key) {
  try {
    const p = JSON.parse(ps || '{}')
    return p[key]?.toFixed(2) || '-'
  } catch { return '-' }
}

async function fetchData() {
  try {
    const [statusRes, trendRes] = await Promise.all([
      fetch('/api/optimization/status').then(r => r.json()),
      fetch('/api/optimization/trend').then(r => r.json()),
    ])
    if (statusRes.success) {
      currentWeights.value = statusRes.data.dimensionWeights || {}
      roleThresholds.value = statusRes.data.roleThresholds || {}
    }
    if (trendRes.success) {
      weightHistory.value = trendRes.data.weightHistory || []
      rolePerformance.value = trendRes.data.rolePerformance || []
      gridHistory.value = trendRes.data.gridHistory || []
      stats.value = trendRes.data.stats || {}
    }
    pageSize.value = statusRes.data?.trendBacktestSamples || 0
  } catch (e) {
    console.error('加载优化数据失败', e)
  }
}

async function triggerOptimize() {
  if (optimizing.value) return
  optimizing.value = true
  triggerMsg.value = ''
  try {
    const res = await fetch('/api/optimization/trigger', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) }).then(r => r.json())
    if (res.success) {
      triggerSuccess.value = true
      const r = res.data
      const parts = []
      if (r.dimension_weights) parts.push(r.dimension_weights.summary)
      if (r.role_thresholds) parts.push(r.role_thresholds.summary)
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
  setTimeout(() => { triggerMsg.value = '' }, 6000)
}

onMounted(() => { fetchData() })
</script>

<style scoped>
.opt-page { padding: 0.5rem 1rem 2rem; max-width: 1400px; margin: 0 auto; }
.page-header { margin-bottom: 1.2rem; }
.page-title { font-size: 1.3rem; font-weight: 700; color: var(--text-primary); }
.page-subtitle { font-size: 0.85rem; color: var(--text-tertiary); margin-top: 2px; }

/* 统计行 */
.opt-stats-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.8rem; margin-bottom: 1rem; }
.stat-card { background: var(--bg-card); border: 1px solid var(--border-color); border-radius: 10px; padding: 0.8rem 1rem; text-align: center; }
.stat-card.clickable { cursor: pointer; transition: all 0.15s; }
.stat-card.clickable:hover { border-color: var(--accent); }
.stat-num { font-size: 1.3rem; font-weight: 700; color: var(--text-primary); }
.stat-label { font-size: 0.75rem; color: var(--text-tertiary); margin-top: 2px; }

/* 双列网格 */
.opt-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }

/* 面板 */
.opt-panel { background: var(--bg-card); border: 1px solid var(--border-color); border-radius: 12px; padding: 1rem; }
.panel-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; }
.panel-header > span:first-child { font-weight: 600; font-size: 0.95rem; color: var(--text-primary); }
.panel-hint { font-size: 0.75rem; color: var(--text-tertiary); }
.section-title { font-size: 0.85rem; font-weight: 600; color: var(--text-secondary); margin-bottom: 0.5rem; }

/* 权重条 */
.weight-bar-group { margin-bottom: 1rem; }
.weight-bar-row { display: flex; align-items: center; gap: 0.8rem; margin-bottom: 0.6rem; }
.weight-bar-label { width: 140px; flex-shrink: 0; }
.dim-name { font-weight: 600; font-size: 0.9rem; color: var(--text-primary); }
.dim-desc { display: block; font-size: 0.7rem; color: var(--text-tertiary); margin-top: 1px; }
.weight-bar-track { flex: 1; height: 18px; background: var(--bg-input); border-radius: 9px; overflow: hidden; }
.weight-bar-fill { height: 100%; border-radius: 9px; transition: width 0.5s ease; min-width: 4px; }
.weight-bar-val { width: 70px; text-align: right; font-weight: 600; font-size: 0.9rem; font-variant-numeric: tabular-nums; }
.weight-bar-val.up { color: #dc2626; }
.weight-bar-val.down { color: #2563eb; }
.weight-dir-arrow { margin-left: 2px; font-size: 0.75rem; }

/* 历史滚动 */
.history-scroll { max-height: 200px; overflow-y: auto; font-size: 0.8rem; }
.history-item { display: flex; gap: 0.6rem; padding: 0.3rem 0; border-bottom: 1px solid var(--border-muted); }
.history-time { color: var(--text-tertiary); flex-shrink: 0; font-size: 0.75rem; white-space: nowrap; }
.history-detail { color: var(--text-secondary); word-break: break-all; }
.history-empty { text-align: center; color: var(--text-tertiary); padding: 1rem; font-size: 0.85rem; }

/* 角色表格 */
.role-table { font-size: 0.85rem; }
.role-row { display: grid; grid-template-columns: 100px 60px 70px 90px; gap: 0.3rem; padding: 0.35rem 0; border-bottom: 1px solid var(--border-muted); align-items: center; }
.role-head { font-weight: 600; color: var(--text-tertiary); font-size: 0.75rem; }
.rc-role { color: var(--text-primary); }
.rc-samples { text-align: center; color: var(--text-secondary); }
.rc-wr { text-align: right; font-weight: 600; }
.rc-excess { text-align: right; font-weight: 600; }
.wr-high { color: #dc2626; }
.wr-mid { color: #d97706; }
.wr-low { color: #6b7280; }

/* 门槛网格 */
.threshold-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0.4rem; }
.threshold-item { display: flex; gap: 0.4rem; font-size: 0.78rem; padding: 0.3rem 0.5rem; background: var(--bg-input); border-radius: 6px; }
.th-role { font-weight: 600; color: var(--text-primary); flex-shrink: 0; }
.th-param { color: var(--text-secondary); }

/* 网格搜索表 */
.grid-table { font-size: 0.8rem; }
.grid-row { display: grid; grid-template-columns: 80px 70px 70px 65px 55px 55px 55px; gap: 0.3rem; padding: 0.3rem 0; border-bottom: 1px solid var(--border-muted); align-items: center; }
.grid-head { font-weight: 600; color: var(--text-tertiary); font-size: 0.72rem; }

/* 触发反馈 */
.trigger-toast { position: fixed; bottom: 2rem; left: 50%; transform: translateX(-50%); padding: 0.7rem 1.5rem; border-radius: 8px; font-size: 0.85rem; z-index: 999; box-shadow: 0 4px 12px rgba(0,0,0,0.15); max-width: 90%; }
.trigger-toast.success { background: #dcfce7; color: #166534; border: 1px solid #bbf7d0; }
.trigger-toast.error { background: #fef2f2; color: #991b1b; border: 1px solid #fecaca; }
</style>
