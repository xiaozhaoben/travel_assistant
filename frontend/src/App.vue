<template>
  <a-layout class="app-layout">
    <header class="app-header">
      <router-link to="/" class="brand-link">
        <div class="brand-icon">T</div>
        <div class="brand-text">
          <span class="brand">TravelPlanner</span>
          <span class="brand-subtitle">AI 多Agent · 智能行程规划</span>
        </div>
      </router-link>
      <nav class="header-nav">
        <router-link to="/" class="nav-link" active-class="nav-link-active" exact>
          <MessageOutlined />
          <span>智能问答</span>
        </router-link>
        <router-link to="/knowledge" class="nav-link" active-class="nav-link-active">
          <DatabaseOutlined />
          <span>知识库</span>
        </router-link>
        <router-link to="/plan" class="nav-link" active-class="nav-link-active">
          <CompassOutlined />
          <span>规划行程</span>
        </router-link>
        <router-link to="/reports" class="nav-link" active-class="nav-link-active">
          <FileTextOutlined />
          <span>历史报表</span>
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
        <span class="footer-brand">TravelPlanner</span>
        <span class="footer-divider">·</span>
        <span class="footer-tech">FastAPI + LangChain + Vue 3</span>
        <span class="footer-divider">·</span>
        <span class="footer-copy">©2026</span>
      </div>
    </footer>
  </a-layout>
</template>

<script setup lang="ts">
import { CompassOutlined, DatabaseOutlined, FileTextOutlined, MessageOutlined } from '@ant-design/icons-vue'
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
/* App-level styles are in styles.css */
</style>
