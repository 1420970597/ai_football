<template>
  <section class="match-list">
    <section class="card filters">
      <div class="section-title">筛选条件</div>
      <div class="filter-grid">
        <label>
          联赛
          <input v-model="store.filter.league" placeholder="输入联赛名称" @keyup.enter="refresh" />
        </label>
        <label>
          队伍
          <input v-model="store.filter.team" placeholder="输入球队关键词" @keyup.enter="refresh" />
        </label>
        <label class="checkbox">
          <input type="checkbox" v-model="store.filter.upcoming" @change="refresh" />
          仅看未开始比赛
        </label>
        <button class="refresh" @click="refresh">刷新列表</button>
      </div>
    </section>

    <section class="empty" v-if="!store.loading && !matches.length">
      <div class="card">
        <h2>暂无符合条件的比赛</h2>
        <p>等待后台同步完成后再来看看。</p>
      </div>
    </section>

    <section v-if="store.error" class="card error">
      <p>{{ store.error }}</p>
    </section>

    <section v-if="store.loading" class="card loading">
      <div class="spinner"></div>
      <p>数据加载中...</p>
    </section>

    <section class="match-grid">
      <article
        class="card match-card"
        v-for="match in matches"
        :key="match.id"
        @click="openMatch(match)"
      >
        <header class="match-header">
          <div>
            <span class="badge">{{ match.league }}</span>
            <h3>{{ formatTeams(match) }}</h3>
          </div>
          <time>{{ formatTime(match.match_datetime) }}</time>
        </header>
        <section class="match-body">
          <div class="team-block">
            <div class="team">
              <span>{{ match.home_team.short_name || match.home_team.name }}</span>
              <small>主队</small>
            </div>
            <div class="score">
              <strong>{{ match.home_score ?? '-' }}</strong>
              <span>:</span>
              <strong>{{ match.away_score ?? '-' }}</strong>
            </div>
            <div class="team">
              <span>{{ match.away_team.short_name || match.away_team.name }}</span>
              <small>客队</small>
            </div>
          </div>
          <dl class="meta">
            <div>
              <dt>文章前瞻</dt>
              <dd>{{ match.articles_count ?? 0 }} 篇</dd>
            </div>
            <div>
              <dt>AI 判定</dt>
              <dd>{{ verdictLabel(match.latest_insight?.verdict) }}</dd>
            </div>
            <div>
              <dt>信心</dt>
              <dd>{{ match.latest_insight?.confidence ? match.latest_insight.confidence + '%' : '待分析' }}</dd>
            </div>
          </dl>
          <div v-if="match.latest_insight" class="insight">
            <h4>{{ match.latest_insight.headline || 'AI 综合前瞻' }}</h4>
            <p>{{ match.latest_insight.key_points || '点击查看完整详情与走势分析。' }}</p>
          </div>
        </section>
      </article>
    </section>
  </section>
</template>

<script setup>
import { computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'

import { useMatchStore } from '@/stores/matchStore'

const store = useMatchStore()
const router = useRouter()

onMounted(() => {
  if (!store.matches.length) {
    store.loadMatches()
  }
})

const matches = computed(() => store.matches)

function refresh() {
  store.loadMatches()
}

function openMatch(match) {
  router.push({ name: 'match-detail', params: { id: match.id } })
}

function formatTime(value) {
  return new Date(value).toLocaleString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    month: '2-digit',
    day: '2-digit'
  })
}

function formatTeams(match) {
  return `${match.home_team.name} vs ${match.away_team.name}`
}

function verdictLabel(verdict) {
  const map = {
    home_win: '主胜',
    away_win: '客胜',
    draw: '平局',
    unknown: '待分析',
    undefined: '待分析'
  }
  return map[verdict] || '待分析'
}
</script>

<style scoped>
.match-list {
  display: grid;
  gap: 24px;
}

.card {
  background: rgba(15, 23, 42, 0.7);
  border-radius: 20px;
  border: 1px solid rgba(148, 163, 184, 0.12);
  box-shadow: 0 20px 40px rgba(15, 23, 42, 0.35);
  padding: 22px 24px;
  transition: transform 0.2s ease;
}

.filters {
  display: grid;
  gap: 18px;
}

.section-title {
  font-size: 16px;
  font-weight: 600;
  color: #38bdf8;
}

.filter-grid {
  display: grid;
  gap: 16px;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
}

label {
  display: grid;
  gap: 6px;
  color: #cbd5f5;
  font-size: 14px;
}

input {
  width: 100%;
  padding: 10px 12px;
  border-radius: 12px;
  border: 1px solid rgba(148, 163, 184, 0.2);
  background: rgba(15, 23, 42, 0.5);
  color: #e2e8f0;
}

.checkbox {
  align-items: center;
  grid-template-columns: auto 1fr;
  gap: 8px;
}

.refresh {
  background: linear-gradient(120deg, rgba(56, 189, 248, 0.9), rgba(20, 184, 166, 0.85));
  color: #0f172a;
  font-weight: 600;
  border: none;
  border-radius: 12px;
  padding: 12px;
  cursor: pointer;
  transition: transform 0.2s ease;
}

.refresh:hover {
  transform: translateY(-2px);
}

.match-grid {
  display: grid;
  gap: 20px;
}

.match-card {
  position: relative;
  cursor: pointer;
}

.match-card::after {
  content: '';
  position: absolute;
  inset: 0;
  border-radius: 20px;
  background: linear-gradient(120deg, rgba(56, 189, 248, 0.08), rgba(20, 184, 166, 0.08));
  opacity: 0;
  transition: opacity 0.3s ease;
}

.match-card:hover {
  transform: translateY(-3px);
  box-shadow: 0 24px 40px rgba(15, 23, 42, 0.45);
}

.match-card:hover::after {
  opacity: 1;
}

.match-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 18px;
}

.match-header time {
  color: #94a3b8;
  font-size: 14px;
}

.match-header h3 {
  margin: 12px 0 0;
  font-size: 18px;
  color: #e2e8f0;
}

.badge {
  display: inline-flex;
  align-items: center;
  padding: 4px 10px;
  border-radius: 999px;
  background: rgba(56, 189, 248, 0.12);
  color: #38bdf8;
  font-size: 12px;
}

.team-block {
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  align-items: center;
  gap: 20px;
}

.team {
  display: flex;
  flex-direction: column;
  gap: 6px;
  color: #cbd5f5;
}

.team small {
  color: #64748b;
  font-size: 12px;
}

.score {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 24px;
  font-weight: 700;
  color: #f8fafc;
}

.meta {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 12px;
  margin: 18px 0 0;
  color: #94a3b8;
  font-size: 13px;
}

.meta dt {
  font-weight: 600;
  color: #cbd5f5;
}

.insight {
  margin-top: 16px;
  padding: 16px;
  border-radius: 14px;
  border: 1px solid rgba(56, 189, 248, 0.2);
  background: rgba(14, 116, 144, 0.12);
}

.insight h4 {
  margin: 0 0 10px;
  font-size: 16px;
  color: #38bdf8;
}

.insight p {
  margin: 0;
  color: #cbd5f5;
  font-size: 14px;
  line-height: 1.5;
}

.loading,
.error {
  text-align: center;
}

.spinner {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  border: 4px solid rgba(56, 189, 248, 0.2);
  border-top-color: #38bdf8;
  margin: 0 auto 12px;
  animation: spin 0.9s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

@media (max-width: 768px) {
  .team-block {
    grid-template-columns: 1fr;
    text-align: center;
  }

  .score {
    justify-content: center;
  }
}
</style>

