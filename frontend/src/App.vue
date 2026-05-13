<template>
  <a-layout class="app-layout">
    <a-layout-header class="app-header">
      <div class="brand">旅行规划工作台</div>
      <div class="brand-subtitle">多Agent协作 · 高德地图MCP · 智能行程规划</div>
    </a-layout-header>
    <a-layout-content class="app-content">
      <router-view />
    </a-layout-content>
    <a-layout-footer class="app-footer">
      旅行规划工作台 ©2026 基于 FastAPI + LangChain + Vue3
    </a-layout-footer>
  </a-layout>
</template>

<style>
#app {
  min-height: 100vh;
  font-family:
    -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial,
    "Noto Sans", "Microsoft YaHei", sans-serif;
}

.app-layout {
  min-height: 100vh;
}

.app-header {
  height: 72px;
  padding: 0 48px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #001529;
}

.brand {
  color: #fff;
  font-size: 24px;
  font-weight: 800;
}

.brand-subtitle {
  color: rgba(255, 255, 255, 0.74);
  font-size: 14px;
}

.app-content {
  padding: 24px;
  background: #f0f2f5;
}

.app-footer {
  text-align: center;
  color: #667085;
}

@media (max-width: 768px) {
  .app-header {
    height: auto;
    min-height: 72px;
    padding: 16px 20px;
    flex-direction: column;
    align-items: flex-start;
    justify-content: center;
    gap: 4px;
  }

  .brand-subtitle {
    font-size: 12px;
  }

  .app-content {
    padding: 12px;
  }
}
</style>
