import { useCallback, useEffect, useRef, useState } from 'react'

export type Media = {
  id: number; provider: string; kind: 'movie' | 'episode' | 'filler'; title: string; show: string | null; season: number | null
  episode: number | null; year: number | null; description: string; genres: string[]; poster: string | null; length: number
  category: string | null; status: string; progress: number; bytes_done: number; size: number | null; speed: number
  error: string | null; attempts: number; next_retry: number; disk_bytes: number; width: number | null; height: number | null; direct: number | null
}
export type Program = Media & { start: number; air_kind: 'program' | 'rerun' | 'filler'; booked: boolean }
export type Block = { name: string; start: string; end: string; days: number[]; match: string; year_from: number | null; year_to: number | null }
export type Channel = {
  id: number; number: number; name: string; category: string; kind: string; mode: string; queue_target: number; accent: string
  blocks: Block[]; logo: string; stream: string
}
export type ChannelStatus = Channel & {
  state: 'on_air' | 'rerun' | 'standby' | 'off_air'; now: Program | null; next: Program[]; queue_length: number; queue_ready: number
  elapsed: number; viewers: number
}
export type LiveChannel = {
  id: number; number: number; name: string; accent: string; logo: string; stream: string; live: boolean; session: string | null
  started: number | null; title: string; viewers: number; info: { kbps?: number; note?: string }
}
export type Storage = { cache_bytes: number; download_bytes: number; limit_bytes: number; free_bytes: number; total_bytes: number; min_free_bytes: number }
export type Status = {
  now: number; channels: ChannelStatus[]; storage: Storage; paused: string | null; lan_url: string; active: number; failed: number
  titles: number; pending_metadata: number; casts: LiveChannel[]
}
export type QueueItem = Media & { queue_id: number; start: number; ahead: boolean; added_by: string }
export type ChannelDetail = ChannelStatus & { queue: QueueItem[]; history: Program[] }
export type LibraryItem = Media & { plays: number; last_aired: number | null; queued_on: number | null }
export type DownloadItem = Media & { channel_id: number | null; needed_at: number | null }

export async function call<T = unknown>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const r = await fetch(path, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined })
  if (!r.ok) {
    let msg = `Request failed (${r.status})`
    try {
      const d = (await r.json()).detail
      msg = typeof d === 'string' ? d : d?.[0] ? `${d[0].loc?.at(-1)}: ${d[0].msg}` : msg
    } catch { /* not json */ }
    throw new Error(msg)
  }
  return r.json()
}

/** Poll a JSON endpoint; pauses while the tab is hidden. */
export function usePoll<T>(path: string | null, every: number) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const current = useRef(path)
  current.current = path
  const load = useCallback(async () => {
    if (!path) return
    try {
      const d = await call<T>(path)
      if (current.current === path) { setData(d); setError(null) }
    } catch (e) {
      if (current.current === path) setError((e as Error).message)
    }
  }, [path])
  useEffect(() => {
    load()
    const t = setInterval(() => { if (!document.hidden) load() }, every)
    const v = () => { if (!document.hidden) load() }
    document.addEventListener('visibilitychange', v)
    return () => { clearInterval(t); document.removeEventListener('visibilitychange', v) }
  }, [load, every])
  return { data, error, reload: load }
}

export const clock = (t: number) => new Date(t * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
export const hhmm = (s: number) => {
  s = Math.max(0, Math.floor(s))
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = String(s % 60).padStart(2, '0')
  return h ? `${h}:${String(m).padStart(2, '0')}:${x}` : `${m}:${x}`
}
export const mins = (s: number) => {
  const m = Math.max(1, Math.round(s / 60))
  return m >= 60 ? `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m` : `${m}m`
}
export const bytes = (n: number) => (n >= 1e12 ? `${(n / 1e12).toFixed(2)} TB` : n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : `${Math.round(n / 1e6)} MB`)
export const name = (m: Media) => m.show ?? m.title
export const sub = (m: Media) =>
  m.show ? (m.season && m.episode ? `S${m.season} E${m.episode} · ${m.title}` : m.title) : [m.year, m.genres.slice(0, 2).join(', ')].filter(Boolean).join(' · ')
export const CATEGORY: Record<string, string> = {
  'classic-movies': 'Classic Movies', 'comedy-movies': 'Comedy Movies', sitcoms: 'Sitcoms', 'mystery-thrillers': 'Mystery & Thrillers',
}
