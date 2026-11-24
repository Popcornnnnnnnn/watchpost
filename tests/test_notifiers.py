"""Offline unit tests for TelegramNotifier and IMessageNotifier."""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from pathlib import Path

import httpx

from watchpost.config import IMessageConfig, TelegramConfig
from watchpost.models import ChannelConfig, ThreadRecord
from watchpost.notifiers.imessage import IMessageNotifier, extract_attributed_body_text
from watchpost.notifiers.telegram import TelegramNotifier, detect_owner_chat_id


def test_telegram_notifier_send_auth_and_actions() -> None:
    sent_payloads: list[dict] = []
    poll_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_calls
        path = request.url.path
        if path.endswith("/sendMessage"):
            body = request.content.decode("utf-8")
            import json

            sent_payloads.append(json.loads(body))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 100 + len(sent_payloads)}})

        if path.endswith("/getUpdates"):
            poll_calls += 1
            if poll_calls == 1:
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": [
                            {
                                "update_id": 10,
                                "message": {
                                    "message_id": 500,
                                    "chat": {"id": 999999, "type": "private", "username": "stranger"},
                                    "from": {"is_bot": False},
                                    "text": "go",
                                },
                            },
                            {
                                "update_id": 11,
                                "message": {
                                    "message_id": 501,
                                    "chat": {"id": 12345, "type": "private", "first_name": "Lead"},
                                    "from": {"is_bot": False},
                                    "reply_to_message": {"message_id": 102},
                                    "text": "go",
                                },
                            },
                            {
                                "update_id": 12,
                                "message": {
                                    "message_id": 502,
                                    "chat": {"id": 12345, "type": "private"},
                                    "from": {"is_bot": False},
                                    "text": "/status",
                                },
                            },
                            {
                                "update_id": 13,
                                "message": {
                                    "message_id": 503,
                                    "chat": {"id": 12345, "type": "private"},
                                    "from": {"is_bot": False},
                                    "text": "Also check bay 3",
                                },
                            },
                        ],
                    },
                )
            return httpx.Response(200, json={"ok": True, "result": []})

        return httpx.Response(404)

    async def _run() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        chats = await detect_owner_chat_id("tok_abc", client=client)
        assert len(chats) == 2

        poll_calls_reset = chats  # silence lint
        del poll_calls_reset

    # Reset poll_calls for main notifier test
    async def _run_notifier() -> None:
        nonlocal poll_calls
        poll_calls = 0
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        ntf = TelegramNotifier(TelegramConfig(enabled=True, bot_token="tok_abc", owner_chat_id="12345"), client=client)
        ch = ChannelConfig(id="ch1", adapter="feishu", external_chat_id="oc_1", name="Ops")
        th = ThreadRecord(id="th_1", channel_id="ch1", backend="mock", backend_thread_ref="r1", title="Bug")

        start_ids = await ntf.notify_start(ch, th, "bay 2 stuck")
        done_ids = await ntf.notify_completion(ch, th, ["Summary bubble", "Draft bubble"])
        assert start_ids == ["101"]
        assert done_ids == ["102", "103"]
        assert len(sent_payloads) == 3

        actions = await ntf.poll_owner_actions()
        assert len(actions) == 3  # stranger chat_id=999999 rejected
        assert actions[0].kind == "approve_go" and actions[0].reply_token == "102"
        assert actions[1].kind == "status"
        assert actions[2].kind == "steer" and actions[2].text == "Also check bay 3"

        # Offset advanced -> subsequent poll returns empty
        assert await ntf.poll_owner_actions() == []
        await client.aclose()

    asyncio.run(_run())
    asyncio.run(_run_notifier())


