import { useEffect, useState, useMemo } from 'react'
import { ExternalLink, ChevronDown, ChevronUp, BellDot, Search, FilterX, Calendar, GitBranch, Trash2 } from 'lucide-react'
import { formatMoscowDateTime } from '../utils/datetime'
import api from '../api/client'
import { useAutoRefresh } from '../hooks/useAutoRefresh'

interface Event { id: number; monitor_id: number; original_url: string; original_text: string | null; original_date: string | null; likes: number; reposts: number; comments: number; views: number; er: number; ai_analysis_result: string | null; ai_filtered: boolean; status: string; sent_at: string | null; error_message: string | null; created_at: string }
interface MonitorItem { id: number; name: string }

const EMPTY_FILTERS = { status: 'all', monitor_id: 'all', date_from: '', date_to: '', search: '' }

// Labels / colours for every status the pipeline can write into ``events``.
const STATUS_LABELS: Record<string, string> = {
  sent: 'Отправлено',
  pending: 'В очереди (повтор)',
  failed: 'Ошибка отправки',
  filtered_keywords: 'Отсеяно: слова',
  filtered_er: 'Отсеяно: ER',
  filtered_ai: 'Отсеяно: ИИ',
  parsed: 'В обработке',
  filtered: 'Отфильтровано',
}
const STATUS_STYLES: Record<string, string> = {
  sent: 'text-emerald-600 bg-emerald-50 dark:bg-emerald-950/40',
  pending: 'text-sky-600 bg-sky-50 dark:bg-sky-950/40',
  failed: 'text-red-600 bg-red-50 dark:bg-red-950/40',
  filtered_keywords: 'text-amber-600 bg-amber-50 dark:bg-amber-950/40',
  filtered_er: 'text-orange-600 bg-orange-50 dark:bg-orange-950/40',
  filtered_ai: 'text-violet-600 bg-violet-50 dark:bg-violet-950/40',
  parsed: 'text-slate-600 bg-slate-100 dark:bg-slate-800',
}
const statusLabel = (s: string) => STATUS_LABELS[s] || 'Отфильтровано'
const statusStyle = (s: string) => STATUS_STYLES[s] || 'text-amber-600 bg-amber-50 dark:bg-amber-950/40'
const statusBar = (s: string) => (s === 'sent' ? 'bg-emerald-500' : s === 'failed' ? 'bg-red-500' : s === 'pending' ? 'bg-sky-500' : s === 'parsed' ? 'bg-slate-400' : 'bg-amber-500')



function PostText({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false); const limit = 280
  if (!text || text.length <= limit) return <p className='text-sm text-slate-700 dark:text-slate-200 whitespace-pre-wrap'>{text || '(без текста)'}</p>
  return <div><p className='text-sm text-slate-700 dark:text-slate-200 whitespace-pre-wrap'>{expanded ? text : text.slice(0, limit).trim() + '...'}</p><button type='button' onClick={() => setExpanded(!expanded)} className='mt-1 text-xs font-semibold text-primary-600 hover:text-primary-700 flex items-center gap-1'>{expanded ? <>Свернуть <ChevronUp className='w-3 h-3' /></> : <>Читать полностью <ChevronDown className='w-3 h-3' /></>}</button></div>
}

