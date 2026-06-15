<template>
  <div class="modal" v-if="visible" style="display:flex;">
    <div class="modal-content">
      <div class="modal-header">
        <span class="modal-title">{{ title }}</span>
        <div class="kline-type-switch">
          <button :class="['kline-type-btn', { active: klineType === 'day' }]" @click="switchType('day')">日K</button>
          <button :class="['kline-type-btn', { active: klineType === 'minute' }]" @click="switchType('minute')">分时</button>
        </div>
        <button class="close-btn" @click="close">&times;</button>
      </div>
      <div ref="chartDom" class="kline-chart-container"></div>
    </div>
  </div>
</template>

<script setup>
import { ref, watch, onUnmounted, nextTick, inject } from 'vue'
import * as echarts from 'echarts'
import { isTradingTime, getNextRefreshDelay } from '../utils/helpers'

const props = defineProps({
  visible: Boolean,
  symbol: String,
})
const emit = defineEmits(['close'])
const showNotification = inject('showNotification')

const chartDom = ref(null)
const title = ref('日K线')
const klineType = ref('day')
let chartInstance = null
let refreshTimer = null

function initChart() {
  if (!chartDom.value) return
  if (chartInstance) chartInstance.dispose()
  chartInstance = echarts.init(chartDom.value)
}

async function fetchData(symbol, type) {
  if (!symbol) return
  try {
    const url = type === 'minute' ? `/api/kline/${symbol}/minute` : `/api/kline/${symbol}`
    const resp = await fetch(url)
    const json = await resp.json()
    if (json.success && json.data) {
      if (type === 'minute') {
        renderMinuteChart(json.data)
      } else {
        renderKlineChart(json.data)
      }
    } else {
      showNotification(json.error || '获取K线数据失败')
    }
  } catch (e) {
    showNotification('获取K线数据失败')
  }
}

function renderKlineChart(data) {
  if (!chartInstance) return
  title.value = `日K线 - ${data.name} (${data.symbol})`
  const upColor = '#f04848', downColor = '#2dc87e'
  const startPct = data.dates.length > 100 ? Math.max(0, (data.dates.length - 100) * 100 / data.dates.length) : 0

  chartInstance.setOption({
    backgroundColor: '#13161f', animation: false,
    legend: { top: 10, data: ['日K', 'MA5', 'MA10', 'MA20'], left: 'center', textStyle: { color: '#8b90a5' } },
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' }, backgroundColor: 'rgba(26,30,43,0.95)', borderColor: '#252a3a', borderWidth: 1, textStyle: { color: '#e8eaf0' } },
    grid: [{ left: '10%', right: '8%', top: 70, height: '50%' }, { left: '10%', right: '8%', top: '63%', height: '16%' }],
    xAxis: [
      { type: 'category', data: data.dates, axisLine: { onZero: false, lineStyle: { color: '#252a3a' } }, boundaryGap: true, axisLabel: { formatter: v => v.slice(5), color: '#525770' } },
      { type: 'category', gridIndex: 1, data: data.dates, axisLine: { onZero: false, lineStyle: { color: '#252a3a' } }, boundaryGap: true, axisLabel: { show: false } }
    ],
    yAxis: [
      { scale: true, splitArea: { show: false }, splitLine: { lineStyle: { color: '#1e2233' } }, axisLabel: { color: '#525770' } },
      { scale: true, gridIndex: 1, splitNumber: 2, axisLabel: { show: true, color: '#525770' }, splitLine: { lineStyle: { color: '#1e2233' } } }
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: startPct, end: 100 },
      { show: true, xAxisIndex: [0, 1], type: 'slider', bottom: 10, start: startPct, end: 100, borderColor: '#252a3a', fillerColor: 'rgba(74,144,255,0.15)', handleStyle: { color: '#4a90ff' }, textStyle: { color: '#525770' }, dataBackground: { lineStyle: { color: '#252a3a' }, areaStyle: { color: '#1e2233' } } }
    ],
    series: [
      { name: '日K', type: 'candlestick', data: data.values, itemStyle: { color: upColor, color0: downColor, borderColor: upColor, borderColor0: downColor } },
      { name: 'MA5', type: 'line', data: data.ma5, smooth: true, symbol: 'none', lineStyle: { width: 1 }, color: '#f0a030' },
      { name: 'MA10', type: 'line', data: data.ma10, smooth: true, symbol: 'none', lineStyle: { width: 1 }, color: '#8b5cf6' },
      { name: 'MA20', type: 'line', data: data.ma20, smooth: true, symbol: 'none', lineStyle: { width: 1 }, color: '#06b6d4' },
      { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: data.vols, itemStyle: { color: p => { const v = data.values[p.dataIndex]; return v[1] > v[0] ? upColor : downColor } } }
    ]
  }, true)
}

