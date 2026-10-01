"""Serve the PropertyFinder web UI, configured from environment variables.

The agent URL (and optional API key) are read from the environment or ui/.env and
served to the browser as /config.js, so the static files never need editing.

Settings (environment variables take precedence over ui/.env):
  AGENT_URL        URL of the agent HTTP API     (default: http://localhost:8000)
  AGENT_API_KEY    sent on every request in the x-api-key header (visible in the browser!)
  UI_HOST          address to listen on          (default: 127.0.0.1)
  UI_PORT          port to listen on             (default: 8080)

Run:            python serve.py [--env-file ui/.env] [--host H] [--port P]
Static hosting: python serve.py --write-config   (writes config.js, then host ui/ anywhere)

Uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UI_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_FILE = UI_DIR / ".env"


def load_env_file(path: Path) -> None:
    """Minimal .env parser: KEY=VALUE lines, # comments, optional quotes. Never overrides real env vars."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def ui_config() -> dict[str, str]:
    return {
        "agentUrl": os.getenv("AGENT_URL", "").strip() or "http://localhost:8000",
        "apiKey": os.getenv("AGENT_API_KEY", "").strip(),
    }


def render_config_js(config: dict[str, str]) -> str:
    return (
        "// Generated from environment variables by serve.py; do not edit by hand.\n"
        "// Users can still override these in the UI's Connection panel.\n"
        f"window.PROPERTY_FINDER_CONFIG = {json.dumps(config, indent=2)};\n"
    )


class UIRequestHandler(SimpleHTTPRequestHandler):
    """Static file handler that serves /config.js from the environment."""

    config_js: bytes = b""

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        if self.path.split("?", 1)[0] == "/config.js":
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(self.config_js)))
            self.end_headers()
            self.wfile.write(self.config_js)
            return
        super().do_GET()

    def end_headers(self) -> None:
        # Avoid stale UI files after edits during development.
        if not self.path.startswith("/config.js"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the PropertyFinder web UI")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE), help="Env file to load (default: ui/.env)")
    parser.add_argument("--host", help="Override UI_HOST")
    parser.add_argument("--port", type=int, help="Override UI_PORT")
    parser.add_argument("--write-config", action="store_true", help="Write config.js for static hosting and exit")
    args = parser.parse_args()

    env_file = Path(args.env_file)
    if args.env_file != str(DEFAULT_ENV_FILE) and not env_file.is_file():
        sys.exit(f"Env file not found: {env_file}")
    load_env_file(env_file)

    config = ui_config()
    config_js = render_config_js(config)

    if args.write_config:
        (UI_DIR / "config.js").write_text(config_js)
        print(f"Wrote {UI_DIR / 'config.js'} (agentUrl={config['agentUrl']})")
        return

    host = args.host or os.getenv("UI_HOST", "").strip() or "127.0.0.1"
    try:
        port = args.port or int(os.getenv("UI_PORT", "8080"))
    except ValueError:
        sys.exit("UI_PORT must be an integer.")

    UIRequestHandler.config_js = config_js.encode()
    handler = partial(UIRequestHandler, directory=str(UI_DIR))
    server = ThreadingHTTPServer((host, port), handler)
    shown_host = "localhost" if host in ("127.0.0.1", "0.0.0.0", "::") else host
    print(f"PropertyFinder web UI: http://{shown_host}:{port}")
    print(f"Agent URL: {config['agentUrl']}" + ("  (with API key)" if config["apiKey"] else ""))
    print(f"Make sure the agent's AGENT_CORS_ORIGINS includes http://{shown_host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
