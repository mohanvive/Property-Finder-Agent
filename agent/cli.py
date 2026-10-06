"""Terminal chat front end for the PropertyFinder agent.

Run interactively:   python cli.py
Single question:     python cli.py "Find 3-bed houses for sale in Austin and quote insurance"
Other config file:   python cli.py --config prod.env
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from agents import Runner, SQLiteSession
from config import DEFAULT_CONFIG_FILE, ConfigError, Settings, load_settings
from property_agent import build_agent, configure_openai, open_mcp_servers, print_connection_report


async def ask(settings: Settings, question: str, session: SQLiteSession) -> str:
    # Fresh MCP connections per question: the servers drop long-lived sessions.
    async with open_mcp_servers(settings.mcp_servers) as connections:
        agent = build_agent(connections, settings.openai.model)
        result = await Runner.run(agent, question, session=session, max_turns=15)
    return str(result.final_output)


async def main() -> None:
    parser = argparse.ArgumentParser(description="PropertyFinder agent")
    parser.add_argument("question", nargs="*", help="Ask a single question and exit")
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG_FILE, help="Config file to load (default: agent/.env)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show tool call logs")
    args = parser.parse_args()

    try:
        settings: Settings = load_settings(args.config)
    except ConfigError as exc:
        sys.exit(f"Configuration error: {exc}")
    configure_openai(settings.openai)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)

    session = SQLiteSession("property-finder")
    if args.question:
        print(await ask(settings, " ".join(args.question), session))
        return

    print("Checking MCP servers...")
    async with open_mcp_servers(settings.mcp_servers) as connections:
        print_connection_report(connections, settings.mcp_servers)

    print(f"\nPropertyFinder ready (model: {settings.openai.model} @ {settings.openai.base_url}). Type 'exit' to quit.\n")
    while True:
        try:
            question = (await asyncio.to_thread(input, "You: ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not question:
            continue
        if question.lower() in {"exit", "quit"}:
            break
        try:
            print(f"\nAgent: {await ask(settings, question, session)}\n")
        except Exception as exc:  # noqa: BLE001 - keep the chat loop alive
            print(f"\n[error] {type(exc).__name__}: {exc}\n", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
