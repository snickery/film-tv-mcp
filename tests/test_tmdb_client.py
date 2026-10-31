"""Tier 1 + Tier 2 tests for TMDBClient.

Tier 1 covers the pure helpers (kind aliases, name resolution, row and
provider formatters). Tier 2 exercises the public methods against a mocked
_get — the `tmdb` fixture replaces it so no network calls happen.

Run with:
    uv run --extra test pytest tests/test_tmdb_client.py -v
"""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from film_tv_mcp.tmdb_client import TMDBClient

# ===================================================================
# Tier 1 — pure helpers
# ===================================================================

class TestKind:
    @pytest.mark.parametrize("alias,expected", [
        ("movie", "movie"), ("Film", "movie"), ("tv", "tv"), ("series", "tv"), ("Show", "tv"),
    ])
    def test_aliases(self, alias, expected):
        assert TMDBClient._kind(alias) == expected

    def test_all_only_when_allowed(self):
        assert TMDBClient._kind("all", allow_all=True) == "all"
        with pytest.raises(ValueError, match="Unknown kind"):
            TMDBClient._kind("all")

    def test_unknown(self):
        with pytest.raises(ValueError, match="movie, tv"):
            TMDBClient._kind("podcast")


TABLE = {"netflix": 8, "netflix standard with ads": 1796, "stan": 21, "disney plus": 337}


class TestResolveNames:
    def test_exact_match_wins(self):
        assert TMDBClient._resolve_names("Netflix", TABLE, "provider") == [8]

    def test_partial_match_takes_all(self):
        assert TMDBClient._resolve_names("disney", TABLE, "provider") == [337]
        assert TMDBClient._resolve_names("ads", TABLE, "provider") == [1796]

    def test_multiple_and_dedupe(self):
        assert TMDBClient._resolve_names("stan, Stan,netflix", TABLE, "provider") == [21, 8]

    def test_unknown_lists_known(self):
        with pytest.raises(ValueError, match="Unknown provider 'binge'. Known: disney plus"):
            TMDBClient._resolve_names("binge", TABLE, "provider")


class TestFormatting:
    def test_movie_row(self):
        row = TMDBClient._format_row(
            {"id": 550, "title": "Fight Club", "release_date": "1999-10-15",
             "vote_average": 8.438, "vote_count": 31000, "overview": "An insomniac..."},
            "movie",
        )
        assert "Fight Club (1999) [movie 550]" in row
        assert "★8.4 (31.0k votes)" in row
        assert "An insomniac..." in row

    def test_multi_row_uses_media_type(self):
        row = TMDBClient._format_row({"id": 1399, "name": "Game of Thrones", "media_type": "tv",
                                      "first_air_date": "2011-04-17"})
        assert "[tv 1399]" in row

    def test_person_row(self):
        row = TMDBClient._format_row({"id": 287, "name": "Brad Pitt", "media_type": "person",
                                      "known_for_department": "Acting",
                                      "known_for": [{"title": "Fight Club"}, {"name": "Friends"}]})
        assert "Brad Pitt [person 287] — Acting" in row
        assert "known for: Fight Club, Friends" in row

    def test_missing_date(self):
        assert "(?)" in TMDBClient._format_row({"id": 1, "title": "Untitled"}, "movie")

    def test_overview_truncated(self):
        row = TMDBClient._format_row({"id": 1, "title": "X", "overview": "word " * 200}, "movie")
        assert row.endswith("…")

    def test_empty_page(self):
        assert TMDBClient._format_page({"results": []}, "Search 'x'") == "Search 'x': no results."

    def test_providers(self):
        out = TMDBClient._format_providers({
            "link": "https://www.themoviedb.org/movie/550/watch?locale=AU",
            "flatrate": [{"provider_name": "Netflix"}],
            "rent": [{"provider_name": "Apple TV"}, {"provider_name": "Google Play Movies"}],
        }, "AU")
        assert "JustWatch" in out
        assert "Stream: Netflix" in out
        assert "Rent: Apple TV, Google Play Movies" in out
        assert "Buy" not in out

    def test_no_providers(self):
        assert "not available" in TMDBClient._format_providers(None, "AU")

    def test_votes(self):
        assert TMDBClient._votes(950) == "950"
        assert TMDBClient._votes(2_400_000) == "2.4M"

    def test_redact(self):
        assert TMDBClient._redact_key("GET /x?api_key=abc123&page=1") == "GET /x?api_key=REDACTED&page=1"


