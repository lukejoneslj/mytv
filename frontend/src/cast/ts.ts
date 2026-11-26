// Minimal MPEG-TS writer for one H.264 and one AAC (ADTS) stream: enough for self-contained HLS segments.
const VPID = 0x100, APID = 0x101, PMT = 0x1000

const CRC = (() => {
  const t = new Uint32Array(256)
  for (let i = 0; i < 256; i++) {
    let c = i << 24
    for (let k = 0; k < 8; k++) c = c & 0x80000000 ? (c << 1) ^ 0x04c11db7 : c << 1
    t[i] = c >>> 0
  }
  return t
})()
const crc32 = (b: Uint8Array) => {
  let c = 0xffffffff
  for (const x of b) c = ((c << 8) ^ CRC[((c >>> 24) ^ x) & 0xff]) >>> 0
  return c
}

const AUD = new Uint8Array([0, 0, 0, 1, 9, 0xf0])

export class Segment {
  private chunks: Uint8Array[] = []
  private cc: Record<number, number>
  constructor(cc: Record<number, number>) {
    this.cc = cc
    this.psi(0, [0x00, 0xb0, 0x0d, 0x00, 0x01, 0xc1, 0x00, 0x00, 0x00, 0x01, 0xe0 | (PMT >> 8), PMT & 0xff])
    this.psi(PMT, [0x02, 0xb0, 0x17, 0x00, 0x01, 0xc1, 0x00, 0x00, 0xe0 | (VPID >> 8), VPID & 0xff, 0xf0, 0x00,
      0x1b, 0xe0 | (VPID >> 8), VPID & 0xff, 0xf0, 0x00, 0x0f, 0xe0 | (APID >> 8), APID & 0xff, 0xf0, 0x00])
  }

  get bytes() { return this.chunks.reduce((n, c) => n + c.length, 0) }

  finish(): Uint8Array {
    const out = new Uint8Array(this.bytes)
    let o = 0
    for (const c of this.chunks) { out.set(c, o); o += c.length }
    return out
  }

  private psi(pid: number, table: number[]) {
    const p = new Uint8Array(188).fill(0xff)
    const body = new Uint8Array(table)
    p.set([0x47, 0x40 | (pid >> 8), pid & 0xff, 0x10 | this.next(pid), 0x00], 0)
    p.set(body, 5)
    const c = crc32(body)
    p.set([c >>> 24, (c >>> 16) & 0xff, (c >>> 8) & 0xff, c & 0xff], 5 + body.length)
    this.chunks.push(p)
  }

  private next(pid: number) {
    const v = this.cc[pid] ?? 0
    this.cc[pid] = (v + 1) & 15
    return v
  }

  /** pts in 90 kHz units */
  video(data: Uint8Array, pts: number, key: boolean) {
    const hasAud = data.length > 5 && data[0] === 0 && data[1] === 0 && ((data[2] === 1 && (data[3] & 0x1f) === 9) || (data[2] === 0 && data[3] === 1 && (data[4] & 0x1f) === 9))
    const payload = hasAud ? data : concat(AUD, data)
    this.pes(VPID, 0xe0, payload, pts, key, true)
  }

  audio(adts: Uint8Array, pts: number) {
    this.pes(APID, 0xc0, adts, pts, false, false)
  }

  private pes(pid: number, sid: number, payload: Uint8Array, pts: number, key: boolean, pcr: boolean) {
    const len = sid === 0xe0 ? 0 : payload.length + 8
    const head = new Uint8Array([0, 0, 1, sid, len >> 8, len & 0xff, 0x84, 0x80, 5, ...stamp(pts, 0x21)])
    const data = concat(head, payload)
    let off = 0, first = true
    while (off < data.length) {
      const p = new Uint8Array(188)
      p[0] = 0x47; p[1] = (first ? 0x40 : 0) | (pid >> 8); p[2] = pid & 0xff
      let i = 4
      const left = data.length - off
      let af: number[] | null = null
      if (first && (pcr || key)) {
        af = [(key ? 0x40 : 0) | (pcr ? 0x10 : 0)]
        if (pcr) af.push(...pcrBytes(Math.max(0, pts - 9000)))
      }
      const room = 184 - (af ? af.length + 1 : 0)
      if (left < room) {
        // stuff the adaptation field so the payload ends exactly at the packet end
        const need = 184 - left
        if (!af) af = need === 1 ? [] : [0x00]
        while (af.length + 1 < need) af.push(0xff)
      }
      p[3] = (af ? 0x30 : 0x10) | this.next(pid)
      if (af) {
        p[i++] = af.length
        p.set(af, i); i += af.length
      }
      const n = Math.min(188 - i, left)
      p.set(data.subarray(off, off + n), i)
      off += n
      first = false
      this.chunks.push(p)
    }
  }
}

function stamp(t: number, prefix: number) {
  t = ((t % 8589934592) + 8589934592) % 8589934592
  return [prefix | ((Math.floor(t / 2 ** 30) & 7) << 1) | 1, Math.floor(t / 2 ** 22) & 0xff, ((Math.floor(t / 2 ** 15) & 0x7f) << 1) | 1,
    Math.floor(t / 2 ** 7) & 0xff, ((t & 0x7f) << 1) | 1]
}

function pcrBytes(t: number) {
  t = ((t % 8589934592) + 8589934592) % 8589934592
  return [Math.floor(t / 2 ** 25) & 0xff, Math.floor(t / 2 ** 17) & 0xff, Math.floor(t / 2 ** 9) & 0xff, Math.floor(t / 2) & 0xff,
    ((t & 1) << 7) | 0x7e, 0]
}

function concat(a: Uint8Array, b: Uint8Array) {
  const o = new Uint8Array(a.length + b.length)
  o.set(a); o.set(b, a.length)
  return o
}

/** ADTS header for one raw AAC-LC frame, in case the encoder returns raw frames. */
export function adts(raw: Uint8Array, rate: number, channels: number) {
  const idx = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000].indexOf(rate)
  const len = raw.length + 7
  return concat(new Uint8Array([0xff, 0xf1, (1 << 6) | (idx << 2) | (channels >> 2), ((channels & 3) << 6) | (len >> 11),
    (len >> 3) & 0xff, ((len & 7) << 5) | 0x1f, 0xfc]), raw)
}
