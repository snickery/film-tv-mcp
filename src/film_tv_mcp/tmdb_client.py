"""TMDB (The Movie Database) client for film-tv-mcp.

TMDB is the core provider: search, title/person/season detail, discovery,
trending and curated lists, recommendations, external-ID lookup, and
where-to-watch (TMDB's provider data comes from JustWatch, which must be
credited wherever it is shown).

Design notes
------------
* One httpx client (thread-safe, so no locking is needed under the server's
  threadpool). Auth is the v4 read token as a bearer header, or the v3 key
  as a query param, redacted from any error text.
* Titles are addressed as (tmdb_id, kind) where kind is 'movie' or 'tv';
  every formatted row carries both so the model can pass them straight back.
* Detail calls fold credits, external IDs, videos, certifications and watch
  providers into one request with ``append_to_response``.
* Genre and provider name→ID tables are fetched once per process and cached,
  so discover() takes names ('Science Fiction', 'Netflix'), not IDs.
* Formatters return compact human-readable text rather than raw JSON.
"""
from __future__ import annotations

import re
from datetime import date

import httpx

from film_tv_mcp.config import LANGUAGE, TMDB_API_KEY, TMDB_API_TOKEN, WATCH_REGION

API_BASE = "https://api.themoviedb.org/3"
SITE_BASE = "https://www.themoviedb.org"

_HTTP_TIMEOUT = 20
_OVERVIEW_CHARS = 220

_KINDS = {
    "movie": "movie", "movies": "movie", "film": "movie", "films": "movie",
    "tv": "tv", "show": "tv", "shows": "tv", "series": "tv", "tv_show": "tv",
}

_SORTS = {
    "popularity": "popularity.desc",
    "rating": "vote_average.desc",
    "newest": "{date}.desc",
    "oldest": "{date}.asc",
    "revenue": "revenue.desc",
}

# Rating sorts are dominated by obscure titles with a handful of 10/10 votes
# unless a vote floor is set; this is applied when the caller gives none.
_RATING_SORT_MIN_VOTES = 200

_AVAILABILITY = {
    "stream": "flatrate",
    "free": "free|ads",
    "rent": "rent",
    "buy": "buy",
    "any": "",
}

_LISTS = {
    "now_playing": "/movie/now_playing",
    "upcoming": "/movie/upcoming",
    "popular_movies": "/movie/popular",
    "top_rated_movies": "/movie/top_rated",
    "airing_today": "/tv/airing_today",
    "on_the_air": "/tv/on_the_air",
    "popular_tv": "/tv/popular",
    "top_rated_tv": "/tv/top_rated",
}

_EXTERNAL_SOURCES = (
    "imdb_id", "tvdb_id", "wikidata_id", "facebook_id", "instagram_id",
    "tiktok_id", "twitter_id", "youtube_id",
)

_PROVIDER_GROUPS = (
    ("flatrate", "Stream"), ("free", "Free"), ("ads", "Free with ads"),
    ("rent", "Rent"), ("buy", "Buy"),
)

# release_dates.type values worth surfacing (TMDB enum).
_RELEASE_TYPES = {3: "cinemas", 4: "digital", 5: "physical", 6: "TV"}

_JUSTWATCH_CREDIT = "(where-to-watch data: JustWatch)"