# ===================================================================
# Tier 2 — public methods with mocked _get
# ===================================================================

PAGE = {"page": 1, "total_pages": 1, "total_results": 1,
        "results": [{"id": 550, "title": "Fight Club", "release_date": "1999-10-15"}]}


class TestSetup:
    def test_unconfigured_raises_setup_pointer(self, unconfigured_tmdb):
        with pytest.raises(ValueError, match="TMDB_API_TOKEN"):
            unconfigured_tmdb.search("anything")

    def test_bearer_token_header(self):
        c = TMDBClient(token="tok", api_key="")
        c._client = MagicMock()
        c._client.get.return_value = httpx.Response(200, json={"ok": True})
        c._get("/configuration")
        _, kwargs = c._client.get.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer tok"
        assert "api_key" not in kwargs["params"]

    def test_v3_key_param(self):
        c = TMDBClient(token="", api_key="k3")
        c._client = MagicMock()
        c._client.get.return_value = httpx.Response(200, json={})
        c._get("/configuration")
        _, kwargs = c._client.get.call_args
        assert kwargs["params"]["api_key"] == "k3"
        assert "Authorization" not in kwargs["headers"]

    def test_error_text_redacted(self):
        c = TMDBClient(token="", api_key="secret")
        c._client = MagicMock()
        c._client.get.return_value = httpx.Response(401, text="bad url ?api_key=secret")
        with pytest.raises(RuntimeError) as e:
            c._get("/configuration")
        assert "secret" not in str(e.value)

    def test_404_is_value_error(self):
        c = TMDBClient(token="t", api_key="")
        c._client = MagicMock()
        c._client.get.return_value = httpx.Response(404, json={})
        with pytest.raises(ValueError, match="check the id and kind"):
            c._get("/movie/0")


class TestSearch:
    def test_multi(self, tmdb):
        tmdb._get.return_value = {**PAGE, "results": [{**PAGE["results"][0], "media_type": "movie"}]}
        out = tmdb.search("fight club")
        tmdb._get.assert_called_once()
        path, params = tmdb._get.call_args.args
        assert path == "/search/multi"
        assert params["query"] == "fight club"
        assert "[movie 550]" in out

    def test_movie_year(self, tmdb):
        tmdb._get.return_value = PAGE
        tmdb.search("dune", kind="movie", year=2021)
        path, params = tmdb._get.call_args.args
        assert path == "/search/movie"
        assert params["primary_release_year"] == 2021

    def test_tv_year(self, tmdb):
        tmdb._get.return_value = PAGE
        tmdb.search("bluey", kind="series", year=2018)
        path, params = tmdb._get.call_args.args
        assert path == "/search/tv"
        assert params["first_air_date_year"] == 2018

    def test_people(self, tmdb):
        tmdb._get.return_value = {"results": []}
        tmdb.search("pitt", kind="people")
        assert tmdb._get.call_args.args[0] == "/search/person"

    def test_empty_query(self, tmdb):
        with pytest.raises(ValueError, match="empty"):
            tmdb.search("  ")


