# PropertyFinder

An AI assistant that searches for properties and estimates their insurance premiums. The repo has two
independent parts:

```
Agent-PropertyFinder/
├── agent/   Python agent (OpenAI Agents SDK + MCP), with a terminal CLI and an HTTP API
└── ui/      Static web UI that connects to the agent's HTTP API
```

```
Browser (ui/) ──HTTP/SSE──▶ agent/server.py ──▶ OpenAI LLM
                                  │
                                  ├──MCP──▶ property-search  (Choreo)
                                  └──MCP──▶ insurance-quoter (Choreo)
```

## Quick start

```bash
cd agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # set OPENAI_API_KEY; AGENT_HOST/AGENT_PORT set the API address
.venv/bin/python server.py    # prints the agent API URL, e.g. http://localhost:8000
```

In a second terminal:

```bash
cd ui
cp .env.example .env          # set AGENT_URL to the agent API URL; UI_HOST/UI_PORT set the UI address
python3 serve.py              # web UI on http://localhost:8080
```

The two sides have to agree on addresses:

| Setting | Where | Must match |
|---|---|---|
| `AGENT_HOST` / `AGENT_PORT` | `agent/.env` | the host and port in the UI's `AGENT_URL` |
| `AGENT_URL` | `ui/.env` | the URL `server.py` prints at startup |
| `AGENT_CORS_ORIGINS` | `agent/.env` | the UI's address, `http://UI_HOST:UI_PORT` |

- [agent/README.md](agent/README.md) covers configuration, the CLI, and the HTTP API reference.
- [ui/README.md](ui/README.md) covers the web UI and how to host it.
