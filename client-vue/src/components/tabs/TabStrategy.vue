<template>
  <div>
    <div class="section-title">策略优化</div>
    <div class="section-subtitle">自适应选股策略 · 每日回测验证 · 动态权重调整 · 误判分析</div>

    <!-- 大盘预测/崩盘预警 -->
    <div class="market-predict-section">
      <div class="market-predict-section-title" @click="marketPredictExpanded = !marketPredictExpanded">
        <span>大盘预测与崩盘预警</span>
        <span class="collapse-arrow" :style="{ transform: marketPredictExpanded ? 'rotate(180deg)' : '' }">▼</span>
      </div>
      <div class="market-predict-section-content" :style="{ display: marketPredictExpanded ? 'block' : 'none' }">
        <div style="display:flex;gap:8px;margin-bottom:10px;">
          <button class="btn-secondary" style="padding:4px 12px;font-size:0.76rem;" @click="loadMarketPredict">刷新</button>
        </div>
        <!-- 预测结果 -->
        <div v-if="marketPredict" class="strategy-card" style="margin-bottom:10px;" :style="{ borderLeft: `3px solid ${predictBorderColor}` }">
          <div class="strategy-card-header">
            <span class="strategy-card-title">{{ marketPredict.prediction?.title || '加载中...' }}</span>
            <span v-if="marketPredict.riskScore !== undefined" :style="{ color: predictBorderColor, fontWeight: 700 }">风险评分 {{ marketPredict.riskScore }}/100</span>
          </div>
          <div style="font-size:0.85rem;color:var(--text-secondary);margin-bottom:8px;">{{ marketPredict.prediction?.message }}</div>
          <div class="strategy-action-box" :style="{ background: predictBgColor, color: predictTextColor, padding: '8px 12px', borderRadius: 'var(--radius-sm)', fontSize: '0.85rem' }">
            建议: {{ marketPredict.prediction?.action }}
          </div>
          <!-- 四维信号 -->
          <div v-if="marketPredict.signals" class="market-predict-signals">
            <div v-for="(sig, key) in marketPredict.signals" :key="key" class="predict-signal-item">
              <div class="predict-signal-header">
                <span class="predict-signal-name">{{ sig.name }}</span>
                <span class="predict-signal-value" :style="{ color: sig.status === 'danger' ? 'var(--accent-danger)' : sig.status === 'warning' ? 'var(--accent-warning)' : 'var(--accent-success)' }">{{ sig.value }}</span>
              </div>
              <div class="predict-signal-bar">
                <div class="predict-signal-fill" :class="sig.status === 'danger' ? 'danger' : sig.status === 'warning' ? 'warning' : 'safe'" :style="{ width: (sig.score / sig.maxScore * 100) + '%' }"></div>
              </div>
              <div class="predict-signal-detail">{{ sig.detail }}</div>
            </div>
          </div>
          <div style="font-size:0.75rem;color:var(--text-tertiary);margin-top:8px;">更新时间: {{ marketPredict.updateTime }}</div>
        </div>
        <div v-else class="empty-state" style="padding:1.5rem;">
          <div class="loading-spinner" style="margin:0 auto 0.5rem;" v-if="marketPredictLoading"></div>
          <span>{{ marketPredictLoading ? '正在分析...' : '点击刷新加载大盘预测' }}</span>
        </div>

        <!-- 崩盘预警 -->
        <div v-if="marketCrash" class="strategy-card" style="margin-bottom:10px;" :style="{ borderLeft: `3px solid ${crashBorderColor}` }">
          <div class="strategy-card-header">
            <span class="strategy-card-title">{{ marketCrash.suggestion?.title || '崩盘检测' }}</span>
            <span :style="{ color: crashBorderColor, fontWeight: 700 }">{{ crashLevelText }}</span>
          </div>
          <div style="font-size:0.85rem;color:var(--text-secondary);margin-bottom:8px;">{{ marketCrash.suggestion?.message }}</div>
          <div v-if="marketCrash.indexes && marketCrash.indexes.length" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;">
            <div v-for="idx in marketCrash.indexes" :key="idx.symbol" style="background:var(--bg-tertiary);padding:4px 10px;border-radius:var(--radius-sm);font-size:0.8rem;">
              <span style="font-weight:600;">{{ idx.name }}</span>
              <span :style="{ color: idx.changePercent >= 0 ? 'var(--accent-danger)' : 'var(--accent-success)', fontWeight: 700 }">{{ idx.price }} ({{ idx.changePercent >= 0 ? '+' : '' }}{{ idx.changePercent?.toFixed(2) }}%)</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 操作按钮 -->
    <div class="scan-controls" style="margin-bottom:24px;">
      <button class="btn-scan" style="padding:10px 20px;" @click="loadWeights">刷新权重</button>
      <button class="btn-scan" style="padding:10px 20px;" @click="runBacktest" :disabled="backtesting">{{ backtesting ? '回测中...' : '手动回测' }}</button>
      <button class="btn-secondary" style="padding:10px 20px;" @click="resetWeights">重置权重</button>
    </div>

    <!-- 策略权重卡片 -->
    <div style="margin-bottom:24px;">
      <div class="section-subtitle-small" style="margin-bottom:12px;">当前策略权重</div>
      <div class="strategy-weights-grid">
        <div v-for="w in weights" :key="w.factor" class="strategy-card">
          <div class="strategy-card-header">
            <span class="strategy-card-title">{{ w.description || w.factor }}</span>
            <span class="strategy-card-subtitle">{{ w.factor }}</span>
          </div>
          <div class="strategy-bar-row">
            <span class="strategy-bar-label">权重</span>
            <div class="strategy-bar-track">
              <div class="strategy-bar-fill" :style="{ width: Math.min(100, Math.round(w.weight * 100)) + '%', background: w.weight > 1.2 ? 'var(--accent-danger)' : w.weight < 0.8 ? 'var(--accent-info)' : 'var(--accent-success)' }"></div>
            </div>
            <span class="strategy-bar-value" :style="{ color: w.weight > 1.2 ? 'var(--accent-danger)' : w.weight < 0.8 ? 'var(--accent-info)' : 'var(--accent-success)' }">{{ w.weight.toFixed(2) }}</span>
          </div>
          <div class="strategy-bar-row">
            <span class="strategy-bar-label">准确率</span>
            <div class="strategy-bar-track">
              <div class="strategy-bar-fill" :style="{ width: w.accuracy + '%', background: w.accuracy > 60 ? 'var(--accent-success)' : w.accuracy < 40 ? 'var(--accent-danger)' : 'var(--accent-warning)' }"></div>
            </div>
            <span class="strategy-bar-value" :style="{ color: w.accuracy > 60 ? 'var(--accent-success)' : w.accuracy < 40 ? 'var(--accent-danger)' : 'var(--accent-warning)' }">{{ w.accuracy }}%</span>
          </div>
          <div class="strategy-card-footer">样本: {{ w.sample_count }} | 正确: {{ w.correct_count }}</div>
        </div>
      </div>
    </div>

    <!-- RPS 相对强度强势股榜（P0，2026-07-16） -->
    <div class="rps-table" style="margin-bottom:24px;">
      <div class="section-subtitle-small" style="margin-bottom:12px;display:flex;align-items:center;gap:10px;">
        <span>RPS 相对强度强势股榜（{{ rpsPeriod }}日）</span>
        <button class="btn-secondary" style="padding:4px 12px;font-size:0.76rem;" @click="refreshRps" :disabled="rpsRefreshing">{{ rpsRefreshing ? '计算中...' : '刷新预计算' }}</button>
      </div>
      <div v-if="rpsError" class="empty-state" style="padding:1.5rem;">{{ rpsError }}（点击"刷新预计算"触发全市场计算，约 30 秒）</div>
      <div v-else-if="rpsList.length" class="stock-table">
        <div class="stock-table-header">
          <span class="sortable" :class="{ active: rpsSortKey==='name', desc: rpsSortKey==='name' && rpsSortDesc }" @click.stop="sortRps('name')">名称</span>
          <span class="sortable" :class="{ active: rpsSortKey==='rps', desc: rpsSortKey==='rps' && rpsSortDesc }" @click.stop="sortRps('rps')">RPS</span>
          <span class="sortable" :class="{ active: rpsSortKey==='intervalChange', desc: rpsSortKey==='intervalChange' && rpsSortDesc }" @click.stop="sortRps('intervalChange')">区间涨幅</span>
          <span class="sortable" :class="{ active: rpsSortKey==='pullback_1m', desc: rpsSortKey==='pullback_1m' && rpsSortDesc }" @click.stop="sortRps('pullback_1m')">近1月回调</span>
          <span>K线</span>
        </div>
        <div v-for="s in pagedRpsList" :key="s.symbol" class="stock-table-row clickable" @click="goToQuoteRps(s.symbol)">
          <span class="name">{{ s.name }}<br><small style="color:var(--text-tertiary);">{{ s.symbol.toUpperCase() }}</small></span>
          <span :style="{ fontWeight:700, color: s.rps>=90?'var(--accent-danger)':s.rps>=70?'var(--accent-warning)':'var(--text-secondary)' }">{{ s.rps.toFixed(0) }}</span>
          <span :style="{ color: s.intervalChange>=0?'var(--accent-danger)':'var(--accent-success)', fontWeight:600 }">{{ s.intervalChange>=0?'+':'' }}{{ s.intervalChange.toFixed(2) }}%</span>
          <span :style="{ color: (s.pullback_1m==null?0:s.pullback_1m)>=0?'var(--accent-danger)':'var(--accent-success)', fontWeight:600 }">
            {{ s.pullback_1m == null ? '—' : (s.pullback_1m>=0?'+':'') + s.pullback_1m.toFixed(2) + '%' }}
          </span>
          <span>
            <button class="rps-kline-btn" @click.stop="emit('open-kline', s.symbol)" title="查看K线">K线</button>
          </span>
        </div>
      </div>
      <div v-if="rpsList.length && rpsTotalPages > 1" class="history-pagination">
        <button class="page-btn" :disabled="rpsPage <= 1" @click="rpsPage--">上一页</button>
        <span class="page-info">第 {{ rpsPage }} / {{ rpsTotalPages }} 页</span>
        <button class="page-btn" :disabled="rpsPage >= rpsTotalPages" @click="rpsPage++">下一页</button>
      </div>
      <div v-else class="empty-state" style="padding:1.5rem;">
        <div class="loading-spinner" style="margin:0 auto 0.5rem;" v-if="rpsLoading"></div>
        <span>{{ rpsLoading ? '加载中...' : '暂无 RPS 数据，点击"刷新预计算"' }}</span>
      </div>
    </div>

    <!-- 聪明钱信号（P1b，2026-07-16） -->
    <div class="smart-money" style="margin-bottom:24px;">
      <div class="section-subtitle-small" style="margin-bottom:12px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
        <span>聪明钱信号（龙虎榜 + 北向资金）</span>
        <button class="btn-secondary" style="padding:4px 12px;font-size:0.76rem;" @click="loadSmartMoney(true)" :disabled="smLoading">{{ smLoading ? '刷新中...' : '刷新' }}</button>
        <span style="font-size:0.72rem;color:var(--text-tertiary);">龙虎榜统计周期为近5日</span>
        <span v-if="dragonTigerLatestDate" style="font-size:0.72rem;color:var(--text-tertiary);margin-left:auto;">数据日期：{{ dragonTigerLatestDate }}</span>
      </div>

      <!-- 北向资金（市场级情绪） -->
      <div v-if="northbound" class="strategy-card" style="margin-bottom:12px;"
           :style="{ borderLeft: `3px solid ${northbound.sentiment==='bullish'?'var(--accent-danger)':northbound.sentiment==='bearish'?'var(--accent-success)':'var(--accent-info)'}` }">
        <div class="strategy-card-header">
          <span class="strategy-card-title">北向资金（沪深股通）</span>
          <span :style="{ fontWeight:700, color: northbound.sentiment==='bullish'?'var(--accent-danger)':northbound.sentiment==='bearish'?'var(--accent-success)':'var(--accent-info)' }">
            {{ northbound.sentiment==='bullish'?'净流入 · 偏多':northbound.sentiment==='bearish'?'净流出 · 偏空':'中性' }}
          </span>
        </div>
        <div style="display:flex;gap:16px;flex-wrap:wrap;font-size:0.85rem;margin-top:4px;">
          <span>当日沪股通: <b :style="{ color: northbound.sh_net>=0?'var(--accent-danger)':'var(--accent-success)' }">{{ fmtWan(northbound.sh_net) }}</b></span>
          <span>当日深股通: <b :style="{ color: northbound.sz_net>=0?'var(--accent-danger)':'var(--accent-success)' }">{{ fmtWan(northbound.sz_net) }}</b></span>
          <span>当日合计: <b :style="{ color: northbound.total>=0?'var(--accent-danger)':'var(--accent-success)' }">{{ fmtWan(northbound.total) }}</b></span>
          <span style="color:var(--text-tertiary)">近1月 沪 {{ fmtWan(northbound.month_sh) }} · 深 {{ fmtWan(northbound.month_sz) }}</span>
        </div>
        <div style="font-size:0.72rem;color:var(--text-tertiary);margin-top:6px;">数据日期: {{ northbound.date || '—' }} ｜ 更新: {{ northbound.updated }}</div>
      </div>

      <!-- 龙虎榜净买入榜（个股级聪明钱） -->
      <div v-if="dragonTigerError" class="empty-state" style="padding:1.5rem;">{{ dragonTigerError }}</div>
      <div v-else-if="dragonTigerList.length" class="stock-table smart-money-table">
        <div class="stock-table-header">
          <span class="sortable" :class="{ active: dragonSortKey==='name', desc: dragonSortKey==='name' && dragonSortDesc }" @click.stop="sortDragon('name')">名称</span>
          <span class="sortable" :class="{ active: dragonSortKey==='net_amt', desc: dragonSortKey==='net_amt' && dragonSortDesc }" @click.stop="sortDragon('net_amt')">龙虎榜净买入</span>
          <span class="sortable" :class="{ active: dragonSortKey==='appearances', desc: dragonSortKey==='appearances' && dragonSortDesc }" @click.stop="sortDragon('appearances')">近5日上榜次数</span>
          <span class="sortable" :class="{ active: dragonSortKey==='max_change', desc: dragonSortKey==='max_change' && dragonSortDesc }" @click.stop="sortDragon('max_change')">期间涨幅</span>
        </div>
        <div v-for="s in pagedDragonTigerList" :key="s.symbol" class="stock-table-row clickable" @click="goToQuoteRps(s.symbol)">
          <span class="name">{{ s.name }}<br><small style="color:var(--text-tertiary);">{{ s.symbol.toUpperCase() }}</small></span>
          <span :style="{ fontWeight:700, color: s.net_amt>=5e8?'var(--accent-danger)':s.net_amt>=1e8?'var(--accent-warning)':'var(--text-secondary)' }">{{ fmtYuan(s.net_amt) }}</span>
          <span>{{ s.appearances }} 次</span>
          <span :style="{ color: s.max_change>=0?'var(--accent-danger)':'var(--accent-success)', fontWeight:600 }">{{ s.max_change>=0?'+':'' }}{{ s.max_change.toFixed(1) }}%</span>
        </div>
      </div>
      <div v-if="dragonTigerList.length && dragonTigerTotalPages > 1" class="history-pagination">
        <button class="page-btn" :disabled="dragonTigerPage <= 1" @click="dragonTigerPage--">上一页</button>
        <span class="page-info">第 {{ dragonTigerPage }} / {{ dragonTigerTotalPages }} 页</span>
        <button class="page-btn" :disabled="dragonTigerPage >= dragonTigerTotalPages" @click="dragonTigerPage++">下一页</button>
      </div>
      <div v-else class="empty-state" style="padding:1.5rem;">
        <div class="loading-spinner" style="margin:0 auto 0.5rem;" v-if="smLoading"></div>
        <span>{{ smLoading ? '加载中...' : '暂无龙虎榜数据' }}</span>
      </div>
    </div>

    <!-- 回测历史 -->
    <div style="margin-bottom:24px;">
      <div class="section-subtitle-small" style="margin-bottom:12px;">回测报告</div>
      <div v-if="backtestHistory.length === 0" class="empty-state">暂无回测数据，每日15:20自动回测，或点击"手动回测"</div>
      <div v-for="bt in pagedBacktestHistory" :key="bt.backtest_date" class="backtest-card">
        <div class="backtest-header">
          <span class="backtest-date">{{ bt.backtest_date }}</span>
          <span class="backtest-market" :style="{ color: bt.market_change > 0 ? 'var(--accent-danger)' : bt.market_change < 0 ? 'var(--accent-success)' : 'var(--text-secondary)' }">
            {{ bt.market_change > 0 ? '📈 上涨' : bt.market_change < 0 ? '📉 下跌' : '➡️ 平盘' }}
            {{ bt.market_change > 0 ? '+' : '' }}{{ parseFloat(bt.market_change).toFixed(2) }}%
          </span>
        </div>
        <div class="backtest-grid">
          <div class="backtest-grid-item">
            <div class="backtest-grid-label">总准确率</div>
            <div class="backtest-grid-value" :style="{ color: bt.accuracy > 60 ? 'var(--accent-success)' : bt.accuracy < 40 ? 'var(--accent-danger)' : 'var(--accent-warning)' }">{{ bt.accuracy }}%</div>
          </div>
          <div class="backtest-grid-item">
            <div class="backtest-grid-label">看涨准确率</div>
            <div class="backtest-grid-value" style="color:var(--accent-danger);">{{ bt.bull_accuracy }}%</div>
          </div>
          <div class="backtest-grid-item">
            <div class="backtest-grid-label">预测总数</div>
            <div class="backtest-grid-value" style="color:var(--text-primary);">{{ bt.total_predictions }}</div>
          </div>
        </div>
        <div v-if="bt.misjudge_analysis" class="backtest-analysis">
          <div v-if="bt.misjudge_analysis.market_context" style="margin-bottom:4px;">📊 大盘: <span :style="{ color: bt.market_change > 0 ? 'var(--accent-danger)' : 'var(--accent-success)' }">{{ bt.misjudge_analysis.market_context }}</span></div>
          <div v-if="bt.misjudge_analysis.bull_misjudge_analysis" style="margin-bottom:4px;color:var(--accent-danger);">🔴 误判分析: {{ bt.misjudge_analysis.bull_misjudge_analysis }}</div>
          <div v-if="bt.misjudge_analysis.suggestions && bt.misjudge_analysis.suggestions.length" style="margin-top:6px;"><strong>优化建议:</strong></div>
          <div v-for="s in (bt.misjudge_analysis.suggestions || [])" :key="s" style="color:#7c3aed;">💡 {{ s }}</div>
        </div>
        <div v-if="bt.weight_adjustments && bt.weight_adjustments.filter(a => a.action !== 'keep').length" class="backtest-adjustments">
          <div><strong>权重调整:</strong></div>
          <div v-for="a in bt.weight_adjustments.filter(a => a.action !== 'keep')" :key="a.factor" style="font-size:0.8rem;">
            {{ a.action === 'increase' ? '⬆️' : '⬇️' }} {{ a.factor }}: {{ a.reason }}
          </div>
        </div>
      </div>
      <div v-if="backtestTotalPages > 1" class="history-pagination">
        <button class="page-btn" :disabled="backtestPage <= 1" @click="backtestPage--">上一页</button>
        <span class="page-info">第 {{ backtestPage }} / {{ backtestTotalPages }} 页</span>
        <button class="page-btn" :disabled="backtestPage >= backtestTotalPages" @click="backtestPage++">下一页</button>
      </div>
    </div>

    <!-- 预测记录 -->
    <div>
      <div class="section-subtitle-small" style="margin-bottom:12px;">预测记录（含历史待验证）</div>
      <div v-if="predictions.length === 0" class="empty-state">暂无预测记录，扫描股票后自动生成</div>
      <div v-else>
        <div class="stock-table">
          <div class="stock-table-header">
            <span>日期</span><span>名称</span><span>评分</span><span>涨跌幅</span><span>外盘占比</span><span>量比</span><span>委比</span><span>验证</span>
          </div>
          <div v-for="r in pagedPredictions" :key="r.id" class="stock-table-row">
            <span style="font-size:0.75rem;color:var(--text-secondary);font-weight:500;">{{ r.predict_date ? r.predict_date.slice(5) : '' }}</span>
            <span class="name">{{ r.name }}<br><small style="color:var(--text-tertiary);">{{ (r.symbol || '').toUpperCase() }}</small></span>
            <span :style="{ fontWeight: 600, color: r.score >= 70 ? 'var(--accent-danger)' : r.score >= 40 ? 'var(--accent-warning)' : 'var(--accent-success)' }">{{ r.score }}</span>
            <span :style="{ color: parseFloat(r.change_percent) >= 0 ? 'var(--accent-danger)' : 'var(--accent-success)' }">{{ parseFloat(r.change_percent) >= 0 ? '+' : '' }}{{ parseFloat(r.change_percent).toFixed(2) }}%</span>
            <span>{{ parseFloat(r.outer_ratio).toFixed(1) }}%</span>
            <span>{{ parseFloat(r.volume_ratio).toFixed(2) }}</span>
            <span :style="{ color: parseFloat(r.weibi) >= 0 ? 'var(--accent-danger)' : 'var(--accent-success)' }">{{ parseFloat(r.weibi) >= 0 ? '+' : '' }}{{ parseFloat(r.weibi).toFixed(2) }}%</span>
            <span :style="{ color: r.verified ? (r.is_correct ? 'var(--accent-success)' : 'var(--accent-danger)') : 'var(--accent-tertiary)', fontWeight: 600 }">{{ r.verified ? (r.is_correct ? '✅正确' : '❌错误') : '⏳待验证' }}</span>
          </div>
        </div>
        <div v-if="predictionTotalPages > 1" class="history-pagination">
          <button class="page-btn" :disabled="predictionPage <= 1" @click="predictionPage--">上一页</button>
          <span class="page-info">第 {{ predictionPage }} / {{ predictionTotalPages }} 页</span>
          <button class="page-btn" :disabled="predictionPage >= predictionTotalPages" @click="predictionPage++">下一页</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, inject } from 'vue'
