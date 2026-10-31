import logging

from mcp.server.fastmcp import FastMCP
from mcp_server_kit import offload_sync_tools, serve

from film_tv_mcp.config import MCP_BEARER_TOKEN, PORT
from film_tv_mcp.ratings_client import MDBListClient, OMDbClient, ratings_report
from film_tv_mcp.tmdb_client import TMDBClient

INSTRUCTIONS = """\
Movie and TV lookups: TMDB metadata, where to watch, and aggregated ratings.
Titles are addressed by (tmdb_id, kind) with kind 'movie' or 'tv'; every
result row shows them as [movie 550] / [tv 1399] — pass both straight back.
Start with search (kind='all' mixes movies, shows and people), then
title_details for cast, crew, classification, IMDb/TVDB ids, trailer and
where to watch in one call. tv_season lists episodes with air dates.
"What should I watch" questions: discover (genre/year/rating/language
filters, and providers='Netflix,Stan' for what's on a service), trending,
browse_list (now_playing, upcoming, top_rated_movies, airing_today, ...),
recommendations. ratings adds IMDb, Rotten Tomatoes, Metacritic and
Letterboxd scores when those providers are configured (source_status shows
which are). Region (where to watch, classification, cinema dates) defaults
to the server's WATCH_REGION; pass region='GB' etc. to override.
Where-to-watch data comes from JustWatch — credit it when you show it."""

mcp = FastMCP(
    "film-tv",
    instructions=INSTRUCTIONS,
    host="0.0.0.0",
    port=PORT,
    stateless_http=True,
    json_response=True,
)
# Sync tools run in worker threads (a blocking API call would otherwise stall every
# request), and each call logs a tool=/outcome= line. httpx.Client is thread-safe.
offload_sync_tools(mcp, logger_name="film_tv_mcp.requests")
tmdb = TMDBClient()
omdb = OMDbClient()
mdblist = MDBListClient()


@mcp.tool()
def search(query: str, kind: str = "all", year: int = 0, page: int = 1) -> str:
    """Find movies, TV shows or people by name. kind: all (default — mixed
    results), movie, tv, or person. year narrows movie/tv searches to a
    release (or first-air) year. 20 results per page."""
    return tmdb.search(query, kind, year, page)


@mcp.tool()
def title_details(tmdb_id: int, kind: str = "movie", region: str = "") -> str:
    """Everything about one movie or TV show in a single call: genres,
    runtime, status, classification and release dates for the region,
    director/creators, top cast, IMDb/TVDB ids, trailer, overview, and where
    to stream/rent/buy it in the region. TV adds seasons, episode counts,
    network, and the last/next episode."""
    return tmdb.title_details(tmdb_id, kind, region)


@mcp.tool()
def tv_season(tv_id: int, season_number: int) -> str:
    """Episode list for one season of a TV show: number, title, air date,
    runtime, rating and a short synopsis. Season 0 is specials."""
    return tmdb.tv_season(tv_id, season_number)


@mcp.tool()
def discover(
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
    """Find movies or TV shows by criteria rather than by name.

    kind: movie or tv.
    genres: comma-separated names, all must match ('Science Fiction,Drama';
      TV uses combined names like 'Sci-Fi & Fantasy').
    year_from / year_to: release (or first-air) year range.
    min_rating: TMDB score floor (0-10). min_votes: vote-count floor.
    original_language: ISO 639-1, comma-separated = any of ('ko' or 'ja,ko').
    providers: comma-separated services, any of ('Netflix,Stan,Disney Plus') —
      limits results to titles on them in the region.
    availability (with providers): stream (default, subscription), free, rent,
      buy, or any.
    sort_by: popularity (default), rating (applies a 200-vote floor unless
      min_votes is set), newest, oldest, revenue (movies only)."""
    return tmdb.discover(
        kind, genres, year_from, year_to, min_rating, min_votes,
        original_language, providers, availability, region, sort_by, page,
    )


@mcp.tool()
def trending(kind: str = "all", window: str = "week") -> str:
    """What's trending on TMDB. kind: all, movie, tv or person.
    window: day or week (default)."""
    return tmdb.trending(kind, window)


@mcp.tool()
def browse_list(list_name: str, region: str = "", page: int = 1) -> str:
    """TMDB's curated lists. list_name: now_playing (in cinemas), upcoming
    (cinema releases), popular_movies, top_rated_movies, airing_today,
    on_the_air (next 7 days), popular_tv, top_rated_tv. The movie lists are
    region-specific."""
    return tmdb.browse_list(list_name, region, page)


@mcp.tool()
def recommendations(tmdb_id: int, kind: str = "movie", page: int = 1) -> str:
    """Titles TMDB recommends to people who liked this movie or show."""
    return tmdb.recommendations(tmdb_id, kind, page)


@mcp.tool()
def where_to_watch(tmdb_id: int, kind: str = "movie", region: str = "") -> str:
    """Where a movie or show can be streamed, watched free, rented or bought
    in a region (default: the server's WATCH_REGION). region='all' lists
    every country with a subscription-streaming option instead."""
    return tmdb.where_to_watch(tmdb_id, kind, region)


@mcp.tool()
def person_details(person_id: int) -> str:
    """One actor, director or writer: bio, birth/death, IMDb id, and their
    best-known acting and crew credits (each with its [movie N]/[tv N] id)."""
    return tmdb.person_details(person_id)


@mcp.tool()
def find_by_external_id(external_id: str, source: str = "") -> str:
    """Map another database's id to TMDB. IMDb ids (tt…, nm…) are detected
    automatically; otherwise pass source: tvdb, wikidata, facebook,
    instagram, tiktok, twitter or youtube. Handy for ids from Sonarr,
    Radarr, Plex or an IMDb link."""
    return tmdb.find_by_external_id(external_id, source)


@mcp.tool()
def ratings(tmdb_id: int, kind: str = "movie") -> str:
    """Critic and audience scores from every configured source: TMDB always;
    IMDb, Rotten Tomatoes (critics + audience), Metacritic, Letterboxd,
    Trakt and more when OMDb and/or MDBList keys are set."""
    return ratings_report(tmdb, omdb, mdblist, tmdb_id, kind)


@mcp.tool()
def source_status(check_live: bool = False) -> str:
    """Which data providers are configured, the default region and language.
    check_live=True also makes one cheap call to each configured provider
    (and reports MDBList's daily request quota)."""
    lines = [f"Region: {tmdb.region} · language: {tmdb.language}"]
    for name, client in (("TMDB", tmdb), ("OMDb", omdb), ("MDBList", mdblist)):
        if not client.configured:
            lines.append(f"  {name}: not configured")
            continue
        state = "configured"
        if check_live:
            try:
                state = client.check()
            except (RuntimeError, ValueError) as e:
                state = f"ERROR — {e}"
        lines.append(f"  {name}: {state}")
    return "\n".join(lines)


def main():
    logging.basicConfig(level=logging.INFO)
    serve(mcp, port=PORT, bearer_token=MCP_BEARER_TOKEN)


if __name__ == "__main__":
    main()
