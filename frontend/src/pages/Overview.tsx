import { Copy, Eye, MonitorUp, Play } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { clock, hhmm, mins, name, sub, type ChannelStatus } from '../api'
import { useApp } from '../main'
import { accent, Bar, Poster, StateTag, useToast } from '../ui'

function Card({ c, skew, t }: { c: ChannelStatus; skew: number; t: number }) {
  const { watch } = useApp()
  const p = c.now
  const filler = p?.air_kind === 'filler'
  const elapsed = p ? t + skew - p.start : 0
  const next = c.next.filter((n) => n.air_kind !== 'filler')[0]
  return (
    <article className="onair" style={accent(c)}>
      <div className="art">
        {filler || !p ? <div className="ident">{c.number}</div> : <Poster m={p} />}
        <button className="watch" onClick={() => watch(c)}><Play size={16} fill="currentColor" /> Watch</button>
      </div>
      <div className="body">
        <header>
          <span className="num">{c.number}</span>
          <Link to={`/channels/${c.id}`}>{c.name}</Link>
          <StateTag s={c.state} />
        </header>
        {p && !filler ? (
          <>
            <h2>{name(p)}</h2>
            <p className="sub">{sub(p)}</p>
            <p className="desc">{p.description}</p>
            <Bar value={elapsed / p.length} label="Playback progress" />
            <div className="times"><span>{hhmm(elapsed)}</span><span>{mins(p.length)}</span><span>ends {clock(p.start + p.length)}</span></div>
          </>
        ) : (
          <>
            <h2>{c.queue_length ? 'First program is downloading' : 'Looking for titles'}</h2>
            <p className="desc">The station card is on screen. Programming starts by itself as soon as a title is ready.</p>
          </>
        )}
        <footer>
          <div><label>Next</label>{next ? <span><b>{clock(next.start)}</b> {name(next)}</span> : <span className="dim">Nothing planned yet</span>}</div>
          <div><label>Queue</label><span>{c.queue_ready} ready of {c.queue_length}</span></div>
          <div className="viewers" title="Screens watching now"><Eye size={15} /> {c.viewers}</div>
        </footer>
      </div>
    </article>
  )
}

function LiveStrip() {
  const { status, watch } = useApp()
  const casts = status?.casts ?? []
  const live = casts.filter((c) => c.live)
  return (
    <section className="livestrip">
      <label>From this Mac</label>
      {live.length ? live.map((c) => (
        <button key={c.id} style={accent(c)} onClick={() => watch(c)}>
          <b>{c.number}</b><span>{c.title || c.name}</span><Eye size={14} /> {c.viewers}
        </button>
      )) : <span className="dim">{casts.length} live channels, nothing streaming.</span>}
      <Link to="/stream"><MonitorUp size={15} /> Stream a tab</Link>
    </section>
  )
}

export default function Overview() {
  const { status } = useApp()
  const toast = useToast()
  const [t, setT] = useState(Date.now() / 1000)
  useEffect(() => { const i = setInterval(() => setT(Date.now() / 1000), 1000); return () => clearInterval(i) }, [])
  const [skew, setSkew] = useState(0)
  useEffect(() => { if (status) setSkew(status.now - Date.now() / 1000) }, [status])
  if (!status) return <div className="loading">Tuning in</div>
  const url = `${status.lan_url}/playlist.m3u`
  const copy = async () => {
    try { await navigator.clipboard.writeText(url) } catch {
      const a = document.createElement('textarea'); a.value = url; document.body.append(a); a.select(); document.execCommand('copy'); a.remove()
    }
    toast('Playlist address copied')
  }
  return (
    <>
      <div className="grid4">{status.channels.map((c) => <Card key={c.id} c={c} skew={skew} t={t} />)}</div>
      <LiveStrip />
      <div className="tune">
        <div><label>Apple TV playlist</label><code>{url}</code></div>
        <div><label>Guide</label><code>{status.lan_url}/epg.xml</code></div>
        <button onClick={copy}><Copy size={15} /> Copy playlist</button>
      </div>
    </>
  )
}