MOVIE = {
    "id": 550, "title": "Fight Club", "original_title": "Fight Club", "release_date": "1999-10-15",
    "tagline": "Mischief. Mayhem. Soap.", "genres": [{"name": "Drama"}], "runtime": 139,
    "status": "Released", "vote_average": 8.4, "vote_count": 31000, "overview": "An insomniac...",
    "belongs_to_collection": None, "budget": 63000000, "revenue": 100853753,
    "credits": {
        "cast": [{"name": "Brad Pitt", "character": "Tyler Durden"}, {"name": "Edward Norton", "character": "Narrator"}],
        "crew": [{"name": "David Fincher", "job": "Director", "department": "Directing"},
                 {"name": "Jim Uhls", "job": "Screenplay", "department": "Writing"}],
    },
    "external_ids": {"imdb_id": "tt0137523", "wikidata_id": "Q190050"},
    "videos": {"results": [{"site": "YouTube", "type": "Trailer", "key": "qtRKdVHc-cE", "official": True}]},
    "release_dates": {"results": [
        {"iso_3166_1": "US", "release_dates": [{"certification": "R", "type": 3, "release_date": "1999-10-15T00:00:00.000Z"}]},
        {"iso_3166_1": "AU", "release_dates": [
            {"certification": "R18+", "type": 3, "release_date": "1999-11-11T00:00:00.000Z"},
            {"certification": "", "type": 4, "release_date": "2010-01-01T00:00:00.000Z"},
        ]},
    ]},
    "watch/providers": {"results": {"AU": {"flatrate": [{"provider_name": "Netflix"}]}}},
}

SHOW = {
    "id": 82728, "name": "Bluey", "first_air_date": "2018-10-01", "last_air_date": "2024-04-14",
    "genres": [{"name": "Animation"}, {"name": "Kids"}], "status": "Returning Series",
    "vote_average": 8.8, "vote_count": 500, "created_by": [{"name": "Joe Brumm"}],
    "networks": [{"name": "ABC Kids"}], "number_of_seasons": 3, "number_of_episodes": 154,
    "episode_run_time": [7], "credits": {"cast": [], "crew": []},
    "external_ids": {"imdb_id": "tt7678620", "tvdb_id": 353546},
    "videos": {"results": []},
    "content_ratings": {"results": [{"iso_3166_1": "AU", "rating": "G"}]},
    "last_episode_to_air": {"season_number": 3, "episode_number": 49, "name": "The Sign", "air_date": "2024-04-14"},
    "next_episode_to_air": None,
    "seasons": [{"name": "Season 1", "episode_count": 52, "air_date": "2018-10-01"}],
    "watch/providers": {"results": {}},
}


class TestTitleDetails:
    def test_movie(self, tmdb):
        tmdb._get.return_value = MOVIE
        out = tmdb.title_details(550, "movie")
        path, params = tmdb._get.call_args.args
        assert path == "/movie/550"
        assert "release_dates" in params["append_to_response"]
        assert "watch/providers" in params["append_to_response"]
        assert "Fight Club (1999) [movie 550]" in out
        assert "Classification (AU): R18+" in out
        assert "Release (AU): cinemas 1999-11-11, digital 2010-01-01" in out
        assert "Director: David Fincher" in out
        assert "Brad Pitt (Tyler Durden)" in out
        assert "imdb tt0137523" in out
        assert "youtube.com/watch?v=qtRKdVHc-cE" in out
        assert "Stream: Netflix" in out
        assert "Original title" not in out

    def test_movie_other_region(self, tmdb):
        tmdb._get.return_value = MOVIE
        out = tmdb.title_details(550, "movie", region="us")
        assert "Classification (US): R" in out
        assert "Where to watch (US): not available" in out

    def test_tv(self, tmdb):
        tmdb._get.return_value = SHOW
        out = tmdb.title_details(82728, "tv")
        params = tmdb._get.call_args.args[1]
        assert "content_ratings" in params["append_to_response"]
        assert "Created by: Joe Brumm" in out
        assert "Seasons: 3, episodes: 154" in out
        assert "Classification (AU): G" in out
        assert "Last episode: S03E49 'The Sign' 2024-04-14" in out
        assert "tvdb 353546" in out
        assert "Next episode" not in out


