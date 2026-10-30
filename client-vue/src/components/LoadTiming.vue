<template>
  <!-- 页面加载耗时（2026-10-30）：切页后自动出现，收起时只占一行。
       数据全部来自浏览器内存，见 utils/perf.js 头部的口径说明。 -->
  <div
    v-if="show"
    class="lt"
    :class="{ 'lt-busy': perfState.active, 'lt-open': perfState.open }"
  >
    <div class="lt-line" @click="perfState.open = !perfState.open">
      <span class="lt-tag">{{ perfState.active ? '加载中' : '本次加载' }}</span>
      <b class="lt-total">{{ sec(shownMs) }}</b>
      <span class="lt-meta" v-if="rows.length">
        {{ perfState.requests.length }} 个请求 · 最慢 {{ shortPath(slowest.url) }}
        {{ sec(slowest.ms) }}<span class="lt-srv-inline" v-if="slowest.serverMs != null">（服务端 {{ sec(slowest.serverMs) }}）</span>
      </span>
      <span class="lt-meta lt-wait" v-else>等待首个请求…</span>
      <span class="lt-caret">{{ perfState.open ? '▴' : '▾' }}</span>
      <button class="lt-close" type="button" title="收起本条（切页后自动出现）" @click.stop="dismiss">×</button>
    </div>

    <div v-if="perfState.open" class="lt-panel">
      <div class="lt-hint" v-if="!perfState.active && perfState.startWaitMs >= 300">
        切到本页后先等了 {{ sec(perfState.startWaitMs) }} 才发出第一个请求 ——
        多半是页面代码（异步 chunk）还在下载，或上一页的轮询没收住。
      </div>
      <div class="lt-row lt-head">
        <span class="lt-path">接口</span>
        <span class="lt-at">发出</span>
        <span class="lt-ms">总耗时</span>
        <span class="lt-srv">服务端</span>
      </div>
      <div v-for="(r, i) in rows" :key="i" class="lt-row">
        <span class="lt-path">{{ shortPath(r.url) }}</span>
        <span class="lt-at">+{{ sec(r.atMs) }}</span>
        <span class="lt-ms" :class="{ slow: r.ms >= 1000 }">{{ sec(r.ms) }}</span>
        <span class="lt-srv">{{ r.serverMs == null ? '—' : sec(r.serverMs) }}</span>
        <span class="lt-bad" v-if="!r.ok">失败{{ r.status ? ' ' + r.status : '' }}</span>
        <span class="lt-net" v-else-if="r.serverMs != null && r.ms - r.serverMs >= 400">
          传输/排队 {{ sec(r.ms - r.serverMs) }}
        </span>
      </div>
      <div class="lt-foot">
        总耗时＝切到本页 → 首屏这批请求全部返回（连续 1.2 秒没有新请求就收口，不把后面的轮询算进来）；
        服务端耗时取自响应头 X-Process-Time，与总耗时的差额＝传输、隧道/代理排队、浏览器解析 JSON。
        只在内存里记，刷新即清零，不写库、不额外发请求。
      </div>
    </div>
  </div>
</template>

<script setup>
  import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
  import { perfState, elapsedMs, shortPath } from '../utils/perf'

  const tick = ref(0)
  const dismissed = ref(false)
  let timer = null

  // 加载中每 150ms 推一次实时读数；收口后停掉，不再触发重渲染
  onMounted(() => {
    timer = setInterval(() => {
      if (perfState.active) tick.value += 1
    }, 150)
  })
  onUnmounted(() => clearInterval(timer))

  // 切页就重新出现（用户手动收起只对本次页面有效）
  watch(
    () => perfState.page,
    () => {
      dismissed.value = false
    }
  )

  const show = computed(() => !dismissed.value && (perfState.active || perfState.visible))

  const shownMs = computed(() => {
    tick.value // 依赖实时心跳
    return perfState.active ? elapsedMs() : perfState.totalMs
  })

  const rows = computed(() => [...perfState.requests].sort((a, b) => b.ms - a.ms))
  const slowest = computed(() => rows.value[0] || { url: '', ms: 0, serverMs: null })

  function dismiss() {
    dismissed.value = true
    perfState.open = false
  }

  function sec(ms) {
    const v = Math.max(0, Number(ms) || 0) / 1000
    if (v >= 10) return `${v.toFixed(1)}s`
    return `${v.toFixed(2)}s`
  }
</script>

<style scoped>
  .lt {
    padding: 5px var(--content-padding);
    background: var(--bg-tertiary);
    border-bottom: 1px solid var(--border-subtle);
    font-size: 0.75rem;
    line-height: 1.4;
    color: var(--text-secondary);
  }

  .lt-line {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
    cursor: pointer;
    user-select: none;
  }

  .lt-tag {
    flex: none;
    color: var(--text-tertiary);
  }

  .lt-total {
    flex: none;
    font-variant-numeric: tabular-nums;
    color: var(--text-primary);
  }

  .lt-busy .lt-total {
    color: var(--accent-warning);
  }

  .lt-meta {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-tertiary);
  }

  .lt-wait {
    color: var(--accent-warning);
  }

  .lt-caret {
    flex: none;
    color: var(--text-tertiary);
  }

  .lt-close {
    flex: none;
    width: 18px;
    height: 18px;
    padding: 0;
    border: none;
    background: transparent;
    color: var(--text-tertiary);
    font-size: 0.9375rem;
    line-height: 1;
    cursor: pointer;
  }

  .lt-close:hover {
    color: var(--text-primary);
  }

  .lt-panel {
    margin-top: 6px;
    padding: 8px 10px;
    max-height: 46vh;
    overflow-y: auto;
    background: var(--bg-secondary);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
  }

  .lt-hint {
    margin-bottom: 6px;
    color: var(--accent-warning);
  }

  .lt-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 64px 64px 64px;
    gap: 6px;
    padding: 2px 0;
    align-items: baseline;
  }

  .lt-head {
    color: var(--text-tertiary);
    border-bottom: 1px solid var(--border-subtle);
    margin-bottom: 2px;
  }

  .lt-path {
    min-width: 0;
    overflow-wrap: anywhere;
    color: var(--text-primary);
  }

  .lt-at,
  .lt-ms,
  .lt-srv {
    font-variant-numeric: tabular-nums;
    text-align: right;
    white-space: nowrap;
  }

  .lt-ms.slow {
    color: var(--accent-warning);
    font-weight: 600;
  }

  .lt-srv {
    color: var(--text-tertiary);
  }

  .lt-bad {
    grid-column: 1 / -1;
    color: var(--accent-danger);
    text-align: right;
  }

  .lt-net {
    grid-column: 1 / -1;
    color: var(--text-tertiary);
    text-align: right;
  }

  .lt-foot {
    margin-top: 6px;
    padding-top: 6px;
    border-top: 1px solid var(--border-subtle);
    color: var(--text-tertiary);
    font-size: 0.6875rem;
    line-height: 1.5;
  }

  @media (max-width: 620px) {
    .lt {
      font-size: 0.6875rem;
    }

    /* 手机上收起态只留最关键的"总耗时 + 请求数 + 最慢接口"，
       服务端耗时在展开明细里仍逐条可见，不靠省略号藏内容 */
    .lt-srv-inline {
      display: none;
    }

    .lt-row {
      grid-template-columns: minmax(0, 1fr) 56px 56px 56px;
      font-size: 0.6875rem;
    }
  }
</style>
