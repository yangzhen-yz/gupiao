// 页面加载耗时探针（2026-10-30）
//
// 目的：回答"这次打开页面到底慢在哪"——是某个接口慢、还是服务端队列排队、
// 还是切页后页面代码/首请求之间的空档。只在浏览器内存里记，不写库、不额外发请求。
//
// 口径：
//   · 本次加载 = 从切到该页 → 首屏这批请求全部返回；连续 1.2s 没有新请求即收口
//     （30s 硬上限，防止拖到下一轮轮询里）。
//   · 服务端耗时来自响应头 X-Process-Time（app/main.py 的 http 中间件写入）。
//     总耗时与服务端之比 = 传输 + 隧道/代理排队 + 浏览器解析 JSON。
//   · 首请求等待 = 切页到第一个请求发出之间的空档，通常是异步 chunk 下载或上页轮询没停。
//   · 只记 /api 与 /eastmoney 的请求，/api/auth/* 属于登录态校验，不计入页面耗时。
//
// 这不是埋点上报：没有 URL 参数、没有 localStorage、没有后端写入；刷新即清零。
import { reactive } from 'vue'

const IDLE_MS = 1200
const HARD_MS = 30000

export const perfState = reactive({
  page: '',
  label: '',
  active: false,    // 正在测量（加载中）
  frozen: false,    // 已收口，数字不再变
  visible: false,   // 有可展示的结果
  totalMs: 0,       // 切页 → 首屏请求全部返回（含一帧渲染）
  startWaitMs: 0,   // 切页 → 第一个请求发出
  requests: [],     // {url, atMs, ms, serverMs, status, ok}
  open: false,      // 明细是否展开
})

let gen = 0          // 切页代数：上一页迟到的响应不许污染本页
let pending = 0      // 本页在途请求数
let startedAt = 0
let lastEndAt = 0    // 最后一个请求**返回**的时刻（收口宽限期不算进总耗时）
let idleTimer = null
let hardTimer = null

function trackable(url) {
  const u = String(url || '')
  if (!u) return false
  if (u.startsWith('/api/auth/')) return false
  return u.startsWith('/api/') || u.startsWith('/eastmoney/') || u.startsWith('http')
}

/** 切页时调用：重置并开始计时。label 是页面中文名，用于展示。 */
export function startPage(page, label = '') {
  gen += 1
  pending = 0
  lastEndAt = 0
  clearTimeout(idleTimer)
  clearTimeout(hardTimer)
  startedAt = performance.now()
  Object.assign(perfState, {
    page,
    label,
    active: true,
    frozen: false,
    visible: false,
    totalMs: 0,
    startWaitMs: 0,
    requests: [],
    open: false,
  })
  hardTimer = setTimeout(finish, HARD_MS)
}

/** 请求发出时调用（apiFetch 内部）。返回的 token 必须在结束时原样交回。 */
export function beginRequest(url) {
  const track = perfState.active && trackable(url)
  const token = {
    gen,
    url: String(url || ''),
    t0: performance.now(),
    atMs: track ? Math.max(0, performance.now() - startedAt) : 0,
    track,
  }
  if (track) {
    pending += 1
    // 有在途请求时不收口：否则 5s 的慢接口会被 1.2s 的空闲计时提前判死
    clearTimeout(idleTimer)
  }
  return token
}

/** 请求结束时调用（含失败/超时）。 */
export function endRequest(token, info = {}) {
  if (!token || token.gen !== gen) return
  if (!token.track) return
  if (!perfState.active) return
  pending = Math.max(0, pending - 1)
  lastEndAt = performance.now()
  perfState.requests.push({
    url: token.url,
    atMs: token.atMs,
    ms: Math.max(0, Math.round(Number(info.ms) || 0)),
    serverMs: Number.isFinite(info.serverMs) ? Math.round(info.serverMs) : null,
    status: Number(info.status) || 0,
    ok: info.ok !== false,
  })
  if (perfState.requests.length > 60) perfState.requests.shift()
  if (pending === 0) {
    clearTimeout(idleTimer)
    idleTimer = setTimeout(finish, IDLE_MS)
  }
}

function finish() {
  if (!perfState.active) return
  clearTimeout(idleTimer)
  clearTimeout(hardTimer)
  const base = startedAt
  const pageGen = gen
  perfState.active = false
  perfState.frozen = true
  if (!perfState.requests.length) {
    perfState.visible = false
    return
  }
  perfState.visible = true
  // 总耗时算到最后一个请求**返回**为止；1.2 秒收口宽限只是判断"这批发完了"，
  // 不能算进加载时间（否则每次都会平白多 1.2s，手机上看着像很慢）。
  const endAt = lastEndAt || performance.now()
  perfState.totalMs = Math.round(endAt - base)
  perfState.startWaitMs = Math.min(...perfState.requests.map(r => r.atMs || 0))
  // 等一帧把渲染补进去，但只补前 300ms：后台标签页的 rAF 会被节流，
  // 不能让它把总耗时越拖越大（本机实测隐藏标签下 rAF 能晚 1 秒以上）。
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(() => {
      if (gen === pageGen && perfState.frozen) {
        const extra = performance.now() - base - perfState.totalMs
        if (extra > 0 && extra < 300) perfState.totalMs += Math.round(extra)
      }
    })
  }
}

/** 加载过程中的实时读数（展示条用） */
export function elapsedMs() {
  return perfState.active ? Math.round(performance.now() - startedAt) : perfState.totalMs
}

/** 给展示条取短路径：去掉 query，保留 /api/xxx 便于和接口用量页对上 */
export function shortPath(url) {
  const s = String(url || '')
  try {
    return new URL(s, typeof location !== 'undefined' ? location.origin : 'http://x').pathname
  } catch (e) {
    return s.split('?')[0]
  }
}
