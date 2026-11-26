// End-to-end check of 7Movies autoplay: real Chrome extension, cross-origin player frame, ended event and tab navigation.
// Starts isolated MyTV and website servers; does not touch the running MyTV instance or the normal Chrome profile.
import { spawn } from 'node:child_process'
import { existsSync, globSync, mkdtempSync, rmSync } from 'node:fs'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const CHROME = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge']
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
const ok = (message) => console.log(`ok   ${message}`)
let chrome, backend, website, profile, dataDir, browserLog = ''
let stopping = false

async function cleanup() {
  if (stopping) return
  stopping = true
  try { chrome?.kill() } catch { /* gone */ }
  try { backend?.kill() } catch { /* gone */ }
  try { website?.close() } catch { /* gone */ }
  await sleep(500)
  for (const path of [profile, dataDir]) if (path) rmSync(path, { recursive: true, force: true })
}

async function fail(message) {
  console.error(`FAIL: ${message}`)
  if (browserLog) {
    const relevant = browserLog.split('\n').filter((line) => /extension|manifest|load_extension/i.test(line)).join('\n')
    console.error(relevant || browserLog.slice(-3000))
  }
  await cleanup()
  process.exit(1)
}
process.on('uncaughtException', (error) => fail(error.stack ?? String(error)))
process.on('unhandledRejection', (error) => fail(error?.stack ?? String(error)))
process.on('SIGINT', () => fail('interrupted'))

async function listen(server) {
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  return server.address().port
}

async function connect(url) {
  const ws = new WebSocket(url)
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject })
  let id = 0
  const pending = new Map()
  ws.onmessage = (event) => {
    const message = JSON.parse(event.data)
    const request = pending.get(message.id)
    if (!request) return
    pending.delete(message.id)
    clearTimeout(request[2])
    message.error ? request[1](new Error(message.error.message)) : request[0](message.result)
  }
  const send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
    const next = ++id
    const timer = setTimeout(() => { pending.delete(next); reject(new Error(`Chrome timed out on ${method}`)) }, 10000)
    pending.set(next, [resolve, reject, timer])
    ws.send(JSON.stringify({ id: next, method, params, sessionId }))
  })
  const attach = async (targetId) => {
    const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true })
    const run = async (expression) => {
      const result = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true, userGesture: true }, sessionId)
      if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description ?? result.exceptionDetails.text)
      return result.result.value
    }
    return { targetId, run }
  }
  const open = async (url) => {
    const { targetId } = await send('Target.createTarget', { url })
    return attach(targetId)
  }
  return { send, attach, open }
}

async function api(path, method = 'GET', body) {
  const response = await fetch(`http://localhost:8401${path}`, {
    method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined,
  })
  const data = await response.json()
  if (!response.ok) throw new Error(`${path}: ${data.detail ?? response.status}`)
  return data
}

async function waitFor(check, description, attempts = 60) {
  for (let i = 0; i < attempts; i++) {
    try { const value = await check(); if (value) return value } catch { /* keep waiting */ }
    await sleep(250)
  }
  throw new Error(`Timed out waiting for ${description}`)
}

const testingChrome = globSync('/tmp/mytv-autoplay-browser/chrome/*/chrome-mac-*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing')
const executable = [process.env.MYTV_CHROME, ...testingChrome, ...CHROME].filter(Boolean).find(existsSync)
if (!executable) await fail('Google Chrome (or Edge) is not installed')

website = createServer((request, response) => {
  response.setHeader('Content-Type', 'text/html')
  if (request.url.startsWith('/player')) {
    response.end(`<!doctype html><title>Player</title><body><canvas id="canvas" width="16" height="9"></canvas><video id="video"></video><script>
      const video = document.getElementById('video')
      const context = document.getElementById('canvas').getContext('2d')
      const draw = () => { context.fillStyle = '#c03'; context.fillRect(0, 0, 16, 9) }
      draw()
      setInterval(draw, 100)
      video.srcObject = document.getElementById('canvas').captureStream(1)
      video.addEventListener('playing', () => { window.playCalled = true })
    </script></body>`)
    return
  }
  response.end(`<!doctype html><title>Mock title — 7Movies</title><body><header class="wp-head-copy"><h1>Mock title</h1><p class="wp-ep">Test</p></header><div class="wp-stage"><iframe allow="autoplay; fullscreen" src="http://player.test:${website.address().port}/player"></iframe></div></body>`)
})
const sourcePort = await listen(website)
const base = `http://7movies.ws:${sourcePort}`
ok(`test website is on ${base}`)

