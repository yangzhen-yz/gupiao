<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-head">
        <div class="login-title">A股量化交易终端</div>
        <div class="login-sub">每个账号的持仓独立保存，互不可见</div>
      </div>

      <!-- 登录 / 注册 切换 -->
      <div class="login-tabs">
        <button :class="['login-tab', { active: mode === 'login' }]" @click="switchMode('login')">登录</button>
        <button
          v-if="allowRegister"
          :class="['login-tab', { active: mode === 'register' }]"
          @click="switchMode('register')"
        >注册</button>
      </div>

      <!-- 提示条：错误 / 首位用户说明 -->
      <div v-if="error" class="login-error">{{ error }}</div>
      <div v-else-if="mode === 'register' && !hasUser && firstUserHint" class="login-hint">
        还没有任何账号。<b>第一个注册的账号</b>将自动认领系统里已有的历史持仓记录。
      </div>

      <form @submit.prevent="submit">
        <label class="field">
          <span class="field-label">用户名</span>
          <input
            v-model.trim="form.username"
            class="field-input"
            type="text"
            placeholder="2-20 位字母/数字/中文"
            autocomplete="username"
            :disabled="busy"
          />
        </label>

        <label v-if="mode === 'register'" class="field">
          <span class="field-label">显示名称<span class="field-optional">（选填）</span></span>
          <input
            v-model.trim="form.displayName"
            class="field-input"
            type="text"
            placeholder="例如：老王 / 小李"
            :disabled="busy"
          />
        </label>

        <label class="field">
          <span class="field-label">密码</span>
          <input
            v-model="form.password"
            class="field-input"
            type="password"
            :placeholder="mode === 'register' ? '至少 6 位' : '请输入密码'"
            :autocomplete="mode === 'register' ? 'new-password' : 'current-password'"
            :disabled="busy"
          />
        </label>

        <button class="login-submit" type="submit" :disabled="busy">
          {{ busy ? '处理中…' : (mode === 'login' ? '登 录' : '注 册 并 登 录') }}
        </button>
      </form>

      <!-- 已有账号快捷登录：省得每次手输用户名 -->
      <div v-if="existingUsers.length" class="login-users">
        <div class="users-label">已有账号</div>
        <div class="users-list">
          <button
            v-for="u in existingUsers"
            :key="u.username"
            class="user-chip"
            :disabled="busy"
            @click="pickUser(u.username)"
          >{{ u.displayName || u.username }}</button>
        </div>
      </div>

      <div class="login-foot">
        行情 / 推荐 / 收评等公共数据无需登录，仅<b>持仓</b>按账号隔离
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { saveAuth, getRecentUsers, saveRecentUser } from '../utils/helpers'

const emit = defineEmits(['success'])

const mode = ref('login')
const busy = ref(false)
const error = ref('')
const allowRegister = ref(true)
const hasUser = ref(true)
const firstUserHint = ref(false)
const existingUsers = ref([])

const form = reactive({ username: '', password: '', displayName: '' })

function switchMode(m) {
  mode.value = m
  error.value = ''
}

function pickUser(username) {
  form.username = username
  form.password = ''
  error.value = ''
}

async function loadStatus() {
  try {
    const r = await fetch('/api/auth/status').then(r => r.json())
    const d = (r && r.data) || {}
    allowRegister.value = d.allowRegister !== false
    hasUser.value = !!d.hasUser
    firstUserHint.value = !d.hasUser
    if (!hasUser.value && allowRegister.value) mode.value = 'register'
  } catch (e) {
    // 状态接口拿不到时保持默认登录态，不阻断登录流程
    console.warn('认证状态获取失败', e)
  }
  // 「已有账号」快捷切换只读本机记住的账号：GET /api/auth/users 现在需要登录，
  // 而且它本来会把所有用户名暴露给任何打开登录页的人。
  existingUsers.value = getRecentUsers()
}