class TMDBClient:
    def __init__(
        self,
        token: str | None = None,
        api_key: str | None = None,
        region: str | None = None,
        language: str | None = None,
        timeout: int = _HTTP_TIMEOUT,
    ):
        # None means "use the env-configured value"; tests inject their own.
        self._token = TMDB_API_TOKEN if token is None else token
        self._api_key = TMDB_API_KEY if api_key is None else api_key
        self.region = (region or WATCH_REGION).upper()
        self.language = language or LANGUAGE
        self._client = httpx.Client(timeout=timeout)
        self._genres: dict[str, dict[str, int]] = {}
        self._providers: dict[tuple[str, str], dict[str, int]] = {}

    @property
    def configured(self) -> bool:
        return bool(self._token or self._api_key)

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    @staticmethod
    def _redact_key(text: str) -> str:
        """Strip api_key query values from error text before it can propagate."""
        return re.sub(r"(api_key=)[^&\s'\"]+", r"\1REDACTED", text)

    def _get(self, path: str, params: dict | None = None) -> dict:
        if not self.configured:
            raise ValueError(
                "TMDB is not configured. Set TMDB_API_TOKEN (the v4 'API Read "
                "Access Token') or TMDB_API_KEY from themoviedb.org → Settings "
                "→ API in the server's env file — see the README § Setup."
            )
        params = {"language": self.language, **(params or {})}
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        else:
            params["api_key"] = self._api_key
        try:
            resp = self._client.get(API_BASE + path, params=params, headers=headers)
        except httpx.HTTPError as e:
            raise RuntimeError(f"TMDB request failed: {self._redact_key(str(e))}") from None
        if resp.status_code == 404:
            raise ValueError(f"TMDB has nothing at {path} — check the id and kind.")
        if resp.status_code != 200:
            raise RuntimeError(
                f"TMDB HTTP {resp.status_code}: {self._redact_key(resp.text[:300])}"
            )
        return resp.json()

    # ------------------------------------------------------------------
    # Pure helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _kind(kind: str, allow_all: bool = False) -> str:
        key = kind.strip().lower()
        if allow_all and key in ("all", "multi", ""):
            return "all"
        if key not in _KINDS:
            choices = "movie, tv" + (", all" if allow_all else "")
            raise ValueError(f"Unknown kind '{kind}'. Use one of: {choices}.")
        return _KINDS[key]

    def _region(self, region: str) -> str:
        return (region or self.region).strip().upper()

    @staticmethod
    def _year(value: str | None) -> str:
        return value[:4] if value else "?"

    @staticmethod
    def _votes(n: int | None) -> str:
        n = n or 0
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}k"
        return str(n)

    @staticmethod
    def _truncate(text: str | None, limit: int = _OVERVIEW_CHARS) -> str:
        text = (text or "").strip()
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    @classmethod
    def _format_row(cls, item: dict, kind: str | None = None) -> str:
        """One search/list result. kind falls back to the item's media_type."""
        kind = kind or item.get("media_type", "movie")
        if kind == "person":
            known = ", ".join(
                k.get("title") or k.get("name") or "?" for k in item.get("known_for", [])[:3]
            )
            line = f"  • {item.get('name', '?')} [person {item.get('id')}]"
            dept = item.get("known_for_department")
            if dept:
                line += f" — {dept}"
            return line + (f"\n    known for: {known}" if known else "")
        title = item.get("title") or item.get("name") or "?"
        when = item.get("release_date") or item.get("first_air_date")
        line = f"  • {title} ({cls._year(when)}) [{kind} {item.get('id')}]"
        if item.get("vote_count"):
            line += f" — ★{item.get('vote_average', 0):.1f} ({cls._votes(item['vote_count'])} votes)"
        overview = cls._truncate(item.get("overview"))
        return line + (f"\n    {overview}" if overview else "")

    @classmethod
    def _format_page(cls, data: dict, heading: str, kind: str | None = None) -> str:
        results = data.get("results", [])
        if not results:
            return f"{heading}: no results."
        rows = [cls._format_row(r, kind) for r in results]
        page, pages = data.get("page", 1), data.get("total_pages", 1)
        footer = f"page {page} of {pages}, {data.get('total_results', len(results))} results"
        return f"{heading} ({footer}):\n" + "\n".join(rows)

    @staticmethod
    def _format_providers(entry: dict | None, region: str) -> str:
        if not entry:
            return f"Where to watch ({region}): not available to stream, rent or buy."
        lines = [f"Where to watch ({region}) {_JUSTWATCH_CREDIT}:"]
        for key, label in _PROVIDER_GROUPS:
            names = [p["provider_name"] for p in entry.get(key, [])]
            if names:
                lines.append(f"  {label}: {', '.join(names)}")
        if entry.get("link"):
            lines.append(f"  links: {entry['link']}")
        return "\n".join(lines)

    @staticmethod
    def _external_ids_line(ids: dict, kind: str, tmdb_id: int) -> str:
        parts = [f"tmdb {kind}/{tmdb_id}"]
        for key, label in (("imdb_id", "imdb"), ("tvdb_id", "tvdb"), ("wikidata_id", "wikidata")):
            if ids.get(key):
                parts.append(f"{label} {ids[key]}")
        return "IDs: " + " · ".join(parts)

    @staticmethod
    def _trailer(videos: dict) -> str:
        for v in videos.get("results", []):
            if v.get("site") == "YouTube" and v.get("type") == "Trailer" and v.get("official", True):
                return f"https://www.youtube.com/watch?v={v['key']}"
        return ""

    # ------------------------------------------------------------------
    # Name → ID tables (cached per process)
    # ------------------------------------------------------------------
    def _genre_ids(self, kind: str) -> dict[str, int]:
        if kind not in self._genres:
            data = self._get(f"/genre/{kind}/list")
            self._genres[kind] = {g["name"].lower(): g["id"] for g in data.get("genres", [])}
        return self._genres[kind]

    def _provider_ids(self, kind: str, region: str) -> dict[str, int]:
        key = (kind, region)
        if key not in self._providers:
            data = self._get(f"/watch/providers/{kind}", {"watch_region": region})
            self._providers[key] = {
                p["provider_name"].lower(): p["provider_id"] for p in data.get("results", [])
            }
        return self._providers[key]

    @staticmethod
    def _resolve_names(names: str, table: dict[str, int], what: str) -> list[int]:
        """Comma-separated names → IDs. Exact (case-insensitive) match wins;
        otherwise every name containing the term ('netflix' → Netflix and
        'Netflix Standard with Ads')."""
        ids: list[int] = []
        for raw in (n.strip().lower() for n in names.split(",")):
            if not raw:
                continue
            if raw in table:
                ids.append(table[raw])
                continue
            partial = [i for name, i in table.items() if raw in name]
            if not partial:
                sample = ", ".join(sorted(table)[:25])
                raise ValueError(f"Unknown {what} '{raw}'. Known: {sample}…")
            ids.extend(partial)
        return list(dict.fromkeys(ids))

    # ------------------------------------------------------------------
    # Public methods (one per tool)
    # ------------------------------------------------------------------
    def search(self, query: str, kind: str = "all", year: int = 0, page: int = 1) -> str:
        if not query.strip():
            raise ValueError("query must not be empty")
        key = kind.strip().lower()
        kind = "person" if key in ("person", "people") else self._kind(kind, allow_all=True)
        params: dict = {"query": query, "page": page, "include_adult": "false"}
        if year and kind == "movie":
            params["primary_release_year"] = year
        elif year and kind == "tv":
            params["first_air_date_year"] = year
        path = "/search/multi" if kind == "all" else f"/search/{kind}"
        data = self._get(path, params)
        row_kind = None if kind == "all" else kind
        return self._format_page(data, f"Search '{query}'", row_kind)

    def title_details(self, tmdb_id: int, kind: str = "movie", region: str = "") -> str:
        kind = self._kind(kind)
        region = self._region(region)
        ratings_part = "release_dates" if kind == "movie" else "content_ratings"
        d = self._get(f"/{kind}/{tmdb_id}", {
            "append_to_response": f"credits,external_ids,videos,watch/providers,{ratings_part}",
        })
        title = d.get("title") or d.get("name") or "?"
        when = d.get("release_date") or d.get("first_air_date")
        lines = [f"{title} ({self._year(when)}) [{kind} {tmdb_id}]"]
        original = d.get("original_title") or d.get("original_name")
        if original and original != title:
            lines.append(f"Original title: {original} ({d.get('original_language', '?')})")
        if d.get("tagline"):
            lines.append(f"Tagline: {d['tagline']}")
        genres = ", ".join(g["name"] for g in d.get("genres", []))
        if genres:
            lines.append(f"Genres: {genres}")
        lines.append(
            f"TMDB rating: {d.get('vote_average', 0):.1f}/10 ({self._votes(d.get('vote_count'))} votes)"
        )
        lines.extend(self._movie_facts(d, region) if kind == "movie" else self._tv_facts(d, region))
        crew = d.get("credits", {}).get("crew", [])
        if kind == "movie":
            directors = [c["name"] for c in crew if c.get("job") == "Director"]
            writers = [c["name"] for c in crew if c.get("department") == "Writing"][:4]
            if directors:
                lines.append(f"Director: {', '.join(directors)}")
            if writers:
                lines.append(f"Writing: {', '.join(dict.fromkeys(writers))}")
        cast = d.get("credits", {}).get("cast", [])[:8]
        if cast:
            lines.append("Cast: " + "; ".join(
                f"{c['name']} ({c['character']})" if c.get("character") else c["name"] for c in cast
            ))
        lines.append(self._external_ids_line(d.get("external_ids", {}), kind, tmdb_id))
        trailer = self._trailer(d.get("videos", {}))
        if trailer:
            lines.append(f"Trailer: {trailer}")
        lines.append(f"TMDB page: {SITE_BASE}/{kind}/{tmdb_id}")
        if d.get("overview"):
            lines.append(f"Overview: {d['overview']}")
        providers = d.get("watch/providers", {}).get("results", {}).get(region)
        lines.append(self._format_providers(providers, region))
        return "\n".join(lines)

    def _movie_facts(self, d: dict, region: str) -> list[str]:
        facts = []
        if d.get("runtime"):
            facts.append(f"Runtime: {d['runtime']} min")
        facts.append(f"Status: {d.get('status', '?')}")
        for r in d.get("release_dates", {}).get("results", []):
            if r.get("iso_3166_1") != region:
                continue
            cert = next((x["certification"] for x in r["release_dates"] if x.get("certification")), "")
            if cert:
                facts.append(f"Classification ({region}): {cert}")
            dates = [
                f"{_RELEASE_TYPES[x['type']]} {x['release_date'][:10]}"
                for x in r["release_dates"] if x.get("type") in _RELEASE_TYPES
            ]
            if dates:
                facts.append(f"Release ({region}): {', '.join(dict.fromkeys(dates))}")
        if d.get("belongs_to_collection"):
            facts.append(f"Collection: {d['belongs_to_collection']['name']}")
        if d.get("budget") or d.get("revenue"):
            facts.append(f"Budget/revenue (USD): {d.get('budget', 0):,} / {d.get('revenue', 0):,}")
        return facts

    def _tv_facts(self, d: dict, region: str) -> list[str]:
        facts = [f"Status: {d.get('status', '?')}"]
        if d.get("created_by"):
            facts.append(f"Created by: {', '.join(c['name'] for c in d['created_by'])}")
        if d.get("networks"):
            facts.append(f"Network: {', '.join(n['name'] for n in d['networks'])}")
        facts.append(
            f"Seasons: {d.get('number_of_seasons', '?')}, episodes: {d.get('number_of_episodes', '?')}"
            f" (aired {d.get('first_air_date') or '?'} → {d.get('last_air_date') or '?'})"
        )
        if d.get("episode_run_time"):
            facts.append(f"Episode runtime: {d['episode_run_time'][0]} min")
        for r in d.get("content_ratings", {}).get("results", []):
            if r.get("iso_3166_1") == region and r.get("rating"):
                facts.append(f"Classification ({region}): {r['rating']}")
        for key, label in (("last_episode_to_air", "Last episode"), ("next_episode_to_air", "Next episode")):
            ep = d.get(key)
            if ep:
                facts.append(
                    f"{label}: S{ep.get('season_number', 0):02d}E{ep.get('episode_number', 0):02d} "
                    f"'{ep.get('name', '')}' {ep.get('air_date') or 'date TBA'}"
                )
        seasons = [
            f"{s.get('name')} ({s.get('episode_count', 0)} eps, {self._year(s.get('air_date'))})"
            for s in d.get("seasons", [])
        ]
        if seasons:
            facts.append(f"Season list: {'; '.join(seasons)}")
        return facts

    def tv_season(self, tv_id: int, season_number: int) -> str:
        d = self._get(f"/tv/{tv_id}/season/{season_number}")
        lines = [f"{d.get('name', f'Season {season_number}')} of [tv {tv_id}] — aired {d.get('air_date') or '?'}"]
        if d.get("overview"):
            lines.append(self._truncate(d["overview"], 400))
        for ep in d.get("episodes", []):
            row = f"  E{ep.get('episode_number', 0):02d} {ep.get('name', '?')} — {ep.get('air_date') or 'TBA'}"
            if ep.get("runtime"):
                row += f", {ep['runtime']} min"
            if ep.get("vote_count"):
                row += f", ★{ep.get('vote_average', 0):.1f}"
            lines.append(row)
            overview = self._truncate(ep.get("overview"), 160)
            if overview:
                lines.append(f"      {overview}")
        return "\n".join(lines)

    def discover(
        self,
        kind: str = "movie",
        genres: str = "",
        year_from: int = 0,
        year_to: int = 0,
        min_rating: float = 0.0,
        min_votes: int = 0,
        original_language: str = "",
        providers: str = "",
        availability: str = "stream",
        region: str = "",
        sort_by: str = "popularity",
        page: int = 1,
    ) -> str:
        kind = self._kind(kind)
        region = self._region(region)
        date_field = "primary_release_date" if kind == "movie" else "first_air_date"
        sort_key = sort_by.strip().lower()
        if sort_key not in _SORTS or (sort_key == "revenue" and kind == "tv"):
            raise ValueError(
                f"Unknown sort_by '{sort_by}'. Use: popularity, rating, newest, oldest"
                + (", revenue." if kind == "movie" else ".")
            )
        params: dict = {
            "sort_by": _SORTS[sort_key].format(date=date_field),
            "page": page,
            "include_adult": "false",
        }
        if genres:
            params["with_genres"] = ",".join(
                str(i) for i in self._resolve_names(genres, self._genre_ids(kind), "genre")
            )
        if year_from:
            params[f"{date_field}.gte"] = f"{year_from}-01-01"
        if year_to:
            params[f"{date_field}.lte"] = f"{year_to}-12-31"
        elif sort_key == "newest":
            # Otherwise the top of the list is announced-but-unreleased titles.
            params[f"{date_field}.lte"] = date.today().isoformat()
        if min_rating:
            params["vote_average.gte"] = min_rating
        if min_votes or sort_key == "rating":
            params["vote_count.gte"] = min_votes or _RATING_SORT_MIN_VOTES
        if original_language:
            params["with_original_language"] = original_language.replace(",", "|")
        if providers:
            avail = availability.strip().lower()
            if avail not in _AVAILABILITY:
                raise ValueError(f"Unknown availability '{availability}'. Use: {', '.join(_AVAILABILITY)}.")
            ids = self._resolve_names(providers, self._provider_ids(kind, region), "provider")
            params["with_watch_providers"] = "|".join(str(i) for i in ids)
            params["watch_region"] = region
            if _AVAILABILITY[avail]:
                params["with_watch_monetization_types"] = _AVAILABILITY[avail]
        data = self._get(f"/discover/{kind}", params)
        heading = f"Discover {kind}" + (f" on {providers} ({region})" if providers else "")
        out = self._format_page(data, heading, kind)
        return out + (f"\n{_JUSTWATCH_CREDIT}" if providers else "")

    def trending(self, kind: str = "all", window: str = "week") -> str:
        key = kind.strip().lower()
        kind = "person" if key in ("person", "people") else self._kind(kind, allow_all=True)
        window = window.strip().lower()
        if window not in ("day", "week"):
            raise ValueError("window must be 'day' or 'week'")
        data = self._get(f"/trending/{kind}/{window}")
        return self._format_page(data, f"Trending {kind} this {window}", None if kind == "all" else kind)

    def browse_list(self, list_name: str, region: str = "", page: int = 1) -> str:
        key = list_name.strip().lower()
        if key not in _LISTS:
            raise ValueError(f"Unknown list '{list_name}'. Use one of: {', '.join(_LISTS)}.")
        path = _LISTS[key]
        params: dict = {"page": page}
        if path.startswith("/movie"):
            params["region"] = self._region(region)
        data = self._get(path, params)
        kind = path.split("/")[1]
        heading = key.replace("_", " ").capitalize()
        if "region" in params:
            heading += f" ({params['region']})"
        return self._format_page(data, heading, kind)

    def recommendations(self, tmdb_id: int, kind: str = "movie", page: int = 1) -> str:
        kind = self._kind(kind)
        data = self._get(f"/{kind}/{tmdb_id}/recommendations", {"page": page})
        return self._format_page(data, f"Recommended if you liked [{kind} {tmdb_id}]", kind)

    def where_to_watch(self, tmdb_id: int, kind: str = "movie", region: str = "") -> str:
        kind = self._kind(kind)
        results = self._get(f"/{kind}/{tmdb_id}/watch/providers").get("results", {})
        if region.strip().lower() != "all":
            region = self._region(region)
            return self._format_providers(results.get(region), region)
        streaming = {
            country: ", ".join(p["provider_name"] for p in entry.get("flatrate", []))
            for country, entry in sorted(results.items()) if entry.get("flatrate")
        }
        if not streaming:
            return f"[{kind} {tmdb_id}] is not on a subscription service anywhere TMDB tracks."
        lines = [f"Subscription streaming by country for [{kind} {tmdb_id}] {_JUSTWATCH_CREDIT}:"]
        lines.extend(f"  {country}: {names}" for country, names in streaming.items())
        return "\n".join(lines)

    def person_details(self, person_id: int) -> str:
        d = self._get(f"/person/{person_id}", {"append_to_response": "combined_credits,external_ids"})
        lines = [f"{d.get('name', '?')} [person {person_id}]"]
        if d.get("known_for_department"):
            lines.append(f"Known for: {d['known_for_department']}")
        born = d.get("birthday")
        if born:
            lines.append(
                f"Born: {born}" + (f", {d['place_of_birth']}" if d.get("place_of_birth") else "")
                + (f" — died {d['deathday']}" if d.get("deathday") else "")
            )
        imdb = d.get("external_ids", {}).get("imdb_id") or d.get("imdb_id")
        lines.append(f"IDs: tmdb person/{person_id}" + (f" · imdb {imdb}" if imdb else ""))
        if d.get("biography"):
            lines.append(f"Bio: {self._truncate(d['biography'], 600)}")
        credits = d.get("combined_credits", {})

        def _credit(c: dict, role: str) -> str:
            title = c.get("title") or c.get("name") or "?"
            when = c.get("release_date") or c.get("first_air_date")
            return f"  • {title} ({self._year(when)}) [{c.get('media_type')} {c.get('id')}] — {role}"

        cast = sorted(credits.get("cast", []), key=lambda c: c.get("vote_count", 0), reverse=True)[:12]
        if cast:
            lines.append("Best-known acting credits:")
            lines.extend(_credit(c, c.get("character") or "cast") for c in cast)
        crew = [c for c in credits.get("crew", []) if c.get("job") in ("Director", "Creator", "Screenplay", "Writer", "Producer")]
        crew = sorted(crew, key=lambda c: c.get("vote_count", 0), reverse=True)[:10]
        if crew:
            lines.append("Best-known crew credits:")
            lines.extend(_credit(c, c.get("job", "crew")) for c in crew)
        return "\n".join(lines)

    def find_by_external_id(self, external_id: str, source: str = "") -> str:
        external_id = external_id.strip()
        src = source.strip().lower()
        if not src:
            if not re.match(r"^(tt|nm)\d+$", external_id):
                raise ValueError("Only IMDb ids (tt…/nm…) are auto-detected; pass source (e.g. 'tvdb').")
            src = "imdb_id"
        if not src.endswith("_id"):
            src += "_id"
        if src not in _EXTERNAL_SOURCES:
            raise ValueError(f"Unknown source '{source}'. Use one of: {', '.join(_EXTERNAL_SOURCES)}.")
        data = self._get(f"/find/{external_id}", {"external_source": src})
        rows = []
        for key, kind in (("movie_results", "movie"), ("tv_results", "tv"), ("person_results", "person")):
            rows.extend(self._format_row(r, kind) for r in data.get(key, []))
        for ep in data.get("tv_episode_results", []):
            rows.append(
                f"  • episode '{ep.get('name')}' S{ep.get('season_number', 0):02d}E{ep.get('episode_number', 0):02d}"
                f" of [tv {ep.get('show_id')}]"
            )
        if not rows:
            return f"No TMDB match for {src} {external_id}."
        return f"{src} {external_id}:\n" + "\n".join(rows)

    def ratings_basics(self, tmdb_id: int, kind: str) -> dict:
        """Title, year, TMDB score and IMDb id — the inputs the ratings tool needs."""
        kind = self._kind(kind)
        d = self._get(f"/{kind}/{tmdb_id}", {"append_to_response": "external_ids"})
        return {
            "kind": kind,
            "title": d.get("title") or d.get("name") or "?",
            "year": self._year(d.get("release_date") or d.get("first_air_date")),
            "tmdb_score": d.get("vote_average", 0.0),
            "tmdb_votes": d.get("vote_count", 0),
            "imdb_id": d.get("external_ids", {}).get("imdb_id") or "",
        }

    def check(self) -> str:
        """Cheap authenticated call for source_status(check_live=True)."""
        self._get("/configuration")
        return "ok"
