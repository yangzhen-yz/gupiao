<template>
  <div class="usage-page">
    <div class="readonly" v-if="loaded && !canWrite">
      你能看各接口的调用量，但只有管理员账号能改上限（这些上限一改就影响全局防封禁策略）。
    </div>

    <div class="loading" v-if="loading && !loaded">读取接口用量…</div>

    <!-- 顶部摘要 -->
    <div class="summary" v-if="loaded">
      <div class="s-card">
        <b>{{ summary.total }}</b><span>今日总调用</span>
      </div>
      <div class="s-card">
        <b>{{ summary.interfaces }}</b><span>被统计接口</span>
      </div>
      <div class="s-card" :class="{ warn: summary.alert > 0 }">
        <b>{{ summary.alert }}</b><span>已达预警(≥{{ alertThreshold }}%)</span>
      </div>
      <div class="s-card" :class="{ danger: summary.over > 0 }">
        <b>{{ summary.over }}</b><span>已超限</span>
      </div>
    </div>

    <div class="note" v-if="loaded">
      统计覆盖所有经 requests/httpx/urllib 发出的第三方调用（腾讯/东财/新浪/企业微信/AI 等），
      westock 走子进程单独计数；localhost 与内网 IP 不计入。改代码前先确认<strong>没有接口逼近上限</strong>，
      避免把东财等平台日调用量推过封禁阈值。
    </div>

    <div class="table-wrap" v-if="loaded">
      <table class="usage-table">
        <thead>
          <tr>
            <th class="col-iface">接口</th>
            <th class="col-count">今日调用</th>
            <th class="col-limit">每日上限</th>
            <th class="col-use">使用率</th>
            <th class="col-status">状态</th>
            <th class="col-ok">成功/失败</th>
            <th class="col-trend">近14天趋势</th>
            <th class="col-last">最近调用</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="it in items" :key="it.key" :class="{ alert: it.alert, over: it.over }">
            <td class="col-iface">
              <div class="iface-name">{{ it.label }}</div>
              <div class="iface-key">{{ it.key }}</div>
            </td>
            <td class="col-count">
              <b :class="{ hot: it.alert }">{{ it.count.toLocaleString() }}</b>
            </td>
            <td class="col-limit">
              <template v-if="canWrite">
                <input
                  class="limit-input"
                  type="number"
                  min="1"
                  :value="it.limit"
                  @change="onLimitChange(it, $event)"
                />
              </template>
              <template v-else>{{ it.limit != null ? it.limit.toLocaleString() : '—' }}</template>
            </td>
            <td class="col-use">
              <div class="bar">
                <div
                  class="bar-fill"
                  :class="barClass(it)"
                  :style="{ width: barWidth(it) }"
                ></div>
              <span class="bar-pct">{{ it.pct != null ? it.pct + '%' : '—' }}</span>
              </div>
            </td>
            <td class="col-status">
              <span class="badge" :class="statusClass(it)">{{ statusText(it) }}</span>
              <span
                v-if="failHot(it)"
                class="badge danger fail-badge"
                title="失败率≥30%且调用≥50次：大概率是被平台风控/封禁，与使用率无关"
                >高失败 {{ failPct(it) }}%</span
              >
            </td>
            <td class="col-ok">
              <span class="ok">{{ it.ok.toLocaleString() }}</span>
              <span class="sep">/</span>
              <span class="fail" :class="{ hot: it.fail > 0 }">{{ it.fail.toLocaleString() }}</span>
            </td>
            <td class="col-trend">
              <svg class="spark" viewBox="0 0 90 24" preserveAspectRatio="none">
                <polyline
                  :points="sparkPoints(it.key)"
                  fill="none"
                  :stroke="sparkColor(it)"
                  stroke-width="1.5"
                />
              </svg>
            </td>
            <td class="col-last">
              <span class="last">{{ lastText(it.lastTs) }}</span>
            </td>
          </tr>
          <tr v-if="!items.length">
            <td colspan="8" class="empty">暂无调用记录</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="footer-bar">
      <button class="refresh" @click="load" :disabled="loading">刷新</button>
      <span class="ts" v-if="loaded">自动刷新（30s）· 数据落库周期 60s</span>
    </div>
  </div>
</template>

