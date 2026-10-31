"""Optional ratings providers for film-tv-mcp, and the merged ratings report.

Rotten Tomatoes has no public API (the official one is a paid partner
licence through Fandango), so its scores come second-hand from two keyed
aggregators. Either, both or neither can be configured:

* **OMDb** (omdbapi.com, keyed by IMDb id): IMDb rating + votes, Rotten
  Tomatoes Tomatometer, Metascore.
* **MDBList** (api.mdblist.com, keyed by TMDB id): the same plus the RT
  audience score (Popcornmeter), Metacritic user score, Letterboxd, Trakt,
  MyAnimeList and RogerEbert.com.

A provider failure never fails the report; it is listed as a note instead.
When both providers return the same source, MDBList's row is kept (it is
the richer of the two) and OMDb only fills gaps.
"""
from __future__ import annotations

import re

import httpx

from film_tv_mcp.config import MDBLIST_API_KEY, OMDB_API_KEY

OMDB_URL = "https://www.omdbapi.com/"
MDBLIST_URL = "https://api.mdblist.com"

_HTTP_TIMEOUT = 15

# MDBList source → (label, native scale). A scale of 0 means "show the
# normalised 0-100 score as a percentage".
_MDBLIST_SOURCES = {
    "imdb": ("IMDb", 10),
    "tomatoes": ("Rotten Tomatoes (critics)", 0),
    "popcorn": ("Rotten Tomatoes (audience)", 0),
    "tomatoesaudience": ("Rotten Tomatoes (audience)", 0),
    "metacritic": ("Metacritic (critics)", 100),
    "metacriticuser": ("Metacritic (users)", 10),
    "letterboxd": ("Letterboxd", 5),
    "trakt": ("Trakt", 0),
    "myanimelist": ("MyAnimeList", 10),
    "rogerebert": ("RogerEbert.com", 4),
}

_OMDB_SOURCES = {
    "Internet Movie Database": "IMDb",
    "Rotten Tomatoes": "Rotten Tomatoes (critics)",
    "Metacritic": "Metacritic (critics)",
}


def _redact(text: str) -> str:
    return re.sub(r"(apikey=)[^&\s'\"]+", r"\1REDACTED", text)


def _votes(n) -> str:
    try:
        n = int(str(n).replace(",", ""))
    except (TypeError, ValueError):
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M votes"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k votes"
    return f"{n} votes"


class _KeyedClient:
    name = ""

    def __init__(self, api_key: str, timeout: int = _HTTP_TIMEOUT):
        self._api_key = api_key
        self._client = httpx.Client(timeout=timeout)

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def _get(self, url: str, params: dict) -> dict:
        try:
            resp = self._client.get(url, params={**params, "apikey": self._api_key})
        except httpx.HTTPError as e:
            raise RuntimeError(f"{self.name} request failed: {_redact(str(e))}") from None
        if resp.status_code != 200:
            raise RuntimeError(f"{self.name} HTTP {resp.status_code}: {_redact(resp.text[:200])}")
        return resp.json()


class OMDbClient(_KeyedClient):
    name = "OMDb"

    def __init__(self, api_key: str | None = None, timeout: int = _HTTP_TIMEOUT):
        super().__init__(OMDB_API_KEY if api_key is None else api_key, timeout)

    def ratings(self, imdb_id: str) -> list[dict]:
        data = self._get(OMDB_URL, {"i": imdb_id})
        if data.get("Response") == "False":
            raise RuntimeError(f"OMDb: {data.get('Error', 'no result')}")
        rows = []
        for r in data.get("Ratings", []):
            label = _OMDB_SOURCES.get(r.get("Source", ""))
            if not label:
                continue
            row = {"label": label, "value": r.get("Value", ""), "detail": "", "url": ""}
            if label == "IMDb":
                row["detail"] = _votes(data.get("imdbVotes"))
                row["url"] = f"https://www.imdb.com/title/{imdb_id}/"
            rows.append(row)
        return rows

    def check(self) -> str:
        self.ratings("tt0111161")
        return "ok"


class MDBListClient(_KeyedClient):
    name = "MDBList"

    def __init__(self, api_key: str | None = None, timeout: int = _HTTP_TIMEOUT):
        super().__init__(MDBLIST_API_KEY if api_key is None else api_key, timeout)

    @staticmethod
    def _format_value(value, score, scale: int) -> str:
        if scale == 0 or value is None:
            return f"{score:g}%" if score is not None else ""
        return f"{value:g}/{scale}"

    def ratings(self, tmdb_id: int, kind: str) -> list[dict]:
        media = "movie" if kind == "movie" else "show"
        data = self._get(f"{MDBLIST_URL}/tmdb/{media}/{tmdb_id}", {})
        if data.get("error"):
            raise RuntimeError(f"MDBList: {data['error']}")
        rows = []
        for r in data.get("ratings", []):
            source = r.get("source", "")
            if source not in _MDBLIST_SOURCES:
                continue
            label, scale = _MDBLIST_SOURCES[source]
            value = self._format_value(r.get("value"), r.get("score"), scale)
            if not value:
                continue
            rows.append({
                "label": label,
                "value": value,
                "detail": _votes(r["votes"]) if r.get("votes") else "",
                # MDBList's "url" is usually a site-relative slug or id
                # ("/m/fight_club", or even the int 167), so keep only
                # absolute links.
                "url": url if (url := str(r.get("url") or "")).startswith("http") else "",
            })
        return rows

    def check(self) -> str:
        data = self._get(f"{MDBLIST_URL}/user", {})
        used, limit = data.get("api_requests_count"), data.get("api_requests")
        return f"ok ({used}/{limit} requests used today)" if limit else "ok"


def ratings_report(tmdb, omdb: OMDbClient, mdblist: MDBListClient, tmdb_id: int, kind: str) -> str:
    basics = tmdb.ratings_basics(tmdb_id, kind)
    kind = basics["kind"]
    lines = [f"Ratings for {basics['title']} ({basics['year']}) [{kind} {tmdb_id}]:"]
    merged: dict[str, dict] = {}
    notes: list[str] = []
    if mdblist.configured:
        try:
            for row in mdblist.ratings(tmdb_id, kind):
                merged.setdefault(row["label"], {**row, "via": "MDBList"})
        except (RuntimeError, ValueError) as e:
            notes.append(str(e))
    if omdb.configured:
        if basics["imdb_id"]:
            try:
                for row in omdb.ratings(basics["imdb_id"]):
                    merged.setdefault(row["label"], {**row, "via": "OMDb"})
            except (RuntimeError, ValueError) as e:
                notes.append(str(e))
        else:
            notes.append("OMDb skipped: TMDB has no IMDb id for this title.")
    lines.append(
        f"  TMDB: {basics['tmdb_score']:.1f}/10 ({_votes(basics['tmdb_votes'])})"
    )
    for row in merged.values():
        line = f"  {row['label']}: {row['value']}"
        if row["detail"]:
            line += f" ({row['detail']})"
        line += f" — via {row['via']}"
        if row["url"]:
            line += f" {row['url']}"
        lines.append(line)
    if not (omdb.configured or mdblist.configured):
        notes.append(
            "Only TMDB's own score is available: set OMDB_API_KEY and/or "
            "MDBLIST_API_KEY for IMDb, Rotten Tomatoes, Metacritic and Letterboxd."
        )
    lines.extend(f"  note: {n}" for n in notes)
    return "\n".join(lines)
