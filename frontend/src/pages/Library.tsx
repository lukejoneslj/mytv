import { Search, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { bytes, call, CATEGORY, mins, name, sub, usePoll, type LibraryItem } from '../api'
import { useApp } from '../main'
import { Empty, Poster, useAct } from '../ui'

const ST: Record<string, string> = { discovered: 'Not downloaded', downloading: 'Downloading', downloaded: 'Waiting', processing: 'Preparing', ready: 'On disk', failed: 'Failed', hidden: 'Hidden' }
const PAGE = 90

export default function Library() {
  const { status } = useApp()
  const [cat, setCat] = useState('')
  const [st, setSt] = useState('')
  const [q, setQ] = useState('')
  const [term, setTerm] = useState('')
  const [limit, setLimit] = useState(PAGE)
  const [open, setOpen] = useState<LibraryItem | null>(null)
  const act = useAct()
  useEffect(() => { const t = setTimeout(() => setTerm(q), 250); return () => clearTimeout(t) }, [q])
  useEffect(() => setLimit(PAGE), [cat, st, term])
  const { data, reload } = usePoll<{ total: number; items: LibraryItem[] }>(`/api/library?category=${cat}&status=${st}&q=${encodeURIComponent(term)}&limit=${limit}`, 10000)
  const ch = open && status?.channels.find((c) => c.category === open.category)
  const patch = (body: unknown, msg: string) => open && act(() => call(`/api/library/${open.id}`, 'PATCH', body), msg, () => { setOpen(null); reload() })
  return (
    <>
      <div className="page-head"><h1>Library</h1><span className="dim">{data ? `${data.total} titles` : ''}{status?.pending_metadata ? `, ${status.pending_metadata} being looked up` : ''}</span>
        <button onClick={() => act(() => call('/api/discover', 'POST'), 'Checking sources for new titles')}>Check for new titles</button></div>
      <div className="filters">
        <div className="search"><Search size={16} /><input aria-label="Search library" placeholder="Title, show, genre or year" value={q} onChange={(e) => setQ(e.target.value)} /></div>
        <select aria-label="Category" value={cat} onChange={(e) => setCat(e.target.value)}><option value="">All categories</option>{Object.entries(CATEGORY).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
        <select aria-label="Status" value={st} onChange={(e) => setSt(e.target.value)}><option value="">Any status</option>{Object.entries(ST).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
      </div>
      <div className="wall">
        {data?.items.map((m) => (
          <button key={m.id} className="tile" onClick={() => setOpen(m)}>
            <Poster m={m} />
            <b>{name(m)}</b><small>{m.show ? m.title : [m.year, m.genres[0], mins(m.length)].filter(Boolean).join(' · ')}</small>
            <small className={`st ${m.status}`}>{ST[m.status]}{m.plays ? ` · aired ${m.plays}×` : ''}</small>
          </button>
        ))}
      </div>
      {data && !data.items.length && <Empty>No titles match these filters.</Empty>}
      {data && data.items.length < data.total && <button className="more" onClick={() => setLimit(limit + PAGE)}>Show more</button>}
      {open && (
        <div className="modal" onClick={() => setOpen(null)}>
          <div className="sheet" role="dialog" aria-label={name(open)} onClick={(e) => e.stopPropagation()}>
            <Poster m={open} />
            <div>
              <button className="icon close" aria-label="Close" onClick={() => setOpen(null)}><X size={20} /></button>
              <h2>{name(open)}</h2><p className="sub">{sub(open)}</p><p className="desc">{open.description || 'No description.'}</p>
              <dl>
                <dt>Runtime</dt><dd>{mins(open.length)}</dd><dt>Genres</dt><dd>{open.genres.join(', ') || 'Unknown'}</dd>
                <dt>Status</dt><dd>{ST[open.status]}{open.error ? `: ${open.error}` : ''}</dd>
                {open.disk_bytes ? <><dt>On disk</dt><dd>{bytes(open.disk_bytes)}{open.height ? `, ${open.height}p` : ''}{open.direct ? ', original video' : ', converted'}</dd></> : null}
                <dt>Aired</dt><dd>{open.plays ? `${open.plays}×, last ${new Date(open.last_aired! * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })}` : 'Not yet'}</dd>
              </dl>
              <label className="inline">Category<select value={open.category ?? ''} onChange={(e) => patch({ category: e.target.value }, 'Category changed')}>{Object.entries(CATEGORY).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
              <div className="actions">
                {open.status === 'hidden' ? <button className="primary" onClick={() => patch({ hidden: false }, 'Back in rotation')}>Put back in rotation</button> : <>
                  {ch && <button className="primary" onClick={() => act(() => call(`/api/channels/${ch.id}/queue`, 'POST', { media_id: open.id, front: true }), `Plays next on ${ch.name}`, () => { setOpen(null); reload() })}>Play next on {ch.name}</button>}
                  <button onClick={() => patch({ hidden: true }, 'Hidden')}>Never air this</button></>}
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