<script setup>
  import { ref, computed } from 'vue'
  import { apiFetch } from '../../utils/helpers'
  import { usePolling } from '../../composables/usePolling'

  // 2026-10-24：active 由 App 传入（仅该 Tab 可见时轮询）。
  // 此前是裸 setInterval：点开过一次后，切到任何别的 Tab / 最小化窗口，
  // 它都照样每 30s 打一次 /api/api-usage（带 history 序列），全应用唯一漏网。
  const props = defineProps({
    active: { type: Boolean, default: true },
  })

  const loading = ref(false)
  const loaded = ref(false)
  const canWrite = ref(false)
  const items = ref([])
  const history = ref({ dates: [], series: {}, okSeries: {}, failSeries: {} })
  const alertThreshold = ref(80)
  const writeIds = ref([])

  const summary = computed(() => {
    const list = items.value
    const total = list.reduce((s, x) => s + x.count, 0)
    const alert = list.filter((x) => x.alert).length
    const over = list.filter((x) => x.over).length
    return { total, interfaces: list.length, alert, over }
  })

  function barWidth(it) {
    if (it.pct == null) return '0%'
    return Math.max(2, Math.min(100, it.pct)) + '%'
  }
  function barClass(it) {
    if (it.over) return 'danger'
    if (it.alert) return 'warn'
    return 'ok'
  }
  function statusText(it) {
    if (it.over) return '🔴 超限'
    if (it.alert) return '⚠️ 预警'
    return '正常'
  }
  function statusClass(it) {
    if (it.over) return 'danger'
    if (it.alert) return 'warn'
    return 'ok'
  }
  function sparkColor(it) {
    if (it.over) return 'var(--accent-danger)'
    if (it.alert) return 'var(--accent-warning)'
    return 'var(--accent-info)'
  }
  // 「高失败」独立告警：状态列此前只看使用率（count/limit），封禁日 ulist 失败率 80%
  // 仍显示"正常"。失败率与使用率是两回事——fail 暴涨才是被封禁的最典型信号。
  // 阈值：调用数 ≥50 才有统计意义，失败率 ≥30% 才亮灯（避免偶发 1-2 次超时刷屏）。
  function failPct(it) {
    return it.count ? Math.round((it.fail / it.count) * 100) : 0
  }
  function failHot(it) {
    return it.count >= 50 && it.fail / it.count >= 0.3
  }
  function sparkPoints(key) {
    const arr = history.value.series ? history.value.series[key] : null
    if (!arr || !arr.length) return ''
    const W = 90, H = 24, n = arr.length
    const max = Math.max(...arr, 1)
    const min = Math.min(...arr, 0)
    const span = max - min || 1
    return arr
      .map((v, i) => {
        const x = n === 1 ? W / 2 : (i / (n - 1)) * W
        const y = H - ((v - min) / span) * (H - 4) - 2
        return `${x.toFixed(1)},${y.toFixed(1)}`
      })
      .join(' ')
  }
  function lastText(ts) {
    if (!ts) return '—'
    const diff = Date.now() - ts
    if (diff < 60000) return '刚刚'
    if (diff < 3600000) return Math.floor(diff / 60000) + ' 分钟前'
    if (diff < 86400000) return Math.floor(diff / 3600000) + ' 小时前'
    return Math.floor(diff / 86400000) + ' 天前'
  }

  async function load() {
    loading.value = true
    try {
      const res = await apiFetch('/api/api-usage')
      const d = res.data || {}
      items.value = d.items || []
      history.value = d.history || { dates: [], series: {}, okSeries: {}, failSeries: {} }
      canWrite.value = !!d.canWrite
      writeIds.value = d.writeIds || []
      alertThreshold.value = d.alertThreshold || 80
      loaded.value = true
    } catch (e) {
      console.error('读取接口用量失败', e)
    } finally {
      loading.value = false
    }
  }

  async function onLimitChange(it, ev) {
    const v = Number(ev.target.value)
    if (!Number.isFinite(v) || v <= 0) {
      ev.target.value = it.limit
      return
    }
    try {
      await apiFetch('/api/api-usage/limits', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: it.key, limit: v }),
      })
      it.limit = v
    } catch (e) {
      ev.target.value = it.limit
      alert('保存上限失败：' + ((e && (e.detail || e.message)) || e))
    }
  }

  // 30s 自动刷新（usePolling：页面隐藏/切走该 Tab 时停发请求，回来自动补一次）
  usePolling(load, { interval: 30000, active: () => props.active })
</script>

