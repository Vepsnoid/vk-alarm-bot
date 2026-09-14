import { useEffect, useState, useMemo } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { Plus, Play, Pause, Trash2, Edit3, Radio, User, BellDot, Clock, Timer, Sparkles, Link2 } from 'lucide-react'
import { formatMoscowDateTime, parseUtc } from '../utils/datetime'
import api from '../api/client'
import { useAutoRefresh } from '../hooks/useAutoRefresh'

interface Monitor { id: number; name: string; is_active: boolean; check_interval_minutes: number; source_channels: string; max_channels: string | null; posts_processed: number; posts_published: number; ai_filtered_out: number; posts_filtered_keywords: number; posts_filtered_er: number; log_all_posts?: boolean; ai_successes: number; ai_fallbacks: number; publication_errors: number; owner_id?: number | null; last_run_at: string | null; run_started_at?: string | null; created_at: string }

/** Live status of a stream: run in progress / countdown / overdue / waiting. */
function NextCheck({ monitor, now }: { monitor: Monitor; now: number }) {
  const interval = monitor.check_interval_minutes || 15
  // A stream with hundreds of sources runs for minutes: show that explicitly
  // instead of a countdown that is already in the past.
  const started = parseUtc(monitor.run_started_at)
  if (started && now - started.getTime() < 20 * 60000) {
    const mins = Math.floor((now - started.getTime()) / 60000)
    return <span className='text-primary-600 dark:text-primary-400 font-medium'>проверяю источники{mins > 0 ? ': ' + mins + ' мин' : '…'}</span>
  }
  const base = parseUtc(monitor.last_run_at)
  if (!base) return <span className='text-emerald-600 dark:text-emerald-400 font-medium'>ожидает запуска</span>
  const totalSec = Math.floor((base.getTime() + interval * 60000 - now) / 1000)
  if (totalSec <= 0) {
    // The interval has elapsed: the scheduler fires the run within ~30 seconds.
    const overdueMin = Math.floor(-totalSec / 60)
    if (overdueMin >= 2) return <span className='text-amber-600 dark:text-amber-400 font-medium'>просрочено: {overdueMin} мин</span>
    return <span className='text-emerald-600 dark:text-emerald-400 font-medium'>запускаю проверку…</span>
  }
  const mm = String(Math.floor(totalSec / 60)).padStart(2, '0')
  const ss = String(totalSec % 60).padStart(2, '0')
  return <span className={totalSec <= 30 ? 'text-amber-600 dark:text-amber-400 font-medium' : ''}>до проверки: {mm}:{ss}</span>
}
interface UserItem { id: number; username: string; role: string }

