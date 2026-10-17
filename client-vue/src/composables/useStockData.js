import { ref } from 'vue'
import { apiFetch } from '../utils/helpers'

// 全局股票映射数据
const stockNameMap = ref({})
const stockCodeMap = ref({})
const quickStocks = ref([])
const scanStockPool = ref([])

export function useStockData() {
  async function loadStockMap() {
    try {
      const result = await apiFetch('/api/stock-map')
      if (result.success && result.data) {
        const map = {}
        const codeMap = {}
        for (const [code, name] of Object.entries(result.data)) {
          map[code] = name
          codeMap[name] = code
        }
        stockNameMap.value = map
        stockCodeMap.value = codeMap
      }
    } catch (e) {
      console.error('加载股票映射失败:', e)
    }
  }

  async function loadQuickStocks() {
    try {
      const result = await apiFetch('/api/hot-stocks')
      if (result.success && result.data && result.data.stocks) {
        quickStocks.value = result.data.stocks
      }
    } catch (e) {
      console.error('加载热门股票失败:', e)
    }
  }

  async function loadScanPool() {
    // 性能优化（2026-07-02）：改调轻量端点，仅返回代码列表，不触发 N 次 K 线计算
    try {
      const result = await apiFetch('/api/custom-scan-pool/symbols')
      if (result.success && result.data) {
        scanStockPool.value = result.data.symbols || []
      }
    } catch (e) {
      console.error('加载扫描池失败:', e)
    }
  }

  function getStockName(symbol) {
    return stockNameMap.value[symbol] || symbol
  }

  function getStockCode(name) {
    return stockCodeMap.value[name] || ''
  }

  return {
    stockNameMap,
    stockCodeMap,
    quickStocks,
    scanStockPool,
    loadStockMap,
    loadQuickStocks,
    loadScanPool,
    getStockName,
    getStockCode,
  }
}
