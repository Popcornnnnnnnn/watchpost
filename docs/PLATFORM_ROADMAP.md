# Multi-Platform Support Matrix

Watchpost separates **core intelligence** (debouncing, Bucket 1/2 context, 4-mode triage, Quiet Hours, cross-channel memory, hot reload, approval gates) from **platform transport adapters**.

## Work Chat Sources (Inbound `ChannelAdapter` / `external_adapters`)

| Platform | Delivery Path | Key Capabilities | Status |
| --- | --- | --- | --- |
| **Feishu / Lark** | Built-in (`tenant`, `user`, `lark-cli`) | Read-only fetch, `<at>` mentions, images/audio/files | Shipped |
| **Slack** | Built-in (`SlackAdapter`) | Slack Web API (`conversations.history`/`replies`), `<@U...>` & `<!channel>` detection, thread linking, private file download | Shipped |
| **Discord** | Built-in (`DiscordAdapter`) | Discord REST API v10, guild channel discovery, `@mention` triggers, `message_reference` chains, attachments | Shipped |
| **Telegram (Inbound Group/DM)** | Built-in (`TelegramInboundAdapter` + `TelegramUpdateHub`) | Shared single-stream `getUpdates` hub alongside `TelegramNotifier` (no stolen updates), `@username`/text mentions, photo/voice/file download | Shipped |
| **DingTalk** | Built-in (`DingTalkAdapter`) | Signed HMAC-SHA256 webhook (`POST /api/webhooks/dingtalk`), `atUsers` triggers, robot file download via OAuth2 | Shipped |
| **WeCom (企业微信)** | Built-in (`WeComAdapter`) | Signed SHA1 webhook (`GET/POST /api/webhooks/wecom`), XXE-safe XML & JSON ingest, `media/get` attachments | Shipped |
| **WhatsApp / Desktop Bridges** | External SDK plugin (`examples/whatsapp_plugin_template.py`) | Cloud API / bridge JSONL template over stdio JSON (`watchpost.sdk`), keeping third-party/copyleft bridges outside `Apache-2.0` core | Shipped (SDK Template) |

## Owner Entrances (Outbound + Steering `NotifierAdapter` / `external_notifiers`)

| Entrance | Best For | Controls (`go`, `skip`, `#N`, `/brief`, `/mute`, `/new`) | Status |
| --- | --- | --- | --- |
| **macOS iMessage** | Mac users with iPhone | Full 2-way (swipe-reply `thread_originator_guid`, voice, photos) | Shipped |
| **Telegram** | Cross-platform desktop/mobile | Full 2-way (reply binding, shared hub, personal chat) | Shipped |
| **Slack DM** | Teams living in Slack | Full 2-way (`conversations.open` + thread reply `thread_ts` steering) | Shipped |
| **Discord DM** | Developers living in Discord | Full 2-way (`@me/channels` DM + `message_reference` reply steering) | Shipped |
| **ntfy** | Lightweight Linux/Android/desktop push | 1-way JSON UTF-8 push + optional 2-way `reply_topic` command polling | Shipped |
| **Bark** | Minimal iOS push alerts | 1-way push with 1-tap `copy` populated strictly from clean draft bubbles | Shipped |
| **Pushover** | Reliable multi-device mobile/desktop push | 1-way push with automatic message chunking (`<=1024` chars) | Shipped |
| **WeCom (企业微信)** | Enterprise internal push (`WeComNotifier`) | 1-way self-built app push (`cgi-bin/message/send`) | Shipped |

## Webhook & Plugin Notes

- **Isolated tunnel listener (`webhook_port`):** Set `webhook_port` (for example `8788`) in `config.json` when exposing DingTalk or WeCom callbacks over a reverse tunnel (`create_webhook_app`), keeping `/api/*` control routes restricted to Bearer-authenticated `127.0.0.1:api_port`.
- **Signed webhook verification:**
  - `POST /api/webhooks/dingtalk` verifies `timestamp` + HMAC-SHA256 `sign` against `dingtalk.webhook_secret` (or `app_secret`) and persists normalized messages into the local webhook inbox.
  - `GET /api/webhooks/wecom` and `POST /api/webhooks/wecom` verify SHA1 `msg_signature` against `wecom.callback_token`; inbound bodies expect plaintext XML or JSON (use WeCom plaintext callback mode or terminate AES callback decryption in an upstream proxy before forwarding to Watchpost).
- **Custom bridges (`watchpost.sdk`):** Any additional platform can be added without modifying Watchpost core via `watchpost plugins scaffold` and verified via `watchpost plugins check`.
