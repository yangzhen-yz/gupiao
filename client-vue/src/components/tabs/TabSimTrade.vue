<template>
  <div class="sim-trade">
    <!-- 顶部说明横幅 -->
    <div class="sim-banner">
      <span class="sim-badge">纯模拟盘</span>
      <span>本页所有交易均为模拟，不连接任何券商、不产生真实下单。用于在隔离账户中验证策略闭环。</span>
      <button class="sim-reset" @click="onReset" :disabled="busy">重置账户</button>
    </div>

    <DataFreshness :updated="fetchedAt" label="账户状态读取于" stale-hint="数据不是今日的，建议刷新" :loading="busy" @refresh="load" />

    <!-- 账户面板 -->
    <div class="sim-account" v-if="state.account">
      <div class="acc-card">
        <div class="acc-label">可用现金</div>
        <div class="acc-val">¥{{ fmt(state.account.cash) }}</div>
      </div>
      <div class="acc-card">
        <div class="acc-label">持仓市值</div>
        <div class="acc-val">¥{{ fmt(state.account.marketValue) }}</div>
      </div>
      <div class="acc-card">
        <div class="acc-label">账户总值</div>
        <div class="acc-val strong">¥{{ fmt(state.account.totalValue) }}</div>
      </div>
      <div class="acc-card">
        <div class="acc-label">当日盈亏</div>
        <div class="acc-val" :class="state.account.dayPnl >= 0 ? 'up' : 'down'">
          {{ state.account.dayPnl >= 0 ? '+' : '' }}{{ fmt(state.account.dayPnl) }} ({{ state.account.dayPnlPct }}%)
        </div>
      </div>
      <div class="acc-card">
        <div class="acc-label">累计盈亏</div>
        <div class="acc-val" :class="state.account.totalPnl >= 0 ? 'up' : 'down'">
          {{ state.account.totalPnl >= 0 ? '+' : '' }}{{ fmt(state.account.totalPnl) }} ({{ state.account.totalPnlPct }}%)
        </div>
      </div>
    </div>

    <!-- 操作区 -->
    <div class="sim-actions">
      <div class="src-group">
        <label class="src-item"><input type="checkbox" v-model="sources" value="recommend" /> 智能推荐（预期优先·最多3只）</label>
        <label class="src-item"><input type="checkbox" v-model="sources" value="tomorrow_plan" /> 收评明日计划</label>
        <label class="src-item"><input type="checkbox" v-model="sources" value="diagnosis" /> 持仓诊断动作</label>
        <label class="src-item"><input type="checkbox" v-model="sources" value="screener" /> 筛选/趋势选中</label>
      </div>
      <div class="src-group" v-if="sources.includes('screener')">
        <input class="sym-input" v-model="symbolsText" placeholder="筛选源股票代码，逗号分隔，如 sh600036,sz000001" />
      </div>
      <div class="btn-group">
        <button class="btn primary" @click="onGenerate" :disabled="busy">从信号生成建议</button>
        <button class="btn" @click="onExecute" :disabled="busy">执行模拟成交</button>
        <span class="busy-tip" v-if="busy">处理中…</span>
      </div>
    </div>

    <!-- 模拟持仓 -->
    <div class="sim-section">
      <h3 class="sec-title">模拟持仓 <span class="sec-sub">{{ state.positions.length }} 只</span></h3>
      <table class="sim-table" v-if="state.positions.length">
        <thead>
          <tr><th>代码</th><th>名称</th><th>股数</th><th>成本</th><th>现价</th><th>市值</th><th>盈亏</th><th>占比</th></tr>
        </thead>
        <tbody>
          <tr v-for="p in state.positions" :key="p.symbol">
            <td class="mono">{{ p.symbol }}</td>
            <td>{{ p.name }}</td>
            <td class="num">{{ p.shares }}</td>
            <td class="num">{{ p.costPrice }}</td>
            <td class="num">{{ p.price }}</td>
            <td class="num">{{ fmt(p.marketValue) }}</td>
            <td class="num" :class="p.pnl >= 0 ? 'up' : 'down'">{{ p.pnl >= 0 ? '+' : '' }}{{ fmt(p.pnl) }} ({{ p.pnlPct }}%)</td>
            <td class="num">{{ p.weight }}%</td>
          </tr>
        </tbody>
      </table>
      <div class="empty" v-else>暂无模拟持仓</div>
    </div>

    <!-- 待执行建议 -->
    <div class="sim-section">
      <h3 class="sec-title">待执行建议 <span class="sec-sub">{{ state.pending.length }} 条</span></h3>
      <table class="sim-table" v-if="state.pending.length">
        <thead>
          <tr><th>方向</th><th>代码</th><th>名称</th><th>建议价</th><th>数量</th><th>来源</th><th>理由</th></tr>
        </thead>
        <tbody>
          <tr v-for="o in state.pending" :key="o.id">
            <td><span class="side" :class="o.side">{{ o.side === 'buy' ? '买入' : '卖出' }}</span></td>
            <td class="mono">{{ o.symbol }}</td>
            <td>{{ o.name }}</td>
            <td class="num">{{ o.proposed_price }}</td>
            <td class="num">{{ o.proposed_qty }}</td>
            <td>{{ srcLabel(o.source) }}</td>
            <td class="reason">{{ o.reason }}</td>
          </tr>
        </tbody>
      </table>
      <div class="empty" v-else>暂无待执行建议，点上方「从信号生成建议」</div>
    </div>

    <!-- 审计日志 -->
    <div class="sim-section">
      <h3 class="sec-title">审计日志 <span class="sec-sub">最近 {{ state.audit.length }} 条</span></h3>
      <table class="sim-table log">
        <thead>
          <tr><th>状态</th><th>方向</th><th>代码</th><th>建议价</th><th>成交价</th><th>数量</th><th>拒绝原因</th></tr>
        </thead>
        <tbody>
          <tr v-for="o in state.audit" :key="o.id">
            <td><span class="st" :class="o.status">{{ stLabel(o.status) }}</span></td>
            <td><span class="side" :class="o.side">{{ o.side === 'buy' ? '买入' : '卖出' }}</span></td>
            <td class="mono">{{ o.symbol }}</td>
            <td class="num">{{ o.proposed_price }}</td>
            <td class="num">{{ o.filled_price || '-' }}</td>
            <td class="num">{{ o.filled_qty || o.proposed_qty }}</td>
            <td class="reason" v-if="o.status === 'rejected'">{{ o.reject_reason }}</td>
            <td class="reason" v-else>{{ o.source }}</td>
          </tr>
        </tbody>
      </table>
      <div class="empty" v-if="!state.audit.length">暂无记录</div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { apiFetch, useNotification } from '../../utils/helpers'
