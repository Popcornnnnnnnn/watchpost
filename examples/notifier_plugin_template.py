#!/usr/bin/env python3
"""Runnable Watchpost NotifierPlugin example backed by local JSONL outbox/inbox files."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from watchpost.sdk import NotifierPlugin, make_owner_action, run_notifier_plugin


class JsonlNotifierPlugin(NotifierPlugin):
    """Minimal external owner notifier appending outbound messages to `WATCHPOST_EXAMPLE_OUTBOX`."""

    name = "jsonl_notifier"

    def send_message(self, text: str) -> str:
        token = f"tok_{uuid.uuid4().hex[:10]}"
        outbox = os.environ.get("WATCHPOST_EXAMPLE_OUTBOX", "").strip()
        if outbox:
            path = Path(outbox).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {"token": token, "text": text, "ts": time.time()},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        return token

    def poll_actions(self) -> list[dict[str, object]]:
        raw = os.environ.get("WATCHPOST_EXAMPLE_ACTIONS", "").strip()
        if not raw:
            return []
        actions_path = Path(raw).expanduser()
        if not actions_path.is_file():
            return []

        lines = actions_path.read_text(encoding="utf-8", errors="replace").splitlines()
        actions_path.write_text("", encoding="utf-8")
        out: list[dict[str, object]] = []
        for line in lines:
            if not line.strip():
                continue
            item = json.loads(line)
            act = make_owner_action(
                str(item.get("text") or ""),
                notifier=self.name,
                reply_token=str(item.get("reply_token") or ""),
            )
            if act is not None:
                out.append(act)
        return out


if __name__ == "__main__":
    raise SystemExit(run_notifier_plugin(JsonlNotifierPlugin()))
