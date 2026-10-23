<template>
  <div class="sim-trade">
    <!-- 顶部说明横幅 -->
    <div class="sim-banner">
      <span class="sim-badge">纯模拟盘</span>
      <span>本页所有交易均为模拟，不连接任何券商、不产生真实下单。用于在隔离账户中验证策略闭环。</span>
      <button class="sim-reset" @click="onReset" :disabled="busy">重置账户</button>
    </div>

    <DataFreshness
      :updated="fetchedAt"
      label="账户状态读取于"
      stale-hint="数据不是今日的，建议刷新"
      :loading="busy"
      @refresh="load"
    />

    <!-- 账户面板 -->
    <div class="sim-account" v-if="state.account">
      <div class="acc-card">
        <div class="acc-label">可用现金</div>
        <div class="acc-val">¥{{ fmtNum(state.account.cash) }}</div>
      </div>
      <div class="acc-card">
        <div class="acc-label">持仓市值</div>
        <div class="acc-val">¥{{ fmtNum(state.account.marketValue) }}</div>
      </div>
      <div class="acc-card">
        <div class="acc-label">账户总值</div>
        <div class="acc-val strong">¥{{ fmtNum(state.account.totalValue) }}</div>
      </div>
      <div class="acc-card">
        <div class="acc-label">当日盈亏</div>
        <div class="acc-val" :class="state.account.dayPnl >= 0 ? 'up' : 'down'">
          {{ state.account.dayPnl >= 0 ? '+' : '' }}{{ fmtNum(state.account.dayPnl) }} ({{ state.account.dayPnlPct }}%)
        </div>
      </div>
      <div class="acc-card">
        <div class="acc-label">累计盈亏</div>
        <div class="acc-val" :class="state.account.totalPnl >= 0 ? 'up' : 'down'">
          {{ state.account.totalPnl >= 0 ? '+' : '' }}{{ fmtNum(state.account.totalPnl) }} ({{
            state.account.totalPnlPct
          }}%)
        </div>
      </div>
    </div>

    <!-- 操作区 -->
    <div class="sim-actions">
      <div class="src-group">
        <label class="src-item"
          ><input type="checkbox" v-model="sources" value="recommend" /> 智能推荐（预期优先·最多3只）</label
        >
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
      <h3 class="sec-title">
        模拟持仓 <span class="sec-sub">{{ state.positions.length }} 只</span>
      </h3>
      <table class="sim-table" v-if="state.positions.length">
        <thead>
          <tr>
            <th>名称</th>
            <th>现价</th>
            <th>盈亏</th>
            <th>股数</th>
            <th>成本</th>
            <th>市值</th>
            <th>占比</th>
            <th class="ops">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in state.positions" :key="p.symbol">
            <td class="nm">
              <b>{{ p.name }}</b
              ><small class="mono">{{ p.symbol }}</small>
              <small
                v-if="p.advice"
                class="pos-advice"
                :class="p.advice.action"
                :title="p.advice.detail || p.advice.lockedReason"
              >
                {{ p.advice.locked ? '今日买入 · 明日可卖' : p.advice.label }}
              </small>
            </td>
            <td class="num">{{ p.price }}</td>
            <td class="num" :class="p.pnl >= 0 ? 'up' : 'down'">
              {{ p.pnl >= 0 ? '+' : '' }}{{ fmtNum(p.pnl) }} ({{ p.pnlPct }}%)
            </td>
            <td class="num">{{ p.shares }}</td>
            <td class="num">{{ p.costPrice }}</td>
            <td class="num">{{ fmtNum(p.marketValue) }}</td>
            <td class="num">{{ p.weight }}%</td>
            <td class="ops">
              <button
                v-if="halfQty(p) > 0"
                class="mini"
                :disabled="busy || p.advice?.locked"
                :title="p.advice?.locked ? p.advice.lockedReason : '卖出一半，取整到 100 股'"
                @click="onSell(p, halfQty(p))"
              >
                半仓
              </button>
              <button
                class="mini sell"
                :disabled="busy || p.advice?.locked"
                :title="p.advice?.locked ? p.advice.lockedReason : '按现价卖出全部持仓'"
                @click="onSell(p, p.shares)"
              >
                清仓
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <div class="empty" v-else>暂无模拟持仓</div>
    </div>

    <!-- 待执行建议 -->
    <div class="sim-section">
      <h3 class="sec-title">
        待执行建议 <span class="sec-sub">{{ state.pending.length }} 条</span>
      </h3>
      <table class="sim-table" v-if="state.pending.length">
        <thead>
          <tr>
            <th>方向</th>
            <th>名称</th>
            <th>建议价</th>
            <th>数量</th>
            <th>来源</th>
            <th class="reason">理由</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="o in state.pending" :key="o.id">
            <td>
              <span class="side" :class="o.side">{{ o.side === 'buy' ? '买入' : '卖出' }}</span>
            </td>
            <td class="nm">
              <b>{{ o.name }}</b
              ><small class="mono">{{ o.symbol }}</small>
            </td>
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
      <h3 class="sec-title">
        审计日志 <span class="sec-sub">最近 {{ state.audit.length }} 条</span>
      </h3>
      <table class="sim-table log">
        <thead>
          <tr>
            <th>状态</th>
            <th>方向</th>
            <th>名称</th>
            <th class="hide-sm">建议价</th>
            <th>成交价</th>
            <th>数量</th>
            <th class="reason">来源 / 拒绝原因</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="o in state.audit" :key="o.id">
            <td>
              <span class="st" :class="o.status">{{ stLabel(o.status) }}</span>
            </td>
            <td>
              <span class="side" :class="o.side">{{ o.side === 'buy' ? '买入' : '卖出' }}</span>
            </td>
            <td class="nm">
              <b>{{ o.name }}</b
              ><small class="mono">{{ o.symbol }}</small>
            </td>
            <td class="num hide-sm">{{ o.proposed_price }}</td>
            <td class="num">{{ o.filled_price || '-' }}</td>
            <td class="num">{{ o.filled_qty || o.proposed_qty }}</td>
            <!-- 被拒的单子这列是中文拒因（后端本来就写中文）；成交/待执行的单子这列是来源，
                 之前直接印 o.source 把 recommend 这种内部枚举名甩给了用户 -->
            <td class="reason" v-if="o.status === 'rejected'">{{ o.reject_reason }}</td>
            <td class="reason" v-else>{{ srcLabel(o.source) }}</td>
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
  import { fmtNum } from '../../utils/format'
  import DataFreshness from '../DataFreshness.vue'

  const { showNotification } = useNotification()
  const state = ref({ account: null, positions: [], pending: [], audit: [] })
  const sources = ref(['recommend', 'tomorrow_plan', 'diagnosis', 'screener'])
  const symbolsText = ref('')
  const busy = ref(false)
  // 新鲜度（2026-10-13）：此前本页既无轮询也没有"更新于"，看不出看到的是不是今天的账户状态
  const fetchedAt = ref('')

  function srcLabel(s) {
    return (
      {
        recommend: '智能推荐',
        tomorrow_plan: '明日计划',
        diagnosis: '持仓诊断',
        screener: '筛选选中',
        manual: '手动卖出',
      }[s] || s
    )
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
    } catch (e) {
      /* 静默，登录态由全局处理 */
    }
  }
  async function onGenerate() {
    if (!sources.value.length) {
      showNotification('请至少选择一个信号源', 'error')
      return
    }
    busy.value = true
    try {
      const symbols = symbolsText.value
        .split(/[,，\s]+/)
        .map(s => s.trim())
        .filter(Boolean)
      const d = await apiFetch('/api/sim/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sources: sources.value, symbols }),
      })
      if (d.success) {
        showNotification(`已生成 ${d.created_count ?? 0} 条建议，${d.rejected_count ?? 0} 条被护栏拦截`)
        await load()
      }
    } catch (e) {
      showNotification('生成失败：' + (e.message || e), 'error')
    } finally {
      busy.value = false
    }
  }
  async function onExecute() {
    busy.value = true
    try {
      const d = await apiFetch('/api/sim/execute', { method: 'POST' })
      if (d.success) {
        showNotification(`成交 ${d.filled_count ?? 0} 条，拦截 ${d.rejected_count ?? 0} 条`)
        await load()
      }
    } catch (e) {
      showNotification('成交失败：' + (e.message || e), 'error')
    } finally {
      busy.value = false
    }
  }
  // 半仓数量：取整到 100 股；不足一手返回 0，按钮不出现
  function halfQty(p) {
    return Math.floor(((p && p.shares) || 0) / 2 / 100) * 100
  }
  // 手动卖出（2026-10-23）：给模拟持仓一个按现价直接了结的出口，
  // 不用等诊断触发止损/止盈才生成卖单。qty 由按钮算好，后端仍做 T+1 与持仓校验。
  async function onSell(p, qty) {
    qty = Number(qty) || 0
    if (qty <= 0) {
      showNotification('可卖数量不足一手', 'error')
      return
    }
    const all = qty >= (p.shares || 0)
    if (!confirm(`按现价${all ? '清仓' : '卖出一半'}：${p.name} ${qty} 股？（纯模拟，不影响真钱）`)) return
    busy.value = true
    try {
      const d = await apiFetch('/api/sim/sell', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol: p.symbol, qty }),
      })
      if (d.success) {
        showNotification(`已卖出 ${d.name} ${d.qty} 股 @ ${d.price}`)
        await load()
      } else {
        showNotification(d.detail || d.message || '卖出失败', 'error')
      }
    } catch (e) {
      showNotification('卖出失败：' + (e.message || e), 'error')
    } finally {
      busy.value = false
    }
  }
  async function onReset() {
    if (!confirm('确认重置模拟账户？持仓与订单将清空，现金回到初始 100 万。')) return
    busy.value = true
    try {
      const d = await apiFetch('/api/sim/reset', { method: 'POST' })
      if (d.success) {
        showNotification('模拟账户已重置')
        await load()
      }
    } catch (e) {
      showNotification('重置失败：' + (e.message || e), 'error')
    } finally {
      busy.value = false
    }
  }

  onMounted(load)
