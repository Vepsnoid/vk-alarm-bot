import { create } from 'zustand'
import { persist } from 'zustand/middleware'

interface AuthState {
  token: string | null
  setToken: (token: string) => void
  logout: () => void
}

export const useAuthStore = create<AuthState>()(
  persist((set, get) => ({
    token: null,
    setToken: (token) => set({ token }),
    logout: () => {
      const token = get().token
      // Clear the local token first: the UI must not wait for the network.
      set({ token: null })
      if (!token) return
      // Revoke it server-side too (bumps ``User.token_version``). Without this the
      // JWT would stay valid for the rest of its 24-hour lifetime even though the
      // user pressed «Выйти». ``fetch`` is used directly to avoid importing the
      // axios client (which imports this store).
      void fetch('/api/auth/logout', { method: 'POST', headers: { Authorization: `Bearer ${token}` } }).catch(() => {})
    },
  }), { name: 'auth-storage' })
)