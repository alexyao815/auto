import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import './styles.css'
import App from './App.vue'
import router from './router'
import { AUTH_EXPIRED_EVENT } from './api'
import { useAuthStore } from './stores/auth'
import { pinia } from './stores'

window.addEventListener(AUTH_EXPIRED_EVENT, () => {
  const auth = useAuthStore(pinia)
  auth.markLoggedOut()
  if (router.currentRoute.value.path !== '/login') {
    const redirect = router.currentRoute.value.fullPath
    void router.replace({ path: '/login', query: { redirect } })
  }
})

createApp(App).use(pinia).use(router).use(ElementPlus).mount('#app')
