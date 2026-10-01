"""HTTP API for the PropertyFinder agent.

Endpoints
  GET    /health                  -> status, model, and connected MCP servers/tools
  POST   /chat                    -> {"message", "session_id"?} -> {"reply", "session_id"}
  POST   /chat/stream             -> same body; Server-Sent Events stream (see below)
  GET    /sessions/{id}/messages  -> conversation history for a session
  DELETE /sessions/{id}           -> clear a session's history

Stream events (each "data:" line is JSON with a "type" field):
  session    {"session_id"}               first event, echoes/assigns the session id
  tool_call  {"name", "arguments"}        the agent invoked an MCP tool
  tool_done  {"name"}                     the tool returned
  delta      {"text"}                     a chunk of the reply text
  done       {"reply"}                    the full final reply
  error      {"message"}                  the run failed

Run:  python server.py [--config agent/.env] [--host H] [--port P]
      (host/port come from AGENT_HOST/AGENT_PORT in the config file)
  or: uvicorn server:create_app --factory --port 8000   (from agent/; config from AGENT_CONFIG or agent/.env)
"""

from __future__ import annotations

import argparse
import contextvars
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from openai.types.responses import ResponseTextDeltaEvent
from pydantic import BaseModel, Field

from agents import Agent, Runner, RunResult, SQLiteSession
from agents.items import ToolCallItem
from agents.mcp import MCPServer
from config import AGENT_DIR, DEFAULT_CONFIG_FILE, ConfigError, Settings, load_settings
from property_agent import build_agent, configure_openai, connect_servers, describe_error

log = logging.getLogger("property-finder.server")

MAX_TURNS = 15
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MESSAGE_PREVIEW_CHARS = 200

# Request ID of the request being handled, attached to every log line it produces.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def setup_logging(level: str) -> None:
    """Send property-finder logs to stderr with timestamps and request IDs.

    Uses its own handler (not the root logger) so it works under uvicorn, `uvicorn --factory`,
    and tracing wrappers such as amp-instrument that may reconfigure root logging.
    """
    app_log = logging.getLogger("property-finder")
    app_log.setLevel(level)
    if not app_log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-5s [%(request_id)s] %(name)s: %(message)s")
        )
        handler.addFilter(RequestIdFilter())
        app_log.addHandler(handler)
        app_log.propagate = False