</script>

<style scoped>
  .sim-trade {
    padding: 12px 16px 40px;
    color: var(--text-primary, #1f2937);
  }
  .sim-banner {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    background: #fff7ed;
    border: 1px solid #fed7aa;
    color: #9a3412;
    padding: 8px 12px;
    border-radius: 8px;
    font-size: 13px;
    margin-bottom: 14px;
  }
  .dark .sim-banner {
    background: #3a2a18;
    border-color: #7c4a1e;
    color: #fbbf24;
  }
  .sim-badge {
    background: #f97316;
    color: #fff;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
  }
  .sim-reset {
    margin-left: auto;
    background: transparent;
    border: 1px solid #fdba74;
    color: #9a3412;
    padding: 4px 12px;
    border-radius: 6px;
    cursor: pointer;
  }
  .sim-reset:disabled {
    opacity: 0.5;
    cursor: default;
  }

  .sim-account {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 10px;
    margin-bottom: 16px;
  }
  .acc-card {
    background: var(--bg-tertiary, #f8fafc);
    border: 1px solid var(--border-subtle, #e5e7eb);
    border-radius: 10px;
    padding: 10px 14px;
  }
  .acc-label {
    font-size: 12px;
    color: var(--text-secondary, #64748b);
  }
  .acc-val {
    font-size: 18px;
    font-weight: 700;
    margin-top: 4px;
  }
  .acc-val.strong {
    color: #4f46e5;
  }
  .up {
    color: #dc2626;
  }
  .down {
    color: #16a34a;
  }
  .dark .up {
    color: #f87171;
  }
  .dark .down {
    color: #4ade80;
  }

  .sim-actions {
    background: var(--bg-tertiary, #f8fafc);
    border: 1px solid var(--border-subtle, #e5e7eb);
    border-radius: 10px;
    padding: 12px 14px;
    margin-bottom: 18px;
  }
  .src-group {
    display: flex;
    gap: 16px;
    flex-wrap: wrap;
    align-items: center;
    margin-bottom: 10px;
  }
  .src-item {
    font-size: 13px;
    display: flex;
    align-items: center;
    gap: 4px;
    cursor: pointer;
  }
  .sym-input {
    flex: 1;
    min-width: 280px;
    padding: 6px 10px;
    border: 1px solid var(--border-subtle, #e5e7eb);
    border-radius: 6px;
    background: var(--bg-input, #fff);
    color: var(--text-primary);
    font-size: 13px;
  }
  .btn-group {
    display: flex;
    gap: 10px;
    align-items: center;
  }
  .btn {
    padding: 7px 16px;
    border-radius: 8px;
    border: 1px solid #c7d2fe;
    background: #eef2ff;
    color: #4338ca;
    cursor: pointer;
    font-size: 13px;
    font-weight: 600;
  }
  .btn.primary {
    background: #4f46e5;
    border-color: #4f46e5;
    color: #fff;
  }
  .btn:disabled {
    opacity: 0.5;
    cursor: default;
  }
  .busy-tip {
    font-size: 12px;
    color: var(--text-secondary);
  }

  .sim-section {
    margin-bottom: 22px;
    /* 持仓表加了"操作"列后列数变多：容器兜一层横向滚动。
       原来只在 ≤699px 生效，但 700–900px 的窄桌面也可能放不下，统一放到基础样式，
       表格能放下时不会出现滚动条，放不下时才可横向拖。 */
    overflow-x: auto;
  }
  .sec-title {
    font-size: 15px;
    font-weight: 700;
    margin: 0 0 8px;
  }
  .sec-sub {
    font-size: 12px;
    font-weight: 400;
    color: var(--text-secondary, #64748b);
    margin-left: 6px;
  }
  .sim-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  /* 对齐治理（2026-10-14）：原来 th/td 一律左对齐、数字列单独右对齐，
   结果表头在左、数字在右，同一列两个起点看着像错行。现在整表居中，
   只有"理由/来源"这种长句子保持左对齐（居中排长句更难读）。 */
  .sim-table th,
  .sim-table td {
    padding: 7px 10px;
    border-bottom: 1px solid var(--border-subtle, #e5e7eb);
    text-align: center;
  }
  .sim-table th {
    color: var(--text-secondary, #64748b);
    font-weight: 600;
    background: var(--bg-tertiary, #f8fafc);
  }
  .sim-table .num {
    text-align: center;
    font-variant-numeric: tabular-nums;
  }
  /* 名称列：名字在上、代码降成小字灰字 —— 代码不再独占一列，但也没被丢掉 */
  .sim-table .nm b {
    display: block;
    font-weight: 600;
    color: var(--text-primary, #111827);
  }
  .sim-table .nm small {
    display: block;
    font-size: 11px;
    color: var(--text-tertiary, #94a3b8);
    margin-top: 1px;
  }
  .sim-table .mono {
    font-family: ui-monospace, monospace;
  }
  /* 模拟持仓的卖出操作（2026-10-23）：小按钮，避免把表格撑得过宽 */
  .sim-table .ops {
    white-space: nowrap;
  }
  .sim-table .mini {
    padding: 4px 8px;
    margin: 0 2px;
    font-size: 11px;
    font-family: inherit;
    color: var(--text-secondary, #64748b);
    background: var(--bg-primary, #fff);
    border: 1px solid var(--border-medium, #d1d5db);
    border-radius: 5px;
    cursor: pointer;
  }
  .sim-table .mini.sell {
    color: #dc2626;
    border-color: #fca5a5;
    background: #fef2f2;
  }
  .dark .sim-table .mini.sell {
    color: #fca5a5;
    border-color: #7f1d1d;
    background: #2a1515;
  }
  .sim-table .mini:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }
  .sim-table .nm .pos-advice {
    font-weight: 600;
    white-space: nowrap;
  }
  /* 动作配色与真实持仓页的 .action-* 徽章保持一致：止损红、止盈红、减仓橙、
     冲高回落黄、止损上移蓝、持有/观察灰。 */
  .sim-table .nm .pos-advice.stop_loss,
  .sim-table .nm .pos-advice.take_profit {
    color: #dc2626;
  }
  .sim-table .nm .pos-advice.reduce {
    color: #d97706;
  }
  .sim-table .nm .pos-advice.giveback {
    color: #a16207;
  }
  .sim-table .nm .pos-advice.hold_trailing {
    color: #1d4ed8;
  }
  .sim-table .nm .pos-advice.watch,
  .sim-table .nm .pos-advice.hold {
    color: var(--text-tertiary, #94a3b8);
    font-weight: 400;
  }
  .sim-table .reason {
    text-align: left;
    color: var(--text-secondary, #64748b);
    max-width: 320px;
  }
  /* 手机上 7 列表格会撑破 420px 视口（实测右边界 431px）：收窄内边距，
   再给容器兜一层横向滚动，避免"内容被屏幕切掉又没滚动条"那种看不全的情况。 */
  @media (max-width: 699px) {
    .sim-table {
      font-size: 12px;
    }
    .sim-table th,
    .sim-table td {
      padding: 6px 5px;
    }
    .sim-table .reason {
      max-width: 220px;
    }
    /* 手机上按钮别缩成点不中的小点：保留足够触控面积 */
    .sim-table .mini {
      padding: 6px 8px;
    }
    /* 8 列在手机上一定放不下：把「操作」列钉在右侧，横向拖动时卖出按钮始终可见 */
    .sim-table .ops {
      position: sticky;
      right: 0;
      z-index: 1;
      background: var(--bg-secondary, #fff);
      box-shadow: -4px 0 6px rgba(15, 23, 42, 0.06);
    }
    .sim-table th.ops {
      background: var(--bg-tertiary, #f8fafc);
    }
    /* 徽章被挤成竖排（"已成交"三个字一行一个）比宽列更糟：先锁住不换行 */
    .sim-table .st,
    .sim-table .side,
    .sim-table .nm b {
      white-space: nowrap;
    }
    /* 审计日志的"建议价"手机上让位给成交价 —— 拒单原因那句中文里本来就带着建议价 */
    .sim-table.log .hide-sm {
      display: none;
    }
  }
  .side {
    padding: 2px 8px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 600;
  }
  .side.buy {
    background: #fee2e2;
    color: #dc2626;
  }
  .side.sell {
    background: #dcfce7;
    color: #16a34a;
  }
  .st {
    padding: 2px 8px;
    border-radius: 6px;
    font-size: 12px;
  }
  .st.pending {
    background: #fef9c3;
    color: #a16207;
  }
  .st.filled {
    background: #dbeafe;
    color: #1d4ed8;
  }
  .st.rejected {
    background: #f3f4f6;
    color: #6b7280;
  }
  .empty {
    color: var(--text-secondary, #94a3b8);
    font-size: 13px;
    padding: 16px;
    text-align: center;
    border: 1px dashed var(--border-subtle, #e5e7eb);
    border-radius: 8px;
  }
</style>
