"""Headless FastAPI local JSON control API tests."""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from watchpost.models import ThreadStatus
from watchpost.server import create_app, create_webhook_app


def _auth_headers(app) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.watchpost_api_token}"}


def test_headless_root_and_rest_api_flow() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(Path(tmp) / "daemon.db")
        client = TestClient(app)
        hdrs = _auth_headers(app)

        # Unauthenticated, cross-origin, and foreign-Host requests are rejected
        assert client.get("/api/health").status_code == 401
        assert client.post("/api/threads/dismiss-all", headers={**hdrs, "Origin": "https://evil.example"}).status_code == 403
        assert client.get("/api/threads", headers={**hdrs, "Host": "evil.example:8787"}).status_code == 403

        # Headless JSON descriptor and strict non-UI security headers
        res = client.get("/")
        assert res.status_code == 200
        body = res.json()
        assert body["service"] == "watchpost"
        assert body["mode"] == "headless-daemon"
        assert "/api/health" in body["endpoints"]
        assert res.headers["X-Content-Type-Options"] == "nosniff"
        assert res.headers["X-Frame-Options"] == "DENY"
        assert res.headers["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"

        h0 = client.get("/api/health", headers=hdrs).json()
        assert h0["status"] == "ok"
        assert h0["metrics"]["threads"] == 0

        # Run built-in simulator demo
        sim = client.post("/api/simulate/run", headers=hdrs)
        assert sim.status_code == 200
        assert sim.json()["threads_created"] == 2

        channels = client.get("/api/channels", headers=hdrs).json()
        assert len(channels) == 2

        # Patch channel autonomy mode
        patch = client.patch("/api/channels/sim_alex_dm", json={"autonomy_mode": "assistant_only"}, headers=hdrs)
        assert patch.status_code == 200
        assert patch.json()["autonomy_mode"] == "assistant_only"

        threads = client.get("/api/threads", headers=hdrs).json()
        assert len(threads) == 2
        go_thread = next(t for t in threads if t["pending_go"])
        non_go_thread = next(t for t in threads if not t["pending_go"])

        # Copy draft metric tracking
        copied = client.post(f"/api/threads/{go_thread['id']}/copied", headers=hdrs)
        assert copied.status_code == 200
        assert copied.json()["marked"] >= 1

        # Approving a thread that is not awaiting go returns 409 Conflict
        refused = client.post(f"/api/threads/{non_go_thread['id']}/approve", headers=hdrs)
        assert refused.status_code == 409

        # Steer then approve "go"
        steered = client.post(
            f"/api/threads/{go_thread['id']}/steer",
            json={"message": "Check worker 2 pool latency too"},
            headers=hdrs,
        )
        assert steered.status_code == 200
        approved = client.post(f"/api/threads/{go_thread['id']}/approve", headers=hdrs)
        assert approved.status_code == 200
        assert approved.json()["pending_go"] is False
        assert approved.json()["status"] == "completed"

        h1 = client.get("/api/health", headers=hdrs).json()
        assert h1["metrics"]["drafts_copied"] >= 1
        assert h1["metrics"]["awaiting_go"] == 0

        assert client.get("/api/threads/nonexistent", headers=hdrs).status_code == 404
        assert client.post("/api/threads/nonexistent/dismiss", headers=hdrs).status_code == 404

        # Dedicated webhook listener isolates signed webhooks from control API routes
        wh_app = create_webhook_app(app.state.watchpost_engine)
        wh_client = TestClient(wh_app)
        assert wh_client.get("/healthz").status_code == 200
        assert wh_client.get("/api/threads").status_code == 404


def test_dismiss_and_dismiss_all_pending_go() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(Path(tmp) / "daemon.db")
        client = TestClient(app)
        hdrs = _auth_headers(app)

        sim = client.post("/api/simulate/run", headers=hdrs)
        assert sim.status_code == 200

        threads = client.get("/api/threads", headers=hdrs).json()
        go_thread = next(t for t in threads if t["pending_go"])

        dismissed = client.post(f"/api/threads/{go_thread['id']}/dismiss", headers=hdrs)
        assert dismissed.status_code == 200
        assert dismissed.json()["pending_go"] is False
        assert dismissed.json()["status"] == "completed"
        assert client.get("/api/health", headers=hdrs).json()["metrics"]["awaiting_go"] == 0

        # Re-simulate and test bulk dismiss-all
        store = app.state.watchpost_store
        store.create_or_update_thread(
            store.get_thread(go_thread["id"]).model_copy(
                update={"pending_go": True, "status": ThreadStatus.awaiting_go}
            )
        )
        assert client.get("/api/health", headers=hdrs).json()["metrics"]["awaiting_go"] == 1
        bulk = client.post("/api/threads/dismiss-all", headers=hdrs)
        assert bulk.status_code == 200
        assert bulk.json() == {"cleared": 1, "awaiting_go": 0}
        assert client.get("/api/health", headers=hdrs).json()["metrics"]["awaiting_go"] == 0


def test_hot_reload_quiet_brief_and_persistent_channel_patch(monkeypatch) -> None:
    import stat
    from watchpost.config import (
        BackendConfig,
        FeishuConfig,
        WatchpostConfig,
        load_config,
        load_raw_config,
        save_config,
    )
    from watchpost.daemon import WatchpostDaemon, build_engine_from_config
    from watchpost.models import ChannelConfig, ChatType, QuietHoursConfig, TriggerMode

    monkeypatch.setenv("TEST_FEISHU_TOKEN", "tok-secret-1")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cfg_path = root / "config.json"
        persona_keep = root / "persona_keep.md"
        persona_keep.write_text("# Persona v1", encoding="utf-8")

        cfg = WatchpostConfig(
            db_path=str(root / "wp.db"),
            media_dir=str(root / "media"),
            workspace_dir=str(root),
            language="zh",
            quiet_hours=QuietHoursConfig(enabled=False, start="23:00", end="08:00"),
            backend=BackendConfig(preset="mock"),
            feishu=FeishuConfig(enabled=False, auth_mode="user", user_access_token="${TEST_FEISHU_TOKEN}"),
            channels=[
                ChannelConfig(
                    id="ch_keep",
                    adapter="simulator",
                    external_chat_id="sim_1",
                    name="Keep Peer",
                    chat_type=ChatType.p2p,
                    trigger_mode=TriggerMode.all,
                    persona_file=str(persona_keep),
                ),
                ChannelConfig(
                    id="ch_drop",
                    adapter="simulator",
                    external_chat_id="sim_2",
                    name="Drop Peer",
                    chat_type=ChatType.p2p,
                    trigger_mode=TriggerMode.all,
                ),
            ],
        )
        save_config(cfg, cfg_path)

        resolved = load_config(cfg_path)
        engine = build_engine_from_config(resolved)
        daemon = WatchpostDaemon(engine, resolved, config_path=cfg_path)
        app = create_app(
            resolved.db_path,
            engine=engine,
            reload_fn=daemon.reload_from_disk,
            config_path=cfg_path,
        )
        client = TestClient(app)
        hdrs = _auth_headers(app)

        # Persistent mode patch writes both SQLite and config.json while preserving placeholders & 0600 perms
        patch = client.patch("/api/channels/ch_keep", json={"autonomy_mode": "assistant_only"}, headers=hdrs)
        assert patch.status_code == 200
        assert patch.json()["autonomy_mode"] == "assistant_only"
        raw_after = load_raw_config(cfg_path)
        assert raw_after.feishu.user_access_token == "${TEST_FEISHU_TOKEN}"
        raw_keep = next(c for c in raw_after.channels if c.id == "ch_keep")
        assert raw_keep.autonomy_mode.value == "assistant_only"
        assert raw_keep.persona_file == str(persona_keep)
        assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600

        # Mutate persona file + add channel + remove ch_drop -> hot reload applies immediately
        persona_keep.write_text("# Persona v2 updated", encoding="utf-8")
        updated_cfg = raw_after.model_copy(
            update={
                "channels": [
                    raw_keep,
                    ChannelConfig(
                        id="ch_new",
                        adapter="simulator",
                        external_chat_id="sim_3",
                        name="New Peer",
                        chat_type=ChatType.p2p,
                        trigger_mode=TriggerMode.all,
                    ),
                ]
            }
        )
        save_config(updated_cfg, cfg_path)

        rel = client.post("/api/reload", headers=hdrs)
        assert rel.status_code == 200
        rel_body = rel.json()
        assert rel_body["reloaded"] is True
        assert rel_body["channels"] == 2
        assert rel_body["disabled"] == 1

        ch_keep_db = engine.store.get_channel("ch_keep")
        ch_drop_db = engine.store.get_channel("ch_drop")
        ch_new_db = engine.store.get_channel("ch_new")
        assert ch_keep_db is not None and "Persona v2 updated" in ch_keep_db.persona_text
        assert ch_drop_db is not None and ch_drop_db.enabled is False
        assert ch_new_db is not None and ch_new_db.enabled is True

        # Quiet & briefing endpoints work cleanly
        q0 = client.get("/api/quiet", headers=hdrs).json()
        assert q0["active"] is False
        q1 = client.post("/api/quiet", json={"action": "mute", "minutes": 45}, headers=hdrs).json()
        assert q1["active"] is True and q1["reason"] == "muted"
        q2 = client.post("/api/quiet", json={"action": "unmute"}, headers=hdrs).json()
        assert q2["active"] is False
        b = client.post("/api/brief", headers=hdrs)
        assert b.status_code == 200 and b.json()["sent"] is True
