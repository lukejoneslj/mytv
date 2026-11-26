import { Check, Ear, EarOff, MonitorUp, Pencil, Play, Plus, Square, Trash2, TriangleAlert, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { call, usePoll, type LiveChannel } from '../api'
import { goLive, listen, onEnded, QUALITIES, retitle, stop, stopOnServer, supported, useCasts, type Cast } from '../cast'
import { useApp } from '../main'
import { accent, StateTag, useAct, useToast } from '../ui'

function Preview({ c }: { c: Cast }) {
  const ref = useRef<HTMLVideoElement>(null)
  useEffect(() => { if (ref.current) ref.current.srcObject = c.preview }, [c.preview])
  useEffect(() => {
    const v = ref.current
    if (!v) return
    v.muted = !c.listening
    v.play().catch(() => {})
  }, [c.listening])
  return <video ref={ref} autoPlay muted playsInline />
}

function Meter({ level }: { level: number }) {
  const db = level > 0 ? 20 * Math.log10(level) : -90
  return <span className="meter" title="Sound level"><i style={{ width: `${Math.max(0, Math.min(100, ((db + 60) / 60) * 100))}%` }} /></span>
}

function Card({ ch, mine, quality, reload }: { ch: LiveChannel; mine: Cast | undefined; quality: string; reload: () => void }) {
  const { watch } = useApp()
  const act = useAct()
  const toast = useToast()
  const [title, setTitle] = useState('')
  const [edit, setEdit] = useState<{ name: string; number: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const elsewhere = ch.live && !mine
  const s = mine?.stats
  const start = async () => {
    setBusy(true)
    try {
      await goLive(ch.id, title.trim(), QUALITIES.find((q) => q.id === quality) ?? QUALITIES[0])
      reload()
    } catch (e) {
      const err = e as Error
      if (err.name !== 'NotAllowedError') toast(err.message || 'Could not start sharing', true)
    } finally { setBusy(false) }
  }
  const warn = !mine ? null
    : s?.trouble ? s.trouble
    : s && s.skipped > 30 && s.fps < mine.fps * 0.6 ? 'The Mac is not keeping up. Stream fewer at once, or start this one again at plain 1080p.'
    : s?.black ? 'The picture is black. Sites that block screen sharing show up like this; that cannot be changed from here.'
    : !mine.audio && mine.surface === 'browser' ? 'Sound was not shared. Stop, choose the tab again and leave "Also share tab audio" on.'
    : !mine.audio ? 'Shared without sound. For sound, play it in a Chrome tab and share the tab.'
    : mine.surface === 'monitor' ? 'Whole screen: the sound is everything the Mac plays, so only use this for one stream.'
    : null
  return (
    <article className="cast" style={accent(ch)}>
      <header>
        <span className="num">{ch.number}</span>
        {edit ? (
          <form className="rename" onSubmit={(e) => { e.preventDefault(); act(() => call(`/api/cast/${ch.id}`, 'PATCH', { name: edit.name, number: +edit.number }), 'Saved. Refresh the playlist on the Apple TV if the number changed.', () => { setEdit(null); reload() }) }}>
            <input type="number" min={1} max={9999} required value={edit.number} onChange={(e) => setEdit({ ...edit, number: e.target.value })} aria-label="Channel number" />
            <input required maxLength={60} value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.target.value })} aria-label="Channel name" />
            <button className="icon" aria-label="Save"><Check size={17} /></button>
            <button type="button" className="icon" aria-label="Cancel" onClick={() => setEdit(null)}><X size={17} /></button>
          </form>
        ) : (
          <>
            <b className="name">{ch.name}</b>
            <StateTag s={ch.live || mine ? 'on_air' : 'standby'} />
            <span className="tools">
              <button className="icon" aria-label={`Rename ${ch.name}`} onClick={() => setEdit({ name: ch.name, number: String(ch.number) })}><Pencil size={15} /></button>
              <button className="icon" aria-label={`Remove ${ch.name}`} disabled={ch.live || !!mine} onClick={() => confirm(`Remove channel ${ch.number} ${ch.name}?`) && act(() => call(`/api/cast/${ch.id}`, 'DELETE'), 'Channel removed. Refresh the playlist on the Apple TV.', reload)}><Trash2 size={15} /></button>
            </span>
          </>
        )}
      </header>
      <div className="screen">
        {mine ? <Preview c={mine} /> : <div className="ident">{ch.number}</div>}
      </div>
      <div className="cast-body">
        {mine ? (
          <>
            <input placeholder="What's on (shown in the TV guide)" maxLength={200} value={mine.title} onChange={(e) => retitle(ch.id, e.target.value)} />
            <p className="meta">
              <span>{s ? `${s.width}×${s.height}` : 'Starting'}</span>
              {s && <span>{s.fps} fps</span>}
              {s && <span>{(s.kbps / 1000).toFixed(1)} Mbps</span>}
              {mine.audio && s && <Meter level={s.level} />}
              <span>{ch.viewers ? `${ch.viewers} watching` : ''}</span>
            </p>
            {warn && <p className="warnline"><TriangleAlert size={15} /> {warn}</p>}
            <div className="actions">
              <button className="stop" disabled={mine.ending} onClick={() => stop(ch.id)}><Square size={14} fill="currentColor" /> {mine.ending ? 'Stopping' : 'Stop'}</button>
              {mine.audio && <button aria-pressed={mine.listening} onClick={() => listen(ch.id)} title="Hear this stream on the Mac. One at a time.">{mine.listening ? <><EarOff size={15} /> Mute here</> : <><Ear size={15} /> Listen here</>}</button>}
              <button onClick={() => watch(ch)}><Play size={14} /> As the TV sees it</button>
            </div>
          </>
        ) : elsewhere ? (
          <>
            <h3>{ch.title || 'Live'}</h3>
            <p className="dim">Streaming from another dashboard window or tab.</p>
            <div className="actions">
              <button className="stop" onClick={() => act(() => stopOnServer(ch.id, ch.session) as Promise<unknown>, 'Stopped', reload)}><Square size={14} fill="currentColor" /> Stop it</button>
              <button onClick={() => watch(ch)}><Play size={14} /> Watch</button>
            </div>
          </>
        ) : (
          <>
            <input placeholder="What's on (optional)" maxLength={200} value={title} onChange={(e) => setTitle(e.target.value)} />
            <div className="actions"><button className="primary" disabled={busy || !supported()} onClick={start}><MonitorUp size={16} /> Choose a tab to stream</button></div>
          </>
        )}
      </div>
    </article>
  )
}

