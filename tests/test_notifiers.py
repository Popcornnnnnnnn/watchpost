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
