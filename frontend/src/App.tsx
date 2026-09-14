import { useEffect } from 'react'
import { Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import Monitors from './pages/Monitors'
import MonitorEdit from './pages/MonitorEdit'
import Events from './pages/Events'
import Settings from './pages/Settings'
import Login from './pages/Login'
import { useAuthStore } from './store/authStore'
import { useThemeStore } from './store/themeStore'
import api from './api/client'

export default function App() {
  const token = useAuthStore((s) => s.token)
  const setTheme = useThemeStore((s) => s.setTheme)

  useEffect(() => {
    if (token) {
      api.get('/auth/me').then((res) => { if (res.data?.theme_preference) setTheme(res.data.theme_preference, false) }).catch(() => {})
    }
  }, [token, setTheme])

  if (!token) return <Login />

  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/monitors" element={<Monitors />} />
        <Route path="/monitors/new" element={<MonitorEdit />} />
        <Route path="/monitors/:id" element={<MonitorEdit />} />
        <Route path="/events" element={<Events />} />
        <Route path="/settings" element={<Settings />} />
      </Routes>
    </Layout>
  )
}