dataDir = mkdtempSync(join(tmpdir(), 'mytv-autoplay-data-'))
backend = spawn(join(ROOT, '.venv/bin/python'), ['-m', 'mytv'], {
  cwd: ROOT,
  env: { ...process.env, MYTV_PORT: '8401', MYTV_DATA_DIR: dataDir, MYTV_AUTOPLAY_BASE_URL: base },
  stdio: ['ignore', 'pipe', 'pipe'],
})
backend.stdout.on('data', (data) => { browserLog += data })
backend.stderr.on('data', (data) => { browserLog += data })
await waitFor(() => api('/api/cast'), 'the isolated MyTV server')
ok('isolated MyTV server is running on port 8401')

profile = mkdtempSync(join(tmpdir(), 'mytv-autoplay-chrome-'))
const debug = 9700 + Math.floor(Math.random() * 200)
chrome = spawn(executable, [
  `--user-data-dir=${profile}`, `--remote-debugging-port=${debug}`, `--load-extension=${join(ROOT, 'chrome-extension')}`,
  `--disable-extensions-except=${join(ROOT, 'chrome-extension')}`, '--no-first-run', '--no-default-browser-check',
  '--autoplay-policy=no-user-gesture-required', '--host-resolver-rules=MAP 7movies.ws 127.0.0.1, MAP player.test 127.0.0.1',
  'about:blank',
], { stdio: ['ignore', 'pipe', 'pipe'] })
chrome.stdout.on('data', (data) => { browserLog += data })
chrome.stderr.on('data', (data) => { browserLog += data })
const version = await waitFor(async () => (await (await fetch(`http://127.0.0.1:${debug}/json/version`)).json()), 'Chrome DevTools')
const cdp = await connect(version.webSocketDebuggerUrl)
await cdp.send('Target.setDiscoverTargets', { discover: true })
const source = await cdp.open(`${base}/movie/1726/watch`)

const worker = await waitFor(async () => {
  const { targetInfos } = await cdp.send('Target.getTargets')
  for (const target of targetInfos.filter((target) => target.type === 'service_worker' && target.url.endsWith('/background.js'))) {
    const attached = { ...await cdp.attach(target.targetId), url: target.url }
    try { if (await attached.run(`chrome.runtime.getManifest().name`) === 'MyTV Autoplay') return attached } catch { /* another worker went idle */ }
  }
  return null
}, 'the autoplay extension service worker')
ok(`real MyTV Autoplay extension loaded in Chrome (${worker.url})`)

const linkedBefore = (await api('/api/cast/1/autoplay')).linked_at ?? 0
const tabId = await worker.run(`(async () => {
  await chrome.storage.local.set({ server: 'http://localhost:8401' })
  const tabs = await chrome.tabs.query({})
  const tab = tabs.find((tab) => tab.url === ${JSON.stringify(`${base}/movie/1726/watch`)})
  if (!tab) throw new Error('Could not find test tab')
  await api('/api/cast/1/autoplay', 'PATCH', { enabled: true, collection: 'movies' })
  await saveAssignment(tab.id, { channel: 1, pending: false })
  await chrome.tabs.reload(tab.id)
  return tab.id
})()`)

await waitFor(async () => {
  const config = await api('/api/cast/1/autoplay')
  return config.linked_at > linkedBefore ? config : null
}, 'the reloaded player tab to link itself')
let player = await waitFor(async () => {
  const { targetInfos } = await cdp.send('Target.getTargets')
  const target = targetInfos.find((target) => target.url === `http://player.test:${sourcePort}/player`)
  return target ? cdp.attach(target.targetId) : null
}, 'the cross-origin player frame')
ok('tab linked to channel 11 with the popular-movies library')

await player.run(`document.getElementById('video').dispatchEvent(new Event('ended'))`)
const nextUrl = await waitFor(async () => {
  const tab = await source.run('location.href')
  return tab && tab !== `${base}/movie/1726/watch` ? tab : null
}, 'the ended event to navigate the same tab')
if (!/^http:\/\/7movies\.ws:\d+\/movie\/\d+\/watch$/.test(nextUrl)) await fail(`unexpected next URL: ${nextUrl}`)
ok(`video ended; the same tab navigated to ${nextUrl}`)

const previousPlayer = player.targetId
player = await waitFor(async () => {
  const { targetInfos } = await cdp.send('Target.getTargets')
  const target = targetInfos.find((target) => target.targetId !== previousPlayer && target.url === `http://player.test:${sourcePort}/player`)
  return target ? cdp.attach(target.targetId) : null
}, 'the next player frame')
await waitFor(() => player.run('window.playCalled === true'), 'the next video to play automatically')
const config = await api('/api/cast/1/autoplay')
if (config.recent?.length !== 1) await fail('the selection was not added to recent-title history')
const channels = await api('/api/cast')
if (!channels[0].autoplay.current || channels[0].title !== channels[0].autoplay.current.title) await fail('the channel did not publish the selected title')
ok('next player started automatically and the channel shows its selected title')

console.log('\nPASS: a real ended event in a cross-origin player advances the linked tab and starts the next title.')
await cleanup()
process.exit(0)