function renderMinuteChart(data) {
  if (!chartInstance) return
  title.value = `分时图 - ${data.name} (${data.symbol})`
  const upColor = '#f04848', downColor = '#2dc87e'

  chartInstance.setOption({
    backgroundColor: '#13161f', animation: false,
    legend: { top: 10, data: ['价格', '昨收'], left: 'center', textStyle: { color: '#8b90a5' } },
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' }, backgroundColor: 'rgba(26,30,43,0.95)', borderColor: '#252a3a', borderWidth: 1, textStyle: { color: '#e8eaf0' } },
    grid: [{ left: '10%', right: '8%', top: 70, height: '55%' }, { left: '10%', right: '8%', top: '68%', height: '14%' }],
    xAxis: [
      { type: 'category', data: data.times, axisLine: { onZero: false, lineStyle: { color: '#252a3a' } }, boundaryGap: false, axisLabel: { rotate: 45, fontSize: 10, color: '#525770' } },
      { type: 'category', gridIndex: 1, data: data.times, axisLine: { onZero: false, lineStyle: { color: '#252a3a' } }, axisLabel: { show: false }, boundaryGap: false }
    ],
    yAxis: [
      { scale: true, splitArea: { show: false }, splitLine: { lineStyle: { color: '#1e2233' } }, axisLabel: { color: '#525770' }, min: v => (v.min - (v.max - v.min) * 0.05).toFixed(2), max: v => (v.max + (v.max - v.min) * 0.05).toFixed(2) },
      { scale: true, gridIndex: 1, splitNumber: 3, axisLabel: { show: true, color: '#525770' }, splitLine: { lineStyle: { color: '#1e2233' } } }
    ],
    series: [
      { name: '价格', type: 'line', data: data.prices, smooth: false, symbol: 'none', lineStyle: { width: 1.5, color: '#4a90ff' }, areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: 'rgba(74,144,255,0.25)' }, { offset: 1, color: 'rgba(74,144,255,0.02)' }]) } },
      { name: '昨收', type: 'line', data: new Array(data.times.length).fill(data.prevClose), symbol: 'none', lineStyle: { width: 1, color: '#525770', type: 'dashed' } },
      { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: data.vols, itemStyle: { color: p => { const i = p.dataIndex; return i === 0 ? upColor : data.prices[i] >= data.prices[i - 1] ? upColor : downColor } } }
    ]
  }, true)
}

function switchType(type) {
  klineType.value = type
  if (props.symbol) fetchData(props.symbol, type)
  scheduleRefresh()
}

function scheduleRefresh() {
  stopRefresh()
  const tick = async () => {
    if (props.symbol) await fetchData(props.symbol, klineType.value)
    refreshTimer = setTimeout(tick, isTradingTime() ? 5000 : getNextRefreshDelay())
  }
  if (isTradingTime()) tick()
}

function stopRefresh() {
  if (refreshTimer) { clearTimeout(refreshTimer); refreshTimer = null }
}

function close() {
  stopRefresh()
  emit('close')
}

function handleResize() { chartInstance?.resize() }

watch(() => props.visible, async (val) => {
  if (val) {
    await nextTick()
    initChart()
    klineType.value = 'day'
    if (props.symbol) fetchData(props.symbol, 'day')
    scheduleRefresh()
    window.addEventListener('resize', handleResize)
  } else {
    stopRefresh()
    window.removeEventListener('resize', handleResize)
  }
})

onUnmounted(() => {
  stopRefresh()
  if (chartInstance) { chartInstance.dispose(); chartInstance = null }
  window.removeEventListener('resize', handleResize)
})
</script>
