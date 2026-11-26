# MyTV: notes for coding agents

MyTV turns a Mac into a home cable-TV server for an Apple TV: four 24/7 movie and sitcom channels built from
public-domain films, plus live channels (11, 12, 13 by default) that show any Chrome tab streamed from the Mac.
How it works: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). How a person uses it: [README.md](README.md).

## Setting it up on a new Mac

When asked to "set this up", do all of this and finish with the PASS from step 5. Nothing from the old Mac is needed:
a fresh install starts with no films and downloads them by itself, while the live channels work immediately.

0. Clone to the internal disk, for example `git clone https://github.com/lukejoneslj/mytv ~/MyTV`. (macOS does not let
   background services read external drives, so autostart only works from the internal disk.)
1. `command -v brew`. If Homebrew is missing, the person must install it, because it asks for their password. Give them:
   `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"`
   and wait until they say it is done (then `eval "$(/opt/homebrew/bin/brew shellenv)"` in your shell).
2. `scripts/setup.sh`: installs ffmpeg, Node, uv and Google Chrome when missing, creates `.venv`, builds the dashboard and
   writes `.env`. It is safe to run again.
3. `scripts/autostart.sh on` starts MyTV now and at every login. (Use `scripts/start.sh` instead if the project is on an
   external drive, and tell the person to add `Start MyTV.command` under System Settings > General > Login Items.)
   If macOS asks whether Python may accept incoming connections, the person must press Allow; the Apple TV needs it.
4. `scripts/doctor.sh` until everything is `ok`. The live channels' standby cards take up to a minute after the first
   start. It prints the addresses for the Apple TV.
5. `node scripts/test/stream_check.mjs` (Node comes with setup.sh) streams a test tab through the real Stream page in a separate Chrome window
   (about 40 seconds) and checks the picture and sound a TV would receive. It must print `PASS`. It uses the
   highest-numbered live channel nobody is streaming on, and refuses if all are in use.
6. Tell the person:
   - Apple TV: install the **UHF** app (or iPlayTV / IPTVX / GSE Smart IPTV), add an M3U playlist with the
     `Apple TV playlist` address doctor printed (prefer the `.local` one, it survives IP changes), guide = `epg.xml`.
   - To stream: open `http://localhost:8400/stream` in Chrome on the Mac, press *Choose a tab to stream*, pick the tab,
     keep *Also share tab audio* on, then press the video's full-screen button in that tab.
   - The film channels show a station card until their first titles have downloaded, usually 5 to 15 minutes.

Moving the old Mac's films is optional: stop MyTV on both, copy the old `data/` folder over the new one, start again.

## Rules

- Do not add ways around a site's screen-sharing block (DRM black screens).
- Never commit `data/`, `.env`, `library/` contents or `frontend/dist/`.

## Working on the code

- Backend: Python 3.13, FastAPI, SQLite, in `backend/mytv`. Tests: `uv pip install --python .venv/bin/python -e '.[dev]'`
  once, then `.venv/bin/python -m pytest`.
- Dashboard: React + Vite in `frontend`. `cd frontend && npm run build` (the server serves `frontend/dist`).
- Run a second, throwaway server for testing without touching the real one:
  `MYTV_PORT=8401 MYTV_DATA_DIR=/tmp/mytv-test .venv/bin/python -m mytv`
- Restart the real server after backend changes: `scripts/stop.sh && scripts/start.sh` (or, under autostart,
  `scripts/autostart.sh on`). Apple TV players carry on by themselves.
- Playback checks: `swift scripts/test/avplayer_check.swift http://localhost:8400/onair/1.m3u8 30` plays a channel with
  AVPlayer, the engine the Apple TV uses.
- Optional 7Movies autoplay uses the unpacked extension in `chrome-extension/`; setup and usage are in README.md.
  `node scripts/test/autoplay_check.mjs` checks its real Chrome navigation flow against a throwaway server on port 8401.