def _db_writable(path: Path) -> str | None:
    """Return None if SQLite can create/write ``path`` (and its journal), else the error."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            conn.execute("BEGIN IMMEDIATE")  # takes a write lock: creates the file, needs a writable dir
            conn.rollback()
        finally:
            conn.close()
        return None
    except (OSError, sqlite3.Error) as exc:
        return f"{type(exc).__name__}: {exc}"


def resolve_sessions_db(configured: str) -> str:
    """Use the configured session DB if writable; otherwise fall back to the temp directory.

    Containers often run with a read-only app directory, where the default agent/sessions.db
    cannot be created. Failing over here keeps chat working instead of every request erroring.
    """
    path = Path(configured).expanduser()
    error = _db_writable(path)
    if error is None:
        log.info("Conversation history database: %s", path)
        return str(path)

    fallback = Path(tempfile.gettempdir()) / "property-finder-sessions.db"
    fallback_error = _db_writable(fallback)
    if fallback_error is None:
        log.warning(
            "Cannot write conversation history database %s (%s). Using %s instead; history there is "
            "lost on restart and not shared between replicas. Set AGENT_SESSIONS_DB to a writable, "
            "persistent path (e.g. a mounted volume) to fix this.", path, error, fallback,
        )
        return str(fallback)
    raise RuntimeError(
        f"No writable location for the conversation history database: {path} ({error}); "
        f"{fallback} ({fallback_error}). Set AGENT_SESSIONS_DB to a writable path."
    )


def preview(text: str, enabled: bool) -> str:
    if not enabled:
        return f"<{len(text)} chars>"
    flat = " ".join(text.split())
    return json.dumps(flat if len(flat) <= MESSAGE_PREVIEW_CHARS else flat[:MESSAGE_PREVIEW_CHARS] + "…")


def tool_calls_in(result: RunResult) -> list[str]:
    return [
        getattr(item.raw_item, "name", None) or "tool"
        for item in result.new_items
        if isinstance(item, ToolCallItem)
    ]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    session_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    session_id: str


def resolve_session_id(session_id: str | None) -> str:
    if session_id is None:
        return uuid.uuid4().hex
    if not SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="session_id must match [A-Za-z0-9_-]{1,64}")
    return session_id


def sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event)}\n\n"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API. Settings default to the file named by AGENT_CONFIG (or agent/.env)."""
    if settings is None:
        try:
            settings = load_settings(os.getenv("AGENT_CONFIG", DEFAULT_CONFIG_FILE))
        except ConfigError as exc:
            sys.exit(f"Configuration error: {exc}")
    cfg = settings
    setup_logging(cfg.server.log_level)
    sessions_db = resolve_sessions_db(cfg.server.sessions_db)
    agent: Agent | None = None
    servers: list[MCPServer] = []

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal agent, servers
        configure_openai(cfg.openai)
        async with AsyncExitStack() as stack:
            print("Connecting to MCP servers...")
            servers = await connect_servers(stack, cfg.mcp_servers)
            if not servers:
                raise RuntimeError("No MCP servers available; cannot start.")
            agent = build_agent(servers, cfg.openai.model)
            yield

    app = FastAPI(title="PropertyFinder Agent API", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.server.cors_origins,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        # x-api-key is sent by the web UI for the gateway in front of the agent; the agent ignores it.
        allow_headers=["Content-Type", "X-Request-ID", "x-api-key"],
        expose_headers=["X-Request-ID"],
    )

    @app.middleware("http")
    async def log_requests(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """Log every HTTP request with a request ID (echoed back in the X-Request-ID header)."""
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        token = request_id_var.set(request_id)
        client = request.client.host if request.client else "-"
        started = time.perf_counter()
        log.info("Request received: %s %s from %s", request.method, request.url.path, client)
        try:
            response = await call_next(request)
            # For event streams this marks the stream opening; the chat handler logs its completion.
            streaming = response.headers.get("content-type", "").startswith("text/event-stream")
            log.info("%s: %s %s -> %d in %.0f ms", "Stream opened" if streaming else "Response sent",
                     request.method, request.url.path, response.status_code,
                     (time.perf_counter() - started) * 1000)
        except Exception:
            log.exception("Request failed: %s %s", request.method, request.url.path)
            raise
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = request_id
        return response

    def open_session(session_id: str) -> SQLiteSession:
        return SQLiteSession(session_id, db_path=sessions_db)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        mcp = []
        for server in servers:
            tools = await server.list_tools()
            mcp.append({"name": server.name, "tools": [t.name for t in tools]})
        return {
            "status": "ok",
            "model": cfg.openai.model,
            "llm_endpoint": cfg.openai.base_url,
            "mcp_servers": mcp,
        }

    @app.post("/chat", response_model=ChatResponse)
    async def chat(body: ChatRequest) -> ChatResponse:
        session_id = resolve_session_id(body.session_id)
        log.info("Chat request: session=%s%s message=%s", session_id,
                 "" if body.session_id else " (new)", preview(body.message, cfg.server.log_messages))
        started = time.perf_counter()
        session = open_session(session_id)
        try:
            result = await Runner.run(agent, body.message, session=session, max_turns=MAX_TURNS)
        except Exception as exc:  # noqa: BLE001 - surface agent failures as HTTP errors
            log.exception("Chat failed: session=%s after %.1f s", session_id, time.perf_counter() - started)
            raise HTTPException(status_code=502, detail=describe_error(exc)) from exc
        finally:
            session.close()
        reply = str(result.final_output)
        log.info("Chat completed: session=%s tools=%s reply=%d chars in %.1f s", session_id,
                 tool_calls_in(result) or "none", len(reply), time.perf_counter() - started)
        return ChatResponse(reply=reply, session_id=session_id)

    @app.post("/chat/stream")
    async def chat_stream(body: ChatRequest) -> StreamingResponse:
        session_id = resolve_session_id(body.session_id)
        request_id = request_id_var.get()
        log.info("Chat stream request: session=%s%s message=%s", session_id,
                 "" if body.session_id else " (new)", preview(body.message, cfg.server.log_messages))

        async def events() -> AsyncIterator[str]:
            # The body streams after the middleware returns, so restore the request ID here.
            request_id_var.set(request_id)
            started = time.perf_counter()
            yield sse({"type": "session", "session_id": session_id})
            session = open_session(session_id)
            tool_names: dict[str, str] = {}
            completed = False
            try:
                result = Runner.run_streamed(agent, body.message, session=session, max_turns=MAX_TURNS)
                async for event in result.stream_events():
                    if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
                        yield sse({"type": "delta", "text": event.data.delta})
                    elif event.type == "run_item_stream_event":
                        raw = getattr(event.item, "raw_item", None)
                        if event.name == "tool_called":
                            name = getattr(raw, "name", None) or "tool"
                            if call_id := getattr(raw, "call_id", None):
                                tool_names[call_id] = name
                            log.info("Tool call: session=%s tool=%s", session_id, name)
                            log.debug("Tool arguments: %s", getattr(raw, "arguments", ""))
                            yield sse({"type": "tool_call", "name": name, "arguments": getattr(raw, "arguments", "")})
                        elif event.name == "tool_output":
                            call_id = raw.get("call_id") if isinstance(raw, dict) else getattr(raw, "call_id", None)
                            yield sse({"type": "tool_done", "name": tool_names.get(call_id, "tool")})
                reply = str(result.final_output)
                completed = True
                log.info("Chat stream completed: session=%s tools=%s reply=%d chars in %.1f s", session_id,
                         list(tool_names.values()) or "none", len(reply), time.perf_counter() - started)
                yield sse({"type": "done", "reply": reply})
            except Exception as exc:  # noqa: BLE001 - report failures to the client in-stream
                completed = True
                log.exception("Chat stream failed: session=%s after %.1f s", session_id,
                              time.perf_counter() - started)
                yield sse({"type": "error", "message": describe_error(exc)})
            finally:
                if not completed:
                    log.warning("Chat stream cancelled (client disconnected): session=%s after %.1f s",
                                session_id, time.perf_counter() - started)
                session.close()

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/sessions/{session_id}/messages")
    async def get_messages(session_id: str) -> dict[str, Any]:
        log.info("History request: session=%s", session_id)
        session = open_session(resolve_session_id(session_id))
        try:
            items = await session.get_items()
        finally:
            session.close()
        messages = []
        for item in items:
            role = item.get("role")
            if role not in ("user", "assistant"):
                continue  # skip tool calls/outputs
            content = item.get("content")
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            if content:
                messages.append({"role": role, "content": content})
        return {"session_id": session_id, "messages": messages}

    @app.delete("/sessions/{session_id}", status_code=204)
    async def delete_session(session_id: str) -> None:
        log.info("Delete session request: session=%s", session_id)
        session = open_session(resolve_session_id(session_id))
        try:
            await session.clear_session()
        finally:
            session.close()

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="PropertyFinder HTTP API")
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG_FILE, help="Config file to load (default: agent/.env)")
    parser.add_argument("--host", help="Override AGENT_HOST from the config file")
    parser.add_argument("--port", type=int, help="Override AGENT_PORT from the config file")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes (dev)")
    args = parser.parse_args()

    # Environment variables take precedence over the config file, and reload workers inherit them.
    os.environ["AGENT_CONFIG"] = str(Path(args.config).resolve())
    if args.host:
        os.environ["AGENT_HOST"] = args.host
    if args.port:
        os.environ["AGENT_PORT"] = str(args.port)
    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        sys.exit(f"Configuration error: {exc}")

    host, port = settings.server.host, settings.server.port
    shown_host = "localhost" if host in ("127.0.0.1", "0.0.0.0", "::") else host
    print(f"PropertyFinder agent API: http://{shown_host}:{port}  (set the web UI's AGENT_URL to this)", flush=True)
    print(f"Allowed web UI origins: {', '.join(settings.server.cors_origins) or '(none)'}", flush=True)

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        "server:create_app" if args.reload else create_app(settings),
        factory=args.reload,
        host=host,
        port=port,
        reload=args.reload,
        app_dir=str(AGENT_DIR),
        access_log=False,  # the request-logging middleware replaces uvicorn's access log
    )


if __name__ == "__main__":
    main()
