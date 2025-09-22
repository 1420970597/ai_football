<template>
  <section class="match-detail" v-if="store.selectedMatch">
    <section class="card hero">
      <header class="hero-header">
        <div>
          <span class="badge">{{ match.league }}</span>
          <h2>{{ match.home_team.name }} vs {{ match.away_team.name }}</h2>
          <p class="meta">
            {{ formatDate(match.match_datetime) }} · {{ match.venue || '待定球场' }} · {{ statusLabel(match.status) }}
          </p>
        </div>
        <div class="odds" v-if="match.odds_home">
          <h4>官方赔率</h4>
          <ul>
            <li>主胜 {{ match.odds_home }}</li>
            <li>平局 {{ match.odds_draw }}</li>
            <li>客胜 {{ match.odds_away }}</li>
          </ul>
        </div>
      </header>

      <section class="scoreboard">
        <div class="team">
          <strong>{{ match.home_team.short_name || match.home_team.name }}</strong>
          <small>主队</small>
        </div>
        <div class="score">
          <span>{{ match.home_score ?? '-' }}</span>
          <small>:</small>
          <span>{{ match.away_score ?? '-' }}</span>
        </div>
        <div class="team">
          <strong>{{ match.away_team.short_name || match.away_team.name }}</strong>
          <small>客队</small>
        </div>
      </section>
    </section>

    <section class="grid">
      <article class="card team-card">
        <h3 class="section-title">球队数据雷达</h3>
        <div class="team-stats" v-for="team in [match.home_team, match.away_team]" :key="team.id">
          <header>
            <h4>{{ team.name }}</h4>
            <small>{{ team.league || '联赛待定' }}</small>
          </header>
          <section v-if="team.stats.length" class="stats">
            <div class="stat" v-for="stat in team.stats" :key="stat.season">
              <strong>{{ stat.season }}</strong>
              <span>战绩 {{ stat.wins }}胜 {{ stat.draws }}平 {{ stat.losses }}负</span>
              <span>场均控球 {{ stat.possession_average ?? '-' }}%</span>
              <span>射门 {{ stat.shots_average ?? '-' }} 次</span>
              <span>传球成功率 {{ stat.passing_accuracy ?? '-' }}%</span>
              <span>近期状态 {{ stat.form || '暂无' }}</span>
            </div>
          </section>
          <p v-else class="empty">暂无该队的赛季统计</p>
        </div>
      </article>

      <article class="card insights" v-if="match.ai_insights?.length">
        <h3 class="section-title">AI 综合前瞻</h3>
        <div class="insight-item" v-for="insight in match.ai_insights" :key="insight.id">
          <header>
            <h4>{{ insight.headline || '综合研判' }}</h4>
            <span class="badge">模型：{{ insight.model_name || insight.provider }}</span>
          </header>
          <p>{{ insight.key_points || '暂无摘要' }}</p>
          <ul class="insight-meta">
            <li>胜负倾向：{{ verdictLabel(insight.verdict) }}</li>
            <li v-if="insight.predicted_score">预测比分：{{ insight.predicted_score }}</li>
            <li v-if="insight.total_goals">总进球：{{ insight.total_goals }}</li>
            <li v-if="insight.confidence">信心指数：{{ insight.confidence }}%</li>
          </ul>
          <details v-if="insight.risk_factors">
            <summary>风险提示</summary>
            <p>{{ insight.risk_factors }}</p>
          </details>
        </div>
      </article>

      <article class="card" v-if="match.recommendations?.length">
        <h3 class="section-title">实战建议</h3>
        <div class="insight-item" v-for="item in match.recommendations" :key="item.id">
          <h4>{{ item.title }}</h4>
          <p>{{ item.summary }}</p>
          <ul>
            <li v-for="point in item.recommendation_points" :key="point">{{ point }}</li>
          </ul>
          <footer>
            <span class="badge">信心：{{ item.confidence_level || '中性' }}</span>
          </footer>
        </div>
      </article>

      <article class="card" v-if="match.articles?.length">
        <h3 class="section-title">媒体前瞻</h3>
        <ul class="article-list">
          <li v-for="article in match.articles" :key="article.id">
            <a :href="article.url" target="_blank" rel="noopener">
              <h4>{{ article.title }}</h4>
              <p>{{ article.summary || '暂无摘要' }}</p>
              <small>{{ formatDate(article.published_at) }} · {{ article.source || '未知来源' }}</small>
            </a>
          </li>
        </ul>
      </article>

      <article class="card" v-if="timeline.length">
        <h3 class="section-title">AI 更新轨迹</h3>
        <ol class="timeline">
          <li v-for="item in timeline" :key="item.id">
            <time>{{ formatDate(item.created_at) }}</time>
            <div>
              <strong>{{ item.headline || verdictLabel(item.verdict) }}</strong>
              <p>{{ item.key_points || '暂无详细描述' }}</p>
            </div>
          </li>
        </ol>
      </article>
    </section>
  </section>

  <section v-else-if="store.detailLoading" class="card loading">
    <div class="spinner"></div>
    <p>加载比赛详情...</p>
  </section>

  <section v-else class="card error">
    <p>暂无该比赛信息。</p>
  </section>
