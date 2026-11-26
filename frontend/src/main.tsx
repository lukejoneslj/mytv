import '@fontsource-variable/archivo/wdth.css'
import { CalendarRange, Download, LayoutGrid, Library as Lib, MonitorUp, Settings as Cog, SlidersHorizontal } from 'lucide-react'
import { createContext, StrictMode, useContext, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, NavLink, Route, Routes } from 'react-router-dom'
import { clock, usePoll, type Status } from './api'
import { useCasts } from './cast'
import Channels from './pages/Channels'
import Downloads from './pages/Downloads'
import Guide from './pages/Guide'
import Library from './pages/Library'
import Overview from './pages/Overview'
import Settings from './pages/Settings'
import Stream from './pages/Stream'
import './style.css'
import { Player, Toasts, type Tunable } from './ui'

type Ctx = { status: Status | null; error: string | null; reload: () => void; watch: (c: Tunable) => void }
const AppCtx = createContext<Ctx>(null!)
export const useApp = () => useContext(AppCtx)

const NAV = [
  ['/', 'On air', LayoutGrid], ['/guide', 'Guide', CalendarRange], ['/channels', 'Channels', SlidersHorizontal],
  ['/stream', 'Stream', MonitorUp], ['/library', 'Library', Lib], ['/downloads', 'Downloads', Download], ['/settings', 'Settings', Cog],
] as const

function App() {
  const { data: status, error, reload } = usePoll<Status>('/api/status', 3000)
  const [watching, watch] = useState<Tunable | null>(null)
  const streaming = useCasts().length
  return (
    <AppCtx.Provider value={{ status, error, reload, watch }}>
      <div className="shell">
        <nav className="side">
          <div className="brand">MyTV</div>
          {NAV.map(([to, label, Icon]) => (
            <NavLink key={to} to={to} end={to === '/'}>
              <Icon size={18} strokeWidth={1.8} /><span>{label}</span>
              {to === '/downloads' && status?.failed ? <em>{status.failed}</em> : null}
              {to === '/stream' && streaming ? <em className="live">{streaming}</em> : null}
            </NavLink>
          ))}
          <div className="side-foot">
            <time>{status ? clock(status.now) : ''}</time>
            <span>{error ? 'Server unreachable' : status ? `${status.titles} titles` : ''}</span>
          </div>
        </nav>
        <main>
          {error && status && <div className="banner bad">Lost contact with the MyTV server. Retrying.</div>}
          {status?.paused && <div className="banner">{status.paused}. Raise the cache limit in Settings or free some disk space.</div>}
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/guide" element={<Guide />} />
            <Route path="/channels" element={<Channels />} />
            <Route path="/channels/:id" element={<Channels />} />
            <Route path="/stream" element={<Stream />} />
            <Route path="/library" element={<Library />} />
            <Route path="/downloads" element={<Downloads />} />
            <Route path="/settings" element={<Settings />} />
          </Routes>
        </main>
      </div>
      {watching && <Player ch={watching} onClose={() => watch(null)} />}
    </AppCtx.Provider>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode><Toasts><BrowserRouter><App /></BrowserRouter></Toasts></StrictMode>,
)
