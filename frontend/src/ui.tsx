import Hls from 'hls.js'
import { X } from 'lucide-react'
import { createContext, useContext, useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { name, type Channel, type Media } from './api'

const ToastCtx = createContext<(msg: string, bad?: boolean) => void>(() => {})
export const useToast = () => useContext(ToastCtx)
export function Toasts({ children }: { children: ReactNode }) {
  const [list, setList] = useState<{ id: number; msg: string; bad?: boolean }[]>([])
  const push = (msg: string, bad?: boolean) => {
    const id = Date.now() + Math.random()
    setList((l) => [...l, { id, msg, bad }])
    setTimeout(() => setList((l) => l.filter((x) => x.id !== id)), 4500)
  }
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" role="status">{list.map((t) => <div key={t.id} className={t.bad ? 'toast bad' : 'toast'}>{t.msg}</div>)}</div>
    </ToastCtx.Provider>
  )
}

/** Run an action and report its result. */
export function useAct() {
  const toast = useToast()
  return async (fn: () => Promise<unknown>, done?: string, after?: () => void) => {
    try {
      await fn()
      if (done) toast(done)
      after?.()
      return true
    } catch (e) {
      toast((e as Error).message, true)
      return false
    }
  }
}

export function Poster({ m, className = '', lazy = false }: { m: Media | null; className?: string; lazy?: boolean }) {
  const [bad, setBad] = useState(false)
  useEffect(() => setBad(false), [m?.id])
  if (m?.poster && !bad) return <img className={`poster ${className}`} src={m.poster} alt="" loading={lazy ? 'lazy' : 'eager'} onError={() => setBad(true)} />
  return <div className={`poster blank ${className}`}><span>{m ? name(m) : ''}</span>{m?.year ? <small>{m.year}</small> : null}</div>
}

export const accent = (c: { accent: string }) => ({ '--accent': c.accent }) as CSSProperties

export function Bar({ value, label }: { value: number; label?: string }) {
  const v = Math.max(0, Math.min(1, value || 0))
  return <div className="bar" role="progressbar" aria-label={label} aria-valuenow={Math.round(v * 100)}><i style={{ width: `${v * 100}%` }} /></div>
}

export const STATE: Record<string, string> = { on_air: 'On air', rerun: 'Rerun', standby: 'Standing by', off_air: 'Off air' }
export function StateTag({ s }: { s: string }) { return <span className={`state ${s}`}><i />{STATE[s]}</span> }

export type Tunable = Pick<Channel, 'number' | 'name' | 'stream' | 'accent'>

export function Player({ ch, onClose }: { ch: Tunable; onClose: () => void }) {
  const ref = useRef<HTMLVideoElement>(null)
  const [err, setErr] = useState('')
  useEffect(() => {
    const v = ref.current!
    const key = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', key)
    let h: Hls | null = null
    setErr('')
    if (Hls.isSupported()) {
      h = new Hls({ liveSyncDurationCount: 3, liveMaxLatencyDurationCount: 8 })
      h.on(Hls.Events.ERROR, (_, d) => {
        if (!d.fatal) return
        if (d.type === Hls.ErrorTypes.MEDIA_ERROR) h?.recoverMediaError()
        else { setErr('Signal lost, reconnecting'); setTimeout(() => h?.loadSource(ch.stream), 3000) }
      })
      h.on(Hls.Events.FRAG_BUFFERED, () => setErr(''))
      h.loadSource(ch.stream)
      h.attachMedia(v)
    } else v.src = ch.stream
    v.play().catch(() => { v.muted = true; v.play().catch(() => {}) })
    return () => { window.removeEventListener('keydown', key); h?.destroy(); v.removeAttribute('src') }
  }, [ch.stream])
  return (
    <div className="modal" onClick={onClose}>
      <div className="player" style={accent(ch)} onClick={(e) => e.stopPropagation()} role="dialog" aria-label={`Watching ${ch.name}`}>
        <header><b>{ch.number}</b> {ch.name}{err && <em>{err}</em>}<button className="icon" onClick={onClose} aria-label="Close"><X size={20} /></button></header>
        <video ref={ref} controls playsInline />
      </div>
    </div>
  )
}

export function Empty({ children }: { children: ReactNode }) { return <p className="empty">{children}</p> }
