import { useEffect, useState, useMemo } from 'react'
import { Activity, GitBranch, FileText, TrendingUp, Eye, RotateCcw } from 'lucide-react'
import { formatMoscowDateTime } from '../utils/datetime'
import api from '../api/client'
import { useAutoRefresh } from '../hooks/useAutoRefresh'

interface MonitorItem { id: number; name: string; is_active: boolean; owner_id?: number | null; posts_processed: number; posts_published: number; ai_successes: number }
interface EventItem { id: number; monitor_id: number; original_url: string; original_text: string | null; status: string; created_at: string; sent_at?: string | null; er: number; ai_filtered: boolean }

export default function Dashboard() {
  const [allMonitors, setAllMonitors] = useState<MonitorItem[]>([])
  const [allEvents, setAllEvents] = useState<EventItem[]>([])
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)

  const fetchData = async (bg = false) => {
    if (!bg) setLoading(true); else setRefreshing(true)
    try {
      const mRes = await api.get('/monitors'); const monitors: MonitorItem[] = mRes.data; setAllMonitors(monitors)
      const eRes = await api.get('/monitors/events?limit=200'); setAllEvents(eRes.data || [])
    } catch (err) { console.error(err) }
    finally { setLoading(false); setRefreshing(false) }
  }

  useEffect(() => { fetchData() }, [])
  useAutoRefresh(() => fetchData(true), 15000)

  const stats = useMemo(() => {
    const totalEvents = allEvents.length
    const sentEvents = allEvents.filter(e => e.status === 'sent').length
    const processedByAI = allMonitors.reduce((s, m) => s + (m.ai_successes || 0), 0)
    const activeMonitors = allMonitors.filter(m => m.is_active).length
    return { totalEvents, sentEvents, processedByAI, activeMonitors, totalMonitors: allMonitors.length }
  }, [allEvents, allMonitors])

  const recentEvents = useMemo(() => allEvents.slice(0, 10), [allEvents])

  if (loading) return <div className='text-center py-20 text-slate-500'>Загрузка...</div>

  return (
    <div>
      <div className='flex items-center justify-between mb-8'>
        <div>
          <h1 className='text-2xl font-bold text-slate-900 dark:text-slate-100'>Дашборд</h1>
          <p className='text-slate-500 dark:text-slate-400 mt-1'>Статистика потоков VK → Max</p>
        </div>
        <button onClick={() => fetchData(true)} disabled={refreshing} className='btn-secondary flex items-center gap-2 text-sm'><RotateCcw className={'w-4 h-4 ' + (refreshing ? 'animate-spin' : '')} /> {refreshing ? 'Обновление...' : 'Обновить'}</button>
      </div>

      <div className='grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-8'>
        <div className='card'><div className='flex items-center justify-between'><div><p className='text-xs text-slate-500 dark:text-slate-400 uppercase tracking-wider'>Активные потоки</p><p className='text-2xl font-bold text-slate-900 dark:text-slate-100 mt-1'>{stats.activeMonitors}/{stats.totalMonitors}</p></div><Activity className='w-10 h-10 text-emerald-500 opacity-60' /></div></div>
        <div className='card'><div className='flex items-center justify-between'><div><p className='text-xs text-slate-500 dark:text-slate-400 uppercase tracking-wider'>Всего событий</p><p className='text-2xl font-bold text-slate-900 dark:text-slate-100 mt-1'>{stats.totalEvents}</p></div><FileText className='w-10 h-10 text-blue-500 opacity-60' /></div></div>
        <div className='card'><div className='flex items-center justify-between'><div><p className='text-xs text-slate-500 dark:text-slate-400 uppercase tracking-wider'>Отправлено в Max</p><p className='text-2xl font-bold text-slate-900 dark:text-slate-100 mt-1'>{stats.sentEvents}</p></div><TrendingUp className='w-10 h-10 text-primary-500 opacity-60' /></div></div>
        <div className='card'><div className='flex items-center justify-between'><div><p className='text-xs text-slate-500 dark:text-slate-400 uppercase tracking-wider'>Обработано ИИ</p><p className='text-2xl font-bold text-slate-900 dark:text-slate-100 mt-1'>{stats.processedByAI}</p></div><Eye className='w-10 h-10 text-purple-500 opacity-60' /></div></div>
      </div>

      <div className='grid grid-cols-1 lg:grid-cols-2 gap-6'>
        <div className='card'>
          <div className='flex items-center justify-between mb-6'><h2 className='text-lg font-semibold text-slate-900 dark:text-slate-100'>Последние события</h2><FileText className='w-5 h-5 text-slate-400' /></div>
          <div className='space-y-3 max-h-[420px] overflow-y-auto pr-1'>
            {recentEvents.map(e => (
              <div key={e.id} className='flex items-center gap-3 p-3 bg-slate-50 dark:bg-slate-800/60 rounded-xl'>
                <div className={'w-2 h-2 rounded-full shrink-0 ' + (e.status === 'sent' ? 'bg-emerald-500' : e.status === 'failed' ? 'bg-red-500' : 'bg-amber-500')} />
                <div className='flex-1 min-w-0'>
                  <p className='text-sm text-slate-700 dark:text-slate-200 truncate'>{e.original_text || 'Пост без текста'}</p>
                  <span className={'text-xs px-2 py-1 rounded-lg shrink-0 ' + (e.status === 'sent' ? 'text-emerald-600 dark:text-emerald-400 bg-emerald-50 dark:bg-emerald-950/40' : e.status === 'failed' ? 'text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-950/40' : 'text-amber-600 dark:text-amber-400 bg-amber-50 dark:bg-amber-950/40')}>
                    {e.status === 'sent' ? 'Отправлено' : e.status === 'failed' ? 'Ошибка' : 'Отфильтровано'}
                  </span>
                  <span className='text-xs text-slate-400 ml-2'>ER: {e.er}%</span>
                  <span className='text-xs text-slate-400 ml-2'>· {formatMoscowDateTime(e.sent_at || e.created_at)} МСК</span>
                </div>
              </div>
            ))}
            {recentEvents.length === 0 && <p className='text-sm text-slate-400 py-4 text-center'>Нет событий</p>}
          </div>
        </div>

        <div className='card'>
          <div className='flex items-center justify-between mb-6'><h2 className='text-lg font-semibold text-slate-900 dark:text-slate-100'>Потоки</h2><GitBranch className='w-5 h-5 text-slate-400' /></div>
          <div className='space-y-3'>
            {allMonitors.filter(m => m.is_active).slice(0, 5).map(m => (
              <div key={m.id} className='flex items-center gap-3 p-3 bg-slate-50 dark:bg-slate-800/60 rounded-xl'>
                <div className='w-2 h-2 rounded-full bg-emerald-500 shrink-0' />
                <div className='flex-1 min-w-0'><p className='text-sm font-medium text-slate-700 dark:text-slate-200'>{m.name}</p><p className='text-xs text-slate-400'>Обработано: {m.posts_processed} • Отправлено: {m.posts_published}</p></div>
              </div>
            ))}
            {allMonitors.filter(m => m.is_active).length === 0 && <p className='text-sm text-slate-400 py-4 text-center'>Нет активных потоков</p>}
          </div>
        </div>
      </div>
    </div>
  )
}
