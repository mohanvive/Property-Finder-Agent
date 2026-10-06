"""PropertyFinder core: an OpenAI-powered agent for property search and insurance quotes.

The agent connects to two MCP servers over streamable HTTP:
  * property-search   - finds properties (rentals, sales, business spaces)
  * insurance-quoter  - premium estimates, add-ons and state comparisons

All endpoints (OpenAI and both MCP servers) are read from the config file; see config.py.
Front ends: cli.py (terminal chat) and server.py (HTTP API).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field

import httpx

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

log = logging.getLogger("property-finder.mcp")

# The Choreo MCP servers sometimes reject a session right after creating it ("Invalid session ID",
# "Session terminated"), e.g. when a request lands on a replica that didn't create the session.
# Each connection is therefore retried, and sessions are opened per request instead of being kept
# open for the life of the process (a long-lived session breaks as soon as the server drops it).
MCP_CONNECT_ATTEMPTS = 3
MCP_RETRY_BACKOFF_SECONDS = 0.5

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
- Whenever the user asks about insurance, premiums, coverage, risk, or the cost of
  insuring a property, call the insurance-quoter tools. Never estimate premiums
  yourself or reuse figures from memory.
- For properties from a search result (they have a property ID), call
  getInsuranceQuoteByPropertyId for each property the user asks about. Use
  getInsuranceQuoteByDetails for properties not in the database or hypothetical
  scenarios, getInsuranceAddOns for optional coverage, and compareInsuranceByState
  to compare locations.
- If the user asks for properties and insurance together, search first, then quote
  each property found (up to 5), and present listing and premium side by side.
- After listing properties without quotes, offer to get insurance quotes for them.
- If an insurance tool call fails, retry it once; if it still fails, tell the user
  the quote couldn't be retrieved right now. Never make up a quote.
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


async def _cleanup_quietly(server: MCPServer) -> None:
    try:
        await server.cleanup()
    except Exception as exc:  # noqa: BLE001 - a failed close must not break the request
        log.debug("Ignoring error closing MCP server %s: %s", server.name, describe_error(exc))


async def connect_with_retry(cfg: MCPServerConfig) -> tuple[MCPServerStreamableHttp, list[str]]:
    """Connect to one MCP server and list its tools, retrying rejected sessions."""
    last_error: Exception | None = None
    for attempt in range(1, MCP_CONNECT_ATTEMPTS + 1):
        server = make_server(cfg)
        try:
            await server.connect()
            tools = await server.list_tools()
            if attempt > 1:
                log.info("Connected to MCP server %s on attempt %d", cfg.name, attempt)
            return server, [t.name for t in tools]
        except Exception as exc:  # noqa: BLE001 - retried below, then reported to the caller
            last_error = exc
            await _cleanup_quietly(server)
            log.warning("MCP server %s: connection attempt %d/%d failed (%s)",
                        cfg.name, attempt, MCP_CONNECT_ATTEMPTS, describe_error(exc))
            if attempt < MCP_CONNECT_ATTEMPTS:
                await asyncio.sleep(MCP_RETRY_BACKOFF_SECONDS * attempt)
    assert last_error is not None
    raise last_error


@dataclass
class MCPConnections:
    servers: list[MCPServer] = field(default_factory=list)
    tools: dict[str, list[str]] = field(default_factory=dict)  # server name -> tool names
    unavailable: dict[str, str] = field(default_factory=dict)  # server name -> error


@asynccontextmanager
async def open_mcp_servers(configs: list[MCPServerConfig]) -> AsyncIterator[MCPConnections]:
    """Connect to every MCP server (with retries) for the duration of one request.

    Servers that still fail are listed in ``unavailable`` instead of aborting, so the agent can
    work with the rest and tell the user what's missing. Connections are opened and closed in
    the same task, as the MCP client requires.
    """
    connections = MCPConnections()
    async with AsyncExitStack() as stack:
        for cfg in configs:
            try:
                server, tools = await connect_with_retry(cfg)
            except Exception as exc:  # noqa: BLE001 - recorded and reported, not fatal
                connections.unavailable[cfg.name] = describe_error(exc)
                log.error("MCP server %s unavailable after %d attempts: %s",
                          cfg.name, MCP_CONNECT_ATTEMPTS, connections.unavailable[cfg.name])
                continue
            stack.push_async_callback(_cleanup_quietly, server)
            connections.servers.append(server)
            connections.tools[cfg.name] = tools
        yield connections


def print_connection_report(connections: MCPConnections, configs: list[MCPServerConfig]) -> None:
    """Print a startup summary of which MCP servers are reachable."""
    for cfg in configs:
        if cfg.name in connections.tools:
            print(f"  ✓ {cfg.name}: {', '.join(connections.tools[cfg.name])}", flush=True)
            continue
        print(f"  ✗ {cfg.name}: could not connect to {cfg.url} ({connections.unavailable.get(cfg.name)})",
              file=sys.stderr, flush=True)
        headers, auth = build_auth(cfg.env_prefix)
        if auth is None and "Authorization" not in headers:
            prefix = cfg.env_prefix
            print(
                f"    No credentials configured. The endpoint may require a Choreo token: set "
                f"{prefix}_CLIENT_ID/{prefix}_CLIENT_SECRET or {prefix}_ACCESS_TOKEN in the config file.",
                file=sys.stderr, flush=True,
            )


def build_agent(connections: MCPConnections, model: str) -> Agent:
    instructions = INSTRUCTIONS
    if connections.unavailable:
        names = ", ".join(sorted(connections.unavailable))
        instructions += (
            f"\nService status: the {names} service is temporarily unavailable, so its tools are "
            "missing. If the user needs it (e.g. insurance quotes), say it can't be reached right "
            "now and suggest trying again shortly. Do not guess its results.\n"
        )
    return Agent(
        name="PropertyFinder",
        instructions=instructions,
        model=model,
        mcp_servers=connections.servers,
    )
