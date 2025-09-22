import { createRouter, createWebHistory } from 'vue-router'

import MatchList from '@/components/MatchList.vue'
import MatchDetail from '@/components/MatchDetail.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      name: 'match-list',
      component: MatchList
    },
    {
      path: '/match/:id',
      name: 'match-detail',
      component: MatchDetail,
      props: true
    }
  ],
  scrollBehavior() {
    return { top: 0 }
  }
})

export default router

