# Multi-Platform Roadmap

Watchpost separates **core intelligence** (debouncing, Bucket 1/2 context, 4-mode triage, Quiet Hours, cross-channel memory, hot reload, approval gates) from **platform transport adapters**.

## Work Chat Sources (Inbound `ChannelAdapter` / `external_adapters`)

| Platform | Delivery Path | Key Needs | Priority |
| --- | --- | --- | --- |
| **Feishu / Lark** | Native built-in (`tenant`, `user`, `lark-cli`) | Read-only fetch, `<at>` mentions, images/audio/files | Shipped |
| **Slack** | Native or SDK plugin (Socket Mode) | Zero public webhook URL, thread `thread_ts` linking, file downloads | Next (Milestone 2) |
| **Discord** | SDK plugin / Gateway bot | Guild channels, `@mention` triggers, thread replies | Milestone 2 |
| **DingTalk / WeCom (企业微信)** | Native webhook/stream or SDK plugin | Domestic team group `@me` triggers & media download | Milestone 2 |
| **Telegram (Inbound Group/DM)** | Reuse existing `TelegramConfig` | Bot privacy-off group watching & reply chains | Milestone 2 |
| **WhatsApp / Desktop Bridges** | External out-of-process plugin (`external_adapters`) | Keep third-party/copyleft bridges outside `Apache-2.0` core | Supported via SDK |

## Owner Entrances (Outbound + Steering `NotifierAdapter` / `external_notifiers`)

| Entrance | Best For | 2-Way Controls (`go`, `skip`, `#N`, `/brief`, `/mute`) | Priority |
| --- | --- | --- | --- |
| **macOS iMessage** | Mac users with iPhone | Full (swipe-reply `thread_originator_guid`, voice, photos) | Shipped |
| **Telegram** | Cross-platform desktop/mobile | Full (reply-to-message, commands, personal chat) | Shipped |
| **Slack DM / Discord DM** | Linux/Windows devs living in Slack or Discord | Full via `make_owner_action()` | Milestone 2 |
| **ntfy / Bark / Pushover** | Ultra-lightweight Linux/iOS/Android push alerts | 1-way push (or 2-way if paired with ntfy subscribe) via `external_notifiers` | Milestone 2 |

## Shipped Foundation (Milestone 1)

- External notifier host (`ExternalCommandNotifier`) + `external_notifiers` config
- Zero-dependency Python SDK (`watchpost.sdk`) with `ChannelPlugin`, `NotifierPlugin`, `make_chat`, `make_message`, `make_owner_action`
- CLI scaffolder & conformance runner (`watchpost plugins scaffold` & `watchpost plugins check`)
- Interactive onboarding wizard (`watchpost setup` / `watchpost init-config --interactive`)