import { apiFetch } from '../../utils/helpers'

const showNotification = inject('showNotification')
const navigateToQuote = inject('navigateToQuote')
const emit = defineEmits(['open-kline'])
const weights = ref([])
const backtestHistory = ref([])
const predictions = ref([])
const backtesting = ref(false)
const marketPredict = ref(null)
const marketPredictLoading = ref(false)
const marketCrash = ref(null)
const marketPredictExpanded = ref(true)

// RPS 相对强度强势股榜（P0，2026-07-16）
const rpsList = ref([])
const rpsLoading = ref(false)
const rpsRefreshing = ref(false)
const rpsError = ref('')
const rpsPeriod = ref(120)

const RPS_PAGE_SIZE = 10
const rpsPage = ref(1)
const rpsSortKey = ref('rps')
const rpsSortDesc = ref(true)

function sortRps(key) {
  if (rpsSortKey.value === key) {
    rpsSortDesc.value = !rpsSortDesc.value
  } else {
    rpsSortKey.value = key
    rpsSortDesc.value = true
  }
  rpsPage.value = 1
}

const sortedRpsList = computed(() => {
  const key = rpsSortKey.value
  return [...rpsList.value].sort((a, b) => {
    if (key === 'name') {
      const va = a.name || a.symbol || ''
      const vb = b.name || b.symbol || ''
      return rpsSortDesc.value ? vb.localeCompare(va, 'zh-CN') : va.localeCompare(vb, 'zh-CN')
    }
    let va = a[key]
    let vb = b[key]
    if (va == null) va = -Infinity
    if (vb == null) vb = -Infinity
    return rpsSortDesc.value ? vb - va : va - vb
  })
})

