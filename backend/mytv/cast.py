"""Live channels fed by this Mac's browser.

The dashboard captures a Chrome tab, encodes it and posts finished two-second MPEG-TS segments. This module only keeps the last
minute of them in memory and lists them. While nobody is streaming, a looping card takes their place, so the channel never ends
and a player that is tuned in carries on by itself when a stream starts or stops.
"""
from __future__ import annotations

import datetime as dt
import math
import os
import shutil
import subprocess
import threading
import time
import zlib
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw

from . import artwork, config
from .db import DB

TARGET = 3          # seconds; no segment is longer
WINDOW = 30.0       # seconds listed in the playlist
KEEP = 45.0         # a segment stays readable this long after it leaves the playlist
GRACE = 10.0        # a stream that sends nothing for this long is replaced by the card
CARD_SECONDS, CARD_PIECE, CARD_FPS = 60, 2, 5
MAX_SEGMENT = 48 * 1024 * 1024
COLORS = ["#5fb0e5", "#e56fa5", "#9ccc5a", "#e5925f", "#b58cf0", "#5fd0c4"]


class Refused(Exception):
    pass


@dataclass
class Seg:
    seq: int
    data: bytes
    dur: float
    disc: bool
    disc_no: int
    at: float


class Room:
    """State of one live channel."""

    def __init__(self, now: float, disc: int = 0):
        # The segment number starts at the clock, so it never runs backwards across a restart. The discontinuity number
        # must stay small (hls.js walks every number up to it), so it is stored instead.
        self.seq = int(now)
        self.disc = self.saved_disc = disc
        self.edge = now - WINDOW
        self.segs: deque[Seg] = deque()
        self.card: list[tuple[bytes, float]] = []
        self.card_at = 0
        self.on_card = False
        self.session: str | None = None
        self.last_n = -1
        self.last_push = 0.0
        self.started = 0.0
        self.info: dict = {}
        self.ended: deque[str] = deque(maxlen=50)

    def live(self, now: float) -> bool:
        return self.session is not None and now - self.last_push < GRACE

    def add(self, data: bytes, dur: float, disc: bool, now: float) -> None:
        if disc:
            self.disc += 1
        self.segs.append(Seg(self.seq, data, dur, disc, self.disc, now))
        self.seq += 1

    def fill(self, now: float) -> None:
        """Cover the time up to now with the card, unless a stream is doing that."""
        if self.live(now) or not self.card:
            return
        behind = now - self.edge
        if behind > WINDOW * 2:  # nobody asked for a while
            skip = math.ceil((behind - WINDOW) / CARD_PIECE)
            self.seq += skip
            self.disc += math.ceil(skip * CARD_PIECE / CARD_SECONDS) + 1
            self.edge += skip * CARD_PIECE
            self.on_card = False
        while self.edge < now:
            if not self.on_card:
                self.card_at = 0
            data, dur = self.card[self.card_at]
            self.add(data, dur, self.card_at == 0, now)
            self.on_card = True
            self.card_at = (self.card_at + 1) % len(self.card)
            self.edge += dur

    def listed(self) -> list[Seg]:
        out, total = [], 0.0
        for s in reversed(self.segs):
            out.append(s)
            total += s.dur
            if total >= WINDOW:
                break
        return out[::-1]

    def prune(self, now: float) -> None:
        first = self.listed()[0].seq if self.segs else 0
        while self.segs and self.segs[0].seq < first and now - self.segs[0].at > WINDOW + KEEP:
            self.segs.popleft()


