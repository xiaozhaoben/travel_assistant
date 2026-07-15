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
  message,
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
import { useAuth } from '@/services/auth'
import {
  ADMIN_ROLE_INVALIDATED_EVENT,
  ensureAnonymousToken,
  hasStoredUserPrincipal,
} from '@/services/authSession'

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
    { path: '/knowledge', name: 'Knowledge', component: Knowledge, meta: { requiresAdmin: true } },
    { path: '/reports', name: 'Reports', component: Reports },
    { path: '/result', name: 'Result', component: Result },
  ],
})

const auth = useAuth()

router.beforeEach(async (to) => {
  const hasUserPrincipal = hasStoredUserPrincipal()
  if (to.meta.requiresAdmin) {
    if (!hasUserPrincipal) {
      return { name: 'Login', query: { redirect: to.fullPath } }
    }
    try {
      const currentUser = await auth.refreshCurrentUser()
      if (currentUser?.role === 'admin') return true
      if (!hasStoredUserPrincipal()) {
        return { name: 'Login', query: { redirect: to.fullPath } }
      }
      message.warning('需要管理员权限')
      return { name: 'QA' }
    } catch {
      if (!hasStoredUserPrincipal()) {
        return { name: 'Login', query: { redirect: to.fullPath } }
      }
      message.error('权限校验服务暂不可用，请稍后重试')
      return false
    }
  }
  if (to.meta.guest && hasUserPrincipal) return { name: 'QA' }
  return true
})

function leaveAdminArea(): void {
  auth.invalidateAdminRole()
  if (router.currentRoute.value.meta.requiresAdmin) {
    message.warning('需要管理员权限')
    void router.replace({ name: 'QA' })
  }
  void auth.refreshCurrentUser().catch(() => undefined)
}

window.addEventListener(ADMIN_ROLE_INVALIDATED_EVENT, () => {
  leaveAdminArea()
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

function bootstrapApp() {
  // 首次签发失败时仍渲染应用；第一个受保护请求会通过共享 Promise 重试。
  void ensureAnonymousToken().catch(() => undefined)
  app.mount('#app')
}

bootstrapApp()