export default function Monitors() {
  const [monitors, setMonitors] = useState<Monitor[]>([])
  const [usersList, setUsersList] = useState<UserItem[]>([])
  const [selectedUser, setSelectedUser] = useState<string>('all')
  const [loading, setLoading] = useState(true)
  const [now, setNow] = useState(Date.now())
  const location = useLocation()
  const navigate = useNavigate()
  const [freshNotice, setFreshNotice] = useState<string | null>(null)

  useEffect(() => {
    const st = location.state as { freshStart?: boolean; name?: string } | null
    if (st?.freshStart) { setFreshNotice(st.name || 'поток'); navigate(location.pathname, { replace: true }) }
  }, [location.state, location.pathname, navigate])


  useEffect(() => { fetchMonitors() }, [])
  useAutoRefresh(() => { fetchMonitors() }, 15000)
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(t) }, [])

  const fetchMonitors = async () => {
    try {
      const res = await api.get('/monitors'); setMonitors(res.data)
      try { const u = await api.get('/users'); setUsersList(u.data) } catch { setUsersList([]) }
    } catch (err) { console.error(err) } finally { setLoading(false) }
  }

  const usersMap = useMemo(() => { const m: Record<number, string> = {}; usersList.forEach(u => { m[u.id] = u.username }); return m }, [usersList])
  const filteredMonitors = useMemo(() => monitors.filter(s => selectedUser === 'all' || s.owner_id === Number(selectedUser)), [monitors, selectedUser])

  const toggleMonitor = async (id: number, current: boolean) => { try { await api.put('/monitors/' + id, { is_active: !current }); fetchMonitors() } catch {} }
  const deleteMonitor = async (id: number) => { if (!confirm('Удалить поток?')) return; try { await api.delete('/monitors/' + id); fetchMonitors() } catch {} }
  const testSources = async (id: number) => { try { const r = await api.post('/monitors/' + id + '/test-sources'); alert(r.data.results.map((i: any) => i.source + ': ' + (i.ok ? 'доступен (id ' + i.id + ')' : (i.error || 'недоступен'))).join('\n') || 'Нет источников') } catch { alert('Ошибка проверки источников') } }
  const testMax = async (id: number) => { try { const r = await api.post('/monitors/' + id + '/test-max'); alert(r.data.results.map((i: any) => i.target + ': ' + (i.ok ? 'доступен' : i.error)).join('\n') || 'Нет каналов') } catch { alert('Ошибка') } }

  if (loading) return <div className='text-center py-20 text-slate-500'>Загрузка...</div>

  return (
    <div>
      {freshNotice && <div className='mb-4 flex items-center justify-between gap-3 rounded-2xl bg-emerald-50 dark:bg-emerald-950/40 px-4 py-3 text-sm text-emerald-700 dark:text-emerald-300'><span className='flex items-center gap-2 min-w-0'><Sparkles className='w-4 h-4 shrink-0' /> Поток «{freshNotice}» сохранён — выполнен fresh start: история пропущена, проверка запущена сейчас.</span><button onClick={() => setFreshNotice(null)} className='shrink-0 hover:underline'>Скрыть</button></div>}

      <div className='flex flex-wrap items-center justify-between gap-3 mb-8'>

        <div><h1 className='text-2xl font-bold text-slate-900 dark:text-slate-100'>Потоки</h1><p className='text-slate-500 dark:text-slate-400 mt-1'>Потоки мониторинга VK → Max</p></div>
        <div className='flex items-center gap-3'>
          {usersList.length > 0 && <select value={selectedUser} onChange={e => setSelectedUser(e.target.value)} className='input-field w-auto text-sm py-2'>
            <option value='all'>Все пользователи</option>
            {usersList.map(u => <option key={u.id} value={u.id}>{u.username}</option>)}
          </select>}
          <Link to='/monitors/new' className='btn-primary flex items-center gap-2'><Plus className='w-5 h-5' /> Создать</Link>
        </div>
      </div>

      <div className='space-y-4'>
        {filteredMonitors.map(m => (
          <div key={m.id} className='card flex items-center gap-4'>
            <div className='flex-1 min-w-0'>
              <div className='flex items-center gap-3 mb-1'>
                <h3 className='text-lg font-semibold text-slate-900 dark:text-slate-100'>{m.name}</h3>
                <span className={'text-xs px-2 py-1 rounded-lg font-medium ' + (m.is_active ? 'bg-emerald-50 dark:bg-emerald-950/40 text-emerald-600 dark:text-emerald-400' : 'bg-slate-100 dark:bg-slate-800 text-slate-500 dark:text-slate-400')}>{m.is_active ? 'Активен' : 'На паузе'}</span>
                {m.owner_id && usersMap[m.owner_id] && <span className='text-xs px-2 py-1 rounded-lg bg-indigo-50 dark:bg-indigo-950/40 text-indigo-600 dark:text-indigo-400 font-medium flex items-center gap-1'><User className='w-3 h-3' />{usersMap[m.owner_id]}</span>}
              </div>
              <div className='flex flex-wrap items-center gap-6 mt-2 text-sm text-slate-500 dark:text-slate-400'>
                <span>Источники: {m.source_channels.split('\n').filter(s => s.trim()).length}</span>
                <span>Max каналы: {m.max_channels ? m.max_channels.split('\n').filter(s => s.trim()).length : 0}</span>
                <span>Обработано: {m.posts_processed}</span>
                <span>Отправлено: {m.posts_published}</span>
                <span>Отсеяно (слова/ER): {m.posts_filtered_keywords}/{m.posts_filtered_er}</span>
                {m.log_all_posts && <span className='text-amber-600 dark:text-amber-400 font-medium'>журнал всех постов: вкл</span>}
              </div>
              <div className='flex flex-wrap items-center gap-4 mt-2 text-xs text-slate-400'>
                {m.is_active && <span className='flex items-center gap-1'><Timer className='w-3 h-3' /> <NextCheck monitor={m} now={now} /></span>}
                <span>ИИ обработано: {m.ai_successes}</span>
                <span>ИИ сбой: {m.ai_fallbacks}</span>
                <span>ИИ отсеяно: {m.ai_filtered_out}</span>
                <span>Ошибки: {m.publication_errors}</span>
                <span className='flex items-center gap-1'><Clock className='w-3 h-3' /> Последний запуск: {m.last_run_at ? formatMoscowDateTime(m.last_run_at) : '—'}</span>
                <span className='flex items-center gap-1'><Timer className='w-3 h-3' /> Периодичность: {m.check_interval_minutes || 15} мин</span>
              </div>
            </div>
            <div className='flex items-center gap-2 shrink-0'>
              <button onClick={() => toggleMonitor(m.id, m.is_active)} className='p-2 hover:bg-slate-100 dark:hover:bg-slate-800 rounded-xl transition-colors' title={m.is_active ? 'Поставить на паузу' : 'Запустить'}>
                {m.is_active ? <Pause className='w-5 h-5 text-amber-500' /> : <Play className='w-5 h-5 text-emerald-500' />}
              </button>
              <Link to={'/monitors/' + m.id} className='p-2 hover:bg-slate-100 dark:hover:bg-slate-800 rounded-xl transition-colors' title='Редактировать'><Edit3 className='w-5 h-5 text-slate-600 dark:text-slate-400' /></Link>
              <button onClick={() => testSources(m.id)} className='p-2 hover:bg-slate-100 dark:hover:bg-slate-800 rounded-xl transition-colors' title='Проверить источники VK'><Link2 className='w-5 h-5 text-emerald-500' /></button>
              <button onClick={() => testMax(m.id)} className='p-2 hover:bg-slate-100 dark:hover:bg-slate-800 rounded-xl transition-colors' title='Проверить Max каналы'><Radio className='w-5 h-5 text-blue-500' /></button>
              <button onClick={() => deleteMonitor(m.id)} className='p-2 hover:bg-red-50 dark:hover:bg-red-950/40 rounded-xl transition-colors' title='Удалить'><Trash2 className='w-5 h-5 text-red-500' /></button>
            </div>
          </div>
        ))}
        {filteredMonitors.length === 0 && <div className='card text-center py-16'><BellDot className='w-12 h-12 text-slate-300 dark:text-slate-700 mx-auto mb-4' /><p className='text-slate-500'>Потоки не созданы</p><Link to='/monitors/new' className='btn-primary mt-4 inline-block'>Создать первый поток</Link></div>}
      </div>
    </div>
  )
}
