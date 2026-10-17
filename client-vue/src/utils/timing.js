// 买入时机图标（2026-10-17 从 TabActionDesk / TabReview / TabRecommend 三处等价实现抽取）
// 时点类型来自 recommend.py 的 _simple_buy_timing / calc_buy_timing：
// auction=次日竞价 early=早盘 tail=尾盘 pullback=回踩低吸 intraday=盘中跟进
const TIMING_ICONS = {
  auction: '🔔',
  early: '⚡',
  tail: '🌙',
  pullback: '📉',
  intraday: '⏱️',
}

export function timingIcon(type) {
  return TIMING_ICONS[type] || '⏱️'
}
