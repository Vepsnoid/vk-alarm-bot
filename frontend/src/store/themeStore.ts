import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import api from '../api/client'
import { useAuthStore } from './authStore'

export type ThemeMode = 'light' | 'dark' | 'system'

interface ThemeState { theme: ThemeMode; setTheme: (theme: ThemeMode, syncBackend?: boolean) => void }

export const applyTheme = (mode: ThemeMode) => {
  if (typeof document === 'undefined') return
  const root = document.documentElement
  let isDark = false
  if (mode === 'dark') isDark = true
  else if (mode === 'light') isDark = false
  else isDark = window.matchMedia('(prefers-color-scheme: dark)').matches
  if (isDark) { root.classList.add('dark'); if (document.body) document.body.classList.add('dark') }
  else { root.classList.remove('dark'); if (document.body) document.body.classList.remove('dark') }
}

export const useThemeStore = create<ThemeState>()(
  persist(
    (set) => ({
      theme: 'system',
      setTheme: (theme, syncBackend = true) => {
        applyTheme(theme)
        set({ theme })
        if (syncBackend && useAuthStore.getState().token) api.put('/auth/theme', { theme }).catch(() => {})
      },
    }),
    { name: 'ui-theme-storage', onRehydrateStorage: () => (state) => { if (state) applyTheme(state.theme) } }
  )
)

useThemeStore.subscribe((state) => applyTheme(state.theme))

if (typeof window !== 'undefined') {
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (useThemeStore.getState().theme === 'system') applyTheme('system')
  })
}