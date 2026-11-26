import re

import pytest

from mytv.cast import GRACE, Casts, Refused
from mytv.db import DB

TS = b"\x47" + b"\x00" * 187


class Clock:
    t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    db = DB(tmp_path / "t.db")
    clock = Clock()
    c = Casts(db, clock, tmp_path / "cards")
    for ch in c.channels():  # a tiny card instead of running ffmpeg
        c.room(ch["id"]).card = [(b"card%d" % i, 2.0) for i in range(3)]
    return db, c, clock


def parse(text):
    seq = int(re.search(r"MEDIA-SEQUENCE:(\d+)", text).group(1))
    disc = int(re.search(r"DISCONTINUITY-SEQUENCE:(\d+)", text).group(1))
    uris = re.findall(r"^/onair/\d+/(\d+)\.ts$", text, re.M)
    return seq, disc, [int(u) for u in uris], text.count("#EXT-X-DISCONTINUITY\n")


def test_seeded_channels_get_free_numbers(env):
    db, c, _ = env
    assert [ch["number"] for ch in c.channels()] == [11, 12, 13]
    db.run("INSERT INTO live_links(number,name,url) VALUES(14,'x','http://x')")
    assert c.create()["number"] == 15
    assert db.number_taken(11) and not db.number_taken(11, "cast_channels", 1)


def test_card_fills_time_and_sequence_only_grows(env):
    _, c, clock = env
    s1, d1, u1, _ = parse(c.playlist(1))
    assert u1 == list(range(s1, s1 + len(u1)))
    clock.t += 10
    s2, d2, u2, _ = parse(c.playlist(1))
    assert s2 >= s1 and d2 >= d1 and u2[-1] > u1[-1]
    clock.t += 3600  # nobody asked for an hour
    s3, d3, u3, _ = parse(c.playlist(1))
    assert s3 > s2 and d3 > d2 and len(u3) <= 16
    assert d3 < 1000  # players walk every number up to this one


def test_counters_never_go_back_after_restart(env, tmp_path):
    db, c, clock = env
    for _ in range(5):
        clock.t += 7
        s1, d1, _, _ = parse(c.playlist(1))
    clock.t += 1
    again = Casts(db, clock, tmp_path / "cards")
    again.room(1).card = c.room(1).card
    s2, d2, _, _ = parse(again.playlist(1))
    assert s2 > s1 and d2 >= d1


def test_stream_replaces_card_and_card_returns_after_stop(env):
    _, c, clock = env
    c.playlist(1)
    for n in range(4):
        clock.t += 2
        c.push(1, "session-aaaa", n, 2.0, n == 0, TS * 10, {"title": "Game"})
    text = c.playlist(1)
    seq, _, uris, _ = parse(text)
    assert c.segment(1, uris[-1]) == TS * 10
    assert c.status(c.get(1))["live"] and c.status(c.get(1))["title"] == "Game"
    c.stop(1, "session-aaaa")
    clock.t += 4
    _, _, uris2, discs = parse(c.playlist(1))
    assert c.segment(1, uris2[-1]).startswith(b"card") and discs >= 2
    with pytest.raises(Refused):  # a stopped stream cannot come back
        c.push(1, "session-aaaa", 4, 2.0, False, TS, {})


def test_second_stream_refused_while_first_is_live_then_allowed(env):
    _, c, clock = env
    c.push(2, "session-one1", 0, 2.0, True, TS, {})
    with pytest.raises(Refused):
        c.push(2, "session-two2", 0, 2.0, True, TS, {})
    clock.t += GRACE + 1
    c.push(2, "session-two2", 0, 2.0, True, TS, {})
    with pytest.raises(Refused):
        c.push(2, "session-one1", 1, 2.0, False, TS, {})


def test_retries_and_bad_segments(env):
    _, c, clock = env
    c.push(1, "session-aaaa", 0, 2.0, True, TS, {})
    c.push(1, "session-aaaa", 0, 2.0, True, TS, {})  # retry of the same piece
    assert sum(1 for s in c.room(1).segs if s.data == TS) == 1
    for bad in (b"", b"x" * 188, TS[:100]):
        with pytest.raises(ValueError):
            c.push(1, "session-aaaa", 1, 2.0, False, bad, {})
    with pytest.raises(ValueError):
        c.push(1, "session-aaaa", 1, 9.0, False, TS, {})


def test_memory_is_bounded(env):
    _, c, clock = env
    for n in range(500):
        clock.t += 2
        c.push(1, "session-aaaa", n, 2.0, False, TS, {})
    assert len(c.room(1).segs) < 60


def test_delete_refused_while_live(env):
    _, c, _ = env
    c.push(3, "session-aaaa", 0, 2.0, True, TS, {})
    with pytest.raises(Refused):
        c.delete(3)
    c.stop(3)
    c.delete(3)
    assert c.get(3) is None