import DataFreshness from '../DataFreshness.vue'

const { showNotification } = useNotification()
const state = ref({ account: null, positions: [], pending: [], audit: [] })
const sources = ref(['recommend', 'tomorrow_plan', 'diagnosis', 'screener'])
const symbolsText = ref('')
const busy = ref(false)
// 新鲜度（2026-10-13）：此前本页既无轮询也没有"更新于"，看不出看到的是不是今天的账户状态
const fetchedAt = ref('')

function fmt(n) {
  if (n == null) return '-'
  return Number(n).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}
function srcLabel(s) {
  return { recommend: '智能推荐', tomorrow_plan: '明日计划', diagnosis: '持仓诊断', screener: '筛选选中' }[s] || s
}
function stLabel(s) {
  return { pending: '待执行', filled: '已成交', rejected: '已拒绝' }[s] || s
}

async function load() {
  try {
    const d = await apiFetch('/api/sim/state')
    if (d.success) {
      state.value = d
      fetchedAt.value = Date.now()
    }
  } catch (e) { /* 静默，登录态由全局处理 */ }
}
async function onGenerate() {
  if (!sources.value.length) { toastError('请至少选择一个信号源'); return }
  busy.value = true
  try {
    const symbols = symbolsText.value.split(/[,，\s]+/).map(s => s.trim()).filter(Boolean)
    const d = await apiFetch('/api/sim/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sources: sources.value, symbols })
    })
    if (d.success) {
      showNotification(`已生成 ${d.created_count} 条建议，${d.rejected_count} 条被护栏拦截`)
      await load()
    }
  } catch (e) { showNotification('生成失败：' + (e.message || e), 'error') }
  finally { busy.value = false }
}
async function onExecute() {
  busy.value = true
  try {
    const d = await apiFetch('/api/sim/execute', { method: 'POST' })
    if (d.success) {
      showNotification(`成交 ${d.filled_count} 条，拦截 ${d.rejected_count} 条`)
      await load()
    }
  } catch (e) { showNotification('成交失败：' + (e.message || e), 'error') }
  finally { busy.value = false }
}
async function onReset() {
  if (!confirm('确认重置模拟账户？持仓与订单将清空，现金回到初始 100 万。')) return
  busy.value = true
  try {
    const d = await apiFetch('/api/sim/reset', { method: 'POST' })
    if (d.success) { showNotification('模拟账户已重置'); await load() }
  } catch (e) { showNotification('重置失败：' + (e.message || e), 'error') }
  finally { busy.value = false }
}

