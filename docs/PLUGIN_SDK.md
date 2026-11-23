# Watchpost Plugin SDK

Watchpost keeps core integrations strict **Apache-2.0** and talks to external messaging platforms and push/DM gateways through a tiny **JSON-over-stdio subprocess contract**. You can write plugins in Python (using `watchpost.sdk`), Node.js, Go, Rust, or shell scripts.

## 1. Two Plugin Kinds

- **Channel Plugins (`external_adapters`):** Watch external DMs or groups and emit `NormalizedMessage` items (`list-chats`, `fetch-recent`, `fetch-by-ids`, `download-media`, `health`).
- **Notifier Plugins (`external_notifiers`):** Deliver short status/conclusion bubbles to the owner and optionally poll owner replies (`notify-start`, `notify-completion`, `send-message`, `poll-actions`, `health`).

## 2. Quickstart with `watchpost plugins`

Scaffold a starter plugin, run automated conformance checks, and inspect the generated snippet:

```bash
watchpost plugins scaffold --kind channel --name slack_bridge --dest ./plugins/slack_bridge
watchpost plugins check --kind channel --test-fetch --cmd python3 ./plugins/slack_bridge/plugin.py

watchpost plugins scaffold --kind notifier --name ntfy_push --dest ./plugins/ntfy_push
watchpost plugins check --kind notifier --cmd python3 ./plugins/ntfy_push/plugin.py
```

Runnable reference implementations live in:
- `examples/channel_plugin_template.py`
- `examples/notifier_plugin_template.py`

## 3. Python SDK (`watchpost.sdk`)

### Channel Plugin Example

```python
from watchpost.sdk import ChannelPlugin, make_chat, make_message, run_channel_plugin

class MyChatPlugin(ChannelPlugin):
    name = "my_chat"

    def list_chats(self):
        return [make_chat("room_1", "Ops Group", chat_type="group")]

    def fetch_recent(self, channel, limit=50):
        return [
            make_message(
                message_id="m_1",
                sender_id="u_1",
                sender_name="Teammate",
                clean_text="@owner can you check the worker logs?",
                mentions_owner=True,
                position=1,
            )
        ]

if __name__ == "__main__":
    raise SystemExit(run_channel_plugin(MyChatPlugin()))
```

### Notifier Plugin Example

```python
from watchpost.sdk import NotifierPlugin, make_owner_action, run_notifier_plugin

class MyPushNotifier(NotifierPlugin):
    name = "my_push"

    def send_message(self, text: str) -> str:
        # Send `text` to Slack DM, Discord DM, ntfy, Bark, Pushover, etc.
        return "msg-token-1"

    def poll_actions(self):
        # Optional for 2-way notifiers; supports #N, go, skip, /brief, /mute, /unmute
        act = make_owner_action("go", notifier=self.name, reply_token="msg-token-1")
        return [act] if act else []

if __name__ == "__main__":
    raise SystemExit(run_notifier_plugin(MyPushNotifier()))
```

## 4. Language-Agnostic Stdio Wire Contract

Watchpost invokes `[...command, <action>]` with JSON on `stdin` and expects a single JSON object on `stdout` (non-zero exit code reports errors via `stderr`).

### Channel Plugin Actions (`external_adapters`)

| Action | Stdin JSON | Expected Stdout JSON |
| --- | --- | --- |
| `health` | `{}` | `{"ok": true, ...}` |
| `list-chats` | `{}` | `{"chats": [{"external_chat_id": "...", "name": "...", "chat_type": "p2p\|group"}]}` |
| `fetch-recent` | `{"channel": {...}, "limit": 50}` | `{"messages": [<NormalizedMessage>, ...]}` |
| `fetch-by-ids` | `{"channel": {...}, "message_ids": ["..."]}` | `{"messages": [<NormalizedMessage>, ...]}` |
| `download-media` | `{"channel": {...}, "message": {...}, "dest_dir": "..."}` | `{"artifacts": [{"kind": "image\|audio\|file", "key": "...", "path": "..."}]}` |

### Notifier Plugin Actions (`external_notifiers`)

| Action | Stdin JSON | Expected Stdout JSON |
| --- | --- | --- |
| `health` | `{}` | `{"ok": true, ...}` |
| `notify-start` | `{"channel": {...}, "thread": {...}, "preview": "...", "is_steer": false, "attachments": []}` | `{"tokens": ["tok_1"]}` |
| `notify-completion` | `{"channel": {...}, "thread": {...}, "bubbles": ["..."]}` | `{"tokens": ["tok_2"]}` |
| `send-message` | `{"text": "..."}` | `{"token": "tok_3"}` |
| `poll-actions` | `{}` | `{"actions": [<OwnerAction>, ...]}` |

## 5. Security & Licensing Notes

- Keep secrets in environment variables (`${VAR_NAME}`) or plugin-local env blocks; never commit tokens to `config.json`.
- External plugins run out-of-process, keeping proprietary desktop protocols and copyleft (`GPL-3.0` / `AGPL`) bridges cleanly isolated from Watchpost's `Apache-2.0` core.
