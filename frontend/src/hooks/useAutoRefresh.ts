import { useEffect, useRef } from 'react'

/**
 * Re-runs ``callback`` on a fixed interval so freshly created data (new events,
 * stream statistics, notifications) shows up without a manual page reload.
 *
 * Polling pauses while the tab is in the background and runs once as soon as the
 * tab becomes visible again; overlapping runs are skipped.
 */
export function useAutoRefresh(callback: () => void | Promise<void>, intervalMs = 15000) {
  const latest = useRef(callback)
  useEffect(() => { latest.current = callback })

  useEffect(() => {
    let busy = false
    const tick = async () => {
      if (busy || document.visibilityState === 'hidden') return
      busy = true
      try { await latest.current() } catch { /* pages handle their own errors */ } finally { busy = false }
    }
    const onVisibility = () => { if (document.visibilityState === 'visible') tick() }
    const id = window.setInterval(tick, intervalMs)
    document.addEventListener('visibilitychange', onVisibility)
    return () => { window.clearInterval(id); document.removeEventListener('visibilitychange', onVisibility) }
  }, [intervalMs])
}
