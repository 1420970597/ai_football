<template>
  <div class="app-shell">
    <header class="app-header">
      <div class="container">
        <div class="header-content">
          <div class="branding">
            <span class="brand-mark">??</span>
            <div>
              <h1>AI 足球比赛前瞻中心</h1>
              <p>数据洞察 · 媒体观点 · AI 预测</p>
            </div>
          </div>
          <nav class="nav-links">
            <RouterLink to="/">比赛中心</RouterLink>
            <RouterLink to="/settings">系统设置</RouterLink>
          </nav>
        </div>
      </div>
    </header>

    <main class="container">
      <RouterView />
    </main>

    <footer class="app-footer">
      <div class="container">
        <small>? {{ new Date().getFullYear() }} AI Football Insights</small>
      </div>
    </footer>
  </div>
</template>

<script setup>
import { RouterLink, RouterView } from 'vue-router'
</script>

<style scoped>
.app-shell {
  min-height: 100vh;
  display: grid;
  grid-template-rows: auto 1fr auto;
}

.app-header {
  background: rgba(15, 23, 42, 0.95);
  border-bottom: 1px solid rgba(148, 163, 184, 0.15);
  box-shadow: 0 12px 30px rgba(15, 23, 42, 0.35);
}

.header-content {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 20px 0;
}

.branding {
  display: flex;
  align-items: center;
  gap: 16px;
}

.brand-mark {
  font-size: 34px;
  filter: drop-shadow(0 4px 10px rgba(56, 189, 248, 0.45));
}

h1 {
  margin: 0;
  font-size: 20px;
  color: #f8fafc;
}

p {
  margin: 2px 0 0;
  color: #94a3b8;
  font-size: 13px;
}

.nav-links {
  display: flex;
  gap: 18px;
  font-size: 15px;
  font-weight: 500;
}

.nav-links a {
  color: #e2e8f0;
  transition: color 0.2s ease;
}

.nav-links a:hover,
.nav-links a.router-link-active {
  color: #38bdf8;
}

.app-footer {
  padding: 24px 0;
  color: #94a3b8;
  font-size: 12px;
  border-top: 1px solid rgba(148, 163, 184, 0.1);
  background: rgba(15, 23, 42, 0.8);
}

@media (max-width: 768px) {
  .header-content {
    flex-direction: column;
    align-items: flex-start;
    gap: 20px;
  }
}
</style>
