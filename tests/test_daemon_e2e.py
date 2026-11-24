"""End-to-end offline tests for WatchpostDaemon wiring Feishu, MockBackend, and Telegram."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import httpx

from watchpost.config import BackendConfig, FeishuConfig, TelegramConfig, WatchpostConfig
from watchpost.daemon import WatchpostDaemon, build_engine_from_config
from watchpost.models import ChannelConfig, ChatType, ThreadRecord, ThreadStatus, TriageMode, TriggerMode
from watchpost.notifiers.telegram import telegram_owner_offset_key


def test_daemon_feishu_to_telegram_to_owner_go_end_to_end() -> None:
    feishu_stage = {"step": 1}
    tg_poll_stage = {"step": 0}
    tg_sent: list[str] = []

    def feishu_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/open-apis/auth/v3/tenant_access_token/internal":
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "f-tok", "expire": 7200})

        if path == "/open-apis/im/v1/messages":
            items = [
                {
                    "message_id": "fm_seed",
                    "create_time": "1730000010000",
                    "msg_type": "text",
                    "sender": {"id": "ou_sam", "sender_type": "user"},
                    "mentions": [{"id": "ou_alex", "name": "Alex", "key": "@_user_1"}],
                    "body": {"content": json.dumps({"text": "@_user_1 bay 2 stuck overnight"})},
                },
                {
                    "message_id": "fm_seed_img",
                    "create_time": "1730000015000",
                    "msg_type": "image",
                    "sender": {"id": "ou_sam", "sender_type": "user"},
                    "body": {"content": json.dumps({"image_key": "img_seed_1"})},
                },
            ]
            if feishu_stage["step"] >= 2:
                items = [
                    {
                        "message_id": "fm_new_ping",
                        "create_time": "1730000090000",
                        "parent_id": "fm_seed",
                        "msg_type": "text",
                        "sender": {"id": "ou_alex", "sender_type": "user"},
                        "mentions": [{"id": "ou_lead", "name": "Lead", "key": "@_user_1"}],
                        "body": {"content": json.dumps({"text": "@_user_1 昨晚 bay 2 报错超时麻烦看一下"})},
                    },
                    *items,
                ]
            return httpx.Response(200, json={"code": 0, "data": {"items": items}})

        if path == "/open-apis/im/v1/messages/fm_seed_img/resources/img_seed_1":
            return httpx.Response(200, content=b"PNGDATA", headers={"content-type": "image/png"})

        return httpx.Response(404)

    def tg_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/sendMessage"):
            payload = json.loads(request.content.decode("utf-8"))
            tg_sent.append(payload["text"])
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 200 + len(tg_sent)}})

        if path.endswith("/getUpdates"):
            tg_poll_stage["step"] += 1
            if tg_poll_stage["step"] == 1:
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": [
                            {
                                "update_id": 1,
                                "message": {
                                    "message_id": 701,
                                    "chat": {"id": 888, "type": "private"},
                                    "from": {"is_bot": False},
                                    "text": "/status",
                                },
                            },
                            {
                                "update_id": 2,
                                "message": {
                                    "message_id": 702,
                                    "chat": {"id": 888, "type": "private"},
                                    "from": {"is_bot": False},
                                    "reply_to_message": {"message_id": 202},
                                    "text": "go",
                                },
                            },
                        ],
                    },
                )
            return httpx.Response(200, json={"ok": True, "result": []})

        return httpx.Response(404)

    async def _run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = WatchpostConfig(
                db_path=str(root / "wp.db"),
                media_dir=str(root / "media"),
                workspace_dir=str(root),
                language="zh",
                backend=BackendConfig(preset="mock"),
                feishu=FeishuConfig(
                    enabled=True,
                    auth_mode="tenant",
                    app_id="app_id_1",
                    app_secret="app_sec_1",
                    owner_open_id="ou_lead",
                    owner_name="Lead",
                ),
                telegram=TelegramConfig(enabled=True, bot_token="tg_tok", owner_chat_id="888"),
                channels=[
                    ChannelConfig(
                        id="feishu_ops",
                        adapter="feishu",
                        external_chat_id="oc_ops",
                        name="Ops Group",
                        chat_type=ChatType.group,
                        trigger_mode=TriggerMode.mention_owner,
                        max_catchup_age_s=0,
                    )
                ],
            )

            f_client = httpx.AsyncClient(transport=httpx.MockTransport(feishu_handler))
            t_client = httpx.AsyncClient(transport=httpx.MockTransport(tg_handler))
            engine = build_engine_from_config(cfg, feishu_client=f_client, telegram_client=t_client)
            daemon = WatchpostDaemon(engine, cfg)

            # Cycle 1: seed overnight history without triggering
            r1 = await daemon.run_once(seed_if_empty=True, debounced=False)
            assert r1["channels_polled"] == 1
            assert engine.store.list_threads() == []

            # Cycle 2: new @Lead message arrives -> diagnosis + start/completion bubbles pushed to Telegram
            feishu_stage["step"] = 2
            tg_poll_stage["step"] = 10  # skip owner action return in cycle 2
            await engine.poll_all_channels(seed_if_empty=False, debounced=False)
            threads = engine.store.list_threads()
            assert len(threads) == 1
            assert threads[0].triage_mode == TriageMode.mode_4_bug
            assert threads[0].pending_go is True
            assert threads[0].status == ThreadStatus.awaiting_go
            assert len(tg_sent) >= 2  # start ping + completion bubble(s)

            # Cycle 3: owner sends /status then swipe-replies "go" to msg_id 202
            tg_poll_stage["step"] = 0
            handled = await engine.poll_owner_actions_once()
            assert handled == 2
            updated = engine.store.get_thread(threads[0].id)
            assert updated is not None
            assert updated.pending_go is False
            assert updated.status == ThreadStatus.completed
            assert any("Watchpost 运行中" in text for text in tg_sent)

            # Verify run_forever exits promptly when stop_event is set
            stop_ev = asyncio.Event()
            stop_ev.set()
            await daemon.run_forever(stop_event=stop_ev, seed_on_start=False)
            await f_client.aclose()
            await t_client.aclose()

    asyncio.run(_run())


def test_daemon_telegram_top_level_personal_direct_typing_and_button_skip() -> None:
    tg_sent: list[dict] = []
    tg_typing: list[dict] = []
    tg_commands: list[dict] = []
    tg_updates: list[list[dict]] = []

    def tg_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/sendMessage"):
            body = json.loads(request.content.decode("utf-8"))
            tg_sent.append(body)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 500 + len(tg_sent)}})
        if path.endswith("/sendChatAction"):
            tg_typing.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"ok": True, "result": True})
        if path.endswith("/setMyCommands"):
            tg_commands.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"ok": True, "result": True})
        if path.endswith("/answerCallbackQuery") or path.endswith("/editMessageReplyMarkup"):
            return httpx.Response(200, json={"ok": True, "result": True})
        if path.endswith("/getUpdates"):
            batch = tg_updates.pop(0) if tg_updates else []
            return httpx.Response(200, json={"ok": True, "result": batch})
        return httpx.Response(404)

    async def _run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = WatchpostConfig(
                db_path=str(root / "wp.db"),
                media_dir=str(root / "media"),
                workspace_dir=str(root),
                personal_workspace_dir=str(root),
                personal_session_ttl_s=3600,
                ack_on_receive=False,
                language="en",
                backend=BackendConfig(preset="mock"),
                telegram=TelegramConfig(enabled=True, bot_token="tg_tok", owner_chat_id="888"),
            )
            t_client = httpx.AsyncClient(transport=httpx.MockTransport(tg_handler))
            engine = build_engine_from_config(cfg, telegram_client=t_client)
            daemon = WatchpostDaemon(engine, cfg)

            # Seed backlog + register_commands via seed_on_start=True
            tg_updates.append(
                [
                    {
                        "update_id": 90,
                        "message": {"message_id": 10, "chat": {"id": 888, "type": "private"}, "from": {"is_bot": False}, "text": "old message"},
                    }
                ]
            )
            stop_ev = asyncio.Event()
            stop_ev.set()
            await daemon.run_forever(stop_event=stop_ev, seed_on_start=True)
            assert len(tg_commands) == 1
            assert engine.store.get_kv(telegram_owner_offset_key("tg_tok")) == 91

            # Plain Telegram text routes into Personal Direct Chat using top-level personal_workspace_dir
            tg_updates.append(
                [
                    {
                        "update_id": 91,
                        "message": {"message_id": 11, "chat": {"id": 888, "type": "private"}, "from": {"is_bot": False}, "text": "check status of tests"},
                    }
                ]
            )
            assert await engine.poll_owner_actions_once() == 1
            personal_th = engine.store.list_threads()[0]
            assert personal_th.channel_id == "personal_direct"
            assert personal_th.sender_id == "telegram"
            assert tg_typing  # sendChatAction typing triggered during turn
            assert any("Summary" in m["text"] or "OK" in m["text"] or len(m["text"]) > 0 for m in tg_sent)

            # Inline skip button callback clears pending_go via Telegram callback_query
            ch_ops = ChannelConfig(id="ch_ops", adapter="simulator", external_chat_id="x1", name="Ops")
            engine.store.upsert_channel(ch_ops)
            th_wait = ThreadRecord(
                id="th_skip_1",
                channel_id="ch_ops",
                backend="mock",
                backend_thread_ref="r_skip",
                title="Bay 3",
                status=ThreadStatus.awaiting_go,
                pending_go=True,
            )
            engine.store.create_or_update_thread(th_wait)
            tg_updates.append(
                [
                    {
                        "update_id": 92,
                        "callback_query": {
                            "id": "cb_skip_1",
                            "from": {"is_bot": False},
                            "data": "wp:skip:th_skip_1",
                            "message": {"message_id": 505, "chat": {"id": 888}},
                        },
                    }
                ]
            )
            assert await engine.poll_owner_actions_once() == 1
            skipped = engine.store.get_thread("th_skip_1")
            assert skipped is not None and skipped.pending_go is False
            assert skipped.status == ThreadStatus.completed

            await t_client.aclose()

    asyncio.run(_run())
