<template>
  <a-layout class="app-layout">
    <header class="app-header">
      <router-link to="/" class="brand-link">
        <span class="brand-icon">◈</span>
        <div class="brand-text">
          <span class="brand">旅行规划工作台</span>
          <span class="brand-subtitle">多Agent协作 · 高德地图MCP · 智能行程规划</span>
        </div>
      </router-link>
      <nav class="header-nav">
        <router-link to="/" class="nav-link" active-class="nav-link-active">
          <CompassOutlined />
          <span>规划行程</span>
        </router-link>
        <router-link to="/reports" class="nav-link" active-class="nav-link-active">
          <FileTextOutlined />
          <span>历史报表</span>
        </router-link>
        <router-link to="/qa" class="nav-link" active-class="nav-link-active">
          <MessageOutlined />
          <span>智能问答</span>
        </router-link>
      </nav>
      <div class="header-user">
        <template v-if="auth.isAuthenticated.value">
          <span class="user-name">{{ auth.user.value?.username }}</span>
          <a-button size="small" ghost @click="handleLogout">退出</a-button>
        </template>
        <template v-else>
          <router-link to="/login">
            <a-button size="small" type="primary">登录</a-button>
          </router-link>
        </template>
      </div>
    </header>
    <a-layout-content class="app-content">
      <router-view />
    </a-layout-content>
    <footer class="app-footer">
      <div class="footer-inner">
        <span class="footer-brand">◈ 旅行规划工作台</span>
        <span class="footer-divider">|</span>
        <span class="footer-tech">FastAPI + LangChain + Vue3</span>
        <span class="footer-divider">|</span>
        <span class="footer-copy">©2026</span>
      </div>
    </footer>
  </a-layout>
</template>

<script setup lang="ts">
import { CompassOutlined, FileTextOutlined, MessageOutlined } from '@ant-design/icons-vue'
import { useRouter } from 'vue-router'
import { useAuth } from '@/services/auth'

const auth = useAuth()
const router = useRouter()

function handleLogout() {
  auth.logout()
  router.push('/')
}
</script>

<style>
#app {
  min-height: 100vh;
  font-family: var(--font-body);
}

.app-layout {
  min-height: 100vh;
  background: var(--color-cream) !important;
}

.app-header {
  height: 72px;
  padding: 0 48px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--color-forest-dark);
  backdrop-filter: blur(12px);
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
  position: sticky;
  top: 0;
  z-index: 100;
  box-shadow: 0 2px 20px rgba(15, 36, 32, 0.3);
}

.brand-link {
  display: flex;
  align-items: center;
  gap: 14px;
  text-decoration: none;
  color: inherit;
}

.brand-icon {
  font-size: 28px;
  color: var(--color-terracotta);
  line-height: 1;
  filter: drop-shadow(0 2px 8px rgba(198, 122, 92, 0.4));
}

.brand-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.brand {
  color: #fff;
  font-size: 20px;
  font-weight: 800;
  font-family: var(--font-display);
  letter-spacing: -0.01em;
  line-height: 1.2;
}

.brand-subtitle {
  color: rgba(255, 255, 255, 0.5);
  font-size: 11px;
  font-weight: 400;
  letter-spacing: 0.04em;
}

.header-nav {
  display: flex;
  align-items: center;
  gap: 6px;
}

.nav-link {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 18px;
  color: rgba(255, 255, 255, 0.65);
  text-decoration: none;
  border-radius: var(--radius-sm);
  font-size: 14px;
  font-weight: 500;
  transition: all var(--transition-base);
  font-family: var(--font-body);
}

.nav-link:hover {
  color: #fff;
  background: rgba(255, 255, 255, 0.08);
}

.nav-link-active {
  color: #fff;
  background: rgba(198, 122, 92, 0.2);
}

.nav-link-active:hover {
  background: rgba(198, 122, 92, 0.3);
}

.header-user {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-left: 20px;
}

.user-name {
  color: rgba(255, 255, 255, 0.85);
  font-size: 14px;
  font-weight: 500;
}

.app-content {
  padding: 0;
  background: transparent !important;
}

.app-footer {
  padding: 20px 48px;
  background: var(--color-forest-dark) !important;
  border-top: 1px solid rgba(255, 255, 255, 0.06);
}

.footer-inner {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  color: rgba(255, 255, 255, 0.45);
  font-size: 13px;
  font-family: var(--font-body);
}

.footer-brand {
  color: rgba(255, 255, 255, 0.65);
  font-weight: 600;
}

.footer-divider {
  color: rgba(255, 255, 255, 0.15);
}

.footer-tech {
  font-weight: 400;
}

.footer-copy {
  font-weight: 400;
}

@media (max-width: 768px) {
  .app-header {
    height: auto;
    min-height: 64px;
    padding: 12px 20px;
    flex-direction: column;
    align-items: flex-start;
    justify-content: center;
    gap: 10px;
  }

  .brand-subtitle {
    font-size: 10px;
  }

  .header-nav {
    width: 100%;
    gap: 4px;
  }

  .nav-link {
    padding: 6px 12px;
    font-size: 13px;
  }

  .app-content {
    padding: 0;
  }

  .app-footer {
    padding: 16px 20px;
  }

  .footer-inner {
    flex-wrap: wrap;
    justify-content: center;
    font-size: 12px;
    gap: 8px;
  }
}
</style>
