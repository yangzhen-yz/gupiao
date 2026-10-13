<template>
  <div class="desk">
    <!-- ========== 一句话结论（sticky：办公室窄窗口滚到哪儿都知道该干什么） ========== -->
    <section :class="['hero', 'hero-' + hero.level]" v-if="data">
      <div class="hero-phase">
        <span :class="['phase-dot', { live: isLive }]"></span>
        <b>{{ data.phaseLabel }}</b>
        <span v-if="data.nextPhaseAt" class="phase-next">
          {{ data.minutesToNext }} 分钟后 → {{ data.nextPhaseAt }}
        </span>
        <span v-else-if="!data.isTradingDay" class="phase-next">今日休市</span>
      </div>
      <h2 class="hero-text">{{ hero.text }}</h2>
      <p class="hero-sub" v-if="hero.sub">{{ hero.sub }}</p>

      <div class="hero-market" v-if="data.market && (data.market.riskScore || data.market.indexes.length)">
        <span class="risk-chip" :class="riskChipClass(data.market.riskScore)">
          大盘风险 {{ data.market.riskScore }}
        </span>
        <span v-for="ix in data.market.indexes" :key="ix.name" class="idx-chip">
          {{ ix.name }} <b :class="numClass(ix.changePercent)">{{ fmtPct(ix.changePercent) }}</b>
        </span>
      </div>

      <div class="hero-foot">
        <span class="asof">数据 {{ asOfText }}<template v-if="data.degraded.length"> · {{ data.degraded.length }} 项暂未就绪</template></span>
        <button class="refresh-btn" :disabled="loading" @click="load(true)">
          {{ loading ? '刷新中…' : '↻ 立即刷新' }}
        </button>
      </div>
    </section>

    <!-- 交易日历跨年守卫：绝不静默把未知年份当交易日 -->
    <div v-if="data && !data.calendarCovered" class="calendar-banner">
      ⚠️ 交易日历未覆盖本年度，以下时段判断仅供参考。请更新服务端休市安排。
    </div>

    <!-- 首次加载 / 失败 -->
    <div v-if="loading && !data" class="desk-empty">正在汇总今日行动清单…</div>
    <div v-else-if="error && !data" class="desk-empty error">
      {{ error }}
      <button class="refresh-btn" @click="load(true)">重试</button>
    </div>

    <!-- ========== 区块：顺序完全由服务端按当前时段决定 ========== -->
    <template v-for="(sec, i) in sections" :key="sec">
      <!-- 持仓待办 -->
      <section v-if="sec === 'positionActions'" class="block">
        <header class="block-head">
          <h3>📌 持仓待办</h3>
          <span class="block-meta">
            {{ data.positionActions.length }} 条需处理<span v-if="data.holdCount"> · {{ data.holdCount }} 只持有中</span>
          </span>
        </header>
        <div v-if="!data.positionActions.length" class="block-empty">持仓暂无触发信号 —— 这本身就是好消息，不用盯。</div>
        <div v-else class="stack">
          <article
            v-for="p in data.positionActions"
            :key="p.symbol"
            :class="['act-card', { urgent: p.urgent }]"
            @click="goQuote(p.symbol)"
          >
            <div class="act-top">
              <span class="act-name">{{ p.name }}<em>{{ (p.symbol || '').toUpperCase() }}</em></span>
              <span :class="['act-badge', 'badge-' + (p.action || 'hold')]">{{ p.actionLabel || '关注' }}</span>
            </div>
            <div class="act-nums">
              <span>现价 <b>{{ p.price ?? '--' }}</b></span>
              <span>盈亏 <b :class="numClass(p.pnlPct)">{{ fmtPct(p.pnlPct) }}</b></span>
              <span v-if="p.stop">止损 <b class="danger">{{ p.stop }}</b></span>
              <span v-if="p.target">目标 <b class="danger">{{ p.target }}</b></span>
              <span v-if="p.trailingLine">移动止盈 <b>{{ p.trailingLine }}</b></span>
            </div>
            <p class="act-detail" v-if="p.detail">{{ p.detail }}</p>
            <div class="act-flags" v-if="p.flags && p.flags.length">
              <span v-for="f in p.flags" :key="f" class="mini-flag">{{ f }}</span>
            </div>
          </article>
          <div class="block-foot">
            <button class="link-btn" @click="goTab('tab-position')">去持仓管理处理全部 →</button>
          </div>
        </div>
      </section>

      <!-- 现在可买 -->
      <section v-else-if="sec === 'buyNow'" class="block">
        <header class="block-head">
          <h3>🎯 此时机可介入</h3>
          <span class="block-meta">
            推荐日 {{ data.freshness.recommendation || '--' }}<template v-if="!data.freshness.recommendationToday"> · 今日尚未生成</template>
          </span>
        </header>
        <div v-if="!data.buyNow.length" class="block-empty">
          {{ data.freshness.recommendationToday
              ? '本时段没有匹配买入时机的标的 —— 按纪律等待，不盘中追高。'
              : '今日推荐还没生成（每日 09:45 / 13:30 / 14:45 自动产出）。' }}
        </div>
        <div v-else class="stack">
          <article v-for="b in data.buyNow" :key="b.symbol" class="buy-card" @click="goQuote(b.symbol)">
            <div class="buy-top">
              <span class="buy-name">{{ b.name }}<em>{{ (b.symbol || '').toUpperCase() }}</em></span>
              <span :class="['timing-badge', 'timing-' + b.timingType]">{{ timingIcon(b.timingType) }} {{ b.timingLabel || '盘中' }}</span>
              <span class="buy-score">评分 <b>{{ b.score }}</b></span>
            </div>
            <div class="buy-price-row">
              <span class="bp-label">现价</span><b :class="numClass(b.changePercent)">{{ b.price }}</b>
              <span :class="numClass(b.changePercent)">{{ fmtPct(b.changePercent) }}</span>
            </div>
            <div class="buy-points">
              <div class="bp"><em>买入参考</em><b class="ok">{{ b.buy ?? '--' }}</b></div>
              <div class="bp"><em>目标卖点</em><b class="danger">{{ b.target ?? '--' }}</b></div>
              <div class="bp"><em>止损价</em><b class="ok">{{ b.stop ?? '--' }}</b></div>
              <div class="bp wide"><em>仓位建议</em><b>{{ b.positionSuggest || '按计划' }}</b></div>
            </div>
            <p class="buy-note" v-if="b.execHint">⚠️ {{ b.execHint }}</p>
            <p class="buy-logic" v-if="b.logic">{{ b.logic }}</p>
          </article>
        </div>
        <div v-if="data.deferredBuys && data.deferredBuys.length" class="deferred">
          <span class="deferred-label">稍后时机：</span>
          <button v-for="d in data.deferredBuys" :key="d.symbol" class="deferred-chip" @click="goQuote(d.symbol)">
            {{ d.name }} · {{ d.timingLabel || '待时机' }}
          </button>
        </div>
        <div class="block-foot">
          <button class="link-btn" @click="goTab('tab-recommend')">查看完整推荐与复盘 →</button>
        </div>
      </section>

      <!-- 明日计划 -->
      <section v-else-if="sec === 'tomorrowPlan'" class="block">
        <header class="block-head">
          <h3>🗓️ 明日计划</h3>
          <span class="block-meta">收评 {{ data.freshness.review || '--' }}</span>
        </header>
        <div v-if="!data.tomorrowPlan.length" class="block-empty">
          今日收评还没有"重点关注"名单（15:10 左右生成）。可以先过一遍自选异动。
        </div>
        <div v-else class="stack">
          <article v-for="t in data.tomorrowPlan" :key="t.symbol" class="plan-card" @click="goQuote(t.symbol)">
            <div class="plan-top">
              <span class="plan-name">{{ t.name }}<em>{{ (t.symbol || '').toUpperCase() }}</em></span>
              <span :class="['timing-badge', 'timing-' + (t.timingType || 'intraday')]" v-if="t.timingLabel">
                {{ timingIcon(t.timingType) }} {{ t.timingLabel }}
              </span>
            </div>
            <p class="plan-cond" v-if="t.plan && t.plan.condition">{{ t.plan.condition }}</p>
            <div class="plan-nums" v-if="t.plan">
              <span v-if="t.plan.buyLow || t.plan.buyHigh">低吸区 <b>{{ t.plan.buyLow ?? '--' }}~{{ t.plan.buyHigh ?? '--' }}</b></span>
              <span v-if="t.plan.takeProfit">止盈 <b class="danger">{{ t.plan.takeProfit }}</b></span>
              <span v-if="t.plan.stopLoss">止损 <b class="ok">{{ t.plan.stopLoss }}</b></span>
            </div>
            <p class="plan-veto" v-if="t.plan && t.plan.veto">🚫 {{ t.plan.veto }}</p>
          </article>
        </div>
        <div class="block-foot">
          <button class="link-btn" @click="goTab('tab-review')">去每日收评看上下文 →</button>
        </div>
      </section>

      <!-- 大盘/账户环境建议 -->
      <section v-else-if="sec === 'tradeWindow'" class="block">
        <header class="block-head"><h3>🌡️ 当前环境与账户状态</h3></header>
        <div v-if="!data.tradeWindow || !data.tradeWindow.title" class="block-empty">
          暂无环境建议（休市时段不重复计算）。
        </div>
        <article v-else :class="['env-card', 'env-' + (data.tradeWindow.level || 'info')]">
          <div class="env-title">{{ data.tradeWindow.title }}</div>
          <p class="env-detail" v-if="data.tradeWindow.detail">{{ data.tradeWindow.detail }}</p>
          <div class="env-nums">
            <span v-if="data.tradeWindow.totalPnlPct != null">
              账户今日 <b :class="numClass(data.tradeWindow.totalPnlPct)">{{ fmtPct(data.tradeWindow.totalPnlPct) }}</b>
            </span>
            <span v-if="data.tradeWindow.urgentSells">急卖信号 <b class="danger">{{ data.tradeWindow.urgentSells }}</b> 只</span>
            <span v-if="data.tradeWindow.suggestedTotal">{{ data.tradeWindow.suggestedTotal }}</span>
          </div>
          <ul class="env-warns" v-if="data.tradeWindow.warnings && data.tradeWindow.warnings.length">
            <li v-for="(w, wi) in data.tradeWindow.warnings" :key="wi">{{ w }}</li>
          </ul>
        </article>
      </section>

      <!-- 自选异动 -->
      <section v-else-if="sec === 'watchlist'" class="block">
        <header class="block-head">
          <h3>👀 自选异动</h3>
          <span class="block-meta">关注 {{ data.watchlistCount }} 只<span v-if="isLive"> · {{ data.watchlistAlerts.length }} 只有动静</span></span>
        </header>
        <div v-if="!isLive" class="block-empty">休市时段不取实时行情，开盘后自动恢复。</div>
        <div v-else-if="!data.watchlistAlerts.length" class="block-empty">自选股波动平稳（|涨跌|≥3% 或放量才提示），可以不用盯。</div>
        <div v-else class="watch-grid">
          <button v-for="w in data.watchlistAlerts" :key="w.symbol" class="watch-item" @click="goQuote(w.symbol)">
            <span class="w-name">{{ w.name || w.symbol }}</span>
            <span class="w-price">{{ w.price }}</span>
            <span :class="['w-pct', numClass(w.changePercent)]">{{ fmtPct(w.changePercent) }}</span>
            <span v-for="f in w.flags" :key="f" class="mini-flag">{{ f }}</span>
          </button>
        </div>
      </section>

      <!-- 复盘入口（午间/盘后/日历未覆盖时） -->
      <section v-else-if="sec === 'reviewDigest'" class="block">
        <header class="block-head"><h3>📖 复盘</h3></header>
        <article class="digest-card" @click="goTab('tab-review')">
          <div class="digest-title">每日收评 · {{ data.freshness.review || '尚未生成' }}</div>
          <p class="digest-sub">
            {{ data.freshness.reviewToday
                ? '今日已收盘产出，含明日重点关注与买卖条件。'
                : '最新一份仍是历史日期；15:10 左右会生成今日收评。' }}
          </p>
          <span class="link-btn">打开收评 →</span>
        </article>
      </section>

      <div v-if="i < sections.length - 1" class="block-sep"></div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { apiFetch } from '../../utils/helpers'
