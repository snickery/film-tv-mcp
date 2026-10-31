# film-tv-mcp

An [MCP](https://modelcontextprotocol.io) server for movie and TV lookups:
search, full title and person detail, episode lists, discovery by genre,
year, language or streaming service, where to watch in any country, and
critic/audience scores from IMDb, Rotten Tomatoes, Metacritic, Letterboxd
and more. Twelve read-only tools, built on the
[Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk)
(FastMCP). Runs as a local stdio server or as a containerized Streamable
HTTP service with bearer auth.

Providers:

| Provider | Required | Gives you |
|---|---|---|
| [TMDB](https://www.themoviedb.org/) | yes (free key) | Everything except third-party scores. Where-to-watch data is from [JustWatch](https://www.justwatch.com/). |
| [OMDb](https://www.omdbapi.com/) | optional | IMDb rating + votes, Rotten Tomatoes Tomatometer, Metascore. |
| [MDBList](https://mdblist.com/) | optional | The above plus RT audience score, Metacritic users, Letterboxd, Trakt, MyAnimeList, RogerEbert.com. |

Rotten Tomatoes has no public API (the official one is a paid partner
licence), so its scores come through OMDb or MDBList.

## Tool Reference

Titles are addressed by `tmdb_id` + `kind` (`movie` or `tv`). Every result
row prints them as `[movie 550]` / `[tv 1399]`, so a model can pass them
straight back.

| Tool | Parameters | Description |
|---|---|---|
| `search` | `query`, `kind = "all"`, `year = 0`, `page = 1` | Find titles or people by name. `kind`: `all` (mixed), `movie`, `tv`, `person`. |
| `title_details` | `tmdb_id`, `kind = "movie"`, `region = ""` | One call for genres, runtime, status, classification + release dates for the region, director/creators, cast, IMDb/TVDB ids, trailer, overview and where to watch. TV adds seasons, network, last/next episode. |
| `tv_season` | `tv_id`, `season_number` | Episodes: number, title, air date, runtime, rating, synopsis. Season 0 = specials. |
| `discover` | `kind = "movie"`, `genres = ""`, `year_from = 0`, `year_to = 0`, `min_rating = 0`, `min_votes = 0`, `original_language = ""`, `providers = ""`, `availability = "stream"`, `region = ""`, `sort_by = "popularity"`, `page = 1` | Find titles by criteria. Genres and providers are names (`"Science Fiction"`, `"Netflix,Stan"`), not ids. `availability`: `stream`, `free`, `rent`, `buy`, `any`. `sort_by`: `popularity`, `rating` (200-vote floor unless `min_votes` is set), `newest` (released only), `oldest`, `revenue`. |
| `trending` | `kind = "all"`, `window = "week"` | Trending titles or people, `day` or `week`. |
| `browse_list` | `list_name`, `region = ""`, `page = 1` | `now_playing`, `upcoming`, `popular_movies`, `top_rated_movies`, `airing_today`, `on_the_air`, `popular_tv`, `top_rated_tv`. Movie lists are regional. |
| `recommendations` | `tmdb_id`, `kind = "movie"`, `page = 1` | "If you liked this." |
| `where_to_watch` | `tmdb_id`, `kind = "movie"`, `region = ""` | Stream / free / rent / buy services in a region; `region="all"` lists subscription streaming in every country. |
| `person_details` | `person_id` | Bio, birth/death, IMDb id, best-known acting and crew credits. |
| `find_by_external_id` | `external_id`, `source = ""` | IMDb (`tt…`/`nm…`, auto-detected), TVDB, Wikidata and social ids → TMDB. |
| `ratings` | `tmdb_id`, `kind = "movie"` | Scores from every configured provider, merged (MDBList preferred, OMDb fills gaps). A failing provider becomes a note, not an error. |
| `source_status` | `check_live = False` | Configured providers, region, language; `check_live` makes one cheap call to each (and shows MDBList's daily quota). |

```python
search(query="severance")                          # → [tv 95396]
title_details(tmdb_id=95396, kind="tv")            # cast, seasons, next episode, where to watch
discover(kind="tv", genres="Comedy", providers="Stan,Binge", sort_by="rating")
where_to_watch(tmdb_id=550, region="all")          # which countries stream it
ratings(tmdb_id=550)                               # IMDb, RT critics + audience, Metacritic, Letterboxd
```

## Setup

1. **TMDB** (required): create a free account, then Settings → API →
   request an API key. Use the **API Read Access Token** (v4, a long JWT)
   as `TMDB_API_TOKEN`; the short v3 **API Key** also works as
   `TMDB_API_KEY`.
2. **OMDb** (optional): request a key at omdbapi.com (free tier: 1,000
   requests/day) and set `OMDB_API_KEY`.
3. **MDBList** (optional): sign in at mdblist.com, copy the API key from
   Preferences and set `MDBLIST_API_KEY`. The free tier has a daily
   request cap; `source_status(check_live=True)` shows usage.
4. Set `WATCH_REGION` (e.g. `AU`) and `TMDB_LANGUAGE` (e.g. `en-AU`) for your
   country.

The server starts without any key: TMDB tools return a setup error, and
`ratings` falls back to TMDB's own score until OMDb or MDBList is set.

## Quick start (stdio)

Most MCP clients (Claude Code, Claude Desktop, VS Code, …) spawn stdio
servers directly. With [uv](https://docs.astral.sh/uv/) installed:

```jsonc
// e.g. Claude Desktop claude_desktop_config.json / Claude Code .mcp.json
{
  "mcpServers": {
    "film-tv": {
      "command": "uvx",
      "args": ["film-tv-mcp", "--stdio"],
      "env": {
        "TMDB_API_TOKEN": "your-read-access-token",
        "WATCH_REGION": "AU",
        "OMDB_API_KEY": "optional",
        "MDBLIST_API_KEY": "optional"
      }
    }
  }
}
```

stdio mode has no network surface and skips bearer auth — the client owns
the process.

## HTTP mode (container)

The bundled `Containerfile` builds a Streamable HTTP server at `/mcp`
(stateless — restarts never strand client sessions). HTTP mode **refuses
to start** without `MCP_BEARER_TOKEN`; clients authenticate with
`Authorization: Bearer <token>`.

```bash
podman build -t film-tv-mcp .    # or: docker build -t film-tv-mcp .
podman run -d --name film-tv-mcp -p 8337:8337 \
  -e TMDB_API_TOKEN=... -e WATCH_REGION=AU -e MCP_BEARER_TOKEN=some-long-random-token \
  film-tv-mcp
```

`film_tv_mcp.healthcheck` does a full HTTP round-trip to `/mcp` (the 401
counts as alive — it proves the event loop responds); wire it to your
container healthcheck with a restart-on-unhealthy policy. Terminate TLS
at a reverse proxy — the server itself speaks plain HTTP.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `TMDB_API_TOKEN` | *(empty)* | TMDB v4 read access token (bearer). Preferred. |
| `TMDB_API_KEY` | *(empty)* | TMDB v3 API key, used only when no token is set. |
| `OMDB_API_KEY` | *(empty)* | Optional OMDb key for the `ratings` tool. |
| `MDBLIST_API_KEY` | *(empty)* | Optional MDBList key for the `ratings` tool. |
| `WATCH_REGION` | `US` | ISO 3166-1 country for where-to-watch, classification and release dates. Every tool with a `region` parameter can override it. |
| `TMDB_LANGUAGE` | `en-US` | Language for titles and overviews (not `LANGUAGE`, which shells set for the locale). |
| `PORT` | `8337` | HTTP listen port. |
| `MCP_BEARER_TOKEN` | *(empty)* | Required in HTTP mode; server refuses to start without it. Not used in `--stdio` mode. |

## Architecture notes

- One thread-safe `httpx.Client` per provider. TMDB detail calls fold
  credits, external ids, videos, certifications and watch providers into
  one request with `append_to_response`.
- Genre and provider name→id tables are fetched once per process and
  cached. Name matching is case-insensitive; an exact name wins, otherwise
  every name containing the term matches (`netflix` also matches
  "Netflix Standard with Ads").
- Every tool is read-only: nothing writes to TMDB, OMDb or MDBList.
- Sync tool handlers are offloaded to a worker thread (the MCP SDK runs
  sync tools inline on the event loop, so a slow upstream call would
  otherwise stall every concurrent request). Per-request log lines
  (`tool= outcome= duration_ms= rss_mib=`) go to stderr.
- API-key values are redacted from error messages before they can reach
  logs or clients.

## Testing

```bash
# Tiers 1 + 2 — pure helpers + mocked HTTP (fast, no network, no keys)
uv run --extra test pytest tests/test_tmdb_client.py tests/test_ratings_client.py -v

# Tier 3 — live, read-only round-trips. Each provider's tests skip
# without its key.
TMDB_API_TOKEN=... OMDB_API_KEY=... MDBLIST_API_KEY=... \
  uv run --extra test pytest tests/test_integration.py -v
```

## Attribution

This product uses the TMDB API but is not endorsed or certified by TMDB.
Where-to-watch data is provided by JustWatch via TMDB.

## License

[MIT](LICENSE)
