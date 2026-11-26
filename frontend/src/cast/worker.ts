// Encodes one captured tab or window into two-second MPEG-TS segments and posts them to the server.
// Runs in a worker so a busy or hidden dashboard tab does not disturb the timing.
import { adts, Segment } from './ts'

export type Start = {
  type: 'start'; video: ReadableStream<VideoFrame>; audio: ReadableStream<AudioData> | null; url: string; session: string
  title: string; width: number; height: number; fps: number; bitrate: number
}
export type Stats = {
  type: 'stats'; fps: number; kbps: number; skipped: number; width: number; height: number; level: number; backlog: number; black: boolean; audio: boolean
  sent: number; trouble: string | null
}
export type Msg = Start | { type: 'title'; title: string } | { type: 'stop' }

const RATE = 48000, CH = 2, FRAME = 1024, KEY_EVERY = 2_000_000, MAX_SEG = 2_900_000
const now = () => performance.now() * 1000 // microseconds, the timeline every stamp is mapped onto

let cfg: Start
let stopped = false, stopping = false
let venc: VideoEncoder, aenc: AudioEncoder
let canvas: OffscreenCanvas, gl: WebGL2RenderingContext | null = null, ctx: OffscreenCanvasRenderingContext2D | null = null, quad: WebGLUniformLocation | null = null
let last: VideoFrame | null = null // newest picture, re-sent when the source sends nothing (paused video)
let lastT = -Infinity, lastKeyReq = -Infinity, forceKey = true
let vOff: number | null = null, aOff: number | null = null
let srcW = 0, srcH = 0, frames = 0, encoded = 0, skipped = 0, sentBytes = 0, peak = 0, audioSeen = 0, blackSince = 0, troubleText: string | null = null
const cc: Record<number, number> = {}

// segment being built
let seg: Segment | null = null, segT0 = 0, segN = 0, segDisc = true, origin = 0, lastChunkT = 0
let pendingAudio: { data: Uint8Array; t: number }[] = []

// audio timeline: samples handed to the encoder so far, and the time of sample 0
let aBase = 0, aFed = 0, aOut = 0, aReady = false
const acc = [new Float32Array(FRAME), new Float32Array(FRAME)]
let accN = 0

/** Map a source timestamp onto our clock, smoothing the arrival jitter. */
function mapTime(ts: number, off: number | null): [number, number] {
  const d = now() - ts
  if (off === null || Math.abs(d - off) > 400_000) off = d
  else off += (d - off) * 0.02
  return [ts + off, off]
}

function encodeFrame(frame: VideoFrame, t: number) {
  if (stopping || venc.state !== 'configured') return
  if (venc.encodeQueueSize > 8) { skipped++; return }
  const key = forceKey || t - lastKeyReq >= KEY_EVERY
  if (key) { lastKeyReq = t; forceKey = false }
  let out: VideoFrame
  const w = frame.displayWidth, h = frame.displayHeight
  // Same shape as the TV (a full-screen tab): hand the captured frame over as is, the encoder scales it on the GPU.
  if (Math.abs(w / h - cfg.width / cfg.height) < 0.01) out = new VideoFrame(frame, { timestamp: t })
  else out = letterbox(frame, w, h, t)
  venc.encode(out, { keyFrame: key })
  out.close()
  encoded++
  lastT = t
}

/** Fit the picture inside the TV frame with black bars, or fill it when the shape is within a few percent. Done on the GPU. */
function letterbox(frame: VideoFrame, w: number, h: number, t: number) {
  const fill = Math.abs(w / h - cfg.width / cfg.height) < 0.035
  const s = fill ? Math.max(cfg.width / w, cfg.height / h) : Math.min(cfg.width / w, cfg.height / h)
  const sx = (w * s) / cfg.width, sy = (h * s) / cfg.height
  if (gl) {
    gl.clear(gl.COLOR_BUFFER_BIT)
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, frame)
    gl.uniform2f(quad, sx, sy)
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4)
  } else {
    const dw = w * s, dh = h * s
    ctx!.fillStyle = '#000'
    ctx!.fillRect(0, 0, cfg.width, cfg.height)
    ctx!.drawImage(frame, (cfg.width - dw) / 2, (cfg.height - dh) / 2, dw, dh)
  }
  return new VideoFrame(canvas, { timestamp: t, alpha: 'discard' })
}

