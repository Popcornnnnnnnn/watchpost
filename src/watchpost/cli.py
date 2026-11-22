"""Watchpost CLI entry point (`demo`, `serve`, `init`)."""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from pathlib import Path

import uvicorn

from watchpost.backends.mock import MockAgentBackend
from watchpost.engine import WatchpostEngine
from watchpost.models import EngineConfig
from watchpost.server import create_app
from watchpost.store import WatchpostStore


async def _run_demo(db_path: Path | None, *, language: str = "zh") -> int:
    if db_path is None:
        with tempfile.TemporaryDirectory() as tmp:
            return await _run_demo_into(Path(tmp) / "demo.db", language=language)
    return await _run_demo_into(db_path, language=language)


async def _run_demo_into(db_path: Path, *, language: str) -> int:
    store = WatchpostStore(db_path)
    engine = WatchpostEngine(
        store,
        MockAgentBackend(language=language),
        config=EngineConfig(language=language),
    )
    summary = await engine.run_demo_scenario()
    print(
        f"[Watchpost Demo] channels={summary['channels_seeded']} "
        f"threads={summary['threads_created']} downloaded_media={summary['downloaded_media_ids']}"
    )
    for th in store.list_threads():
        print(f"\n--- {th.title} [{th.triage_mode.value} | {th.status.value}] ---")
        for ev in store.list_thread_events(th.id):
            if ev.kind != "agent_reply":
                continue
            for idx, bubble in enumerate(ev.summary_bubbles, start=1):
                print(f"  [Bubble {idx}] {bubble}")
            if ev.suggested_reply:
                print(f"  [Copy Draft] {ev.suggested_reply}")
            if ev.needs_attention:
                print(f"  [Attention] {ev.needs_attention}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="watchpost",
        description="Headless local background bridge for your coding agent — zero-copy-paste head-starts delivered to your phone",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Initialize Watchpost SQLite WAL database")
    p_init.add_argument("--db", default="./data/watchpost.db", help="Path to SQLite database")

    p_demo = sub.add_parser("demo", help="Run offline head-start simulation scenario")
    p_demo.add_argument("--db", default="", help="Optional persistent DB path (defaults to temporary)")
    p_demo.add_argument("--lang", default="zh", help="Output language ('zh' or 'en')")

    p_serve = sub.add_parser("serve", help="Start headless Watchpost JSON control API server")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8787)
    p_serve.add_argument("--db", default="./data/watchpost.db")
    p_serve.add_argument("--lang", default="zh")

    args = parser.parse_args(argv)
    if args.command == "init":
        store = WatchpostStore(Path(args.db))
        print(f"Initialized Watchpost SQLite WAL database at {store.db_path}")
        return 0
    if args.command == "demo":
        target = Path(args.db) if args.db else None
        return asyncio.run(_run_demo(target, language=args.lang))
    if args.command == "serve":
        app = create_app(Path(args.db), config=EngineConfig(language=args.lang))
        uvicorn.run(app, host=args.host, port=args.port)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
