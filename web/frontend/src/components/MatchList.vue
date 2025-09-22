<template>
  <section class="match-list">
    <section class="card filters">
      <div class="section-title">即将开赛</div>
      <div class="filter-grid">
        <label>
          联赛
          <input
            v-model="store.filter.league"
            placeholder="输入联赛名称"
            @keyup.enter="refresh"
          />
        </label>
        <label>
          球队
          <input
            v-model="store.filter.team"
            placeholder="球队名关键词"
            @keyup.enter="refresh"
          />
        </label>
        <label class="checkbox">
          <input type="checkbox" v-model="store.filter.upcoming" @change="refresh" />
          仅显示未开始的比赛
        </label>
        <button class="refresh" @click="refresh">刷新列表</button>
      </div>
    </section>

    <section class="empty" v-if="!store.loading && !matches.length">
      <div class="card">
        <h2>暂无比赛数据</h2>
        <p>等待后台同步完成或请稍后重试。</p>
      </div>
    </section>

    <section v-if="store.error" class="card error">
      <p>{{ store.error }}</p>
    </section>

    <section v-if="store.loading" class="card loading">
      <div class="spinner"></div>
      <p>加载中...</p>
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
          <div v-if="match.latest_insight" class="insight">
            <h4>{{ match.latest_insight.headline || 'AI 前瞻' }}</h4>
            <p>{{ match.latest_insight.key_points || '查看详情获取完整分析。' }}</p>
            <footer class="insight-meta">
              <span>胜负倾向：{{ verdictLabel(match.latest_insight.verdict) }}</span>
              <span v-if="match.latest_insight.confidence">
                信心指数：{{ match.latest_insight.confidence }}%
              </span>
            </footer>
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

function verdictLabel(code) {
  if (!code) return '待定'
  const map = {
    home: '倾向主胜',
    draw: '倾向平局',
    away: '倾向客胜'
  }
  return map[code] || code
}
</script>

<style scoped>
.filters input {
  width: 100%;
  padding: 10px 12px;
  border-radius: 10px;
  border: 1px solid rgba(148, 163, 184, 0.3);
  background: rgba(15, 23, 42, 0.6);
  color: #e2e8f0;
}

.filter-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 16px;
  align-items: end;
}

.checkbox {
  display: flex;
  align-items: center;
  gap: 10px;
}

.refresh {
  padding: 10px 16px;
  border-radius: 12px;
  background: linear-gradient(90deg, #38bdf8, #14b8a6);
  border: none;
  color: #0f172a;
  font-weight: 600;
  cursor: pointer;
  transition: transform 0.2s ease;
}

.refresh:hover {
  transform: translateY(-1px);
}

.match-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 20px;
}

.match-card {
  cursor: pointer;
  transition: transform 0.2s ease, box-shadow 0.2s ease;
  position: relative;
  overflow: hidden;
}

.match-card::after {
  content: '';
  position: absolute;
  inset: 0;
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

.insight {
  margin-top: 20px;
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
  margin: 0 0 12px;
  color: #cbd5f5;
  font-size: 14px;
  line-height: 1.5;
}

.insight-meta {
  display: flex;
  gap: 18px;
  font-size: 13px;
  color: #94a3b8;
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