export default function Events() {
  const [events, setEvents] = useState<Event[]>([])
  const [loading, setLoading] = useState(true)
  const [monitors, setMonitors] = useState<MonitorItem[]>([])
  const [filters, setFilters] = useState(EMPTY_FILTERS)
  const [applied, setApplied] = useState(EMPTY_FILTERS)

  // ``bg`` runs the same query in the background (auto refresh) without the
  // loading spinner; the page is refreshed every 15 s by ``useAutoRefresh``.
  const fetchEvents = async (bg = false) => {
    if (!bg) setLoading(true)
    try {
      const params = new URLSearchParams(); params.set('limit', '200')
      if (applied.status !== 'all') params.set('status_filter', applied.status)
      if (applied.monitor_id !== 'all') params.set('monitor_id', applied.monitor_id)
      if (applied.date_from) params.set('date_from', applied.date_from)
      if (applied.date_to) params.set('date_to', applied.date_to)
      if (applied.search.trim()) params.set('search', applied.search.trim())
      const res = await api.get('/monitors/events?' + params)
      setEvents(res.data || [])
    } catch {} finally { setLoading(false) }
  }

  const fetchMonitors = async () => { try { const r = await api.get('/monitors'); setMonitors(r.data) } catch { setMonitors([]) } }

  useEffect(() => { fetchMonitors() }, [])
  useEffect(() => { fetchEvents() }, [applied])
  useAutoRefresh(() => fetchEvents(true), 15000)

  const monitorsMap = useMemo(() => { const m: Record<number, string> = {}; monitors.forEach(x => { m[x.id] = x.name }); return m }, [monitors])
  const hasActiveFilters = useMemo(() => JSON.stringify(applied) !== JSON.stringify(EMPTY_FILTERS), [applied])

  const applyFilters = (e?: React.FormEvent) => { e?.preventDefault(); setApplied({ ...filters }) }
  const resetFilters = () => { setFilters(EMPTY_FILTERS); setApplied(EMPTY_FILTERS) }
  const update = (k: keyof typeof EMPTY_FILTERS, v: string) => setFilters(f => ({ ...f, [k]: v }))

  const filterParams = () => {
    const params = new URLSearchParams()
    if (applied.status !== 'all') params.set('status_filter', applied.status)
    if (applied.monitor_id !== 'all') params.set('monitor_id', applied.monitor_id)
    if (applied.date_from) params.set('date_from', applied.date_from)
    if (applied.date_to) params.set('date_to', applied.date_to)
    if (applied.search.trim()) params.set('search', applied.search.trim())
    return params
  }

  const deleteEvent = async (id: number) => {
    if (!confirm('Удалить это событие?')) return
    try { await api.delete('/monitors/events/' + id); setEvents(list => list.filter(e => e.id !== id)) }
    catch { alert('Не удалось удалить событие'); fetchEvents(true) }
  }

  const clearEvents = async () => {
    if (events.length === 0) return
    if (!confirm('Удалить все найденные события (' + events.length + ')? Действие необратимо.')) return
    try { const r = await api.post('/monitors/events/clear?' + filterParams()); fetchEvents(true); alert('Удалено событий: ' + (r.data?.deleted ?? 0)) }
    catch { alert('Не удалось удалить события') }
  }

  if (loading && events.length === 0) return <div className='text-center py-20 text-slate-500'>Загрузка...</div>


  return (
    <div>
      <div className='flex flex-wrap items-center justify-between gap-3 mb-8'>
        <div><h1 className='text-2xl font-bold text-slate-900 dark:text-slate-100'>События</h1></div>
      </div>
      <form onSubmit={applyFilters} className='card mb-6 space-y-4'>
        <div className='grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-6 gap-3 min-w-0'>
          <div className='min-w-0 sm:col-span-2 xl:col-span-2'>
            <label className='block text-xs font-medium text-slate-500 dark:text-slate-400 mb-1'>Поиск по тексту</label>
            <div className='relative min-w-0'>
              <Search className='w-4 h-4 text-slate-400 absolute left-3 top-1/2 -translate-y-1/2' />
              <input value={filters.search} onChange={e => update('search', e.target.value)} className='input-field pl-9 text-sm py-2 min-w-0' placeholder='Ключевое слово...' />
            </div>
          </div>
          <div className='min-w-0'>
            <label className='block text-xs font-medium text-slate-500 dark:text-slate-400 mb-1'>Поток</label>
            <select value={filters.monitor_id} onChange={e => update('monitor_id', e.target.value)} className='input-field text-sm py-2 min-w-0'>
              <option value='all'>Все потоки</option>
              {monitors.map(m => <option key={m.id} value={m.id}>{m.name}</option>)}
            </select>
          </div>
          <div className='min-w-0'>
            <label className='block text-xs font-medium text-slate-500 dark:text-slate-400 mb-1'>Статус</label>
            <select value={filters.status} onChange={e => update('status', e.target.value)} className='input-field text-sm py-2 min-w-0'>
              <option value='all'>Все</option><option value='sent'>Отправлено</option><option value='pending'>В очереди (повтор)</option><option value='failed'>Ошибка отправки</option><option value='filtered_keywords'>Отсеяно: слова</option><option value='filtered_er'>Отсеяно: ER</option><option value='filtered_ai'>Отсеяно: ИИ</option><option value='parsed'>В обработке</option><option value='filtered'>Отфильтровано (старое)</option>
            </select>
          </div>
          <div className='min-w-0'>
            <label className='block text-xs font-medium text-slate-500 dark:text-slate-400 mb-1'>Дата с</label>
            <input type='date' value={filters.date_from} onChange={e => update('date_from', e.target.value)} className='input-field text-sm py-2 min-w-0' />
          </div>
          <div className='min-w-0'>
            <label className='block text-xs font-medium text-slate-500 dark:text-slate-400 mb-1'>Дата по</label>
            <input type='date' value={filters.date_to} onChange={e => update('date_to', e.target.value)} className='input-field text-sm py-2 min-w-0' />
          </div>
        </div>
        <div className='flex flex-wrap items-center gap-3'>
          <button type='submit' className='btn-primary flex items-center gap-2 text-sm'><Calendar className='w-4 h-4' /> Применить фильтры</button>
          <button type='button' onClick={resetFilters} className='btn-secondary flex items-center gap-2 text-sm'><FilterX className='w-4 h-4' /> Сбросить</button>
          <button type='button' onClick={clearEvents} disabled={events.length === 0} className='btn-secondary flex items-center gap-2 text-sm text-red-600 dark:text-red-400 disabled:opacity-40' title='Удалить все события по текущим фильтрам'><Trash2 className='w-4 h-4' /> Удалить найденные</button>
          <span className='text-xs text-slate-400 ml-auto'>Найдено: {events.length}</span>
        </div>
      </form>


      <div className='space-y-4'>
        {events.map(e => (
          <div key={e.id} className='card'>
            <div className='flex items-start gap-4'>
              <div className={'w-1.5 h-full min-h-[40px] rounded-full shrink-0 mt-1 ' + statusBar(e.status)} />
              <div className='flex-1 min-w-0'>
                <div className='flex items-center gap-2 mb-2'>
                  <span className={'text-xs px-2 py-1 rounded-lg font-medium ' + statusStyle(e.status)}>
                    {statusLabel(e.status)}
                  </span>
                  {monitorsMap[e.monitor_id] && <span className='text-xs px-2 py-1 rounded-lg font-medium bg-indigo-50 dark:bg-indigo-950/40 text-indigo-600 dark:text-indigo-400 flex items-center gap-1'><GitBranch className='w-3 h-3' />{monitorsMap[e.monitor_id]}</span>}
                  <a href={e.original_url} target='_blank' rel='noopener noreferrer' className='text-xs text-primary-600 hover:underline flex items-center gap-1'><ExternalLink className='w-3 h-3' />Оригинал</a>
                  <button type='button' onClick={() => deleteEvent(e.id)} className='ml-auto p-1.5 text-slate-400 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-950/40 rounded-lg transition-colors' title='Удалить событие'><Trash2 className='w-4 h-4' /></button>
                </div>
                <PostText text={e.original_text || ''} />
                {e.ai_analysis_result && <div className='mt-2 p-2 bg-indigo-50 dark:bg-indigo-950/40 rounded-lg text-xs text-indigo-600 dark:text-indigo-400'><strong>ИИ:</strong> {e.ai_analysis_result}</div>}
                {e.error_message && <div className={'mt-2 p-2 rounded-lg text-xs ' + (e.status === 'failed' ? 'bg-red-50 dark:bg-red-950/40 text-red-600 dark:text-red-400' : 'bg-amber-50 dark:bg-amber-950/40 text-amber-700 dark:text-amber-400')}>{e.error_message}</div>}
                <div className='flex flex-wrap items-center gap-4 mt-2 text-xs text-slate-400 dark:text-slate-500'>
                  <span>❤️ {e.likes}</span><span>🔄 {e.reposts}</span><span>💬 {e.comments}</span><span>👁 {e.views}</span><span className='text-primary-500 font-medium'>ER: {e.er}%</span>
                  {e.original_date && <span>🗓 Пост: {formatMoscowDateTime(e.original_date)} МСК</span>}
                  <span className='ml-auto'>{e.sent_at ? '📅 Отправлено: ' + formatMoscowDateTime(e.sent_at) + ' МСК' : '📥 ' + formatMoscowDateTime(e.created_at) + ' МСК'}</span>
                </div>
              </div>
            </div>
          </div>
        ))}
        {events.length === 0 && <div className='card text-center py-16'><BellDot className='w-12 h-12 text-slate-300 mx-auto mb-4' /><p className='text-slate-500'>Событий не найдено</p>{hasActiveFilters && <button onClick={resetFilters} className='text-xs text-primary-600 hover:underline mt-2'>Сбросить фильтры</button>}</div>}
      </div>
    </div>
  )
}
