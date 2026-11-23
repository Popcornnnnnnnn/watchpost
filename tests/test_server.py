"""Headless FastAPI local JSON control API tests."""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from watchpost.models import ThreadStatus
from watchpost.server import create_app


def test_headless_root_and_rest_api_flow() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(Path(tmp) / "daemon.db")
        client = TestClient(app)

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

        h0 = client.get("/api/health").json()
        assert h0["status"] == "ok"
        assert h0["metrics"]["threads"] == 0

        # Run built-in simulator demo
        sim = client.post("/api/simulate/run")
        assert sim.status_code == 200
        assert sim.json()["threads_created"] == 2

        channels = client.get("/api/channels").json()
        assert len(channels) == 2

        # Patch channel autonomy mode
        patch = client.patch("/api/channels/sim_alex_dm", json={"autonomy_mode": "assistant_only"})
        assert patch.status_code == 200
        assert patch.json()["autonomy_mode"] == "assistant_only"

        threads = client.get("/api/threads").json()
        assert len(threads) == 2
        go_thread = next(t for t in threads if t["pending_go"])

        # Copy draft metric tracking
        copied = client.post(f"/api/threads/{go_thread['id']}/copied")
        assert copied.status_code == 200
        assert copied.json()["marked"] >= 1

        # Steer then approve "go"
        steered = client.post(f"/api/threads/{go_thread['id']}/steer", json={"message": "Check bay 2 pressure too"})
        assert steered.status_code == 200
        approved = client.post(f"/api/threads/{go_thread['id']}/approve")
        assert approved.status_code == 200
        assert approved.json()["pending_go"] is False
        assert approved.json()["status"] == "completed"

        h1 = client.get("/api/health").json()
        assert h1["metrics"]["drafts_copied"] >= 1
        assert h1["metrics"]["awaiting_go"] == 0

        assert client.get("/api/threads/nonexistent").status_code == 404
        assert client.post("/api/threads/nonexistent/dismiss").status_code == 404


def test_dismiss_and_dismiss_all_pending_go() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(Path(tmp) / "daemon.db")
        client = TestClient(app)

        sim = client.post("/api/simulate/run")
        assert sim.status_code == 200

        threads = client.get("/api/threads").json()
        go_thread = next(t for t in threads if t["pending_go"])

        dismissed = client.post(f"/api/threads/{go_thread['id']}/dismiss")
        assert dismissed.status_code == 200
        assert dismissed.json()["pending_go"] is False
        assert dismissed.json()["status"] == "completed"
        assert client.get("/api/health").json()["metrics"]["awaiting_go"] == 0

        # Re-simulate and test bulk dismiss-all
        store = app.state.watchpost_store
        store.create_or_update_thread(
            store.get_thread(go_thread["id"]).model_copy(
                update={"pending_go": True, "status": ThreadStatus.awaiting_go}
            )
        )
        assert client.get("/api/health").json()["metrics"]["awaiting_go"] == 1
        bulk = client.post("/api/threads/dismiss-all")
        assert bulk.status_code == 200
        assert bulk.json() == {"cleared": 1, "awaiting_go": 0}
        assert client.get("/api/health").json()["metrics"]["awaiting_go"] == 0
