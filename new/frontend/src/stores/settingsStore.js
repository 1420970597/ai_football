import { defineStore } from 'pinia'

import { fetchSettings, triggerUpdate, updateSettings } from '@/services/api'

export const useSettingsStore = defineStore('settings', {
  state: () => ({
    config: null,
    loading: false,
    saving: false,
    syncing: false,
    error: null,
    message: null
  }),
  actions: {
    async load() {
      this.loading = true
      this.error = null
      try {
        this.config = await fetchSettings()
      } catch (error) {
        console.error(error)
        this.error = '加载配置失败，请稍后重试'
      } finally {
        this.loading = false
      }
    },
    async save(payload) {
      this.saving = true
      this.error = null
      this.message = null
      try {
        this.config = await updateSettings(payload)
        this.message = '配置保存成功'
      } catch (error) {
        console.error(error)
        this.error = '保存配置失败'
      } finally {
        this.saving = false
      }
    },
    async triggerSync() {
      this.syncing = true
      this.error = null
      this.message = null
      try {
        await triggerUpdate()
        this.message = '已提交更新任务，后台正在刷新数据'
      } catch (error) {
        console.error(error)
        this.error = '无法触发更新，请检查服务状态'
      } finally {
        this.syncing = false
      }
    }
  }
})
