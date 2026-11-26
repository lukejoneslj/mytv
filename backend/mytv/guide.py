"""M3U playlist and XMLTV guide."""
from __future__ import annotations

import datetime as dt
import json
from xml.sax.saxutils import escape, quoteattr

from .cast import Casts
from .db import DB
from .engine import Engine


def m3u(db: DB, engine: Engine, base: str, ts: bool = False, casts: Casts | None = None) -> str:
    out = [f'#EXTM3U url-tvg="{base}/epg.xml" x-tvg-url="{base}/epg.xml"']
    for c in engine.channels():
        out.append(f'#EXTINF:-1 tvg-id="mytv.{c["id"]}" tvg-chno="{c["number"]}" tvg-name="{c["name"]}" '
                   f'tvg-logo="{base}/logo/{c["id"]}.png" group-title="MyTV",{c["name"]}')
        out.append(f'{base}/stream/{c["id"]}.ts' if ts else f'{base}/live/{c["id"]}.m3u8')
    for c in casts.channels() if casts else []:
        out.append(f'#EXTINF:-1 tvg-id="mytv.cast.{c["id"]}" tvg-chno="{c["number"]}" tvg-name="{c["name"]}" '
                   f'tvg-logo="{base}/onair/{c["id"]}.png" group-title="MyTV",{c["name"]}')
        out.append(f'{base}/onair/{c["id"]}/stream.ts' if ts else f'{base}/onair/{c["id"]}.m3u8')
    for x in db.all("SELECT * FROM live_links ORDER BY number"):
        out.append(f'#EXTINF:-1 tvg-id="mytv.link.{x["id"]}" tvg-chno="{x["number"]}" tvg-name="{x["name"]}" '
                   f'group-title="MyTV Live",{x["name"]}')
        out.append(x["url"])
    return "\n".join(out) + "\n"


def _t(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def xmltv(engine: Engine, base: str, casts: Casts | None = None) -> str:
    now = engine.clock()
    chans = engine.channels()
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="MyTV">']
    for c in chans:
        out.append(f'<channel id="mytv.{c["id"]}"><display-name>{escape(c["name"])}</display-name>'
                   f'<display-name>{c["number"]}</display-name><icon src={quoteattr(base + "/logo/%d.png" % c["id"])}/></channel>')
    live = [casts.status(c) for c in casts.channels()] if casts else []
    for c in live:
        out.append(f'<channel id="mytv.cast.{c["id"]}"><display-name>{escape(c["name"])}</display-name>'
                   f'<display-name>{c["number"]}</display-name><icon src={quoteattr(base + "/onair/%d.png" % c["id"])}/></channel>')
    for c in chans:
        for p in engine.schedule(c["id"], now - 4 * 3600, now + 48 * 3600):
            title = p["show"] or p["title"]
            out.append(f'<programme start="{_t(p["start"])}" stop="{_t(p["start"] + p["length"])}" channel="mytv.{c["id"]}">')
            out.append(f"<title>{escape(title)}</title>")
            if p["show"]:
                out.append(f'<sub-title>{escape(p["title"])}</sub-title>')
            if p["description"] and p["kind"] != "filler":
                out.append(f'<desc>{escape(p["description"][:600])}</desc>')
            if p["year"]:
                out.append(f'<date>{p["year"]}</date>')
            for g in json.loads(p["genres"]):
                out.append(f"<category>{escape(g)}</category>")
            if p["has_poster"]:
                out.append(f'<icon src={quoteattr(base + "/poster/%d.jpg" % p["id"])}/>')
            if p["season"] and p["episode"]:
                out.append(f'<episode-num system="xmltv_ns">{p["season"] - 1}.{p["episode"] - 1}.</episode-num>')
                out.append(f'<episode-num system="onscreen">S{p["season"]:02d}E{p["episode"]:02d}</episode-num>')
            if p["air_kind"] == "rerun":
                out.append("<previously-shown/>")
            out.append("</programme>")
    for c in live:
        t = c["started"] if c["live"] else now // 3600 * 3600
        title = (c["title"] or c["name"]) if c["live"] else "Nothing streaming"
        for _ in range(8):
            out.append(f'<programme start="{_t(t)}" stop="{_t(t + 6 * 3600)}" channel="mytv.cast.{c["id"]}">'
                       f"<title>{escape(title)}</title></programme>")
            t += 6 * 3600
    out.append("</tv>")
    return "\n".join(out)
