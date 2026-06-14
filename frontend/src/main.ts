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
  Tag,
} from 'ant-design-vue'
import 'ant-design-vue/dist/reset.css'
import App from './App.vue'
import './styles.css'

const Home = () => import('./views/Home.vue')
const QA = () => import('./views/QA.vue')
const Reports = () => import('./views/Reports.vue')
const Result = () => import('./views/Result.vue')

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/', name: 'Home', component: Home },
    { path: '/qa', name: 'QA', component: QA },
    { path: '/reports', name: 'Reports', component: Reports },
    { path: '/result', name: 'Result', component: Result },
  ],
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
  Tag,
].forEach((component) => app.use(component))
app.mount('#app')