const rpsTotalPages = computed(() => Math.ceil(sortedRpsList.value.length / RPS_PAGE_SIZE))
const pagedRpsList = computed(() => {
  const start = (rpsPage.value - 1) * RPS_PAGE_SIZE
  return sortedRpsList.value.slice(start, start + RPS_PAGE_SIZE)
})

const PREDICTION_PAGE_SIZE = 5
const predictionPage = ref(1)
const predictionTotalPages = computed(() => Math.ceil(predictions.value.length / PREDICTION_PAGE_SIZE))
const pagedPredictions = computed(() => {
  const start = (predictionPage.value - 1) * PREDICTION_PAGE_SIZE
  return predictions.value.slice(start, start + PREDICTION_PAGE_SIZE)
})

const BACKTEST_PAGE_SIZE = 3
const backtestPage = ref(1)
const backtestTotalPages = computed(() => Math.ceil(backtestHistory.value.length / BACKTEST_PAGE_SIZE))
const pagedBacktestHistory = computed(() => {
  const start = (backtestPage.value - 1) * BACKTEST_PAGE_SIZE
  return backtestHistory.value.slice(start, start + BACKTEST_PAGE_SIZE)
})

const predictBorderColor = computed(() => {
  const s = marketPredict.value?.predictStatus
  if (s === 'severe_danger' || s === 'danger') return 'var(--accent-danger)'
  if (s === 'warning') return 'var(--accent-warning)'
  return 'var(--accent-success)'
})
const predictBgColor = computed(() => {
  const s = marketPredict.value?.predictStatus
  if (s === 'severe_danger' || s === 'danger') return 'rgba(220,38,38,0.08)'
  if (s === 'warning') return 'rgba(234,88,12,0.08)'
  return 'rgba(22,163,74,0.08)'
})
const predictTextColor = computed(() => {
  const s = marketPredict.value?.predictStatus
  if (s === 'severe_danger' || s === 'danger') return 'var(--accent-danger)'
  if (s === 'warning') return 'var(--accent-warning)'
  return 'var(--accent-success)'
})
const crashBorderColor = computed(() => {
  const s = marketCrash.value?.marketStatus
  if (s === 'severe_crash') return 'var(--accent-danger)'
  if (s === 'crash') return 'var(--accent-warning)'
  return 'var(--accent-success)'
})
const crashLevelText = computed(() => {
  const s = marketCrash.value?.crashLevel
  if (s === 'severe') return '暴跌'
  if (s === 'moderate') return '大跌'
  return '正常'
})

