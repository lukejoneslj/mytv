"""Direct media URLs the user adds by hand. Web pages are rejected: the URL must be a video file."""
from __future__ import annotations

from typing import Iterable

import httpx

from .. import config
from ..db import DB
from .base import Item, Provider, Source, SourceUnavailable


class DirectUrl(Provider):
    name = "url"
    label = "Direct links you add"

    def discover(self, db: DB) -> Iterable[Item]:
        return ()

    def resolve(self, media: dict) -> Source:
        from ..ingest import IngestError, probe
        url = media["ref"]
        if not url.startswith(("http://", "https://")):
            raise SourceUnavailable("Only http and https links are supported")
        with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": config.USER_AGENT}) as c:
            with c.stream("GET", url) as r:
                if r.status_code >= 400:
                    raise SourceUnavailable(f"Server answered {r.status_code}")
                ctype = r.headers.get("content-type", "").lower()
                if ctype.startswith("text/") or "html" in ctype or "mpegurl" in ctype:
                    raise SourceUnavailable(f"That link is a {ctype.split(';')[0]} page, not a video file")
                size = int(r.headers.get("content-length") or 0) or None
                final = str(r.url)
        try:
            info = probe(final)
        except IngestError as e:
            raise SourceUnavailable(str(e))
        return Source(url=url, size=size, runtime=info["duration"])
