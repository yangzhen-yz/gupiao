/**
 * apiFetch 5xx 重试 / 4xx 不重试 / 非幂等不重试 的桩测（纯 Node + vm，零网络、零浏览器）。
 *
 * 为什么用 vm 抽源码：helpers.js 是 ESM 且 import vue，直接 require 不了。
 * 这里剥掉 import / export 关键字、注入最小 stub，跑的就是线上同一份函数体
 * （不是照抄一份逻辑），helpers.js 改了口径这里会立刻红。
 *
 * 用法：node scripts/api_fetch_selftest.cjs
 */
const fs = require('fs')
const path = require('path')
const vm = require('vm')

const SRC = path.join(__dirname, '..', 'client-vue', 'src', 'utils', 'helpers.js')
let code = fs.readFileSync(SRC, 'utf8')
code = code
  .replace(/^import .*$/gm, '')
  .replace(/^export (async function|function|const|class)/gm, '$1')

const PASS = []
const FAIL = []
function check(name, cond, extra = '') {
  ;(cond ? PASS : FAIL).push(name)
  console.log(`${cond ? 'OK  ' : 'FAIL'} ${name}${extra ? ' | ' + extra : ''}`)
}

function makeEnv(fetchImpl) {
  const calls = []
  const probeBegins = []
  const probeEnds = []
  const notices = []
  const store = {}
  const ctx = {
    console: { warn() {}, error() {}, log() {} },
    setTimeout, clearTimeout, Promise, Date, Math, JSON, Number, String, Array, Object, Error,
    performance: { now: () => Date.now() },
    AbortController,
    localStorage: {
      getItem: k => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v) },
      removeItem: k => { delete store[k] },
    },
    fetch: async (url, opts) => {
      calls.push({ url, method: (opts && opts.method) || 'GET' })
      return fetchImpl(calls.length, url, opts)
    },
    ref: v => ({ value: v }),
    beginRequest: url => { const t = probeBegins.length + 1; probeBegins.push({ t, url }); return t },
    endRequest: (tok, info) => { probeEnds.push({ tok, info }) },
    __notices: notices,
  }
  ctx.globalThis = ctx
  vm.createContext(ctx)
  vm.runInContext(
    code + '\n;globalThis.__api = { apiFetch, apiFetchText, setUnauthorizedHandler, useNotification };',
    ctx,
  )
  // 覆盖 vm 全局上的 useNotification（函数声明在 runInContext 里会挂成 global 属性，
  // 所以必须在执行完源码之后再覆盖；改 __api 上那个引用是没用的，模块内调的是全局绑定）
  ctx.useNotification = () => ({
    notifications: { value: [] },
    showNotification: (msg, type) => notices.push({ msg, type }),
  })
  return { ctx, calls, probeBegins, probeEnds, notices }
}

const ok = body => ({ status: 200, headers: { get: () => null }, json: async () => body, text: async () => JSON.stringify(body) })
const bad = status => ({ status, headers: { get: () => null }, json: async () => ({ detail: 'boom' }), text: async () => '{"detail":"boom"}' })

async function run() {
  // ① 5xx 幂等 GET：第 3 次成功，共 3 次请求
  {
    const e = makeEnv(n => (n < 3 ? bad(500) : ok({ success: true, data: 'OK' })))
    const r = await e.ctx.__api.apiFetch('/api/x')
    check('① 5xx 的 GET 重试 2 次后成功', e.calls.length === 3 && r.data === 'OK', `calls=${e.calls.length}`)
    check('① 每次尝试都单独记探针', e.probeBegins.length === 3 && e.probeEnds.length === 3,
      `begin=${e.probeBegins.length} end=${e.probeEnds.length}`)
  }
  // ② 5xx 耗尽：仍按旧形状 resolve（不破坏只判 result.success 的老调用方）+ 只提示一次
  {
    const e = makeEnv(() => bad(503))
    const r = await e.ctx.__api.apiFetch('/api/x')
    check('② 5xx 耗尽仍 resolve 旧形状', e.calls.length === 3 && r && r.detail === 'boom',
      `calls=${e.calls.length} body=${JSON.stringify(r)}`)
    check('② 耗尽后弹一次服务异常提示', e.notices.length === 1 && /服务暂时不可用/.test(e.notices[0].msg),
      `notices=${e.notices.length}`)
  }
  // ③ 非幂等方法绝不重试（重复下单是红线）
  for (const m of ['POST', 'PUT', 'DELETE', 'PATCH']) {
    const e = makeEnv(() => bad(500))
    await e.ctx.__api.apiFetch('/api/x', { method: m, body: '{}' })
    check(`③ ${m} 不重试`, e.calls.length === 1, `calls=${e.calls.length}`)
  }
  // ④ 4xx 不重试
  {
    const e = makeEnv(() => bad(400))
    await e.ctx.__api.apiFetch('/api/x')
    check('④ 4xx 不重试', e.calls.length === 1, `calls=${e.calls.length}`)
  }
  // ⑤ 401 清登录态、不重试、抛带 status 的错
  {
    const e = makeEnv(() => bad(401))
    let handler = 0
    e.ctx.__api.setUnauthorizedHandler(() => { handler++ })
    let thrown = null
    try { await e.ctx.__api.apiFetch('/api/x') } catch (err) { thrown = err }
    check('⑤ 401 不重试且抛 status=401', e.calls.length === 1 && thrown && thrown.status === 401,
      `calls=${e.calls.length}`)
    check('⑤ 401 触发会话失效回调', handler === 1, `handler=${handler}`)
  }
  // ⑥ 网络层 reject → 抛异常 + 离线提示（30s 节流，连发两次只提示一次）
  {
    const e = makeEnv(() => { throw new TypeError('Failed to fetch') })
    let thrown = null
    try { await e.ctx.__api.apiFetch('/api/x') } catch (err) { thrown = err }
    let thrown2 = null
    try { await e.ctx.__api.apiFetch('/api/y') } catch (err) { thrown2 = err }
    check('⑥ fetch 失败抛异常', thrown instanceof Error && thrown2 instanceof Error)
    check('⑥ 离线提示 30s 内只弹一次',
      e.notices.length === 1 && /网络/.test(e.notices[0].msg), `notices=${e.notices.length}`)
  }
  // ⑦ expectText / withMeta 形状不变
  {
    const e = makeEnv(() => ok('v_sh600519="1~贵州茅台~";'))
    const txt = await e.ctx.__api.apiFetchText('/qt.gtimg.cn')
    check('⑦ apiFetchText 仍返回纯文本', typeof txt === 'string' && txt.includes('贵州茅台'))
  }
  {
    const e = makeEnv(() => ok({ success: true, data: [1, 2] }))
    const r = await e.ctx.__api.apiFetch('/api/x', { withMeta: true })
    check('⑦ withMeta 形状不变', Array.isArray(r.data.data) && 'meta' in r,
      JSON.stringify(r).slice(0, 80))
  }
  // ⑧ 200 一次直达、无提示
  {
    const e = makeEnv(() => ok({ success: true }))
    await e.ctx.__api.apiFetch('/api/x')
    check('⑧ 200 一次直达且无提示', e.calls.length === 1 && e.notices.length === 0,
      `calls=${e.calls.length} notices=${e.notices.length}`)
  }

  console.log(`\n=== ${PASS.length} passed / ${FAIL.length} failed ===`)
  if (FAIL.length) {
    console.log('FAILED:', FAIL.join(', '))
    process.exit(1)
  }
}

run().catch(e => { console.error(e); process.exit(1) })
