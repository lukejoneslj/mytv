import json
import re
import shutil

import pytest

from mytv.db import DB
from mytv.downloads import DownloadManager, NoSpace
from mytv.engine import TARGET_DURATION, Engine


class Clock:
    t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    db = DB(tmp_path / "t.db")
    clock = Clock()
    cache = tmp_path / "cache"
    cache.mkdir()
    eng = Engine(db, cache, clock)
    dl = tmp_path / "dl"
    dl.mkdir()
    return db, eng, clock, DownloadManager(db, eng, dl)


def add(db, eng, title, segs=None, ready=True, cat="classic-movies", kind="movie", show=None, year=1940):
    segs = segs or [6.0] * 100
    cur = db.run("""INSERT INTO media(provider,ref,kind,title,show,year,category,dedupe,added_at,enriched,runtime,status,duration,nsegs)
                    VALUES('t',?,?,?,?,?,?,?,0,1,?,?,?,?)""", title, kind, title, show, year, cat, title, sum(segs),
                 "ready" if ready else "discovered", sum(segs) if ready else None, len(segs))
    mid = cur.lastrowid
    if ready:
        put(eng, mid, segs)
    return mid


def put(eng, mid, segs):
    d = eng.media_dir(mid)
    d.mkdir()
    (d / "index.json").write_text(json.dumps({"segments": segs}))
    (d / "00000.ts").write_bytes(b"x")
    (d / f"{len(segs) - 1:05d}.ts").write_bytes(b"x")


def filler(db, eng, cid=1):
    cur = db.run("""INSERT INTO media(provider,ref,kind,title,dedupe,added_at,enriched,status) VALUES('system',?,'filler','f',?,0,1,'ready')""",
                 f"filler-{cid}", f"filler-{cid}")
    put(eng, cur.lastrowid, [6.0] * 5)
    return cur.lastrowid


def ch(db, cid=1):
    return db.one("SELECT * FROM channels WHERE id=?", cid)


def parse(text):
    seq = int(re.search(r"MEDIA-SEQUENCE:(\d+)", text)[1])
    disc = int(re.search(r"DISCONTINUITY-SEQUENCE:(\d+)", text)[1])
    out = {}
    dur = None
    for line in text.splitlines():
        if line == "#EXT-X-DISCONTINUITY":
            disc += 1
        elif line.startswith("#EXTINF:"):
            dur = float(line[8:-1])
        elif line.startswith("/media/"):
            out[seq] = (line, disc, dur)
            seq += 1
    return out


def test_playlist_is_consistent_across_many_transitions(env):
    db, eng, clock, _ = env
    for i in range(12):
        mid = add(db, eng, f"m{i}", [4.0 + (j * 7 % 9) for j in range(20 + i)])
        eng.enqueue(1, mid)
    seen = {}
    last_first = -1
    for _ in range(700):
        eng.commit(ch(db))
        segs = parse(eng.playlist(1))
        assert sum(s[2] for s in segs.values()) >= 3 * TARGET_DURATION  # players need three targets
        for n, s in segs.items():
            assert seen.setdefault(n, s) == s  # a published segment never changes
            assert round(s[2]) <= TARGET_DURATION
        assert min(segs) >= last_first
        last_first = min(segs)
        clock.t += 2.5
    assert sorted(seen) == list(range(max(seen) + 1))  # nothing skipped, ever
    assert len({s[1] for s in seen.values()}) > 5


def test_airings_are_back_to_back(env):
    db, eng, clock, _ = env
    for i in range(5):
        eng.enqueue(1, add(db, eng, f"m{i}"))
    for _ in range(300):
        eng.commit(ch(db))
        clock.t += 7
    rows = db.all("SELECT * FROM airings ORDER BY start")
    for a, b in zip(rows, rows[1:]):
        assert b["start"] == pytest.approx(a["start"] + a["duration"])
        assert b["seq_base"] == a["seq_base"] + a["nsegs"]
        assert b["disc_base"] == a["disc_base"] + 1


