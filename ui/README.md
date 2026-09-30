# PropertyFinder Web UI

A static web chat client for the PropertyFinder agent's HTTP API ([../agent](../agent)). It uses plain
HTML, CSS, and JavaScript with no build step, so you can host it on any static server or CDN, separately
from the agent.

| File | Role |
|---|---|
| [index.html](index.html) | Page layout |
| [app.js](app.js) | Connection handling, streaming chat, and history |
| [styles.css](styles.css) | Styles (light and dark themes, mobile-friendly) |
| [serve.py](serve.py) | Standard-library server that serves the UI and builds `config.js` from environment variables |
| [config.js](config.js) | Fallback defaults for plain static hosting |
| [.env.example](.env.example) | Configuration template |

## Configuration

`serve.py` reads these from the environment or `ui/.env`. Real environment variables take precedence
over the file.

| Setting | Default | Description |
|---|---|---|
| `AGENT_URL` | `http://localhost:8000` | Agent HTTP API URL, as the user's browser reaches it |
| `AGENT_API_KEY` | unset | Only if the agent sets `AGENT_API_KEY`. **This value is sent to the browser.** |
| `UI_HOST` / `UI_PORT` | `127.0.0.1` / `8080` | Address the UI server listens on |

The agent must allow the UI's address: add `http://UI_HOST:UI_PORT` to `AGENT_CORS_ORIGINS` in
`agent/.env`.

## Run

Start the agent API first (`python server.py` in `agent/`), then:

```bash
cd ui
cp .env.example .env     # first time only
python3 serve.py         # or: AGENT_URL=http://agent.example.com python3 serve.py
```

Open the URL it prints, which is http://localhost:8080 by default. `serve.py` has no dependencies, so any
Python 3.10+ works.

Other options:

```bash
python3 serve.py --env-file prod.env        # use a different env file
python3 serve.py --port 3000                # override UI_HOST/UI_PORT (--host, --port)
python3 serve.py --write-config             # write config.js from the env, for static hosting
```

For static hosting on a CDN, S3, nginx or similar, run `--write-config` with the target `AGENT_URL`, then
upload the `ui/` folder.

## Overriding the connection in the browser

Click the connection status in the top-right to point the UI at a different agent, or to enter an
API key. The browser remembers that choice until you click **Use default**, or until the configured
`AGENT_URL` changes. The configured value then takes over again.

Replies stream through `POST /chat/stream`. The conversation's session ID is kept in the browser, so
reloading the page restores the chat. **New chat** clears it.