import { usePolling } from '../../composables/usePolling'
import { navigate } from '../../composables/useHashRoute'

/**
 * 今日行动台（2026-10-13）
 *
 * 设计原则：这一页**不重新发明任何数据**，只把服务端按当前时段排好序的既有字段渲染出来。
 * 时段判定、区块顺序、一句话结论全部来自 GET /api/action-desk —— 前端不再自己猜"现在几点该干什么"。
 */
const props = defineProps({
  active: { type: Boolean, default: true },
})

const data = ref(null)
const loading = ref(false)
const error = ref('')

const LIVE_PHASES = ['auction', 'morning', 'lunch', 'mid', 'tail']
const isLive = computed(() => !!data.value && LIVE_PHASES.includes(data.value.phase))
const sections = computed(() => (data.value && data.value.sections) || [])
const hero = computed(() => (data.value && data.value.headline) || { level: 'muted', text: '正在汇总…', sub: '' })

const asOfText = computed(() => {
  const v = data.value && data.value.asOf
  if (!v) return '--'
  const t = String(v).slice(11, 16)
  return t || String(v)
})

// 轮询间隔由服务端按时段给（尾盘 30s、午间 5min、休市 30min）；拿不到就退回 60s
const { start } = usePolling(() => load(false), {
  interval: () => (data.value && data.value.nextPollMs) || 60000,
  active: () => props.active,
})

