"""Tests for watchpost.sdk, ExternalCommandNotifier, plugin scaffolder/checker CLI, and setup wizard."""

from __future__ import annotations

import asyncio
import io
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from watchpost.adapters.external_plugin import ExternalCommandAdapter
from watchpost.cli import main as cli_main
from watchpost.config import (
    BackendConfig,
    ExternalAdapterConfig,
    ExternalNotifierConfig,
    WatchpostConfig,
    load_config,
    load_raw_config,
    save_config,
)
from watchpost.daemon import build_engine_from_config, run_doctor
from watchpost.models import ChannelConfig, ChatType, ThreadRecord, ThreadStatus, TriggerMode
from watchpost.notifiers.external_plugin import ExternalCommandNotifier
from watchpost.plugins import check_plugin
from watchpost.sdk import (
    ChannelPlugin,
    make_chat,
    make_message,
    run_channel_plugin,
)
from watchpost.setup_wizard import run_setup_wizard

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CHANNEL = REPO_ROOT / "examples" / "channel_plugin_template.py"
EXAMPLE_NOTIFIER = REPO_ROOT / "examples" / "notifier_plugin_template.py"


def test_sdk_channel_plugin_and_external_adapter_roundtrip(tmp_path: Path) -> None:
    class _InMemChannel(ChannelPlugin):
        name = "mem_chat"

        def list_chats(self) -> list[dict[str, str]]:
            return [make_chat("room_1", "Ops Room", chat_type="group")]

        def fetch_recent(self, channel: dict[str, Any], limit: int = 50) -> list[dict[str, Any]]:
            del channel, limit
            return [
                make_message(
                    message_id="m_1",
                    sender_id="u_1",
                    sender_name="Teammate",
                    clean_text="@owner check queue",
                    mentions_owner=True,
                    position=1,
                )
            ]

        def fetch_by_ids(self, channel: dict[str, Any], message_ids: list[str]) -> list[dict[str, Any]]:
            del channel
            return [
                make_message(message_id=mid, sender_name="Teammate", clean_text="parent")
                for mid in message_ids
            ]

        def download_media(
            self,
            channel: dict[str, Any],
            message: dict[str, Any],
            dest_dir: Path,
        ) -> list[dict[str, Any]]:
            del channel, message, dest_dir
            return [{"kind": "image", "key": "k1", "path": str(tmp_path / "img.png")}]

    plugin = _InMemChannel()
    for action, stdin_obj, key in (
        ("health", {}, "ok"),
        ("list-chats", {}, "chats"),
        ("fetch-recent", {"channel": {"external_chat_id": "room_1"}, "limit": 5}, "messages"),
        ("fetch-by-ids", {"channel": {"external_chat_id": "room_1"}, "message_ids": ["m_0"]}, "messages"),
        ("download-media", {"channel": {}, "message": {}, "dest_dir": str(tmp_path)}, "artifacts"),
    ):
        out = io.StringIO()
        err = io.StringIO()
        rc = run_channel_plugin(
            plugin,
            argv=[action],
            stdin=io.StringIO(json.dumps(stdin_obj)),
            stdout=out,
            stderr=err,
        )
        assert rc == 0, err.getvalue()
        parsed = json.loads(out.getvalue())
        assert key in parsed

    bad_err = io.StringIO()
    assert run_channel_plugin(plugin, argv=["bogus"], stdin=io.StringIO("{}"), stdout=io.StringIO(), stderr=bad_err) == 2

    # Full subprocess ExternalCommandAdapter against example template + JSONL inbox
    inbox = tmp_path / "inbox.jsonl"
    inbox.write_text(
        json.dumps(
            {
                "chat_id": "c_ext",
                "chat_name": "Ext Team",
                "chat_type": "p2p",
                "message_id": "ext_m1",
                "sender_id": "peer_1",
                "sender_name": "Alex",
                "text": "worker timeout on step 2",
                "position": 10,
                "create_ms": 1730000000000,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    async def _run() -> None:
        adapter = ExternalCommandAdapter(
            ExternalAdapterConfig(
                name="jsonl_example",
                command=[sys.executable, str(EXAMPLE_CHANNEL)],
                env={"WATCHPOST_EXAMPLE_INBOX": str(inbox)},
            )
        )
        h = await adapter.health()
        assert h["ok"] is True
        chats = await adapter.list_available_chats()
        assert chats == [{"external_chat_id": "c_ext", "name": "Ext Team", "chat_type": "p2p"}]
        ch = ChannelConfig(
            id="ch_ext",
            adapter="jsonl_example",
            external_chat_id="c_ext",
            name="Ext Team",
            chat_type=ChatType.p2p,
            trigger_mode=TriggerMode.all,
        )
        msgs = await adapter.fetch_recent(ch, limit=10)
        assert len(msgs) == 1
        assert msgs[0].message_id == "ext_m1"
        assert msgs[0].clean_text == "worker timeout on step 2"

    asyncio.run(_run())


def test_external_command_notifier_roundtrip_and_daemon_wiring(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox.jsonl"
    actions_file = tmp_path / "actions.jsonl"
    actions_file.write_text(
        "\n".join(
            [
                json.dumps({"text": "#1 go", "reply_token": "tok_prev"}),
                json.dumps({"text": "/brief"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    async def _run() -> None:
        ntf = ExternalCommandNotifier(
            ExternalNotifierConfig(
                name="jsonl_notifier",
                command=[sys.executable, str(EXAMPLE_NOTIFIER)],
                env={
                    "WATCHPOST_EXAMPLE_OUTBOX": str(outbox),
                    "WATCHPOST_EXAMPLE_ACTIONS": str(actions_file),
                },
            )
        )
        h = await ntf.health()
        assert h["ok"] is True

        ch = ChannelConfig(
            id="ch_1",
            adapter="feishu",
            external_chat_id="oc_1",
            name="Support",
            chat_type=ChatType.p2p,
        )
        th = ThreadRecord(
            id="th_1",
            channel_id="ch_1",
            backend="mock",
            backend_thread_ref="ref_1",
            sender_name="Alex",
            title="Timeout",
            status=ThreadStatus.active,
        )
        start_toks = await ntf.notify_start(ch, th, "worker failing", is_steer=False)
        comp_toks = await ntf.notify_completion(ch, th, ["Summary bubble", "Draft bubble"])
        direct_tok = await ntf.send_message("Direct note")
        assert len(start_toks) >= 2
        assert len(comp_toks) == 2
        assert direct_tok.startswith("tok_")

        lines = [json.loads(line) for line in outbox.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert any("Summary bubble" == item["text"] for item in lines)
        assert any("Direct note" == item["text"] for item in lines)

        acts = await ntf.poll_owner_actions()
        assert len(acts) == 2
        assert acts[0].notifier == "jsonl_notifier"
        assert acts[0].kind == "approve_go" and acts[0].list_index == 1 and acts[0].reply_token == "tok_prev"
        assert acts[1].kind == "brief"
        # Inbox cleared after consumption
        assert await ntf.poll_owner_actions() == []

    asyncio.run(_run())

    # Daemon wiring + doctor validation
    cfg_path = tmp_path / "config.json"
    cfg = WatchpostConfig(
        db_path=str(tmp_path / "wp.db"),
        media_dir=str(tmp_path / "media"),
        workspace_dir=str(tmp_path),
        backend=BackendConfig(preset="mock"),
        external_notifiers=[
            ExternalNotifierConfig(name="jsonl_notifier", command=[sys.executable, str(EXAMPLE_NOTIFIER)]),
        ],
    )
    save_config(cfg, cfg_path)
    engine = build_engine_from_config(load_config(cfg_path))
    assert any(n.name == "jsonl_notifier" for n in engine.notifiers)
    doc = run_doctor(cfg_path)
    assert doc["ok"] is True
    assert doc["checks"]["notifier:jsonl_notifier"]["ok"] is True

    bad_cfg_path = tmp_path / "bad.json"
    save_config(
        WatchpostConfig(
            db_path=str(tmp_path / "wp2.db"),
            media_dir=str(tmp_path / "media2"),
            workspace_dir=str(tmp_path),
            backend=BackendConfig(preset="mock"),
            external_notifiers=[
                ExternalNotifierConfig(name="missing_ntf", command=[str(tmp_path / "no-such-bin")]),
            ],
        ),
        bad_cfg_path,
    )
    bad_doc = run_doctor(bad_cfg_path)
    assert bad_doc["ok"] is False
    assert bad_doc["checks"]["notifier:missing_ntf"]["ok"] is False


def test_plugins_scaffold_check_and_list_cli(tmp_path: Path) -> None:
    ch_dir = tmp_path / "ch_plug"
    ntf_dir = tmp_path / "ntf_plug"

    assert cli_main(["plugins", "scaffold", "--kind", "channel", "--name", "slack_demo", "--dest", str(ch_dir)]) == 0
    assert cli_main(["plugins", "scaffold", "--kind", "notifier", "--name", "ntfy_demo", "--dest", str(ntf_dir)]) == 0
    assert (ch_dir / "README.md").is_file()
    assert (ntf_dir / "README.md").is_file()
    assert stat.S_IMODE((ch_dir / "plugin.py").stat().st_mode) & stat.S_IXUSR

    # Scaffolded plugins and shipped example templates must all pass `plugins check`
    for kind, script in (
        ("channel", ch_dir / "plugin.py"),
        ("notifier", ntf_dir / "plugin.py"),
        ("channel", EXAMPLE_CHANNEL),
        ("notifier", EXAMPLE_NOTIFIER),
    ):
        chk_res = asyncio.run(
            check_plugin(kind=kind, command=[sys.executable, str(script)], test_fetch=(kind == "channel"))
        )
        assert chk_res["ok"] is True, chk_res
        cli_args = ["plugins", "check", "--kind", kind, "--cmd", sys.executable, str(script)]
        if kind == "channel":
            cli_args.append("--test-fetch")
        assert cli_main(cli_args) == 0

    # plugins list CLI
    cfg_path = tmp_path / "cfg.json"
    save_config(
        WatchpostConfig(
            external_adapters=[ExternalAdapterConfig(name="slack_demo", command=[sys.executable, str(ch_dir / "plugin.py")])],
            external_notifiers=[ExternalNotifierConfig(name="ntfy_demo", command=[sys.executable, str(ntf_dir / "plugin.py")])],
        ),
        cfg_path,
    )
    assert cli_main(["plugins", "list", "--config", str(cfg_path), "--json"]) == 0


def test_setup_wizard_interactive_and_non_interactive_and_init_config_flag(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    # 1. Interactive setup wizard via mocked prompts
    cfg_interactive = tmp_path / "interactive.json"
    answers = iter(
        [
            str(tmp_path),  # workspace
            "en",  # language
            "mock",  # backend
            "none",  # backup
            "feishu-api",  # platform
            "telegram",  # notifier
            "y",  # quiet hours enabled
            "23:00",  # quiet start
            "08:00",  # quiet end
            "8787",  # api port
        ]
    )
    logs: list[str] = []
    rc, saved = run_setup_wizard(
        config_path=cfg_interactive,
        interactive=True,
        input_fn=lambda _p: next(answers),
        print_fn=logs.append,
        defaults={"db_path": str(tmp_path / "i.db"), "media_dir": str(tmp_path / "i_media")},
    )
    assert rc == 0 and saved == cfg_interactive
    assert stat.S_IMODE(cfg_interactive.stat().st_mode) == 0o600
    raw = load_raw_config(cfg_interactive)
    assert raw.language == "en"
    assert raw.backend.preset == "mock"
    assert raw.feishu.app_id == "${FEISHU_APP_ID}"
    assert raw.telegram.bot_token == "${TELEGRAM_BOT_TOKEN}"
    assert raw.quiet_hours.enabled is True
    assert raw.quiet_hours.start == "23:00" and raw.quiet_hours.end == "08:00"
    assert raw.api_port == 8787
    assert (tmp_path / "i.db").is_file()

    # 2. Non-interactive `watchpost setup` CLI + overwrite protection
    cfg_cli = tmp_path / "cli_setup.json"
    setup_args = [
        "setup",
        "--non-interactive",
        "--config",
        str(cfg_cli),
        "--workspace",
        str(tmp_path),
        "--lang",
        "zh",
        "--backend",
        "mock",
        "--backup",
        "none",
        "--platform",
        "none",
        "--notifier",
        "imessage",
        "--quiet-start",
        "23:00",
        "--quiet-end",
        "08:00",
        "--api-port",
        "8791",
        "--db",
        str(tmp_path / "c.db"),
        "--media-dir",
        str(tmp_path / "c_media"),
    ]
    assert cli_main(setup_args) == 0
    assert cli_main(setup_args) == 1  # refuses overwrite without --force
    assert cli_main([*setup_args, "--force"]) == 0
    raw_cli = load_raw_config(cfg_cli)
    assert raw_cli.imessage.enabled is True
    assert raw_cli.imessage.owner_handle == "${WATCHPOST_IMESSAGE_HANDLE}"
    assert raw_cli.imessage.personal_workspace_dir == str(tmp_path)
    assert raw_cli.api_port == 8791

    # 3. `watchpost init-config --interactive` runs wizard while plain `init-config` stays non-interactive
    cfg_init_i = tmp_path / "init_i.json"
    init_answers = iter([str(tmp_path), "zh", "mock", "none", "none", "none", "n", "0"])
    monkeypatch.setattr("builtins.input", lambda _p: next(init_answers))
    monkeypatch.setattr("watchpost.setup_wizard.DEFAULT_DB_PATH", str(tmp_path / "init_i.db"))
    monkeypatch.setattr("watchpost.setup_wizard.DEFAULT_MEDIA_DIR", str(tmp_path / "init_i_media"))
    assert cli_main(["init-config", "--interactive", "--config", str(cfg_init_i)]) == 0
    assert load_raw_config(cfg_init_i).quiet_hours.enabled is False
    assert load_raw_config(cfg_init_i).personal_workspace_dir == str(tmp_path)


def test_telegram_setup_preserves_channels_uploads_avatar_and_hot_swaps_daemon(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    import httpx
    from watchpost.cli import run_telegram_setup
    from watchpost.config import BackendConfig, IMessageConfig, TelegramConfig
    from watchpost.daemon import WatchpostDaemon, build_engine_from_config
    from watchpost.engine import WatchpostEngine
    from watchpost.notifiers import classify_owner_command
    from watchpost.setup_wizard import detect_environment

    assert detect_environment()["recommended_notifier"] == "telegram"

    cfg_path = tmp_path / "watchpost.json"
    existing_ch = ChannelConfig(
        id="feishu_ops",
        adapter="feishu",
        external_chat_id="oc_ops_99",
        name="Ops Support",
        chat_type=ChatType.group,
        trigger_mode=TriggerMode.mention_owner,
    )
    save_config(
        WatchpostConfig(
            db_path=str(tmp_path / "wp.db"),
            media_dir=str(tmp_path / "media"),
            workspace_dir=str(tmp_path),
            language="zh",
            api_port=0,
            backend=BackendConfig(preset="mock"),
            imessage=IMessageConfig(
                enabled=True,
                owner_handle="+15550100",
                personal_workspace_dir=str(tmp_path),
                personal_session_ttl_s=3600,
                ack_on_receive=False,
            ),
            channels=[existing_ch],
        ),
        cfg_path,
    )

    seen_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen_requests.append(path)
        if path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 123, "username": "wp_live_bot", "first_name": "Watchpost"}})
        if path.endswith("/getUpdates"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {"update_id": 77, "message": {"message_id": 1, "chat": {"id": 555123, "type": "private"}, "text": "/start"}}
                    ],
                },
            )
        if any(
            path.endswith(ep)
            for ep in ("/setMyCommands", "/setMyShortDescription", "/setMyDescription", "/setMyProfilePhoto", "/sendMessage")
        ):
            if path.endswith("/sendMessage"):
                return httpx.Response(200, json={"ok": True, "result": {"message_id": 900}})
            return httpx.Response(200, json={"ok": True, "result": True})
        return httpx.Response(404)

    async def _run() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            res = await run_telegram_setup(
                config_path=cfg_path,
                token="123456:TESTTOKEN",
                wait_s=0.1,
                no_reload=True,
                send_test=True,
                print_fn=lambda _m: None,
                client=client,
            )
            assert res["ok"] is True
            assert res["owner_chat_id"] == "555123"
            assert res["profile"] == {
                "commands": True,
                "short_description": True,
                "description": True,
                "avatar": True,
            }
            assert res["test_sent"] is True
            assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600

            saved = load_config(cfg_path)
            assert saved.telegram.enabled is True
            assert saved.telegram.bot_token == "123456:TESTTOKEN"
            assert saved.telegram.owner_chat_id == "555123"
            assert saved.imessage.enabled is False
            assert saved.personal_workspace_dir == str(tmp_path)
            assert [c.id for c in saved.channels] == ["feishu_ops"]

            # Hot-swap daemon from old config -> Telegram notifier triggers seed_offset + register_commands
            old_cfg = WatchpostConfig(
                db_path=str(tmp_path / "wp.db"),
                media_dir=str(tmp_path / "media"),
                workspace_dir=str(tmp_path),
                language="zh",
                api_port=0,
                backend=BackendConfig(preset="mock"),
            )
            daemon = WatchpostDaemon(build_engine_from_config(old_cfg), old_cfg, config_path=cfg_path)
            cmds_before = sum(1 for p in seen_requests if p.endswith("/setMyCommands"))

            # Patch httpx.AsyncClient only for the newly constructed notifier during reload
            real_async_client = httpx.AsyncClient
            monkeypatch.setattr(
                "watchpost.notifiers.telegram._make_http_client",
                lambda proxy_url="", timeout_s=20.0: real_async_client(transport=httpx.MockTransport(handler)),
            )
            reload_res = await daemon.reload_from_disk()
            assert reload_res["reloaded"] is True
            assert any(n.name == "telegram" for n in daemon.engine.notifiers)
            assert sum(1 for p in seen_requests if p.endswith("/setMyCommands")) > cmds_before
            assert daemon.engine.store.get_kv("telegram_owner_update_offset") == 78

            # Verify cleaned /status & /help outputs omit noisy labels
            sent_texts: list[str] = []

            class _CaptureNotifier:
                name = "telegram"

                async def send_message(self, text: str) -> str:
                    sent_texts.append(text)
                    return "m1"

                async def notify_start(self, *args: Any, **kwargs: Any) -> list[str]:
                    return []

                async def notify_completion(self, *args: Any, **kwargs: Any) -> list[str]:
                    return []

                async def poll_owner_actions(self) -> list[Any]:
                    return []

            daemon.engine.notifiers = [_CaptureNotifier()]
            await daemon.engine.handle_owner_action(classify_owner_command("/status", notifier="telegram"))  # type: ignore[arg-type]
            await daemon.engine.handle_owner_action(classify_owner_command("/help", notifier="telegram"))  # type: ignore[arg-type]
            await daemon.engine.handle_owner_action(classify_owner_command("/new", notifier="telegram"))  # type: ignore[arg-type]
            await daemon.engine.handle_owner_action(classify_owner_command("/reset", notifier="telegram"))  # type: ignore[arg-type]
            assert len(sent_texts) == 4
            assert all("个人直聊" not in txt for txt in sent_texts)

            await daemon.close_resources()
        finally:
            await client.aclose()

    asyncio.run(_run())

    # Verify live doctor check with mocked get_bot_info
    async def _fake_bot_info(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"id": 123, "username": "wp_live_bot", "first_name": "Watchpost"}

    monkeypatch.setattr("watchpost.daemon.get_bot_info", _fake_bot_info)
    doc = run_doctor(cfg_path, live=True)
    assert doc["ok"] is True
    assert "@wp_live_bot, API reachable" in doc["checks"]["telegram"]["detail"]
