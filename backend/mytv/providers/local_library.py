"""Your own files: drop videos into library/<category>/. Originals are read, never changed or deleted."""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Iterable

from .. import config
from ..db import DB
from .base import Item, Provider, Source, SourceUnavailable

VIDEO = {".mp4", ".m4v", ".mkv", ".mov", ".avi", ".mpg", ".mpeg", ".ts", ".webm", ".wmv"}


def parse_name(path: Path) -> dict:
    name = re.sub(r"[._]", " ", path.stem)
    m = re.search(r"^(.*?)[\s-]*\bS(\d{1,2})\s?E(\d{1,3})\b[\s-]*(.*)$", name, re.I)
    if m:
        show = re.sub(r"\(?\b(19|20)\d{2}\b\)?", "", m.group(1)).strip(" -") or path.parent.name
        return {"show": show, "season": int(m.group(2)), "episode": int(m.group(3)),
                "title": _trim(m.group(4)) or f"Episode {int(m.group(3))}", "year": None}
    y = re.search(r"\b(19[0-9]{2}|20[0-4][0-9])\b(?!p)", name)
    title = _trim(name[: y.start()] if y and y.start() > 1 else name)
    return {"show": None, "season": None, "episode": None, "title": title, "year": int(y.group()) if y else None}


def _trim(t: str) -> str:
    t = re.split(r"\b(1080p|720p|480p|2160p|bluray|web-?dl|webrip|hdtv|x26[45]|h ?26[45]|dvdrip|remux)\b", t, flags=re.I)[0]
    return re.sub(r"\s+", " ", t).strip(" -([")


class LocalLibrary(Provider):
    name = "local"
    label = "Your library folder"
    interval_hours = 1 / 12
    remote = False

    def discover(self, db: DB) -> Iterable[Item]:
        seen = set()
        for category in config.CATEGORIES:
            root = config.LIBRARY_DIR / category
            if not root.exists():
                continue
            for p in sorted(root.rglob("*")):
                if p.suffix.lower() not in VIDEO or p.name.startswith(".") or not p.is_file():
                    continue
                st = p.stat()
                if time.time() - st.st_mtime < 20 or st.st_size == 0:
                    continue  # still being copied in
                ref = str(p.relative_to(config.LIBRARY_DIR))
                seen.add(ref)
                info = parse_name(p)
                kind = "episode" if category == "sitcoms" else "movie"
                if kind == "episode" and not info["show"]:
                    info["show"] = p.parent.name if p.parent != root else info["title"]
                yield Item(ref=ref, kind=kind, category=category, size=st.st_size, popularity=1e6, license="user-supplied", **info)
        for m in db.all("SELECT id, ref FROM media WHERE provider='local' AND status NOT IN ('hidden','ready')"):
            if m["ref"] not in seen:
                db.run("UPDATE media SET status='hidden', error='File removed from library' WHERE id=?", m["id"])
                db.run("DELETE FROM queue WHERE media_id=?", m["id"])

    def resolve(self, media: dict) -> Source:
        from ..ingest import IngestError, probe
        p = (config.LIBRARY_DIR / media["ref"]).resolve()
        if not p.is_relative_to(config.LIBRARY_DIR.resolve()) or not p.exists():
            raise SourceUnavailable("File is no longer in the library folder")
        try:
            info = probe(str(p))
        except IngestError as e:
            raise SourceUnavailable(str(e))
        return Source(url=str(p), size=p.stat().st_size, runtime=info["duration"])
