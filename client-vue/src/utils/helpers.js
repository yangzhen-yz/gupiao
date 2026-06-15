import { ref, onUnmounted } from 'vue'

// 判断是否为交易日
const HOLIDAYS_2025 = [
  '2025-01-01','2025-01-28','2025-01-29','2025-01-30','2025-01-31','2025-02-03','2025-02-04',
  '2025-04-04','2025-05-01','2025-05-02','2025-05-05','2025-06-02',
  '2025-10-01','2025-10-02','2025-10-03','2025-10-06','2025-10-07','2025-10-08',
]

export function isHoliday(date = new Date()) {
  const y = date.getFullYear(), m = String(date.getMonth()+1).padStart(2,'0'), d = String(date.getDate()).padStart(2,'0')
  return HOLIDAYS_2025.includes(`${y}-${m}-${d}`)
}

export function isTradingTime() {
  const now = new Date()
  const day = now.getDay()
  if (day === 0 || day === 6) return false
  if (isHoliday(now)) return false
  const h = now.getHours(), m = now.getMinutes()
  if ((h === 9 && m >= 30) || (h === 10) || (h === 11 && m <= 30)) return true
  if ((h >= 13 && h < 15) || (h === 15 && m === 0)) return true
  return false
}

export function getNextRefreshDelay() {
  if (isTradingTime()) return 1000
  const now = new Date()
  const day = now.getDay(), h = now.getHours(), m = now.getMinutes()
  let next = new Date(now)
  if (day === 0 || day === 6) {
    next.setDate(now.getDate() + (day === 0 ? 1 : 2))
    next.setHours(9, 30, 0, 0)
  } else if (h >= 15) {
    next.setDate(now.getDate() + 1)
    next.setHours(9, 30, 0, 0)
  } else if (h >= 11 && h < 13) {
    next.setHours(13, 0, 0, 0)
  } else {
    next.setHours(9, 30, 0, 0)
  }
  return Math.max(next.getTime() - now.getTime(), 60000)
}

export function getTradingStatusText() {
  if (isTradingTime()) return '📈 交易中 · 自动刷新'
  const now = new Date()
  const day = now.getDay()
  if (day === 0 || day === 6) return '🕐 周末休市'
  if (isHoliday(now)) return '🕐 节假日休市'
  const h = now.getHours()
  if (h < 9 || (h === 9 && now.getMinutes() < 30)) return '🕐 盘前等待'
  if (h >= 15) return '🕐 已收盘'
  if (h >= 11 && h < 13) return '🕐 午间休市'
  return '🕐 盘中休市'
}

export function formatNowTime() {
  const now = new Date()
  return now.toLocaleTimeString('zh-CN', { hour12: false })
}

export function formatNumber(numStr) {
  if (!numStr) return '--'
  const num = parseFloat(numStr)
  if (isNaN(num)) return '--'
  if (Math.abs(num) >= 1e8) return (num / 1e8).toFixed(2) + '亿'
  if (Math.abs(num) >= 1e4) return (num / 1e4).toFixed(2) + '万'
  return num.toFixed(2)
}

// 通知
const notifications = ref([])
let notifId = 0

export function useNotification() {
  function showNotification(message, duration = 3000) {
    const id = ++notifId
    notifications.value.push({ id, message })
    setTimeout(() => {
      notifications.value = notifications.value.filter(n => n.id !== id)
    }, duration)
  }
  return { notifications, showNotification }
}

// API 请求封装
export async function apiFetch(url, options = {}) {
  try {
    const resp = await fetch(url, options)
    return await resp.json()
  } catch (e) {
    console.error(`API请求失败: ${url}`, e)
    throw e
  }
}