</template>

<script setup>
import { computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'

import { useMatchStore } from '@/stores/matchStore'

const store = useMatchStore()
const route = useRoute()

const matchId = computed(() => route.params.id)

onMounted(async () => {
  await Promise.all([
    store.loadMatchDetail(matchId.value),
    store.loadTimeline(matchId.value)
  ])
})

watch(matchId, async (next) => {
  if (!next) return
  await Promise.all([
    store.loadMatchDetail(next),
    store.loadTimeline(next)
  ])
})

const match = computed(() => store.selectedMatch)
const timeline = computed(() => store.timeline)

function formatDate(value) {
  if (!value) return '时间待定'
  return new Date(value).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  })
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

function statusLabel(status) {
  const map = {
    scheduled: '未开赛',
    live: '进行中',
    finished: '已结束',
    postponed: '延期'
  }
  return map[status] || status
}
</script>

<style scoped>
.hero {
  margin-bottom: 24px;
}

.hero-header {
  display: flex;
  justify-content: space-between;
  gap: 24px;
}

.hero-header h2 {
  margin: 12px 0 4px;
  font-size: 28px;
  color: #f1f5f9;
}

.hero-header .meta {
  margin: 0;
  color: #94a3b8;
}

.odds {
  background: rgba(56, 189, 248, 0.12);
  border-radius: 14px;
  padding: 16px;
  border: 1px solid rgba(56, 189, 248, 0.25);
}

.scoreboard {
  margin-top: 24px;
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  align-items: center;
  gap: 20px;
  padding: 20px;
  border-radius: 16px;
  border: 1px solid rgba(148, 163, 184, 0.15);
  background: rgba(15, 23, 42, 0.6);
}

.scoreboard .team {
  text-align: center;
  color: #cbd5f5;
}

.scoreboard .team strong {
  font-size: 20px;
}

.scoreboard .score {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 40px;
  font-weight: 700;
  color: #f8fafc;
}

.grid {
  display: grid;
  gap: 24px;
}

.team-card .team-stats {
  display: grid;
  gap: 18px;
}

.team-card .stats {
  display: grid;
  gap: 14px;
  margin-top: 12px;
}

.stat {
  padding: 12px 14px;
  border-radius: 12px;
  border: 1px solid rgba(148, 163, 184, 0.15);
  background: rgba(15, 23, 42, 0.5);
  display: grid;
  gap: 6px;
  font-size: 14px;
  color: #cbd5f5;
}

.insight-item {
  padding: 16px;
  border-radius: 14px;
  border: 1px solid rgba(56, 189, 248, 0.18);
  background: rgba(14, 116, 144, 0.12);
  margin-bottom: 16px;
}

.insight-item h4 {
  margin: 0 0 8px;
  color: #38bdf8;
}

.insight-item ul {
  list-style: none;
  padding: 0;
  margin: 12px 0 0;
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  color: #94a3b8;
}

.article-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: grid;
  gap: 16px;
}

.article-list a {
  display: block;
  padding: 16px;
  border-radius: 12px;
  background: rgba(15, 23, 42, 0.55);
  border: 1px solid rgba(148, 163, 184, 0.15);
  transition: transform 0.2s ease;
}

.article-list a:hover {
  transform: translateY(-2px);
  border-color: rgba(56, 189, 248, 0.4);
}

.article-list h4 {
  margin: 0 0 6px;
  color: #e2e8f0;
}

.timeline {
  list-style: none;
  padding: 0;
  margin: 0;
  display: grid;
  gap: 18px;
}

.timeline li {
  border-left: 2px solid rgba(56, 189, 248, 0.35);
  padding-left: 16px;
  position: relative;
}

.timeline li::before {
  content: '';
  position: absolute;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #38bdf8;
  left: -6px;
  top: 4px;
}

.timeline time {
  font-size: 12px;
  color: #94a3b8;
}

.timeline strong {
  color: #e2e8f0;
}

.empty {
  margin: 0;
  color: #64748b;
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

@media (max-width: 900px) {
  .hero-header {
    flex-direction: column;
  }

  .scoreboard {
    grid-template-columns: 1fr;
  }
}
</style>