onMounted(load)
</script>

<style scoped>
.sim-trade { padding: 12px 16px 40px; color: var(--text-primary, #1f2937); }
.sim-banner {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  background: #fff7ed; border: 1px solid #fed7aa; color: #9a3412;
  padding: 8px 12px; border-radius: 8px; font-size: 13px; margin-bottom: 14px;
}
.dark .sim-banner { background: #3a2a18; border-color: #7c4a1e; color: #fbbf24; }
.sim-badge { background: #f97316; color: #fff; padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; }
.sim-reset { margin-left: auto; background: transparent; border: 1px solid #fdba74; color: #9a3412; padding: 4px 12px; border-radius: 6px; cursor: pointer; }
.sim-reset:disabled { opacity: .5; cursor: default; }

.sim-account { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 16px; }
.acc-card { background: var(--bg-tertiary, #f8fafc); border: 1px solid var(--border-subtle, #e5e7eb); border-radius: 10px; padding: 10px 14px; }
.acc-label { font-size: 12px; color: var(--text-secondary, #64748b); }
.acc-val { font-size: 18px; font-weight: 700; margin-top: 4px; }
.acc-val.strong { color: #4f46e5; }
.up { color: #dc2626; } .down { color: #16a34a; }
.dark .up { color: #f87171; } .dark .down { color: #4ade80; }

.sim-actions { background: var(--bg-tertiary, #f8fafc); border: 1px solid var(--border-subtle, #e5e7eb); border-radius: 10px; padding: 12px 14px; margin-bottom: 18px; }
.src-group { display: flex; gap: 16px; flex-wrap: wrap; align-items: center; margin-bottom: 10px; }
.src-item { font-size: 13px; display: flex; align-items: center; gap: 4px; cursor: pointer; }
.sym-input { flex: 1; min-width: 280px; padding: 6px 10px; border: 1px solid var(--border-subtle, #e5e7eb); border-radius: 6px; background: var(--bg-input, #fff); color: var(--text-primary); font-size: 13px; }
.btn-group { display: flex; gap: 10px; align-items: center; }
.btn { padding: 7px 16px; border-radius: 8px; border: 1px solid #c7d2fe; background: #eef2ff; color: #4338ca; cursor: pointer; font-size: 13px; font-weight: 600; }
.btn.primary { background: #4f46e5; border-color: #4f46e5; color: #fff; }
.btn:disabled { opacity: .5; cursor: default; }
.busy-tip { font-size: 12px; color: var(--text-secondary); }

.sim-section { margin-bottom: 22px; }
.sec-title { font-size: 15px; font-weight: 700; margin: 0 0 8px; }
.sec-sub { font-size: 12px; font-weight: 400; color: var(--text-secondary, #64748b); margin-left: 6px; }
.sim-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.sim-table th, .sim-table td { padding: 7px 10px; border-bottom: 1px solid var(--border-subtle, #e5e7eb); text-align: left; }
.sim-table th { color: var(--text-secondary, #64748b); font-weight: 600; background: var(--bg-tertiary, #f8fafc); }
.sim-table .num { text-align: right; }
.sim-table .mono { font-family: ui-monospace, monospace; }
.sim-table .reason { color: var(--text-secondary, #64748b); max-width: 320px; }
.side { padding: 2px 8px; border-radius: 6px; font-size: 12px; font-weight: 600; }
.side.buy { background: #fee2e2; color: #dc2626; }
.side.sell { background: #dcfce7; color: #16a34a; }
.st { padding: 2px 8px; border-radius: 6px; font-size: 12px; }
.st.pending { background: #fef9c3; color: #a16207; }
.st.filled { background: #dbeafe; color: #1d4ed8; }
.st.rejected { background: #f3f4f6; color: #6b7280; }
.empty { color: var(--text-secondary, #94a3b8); font-size: 13px; padding: 16px; text-align: center; border: 1px dashed var(--border-subtle, #e5e7eb); border-radius: 8px; }
</style>
