"""Discovery (find titles) and enrichment (resolve the file, metadata, poster, category)."""
from __future__ import annotations

import json
import re
import time

import httpx

from . import artwork, metadata
from .db import DB
from .providers import PROVIDERS, Item, SourceUnavailable

_series_cache: dict[str, dict | None] = {}


def norm(s: str) -> str:
    s = re.sub(r"^(the|a|an) ", "", s.lower().strip())
    return re.sub(r"[^a-z0-9]+", "", s.replace("&", "and"))


def dedupe_key(provider: str, it: Item) -> str:
    if it.kind == "movie":
        return f"movie:{norm(it.title)}:{it.year or provider + it.ref}"
    if it.season and it.episode:
        return f"ep:{norm(it.show or '')}:s{it.season}e{it.episode}"
    generic = not it.title or re.fullmatch(r"episode( no\.? ?\d+)?", it.title, re.I)
    return f"ep:{norm(it.show or '')}:{provider + it.ref if generic else norm(it.title)}"


def add_item(db: DB, provider: str, it: Item, enriched: int = 0) -> int | None:
    """Insert a title unless it is already known. Returns its id, or None for a duplicate."""
    row = db.one("SELECT id, status FROM media WHERE provider=? AND ref=?", provider, it.ref)
    if row:
        if row["status"] == "hidden" and provider == "local":
            db.run("UPDATE media SET status='discovered', error=NULL, enriched=0 WHERE id=?", row["id"])
        return None
    cur = db.run(
        """INSERT OR IGNORE INTO media(provider,ref,kind,title,show,season,episode,year,genres,description,category,popularity,
           license,source_url,size,md5,runtime,dedupe,added_at,enriched) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        provider, it.ref, it.kind, it.title, it.show, it.season, it.episode, it.year, json.dumps(it.genres), it.description,
        it.category, it.popularity, it.license, it.source_url, it.size, it.md5, it.runtime, dedupe_key(provider, it),
        time.time(), enriched)
    if cur.rowcount:
        return cur.lastrowid
    if provider in ("local", "url"):  # the user's own copy replaces the same title from elsewhere
        row = db.one("SELECT * FROM media WHERE dedupe=?", dedupe_key(provider, it))
        if row and row["provider"] not in ("local", "url") and row["status"] in ("discovered", "failed", "hidden"):
            db.run("""UPDATE media SET provider=?, ref=?, category=?, kind=?, source_url=?, size=?, md5=NULL, license=?, popularity=?,
                      status='discovered', enriched=?, error=NULL, attempts=0, next_retry=0, height=NULL WHERE id=?""",
                   provider, it.ref, it.category, it.kind, it.source_url, it.size, it.license, it.popularity, enriched, row["id"])
            return row["id"]
    return None


def discover(db: DB, name: str) -> int:
    p = PROVIDERS[name]
    if not p.enabled(db):
        return 0
    n = sum(1 for it in p.discover(db) if add_item(db, name, it))
    db.set(f"discovered_{name}", time.time())
    if n:
        db.event("info", "discovery", f"{p.label}: {n} new titles")
    return n


def discover_due(db: DB) -> None:
    for name, p in PROVIDERS.items():
        every = float(db.get("discovery_hours")) if p.remote else p.interval_hours
        if time.time() - db.get(f"discovered_{name}", 0) < every * 3600:
            continue
        try:
            discover(db, name)
        except (httpx.HTTPError, KeyError, ValueError) as e:
            db.event("warn", "discovery", f"{p.label} could not be reached ({type(e).__name__}); retrying later")
            db.set(f"discovered_{name}", time.time() - every * 3600 + 600)


def next_unenriched(db: DB, turn: int) -> dict | None:
    """Alternate movies and episodes so every channel gets material quickly."""
    kinds = ("movie", "episode") if turn % 2 == 0 else ("episode", "movie")
    for k in kinds:
        m = db.one("""SELECT * FROM media WHERE enriched=0 AND kind=? AND status!='hidden' AND next_retry<?
                      ORDER BY provider='local' DESC, popularity DESC LIMIT 1""", k, time.time())
        if m:
            return m
    return None


def hide(db: DB, m: dict, why: str) -> None:
    db.run("UPDATE media SET status='hidden', enriched=1, error=? WHERE id=?", why, m["id"])
    db.run("DELETE FROM queue WHERE media_id=?", m["id"])


def enrich(db: DB, m: dict) -> None:
    p = PROVIDERS[m["provider"]]
    src = p.resolve(m)
    for it in src.extra:
        add_item(db, m["provider"], it)
    if m["provider"] == "internet_archive" and src.height and src.height < int(db.get("min_height")):
        raise SourceUnavailable(f"Low resolution ({src.height}p)")
    title, year, desc = src.title or m["title"], m["year"], m["description"] or src.description
    genres, poster = json.loads(m["genres"]), None
    wiki = None
    if m["kind"] == "movie":
        wiki = metadata.film(title, year)
        if m["provider"] == "internet_archive":
            if not wiki:
                raise SourceUnavailable("No matching film in DBpedia")
            if not wiki["american"] or wiki["foreign"]:
                raise SourceUnavailable("Not listed as an American film; US copyright may have been restored")
            if wiki["year"] > int(db.get("archive_max_year")):
                raise SourceUnavailable("Newer than the configured year")
            if re.search(r"sexploitation|erotic|propaganda|pornograph|documentary", " ".join(wiki["categories"]), re.I):
                raise SourceUnavailable("Excluded genre")
        if wiki:
            title, year = wiki["title"], wiki["year"]
    elif m["show"]:
        if m["show"] not in _series_cache:
            _series_cache[m["show"]] = metadata.series(m["show"])
        wiki = _series_cache[m["show"]]
    if wiki:
        poster = wiki.get("poster_url")
        if m["kind"] == "movie":
            desc, genres = desc if len(desc) > 60 else wiki["description"] or desc, wiki["genres"] or genres
        else:
            desc = desc or wiki["description"]
    extra = metadata.tmdb(db.get("tmdb_api_key") or metadata.config.TMDB_API_KEY, m["show"] or title, year, m["kind"] == "episode")
    if extra:
        poster = extra["poster_url"] or poster
        if m["kind"] == "movie":
            desc, genres = extra["description"] or desc, extra["genres"] or genres
    category = m["category"] or metadata.category_for(genres)
    dedupe = m["dedupe"]
    if m["kind"] == "movie" and year:
        dedupe = f"movie:{norm(title)}:{year}"
        if db.one("SELECT 1 FROM media WHERE dedupe=? AND id!=?", dedupe, m["id"]):
            raise SourceUnavailable("Duplicate of another title")
    has_poster = 0
    tmdb_poster = extra and extra["poster_url"]
    for url in (poster, src.poster_url):
        if url and not has_poster:
            try:
                has_poster = int(metadata.save_poster(url, m["id"]))
            except httpx.HTTPError:
                pass
            if has_poster and url == tmdb_poster:
                has_poster = 3
    db.run("""UPDATE media SET title=?, year=?, description=?, genres=?, category=?, dedupe=?, has_poster=MAX(has_poster,?), source_url=?,
              size=?, md5=?, runtime=?, height=COALESCE(height,?), enriched=1, error=NULL WHERE id=?""",
           title, year, desc[:1200], json.dumps(genres), category, dedupe, has_poster, src.url, src.size, src.md5,
           src.runtime or m["runtime"], src.height, m["id"])


def enrich_step(db: DB, turn: int) -> dict | None:
    """Look up one title. Returns it, or None when there is nothing to do."""
    m = next_unenriched(db, turn)
    if not m:
        return None
    try:
        enrich(db, m)
    except SourceUnavailable as e:
        hide(db, m, str(e))
    except httpx.HTTPError as e:
        db.run("UPDATE media SET next_retry=?, error=? WHERE id=?", time.time() + 180, f"Metadata: {type(e).__name__}", m["id"])
        time.sleep(3)
    except Exception as e:  # one odd record must not stall the rest
        hide(db, m, f"Metadata error: {e}"[:200])
    return m


def poster_step(db: DB) -> bool:
    """Once a TMDB key exists, upgrade artwork one title at a time. has_poster: 1 source art, 2 film still, 3 TMDB."""
    key = db.get("tmdb_api_key") or metadata.config.TMDB_API_KEY
    if not key:
        return False
    m = db.one("""SELECT * FROM media WHERE enriched=1 AND status!='hidden' AND kind!='filler' AND has_poster<3 AND tmdb_checked=0
                  ORDER BY (SELECT COUNT(*) FROM queue q WHERE q.media_id=media.id) DESC, popularity DESC LIMIT 1""")
    if not m:
        return False
    try:
        hit = metadata.tmdb(key, m["show"] or m["title"], None if m["show"] else m["year"], m["kind"] == "episode")
        ok = bool(hit and hit["poster_url"] and metadata.save_poster(hit["poster_url"], m["id"]))
    except httpx.HTTPStatusError as e:
        db.event("warn", "metadata", f"TMDB answered {e.response.status_code}; check the API key")
        return False
    except httpx.HTTPError:
        return False
    db.run("UPDATE media SET tmdb_checked=1, has_poster=? WHERE id=?", 3 if ok else m["has_poster"], m["id"])
    return True


def still_step(db: DB) -> bool:
    """Make a poster from one frame for titles without art: queued ones first, then the rest, slowly."""
    m = db.one("""SELECT * FROM media WHERE enriched=1 AND has_poster=0 AND still_tried=0 AND source_url IS NOT NULL
                  AND status NOT IN ('hidden','failed') AND kind!='filler'
                  ORDER BY (SELECT COUNT(*) FROM queue q WHERE q.media_id=media.id) DESC, popularity DESC LIMIT 1""")
    if not m:
        return False
    db.run("UPDATE media SET still_tried=1 WHERE id=?", m["id"])
    artwork.still_poster(db, m, remote=True)
    return True
