import { ref } from 'vue'

// 判断是否为交易日（2026年A股休市日历，来源：沪深北交易所公告）
const HOLIDAYS_2026 = [
  '2026-01-01',
  '2026-01-02', // 元旦
  '2026-02-16',
  '2026-02-17',
  '2026-02-18',
  '2026-02-19',
  '2026-02-20',
  '2026-02-23', // 春节
  '2026-04-06', // 清明节
  '2026-05-01',
  '2026-05-04',
  '2026-05-05', // 劳动节
  '2026-06-19', // 端午节
  '2026-09-25', // 中秋节
  '2026-10-01',
  '2026-10-02',
  '2026-10-05',
  '2026-10-06',
  '2026-10-07', // 国庆节
]

export function isHoliday(date = new Date()) {
  const y = date.getFullYear(),
    m = String(date.getMonth() + 1).padStart(2, '0'),
    d = String(date.getDate()).padStart(2, '0')
  return HOLIDAYS_2026.includes(`${y}-${m}-${d}`)
}

// 可操作窗口：集合竞价 9:15 起到上午收市、下午开市到 15:00。
// 与 isTradingTime 的区别是含竞价段 —— 竞价时挂单是有意义的，盘前 7 点不是。
export function isOrderableNow(date = new Date()) {
  const day = date.getDay()
  if (day === 0 || day === 6) return false
  if (isHoliday(date)) return false
  const mins = date.getHours() * 60 + date.getMinutes()
  return (mins >= 9 * 60 + 15 && mins <= 11 * 60 + 30) || (mins >= 13 * 60 && mins <= 15 * 60)
}

export function isTradingTime() {
  const now = new Date()
  const day = now.getDay()
  if (day === 0 || day === 6) return false
  if (isHoliday(now)) return false
  const h = now.getHours(),
    m = now.getMinutes()
  if ((h === 9 && m >= 30) || h === 10 || (h === 11 && m <= 30)) return true
  if ((h >= 13 && h < 15) || (h === 15 && m === 0)) return true
  return false
}

export function getNextRefreshDelay() {
  if (isTradingTime()) return 1000
  const now = new Date()
  const day = now.getDay(),
    h = now.getHours()
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

const TYPE_ICONS = {
  success: '✓',
  error: '✕',
  warning: '!',
  info: 'i',
}

export function useNotification() {
  function showNotification(message, type = null, duration = 3000) {
    // 兼容旧调用：showNotification(msg, duration)
    if (type !== null && typeof type === 'number') {
      duration = type
      type = null
    }
    // 未指定类型时，从消息内容自动推断
    if (!type) {
      if (/失败|错误/.test(message)) type = 'error'
      else if (/成功|完成|已添加|已移出|已保存/.test(message)) type = 'success'
      else if (/请输入|请先/.test(message)) type = 'warning'
      else type = 'info'
    }
    const id = ++notifId
    notifications.value.unshift({ id, message, type })
    setTimeout(() => {
      notifications.value = notifications.value.filter(n => n.id !== id)
    }, duration)
  }
  return { notifications, showNotification, TYPE_ICONS }
}

// ========== 登录态管理（2026-10-12 多用户） ==========
// token 存 localStorage：关掉浏览器再打开不用重新登录（服务端会话有效期 30 天）
const TOKEN_KEY = 'gupiao_auth_token'
const USER_KEY = 'gupiao_auth_user'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || ''
}

export function getAuthUser() {
  try {
    return JSON.parse(localStorage.getItem(USER_KEY) || 'null')
  } catch (e) {
    return null
  }
}

export function saveAuth(token, user) {
  localStorage.setItem(TOKEN_KEY, token || '')
  localStorage.setItem(USER_KEY, JSON.stringify(user || null))
}

export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

// 本机登录过的账号（2026-10-13）：登录页的「已有账号」快捷切换原先读 GET /api/auth/users，
// 那个接口不需要登录就会把所有用户名暴露出去。系统经隧道短暂对外时已把它改成需登录，
// 快捷切换因此改成本地记住的账号 —— 自己的设备上好使，陌生人打开链接看不到。
const RECENT_USERS_KEY = 'gupiao_recent_users'

export function getRecentUsers() {
  try {
    const list = JSON.parse(localStorage.getItem(RECENT_USERS_KEY) || '[]')
    return Array.isArray(list) ? list : []
  } catch (e) {
    return []
  }
}

export function saveRecentUser(user) {
  if (!user || !user.username) return
  const list = getRecentUsers().filter(u => u.username !== user.username)
  list.unshift({ username: user.username, displayName: user.displayName || user.username })
  localStorage.setItem(RECENT_USERS_KEY, JSON.stringify(list.slice(0, 5)))
}

// 会话失效（401）时由 App 注册回调，负责弹回登录页
let _unauthorizedHandler = null
export function setUnauthorizedHandler(fn) {
  _unauthorizedHandler = fn
}

// API 请求默认超时（毫秒）。2026-10-17：之前所有请求都没有超时，
// 后端 `/api/rps` 那种"一个接口 50 秒"的情况会直接把整页 loading 无限挂住，
// 用户看到的就是"每个页面都很慢、而且卡住不动"。超时后快速失败，页面其余部分照常可用。
export const API_TIMEOUT_MS = 10000

// API 请求封装：自动附带登录 token；私有接口返回 401 时清理登录态并通知上层
export async function apiFetch(url, options = {}) {
  const { timeout = API_TIMEOUT_MS, expectText = false, ...rest } = options
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeout)
  try {
    const opts = { ...rest, signal: ctrl.signal }
    const token = getToken()
    if (token) {
      opts.headers = { ...(options.headers || {}), Authorization: `Bearer ${token}` }
    }
    const resp = await fetch(url, opts)
    if (resp.status === 401 && !url.startsWith('/api/auth/')) {
      clearAuth()
      if (_unauthorizedHandler) _unauthorizedHandler()
      const err = new Error('未登录或登录已过期')
      err.status = 401
      throw err
    }
    // 腾讯行情那类接口返回的是文本不是 JSON，用 expectText 走同一条超时逻辑
    return expectText ? await resp.text() : await resp.json()
  } catch (e) {
    if (e.name === 'AbortError') {
      const err = new Error(`请求超时（${Math.round(timeout / 1000)}s）: ${url}`)
      err.timedOut = true
      console.warn(err.message)
      throw err
    }
    if (e.status !== 401) console.error(`API请求失败: ${url}`, e)
    throw e
  } finally {
    clearTimeout(timer)
  }
}

// 取纯文本响应（腾讯行情 `qt.gtimg.cn` 风格接口），同样带超时
export async function apiFetchText(url, options = {}) {
  return apiFetch(url, { ...options, expectText: true })
}

// 风险等级标签与样式（TabRecommend / TabScan 共用）
export function riskLabel(level) {
  return { low: '低风险', mid: '中风险', high: '高风险' }[level] || '中风险'
}

export function riskLevelClass(level) {
  return { low: 'low', mid: 'mid', high: 'high' }[level] || 'mid'
}