async function load(force) {
  if (loading.value) return
  loading.value = true
  error.value = ''
  try {
    // dev 覆盖时段：?phase=tail 便于在非尾盘时段截图验证尾盘版式
    const override = new URLSearchParams(location.search).get('phase')
    const url = '/api/action-desk' + (force && override ? `?phase=${encodeURIComponent(override)}` : '')
    const r = await apiFetch(url)
    if (r && r.success) {
      data.value = r.data
    } else {
      error.value = (r && r.error) || '行动台数据获取失败'
    }
  } catch (e) {
    error.value = e && e.status === 401 ? '' : '行动台数据获取失败，请检查服务是否在运行'
  } finally {
    loading.value = false
  }
}

function goQuote(symbol) {
  if (symbol) navigate('quote', symbol)
}
function goTab(tabId) {
  navigate(tabId)
}

// A股配色约定：红=涨，绿=跌
function numClass(v) {
  const n = Number(v)
  if (!Number.isFinite(n) || n === 0) return 'flat'
  return n > 0 ? 'up' : 'down'
}
function fmtPct(v) {
  if (v === null || v === undefined || v === '') return '--'
  const n = Number(v)
  if (!Number.isFinite(n)) return '--'
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%'
}
function riskChipClass(v) {
  const n = Number(v) || 0
  if (n >= 60) return 'risk-high'
  if (n >= 40) return 'risk-mid'
  return 'risk-low'
}
function timingIcon(type) {
  return { auction: '🔔', early: '⚡', tail: '🌙', pullback: '📉', intraday: '⏱️' }[type] || '⏱️'
}