class TestSeason:
    def test_episodes(self, tmdb):
        tmdb._get.return_value = {"name": "Season 1", "air_date": "2018-10-01", "episodes": [
            {"episode_number": 1, "name": "Magic Xylophone", "air_date": "2018-10-01", "runtime": 7,
             "vote_average": 8.0, "vote_count": 10, "overview": "Bluey and Bingo..."},
            {"episode_number": 2, "name": "Hospital", "air_date": None},
        ]}
        out = tmdb.tv_season(82728, 1)
        assert tmdb._get.call_args.args[0] == "/tv/82728/season/1"
        assert "E01 Magic Xylophone — 2018-10-01, 7 min, ★8.0" in out
        assert "E02 Hospital — TBA" in out


class TestDiscover:
    @pytest.fixture
    def tables(self, tmdb):
        def fake_get(path, params=None):
            if path == "/genre/movie/list":
                return {"genres": [{"id": 878, "name": "Science Fiction"}, {"id": 18, "name": "Drama"}]}
            if path == "/watch/providers/movie":
                return {"results": [{"provider_id": 8, "provider_name": "Netflix"},
                                    {"provider_id": 21, "provider_name": "Stan"}]}
            return PAGE
        tmdb._get.side_effect = fake_get
        return tmdb

    def _discover_params(self, tmdb):
        call = [c for c in tmdb._get.call_args_list if c.args[0].startswith("/discover")][-1]
        return call.args[1]

    def test_genres_and_years(self, tables):
        tables.discover(genres="science fiction, drama", year_from=2015, year_to=2020)
        p = self._discover_params(tables)
        assert p["with_genres"] == "878,18"
        assert p["primary_release_date.gte"] == "2015-01-01"
        assert p["primary_release_date.lte"] == "2020-12-31"
        assert p["sort_by"] == "popularity.desc"

    def test_providers(self, tables):
        out = tables.discover(providers="Netflix,stan")
        p = self._discover_params(tables)
        assert p["with_watch_providers"] == "8|21"
        assert p["watch_region"] == "AU"
        assert p["with_watch_monetization_types"] == "flatrate"
        assert "JustWatch" in out

    def test_availability_any_omits_monetization(self, tables):
        tables.discover(providers="Netflix", availability="any")
        assert "with_watch_monetization_types" not in self._discover_params(tables)

    def test_rating_sort_vote_floor(self, tables):
        tables.discover(sort_by="rating")
        p = self._discover_params(tables)
        assert p["sort_by"] == "vote_average.desc"
        assert p["vote_count.gte"] == 200

    def test_newest_caps_at_today(self, tables):
        tables.discover(sort_by="newest")
        p = self._discover_params(tables)
        assert p["sort_by"] == "primary_release_date.desc"
        assert "primary_release_date.lte" in p

    def test_tv_date_field(self, tmdb):
        tmdb._get.return_value = PAGE
        tmdb.discover(kind="tv", year_from=2020, original_language="ja,ko")
        p = tmdb._get.call_args.args[1]
        assert p["first_air_date.gte"] == "2020-01-01"
        assert p["with_original_language"] == "ja|ko"

    def test_revenue_movie_only(self, tmdb):
        with pytest.raises(ValueError, match="sort_by"):
            tmdb.discover(kind="tv", sort_by="revenue")

    def test_genre_table_cached(self, tables):
        tables.discover(genres="drama")
        tables.discover(genres="drama")
        paths = [c.args[0] for c in tables._get.call_args_list]
        assert paths.count("/genre/movie/list") == 1


