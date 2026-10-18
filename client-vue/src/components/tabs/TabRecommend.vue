<template>
  <div>
    <div class="section-title">AI智能选股推荐</div>
    <div class="section-subtitle">
      基于综合评分模型，每日精选3支最具上涨潜力的股票，附精准买卖点提示 · 每日 09:45 / 13:30 / 14:45
      自动生成并保存，收盘前快照确保"最后一次推荐"落库可复盘
    </div>
    <div
      class="scan-controls"
      style="background: #f8fafc; padding: 20px; border-radius: 12px; border: 1px solid #e2e8f0; margin-bottom: 24px"
    >
      <button
        class="btn-scan"
        style="padding: 12px 24px; display: flex; align-items: center; gap: 8px"
        @click="generateRecommendations"
        :disabled="generating"
      >
        <span>🎯</span> {{ generating ? '生成中...' : '生成今日推荐' }}
      </button>
      <button
        v-if="todayRecommend.length > 0"
        class="btn-secondary"
        style="padding: 12px 24px"
        @click="saveRecommendations"
      >
        💾 保存推荐记录
      </button>
    </div>
    <div class="refresh-controls">
      <div class="toggle-section"><span class="toggle-label">推荐成功率</span></div>
      <div class="last-update">{{ recommendStats }}</div>
    </div>
    <!-- 新鲜度（2026-10-13）：本页不轮询，若不标出"这是哪天的推荐"，收盘后很容易被当成今日信号 -->
    <DataFreshness
      :updated="todayDate"
      :stale="!todayRecommend.length"
      :day-compare="false"
      label="当前推荐日期"
      stale-hint="今日推荐尚未生成"
      @refresh="loadRecommendations"
    />
    <div v-if="reviewData.available" class="review-summary-box">
      <div class="review-summary-title">
        📊 推荐复盘（近{{ reviewData.recent_days }}天 · 共{{ reviewData.total?.t1?.n || 0 }}期）
      </div>
      <div class="review-summary-row">
        <span class="review-metric">
          <em>低吸参考价 T+1</em>
          <b
            :style="{ color: (reviewData.total?.t1?.avg || 0) >= 0 ? 'var(--accent-danger)' : 'var(--accent-success)' }"
          >
            胜率{{ reviewData.total?.t1?.win_rate ?? '--' }}% · 均值{{ (reviewData.total?.t1?.avg ?? 0) >= 0 ? '+' : ''
            }}{{ reviewData.total?.t1?.avg ?? '--' }}%
          </b>
        </span>
        <span class="review-metric">
          <em>现价追入 T+1</em>
          <b
            :style="{
              color: (reviewData.total?.exec_t1?.avg || 0) >= 0 ? 'var(--accent-danger)' : 'var(--accent-success)',
            }"
          >
            胜率{{ reviewData.total?.exec_t1?.win_rate ?? '--' }}% · 均值{{
              (reviewData.total?.exec_t1?.avg ?? 0) >= 0 ? '+' : ''
            }}{{ reviewData.total?.exec_t1?.avg ?? '--' }}%
          </b>
        </span>
        <span class="review-metric">
          <em>吃面(跌>5%)</em>
          <b>{{ reviewData.total?.noodle_count ?? 0 }} 只</b>
        </span>
        <span class="review-metric">
          <em>5日平均回撤</em>
          <b style="color: var(--accent-success)">{{ reviewData.total?.dd5_avg ?? '--' }}%</b>
        </span>
      </div>
      <div class="review-summary-hint">
        💡 数据显示：低吸/尾盘介入才有正期望，盘中追高易被次日低开吃掉——请按卡片「买入时机」徽章执行
      </div>
    </div>
    <div class="recommend-section">
      <div class="section-header">
        <div class="section-subtitle-small">今日推荐股票</div>
        <span class="section-stats">{{ todayDate }}</span>
      </div>
      <div
        v-if="todayRecommend.length === 0"
        style="
          text-align: center;
          padding: 4rem 2rem;
          color: #94a3b8;
          background: #f8fafc;
          border-radius: 16px;
          border: 2px dashed #e2e8f0;
          margin: 1rem 0;
        "
      >
        ✨ 准备就绪，点击上方按钮生成今日潜力股
      </div>
      <div
        v-for="(r, idx) in todayRecommend"
        :key="r.symbol"
        :class="['recommend-card', `top-${idx + 1}`]"
        @click="openKlineFor(r, todayDate)"
        style="cursor: pointer"
        title="点击查看K线（标记推荐日）"
      >
        <div class="recommend-header">
          <div class="recommend-rank-badge">{{ idx === 0 ? '🥇' : idx === 1 ? '🥈' : '🥉' }}</div>
          <div class="recommend-info-main">
            <div class="recommend-name-row">
              <span class="recommend-name-text">{{ r.name }}</span>
              <span
                v-if="r.placement_info"
                class="placement-badge"
                :title="`${r.placement_info.notice_date} ${r.placement_info.title}`"
                >📢 近期增发</span
              >
              <span class="recommend-code-text">{{ r.symbol.toUpperCase() }}</span>
              <button class="kline-hint-btn" @click.stop="openKlineFor(r, todayDate)" title="查看K线（标记推荐日）">
                📈 K线
              </button>
            </div>
          </div>
          <div
            class="recommend-price-large"
            :style="{ color: r.change >= 0 ? 'var(--accent-danger)' : 'var(--accent-success)' }"
          >
            {{ r.price }}
          </div>
        </div>
        <div class="recommend-stats-grid">
          <div class="recommend-stat-box">
            <span class="recommend-stat-label">今日涨跌</span>
            <span
              class="recommend-stat-value"
              :style="{ color: r.change >= 0 ? 'var(--accent-danger)' : 'var(--accent-success)' }"
              >{{ r.change >= 0 ? '+' : '' }}{{ r.changePercent?.toFixed(2) || 0 }}%</span
            >
          </div>
          <div class="recommend-stat-box">
            <span class="recommend-stat-label">AI 综合评分</span>
            <span
              class="recommend-stat-value"
              :style="{
                color:
                  r.score >= 70
                    ? 'var(--accent-danger)'
                    : r.score >= 40
                      ? 'var(--accent-warning)'
                      : 'var(--accent-success)',
              }"
              >{{ r.score }} 分</span
            >
          </div>
        </div>
        <div v-if="r.buySellPoints || r.buyPoint" class="recommend-points-row">
          <div class="recommend-point-card buy">
            <div class="recommend-point-label">买入参考</div>
            <div class="recommend-point-value" style="color: var(--accent-success)">
              {{ r.buySellPoints?.buy || r.buyPoint }}
            </div>
          </div>
          <div class="recommend-point-card current">
            <div class="recommend-point-label">推荐时价</div>
            <div class="recommend-point-value" style="color: var(--accent-primary)">
              {{ r.buySellPoints?.current || r.price }}
            </div>
          </div>
          <div class="recommend-point-card sell">
            <div class="recommend-point-label">目标卖点</div>
            <div class="recommend-point-value" style="color: var(--accent-danger)">
              {{ r.buySellPoints?.sell || r.sellPoint }}
            </div>
          </div>
        </div>
        <div v-if="r.buySellPoints?.buyTiming" class="recommend-timing-row">
          <span class="timing-badge" :class="'timing-' + r.buySellPoints.buyTiming.type">
            {{ timingIcon(r.buySellPoints.buyTiming.type) }} {{ r.buySellPoints.buyTiming.label }}
          </span>
          <span class="recommend-timing-note">{{ r.buySellPoints.buyTiming.note }}</span>
          <span v-if="r.buySellPoints.buyTiming.execHint" class="recommend-exec-hint"
            >⚠️ {{ r.buySellPoints.buyTiming.execHint }}</span
          >
        </div>
        <!-- 执行诚实化（2026-10-18，P0-1）：低吸区间是系统真正的 edge（历史 T+1 胜率 60.4%），
             所以它做成默认那颗按钮；"按现价买"这颗才是被置灰的对象（43.2% 那个口径）。 -->
        <div v-if="buyRange(r)" class="recommend-exec">
          <button class="exec-btn primary" @click="copyPlan(r, 'low')">
            挂低吸单 {{ buyRange(r) }}
          </button>
          <button class="exec-btn chase" :class="{ off: isChase(r) }" :disabled="isChase(r)"
            @click="copyPlan(r, 'now')">
            {{ isChase(r) ? '现价追高 · 已置灰' : '按现价 ' + nowPrice(r) }}
          </button>
          <span class="exec-meta">
            现价{{ overTxt(r) }}；今天最多付到 {{ r.buySellPoints.buyTiming.limitPrice }}
          </span>
        </div>
        <div v-if="r.buySellPoints?.buyTiming?.exitNote" class="recommend-exit">
          {{ r.buySellPoints.buyTiming.exitNote }}
        </div>
        <div v-if="r.riskControl" class="recommend-risk-box">
          <div class="recommend-risk-title">🛡️ 风控建议</div>
          <div class="recommend-risk-row">
            <span class="risk-badge" :class="riskLevelClass(r.riskControl?.risk_level)">{{
              riskLabel(r.riskControl?.risk_level)
            }}</span>
            <span class="recommend-risk-item"
              >止损价 <b>{{ r.riskControl?.stop_loss }}</b
              ><small>（−{{ r.riskControl?.stop_loss_pct }}%）</small></span
            >
            <span class="recommend-risk-item">{{ r.riskControl?.position_suggest }}</span>
          </div>
          <div v-if="r.valuation?.overvalued" class="val-badge over">
            估值高估 PE{{ r.valuation?.pe_pct }}% · PB{{ r.valuation?.pb_pct }}%
          </div>
          <div class="recommend-risk-note">{{ r.riskControl?.risk_note }}</div>
        </div>
        <div class="recommend-reason-box">
          <div class="recommend-reason-text">{{ r.reason }}</div>
        </div>
      </div>
    </div>
    <div class="history-section">
      <div class="section-subtitle-small">历史推荐记录</div>
      <div v-if="historyRecommend.length === 0" style="text-align: center; padding: 2rem; color: #64748b">
        暂无历史记录
      </div>
      <div v-for="record in pagedHistoryRecommend" :key="record.date" class="history-item">
        <div class="history-date-header">📅 {{ record.date }}</div>
        <div class="history-stock-grid">
          <div
            v-for="r in record.daily_recommendations || record.stocks"
            :key="r.symbol"
            class="history-stock-card"
            @click="openKlineFor(r, record.date)"
            style="cursor: pointer"
            :title="`点击查看K线（标记 ${record.date} 推荐日）`"
          >
            <div class="history-stock-name-row">
              <div>
                <div
                  style="
                    font-weight: 700;
                    font-size: 0.95rem;
                    color: var(--text-primary);
                    white-space: nowrap;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    max-width: 100px;
                  "
                >
                  {{ r.name }}
                </div>
                <div style="font-size: 0.7rem; color: var(--text-tertiary)">{{ r.symbol?.toUpperCase() }}</div>
              </div>
              <div class="history-profit-badge" :class="getProfitClass(r)">{{ getProfitText(r) }}</div>
            </div>
            <div class="history-price-table">
              <span class="history-price-label">买入</span>
              <span class="history-price-val" style="color: var(--accent-danger)">{{
                r.buySellPoints?.buy || r.buyPoint || '--'
              }}</span>
              <span class="history-price-label">现价</span>
              <span class="history-price-val">{{ r.currentPrice || r.price || '--' }}</span>
              <span class="history-price-label">盈利</span>
              <span
                class="history-price-val"
                :style="{ color: getProfitValue(r) >= 0 ? 'var(--accent-danger)' : 'var(--accent-success)' }"
                >{{ getProfitText(r) }}</span
              >
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
  import { apiFetch, riskLabel, riskLevelClass } from '../../utils/helpers'
  import { timingIcon } from '../../utils/timing'
  import DataFreshness from '../DataFreshness.vue'

  // App 传入当前 Tab 是否可见（2026-10-13 轮询治理的统一接口；本页不轮询，仅声明）
  defineProps({ active: { type: Boolean, default: true } })
  const emit = defineEmits(['open-kline'])

  const showNotification = inject('showNotification')

  // 2026-10-16：点击股票卡片打开完整 K线弹窗，并带上"推荐日"作标记（不截断，标记线定位推荐日）
  function toISODate(s) {
    if (!s) return ''
    const d = String(s).replace(/\//g, '-')
    if (/^\d{4}-\d{2}-\d{2}$/.test(d)) return d
    const dt = new Date(s)
    if (!isNaN(dt.getTime())) return dt.toISOString().slice(0, 10)
    return d
  }
  function openKlineFor(r, dateStr) {
    if (!r || !r.symbol) return
    emit('open-kline', r.symbol, toISODate(dateStr))
  }

  /* ---------- 执行诚实化（2026-10-18，P0-1）----------
     区间与超价幅度都由后端 `services/strategy/buy_range.py` 算好随卡片下发，
     前端只做"读出来、按不按得下去"这一件事 —— 不在浏览器里再算一遍第二个口径。 */
  const bt = r => (r && r.buySellPoints && r.buySellPoints.buyTiming) || null
  function buyRange(r) {
    const t = bt(r)
    if (!t || !t.buyLow || !t.buyHigh) return ''
    return `${t.buyLow}~${t.buyHigh}`
  }
  function nowPrice(r) {
    return (r && r.buySellPoints && r.buySellPoints.current) || (r && r.price) || ''
  }
  // 追高判定用后端的 overPct（现价高出买入上限多少），阈值与后端警告文案对齐取 1%：
  // 低吸参考价按构造就低于现价，0.x% 的差是振幅噪声，不该因此把按钮锁死；
  // 越过 1% 才是"现价追入"那个 43.2% 的口径，这颗按钮就该按不下去。
  function isChase(r) {
    const t = bt(r)
    return !!(t && t.overPct != null && t.overPct >= 1)
  }
  function overTxt(r) {
    const t = bt(r)
    if (!t || t.overPct == null) return '与区间关系未知'
    if (t.overPct >= 1) return `已越过上限 ${t.overPct}%（这就是追高）`
    if (t.overPct > 0) return `高出 ${t.overPct}%（噪声范围内，可以买）`
    return `还低 ${Math.abs(t.overPct)}%（在区间内）`
  }
  function copyPlan(r, mode) {
    const t = bt(r)
    if (mode === 'now' && isChase(r)) {
      // 这颗按钮在追高时是 disabled 的，点不到；留着这层判断是因为键盘回车能绕过 disabled
      showNotification('现价已越过买入上限，这个口径历史 T+1 只有 43.2% 胜率——改挂低吸价', 'warning', 3000)
      return
    }
    const txt =
      mode === 'low'
        ? `${r.name || ''} ${r.symbol || ''} 挂低吸单 ${buyRange(r)}（追价上限 ${t.limitPrice}）`
        : `${r.name || ''} ${r.symbol || ''} 现价 ${nowPrice(r)}`
    const done = () => showNotification(`已复制：${txt}`, 'success', 2500)
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard
        .writeText(txt)
        .then(done)
        .catch(() => showNotification('复制失败，请按卡片上的价手动挂单', 'warning', 3000))
    } else {
      showNotification('这台浏览器不给剪贴板权限，请按卡片上的价手动挂单', 'warning', 3000)
    }
  }
  const todayRecommend = ref([])
  const historyRecommend = ref([])
  const todayDate = ref('--')
  const recommendStats = ref('--')
  const generating = ref(false)
  const reviewData = ref({})

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
          name: r.name,
          symbol: r.symbol,
          price: r.price,
          change: r.change,
          changePercent: r.changePercent,
          score: r.score,
          buySellPoints: r.buySellPoints || { buy: r.buyPoint, current: r.price, sell: r.sellPoint },
          reason: r.reason,
        })),
      }
      const result = await apiFetch('/api/daily_recommendations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(record),
      })
      if (result.success) {
        showNotification('保存成功！推荐记录已保存到数据库')
        await loadRecommendations()
      } else {
        showNotification('保存失败：' + (result.error || '未知错误'))
      }
    } catch (e) {
      showNotification('保存失败')
    }
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
    } catch (e) {
      console.error('加载推荐失败:', e)
    }
  }

  async function loadReview() {
    try {
      const result = await apiFetch('/api/recommend-review?days=30')
      if (result.success && result.data) {
        reviewData.value = result.data
      }
    } catch (e) {
      console.error('加载推荐复盘失败:', e)
    }
  }

  onMounted(() => {
    loadRecommendations()
    loadReview()
  })
