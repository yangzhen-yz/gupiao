import { onUnmounted, watch } from 'vue'

/**
 * 轮询治理（2026-10-13 行动台改造）
 *
 * 现状问题：已挂载的 Tab 永不销毁，隐藏中的 Tab 仍在按自己的 setInterval 打接口 ——
 * 最贵的一处是「趋势发现」在后台每 30s 重跑一次全市场扫描。
 *
 * 本组合函数只接管**定时器**，不接管**挂载**：App 仍然保持"懒挂载且永不销毁"，
 * 因为跨组件方法调用（tabQuoteRef.searchStock、tabPositionRef.loadPositions）依赖
 * 子组件实例一直活着。
 *
 * 沿用原有 setTimeout 自重排风格（而非 setInterval）：慢请求不会堆叠，
 * 间隔可在运行时变化（行动台按时段返回 nextPollMs）。
 *
 * @param {() => Promise<void>} fn  一次轮询要做的事（异常会被吞掉，不打断轮询）
 * @param {object} [opts]
 * @param {number|import('vue').Ref<number>|(() => number)} [opts.interval] 间隔毫秒，默认 60s
 * @param {import('vue').Ref<boolean>|(() => boolean)} [opts.active] 仅激活时轮询（通常传 props.active）
 * @param {boolean} [opts.immediate] 启动时立刻跑一次，默认 true
 * @param {number} [opts.minGapMs] 回到前台/切回该 Tab 时的"够不够新"阈值，默认 5s
 */
export function usePolling(fn, opts = {}) {
  const minGapMs = opts.minGapMs ?? 5000

  function readInterval() {
    const raw = typeof opts.interval === 'function'
      ? opts.interval()
      : (opts.interval && typeof opts.interval === 'object' ? opts.interval.value : opts.interval)
    const ms = Number(raw)
    return Number.isFinite(ms) && ms > 0 ? ms : 60000
  }

  function readActive() {
    const a = opts.active
    if (a === undefined || a === null) return true
    if (typeof a === 'function') return !!a()
    if (typeof a === 'object' && 'value' in a) return !!a.value
    return !!a
  }

  const visible = () => typeof document === 'undefined' || document.visibilityState === 'visible'

  let timer = null
  let running = false
  let started = false
  let lastAt = 0

  function clear() {
    if (timer) { clearTimeout(timer); timer = null }
  }

  function schedule(ms) {
    clear()
    if (!started) return
    timer = setTimeout(tick, ms ?? readInterval())
  }

  async function tick() {
    clear()
    if (!started || running) return
    if (!visible() || !readActive()) {
      // 不满足条件时仍自重排：等 visibilitychange / active 变化把它提前叫醒
      schedule()
      return
    }
    running = true
    try {
      await fn()
      lastAt = Date.now()
    } catch (e) {
      /* 单次失败静默，下一轮继续 */
    } finally {
      running = false
      schedule()
    }
  }

  /** 手动刷新（够新则跳过，避免点 Tab 就重复请求） */
  async function refresh(force = true) {
    if (!started) return
    if (!force && Date.now() - lastAt < readInterval()) { schedule(); return }
    await tick()
  }

  function start() {
    if (started) return
    started = true
    if (opts.immediate === false) schedule()
    else tick()
  }

  function stop() {
    started = false
    clear()
  }

  // 切回该 Tab：立刻补一次（超过 minGapMs 才算过期），否则只重新排程
  if (typeof opts.active === 'function' || (opts.active && typeof opts.active === 'object')) {
    watch(() => readActive(), (on) => {
      if (!started) return
      if (on && Date.now() - lastAt > minGapMs) tick()
      else schedule()
    })
  }

  const onVisibility = () => {
    if (!started) return
    if (visible() && readActive() && Date.now() - lastAt > minGapMs) tick()
    else schedule()
  }
  if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', onVisibility)
  }

  onUnmounted(() => {
    stop()
    if (typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', onVisibility)
    }
  })

  return { start, stop, refresh, schedule }
}
