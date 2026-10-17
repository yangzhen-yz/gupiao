/* ESLint 配置 - Vue3 + Vite 项目（2026-10-17 新增）
 * 规则取向：vue3-recommended + eslint:recommended，风格冲突规则交给 Prettier。
 * 用法：npm run lint / npm run lint:fix
 */
module.exports = {
  root: true,
  env: {
    browser: true,
    es2022: true,
    node: true,
  },
  extends: [
    'eslint:recommended',
    'plugin:vue/vue3-recommended',
    // 必须放最后：关掉所有与 Prettier 冲突的格式规则
    '@vue/eslint-config-prettier',
  ],
  parserOptions: {
    ecmaVersion: 'latest',
    sourceType: 'module',
  },
  rules: {
    // 项目存在 LoginPage / KlineModal 等命名，按 vue3 规范其实合法；
    // 但 AiDiagModal / DataFreshness 之外的单词名组件历史上存在，先关掉避免大面积报错
    'vue/multi-word-component-names': 'off',
    // 前端零 console.log 是现状，但开发期调试保留空间，设 warn 不设 error；
    // catch 块里的 console.error/warn 是有意的错误上报，放行
    'no-console': ['warn', { allow: ['warn', 'error'] }],
    'no-unused-vars': 'warn',
    // Vue3 script setup 下 props/emits 多用 defineXxx，旧规则易误报
    'vue/no-unused-properties': 'warn',
    'vue/require-default-prop': 'off',
    'vue/require-explicit-emits': 'off',
  },
  ignorePatterns: ['dist/', 'node_modules/', 'server/', '*.config.js', '.eslintrc.cjs', 'scan_css.cjs', 'test_*.cjs'],
}
