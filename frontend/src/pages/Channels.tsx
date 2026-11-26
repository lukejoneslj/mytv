import { ArrowDown, ArrowUp, Plus, Search, Trash2, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { NavLink, useParams } from 'react-router-dom'
import { call, CATEGORY, clock, mins, name, sub, usePoll, type Block, type Channel, type ChannelDetail, type LibraryItem } from '../api'
import { useApp } from '../main'
import { accent, Empty, Poster, StateTag, useAct } from '../ui'

const DAYS = ['M', 'T', 'W', 'T', 'F', 'S', 'S']

function AddTitle({ ch, onDone }: { ch: Channel; onDone: () => void }) {
  const [q, setQ] = useState('')
  const [term, setTerm] = useState('')
  useEffect(() => { const t = setTimeout(() => setTerm(q), 250); return () => clearTimeout(t) }, [q])
  const { data } = usePoll<{ items: LibraryItem[] }>(`/api/library?category=${ch.category}&q=${encodeURIComponent(term)}&limit=30`, 60000)
  const act = useAct()
  const [link, setLink] = useState({ url: '', title: '', year: '', show: '' })
  const [busy, setBusy] = useState(false)
  const add = (id: number, front: boolean) => act(() => call(`/api/channels/${ch.id}/queue`, 'POST', { media_id: id, front }), front ? 'Plays next' : 'Added to the queue', onDone)
  return (
    <div className="panel">
      <h3>Add a title</h3>
      <div className="search"><Search size={16} /><input placeholder={`Search ${CATEGORY[ch.category]}`} value={q} onChange={(e) => setQ(e.target.value)} /></div>
      <ul className="picks">
        {data?.items.map((m) => (
          <li key={m.id}><span><b>{name(m)}</b><small>{sub(m)}</small></span>
            <button onClick={() => add(m.id, true)}>Play next</button><button onClick={() => add(m.id, false)}>Add</button></li>
        ))}
        {data && !data.items.length && <Empty>No titles match.</Empty>}
      </ul>
      <h3>Add from a direct link</h3>
      <form className="linkform" onSubmit={async (e) => {
        e.preventDefault()
        setBusy(true)
        const ok = await act(() => call('/api/library', 'POST', { url: link.url, title: link.title, category: ch.category, year: link.year ? +link.year : null, show: link.show || null }), 'Added, downloading next', onDone)
        setBusy(false)
        if (ok) setLink({ url: '', title: '', year: '', show: '' })
      }}>
        <input required type="url" placeholder="https://… video file you have rights to use" value={link.url} onChange={(e) => setLink({ ...link, url: e.target.value })} />
        <div className="two">
          <input required placeholder="Title" value={link.title} onChange={(e) => setLink({ ...link, title: e.target.value })} />
          {ch.kind === 'episode' ? <input placeholder="Show" value={link.show} onChange={(e) => setLink({ ...link, show: e.target.value })} />
            : <input placeholder="Year" inputMode="numeric" pattern="\d{4}" value={link.year} onChange={(e) => setLink({ ...link, year: e.target.value })} />}
        </div>
        <button disabled={busy}>{busy ? 'Checking the link' : 'Add link'}</button>
      </form>
    </div>
  )
}

function Blocks({ value, onChange }: { value: Block[]; onChange: (b: Block[]) => void }) {
  const set = (i: number, p: Partial<Block>) => onChange(value.map((b, j) => (j === i ? { ...b, ...p } : b)))
  return (
    <div className="blocks">
      {value.map((b, i) => (
        <div className="block" key={i}>
          <div className="two"><input aria-label="Block name" placeholder="Name, e.g. Noir night" value={b.name} onChange={(e) => set(i, { name: e.target.value })} />
            <button type="button" className="icon" aria-label="Remove block" onClick={() => onChange(value.filter((_, j) => j !== i))}><Trash2 size={16} /></button></div>
          <div className="two"><input type="time" aria-label="From" value={b.start} onChange={(e) => set(i, { start: e.target.value })} /><input type="time" aria-label="To" value={b.end} onChange={(e) => set(i, { end: e.target.value })} /></div>
          <div className="days">{DAYS.map((d, n) => <button type="button" key={n} aria-pressed={b.days.includes(n)} onClick={() => set(i, { days: b.days.includes(n) ? b.days.filter((x) => x !== n) : [...b.days, n].sort() })}>{d}</button>)}</div>
          <input aria-label="Match" placeholder="Genre, show or word to match" value={b.match} onChange={(e) => set(i, { match: e.target.value })} />
          <div className="two"><input inputMode="numeric" placeholder="From year" value={b.year_from ?? ''} onChange={(e) => set(i, { year_from: e.target.value ? +e.target.value || null : null })} /><input inputMode="numeric" placeholder="To year" value={b.year_to ?? ''} onChange={(e) => set(i, { year_to: e.target.value ? +e.target.value || null : null })} /></div>
        </div>
      ))}
      <button type="button" onClick={() => onChange([...value, { name: '', start: '20:00', end: '23:00', days: [0, 1, 2, 3, 4, 5, 6], match: '', year_from: null, year_to: null }])}><Plus size={15} /> Add block</button>
    </div>
  )
}

function Editor({ ch, reload }: { ch: ChannelDetail; reload: () => void }) {
  const pick = (c: ChannelDetail) => ({ name: c.name, number: c.number, category: c.category, mode: c.mode, queue_target: c.queue_target, accent: c.accent, blocks: c.blocks })
  const [f, setF] = useState(pick(ch))
  useEffect(() => setF(pick(ch)), [ch.id])
  const act = useAct()
  const dirty = JSON.stringify(f) !== JSON.stringify(pick(ch))
  return (
    <form className="panel form" onSubmit={(e) => { e.preventDefault(); act(() => call(`/api/channels/${ch.id}`, 'PATCH', f), 'Channel saved', reload) }}>
      <h3>Channel</h3>
      <div className="two num-name"><label>Number<input type="number" min={1} max={9999} value={f.number} onChange={(e) => setF({ ...f, number: +e.target.value })} /></label>
        <label>Name<input required maxLength={60} value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></label></div>
      <label>Category<select value={f.category} onChange={(e) => setF({ ...f, category: e.target.value })}>{Object.entries(CATEGORY).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
      <label>Order<select value={f.mode} onChange={(e) => setF({ ...f, mode: e.target.value })}><option value="shuffle">Shuffled</option><option value="chronological">{ch.kind === 'episode' ? 'By show and episode' : 'By release year'}</option></select></label>
      <div className="two"><label>Titles downloaded ahead<input type="number" min={1} max={50} value={f.queue_target} onChange={(e) => setF({ ...f, queue_target: +e.target.value })} /></label>
        <label>Color<input type="color" value={f.accent} onChange={(e) => setF({ ...f, accent: e.target.value })} /></label></div>
      <h3>Programming blocks</h3>
      <Blocks value={f.blocks} onChange={(blocks) => setF({ ...f, blocks })} />
      <button className="primary" disabled={!dirty}>Save channel</button>
    </form>
  )
}

export default function Channels() {
  const { status, watch } = useApp()
  const { id } = useParams()
  const cid = id ? +id : status?.channels[0]?.id
  const { data: ch, reload } = usePoll<ChannelDetail>(cid ? `/api/channels/${cid}` : null, 4000)
  const act = useAct()
  const [tab, setTab] = useState<'queue' | 'history'>('queue')
  if (!status || !ch || ch.id !== cid) return <div className="loading">Loading channels</div>
  const go = (path: string, body: unknown, msg?: string, method = 'POST') => act(() => call(`/api/channels/${ch.id}${path}`, method, body), msg, reload)
  return (
    <div style={accent(ch)}>
      <div className="chtabs">{status.channels.map((c) => <NavLink key={c.id} to={`/channels/${c.id}`} style={accent(c)} className={c.id === cid ? 'active' : ''}><b>{c.number}</b> {c.name}</NavLink>)}</div>
      <section className="nowbar">
        <Poster m={ch.now?.air_kind === 'filler' ? null : ch.now} />
        <div><StateTag s={ch.state} /><h2>{ch.now && ch.now.air_kind !== 'filler' ? name(ch.now) : 'Station card'}</h2><p className="sub">{ch.now && ch.now.air_kind !== 'filler' ? `${sub(ch.now)} · ends ${clock(ch.now.start + ch.now.length)}` : 'Waiting for the first title'}</p></div>
        <div className="actions"><button className="primary" onClick={() => watch(ch)}>Watch</button>
          <button onClick={() => go('/skip', {}, 'Skipping. The change takes about 45 seconds to reach the TV.')}>Skip program</button>
          <button onClick={() => go('/reshuffle', {}, 'New lineup planned')}>Replan lineup</button></div>
      </section>
      <div className="cols">
        <section className="panel">
          <div className="seg"><button aria-pressed={tab === 'queue'} onClick={() => setTab('queue')}>Up next ({ch.queue_length})</button><button aria-pressed={tab === 'history'} onClick={() => setTab('history')}>Already aired</button></div>
          {tab === 'queue' ? (
            <ol className="queue">
              {ch.queue.map((m, i) => (
                <li key={m.queue_id}>
                  <time>{clock(m.start)}</time><Poster m={m} />
                  <span><b>{name(m)}</b><small>{sub(m)} · {mins(m.length)}</small></span>
                  <em className={`st ${m.status}`}>{m.status === 'ready' ? 'Ready' : m.status === 'downloading' ? `${Math.round(m.progress * 100)}%` : m.status === 'processing' ? 'Preparing' : m.status === 'downloaded' ? 'Waiting' : m.ahead ? 'Next to download' : 'Planned'}</em>
                  <button className="icon" aria-label="Move up" disabled={!i} onClick={() => go('/queue/move', { media_id: m.id, index: i - 1 })}><ArrowUp size={16} /></button>
                  <button className="icon" aria-label="Move down" disabled={i === ch.queue.length - 1} onClick={() => go('/queue/move', { media_id: m.id, index: i + 1 })}><ArrowDown size={16} /></button>
                  <button className="icon" aria-label="Remove" onClick={() => go(`/queue/${m.id}`, undefined, 'Removed', 'DELETE')}><X size={16} /></button>
                </li>
              ))}
              {!ch.queue.length && <Empty>Nothing queued. Titles appear here as soon as they are discovered.</Empty>}
            </ol>
          ) : (
            <ol className="queue">
              {ch.history.map((m) => <li key={`${m.id}-${m.start}`}><time>{clock(m.start)}</time><Poster m={m} /><span><b>{name(m)}</b><small>{sub(m)} · {mins(m.length)}</small></span><em className="st">{m.air_kind === 'rerun' ? 'Rerun' : ''}</em></li>)}
              {!ch.history.length && <Empty>Nothing has finished airing yet.</Empty>}
            </ol>
          )}
        </section>
        <div><Editor ch={ch} reload={reload} /><AddTitle ch={ch} onDone={reload} /></div>
      </div>
    </div>
  )
}
