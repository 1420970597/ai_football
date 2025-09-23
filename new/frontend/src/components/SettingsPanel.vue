<template>
  <section class="settings" v-if="store.config || !store.loading">
    <header class="page-header">
      <h2>系统配置</h2>
      <p>调整大模型、数据抓取与更新周期参数，保存后立即生效。</p>
    </header>

    <section v-if="store.error" class="alert error">{{ store.error }}</section>
    <section v-if="store.message" class="alert success">{{ store.message }}</section>

    <form class="form" @submit.prevent="save">
      <fieldset>
        <legend>大模型接口</legend>
        <label>
          接口地址
          <input v-model="form.ai_base_url" placeholder="https://api.openai.com/v1/chat/completions" required />
        </label>
        <label>
          模型名称
          <input v-model="form.ai_model" placeholder="gpt-4o-mini" required />
        </label>
        <label>
          接口密钥
          <input v-model="form.ai_api_key" type="password" placeholder="sk-..." />
          <small>不填写表示沿用已有密钥</small>
        </label>
      </fieldset>

      <fieldset>
        <legend>提示词设置</legend>
        <label>
          文章摘要提示词
          <textarea v-model="form.article_prompt" rows="5" />
        </label>
        <label>
          比赛汇总提示词
          <textarea v-model="form.match_prompt" rows="5" />
        </label>
        <label>
          一致性判断提示词
          <textarea v-model="form.consensus_prompt" rows="5" />
        </label>
      </fieldset>

      <fieldset class="inline">
        <label>
          更新周期（分钟）
          <input v-model.number="form.update_interval_minutes" type="number" min="30" step="10" required />
        </label>
        <label class="checkbox">
          <input v-model="form.sogou_enabled" type="checkbox" />
          启用搜狗文章抓取
        </label>
        <div class="timestamps" v-if="store.config">
          <small>上次赛事同步：{{ formatTime(store.config.last_update_at) || '未同步' }}</small>
          <small>上次文章同步：{{ formatTime(store.config.last_article_sync_at) || '未同步' }}</small>
        </div>
      </fieldset>

      <footer class="actions">
        <button type="submit" :disabled="store.saving">
          {{ store.saving ? '保存中...' : '保存配置' }}
        </button>
        <button type="button" class="secondary" @click="reload" :disabled="store.loading">
          重新加载
        </button>
        <button type="button" class="ghost" @click="trigger" :disabled="store.syncing">
          {{ store.syncing ? '正在触发...' : '手动触发数据刷新' }}
        </button>
      </footer>
    </form>
  </section>

  <section v-else class="loading">
    <div class="spinner" />
    <p>加载配置...</p>
  </section>
</template>

<script setup>
import { onMounted, reactive, watch } from 'vue'
import { storeToRefs } from 'pinia'

import { useSettingsStore } from '@/stores/settingsStore'

const store = useSettingsStore()
const { config } = storeToRefs(store)

const form = reactive({
  ai_base_url: '',
  ai_model: '',
  ai_api_key: '',
  article_prompt: '',
  match_prompt: '',
  consensus_prompt: '',
  update_interval_minutes: 180,
  sogou_enabled: true
})

onMounted(() => {
  store.load()
})

watch(config, (value) => {
  if (!value) return
  Object.assign(form, value)
  form.ai_api_key = ''
})

function save() {
  const payload = { ...form }
  if (!payload.ai_api_key) {
    delete payload.ai_api_key
  }
  store.save(payload)
}

function reload() {
  store.load()
}

function trigger() {
  store.triggerSync()
}

function formatTime(value) {
  if (!value) return ''
  return new Date(value).toLocaleString('zh-CN')
}
</script>

<style scoped>
.settings {
  display: grid;
  gap: 24px;
}

.page-header h2 {
  margin: 0;
  font-size: 24px;
  color: #e2e8f0;
}

.page-header p {
  margin: 6px 0 0;
  color: #94a3b8;
}

.form {
  display: grid;
  gap: 28px;
  background: rgba(15, 23, 42, 0.7);
  padding: 24px;
  border-radius: 16px;
  border: 1px solid rgba(148, 163, 184, 0.15);
  box-shadow: 0 18px 40px rgba(15, 23, 42, 0.3);
}

fieldset {
  display: grid;
  gap: 18px;
  border: none;
  padding: 0;
}

legend {
  font-weight: 600;
  color: #38bdf8;
  margin-bottom: 4px;
}

label {
  display: grid;
  gap: 8px;
  color: #cbd5f5;
  font-size: 14px;
}

input,
textarea {
  width: 100%;
  padding: 12px;
  border-radius: 10px;
  border: 1px solid rgba(148, 163, 184, 0.2);
  background: rgba(15, 23, 42, 0.5);
  color: #e2e8f0;
}

textarea {
  resize: vertical;
}

.checkbox {
  align-items: center;
  grid-template-columns: auto 1fr;
  gap: 12px;
}

.actions {
  display: flex;
  gap: 12px;
}

button {
  padding: 12px 18px;
  border-radius: 12px;
  border: none;
  cursor: pointer;
  font-weight: 600;
  transition: transform 0.2s ease, box-shadow 0.2s ease;
}

button:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

button:not(.secondary):not(.ghost) {
  background: linear-gradient(120deg, #0ea5e9, #0f766e);
  color: #f8fafc;
  box-shadow: 0 10px 24px rgba(14, 165, 233, 0.3);
}

button.secondary {
  background: rgba(148, 163, 184, 0.2);
  color: #e2e8f0;
}

button.ghost {
  background: transparent;
  border: 1px solid rgba(56, 189, 248, 0.35);
  color: #38bdf8;
}

button:hover:not(:disabled) {
  transform: translateY(-2px);
}

.alert {
  padding: 14px;
  border-radius: 10px;
  font-size: 14px;
}

.alert.error {
  background: rgba(239, 68, 68, 0.15);
  border: 1px solid rgba(239, 68, 68, 0.35);
  color: #fecaca;
}

.alert.success {
  background: rgba(34, 197, 94, 0.15);
  border: 1px solid rgba(34, 197, 94, 0.35);
  color: #bbf7d0;
}

.loading {
  text-align: center;
  padding: 60px 0;
  color: #94a3b8;
}

.spinner {
  width: 36px;
  height: 36px;
  margin: 0 auto 12px;
  border-radius: 50%;
  border: 4px solid rgba(56, 189, 248, 0.2);
  border-top-color: #38bdf8;
  animation: spin 0.9s linear infinite;
}

.timestamps {
  display: grid;
  gap: 4px;
  color: #94a3b8;
}

.inline {
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  align-items: end;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