def test_unready_title_is_passed_over_then_airs_later(env):
    db, eng, clock, _ = env
    a, b = add(db, eng, "a", ready=False), add(db, eng, "b")
    eng.enqueue(1, a)
    eng.enqueue(1, b)
    eng.commit(ch(db))
    assert eng.current(1)["media_id"] == b
    assert [m["id"] for m in eng.queue(1)] == [a]


def test_filler_when_empty_then_real_title_takes_over(env):
    db, eng, clock, _ = env
    f = filler(db, eng)
    eng.commit(ch(db))
    assert eng.current(1)["media_id"] == f
    assert eng.last_airing(1)["start"] < clock.t + 120
    m = add(db, eng, "late")
    eng.enqueue(1, m)
    for _ in range(30):
        clock.t += 5
        eng.commit(ch(db))
        assert eng.playlist(1)
    assert eng.current(1)["media_id"] == m


def test_rerun_when_nothing_new(env):
    db, eng, clock, _ = env
    a, b = add(db, eng, "a"), add(db, eng, "b")
    eng.enqueue(1, a)
    eng.enqueue(1, b)
    for _ in range(400):
        eng.commit(ch(db))
        assert eng.playlist(1)
        clock.t += 5
    order = [r["media_id"] for r in db.all("SELECT media_id FROM airings ORDER BY start")]
    assert all(x != y for x, y in zip(order, order[1:]))


def test_restart_keeps_position(env, tmp_path):
    db, eng, clock, _ = env
    eng.enqueue(1, add(db, eng, "a", [6.0] * 500))
    eng.tick()
    before = eng.playlist(1)
    again = Engine(DB(tmp_path / "t.db"), eng.cache, clock)
    again.tick()
    assert again.playlist(1) == before


def test_resume_after_outage(env):
    db, eng, clock, _ = env
    a = add(db, eng, "a", [6.0] * 500)
    eng.enqueue(1, a)
    eng.tick()
    clock.t += 600
    eng.tick()
    clock.t += 8 * 3600
    eng.tick()
    cur = eng.current(1)
    assert cur["media_id"] == a and cur["seg_offset"] == 100
    assert eng.playlist(1)


def test_live_after_outage_moves_on(env):
    db, eng, clock, _ = env
    db.set("after_outage", "live")
    a, b = add(db, eng, "a", [6.0] * 500), add(db, eng, "b")
    eng.enqueue(1, a)
    eng.enqueue(1, b)
    eng.tick()
    clock.t += 8 * 3600
    eng.tick()
    assert eng.current(1)["media_id"] == b


def test_skip_never_rewrites_published_segments(env):
    db, eng, clock, _ = env
    a, b = add(db, eng, "a", [6.0] * 500), add(db, eng, "b")
    eng.enqueue(1, a)
    eng.enqueue(1, b)
    eng.commit(ch(db))
    clock.t += 300
    eng.commit(ch(db))
    before = parse(eng.playlist(1))
    eng.skip(1)
    after = parse(eng.playlist(1))
    assert all(after[n] == s for n, s in before.items() if n in after)
    clock.t += 90
    eng.commit(ch(db))
    assert eng.current(1)["media_id"] == b


def test_skip_returns_booked_titles_to_queue(env):
    db, eng, clock, _ = env
    ids = [add(db, eng, f"m{i}", [6.0] * 30) for i in range(6)]
    for i in ids:
        eng.enqueue(1, i)
    eng.commit(ch(db))
    eng.skip(1)
    airing = [r["media_id"] for r in db.all("SELECT media_id FROM airings ORDER BY start")]
    queued = [m["id"] for m in eng.queue(1)]
    assert airing + queued == ids


def test_vanished_files_do_not_black_out_channel(env):
    db, eng, clock, _ = env
    filler(db, eng)
    a, b = add(db, eng, "a", [6.0] * 500), add(db, eng, "b")
    eng.enqueue(1, a)
    eng.enqueue(1, b)
    eng.commit(ch(db))
    clock.t += 100
    shutil.rmtree(eng.media_dir(a))
    eng.commit(ch(db))
    assert eng.playlist(1)
    assert eng.current(1)["media_id"] == b


