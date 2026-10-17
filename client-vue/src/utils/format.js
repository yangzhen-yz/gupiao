// 数值格式化（2026-10-17 从 TabSettings / TabSimTrade 各一个同名 fmt 抽取并改名区分——
// 原来两个 fmt 同名不同义：一个四舍五入、一个千分位，容易看混）

// 四舍五入到 2 位小数；非数字原样返回（配置项阈值展示用）
export function round2(v) {
  return typeof v === 'number' ? Math.round(v * 100) / 100 : v
}

// 千分位格式化，最多 2 位小数；null/undefined 显示 '-'（金额、数量展示用）
export function fmtNum(n) {
  if (n == null) return '-'
  return Number(n).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}
