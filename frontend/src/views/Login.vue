<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-header">
        <div class="login-brand-badge">
          <span class="login-brand-icon">T</span>
        </div>
        <h1>TravelPlanner</h1>
        <p>登录后同步您的会话历史与行程</p>
      </div>

      <a-tabs v-model:activeKey="activeTab" centered>
        <a-tab-pane key="login" tab="登录">
          <a-form :model="loginForm" @finish="handleLogin" layout="vertical">
            <a-form-item
              label="用户名"
              name="username"
              :rules="[{ required: true, message: '请输入用户名' }]"
            >
              <a-input v-model:value="loginForm.username" placeholder="请输入用户名" size="large" />
            </a-form-item>
            <a-form-item
              label="密码"
              name="password"
              :rules="[{ required: true, message: '请输入密码' }]"
            >
              <a-input-password v-model:value="loginForm.password" placeholder="请输入密码" size="large" />
            </a-form-item>
            <a-form-item>
              <a-button type="primary" html-type="submit" block size="large" :loading="loading" class="login-submit-btn">
                登录
              </a-button>
            </a-form-item>
          </a-form>
        </a-tab-pane>

        <a-tab-pane key="register" tab="注册">
          <a-form :model="registerForm" @finish="handleRegister" layout="vertical">
            <a-form-item
              label="用户名"
              name="username"
              :rules="[
                { required: true, message: '请输入用户名' },
                { min: 3, message: '用户名至少 3 个字符' },
                { pattern: /^[a-zA-Z0-9_\u4e00-\u9fff]+$/, message: '仅支持字母、数字、下划线和中文' },
              ]"
            >
              <a-input v-model:value="registerForm.username" placeholder="3-50 个字符" size="large" />
            </a-form-item>
            <a-form-item
              label="密码"
              name="password"
              :rules="[
                { required: true, message: '请输入密码' },
                { min: 6, message: '密码至少 6 个字符' },
              ]"
            >
              <a-input-password v-model:value="registerForm.password" placeholder="至少 6 个字符" size="large" />
            </a-form-item>
            <a-form-item
              label="确认密码"
              name="confirmPassword"
              :rules="[
                { required: true, message: '请确认密码' },
                { validator: confirmPasswordValidator },
              ]"
            >
              <a-input-password v-model:value="registerForm.confirmPassword" placeholder="再次输入密码" size="large" />
            </a-form-item>
            <a-form-item>
              <a-button type="primary" html-type="submit" block size="large" :loading="loading" class="login-submit-btn">
                注册
              </a-button>
            </a-form-item>
          </a-form>
        </a-tab-pane>
      </a-tabs>

      <div class="login-footer">
        <router-link to="/">← 返回首页</router-link>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { useAuth } from '@/services/auth'

const router = useRouter()
const auth = useAuth()

const activeTab = ref('login')
const loading = ref(false)

const loginForm = reactive({ username: '', password: '' })
const registerForm = reactive({ username: '', password: '', confirmPassword: '' })

function confirmPasswordValidator(_rule: unknown, value: string) {
  if (value && value !== registerForm.password) {
    return Promise.reject('两次输入的密码不一致')
  }
  return Promise.resolve()
}

function authErrorMessage(error: any, fallback: string): string {
  const responseMessage = error?.response?.data?.message
  if (typeof responseMessage === 'string' && responseMessage.trim()) return responseMessage
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  return error?.message || fallback
}

async function handleLogin() {
  loading.value = true
  try {
    await auth.login(loginForm.username, loginForm.password)
    message.success('登录成功')
    if (auth.anonymousMergePending.value) message.warning('匿名历史暂未合并，将在稍后安全重试')
    router.push(typeof router.currentRoute.value.query.redirect === 'string' ? router.currentRoute.value.query.redirect : '/')
  } catch (error: any) {
    message.error(authErrorMessage(error, '登录失败'))
  } finally {
    loading.value = false
  }
}

async function handleRegister() {
  loading.value = true
  try {
    await auth.register(registerForm.username, registerForm.password)
    message.success('注册成功，已自动登录')
    if (auth.anonymousMergePending.value) message.warning('匿名历史暂未合并，将在稍后安全重试')
    router.push(typeof router.currentRoute.value.query.redirect === 'string' ? router.currentRoute.value.query.redirect : '/')
  } catch (error: any) {
    message.error(authErrorMessage(error, '注册失败'))
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  min-height: calc(100vh - 64px);
  display: flex;
  align-items: center;
  justify-content: center;
  background:
    radial-gradient(ellipse 60% 50% at 30% 20%, rgba(249, 115, 22, 0.06) 0%, transparent 60%),
    radial-gradient(ellipse 50% 40% at 80% 80%, rgba(6, 182, 212, 0.05) 0%, transparent 60%),
    var(--surface);
  padding: 32px 16px;
}

.login-card {
  width: 100%;
  max-width: 400px;
  background: var(--card);
  border-radius: var(--radius-2xl);
  padding: 40px 36px 28px;
  box-shadow: var(--shadow-xl);
  border: 1px solid var(--border-light);
  animation: scaleIn 0.4s ease-out;
}

.login-header {
  text-align: center;
  margin-bottom: 32px;
}

.login-brand-badge {
  width: 56px;
  height: 56px;
  margin: 0 auto 16px;
  border-radius: var(--radius-xl);
  background: linear-gradient(135deg, var(--accent), #F59E0B);
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 8px 24px rgba(249, 115, 22, 0.3);
}
.login-brand-icon {
  font-size: 24px;
  font-weight: 900;
  color: #fff;
  font-family: var(--font-display);
}

.login-header h1 {
  margin: 0 0 6px;
  font-size: 24px;
  font-weight: 800;
  font-family: var(--font-display);
  color: var(--text-primary);
  letter-spacing: -0.02em;
}
.login-header p {
  margin: 0;
  color: var(--text-muted);
  font-size: 14px;
}

.login-submit-btn {
  height: 48px !important;
  border-radius: var(--radius-lg) !important;
  font-weight: 700 !important;
  font-size: 15px !important;
}

.login-footer {
  text-align: center;
  margin-top: 20px;
  padding-top: 16px;
  border-top: 1px solid var(--border-light);
}
.login-footer a {
  color: var(--text-secondary);
  text-decoration: none;
  font-size: 13px;
  font-weight: 500;
  transition: color var(--transition-fast);
}
.login-footer a:hover {
  color: var(--accent);
}
</style>
