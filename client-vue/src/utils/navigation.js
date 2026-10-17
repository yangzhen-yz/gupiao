// 跨 Tab 跳转到实时行情并定位股票（2026-10-17 从 TabActionDesk / TabFlow 两处等价实现抽取）
// App.vue 的 provide('navigateToQuote') 内部同样是 navigate('quote', symbol)，
// 这里统一走 composable，子组件不再需要 inject 这一层。
import { navigate } from '../composables/useHashRoute'

export function goQuote(symbol) {
  if (symbol) navigate('quote', symbol)
}
