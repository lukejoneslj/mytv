"""MyTV server: API, live streams, guide and dashboard."""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import logging.handlers
import os
import re
import socket
import subprocess
import time
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from . import artwork, config, guide, library
from .cast import MAX_SEGMENT, Casts, Refused
from .db import DB, DEFAULT_SETTINGS
from .downloads import DownloadManager, label
from .engine import Engine
from .providers import PROVIDERS, Item
from .supervisor import Supervisor

config.ensure_dirs()
log = logging.getLogger("mytv")
log.setLevel(logging.INFO)
_h = logging.handlers.RotatingFileHandler(config.LOG_DIR / "mytv.log", maxBytes=2_000_000, backupCount=3)
_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_h)

db = DB(config.DB_PATH)
engine = Engine(db)
downloads = DownloadManager(db, engine)
casts = Casts(db)
sup = Supervisor(db)
viewers: dict[tuple[int | str, str], float] = {}
started = time.time()
_turn = 0


def _enrich_tick() -> float:
    global _turn
    _turn += 1
    m = library.enrich_step(db, _turn)
    if m and m["provider"] == "local":
        engine.make_way(m["category"])
    return 1.0 if m or library.poster_step(db) else 20.0


def _art_tick() -> float:
    return 6.0 if library.still_step(db) else 30.0


def _discover_tick() -> None:
    library.discover_due(db)


def _cards_tick() -> None:
    for ch in engine.channels():
        artwork.station_card(db, ch)
    casts.cards()


