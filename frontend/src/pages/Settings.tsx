import { Trash2 } from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import { call, clock, usePoll } from '../api'
import { useApp } from '../main'
import { useAct } from '../ui'

type V = Record<string, string | number | boolean>
type S = {
  values: V; limits: Record<string, [number, number]>; network: { host: string; port: number; lan_url: string; bonjour_url: string }
  library_dir: string; data_dir: string; providers: { name: string; label: string; enabled: boolean; last_run: number }[]
  links: { id: number; number: number; name: string; url: string }[]
}

export default function Settings() {
  const { data, reload } = usePoll<S>('/api/settings', 60000)
  const { status } = useApp()
  const [v, setV] = useState<V | null>(null)
  const [link, setLink] = useState({ number: '', name: '', url: '' })
  const act = useAct()
  useEffect(() => { if (data && !v) setV(data.values) }, [data])
  if (!data || !v) return <div className="loading">Loading settings</div>
  const changed = Object.fromEntries(Object.entries(v).filter(([k, x]) => x !== data.values[k]))
  const num = (k: string, label: string, hint?: string) => (
    <label className="set"><span>{label}{hint && <small>{hint}</small>}</span><input type="number" min={data.limits[k]?.[0]} max={data.limits[k]?.[1]} value={v[k] as number} onChange={(e) => setV({ ...v, [k]: +e.target.value })} /></label>)
  const bool = (k: string, label: string, hint?: string) => (
    <label className="set"><span>{label}{hint && <small>{hint}</small>}</span><input type="checkbox" role="switch" checked={v[k] as boolean} onChange={(e) => setV({ ...v, [k]: e.target.checked })} /></label>)
  const pick = (k: string, label: string, opts: [string | number, string][], hint?: string) => (
    <label className="set"><span>{label}{hint && <small>{hint}</small>}</span><select value={v[k] as string} onChange={(e) => setV({ ...v, [k]: typeof opts[0][0] === 'number' ? +e.target.value : e.target.value })}>{opts.map(([o, t]) => <option key={o} value={o}>{t}</option>)}</select></label>)
  const text = (k: string, label: string, hint?: string, type = 'text') => (
    <label className="set wide"><span>{label}{hint && <small>{hint}</small>}</span><input type={type} autoComplete="off" value={v[k] as string} onChange={(e) => setV({ ...v, [k]: e.target.value })} /></label>)
  const Group = ({ title, children }: { title: string; children: ReactNode }) => <section className="panel"><h3>{title}</h3>{children}</section>
  return (
    <>
      <div className="page-head"><h1>Settings</h1>
        <button className="primary" disabled={!Object.keys(changed).length} onClick={() => act(() => call('/api/settings', 'PATCH', changed), 'Settings saved', async () => { await reload(); setV(null) })}>Save changes</button></div>
      <div className="masonry">
        {Group({ title: 'Storage', children: <>{num('cache_limit_gb', 'Cache limit (GB)', 'Most space videos may use')}{num('min_free_gb', 'Always leave free (GB)')}{bool('auto_cleanup', 'Delete watched titles automatically')}{num('cleanup_delay_min', 'Wait before deleting (minutes)')}{num('rerun_reserve_movies', 'Finished movies kept for reruns')}{num('rerun_reserve_episodes', 'Finished episodes kept for reruns')}</> })}
        {Group({ title: 'Downloads and quality', children: <>{num('max_downloads', 'Downloads at once')}{pick('max_height', 'Maximum resolution', [[480, '480p'], [720, '720p'], [1080, '1080p'], [2160, '4K']])}{pick('min_height', 'Minimum resolution', [[0, 'Any'], [320, 'Skip tiny 240p copies'], [480, '480p'], [720, '720p']], 'For Internet Archive titles')}{pick('video_policy', 'Video', [['auto', 'Original when compatible'], ['always', 'Always convert']], 'Originals use far less power')}{bool('normalize_audio', 'Even out volume between titles')}</> })}
        {Group({ title: 'Scheduling', children: <>{num('plan_ahead_hours', 'Plan ahead (hours)', 'Length of the program guide')}{pick('after_outage', 'After a shutdown', [['resume', 'Resume the interrupted program'], ['live', 'Start the next program']])}<p className="note">Queue sizes, order and time blocks are set per channel on the Channels page.</p></> })}
        {Group({ title: 'Sources', children: <>{bool('archive_enabled', 'Internet Archive', 'Public-domain films and sitcoms, no account needed')}{num('archive_max_year', 'Newest release year')}{num('archive_max_titles', 'Titles to scan')}{bool('include_silent', 'Include silent films')}{num('discovery_hours', 'Look for new titles every (hours)')}
          <p className="note">Your own videos: put files in <code>{data.library_dir}</code> under the matching category folder. They are never changed or deleted.</p>
          <ul className="lines">{data.providers.map((p) => <li key={p.name}><span><b>{p.label}</b></span><time>{p.last_run > 1e9 ? `checked ${clock(p.last_run)}` : ''}</time></li>)}</ul></> })}
        {Group({ title: 'Posters and details', children: <>{text('tmdb_api_key', 'TMDB API key', 'Optional and free. Adds real movie posters.', 'password')}</> })}
        {Group({ title: 'Network', children: <><dl className="net"><dt>Apple TV</dt><dd><code>{data.network.lan_url}/playlist.m3u</code></dd><dt>Survives IP changes</dt><dd><code>{data.network.bonjour_url}/playlist.m3u</code></dd><dt>Guide</dt><dd><code>{data.network.lan_url}/epg.xml</code></dd><dt>Listening on</dt><dd><code>{data.network.host}:{data.network.port}</code></dd></dl>{text('public_url', 'Fixed address', 'Leave empty to use whatever address the player connects with')}<p className="note">Port and data folder are set in the .env file.</p></> })}
        {Group({ title: 'Pass-through channels', children: <><p className="note">Add a live stream you have rights to use. It appears as an extra channel on the Apple TV and nothing is downloaded.</p>
          <ul className="lines">{data.links.map((l) => <li key={l.id}><span><b>{l.number} {l.name}</b><small>{l.url}</small></span><button className="icon" aria-label={`Remove ${l.name}`} onClick={() => act(() => call(`/api/links/${l.id}`, 'DELETE'), 'Removed', reload)}><Trash2 size={16} /></button></li>)}</ul>
          <form className="linkform" onSubmit={(e) => { e.preventDefault(); act(() => call('/api/links', 'POST', { ...link, number: +link.number }), 'Channel added. Refresh the playlist on the Apple TV.', () => { setLink({ number: '', name: '', url: '' }); reload() }) }}>
            <div className="two num-name"><input required type="number" min={1} placeholder="No." value={link.number} onChange={(e) => setLink({ ...link, number: e.target.value })} /><input required placeholder="Name" value={link.name} onChange={(e) => setLink({ ...link, name: e.target.value })} /></div>
            <input required type="url" placeholder="https://…/stream.m3u8" value={link.url} onChange={(e) => setLink({ ...link, url: e.target.value })} /><button>Add channel</button></form></> })}
      </div>
      <p className="dim foot">Data folder: {data.data_dir}. {status ? `${status.titles} titles in rotation.` : ''}</p>
    </>
  )
}
