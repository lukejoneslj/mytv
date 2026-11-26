"""Plots, genres and posters. DBpedia and TVmaze need no key; TMDB is used as well when a key is set."""
from __future__ import annotations

import io
import re
import time
from urllib.parse import unquote

import httpx
from PIL import Image

from . import config

GENRES = {
    "Comedy": r"comed(y|ies)|screwball|slapstick|farce|parod", "Mystery": r"myster|detective|whodunit",
    "Thriller": r"thriller|suspense", "Film Noir": r"\bnoir", "Crime": r"crime|gangster|heist",
    "Drama": r"\bdrama|melodrama", "Western": r"\bwestern", "Horror": r"horror", "Science Fiction": r"science fiction",
    "Adventure": r"adventure|swashbuckl", "Musical": r"musical", "Romance": r"roman(ce|tic)", "War": r"\bwar\b",
    "Fantasy": r"fantasy", "Spy": r"\bspy\b", "Action": r"\baction\b", "Animation": r"animat",
}
TMDB_GENRES = {28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime", 18: "Drama", 10751: "Family",
               14: "Fantasy", 36: "History", 27: "Horror", 10402: "Musical", 9648: "Mystery", 10749: "Romance",
               878: "Science Fiction", 53: "Thriller", 10752: "War", 37: "Western"}
http = httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": config.USER_AGENT},
                    transport=httpx.HTTPTransport(retries=2))


def genres_in(text: str) -> list[str]:
    return [g for g, rx in GENRES.items() if re.search(rx, text, re.I)]


SMALL = {"a", "an", "the", "of", "to", "in", "on", "at", "for", "and", "or", "with", "from", "by", "is", "vs"}


def heading_case(t: str) -> str:
    """The capitalisation encyclopedias use, so titles match exactly."""
    words = t.split()
    out = []
    for i, w in enumerate(words):
        low = w.lower()
        out.append(low if low in SMALL and 0 < i < len(words) - 1 else w if not w.isupper() else w.capitalize())
    if out:
        out[0] = out[0][:1].upper() + out[0][1:]
    return " ".join(out)


def _sparql(query: str) -> httpx.Response:
    for attempt in range(4):  # the public endpoint drops a request now and then
        r = http.get("https://dbpedia.org/sparql", headers={"Accept": "application/sparql-results+json"}, params={"query": query})
        if r.status_code < 500:
            break
        time.sleep(1 + attempt)
    r.raise_for_status()
    return r


def film(title: str, year: int | None) -> dict | None:
    """Look a film up in DBpedia (open data drawn from Wikipedia). Needs no key."""
    names = []
    for t in dict.fromkeys((heading_case(title), title)):
        names += [f"{t} ({y} film)" for y in ((year, year - 1, year + 1) if year else ())] + [f"{t} (film)", t]
    values = " ".join('"%s"@en' % n.replace("\\", "").replace('"', "") for n in names)
    pages: dict[str, dict] = {}
    for link in ("?f rdfs:label ?l .", "?x rdfs:label ?l . ?x dbo:wikiPageRedirects ?f ."):
        r = _sparql(
            f'SELECT ?f ?c ?t WHERE {{ VALUES ?l {{ {values} }} {link} ?f dct:subject ?c . '
            f'OPTIONAL {{ ?f dbo:description ?t FILTER(lang(?t)="en") }} }}')
        for b in r.json()["results"]["bindings"]:
            page = pages.setdefault(b["f"]["value"], {"cats": [], "text": ""})
            page["cats"].append(b["c"]["value"].rsplit("Category:", 1)[-1].replace("_", " "))
            page["text"] = b.get("t", {}).get("value", "")
        if any(re.fullmatch(r"\d{4} (?:.* )?films", c) for p in pages.values() for c in p["cats"]):
            break
    best = None
    for uri, page in pages.items():
        cats, text = list(dict.fromkeys(page["cats"])), page["text"]
        ym = next((m for c in cats if (m := re.fullmatch(r"(\d{4}) (?:.* )?films", c))), None)
        if not ym:
            continue
        py = int(ym.group(1))
        if year and abs(py - year) > 1:
            continue
        name = unquote(uri.rsplit("/", 1)[-1]).replace("_", " ")
        hit = {"title": re.sub(r"\s*\([^)]*\)$", "", name), "year": py,
               "description": text, "categories": cats, "genres": genres_in(" ".join(cats)) or genres_in(text[:300]),
               "american": any(re.search(r"\bAmerican\b.*\bfilms?\b", c) for c in cats) or "American" in text[:200],
               "foreign": any(re.search(r"\b(British|French|Italian|German|Japanese|Soviet|Spanish|Mexican|Swedish|Canadian|"
                                        r"Australian|Indian|Danish|Polish|Yugoslav|Argentine)\b.*\bfilms?\b", c) and
                              "American" not in c and "shot in" not in c for c in cats)}
        if not best or (year and py == year != best["year"]):
            best = hit
    return best


def series(title: str) -> dict | None:
    """Look a TV series up in TVmaze (free, no key)."""
    r = http.get("https://api.tvmaze.com/search/shows", params={"q": title})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    plain = re.sub(r"[^a-z0-9]", "", re.sub(r"^(the) ", "", title.lower()))
    for row in r.json():
        s = row["show"]
        name = re.sub(r"[^a-z0-9]", "", re.sub(r"^(the) ", "", s["name"].lower()))
        year = int((s.get("premiered") or "0")[:4] or 0)
        if not 1945 <= year <= 1975 or not (name == plain or name in plain or plain in name):
            continue
        return {"description": re.sub(r"<[^>]+>", "", s.get("summary") or ""), "poster_url": (s.get("image") or {}).get("original"),
                "genres": s.get("genres") or [], "year": year}
    return None


def tmdb(key: str, title: str, year: int | None, tv: bool) -> dict | None:
    if not key:
        return None
    kind = "tv" if tv else "movie"
    params = {"query": title, ("first_air_date_year" if tv else "year"): year or ""}
    headers = {}
    if len(key) > 60:
        headers["Authorization"] = f"Bearer {key}"
    else:
        params["api_key"] = key
    r = http.get(f"https://api.themoviedb.org/3/search/{kind}", params=params, headers=headers)
    r.raise_for_status()
    res = r.json().get("results") or []
    if not res:
        return None
    x = res[0]
    return {"description": x.get("overview") or "",
            "poster_url": f"https://image.tmdb.org/t/p/w500{x['poster_path']}" if x.get("poster_path") else None,
            "genres": [TMDB_GENRES[g] for g in x.get("genre_ids", []) if g in TMDB_GENRES]}


def save_poster(url: str, media_id: int) -> bool:
    r = http.get(url)
    if r.status_code != 200 or len(r.content) < 2000:
        return False
    try:
        im = Image.open(io.BytesIO(r.content)).convert("RGB")
    except OSError:
        return False
    if im.width < 180 or im.height < im.width * 1.1:
        return False  # not poster shaped
    im.thumbnail((500, 750))
    im.save(config.POSTER_DIR / f"{media_id}.jpg", quality=86)
    return True


def category_for(genres: list[str]) -> str:
    g = {x.lower() for x in genres}
    if "comedy" in g:
        return "comedy-movies"
    if g & {"mystery", "thriller", "film noir", "crime", "spy", "film-noir", "suspense"}:
        return "mystery-thrillers"
    return "classic-movies"
