import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import Antd from 'ant-design-vue'
import 'ant-design-vue/dist/reset.css'
import App from './App.vue'
import Home from './views/Home.vue'
import Reports from './views/Reports.vue'
import Result from './views/Result.vue'
import './styles.css'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/', name: 'Home', component: Home },
    { path: '/reports', name: 'Reports', component: Reports },
    { path: '/result', name: 'Result', component: Result },
  ],
})

const app = createApp(App)
app.use(router)
app.use(Antd)
app.mount('#app')
