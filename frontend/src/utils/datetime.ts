// Единое форматирование даты/времени: все отметки времени хранятся на бэкенде
// в UTC (naive), поэтому перед отображением они явно приводятся к московскому
// времени (Europe/Moscow). Используйте эти хелперы везде в UI.

export const MSK_TIMEZONE = 'Europe/Moscow'

/**
 * Разбирает строку даты из API в объект Date.
 * Бэкенд отдаёт UTC без указания зоны (например "2026-09-11T11:45:08.490000"),
 * поэтому при отсутствии смещения добавляем 'Z'.
 */
export function parseUtc(value?: string | null): Date | null {
  if (!value) return null
  let s = value.trim().replace(' ', 'T')
  const hasTimezone = /(Z|[+-]\d{2}:?\d{2})$/.test(s)
  // JS Date надёжно понимает максимум миллисекунды — обрезаем лишние разряды.
  s = s.replace(/\.(\d{3})\d+/, '.$1')
  const date = new Date(hasTimezone ? s : s + 'Z')
  return Number.isNaN(date.getTime()) ? null : date
}

/** Форматирует значение по московскому времени с произвольными опциями Intl. */
export function formatMoscow(value?: string | null, options?: Intl.DateTimeFormatOptions): string {
  const date = parseUtc(value)
  if (!date) return ''
  return date.toLocaleString('ru-RU', { timeZone: MSK_TIMEZONE, ...options })
}

/** Дата и время по Москве: 11.09.2026, 14:45 */
export function formatMoscowDateTime(value?: string | null): string {
  return formatMoscow(value, {
    day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

/** Только дата по Москве: 11.09.2026 */
export function formatMoscowDate(value?: string | null): string {
  return formatMoscow(value, { day: '2-digit', month: '2-digit', year: 'numeric' })
}