function setupGL() {
  const g = canvas.getContext('webgl2', { alpha: false, antialias: false, depth: false, preserveDrawingBuffer: true, powerPreference: 'high-performance' })
  if (!g) return null
  const sh = (type: number, src: string) => { const x = g.createShader(type)!; g.shaderSource(x, src); g.compileShader(x); return x }
  const p = g.createProgram()!
  g.attachShader(p, sh(g.VERTEX_SHADER, `#version 300 es
    in vec2 a; uniform vec2 s; out vec2 uv;
    void main() { uv = vec2(a.x * .5 + .5, .5 - a.y * .5); gl_Position = vec4(a * s, 0., 1.); }`))
  g.attachShader(p, sh(g.FRAGMENT_SHADER, `#version 300 es
    precision mediump float; in vec2 uv; uniform sampler2D t; out vec4 o;
    void main() { o = vec4(texture(t, uv).rgb, 1.); }`))
  g.linkProgram(p)
  if (!g.getProgramParameter(p, g.LINK_STATUS)) return null
  g.useProgram(p)
  g.bindBuffer(g.ARRAY_BUFFER, g.createBuffer())
  g.bufferData(g.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), g.STATIC_DRAW)
  const loc = g.getAttribLocation(p, 'a')
  g.enableVertexAttribArray(loc)
  g.vertexAttribPointer(loc, 2, g.FLOAT, false, 0, 0)
  g.bindTexture(g.TEXTURE_2D, g.createTexture())
  for (const [k, v] of [[g.TEXTURE_MIN_FILTER, g.LINEAR], [g.TEXTURE_MAG_FILTER, g.LINEAR], [g.TEXTURE_WRAP_S, g.CLAMP_TO_EDGE], [g.TEXTURE_WRAP_T, g.CLAMP_TO_EDGE]]) g.texParameteri(g.TEXTURE_2D, k, v)
  g.clearColor(0, 0, 0, 1)
  g.viewport(0, 0, cfg.width, cfg.height)
  quad = g.getUniformLocation(p, 's')
  return g
}

async function readVideo(r: ReadableStreamDefaultReader<VideoFrame>) {
  let checked = 0
  for (;;) {
    const { value: f, done } = await r.read()
    if (done || stopped) { f?.close(); break }
    let t: number
    ;[t, vOff] = mapTime(f.timestamp, vOff)
    srcW = f.displayWidth; srcH = f.displayHeight
    if (t - lastT < 1e6 / cfg.fps * 0.8) { f.close(); continue } // over the frame rate
    if (t - lastT > 1_000_000 && lastT > 0) forceKey = true // we were frozen: start a fresh piece
    if (t <= lastT) t = lastT + 1000
    encodeFrame(f, t)
    frames++
    last?.close()
    last = f
    if (t - checked > 2_000_000) { checked = t; checkBlack(f) }
  }
  finish('The tab or window was closed or sharing was stopped')
}

const probe = new OffscreenCanvas(16, 9)
const pctx = probe.getContext('2d', { willReadFrequently: true })!
function checkBlack(f: VideoFrame) {
  pctx.drawImage(f, 0, 0, 16, 9)
  const px = pctx.getImageData(0, 0, 16, 9).data
  let max = 0
  for (let i = 0; i < px.length; i += 4) max = Math.max(max, px[i], px[i + 1], px[i + 2])
  blackSince = max < 12 ? blackSince || now() : 0
}

/** The source sent nothing for a while (static page, paused video): repeat the last picture so the channel keeps going. */
function keepAlive() {
  if (stopped || !last) return
  const t = now()
  if (t - lastT > 150_000) encodeFrame(last, t)
}

async function readAudio(r: ReadableStreamDefaultReader<AudioData>) {
  for (;;) {
    const { value: a, done } = await r.read()
    if (done || stopped) { a?.close(); break }
    let t: number
    ;[t, aOff] = mapTime(a.timestamp, aOff)
    audioSeen = now()
    const n = a.numberOfFrames, chans = a.numberOfChannels
    const planes: Float32Array[] = []
    for (let c = 0; c < chans; c++) {
      const p = new Float32Array(n)
      try { a.copyTo(p, { planeIndex: c, format: 'f32-planar' }) } catch { p.fill(0) }
      planes.push(p)
    }
    const rate = a.sampleRate
    a.close()
    const L = planes[0], R = planes[1] ?? planes[0]
    feed(rate === RATE ? [L, R] : [resample(L, rate), resample(R, rate)], t)
  }
}

function resample(x: Float32Array, from: number) {
  const n = Math.round(x.length * RATE / from), out = new Float32Array(n), k = from / RATE
  for (let i = 0; i < n; i++) {
    const p = i * k, j = Math.floor(p), f = p - j
    out[i] = x[j] * (1 - f) + (x[Math.min(j + 1, x.length - 1)] ?? 0) * f
  }
  return out
}

