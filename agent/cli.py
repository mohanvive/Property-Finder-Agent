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
from contextlib import AsyncExitStack

from agents import Agent, Runner, SQLiteSession
from config import DEFAULT_CONFIG_FILE, ConfigError, Settings, load_settings
from property_agent import build_agent, configure_openai, connect_servers


async def ask(agent: Agent, question: str, session: SQLiteSession) -> str:
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

    async with AsyncExitStack() as stack:
        print("Connecting to MCP servers...")
        servers = await connect_servers(stack, settings.mcp_servers)
        if not servers:
            sys.exit("No MCP servers available; cannot continue.")

        agent = build_agent(servers, settings.openai.model)
        session = SQLiteSession("property-finder")

        if args.question:
            print(await ask(agent, " ".join(args.question), session))
            return

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
                print(f"\nAgent: {await ask(agent, question, session)}\n")
            except Exception as exc:  # noqa: BLE001 - keep the chat loop alive
                print(f"\n[error] {type(exc).__name__}: {exc}\n", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
