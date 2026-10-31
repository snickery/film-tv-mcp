"""Tier 1 + Tier 2 tests for the OMDb / MDBList clients and the merged report.

The provider clients' _get is mocked; the TMDB side is a MagicMock standing
in for TMDBClient.ratings_basics.

Run with:
    uv run --extra test pytest tests/test_ratings_client.py -v
"""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from film_tv_mcp.ratings_client import MDBListClient, OMDbClient, ratings_report

OMDB_FIGHT_CLUB = {
    "Response": "True", "imdbVotes": "2,512,345",
    "Ratings": [
        {"Source": "Internet Movie Database", "Value": "8.8/10"},
        {"Source": "Rotten Tomatoes", "Value": "79%"},
        {"Source": "Metacritic", "Value": "67/100"},
    ],
}

MDBLIST_FIGHT_CLUB = {"ratings": [
    {"source": "imdb", "value": 8.8, "score": 88, "votes": 2512345, "url": "https://www.imdb.com/title/tt0137523/"},
    {"source": "tomatoes", "value": 79, "score": 79, "votes": 180, "url": "https://www.rottentomatoes.com/m/fight_club"},
    {"source": "popcorn", "value": 96, "score": 96, "votes": 250000},
    {"source": "letterboxd", "value": 4.3, "score": 86, "votes": 1200000},
    {"source": "metacritic", "value": None, "score": None, "votes": None},
    {"source": "somethingnew", "value": 1, "score": 1},
]}


def _omdb(data=None, key="k"):
    c = OMDbClient(api_key=key)
    c._get = MagicMock(return_value=data if data is not None else OMDB_FIGHT_CLUB)
    return c


def _mdblist(data=None, key="k"):
    c = MDBListClient(api_key=key)
    c._get = MagicMock(return_value=data if data is not None else MDBLIST_FIGHT_CLUB)
    return c


def _tmdb(imdb_id="tt0137523"):
    t = MagicMock()
    t.ratings_basics.return_value = {"kind": "movie", "title": "Fight Club", "year": "1999",
                                     "tmdb_score": 8.4, "tmdb_votes": 31000, "imdb_id": imdb_id}
    return t


class TestOMDb:
    def test_ratings(self):
        rows = _omdb().ratings("tt0137523")
        assert [r["label"] for r in rows] == ["IMDb", "Rotten Tomatoes (critics)", "Metacritic (critics)"]
        assert rows[0]["detail"] == "2.5M votes"
        assert rows[1]["value"] == "79%"

    def test_not_found(self):
        with pytest.raises(RuntimeError, match="Incorrect IMDb ID"):
            _omdb({"Response": "False", "Error": "Incorrect IMDb ID."}).ratings("tt0")

    def test_http_error_redacted(self):
        c = OMDbClient(api_key="sekrit")
        c._client = MagicMock()
        c._client.get.return_value = httpx.Response(401, text="nope ?apikey=sekrit")
        with pytest.raises(RuntimeError) as e:
            c.ratings("tt1")
        assert "sekrit" not in str(e.value)


class TestMDBList:
    def test_ratings_scales(self):
        c = _mdblist()
        rows = {r["label"]: r for r in c.ratings(550, "movie")}
        assert c._get.call_args.args[0].endswith("/tmdb/movie/550")
        assert rows["IMDb"]["value"] == "8.8/10"
        assert rows["Rotten Tomatoes (critics)"]["value"] == "79%"
        assert rows["Rotten Tomatoes (audience)"]["value"] == "96%"
        assert rows["Letterboxd"]["value"] == "4.3/5"
        assert "Metacritic (critics)" not in rows  # null value skipped
        assert len(rows) == 4  # unknown source skipped

    def test_tv_uses_show(self):
        c = _mdblist({"ratings": []})
        c.ratings(1399, "tv")
        assert c._get.call_args.args[0].endswith("/tmdb/show/1399")

    def test_error_body(self):
        with pytest.raises(RuntimeError, match="Invalid API key"):
            _mdblist({"error": "Invalid API key"}).ratings(1, "movie")


class TestReport:
    def test_tmdb_only(self):
        out = ratings_report(_tmdb(), _omdb(key=""), _mdblist(key=""), 550, "movie")
        assert "TMDB: 8.4/10 (31.0k votes)" in out
        assert "set OMDB_API_KEY and/or MDBLIST_API_KEY" in out

    def test_merge_prefers_mdblist_and_fills_from_omdb(self):
        out = ratings_report(_tmdb(), _omdb(), _mdblist(), 550, "movie")
        assert "IMDb: 8.8/10 (2.5M votes) — via MDBList" in out
        assert "Rotten Tomatoes (audience): 96%" in out
        # MDBList had a null Metacritic, so OMDb's fills the gap.
        assert "Metacritic (critics): 67/100 — via OMDb" in out
        assert out.count("IMDb:") == 1

    def test_provider_failure_is_a_note(self):
        bad = _mdblist()
        bad._get.side_effect = RuntimeError("MDBList HTTP 429: limit")
        out = ratings_report(_tmdb(), _omdb(), bad, 550, "movie")
        assert "note: MDBList HTTP 429" in out
        assert "IMDb: 8.8/10" in out

    def test_omdb_skipped_without_imdb_id(self):
        omdb = _omdb()
        out = ratings_report(_tmdb(imdb_id=""), omdb, _mdblist(key=""), 1, "movie")
        omdb._get.assert_not_called()
        assert "no IMDb id" in out
