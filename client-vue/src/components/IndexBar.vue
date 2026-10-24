<template>
  <div class="index-bar">
    <div class="index-brand">
      <span class="index-brand-title">A股 · 实时行情</span>
      <span class="index-brand-status" :class="statusClass">{{ tradingStatus }}</span>
    </div>
    <div class="index-item" v-for="idx in indexes" :key="idx.code">
      <span class="index-name">{{ idx.name }}</span>
      <span class="index-price" :class="idx.change > 0 ? 'up' : idx.change < 0 ? 'down' : 'flat'">
        {{ idx.price || '--' }}
      </span>
      <span class="index-change" :class="idx.change > 0 ? 'up' : idx.change < 0 ? 'down' : 'flat'">
        {{ idx.changeText || '--' }}
      </span>
    </div>
  </div>
</template>

<script setup>
  import { ref, computed } from 'vue'
  import { apiFetch } from '../utils/helpers'
  import { isTradingTime, getNextRefreshDelay, getTradingStatusText } from '../utils/helpers'
  import { usePolling } from '../composables/usePolling'

  const indexes = ref([
    { code: 'sh000001', name: '上证', price: '', change: 0, changeText: '' },
    { code: 'sz399001', name: '深证', price: '', change: 0, changeText: '' },
    { code: 'sz399006', name: '创业板', price: '', change: 0, changeText: '' },
  ])

  // 市场状态徽章（2026-08-31 美化：顶部栏增加交易状态/品牌，替代原来光秃秃的三只指数）
  const tradingStatus = ref(getTradingStatusText().replace(/^[^\u4e00-\u9fa5]+\s*/, ''))
  const statusClass = computed(() => {
    if (isTradingTime()) return 'trading'
    const t = tradingStatus.value
    if (t.includes('收盘')) return 'closed'
    if (t.includes('休市')) return 'rest'
    return 'idle'
  })

  async function loadIndexBar() {
    // 2026-10-17 性能：原来每 5 秒打 3 个 `/api/stock/{code}`（3 次 RTT、且裸 fetch 无超时），
    // 后端 `/api/market-index` 一次就把三只指数（上证/深成/创业板）连同涨跌都算好了，实测 920 字节、
    // 命中缓存 5ms。换成它之后顶部栏从 3 个请求变 1 个，并且带超时，慢也不会挂住页面。
    try {
      const res = await apiFetch('/api/market-index')
      const list = (res && res.data && res.data.indexes) || []
      indexes.value.forEach(idx => {
        const hit = list.find(x => (x.symbol || '').toLowerCase() === idx.code)
        if (!hit || hit.price == null) return
        idx.price = hit.price
        idx.change = hit.changePercent ?? 0
        idx.changeText = `${idx.change >= 0 ? '+' : ''}${Number(idx.change).toFixed(2)}%`
      })
    } catch (e) {
      /* silent：指数条拿不到不影响主功能 */
    }
  }

  // 2026-10-24：迁 usePolling——页面隐藏时停发请求，回前台超 5s 自动补一次。
  // 间隔保持原口径：交易时段 5s，休市顺延到下一时段；usePolling 自带挂载即首拉与卸载清理。
  usePolling(
    async () => {
      tradingStatus.value = getTradingStatusText().replace(/^[^\u4e00-\u9fa5]+\s*/, '')
      await loadIndexBar()
    },
    { interval: () => (isTradingTime() ? 5000 : getNextRefreshDelay()) }
  )
</script>
