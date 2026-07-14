import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import {
  Affix,
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Collapse,
  DatePicker,
  Descriptions,
  Divider,
  Empty,
  Form,
  Input,
  InputNumber,
  Layout,
  List,
  Menu,
  Modal,
  Progress,
  Radio,
  Row,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
} from 'ant-design-vue'
import 'ant-design-vue/dist/reset.css'
import App from './App.vue'
import './styles.css'
import { ensureAnonymousToken, hasStoredUserPrincipal } from '@/services/authSession'

const Home = () => import('./views/Home.vue')
const Login = () => import('./views/Login.vue')
const QA = () => import('./views/QA.vue')
const Knowledge = () => import('./views/Knowledge.vue')
const Reports = () => import('./views/Reports.vue')
const Result = () => import('./views/Result.vue')

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/', name: 'QA', component: QA },
    { path: '/login', name: 'Login', component: Login, meta: { guest: true } },
    { path: '/plan', name: 'Home', component: Home },
    { path: '/knowledge', name: 'Knowledge', component: Knowledge, meta: { requiresUser: true } },
    { path: '/reports', name: 'Reports', component: Reports },
    { path: '/result', name: 'Result', component: Result },
  ],
})

router.beforeEach((to, _from, next) => {
  const hasUserPrincipal = hasStoredUserPrincipal()
  if (to.meta.requiresUser && !hasUserPrincipal) {
    next({ name: 'Login', query: { redirect: to.fullPath } })
  } else if (to.meta.guest && hasUserPrincipal) {
    next({ name: 'QA' })
  } else {
    next()
  }
})

const app = createApp(App)
app.use(router)
;[
  Affix,
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Collapse,
  DatePicker,
  Descriptions,
  Divider,
  Empty,
  Form,
  Input,
  InputNumber,
  Layout,
  List,
  Menu,
  Modal,
  Progress,
  Radio,
  Row,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
].forEach((component) => app.use(component))

async function bootstrapApp() {
  try {
    await ensureAnonymousToken()
  } catch {
    // 首次签发失败时仍渲染应用；第一个受保护请求会通过共享 Promise 重试。
  }
  app.mount('#app')
}

void bootstrapApp()
