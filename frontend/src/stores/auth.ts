import { defineStore } from 'pinia'

import { api } from '../api'

let pendingCheck: Promise<boolean> | undefined

export const useAuthStore = defineStore('auth', {
  state: () => ({ checked: false, username: '' }),
  actions: {
    markAuthenticated(username: string) {
      this.checked = true
      this.username = username
    },
    markLoggedOut() {
      this.checked = true
      this.username = ''
      pendingCheck = undefined
    },
    async ensureSession(force = false) {
      if (!force && this.checked) return Boolean(this.username)
      if (!pendingCheck) {
        pendingCheck = api<{ username: string }>('/auth/me')
          .then((result) => {
            this.markAuthenticated(result.username)
            return true
          })
          .catch(() => {
            this.markLoggedOut()
            return false
          })
          .finally(() => { pendingCheck = undefined })
      }
      return pendingCheck
    },
  },
})
