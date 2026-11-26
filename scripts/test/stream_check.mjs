// End-to-end check of the Stream feature on this Mac, the way a person uses it:
// opens a separate Chrome window (its own throwaway profile), plays a test tab with a moving picture and a tone,
// streams it to a live channel through the real Stream page, then checks what a TV would receive.
// usage: node scripts/test/stream_check.mjs [port]      (MyTV must be running)
import { execFileSync, spawn, spawnSync } from 'node:child_process'
import { existsSync, mkdtempSync, rmSync } from 'node:fs'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const PORT = +(process.argv[2] || process.env.MYTV_PORT || 8400)
const BASE = `http://localhost:${PORT}`
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const fail = (msg) => { console.error(`FAIL: ${msg}`); cleanup(); process.exit(1) }
const ok = (msg) => console.log(`ok   ${msg}`)
process.on('uncaughtException', (e) => fail(e.stack ?? String(e)))
process.on('unhandledRejection', (e) => fail(e?.stack ?? String(e)))
process.on('SIGINT', () => fail('interrupted'))

const CHROME = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge']
  .find(existsSync)
if (!CHROME) { console.error('FAIL: Google Chrome (or Edge) is not installed. Run: brew install --cask google-chrome'); process.exit(1) }

const SOURCE = `<!doctype html><title>Source</title><body style="margin:0;overflow:hidden;background:#000"><canvas id=c width=1280 height=720 style="width:100vw;height:100vh"></canvas><script>
const g = c.getContext('2d'); let n = 0
;(function draw() { g.fillStyle = '#c03'; g.fillRect(0, 0, 1280, 720); g.fillStyle = '#fff'; g.fillRect((n * 6) % 1280, 300, 100, 100); g.font = 'bold 90px sans-serif'; g.fillText('MyTV test ' + n++, 60, 140); requestAnimationFrame(draw) })()
const a = new AudioContext(), o = a.createOscillator(), v = a.createGain(); v.gain.value = 0.3; o.frequency.value = 1000; o.connect(v).connect(a.destination); o.start()
setInterval(() => a.state !== 'running' && a.resume(), 500)
</script>`

let chrome, server, profile
function cleanup() {
  try { chrome?.kill() } catch { /* gone */ }
  try { server?.close() } catch { /* gone */ }
  if (profile) setTimeout(() => rmSync(profile, { recursive: true, force: true }), 1500)
}

// tiny Chrome DevTools client
async function cdp(port) {
  for (let i = 0; i < 40; i++) {
    try { const v = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json(); return connect(v.webSocketDebuggerUrl) } catch { await sleep(250) }
  }
  fail('Chrome did not start')
}
async function connect(url) {
  const ws = new WebSocket(url)
  await new Promise((r, j) => { ws.onopen = r; ws.onerror = j })
  let id = 0
  const pending = new Map()
  ws.onmessage = (e) => { const m = JSON.parse(e.data); const p = pending.get(m.id); if (p) { pending.delete(m.id); m.error ? p[1](new Error(m.error.message)) : p[0](m.result) } }
  const send = (method, params = {}, sessionId) => new Promise((r, j) => { const i = ++id; pending.set(i, [r, j]); ws.send(JSON.stringify({ id: i, method, params, sessionId })) })
  const open = async (u) => {
    const { targetId } = await send('Target.createTarget', { url: u })
    const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true })
    const run = async (expression) => {
      const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true, userGesture: true }, sessionId)
      if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description ?? r.exceptionDetails.text)
      return r.result.value
    }
    return { targetId, run, front: () => send('Target.activateTarget', { targetId }) }
  }
  return { send, open }
}

