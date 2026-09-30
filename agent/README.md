# PropertyFinder Agent

A Python AI agent built on the [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/) that
searches for properties and gives insurance premium estimates. It gets its tools from two MCP servers
hosted on Choreo, connecting over streamable HTTP:

| MCP server | Purpose |
|---|---|
| `property-search` | Find rentals, homes for sale, and business spaces |
| `insurance-quoter` | Quotes by property ID or by details, add-ons, and cross-state comparisons |

## Layout

| File | Role |
|---|---|
| [property_agent.py](property_agent.py) | Core: agent instructions, OpenAI client setup, MCP connections |
| [cli.py](cli.py) | Terminal chat front end |
| [server.py](server.py) | HTTP API front end (FastAPI) |
| [config.py](config.py) | Loads all settings from the config file |
| [auth.py](auth.py) | Choreo OAuth2 auth for the MCP servers |

## Setup

Run these from this `agent/` folder:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then set OPENAI_API_KEY (and Choreo credentials if needed)
```

## Configuration

[config.py](config.py) reads every setting from the config file, which defaults to `agent/.env` no matter
which directory you run from. Use `--config <file>` to load another file, for example one per environment.
Real environment variables take precedence over the file.

| Setting | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | yes | API key for the LLM endpoint |
| `OPENAI_BASE_URL` | no | OpenAI-compatible endpoint. Defaults to `https://api.openai.com/v1` |
| `OPENAI_MODEL` | no | Model name. Defaults to `gpt-5-mini` |
| `OPENAI_API_MODE` | no | `responses` (default), or `chat_completions` for endpoints without the Responses API |
| `OPENAI_AGENTS_DISABLE_TRACING` | no | Set to `true` when the endpoint isn't OpenAI |
| `PROPERTY_SEARCH_MCP_URL` | yes | Property search MCP endpoint |
| `INSURANCE_QUOTER_MCP_URL` | yes | Insurance quoter MCP endpoint |
| `AGENT_HOST` / `AGENT_PORT` | no | HTTP API bind address. Defaults to `127.0.0.1` / `8000`; use `0.0.0.0` to expose it on the network |
| `AGENT_API_KEY` | no | If set, HTTP clients must send `Authorization: Bearer <key>` |
| `AGENT_CORS_ORIGINS` | no | Browser origins allowed to call the API, i.e. where `ui/` is served. Defaults to `http://localhost:8080,http://127.0.0.1:8080` |
| `AGENT_SESSIONS_DB` | no | SQLite file for conversation history. Defaults to `agent/sessions.db` |

### Choreo authentication

If an MCP endpoint is protected (it returns 401), create an application in the Choreo developer portal,
subscribe it to the API, and generate keys. Then set one of these:

- `PROPERTY_SEARCH_CLIENT_ID` + `PROPERTY_SEARCH_CLIENT_SECRET`: the agent fetches tokens from
  `https://sts.choreo.dev/oauth2/token` and refreshes them automatically. You can override the endpoint
  with `PROPERTY_SEARCH_TOKEN_URL`.
- `PROPERTY_SEARCH_ACCESS_TOKEN`: a static token, which expires.

The `INSURANCE_QUOTER_` prefix works the same way. Use the `CHOREO_` prefix to share credentials across
both servers.

If a server can't be reached, the agent skips it with a warning and runs with the tools it has.

## Command line

```bash
python cli.py                            # interactive chat (keeps conversation memory)
python cli.py "Quote insurance for a $450k house in Austin, TX 78701, 2000 sqft"
python cli.py --config prod.env          # use a different config file
python cli.py -v                         # verbose logging
```

## HTTP API

```bash
python server.py                         # serves on AGENT_HOST:AGENT_PORT from the config file
python server.py --port 9000             # override the config's host/port (--host, --port)
python server.py --reload                # auto-reload during development
```

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Status, model, LLM endpoint, and connected MCP servers/tools |
| `POST` | `/chat` | `{"message", "session_id"?}` → `{"reply", "session_id"}` |
| `POST` | `/chat/stream` | Same body; Server-Sent Events: `session`, `tool_call`, `tool_done`, `delta`, `done`, `error` |
| `GET` | `/sessions/{id}/messages` | Conversation history |
| `DELETE` | `/sessions/{id}` | Clear a conversation |

At startup the server prints its URL, for example `http://localhost:8000`. Set the web UI's `AGENT_URL`
to that URL.

Omit `session_id` to start a new conversation. The response returns an ID; send it with later messages
to continue that conversation.

```bash
curl -X POST localhost:8000/chat -H 'Content-Type: application/json' \
  -d '{"message": "Find 3-bedroom houses for sale in Austin"}'
```

Interactive API docs are served at http://localhost:8000/docs.

## Example prompts

- "Find 2-bedroom apartments for rent in San Francisco under $4,000"
- "Get an insurance quote for property 12 and list the recommended add-ons"
- "Compare insurance for a $600k single-family home, 2,200 sqft, across CA, TX, FL and CO"
