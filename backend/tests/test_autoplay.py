import json
import random
from urllib.parse import parse_qs, urlsplit

import pytest

from mytv.autoplay import MOVIES, SHOWS, Autoplay, movie_details, show_details
from mytv.db import DB


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "t.db")


def seasons(_):
    return [{"season": 1, "episodes": 24}, {"season": 2, "episodes": 18}]


def test_reads_season_counts_from_watch_page():
    detail = {"seasons": [{"season_number": 0, "episode_count": 4}, {"season_number": 1, "episode_count": 24}]}
    text = 'xx"detail":' + json.dumps(detail) + ',"mediaType":"tv"'
    page = '<script>self.__next_f.push(' + json.dumps([1, text]) + ')</script>'
    assert show_details(page) == [{"season": 1, "episodes": 24}]
    with pytest.raises(ValueError):
        show_details("not a watch page")


def test_episode_url_uses_real_season_and_episode(db):
    auto = Autoplay(db, seasons, random.Random(4))
    current = auto.next(1)
    parts = urlsplit(current["url"])
    show_id = int(parts.path.split("/")[2])
    assert show_id in {s["id"] for s in SHOWS}
    query = parse_qs(parts.query)
    season = int(query["season"][0])
    episode = int(query["episode"][0])
    assert season in (1, 2)
    assert 1 <= episode <= (24 if season == 1 else 18)
    assert auto.get(1)["current"] == current
    assert auto.get(1)["recent"] == [f"tv:{show_id}:{season}:{episode}"]


def test_show_metadata_is_cached(db):
    calls = []
    auto = Autoplay(db, lambda i: calls.append(i) or seasons(i), random.Random(1))
    auto.seasons(1421)
    auto.seasons(1421)
    assert calls == [1421]


def test_consecutive_episodes_change_show(db):
    auto = Autoplay(db, seasons, random.Random(2))
    auto.next(1)
    auto.next(1)
    assert auto.get(1)["recent"][0].split(":")[1] != auto.get(1)["recent"][1].split(":")[1]


def test_movies_do_not_repeat_recent_titles(db):
    auto = Autoplay(db, seasons, random.Random(8))
    auto.configure(1, True, "movies")
    picked = [auto.next(1) for _ in range(30)]
    keys = [p["url"].split("/")[-2] for p in picked]
    assert len(set(keys[:24])) == 24
    assert all(p["url"].startswith("https://7movies.ws/movie/") for p in picked)
    assert len(MOVIES) >= 100 and len(SHOWS) >= 20


def test_configuration_and_tab_state(db):
    auto = Autoplay(db, seasons)
    assert not auto.get(1)["enabled"]
    auto.configure(1, True, "movies")
    auto.record_state(1, "Iron Man", "https://7movies.ws/movie/1726/watch")
    cfg = auto.get(1)
    assert cfg["enabled"] and cfg["collection"] == "movies"
    assert cfg["current"]["title"] == "Iron Man"
    assert cfg["linked_at"]
    with pytest.raises(ValueError):
        auto.configure(1, None, "unknown")


def catalog_item(movie_id, title="A movie", media_type="movie", genre="Trending"):
    return {"id": movie_id, "title": title, "year": "2026", "mediaType": media_type, "genre": genre, "rating": 8.1}


def catalog_page(items):
    text = '1a:{"catalog":' + json.dumps(items) + '}'
    return '<script>self.__next_f.push(' + json.dumps([1, text]) + ')</script>'


def test_reads_movie_cards_and_ignores_tv(db):
    movies = movie_details(catalog_page([
        catalog_item(900001, genre="Trending"), catalog_item(900002, "Another", genre="Popular"),
        catalog_item(900003, "A show", media_type="tv"), catalog_item(900001),
    ]))
    assert [(m["id"], m["genre"]) for m in movies] == [(900001, "Trending"), (900002, "Popular")]


def test_discovered_movies_merge_without_duplicates_and_can_be_selected(db):
    db.set("autoplay_movie_discovery", {"movies": [catalog_item(900001), catalog_item(1726, "Iron Man refreshed")]})
    auto = Autoplay(db, seasons)
    assert auto.movie_count() == len(MOVIES) + 1
    auto.configure(1, True, "movies")
    auto.rand.choice = lambda choices: choices[-1]
    current = auto.next(1)
    assert current["title"] == "A movie (2026)"
    assert current["url"] == "https://7movies.ws/movie/900001/watch"


def test_discovery_refreshes_after_twelve_hours_and_caches(db):
    now = [100_000.0]
    calls = []
    items = [catalog_item(900000 + i) for i in range(20)]
    auto = Autoplay(db, seasons, discover=lambda: calls.append(1) or items, clock=lambda: now[0])
    auto.refresh_due()
    auto.refresh_due()
    assert calls == [1]
    assert auto.discovery() == {"updated_at": now[0], "discovered": 20, "total": len(MOVIES) + 20}
    now[0] += 13 * 3600
    auto.refresh_due()
    assert calls == [1, 1]


def test_failed_refresh_keeps_last_good_catalog(db):
    now = [100_000.0]
    items = [catalog_item(900000 + i) for i in range(20)]
    auto = Autoplay(db, seasons, discover=lambda: items, clock=lambda: now[0])
    auto.refresh_due()
    before = auto.discovery()
    auto.discover = lambda: (_ for _ in ()).throw(OSError("offline"))
    now[0] += 13 * 3600
    auto.refresh_due()
    assert auto.discovery() == before
    assert auto.movie_count() == len(MOVIES) + 20
