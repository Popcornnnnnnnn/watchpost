"""Offline unit tests for read-only FeishuAdapter using httpx.MockTransport."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import httpx

from watchpost.adapters.feishu import FeishuAdapter
from watchpost.config import FeishuConfig
from watchpost.models import ChannelConfig, ChatType


def test_feishu_adapter_auth_pagination_normalization_and_media() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(f"{request.method} {request.url.path}")
        path = request.url.path

        if path == "/open-apis/auth/v3/tenant_access_token/internal":
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t-secret-tok", "expire": 7200})

        assert request.headers.get("Authorization") == "Bearer t-secret-tok"

        if path == "/open-apis/im/v1/chats":
            page_token = request.url.params.get("page_token", "")
            if not page_token:
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "has_more": True,
                            "page_token": "p2",
                            "items": [{"chat_id": "oc_group_1", "name": "Ops Group", "chat_mode": "group"}],
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "has_more": False,
                        "items": [{"chat_id": "oc_dm_1", "name": "Alex DM", "chat_mode": "p2p"}],
                    },
                },
            )

        if path == "/open-apis/im/v1/messages":
            # Newest-first API response (adapter must reverse to chronological)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [
                            {
                                "message_id": "om_4_file",
                                "create_time": "1730000040000",
                                "msg_type": "file",
                                "deleted": False,
                                "sender": {"id": "ou_alex", "sender_type": "user"},
                                "body": {
                                    "content": json.dumps(
                                        {"file_key": "file_k1", "file_name": "../../etc/evil.log"}
                                    )
                                },
                            },
                            {
                                "message_id": "om_3_recalled",
                                "create_time": "1730000030000",
                                "msg_type": "text",
                                "deleted": True,
                                "sender": {"id": "ou_alex", "sender_type": "user"},
                                "body": {"content": json.dumps({"text": "secret"})},
                            },
                            {
                                "message_id": "om_2_post",
                                "create_time": "1730000020000",
                                "parent_id": "om_1_text",
                                "msg_type": "post",
                                "sender": {"id": "ou_alex", "sender_type": "user"},
                                "body": {
                                    "content": json.dumps(
                                        {
                                            "zh_cn": {
                                                "title": "Fault Report",
                                                "content": [
                                                    [
                                                        {"tag": "text", "text": "See screenshot: "},
                                                        {"tag": "img", "image_key": "img_post_1"},
                                                    ]
                                                ],
                                            }
                                        }
                                    )
                                },
                            },
                            {
                                "message_id": "om_1_text",
                                "create_time": "1730000010000",
                                "msg_type": "text",
                                "sender": {"id": "ou_alex", "sender_type": "user"},
                                "mentions": [{"id": "ou_owner", "name": "Lead", "key": "@_user_1"}],
                                "body": {"content": json.dumps({"text": "@_user_1 check worker 2"})},
                            },
                        ]
                    },
                },
            )

        if path == "/open-apis/im/v1/messages/om_missing":
            return httpx.Response(404, json={"code": 230002, "msg": "not found"})

        if path == "/open-apis/im/v1/messages/om_parent":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "message": {
                            "message_id": "om_parent",
                            "create_time": "1730000005000",
                            "msg_type": "audio",
                            "sender": {"id": "ou_owner", "sender_type": "user"},
                            "body": {"content": json.dumps({"file_key": "aud_1"})},
                        }
                    },
                },
            )

        if path == "/open-apis/im/v1/messages/om_4_file/resources/file_k1":
            return httpx.Response(200, content=b"LOG_BYTES", headers={"content-type": "text/plain"})

        return httpx.Response(500, json={"error": f"unmocked path {path}"})

    async def _run() -> None:
        cfg = FeishuConfig(
            enabled=True,
            auth_mode="tenant",
            app_id="app_1",
            app_secret="sec_1",
            owner_open_id="ou_owner",
            owner_name="Lead",
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = FeishuAdapter(cfg, client=client)

        chats = await adapter.list_available_chats()
        assert len(chats) == 2
        assert chats[0]["chat_type"] == "group"
        assert chats[1]["chat_type"] == "p2p"

        ch = ChannelConfig(
            id="ch_feishu_1",
            adapter="feishu",
            external_chat_id="oc_group_1",
            name="Ops Group",
            chat_type=ChatType.group,
        )
        msgs = await adapter.fetch_recent(ch, limit=20)
        assert [m.message_id for m in msgs] == ["om_1_text", "om_2_post", "om_3_recalled", "om_4_file"]
        assert msgs[0].mentions_owner is True
        assert "@Lead check worker 2" in msgs[0].clean_text
        assert msgs[1].reply_to_id == "om_1_text"
        assert "Fault Report" in msgs[1].clean_text
        assert msgs[1].media_refs[0].key == "img_post_1"
        assert msgs[2].deleted is True and msgs[2].clean_text == "[Recalled]"
        assert msgs[3].media_refs[0].kind == "file"

        fetched = await adapter.fetch_by_ids(ch, ["om_missing", "om_parent"])
        assert [m.message_id for m in fetched] == ["om_parent"]
        assert fetched[0].is_owner is True
        assert fetched[0].media_refs[0].kind == "audio"

        with tempfile.TemporaryDirectory() as tmp:
            arts = await adapter.download_media(ch, msgs[3], Path(tmp) / "dl")
            assert len(arts) == 1
            out_file = Path(arts[0].path)
            assert out_file.is_file()
            assert out_file.parent.name == "dl"
            assert out_file.read_bytes() == b"LOG_BYTES"

        # Tenant token cached across all calls, zero read-receipt or outbound write calls
        assert seen_paths.count("POST /open-apis/auth/v3/tenant_access_token/internal") == 1
        assert all("/read_users" not in p and "POST /open-apis/im/v1/messages" not in p for p in seen_paths)

        await client.aclose()

    asyncio.run(_run())