async function loadMarketPredict() {
  marketPredictLoading.value = true
  try {
    const [predictResult, crashResult] = await Promise.all([
      apiFetch('/api/market-predict'),
      apiFetch('/api/market-index')
    ])
    if (predictResult.success) marketPredict.value = predictResult.data
    if (crashResult.success) marketCrash.value = crashResult.data
  } catch (e) {
    showNotification('加载大盘预测失败')
  } finally {
    marketPredictLoading.value = false
  }
}

async function loadWeights() {
  try {
    const result = await apiFetch('/api/strategy/weights')
    if (result.success && result.data) {
      weights.value = result.data
    }
  } catch (e) { console.error('加载权重失败:', e) }
}

async function loadBacktestHistory() {
  try {
    const result = await apiFetch('/api/strategy/backtest-history?days=30')
    if (result.success && result.data) {
      backtestHistory.value = result.data
    }
  } catch (e) { console.error('加载回测历史失败:', e) }
}

async function loadPredictionRecords() {
  try {
    const result = await apiFetch('/api/strategy/predictions?include_today_and_pending=true')
    if (result.success && result.data) {
      predictions.value = result.data
      predictionPage.value = 1
    }
  } catch (e) { console.error('加载预测记录失败:', e) }
}

async function runBacktest() {
  backtesting.value = true
  try {
    const result = await apiFetch('/api/strategy/backtest', { method: 'POST' })
    if (result.success) {
      showNotification('回测完成')
      await Promise.all([loadWeights(), loadBacktestHistory(), loadPredictionRecords()])
    } else {
      showNotification(result.error || '回测失败')
    }
  } catch (e) {
    showNotification('回测请求失败')
  } finally {
    backtesting.value = false
  }
}