async function submit() {
  error.value = ''
  if (!form.username) { error.value = '请输入用户名'; return }
  if (!form.password) { error.value = '请输入密码'; return }
  busy.value = true
  try {
    const path = mode.value === 'login' ? '/api/auth/login' : '/api/auth/register'
    const payload = mode.value === 'login'
      ? { username: form.username, password: form.password }
      : { username: form.username, password: form.password, displayName: form.displayName }
    const resp = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    const r = await resp.json()
    if (!resp.ok || !r.success) {
      error.value = r.detail || r.message || '操作失败'
      return
    }
    if (!r.data || !r.data.token) {
      // 注册成功但未签发会话（如注册接口已关闭自助注册）→ 回到登录态
      error.value = r.message || '注册成功，请登录'
      mode.value = 'login'
      await loadStatus()
      return
    }
    saveAuth(r.data.token, r.data.user)
    saveRecentUser(r.data.user)
    emit('success', r.data.user)
  } catch (e) {
    error.value = '网络错误，请确认后端服务已启动'
  } finally {
    busy.value = false
  }
}

onMounted(loadStatus)
</script>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg-canvas);
  padding: 24px;
}
.login-card {
  width: 100%;
  max-width: 380px;
  background: var(--bg-secondary);
  border: 1px solid var(--border-subtle);
  border-radius: 12px;
  padding: 28px 26px 20px;
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.08);
}
.login-head { text-align: center; margin-bottom: 18px; }
.login-title { font-size: 1.15rem; font-weight: 700; color: var(--text-primary); letter-spacing: 0.5px; }
.login-sub { font-size: 0.75rem; color: var(--text-secondary); margin-top: 4px; }

.login-tabs { display: flex; gap: 6px; margin-bottom: 14px; background: var(--bg-tertiary); padding: 3px; border-radius: 8px; }
.login-tab {
  flex: 1;
  padding: 7px 0;
  border: none;
  background: transparent;
  border-radius: 6px;
  font-size: 0.85rem;
  color: var(--text-secondary);
  cursor: pointer;
  font-family: inherit;
}
.login-tab.active { background: var(--bg-elevated); color: var(--text-primary); font-weight: 600; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }

.login-error, .login-hint {
  font-size: 0.76rem;
  line-height: 1.5;
  padding: 8px 10px;
  border-radius: 6px;
  margin-bottom: 12px;
}
.login-error { background: var(--accent-danger-bg); border: 1px solid var(--accent-danger-border); color: var(--accent-danger); }
.login-hint { background: var(--accent-info-bg); border: 1px solid var(--accent-info-border); color: var(--accent-info); }

.field { display: block; margin-bottom: 12px; }
.field-label { display: block; font-size: 0.75rem; color: var(--text-secondary); margin-bottom: 5px; }
.field-optional { color: var(--text-tertiary); }
.field-input {
  width: 100%;
  box-sizing: border-box;
  padding: 9px 11px;
  font-size: 0.88rem;
  font-family: inherit;
  color: var(--text-primary);
  background: var(--bg-primary);
  border: 1px solid var(--border-medium);
  border-radius: 7px;
  outline: none;
}
.field-input:focus { border-color: var(--accent-primary); }
.field-input:disabled { opacity: 0.6; }

.login-submit {
  width: 100%;
  margin-top: 4px;
  padding: 10px 0;
  font-size: 0.92rem;
  font-weight: 600;
  letter-spacing: 1px;
  color: var(--text-inverse);
  background: var(--accent-primary);
  border: none;
  border-radius: 7px;
  cursor: pointer;
  font-family: inherit;
}
.login-submit:hover:not(:disabled) { opacity: 0.9; }
.login-submit:disabled { opacity: 0.5; cursor: not-allowed; }

.login-users { margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--border-subtle); }
.users-label { font-size: 0.7rem; color: var(--text-tertiary); margin-bottom: 7px; }
.users-list { display: flex; flex-wrap: wrap; gap: 6px; }
.user-chip {
  padding: 4px 10px;
  font-size: 0.78rem;
  font-family: inherit;
  color: var(--text-primary);
  background: var(--bg-tertiary);
  border: 1px solid var(--border-subtle);
  border-radius: 12px;
  cursor: pointer;
}
.user-chip:hover:not(:disabled) { border-color: var(--accent-primary); }

.login-foot {
  margin-top: 14px;
  text-align: center;
  font-size: 0.68rem;
  color: var(--text-tertiary);
  line-height: 1.5;
}
</style>
