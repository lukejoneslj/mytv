"""Random 7Movies URLs for browser-tab live channels."""
from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Callable

import httpx

from . import config
from .db import DB

LIBRARY = json.loads(Path(__file__).with_name("autoplay_library.json").read_text())
SHOWS = LIBRARY["shows"]
MOVIES = LIBRARY["movies"]
COLLECTIONS = LIBRARY["collections"]


def show_details(page: str) -> list[dict]:
    """Read season numbers and episode counts from the metadata embedded in a watch page."""
    for match in re.finditer(r"self\.__next_f\.push\((\[1,.*?\])\)</script>", page, re.S):
        try:
            text = json.loads(match.group(1))[1]
            at = text.index('"detail":') + len('"detail":')
            detail, _ = json.JSONDecoder().raw_decode(text[at:])
        except (ValueError, KeyError, IndexError, json.JSONDecodeError):
            continue
        seasons = [
            {"season": s["season_number"], "episodes": s["episode_count"]}
            for s in detail.get("seasons", [])
            if s.get("season_number", 0) > 0 and s.get("episode_count", 0) > 0
        ]
        if seasons:
            return seasons
    raise ValueError("The show's season list could not be read")


def fetch_show(show_id: int) -> list[dict]:
    url = f"{config.AUTOPLAY_BASE_URL}/tv/{show_id}/watch?season=1&episode=1"
    r = httpx.get(url, headers={"User-Agent": config.USER_AGENT}, follow_redirects=True, timeout=20)
    r.raise_for_status()
    return show_details(r.text)


def movie_details(page: str) -> list[dict]:
    """Read movie cards from the trending, popular and other catalog rails on the home page."""
    found = {}
    for match in re.finditer(r"self\.__next_f\.push\((\[1,.*?\])\)</script>", page, re.S):
        try:
            text = json.loads(match.group(1))[1]
        except json.JSONDecodeError:
            continue
        for candidate in re.finditer(r'\{\s*"id"\s*:\s*\d+\s*,\s*"title"\s*:', text):
            try:
                item, _ = json.JSONDecoder().raw_decode(text[candidate.start():])
                year = int(str(item.get("year") or item.get("releaseDate", ""))[:4])
                title = item["title"].strip()
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
            if item.get("mediaType") == "movie" and item.get("id") and title:
                found[item["id"]] = {
                    "id": item["id"], "title": title[:200], "year": year,
                    "genre": str(item.get("genre") or "")[:60], "rating": item.get("rating"),
                }
    return list(found.values())


def fetch_movies() -> list[dict]:
    r = httpx.get(f"{config.AUTOPLAY_BASE_URL}/", headers={"User-Agent": config.USER_AGENT}, follow_redirects=True, timeout=20)
    r.raise_for_status()
    movies = movie_details(r.text)
    if len(movies) < 20:
        raise ValueError("The movie catalog could not be read")
    return movies


