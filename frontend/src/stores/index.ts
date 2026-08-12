import { createPinia } from 'pinia'

// Router 守卫与 Vue 应用必须共享同一个 Pinia 实例，否则会重复恢复 Session。
export const pinia = createPinia()