def test_cleanup_never_touches_airing_booked_queued(env):
    db, eng, clock, dm = env
    db.set("rerun_reserve_movies", 0)
    ids = [add(db, eng, f"m{i}", [6.0] * 200) for i in range(4)]
    for i in ids:
        eng.enqueue(1, i)
    for _ in range(800):
        eng.commit(ch(db))
        dm.cleanup()
        for r in db.all("SELECT media_id FROM airings WHERE start<? AND start+duration>?", clock.t + 36, clock.t - 30):
            assert eng.playable(r["media_id"])
        for m in eng.queue(1):
            assert eng.playable(m["id"])
        clock.t += 5
    gone = [i for i in ids if not eng.media_dir(i).exists()]
    assert ids[0] in gone
    assert db.one("SELECT title FROM media WHERE id=?", ids[0])["title"] == "m0"  # metadata kept
    assert not dm.evict(eng.current(1)["media_id"])


def test_make_room_respects_limit(env):
    db, eng, clock, dm = env
    m = add(db, eng, "big")
    db.run("UPDATE media SET disk_bytes=? WHERE id=?", 50_000_000_000, m)
    db.set("cache_limit_gb", 60)
    db.set("min_free_gb", 0)
    eng.enqueue(1, m)
    with pytest.raises(NoSpace):
        dm.make_room(20_000_000_000)
    assert eng.playable(m)
    db.run("DELETE FROM queue")
    dm.make_room(20_000_000_000)  # now evictable
    assert not eng.playable(m)


def test_fill_queue_no_duplicates_and_mixes_shows(env):
    db, eng, clock, _ = env
    for s in "ABC":
        for i in range(30):
            add(db, eng, f"{s}{i}", [6.0] * 250, ready=False, cat="sitcoms", kind="episode", show=s)
    eng.fill_queue(ch(db, 3))
    q = eng.queue(3)
    assert len(q) >= 50 and len({m["id"] for m in q}) == len(q)
    assert all(x["show"] != y["show"] for x, y in zip(q, q[1:]))


def test_chronological_and_blocks(env):
    db, eng, clock, _ = env
    for y in (1950, 1931, 1942):
        add(db, eng, f"y{y}", ready=False, year=y)
    db.run("UPDATE channels SET mode='chronological' WHERE id=1")
    eng.fill_queue(ch(db))
    assert [m["year"] for m in eng.queue(1)] == [1931, 1942, 1950]
    db.run("DELETE FROM queue")
    db.run("UPDATE channels SET blocks=? WHERE id=1", json.dumps([{"start": "00:00", "end": "23:59", "days": list(range(7)), "year_from": 1945}]))
    eng.fill_queue(ch(db))
    assert eng.queue(1)[0]["year"] == 1950


def test_wanted_is_soonest_first_and_limited(env):
    db, eng, clock, _ = env
    for c, cat in ((1, "classic-movies"), (2, "comedy-movies")):
        for i in range(6):
            eng.enqueue(c, add(db, eng, f"{cat}{i}", ready=False, cat=cat))
    w = eng.wanted()
    assert len(w) == 6
    assert [m["needed_at"] for m in w] == sorted(m["needed_at"] for m in w)


def test_own_files_air_soon_and_replace_archive_copies(env):
    from mytv.library import add_item
    from mytv.providers import Item
    db, eng, clock, _ = env
    for i in range(40):
        add(db, eng, f"m{i}", [6.0] * 900, ready=False)
    eng.fill_queue(ch(db))
    n = len(eng.queue(1))
    old = add_item(db, "internet_archive", Item(ref="x", kind="movie", title="His Girl Friday", year=1940))
    own = add_item(db, "local", Item(ref="classic-movies/His Girl Friday (1940).mkv", kind="movie", title="His Girl Friday",
                                     year=1940, category="classic-movies"))
    assert own == old and db.one("SELECT provider FROM media WHERE id=?", own)["provider"] == "local"
    db.run("UPDATE media SET enriched=1 WHERE id=?", own)
    eng.make_way("classic-movies")
    eng.fill_queue(ch(db))
    order = [m["id"] for m in eng.queue(1)]
    assert order.index(own) == 3 and len(order) >= n - 1