@asynccontextmanager
async def lifespan(_: FastAPI):
    lock = open(config.DATA_DIR / "mytv.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a second server must not touch this data folder
    except OSError:
        raise RuntimeError("MyTV is already running with this data folder")
    await asyncio.to_thread(downloads.recover)
    sup.add("station-cards", _cards_tick, 15, 300)
    sup.add("live", casts.tick, 5, 60)
    sup.add("scheduler", engine.tick, 5, 60)
    sup.add("downloads", downloads.tick, 4, 120)
    sup.add("discovery", _discover_tick, 60, 1800)
    sup.add("metadata", _enrich_tick, 1, 600)
    sup.add("artwork", _art_tick, 6, 600)
    db.event("info", "system", "MyTV started")

    async def watchdog():
        while True:
            await asyncio.sleep(15)
            sup.watch()

    task = asyncio.create_task(watchdog())
    yield
    task.cancel()
    sup.stop_event.set()
    await asyncio.to_thread(downloads.stop)
    subprocess.run(["pkill", "-P", str(os.getpid())])  # any ffmpeg helper still running
    db.set("last_alive", time.time())


app = FastAPI(title="MyTV", lifespan=lifespan)


@app.middleware("http")
async def guard(request: Request, call_next):
    """Mutations must come from the dashboard itself, not from some other website open in a browser."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "Cross-site request refused"}, 403)
        if not config.ADMIN_FROM_LAN and request.client and request.client.host not in ("127.0.0.1", "::1"):
            return JSONResponse({"detail": "Changes are only allowed from the Mac running MyTV"}, 403)
    return await call_next(request)


def base_url(request: Request) -> str:
    fixed = db.get("public_url") or config.PUBLIC_URL
    return fixed.rstrip("/") if fixed else f"http://{request.headers.get('host', f'localhost:{config.PORT}')}"


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # no packet is sent; this only selects the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def channel(cid: int) -> dict:
    ch = db.one("SELECT * FROM channels WHERE id=?", cid)
    if not ch:
        raise HTTPException(404, "No such channel")
    return ch


def media_row(mid: int) -> dict:
    m = db.one("SELECT * FROM media WHERE id=? AND kind!='filler'", mid)
    if not m:
        raise HTTPException(404, "No such title")
    return m


# ---------------------------------------------------------------- streams
NOCACHE = {"Cache-Control": "no-cache, no-store", "Access-Control-Allow-Origin": "*"}


@app.api_route("/live/{cid}.m3u8", methods=["GET", "HEAD"])
def live(cid: int, request: Request):
    ch = channel(cid)
    text = engine.playlist(cid)
    if not text:
        engine.commit(ch)  # first tune-in after waking from sleep
        text = engine.playlist(cid)
    if not text:
        raise HTTPException(503, "Channel is starting up")
    viewers[(cid, request.client.host if request.client else "?")] = time.time()
    return PlainTextResponse(text, media_type="application/vnd.apple.mpegurl", headers=NOCACHE)


@app.api_route("/media/{mid}/{name}", methods=["GET", "HEAD"])
def segment(mid: int, name: str):
    if not re.fullmatch(r"\d{5}\.ts", name):
        raise HTTPException(404)
    p = config.CACHE_DIR / str(mid) / name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="video/mp2t", headers={"Cache-Control": "max-age=3600", "Access-Control-Allow-Origin": "*"})


@app.get("/stream/{cid}.ts")
async def transport_stream(cid: int):
    """Continuous MPEG-TS for players that cannot do HLS."""
    channel(cid)
    return await ts_of(f"/live/{cid}.m3u8")


async def ts_of(path: str) -> StreamingResponse:
    proc = await asyncio.create_subprocess_exec(
        config.FFMPEG, "-loglevel", "error", "-nostdin", "-i", f"http://127.0.0.1:{config.PORT}{path}",
        "-c", "copy", "-f", "mpegts", "pipe:1", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

    async def body():
        try:
            while data := await proc.stdout.read(65536):
                yield data
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()

    return StreamingResponse(body(), media_type="video/mp2t", headers={"Cache-Control": "no-store"})


@app.api_route("/playlist.m3u", methods=["GET", "HEAD"])
@app.api_route("/playlist.m3u8", methods=["GET", "HEAD"])
def playlist(request: Request, format: str = "hls"):
    return PlainTextResponse(guide.m3u(db, engine, base_url(request), ts=format == "ts", casts=casts),
                             media_type="audio/x-mpegurl", headers=NOCACHE)


@app.api_route("/epg.xml", methods=["GET", "HEAD"])
def epg(request: Request):
    return Response(guide.xmltv(engine, base_url(request), casts), media_type="application/xml", headers=NOCACHE)


@app.get("/logo/{cid}.png")
def logo(cid: int):
    return FileResponse(artwork.logo(channel(cid)), media_type="image/png")


@app.get("/poster/{mid}.jpg")
def poster(mid: int):
    p = config.POSTER_DIR / f"{mid}.jpg"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


# ---------------------------------------------------------------- API
def pub(m: dict) -> dict:
    if m["kind"] == "filler":
        m = {**m, "description": ""}
    return {k: m.get(k) for k in (
        "id", "provider", "kind", "title", "show", "season", "episode", "year", "description", "runtime", "duration", "category",
        "status", "progress", "bytes_done", "size", "speed", "error", "attempts", "next_retry", "disk_bytes", "width", "height",
        "direct", "license", "added_at")} | {"genres": json.loads(m.get("genres") or "[]"),
                                             "poster": f"/poster/{m['id']}.jpg" if m.get("has_poster") else None,
                                             "length": engine.length_of(m)}


def prog(p: dict) -> dict:
    return pub(p) | {"start": p["start"], "length": p["length"], "air_kind": p["air_kind"], "booked": p["booked"],
                     "queue_id": p.get("queue_id")}


def chan_pub(ch: dict) -> dict:
    return {**ch, "blocks": json.loads(ch["blocks"]), "logo": f"/logo/{ch['id']}.png", "stream": f"/live/{ch['id']}.m3u8"}


def watching(key: int | str, now: float) -> int:
    for k in [k for k, t in viewers.items() if now - t > 45]:
        viewers.pop(k, None)
    return sum(1 for (c, _) in list(viewers) if c == key)


def chan_status(ch: dict, now: float) -> dict:
    sched = engine.schedule(ch["id"], now, now + 36 * 3600)
    cur = next((p for p in sched if p["booked"] and p["start"] <= now < p["start"] + p["length"]), None)
    after = [p for p in sched if p["start"] > now]
    q = engine.queue(ch["id"])
    ready = sum(1 for m in q if m["status"] == "ready")
    if not cur:
        state = "off_air"
    elif cur["air_kind"] == "filler":
        state = "standby"
    elif cur["air_kind"] == "rerun":
        state = "rerun"
    else:
        state = "on_air"
    return {**chan_pub(ch), "state": state, "now": prog(cur) if cur else None, "next": [prog(p) for p in after[:3]],
            "queue_length": len(q), "queue_ready": ready, "elapsed": now - cur["start"] if cur else 0,
            "viewers": watching(ch["id"], now)}


@app.get("/api/status")
def status(request: Request):
    now = time.time()
    return {"now": now, "channels": [chan_status(c, now) for c in engine.channels()],
            "casts": [cast_status(c) for c in casts.channels()], "storage": downloads.usage(),
            "paused": downloads.paused_reason, "lan_url": db.get("public_url") or config.PUBLIC_URL or f"http://{lan_ip()}:{config.PORT}",
            "active": db.one("SELECT COUNT(*) AS n FROM media WHERE status IN ('downloading','downloaded','processing')")["n"],
            "failed": db.one("SELECT COUNT(*) AS n FROM media WHERE status='failed'")["n"],
            "titles": db.one("SELECT COUNT(*) AS n FROM media WHERE enriched=1 AND status NOT IN ('hidden') AND kind!='filler'")["n"],
            "pending_metadata": db.one("SELECT COUNT(*) AS n FROM media WHERE enriched=0 AND status!='hidden'")["n"]}


@app.get("/health")
def health():
    now = time.time()
    loops = sup.status()
    chans = {c["name"]: bool(engine.playlist(c["id"])) for c in engine.channels()}
    u = downloads.usage()
    disk_ok = u["free_bytes"] > u["min_free_bytes"] / 2
    ok = all(x["ok"] for n, x in loops.items() if n in ("scheduler", "downloads")) and all(chans.values()) and disk_ok
    return JSONResponse({"ok": ok, "uptime": now - started, "channels": chans, "tasks": loops, "disk_ok": disk_ok}, 200 if ok else 503)


@app.get("/api/guide")
def api_guide(hours: float = 24, back: float = 1):
    now = time.time()
    return {"now": now, "channels": [{**chan_pub(c), "programs": [prog(p) for p in engine.schedule(
        c["id"], now - back * 3600, now + min(hours, 72) * 3600)]} for c in engine.channels()]}


@app.get("/api/channels/{cid}")
def api_channel(cid: int):
    ch = channel(cid)
    t = engine.last_airing(cid)
    start = max(time.time(), t["start"] + t["duration"]) if t else time.time()
    q = []
    for i, m in enumerate(engine.queue(cid)):
        q.append(pub(m) | {"queue_id": m["queue_id"], "start": start, "added_by": m["added_by"], "ahead": i < ch["queue_target"]})
        start += engine.length_of(m)
    hist = db.all("""SELECT a.start, a.duration AS length, a.kind AS air_kind, m.* FROM airings a JOIN media m ON m.id=a.media_id
                     WHERE a.channel_id=? AND a.kind!='filler' AND a.start+a.duration<? ORDER BY a.start DESC LIMIT 20""", cid, time.time())
    return {**chan_status(ch, time.time()), "queue": q, "history": [prog(h | {"booked": True}) for h in hist]}


class Block(BaseModel):
    name: str = Field("", max_length=60)
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{2}:\d{2}$")
    days: list[int] = list(range(7))
    match: str = ""
    year_from: int | None = None
    year_to: int | None = None


class ChannelPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=60, pattern=r'^[^"\r\n]+$')
    number: int | None = Field(None, ge=1, le=9999)
    category: str | None = None
    mode: str | None = Field(None, pattern="^(shuffle|chronological)$")
    queue_target: int | None = Field(None, ge=1, le=50)
    accent: str | None = Field(None, pattern="^#[0-9a-fA-F]{6}$")
    blocks: list[Block] | None = None


@app.patch("/api/channels/{cid}")
def patch_channel(cid: int, body: ChannelPatch):
    ch = channel(cid)
    data = body.model_dump(exclude_none=True)
    if "category" in data and data["category"] not in config.CATEGORIES:
        raise HTTPException(422, "Unknown category")
    if "number" in data and db.number_taken(data["number"], "channels", cid):
        raise HTTPException(409, "That channel number is taken")
    if "category" in data:
        if db.one("SELECT 1 FROM channels WHERE category=? AND id!=?", data["category"], cid):
            raise HTTPException(409, "Another channel already uses that category")
        data["kind"] = "episode" if data["category"] == "sitcoms" else "movie"
    if "blocks" in data:
        data["blocks"] = json.dumps(data["blocks"])
    with engine.lock:
        for k, v in data.items():
            db.run(f"UPDATE channels SET {k}=? WHERE id=?", v, cid)
        if {"category", "mode", "blocks"} & data.keys() and any(data.get(k) != ch[k] for k in data):
            db.run("DELETE FROM queue WHERE channel_id=? AND added_by='auto'", cid)  # replan under the new rules
    engine.fill_queue(channel(cid))
    return chan_pub(channel(cid))


@app.post("/api/channels/{cid}/skip")
def skip(cid: int):
    channel(cid)
    engine.skip(cid)
    return {"ok": True}


@app.post("/api/channels/{cid}/reshuffle")
def reshuffle(cid: int):
    ch = channel(cid)
    db.run("DELETE FROM queue WHERE channel_id=? AND added_by='auto'", cid)
    engine.fill_queue(ch)
    return {"ok": True}


class QueueAdd(BaseModel):
    media_id: int
    front: bool = False


@app.post("/api/channels/{cid}/queue")
def queue_add(cid: int, body: QueueAdd):
    ch = channel(cid)
    m = media_row(body.media_id)
    if m["category"] != ch["category"]:
        raise HTTPException(409, "That title belongs to a different channel's category")
    if m["status"] in ("failed", "hidden"):
        db.run("UPDATE media SET status='discovered', attempts=0, next_retry=0, error=NULL WHERE id=?", m["id"])
    engine.enqueue(cid, body.media_id, body.front)
    return {"ok": True}


@app.delete("/api/channels/{cid}/queue/{mid}")
def queue_remove(cid: int, mid: int):
    db.run("DELETE FROM queue WHERE channel_id=? AND media_id=?", cid, mid)
    return {"ok": True}


class QueueMove(BaseModel):
    media_id: int
    index: int


@app.post("/api/channels/{cid}/queue/move")
def queue_move(cid: int, body: QueueMove):
    engine.move(cid, body.media_id, body.index)
    return {"ok": True}


@app.get("/api/library")
def api_library(category: str = "", q: str = "", status: str = "", limit: int = 120, offset: int = 0):
    where, args = ["m.kind!='filler'", "m.enriched=1"], []
    if status == "hidden":
        where.append("m.status='hidden'")
    else:
        where.append("m.status!='hidden'")
        if status:
            where.append("m.status=?")
            args.append(status)
    if category:
        where.append("m.category=?")
        args.append(category)
    if q:
        where.append("(m.title LIKE ? OR m.show LIKE ? OR m.genres LIKE ? OR CAST(m.year AS TEXT)=?)")
        args += [f"%{q}%"] * 3 + [q]
    w = " AND ".join(where)
    total = db.one(f"SELECT COUNT(*) AS n FROM media m WHERE {w}", *args)["n"]
    rows = db.all(f"""SELECT m.*, (SELECT COUNT(*) FROM airings a WHERE a.media_id=m.id AND a.start<?) AS plays,
                      (SELECT MAX(start) FROM airings a WHERE a.media_id=m.id AND a.start<?) AS last_aired,
                      (SELECT channel_id FROM queue q WHERE q.media_id=m.id) AS queued_on
                      FROM media m WHERE {w} ORDER BY m.status='ready' DESC, m.popularity DESC LIMIT ? OFFSET ?""",
                  time.time(), time.time(), *args, min(limit, 500), offset)
    return {"total": total, "items": [pub(r) | {k: r[k] for k in ("plays", "last_aired", "queued_on")} for r in rows]}


class AddUrl(BaseModel):
    url: str = Field(pattern=r"^https?://", max_length=2000)
    title: str = Field(min_length=1, max_length=200)
    category: str
    year: int | None = None
    show: str | None = None


@app.post("/api/library")
def add_url(body: AddUrl):
    if body.category not in config.CATEGORIES:
        raise HTTPException(422, "Unknown category")
    kind = "episode" if body.category == "sitcoms" else "movie"
    it = Item(ref=body.url, kind=kind, title=body.title, show=body.show or (body.title if kind == "episode" else None),
              year=body.year, category=body.category, license="user-supplied", popularity=1e6)
    from .providers import SourceUnavailable
    probe_row = {"ref": body.url}
    try:
        PROVIDERS["url"].resolve(probe_row)
    except SourceUnavailable as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(422, f"Could not open that link ({type(e).__name__})")
    mid = library.add_item(db, "url", it)
    if not mid:
        raise HTTPException(409, "That title is already in the library")
    library.enrich(db, db.one("SELECT * FROM media WHERE id=?", mid))
    ch = db.one("SELECT * FROM channels WHERE category=?", body.category)
    if ch:
        engine.enqueue(ch["id"], mid, front=True)
    return pub(media_row(mid))


class MediaPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    category: str | None = None
    hidden: bool | None = None


@app.patch("/api/library/{mid}")
def patch_media(mid: int, body: MediaPatch):
    m = media_row(mid)
    if body.title:
        db.run("UPDATE media SET title=? WHERE id=?", body.title, mid)
    if body.category:
        if body.category not in config.CATEGORIES:
            raise HTTPException(422, "Unknown category")
        db.run("UPDATE media SET category=?, kind=? WHERE id=?", body.category,
               "episode" if body.category == "sitcoms" else "movie", mid)
        db.run("DELETE FROM queue WHERE media_id=?", mid)
    if body.hidden is True:
        db.run("DELETE FROM queue WHERE media_id=?", mid)
        downloads.evict(mid)
        db.run("UPDATE media SET status='hidden', error='Hidden by you' WHERE id=? AND status!='ready'", mid)
        if m["status"] == "ready" and media_row(mid)["status"] == "ready":
            raise HTTPException(409, "It is on air or just finished; hide it once it is over")
    if body.hidden is False:
        db.run("UPDATE media SET status='discovered', error=NULL WHERE id=? AND status='hidden'", mid)
    return pub(media_row(mid))


@app.get("/api/downloads")
def api_downloads():
    active = db.all("SELECT * FROM media WHERE status IN ('downloading','downloaded','processing') ORDER BY status")
    chan = {r["media_id"]: r["channel_id"] for r in db.all("SELECT media_id, channel_id FROM queue")}
    wanted = engine.wanted()
    due = {m["id"]: m["needed_at"] for m in wanted}

    def row(m):
        return pub(m) | {"channel_id": chan.get(m["id"]) or m.get("channel_id"), "needed_at": due.get(m["id"])}
    return {"active": [row(m) for m in active], "upcoming": [row(m) for m in wanted if m["status"] == "discovered"],
            "failed": [row(m) for m in db.all("SELECT * FROM media WHERE status='failed' ORDER BY id DESC LIMIT 50")],
            "ready": [row(m) for m in db.all("SELECT * FROM media WHERE status='ready' AND kind!='filler' ORDER BY disk_bytes DESC")],
            "storage": downloads.usage(), "paused": downloads.paused_reason}


@app.post("/api/downloads/{mid}/retry")
def retry(mid: int):
    media_row(mid)
    downloads.retry(mid)
    return {"ok": True}


@app.delete("/api/downloads/{mid}")
def evict(mid: int):
    media_row(mid)
    if not downloads.evict(mid):
        raise HTTPException(409, "In use: it is on air, booked, queued or has only just finished")
    return {"ok": True}


LIMITS = {"cache_limit_gb": (5, 100000), "min_free_gb": (2, 10000), "rerun_reserve_movies": (0, 50), "rerun_reserve_episodes": (0, 200),
          "cleanup_delay_min": (1, 1440), "max_downloads": (1, 6), "plan_ahead_hours": (4, 72), "max_height": (360, 2160), "min_height": (0, 1080),
          "archive_max_year": (1928, 1970), "archive_max_titles": (20, 3000), "discovery_hours": (1, 720)}


@app.get("/api/settings")
def get_settings(request: Request):
    s = db.settings()
    s["tmdb_api_key"] = "•" * 8 if (s["tmdb_api_key"] or config.TMDB_API_KEY) else ""
    host = socket.gethostname().removesuffix(".local")
    return {"values": s, "limits": LIMITS, "categories": config.CATEGORIES,
            "network": {"host": config.HOST, "port": config.PORT, "lan_url": f"http://{lan_ip()}:{config.PORT}",
                        "bonjour_url": f"http://{host}.local:{config.PORT}"},
            "library_dir": str(config.LIBRARY_DIR), "data_dir": str(config.DATA_DIR),
            "providers": [{"name": p.name, "label": p.label, "enabled": p.enabled(db), "last_run": db.get(f"discovered_{n}", 0)}
                          for n, p in PROVIDERS.items()],
            "links": db.all("SELECT * FROM live_links ORDER BY number")}


@app.patch("/api/settings")
def patch_settings(body: dict[str, Any]):
    for k, v in body.items():
        if k not in DEFAULT_SETTINGS:
            raise HTTPException(422, f"Unknown setting {k}")
        if k in LIMITS:
            lo, hi = LIMITS[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                raise HTTPException(422, f"{k} must be between {lo} and {hi}")
        elif not isinstance(v, type(DEFAULT_SETTINGS[k])):
            raise HTTPException(422, f"{k} has the wrong type")
        if k == "video_policy" and v not in ("auto", "always") or k == "after_outage" and v not in ("resume", "live"):
            raise HTTPException(422, f"Invalid value for {k}")
        if k == "public_url" and v and not re.match(r"^https?://", v):
            raise HTTPException(422, "The address must start with http://")
    for k, v in body.items():
        if k == "tmdb_api_key" and "•" in v:
            continue
        db.set(k, v)
        if k == "tmdb_api_key":
            db.run("UPDATE media SET tmdb_checked=0")
    return {"ok": True}


@app.post("/api/discover")
def discover_now():
    for n in PROVIDERS:
        db.set(f"discovered_{n}", 0)
    return {"ok": True}


class Link(BaseModel):
    name: str = Field(min_length=1, max_length=60, pattern=r'^[^"\r\n]+$')
    url: str = Field(pattern=r"^https?://[^\s]+$", max_length=2000)
    number: int = Field(ge=1, le=9999)


@app.post("/api/links")
def add_link(body: Link):
    if db.number_taken(body.number):
        raise HTTPException(409, "That channel number is taken")
    db.run("INSERT INTO live_links(number,name,url) VALUES(?,?,?)", body.number, body.name, body.url)
    return {"ok": True}


@app.delete("/api/links/{lid}")
def del_link(lid: int):
    db.run("DELETE FROM live_links WHERE id=?", lid)
    return {"ok": True}


# ---------------------------------------------------------------- live channels, fed by the Stream page
def cast_channel(cid: int) -> dict:
    ch = casts.get(cid)
    if not ch:
        raise HTTPException(404, "No such live channel")
    return ch


def cast_status(ch: dict) -> dict:
    return casts.status(ch) | {"viewers": watching(f"cast{ch['id']}", time.time())}


@app.api_route("/onair/{cid}.m3u8", methods=["GET", "HEAD"])
def cast_playlist(cid: int, request: Request):
    cast_channel(cid)
    text = casts.playlist(cid)
    if not text:
        raise HTTPException(503, "Channel is starting up")
    viewers[(f"cast{cid}", request.client.host if request.client else "?")] = time.time()
    return PlainTextResponse(text, media_type="application/vnd.apple.mpegurl", headers=NOCACHE)


@app.get("/onair/{cid}.png")
def cast_logo(cid: int):
    ch = cast_channel(cid)
    return FileResponse(artwork.logo({**ch, "id": f"cast{cid}"}), media_type="image/png")


@app.get("/onair/{cid}/stream.ts")
async def cast_transport_stream(cid: int):
    cast_channel(cid)
    return await ts_of(f"/onair/{cid}.m3u8")


@app.api_route("/onair/{cid}/{seq}.ts", methods=["GET", "HEAD"])
def cast_segment(cid: int, seq: int):
    data = casts.segment(cid, seq)
    if data is None:
        raise HTTPException(404)
    return Response(data, media_type="video/mp2t", headers={"Cache-Control": "max-age=60", "Access-Control-Allow-Origin": "*"})


@app.get("/api/cast")
def cast_list():
    return [cast_status(c) for c in casts.channels()]


class CastNew(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=60, pattern=r'^[^"\r\n]+$')


@app.post("/api/cast")
def cast_new(body: CastNew):
    if len(casts.channels()) >= 12:
        raise HTTPException(409, "Twelve live channels is the limit")
    ch = casts.create(body.name)
    casts.cards()
    return cast_status(ch)


class CastPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=60, pattern=r'^[^"\r\n]+$')
    number: int | None = Field(None, ge=1, le=9999)


@app.patch("/api/cast/{cid}")
def cast_patch(cid: int, body: CastPatch):
    cast_channel(cid)
    if body.number and db.number_taken(body.number, "cast_channels", cid):
        raise HTTPException(409, "That channel number is taken")
    for k, v in body.model_dump(exclude_none=True).items():
        db.run(f"UPDATE cast_channels SET {k}=? WHERE id=?", v, cid)
    casts.cards()
    return cast_status(cast_channel(cid))


@app.delete("/api/cast/{cid}")
def cast_delete(cid: int):
    cast_channel(cid)
    try:
        casts.delete(cid)
    except Refused as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/cast/{cid}/segments")
async def cast_push(cid: int, request: Request, session: str = Query(min_length=8, max_length=64), n: int = Query(ge=0),
                    dur: float = 0, disc: bool = False, title: str = Query("", max_length=200), note: str = Query("", max_length=80)):
    cast_channel(cid)
    if int(request.headers.get("content-length") or MAX_SEGMENT + 1) > MAX_SEGMENT:
        raise HTTPException(413, "Segment too large")
    data = await request.body()
    try:
        casts.push(cid, session, n, dur, disc, data, {"title": title.strip(), "note": note, "kbps": round(len(data) * 8 / 1000 / max(dur, .1))})
    except Refused as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"ok": True}


class CastStop(BaseModel):
    session: str | None = Field(None, max_length=64)


@app.post("/api/cast/{cid}/stop")
def cast_stop(cid: int, body: CastStop):
    cast_channel(cid)
    casts.stop(cid, body.session)
    return {"ok": True}


@app.get("/api/events")
def events(limit: int = 100):
    return db.all("SELECT * FROM events ORDER BY id DESC LIMIT ?", min(limit, 500))


# ---------------------------------------------------------------- dashboard
@app.get("/{path:path}")
def spa(path: str):
    if path.startswith(("api/", "live/", "media/", "stream/", "onair/")):
        raise HTTPException(404)
    root = config.FRONTEND_DIST.resolve()
    p = (root / path).resolve()
    if path and p.is_relative_to(root) and p.is_file():
        return FileResponse(p)
    if (root / "index.html").exists():
        return FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})
    return PlainTextResponse("Dashboard not built yet. Run scripts/setup.sh", 503)


def run() -> None:
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning", access_log=False, timeout_graceful_shutdown=3)


if __name__ == "__main__":
    run()