class Autoplay:
    def __init__(self, db: DB, fetch: Callable[[int], list[dict]] = fetch_show, rand: random.Random | None = None,
                 discover: Callable[[], list[dict]] = fetch_movies, clock: Callable[[], float] = time.time):
        self.db, self.fetch, self.rand, self.discover, self.clock = db, fetch, rand or random.Random(), discover, clock

    @staticmethod
    def key(cid: int) -> str:
        return f"autoplay_{cid}"

    def get(self, cid: int) -> dict:
        value = self.db.get(self.key(cid), {})
        return {
            "enabled": bool(value.get("enabled", False)),
            "collection": value.get("collection") if value.get("collection") in COLLECTIONS else "sitcoms",
            "current": value.get("current"),
            "recent": value.get("recent", []),
            "last_show": value.get("last_show"),
            "linked_at": value.get("linked_at"),
        }

    def save(self, cid: int, **changes) -> dict:
        current = self.get(cid)
        current.update(changes)
        self.db.set(self.key(cid), current)
        return self.get(cid)

    def configure(self, cid: int, enabled: bool | None, collection: str | None) -> dict:
        changes = {}
        if enabled is not None:
            changes["enabled"] = enabled
        if collection is not None:
            if collection not in COLLECTIONS:
                raise ValueError("No such autoplay library")
            changes["collection"] = collection
            changes["recent"] = []
            changes["last_show"] = None
        return self.save(cid, **changes)

    def seasons(self, show_id: int) -> list[dict]:
        key = f"autoplay_show_{show_id}"
        cached = self.db.get(key, {})
        if cached.get("seasons") and self.clock() - cached.get("at", 0) < 30 * 86400:
            return cached["seasons"]
        seasons = self.fetch(show_id)
        self.db.set(key, {"at": self.clock(), "seasons": seasons})
        return seasons

    def refresh_due(self) -> None:
        """Refresh the home-page catalog twice a day, retaining the last good catalog on errors."""
        cached = self.db.get("autoplay_movie_discovery", {})
        now = self.clock()
        if now - cached.get("at", 0) < 12 * 3600 or now - cached.get("attempt", 0) < 3600:
            return
        self.db.set("autoplay_movie_discovery", {**cached, "attempt": now})
        try:
            discovered = self.discover()
            if len(discovered) < 20:
                raise ValueError("The movie catalog was too small to use")
        except (httpx.HTTPError, ValueError, OSError) as e:
            self.db.event("warn", "autoplay", f"Movie catalog refresh failed; keeping the previous library: {e}")
            return
        self.db.set("autoplay_movie_discovery", {**cached, "attempt": now, "at": now, "movies": discovered})
        self.db.event("info", "autoplay", f"Movie catalog refreshed: {len(discovered)} discovered, {self.movie_count()} total")

    def movies(self) -> list[dict]:
        discovered = self.db.get("autoplay_movie_discovery", {}).get("movies", [])
        return list(({**movie} for movie in {m["id"]: m for m in [*MOVIES, *discovered]}.values()))

    def movie_count(self) -> int:
        return len(self.movies())

    def discovery(self) -> dict:
        cached = self.db.get("autoplay_movie_discovery", {})
        return {"updated_at": cached.get("at"), "discovered": len(cached.get("movies", [])), "total": self.movie_count()}

    def next(self, cid: int) -> dict:
        cfg = self.get(cid)
        selection = self.next_movie(cfg) if cfg["collection"] == "movies" else self.next_episode(cfg)
        current = {"title": selection["title"], "url": selection["url"], "at": time.time()}
        self.save(cid, current=current, recent=[*cfg["recent"], selection["key"]][-50:], last_show=cfg["last_show"])
        return current

    def next_movie(self, cfg: dict) -> dict:
        movies = self.movies()
        recent = set(cfg["recent"][-24:])
        choices = [m for m in movies if f"movie:{m['id']}" not in recent] or movies
        movie = self.rand.choice(choices)
        return {
            "key": f"movie:{movie['id']}",
            "title": f"{movie['title']} ({movie['year']})",
            "url": f"{config.AUTOPLAY_BASE_URL}/movie/{movie['id']}/watch",
        }

    def next_episode(self, cfg: dict) -> dict:
        shows = [s for s in SHOWS if s["id"] != cfg["last_show"]] or SHOWS
        recent = set(cfg["recent"][-50:])
        error = None
        for _ in range(4):
            show = self.rand.choice(shows)
            try:
                seasons = self.seasons(show["id"])
            except (httpx.HTTPError, ValueError, OSError) as e:
                error = e
                continue
            season = self.rand.choice(seasons)
            episode = self.rand.randint(1, season["episodes"])
            key = f"tv:{show['id']}:{season['season']}:{episode}"
            if key in recent and len(recent) < 250:
                episode = episode % season["episodes"] + 1
                key = f"tv:{show['id']}:{season['season']}:{episode}"
            cfg["last_show"] = show["id"]
            return {
                "key": key,
                "title": f"{show['title']} S{season['season']:02d}E{episode:02d}",
                "url": f"{config.AUTOPLAY_BASE_URL}/tv/{show['id']}/watch?season={season['season']}&episode={episode}",
            }
        raise RuntimeError(f"Could not read a show's episode list: {error}")

    def record_state(self, cid: int, title: str, url: str) -> dict:
        cfg = self.get(cid)
        current = dict(cfg.get("current") or {})
        current.update({"title": title or current.get("title", ""), "url": url, "at": time.time()})
        return self.save(cid, current=current, linked_at=time.time())