<style scoped>
  /* 仅用 main.css 主题 token（--bg-* / --text-* / --accent-* / --border-*），不写死颜色 */
  .usage-page {
    padding: var(--spacing-sm) 0 var(--spacing-xl);
    color: var(--text-primary);
  }
  .readonly {
    font-size: 12px;
    padding: 6px var(--spacing-sm);
    margin-bottom: var(--spacing-sm);
    background: var(--accent-warning-bg);
    color: var(--accent-warning);
    border: 1px solid var(--accent-warning-border);
    border-radius: var(--radius-sm);
  }
  .loading {
    font-size: 12px;
    color: var(--text-tertiary);
    padding: var(--spacing-sm);
  }
  .summary {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: var(--spacing-sm);
    margin-bottom: var(--spacing-sm);
  }
  .s-card {
    background: var(--bg-secondary);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    padding: var(--spacing-sm) var(--spacing-md);
    box-shadow: var(--shadow-soft);
    display: flex;
    flex-direction: column;
  }
  .s-card b {
    font-size: 22px;
    color: var(--text-primary);
  }
  .s-card span {
    font-size: 12px;
    color: var(--text-tertiary);
    margin-top: 2px;
  }
  .s-card.warn {
    border-color: var(--accent-warning-border);
    background: var(--accent-warning-bg);
  }
  .s-card.warn b,
  .s-card.warn span {
    color: var(--accent-warning);
  }
  .s-card.danger {
    border-color: var(--accent-danger-border);
    background: var(--accent-danger-bg);
  }
  .s-card.danger b,
  .s-card.danger span {
    color: var(--accent-danger);
  }
  .note {
    font-size: 11.5px;
    color: var(--text-tertiary);
    line-height: 1.6;
    margin-bottom: var(--spacing-sm);
    background: var(--bg-secondary);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    padding: var(--spacing-sm);
  }
  .note strong {
    color: var(--text-secondary);
  }
  .table-wrap {
    overflow-x: auto;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--bg-secondary);
    box-shadow: var(--shadow-soft);
  }
  .usage-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }
  .usage-table th,
  .usage-table td {
    padding: 7px var(--spacing-sm);
    text-align: left;
    border-bottom: 1px solid var(--border-subtle);
    white-space: nowrap;
  }
  .usage-table thead th {
    position: sticky;
    top: 0;
    background: var(--bg-tertiary, var(--bg-secondary));
    color: var(--text-tertiary);
    font-weight: 600;
    font-size: 11px;
    z-index: 1;
  }
  .usage-table tbody tr:hover {
    background: var(--bg-primary);
  }
  .usage-table tr.alert {
    background: var(--accent-warning-bg);
  }
  .usage-table tr.over {
    background: var(--accent-danger-bg);
  }
  .iface-name {
    color: var(--text-primary);
    font-weight: 600;
  }
  .iface-key {
    color: var(--text-tertiary);
    font-size: 10.5px;
    margin-top: 2px;
  }
  .col-count b {
    font-size: 14px;
  }
  .col-count b.hot {
    color: var(--accent-danger);
  }
  .limit-input {
    width: 80px;
    padding: 3px 6px;
    font-size: 12px;
    color: var(--text-primary);
    background: var(--bg-primary);
    border: 1px solid var(--border-medium);
    border-radius: var(--radius-xs);
  }
  .bar {
    display: flex;
    align-items: center;
    gap: 6px;
    width: 130px;
  }
  .bar-fill {
    height: 8px;
    border-radius: var(--radius-xs);
    min-width: 2px;
    transition: width 0.3s ease;
  }
  .bar-fill.ok {
    background: var(--accent-success);
  }
  .bar-fill.warn {
    background: var(--accent-warning);
  }
  .bar-fill.danger {
    background: var(--accent-danger);
  }
  .bar-pct {
    font-size: 11px;
    color: var(--text-secondary);
    min-width: 34px;
  }
  .badge {
    font-size: 11px;
    padding: 2px 7px;
    border-radius: var(--radius-xs);
    border: 1px solid transparent;
  }
  .badge.ok {
    color: var(--accent-success);
    background: var(--accent-success-bg);
    border-color: var(--accent-success-border);
  }
  .badge.warn {
    color: var(--accent-warning);
    background: var(--accent-warning-bg);
    border-color: var(--accent-warning-border);
  }
  .badge.danger {
    color: var(--accent-danger);
    background: var(--accent-danger-bg);
    border-color: var(--accent-danger-border);
  }
  .fail-badge {
    margin-left: 4px;
  }
  .ok {
    color: var(--accent-success);
  }
  .fail {
    color: var(--text-tertiary);
  }
  .fail.hot {
    color: var(--accent-danger);
  }
  .sep {
    color: var(--text-tertiary);
    margin: 0 2px;
  }
  .spark {
    width: 90px;
    height: 24px;
    display: block;
  }
  .last {
    color: var(--text-tertiary);
    font-size: 11px;
  }
  .empty {
    text-align: center;
    color: var(--text-tertiary);
    padding: var(--spacing-lg);
  }
  .footer-bar {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin-top: var(--spacing-sm);
  }
  .refresh {
    font-size: 12px;
    padding: 4px 14px;
    background: var(--accent-info);
    color: #fff;
    border: none;
    border-radius: var(--radius-sm);
    cursor: pointer;
  }
  .refresh:disabled {
    opacity: 0.6;
    cursor: default;
  }
  .ts {
    font-size: 11px;
    color: var(--text-tertiary);
  }
</style>
