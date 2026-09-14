import { useState, useEffect, useRef } from 'react'
import { NavLink } from 'react-router-dom'
import { LayoutDashboard, GitBranch, FileText, Settings as SettingsIcon, LogOut, Bell, AlertTriangle, XCircle, Info } from 'lucide-react'
import { useAuthStore } from '../store/authStore'
import { formatMoscowDateTime } from '../utils/datetime'
import api from '../api/client'
import { useAutoRefresh } from '../hooks/useAutoRefresh'
import SidebarQuote from './SidebarQuote'


interface NotificationItem { id: number; level: string; title: string; message: string; monitor_id: number | null; is_read: boolean; created_at: string }

export default function Layout({ children }: { children: React.ReactNode }) {
  const logout = useAuthStore((s) => s.logout)
  const [unreadCount, setUnreadCount] = useState(0)
  const [notifications, setNotifications] = useState<NotificationItem[]>([])
  const [showNotifs, setShowNotifs] = useState(false)
  const popoverRef = useRef<HTMLDivElement>(null)

  useEffect(() => { fetchNotifications() }, [])
  useAutoRefresh(() => fetchNotifications(), 15000)

  useEffect(() => {
    const h = (e: MouseEvent) => { if (popoverRef.current && !popoverRef.current.contains(e.target as Node)) setShowNotifs(false) }
    document.addEventListener('mousedown', h); return () => document.removeEventListener('mousedown', h)
  }, [])

  const fetchNotifications = async () => { try { const r = await api.get('/notifications'); setUnreadCount(r.data.unread_count); setNotifications(r.data.notifications) } catch {} }
  const markRead = async (id: number) => { try { await api.post('/notifications/' + id + '/read'); fetchNotifications() } catch {} }
  const markAllRead = async () => { try { await api.post('/notifications/read-all'); fetchNotifications() } catch {} }
  const clearAll = async () => { try { await api.delete('/notifications'); fetchNotifications() } catch {} }

  const navClass = ({ isActive }: { isActive: boolean }) => 'flex items-center gap-3 px-4 py-3 rounded-2xl text-sm transition-all duration-200 ' + (isActive ? 'bg-primary-50 dark:bg-primary-950/40 text-primary-600 dark:text-primary-400 font-semibold' : 'text-slate-500 dark:text-slate-400 hover:bg-black/5 dark:hover:bg-white/5 hover:text-slate-900 dark:hover:text-slate-100 font-medium')

  return (
    <div className='min-h-screen flex bg-surface-100 dark:bg-black text-slate-800 dark:text-slate-100 transition-colors duration-300'>
      <aside className='w-64 bg-white/70 dark:bg-surface-900/70 backdrop-blur-xl border-r border-black/5 dark:border-white/10 fixed h-full transition-colors duration-300 z-20'>
        <div className='p-6'>
          <h1 className='text-xl font-bold text-slate-900 dark:text-slate-100 flex items-center gap-2'>
            <img src='/logo.png' alt='VK Alarm Bot' className='h-9 w-9 object-contain' />
            VK Alarm
          </h1>
        </div>
        <nav className='px-4 space-y-1'>
          <NavLink to='/' end className={navClass}><LayoutDashboard className='w-5 h-5' /> Дашборд</NavLink>
          <NavLink to='/monitors' className={navClass}><GitBranch className='w-5 h-5' /> Потоки</NavLink>
          <NavLink to='/events' className={navClass}><FileText className='w-5 h-5' /> События</NavLink>
          <NavLink to='/settings' className={navClass}><SettingsIcon className='w-5 h-5' /> Настройки</NavLink>
        </nav>
        <SidebarQuote />
        <div className='absolute bottom-0 left-0 right-0 p-4 border-t border-black/5 dark:border-white/10'>
          <button onClick={logout} className='flex items-center gap-3 px-4 py-3 rounded-xl text-sm font-medium text-slate-600 dark:text-slate-400 hover:bg-slate-50 dark:hover:bg-slate-800/60 hover:text-red-600 dark:hover:text-red-400 w-full transition-all'>
            <LogOut className='w-5 h-5' /> Выйти
          </button>
        </div>
      </aside>

      <div className='ml-64 flex-1 min-w-0 min-h-screen overflow-x-hidden'>
        <header className='bg-white/70 dark:bg-surface-900/70 backdrop-blur-xl border-b border-black/5 dark:border-white/10 px-8 py-3.5 flex items-center justify-end sticky top-0 z-10 transition-colors duration-300'>
          <div className='relative'>
            <button onClick={() => setShowNotifs(!showNotifs)} className='relative p-2 hover:bg-slate-100 dark:hover:bg-slate-800 rounded-xl transition-colors'>
              <Bell className='w-5 h-5 text-slate-600 dark:text-slate-400' />
              {unreadCount > 0 && <span className='absolute -top-1 -right-1 bg-red-500 text-white text-[10px] font-bold px-1.5 py-0.5 rounded-full min-w-[18px] text-center'>{unreadCount}</span>}
            </button>
            {showNotifs && (
              <div ref={popoverRef} className='absolute right-0 mt-2 w-96 bg-white dark:bg-slate-800 rounded-2xl shadow-xl border border-slate-200 dark:border-slate-700 z-30'>
                <div className='flex items-center justify-between p-4 border-b border-slate-100 dark:border-slate-700'>
                  <h3 className='text-sm font-semibold text-slate-900 dark:text-slate-100 flex items-center gap-2'><Bell className='w-4 h-4 text-primary-500' /> Оповещения {unreadCount > 0 && <span className='text-xs bg-red-100 dark:bg-red-950/50 text-red-600 dark:text-red-400 px-2 py-0.5 rounded-full'>{unreadCount}</span>}</h3>
                  <div className='flex items-center gap-2'>
                    <button onClick={markAllRead} disabled={unreadCount === 0} className='text-xs text-primary-600 dark:text-primary-400 hover:underline disabled:opacity-40'>Прочитать все</button>
                    <button onClick={clearAll} disabled={notifications.length === 0} className='text-xs text-red-600 dark:text-red-400 hover:underline disabled:opacity-40'>Очистить</button>
                  </div>
                </div>
                <div className='max-h-80 overflow-y-auto divide-y divide-slate-100 dark:divide-slate-800/60'>
                  {notifications.map(n => (
                    <div key={n.id} onClick={() => markRead(n.id)} className={'p-3.5 text-xs cursor-pointer transition-colors ' + (!n.is_read ? 'bg-primary-50/40 dark:bg-primary-950/20' : 'hover:bg-slate-50 dark:hover:bg-slate-800/40')}>
                      <div className='flex items-start gap-2'>
                        {n.level === 'critical' ? <XCircle className='w-4 h-4 text-red-500 shrink-0 mt-0.5' /> : n.level === 'error' ? <AlertTriangle className='w-4 h-4 text-amber-500 shrink-0 mt-0.5' /> : <Info className='w-4 h-4 text-blue-500 shrink-0 mt-0.5' />}
                        <div className='flex-1 min-w-0'>
                          <p className={'font-semibold ' + (!n.is_read ? 'text-slate-900 dark:text-slate-100' : 'text-slate-600 dark:text-slate-400')}>{n.title}</p>
                          <p className='text-slate-500 dark:text-slate-400 mt-0.5 whitespace-pre-wrap'>{n.message}</p>
                          <span className='text-[10px] text-slate-400 dark:text-slate-500 mt-1 block'>{formatMoscowDateTime(n.created_at)}</span>
                        </div>
                      </div>
                    </div>
                  ))}
                  {notifications.length === 0 && <div className='p-8 text-center text-slate-400 dark:text-slate-500 text-xs'>Нет новых оповещений</div>}
                </div>
              </div>
            )}
          </div>
        </header>
        <main className='flex-1 p-8 lg:p-10'>{children}</main>
      </div>
    </div>
  )
}
