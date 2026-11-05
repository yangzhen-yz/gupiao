/**
 * 全站 UI 巡检（只读）：逐个打开所有 Tab、点击所有安全的切换类按钮，
 * 采集每个动作触发的 /api 请求耗时，输出慢点清单。
 *
 * 用法：
 *   GP_TOKEN=<有效会话token> SCAN_ONLY=1 node scripts/ui_sweep.cjs   # 只扫按钮清单
 *   GP_TOKEN=<有效会话token> node scripts/ui_sweep.cjs                # 全量点击巡检
 *   GP_BASE=http://127.0.0.1:3003 可覆盖地址
 *
 * 安全约束：只点白名单里的「切换/筛选/分页/排序」类按钮；任何会写库、触发全市场
 * 重扫、发推送、改配置的按钮一律跳过（黑名单见 DANGEROUS）。巡检不修改任何业务数据。
 */
const { chromium } = require('D:/360Downloads/cc/node_modules/playwright')
const fs = require('fs')
const path = require('path')

const BASE = process.env.GP_BASE || 'http://127.0.0.1:3003'
const TOKEN = process.env.GP_TOKEN || ''
const SCAN_ONLY = process.env.SCAN_ONLY === '1'
const OUT = path.join(__dirname, '..', 'server', 'analysis_out', 'ui_sweep_report.json')

// hash 段 → 页面名 / 所属组 / 菜单文本
const TABS = [
  { seg: 'desk', label: '行动台', group: null },
  { seg: 'quote', label: '实时行情', group: 'market' },
  { seg: 'scan', label: '股票筛选', group: 'market' },
  { seg: 'trend', label: '趋势发现', group: 'market' },
  { seg: 'flow', label: '资金流向', group: 'market' },
  { seg: 'recommend', label: '智能推荐', group: 'decide' },
  { seg: 'position', label: '持仓管理', group: 'decide' },
  { seg: 'sim', label: '模拟交易', group: 'decide' },
  { seg: 'review', label: '每日收评', group: 'review' },
  { seg: 'optimization', label: '自动优化', group: 'review' },
  { seg: 'settings', label: '口径与开关', group: 'review' },
  { seg: 'backtest', label: '回测曲线', group: 'review' },
  { seg: 'api-usage', label: '接口用量', group: 'ops' },
]
const GROUP_LABEL = { market: '盘面', decide: '决策', review: '复盘', ops: '运维' }

// 精确白名单：只允许「切换 / 筛选 / 分页 / 只读查询 / 展开」类按钮。
// 任何写库、全市场重扫、发推送、改配置的按钮都不在此表 → 一律跳过。
const CLICK_WHITELIST = {
  desk: [/^展开全部/, /^另有 \d+ 条转弱信号/, /^全部 \d+ 个/, /^口径与分项/, /^口径与来源/],
  // 弹窗类先点：查询/🔄 会触发重渲染把条件渲染的按钮摘掉（11-04 实测 K线图被 SKIP）
  quote: [/^K线图$/, /^五层诊断$/, /^查询$/, /^🔄$/],
  scan: [],                       // 只有一个 select，走下面的下拉切换
  trend: [/^K线$/],                // 同名按钮只点第一个
  flow: [/^下一页/],               // 分页只点一次
  recommend: [/^📈 K线$/, /^下一页$/, /^按现价/, /^挂低吸单/],
  position: [/^设置$/, /^绑定$/],   // 打开推送渠道设置弹窗（只读，不点保存）
  sim: [/^↻ 手动刷新$/],
  review: [/^涨幅榜$/, /^跌幅榜$/, /^主力资金$/, /^下一页$/],
  optimization: [/^↻ 手动刷新$/],
  settings: [],
  backtest: [/^近3个月$/, /^近6个月$/, /^近12个月$/, /^上午档·次日开盘卖$/,
             /^下午档·次日尾盘卖$/, /^尾盘买入·次日开盘卖$/, /^排板全成交·次日尾盘卖$/],
  'api-usage': [/^刷新$/],
}
// 会打开弹窗的按钮：点完要测量弹窗自身请求，再关掉，避免影响后续页面
const MODAL_BUTTONS = /K线|五层诊断|^设置$|^绑定$/
function isAllowed(seg, text) {
  return (CLICK_WHITELIST[seg] || []).some(re => re.test(text))
}

