import { ref, computed } from 'vue'

/**
 * 主题（2026-10-13 暗色真正启用）
 *
 * 背景：整套暗色 token 与暗色补丁原先写在 @media (prefers-color-scheme: dark) 里，
 * 而代码里 data-theme="dark" 的选择器没有任何地方会设置该属性 —— 暗色是不可达的死代码。
 * 现在 main.css 已全部改为 [data-theme="dark"] 作用域，主题由本模块唯一决定：
 *   - 用户没选过 → 跟随系统偏好
 *   - 用户选过  → 固定为所选（存 localStorage，下次打开仍是它）
 * 首帧防闪烁由 index.html 里的内联脚本负责（早于 CSS 应用）。
 */
const THEME_KEY = 'gupiao_theme'

function systemPrefersDark() {
  return typeof window !== 'undefined' && !!window.matchMedia
    && window.matchMedia('(prefers-color-scheme: dark)').matches
}

export function currentTheme() {
  const saved = typeof localStorage !== 'undefined' ? localStorage.getItem(THEME_KEY) : null
  return saved === 'dark' || saved === 'light' ? saved : (systemPrefersDark() ? 'dark' : 'light')
}

export function applyTheme(theme) {
  document.documentElement.dataset.theme = theme
  document.documentElement.style.colorScheme = theme
}

export function useTheme() {
  const theme = ref(currentTheme())
  applyTheme(theme.value)

  // 未手动选择时跟随系统切换
  if (typeof window !== 'undefined' && window.matchMedia) {
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = (e) => {
      const saved = localStorage.getItem(THEME_KEY)
      if (saved === 'dark' || saved === 'light') return
      theme.value = e.matches ? 'dark' : 'light'
      applyTheme(theme.value)
    }
    if (mq.addEventListener) mq.addEventListener('change', onChange)
    else if (mq.addListener) mq.addListener(onChange)   // 兼容旧版 Edge/Chrome
  }

  function toggleTheme() {
    theme.value = theme.value === 'dark' ? 'light' : 'dark'
    localStorage.setItem(THEME_KEY, theme.value)
    applyTheme(theme.value)
  }

  return { theme, isDark: computed(() => theme.value === 'dark'), toggleTheme, applyTheme }
}
