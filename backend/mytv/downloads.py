"""Download manager and rolling cache."""
from __future__ import annotations

import hashlib
import shutil
import threading
import time
from pathlib import Path

import httpx

from . import artwork, config
from .db import DB
from .engine import Engine
from .ingest import Cancelled, IngestError, ingest, probe
from .library import enrich, hide
from .providers import PROVIDERS, SourceUnavailable

GB = 1_000_000_000
MAX_ATTEMPTS = 5
PROCESSING_SLOTS = 2


class NoSpace(Exception):
    pass


class DownloadManager:
    def __init__(self, db: DB, engine: Engine, download_dir: Path = config.DOWNLOAD_DIR):
        self.db, self.engine, self.dir = db, engine, download_dir
        self.jobs: dict[int, tuple[threading.Thread, threading.Event]] = {}
        self.lock = threading.RLock()
        self.gate = threading.Condition()
        self.processing: set[int] = set()
        self.waiting: dict[int, float] = {}  # media id -> when it is needed
        self.paused_reason: str | None = None
        self.stopping = threading.Event()

    # ---- startup / shutdown -------------------------------------------
    def recover(self) -> None:
        """Repair state after a crash: unfinished work goes back to the queue, partial files are kept for resuming."""
        for d in self.engine.cache.glob("*.tmp"):
            shutil.rmtree(d, ignore_errors=True)
        self.db.run("UPDATE media SET status='discovered', speed=0 WHERE status IN ('downloading','downloaded','processing')")
        for m in self.db.all("SELECT id FROM media WHERE status='ready'"):
            if not self.engine.playable(m["id"]):
                self.db.run("UPDATE media SET status='discovered', disk_bytes=0, progress=0 WHERE id=?", m["id"])
        ready = {str(m["id"]) for m in self.db.all("SELECT id FROM media WHERE status='ready'")}
        for d in self.engine.cache.iterdir():
            if d.is_dir() and d.name not in ready:
                shutil.rmtree(d, ignore_errors=True)
        active = {str(m["media_id"]) for m in self.db.all("SELECT media_id FROM queue")}
        for f in self.dir.iterdir():
            if f.stem not in active and not f.name.startswith("."):
                f.unlink(missing_ok=True)

    def stop(self) -> None:
        self.stopping.set()
        for t, ev in list(self.jobs.values()):
            ev.set()
        deadline = time.monotonic() + 8
        for t, _ in list(self.jobs.values()):
            t.join(timeout=max(0.1, deadline - time.monotonic()))

    # ---- storage -------------------------------------------------------
    def usage(self) -> dict:
        cache = self.db.one("SELECT COALESCE(SUM(disk_bytes),0) AS n FROM media WHERE status='ready'")["n"]
        parts = 0
        for f in self.dir.iterdir():
            try:
                parts += f.stat().st_size
            except OSError:
                pass
        du = shutil.disk_usage(config.DATA_DIR)
        return {"cache_bytes": cache, "download_bytes": parts, "limit_bytes": int(float(self.db.get("cache_limit_gb")) * GB),
                "free_bytes": du.free, "total_bytes": du.total, "min_free_bytes": int(float(self.db.get("min_free_gb")) * GB)}

    def protected(self) -> set[int]:
        """Titles that must not be deleted: on air, booked, just finished, or queued."""
        now = self.engine.clock()
        grace = max(60.0, float(self.db.get("cleanup_delay_min")) * 60)
        ids = {r["media_id"] for r in self.db.all("SELECT media_id FROM airings WHERE start+duration>?", now - grace)}
        ids |= {r["media_id"] for r in self.db.all("SELECT media_id FROM queue")}
        ids |= {r["id"] for r in self.db.all("SELECT id FROM media WHERE kind='filler'")}
        return ids

    def evict(self, media_id: int) -> bool:
        # engine.lock: nothing can be booked between the check and the delete
        with self.engine.lock:
            if media_id in self.protected():
                return False
            m = self.db.one("SELECT * FROM media WHERE id=? AND status='ready'", media_id)
            if not m:
                return False
            self.db.run("UPDATE media SET status='discovered', progress=0, bytes_done=0, disk_bytes=0 WHERE id=?", media_id)
            shutil.rmtree(self.engine.media_dir(media_id), ignore_errors=True)
        self.db.event("info", "cleanup", f"Deleted video for {label(m)} ({m['disk_bytes'] / GB:.2f} GB), kept its details")
        return True

    def _evictable(self) -> list[dict]:
        keep = self.protected()
        return [m for m in self.db.all(
            """SELECT m.*, (SELECT MAX(start+duration) FROM airings a WHERE a.media_id=m.id) AS aired FROM media m
               WHERE m.status='ready' AND m.kind!='filler' ORDER BY COALESCE(aired, 0)""") if m["id"] not in keep]

    def cleanup(self) -> None:
        if not self.db.get("auto_cleanup"):
            return
        by_cat: dict[str, list[dict]] = {}
        for m in self._evictable():
            by_cat.setdefault(m["category"], []).append(m)
        for cat, items in by_cat.items():
            reserve = int(self.db.get("rerun_reserve_episodes" if cat == "sitcoms" else "rerun_reserve_movies"))
            for m in items[: max(0, len(items) - reserve)]:
                self.evict(m["id"])

    def make_room(self, need: int) -> None:
        """Ensure `need` bytes fit, evicting the rerun reserve if necessary. Raises NoSpace."""
        def short() -> str | None:
            u = self.usage()
            if u["cache_bytes"] + u["download_bytes"] + need > u["limit_bytes"]:
                return f"cache limit of {u['limit_bytes'] / GB:.0f} GB reached"
            if u["free_bytes"] - need < u["min_free_bytes"]:
                return f"less than {u['min_free_bytes'] / GB:.0f} GB would stay free on disk"
            return None

        why = short()
        if why and self.db.get("auto_cleanup"):
            for m in self._evictable():
                self.evict(m["id"])
                why = short()
                if not why:
                    break
        if why:
            raise NoSpace(why)

    # ---- scheduling ----------------------------------------------------
    def tick(self) -> None:
        with self.lock:
            self.jobs = {k: v for k, v in self.jobs.items() if v[0].is_alive()}
            self.cleanup()
            wanted = self.engine.wanted()
            ids = {m["id"] for m in wanted}
            for mid, (_, ev) in self.jobs.items():
                if mid not in ids and not self.db.one("SELECT 1 FROM queue WHERE media_id=?", mid):
                    ev.set()  # removed from its queue
            now = time.time()
            paused = None
            for m in wanted:
                if len(self.jobs) >= int(self.db.get("max_downloads")) + 3:
                    break
                if m["id"] in self.jobs or m["status"] in ("ready", "failed", "hidden") or m["next_retry"] > now:
                    continue
                downloading = self.db.one("SELECT COUNT(*) AS n FROM media WHERE status='downloading'")["n"]
                if downloading >= int(self.db.get("max_downloads")):
                    break
                remote = PROVIDERS[m["provider"]].remote
                have = self._part(m["id"]).stat().st_size if self._part(m["id"]).exists() else 0
                # room for the rest of the source file plus its processed copy
                need = int((m["size"] or GB) * (2.1 if remote else 1.1)) - have
                try:
                    self.make_room(max(need, 0))
                except NoSpace as e:
                    paused = f"Downloads paused: {e}"
                    self.db.event("warn", "storage", paused)
                    break
                ev = threading.Event()
                self.db.run("UPDATE media SET status='downloading', error=NULL WHERE id=?", m["id"])
                t = threading.Thread(target=self._job, args=(m["id"], ev), daemon=True, name=f"job-{m['id']}")
                self.jobs[m["id"]] = (t, ev)
                t.start()
            self.paused_reason = paused

    def retry(self, media_id: int) -> None:
        self.db.run("UPDATE media SET status='discovered', attempts=0, next_retry=0, error=NULL WHERE id=? AND status='failed'", media_id)

    # ---- one title -----------------------------------------------------
    def _part(self, media_id: int) -> Path:
        return self.dir / f"{media_id}.part"

    def _job(self, media_id: int, cancel: threading.Event) -> None:
        m = self.db.one("SELECT * FROM media WHERE id=?", media_id)
        try:
            if not m["source_url"]:
                enrich(self.db, m)
                m = self.db.one("SELECT * FROM media WHERE id=?", media_id)
            if PROVIDERS[m["provider"]].remote:
                path = self._download(m, cancel)
            else:
                path = Path(PROVIDERS[m["provider"]].resolve(m).url)
            self._process(m, path, cancel)
        except Cancelled:
            self.db.run("UPDATE media SET status='discovered', speed=0 WHERE id=?", media_id)
            if not self.stopping.is_set():
                self._part(media_id).unlink(missing_ok=True)
        except SourceUnavailable as e:
            hide(self.db, m, str(e))
            self.db.event("warn", "downloads", f"{label(m)} is not available: {e}")
        except NoSpace as e:
            self.db.run("UPDATE media SET status='discovered', speed=0, next_retry=? WHERE id=?", time.time() + 300, media_id)
            self.db.event("warn", "storage", f"{label(m)}: {e}")
        except Exception as e:
            self._fail(m, e)

    def _fail(self, m: dict, e: Exception) -> None:
        n = self.db.one("SELECT attempts FROM media WHERE id=?", m["id"])["attempts"] + 1
        msg = str(e)[:300] or type(e).__name__
        if n >= MAX_ATTEMPTS or isinstance(e, IngestError) and n >= 2:
            self.db.run("UPDATE media SET status='failed', attempts=?, error=?, speed=0 WHERE id=?", n, msg, m["id"])
            self.db.run("DELETE FROM queue WHERE media_id=?", m["id"])  # the queue refills with something else
            self._part(m["id"]).unlink(missing_ok=True)
            self.db.event("error", "downloads", f"{label(m)} failed: {msg}")
        else:
            wait = min(60 * 2 ** (n - 1), 1800)
            self.db.run("UPDATE media SET status='discovered', attempts=?, error=?, speed=0, next_retry=? WHERE id=?",
                        n, msg, time.time() + wait, m["id"])
            self.db.event("warn", "downloads", f"{label(m)}: {msg}. Retrying in {wait // 60} min")

    def _download(self, m: dict, cancel: threading.Event) -> Path:
        part = self._part(m["id"])
        have = part.stat().st_size if part.exists() else 0
        if m["size"] and have > m["size"]:
            part.unlink()
            have = 0
        if not (m["size"] and have == m["size"]):
            headers = {"User-Agent": config.USER_AGENT, "Accept-Encoding": "identity"}
            if have:
                headers["Range"] = f"bytes={have}-"
            with httpx.Client(timeout=httpx.Timeout(30, read=60), follow_redirects=True) as c, \
                    c.stream("GET", m["source_url"], headers=headers) as r:
                if r.status_code == 416:
                    part.unlink(missing_ok=True)
                    raise IOError("Server refused to resume; starting over")
                if r.status_code in (401, 403, 404, 410):
                    raise SourceUnavailable(f"Server answered {r.status_code}")
                r.raise_for_status()
                if "html" in r.headers.get("content-type", ""):
                    raise SourceUnavailable("Link returned a web page instead of a video")
                if have and r.status_code != 206:
                    have = 0  # server ignored the range: start again
                length = int(r.headers.get("content-length") or 0)
                total = m["size"] or (have + length if length else None)
                done, mark, mark_t = have, have, time.monotonic()
                with open(part, "ab" if have else "wb") as f:
                    for chunk in r.iter_raw():  # unbuffered, so nothing received is lost if the connection drops
                        if cancel.is_set():
                            raise Cancelled()
                        f.write(chunk)
                        done += len(chunk)
                        dt = time.monotonic() - mark_t
                        if dt >= 1:
                            self.db.run("UPDATE media SET bytes_done=?, progress=?, speed=? WHERE id=?",
                                        done, done / total if total else 0, (done - mark) / dt, m["id"])
                            mark, mark_t = done, time.monotonic()
                            if shutil.disk_usage(self.dir).free < int(float(self.db.get("min_free_gb")) * GB) // 2:
                                raise NoSpace("disk is nearly full")
                if total and done != total:
                    raise IOError(f"Connection closed early ({done / 1e6:.0f} of {total / 1e6:.0f} MB)")
        if m["md5"]:
            h = hashlib.md5()
            with open(part, "rb") as f:
                while chunk := f.read(1 << 22):
                    if cancel.is_set():
                        raise Cancelled()
                    h.update(chunk)
            if h.hexdigest() != m["md5"]:
                part.unlink()
                raise IOError("Checksum mismatch, file was damaged in transit")
        self.db.run("UPDATE media SET status='downloaded', bytes_done=?, progress=1, speed=0 WHERE id=?",
                    part.stat().st_size, m["id"])
        return part

    def _enter_gate(self, media_id: int, cancel: threading.Event) -> None:
        """At most PROCESSING_SLOTS ffmpeg jobs; the title needed soonest goes first."""
        with self.gate:
            try:
                while True:
                    if cancel.is_set():
                        raise Cancelled()
                    due = {m["id"]: m["needed_at"] for m in self.engine.wanted()}
                    self.waiting[media_id] = due.get(media_id, float("inf"))
                    if len(self.processing) < PROCESSING_SLOTS and min(self.waiting, key=self.waiting.get) == media_id:
                        self.processing.add(media_id)
                        return
                    self.gate.wait(1)
            finally:
                self.waiting.pop(media_id, None)

    def _process(self, m: dict, path: Path, cancel: threading.Event) -> None:
        self.db.run("UPDATE media SET status='downloaded', speed=0 WHERE id=?", m["id"])
        self._enter_gate(m["id"], cancel)
        try:
            self.db.run("UPDATE media SET status='processing', progress=0 WHERE id=?", m["id"])
            self.make_room(path.stat().st_size)
            last = [0.0]

            def on_progress(f: float) -> None:
                if time.monotonic() - last[0] > 1:
                    last[0] = time.monotonic()
                    self.db.run("UPDATE media SET progress=? WHERE id=?", f, m["id"])

            try:
                probe(str(path))
            except IngestError:
                if PROVIDERS[m["provider"]].remote:
                    path.unlink(missing_ok=True)  # damaged: fetch it again
                raise
            facts = ingest(str(path), self.engine.media_dir(m["id"]), policy=self.db.get("video_policy"),
                           max_height=int(self.db.get("max_height")), normalize=bool(self.db.get("normalize_audio")),
                           progress=on_progress, cancel=cancel)
        finally:
            with self.gate:
                self.processing.discard(m["id"])
                self.gate.notify_all()
        self.db.run("""UPDATE media SET status='ready', progress=1, duration=?, nsegs=?, disk_bytes=?, direct=?, width=?, height=?,
                       attempts=0, error=NULL, speed=0 WHERE id=?""", facts["duration"], facts["nsegs"], facts["disk_bytes"],
                    facts["direct"], facts["width"], facts["height"], m["id"])
        if PROVIDERS[m["provider"]].remote:
            path.unlink(missing_ok=True)
        self.db.event("info", "downloads", f"{label(m)} is ready to air")
        try:
            artwork.still_poster(self.db, self.db.one("SELECT * FROM media WHERE id=?", m["id"]))
        except Exception as e:  # artwork is never worth failing a download for
            self.db.event("warn", "artwork", f"{label(m)}: {e}"[:200])


def label(m: dict) -> str:
    return f"{m['show']}: {m['title']}" if m.get("show") else m["title"]
