import { defineStore } from 'pinia'

import { fetchMatchDetail, fetchMatches, fetchMatchTimeline } from '@/services/api'

export const useMatchStore = defineStore('match', {
  state: () => ({
    matches: [],
    selectedMatch: null,
    timeline: [],
    loading: false,
    detailLoading: false,
    filter: {
      league: '',
      team: '',
      upcoming: true
    },
    error: null
  }),
  actions: {
    async loadMatches(customFilter = {}) {
      this.loading = true
      this.error = null
      try {
        const params = { ...this.filter, ...customFilter }
        const data = await fetchMatches(params)
        this.matches = data.results ?? data
      } catch (error) {
        this.error = '比赛列表加载失败，请稍后再试'
        console.error(error)
      } finally {
        this.loading = false
      }
    },
    async loadMatchDetail(matchId) {
      this.detailLoading = true
      this.error = null
      try {
        this.selectedMatch = await fetchMatchDetail(matchId)
      } catch (error) {
        this.error = '获取比赛详情失败'
        console.error(error)
      } finally {
        this.detailLoading = false
      }
    },
    async loadTimeline(matchId) {
      try {
        const data = await fetchMatchTimeline(matchId)
        this.timeline = data.insights
      } catch (error) {
        console.error(error)
      }
    }
  }
})
