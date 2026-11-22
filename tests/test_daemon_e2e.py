"""End-to-end offline tests for WatchpostDaemon wiring Feishu, MockBackend, and Telegram."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import httpx

from watchpost.config import BackendConfig, FeishuConfig, TelegramConfig, WatchpostConfig
from watchpost.daemon import WatchpostDaemon, build_engine_from_config
from watchpost.models import ChannelConfig, ChatType, ThreadStatus, TriageMode, TriggerMode


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