async function resetWeights() {
  if (!confirm('确定要重置所有策略权重为默认值吗？')) return
  try {
    const result = await apiFetch('/api/strategy/weights/reset', { method: 'POST' })
    if (result.success) {
      showNotification('策略权重已重置')
      await loadWeights()
    }
  } catch (e) { showNotification('重置失败') }
}

async function loadRps() {
  rpsLoading.value = true
  rpsError.value = ''
  try {
    const r = await apiFetch(`/api/rps?period_days=${rpsPeriod.value}&top_n=50`)
    if (r.success && r.data) {
      if (r.data.error) rpsError.value = r.data.error
      rpsList.value = r.data.stocks || []
      rpsPage.value = 1
    }
  } catch (e) {
    rpsError.value = '加载 RPS 失败'
  } finally {
    rpsLoading.value = false
  }
}

async function refreshRps() {
  rpsRefreshing.value = true
  try {
    const r = await apiFetch('/api/rps/refresh?period_days=' + rpsPeriod.value, { method: 'POST' })
    if (r.success) {
      showNotification('RPS 预计算已在后台启动，约 30 秒后自动刷新')
      setTimeout(loadRps, 35000)
    } else {
      showNotification('触发失败')
    }
  } catch (e) {
    showNotification('触发失败')
  } finally {
    rpsRefreshing.value = false
  }
}

