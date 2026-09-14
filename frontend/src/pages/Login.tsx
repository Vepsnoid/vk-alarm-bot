import { useState } from 'react'
import { Eye, EyeOff } from 'lucide-react'
import api from '../api/client'
import { useAuthStore } from '../store/authStore'

export default function Login() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const setToken = useAuthStore((s) => s.setToken)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true); setError('')
    try {
      const res = await api.post('/auth/login', { username, password })
      setToken(res.data.access_token)
    } catch (err: any) {
      const detail = err?.response?.data?.detail
      setError(detail || 'Неверный логин или пароль. Проверьте ADMIN_USERNAME и ADMIN_PASSWORD в файле .env на сервере.')
    }
    finally { setLoading(false) }
  }

  return (
    <div className='min-h-screen flex items-center justify-center bg-surface-100 dark:bg-black p-4 transition-colors duration-300'>
      <div className='card w-full max-w-md'>
        <div className='text-center mb-8'>
          <img src='/logo.png' alt='VK Alarm Bot' className='h-20 w-20 object-contain mx-auto mb-4' />
          <h1 className='text-2xl font-bold text-surface-900'>VK Alarm Bot</h1>
          <p className='text-surface-500 mt-1'>Вход в панель управления</p>
        </div>
        <form onSubmit={handleSubmit} className='space-y-4'>
          <div>
            <label className='block text-sm font-medium text-surface-700 mb-1.5'>Логин</label>
            <input type='text' value={username} onChange={e => setUsername(e.target.value)} className='input-field' placeholder='admin' autoComplete='username' required />
          </div>
          <div>
            <label className='block text-sm font-medium text-surface-700 mb-1.5'>Пароль</label>
            <div className='relative'>
              <input
                type={showPassword ? 'text' : 'password'}
                value={password}
                onChange={e => setPassword(e.target.value)}
                className='input-field pr-12'
                placeholder='••••••'
                autoComplete='current-password'
                required
              />
              <button
                type='button'
                onClick={() => setShowPassword(v => !v)}
                className='absolute right-3 top-1/2 -translate-y-1/2 text-surface-500 hover:text-surface-700 dark:hover:text-surface-300'
                title={showPassword ? 'Скрыть пароль' : 'Показать пароль'}
                aria-label={showPassword ? 'Скрыть пароль' : 'Показать пароль'}
              >
                {showPassword ? <EyeOff className='w-5 h-5' /> : <Eye className='w-5 h-5' />}
              </button>
            </div>
          </div>
          {error && <div className='p-3 bg-red-50 text-red-600 rounded-2xl text-sm'>{error}</div>}
          <button type='submit' disabled={loading} className='btn-primary w-full flex items-center justify-center gap-2'>{loading ? 'Вход...' : 'Войти'}</button>
        </form>
      </div>
    </div>
  )
}
