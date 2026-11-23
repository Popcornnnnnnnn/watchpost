"""Offline tests for DingTalk, WeCom webhooks, WhatsApp SDK plugin, daemon wiring, and setup."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sys
import time
from typing import Any

from fastapi.testclient import TestClient
import httpx
import pytest

from watchpost.adapters.dingtalk import DingTalkAdapter, verify_dingtalk_signature
from watchpost.adapters.telegram import TelegramInboundAdapter
from watchpost.adapters.wecom import WeComAdapter, parse_wecom_xml, verify_wecom_signature
from watchpost.backends.mock import MockAgentBackend
from watchpost.config import (
    BackendConfig,
    BarkConfig,
    DingTalkConfig,
    DiscordConfig,
    NtfyConfig,
    PushoverConfig,
    SlackConfig,
    TelegramConfig,
    WatchpostConfig,
    WeComConfig,
    resolve_config,
)
from watchpost.daemon import WatchpostDaemon, build_engine_from_config, run_doctor
from watchpost.engine import WatchpostEngine
from watchpost.models import ChannelConfig
from watchpost.notifiers.wecom import WeComNotifier
from watchpost.plugins import check_plugin
from watchpost.server import create_app
from watchpost.setup_wizard import run_setup_wizard
from watchpost.store import WatchpostStore


def _sign_dingtalk(ts: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), f"{ts}\n{secret}".encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _sign_wecom(token: str, timestamp: str, nonce: str, extra: str = "") -> str:
    parts = [token, timestamp, nonce]
    if extra:
        parts.append(extra)
    return hashlib.sha1("".join(sorted(parts)).encode("utf-8")).hexdigest()


def test_dingtalk_signature_ingest_and_media_download(tmp_path: Path) -> None:
    now_ms = int(time.time() * 1000)
    ts = str(now_ms)
    secret = "dt-secret-key"
    sig = _sign_dingtalk(ts, secret)
    assert verify_dingtalk_signature(ts, sig, secret, now_ms=now_ms)
    assert not verify_dingtalk_signature(ts, "bad-sig", secret, now_ms=now_ms)
    assert not verify_dingtalk_signature(str(now_ms - 5_000_000), sig, secret, now_ms=now_ms)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1.0/oauth2/accessToken"):
            return httpx.Response(200, json={"accessToken": "dt-tok-1", "expireIn": 7200})
        if request.url.path.endswith("/v1.0/robot/messageFiles/download"):
            assert request.headers.get("x-acs-dingtalk-access-token") == "dt-tok-1"
            return httpx.Response(200, json={"downloadUrl": "https://dl.dingtalk.test/pic.png"})
        if str(request.url) == "https://dl.dingtalk.test/pic.png":
            return httpx.Response(200, content=b"DTPNG")
        return httpx.Response(404)

    inbox = tmp_path / "dingtalk.jsonl"
    cfg = DingTalkConfig(
        enabled=True,
        app_key="ak1",
        app_secret="as1",
        robot_code="rc1",
        webhook_secret=secret,
        owner_user_id="staff_owner",
    )

    async def _run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = DingTalkAdapter(cfg, inbox_path=inbox, client=client)
            msg = adapter.ingest_webhook_payload(
                {
                    "conversationId": "cid_ops",
                    "conversationType": "2",
                    "conversationTitle": "Ops Alert Group",
                    "msgId": "dt_msg_1",
                    "createAt": now_ms,
                    "senderStaffId": "staff_alice",
                    "senderNick": "Alice",
                    "msgtype": "richText",
                    "content": {
                        "richText": [
                            {"text": "Node #2 offline"},
                            {"pictureDownloadCode": "code_img_1"},
                        ]
                    },
                    "atUsers": [{"staffId": "staff_owner", "name": "Owner"}],
                }
            )
            assert msg.mentions_owner is True
            assert msg.media_refs[0].key == "code_img_1"

            reloaded_adapter = DingTalkAdapter(cfg, inbox_path=inbox, client=client)
            chats = await reloaded_adapter.list_available_chats()
            assert chats == [{"external_chat_id": "cid_ops", "name": "Ops Alert Group", "chat_type": "group"}]

            ch = ChannelConfig(id="dt_ops", adapter="dingtalk", external_chat_id="cid_ops", name="Ops")
            fetched = await reloaded_adapter.fetch_recent(ch)
            assert [m.message_id for m in fetched] == ["dt_msg_1"]
            by_ids = await reloaded_adapter.fetch_by_ids(ch, ["dt_msg_1"])
            assert len(by_ids) == 1

            arts = await reloaded_adapter.download_media(ch, fetched[0], tmp_path / "dt_media")
            assert len(arts) == 1 and Path(arts[0].path).read_bytes() == b"DTPNG"

    asyncio.run(_run())


def test_wecom_security_ingest_media_and_notifier(tmp_path: Path) -> None:
    assert verify_wecom_signature("wc_tok", "1700000", "n1", _sign_wecom("wc_tok", "1700000", "n1", "echo"), "echo")
    assert not verify_wecom_signature("wc_tok", "1700000", "n1", "wrong", "echo")

    with pytest.raises(ValueError, match="DOCTYPE"):
        parse_wecom_xml("<!DOCTYPE foo [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]><xml><MsgType>text</MsgType></xml>")

    with pytest.raises(ValueError, match="Encrypted"):
        parse_wecom_xml("<xml><Encrypt>abcd</Encrypt></xml>")

    sent_payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/gettoken"):
            return httpx.Response(200, json={"errcode": 0, "access_token": "wc-acc-1", "expires_in": 7200})
        if request.url.path.endswith("/media/get"):
            assert request.url.params.get("media_id") == "MED_1"
            return httpx.Response(200, content=b"AMR_AUDIO", headers={"content-type": "audio/amr"})
        if request.url.path.endswith("/message/send"):
            sent_payloads.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"errcode": 0, "msgid": "wc_sent_1"})
        return httpx.Response(404)

    inbox = tmp_path / "wecom.jsonl"
    cfg = WeComConfig(
        enabled=True,
        notify_enabled=True,
        corp_id="ww123",
        corp_secret="sec456",
        agent_id=1000002,
        callback_token="wc_tok",
        owner_user_id="zhangsan",
        owner_name="ZhangSan",
    )

    async def _run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = WeComAdapter(cfg, inbox_path=inbox, client=client)
            msg = adapter.ingest_webhook_payload(
                "<xml>"
                "<FromUserName>lisi</FromUserName>"
                "<ChatId>wr_ops</ChatId>"
                "<CreateTime>1700000500</CreateTime>"
                "<MsgType>voice</MsgType>"
                "<MediaId>MED_1</MediaId>"
                "<MsgId>wc_m1</MsgId>"
                "<MentionedList>zhangsan</MentionedList>"
                "</xml>"
            )
            assert msg.mentions_owner is True
            ch = ChannelConfig(id="wc_1", adapter="wecom", external_chat_id="wr_ops", name="WeCom Ops")
            fetched = await adapter.fetch_recent(ch)
            assert [m.message_id for m in fetched] == ["wc_m1"]
            arts = await adapter.download_media(ch, fetched[0], tmp_path / "wc_media")
            assert len(arts) == 1 and Path(arts[0].path).read_bytes() == b"AMR_AUDIO"

            notifier = WeComNotifier(cfg, client=client)
            tok = await notifier.send_message("Deployment ready for review")
            assert tok == "wc_sent_1"
            assert sent_payloads[0]["agentid"] == 1000002
            assert sent_payloads[0]["touser"] == "zhangsan"

    asyncio.run(_run())


def test_server_webhook_routes_fail_closed_and_ingest(tmp_path: Path) -> None:
    store = WatchpostStore(tmp_path / "wp.db")
    engine = WatchpostEngine(store, MockAgentBackend())

    dt_secret = "dt_webhook_sec"
    wc_token = "wc_callback_tok"
    dt_adapter = DingTalkAdapter(
        DingTalkConfig(enabled=True, webhook_secret=dt_secret),
        inbox_path=tmp_path / "dt.jsonl",
    )
    wc_adapter = WeComAdapter(
        WeComConfig(enabled=True, callback_token=wc_token),
        inbox_path=tmp_path / "wc.jsonl",
    )
    engine.register_adapter(dt_adapter)
    engine.register_adapter(wc_adapter)

    app = create_app(store.db_path, engine=engine)
    client = TestClient(app)

    bad_dt = client.post(
        "/api/webhooks/dingtalk",
        headers={"timestamp": str(int(time.time() * 1000)), "sign": "invalid"},
        json={"conversationId": "c1", "msgId": "m1"},
    )
    assert bad_dt.status_code == 401

    now_ts = str(int(time.time() * 1000))
    ok_dt = client.post(
        "/api/webhooks/dingtalk",
        headers={"timestamp": now_ts, "sign": _sign_dingtalk(now_ts, dt_secret)},
        json={
            "conversationId": "c_dt",
            "msgId": "dt_wh_1",
            "senderNick": "Eve",
            "msgtype": "text",
            "text": {"content": "Timeout on gateway"},
        },
    )
    assert ok_dt.status_code == 200
    assert ok_dt.json() == {"ok": True, "message_id": "dt_wh_1"}

    async def _check_inboxes() -> None:
        dt_msgs = await dt_adapter.fetch_recent(
            ChannelConfig(id="ch_dt", adapter="dingtalk", external_chat_id="c_dt", name="DT")
        )
        assert [m.message_id for m in dt_msgs] == ["dt_wh_1"]

        bad_echo = client.get(
            "/api/webhooks/wecom",
            params={"msg_signature": "bad", "timestamp": "123", "nonce": "abc", "echostr": "HELLO"},
        )
        assert bad_echo.status_code == 401

        ok_sig = _sign_wecom(wc_token, "123", "abc", "HELLO")
        ok_echo = client.get(
            "/api/webhooks/wecom",
            params={"msg_signature": ok_sig, "timestamp": "123", "nonce": "abc", "echostr": "HELLO"},
        )
        assert ok_echo.status_code == 200 and ok_echo.text == "HELLO"

        post_sig = _sign_wecom(wc_token, "123", "abc")
        bad_wc = client.post(
            "/api/webhooks/wecom",
            params={"msg_signature": "nope", "timestamp": "123", "nonce": "abc"},
            content=b"<xml><FromUserName>u1</FromUserName><MsgType>text</MsgType><Content>hi</Content></xml>",
        )
        assert bad_wc.status_code == 401

        ok_wc = client.post(
            "/api/webhooks/wecom",
            params={"msg_signature": post_sig, "timestamp": "123", "nonce": "abc"},
            content=(
                b"<xml><FromUserName>u1</FromUserName><ChatId>wc_room</ChatId>"
                b"<MsgId>wc_wh_1</MsgId><MsgType>text</MsgType><Content>DB pool full</Content></xml>"
            ),
        )
        assert ok_wc.status_code == 200 and ok_wc.json() == {"ok": True, "message_id": "wc_wh_1"}
        wc_msgs = await wc_adapter.fetch_recent(
            ChannelConfig(id="ch_wc", adapter="wecom", external_chat_id="wc_room", name="WC")
        )
        assert [m.message_id for m in wc_msgs] == ["wc_wh_1"]

    asyncio.run(_check_inboxes())


def test_daemon_wiring_reload_doctor_and_setup_wizard(tmp_path: Path) -> None:
    resolved = resolve_config(
        WatchpostConfig(),
        env={
            "SLACK_BOT_TOKEN": "xoxb-env",
            "DISCORD_BOT_TOKEN": "disc-env",
            "DINGTALK_WEBHOOK_SECRET": "dt-env",
            "WECOM_CORP_ID": "wc-env",
            "WECOM_AGENT_ID": "1000009",
            "NTFY_TOPIC": "wp-env",
            "BARK_DEVICE_KEY": "bk-env",
            "PUSHOVER_APP_TOKEN": "po-app",
            "PUSHOVER_USER_KEY": "po-usr",
        },
    )
    assert resolved.slack.bot_token == "xoxb-env"
    assert resolved.discord.bot_token == "disc-env"
    assert resolved.dingtalk.webhook_secret == "dt-env"
    assert resolved.wecom.corp_id == "wc-env" and resolved.wecom.agent_id == 1000009
    assert resolved.ntfy.topic == "wp-env"
    assert resolved.bark.device_key == "bk-env"
    assert resolved.pushover.app_token == "po-app"

    cfg_path = tmp_path / "config.json"
    cfg_data = WatchpostConfig(
        db_path=str(tmp_path / "wp.db"),
        workspace_dir=str(tmp_path),
        backend=BackendConfig(preset="mock"),
        telegram=TelegramConfig(enabled=True, inbound_enabled=True, bot_token="shared-tg-tok", owner_chat_id="901"),
        slack=SlackConfig(enabled=True, dm_enabled=True, bot_token="xoxb-1", owner_user_id="U1"),
        discord=DiscordConfig(enabled=True, dm_enabled=True, bot_token="disc-1", owner_user_id="D1"),
        dingtalk=DingTalkConfig(enabled=True, webhook_secret="dt-sec"),
        wecom=WeComConfig(enabled=True, notify_enabled=True, corp_id="ww1", corp_secret="sec", agent_id=1000002, callback_token="wc-tok", owner_user_id="owner1"),
        ntfy=NtfyConfig(enabled=True, topic="wp-alerts"),
        bark=BarkConfig(enabled=True, device_key="bark1"),
        pushover=PushoverConfig(enabled=True, app_token="poa", user_key="pou"),
    )
    cfg_path.write_text(json.dumps(cfg_data.model_dump(mode="json")), encoding="utf-8")

    loaded = resolve_config(cfg_data)
    engine = build_engine_from_config(loaded)
    assert {"slack", "discord", "telegram", "dingtalk", "wecom"} <= set(engine.adapters.keys())
    tg_adapter = engine.adapters["telegram"]
    assert isinstance(tg_adapter, TelegramInboundAdapter)
    tg_notifier = next(n for n in engine.notifiers if n.name == "telegram")
    assert tg_notifier.hub is tg_adapter.hub
    assert {"telegram", "slack", "discord", "wecom", "ntfy", "bark", "pushover"} <= {n.name for n in engine.notifiers}

    doc = run_doctor(cfg_path)
    assert doc["ok"] is True
    assert doc["checks"]["slack"]["ok"] is True
    assert doc["checks"]["ntfy"]["ok"] is True
    assert doc["checks"]["bark"]["ok"] is True
    assert doc["checks"]["pushover"]["ok"] is True

    async def _run_reload() -> None:
        daemon = WatchpostDaemon(engine, loaded, config_path=cfg_path)
        cfg_updated = cfg_data.model_copy(update={"pushover": PushoverConfig(enabled=False)})
        cfg_path.write_text(json.dumps(cfg_updated.model_dump(mode="json")), encoding="utf-8")
        reload_res = await daemon.reload_from_disk()
        assert reload_res["reloaded"] is True
        assert "pushover" not in {n.name for n in daemon.engine.notifiers}
        assert daemon.engine.adapters["telegram"].hub is tg_adapter.hub
        await daemon.close_resources()

    asyncio.run(_run_reload())

    wiz_cfg = tmp_path / "wiz_config.json"
    rc, saved = run_setup_wizard(
        config_path=wiz_cfg,
        interactive=False,
        defaults={
            "workspace": str(tmp_path),
            "backend": "mock",
            "backup": "none",
            "platform": "slack",
            "notifier": "ntfy",
        },
        print_fn=lambda _msg: None,
    )
    assert rc == 0 and saved == wiz_cfg
    raw_txt = wiz_cfg.read_text(encoding="utf-8")
    assert "${SLACK_BOT_TOKEN}" in raw_txt and "${NTFY_TOPIC}" in raw_txt


def test_whatsapp_plugin_template_passes_sdk_conformance() -> None:
    script = Path(__file__).resolve().parents[1] / "examples" / "whatsapp_plugin_template.py"
    res = asyncio.run(
        check_plugin(
            kind="channel",
            command=[sys.executable, str(script)],
            test_fetch=True,
        )
    )
    assert res["ok"] is True, res
    fetch_chk = next(c for c in res["checks"] if c["name"] == "fetch-recent")
    assert "validated 1 NormalizedMessage" in fetch_chk["detail"], fetch_chk