function goToQuoteRps(symbol) {
  if (navigateToQuote) navigateToQuote(symbol)
}

// 聪明钱信号（P1b，2026-07-16）
const dragonTigerList = ref([])
const dragonTigerError = ref('')
const dragonTigerLatestDate = ref('')
const northbound = ref(null)
const smLoading = ref(false)

const DRAGON_TIGER_PAGE_SIZE = 10
const dragonTigerPage = ref(1)
const dragonSortKey = ref('net_amt')
const dragonSortDesc = ref(true)

function sortDragon(key) {
  if (dragonSortKey.value === key) {
    dragonSortDesc.value = !dragonSortDesc.value
  } else {
    dragonSortKey.value = key
    dragonSortDesc.value = true
  }
  dragonTigerPage.value = 1
}

const sortedDragonTigerList = computed(() => {
  const key = dragonSortKey.value
  return [...dragonTigerList.value].sort((a, b) => {
    if (key === 'name') {
      const va = a.name || a.symbol || ''
      const vb = b.name || b.symbol || ''
      return dragonSortDesc.value ? vb.localeCompare(va, 'zh-CN') : va.localeCompare(vb, 'zh-CN')
    }
    let va = a[key]
    let vb = b[key]
    if (va == null) va = -Infinity
    if (vb == null) vb = -Infinity
    return dragonSortDesc.value ? vb - va : va - vb
  })
})

