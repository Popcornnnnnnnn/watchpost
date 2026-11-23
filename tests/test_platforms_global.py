"""Offline unit tests for global Milestone 2 platforms (Slack, Discord, Telegram inbound, ntfy, Bark, Pushover)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

from watchpost.adapters.discord import DiscordAdapter
from watchpost.adapters.slack import SlackAdapter
from watchpost.adapters.telegram import TelegramInboundAdapter, TelegramUpdateHub
from watchpost.config import (
    BarkConfig,
    DiscordConfig,
    NtfyConfig,
    PushoverConfig,
    SlackConfig,
    TelegramConfig,
)
from watchpost.models import (
    ChannelConfig,
    ChatType,
    MediaArtifact,
    ThreadRecord,
)
from watchpost.notifiers.bark import BarkNotifier
from watchpost.notifiers.discord import DiscordNotifier
from watchpost.notifiers.ntfy import NtfyNotifier
from watchpost.notifiers.pushover import PushoverNotifier
from watchpost.notifiers.slack import SlackNotifier
from watchpost.notifiers.telegram import TelegramNotifier


def test_slack_adapter_chats_messages_threads_and_media(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        assert request.headers.get("Authorization") == "Bearer xoxb-test"
        if request.url.path.endswith("/conversations.list"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "channels": [
                        {"id": "C_OPS", "name": "ops-alerts", "is_im": False},
                        {"id": "D_ALICE", "is_im": True, "user": "U_ALICE"},
                    ],
                },
            )
        if request.url.path.endswith("/users.info"):
            uid = request.url.params.get("user", "")
            names = {"U_ALICE": "Alice", "U_OWNER": "OwnerNick"}
            return httpx.Response(
                200,
                json={"ok": True, "user": {"id": uid, "profile": {"display_name": names.get(uid, uid)}}},
            )
        if request.url.path.endswith("/conversations.history"):
            if request.url.params.get("inclusive") == "true":
                ts = request.url.params.get("latest", "")
                return httpx.Response(
                    200,
                    json={"ok": True, "messages": [{"ts": ts, "user": "U_ALICE", "text": "Parent question"}]},
                )
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": [
                        {
                            "ts": "1700000001.000100",
                            "user": "U_ALICE",
                            "text": "Hey <@U_OWNER> check <!channel> log",
                            "reply_count": 1,
                            "latest_reply": "1700000003.000100",
                            "files": [
                                {
                                    "id": "F_IMG_1",
                                    "name": "trace.png",
                                    "filetype": "png",
                                    "mimetype": "image/png",
                                    "url_private_download": "https://files.slack.test/F_IMG_1/trace.png",
                                }
                            ],
                        },
                        {"ts": "1700000002.000100", "subtype": "channel_join", "user": "U_ALICE", "text": "joined"},
                    ],
                },
            )
        if request.url.path.endswith("/conversations.replies"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": [
                        {"ts": "1700000001.000100", "user": "U_ALICE", "text": "Parent"},
                        {
                            "ts": "1700000003.000100",
                            "thread_ts": "1700000001.000100",
                            "user": "U_ALICE",
                            "text": "Still failing after retry",
                        },
                    ],
                },
            )
        if str(request.url) == "https://files.slack.test/F_IMG_1/trace.png":
            return httpx.Response(200, content=b"PNGDATA")
        return httpx.Response(404, json={"ok": False, "error": "not_found"})

    async def _run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = SlackAdapter(
                SlackConfig(enabled=True, bot_token="xoxb-test", owner_user_id="U_OWNER"),
                client=client,
            )
            chats = await adapter.list_available_chats()
            assert len(chats) == 2
            assert chats[0] == {"external_chat_id": "C_OPS", "name": "#ops-alerts", "chat_type": "group"}
            assert chats[1] == {"external_chat_id": "D_ALICE", "name": "Alice", "chat_type": "p2p"}

            ch = ChannelConfig(id="slack_ops", adapter="slack", external_chat_id="C_OPS", name="Ops")
            msgs = await adapter.fetch_recent(ch)
            assert [m.message_id for m in msgs] == ["1700000001.000100", "1700000003.000100"]
            assert msgs[0].mentions_owner is True
            assert msgs[0].mentions_all is True
            assert "@OwnerNick" in msgs[0].clean_text
            assert "@channel" in msgs[0].clean_text
            assert msgs[1].reply_to_id == "1700000001.000100"

            by_id = await adapter.fetch_by_ids(ch, ["1700000001.000100"])
            assert len(by_id) == 1 and by_id[0].sender_name == "Alice"

            arts = await adapter.download_media(ch, msgs[0], tmp_path / "media")
            assert len(arts) == 1 and isinstance(arts[0], MediaArtifact)
            assert Path(arts[0].path).read_bytes() == b"PNGDATA"

    asyncio.run(_run())


def test_slack_notifier_dm_open_and_thread_steering() -> None:
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/conversations.open"):
            return httpx.Response(200, json={"ok": True, "channel": {"id": "D_DM_1"}})
        if request.url.path.endswith("/chat.postMessage"):
            body = json.loads(request.content.decode())
            posted.append(body)
            return httpx.Response(200, json={"ok": True, "ts": "1700000010.000100"})
        if request.url.path.endswith("/conversations.history"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": [
                        {"ts": "1700000010.000100", "bot_id": "B1", "text": "alert", "reply_count": 1},
                        {"ts": "1700000012.000100", "user": "U_OTHER", "text": "go"},
                        {"ts": "1700000013.000100", "user": "U_OWNER", "text": "#1 check logs"},
                        {"ts": "1700000014.000100", "user": "U_OWNER", "text": "status"},
                    ],
                },
            )
        if request.url.path.endswith("/conversations.replies"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": [
                        {"ts": "1700000010.000100", "bot_id": "B1", "text": "alert"},
                        {
                            "ts": "1700000011.000200",
                            "thread_ts": "1700000010.000100",
                            "user": "U_OWNER",
                            "text": "go",
                        },
                    ],
                },
            )
        return httpx.Response(404, json={"ok": False})

    ch = ChannelConfig(id="feishu_1", adapter="feishu", external_chat_id="oc_1", name="Support")
    th = ThreadRecord(id="th_1", channel_id="feishu_1", backend="mock", backend_thread_ref="r1")

    async def _run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            notifier = SlackNotifier(
                SlackConfig(dm_enabled=True, bot_token="xoxb-test", owner_user_id="U_OWNER"),
                client=client,
            )
            tokens = await notifier.notify_completion(ch, th, ["Summary bubble"])
            assert tokens == ["1700000010.000100"]
            assert posted[0]["channel"] == "D_DM_1"

            actions = await notifier.poll_owner_actions()
            assert [(a.kind, a.reply_token, a.list_index) for a in actions] == [
                ("approve_go", "1700000010.000100", 0),
                ("steer", "", 1),
                ("status", "", 0),
            ]
            # Second poll deduplicates already-seen timestamps cleanly
            assert await notifier.poll_owner_actions() == []

    asyncio.run(_run())


def test_discord_adapter_and_notifier_offline(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://cdn.discord.test/ATT1/dump.txt":
            assert "Authorization" not in request.headers
            return httpx.Response(200, content=b"ERR_LOG")
        assert request.headers.get("Authorization") == "Bot disc-secret"
        if request.url.path.endswith("/users/@me/guilds"):
            return httpx.Response(200, json=[{"id": "G1", "name": "Engineering"}])
        if request.url.path.endswith("/guilds/G1/channels"):
            return httpx.Response(
                200,
                json=[
                    {"id": "C_TEXT", "name": "backend-ops", "type": 0},
                    {"id": "C_VOICE", "name": "standup-voice", "type": 2},
                ],
            )
        if request.url.path.endswith("/channels/C_TEXT/messages/9001"):
            return httpx.Response(
                200,
                json={
                    "id": "9001",
                    "content": "First root",
                    "timestamp": "2026-11-24T00:00:01Z",
                    "author": {"id": "U2", "username": "bob"},
                    "mentions": [],
                    "attachments": [],
                },
            )
        if request.url.path.endswith("/channels/C_TEXT/messages"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "9002",
                        "content": "<@111> please check",
                        "timestamp": "2026-11-24T00:00:05Z",
                        "author": {"id": "U2", "global_name": "Bob"},
                        "mentions": [{"id": "111", "global_name": "Owner"}],
                        "mention_everyone": True,
                        "message_reference": {"message_id": "9001"},
                        "attachments": [
                            {
                                "id": "ATT1",
                                "filename": "dump.txt",
                                "content_type": "text/plain",
                                "url": "https://cdn.discord.test/ATT1/dump.txt",
                            }
                        ],
                    },
                    {
                        "id": "9001",
                        "content": "Initial alert",
                        "timestamp": "2026-11-24T00:00:01Z",
                        "author": {"id": "U2", "username": "bob"},
                        "mentions": [],
                        "attachments": [],
                    },
                ],
            )
        if request.url.path.endswith("/users/@me/channels"):
            return httpx.Response(200, json={"id": "DM_777"})
        if request.method == "POST" and request.url.path.endswith("/channels/DM_777/messages"):
            return httpx.Response(200, json={"id": "9900"})
        if request.method == "GET" and request.url.path.endswith("/channels/DM_777/messages"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "9902",
                        "content": "approve",
                        "author": {"id": "111", "username": "owner"},
                        "message_reference": {"message_id": "9900"},
                    },
                    {"id": "9900", "content": "bot alert", "author": {"id": "BOT", "bot": True}},
                ],
            )
        return httpx.Response(404)

    async def _run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cfg = DiscordConfig(enabled=True, dm_enabled=True, bot_token="disc-secret", owner_user_id="111")
            adapter = DiscordAdapter(cfg, client=client)
            chats = await adapter.list_available_chats()
            assert chats == [{"external_chat_id": "C_TEXT", "name": "#backend-ops", "chat_type": "group"}]

            ch = ChannelConfig(id="disc_1", adapter="discord", external_chat_id="C_TEXT", name="backend-ops")
            msgs = await adapter.fetch_recent(ch)
            assert [m.message_id for m in msgs] == ["9001", "9002"]
            assert msgs[1].mentions_owner is True
            assert msgs[1].mentions_all is True
            assert msgs[1].reply_to_id == "9001"
            assert msgs[1].clean_text == "@Owner please check"

            arts = await adapter.download_media(ch, msgs[1], tmp_path / "disc")
            assert len(arts) == 1 and Path(arts[0].path).read_bytes() == b"ERR_LOG"

            by_id = await adapter.fetch_by_ids(ch, ["9001"])
            assert len(by_id) == 1

            notifier = DiscordNotifier(cfg, client=client)
            th = ThreadRecord(id="t1", channel_id="disc_1", backend="mock", backend_thread_ref="r1")
            toks = await notifier.notify_completion(ch, th, ["Summary bubble"])
            assert toks == ["9900"]
            actions = await notifier.poll_owner_actions()
            assert len(actions) == 1
            assert actions[0].kind == "approve_go"
            assert actions[0].reply_token == "9900"

    asyncio.run(_run())


def test_telegram_shared_hub_prevents_stolen_updates_and_downloads_media(tmp_path: Path) -> None:
    poll_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        if request.url.path.endswith("/getUpdates"):
            poll_count += 1
            if poll_count == 1:
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": [
                            {
                                "update_id": 100,
                                "message": {
                                    "message_id": 501,
                                    "date": 1700000100,
                                    "chat": {"id": -100888, "type": "supergroup", "title": "OnCall Team"},
                                    "from": {"id": 222, "first_name": "Sam", "username": "samdev"},
                                    "text": "@owner_tg timeout in worker",
                                    "entities": [{"type": "mention", "offset": 0, "length": 9}],
                                    "voice": {"file_id": "VOICE_1", "mime_type": "audio/ogg"},
                                },
                            },
                            {
                                "update_id": 101,
                                "message": {
                                    "message_id": 801,
                                    "date": 1700000105,
                                    "chat": {"id": 999001, "type": "private", "first_name": "Owner"},
                                    "from": {"id": 999001, "first_name": "Owner"},
                                    "text": "go",
                                    "reply_to_message": {"message_id": 42},
                                },
                            },
                        ],
                    },
                )
            return httpx.Response(200, json={"ok": True, "result": []})
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "voice/file_1.ogg"}})
        if request.url.path.endswith("/file/bottg-tok/voice/file_1.ogg"):
            return httpx.Response(200, content=b"OGGBYTES")
        return httpx.Response(404)

    async def _run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cfg = TelegramConfig(
                enabled=True,
                inbound_enabled=True,
                bot_token="tg-tok",
                owner_chat_id="999001",
                owner_username="@owner_tg",
            )
            hub = TelegramUpdateHub("tg-tok", client=client)
            inbound = TelegramInboundAdapter(cfg, hub=hub)
            notifier = TelegramNotifier(cfg, hub=hub, client=client)

            ch = ChannelConfig(id="tg_oncall", adapter="telegram", external_chat_id="-100888", name="OnCall Team")
            group_msgs = await inbound.fetch_recent(ch)
            assert len(group_msgs) == 1
            assert group_msgs[0].mentions_owner is True
            assert group_msgs[0].media_refs[0].kind == "audio"

            actions = await notifier.poll_owner_actions()
            assert len(actions) == 1
            assert actions[0].kind == "approve_go"
            assert actions[0].reply_token == "42"

            chats = await inbound.list_available_chats()
            assert chats == [{"external_chat_id": "-100888", "name": "OnCall Team", "chat_type": "group"}]

            arts = await inbound.download_media(ch, group_msgs[0], tmp_path / "tg")
            assert len(arts) == 1 and Path(arts[0].path).read_bytes() == b"OGGBYTES"

    asyncio.run(_run())


def test_ntfy_bark_and_pushover_notifiers() -> None:
    ntfy_posts: list[dict[str, Any]] = []
    bark_posts: list[dict[str, Any]] = []
    pushover_forms: list[str] = []

    def ntfy_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            ntfy_posts.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"id": "msg_out_1"})
        ndjson = "\n".join(
            [
                json.dumps({"event": "open", "topic": "wp-reply"}),
                json.dumps({"event": "message", "id": "msg_out_1", "message": "echo"}),
                json.dumps({"event": "message", "id": "cmd_1", "message": "#2 go"}),
            ]
        )
        return httpx.Response(200, text=ndjson)

    def bark_handler(request: httpx.Request) -> httpx.Response:
        bark_posts.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"code": 200, "timestamp": 1700000200})

    def pushover_handler(request: httpx.Request) -> httpx.Response:
        pushover_forms.append(request.content.decode("utf-8"))
        return httpx.Response(200, json={"status": 1, "request": "req_1"})

    ch = ChannelConfig(id="c1", adapter="slack", external_chat_id="C1", name="#alerts", chat_type=ChatType.group)
    th = ThreadRecord(id="t1", channel_id="c1", backend="mock", backend_thread_ref="r1", sender_name="Bob")

    async def _run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(ntfy_handler)) as ntfy_client:
            ntfy = NtfyNotifier(
                NtfyConfig(
                    enabled=True,
                    server_url="https://ntfy.example",
                    topic="wp-alerts",
                    reply_topic="wp-reply",
                    token="tk_secret",
                    priority="high",
                    tags=["warning"],
                ),
                client=ntfy_client,
            )
            assert await ntfy.notify_start(ch, th, "hi") == []
            toks = await ntfy.notify_completion(ch, th, ["中文告警内容"])
            assert toks == ["msg_out_1"]
            assert ntfy_posts[0]["priority"] == 4
            assert ntfy_posts[0]["tags"] == ["warning"]
            actions = await ntfy.poll_owner_actions()
            assert len(actions) == 1 and actions[0].kind == "approve_go" and actions[0].list_index == 2
            assert await ntfy.poll_owner_actions() == []

        async with httpx.AsyncClient(transport=httpx.MockTransport(bark_handler)) as bark_client:
            bark = BarkNotifier(
                BarkConfig(enabled=True, server_url="https://bark.example", device_key="dev123", auto_copy=True),
                client=bark_client,
            )
            assert await bark.notify_start(ch, th, "hi") == []
            toks = await bark.notify_completion(
                ch,
                th,
                ["【#alerts】服务连接超时，正在排查。\n\n下面这条可直接复制回复：", "我在看日志，稍后同步原因。"],
            )
            assert toks == ["1700000200"]
            assert bark_posts[0]["copy"] == "我在看日志，稍后同步原因。"
            assert bark_posts[0]["autoCopy"] == "1"

        async with httpx.AsyncClient(transport=httpx.MockTransport(pushover_handler)) as po_client:
            po = PushoverNotifier(
                PushoverConfig(enabled=True, app_token="app1", user_key="usr1"),
                client=po_client,
            )
            toks = await po.notify_completion(ch, th, ["A" * 1100])
            assert toks == ["req_1"]
            assert len(pushover_forms) == 2

    asyncio.run(_run())
