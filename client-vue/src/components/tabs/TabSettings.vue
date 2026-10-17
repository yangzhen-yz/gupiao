<template>
  <div class="settings-page">
    <!-- 顶部口径摘要：先让人看见"现在到底是什么范围在选票" -->
    <div class="scope-card" v-if="summary">
      <div class="scope-nums">
        <div class="num">
          <b>{{ summary.selectableScope }}</b
          ><span>只 · 选票范围</span>
        </div>
        <div class="num dim">
          <b>{{ summary.dataScope }}</b
          ><span>只 · 数据范围</span>
        </div>
      </div>
      <div class="scope-text">{{ summary.scopeText }}</div>
      <div class="scope-note">
        数据范围＝日K同步与覆盖率分母；选票范围＝海选排名、推荐、扫描真正用的那批。
        分开放是因为流动性会来回变，用它管同步会让历史序列挖洞。
      </div>
      <div class="warn" v-if="summary.staleOutOfSync">
        另有 {{ summary.staleOutOfSync }} 只因日K已停止同步（最后一条不是最新交易日）而不可能入选；
        放宽市值/主板这类下限也不会立刻放回来，得先把它们纳入同步。
      </div>
      <div class="warn" v-if="summary.previewParity && summary.previewParity.ok === false">
        ⚠️ 影响预览与真实范围不一致（预览 {{ summary.previewParity.preview }} vs 真实
        {{ summary.previewParity.actual }}）—— 预览数字先别信，等修。
      </div>
    </div>

    <div class="readonly" v-if="loaded && !canWrite">
      你能看当前口径，但只有管理员账号能改（这些开关一改就影响所有账号的选票范围）。
    </div>

    <div v-if="loading" class="loading">读取配置…</div>

    <section v-for="g in groups" :key="g.name" class="group">
      <h3 @click="toggle(g.name)">
        {{ g.name }}
        <span class="cnt">{{ g.items.length }} 项</span>
        <span class="arrow">{{ openGroups.has(g.name) ? '▾' : '▸' }}</span>
      </h3>
      <div v-show="openGroups.has(g.name)" class="rows">
        <div v-for="it in g.items" :key="it.key" class="row" :class="{ overridden: it.overridden }">
          <div class="main">
            <label class="lab">
              <input
                type="checkbox"
                v-model="it.value"
                :disabled="!canWrite || it.saving"
                v-if="it.kind === 'bool'"
                @change="save(it)"
              />
              <span class="name">{{ it.label }}</span>
            </label>
            <!-- 下拉型（如黑名单判据）：选项由后端给，前端不写死，避免两边不一致 -->
            <div class="ctl" v-if="it.kind === 'enum'">
              <select :value="it.value" :disabled="!canWrite || it.saving" @change="onEnum(it, $event)">
                <option v-for="o in it.options" :key="o.value" :value="o.value">{{ o.label }}</option>
              </select>
              <button class="mini" v-if="it.overridden && canWrite" @click="reset(it)">恢复默认</button>
            </div>
            <div class="ctl" v-else-if="it.kind === 'int' || it.kind === 'float'">
              <input
                type="number"
                :value="it.value"
                :min="it.min"
                :max="it.max"
                :step="it.step"
                :disabled="!canWrite || it.saving"
                @input="onInput(it, $event)"
                @change="onNum(it, $event)"
              />
              <span class="unit">{{ it.unit }}</span>
              <button class="mini" v-if="it.overridden && canWrite" @click="reset(it)">恢复默认</button>
            </div>
            <span class="tags">
              <span class="tag live" v-if="it.live">即时生效</span>
              <span class="tag restart" v-else>改完要重启</span>
              <span class="tag cov" v-if="it.overridden">已覆盖默认 {{ round2(it.default) }}{{ it.unit }}</span>
            </span>
          </div>
          <div class="note">{{ it.note }}</div>
          <div class="preview" v-if="it.preview">
            <template v-if="it.kind === 'bool'">{{ it.value ? '若关掉这一条：' : '若打开这一条：' }}</template>
            <template v-else>按这个值预览：</template>
            <b>{{ it.preview.now }} → {{ it.preview.after }} 只</b>
            <span :class="['delta', it.preview.delta > 0 ? 'up' : it.preview.delta < 0 ? 'down' : 'same']">
              {{ it.preview.delta > 0 ? '+' : '' }}{{ it.preview.delta }}
            </span>
            <span class="hint" v-if="it.preview.hint">｜{{ it.preview.hint }}</span>
          </div>
          <div class="err" v-if="it.error">{{ it.error }}</div>
        </div>
      </div>
    </section>

    <section class="group" v-if="history.length">
      <h3 @click="showHistory = !showHistory">
        改动记录
        <span class="cnt">{{ history.length }} 条</span>
        <span class="arrow">{{ showHistory ? '▾' : '▸' }}</span>
      </h3>
      <div v-show="showHistory" class="rows hist">
        <div v-for="(h, i) in history" :key="i" class="hrow">
          <span class="htime">{{ h.updated_at }}</span>
          <span class="hwho">{{ h.updated_by }}</span>
          <span class="hkey">{{ labelOf(h.key) || h.key }}</span>
          <span class="hval">→ {{ h.value }}</span>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup>
  import { ref, reactive, computed, onMounted } from 'vue'
  import { inject } from 'vue'
  import { apiFetch } from '../../utils/helpers'
  import { round2 } from '../../utils/format'

  const showNotification = inject('showNotification', () => {})

  const loading = ref(true)
  const loaded = ref(false)
  const canWrite = ref(false)
  const items = ref([])
  const summary = ref(null)
  const history = ref([])
  const showHistory = ref(false)
  const openGroups = reactive(new Set(['选股范围']))

  const groups = computed(() => {
    const by = {}
    items.value.forEach(it => {
      ;(by[it.group] = by[it.group] || []).push(it)
    })
    return Object.keys(by).map(name => ({ name, items: by[name] }))
  })

  function labelOf(key) {
    const it = items.value.find(x => x.key === key)
    return it && it.label
  }

  async function load() {
    loading.value = true
    try {
      const res = await apiFetch('/api/settings')
      const d = res.data || {}
      items.value = (d.items || []).map(it => ({ ...it, saving: false, preview: null, error: '' }))
      summary.value = d.summary || null
      history.value = d.history || []
      canWrite.value = !!d.canWrite
      loaded.value = true
    } catch (e) {
      showNotification('读取配置失败：' + (e.message || e), 'error')
    } finally {
      loading.value = false
    }
  }

  // 预览只在需要时打：全量预取要 4 秒以上，不该挡首屏。
  //  · 布尔项：展示**反方**预览（"关掉这一条会放回多少只"），这是决定要不要拨它的依据；
  //  · 数值项：边输边预览（@input 防抖），拨到某个阈值前先看见代价。
  async function fetchPreview(it, incoming) {
    try {
      const res = await apiFetch(`/api/settings/preview?key=${it.key}&value=${encodeURIComponent(incoming)}`)
      it.preview = res.data || null
    } catch (e) {
      it.preview = null
    }
  }

  let numTimer = null
  function onInput(it, ev) {
    const v = Number(ev.target.value)
    if (Number.isNaN(v)) return
    clearTimeout(numTimer)
    numTimer = setTimeout(() => fetchPreview(it, v), 400)
  }

  async function previewOpposites(list) {
    for (const it of list) {
      if (it.kind === 'bool') await fetchPreview(it, !it.value)
    }
  }

  async function save(it) {
    it.error = ''
    it.saving = true
    try {
      await apiFetch(`/api/settings/${it.key}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value: it.value }),
      })
      showNotification(
        `${it.label} 已改为` +
          (it.kind === 'bool' ? (it.value ? '开' : '关') : round2(it.value) + it.unit) +
          (it.live ? '（即时生效）' : '（要重启服务才生效）'),
        'success'
      )
      await load()
    } catch (e) {
      it.error = (e && (e.detail || e.message)) || '保存失败'
      showNotification(`${it.label} 保存失败：${it.error}`, 'error')
      await load() // 回滚到后端认为的正确值，别让界面停在一个没生效的状态
    } finally {
      it.saving = false
    }
  }

  async function onEnum(it, ev) {
    it.value = ev.target.value // 下拉只有有限几个取值，选中即保存
    await save(it)
  }

  async function onNum(it, ev) {
    const v = Number(ev.target.value)
    if (Number.isNaN(v)) {
      await load()
      return
    }
    it.value = v
    await save(it)
  }

  async function reset(it) {
    try {
      await apiFetch(`/api/settings/${it.key}/reset`, { method: 'POST' })
      showNotification(`${it.label} 已恢复默认 ${round2(it.default)}${it.unit}`, 'success')
      await load()
    } catch (e) {
      showNotification('恢复默认失败：' + ((e && (e.detail || e.message)) || e), 'error')
    }
  }

  onMounted(async () => {
    await load()
    // 默认展开的那一组补反方预览；展开别的组时也算那一组
    const first = groups.value[0]
    if (first) previewOpposites(first.items)
  })

  async function toggle(name) {
    const willOpen = !openGroups.has(name)
    if (willOpen) {
      const g = groups.value.find(x => x.name === name)
      if (g) previewOpposites(g.items)
    }
    if (willOpen) openGroups.add(name)
    else openGroups.delete(name)
  }
</script>

<style scoped>
  /* ⚠️ 只用 assets/main.css 里定义的主题 token（--bg-* / --text-* / --accent-* / --border-*），
   不要写死颜色、也不要引用不存在的变量名：第一版卡片底色写的是"未定义变量 + 深蓝兜底值"，
   浅色主题下兜底值生效 → 深底配主题的深色文字，整页内容看不见（2026-10-17 用户截图反馈）。 */
  .settings-page {
    padding: var(--spacing-sm) 0 var(--spacing-xl);
    color: var(--text-primary);
  }
  .scope-card {
    background: var(--bg-secondary);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    padding: var(--spacing-md);
    margin-bottom: var(--spacing-sm);
    box-shadow: var(--shadow-soft);
  }
  .scope-nums {
    display: flex;
    gap: var(--spacing-lg);
    align-items: baseline;
  }
  .num b {
    font-size: 22px;
    color: var(--text-primary);
  }
  .num span {
    font-size: 12px;
    color: var(--text-tertiary);
    margin-left: 4px;
  }
  .num.dim b {
    font-size: 16px;
    color: var(--text-secondary);
  }
  .scope-text {
    font-size: 12px;
    line-height: 1.55;
    margin-top: 6px;
    color: var(--text-secondary);
    word-break: break-all;
  }
  .scope-note {
    font-size: 11px;
    color: var(--text-tertiary);
    margin-top: 6px;
    line-height: 1.5;
  }
  .warn {
    font-size: 11.5px;
    margin-top: 6px;
    color: var(--accent-warning);
    line-height: 1.5;
  }
  .readonly {
    font-size: 12px;
    padding: 6px var(--spacing-sm);
    margin-bottom: var(--spacing-sm);
    background: var(--accent-warning-bg);
    color: var(--accent-warning);
    border: 1px solid var(--accent-warning-border);
    border-radius: var(--radius-sm);
  }
  .loading {
    font-size: 12px;
    color: var(--text-tertiary);
    padding: var(--spacing-sm);
  }
  .group {
    margin-bottom: var(--spacing-sm);
  }
  .group h3 {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 13px;
    margin: 0 0 6px;
    padding: 7px var(--spacing-sm);
    cursor: pointer;
    color: var(--text-primary);
    background: var(--bg-secondary);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    box-shadow: var(--shadow-xs);
  }
  .group h3 .cnt,
  .group h3 .arrow {
    font-size: 11px;
    color: var(--text-tertiary);
    font-weight: 400;
  }
  .group h3 .arrow {
    margin-left: auto;
  }
  .rows {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .row {
    background: var(--bg-secondary);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    padding: var(--spacing-sm);
    box-shadow: var(--shadow-xs);
  }
  .row.overridden {
    border-color: var(--accent-info-border);
    background: var(--accent-info-bg);
  }
  .main {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    flex-wrap: wrap;
  }
  .lab {
    display: flex;
    align-items: center;
    gap: 6px;
    cursor: pointer;
  }
  .lab input[type='checkbox'] {
    width: 16px;
    height: 16px;
    accent-color: var(--accent-info);
  }
  .name {
    font-size: 13px;
    color: var(--text-primary);
  }
  .ctl {
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .ctl input {
    width: 84px;
    padding: 3px 6px;
    font-size: 13px;
    color: var(--text-primary);
    background: var(--bg-primary);
    border: 1px solid var(--border-medium);
    border-radius: var(--radius-xs);
  }
  .unit {
    font-size: 11px;
    color: var(--text-tertiary);
  }
  .mini {
    font-size: 11px;
    padding: 2px 6px;
    background: transparent;
    color: var(--accent-info);
    border: 1px solid var(--accent-info-border);
    border-radius: var(--radius-xs);
    cursor: pointer;
  }
  .tags {
    display: flex;
    gap: 4px;
    flex-wrap: wrap;
    margin-left: auto;
  }
  .tag {
    font-size: 10.5px;
    padding: 1px 5px;
    border-radius: var(--radius-xs);
    border: 1px solid transparent;
  }
  .tag.live {
    color: var(--accent-success);
    background: var(--accent-success-bg);
    border-color: var(--accent-success-border);
  }
  .tag.restart {
    color: var(--accent-warning);
    background: var(--accent-warning-bg);
    border-color: var(--accent-warning-border);
  }
  .tag.cov {
    color: var(--accent-info);
    background: var(--accent-info-bg);
    border-color: var(--accent-info-border);
  }
  .note {
    font-size: 11px;
    color: var(--text-tertiary);
    margin-top: 4px;
    line-height: 1.5;
  }
  .preview {
    font-size: 11.5px;
    margin-top: 5px;
    color: var(--text-secondary);
    line-height: 1.5;
  }
  .preview b {
    color: var(--text-primary);
  }
  .preview .delta.up {
    color: var(--accent-success);
  }
  .preview .delta.down {
    color: var(--accent-danger);
  }
  .preview .delta.same {
    color: var(--text-tertiary);
  }
  .preview .hint {
    color: var(--accent-warning);
  }
  .err {
    font-size: 11.5px;
    color: var(--accent-danger);
    background: var(--accent-danger-bg);
    border: 1px solid var(--accent-danger-border);
    border-radius: var(--radius-xs);
    margin-top: 5px;
    padding: 4px 6px;
  }
  .hist .hrow {
    display: flex;
    gap: var(--spacing-sm);
    font-size: 11px;
    padding: 3px var(--spacing-xs);
    color: var(--text-secondary);
    border-bottom: 1px solid var(--border-subtle);
  }
  .hist .htime {
    color: var(--text-tertiary);
  }
  .hist .hval {
    margin-left: auto;
    color: var(--text-tertiary);
  }
</style>