export default function Stream() {
  const { data, reload } = usePoll<LiveChannel[]>('/api/cast', 3000)
  const mine = useCasts()
  const toast = useToast()
  const act = useAct()
  const [quality, setQuality] = useState(() => localStorage.getItem('mytv.quality') ?? QUALITIES[0].id)
  useEffect(() => { localStorage.setItem('mytv.quality', quality) }, [quality])
  useEffect(() => { onEnded((ch, reason) => { if (reason !== 'Stopped') toast(reason, true); reload() }) }, [])
  const ok = supported()
  const local = location.hostname === 'localhost' || location.hostname === '127.0.0.1'
  return (
    <>
      <div className="page-head"><h1>Stream</h1>
        <label className="inline-select">Quality <select value={quality} onChange={(e) => setQuality(e.target.value)} title="Used for the next stream you start">{QUALITIES.map((q) => <option key={q.id} value={q.id}>{q.label}</option>)}</select></label>
        <button onClick={() => act(() => call('/api/cast', 'POST', {}), 'Channel added. Refresh the playlist on the Apple TV.', reload)}><Plus size={16} /> Add channel</button>
      </div>
      {!ok && (
        <div className="banner">
          {!window.isSecureContext && !local
            ? <>Streaming only works on the Mac running MyTV. Open <code>http://localhost:{location.port || 80}/stream</code> there.</>
            : <>Streaming needs Google Chrome or Microsoft Edge on this Mac.</>}
        </div>
      )}
      <section className="howto">
        <div><b>1</b><p>Play the video in its own Chrome tab. A video file on the Mac works too: drag it onto Chrome.</p></div>
        <div><b>2</b><p>Press <em>Choose a tab to stream</em> on a channel, pick the tab, keep <em>Also share tab audio</em> on, press Share.</p></div>
        <div><b>3</b><p>In that tab press the player's full-screen button. The video then fills the TV while it stays inside its tab here.</p></div>
        <div><b>4</b><p>Repeat for more channels. Each keeps its own sound and is silent on the Mac. Leave this page open.</p></div>
      </section>
      {!data ? <div className="loading">Loading</div> : (
        <div className="casts">{data.map((ch) => <Card key={ch.id} ch={ch} mine={mine.find((c) => c.channel === ch.id)} quality={quality} reload={reload} />)}</div>
      )}
    </>
  )
}
