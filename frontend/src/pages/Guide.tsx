import { useEffect, useRef, useState } from 'react'
import { clock, mins, name, sub, usePoll, type Channel, type Program } from '../api'
import { useApp } from '../main'
import { accent, Poster } from '../ui'

const PX = 5.2 // pixels per minute

export default function Guide() {
  const { data } = usePoll<{ now: number; channels: (Channel & { programs: Program[] })[] }>('/api/guide?hours=24&back=1', 15000)
  const { watch } = useApp()
  const [sel, setSel] = useState<{ p: Program; c: Channel } | null>(null)
  const scroller = useRef<HTMLDivElement>(null)
  const placed = useRef(false)
  const [now, setNow] = useState(Date.now() / 1000)
  useEffect(() => { const i = setInterval(() => setNow(Date.now() / 1000), 10000); return () => clearInterval(i) }, [])
  const origin = data ? Math.floor((data.now - 3600) / 1800) * 1800 : 0
  const jump = () => scroller.current?.scrollTo({ left: ((now - origin) / 60) * PX - 60, behavior: placed.current ? 'smooth' : 'instant' })
  useEffect(() => { if (data && !placed.current) { jump(); placed.current = true } }, [data])
  if (!data) return <div className="loading">Loading the guide</div>
  const x = (t: number) => ((t - origin) / 60) * PX
  const end = data.now + 24 * 3600
  const ticks = Array.from({ length: Math.ceil((end - origin) / 1800) }, (_, i) => origin + i * 1800)
  const shown = sel ?? (() => {
    const c = data.channels[0]
    const p = c?.programs.find((q) => q.start <= now && q.start + q.length > now)
    return p ? { p, c } : null
  })()
  return (
    <div className="guide-page">
      <div className="page-head"><h1>Guide</h1><button onClick={jump}>Jump to now</button></div>
      <section className="guide-info" style={shown ? accent(shown.c) : undefined}>
        {shown && (
          <>
            {shown.p.air_kind === 'filler' ? <div className="ident poster">{shown.c.number}</div> : <Poster m={shown.p} />}
            <div>
              <p className="when">{shown.c.number} {shown.c.name} · {clock(shown.p.start)} to {clock(shown.p.start + shown.p.length)} · {mins(shown.p.length)}{shown.p.booked ? '' : ' · estimated'}</p>
              <h2>{shown.p.air_kind === 'filler' ? 'Station card' : name(shown.p)}</h2>
              <p className="sub">{sub(shown.p)}</p>
              <p className="desc">{shown.p.description}</p>
              {shown.p.start <= now && shown.p.start + shown.p.length > now && <button className="primary" onClick={() => watch(shown.c)}>Watch now</button>}
            </div>
          </>
        )}
      </section>
      <div className="guide">
        <div className="guide-ch">
          <div className="corner" />
          {data.channels.map((c) => <button key={c.id} style={accent(c)} onClick={() => watch(c)} aria-label={`Watch ${c.name}`}><b>{c.number}</b><span>{c.name}</span></button>)}
        </div>
        <div className="guide-scroll" ref={scroller}>
          <div className="guide-inner" style={{ width: x(end) }}>
            <div className="ticks">{ticks.map((t) => <span key={t} style={{ left: x(t) }}>{clock(t)}</span>)}</div>
            {data.channels.map((c) => (
              <div className="row" key={c.id} style={accent(c)}>
                {c.programs.map((p) => {
                  const left = Math.max(0, x(p.start)), right = Math.min(x(end), x(p.start + p.length))
                  const live = p.start <= now && p.start + p.length > now
                  return (
                    <button key={`${p.id}-${p.start}`} className={`prog${live ? ' live' : ''}${p.start + p.length < now ? ' past' : ''}${shown?.p.id === p.id && shown.p.start === p.start ? ' sel' : ''}${p.air_kind === 'filler' ? ' filler' : ''}`}
                      style={{ left, width: Math.max(4, right - left - 3) }} onClick={() => setSel({ p, c })} onFocus={() => setSel({ p, c })}>
                      <span className="stick"><b>{p.air_kind === 'filler' ? 'Station card' : name(p)}</b><small>{p.show ? p.title : p.year} · {mins(p.length)}</small></span>
                    </button>
                  )
                })}
              </div>
            ))}
            <div className="nowline" style={{ left: x(now) }} />
          </div>
        </div>
      </div>
    </div>
  )
}
