"""Tier 3 integration tests — hit the real TMDB / OMDb / MDBList APIs.

Each class skips unless its key is set. Everything is read-only, so there is
nothing to create or clean up; the tests assert on long-settled facts
(Fight Club, Game of Thrones) without pinning volatile values like scores,
vote counts or streaming availability.

Run with:
    TMDB_API_TOKEN=... [OMDB_API_KEY=...] [MDBLIST_API_KEY=...] \\
        uv run --extra test pytest tests/test_integration.py -v
"""
from __future__ import annotations

import pytest

from tests.conftest import requires_mdblist, requires_omdb, requires_tmdb


@pytest.fixture(scope="module")
def live_tmdb():
    from film_tv_mcp.tmdb_client import TMDBClient
    return TMDBClient(region="AU", language="en-AU")


@requires_tmdb
class TestLiveTMDB:
    def test_search_multi(self, live_tmdb):
        assert "[movie 550]" in live_tmdb.search("Fight Club", year=0)

    def test_movie_details(self, live_tmdb):
        out = live_tmdb.title_details(550, "movie")
        assert "imdb tt0137523" in out
        assert "David Fincher" in out
        assert "Where to watch (AU)" in out

    def test_tv_details_and_season(self, live_tmdb):
        out = live_tmdb.title_details(1399, "tv")
        assert "Seasons: 8" in out
        season = live_tmdb.tv_season(1399, 1)
        assert "E01 Winter Is Coming" in season

    def test_discover_with_provider_and_genre(self, live_tmdb):
        out = live_tmdb.discover(genres="Drama", providers="Netflix", sort_by="rating")
        assert "Discover movie on Netflix (AU)" in out

    def test_find_imdb(self, live_tmdb):
        assert "[movie 550]" in live_tmdb.find_by_external_id("tt0137523")

    def test_person(self, live_tmdb):
        assert "Fight Club" in live_tmdb.person_details(287)

    def test_lists(self, live_tmdb):
        assert live_tmdb.browse_list("now_playing").startswith("Now playing (AU)")
        assert "Trending" in live_tmdb.trending("all", "week")


@requires_omdb
class TestLiveOMDb:
    def test_ratings(self):
        from film_tv_mcp.ratings_client import OMDbClient
        labels = {r["label"] for r in OMDbClient().ratings("tt0137523")}
        assert "IMDb" in labels


@requires_mdblist
class TestLiveMDBList:
    def test_ratings(self):
        from film_tv_mcp.ratings_client import MDBListClient
        labels = {r["label"] for r in MDBListClient().ratings(550, "movie")}
        assert "IMDb" in labels
