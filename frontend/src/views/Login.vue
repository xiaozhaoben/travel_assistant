<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-header">
        <span class="login-brand-icon">◈</span>
        <h1>旅行规划工作台</h1>
        <p>登录后同步您的会话历史</p>
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
              <a-button type="primary" html-type="submit" block size="large" :loading="loading">
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
              <a-button type="primary" html-type="submit" block size="large" :loading="loading">
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

async function handleLogin() {
  loading.value = true
  try {
    await auth.login(loginForm.username, loginForm.password)
    message.success('登录成功')
    router.push('/qa')
  } catch (error: any) {
    const detail = error?.response?.data?.detail
    message.error(typeof detail === 'string' ? detail : error.message || '登录失败')
  } finally {
    loading.value = false
  }
}

async function handleRegister() {
  loading.value = true
  try {
    await auth.register(registerForm.username, registerForm.password)
    message.success('注册成功，已自动登录')
    router.push('/qa')
  } catch (error: any) {
    const detail = error?.response?.data?.detail
    message.error(typeof detail === 'string' ? detail : error.message || '注册失败')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  min-height: calc(100vh - 72px);
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--color-cream);
  padding: 32px 16px;
}

.login-card {
  width: 100%;
  max-width: 420px;
  background: #fff;
  border-radius: var(--radius-md, 12px);
  padding: 40px 36px 28px;
  box-shadow: 0 8px 40px rgba(0, 0, 0, 0.08);
  border: 1px solid var(--color-border-light, #eee);
}

.login-header {
  text-align: center;
  margin-bottom: 28px;
}

.login-brand-icon {
  font-size: 36px;
  color: var(--color-terracotta, #c67a5c);
  display: block;
  margin-bottom: 8px;
}

.login-header h1 {
  margin: 0;
  font-size: 22px;
  font-weight: 700;
  color: var(--color-forest-dark, #1a3a34);
}

.login-header p {
  margin: 6px 0 0;
  color: var(--color-text-secondary, #888);
  font-size: 14px;
}

.login-footer {
  text-align: center;
  margin-top: 20px;
  padding-top: 16px;
  border-top: 1px solid var(--color-border-light, #eee);
}

.login-footer a {
  color: var(--color-forest, #2d6a5a);
  text-decoration: none;
  font-size: 14px;
}

.login-footer a:hover {
  text-decoration: underline;
}
</style>
