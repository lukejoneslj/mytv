import { bytes, call, clock, name, sub, usePoll, type DownloadItem, type Storage } from '../api'
import { useApp } from '../main'
import { accent, Bar, Empty, Poster, useAct } from '../ui'

type D = { active: DownloadItem[]; upcoming: DownloadItem[]; failed: DownloadItem[]; ready: DownloadItem[]; storage: Storage; paused: string | null }
type Ev = { id: number; ts: number; level: string; source: string; message: string }

export default function Downloads() {
  const { data, reload } = usePoll<D>('/api/downloads', 2000)
  const { data: events } = usePoll<Ev[]>('/api/events?limit=40', 6000)
  const { status } = useApp()
  const act = useAct()
  if (!data || !status) return <div className="loading">Loading downloads</div>
  const s = data.storage
  const used = s.cache_bytes + s.download_bytes
  const ch = (m: DownloadItem) => status.channels.find((c) => c.id === m.channel_id)
  const speed = data.active.reduce((n, m) => n + (m.status === 'downloading' ? m.speed : 0), 0)
  return (
    <>
      <div className="page-head"><h1>Downloads</h1></div>
      <div className="stats">
        <div><label>Cache</label><b>{bytes(used)}</b><span>of {bytes(s.limit_bytes)}</span><Bar value={used / s.limit_bytes} label="Cache used" /></div>
        <div><label>Disk free</label><b>{bytes(s.free_bytes)}</b><span>keeps {bytes(s.min_free_bytes)} free</span><Bar value={1 - s.free_bytes / s.total_bytes} label="Disk used" /></div>
        <div><label>Speed</label><b>{(speed / 1e6).toFixed(1)} MB/s</b><span>{data.active.length} in progress</span></div>
        <div><label>Ready to air</label><b>{data.ready.length}</b><span>{data.upcoming.length} more lined up</span></div>
      </div>
      <div className="cols">
        <section>
          <div className="panel"><h3>In progress</h3>
            <ul className="jobs">{data.active.map((m) => {
              const c = ch(m)
              return <li key={m.id} style={c && accent(c)}><Poster m={m} /><div><b>{name(m)}</b><small>{sub(m)}</small>
                <Bar value={m.status === 'downloaded' ? 1 : m.progress} label="Progress" />
                <small>{m.status === 'downloading' ? `${bytes(m.bytes_done)}${m.size ? ` of ${bytes(m.size)}` : ''} at ${(m.speed / 1e6).toFixed(1)} MB/s` : m.status === 'processing' ? `Preparing for broadcast, ${Math.round(m.progress * 100)}%` : 'Downloaded and verified, waiting to be prepared'}{m.needed_at ? `, airs ${clock(m.needed_at)}` : ''}</small></div></li>
            })}{!data.active.length && <Empty>Everything needed is already on disk.</Empty>}</ul></div>
          <div className="panel"><h3>Next up</h3>
            <ul className="lines">{data.upcoming.map((m) => <li key={m.id}><span><b>{name(m)}</b><small>{ch(m)?.name}{m.error ? ` · ${m.error}` : ''}</small></span><span>{m.size ? bytes(m.size) : ''}</span><time>{m.needed_at ? clock(m.needed_at) : ''}</time></li>)}
              {!data.upcoming.length && <Empty>No downloads waiting.</Empty>}</ul></div>
          {data.failed.length > 0 && <div className="panel"><h3>Failed</h3>
            <ul className="lines">{data.failed.map((m) => <li key={m.id}><span><b>{name(m)}</b><small className="bad">{m.error}</small></span><button onClick={() => act(() => call(`/api/downloads/${m.id}/retry`, 'POST'), 'Will be tried again', reload)}>Retry</button></li>)}</ul></div>}
        </section>
        <section>
          <div className="panel"><h3>On disk</h3>
            <ul className="lines">{data.ready.map((m) => <li key={m.id}><span><b>{name(m)}</b><small>{sub(m)}</small></span><span>{bytes(m.disk_bytes)}</span><button onClick={() => act(() => call(`/api/downloads/${m.id}`, 'DELETE'), 'Video deleted', reload)}>Delete</button></li>)}
              {!data.ready.length && <Empty>No titles ready yet.</Empty>}</ul></div>
          <div className="panel"><h3>Activity</h3>
            <ul className="log">{events?.map((e) => <li key={e.id} className={e.level}><time>{clock(e.ts)}</time><span>{e.message}</span></li>)}</ul></div>
        </section>
      </div>
    </>
  )
}