const api = async (path, init) => { const r = await fetch(BASE + path, init); if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`); return r.json() }

try { await api('/api/status') } catch { fail(`MyTV is not answering on ${BASE}. Start it with scripts/start.sh`) }
const chans = await api('/api/cast')
const target = [...chans].reverse().find((c) => !c.live)
if (!target) fail('every live channel is in use; stop one on the Stream page')
const index = chans.indexOf(target)
ok(`MyTV is running; testing on channel ${target.number} (${target.name})`)

server = createServer((_, res) => { res.writeHead(200, { 'Content-Type': 'text/html' }); res.end(SOURCE) }).listen(0, '127.0.0.1')
await new Promise((r) => server.once('listening', r))
const srcUrl = `http://127.0.0.1:${server.address().port}/`

profile = mkdtempSync(join(tmpdir(), 'mytv-check-'))
const debug = 9400 + Math.floor(Math.random() * 400)
chrome = spawn(CHROME, [`--user-data-dir=${profile}`, `--remote-debugging-port=${debug}`, '--no-first-run', '--no-default-browser-check',
  '--autoplay-policy=no-user-gesture-required', '--auto-select-tab-capture-source-by-title=MYTV-CHECK-SOURCE', '--window-size=1200,800', 'about:blank'], { stdio: 'ignore' })
const c = await cdp(debug)
const dash = await c.open(`${BASE}/stream`)
await sleep(3000)
const src = await c.open(srcUrl)
await sleep(1500)
await src.run(`document.title = 'MYTV-CHECK-SOURCE'`)
await dash.front()
await sleep(500)
const can = await dash.run(`(() => { const b = document.querySelectorAll('.cast')[${index}]?.querySelector('button.primary'); if (!b || b.disabled) return false; b.click(); return true })()`)
if (!can) fail('the Stream page could not start sharing (is this Chrome on the Mac running MyTV?)')
await sleep(2500)
await src.run(`document.title = 'Source'`)

let live
for (let i = 0; i < 20 && !live?.live; i++) { await sleep(1000); live = (await api('/api/cast')).find((x) => x.id === target.id) }
if (!live?.live) fail('no segments reached the server within 20 seconds')
ok(`stream is live (${live.info.note}, ${live.info.kbps} kbps)`)
await sleep(12000)

const rec = join(profile, 'rec.ts')
try {
  execFileSync('ffmpeg', ['-loglevel', 'error', '-y', '-live_start_index', '-5', '-i', `${BASE}/onair/${target.id}.m3u8`, '-t', '8', '-c', 'copy', rec], { timeout: 60000 })
} catch (e) { fail(`could not record the channel: ${e.message}`) }
const probe = JSON.parse(execFileSync('ffprobe', ['-v', 'error', '-count_frames', '-show_streams', '-of', 'json', rec]).toString()).streams
const v = probe.find((s) => s.codec_type === 'video'), a = probe.find((s) => s.codec_type === 'audio')
if (!v || v.width !== 1920 || v.height !== 1080) fail(`video is not 1920x1080: ${JSON.stringify(v && [v.width, v.height])}`)
const fps = +v.nb_read_frames / 8
if (fps < 20) fail(`only ${fps.toFixed(0)} frames per second reached the channel`)
ok(`video: H.264 1920x1080, about ${fps.toFixed(0)} fps`)
if (!a) fail('no audio track')
const vol = spawnSync('ffmpeg', ['-hide_banner', '-i', rec, '-af', 'volumedetect', '-vn', '-f', 'null', '-'], { encoding: 'utf8' }).stderr
  .match(/mean_volume: (-?[\d.]+) dB/)
if (!vol || +vol[1] < -40) fail(`the tab's sound did not arrive (mean volume ${vol?.[1]} dB)`)
ok(`audio: AAC ${a.sample_rate} Hz ${a.channels} channels, tone present (${vol[1]} dB)`)

await dash.front()
await dash.run(`document.querySelectorAll('.cast')[${index}].querySelector('button.stop')?.click()`)
for (let i = 0; i < 15; i++) { await sleep(1000); live = (await api('/api/cast')).find((x) => x.id === target.id); if (!live.live) break }
if (live.live) fail('the stream did not stop')
ok('stopped; the channel is back on its standby card')
console.log(`\nPASS: streaming works. On the Apple TV this was channel ${target.number}.`)
cleanup()
process.exit(0)
