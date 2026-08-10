import { createRouter, createWebHistory } from 'vue-router'
import DashboardView from './views/DashboardView.vue'
import TaskCreateView from './views/TaskCreateView.vue'
import TaskDetailView from './views/TaskDetailView.vue'
import TaskListView from './views/TaskListView.vue'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: DashboardView },
    { path: '/tasks/new', component: TaskCreateView },
    { path: '/tasks', component: TaskListView },
    { path: '/tasks/:id', component: TaskDetailView },
    { path: '/reports', component: TaskListView },
    { path: '/system', component: DashboardView },
  ],
})
