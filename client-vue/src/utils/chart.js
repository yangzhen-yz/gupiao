/**
 * chart.js - 折线图坐标换算（2026-10-18 从 TabBacktest.vue 抽出来）
 *
 * 为什么单独一个文件：曲线画错了不会报错，只会"看着对"。
 * 轴反了、点落在画布外、滚动均值把 null 当 0 —— 这三种情况肉眼在缩略图上根本分辨不出来，
 * 而这页是要拿来做交易决策的。抽成纯函数之后可以在 Node 里直接断言（见 scripts/check-chart.mjs），
 * 不用登录、不用浏览器。
 *
 * 约定：SVG 的 y 向下增长，所以"值越大 y 越小"，这是这类图最容易写反的一处。
 */

/** 数据域：把所有序列的最小/最大值并进一个带上下留白的区间，并保证基准线 base 在区间内。 */
export function domainOf(series, base = 0, padRatio = 0.08) {
  let lo = base,
    hi = base
  for (const vals of series) {
    for (const v of vals) {
      if (v === null || v === undefined || !Number.isFinite(v)) continue
      if (v < lo) lo = v
      if (v > hi) hi = v
    }
  }
  const span = hi - lo
  const pad = span > 0 ? span * padRatio : Math.max(1, Math.abs(hi) * 0.02)
  return [lo - pad, hi + pad]
}

/** 第 i 个点的横坐标：n 个点均匀铺在 [pl, w-pr] 上；只有一个点时贴左边而不是除以 0。 */
export function xAt(i, n, box) {
  const inner = box.w - box.pl - box.pr
  if (n <= 1) return box.pl
  return box.pl + (i / (n - 1)) * inner
}

/** 值 v 的纵坐标（轴是反的：值越大越靠上，y 越小）。 */
export function yAt(v, lo, hi, box) {
  const t = (v - lo) / Math.max(1e-9, hi - lo)
  return box.h - box.pb - t * (box.h - box.pt - box.pb)
}

/** 把一串值折成 "x,y x,y"。null/非数字直接跳过（停牌、那天没有推荐），而不是当成 0。 */
export function polyline(vals, lo, hi, box) {
  const pts = []
  vals.forEach((v, i) => {
    if (v === null || v === undefined || !Number.isFinite(v)) return
    const x = xAt(i, vals.length, box)
    const y = yAt(v, lo, hi, box)
    if (!Number.isFinite(x) || !Number.isFinite(y)) return
    pts.push(`${x.toFixed(1)},${y.toFixed(1)}`)
  })
  return pts.join(' ')
}

/** 等距网格线（含标签用的数值），n 等分。 */
export function gridValues(lo, hi, n = 3) {
  const out = []
  for (let i = 0; i <= n; i++) out.push(lo + ((hi - lo) * i) / n)
  return out
}

/**
 * 滚动加权平均：pairs=[{v,n}]，v 是当期胜率、n 是当期样本条数。
 * 直接对胜率取平均会让"1 条的那一天"和"5 条的那一天"同权，所以按 n 加权；
 * 窗口内全是 null 时返回 null（画线时跳过），不能返回 0 —— 0% 胜率是个会误导人的读数。
 */
export function rollingAvg(pairs, k = 5) {
  return pairs.map((_p, i) => {
    const win = pairs.slice(Math.max(0, i - k + 1), i + 1)
    let num = 0,
      den = 0
    for (const p of win) {
      if (p.v === null || p.v === undefined) continue
      num += p.v * (p.n || 1)
      den += p.n || 1
    }
    return den ? num / den : null
  })
}

/** 按权重合成整体胜率（同 rollingAvg 的加权口径，用于"整体/前后段"汇总）。 */
export function weightedRate(pairs) {
  let num = 0,
    den = 0,
    sum = 0
  for (const p of pairs) {
    if (p.v === null || p.v === undefined) continue
    num += p.v * (p.n || 1)
    den += p.n || 1
    sum += (p.avg || 0) * (p.n || 1)
  }
  return den ? { win: num / den, avg: sum / den, n: den } : null
}
