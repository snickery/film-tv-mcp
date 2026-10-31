"""Shared fixtures for film-tv-mcp tests."""

import os
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def tmdb():
    """A TMDBClient with a fake token, region AU, and _get mocked.

    Tests assert on the mock calls (path / params) and inject canned API
    responses via .return_value or .side_effect.
    """
    from film_tv_mcp.tmdb_client import TMDBClient

    c = TMDBClient(token="test-token", api_key="", region="AU", language="en-AU")
    c._get = MagicMock(name="_get")
    return c


@pytest.fixture
def unconfigured_tmdb():
    """A TMDBClient with NO credentials — for asserting the setup error."""
    from film_tv_mcp.tmdb_client import TMDBClient

    return TMDBClient(token="", api_key="")


requires_tmdb = pytest.mark.skipif(
    not (os.environ.get("TMDB_API_TOKEN") or os.environ.get("TMDB_API_KEY")),
    reason="Set TMDB_API_TOKEN (or TMDB_API_KEY) to run live TMDB integration tests",
)
requires_omdb = pytest.mark.skipif(
    not os.environ.get("OMDB_API_KEY"), reason="Set OMDB_API_KEY to run live OMDb tests",
)
requires_mdblist = pytest.mark.skipif(
    not os.environ.get("MDBLIST_API_KEY"), reason="Set MDBLIST_API_KEY to run live MDBList tests",
)
