<template>
  <div class="diag-mask" @click.self="close">
    <div class="diag-panel">
      <div class="diag-head">
        <div class="diag-title">
          <b>{{ data ? data.name : symbol }}</b>
          <span class="diag-code">{{ symbol }}</span>
          <span v-if="data" class="diag-asof">{{ data.asOf }}</span>
        </div>
        <button class="diag-x" @click="close">×</button>
      </div>

      <div v-if="loading" class="diag-loading">正在聚合五层本地数据…</div>
      <div v-else-if="error" class="diag-error">{{ error }}</div>

      <div v-else-if="data" class="diag-body">
        <!-- 摘要先行：结论 + 逐层判定（一屏内看完态度） -->
        <div class="diag-stance" :class="stanceClass">{{ data.stance }}</div>
        <div class="diag-signals">
          <span v-for="(s, i) in data.signals" :key="i" class="sig" :class="s.verdict">
            {{ s.verdict === 'support' ? '✓' : s.verdict === 'oppose' ? '✗' : '?' }} {{ s.layer }}
          </span>
        </div>

        <!-- 五层卡片：默认收起只留一行要点，点开展开明细（移动端 1.5 屏约束） -->
        <div v-for="card in cards" :key="card.key" class="diag-card">
          <div class="card-head" @click="toggle(card.key)">
            <span class="card-name">{{ card.title }}</span>
            <span class="card-brief" :class="card.tone">{{ card.brief }}</span>
            <span class="card-arrow">{{ open[card.key] ? '▾' : '▸' }}</span>
          </div>
          <div v-if="open[card.key]" class="card-detail">
            <div v-for="(line, j) in card.lines" :key="j" class="line" :class="line.cls">{{ line.text }}</div>
          </div>
        </div>

        <div class="diag-note">{{ data.disclaimer }}</div>
      </div>
    </div>
  </div>
</template>

<script setup>
  import { ref, computed, watch } from 'vue'
  import { apiFetch } from '../utils/helpers'

  const props = defineProps({
    visible: { type: Boolean, default: false },
    symbol: { type: String, default: '' },
  })
  const emit = defineEmits(['close'])

  const loading = ref(false)
  const error = ref('')
  const data = ref(null)
  const open = ref({})

  function close() {
    emit('close')
  }
  function toggle(key) {
    open.value = { ...open.value, [key]: !open.value[key] }
  }

  const stanceClass = computed(() => {
    const s = data.value && data.value.stance
    return s === '谨慎看多' ? 'bull' : s === '回避' ? 'bear' : 'neutral'
  })

  // A股红涨绿跌：--accent-danger=红(涨/看多)，--accent-success=绿(跌/风险)
  function pctCls(v) {
    if (v == null) return ''
    return v > 0 ? 'up' : v < 0 ? 'down' : ''
  }

  const cards = computed(() => {
    const d = data.value
    if (!d) return []
    const L = d.layers
    const out = []

    const p = L.position
    if (p.available) {
      out.push({
        key: 'pos',
        title: '① 位置结构',
        tone: p.belowAllMa ? 'bad' : 'ok',
        brief: `收 ${p.close}｜${p.belowAllMa ? '三线下方' : 'MA10 ' + (p.close > p.ma10 ? '上' : '下')}`,
        lines: [
          { text: `MA10 ${p.ma10} / MA20 ${p.ma20} / MA60 ${p.ma60 || '—'}（数据日 ${p.asOf}）` },
          { text: `60日高 ${p.high60} → 回撤 ${p.drawdownPct}%；箱体底（近10日低）${p.low10}` },
          {
            text: `5日波动率 ${p.vol5Pct}%（≥3% 即高波动）；近5日 ${p.last5Pct.map(x => (x > 0 ? '+' : '') + x + '%').join(' / ')}`,
          },
          {
            text: `量能：当日为60日均量的 ${p.volShrink != null ? Math.round(p.volShrink * 100) + '%' : '—'}（近3日 ${p.vol3d.join('/')} 万手）`,
            cls: (p.volShrink ?? 1) < 0.7 ? 'warn' : '',
          },
        ],
      })
    } else {
      out.push({
        key: 'pos',
        title: '① 位置结构',
        tone: 'bad',
        brief: p.reason || '不可用',
        lines: [{ text: p.reason || '' }],
      })
    }

    const f = L.fundflow
    if (f.available) {
      out.push({
        key: 'flow',
        title: '② 资金',
        tone: 'ok',
        brief: `主力净 ${f.mainNetYi} 亿（${f.source}）`,
        lines: [
          { text: `当日主力净额 ${f.mainNetYi} 亿`, cls: pctCls(f.mainNetYi) },
          ...(f.mainNet5dYi != null
            ? [{ text: `5日累计 ${f.mainNet5dYi} 亿 / 20日累计 ${f.mainNet20dYi} 亿`, cls: pctCls(f.mainNet5dYi) }]
            : []),
          { text: f.note || '', cls: 'muted' },
        ],
      })
    } else {
      out.push({
        key: 'flow',
        title: '② 资金',
        tone: 'blind',
        brief: '本层失明（无数据）',
        lines: [{ text: f.reason || '无数据', cls: 'muted' }],
      })
    }

    const e = L.environment
    out.push({
      key: 'env',
      title: '③ 市场环境',
      tone: (e.cycleScore || 0) >= 55 ? 'ok' : 'bad',
      brief: e.available ? `${e.cycle} ${e.cycleScore} 分（${e.date}）` : '无收评',
      lines: e.available
        ? [
            { text: `情绪分 ${e.sentimentScore}｜${e.detail || ''}` },
            { text: `系统建议：${e.advice || '—'}`, cls: 'muted' },
          ]
        : [{ text: e.reason || '' }],
    })

    const h = L.history
    const hl = []
    if (h.snapshots && h.snapshots.length) {
      const s0 = h.snapshots[0]
      hl.push({
        text: `最近批次（${s0.snap_date} ${s0.batch}）：${s0.score} 分 / 排名 ${s0.rank}${s0.blocked_by ? '｜' + s0.blocked_by : ''}`,
      })
    }
    for (const o of h.outcomes || []) {
      hl.push({
        text: `${o.recDate} 入选 → T+1 ${fmt(o.t1)} / T+3 ${fmt(o.t3)}`,
        cls: (o.t3 ?? 0) < 0 ? 'down' : (o.t3 ?? 0) > 0 ? 'up' : '',
      })
    }
    out.push({
      key: 'hist',
      title: '④ 系统历史账',
      tone: h.allBlocked ? 'bad' : 'ok',
      brief: h.allBlocked ? '历次全被门槛挡下' : `${(h.outcomes || []).length} 次入选记录`,
      lines: hl.length ? hl : [{ text: '无快照、无入选记录', cls: 'muted' }],
    })

    const d5 = L.discipline
    out.push({
      key: 'disc',
      title: '⑤ 纪律价位',
      tone: 'ok',
      brief: d5.available ? d5.items.map(i => i.name).join(' / ') : '依赖①',
      lines: d5.available
        ? d5.items.map(i => ({ text: `${i.name}：${i.price}｜${i.text}` }))
        : [{ text: d5.reason || '' }],
    })
    return out
  })

  function fmt(v) {
    if (v == null) return '未到期'
    return (v > 0 ? '+' : '') + v + '%'
  }

  async function fetchDiag(symbol) {
    loading.value = true
    error.value = ''
    data.value = null
    open.value = {}
    try {
      const r = await apiFetch(`/api/stock-diagnose/${symbol}`)
      if (r && r.success) data.value = r.data
      else error.value = (r && (r.detail || r.error)) || '诊断失败'
    } catch (e) {
      error.value = '诊断请求失败：' + ((e && (e.detail || e.message)) || e)
    } finally {
      loading.value = false
    }
  }

  watch(
    () => props.visible,
    v => {
      if (v && props.symbol) fetchDiag(props.symbol)
    }
  )
