import os

# TMDB is the core provider and the only required one. Either credential works:
# the v4 "API Read Access Token" (sent as a bearer header, preferred) or the
# v3 "API Key" (sent as a query param). Both come from themoviedb.org →
# Settings → API. The server starts without either — every tool call then
# returns a setup error instead, so deployment can precede the key.
TMDB_API_TOKEN = os.environ.get("TMDB_API_TOKEN", "")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")

# ISO 3166-1 country for where-to-watch, certifications and release dates
# (e.g. "AU"), and the language for titles/overviews (e.g. "en-AU").
WATCH_REGION = os.environ.get("WATCH_REGION", "US").upper()
LANGUAGE = os.environ.get("TMDB_LANGUAGE", "en-US")

# Optional ratings providers, used by the ratings tool when set.
# OMDb: IMDb rating + votes, Rotten Tomatoes Tomatometer, Metascore.
# MDBList: adds RT audience (Popcornmeter), Letterboxd, Trakt, MyAnimeList.
OMDB_API_KEY = os.environ.get("OMDB_API_KEY", "")
MDBLIST_API_KEY = os.environ.get("MDBLIST_API_KEY", "")

PORT = int(os.environ.get("PORT", "8337"))
MCP_BEARER_TOKEN = os.environ.get("MCP_BEARER_TOKEN", "")
