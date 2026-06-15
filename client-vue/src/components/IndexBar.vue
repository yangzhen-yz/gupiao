<template>
  <div class="index-bar">
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
import { ref, onMounted, onUnmounted } from 'vue'
import { isTradingTime, getNextRefreshDelay } from '../utils/helpers'

const indexes = ref([
  { code: 'sh000001', name: '上证', price: '', change: 0, changeText: '' },
  { code: 'sz399001', name: '深证', price: '', change: 0, changeText: '' },
  { code: 'sz399006', name: '创业板', price: '', change: 0, changeText: '' },
])

let timer = null

function parseTencentData(raw) {
  try {
    const lines = raw.split(';')
    for (let line of lines) {
      if (line.startsWith('v_') && line.includes('=')) {
        const parts = line.split('=')
        const content = parts[1].replace(/^"|"$/g, '')
        const fields = content.split('~')
        if (fields.length > 32) {
          const price = fields[3]
          const changePct = parseFloat(fields[32]) || 0
          return { price, changePct }
        }
      }
    }
  } catch (e) { /* silent */ }
  return null
}

async function loadIndexBar() {
  for (const idx of indexes.value) {
    try {
      const resp = await fetch(`/api/stock/${idx.code}`)
      const raw = await resp.text()
      const parsed = parseTencentData(raw)
      if (parsed) {
        idx.price = parsed.price
        idx.change = parsed.changePct
        idx.changeText = `${parsed.changePct >= 0 ? '+' : ''}${parsed.changePct.toFixed(2)}%`
      }
    } catch (e) { /* silent */ }
  }
}

function scheduleRefresh() {
  if (timer) clearTimeout(timer)
  const delay = isTradingTime() ? 5000 : getNextRefreshDelay()
  timer = setTimeout(async () => {
    await loadIndexBar()
    scheduleRefresh()
  }, delay)
}

onMounted(() => {
  loadIndexBar()
  scheduleRefresh()
})

onUnmounted(() => {
  if (timer) clearTimeout(timer)
})
</script>
