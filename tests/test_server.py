"""FastAPI server and Web Inbox Dashboard tests."""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from watchpost.server import create_app


def test_dashboard_and_rest_api_flow() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(Path(tmp) / "web.db")
        client = TestClient(app)

        # HTML shell, security headers, modern clipboard & light/dark meta, zero innerHTML sink
        res = client.get("/")
        assert res.status_code == 200
        assert res.headers["X-Content-Type-Options"] == "nosniff"
        assert res.headers["X-Frame-Options"] == "DENY"
        assert "script-src 'nonce-" in res.headers["Content-Security-Policy"]
        assert '<meta name="color-scheme" content="light dark">' in res.text
        assert 'command="--copy"' in res.text
        assert "innerHTML" not in res.text

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
