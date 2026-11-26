# Architecture

```
 sources ──> discovery ──> metadata ──> queue (24 h plan) ──> downloads ──> prepare once ──> cache of HLS segments
                                                                                                     │
 Apple TV  <── /live/N.m3u8 = f(airings table, wall clock) <── booked airings <── scheduler ─────────┘
```

## Why there is no ErsatzTV

ErsatzTV transcodes or remuxes every channel with a running ffmpeg process and expects a permanent library that it scans.
MyTV's library is a rolling cache whose files come and go, so both programs would need to own the schedule.

Instead, each title is cut into HLS segments **once**, right after download. A live playlist is then computed on request
from the airings table and the clock:

- no encoder runs while a channel is on air, so four channels cost almost no CPU;
- a crash cannot lose the position, because the position is the clock;
- an incomplete file cannot air, because a title can only be booked after its segments are complete;
- published segments are never rewritten, so players do not glitch on transitions, skips or restarts.

## Parts

| File | Job |
|---|---|
| `engine.py` | Airings, queue, live playlist, fallback, resume after an outage |
| `ingest.py` | File to HLS segments: copy H.264, hardware-encode the rest, level the volume |
| `downloads.py` | Resumable verified downloads, processing order, storage limits, cleanup |
| `library.py`, `metadata.py` | Discovery, duplicates, genres, categories, posters |
| `providers/` | One class per source; add a source by adding a class |
| `guide.py` | M3U and XMLTV |
| `cast.py`, `frontend/src/cast/` | Live channels fed from Chrome on the Mac |
| `autoplay.py`, `chrome-extension/` | Random 7Movies libraries and automatic next-title navigation in linked tabs |
| `supervisor.py`, `scripts/run.sh` | Restart background loops; restart the process if it dies or hangs |

## Live channels from the Mac

The Stream page captures a tab with the browser's share dialog. A worker per stream reads the frames and samples, encodes
them with the browser's hardware H.264 and AAC encoders (WebCodecs), cuts two-second MPEG-TS segments itself (`ts.ts`) and
posts each finished segment to `/api/cast/N/segments`. The server runs no encoder for these channels: `cast.py` keeps the
last minute of segments in memory and lists them, and fills any time nobody is streaming with a looping card, so the
playlist never ends and a TV that is tuned in carries on by itself when a stream starts or stops.

- Tab capture gives each tab its own sound, muted on the Mac, so streams never mix.
- Every stamp is mapped onto one clock; audio holes are filled with silence and a still picture is repeated, so a paused
  video or a silent tab still makes a steady channel.
- Segment numbers start from the clock and the discontinuity count is stored, so neither goes backwards after a restart
  (hls.js walks every discontinuity number, so that one must stay small). A stream survives a server restart: the page
  keeps retrying and the server adopts it.
- A full-screen video in a captured tab fills only the tab (Chrome's capture mode), and is captured at the TV's 16:9 size,
  which skips the letterbox step. Other shapes are letterboxed on the GPU.
- Tested: three tabs at 1080p60 and five at 1080p30 at once on an M2 Pro, each with only its own sound, audio within
  about 20 ms of the picture; AVPlayer plays card to stream to card without a stall.

The optional MyTV Autoplay extension links a 7Movies tab to a live channel through its popup. Content scripts run only
for pages whose top-level origin is 7Movies, including cross-origin player frames, and listen for the real video's
`ended` event. The service worker asks `autoplay.py` for a URL and navigates the same tab, so tab capture stays attached.
Season counts are parsed from the site's embedded metadata and cached in SQLite. Movie cards from the home page's
Trending, Most watched, Popular and other rails are merged with the curated fallback and refreshed every 12 hours;
failed refreshes retain the last good catalog. Extension requests use a narrow, origin-checked API; they cannot change
any other server setting. A CSS theater layout keeps each new player at tab size.

## Rules that protect the broadcast

- Booked airings are immutable. The queue after them can change freely.
- If the next title is not ready, the first ready one goes ahead and the late one keeps its place.
- Nothing ready: rerun a title still on disk. Nothing on disk: station card, in 30-second pieces, until something is.
- Deleting takes the scheduler lock and refuses anything on air, booked, queued, or finished less than the configured delay ago.
- Downloads stop before the cache limit or the free-space floor is crossed; the rerun reserve is given up first.
- Segments are read-only files, so any number of screens can read the same one.

## Internet Archive content filter

The Internet Archive provider requires a public-domain license tag, a release year up to 1963 and, for films, a DBpedia
entry describing an American film with no foreign production, because many foreign films had US copyright restored. The
license tag is set by the uploader, so this is a careful filter and not a legal guarantee.
