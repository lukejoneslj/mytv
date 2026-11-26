// Streams on this page: one per live channel, each fed by a tab, window or screen picked in Chrome's share dialog.
import { useSyncExternalStore } from 'react'
import type { Msg, Stats } from './worker'

export type Quality = { id: string; label: string; width: number; height: number; fps: number; bitrate: number }
export const QUALITIES: Quality[] = [
  { id: '1080p30', label: '1080p · films, TV, most videos', width: 1920, height: 1080, fps: 30, bitrate: 8_000_000 },
  { id: '1080p60', label: '1080p 60 fps · sports, games (up to 3 at once)', width: 1920, height: 1080, fps: 60, bitrate: 12_000_000 },
  { id: '720p30', label: '720p · lightest', width: 1280, height: 720, fps: 30, bitrate: 4_500_000 },
]

export type Cast = {
  channel: number; session: string; title: string; surface: string; audio: boolean; preview: MediaStream; started: number
  stats: Stats | null; listening: boolean; ending: boolean; fps: number
}

export const supported = () =>
  window.isSecureContext && !!navigator.mediaDevices?.getDisplayMedia && 'MediaStreamTrackProcessor' in window && 'VideoEncoder' in window && 'AudioEncoder' in window

let casts: Cast[] = []
const workers = new Map<number, { w: Worker; tracks: MediaStreamTrack[] }>()
const subs = new Set<() => void>()
let ended: (ch: number, reason: string, refused: boolean) => void = () => {}

function set(next: Cast[]) { casts = next; subs.forEach((f) => f()) }
function update(ch: number, f: (c: Cast) => Partial<Cast>) { set(casts.map((c) => (c.channel === ch ? { ...c, ...f(c) } : c))) }

export function useCasts() {
  return useSyncExternalStore((f) => { subs.add(f); return () => subs.delete(f) }, () => casts)
}
export function onEnded(f: typeof ended) { ended = f }

declare const MediaStreamTrackProcessor: { new (o: { track: MediaStreamTrack; maxBufferSize?: number }): { readable: ReadableStream } }

export async function goLive(channel: number, title: string, q: Quality) {
  if (workers.has(channel)) return
  const stream = await navigator.mediaDevices.getDisplayMedia({
    video: { width: { ideal: q.width, max: q.width }, height: { ideal: q.height, max: q.height }, frameRate: { ideal: q.fps, max: q.fps }, displaySurface: 'browser' },
    audio: { suppressLocalAudioPlayback: true, echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 2, sampleRate: 48000 },
    selfBrowserSurface: 'exclude', surfaceSwitching: 'include', systemAudio: 'include', windowAudio: 'window', monitorTypeSurfaces: 'include',
    preferCurrentTab: false,
  } as DisplayMediaStreamOptions)
  const [v] = stream.getVideoTracks()
  const [a] = stream.getAudioTracks()
  v.contentHint = 'motion'
  const surface = (v.getSettings() as { displaySurface?: string }).displaySurface ?? 'browser'
  const w = new Worker(new URL('./worker.ts', import.meta.url), { type: 'module', name: `stream-${channel}` })
  const session = crypto.randomUUID().replace(/-/g, '')
  const video = new MediaStreamTrackProcessor({ track: v, maxBufferSize: 4 }).readable
  const audio = a ? new MediaStreamTrackProcessor({ track: a, maxBufferSize: 50 }).readable : null
  const msg: Msg = { type: 'start', video, audio, url: `/api/cast/${channel}/segments`, session, title, width: q.width, height: q.height, fps: q.fps, bitrate: q.bitrate }
  w.postMessage(msg, audio ? [video, audio] : [video])
  workers.set(channel, { w, tracks: stream.getTracks() })
  set([...casts, { channel, session, title, surface, audio: !!a, preview: stream, started: Date.now(), stats: null, listening: false, ending: false, fps: q.fps }])
  w.onmessage = (e) => {
    const m = e.data
    if (m.type === 'stats') update(channel, () => ({ stats: m }))
    else if (m.type === 'ended') { cleanup(channel); ended(channel, m.reason, m.refused) }
  }
  w.onerror = (e) => { cleanup(channel); ended(channel, `Stream failed: ${e.message}`, false) }
  v.addEventListener('ended', () => stop(channel))
}

export function stop(channel: number) {
  const x = workers.get(channel)
  if (!x) return
  update(channel, () => ({ ending: true }))
  x.tracks.forEach((t) => t.stop())
  x.w.postMessage({ type: 'stop' } satisfies Msg)
  setTimeout(() => { if (workers.get(channel) === x) { cleanup(channel); ended(channel, 'Stopped', false) } }, 8000)
}

function cleanup(channel: number) {
  const x = workers.get(channel)
  if (!x) return
  const session = casts.find((c) => c.channel === channel)?.session
  workers.delete(channel)
  x.tracks.forEach((t) => t.stop())
  setTimeout(() => x.w.terminate(), 500)
  set(casts.filter((c) => c.channel !== channel))
  if (session) stopOnServer(channel, session)
}

export function stopOnServer(channel: number, session: string | null) {
  return fetch(`/api/cast/${channel}/stop`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session }) }).catch(() => {})
}

export function retitle(channel: number, title: string) {
  workers.get(channel)?.w.postMessage({ type: 'title', title } satisfies Msg)
  update(channel, () => ({ title }))
}

/** Hear one stream on this Mac. Only one at a time, so they never play over each other. */
export function listen(channel: number | null) {
  set(casts.map((c) => ({ ...c, listening: c.channel === channel && !c.listening })))
}

window.addEventListener('beforeunload', (e) => { if (workers.size) e.preventDefault() })
