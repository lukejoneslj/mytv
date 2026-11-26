"""Broadcast engine.

Every title is cut into HLS segments once, when it is ingested. A channel's live playlist is then a
pure function of (airings table, wall clock): no encoder runs while a channel is on air, so four
channels cost almost nothing, and a crash or restart cannot lose the programme position.

An *airing* is an immutable booking of a ready title at an exact time. Airings on a channel are
back to back; segment sequence and discontinuity numbers are stored so they survive restarts.
The *queue* holds what comes after the last airing; its times are projections.
"""
from __future__ import annotations

import datetime as dt
import json
import random
import threading
import time
from bisect import bisect_right
from functools import lru_cache
from pathlib import Path
from typing import Callable

from . import config
from .db import DB

TARGET_DURATION = 12      # constant per RFC 8216; ingest guarantees no segment is longer
WINDOW_BACK = 30.0
WINDOW_AHEAD = 36.0       # players sit 3 target durations behind the end, i.e. at wall-clock "now"
COMMIT_AHEAD = 600.0      # book real programmes this far ahead
FILLER_AHEAD = 90.0       # fallback is booked in small bites so a real title can take over quickly
RESUME_MIN_LEFT = 120.0
OWN = ("local", "url")
DEFAULT_RUNTIME = {"movie": 5400.0, "episode": 1560.0, "filler": 30.0}


@lru_cache(maxsize=64)
def _load_index(path: str, mtime: float) -> tuple[list[float], list[float]]:
    segs = json.loads(Path(path).read_text())["segments"]
    cum = [0.0]
    for s in segs:
        cum.append(cum[-1] + s)
    return segs, cum