class Casts:
    def __init__(self, db: DB, clock: Callable[[], float] = time.time, card_dir: Path | None = None):
        self.db, self.clock = db, clock
        self.card_dir = card_dir or config.DATA_DIR / "cards"
        self.lock = threading.RLock()
        self.rooms: dict[int, Room] = {}
        self._awake: subprocess.Popen | None = None
        if db.get("cast_seeded") is None:
            for _ in range(3):
                self.create()
            db.set("cast_seeded", True)

    # ---- channels ------------------------------------------------------
    def channels(self) -> list[dict]:
        return self.db.all("SELECT * FROM cast_channels ORDER BY number")

    def get(self, cid: int) -> dict | None:
        return self.db.one("SELECT * FROM cast_channels WHERE id=?", cid)

    def free_number(self) -> int:
        n = 11
        while self.db.number_taken(n):
            n += 1
        return n

    def create(self, name: str | None = None) -> dict:
        with self.lock:
            k = self.db.one("SELECT COALESCE(MAX(id),0)+1 AS k FROM cast_channels")["k"]
            cur = self.db.run("INSERT INTO cast_channels(number,name,accent) VALUES(?,?,?)",
                              self.free_number(), name or f"Live {k}", COLORS[(k - 1) % len(COLORS)])
            return self.get(cur.lastrowid)

    def delete(self, cid: int) -> None:
        with self.lock:
            room = self.rooms.pop(cid, None)
            if room and room.live(self.clock()):
                self.rooms[cid] = room
                raise Refused("Stop the stream first")
            self.db.run("DELETE FROM cast_channels WHERE id=?", cid)
            self.db.run("DELETE FROM settings WHERE key=?", f"cast_disc_{cid}")
        for d in self.card_dir.glob(f"{cid}-*"):
            shutil.rmtree(d, ignore_errors=True)

    def room(self, cid: int) -> Room:
        with self.lock:
            if cid not in self.rooms:
                self.rooms[cid] = Room(self.clock(), int(self.db.get(f"cast_disc_{cid}", 0)) + 1)
            return self.rooms[cid]

    def _keep(self, cid: int, r: Room) -> None:
        if r.disc != r.saved_disc:
            self.db.set(f"cast_disc_{cid}", r.disc)
            r.saved_disc = r.disc

    def status(self, ch: dict) -> dict:
        now = self.clock()
        with self.lock:
            r = self.room(ch["id"])
            live = r.live(now)
            return {**ch, "logo": f"/onair/{ch['id']}.png", "stream": f"/onair/{ch['id']}.m3u8", "live": live,
                    "session": r.session if live else None, "started": r.started if live else None,
                    "title": r.info.get("title") or "" if live else "", "info": r.info if live else {}}

    # ---- incoming ------------------------------------------------------
    def push(self, cid: int, session: str, n: int, dur: float, disc: bool, data: bytes, info: dict) -> None:
        now = self.clock()
        if not (0 < dur <= TARGET + 0.49) or len(data) % 188 or not data or data[0] != 0x47:
            raise ValueError("Not a valid segment")
        with self.lock:
            r = self.room(cid)
            if session in r.ended:
                raise Refused("This stream was stopped")
            if session != r.session:
                if r.live(now):
                    raise Refused("Another stream is already on this channel")
                if r.session:
                    r.ended.append(r.session)
                r.session, r.last_n, r.started = session, -1, now
                disc = True
            if n <= r.last_n:
                return  # a retry of something that did arrive
            r.add(data, dur, disc or r.on_card or not r.segs or n != r.last_n + 1, now)
            r.on_card = False
            r.last_n, r.last_push, r.edge, r.info = n, now, now, info
            r.prune(now)
            self._keep(cid, r)

    def stop(self, cid: int, session: str | None = None) -> None:
        with self.lock:
            r = self.room(cid)
            if session:
                r.ended.append(session)
            if r.session and session in (None, r.session):
                r.ended.append(r.session)
                r.session, r.info = None, {}
                r.edge = self.clock()

    # ---- outgoing ------------------------------------------------------
    def playlist(self, cid: int) -> str | None:
        now = self.clock()
        with self.lock:
            r = self.room(cid)
            r.fill(now)
            r.prune(now)
            self._keep(cid, r)
            segs = r.listed()
        if not segs:
            return None
        # A discontinuity tag on the first listed segment counts as still inside the window.
        out = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-INDEPENDENT-SEGMENTS", f"#EXT-X-TARGETDURATION:{TARGET}",
               f"#EXT-X-MEDIA-SEQUENCE:{segs[0].seq}", f"#EXT-X-DISCONTINUITY-SEQUENCE:{segs[0].disc_no - segs[0].disc}"]
        for s in segs:
            if s.disc:
                out.append("#EXT-X-DISCONTINUITY")
            out += [f"#EXTINF:{s.dur:.5f},", f"/onair/{cid}/{s.seq}.ts"]
        return "\n".join(out) + "\n"

    def segment(self, cid: int, seq: int) -> bytes | None:
        with self.lock:
            r = self.rooms.get(cid)
            return next((s.data for s in reversed(r.segs) if s.seq == seq), None) if r else None

    # ---- housekeeping --------------------------------------------------
    def tick(self) -> None:
        now = self.clock()
        with self.lock:
            ids = {c["id"] for c in self.channels()}
            for cid in [c for c in self.rooms if c not in ids]:
                self.rooms.pop(cid)
            for r in self.rooms.values():
                r.prune(now)
            live = any(r.live(now) for r in self.rooms.values())
        # Chrome stops drawing tabs when the display sleeps
        running = self._awake is not None and self._awake.poll() is None
        if live and not running:
            self._awake = subprocess.Popen(["caffeinate", "-d", "-w", str(os.getpid())])
        elif not live and running:
            self._awake.terminate()
            self._awake.wait()
            self._awake = None

    def cards(self) -> None:
        for ch in self.channels():
            card = self._card(ch)
            with self.lock:
                r = self.room(ch["id"])
                if r.card is not card and [d for d, _ in r.card] != [d for d, _ in card]:
                    r.card, r.card_at, r.on_card = card, 0, False

    def _card(self, ch: dict) -> list[tuple[bytes, float]]:
        sig = f"{ch['id']}-{zlib.crc32(repr((ch['name'], ch['number'], ch['accent'], CARD_SECONDS, CARD_FPS)).encode()):08x}"
        d = self.card_dir / sig
        if not (d / "done").exists():
            for old in self.card_dir.glob(f"{ch['id']}-*"):
                shutil.rmtree(old, ignore_errors=True)
            d.mkdir(parents=True, exist_ok=True)
            im = Image.new("RGB", (1920, 1080), "#14130f")
            dr = ImageDraw.Draw(im)
            dr.rectangle((0, 0, 36, 1080), fill=ch["accent"])
            dr.text((165, 375), str(ch["number"]), font=artwork.font(225), fill=ch["accent"], anchor="lm")
            dr.text((165, 555), ch["name"], font=artwork.font(126), fill="#f3efe6", anchor="lm")
            dr.text((165, 678), "Nothing is streaming. Start one from MyTV on the Mac.", font=artwork.font(54), fill="#a29c8e", anchor="lm")
            im.save(d / "card.png")
            subprocess.run(
                [config.FFMPEG, "-loglevel", "error", "-y", "-loop", "1", "-framerate", str(CARD_FPS), "-i", str(d / "card.png"), "-f", "lavfi",
                 "-i", "anullsrc=r=48000:cl=stereo", "-t", str(CARD_SECONDS), "-c:v", "h264_videotoolbox", "-allow_sw", "1",
                 "-profile:v", "high", "-b:v", "1500k", "-g", str(CARD_FPS * CARD_PIECE), "-bf", "0", "-pix_fmt", "yuv420p",
                 "-force_key_frames", f"expr:gte(t,n_forced*{CARD_PIECE})", "-c:a", "aac", "-b:a", "128k", "-f", "hls", "-hls_time",
                 str(CARD_PIECE), "-hls_list_size", "0", "-hls_segment_filename", str(d / "%03d.ts"), str(d / "card.m3u8")],
                check=True, timeout=120, stdin=subprocess.DEVNULL)
            (d / "done").touch()
        durs = [float(x.split(":")[1].rstrip(",")) for x in (d / "card.m3u8").read_text().splitlines() if x.startswith("#EXTINF")]
        return [((d / f"{i:03d}.ts").read_bytes(), t) for i, t in enumerate(durs)]

