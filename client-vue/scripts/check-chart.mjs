/**
 * check-chart.mjs - 回测曲线页的坐标换算断言（node scripts/check-chart.mjs）
 *
 * 这页不能靠"看一眼觉得对"验收：轴反了、点落到画布外、图上期末数与 KPI 数字不一致，
 * 在小图上肉眼都看不出来。这里把纯函数和真实接口数据都过一遍，
 * 不需要登录、不需要浏览器。
 */
/* eslint-disable no-console */
// 这是个命令行验收脚本，打印断言结果就是它的用途，不是调试残留。
import { domainOf, xAt, yAt, polyline, gridValues, rollingAvg, weightedRate } from '../src/utils/chart.js'

let fails = 0
const ok = (cond, msg) => {
  console.log((cond ? '  ✓ ' : '  ✗ ') + msg)
  if (!cond) fails++
}
const BOX = { w: 760, h: 230, pl: 54, pr: 10, pt: 12, pb: 22 }

console.log('[1] 纯函数')
const xs = [0, 1, 2, 3, 4].map(i => xAt(i, 5, BOX))
ok(
  xs.every((v, i) => i === 0 || v > xs[i - 1]),
  `x 随序号单调递增（${xs.map(v => v.toFixed(0)).join(' → ')}）`
)
ok(Math.abs(xs[0] - BOX.pl) < 1e-9 && Math.abs(xs[4] - (BOX.w - BOX.pr)) < 1e-9, '首尾点正好落在绘图区两端')
ok(xAt(0, 1, BOX) === BOX.pl && Number.isFinite(xAt(0, 0, BOX)), '只有 1 个点（或 0 个点）时不出现除零')

const ys = [0, 50, 100].map(v => yAt(v, 0, 100, BOX))
ok(ys[0] > ys[1] && ys[1] > ys[2], `值越大 y 越小（轴没反：${ys.map(v => v.toFixed(0)).join(' / ')}）`)
ok(
  ys.every(v => v <= BOX.h - BOX.pb + 1e-9 && v >= BOX.pt - 1e-9),
  '所有 y 都在画布内'
)

const [lo, hi] = domainOf([[120000, 80000]], 100000)
ok(lo < 80000 && hi > 120000, `数据域包含极值与本金基准线（${Math.round(lo)} ~ ${Math.round(hi)}）`)
ok(domainOf([], 100000)[0] < 100000 && domainOf([], 100000)[1] > 100000, '空序列也不会塌成一条线')
ok(gridValues(0, 100, 3).length === 4, '网格线数量 = 等分数 + 1')

ok(polyline([1, null, 3], 0, 10, BOX).split(' ').length === 2, 'null（停牌/当天没推荐）被跳过而不是当 0 画')
ok(polyline([1, NaN, 3], 0, 10, BOX).split(' ').length === 2, 'NaN 同样不画')

const roll = rollingAvg(
  [
    { v: 100, n: 1 },
    { v: 0, n: 9 },
  ],
  2
)
ok(Math.abs(roll[1] - 10) < 1e-9, `滚动按样本数加权（1条@100% + 9条@0% → ${roll[1]}%，不是 50%）`)
ok(rollingAvg([{ v: null, n: 0 }], 3)[0] === null, '窗口内全空返回 null，不返回会误导人的 0')
const wr = weightedRate([
  { v: 60, n: 10, avg: 1 },
  { v: 80, n: 30, avg: 3 },
])
ok(Math.abs(wr.win - 75) < 1e-9 && Math.abs(wr.avg - 2.5) < 1e-9, '汇总按 n 加权（胜率 75%、均值 2.5%）')

console.log('\n[2] 真实接口数据')
const base = process.env.GUIPAO_BASE || 'http://127.0.0.1:3003'
let data
try {
  const r = await fetch(`${base}/api/recommend-sim?months=3`)
  data = (await r.json()).data
} catch (e) {
  console.log(`  跳过（后端没在跑：${e.message}）`)
  data = null
}
if (data) {
  const defs = data.curves.filter(c => c.default)
  ok(defs.length === 2, `默认展示两条线（实得 ${defs.length}：${defs.map(c => c.key).join(', ')}）`)
  const dates = [...new Set(data.curves.flatMap(c => c.nav.map(p => p.date)))].sort()
  const dom2 = domainOf(
    defs.map(c =>
      dates.map(d => {
        const pts = c.nav.filter(p => p.date <= d)
        return pts.length ? pts[pts.length - 1].nav : data.capital
      })
    ),
    data.capital
  )
  for (const c of defs) {
    const vals = dates.map(d => {
      const pts = c.nav.filter(p => p.date <= d)
      return pts.length ? pts[pts.length - 1].nav : data.capital
    })
    const pl = polyline(vals, dom2[0], dom2[1], BOX)
    const coords = pl.split(' ').map(s => s.split(',').map(Number))
    ok(coords.length === dates.length, `${c.key} 的点数与日期轴一致（${coords.length}/${dates.length}）`)
    ok(
      coords.every(
        ([x, y]) =>
          Number.isFinite(x) &&
          Number.isFinite(y) &&
          x >= BOX.pl - 1 &&
          x <= BOX.w - BOX.pr + 1 &&
          y >= BOX.pt - 1 &&
          y <= BOX.h - BOX.pb + 1
      ),
      `${c.key} 所有点都在画布内`
    )
    const last = coords[coords.length - 1]
    const yExpect = yAt(c.end, dom2[0], dom2[1], BOX)
    ok(
      Math.abs(last[1] - yExpect) < 0.2,
      `${c.key} 曲线末端就是 KPI 上那个期末数（${Math.round(c.end).toLocaleString()} 元）`
    )
  }
  ok(
    data.curves.every(c => c.nav.length === 0 || c.nav.every(p => Number.isFinite(p.nav))),
    'nav 序列里没有 NaN'
  )
  const sealed = data.curves.find(c => c.key === 'tailclose_open').skipReasons || {}
  console.log(
    `  提示：尾盘买入这一档有 ${
      Object.entries(sealed)
        .filter(([k]) => k.includes('封死'))
        .map(([, v]) => v)[0] || 0
    } 条是"买不到"被剔除的`
  )
}

console.log(fails ? `\n失败 ${fails} 项` : '\n全部通过')
process.exit(fails ? 1 : 0)
