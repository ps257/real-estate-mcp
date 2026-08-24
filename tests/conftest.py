"""Shared test fixtures.

Two kinds of tests live here:
  - NO-DB tests (test_shaping, test_server_tools): run instantly, no Supabase needed. Start here.
  - LIVE-DB tests (test_live_db): skipped automatically unless SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY
    are set in the environment/.env. These prove the tools actually query the real database.

Real sample ids below were pulled from the live DB on 2026-07-31. If a test fails because an id is
gone, re-introspect (Supabase MCP) and update these.
"""

from __future__ import annotations

try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

import os

import pytest
from dotenv import load_dotenv

# Load .env before the skip marker below reads the environment — otherwise credentials that live
# only in .env are invisible here and every live-DB test silently skips.
load_dotenv()


def _has_db_creds() -> bool:
    return bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_ROLE_KEY"))


def tool_data(result):
    """Unwrap a tool's return value from a FastMCP ToolResult.

    FastMCP 3.x exposes it on `.structured_content` (not `.data`), and wraps non-object returns
    (lists, strings) in a `{"result": ...}` envelope per the MCP spec. Dict returns come through
    as-is. This helper hides that difference from the tests.
    """
    content = result.structured_content
    if isinstance(content, dict) and set(content) == {"result"}:
        return content["result"]
    return content

# Skip marker applied to every live-DB test. Usage: @needs_db above the test.
needs_db = pytest.mark.skipif(
    not _has_db_creds(),
    reason="Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (e.g. in .env) to run live-DB tests.",
)


# --- Real sample ids for live-DB tests (verified 2026-07-31) ---
@pytest.fixture(scope="session")
def sample_project_id() -> str:
    """A project that definitely has many listings."""
    return "vhm:vinhomes-ocean-park"  # 524 listings, Hà Nội


@pytest.fixture(scope="session")
def sample_project_name() -> str:
    return "Vinhomes"  # matches several Vinhomes projects


@pytest.fixture(scope="session")
def sample_listing_ids() -> list[str]:
    """Real listing ids in the sample project (for get_listing / compare_listings)."""
    return ["oh:TOFMRB", "oh:JWJ33B", "oh:FUAPLB"]


@pytest.fixture(scope="session")
def mcp_server():
    """The shared FastMCP server instance."""
    from app.server import mcp

    return mcp
