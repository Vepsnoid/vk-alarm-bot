import { useState, useEffect } from 'react'
import { Save, Key, CheckCircle, XCircle, RefreshCw, Loader2, Palette, Sun, Moon, Monitor, Users, UserPlus, Shield, Trash2, KeyRound, Eye, EyeOff } from 'lucide-react'
import api from '../api/client'
import { useThemeStore } from '../store/themeStore'

interface UserItem { id: number; username: string; role: string; is_active: boolean; created_at: string }

const PROVIDER_LABELS: Record<string, string> = { gigachat: 'GigaChat (Сбер)', openai: 'OpenAI', deepseek: 'DeepSeek', openrouter: 'OpenRouter', custom: 'Другой (OpenAI-совместимый)' }
const MODEL_PRESETS: Record<string, string[]> = {
  gigachat: ['GigaChat', 'GigaChat-Pro', 'GigaChat-Max'],
  openai: ['gpt-4o-mini', 'gpt-4o', 'gpt-4.1-mini', 'gpt-3.5-turbo'],
  deepseek: ['deepseek-chat', 'deepseek-reasoner'],
  openrouter: ['openai/gpt-4o-mini', 'anthropic/claude-3.5-sonnet', 'google/gemini-2.0-flash-001'],
  custom: [],
}
const PROVIDER_DEFAULT_BASE: Record<string, string> = { gigachat: '', openai: 'https://api.openai.com/v1', deepseek: 'https://api.deepseek.com/v1', openrouter: 'https://openrouter.ai/api/v1', custom: '' }

function StatusDot({ state, title }: { state: 'ok' | 'bad' | 'unknown'; title?: string }) {
  const color = state === 'ok' ? 'bg-emerald-500 ring-4 ring-emerald-500/20' : state === 'bad' ? 'bg-red-500 ring-4 ring-red-500/20' : 'bg-slate-300 dark:bg-slate-600'
  return <span title={title} className={'inline-block w-3 h-3 rounded-full shrink-0 ' + color} />
}

