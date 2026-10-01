"""PropertyFinder core: an OpenAI-powered agent for property search and insurance quotes.

The agent connects to two MCP servers over streamable HTTP:
  * property-search   - finds properties (rentals, sales, business spaces)
  * insurance-quoter  - premium estimates, add-ons and state comparisons

All endpoints (OpenAI and both MCP servers) are read from the config file; see config.py.
Front ends: cli.py (terminal chat) and server.py (HTTP API).
"""

from __future__ import annotations

import sys

import httpx
from contextlib import AsyncExitStack

from openai import AsyncOpenAI, DefaultAsyncHttpxClient

from agents import (
    Agent,
    set_default_openai_api,
    set_default_openai_client,
    set_tracing_disabled,
)
from agents.mcp import MCPServer, MCPServerStreamableHttp
from auth import build_auth
from config import MCPServerConfig, OpenAIConfig

INSTRUCTIONS = """\
You are PropertyFinder, a helpful real-estate assistant for US properties.

You can:
1. Search for properties (rentals, homes for sale, and business/commercial spaces)
   using the property-search tools.
2. Estimate property insurance premiums, coverage tiers, optional add-ons, and
   compare insurance costs across states using the insurance-quoter tools.

Guidelines:
- Always use the tools for property listings and premium figures; never invent
  listings, prices, or quotes.
- When a property came from a search result and has an ID, prefer
  getInsuranceQuoteByPropertyId. Use getInsuranceQuoteByDetails for properties
  not in the database or hypothetical scenarios.
- Insurance categories are RESIDENTIAL_RENTAL, RESIDENTIAL_SALE, or BUSINESS, and
  states are 2-letter abbreviations (e.g. CA, TX).
- If key details are missing (location, budget, property type), make a
  reasonable search first and then ask a brief follow-up question if needed.
- Present results concisely: use short tables or bullet lists with price, beds/
  baths or size, location, and property ID. For quotes, show each tier's annual
  and monthly premium, deductible, and the risk level with its main factors.
- Mention that insurance figures are estimates, not binding offers.
"""


def describe_error(exc: BaseException) -> str:
    """Unwrap ExceptionGroups (raised by the MCP client's task group) to the root cause."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return f"{type(exc).__name__}: {exc}"


async def _drop_authorization_header(request: httpx.Request) -> None:
    request.headers.pop("Authorization", None)


def build_openai_client(cfg: OpenAIConfig) -> AsyncOpenAI:
    """Create the OpenAI client, sending the API key in the configured header.

    With OPENAI_API_KEY_HEADER=Authorization this is the standard client ("Authorization: Bearer <key>").
    Otherwise the key goes in that header (e.g. "API-Key: <key>") and no Authorization header is sent.
    The SDK always adds "Authorization: Bearer <api_key>" itself, so it's removed just before sending.
    """
    if cfg.api_key_header.lower() == "authorization":
        return AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.base_url)
    return AsyncOpenAI(
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        default_headers={cfg.api_key_header: cfg.api_key},
        http_client=DefaultAsyncHttpxClient(event_hooks={"request": [_drop_authorization_header]}),
    )


def configure_openai(cfg: OpenAIConfig) -> None:
    """Point the Agents SDK at the configured OpenAI (or OpenAI-compatible) endpoint."""
    print(f"LLM endpoint: {cfg.base_url} (model: {cfg.model}, API: {cfg.api_mode}, "
          f"key header: {cfg.api_key_header})", flush=True)
    set_default_openai_client(build_openai_client(cfg))
    set_default_openai_api(cfg.api_mode)
    # Traces are uploaded to OpenAI, which fails for other endpoints unless tracing is off.
    if cfg.disable_tracing:
        set_tracing_disabled(True)


def make_server(cfg: MCPServerConfig) -> MCPServerStreamableHttp:
    headers, auth = build_auth(cfg.env_prefix)
    params = {"url": cfg.url, "headers": headers, "timeout": 30, "sse_read_timeout": 300}
    if auth is not None:
        params["auth"] = auth
    return MCPServerStreamableHttp(
        params=params,
        name=cfg.name,
        cache_tools_list=True,
        client_session_timeout_seconds=60,
        max_retry_attempts=2,
    )


async def connect_servers(stack: AsyncExitStack, configs: list[MCPServerConfig]) -> list[MCPServer]:
    """Connect to each MCP server, skipping (with a warning) any that fail."""
    connected: list[MCPServer] = []
    for cfg in configs:
        server = make_server(cfg)
        prefix = cfg.env_prefix
        try:
            await stack.enter_async_context(server)
            tools = await server.list_tools()
            print(f"  ✓ {server.name}: {', '.join(t.name for t in tools)}")
            connected.append(server)
        except Exception as exc:  # noqa: BLE001 - report and keep going with the other server
            reason = describe_error(exc)
            print(f"  ✗ {server.name}: could not connect to {cfg.url} ({reason})", file=sys.stderr)
            headers, auth = build_auth(prefix)
            if auth is None and "Authorization" not in headers:
                print(
                    f"    No credentials configured. The endpoint may require a Choreo token: set "
                    f"{prefix}_CLIENT_ID/{prefix}_CLIENT_SECRET or {prefix}_ACCESS_TOKEN in the config file.",
                    file=sys.stderr,
                )
    return connected


def build_agent(servers: list[MCPServer], model: str) -> Agent:
    return Agent(
        name="PropertyFinder",
        instructions=INSTRUCTIONS,
        model=model,
        mcp_servers=servers,
    )
