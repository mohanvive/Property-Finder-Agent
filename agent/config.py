"""Externalized configuration for the PropertyFinder agent.

All endpoints and model settings come from environment variables, loaded from a
.env-style config file (default: ``agent/.env``; override with ``--config``).
Nothing is hardcoded, so the same code can point at different LLM or MCP deployments.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

AGENT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_FILE = str(AGENT_DIR / ".env")
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENAI_API_KEY_HEADER = "API-Key"


class ConfigError(Exception):
    """Raised when a required setting is missing or invalid."""


@dataclass(frozen=True)
class OpenAIConfig:
    api_key: str
    base_url: str  # OpenAI or OpenAI-compatible API URL (OPENAI_BASE_URL)
    api_key_header: str  # header carrying api_key; "Authorization" means the standard "Bearer <key>"
    model: str
    api_mode: str  # "responses" or "chat_completions"
    disable_tracing: bool


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    url: str
    env_prefix: str  # prefix used by auth.build_auth for credentials


@dataclass(frozen=True)
class ServerConfig:
    """Settings for the HTTP API (server.py)."""

    host: str
    port: int
    cors_origins: list[str]
    sessions_db: str  # SQLite file holding per-session conversation history
    log_level: str  # DEBUG, INFO, WARNING, ...
    log_messages: bool  # include (truncated) user messages in request logs


@dataclass(frozen=True)
class Settings:
    openai: OpenAIConfig
    property_search: MCPServerConfig
    insurance_quoter: MCPServerConfig
    server: ServerConfig

    @property
    def mcp_servers(self) -> list[MCPServerConfig]:
        return [self.property_search, self.insurance_quoter]


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set. Add it to your config file (see .env.example).")
    return value


def _optional(name: str) -> str | None:
    return os.getenv(name, "").strip() or None


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


def _url(name: str, default: str) -> str:
    value = (os.getenv(name, "").strip() or default).rstrip("/")
    if not value.startswith(("http://", "https://")):
        raise ConfigError(f"{name} must be an http(s) URL, got: {value!r}")
    return value


def load_settings(config_file: str | Path = DEFAULT_CONFIG_FILE) -> Settings:
    """Load settings from ``config_file`` (if present) and the process environment.

    Variables already set in the environment take precedence over the file.
    """
    path = Path(config_file)
    if path.is_file():
        load_dotenv(path, override=False)
    elif str(config_file) != DEFAULT_CONFIG_FILE:
        raise ConfigError(f"Config file not found: {path}")

    api_mode = os.getenv("OPENAI_API_MODE", "responses").strip().lower()
    if api_mode not in ("responses", "chat_completions"):
        raise ConfigError("OPENAI_API_MODE must be 'responses' or 'chat_completions'.")

    try:
        port = int(os.getenv("AGENT_PORT", "8000"))
    except ValueError as exc:
        raise ConfigError("AGENT_PORT must be an integer.") from exc
    cors = os.getenv("AGENT_CORS_ORIGINS", "http://localhost:8080,http://127.0.0.1:8080")
    log_level = (os.getenv("AGENT_LOG_LEVEL", "").strip() or "INFO").upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ConfigError("AGENT_LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL.")

    return Settings(
        openai=OpenAIConfig(
            api_key=_require("OPENAI_API_KEY"),
            base_url=_url("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL),
            api_key_header=os.getenv("OPENAI_API_KEY_HEADER", "").strip() or DEFAULT_OPENAI_API_KEY_HEADER,
            model=os.getenv("OPENAI_MODEL", "").strip() or "gpt-5-mini",
            api_mode=api_mode,
            disable_tracing=_flag("OPENAI_AGENTS_DISABLE_TRACING"),
        ),
        property_search=MCPServerConfig(
            name="property-search",
            url=_require("PROPERTY_SEARCH_MCP_URL"),
            env_prefix="PROPERTY_SEARCH",
        ),
        insurance_quoter=MCPServerConfig(
            name="insurance-quoter",
            url=_require("INSURANCE_QUOTER_MCP_URL"),
            env_prefix="INSURANCE_QUOTER",
        ),
        server=ServerConfig(
            host=os.getenv("AGENT_HOST", "").strip() or "127.0.0.1",
            port=port,
            cors_origins=[o.strip() for o in cors.split(",") if o.strip()],
            sessions_db=os.getenv("AGENT_SESSIONS_DB", "").strip() or str(AGENT_DIR / "sessions.db"),
            log_level=log_level,
            log_messages=os.getenv("AGENT_LOG_MESSAGES", "true").strip().lower() in ("1", "true", "yes"),
        ),
    )
