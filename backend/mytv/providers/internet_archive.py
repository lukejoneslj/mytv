"""Internet Archive: documented search and metadata APIs, no account needed.

Safety rules, all of which must hold before a title is used:
  * the item carries a public-domain license tag,
  * it was released no later than the configured year,
  * (movies, checked during enrichment) DBpedia lists it as an American film, because foreign films
    often had their US copyright restored even when uploads call them public domain,
  * (television) the show is on the list below of US series with public-domain episodes.
"""
from __future__ import annotations

import html
import re
import time
from typing import Iterable
from urllib.parse import quote

import httpx

from .. import config
from ..db import DB
from .base import Item, Provider, Source, SourceUnavailable

SEARCH = "https://archive.org/advancedsearch.php"
META = "https://archive.org/metadata/"
EXCLUDE = re.compile(
    r"sexploit|\bsex\b|\bnud|burlesque|strip(tease|per)|erotic|exploitation|concentration camp|propaganda|trailer|"
    r"colori[sz]ed|\bserial\b|chapter \d|\bclip\b|blackface|stag film|\bpart \d|\breel \d|teaserama", re.I)
SILENT = re.compile(r"\bsilents?\b", re.I)

# show title -> phrases uploaders use for it
SHOWS: dict[str, list[str]] = {
    "The Adventures of Ozzie and Harriet": ["Ozzie and Harriet"],
    "The Jack Benny Program": ["Jack Benny"],
    "Life with Elizabeth": ["Life with Elizabeth"],
    "My Little Margie": ["My Little Margie"],
    "The Lucy Show": ["The Lucy Show"],
    "The George Burns and Gracie Allen Show": ["Burns and Allen"],
    "Petticoat Junction": ["Petticoat Junction"],
    "The Dick Van Dyke Show": ["Dick Van Dyke Show"],
    "The Bob Cummings Show": ["Love That Bob", "Bob Cummings Show"],
    "The Stu Erwin Show": ["Trouble with Father", "Stu Erwin"],
    "Meet Corliss Archer": ["Corliss Archer"],
    "Make Room for Daddy": ["Make Room for Daddy", "Danny Thomas Show"],
    "Our Miss Brooks": ["Our Miss Brooks"],
    "Topper": ["Topper"],
    "The Beverly Hillbillies": ["Beverly Hillbillies"],
    "I Married Joan": ["I Married Joan"],
    "Mama": ["I Remember Mama", "Mama"],
    "The Eve Arden Show": ["Eve Arden Show"],
    "The Betty Hutton Show": ["Betty Hutton Show"],
    "The Andy Griffith Show": ["Andy Griffith Show"],
    "The Goldbergs": ["The Goldbergs"],
    "The Life of Riley": ["Life of Riley"],
    "Date with the Angels": ["Date with the Angels"],
    "My Hero": ["My Hero"],
    "The Abbott and Costello Show": ["Abbott and Costello Show"],
    "Mister Peepers": ["Mister Peepers", "Mr. Peepers"],
}
def clean_title(raw: str) -> str:
    t = re.sub(r"''|\"|“|”", "", raw)
    t = re.sub(r"\s*[\(\[]?\b(19[1-6]\d)\b[\)\]]?\s*", " ", t)
    t = re.sub(r"[\(\[][^)\]]*(ipod|widescreen|dvd|hd|restored|complete|full|movie|film|public domain)[^)\]]*[\)\]]", "", t, flags=re.I)
    t = re.sub(r"\s*[-–|:]\s*(full movie|full length|hd|dvd quality).*$", "", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" -–:,.")
    m = re.match(r"^(.*), (The|A|An)$", t, re.I)
    return f"{m.group(2)} {m.group(1)}" if m else t


def episode_title(raw: str, show: str, phrases: list[str]) -> str:
    raw = re.sub(r"^\d{2}[ _-]\d{2}[ _-]\d{2}[ _-]+", "", raw)  # leading air date
    raw = re.sub(r"\bS ?\d{1,2} ?[er]p? ?\d{1,2}\b", "", raw, flags=re.I)
    if " " not in raw.strip():  # RunTogetherFileName
        raw = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", raw)
    raw = re.sub(r"(?<=['\"])\s*-\s*", " - ", raw)
    m = re.search(r"(?:Episode )?Title:?\s*['\"]*([^'\")]+)", raw)
    if m:
        return m.group(1).strip(" .'")
    t = re.sub(r"''|\"", "", raw)
    t = re.sub(r"\([^)]*(sitcom|classic|tv|television|aired|comedy)[^)]*\)", "", t, flags=re.I)
    t = re.sub(r"\((episode )?aired[^)]*\)|,? ?aired \d.*$|^19\d\d - |\(?\b19[56]\d\)?$", "", t, flags=re.I)
    t = re.sub(r"\(\d{4}\)|\(\d{1,2}-\d{1,2}-\d{2,4}\)|\bS\d+\s?x?E\d+\b|\(?\d+x\d+\)?", "", t, flags=re.I)
    t = re.sub(r"\b(early|classic|fifties|50's|60's)\s+(tv|television)?\s*(comedy)?\s*:?", "", t, flags=re.I)
    t = re.sub(r"\b(TV-Show|Classic TV|Television Comedy|LMAO)\b:?", "", t, flags=re.I)
    for p in sorted([show, *phrases], key=len, reverse=True):
        t = re.sub(rf"'?\b(The Adventures of |The )?{re.escape(re.sub('^The ', '', p))}\b(?!'s)( Show| Program)?['!]*", "", t, flags=re.I)
    t = re.sub(r"^'?s (?=[A-Z])|^old time television:?\s*|\(?episode: ", "", t, flags=re.I)
    t = re.sub(r"^\W*(\d{1,3}): ", "", t)
    t = re.sub(r"(?<=[A-Za-z])19[56]\d$", "", t)
    t = re.sub(r"\bmisc(ellaneous)? episode\b", "Episode", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" -–:,.'!#()")
    if t.isupper():
        t = t.title()
    return t if len(t) > 2 and t.lower() not in ("the", "show", "hour", "video") else ""