class Engine:
    def __init__(self, db: DB, cache_dir: Path = config.CACHE_DIR, clock: Callable[[], float] = time.time):
        self.db = db
        self.cache = cache_dir
        self.clock = clock
        self.lock = threading.RLock()
        self.rng = random.Random()

    # ---- files ---------------------------------------------------------
    def media_dir(self, media_id: int) -> Path:
        return self.cache / str(media_id)

    def index(self, media_id: int) -> tuple[list[float], list[float]] | None:
        p = self.media_dir(media_id) / "index.json"
        try:
            return _load_index(str(p), p.stat().st_mtime)
        except (OSError, ValueError, KeyError):
            return None

    def playable(self, media_id: int) -> bool:
        idx = self.index(media_id)
        if not idx or not idx[0]:
            return False
        d = self.media_dir(media_id)
        return (d / "00000.ts").exists() and (d / f"{len(idx[0]) - 1:05d}.ts").exists()

    # ---- queries -------------------------------------------------------
    def channels(self) -> list[dict]:
        return self.db.all("SELECT * FROM channels WHERE enabled=1 ORDER BY number")

    def last_airing(self, cid: int) -> dict | None:
        return self.db.one("SELECT * FROM airings WHERE channel_id=? ORDER BY start DESC LIMIT 1", cid)

    def current(self, cid: int, at: float | None = None) -> dict | None:
        at = self.clock() if at is None else at
        return self.db.one(
            "SELECT * FROM airings WHERE channel_id=? AND start<=? AND start+duration>? ORDER BY start DESC LIMIT 1",
            cid, at, at,
        )

    def queue(self, cid: int) -> list[dict]:
        return self.db.all(
            """SELECT q.id AS queue_id, q.position, q.added_by, m.* FROM queue q JOIN media m ON m.id=q.media_id
               WHERE q.channel_id=? ORDER BY q.position, q.id""", cid)

    def schedule(self, cid: int, since: float, until: float) -> list[dict]:
        """Booked airings plus projected queue items, in order."""
        out = []
        for a in self.db.all(
            """SELECT a.start, a.duration AS length, a.kind AS air_kind, a.seg_offset, m.* FROM airings a
               JOIN media m ON m.id=a.media_id
               WHERE a.channel_id=? AND a.start+a.duration>? AND a.start<? ORDER BY a.start""", cid, since, until):
            # merge a run of fallback clips into one guide entry
            if out and a["air_kind"] == "filler" and out[-1]["air_kind"] == "filler":
                out[-1]["length"] = a["start"] + a["length"] - out[-1]["start"]
            else:
                out.append({**a, "booked": True})
        last = self.last_airing(cid)
        t = max(last["start"] + last["duration"], self.clock()) if last else self.clock()
        for m in self.queue(cid):
            if t >= until:
                break
            length = self.length_of(m)
            if t + length > since:
                out.append({**m, "start": t, "length": length, "air_kind": "program", "booked": False})
            t += length
        return out

    @staticmethod
    def length_of(m: dict) -> float:
        return m.get("duration") or m.get("runtime") or DEFAULT_RUNTIME.get(m["kind"], 1800.0)

    # ---- live playlist -------------------------------------------------
    def playlist(self, cid: int) -> str | None:
        now = self.clock()
        lo, hi = now - WINDOW_BACK, now + WINDOW_AHEAD
        airings = self.db.all(
            "SELECT * FROM airings WHERE channel_id=? AND start<? AND start+duration>? ORDER BY start", cid, hi, lo)
        rows: list[tuple[dict, int, float, float]] = []
        for a in airings:
            idx = self.index(a["media_id"])
            if not idx:
                continue
            segs, cum = idx
            base = cum[a["seg_offset"]]
            first = max(0, bisect_right(cum, base + lo - a["start"]) - 1 - a["seg_offset"])
            for i in range(first, a["nsegs"]):
                k = a["seg_offset"] + i
                if k >= len(segs):
                    break
                t0 = a["start"] + cum[k] - base
                if t0 >= hi:
                    break
                if t0 + segs[k] > lo:
                    rows.append((a, i, t0, segs[k]))
        if not rows:
            return None
        a0, i0 = rows[0][0], rows[0][1]
        out = [
            "#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-INDEPENDENT-SEGMENTS",
            f"#EXT-X-TARGETDURATION:{TARGET_DURATION}",
            f"#EXT-X-MEDIA-SEQUENCE:{a0['seq_base'] + i0}",
            f"#EXT-X-DISCONTINUITY-SEQUENCE:{a0['disc_base']}",
        ]
        prev = None
        for a, i, t0, length in rows:
            if prev is not None and a["id"] != prev:
                out.append("#EXT-X-DISCONTINUITY")
            if a["id"] != prev:
                stamp = dt.datetime.fromtimestamp(t0, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                out.append(f"#EXT-X-PROGRAM-DATE-TIME:{stamp}Z")
            prev = a["id"]
            out.append(f"#EXTINF:{length:.5f},")
            out.append(f"/media/{a['media_id']}/{a['seg_offset'] + i:05d}.ts")
        return "\n".join(out) + "\n"

    # ---- booking -------------------------------------------------------
    def _book(self, cid: int, media_id: int, start: float, kind: str, offset: int = 0) -> dict:
        segs, cum = self.index(media_id)
        n = len(segs) - offset
        prev = self.last_airing(cid)
        a = dict(
            channel_id=cid, media_id=media_id, start=start, duration=cum[-1] - cum[offset], seg_offset=offset, nsegs=n,
            seq_base=prev["seq_base"] + prev["nsegs"] if prev else 0,
            disc_base=prev["disc_base"] + 1 if prev else 0, kind=kind,
        )
        self.db.run(
            """INSERT INTO airings(channel_id,media_id,start,duration,seg_offset,nsegs,seq_base,disc_base,kind)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            *a.values(),
        )
        return a

    def _pick(self, ch: dict) -> tuple[int, str] | None:
        """Next title to book: first ready queue item, else a rerun, else the fallback clip."""
        for m in self.queue(ch["id"]):
            if m["status"] != "ready":
                continue  # not downloaded yet: it keeps its place, something ready goes first
            if self.playable(m["id"]):
                self.db.run("DELETE FROM queue WHERE id=?", m["queue_id"])
                return m["id"], "program"
            self.mark_missing(m["id"])
        last = self.last_airing(ch["id"])
        ready = self.db.all(
            """SELECT m.id, (SELECT MAX(start) FROM airings a WHERE a.media_id=m.id) AS aired FROM media m
               WHERE m.category=? AND m.kind=? AND m.status='ready' ORDER BY aired IS NOT NULL, aired""",
            ch["category"], ch["kind"])
        ready = [r for r in ready if self.playable(r["id"])]
        fresh = [r for r in ready if not last or r["id"] != last["media_id"]]
        if fresh or ready:
            r = (fresh or ready)[0]
            return r["id"], "rerun" if r["aired"] else "program"
        f = self.db.one("SELECT id FROM media WHERE provider='system' AND ref=? AND status='ready'", f"filler-{ch['id']}")
        if f and self.playable(f["id"]):
            return f["id"], "filler"
        return None

    def mark_missing(self, media_id: int) -> None:
        self.db.run(
            "UPDATE media SET status='discovered', progress=0, disk_bytes=0 WHERE id=? AND status='ready' AND kind!='filler'",
            media_id)

    def commit(self, ch: dict) -> None:
        """Book airings so the channel is covered COMMIT_AHEAD seconds ahead. Safe to call at any time."""
        with self.lock, self.db.tx():
            now = self.clock()
            self._drop_broken(ch["id"], now)
            last = self.last_airing(ch["id"])
            end = last["start"] + last["duration"] if last else 0.0
            if end < now - 1:
                end = now
                if last and self._resume(ch["id"], now):
                    last = self.last_airing(ch["id"])
                    end = last["start"] + last["duration"]
            while end < now + COMMIT_AHEAD:
                pick = self._pick(ch)
                if not pick:
                    return
                media_id, kind = pick
                if kind == "filler" and end >= now + FILLER_AHEAD:
                    return
                a = self._book(ch["id"], media_id, end, kind)
                if kind != "program":
                    self._note_fallback(ch, kind)
                end += a["duration"]

    def _note_fallback(self, ch: dict, kind: str) -> None:
        if kind == "filler":
            self.db.event("warn", "engine", f"{ch['name']}: nothing ready, showing the station card")
        else:
            self.db.event("info", "engine", f"{ch['name']}: nothing new ready, airing a rerun")

    def _drop_broken(self, cid: int, now: float) -> None:
        """If someone deleted cached files under us, cancel those airings instead of going black."""
        broken = None
        for a in self.db.all("SELECT * FROM airings WHERE channel_id=? AND start+duration>? ORDER BY start", cid, now):
            if not self.playable(a["media_id"]):
                broken = a
                break
        if not broken:
            return
        self.mark_missing(broken["media_id"])
        self.db.event("error", "engine", f"Cached video for title {broken['media_id']} disappeared, rescheduling")
        self._cut(cid, broken, max(now, broken["start"]))

    def _cut(self, cid: int, a: dict, at: float) -> None:
        """Delete every airing after `a` (their titles return to the queue front) and end `a` at the
        first segment boundary at or after `at`."""
        later = self.db.all("SELECT * FROM airings WHERE channel_id=? AND start>? ORDER BY start DESC", cid, a["start"])
        for x in later:
            self.db.run("DELETE FROM airings WHERE id=?", x["id"])
            if x["kind"] == "program":
                self._push_front(cid, x["media_id"])
        idx = self.index(a["media_id"])
        n = 0
        length = 0.0
        if idx:
            segs, cum = idx
            base = cum[a["seg_offset"]]
            while n < a["nsegs"] and a["start"] + length < at:
                n += 1
                length = cum[a["seg_offset"] + n] - base
        if n == 0:
            self.db.run("DELETE FROM airings WHERE id=?", a["id"])
        else:
            self.db.run("UPDATE airings SET nsegs=?, duration=? WHERE id=?", n, length, a["id"])

    def _push_front(self, cid: int, media_id: int) -> None:
        r = self.db.one("SELECT MIN(position) AS p FROM queue WHERE channel_id=?", cid)
        pos = (r["p"] if r["p"] is not None else 1.0) - 1.0
        self.db.run("INSERT OR IGNORE INTO queue(channel_id,media_id,position) VALUES(?,?,?)", cid, media_id, pos)

    def _resume(self, cid: int, now: float) -> bool:
        """After an outage, pick the interrupted programme up where it stopped."""
        alive = self.db.get("last_alive", 0)
        if self.db.get("after_outage") != "resume" or not alive or now - alive < 60:
            return False
        a = self.current(cid, alive)
        if not a or a["kind"] == "filler" or not self.playable(a["media_id"]):
            return False
        left = a["start"] + a["duration"] - alive
        if left < RESUME_MIN_LEFT:
            return False
        _, cum = self.index(a["media_id"])
        offset = max(0, bisect_right(cum, cum[a["seg_offset"]] + alive - a["start"]) - 1)
        self._book(cid, a["media_id"], now, a["kind"], offset)
        self.db.event("info", "engine", f"Channel {cid}: resumed after an outage of {int((now - alive) / 60)} min")
        return True

    def skip(self, cid: int) -> None:
        """End the current programme just past what players have already been shown."""
        with self.lock, self.db.tx():
            now = self.clock()
            cut = now + WINDOW_AHEAD + TARGET_DURATION
            a = self.current(cid, cut) or self.current(cid)
            if not a:
                return
            # the next airing may already be inside the published window: keep it
            self._cut(cid, a, max(cut, a["start"]))
        ch = self.db.one("SELECT * FROM channels WHERE id=?", cid)
        self.commit(ch)

    # ---- queue ---------------------------------------------------------
    def enqueue(self, cid: int, media_id: int, front: bool = False, by: str = "manual") -> None:
        with self.lock, self.db.tx():
            self.db.run("DELETE FROM queue WHERE channel_id=? AND media_id=?", cid, media_id)
            agg = "MIN" if front else "MAX"
            r = self.db.one(f"SELECT {agg}(position) AS p FROM queue WHERE channel_id=?", cid)
            pos = (r["p"] or 0.0) + (-1.0 if front else 1.0)
            self.db.run("INSERT INTO queue(channel_id,media_id,position,added_by) VALUES(?,?,?,?)", cid, media_id, pos, by)

    def move(self, cid: int, media_id: int, index: int) -> None:
        with self.lock, self.db.tx():
            ids = [m["id"] for m in self.queue(cid)]
            if media_id not in ids:
                return
            ids.remove(media_id)
            ids.insert(max(0, min(index, len(ids))), media_id)
            for i, mid in enumerate(ids):
                self.db.run("UPDATE queue SET position=? WHERE channel_id=? AND media_id=?", float(i), cid, mid)

    def active_block(self, ch: dict, at: float) -> dict | None:
        t = dt.datetime.fromtimestamp(at)
        minute = t.hour * 60 + t.minute
        for b in json.loads(ch["blocks"] or "[]"):
            try:
                s = sum(int(x) * k for x, k in zip(b["start"].split(":"), (60, 1)))
                e = sum(int(x) * k for x, k in zip(b["end"].split(":"), (60, 1)))
            except (KeyError, ValueError):
                continue
            day = t.weekday()
            if s <= e:
                inside = s <= minute < e
            else:  # crosses midnight
                inside = minute >= s or minute < e
                if minute < e:
                    day = (day - 1) % 7
            if inside and day in (b.get("days") or range(7)):
                return b
        return None

    @staticmethod
    def block_match(b: dict, m: dict) -> bool:
        text = (b.get("match") or "").strip().lower()
        if text:
            hay = " ".join([m["title"], m["show"] or "", m["genres"], m["description"] or ""]).lower()
            if text not in hay:
                return False
        y = m["year"] or 0
        if b.get("year_from") and y < int(b["year_from"]):
            return False
        if b.get("year_to") and (not y or y > int(b["year_to"])):
            return False
        return True

    def fill_queue(self, ch: dict) -> int:
        """Plan the channel `plan_ahead_hours` out. Planning is metadata only; downloads follow it."""
        with self.lock, self.db.tx():
            now = self.clock()
            horizon = now + float(self.db.get("plan_ahead_hours")) * 3600
            last = self.last_airing(ch["id"])
            end = max(now, last["start"] + last["duration"]) if last else now
            q = self.queue(ch["id"])
            end += sum(self.length_of(m) for m in q)
            count, added = len(q), 0
            if end >= horizon and count >= ch["queue_target"]:
                return 0
            pool = self.db.all(
                """SELECT m.*, (SELECT MAX(start) FROM airings a WHERE a.media_id=m.id) AS aired FROM media m
                   WHERE m.category=? AND m.kind=? AND m.enriched=1 AND m.status NOT IN ('failed','hidden')
                     AND NOT (m.provider='internet_archive' AND COALESCE(m.height, 9999) < ?)
                     AND m.id NOT IN (SELECT media_id FROM queue)""", ch["category"], ch["kind"], self.db.get("min_height"))
            on_air = {r["media_id"] for r in self.db.all(
                "SELECT media_id FROM airings WHERE channel_id=? AND start+duration>?", ch["id"], now - 6 * 3600)}
            rest = [m for m in pool if m["id"] not in on_air]
            prev_show = q[-1]["show"] if q else None
            while (end < horizon or count < ch["queue_target"]) and rest and added < 150:
                m = self._choose(ch, rest, end, prev_show)
                rest.remove(m)
                self.enqueue(ch["id"], m["id"], by="auto")
                prev_show = m["show"]
                end += self.length_of(m)
                count += 1
                added += 1
            return added

    def make_way(self, category: str) -> None:
        """The user added a file: drop the far end of the plan so new titles air within a few programmes."""
        with self.lock, self.db.tx():
            ch = self.db.one("SELECT * FROM channels WHERE category=?", category)
            for m in self.queue(ch["id"])[ch["queue_target"]:] if ch else []:
                if m["added_by"] == "auto" and m["status"] == "discovered" and m["provider"] not in OWN:
                    self.db.run("DELETE FROM queue WHERE id=?", m["queue_id"])

    def _choose(self, ch: dict, rest: list[dict], at: float, prev_show: str | None) -> dict:
        pool = rest
        block = self.active_block(ch, at)
        if block:
            pool = [m for m in rest if self.block_match(block, m)] or rest
        unseen = [m for m in pool if not m["aired"]]
        if unseen:
            pool = [m for m in unseen if m["provider"] in OWN] or unseen  # the user's new files first
        else:  # everything has aired: draw from the half that aired longest ago
            pool = sorted(pool, key=lambda m: m["aired"])
            pool = pool[: max(1, (len(pool) + 1) // 2)]
        if ch["mode"] == "chronological":
            return min(pool, key=lambda m: (m["show"] or "", m["season"] or 0, m["episode"] or 0, m["year"] or 9999, m["title"])
                       if ch["kind"] == "episode" else (m["year"] or 9999, m["title"]))
        other = [m for m in pool if m["show"] != prev_show or not m["show"]]
        pool = other or pool
        if ch["kind"] == "episode":  # pick a show first so a long-running one does not dominate
            shows: dict[str, list[dict]] = {}
            for m in pool:
                shows.setdefault(m["show"] or m["title"], []).append(m)
            names = sorted(shows)
            show = self.rng.choices(names, weights=[min(len(shows[n]), 8) for n in names])[0]
            return self.rng.choice(shows[show])
        return self.rng.choices(pool, weights=[1.0 + (m["popularity"] or 0) ** 0.5 / 100 for m in pool])[0]

    # ---- heartbeat -----------------------------------------------------
    def tick(self) -> None:
        for ch in self.channels():
            self.fill_queue(ch)
            self.commit(ch)
        self.db.set("last_alive", self.clock())
        self.db.run("DELETE FROM airings WHERE kind='filler' AND start<?", self.clock() - 86400)

    # ---- what must stay on disk ---------------------------------------
    def wanted(self) -> list[dict]:
        """Queue items that should be on disk, soonest first."""
        out = []
        now = self.clock()
        for ch in self.channels():
            last = self.last_airing(ch["id"])
            t = max(now, last["start"] + last["duration"]) if last else now
            for i, m in enumerate(self.queue(ch["id"])):
                if i >= ch["queue_target"]:
                    break
                out.append({**m, "needed_at": t, "channel_id": ch["id"], "channel_name": ch["name"]})
                t += self.length_of(m)
        return sorted(out, key=lambda m: m["needed_at"])