</script>

<style scoped>
  /* 仅用 main.css 主题 token；红=涨/看多，绿=跌/风险（A股约定） */
  .diag-mask {
    position: fixed;
    inset: 0;
    z-index: 1200;
    background: rgba(0, 0, 0, 0.45);
    display: flex;
    align-items: flex-start;
    justify-content: center;
    padding: var(--spacing-md) var(--spacing-sm);
    overflow-y: auto;
  }
  .diag-panel {
    width: 100%;
    max-width: 640px;
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    padding: var(--spacing-md);
    color: var(--text-primary);
  }
  .diag-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: var(--spacing-sm);
  }
  .diag-title {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    flex-wrap: wrap;
  }
  .diag-title b {
    font-size: 16px;
  }
  .diag-code,
  .diag-asof {
    font-size: 11px;
    color: var(--text-tertiary);
  }
  .diag-x {
    background: none;
    border: none;
    font-size: 20px;
    color: var(--text-tertiary);
    cursor: pointer;
    line-height: 1;
  }
  .diag-loading,
  .diag-error {
    font-size: 12px;
    color: var(--text-tertiary);
    padding: var(--spacing-md) 0;
  }
  .diag-error {
    color: var(--accent-danger);
  }
  .diag-stance {
    font-size: 20px;
    font-weight: 700;
    margin-bottom: 6px;
  }
  .diag-stance.bull {
    color: var(--accent-danger);
  }
  .diag-stance.bear {
    color: var(--accent-success);
  }
  .diag-stance.neutral {
    color: var(--text-secondary);
  }
  .diag-signals {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin-bottom: var(--spacing-sm);
  }
  .sig {
    font-size: 11px;
    padding: 2px 8px;
    border-radius: var(--radius-xs);
    border: 1px solid var(--border-color);
  }
  .sig.support {
    color: var(--accent-danger);
    border-color: var(--accent-danger-border);
    background: var(--accent-danger-bg);
  }
  .sig.oppose {
    color: var(--accent-success);
    border-color: var(--accent-success-border);
    background: var(--accent-success-bg);
  }
  .sig.blind {
    color: var(--text-tertiary);
  }
  .diag-card {
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    margin-bottom: 8px;
  }
  .card-head {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 10px;
    cursor: pointer;
  }
  .card-name {
    font-size: 12px;
    font-weight: 600;
    white-space: nowrap;
  }
  .card-brief {
    font-size: 11px;
    color: var(--text-secondary);
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .card-brief.bad {
    color: var(--accent-success);
  }
  .card-brief.blind {
    color: var(--text-tertiary);
  }
  .card-arrow {
    font-size: 10px;
    color: var(--text-tertiary);
  }
  .card-detail {
    padding: 2px 10px 8px;
    border-top: 1px dashed var(--border-color);
  }
  .line {
    font-size: 11px;
    color: var(--text-secondary);
    padding: 2px 0;
  }
  .line.up {
    color: var(--accent-danger);
  }
  .line.down {
    color: var(--accent-success);
  }
  .line.warn {
    color: var(--accent-warning);
  }
  .line.muted {
    color: var(--text-tertiary);
  }
  .diag-note {
    font-size: 10px;
    color: var(--text-tertiary);
    margin-top: 6px;
  }
</style>