def _text(v) -> str:
    return " ".join(v) if isinstance(v, list) else str(v or "")


def _length(f: dict) -> float:
    raw = str(f.get("length") or 0)
    try:
        if ":" in raw:
            s = 0.0
            for part in raw.split(":"):
                s = s * 60 + float(part)
            return s
        return float(raw)
    except ValueError:
        return 0.0


def rank(f: dict) -> float:
    """Higher is better. Prefers H.264 MP4 at a decent size over huge MPEG-2 masters and tiny proxies."""
    name, fmt = f["name"].lower(), (f.get("format") or "").lower()
    size = int(f.get("size") or 0)
    h = int(f.get("height") or 0)
    if size < 5_000_000 or size > 4_500_000_000:
        return -1
    if name.endswith((".mp4", ".m4v")) and "512kb" not in fmt:
        s = 100.0 if "h.264" in fmt else 85.0
    elif name.endswith(".mkv"):
        s = 70.0
    elif name.endswith((".mpeg", ".mpg", ".avi", ".mov")):
        s = 60.0
    elif "512kb" in fmt:
        s = 40.0
    else:
        return -1
    return s + min(h, 1080) / 60 - size / 1e9 * 2


class InternetArchive(Provider):
    name = "internet_archive"
    label = "Internet Archive (public domain)"

    def __init__(self) -> None:
        self.http = httpx.Client(timeout=45, follow_redirects=True, headers={"User-Agent": config.USER_AGENT},
                                 transport=httpx.HTTPTransport(retries=2))

    def enabled(self, db: DB) -> bool:
        return bool(db.get("archive_enabled"))

    def _search(self, q: str, rows: int) -> list[dict]:
        out: list[dict] = []
        page = 1
        while len(out) < rows:
            n = min(200, rows - len(out))
            r = self.http.get(SEARCH, params={
                "q": q, "fl[]": ["identifier", "title", "year", "downloads", "subject", "licenseurl", "language", "description"],
                "sort[]": "downloads desc", "rows": 200, "page": page, "output": "json"})
            r.raise_for_status()
            docs = r.json()["response"]["docs"]
            out += docs[:n]
            if len(docs) < 200:
                break
            page += 1
            time.sleep(1)
        return out

    def discover(self, db: DB) -> Iterable[Item]:
        max_year = int(db.get("archive_max_year"))
        silent = bool(db.get("include_silent"))
        first = 1915 if silent else 1929
        q = (f"collection:feature_films AND mediatype:movies AND licenseurl:*publicdomain* AND year:[{first} TO {max_year}]")
        for d in self._search(q, int(db.get("archive_max_titles"))):
            text = f"{_text(d.get('title'))} {_text(d.get('subject'))}"
            if EXCLUDE.search(text) or (not silent and SILENT.search(text)):
                continue
            lang = _text(d.get("language")).lower()
            if lang and not lang.startswith("eng"):
                continue
            yield Item(ref=d["identifier"], kind="movie", title=clean_title(_text(d.get("title"))), year=self._year(d),
                       genres=[s.strip().title() for s in re.split(r"[;,]", ";".join(d["subject"]) if isinstance(
                           d.get("subject"), list) else _text(d.get("subject"))) if 2 < len(s.strip()) < 30][:8],
                       popularity=float(d.get("downloads") or 0), license=d.get("licenseurl"))
        for show, phrases in SHOWS.items():
            terms = " OR ".join(f'title:"{p}"' for p in phrases)
            docs = self._search(f"({terms}) AND mediatype:movies AND licenseurl:*publicdomain* AND year:[1948 TO 1966]", 120)
            for d in docs:
                raw = _text(d.get("title"))
                if re.search(r"radio|promo|commercial|bloopers|trailer|biography|interview|documentary|cartoon", raw, re.I):
                    continue
                if show in ("Mama", "Topper", "My Hero", "The Goldbergs") and not re.search(
                        rf"^\W*(I Remember )?{re.escape(re.sub('^The ', '', show))}\b(?! mia)", re.sub(r"^The ", "", raw), re.I):
                    continue
                m = re.search(r"S(?:eason )?0?(\d+)\s?[x,-]?\s?E(?:pisode )?0?(\d+)|\b(\d{1,2})x(\d{2})", raw, re.I)
                season = int(m.group(1) or m.group(3)) if m else None
                ep = int(m.group(2) or m.group(4)) if m else None
                yield Item(ref=d["identifier"], kind="episode", title=episode_title(raw, show, phrases) or "Episode", show=show,
                           season=season, episode=ep, year=self._year(d), genres=["Sitcom"], category="sitcoms",
                           popularity=float(d.get("downloads") or 0), license=d.get("licenseurl"))
            time.sleep(0.5)

    @staticmethod
    def _year(d: dict) -> int | None:
        m = re.search(r"\d{4}", _text(d.get("year")))
        return int(m.group()) if m else None

    def resolve(self, media: dict) -> Source:
        identifier, _, wanted = media["ref"].partition("/")
        r = self.http.get(META + quote(identifier))
        r.raise_for_status()
        d = r.json()
        meta = d.get("metadata")
        if not meta or d.get("is_dark"):
            raise SourceUnavailable("Removed from the Internet Archive")
        if meta.get("access-restricted-item") == "true":
            raise SourceUnavailable("Access restricted by the Internet Archive")
        if "publicdomain" not in _text(meta.get("licenseurl")):
            raise SourceUnavailable("No public-domain license")
        lang = _text(meta.get("language")).lower()
        if lang and not lang.startswith("eng"):
            raise SourceUnavailable(f"Language: {lang}")

        # group derivatives under the original they came from
        groups: dict[str, list[dict]] = {}
        for f in d.get("files", []):
            if f.get("private") == "true" or rank(f) < 0:
                continue
            groups.setdefault(f.get("original") or f["name"], []).append(f)
        best = {k: max(v, key=rank) for k, v in groups.items()}
        # an original may itself be listed as another group's key; keep one entry per title stem
        stems: dict[str, dict] = {}
        for f in best.values():
            stem = re.sub(r"(_512kb|\.ia|_edit|_archive)?\.\w+$", "", f["name"])
            if stem not in stems or rank(f) > rank(stems[stem]):
                stems[stem] = f
        files = sorted(stems.values(), key=lambda f: f["name"])
        if wanted:
            files = [f for f in files if f["name"] == wanted]
        if not files:
            raise SourceUnavailable("No usable video file")

        extra: list[Item] = []
        title = None

        def name_of(f: dict) -> str:
            stem = re.sub(r"\.\w+$", "", f["name"].split("/")[-1])
            stem = re.sub(r"(_512kb|\.ia)$", "", stem).replace("_", " ").replace(".", " ")
            return episode_title(stem, media["show"] or "", SHOWS.get(media["show"] or "", [])) or "Episode"

        if media["kind"] == "episode":
            ok = [f for f in files if 600 <= _length(f) <= 4200]
            if not ok:
                raise SourceUnavailable("No episode-length video")
            chosen = ok[0]
            if len(ok) > 1 and re.fullmatch(r"Episode( No.*)?", media["title"]):
                title = name_of(chosen)
            for f in ok[1:60]:
                extra.append(Item(ref=f"{identifier}/{f['name']}", kind="episode", title=name_of(f), show=media["show"],
                                  year=media["year"], genres=["Sitcom"], category="sitcoms",
                                  popularity=media["popularity"], license=meta.get("licenseurl")))
        else:
            chosen = max(files, key=lambda f: (_length(f) > 2700, rank(f)))
            if _length(chosen) and _length(chosen) < 2700:
                raise SourceUnavailable("Too short to be a feature")
            longest = max(_length(f) for f in files)
            if longest > 2700 and sum(1 for f in files if _length(f) > 2700) > 3:
                raise SourceUnavailable("Multi-film collection")
        art = [f for f in d.get("files", []) if f.get("source") == "original" and f.get("format") in ("JPEG", "PNG", "JPEG Thumb")
               and 25_000 < int(f.get("size") or 0) < 8_000_000 and "thumb" not in f["name"].lower()]
        desc = re.sub(r"<[^>]+>", " ", _text(meta.get("description")))
        desc = html.unescape(re.sub(r"\(DVD Quality\)|https?://\S+|[^.]*\bIMDb\b[^.]*\.?", "", desc))
        return Source(url=f"https://archive.org/download/{quote(identifier)}/{quote(chosen['name'])}",
                      size=int(chosen.get("size") or 0) or None, md5=chosen.get("md5"), runtime=_length(chosen) or None,
                      description=re.sub(r"\s+", " ", desc).strip()[:600],
                      poster_url=f"https://archive.org/download/{quote(identifier)}/{quote(art[0]['name'])}" if art else None,
                      height=int(chosen.get("height") or 0) or None, title=title, extra=extra)