export default function Settings() {
  const { theme, setTheme } = useThemeStore()
  const [currentUser, setCurrentUser] = useState<{ username: string; role: string } | null>(null)
  const [settings, setSettings] = useState({ vk_service_token: '', token_status: false, vk_account_blocked: false, vk_service_token_configured: false, ai_configured: false, ai_status: false, max_bot_token: '', max_bot_token_configured: false, max_bot_token_valid: false, max_bot_identity: '', max_bot_note: '', ai_api_key: '', ai_provider: 'gigachat', ai_model: 'GigaChat', ai_api_base: '' })
  const [users, setUsers] = useState<UserItem[]>([])
  const [showAddUser, setShowAddUser] = useState(false)
  const [newUser, setNewUser] = useState({ username: '', password: '', role: 'user' })
  const [showUserPassword, setShowUserPassword] = useState(false)
  const [usersError, setUsersError] = useState('')
  const [usersNotice, setUsersNotice] = useState('')
  const [savingUser, setSavingUser] = useState(false)
  const [saving, setSaving] = useState(false); const [saved, setSaved] = useState(false)
  const [checkingVk, setCheckingVk] = useState(false); const [checkingAi, setCheckingAi] = useState(false)
  const [checkingMax, setCheckingMax] = useState(false)
  const [models, setModels] = useState<string[]>([])
  const [loadingModels, setLoadingModels] = useState(false)
  const [modelsError, setModelsError] = useState('')


  const isAdmin = currentUser?.role === 'admin'

  useEffect(() => { fetchSettings(); fetchMe() }, [])

  const fetchMe = async () => { try { const r = await api.get('/auth/me'); setCurrentUser(r.data); if (r.data.role === 'admin') fetchUsers() } catch {} }
  const loadModels = async (opts?: { provider?: string; api_base?: string; api_key?: string }) => {
    setLoadingModels(true); setModelsError('')
    try {
      const r = await api.post('/settings/ai/models', { ai_provider: opts?.provider ?? settings.ai_provider, ai_api_base: opts?.api_base ?? settings.ai_api_base, ai_api_key: opts?.api_key ?? settings.ai_api_key })
      if (r.data.ok) { setModels(r.data.models || []); if (!(r.data.models || []).length) setModelsError('Список моделей пуст') }
      else { setModels([]); setModelsError(r.data.error || 'Не удалось получить список моделей') }
    } catch (err: any) { setModels([]); setModelsError(err.response?.data?.detail || 'Ошибка загрузки моделей') } finally { setLoadingModels(false) }
  }
  const fetchSettings = async () => { try { const r = await api.get('/settings'); setSettings(r.data); if (r.data.ai_configured) loadModels({ provider: r.data.ai_provider, api_base: r.data.ai_api_base, api_key: r.data.ai_api_key }) } catch {} }
  const fetchUsers = async () => { try { const r = await api.get('/users'); setUsers(r.data); setUsersError('') } catch (err: any) { setUsers([]); setUsersError(err?.response?.data?.detail || 'Не удалось загрузить список пользователей (проверьте права администратора)') } }
  const checkTokens = async () => { setCheckingVk(true); setCheckingAi(true); setCheckingMax(true); await fetchSettings(); setCheckingVk(false); setCheckingAi(false); setCheckingMax(false) }
  const onProviderChange = (provider: string) => {
    const base = PROVIDER_DEFAULT_BASE[provider] ?? settings.ai_api_base
    setSettings(s => ({ ...s, ai_provider: provider, ai_api_base: base, ai_model: MODEL_PRESETS[provider]?.[0] || s.ai_model }))
    loadModels({ provider, api_base: base, api_key: settings.ai_api_key })
  }

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault(); setSaving(true)
    try { await api.put('/settings', { vk_service_token: settings.vk_service_token, max_bot_token: settings.max_bot_token, ai_api_key: settings.ai_api_key, ai_provider: settings.ai_provider, ai_model: settings.ai_model, ai_api_base: settings.ai_api_base }); setSaved(true); setTimeout(() => setSaved(false), 3000); fetchSettings() } catch (err: any) { alert(err.response?.data?.detail || 'Ошибка') }
    finally { setSaving(false) }
  }

  const handleCreateUser = async (e: React.FormEvent) => {
    e.preventDefault(); setSavingUser(true); setUsersError(''); setUsersNotice('')
    try {
      const r = await api.post('/users', newUser)
      setNewUser({ username: '', password: '', role: 'user' })
      setShowAddUser(false)
      setUsersNotice('Пользователь «' + (r.data?.username || newUser.username) + '» создан')
      fetchUsers()
    } catch (err: any) {
      setUsersError(err?.response?.data?.detail || 'Не удалось создать пользователя: ' + (err?.message || 'неизвестная ошибка'))
    } finally { setSavingUser(false) }
  }
  const handleResetPassword = async (user: UserItem) => { const p = prompt('Новый пароль для ' + user.username + ':'); if (!p?.trim()) return; try { await api.put('/users/' + user.id, { password: p.trim() }); alert('Пароль изменен') } catch (err: any) { alert(err?.response?.data?.detail || 'Ошибка') } }
  const handleToggleUserStatus = async (user: UserItem) => { try { await api.put('/users/' + user.id, { is_active: !user.is_active }); fetchUsers() } catch (err: any) { setUsersError(err?.response?.data?.detail || 'Ошибка') } }
  const handleDeleteUser = async (user: UserItem) => { if (!confirm('Удалить ' + user.username + '?')) return; try { await api.delete('/users/' + user.id); fetchUsers() } catch { alert('Ошибка') } }

  return (
    <div>
      <div className='mb-8'><h1 className='text-2xl font-bold text-slate-900 dark:text-slate-100'>Настройки</h1><p className='text-slate-500 dark:text-slate-400 mt-1'>Управление системой</p></div>
      <form onSubmit={handleSave} className='max-w-3xl space-y-6'>
        <div className='card space-y-4'>
          <div className='flex items-center justify-between mb-4'>
            <div className='flex items-center gap-2'><Key className='w-5 h-5 text-primary-500' /><h2 className='text-lg font-semibold text-slate-900 dark:text-slate-100'>Токены API</h2></div>
            <button type='button' onClick={checkTokens} disabled={checkingVk || checkingAi || checkingMax} className='text-xs text-primary-600 dark:text-primary-400 hover:underline flex items-center gap-1'>{(checkingVk || checkingAi || checkingMax) ? <Loader2 className='w-3 h-3 animate-spin' /> : <RefreshCw className='w-3 h-3' />} Проверить</button>
          </div>
          <div>
            <label className='block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1.5'>Сервисный ключ VK API</label>
            <div className='flex items-center gap-2'>
              <input type='text' value={settings.vk_service_token} onChange={e => setSettings({ ...settings, vk_service_token: e.target.value })} className='input-field font-mono text-sm flex-1' disabled={!isAdmin} placeholder='vk1.a....' />
              <div className='flex items-center gap-2 shrink-0'>
                <StatusDot state={!settings.vk_service_token_configured ? 'unknown' : settings.token_status ? 'ok' : 'bad'} title={!settings.vk_service_token_configured ? 'Не задан' : settings.token_status ? 'Токен валиден' : settings.vk_account_blocked ? 'Аккаунт заблокирован' : 'Токен невалиден'} />
                <span className='text-xs text-slate-500 dark:text-slate-400 whitespace-nowrap'>{!settings.vk_service_token_configured ? 'Не задан' : settings.token_status ? 'Валиден' : settings.vk_account_blocked ? 'Заблокирован' : 'Невалиден'}</span>
              </div>
            </div>
            {!isAdmin && <p className='text-xs text-amber-600 mt-1'>Изменение доступно только Администраторам</p>}
          </div>
          {isAdmin && <div>
            <label className='block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1.5'>Токен бота Max</label>
            <div className='flex items-center gap-2'>
              <input type='text' value={settings.max_bot_token} onChange={e => setSettings({ ...settings, max_bot_token: e.target.value })} className='input-field font-mono text-sm flex-1' placeholder='Токен бота Max' />
              <div className='flex items-center gap-2 shrink-0'>
                <StatusDot state={!settings.max_bot_token_configured ? 'unknown' : settings.max_bot_token_valid ? 'ok' : 'bad'} title={!settings.max_bot_token_configured ? 'Не задан' : settings.max_bot_token_valid ? 'Токен валиден' : 'Токен невалиден'} />
                <span className='text-xs text-slate-500 dark:text-slate-400 whitespace-nowrap'>{!settings.max_bot_token_configured ? 'Не задан' : settings.max_bot_token_valid ? 'Валиден' : 'Невалиден'}</span>
              </div>
            </div>
            {settings.max_bot_identity && <div className='mt-2 text-xs text-slate-500 dark:text-slate-400'>Токен принадлежит боту: <strong className='text-slate-700 dark:text-slate-200'>{settings.max_bot_identity}</strong>. Отправлять в канал может только этот бот — добавьте в канал именно его.</div>}
            {settings.max_bot_note && <div className='mt-1 text-xs text-amber-600 dark:text-amber-400'>Max: {settings.max_bot_note}</div>}
          </div>}
          {isAdmin && <div>
            <div className='grid grid-cols-1 md:grid-cols-2 gap-3 min-w-0'>
              <div className='min-w-0'>
                <label className='block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1.5'>Провайдер ИИ</label>
                <select value={settings.ai_provider} onChange={e => onProviderChange(e.target.value)} className='input-field text-sm min-w-0'>
                  {Object.keys(PROVIDER_LABELS).map(p => <option key={p} value={p}>{PROVIDER_LABELS[p]}</option>)}
                </select>
              </div>
              <div className='min-w-0'>
                <label className='flex items-center justify-between text-sm font-medium text-slate-700 dark:text-slate-300 mb-1.5'>Модель
                  <button type='button' onClick={() => loadModels()} disabled={loadingModels} className='text-xs text-primary-600 dark:text-primary-400 hover:underline flex items-center gap-1 font-normal'>{loadingModels ? <Loader2 className='w-3 h-3 animate-spin' /> : <RefreshCw className='w-3 h-3' />} Загрузить список</button>
                </label>
                <input list='ai-model-presets' value={settings.ai_model} onChange={e => setSettings({ ...settings, ai_model: e.target.value })} className='input-field text-sm min-w-0' placeholder='Название модели' />
                <datalist id='ai-model-presets'>{(models.length ? models : (MODEL_PRESETS[settings.ai_provider] || [])).map(m => <option key={m} value={m} />)}</datalist>
                {(models.length ? models : (MODEL_PRESETS[settings.ai_provider] || [])).length > 0 && (
                  <div className='flex flex-wrap gap-1.5 mt-2'>
                    {(models.length ? models : (MODEL_PRESETS[settings.ai_provider] || [])).map(m => (
                      <button key={m} type='button' onClick={() => setSettings({ ...settings, ai_model: m })} className={'text-xs px-2 py-1 rounded-lg border transition-colors ' + (settings.ai_model === m ? 'border-primary-400 bg-primary-50 dark:bg-primary-950/40 text-primary-700 dark:text-primary-400 font-medium' : 'border-slate-200 dark:border-slate-700 text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800')}>{m}</button>
                    ))}
                  </div>
                )}
                {modelsError ? <p className='text-xs text-amber-600 dark:text-amber-400 mt-1'>{modelsError}</p> : models.length > 0 ? <p className='text-xs text-slate-400 mt-1'>Загружено моделей: {models.length} — нажмите на название, чтобы подставить его в поле</p> : null}
                <p className='text-xs text-slate-400 mt-1'>Список приходит из API провайдера (<code>GET /models</code>). Если нужной модели в нём нет — впишите её название вручную: часть провайдеров принимает синонимы, которых нет в списке.</p>
              </div>
            </div>
            {settings.ai_provider !== 'gigachat' && <div className='mt-3'>
              <label className='block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1.5'>API Base URL</label>
              <input value={settings.ai_api_base} onChange={e => setSettings({ ...settings, ai_api_base: e.target.value })} className='input-field font-mono text-sm' placeholder={PROVIDER_DEFAULT_BASE[settings.ai_provider] || 'https://api.example.com/v1'} />
            </div>}
            <label className='block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1.5 mt-3'>API Key {settings.ai_provider === 'gigachat' ? '/ GigaChat Credentials' : 'провайдера'}</label>
            <div className='flex items-center gap-2'>
              <input type='text' value={settings.ai_api_key} onChange={e => setSettings({ ...settings, ai_api_key: e.target.value })} className='input-field font-mono text-sm flex-1' placeholder={settings.ai_provider === 'gigachat' ? 'Authorization Key или Client ID:Client Secret' : 'API-ключ провайдера'} />
              <div className='flex items-center gap-2 shrink-0'>
                <StatusDot state={!settings.ai_configured ? 'unknown' : settings.ai_status ? 'ok' : 'bad'} title={!settings.ai_configured ? 'Не задан' : settings.ai_status ? 'ИИ настроен' : 'Ключ невалиден'} />
                <span className='text-xs text-slate-500 dark:text-slate-400 whitespace-nowrap'>{!settings.ai_configured ? 'Не задан' : settings.ai_status ? 'Валиден' : 'Невалиден'}</span>
              </div>
            </div>
            <p className='text-xs text-slate-400 mt-1'>Поддерживается любой OpenAI-совместимый провайдер: OpenAI, DeepSeek, OpenRouter и др.</p>

          </div>}
        </div>

        <div className='card space-y-4'>
          <div className='flex items-center gap-2 mb-4'><Palette className='w-5 h-5 text-purple-500' /><h2 className='text-lg font-semibold text-slate-900 dark:text-slate-100'>Тема оформления</h2></div>
          <div className='flex items-center gap-3'>
            {(['light', 'dark', 'system'] as const).map(t => (
              <button key={t} type='button' onClick={() => setTheme(t)} className={'flex items-center gap-2 px-4 py-2.5 rounded-xl border-2 transition-all text-sm font-medium ' + (theme === t ? 'border-primary-500 bg-primary-50 dark:bg-primary-950/40 text-primary-700 dark:text-primary-400' : 'border-slate-200 dark:border-slate-700 text-slate-600 dark:text-slate-400 hover:bg-slate-50 dark:hover:bg-slate-800')}>
                {t === 'light' ? <Sun className='w-4 h-4' /> : t === 'dark' ? <Moon className='w-4 h-4' /> : <Monitor className='w-4 h-4' />}
                {t === 'light' ? 'Светлая' : t === 'dark' ? 'Темная' : 'Системная'}
              </button>
            ))}
          </div>
        </div>

        {isAdmin && <div className='card space-y-4'>
          <div className='flex items-center justify-between mb-4'>
            <h2 className='text-lg font-semibold text-slate-900 dark:text-slate-100 flex items-center gap-2'><Users className='w-5 h-5 text-primary-500' /> Пользователи</h2>
            <button type='button' onClick={() => setShowAddUser(!showAddUser)} className='btn-secondary flex items-center gap-2 text-sm py-2'><UserPlus className='w-4 h-4' />{showAddUser ? 'Отмена' : 'Добавить'}</button>
          </div>
          {usersError && <div className='p-3 mb-3 bg-red-50 dark:bg-red-950/40 text-red-600 dark:text-red-400 rounded-xl text-sm'>{usersError}</div>}
          {usersNotice && <div className='p-3 mb-3 bg-emerald-50 dark:bg-emerald-950/40 text-emerald-700 dark:text-emerald-300 rounded-xl text-sm'>{usersNotice}</div>}
          {showAddUser && <form onSubmit={handleCreateUser} className='flex items-end gap-2 p-3 bg-slate-50 dark:bg-slate-800/60 rounded-xl'>
            <div className='flex-1'><label className='text-xs text-slate-500 mb-1 block'>Логин</label><input value={newUser.username} onChange={e => setNewUser({ ...newUser, username: e.target.value })} className='input-field text-sm py-2' required /></div>
            <div className='flex-1'><label className='text-xs text-slate-500 mb-1 block'>Пароль</label><div className='relative'><input type={showUserPassword ? 'text' : 'password'} value={newUser.password} onChange={e => setNewUser({ ...newUser, password: e.target.value })} className='input-field text-sm py-2 pr-10' required /><button type='button' onClick={() => setShowUserPassword(v => !v)} className='absolute right-2 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600 dark:hover:text-slate-200' title={showUserPassword ? 'Скрыть пароль' : 'Показать пароль'}>{showUserPassword ? <EyeOff className='w-4 h-4' /> : <Eye className='w-4 h-4' />}</button></div></div>
            <div><label className='text-xs text-slate-500 mb-1 block'>Роль</label><select value={newUser.role} onChange={e => setNewUser({ ...newUser, role: e.target.value })} className='input-field text-sm py-2'><option value='user'>Пользователь</option><option value='admin'>Администратор</option></select></div>
            <button type='submit' disabled={savingUser} className='btn-primary text-sm py-2'>{savingUser ? '...' : 'Создать'}</button>
          </form>}
          <div className='space-y-2'>
            {users.map(u => (
              <div key={u.id} className='flex items-center gap-3 p-3 bg-slate-50 dark:bg-slate-800/60 rounded-xl'>
                <Shield className={'w-5 h-5 ' + (u.role === 'admin' ? 'text-primary-500' : 'text-slate-400')} />
                <div className='flex-1'><p className='text-sm font-medium text-slate-700 dark:text-slate-200'>{u.username}</p><p className='text-xs text-slate-400'>{u.role === 'admin' ? 'Администратор' : 'Пользователь'} {!u.is_active && '(заблокирован)'}</p></div>
                <button onClick={() => handleResetPassword(u)} className='p-2 hover:bg-slate-100 dark:hover:bg-slate-800 rounded-xl' title='Сбросить пароль'><KeyRound className='w-4 h-4 text-blue-500' /></button>
                <button onClick={() => handleToggleUserStatus(u)} className='p-2 hover:bg-slate-100 dark:hover:bg-slate-800 rounded-xl' title={u.is_active ? 'Заблокировать' : 'Разблокировать'}>{u.is_active ? <XCircle className='w-4 h-4 text-amber-500' /> : <CheckCircle className='w-4 h-4 text-emerald-500' />}</button>
                <button onClick={() => handleDeleteUser(u)} className='p-2 hover:bg-red-50 dark:hover:bg-red-950/40 rounded-xl' title='Удалить'><Trash2 className='w-4 h-4 text-red-500' /></button>
              </div>
            ))}
          </div>
        </div>}

        {isAdmin && <div className='flex items-center gap-4'>
          <button type='submit' disabled={saving} className='btn-primary flex items-center gap-2'><Save className='w-5 h-5' />{saving ? 'Сохранение...' : 'Сохранить настройки'}</button>
          {saved && <span className='text-sm text-emerald-600 font-medium'>Настройки сохранены!</span>}
        </div>}
      </form>
    </div>
  )
}
