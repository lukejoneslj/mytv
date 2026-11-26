"""Channel logos and the fallback station card, drawn locally."""
from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import config
from .db import DB
from .ingest import ingest

FONTS = ["/System/Library/Fonts/Avenir Next Condensed.ttc", "/System/Library/Fonts/HelveticaNeue.ttc",
         "/System/Library/Fonts/Helvetica.ttc"]


def font(size: int, index: int = 0):
    for f in FONTS:
        try:
            return ImageFont.truetype(f, size, index=index)
        except OSError:
            continue
    return ImageFont.load_default(size)


def logo(ch: dict) -> Path:
    path = config.DATA_DIR / "logos" / f"{ch['id']}-{ch['accent'].lstrip('#')}-{ch['number']}.png"
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((16, 16, 496, 496), 72, fill=ch["accent"])
    d.text((256, 262), str(ch["number"]), font=font(380), fill="#14130f", anchor="mm")
    im.save(path)
    return path


def station_card(db: DB, ch: dict) -> None:
    """30-second clip shown when a channel has nothing ready."""
    ref = f"filler-{ch['id']}"
    row = db.one("SELECT * FROM media WHERE provider='system' AND ref=?", ref)
    sig = f"{ch['name']}|{ch['number']}|{ch['accent']}"
    if row and row["status"] == "ready" and row["description"] == sig and (config.CACHE_DIR / str(row["id"]) / "index.json").exists():
        return
    if not row:
        import time
        db.run("""INSERT INTO media(provider,ref,kind,title,dedupe,added_at,enriched,runtime) VALUES('system',?,'filler',?,?,?,1,30)""",
               ref, "Back in a moment", ref, time.time())
        row = db.one("SELECT * FROM media WHERE provider='system' AND ref=?", ref)
    im = Image.new("RGB", (1280, 720), "#14130f")
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, 24, 720), fill=ch["accent"])
    d.text((110, 250), str(ch["number"]), font=font(150), fill=ch["accent"], anchor="lm")
    d.text((110, 370), ch["name"], font=font(84), fill="#f3efe6", anchor="lm")
    d.text((110, 452), "Back in a moment. The next program is loading.", font=font(36), fill="#a29c8e", anchor="lm")
    png = config.DATA_DIR / f"{ref}.png"
    mp4 = config.DATA_DIR / f"{ref}.mp4"
    im.save(png)
    subprocess.run([config.FFMPEG, "-loglevel", "error", "-y", "-loop", "1", "-i", str(png), "-f", "lavfi", "-i",
                    "anullsrc=r=48000:cl=stereo", "-t", "30", "-r", "25", "-c:v", "h264_videotoolbox", "-allow_sw", "1",
                    "-b:v", "1500k", "-g", "50", "-pix_fmt", "yuv420p", "-c:a", "aac", str(mp4)], check=True, timeout=120)
    facts = ingest(str(mp4), config.CACHE_DIR / str(row["id"]), normalize=False)
    db.run("UPDATE media SET status='ready', description=?, duration=?, nsegs=?, disk_bytes=?, direct=1 WHERE id=?",
           sig, facts["duration"], facts["nsegs"], facts["disk_bytes"], row["id"])
    png.unlink(missing_ok=True)
    mp4.unlink(missing_ok=True)


def _frames(sources: list[tuple[str, float]], tag: str):
    for src, seek in sources:
        out = config.DATA_DIR / f"still-{tag}.jpg"
        try:
            subprocess.run([config.FFMPEG, "-loglevel", "error", "-y", "-ss", f"{seek:.1f}", "-i", src, "-frames:v", "1",
                            "-vf", "scale=iw*sar:ih", str(out)], timeout=60, capture_output=True, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            continue
        if out.exists():
            im = Image.open(out).convert("RGB")
            im.load()
            out.unlink()
            yield im


def still_poster(db: DB, m: dict, remote: bool = False) -> bool:
    """Poster made from a frame of the title itself, when no artwork is available."""
    if m["has_poster"]:
        return False
    if remote:
        runtime = m["runtime"] or 1500
        sources = [(m["source_url"], runtime * k) for k in (0.22, 0.47)]
    else:
        d, n = config.CACHE_DIR / str(m["id"]), int(m["nsegs"] or 0)
        sources = [(str(d / f"{int(n * k):05d}.ts"), 0) for k in (0.12, 0.2, 0.3, 0.42, 0.55, 0.68)]
    best, best_score = None, -1.0
    for im in _frames(sources, str(m["id"])):  # keep the frame with the most contrast
        px = im.convert("L").resize((64, 48)).tobytes()
        mean = sum(px) / len(px)
        score = sum((p - mean) ** 2 for p in px) / len(px)
        if score > best_score:
            best, best_score = im, score
    if best is None:
        return False
    w, h = best.size
    cw = min(w, int(h * 2 / 3))
    im = best.crop(((w - cw) // 2, 0, (w + cw) // 2, h)).resize((500, 750), Image.LANCZOS)
    shade = Image.new("L", (1, 750))
    for y in range(750):
        shade.putpixel((0, y), int(max(0, (y - 330) / 420) ** 0.8 * 235))
    im.paste(Image.new("RGB", (500, 750), "#100f0c"), (0, 0), shade.resize((500, 750)))
    dr = ImageDraw.Draw(im)
    text = m["show"] or m["title"]
    size = 62
    while True:
        f, lines, cur = font(size), [], ""
        for word in text.split():
            if dr.textlength(f"{cur} {word}".strip(), font=f) > 436 and cur:
                lines.append(cur)
                cur = word
            else:
                cur = f"{cur} {word}".strip()
        lines.append(cur)
        if size <= 30 or (len(lines) <= 4 and all(dr.textlength(x, font=f) <= 436 for x in lines)):
            break
        size -= 4
    y = 700 - len(lines) * int(size * 1.02) - (34 if m["year"] else 0)
    for line in lines:
        dr.text((32, y), line, font=f, fill="#f6f1e6")
        y += int(size * 1.02)
    if m["year"]:
        dr.text((32, y + 6), str(m["year"]), font=font(28), fill="#c9bfa9")
    im.save(config.POSTER_DIR / f"{m['id']}.jpg", quality=86)
    db.run("UPDATE media SET has_poster=2 WHERE id=? AND has_poster=0", m["id"])
    return True
