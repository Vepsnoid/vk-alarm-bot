import { useState, useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { Save, ArrowLeft, Sparkles, Loader2, BellDot } from 'lucide-react'
import api from '../api/client'

export default function MonitorEdit() {
  const { id } = useParams()
  const navigate = useNavigate()
  const isEdit = !!id

  const [form, setForm] = useState({ name: '', source_channels: '', max_channels: '', check_interval_minutes: 15, copy_media: true, log_all_posts: false, keywords: '', minus_words: '', min_er: 0, max_er: 100, use_ai: false, ai_prompt: '', ai_tone: 'нейтральный', ai_max_length: 5000, ai_fallback_to_original: true, owner_id: '', is_active: true })
  const [loading, setLoading] = useState(false); const [saving, setSaving] = useState(false)
  const [aiStatus, setAiStatus] = useState(false); const [checkingAi, setCheckingAi] = useState(false)
  const [errors, setErrors] = useState<string[]>([]); const [fieldErrors, setFieldErrors] = useState<Record<string, boolean>>({})
  const [isAdmin, setIsAdmin] = useState(false); const [usersList, setUsersList] = useState<{ id: number; username: string }[]>([])
  const [myId, setMyId] = useState<number | null>(null)

  useEffect(() => { (async () => { await fetchAiStatus(); const uid = await fetchMe(); if (isEdit) await fetchMonitor(uid) })() }, [id])

  const fetchAiStatus = async () => { try { const r = await api.get('/settings'); setAiStatus(r.data.ai_status) } catch { setAiStatus(false) } }
  const fetchMe = async (): Promise<number | null> => { try { const r = await api.get('/auth/me'); const uid = r.data.id ?? null; setMyId(uid); if (r.data.role === 'admin') { setIsAdmin(true); const u = await api.get('/users'); setUsersList(u.data) } return uid } catch { return null } }
  const checkAiToken = async () => { setCheckingAi(true); try { const r = await api.get('/settings'); setAiStatus(r.data.ai_status); alert(r.data.ai_status ? 'Токен ИИ валиден' : 'Токен не валиден') } catch { alert('Ошибка') } finally { setCheckingAi(false) } }
  const fetchMonitor = async (myIdArg: number | null = null) => { setLoading(true); try { const r = await api.get('/monitors/' + id); const d = r.data; setForm({ ...d, check_interval_minutes: d.check_interval_minutes ?? 15, copy_media: d.copy_media ?? true, log_all_posts: d.log_all_posts ?? false, owner_id: d.owner_id != null && d.owner_id !== myIdArg ? String(d.owner_id) : '', source_channels: d.source_channels || '', max_channels: d.max_channels || '', keywords: d.keywords || '', minus_words: d.minus_words || '', ai_prompt: d.ai_prompt || '' }) } catch {} finally { setLoading(false) } }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    const missing: string[] = []; const fe: Record<string, boolean> = {}
    if (!form.name.trim()) { missing.push('Название потока'); fe.name = true }
    if (!form.source_channels.trim()) { missing.push('Источники VK'); fe.source_channels = true }
    if (!form.max_channels.trim()) { missing.push('Целевой канал Max'); fe.max_channels = true }
    if (missing.length) { setErrors(missing); setFieldErrors(fe); return }
    setErrors([]); setFieldErrors({}); setSaving(true)
    try {
      const f = { ...form, owner_id: form.owner_id ? Number(form.owner_id) : (isAdmin && myId ? myId : null), use_ai: aiStatus ? form.use_ai : false }
      if (isEdit) await api.put('/monitors/' + id, f); else await api.post('/monitors', f)
      navigate('/monitors', isEdit ? { state: { freshStart: true, name: form.name } } : undefined)
    } catch (err: any) { alert('Ошибка сохранения: ' + (err.response?.data?.detail || err.message || 'неизвестная')) }
    finally { setSaving(false) }
  }

  if (loading) return <div className='text-center py-20 text-slate-500'>Загрузка...</div>

  const update = (k: string, v: any) => { setForm(f => ({ ...f, [k]: v })); if (errors.length) setErrors([]); setFieldErrors(fe => { if (!fe[k]) return fe; const next = { ...fe }; delete next[k]; return next }) }

  return (
    <div>
      <div className='flex items-center gap-4 mb-8'>
        <button onClick={() => navigate('/monitors')} className='p-2 hover:bg-slate-100 dark:hover:bg-slate-800 rounded-xl transition-colors'><ArrowLeft className='w-5 h-5 text-slate-600 dark:text-slate-400' /></button>
        <div><h1 className='text-2xl font-bold text-slate-900 dark:text-slate-100'>{isEdit ? 'Редактировать поток' : 'Новый поток'}</h1><p className='text-slate-500 dark:text-slate-400 mt-1'>Парсинг VK → фильтрация → Max</p></div>
      </div>

      <form onSubmit={handleSubmit} className='max-w-3xl space-y-6'>
        {errors.length > 0 && (
          <div className='p-4 bg-red-50 dark:bg-red-950/40 border border-red-200 dark:border-red-900/50 text-red-700 dark:text-red-400 rounded-2xl text-sm'>
            <p className='font-semibold mb-1'>Заполните обязательные поля:</p>
            <ul className='list-disc list-inside space-y-0.5'>{errors.map(er => <li key={er}>{er}</li>)}</ul>
          </div>
        )}
        <div className='card space-y-4'>
          <h2 className='text-lg font-semibold text-slate-900 dark:text-slate-100 flex items-center gap-2'><BellDot className='w-5 h-5 text-primary-500' /> Основное</h2>
          <div><label className='block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1.5'>Название <span className='text-red-500'>*</span></label><input value={form.name} onChange={e => update('name', e.target.value)} className={'input-field ' + (fieldErrors.name ? 'border-red-400 dark:border-red-500' : '')} placeholder='Новостной поток' /></div>
          <div><label className='block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1.5'>Периодичность проверки (мин)</label><input type='number' min={1} max={1440} value={form.check_interval_minutes} onChange={e => update('check_interval_minutes', parseInt(e.target.value) || 15)} className='input-field' /><p className='text-xs text-slate-400 mt-1'>Как часто проверять источники. Учитывайте лимиты VK API и ключевые/минус-слова для отсева.</p></div>
          <label className='flex items-center gap-2 text-sm text-slate-600 dark:text-slate-300'><input type='checkbox' checked={form.copy_media} onChange={e => update('copy_media', e.target.checked)} className='rounded text-primary-500' /> Пересылать медиа-вложения (копировать фото/документы из VK в Max)</label>
          <label className='flex items-start gap-2 text-sm text-slate-600 dark:text-slate-300'><input type='checkbox' checked={form.log_all_posts} onChange={e => update('log_all_posts', e.target.checked)} className='rounded text-primary-500 mt-0.5' /><span>Журнал пайплайна: сохранять <strong>все</strong> спарсенные посты в «Событиях» со статусами (отсеяно словами/ER, в обработке, отправлено, ошибка). Хранятся последние 2000 записей на поток — включайте для отладки фильтров.</span></label>
          {isAdmin && <div><label className='block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1.5'>Владелец потока</label><select value={form.owner_id} onChange={e => update('owner_id', e.target.value)} className='input-field'><option value=''>Я (администратор)</option>{usersList.filter(u => u.id !== myId).map(u => <option key={u.id} value={u.id}>{u.username}</option>)}</select><p className='text-xs text-slate-400 mt-1'>Администратор может назначить поток другому пользователю.</p></div>}
          <div><label className='block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1.5'>Источники VK <span className='text-red-500'>*</span> (по одному на строку)</label><textarea value={form.source_channels} onChange={e => update('source_channels', e.target.value)} className={'input-field h-24 resize-none ' + (fieldErrors.source_channels ? 'border-red-400 dark:border-red-500' : '')} placeholder='durov&#10;https://vk.com/public1&#10;club1' /><p className='text-xs text-slate-400 mt-1'>Принимаются ссылки vk.com / vk.ru (любой поддомен, в т.ч. m.vk.com), короткие имена, club123 / public123 / id123 / wall-1_2. Битые ссылки и закрытые каналы не мешают остальным источникам — причина появится в оповещениях.</p></div>
          <div><label className='block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1.5'>Max каналы <span className='text-red-500'>*</span> (по одному на строку)</label><textarea value={form.max_channels} onChange={e => update('max_channels', e.target.value)} className={'input-field h-24 resize-none ' + (fieldErrors.max_channels ? 'border-red-400 dark:border-red-500' : '')} placeholder='ID чата (например -78905088689474) или ссылка Max&#10;https://max.ru/chat/123' /></div>
        </div>

        <div className='card space-y-4'>
          <h2 className='text-lg font-semibold text-slate-900 dark:text-slate-100 flex items-center gap-2'><Sparkles className='w-5 h-5 text-primary-500' /> Фильтры</h2>
          <div className='grid grid-cols-2 gap-4'>
            <div><label className='block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1.5'>Мин. ER (%)</label><input type='number' min={0} max={100} step={0.1} value={form.min_er} onChange={e => update('min_er', parseFloat(e.target.value) || 0)} className='input-field' /></div>
            <div><label className='block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1.5'>Макс. ER (%)</label><input type='number' min={0} max={100} step={0.1} value={form.max_er} onChange={e => update('max_er', parseFloat(e.target.value) || 100)} className='input-field' /></div>
          </div>
          <div><label className='block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1.5'>Ключевые слова (по одному на строку)</label><textarea value={form.keywords} onChange={e => update('keywords', e.target.value)} className='input-field h-20 resize-none' placeholder='новости&#10;политика&#10;технологии' /></div>
          <div><label className='block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1.5'>Минус слова (по одному на строку)</label><textarea value={form.minus_words} onChange={e => update('minus_words', e.target.value)} className='input-field h-20 resize-none' placeholder='реклама&#10;спам' /></div>
        </div>

        <div className='card space-y-4'>
          <div className='flex items-center justify-between'>
            <h2 className='text-lg font-semibold text-slate-900 dark:text-slate-100 flex items-center gap-2'><Sparkles className='w-5 h-5 text-primary-500' /> Нейросеть (обработка промптом)</h2>
            <div className='flex items-center gap-3'>
              <button type='button' onClick={checkAiToken} disabled={checkingAi} className='text-xs text-primary-600 dark:text-primary-400 hover:underline flex items-center gap-1'>{checkingAi ? <Loader2 className='w-3 h-3 animate-spin' /> : <Sparkles className='w-3 h-3' />} Проверить ИИ</button>
              <button type='button' onClick={() => form.use_ai && aiStatus ? update('use_ai', false) : update('use_ai', true)} disabled={!aiStatus} className={'toggle relative ' + (!aiStatus ? 'opacity-40 cursor-not-allowed' : '') + ' ' + (form.use_ai && aiStatus ? 'toggle-active' : 'toggle-inactive')} title={!aiStatus ? 'ИИ не настроен' : ''}>
                <span className={'toggle-dot ' + (form.use_ai && aiStatus ? 'translate-x-6' : 'translate-x-1')} />
              </button>
            </div>
          </div>
          {!aiStatus && <p className='text-xs text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-950/40 p-3 rounded-xl'>ИИ не настроен в .env или токен не валиден</p>}
          {form.use_ai && aiStatus && <>
            <div><label className='block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1.5'>Промпт обработки (инструкция для ИИ)</label><textarea value={form.ai_prompt} onChange={e => update('ai_prompt', e.target.value)} className='input-field h-32 resize-none' placeholder='Например: перепиши текст транслитом / сделай краткий дайджест / перескажи в 2 абзаца / переведи на английский' /><p className='text-xs text-slate-400 mt-1'>Сначала посты отсеиваются по ключевым и минус-словам, затем текст обрабатывается этой инструкцией — и в Max уходит уже преобразованный текст. Если ИИ вернёт <code>SKIP</code> (или «пропустить», «нерелевантно») — пост <strong>не публикуется</strong>: так можно поручить нейросети отбор релевантных сообщений.</p></div>
            <div className='grid grid-cols-2 gap-4 min-w-0'>
              <div className='min-w-0'><label className='block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1.5'>Тон</label><input value={form.ai_tone} onChange={e => update('ai_tone', e.target.value)} className='input-field min-w-0' placeholder='нейтральный' /></div>
              <div className='min-w-0'><label className='block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1.5'>Макс. символов</label><input type='number' min={100} max={15000} value={form.ai_max_length} onChange={e => update('ai_max_length', parseInt(e.target.value) || 5000)} className='input-field min-w-0' /></div>
            </div>
            <label className='flex items-center gap-2 text-sm text-slate-600 dark:text-slate-300'><input type='checkbox' checked={form.ai_fallback_to_original} onChange={e => update('ai_fallback_to_original', e.target.checked)} className='rounded text-primary-500' /> Отправлять оригинал при ошибке ИИ</label>
          </>}
        </div>

        <div className='flex items-center gap-4'>
          <button type='submit' disabled={saving} className='btn-primary flex items-center gap-2'><Save className='w-5 h-5' />{saving ? 'Сохранение...' : 'Сохранить'}</button>
          <button type='button' onClick={() => navigate('/monitors')} className='btn-secondary'>Отмена</button>
          <span className='text-xs text-slate-400'>Сохранение перезапускает поток (fresh start): проверка начнётся сразу, а история с прошлого запуска будет пропущена.</span>
        </div>
      </form>
    </div>
  )
}
