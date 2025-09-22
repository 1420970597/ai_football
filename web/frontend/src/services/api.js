import axios from 'axios'

const apiClient = axios.create({
  baseURL: '/api',
  timeout: 12000
})

apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    console.error('API error:', error)
    throw error
  }
)

export const fetchMatches = (params = {}) =>
  apiClient.get('/matches/', { params }).then((res) => res.data)

export const fetchMatchDetail = (matchId) =>
  apiClient.get(`/matches/${matchId}/`).then((res) => res.data)

export const fetchMatchTimeline = (matchId) =>
  apiClient.get(`/matches/${matchId}/timeline/`).then((res) => res.data)

export const fetchTeams = () =>
  apiClient.get('/teams/').then((res) => res.data)

export default apiClient