/** Hand samples to the encoder in 1024-sample frames, keeping them on the shared clock. */
function feed([L, R]: Float32Array[], t: number) {
  if (!aReady) { aBase = t; aFed = 0; aReady = true }
  const at = aBase + ((aFed + accN) / RATE) * 1e6
  const drift = t - at
  if (drift > 1_000_000 || drift < -1_000_000) { // long gap: restart the audio clock
    aBase = t - ((aFed + accN) / RATE) * 1e6
  } else if (drift > 60_000) silence(Math.round((drift / 1e6) * RATE)) // a hole: fill it
  else if (drift < -60_000) return // ahead of the clock: drop this piece
  for (let i = 0; i < L.length; i++) {
    const l = L[i], r = R[i]
    const m = Math.max(Math.abs(l), Math.abs(r))
    if (m > peak) peak = m
    acc[0][accN] = l; acc[1][accN] = r
    if (++accN === FRAME) pushFrame()
  }
}

function silence(n: number) {
  for (let i = 0; i < n; i++) {
    acc[0][accN] = 0; acc[1][accN] = 0
    if (++accN === FRAME) pushFrame()
  }
}

function pushFrame() {
  if (stopping) { accN = 0; return }
  const data = new Float32Array(FRAME * CH)
  data.set(acc[0], 0); data.set(acc[1], FRAME)
  const ad = new AudioData({ format: 'f32-planar', sampleRate: RATE, numberOfFrames: FRAME, numberOfChannels: CH, timestamp: aBase + (aFed / RATE) * 1e6, data })
  aFed += FRAME
  accN = 0
  if (aenc.state === 'configured') aenc.encode(ad)
  ad.close()
}

/** No sound from the source (silent tab, a window, no audio shared): keep the audio track going with silence. */
function audioKeepAlive() {
  if (stopped || !aReady && lastT < 0) return
  const t = now() - 250_000
  if (!aReady) { aBase = t; aFed = 0; aReady = true }
  const at = aBase + ((aFed + accN) / RATE) * 1e6
  if (t - at > 100_000 && now() - audioSeen > 300_000) silence(Math.round(((t - at) / 1e6) * RATE))
}

// ---- segmenting
function onVideo(chunk: EncodedVideoChunk) {
  const data = new Uint8Array(chunk.byteLength)
  chunk.copyTo(data)
  const t = chunk.timestamp
  const key = chunk.type === 'key'
  if (key && (!seg || t - segT0 >= 1_800_000 || t - lastChunkT > 1_000_000)) cut(t)
  if (!seg) return // wait for the first key frame
  if (t - segT0 > MAX_SEG) { forceKey = true }
  flushAudio(t)
  seg.video(data, pts(t), key)
  lastChunkT = t
}

function onAudio(chunk: EncodedAudioChunk) {
  const raw = new Uint8Array(chunk.byteLength)
  chunk.copyTo(raw)
  const data = raw[0] === 0xff && (raw[1] & 0xf0) === 0xf0 ? raw : adts(raw, RATE, CH)
  // stamp from our own sample count: encoder output order is the input order
  const t = aBase + (aOut / RATE) * 1e6
  aOut += FRAME
  pendingAudio.push({ data, t })
}

function flushAudio(upTo: number) {
  if (!seg) return
  const keep: typeof pendingAudio = []
  let group: Uint8Array[] = [], gt = 0
  const out = () => {
    if (!group.length) return
    const n = group.reduce((s, g) => s + g.length, 0), b = new Uint8Array(n)
    let o = 0
    for (const g of group) { b.set(g, o); o += g.length }
    seg!.audio(b, pts(gt))
    group = []
  }
  for (const a of pendingAudio) {
    if (a.t > upTo + 100_000) { keep.push(a); continue }
    if (a.t < segT0 - 200_000) continue // belongs before this piece started
    if (!group.length) gt = a.t
    group.push(a.data)
    if (group.length === 4) out()
  }
  out()
  pendingAudio = keep
}

const pts = (t: number) => Math.round(((t - origin) / 1e6) * 90000) + 90000

function cut(t: number) {
  if (seg) {
    const gap = t - lastChunkT > 1_000_000
    const end = gap ? lastChunkT + 1e6 / cfg.fps : t
    flushAudio(end)
    upload(seg.finish(), Math.min((end - segT0) / 1e6, 3.4), segDisc)
    segDisc = gap
    if (gap) origin = t
  } else origin = t
  seg = new Segment(cc)
  segT0 = t
}