class TestLists:
    def test_trending(self, tmdb):
        tmdb._get.return_value = PAGE
        tmdb.trending("tv", "day")
        assert tmdb._get.call_args.args[0] == "/trending/tv/day"

    def test_trending_bad_window(self, tmdb):
        with pytest.raises(ValueError, match="window"):
            tmdb.trending("all", "month")

    def test_now_playing_regional(self, tmdb):
        tmdb._get.return_value = PAGE
        out = tmdb.browse_list("now_playing")
        path, params = tmdb._get.call_args.args
        assert path == "/movie/now_playing"
        assert params["region"] == "AU"
        assert out.startswith("Now playing (AU)")

    def test_tv_list_no_region(self, tmdb):
        tmdb._get.return_value = PAGE
        tmdb.browse_list("airing_today")
        assert "region" not in tmdb._get.call_args.args[1]

    def test_unknown_list(self, tmdb):
        with pytest.raises(ValueError, match="now_playing"):
            tmdb.browse_list("box_office")

    def test_recommendations(self, tmdb):
        tmdb._get.return_value = PAGE
        tmdb.recommendations(550, "film")
        assert tmdb._get.call_args.args[0] == "/movie/550/recommendations"


WATCH_RESULTS = {"results": {
    "AU": {"flatrate": [{"provider_name": "Netflix"}]},
    "GB": {"rent": [{"provider_name": "Apple TV"}]},
    "US": {"flatrate": [{"provider_name": "Hulu"}]},
}}


class TestWhereToWatch:
    def test_default_region(self, tmdb):
        tmdb._get.return_value = WATCH_RESULTS
        assert "Stream: Netflix" in tmdb.where_to_watch(550)

    def test_all_regions_streaming_only(self, tmdb):
        tmdb._get.return_value = WATCH_RESULTS
        out = tmdb.where_to_watch(550, region="all")
        assert "AU: Netflix" in out
        assert "US: Hulu" in out
        assert "GB" not in out


class TestFind:
    def test_imdb_autodetect(self, tmdb):
        tmdb._get.return_value = {"movie_results": [PAGE["results"][0]], "tv_results": [],
                                  "person_results": [], "tv_episode_results": []}
        out = tmdb.find_by_external_id("tt0137523")
        path, params = tmdb._get.call_args.args
        assert path == "/find/tt0137523"
        assert params["external_source"] == "imdb_id"
        assert "[movie 550]" in out

    def test_tvdb_short_source(self, tmdb):
        tmdb._get.return_value = {}
        out = tmdb.find_by_external_id("353546", source="tvdb")
        assert tmdb._get.call_args.args[1]["external_source"] == "tvdb_id"
        assert "No TMDB match" in out

    def test_numeric_needs_source(self, tmdb):
        with pytest.raises(ValueError, match="pass source"):
            tmdb.find_by_external_id("353546")

    def test_unknown_source(self, tmdb):
        with pytest.raises(ValueError, match="Unknown source"):
            tmdb.find_by_external_id("x", source="letterboxd")


class TestPerson:
    def test_credits_sorted_by_votes(self, tmdb):
        tmdb._get.return_value = {
            "name": "Brad Pitt", "known_for_department": "Acting", "birthday": "1963-12-18",
            "place_of_birth": "Shawnee, Oklahoma, USA", "external_ids": {"imdb_id": "nm0000093"},
            "combined_credits": {
                "cast": [
                    {"id": 1, "media_type": "movie", "title": "Obscure", "release_date": "2001-01-01", "vote_count": 3},
                    {"id": 550, "media_type": "movie", "title": "Fight Club", "release_date": "1999-10-15",
                     "character": "Tyler Durden", "vote_count": 31000},
                ],
                "crew": [{"id": 2, "media_type": "movie", "title": "Prod", "job": "Producer", "vote_count": 5},
                         {"id": 3, "media_type": "movie", "title": "Grip", "job": "Key Grip", "vote_count": 9}],
            },
        }
        out = tmdb.person_details(287)
        assert out.index("Fight Club") < out.index("Obscure")
        assert "imdb nm0000093" in out
        assert "Producer" in out
        assert "Key Grip" not in out


class TestRatingsBasics:
    def test_basics(self, tmdb):
        tmdb._get.return_value = MOVIE
        b = tmdb.ratings_basics(550, "movie")
        assert b == {"kind": "movie", "title": "Fight Club", "year": "1999", "tmdb_score": 8.4,
                     "tmdb_votes": 31000, "imdb_id": "tt0137523"}
