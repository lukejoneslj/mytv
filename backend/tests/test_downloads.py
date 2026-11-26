"""Real downloads from a misbehaving local server, real ffmpeg."""
import hashlib
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mytv import config
from mytv.db import DB
from mytv.downloads import DownloadManager
from mytv.engine import Engine, TARGET_DURATION
from mytv.ingest import MAX_SEG, IngestError, ingest, merge_short, probe


def make(path, *, codec="libx264", seconds=30, extra=(), audio=True):
    cmd = [config.FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=25:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-c:v", codec, "-pix_fmt", "yuv420p", *extra, str(path)]
    subprocess.run(cmd, check=True)
    return path


class Handler(BaseHTTPRequestHandler):
    files: dict = {}
    mode = "ok"
    hits: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Handler.hits.append(self.headers.get("Range"))
        if Handler.mode == "html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html>hello</html>")
            return
        data = Handler.files[self.path]
        start = 0
        if r := self.headers.get("Range"):
            start = int(r.split("=")[1].split("-")[0])
            self.send_response(206)
        else:
            self.send_response(200)
        body = data[start:]
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if Handler.mode == "drop":  # connection dies half way
            Handler.mode = "ok"
            self.wfile.write(body[: len(body) // 2])
            self.wfile.flush()
            self.connection.close()
            return
        self.wfile.write(body)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "POSTER_DIR", tmp_path / "posters")
    (tmp_path / "posters").mkdir()
    db = DB(tmp_path / "t.db")
    db.set("min_free_gb", 0)
    for d in ("cache", "dl"):
        (tmp_path / d).mkdir()
    eng = Engine(db, tmp_path / "cache")
    dm = DownloadManager(db, eng, tmp_path / "dl")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    Handler.files, Handler.mode, Handler.hits = {}, "ok", []
    data = make(tmp_path / "src.mp4").read_bytes()
    Handler.files["/a.mp4"] = data
    yield db, eng, dm, f"http://127.0.0.1:{srv.server_port}", data
    dm.stop()
    srv.shutdown()


def add(db, url, data, md5=None, ref="a"):
    cur = db.run("""INSERT INTO media(provider,ref,kind,title,category,dedupe,added_at,enriched,source_url,size,md5,year)
                    VALUES('url',?,'movie',?,'classic-movies',?,0,1,?,?,?,1940)""", ref, ref, ref, url, len(data),
                 md5 or hashlib.md5(data).hexdigest())
    db.run("INSERT INTO queue(channel_id,media_id,position) VALUES(1,?,?)", cur.lastrowid, cur.lastrowid)
    return cur.lastrowid


def pump(db, dm, mid, until, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        dm.tick()
        s = db.one("SELECT * FROM media WHERE id=?", mid)
        if until(s):
            return s
        time.sleep(0.1)
    raise AssertionError(dict(s))


def test_download_to_air(env):
    db, eng, dm, base, data = env
    mid = add(db, base + "/a.mp4", data)
    m = pump(db, dm, mid, lambda s: s["status"] == "ready")
    assert m["direct"] == 1 and eng.playable(mid)
    assert abs(m["duration"] - 30) < 1
    eng.commit(db.one("SELECT * FROM channels WHERE id=1"))
    assert f"/media/{mid}/00000.ts" in eng.playlist(1)
    time.sleep(0.3)
    assert not (dm.dir / f"{mid}.part").exists()  # source removed after processing


def test_interrupted_download_resumes_where_it_stopped(env):
    db, eng, dm, base, data = env
    Handler.mode = "drop"
    mid = add(db, base + "/a.mp4", data)
    m = pump(db, dm, mid, lambda s: s["attempts"] == 1 and s["status"] == "discovered")
    assert m["error"] and not eng.playable(mid)  # never offered for playback while incomplete
    part = (dm.dir / f"{mid}.part").stat().st_size
    assert 0 < part < len(data)
    db.run("UPDATE media SET next_retry=0")
    pump(db, dm, mid, lambda s: s["status"] == "ready")
    assert Handler.hits[-1] == f"bytes={part}-"


def test_damaged_file_is_rejected(env):
    db, eng, dm, base, data = env
    mid = add(db, base + "/a.mp4", data, md5="0" * 32)
    m = pump(db, dm, mid, lambda s: s["attempts"] == 1 and s["status"] == "discovered")
    assert "Checksum" in m["error"] and not eng.playable(mid)


def test_repeated_failure_leaves_the_queue(env):
    db, eng, dm, base, data = env
    mid = add(db, "http://127.0.0.1:9/none.mp4", data)
    for _ in range(5):
        pump(db, dm, mid, lambda s: s["status"] in ("discovered", "failed") and mid not in {
            k for k, v in dm.jobs.items() if v[0].is_alive()} and (s["attempts"] or s["status"] == "failed"))
        db.run("UPDATE media SET next_retry=0")
    pump(db, dm, mid, lambda s: s["status"] == "failed")
    assert not db.all("SELECT * FROM queue")


def test_web_page_is_not_a_video(env):
    db, eng, dm, base, data = env
    Handler.mode = "html"
    mid = add(db, base + "/a.mp4", data)
    m = pump(db, dm, mid, lambda s: s["status"] == "hidden")
    assert "web page" in m["error"]


def test_storage_limit_pauses_then_continues(env):
    db, eng, dm, base, data = env
    db.set("cache_limit_gb", 0.000001)
    mid = add(db, base + "/a.mp4", data)
    for _ in range(5):
        dm.tick()
    assert dm.paused_reason and not Handler.hits
    db.set("cache_limit_gb", 10)
    pump(db, dm, mid, lambda s: s["status"] == "ready")
    assert dm.paused_reason is None


def test_crash_recovery_requeues_unfinished_work(env):
    db, eng, dm, base, data = env
    mid = add(db, base + "/a.mp4", data)
    db.run("UPDATE media SET status='processing'")
    (eng.cache / f"{mid}.tmp").mkdir()
    (dm.dir / f"{mid}.part").write_bytes(data[:1000])
    (dm.dir / "999.part").write_bytes(b"x")
    dm.recover()
    assert not (eng.cache / f"{mid}.tmp").exists() and not (dm.dir / "999.part").exists()
    assert (dm.dir / f"{mid}.part").exists()
    pump(db, dm, mid, lambda s: s["status"] == "ready")


@pytest.mark.parametrize("name,codec,extra,audio", [
    ("mpeg2.mpg", "mpeg2video", ["-flags", "+ilme+ildct", "-top", "1"], True),
    ("mpeg4.avi", "mpeg4", [], True),
    ("sparse.mp4", "libx264", ["-g", "2000", "-keyint_min", "2000", "-sc_threshold", "0"], True),  # one keyframe
    ("silent.mp4", "libx264", [], False),
])
def test_awkward_sources_become_valid_segments(tmp_path, name, codec, extra, audio):
    if "-top" in extra:
        extra = ["-flags", "+ilme+ildct", "-field_order", "tt"]
    src = make(tmp_path / name, codec=codec, extra=extra, audio=audio)
    facts = ingest(str(src), tmp_path / "out")
    segs = json.loads((tmp_path / "out" / "index.json").read_text())["segments"]
    assert all(round(s) <= TARGET_DURATION for s in segs)
    assert all(s >= 3 for s in segs[:-1])
    assert abs(sum(segs) - 30) < 1.5
    for i in (0, len(segs) - 1):
        p = probe(str(tmp_path / "out" / f"{i:05d}.ts"))
        assert p["video"]["codec_name"] == "h264" and p["audio"]["codec_name"] == "aac" and p["audio"]["channels"] == 2
    assert facts["nsegs"] == len(segs)
    for i in range(len(segs)):  # every segment decodes on its own
        r = subprocess.run([config.FFMPEG, "-v", "error", "-i", str(tmp_path / "out" / f"{i:05d}.ts"), "-f", "null", "-"],
                           capture_output=True, text=True)
        assert r.returncode == 0


def test_garbage_is_rejected(tmp_path):
    (tmp_path / "x.mp4").write_bytes(b"not a video" * 1000)
    with pytest.raises(IngestError):
        ingest(str(tmp_path / "x.mp4"), tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_uneven_keyframes_leave_no_slivers(tmp_path):
    expr = "+".join(f"eq(n,{k})" for k in [0, 200, 400, 600, 605, 608, 610, 612, 615, 620, 700])
    src = make(tmp_path / "k.mp4", extra=["-g", "9999", "-keyint_min", "9999", "-sc_threshold", "0", "-force_key_frames", f"expr:{expr}"])
    ingest(str(src), tmp_path / "out")
    segs = json.loads((tmp_path / "out" / "index.json").read_text())["segments"]
    assert min(segs[:-1]) >= 3 and max(segs) <= 12.49 and abs(sum(segs) - 30) < 1
    joined = tmp_path / "all.ts"
    joined.write_bytes(b"".join(f.read_bytes() for f in sorted((tmp_path / "out").glob("*.ts"))))
    r = subprocess.run([config.FFMPEG, "-v", "error", "-i", str(joined), "-f", "null", "-"], capture_output=True, text=True)
    assert r.returncode == 0 and not r.stderr
    n = subprocess.run([config.FFPROBE, "-v", "error", "-count_frames", "-select_streams", "v", "-show_entries", "stream=nb_read_frames",
                        "-of", "csv=p=0", str(joined)], capture_output=True, text=True).stdout.strip().strip(",")
    assert int(n.split()[0].strip(",")) == 750  # every frame survived


def test_merge_keeps_every_byte_in_order(tmp_path):
    import random
    rng = random.Random(7)
    for case in range(300):
        d = tmp_path / str(case)
        d.mkdir()
        segs = [round(rng.choice([0.03, 0.2, 1.1, 3.5, 5, 8.3, 11.3, 12.4]), 2) for _ in range(rng.randint(1, 25))]
        for i in range(len(segs)):
            (d / f"{i:05d}.ts").write_bytes(b"<%d>" % i)
        out = merge_short(d, segs)
        files = sorted(d.glob("*.ts"))
        assert [f.name for f in files] == [f"{i:05d}.ts" for i in range(len(out))]
        assert b"".join(f.read_bytes() for f in files) == b"".join(b"<%d>" % i for i in range(len(segs)))
        assert abs(sum(out) - sum(segs)) < 1e-6 and max(out) <= MAX_SEG
