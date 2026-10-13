import { ref, computed } from 'vue'

/**
 * Hash 路由（2026-10-13 行动台改造）
 *
 * 为什么不引入 vue-router：本应用是 10 个平级视图 + 2 个弹窗，没有嵌套路由、
 * 没有路由守卫需求（登录门禁在 App 顶层按 currentUser 判即可）；而构建产物是
 * FastAPI 直服的静态目录（vite.config outDir=../server/static），history 模式
 * 还要额外加一条 SPA catch-all 路由。hash 方案 30 行解决同样的问题：
 *   - 刷新后停在你看的那一页（此前永远弹回"实时行情"）
 *   - 可以把 #/quote/sz000021 发给同事
 *   - 跨页打开个股不再依赖父组件直接调子组件方法（tabQuoteRef.searchStock）
 */

// hash 段 → Tab id。Tab id 保持原样，所有既有 navigateToQuote 调用点无需改。
const TAB_BY_SEG = {
  desk: 'tab-desk',
  quote: 'tab-quote',
  scan: 'tab-scan',
  trend: 'tab-trend',
  recommend: 'tab-recommend',
  position: 'tab-position',
  flow: 'tab-flow',
  review: 'tab-review',
  sim: 'tab-sim',
  optimization: 'tab-optimization',
}
const SEG_BY_TAB = Object.fromEntries(Object.entries(TAB_BY_SEG).map(([k, v]) => [v, k]))

export const DEFAULT_TAB = 'tab-desk'

function parseHash(hash) {
  // 允许 #/position?phase=tail 这种带查询的写法：先剥掉 ? 之后的部分再认 Tab
  const raw = (hash || '').replace(/^#\/?/, '').split('?')[0]
  if (!raw) return { tab: DEFAULT_TAB, param: '' }
  const [seg, param = ''] = raw.split('/')
  const tab = TAB_BY_SEG[seg]
  if (!tab) return { tab: DEFAULT_TAB, param: '' }
  return { tab, param: decodeURIComponent(param) }
}

export const route = ref(parseHash(typeof location !== 'undefined' ? location.hash : ''))

// 最近一次 hash 变更的来源：'nav' 表示本应用主动跳转，'back' 表示浏览器前进/后退。
// App 里的 watcher 用它决定要不要再回写 hash（避免自激循环）。
let _lastSource = 'nav'

export function hashPath(tab, param = '') {
  // 同时接受 Tab id（'tab-position'）与 hash 段名（'position'），调用方不必记两套命名
  const seg = TAB_BY_SEG[tab] ? tab : (SEG_BY_TAB[tab] || 'desk')
  return param ? `#/${seg}/${encodeURIComponent(param)}` : `#/${seg}`
}

/** 跳转：写 hash，由 hashchange 统一驱动状态（前进/后退天然可用） */
export function navigate(tab, param = '') {
  const target = hashPath(tab, param)
  _lastSource = 'nav'
  if (location.hash === target) {
    // 同一页同一参数：hashchange 不会触发，手动同步一次状态
    route.value = parseHash(target)
    return
  }
  location.hash = target
}

/** 供 App 顶层在 hashchange 时调用 */
export function syncFromHash() {
  route.value = parseHash(location.hash)
  return route.value
}

export function isExternalNav() {
  return _lastSource !== 'nav'
}

export function markNav() {
  _lastSource = 'nav'
}

export function useHashRoute() {
  if (typeof window !== 'undefined' && !window.__gupiaoHashBound) {
    window.__gupiaoHashBound = true
    window.addEventListener('hashchange', () => {
      _lastSource = 'back'
      syncFromHash()
    })
  }
  // 首次进入没有 hash 时补一个，地址栏可见、可分享
  if (typeof location !== 'undefined' && !location.hash) {
    location.replace(hashPath(DEFAULT_TAB))
  }
  const activeTab = computed(() => route.value.tab)
  const routeParam = computed(() => route.value.param)
  return { route, activeTab, routeParam, navigate, syncFromHash }
}
