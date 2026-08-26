"""Environment-based configuration. All secrets come from `.env` (gitignored)."""

from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


@lru_cache(maxsize=1)
def supabase_url() -> str:
    url = os.environ.get("SUPABASE_URL")
    if not url:
        raise ConfigError("SUPABASE_URL is not set. Copy .env.example to .env and fill it in.")
    return url


@lru_cache(maxsize=1)
def supabase_key() -> str:
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not key:
        raise ConfigError(
            "SUPABASE_SERVICE_ROLE_KEY is not set. Copy .env.example to .env and fill it in."
        )
    return key


def transport() -> str:
    return os.environ.get("MCP_TRANSPORT", "stdio")


def host() -> str:
    return os.environ.get("MCP_HOST", "0.0.0.0")


def port() -> int:
    return int(os.environ.get("PORT") or os.environ.get("MCP_PORT", "8000"))


def langfuse_enabled() -> bool:
    """Whether this process should initialize Langfuse tracing.

    Credentials still gate initialization, so a missing `.env` can never stop the MCP server.
    """
    return os.environ.get("LANGFUSE_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def langfuse_public_key() -> str | None:
    return os.environ.get("LANGFUSE_PUBLIC_KEY") or None


def langfuse_secret_key() -> str | None:
    return os.environ.get("LANGFUSE_SECRET_KEY") or None


def langfuse_base_url() -> str:
    return os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com").rstrip("/")


def langfuse_environment() -> str:
    return os.environ.get("LANGFUSE_TRACING_ENVIRONMENT", "development")