</script>

<style scoped>
  /* 推荐复盘摘要（2026-10-12） */
  .review-summary-box {
    margin: 0 0 1rem;
    padding: 0.875rem 1rem;
    background: linear-gradient(135deg, #fefce8 0%, #fef9c3 100%);
    border: 1px solid #fde047;
    border-radius: 12px;
  }
  .review-summary-title {
    font-size: 0.875rem;
    font-weight: 700;
    color: #854d0e;
    margin-bottom: 0.5rem;
  }
  .review-summary-row {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem 1.5rem;
  }
  .review-metric {
    display: inline-flex;
    align-items: baseline;
    gap: 0.375rem;
    font-size: 0.8125rem;
  }
  .review-metric em {
    font-style: normal;
    color: #a16207;
  }
  .review-metric b {
    font-size: 0.9375rem;
  }
  .review-summary-hint {
    margin-top: 0.5rem;
    font-size: 0.75rem;
    color: #a16207;
    line-height: 1.5;
  }
  .recommend-exec-hint {
    flex-basis: 100%;
    font-size: 0.75rem;
    color: #b45309;
    background: #fffbeb;
    border: 1px dashed #fcd34d;
    border-radius: 6px;
    padding: 0.25rem 0.5rem;
    line-height: 1.5;
  }
  /* 执行按钮（2026-10-18，P0-1）：低吸是默认动作，现价追入才是被置灰的那个。
     配色不用红绿 —— 这两个颜色在本项目里已经欠给"涨/跌"了，按钮再抢就会读反。 */
  .recommend-exec {
    display: flex;
    align-items: center;
    gap: 0.375rem;
    flex-wrap: wrap;
    margin: 0.25rem 0 0;
  }
  .exec-btn {
    font: inherit;
    font-size: 0.75rem;
    font-weight: 700;
    padding: 0.25rem 0.625rem;
    border-radius: 6px;
    cursor: pointer;
    border: 1px solid var(--accent-primary, #2563eb);
    background: var(--accent-primary, #2563eb);
    color: #fff;
  }
  .exec-btn.chase {
    background: transparent;
    color: #b45309;
    border-color: #fcd34d;
  }
  .exec-btn.chase.off {
    color: #9ca3af;
    border-color: #e5e7eb;
    background: #f3f4f6;
    cursor: not-allowed;
    text-decoration: line-through;
  }
  .exec-meta {
    font-size: 0.6875rem;
    color: #6b7280;
  }
  .recommend-exit {
    margin-top: 0.25rem;
    font-size: 0.6875rem;
    line-height: 1.5;
    color: #6b7280;
    background: #f8fafc;
    border-left: 2px solid #cbd5e1;
    padding: 0.25rem 0.5rem;
  }
  /* 买入时机徽章（2026-10-12） */
  .recommend-timing-row {
    margin: 0.5rem 0 0.25rem;
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
    flex-wrap: wrap;
  }
  .timing-badge {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    padding: 0.25rem 0.625rem;
    border-radius: 999px;
    font-size: 0.8125rem;
    font-weight: 700;
    white-space: nowrap;
    border: 1px solid transparent;
    flex-shrink: 0;
  }
  .timing-badge.timing-auction {
    background: #f5f3ff;
    color: #6d28d9;
    border-color: #ddd6fe;
  }
  .timing-badge.timing-early {
    background: #fef2f2;
    color: #dc2626;
    border-color: #fecaca;
  }
  .timing-badge.timing-tail {
    background: #fff7ed;
    color: #c2410c;
    border-color: #fed7aa;
  }
  .timing-badge.timing-pullback {
    background: #eff6ff;
    color: #1d4ed8;
    border-color: #bfdbfe;
  }
  .timing-badge.timing-intraday {
    background: #f8fafc;
    color: #475569;
    border-color: #e2e8f0;
  }
  .recommend-timing-note {
    font-size: 0.75rem;
    color: #64748b;
    line-height: 1.5;
    flex: 1;
    min-width: 200px;
    padding-top: 0.125rem;
  }
  .recommend-risk-box {
    margin: 0.75rem 0 0.5rem;
    padding: 0.75rem 0.875rem;
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
  }
  .recommend-risk-title {
    font-size: 0.8125rem;
    font-weight: 700;
    color: #334155;
    margin-bottom: 0.5rem;
  }
  .recommend-risk-row {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.5rem 0.875rem;
  }
  .recommend-risk-item {
    font-size: 0.8125rem;
    color: #475569;
  }
  .recommend-risk-item b {
    color: var(--accent-danger);
    font-size: 0.9375rem;
  }
  .recommend-risk-item small {
    color: #94a3b8;
    font-weight: 400;
  }
  .recommend-risk-note {
    margin-top: 0.5rem;
    font-size: 0.75rem;
    color: #64748b;
    line-height: 1.5;
  }
  /* 看K线入口（2026-10-16）：推荐卡点击看推荐日之前走势 */
  .kline-hint-btn {
    margin-left: 0.375rem;
    padding: 0.1rem 0.5rem;
    font-size: 0.7rem;
    font-weight: 600;
    color: #1d4ed8;
    background: #eff6ff;
    border: 1px solid #bfdbfe;
    border-radius: 999px;
    cursor: pointer;
    white-space: nowrap;
  }
  .kline-hint-btn:hover {
    background: #dbeafe;
  }
</style>