// ---- upload
const queue: { data: Uint8Array; dur: number; disc: boolean; n: number }[] = []
let uploading = false, n = 0, dropped = false
function upload(data: Uint8Array, dur: number, disc: boolean) {
  queue.push({ data, dur, disc: disc || dropped, n: n++ })
  dropped = false
  while (queue.length > 12) { queue.shift(); dropped = true } // the server was away: skip ahead rather than lag behind
  if (!uploading) send()
}

async function send() {
  uploading = true
  while (queue.length && !stopped) {
    const s = queue[0]
    const q = new URLSearchParams({ session: cfg.session, n: String(s.n), dur: s.dur.toFixed(4), disc: String(s.disc), title: cfg.title, note: `${srcW}x${srcH}` })
    try {
      const r = await fetch(`${cfg.url}?${q}`, { method: 'POST', body: s.data as BlobPart, headers: { 'Content-Type': 'video/mp2t' } })
      if (r.status === 409 || r.status === 404) {
        let why = 'The channel refused the stream'
        try { why = (await r.json()).detail } catch { /* keep */ }
        finish(why, true)
        return
      }
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      queue.shift()
      sentBytes += s.data.length
      troubleText = null
    } catch {
      troubleText = 'Cannot reach the MyTV server, retrying'
      await new Promise((res) => setTimeout(res, 700))
    }
  }
  uploading = false
}

function finish(reason: string, refused = false) {
  if (stopped) return
  stopped = true
  last?.close(); last = null
  try { venc.close() } catch { /* closed */ }
  try { aenc.close() } catch { /* closed */ }
  postMessage({ type: 'ended', reason, refused })
}

const history: [number, number, number][] = []
function report() {
  const t = now()
  history.push([t, encoded, sentBytes])
  while (history.length > 7) history.shift()
  const [t0, e0, b0] = history[0], dt = Math.max((t - t0) / 1e6, 0.5)
  const s: Stats = {
    type: 'stats', fps: Math.round((encoded - e0) / dt), kbps: Math.round(((sentBytes - b0) * 8) / 1000 / dt), skipped,
    width: srcW, height: srcH, level: peak, backlog: queue.length, black: !!blackSince && t - blackSince > 6_000_000,
    audio: t - audioSeen < 2_000_000, sent: sentBytes, trouble: troubleText,
  }
  peak = 0
  postMessage(s)
}

async function start(c: Start) {
  cfg = c
  canvas = new OffscreenCanvas(c.width, c.height)
  gl = setupGL()
  if (!gl) ctx = canvas.getContext('2d', { alpha: false })!
  const level = c.height * c.fps > 1080 * 30 ? '2a' : '28'
  const vconf: VideoEncoderConfig = {
    codec: `avc1.6400${level}`, width: c.width, height: c.height, bitrate: c.bitrate, framerate: c.fps, bitrateMode: 'variable',
    latencyMode: 'quality', hardwareAcceleration: 'prefer-hardware', avc: { format: 'annexb' },
  }
  if (!(await VideoEncoder.isConfigSupported(vconf)).supported) { vconf.hardwareAcceleration = 'no-preference'; vconf.codec = `avc1.4d00${level}` }
  venc = new VideoEncoder({ output: onVideo, error: (e) => finish(`Video encoder failed: ${e.message}`) })
  venc.configure(vconf)
  const aconf = { codec: 'mp4a.40.2', sampleRate: RATE, numberOfChannels: CH, bitrate: 192_000, aac: { format: 'adts' } } as AudioEncoderConfig
  if (!(await AudioEncoder.isConfigSupported(aconf)).supported) { finish('This browser cannot encode AAC audio. Use Google Chrome.'); return }
  aenc = new AudioEncoder({ output: onAudio, error: (e) => finish(`Audio encoder failed: ${e.message}`) })
  aenc.configure(aconf)
  readVideo(c.video.getReader())
  if (c.audio) readAudio(c.audio.getReader())
  setInterval(keepAlive, 40)
  setInterval(audioKeepAlive, 100)
  setInterval(report, 1000)
}

onmessage = (e: MessageEvent<Msg>) => {
  const m = e.data
  if (m.type === 'start') start(m).catch((err) => finish(String(err?.message ?? err)))
  else if (m.type === 'title') cfg.title = m.title
  else if (m.type === 'stop') {
    stopping = true
    // send what is encoded so far, then end
    Promise.allSettled([venc?.flush(), aenc?.flush()]).then(() => {
      if (seg && lastChunkT > segT0) { flushAudio(lastChunkT); upload(seg.finish(), Math.min((lastChunkT - segT0) / 1e6 + 1 / cfg.fps, 3.4), segDisc); seg = null }
      const wait = () => (queue.length && !troubleText ? setTimeout(wait, 100) : finish('Stopped'))
      wait()
    })
  }
}
