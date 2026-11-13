<template>
  <div class="freshness" :class="{ stale: isStale }">
    <span class="fresh-text">
      <em>{{ label }}</em>
      <b>{{ timeText }}</b>
    </span>
    <span v-if="isStale" class="fresh-warn">⚠️ {{ staleHint }}</span>
    <button v-if="!loading" class="fresh-btn" @click="$emit('refresh')">↻ 手动刷新</button>
    <span v-else class="fresh-loading">刷新中…</span>
  </div>
</template>

<script setup>
  import { computed } from 'vue'

  /**
   * 数据新鲜度条（2026-10-13 行动台改造）
   *
   * 从 TabReview 的 isStale 抽出来复用。此前「智能推荐 / 模拟交易 / 自动优化」三个页面
   * 既没有轮询、也没有"更新于"时间戳，用户无法分辨屏上的是今天的数还是上周的数 ——
   * 对收盘后才看的上班族，这是最致命的一种误导。
   */
  const props = defineProps({
    // 服务端返回的时间：ISO 串 / 'YYYY-MM-DD' / 时间戳 / Date
    updated: { type: [String, Number, Object], default: '' },
    label: { type: String, default: '更新于' },
    staleHint: { type: String, default: '不是今日数据' },
    // 显式覆盖判定（例如"今日本就还没生成"应当显示为待生成而不是过期）
    stale: { type: Boolean, default: false },
    loading: { type: Boolean, default: false },
    // 是否把"跨天"算作过期；关掉只用于纯提示的场景
    dayCompare: { type: Boolean, default: true },
    // 纯日期量（如"当前推荐日期"）按日历日展示，不要渲染成 00:00 这种无意义时间
    dateOnly: { type: Boolean, default: false },
  })
  defineEmits(['refresh'])

  const parsed = computed(() => {
    const v = props.updated
    if (!v) return null
    if (v instanceof Date) return isNaN(v) ? null : v
    if (typeof v === 'number') {
      const d = new Date(v)
      return isNaN(d) ? null : d
    }
    // 'YYYY-MM-DD' 按本地时间解析，避免 new Date('2026-10-13') 被当 UTC 而跨天
    const d = new Date(String(v).replace(/^(\d{4}-\d{2}-\d{2})$/, '$1T00:00:00'))
    return isNaN(d) ? null : d
  })

  const timeText = computed(() => {
    if (!parsed.value) return '暂无数据'
    const hh = String(parsed.value.getHours()).padStart(2, '0')
    const mm = String(parsed.value.getMinutes()).padStart(2, '0')
    const mo = String(parsed.value.getMonth() + 1).padStart(2, '0')
    const da = String(parsed.value.getDate()).padStart(2, '0')
    const yyyy = parsed.value.getFullYear()
    // dateOnly：把"推荐日期"这种纯日期量当日历日展示，而不是今天的时间 00:00
    if (props.dateOnly) return `${yyyy}-${mo}-${da}`
    if (isToday.value) return `${hh}:${mm}`
    return `${mo}-${da} ${hh}:${mm}`
  })

  const isToday = computed(() => {
    const d = parsed.value
    if (!d) return false
    const n = new Date()
    return d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate()
  })

  const isStale = computed(() => {
    if (props.stale) return true
    if (!parsed.value) return false
    return props.dayCompare && !isToday.value
  })
</script>

<style scoped>
  .freshness {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.5rem 0.75rem;
    padding: 0.4375rem 0.75rem;
    margin-bottom: 0.875rem;
    background: var(--bg-secondary);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    font-size: 0.8125rem;
    color: var(--text-secondary);
  }
  .fresh-text {
    display: inline-flex;
    align-items: baseline;
    gap: 0.375rem;
  }
  .fresh-text em {
    font-style: normal;
    color: var(--text-tertiary);
  }
  .fresh-text b {
    color: var(--text-primary);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
  .fresh-warn {
    color: var(--accent-warning);
    font-weight: 600;
  }
  .fresh-btn {
    margin-left: auto;
    padding: 3px 10px;
    font-size: 0.75rem;
    font-family: inherit;
    color: var(--text-secondary);
    background: var(--bg-tertiary);
    border: 1px solid var(--border-medium);
    border-radius: 5px;
    cursor: pointer;
    white-space: nowrap;
  }
  .fresh-btn:hover {
    border-color: var(--accent-primary);
    color: var(--text-primary);
  }
  .fresh-loading {
    margin-left: auto;
    font-size: 0.75rem;
    color: var(--text-tertiary);
  }
  .freshness.stale {
    background: var(--accent-warning-bg);
    border-color: var(--accent-warning-border);
  }
</style>