const dragonTigerTotalPages = computed(() => Math.ceil(sortedDragonTigerList.value.length / DRAGON_TIGER_PAGE_SIZE))
const pagedDragonTigerList = computed(() => {
  const start = (dragonTigerPage.value - 1) * DRAGON_TIGER_PAGE_SIZE
  return sortedDragonTigerList.value.slice(start, start + DRAGON_TIGER_PAGE_SIZE)
})

function fmtYuan(v) {
  if (v === null || v === undefined) return '—'
  const yi = v / 1e8
  return (yi >= 0 ? '' : '-') + Math.abs(yi).toFixed(2) + '亿'
}
function fmtWan(v) {
  if (v === null || v === undefined) return '—'
  const wan = v  // 北向接口单位为万元
  if (Math.abs(wan) >= 1e4) return (wan >= 0 ? '' : '-') + (Math.abs(wan) / 1e4).toFixed(2) + '亿'
  return (wan >= 0 ? '' : '-') + Math.abs(wan).toFixed(0) + '万'
}

async function loadSmartMoney(force = false) {
  smLoading.value = true
  try {
    const [dt, nb] = await Promise.all([
      apiFetch(`/api/dragon-tiger?days=5&top_n=20&force=${force ? 1 : 0}`),
      apiFetch(`/api/northbound?force=${force ? 1 : 0}`)
    ])
    if (dt.success && dt.data) {
      if (dt.data.error) dragonTigerError.value = dt.data.error
      dragonTigerList.value = dt.data.stocks || []
      dragonTigerLatestDate.value = dt.data.latestTradeDate || ''
      dragonTigerPage.value = 1
    }
    if (nb.success && nb.data) northbound.value = nb.data
  } catch (e) {
    dragonTigerError.value = '加载聪明钱数据失败'
  } finally {
    smLoading.value = false
  }
}