def test_imessage_notifier_backlog_echo_and_attributed_body() -> None:
    # Verify typedstream attributedBody extraction
    sample_blob = b"\x04\x0bstreamtyped\x81\xe8\x03NSString\x01\x94\x84\x01+\x0eCheck bay 2 OK\x86\x84\x02"
    assert extract_attributed_body_text(sample_blob) == "Check bay 2 OK"

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "chat.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT);
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                guid TEXT,
                text TEXT,
                attributedBody BLOB,
                is_from_me INTEGER DEFAULT 0,
                handle_id INTEGER,
                thread_originator_guid TEXT DEFAULT ''
            );
            CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
            INSERT INTO handle (ROWID, id) VALUES (1, '+15550100'), (2, '+15559999');
            INSERT INTO chat (ROWID, chat_identifier) VALUES (1, '+15550100'), (2, '+15559999');
            INSERT INTO message (ROWID, guid, text, is_from_me, handle_id) VALUES
                (1, 'g-old', 'old backlog message', 0, 1);
            INSERT INTO chat_message_join VALUES (1, 1);
            """
        )
        conn.commit()
        conn.close()

        sent_texts: list[tuple[str, str]] = []

        def fake_send(handle: str, body: str) -> bool:
            sent_texts.append((handle, body))
            c = sqlite3.connect(db_path)
            c.execute(
                "INSERT INTO message (ROWID, guid, text, is_from_me, handle_id) VALUES (?, ?, ?, 1, 1)",
                (len(sent_texts) + 10, f"g-out-{len(sent_texts)}", body),
            )
            c.execute("INSERT INTO chat_message_join VALUES (1, ?)", (len(sent_texts) + 10,))
            c.commit()
            c.close()
            return True

        async def _run() -> None:
            ntf = IMessageNotifier(
                IMessageConfig(enabled=True, owner_handle="+15550100", chat_db_path=str(db_path)),
                send_fn=fake_send,
            )
            # First poll seeds ROWID and skips old backlog
            assert await ntf.poll_owner_actions() == []

            ch = ChannelConfig(id="ch1", adapter="feishu", external_chat_id="oc_1", name="Ops")
            th = ThreadRecord(id="th_1", channel_id="ch1", backend="mock", backend_thread_ref="r1", title="Bug")
            toks = await ntf.notify_completion(ch, th, ["Node 2 down."])
            assert toks == ["g-out-1"]

            # Watchpost's own echo must be suppressed, while owner reply triggers
            c = sqlite3.connect(db_path)
            # Unrelated sender ignored
            c.execute("INSERT INTO message (ROWID, guid, text, is_from_me, handle_id) VALUES (20, 'g-stranger', 'go', 0, 2)")
            c.execute("INSERT INTO chat_message_join VALUES (2, 20)")
            # Owner swipe-reply using attributedBody (NULL text)
            c.execute(
                "INSERT INTO message (ROWID, guid, text, attributedBody, is_from_me, handle_id, thread_originator_guid) "
                "VALUES (21, 'g-in-1', NULL, ?, 0, 1, 'g-out-1')",
                (b"\x04\x0bNSString\x01\x94\x84\x01+\x02go\x86",),
            )
            c.execute("INSERT INTO chat_message_join VALUES (1, 21)")
            c.commit()
            c.close()

            actions = await ntf.poll_owner_actions()
            assert len(actions) == 1
            assert actions[0].kind == "approve_go"
            assert actions[0].reply_token == "g-out-1"

        asyncio.run(_run())


def test_telegram_notifier_media_buttons_typing_commands_and_persistent_offset() -> None:
    import json
    from watchpost.store import WatchpostStore

    seen_endpoints: list[str] = []
    sent_json: list[dict] = []
    photo_uploads: list[bytes] = []
    callbacks_answered: list[dict] = []
    markups_cleared: list[dict] = []
    typing_calls: list[dict] = []
    commands_calls: list[dict] = []
    poll_offsets: list[str | None] = []
    updates_queue: list[list[dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen_endpoints.append(path)
        if path.endswith("/sendMessage"):
            body = json.loads(request.content.decode("utf-8"))
            sent_json.append(body)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 300 + len(sent_json)}})
        if path.endswith("/sendPhoto"):
            photo_uploads.append(request.content)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 400 + len(photo_uploads)}})
        if path.endswith("/sendChatAction"):
            typing_calls.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"ok": True, "result": True})
        if path.endswith("/setMyCommands"):
            commands_calls.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"ok": True, "result": True})
        if path.endswith("/answerCallbackQuery"):
            callbacks_answered.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"ok": True, "result": True})
        if path.endswith("/editMessageReplyMarkup"):
            markups_cleared.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"ok": True, "result": True})
        if path.endswith("/getUpdates"):
            poll_offsets.append(request.url.params.get("offset"))
            batch = updates_queue.pop(0) if updates_queue else []
            return httpx.Response(200, json={"ok": True, "result": batch})
        if path.endswith("/getFile"):
            fid = request.url.params.get("file_id", "")
            mapping = {
                "f_photo_lg": "photos/snap.jpg",
                "f_voice_1": "voice/note.ogg",
                "f_doc_1": "docs/trace.log",
            }
            return httpx.Response(200, json={"ok": True, "result": {"file_path": mapping.get(fid, "other/raw.bin")}})
        if "/file/bot" in path:
            return httpx.Response(200, content=b"FILEBYTES-" + path.encode("utf-8"))
        return httpx.Response(404)

    async def _run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            img = root / "bay2.png"
            img.write_bytes(b"\x89PNG\r\n\x1a\nFAKEPNG")
            store = WatchpostStore(root / "wp.db")
            cfg = TelegramConfig(enabled=True, bot_token="tok_full", owner_chat_id="777")
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

            ntf = TelegramNotifier(cfg, store=store, media_dir=root / "media", language="zh", client=client)

            # 1. Seed backlog on cold start without returning owner actions
            updates_queue.append(
                [
                    {
                        "update_id": 50,
                        "message": {"message_id": 1, "chat": {"id": 777, "type": "private"}, "from": {"is_bot": False}, "text": "stale"},
                    }
                ]
            )
            assert await ntf.seed_offset() == 1
            assert store.get_kv("telegram_owner_update_offset") == 51

            # 2. Commands + typing
            assert await ntf.register_commands() is True
            assert len(commands_calls[0]["commands"]) == 8
            assert commands_calls[0]["commands"][0]["command"] == "status"
            await ntf.send_typing()
            assert typing_calls[-1] == {"chat_id": "777", "action": "typing"}

            # 3. Start with image attachment sends text + photo
            ch = ChannelConfig(id="ch_ops", adapter="feishu", external_chat_id="oc_ops", name="Ops")
            th_go = ThreadRecord(
                id="th_go_1",
                channel_id="ch_ops",
                backend="mock",
                backend_thread_ref="r1",
                title="Bay 2",
                pending_go=True,
            )
            start_ids = await ntf.notify_start(ch, th_go, "Bay 2 screenshot", attachments=[str(img)])
            assert start_ids == ["301", "401"]
            assert len(photo_uploads) == 1

            # 4. Completion attaches inline keyboard only to last bubble when pending_go=True
            done_ids = await ntf.notify_completion(ch, th_go, ["Summary part", "Needs confirmation"])
            assert done_ids == ["302", "303"]
            assert "reply_markup" not in sent_json[1]
            kb = sent_json[2]["reply_markup"]["inline_keyboard"][0]
            assert kb[0]["callback_data"] == "wp:go:th_go_1"
            assert kb[1]["callback_data"] == "wp:skip:th_go_1"

            # 5. Restarted TelegramNotifier resumes from persisted offset 51
            ntf2 = TelegramNotifier(cfg, store=store, media_dir=root / "media", language="zh", client=client)
            updates_queue.append(
                [
                    {
                        "update_id": 51,
                        "callback_query": {
                            "id": "cb_stranger",
                            "from": {"is_bot": False},
                            "data": "wp:go:th_go_1",
                            "message": {"message_id": 303, "chat": {"id": 999}},
                        },
                    },
                    {
                        "update_id": 52,
                        "callback_query": {
                            "id": "cb_owner",
                            "from": {"is_bot": False},
                            "data": "wp:go:th_go_1",
                            "message": {"message_id": 303, "chat": {"id": 777}},
                        },
                    },
                    {
                        "update_id": 53,
                        "message": {
                            "message_id": 801,
                            "chat": {"id": 777, "type": "private"},
                            "from": {"is_bot": False},
                            "caption": "Inspect this log & photo",
                            "photo": [
                                {"file_id": "f_photo_sm", "file_size": 100},
                                {"file_id": "f_photo_lg", "file_size": 2048},
                            ],
                        },
                    },
                    {
                        "update_id": 54,
                        "message": {
                            "message_id": 802,
                            "chat": {"id": 777, "type": "private"},
                            "from": {"is_bot": False},
                            "voice": {"file_id": "f_voice_1", "file_size": 512},
                        },
                    },
                    {
                        "update_id": 55,
                        "message": {
                            "message_id": 803,
                            "chat": {"id": 777, "type": "private"},
                            "from": {"is_bot": False},
                            "document": {"file_id": "f_doc_1", "file_name": "trace.log", "mime_type": "text/plain"},
                        },
                    },
                ]
            )
            actions = await ntf2.poll_owner_actions()
            assert poll_offsets[-1] == "51"
            assert store.get_kv("telegram_owner_update_offset") == 56
            assert len(actions) == 4
            assert actions[0].kind == "approve_go" and actions[0].thread_id == "th_go_1"
            assert callbacks_answered == [{"callback_query_id": "cb_owner", "text": "已批准执行"}]
            assert markups_cleared[0]["reply_markup"] == {"inline_keyboard": []}

            assert actions[1].kind == "steer" and actions[1].text == "Inspect this log & photo"
            assert actions[1].attachments[0]["is_audio"] == "false"
            assert Path(actions[1].attachments[0]["path"]).is_file()

            assert actions[2].kind == "steer" and actions[2].text == ""
            assert actions[2].attachments[0]["is_audio"] == "true"
            assert Path(actions[2].attachments[0]["path"]).is_file()

            assert actions[3].kind == "steer"
            assert actions[3].attachments[0]["name"] == "trace.log"
            assert actions[3].attachments[0]["is_audio"] == "false"

            await client.aclose()

    asyncio.run(_run())