function now() { return Date.now() }
function normUrl(u) {
  try {
    const x = new URL(u)
    return x.pathname + (x.search ? '?' + x.search.split('&').slice(0, 3).join('&') : '')
  } catch (e) { return u }
}

async function main() {
  if (!TOKEN) throw new Error('缺少 GP_TOKEN 环境变量（只读巡检需要一个有效会话）')
  const browser = await chromium.launch({ headless: true, channel: 'chrome' })
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  await context.addInitScript(t => {
    try { localStorage.setItem('gupiao_auth_token', t) } catch (e) {}
  }, TOKEN)
  const page = await context.newPage()

  // 请求日志：所有 /api/ 请求，记录发起与结束时间
  const log = []
  const reqMeta = new WeakMap()
  page.on('request', r => {
    // 也记录懒加载 chunk：切页白等往往在 JS 下载，而不只在接口
    if (!r.url().includes('/api/') && !r.url().includes('/assets/')) return
    const item = { url: normUrl(r.url()), full: r.url(), method: r.method(), start: now(), end: 0, status: 0, failed: '' }
    reqMeta.set(r, item)
    log.push(item)
  })
  page.on('response', res => {
    const it = reqMeta.get(res.request()); if (it) it.status = res.status()
  })
  page.on('requestfinished', r => {
    const it = reqMeta.get(r); if (!it) return
    it.end = now()
  })
  page.on('requestfailed', r => {
    const it = reqMeta.get(r); if (!it) return
    it.end = now(); it.failed = (r.failure() || {}).errorText || 'failed'
  })

  // 等待「本动作新触发的请求」全部完成。每个 URL 只跟踪本动作里的第一条，
  // 之后同 URL 的轮询不再算新请求 —— 否则轮询会让等待永远不 quiet（实测每页白等 25s）。
  async function settle(from, baseline, capMs = 25000, quietMs = 600, minMs = 900) {
    const t0 = now()
    const tracked = new Map()
    let cursor = from
    let lastNew = t0
    while (now() - t0 < capMs) {
      while (cursor < log.length) {
        const r = log[cursor++]
        if (baseline.has(r.url) || tracked.has(r.url)) continue
        tracked.set(r.url, r)
        lastNew = now()
      }
      const pending = [...tracked.values()].filter(r => !r.end)
      if (now() - t0 >= minMs && !pending.length && now() - lastNew >= quietMs) break
      await page.waitForTimeout(120)
    }
    // 收尾：把最后 120ms 内新起的请求也纳入
    while (cursor < log.length) {
      const r = log[cursor++]
      if (!baseline.has(r.url) && !tracked.has(r.url)) tracked.set(r.url, r)
    }
    const fresh = [...tracked.values()]
    return {
      totalMs: now() - t0,
      reqs: fresh.map(r => ({ url: r.url, full: r.full, method: r.method, ms: (r.end || now()) - r.start, status: r.status, failed: r.failed })),
      slowest: fresh.length ? Math.max(...fresh.map(r => (r.end || now()) - r.start)) : 0,
      timedOut: fresh.some(r => !r.end),
    }
  }

  const results = []
  const boot0 = now()
  await page.goto(BASE + '/#/desk', { waitUntil: 'domcontentloaded', timeout: 30000 })
  await page.waitForSelector('.main-container', { timeout: 30000 })
  await page.waitForTimeout(1800)
  const bootRes = { totalMs: now() - boot0, reqs: log.map(r => ({ url: r.url, ms: (r.end || now()) - r.start, status: r.status })) }
  results.push({ kind: 'boot', label: '登录+行动台首屏', ...bootRes })
  console.log(`[BOOT] ${bootRes.totalMs}ms, ${bootRes.reqs.length} reqs`)

  async function gotoGroup(seg, label, group) {
    if (!group) {
      await page.locator('.desk-entry').click()
      return
    }
    const chip = page.locator(`.tab-group:not(.all-group) .group-chip`).filter({ hasText: GROUP_LABEL[group] }).first()
    await chip.click({ timeout: 4000 })
    await page.waitForSelector('[data-nav-menu] .menu-item', { timeout: 4000 })
    await page.locator('[data-nav-menu] .menu-item').filter({ hasText: label }).first().click()
  }

  for (const tab of TABS) {
    const from = log.length
    const baseline = new Set(log.map(r => r.url))
    let clickErr = ''
    // 进页面前先清掉上一页可能残留的弹窗/菜单
    await page.keyboard.press('Escape').catch(() => {})
    try {
      if (tab.seg !== 'desk') await gotoGroup(tab.seg, tab.label, tab.group)
    } catch (e) {
      // 宽屏菜单点击失败时退化为 hash 直达（页面本身仍会被真实渲染）
      clickErr = e.message.slice(0, 120)
      await page.evaluate(s => { location.hash = '#/' + s }, tab.seg)
    }
    const t0 = now()
    const res = await settle(from, baseline)
    const bodyText = await page.locator('body').innerText()
    const activeText = await page.locator('.tab-content.active').first().innerText().catch(() => '')
    const rec = {
      kind: 'page', seg: tab.seg, label: tab.label,
      totalMs: res.totalMs, wallMs: now() - t0, slowestMs: res.slowest,
      reqs: res.reqs, reqCount: res.reqs.length, timedOut: res.timedOut,
      bodyLen: bodyText.length, activeLen: activeText.length, bodyHead: activeText.slice(0, 120), clickErr,
    }
    results.push(rec)
    console.log(`[PAGE] ${tab.label.padEnd(6)} total=${rec.totalMs}ms slowest=${rec.slowestMs}ms reqs=${rec.reqCount} activeLen=${rec.activeLen}${clickErr ? ' ERR=' + clickErr : ''}`)

    // 扫描本页可交互控件
    const controls = await page.evaluate(() => {
      const root = document.querySelector('.tab-content.active')
      if (!root) return []
      const out = []
      root.querySelectorAll('button, select, input[type=checkbox], [role=button]').forEach(el => {
        const r = el.getBoundingClientRect()
        if (r.width < 2 || r.height < 2) return
        const style = getComputedStyle(el)
        if (style.visibility === 'hidden' || style.display === 'none') return
        let text = (el.innerText || el.value || el.getAttribute('aria-label') || el.title || '').trim().replace(/\s+/g, ' ')
        if (!text) text = el.getAttribute('placeholder') || ''
        out.push({ tag: el.tagName.toLowerCase(), text: text.slice(0, 30), disabled: !!el.disabled, type: el.type || '' })
      })
      return out
    })
    rec.controls = controls
    if (SCAN_ONLY) continue

    // 逐个点击白名单按钮（同类文本只点一次），每次点击后测量新请求
    const seen = new Set()
    const _wl = CLICK_WHITELIST[tab.seg] || []
    const _rank = t => { const i = _wl.findIndex(re => re.test(t)); return i < 0 ? 99 : i }
    const clickable = controls
      .filter(c => c.tag === 'button' && !c.disabled && c.text && isAllowed(tab.seg, c.text))
      .sort((a, b) => _rank(a.text) - _rank(b.text))
    for (const c of clickable) {
      const key = c.text.replace(/[\d.]+/g, '#')
      if (seen.has(key)) continue
      seen.add(key)
      // 按钮文本可能被重新渲染改写（价格、数字），用稳定前缀子串匹配。
      // hasText 传**字符串**（子串语义）而不是 RegExp：价格里带 `~`、括号、反斜杠时，
      // 先转义再截断 8 字符会把转义符切成半个，生成非法正则（11-04 实测崩过一次）。
      // force 跳过 overlay 遮挡判断，仍触发原生 click。
      const scope = '.tab-content.active button'
      let loc = page.locator(scope).filter({ hasText: c.text }).first()
      if (!(await loc.count())) loc = page.locator(scope).filter({ hasText: c.text.slice(0, 6) }).first()
      if (!(await loc.count())) {
        console.log(`  [SKIP] ${c.text.slice(0, 14)}（点击时按钮已不在 DOM，多为上一次点击触发重渲染）`)
        continue
      }
      const f2 = log.length
      const b2 = new Set(log.map(r => r.url))
      const t2 = now()
      let err = ''
      try { await loc.click({ timeout: 3000, force: true }) } catch (e) { err = e.message.slice(0, 80) }
      const s2 = await settle(f2, b2, 20000, 450, 700)
      const writes = s2.reqs.filter(r => !/^(GET|HEAD)$/.test(r.method))
      const r2 = {
        kind: 'button', page: tab.label, text: c.text,
        totalMs: s2.totalMs, wallMs: now() - t2, slowestMs: s2.slowest,
        reqs: s2.reqs, reqCount: s2.reqs.length, timedOut: s2.timedOut,
        writes: writes.map(r => r.method + ' ' + r.url.slice(0, 80)), err,
      }
      results.push(r2)
      console.log(`  [BTN] ${c.text.slice(0, 14).padEnd(14)} ${r2.totalMs}ms slowest=${r2.slowestMs}ms reqs=${r2.reqCount}${err ? ' ERR=' + err : ''}${writes.length ? ' WRITE=' + writes.length : ''}`)
      // 弹窗类：关掉，避免残留 overlay 挡住后续点击
      if (MODAL_BUTTONS.test(c.text)) {
        await page.keyboard.press('Escape').catch(() => {})
        const close = page.locator('[class*="modal"] button').filter({ hasText: /^(×|✕|关闭|× 关闭)$/ }).first()
        if (await close.count()) await close.click({ timeout: 1200, force: true }).catch(() => {})
        // 兜底：若弹窗仍在，点它自己的关闭按钮（不直接删 DOM，避免破坏 Vue 状态）
        const anyClose = page.locator('[class*="close"]').first()
        if (await anyClose.count()) await anyClose.click({ timeout: 1000, force: true }).catch(() => {})
        await page.waitForTimeout(150)
      }
    }

    // 下拉切换：股票筛选的「过滤模式」等纯前端筛选，逐项切一遍
    const selects = page.locator('.tab-content.active select')
    const selCount = await selects.count()
    for (let si = 0; si < Math.min(selCount, 3); si++) {
      const sel = selects.nth(si)
      const opts = await sel.locator('option').evaluateAll(os => os.map(o => ({ v: o.value, t: o.textContent.trim() })))
      for (const o of opts.slice(0, 4)) {
        const f3 = log.length, b3 = new Set(log.map(r => r.url)), t3 = now()
        await sel.selectOption(o.v).catch(() => {})
        const s3 = await settle(f3, b3, 12000, 350, 500)
        const r3 = { kind: 'select', page: tab.label, text: o.t.slice(0, 20), totalMs: s3.totalMs,
                     slowestMs: s3.slowest, reqs: s3.reqs, reqCount: s3.reqs.length }
        results.push(r3)
      }
    }
  }

  fs.writeFileSync(OUT, JSON.stringify(results, null, 2), 'utf8')
  console.log('\n=== 汇总（按 totalMs 降序，前 30） ===')
  results.filter(r => r.totalMs > 0).sort((a, b) => b.totalMs - a.totalMs).slice(0, 30)
    .forEach(r => console.log(`${String(r.totalMs).padStart(7)}ms | slowest=${String(r.slowestMs).padStart(6)}ms | reqs=${String(r.reqCount || 0).padStart(2)} | ${r.kind === 'page' ? '页面' : r.kind === 'button' ? '按钮' : '启动'} ${r.page || ''} ${r.label || ''} ${r.text || ''}`))
  console.log(`\n报告: ${OUT}`)
  await browser.close()
}

main().catch(e => { console.error('SWEEP_FAILED', e); process.exit(1) })
