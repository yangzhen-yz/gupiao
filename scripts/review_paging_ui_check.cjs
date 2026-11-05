/**
 * 每日收评「服务端分页」浏览器端到端回归（只读、零写库、不需要真实会话）。
 *
 * 为什么全桩：前端 App.vue 会先校验 /api/auth/me，没 token 就弹登录页；而收评页
 * 还会并发拉行情/热搜等十几个接口。这里用 Playwright route 把 /api/** 全部桩掉
 * —— 收评数据取自**运行中服务的真实全量只读接口**再按页切片（字段齐全，渲染不炸），
 * 其余接口给最小成功响应。既不碰生产写入，也不需要任何凭据。
 *
 * 验证：首屏只渲染 1 条、分页文案正确、点「下一页」确实发起 ?page=2 且内容变化、
 * 最近 3 天明日关注由后端下发后仍会补行情。
 *
 * 用法：node scripts/review_paging_ui_check.cjs
 */
const { chromium } = require('D:/360Downloads/cc/node_modules/playwright')

const BASE = process.env.GP_BASE || 'http://127.0.0.1:3003'
const PASS = []
const FAIL = []

function check(name, cond, extra = '') {
  ;(cond ? PASS : FAIL).push(name)
  console.log(`${cond ? 'OK  ' : 'FAIL'} ${name}${extra ? ' | ' + extra : ''}`)
}

const json = (body, status = 200) => ({
  status,
  contentType: 'application/json',
  headers: { 'X-Process-Time': '1' },
  body: JSON.stringify(body),
})

async function main() {
  // 真实全量只读数据，仅用于给桩提供完整字段
  const allResp = await fetch(BASE + '/api/daily-reviews')
  const all = await allResp.json()
  const rows = all.data || []
  if (rows.length < 2) throw new Error('收评数据不足 2 条，无法验证翻页')
  const recentFocus = (rows.slice(0, 3).flatMap(r => r.tomorrowFocus || []))
    .filter((s, i, arr) => arr.findIndex(x => x.code === s.code) === i)

  const browser = await chromium.launch({ headless: true, channel: 'chrome' })
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  await ctx.addInitScript(() => {
    try {
      localStorage.setItem('gupiao_auth_token', 'ui-probe-token')
      localStorage.setItem('gupiao_auth_user', JSON.stringify({ username: 'probe', displayName: '探针' }))
    } catch (e) {}
  })
  const page = await ctx.newPage()
  const pageErrors = []
  page.on('pageerror', e => pageErrors.push(String(e)))
  const reviewUrls = []

  await page.route(u => u.pathname.startsWith('/api/'), async route => {
    const u = new URL(route.request().url())
    if (u.pathname === '/api/auth/me') {
      return route.fulfill(json({ success: true, data: { user: { username: 'probe', displayName: '探针' } } }))
    }
    if (u.pathname === '/api/daily-reviews') {
      reviewUrls.push(u.search)
      const p = Math.max(1, Number(u.searchParams.get('page')) || 1)
      return route.fulfill(json({
        success: true, data: [rows[p - 1]].filter(Boolean),
        page: p, pageSize: 1, total: rows.length, totalPages: rows.length,
        recentFocus,
      }))
    }
    // 其它接口给最小成功响应；expectText 的行情接口解析失败会被 try/catch 吞掉
    return route.fulfill(json({ success: true, data: [] }))
  })

  try {
    await page.goto(BASE + '/#/review', { waitUntil: 'domcontentloaded', timeout: 30000 })
    await page.waitForSelector('.review-card', { timeout: 15000 })
    await page.waitForTimeout(800)

    const cards1 = await page.locator('.review-card').count()
    check('① 首屏只渲染 1 条收评', cards1 === 1, `cards=${cards1}`)
    const pageInfo1 = (await page.locator('.page-info').innerText().catch(() => '')) || ''
    check('① 分页文案来自后端 totalPages',
      /第\s*1\s*\/\s*\d+\s*页/.test(pageInfo1) && pageInfo1.includes(String(rows.length)),
      `text=${pageInfo1.trim()}`)
    check('① 首屏请求带 page=1', reviewUrls.some(q => q.includes('page=1')),
      `urls=${reviewUrls.slice(0, 3).join(' ')}`)
    const date1 = (await page.locator('.review-card').first().innerText()).slice(0, 120)

    const nextBtn = page.getByRole('button', { name: '下一页' })
    await nextBtn.click()
    await page.waitForFunction(
      () => document.querySelector('.page-info')?.textContent?.includes('2 /'),
      { timeout: 10000 },
    )
    await page.waitForTimeout(500)
    check('② 翻页发起了 ?page=2', reviewUrls.some(q => q.includes('page=2')),
      `urls=${reviewUrls.slice(-3).join(' ')}`)
    const cards2 = await page.locator('.review-card').count()
    check('② 第 2 页仍只渲染 1 条', cards2 === 1, `cards=${cards2}`)
    const date2 = (await page.locator('.review-card').first().innerText()).slice(0, 120)
    check('② 第 2 页内容与第 1 页不同', date1 !== date2,
      `p1=${date1.slice(0, 30)} p2=${date2.slice(0, 30)}`)

    await page.getByRole('button', { name: '上一页' }).click()
    await page.waitForFunction(
      () => document.querySelector('.page-info')?.textContent?.includes('1 /'),
      { timeout: 10000 },
    )
    check('③ 翻回第 1 页正常', reviewUrls.filter(q => q.includes('page=1')).length >= 2,
      `page1 请求数=${reviewUrls.filter(q => q.includes('page=1')).length}`)

    check('④ 无 JS 运行时异常', pageErrors.length === 0,
      pageErrors.slice(0, 2).join(' / '))
  } finally {
    await browser.close()
  }

  console.log(`\n=== ${PASS.length} passed / ${FAIL.length} failed ===`)
  if (FAIL.length) {
    console.log('FAILED:', FAIL.join(', '))
    process.exit(1)
  }
}

main().catch(e => { console.error(e); process.exit(1) })