onMounted(() => {
  if (data.value) start()
  else load(false).then(start)
})
</script>

<style scoped>
.desk { display: flex; flex-direction: column; gap: 0; }

/* ===== Hero ===== */
.hero {
  position: sticky;
  top: 0;
  z-index: 20;
  margin: 0 calc(-1 * var(--spacing-md)) 1rem;
  padding: 0.9375rem 1.125rem 0.8125rem;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-medium);
  background: var(--bg-secondary);
  box-shadow: var(--shadow-medium);
}
@media (min-width: 1200px) { .hero { margin: 0 0 1rem; } }
.hero-error { border-color: var(--accent-danger-border); background: linear-gradient(180deg, #fff5f5 0%, var(--bg-secondary) 70%); }
.hero-warning { border-color: var(--accent-warning-border); background: linear-gradient(180deg, #fffaf3 0%, var(--bg-secondary) 70%); }
.hero-info { border-color: var(--accent-info-border); background: linear-gradient(180deg, #f2f9fe 0%, var(--bg-secondary) 70%); }
.hero-muted { background: var(--bg-secondary); }

.hero-phase {
  display: flex; align-items: center; flex-wrap: wrap; gap: 0.5rem;
  font-size: 0.8125rem; color: var(--text-secondary); margin-bottom: 0.375rem;
}
.hero-phase b { color: var(--text-primary); font-weight: 700; }
.phase-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-tertiary); flex-shrink: 0; }
.phase-dot.live { background: var(--accent-danger); box-shadow: 0 0 0 3px rgba(220, 38, 38, 0.15); }
.phase-next { font-variant-numeric: tabular-nums; color: var(--text-tertiary); }

.hero-text { margin: 0; font-size: 1.1875rem; line-height: 1.4; font-weight: 700; color: var(--text-primary); }
.hero-sub { margin: 0.3125rem 0 0; font-size: 0.8125rem; line-height: 1.6; color: var(--text-secondary); }

.hero-market { display: flex; flex-wrap: wrap; align-items: center; gap: 0.375rem 0.625rem; margin-top: 0.625rem; }
.risk-chip, .idx-chip {
  font-size: 0.75rem; padding: 2px 8px; border-radius: var(--radius-pill);
  border: 1px solid var(--border-subtle); background: var(--bg-tertiary); color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}
.risk-chip.risk-high { background: var(--accent-danger-bg); border-color: var(--accent-danger-border); color: var(--accent-danger); font-weight: 700; }
.risk-chip.risk-mid { background: var(--accent-warning-bg); border-color: var(--accent-warning-border); color: #b45309; font-weight: 700; }
.risk-chip.risk-low { background: var(--accent-success-bg); border-color: var(--accent-success-border); color: var(--accent-success); }
.idx-chip b { color: var(--text-primary); font-weight: 600; }

.hero-foot { display: flex; align-items: center; gap: 0.75rem; margin-top: 0.625rem; }
.asof { font-size: 0.75rem; color: var(--text-tertiary); font-variant-numeric: tabular-nums; }
.refresh-btn {
  margin-left: auto; padding: 4px 12px; font-size: 0.75rem; font-family: inherit;
  color: var(--text-secondary); background: var(--bg-tertiary);
  border: 1px solid var(--border-medium); border-radius: var(--radius-sm); cursor: pointer;
}
.refresh-btn:hover:not(:disabled) { border-color: var(--accent-primary); color: var(--text-primary); }
.refresh-btn:disabled { opacity: 0.6; cursor: default; }

.calendar-banner {
  padding: 0.5rem 0.75rem; margin-bottom: 0.875rem; font-size: 0.8125rem;
  color: #92400e; background: #fef3c7; border: 1px solid #fcd34d; border-radius: var(--radius-sm);
}
.desk-empty {
  padding: 3rem 1.5rem; text-align: center; color: var(--text-tertiary);
  background: var(--bg-secondary); border: 1px dashed var(--border-medium); border-radius: var(--radius-md);
}
.desk-empty.error { color: var(--accent-danger); }
.desk-empty .refresh-btn { display: inline-block; margin: 0.75rem auto 0; }

/* ===== Block 外壳 ===== */
.block { margin-bottom: 1.125rem; }
.block-head { display: flex; align-items: baseline; flex-wrap: wrap; gap: 0.5rem 0.75rem; margin-bottom: 0.5rem; }
.block-head h3 { margin: 0; font-size: 0.9375rem; font-weight: 700; color: var(--text-primary); }
.block-meta { font-size: 0.75rem; color: var(--text-tertiary); font-variant-numeric: tabular-nums; }
.block-empty {
  padding: 1.125rem 1rem; font-size: 0.8125rem; line-height: 1.6; color: var(--text-tertiary);
  background: var(--bg-secondary); border: 1px dashed var(--border-subtle); border-radius: var(--radius-md);
}
.block-foot { margin-top: 0.5rem; }
.link-btn {
  background: none; border: none; padding: 0; font-family: inherit; font-size: 0.8125rem;
  color: var(--accent-secondary); cursor: pointer; text-align: left;
}
.link-btn:hover { color: var(--accent-primary); text-decoration: underline; }
.block-sep { height: 1px; background: var(--border-subtle); margin: 0 0 1.125rem; }

.stack { display: flex; flex-direction: column; gap: 0.625rem; }

/* 窄窗口（办公室默认 500–900px）保持单列 + 卡片不挤压 */
@media (min-width: 1100px) {
  .stack { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.75rem; }
}

.act-card, .buy-card, .plan-card, .env-card, .digest-card {
  padding: 0.8125rem 0.9375rem; background: var(--bg-secondary);
  border: 1px solid var(--border-subtle); border-radius: var(--radius-md);
  box-shadow: var(--shadow-soft); transition: border-color .12s, box-shadow .12s;
}
.act-card, .buy-card, .plan-card, .digest-card { cursor: pointer; }
.act-card:hover, .buy-card:hover, .plan-card:hover, .digest-card:hover {
  border-color: var(--border-strong); box-shadow: var(--shadow-medium);
}
.act-card.urgent { border-left: 3px solid var(--accent-danger); }

.act-top, .buy-top, .plan-top { display: flex; align-items: center; flex-wrap: wrap; gap: 0.5rem; }
.act-name, .buy-name, .plan-name { font-size: 0.9375rem; font-weight: 700; color: var(--text-primary); }
.act-name em, .buy-name em, .plan-name em {
  font-style: normal; font-size: 0.6875rem; font-weight: 500; color: var(--text-tertiary);
  margin-left: 0.375rem; letter-spacing: .02em;
}
.buy-score { margin-left: auto; font-size: 0.75rem; color: var(--text-tertiary); }
.buy-score b { color: var(--accent-warning); font-size: 0.875rem; }

.act-badge { font-size: 0.75rem; font-weight: 700; padding: 2px 8px; border-radius: var(--radius-pill); border: 1px solid transparent; }
.badge-stop_loss { background: var(--accent-danger-bg); color: var(--accent-danger); border-color: var(--accent-danger-border); }
.badge-reduce { background: var(--accent-warning-bg); color: #b45309; border-color: var(--accent-warning-border); }
.badge-take_profit { background: var(--accent-success-bg); color: var(--accent-success); border-color: var(--accent-success-border); }
.badge-trailing, .badge-hold { background: var(--bg-tertiary); color: var(--text-secondary); border-color: var(--border-subtle); }

.act-nums, .plan-nums, .env-nums, .buy-price-row {
  display: flex; flex-wrap: wrap; gap: 0.25rem 1rem; margin-top: 0.5rem;
  font-size: 0.8125rem; color: var(--text-secondary); font-variant-numeric: tabular-nums;
}
.act-nums b, .plan-nums b, .env-nums b { color: var(--text-primary); font-weight: 600; }
.act-detail, .buy-logic, .plan-cond {
  margin: 0.4375rem 0 0; font-size: 0.75rem; line-height: 1.6; color: var(--text-tertiary);
}
.act-flags { margin-top: 0.375rem; display: flex; flex-wrap: wrap; gap: 0.25rem; }
.mini-flag {
  font-size: 0.6875rem; padding: 1px 6px; border-radius: var(--radius-xs);
  background: var(--accent-info-bg); color: var(--accent-info); border: 1px solid var(--accent-info-border);
}

.buy-price-row { align-items: baseline; }
.bp-label { color: var(--text-tertiary); }
.buy-price-row b { font-size: 1rem; }
.buy-points {
  display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0.375rem; margin-top: 0.625rem;
}
.bp {
  padding: 0.375rem 0.5rem; background: var(--bg-tertiary);
  border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); text-align: center;
}
.bp.wide { grid-column: 1 / -1; text-align: left; }
.bp em { display: block; font-style: normal; font-size: 0.6875rem; color: var(--text-tertiary); margin-bottom: 2px; }
.bp b { font-size: 0.9375rem; color: var(--text-primary); font-variant-numeric: tabular-nums; }

.buy-note {
  margin: 0.5rem 0 0; padding: 0.3rem 0.5rem; font-size: 0.75rem; line-height: 1.5; color: #b45309;
  background: #fffbeb; border: 1px dashed #fcd34d; border-radius: var(--radius-sm);
}
.plan-veto {
  margin: 0.4375rem 0 0; font-size: 0.75rem; line-height: 1.5; color: var(--accent-danger);
}

/* 买入时机徽章（与智能推荐页同一套语义色） */
.timing-badge {
  display: inline-flex; align-items: center; gap: 0.25rem; padding: 2px 8px;
  border-radius: var(--radius-pill); font-size: 0.75rem; font-weight: 700;
  white-space: nowrap; border: 1px solid transparent;
}
.timing-auction { background: #f5f3ff; color: #6d28d9; border-color: #ddd6fe; }
.timing-early { background: #fef2f2; color: #dc2626; border-color: #fecaca; }
.timing-tail { background: #fff7ed; color: #c2410c; border-color: #fed7aa; }
.timing-pullback { background: #eff6ff; color: #1d4ed8; border-color: #bfdbfe; }
.timing-intraday { background: #f8fafc; color: #475569; border-color: #e2e8f0; }

.deferred {
  display: flex; flex-wrap: wrap; align-items: center; gap: 0.375rem;
  margin-top: 0.625rem; padding-top: 0.625rem; border-top: 1px dashed var(--border-subtle);
}
.deferred-label { font-size: 0.75rem; color: var(--text-tertiary); }
.deferred-chip {
  font-family: inherit; font-size: 0.75rem; padding: 2px 8px; cursor: pointer;
  color: var(--text-secondary); background: var(--bg-tertiary);
  border: 1px solid var(--border-subtle); border-radius: var(--radius-pill);
}
.deferred-chip:hover { border-color: var(--border-strong); color: var(--text-primary); }

.env-card.env-warning { border-left: 3px solid var(--accent-warning); }
.env-card.env-danger, .env-card.env-error { border-left: 3px solid var(--accent-danger); }
.env-card.env-success { border-left: 3px solid var(--accent-success); }
.env-title { font-size: 0.875rem; font-weight: 700; color: var(--text-primary); }
.env-detail { margin: 0.375rem 0 0; font-size: 0.8125rem; line-height: 1.6; color: var(--text-secondary); }
.env-warns { margin: 0.5rem 0 0; padding-left: 1.125rem; font-size: 0.75rem; line-height: 1.7; color: var(--text-tertiary); }

.watch-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 0.5rem; }
.watch-item {
  display: flex; align-items: center; flex-wrap: wrap; gap: 0.375rem;
  padding: 0.5rem 0.625rem; font-family: inherit; text-align: left; cursor: pointer;
  background: var(--bg-secondary); border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm); box-shadow: var(--shadow-xs);
}
.watch-item:hover { border-color: var(--border-strong); }
.w-name { font-size: 0.8125rem; font-weight: 600; color: var(--text-primary); }
.w-price { font-size: 0.75rem; color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.w-pct { margin-left: auto; font-size: 0.8125rem; font-weight: 700; font-variant-numeric: tabular-nums; }

.digest-title { font-size: 0.875rem; font-weight: 700; color: var(--text-primary); }
.digest-sub { margin: 0.375rem 0 0.5rem; font-size: 0.8125rem; line-height: 1.6; color: var(--text-secondary); }

/* A股语义色（红涨绿跌） */
.up { color: var(--accent-danger); }
.down { color: var(--accent-success); }
.flat { color: var(--text-secondary); }
.danger { color: var(--accent-danger); }
.ok { color: var(--accent-success); }
</style>