onMounted(() => {
  loadWeights()
  loadBacktestHistory()
  loadPredictionRecords()
  loadMarketPredict()
  loadRps()
  loadSmartMoney()
})
</script>

<style scoped>
.smart-money-table .stock-table-header,
.smart-money-table .stock-table-row {
  grid-template-columns: 2fr 1.2fr 0.8fr 0.8fr;
}
.rps-table .stock-table-header .sortable {
  cursor: pointer;
  user-select: none;
  transition: color 0.2s;
}
.rps-table .stock-table-header .sortable::after {
  content: ' ⇅';
  font-size: 0.72em;
  color: var(--text-tertiary);
  opacity: 0.5;
}
.rps-table .stock-table-header .sortable.active {
  color: var(--accent-primary);
  font-weight: 700;
}
.rps-table .stock-table-header .sortable.active::after {
  content: ' ↓';
  opacity: 1;
}
.rps-table .stock-table-header .sortable.active.desc::after {
  content: ' ↑';
}
@media (max-width: 640px) {
  .smart-money-table .stock-table-header,
  .smart-money-table .stock-table-row {
    grid-template-columns: 1.6fr 1.2fr 0.7fr;
  }
  /* 窄屏隐藏"期间涨幅"列 */
  .smart-money-table .stock-table-header span:nth-child(4),
  .smart-money-table .stock-table-row span:nth-child(4) {
    display: none;
  }
}
</style>
