"""Turns a finished media file into ready-to-air HLS segments.

Compatible H.264 is copied untouched. Anything else goes through the Apple hardware encoder.
Audio always becomes stereo AAC at an even loudness, so channels don't jump in volume.
"""
from __future__ import annotations

import json
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from . import config
from .engine import TARGET_DURATION

MAX_SEG = TARGET_DURATION + 0.49  # RFC 8216: EXTINF rounded to nearest integer must not exceed the target

MIN_SEG = 5.0


class IngestError(Exception):
    pass


class Cancelled(Exception):
    pass


def probe(src: str) -> dict:
    cmd = [config.FFPROBE, "-v", "error", "-show_format", "-show_streams", "-of", "json", src]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        raise IngestError("ffprobe timed out")
    if r.returncode:
        raise IngestError(f"Not a playable video: {r.stderr.strip()[-200:]}")
    d = json.loads(r.stdout)
    v = next((s for s in d["streams"] if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    if not v:
        raise IngestError("File has no video track")
    duration = float(d["format"].get("duration") or v.get("duration") or 0)
    if duration < 1:
        raise IngestError("File has no readable duration")
    return {"video": v, "audio": a, "duration": duration}


def can_copy(v: dict, policy: str, max_height: int) -> bool:
    if policy == "always" or v.get("codec_name") != "h264":
        return False
    if v.get("pix_fmt") not in ("yuv420p", "yuvj420p"):
        return False
    if v.get("field_order", "progressive") not in ("progressive", "unknown"):
        return False
    if int(v.get("level") or 40) > 42 or v.get("profile") not in (
            "Constrained Baseline", "Baseline", "Main", "High"):
        return False
    return int(v.get("height") or 0) <= max_height


def loudness_gain(src: str) -> float:
    """Static gain (dB) that brings the title to -18 LUFS."""
    r = subprocess.run(
        ["nice", "-n", "10", config.FFMPEG, "-hide_banner", "-nostats", "-i", src, "-vn", "-map", "0:a:0",
         "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
        capture_output=True, text=True, timeout=1800, errors="replace")
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)
    if not m:
        return 0.0
    measured = float(m[-1])
    return 0.0 if measured < -60 else max(-12.0, min(12.0, -18.0 - measured))


def _video_args(v: dict, max_height: int) -> list[str]:
    w, h = int(v["width"]), int(v["height"])
    sar = 1.0
    try:
        n, d = (v.get("sample_aspect_ratio") or "1:1").split(":")
        sar = float(n) / float(d) if float(n) and float(d) else 1.0
    except ValueError:
        pass
    th = min(h, max_height) // 2 * 2
    tw = int(round(w * sar * th / h / 2)) * 2
    vf = []
    if v.get("field_order") in ("tt", "bb", "tb", "bt"):
        vf.append("bwdif=mode=send_frame")
    vf += [f"scale={tw}:{th}:flags=lanczos", "setsar=1", "format=yuv420p"]
    rate = 2500 if th <= 480 else 3500 if th <= 576 else 5000 if th <= 720 else 8000
    return ["-vf", ",".join(vf), "-c:v", "h264_videotoolbox", "-allow_sw", "1", "-profile:v", "high", "-b:v", f"{rate}k",
            "-maxrate", f"{int(rate * 1.5)}k", "-bufsize", f"{rate * 3}k",
            "-force_key_frames", "expr:gte(t,n_forced*4)", "-pix_fmt", "yuv420p"]


def _run(cmd: list[str], duration: float, progress: Callable[[float], None], cancel: threading.Event) -> None:
    p = subprocess.Popen(["nice", "-n", "10", *cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
    errs: list[str] = []
    beat = [time.monotonic()]

    def drain():
        for line in p.stderr:
            errs.append(line)
            del errs[:-40]

    def watchdog():
        while p.poll() is None:
            if cancel.is_set() or time.monotonic() - beat[0] > 180:
                p.kill()
                return
            time.sleep(0.5)

    threading.Thread(target=drain, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    try:
        for line in p.stdout:
            if line.startswith("out_time_us="):
                try:
                    us = int(line.split("=")[1])
                except ValueError:
                    continue
                beat[0] = time.monotonic()
                progress(min(1.0, us / 1e6 / duration))
    finally:
        if p.poll() is None and cancel.is_set():
            p.kill()
        p.wait()
    if cancel.is_set():
        raise Cancelled()
    if p.returncode:
        raise IngestError("ffmpeg failed: " + "".join(errs)[-300:].strip())


def read_segments(playlist: Path) -> list[float]:
    return [float(x) for x in re.findall(r"#EXTINF:([\d.]+)", playlist.read_text())]


def merge_short(folder: Path, segs: list[float]) -> list[float]:
    """ffmpeg cuts at every keyframe while it catches up after a long gap, leaving slivers. Join neighbours so each
    piece lasts MIN_SEG..MAX_SEG. MPEG-TS pieces from one mux can be joined byte for byte."""
    groups: list[list[int]] = []
    length = 0.0
    for i, d in enumerate(segs):
        if groups and length < MIN_SEG and length + d <= MAX_SEG:
            groups[-1].append(i)
            length += d
        else:
            groups.append([i])
            length = d
    if len(groups) > 1 and length < MIN_SEG and sum(segs[i] for i in groups[-2]) + length <= MAX_SEG:
        tail = groups.pop()
        groups[-1] += tail
    out = []
    for n, g in enumerate(groups):
        first = folder / f"{g[0]:05d}.ts"
        if len(g) > 1:
            with open(first, "ab") as f:
                for i in g[1:]:
                    part = folder / f"{i:05d}.ts"
                    f.write(part.read_bytes())
                    part.unlink()
        if g[0] != n:
            first.rename(folder / f"{n:05d}.ts")
        out.append(sum(segs[i] for i in g))
    return out


def ingest(src: str, dest: Path, *, policy: str = "auto", max_height: int = 1080, normalize: bool = True,
           progress: Callable[[float], None] = lambda f: None, cancel: threading.Event | None = None) -> dict:
    """Write dest/00000.ts... and dest/index.json atomically. Returns facts about the result."""
    cancel = cancel or threading.Event()
    info = probe(src)
    v, a, duration = info["video"], info["audio"], info["duration"]
    tmp = dest.with_name(f"{dest.name}.{secrets.token_hex(4)}.tmp")  # unique: an orphaned ffmpeg must never share it
    gain = loudness_gain(src) if a and normalize else 0.0
    if cancel.is_set():
        raise Cancelled()

    attempts: list[tuple[str, int]] = []
    if can_copy(v, policy, max_height):
        attempts += [("copy", 4), ("copy", 1)]  # second try cuts at every keyframe
    attempts.append(("encode", 4))

    segs: list[float] = []
    mode = "copy"
    for mode, hls_time in attempts:
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        cmd = [config.FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-fflags", "+genpts", "-i", src]
        if not a:
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest"]
        cmd += ["-map", f"0:{v['index']}", "-map", f"0:{a['index']}" if a else "1:a", "-sn", "-dn", "-map_metadata", "-1"]
        cmd += ["-c:v", "copy"] if mode == "copy" else _video_args(v, max_height)
        af = "aresample=48000:async=1:first_pts=0"
        if abs(gain) > 0.5:
            af = f"volume={gain:.2f}dB,alimiter=limit=0.89:level=false," + af
        cmd += ["-af", af, "-c:a", "aac", "-b:a", "160k", "-ac", "2", "-ar", "48000",
                "-max_muxing_queue_size", "4096", "-f", "hls", "-hls_time", str(hls_time), "-hls_list_size", "0",
                "-hls_playlist_type", "vod", "-hls_flags", "independent_segments",
                "-hls_segment_filename", str(tmp / "%05d.ts"), "-progress", "pipe:1", str(tmp / "v.m3u8")]
        try:
            _run(cmd, duration, progress, cancel)
            segs = read_segments(tmp / "v.m3u8")
        except IngestError:
            if mode == "encode":
                shutil.rmtree(tmp, ignore_errors=True)
                raise
            continue
        except Cancelled:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        # bad timestamps in old files show up as a wrong total length: re-encode those too
        if segs and max(segs) <= MAX_SEG and abs(sum(segs) - duration) < max(8, duration * 0.03):
            break
        if mode == "encode" and (not segs or max(segs) > MAX_SEG or sum(segs) < duration * 0.9):
            shutil.rmtree(tmp, ignore_errors=True)
            raise IngestError(f"Processed video is incomplete ({sum(segs):.0f}s of {duration:.0f}s)")

    (tmp / "v.m3u8").unlink()
    segs = merge_short(tmp, segs)
    files = sorted(tmp.glob("*.ts"))
    if len(files) != len(segs):
        shutil.rmtree(tmp, ignore_errors=True)
        raise IngestError("Segment count mismatch")
    (tmp / "index.json").write_text(json.dumps({"segments": segs}))
    size = sum(f.stat().st_size for f in files)
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    return {"duration": sum(segs), "nsegs": len(segs), "disk_bytes": size, "direct": int(mode == "copy"),
            "width": int(v.get("width") or 0), "height": int(v.get("height") or 0)}
