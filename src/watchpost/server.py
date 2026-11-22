"""Headless FastAPI local JSON control API for the Watchpost daemon (no Web UI)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from watchpost import __version__
from watchpost.adapters.simulator import SimulatorAdapter
from watchpost.backends.mock import MockAgentBackend
from watchpost.engine import WatchpostEngine
from watchpost.models import AutonomyMode, EngineConfig, TriggerMode
from watchpost.ports import AgentBackend
from watchpost.store import WatchpostStore


class ChannelPatchBody(BaseModel):
    enabled: bool | None = None
    autonomy_mode: AutonomyMode | None = None
    trigger_mode: TriggerMode | None = None
    persona_text: str | None = None
    whisper_prompt: str | None = None


class SteerBody(BaseModel):
    message: str


def create_app(
    db_path: Path | str,
    *,
    backend: AgentBackend | None = None,
    config: EngineConfig | None = None,
) -> FastAPI:
    store = WatchpostStore(db_path)
    chosen_backend = backend or MockAgentBackend(language=(config.language if config else "zh"))
    engine = WatchpostEngine(store, chosen_backend, config=config)
    engine.register_adapter(SimulatorAdapter())

    app = FastAPI(title="Watchpost", version=__version__)
    app.state.watchpost_store = store
    app.state.watchpost_engine = engine

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        return response

    @app.get("/")
    async def root() -> dict[str, Any]:
        return {
            "service": "watchpost",
            "mode": "headless-daemon",
            "version": __version__,
            "backend": chosen_backend.name,
            "endpoints": [
                "/api/health",
                "/api/channels",
                "/api/threads",
                "/api/simulate/run",
            ],
        }

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "backend": chosen_backend.name,
            "metrics": store.get_metrics(),
        }

    @app.get("/api/channels")
    async def list_channels() -> list[dict[str, Any]]:
        return [c.model_dump(mode="json") for c in store.list_channels()]

    @app.patch("/api/channels/{channel_id}")
    async def patch_channel(channel_id: str, body: ChannelPatchBody) -> dict[str, Any]:
        updated = store.update_channel_settings(
            channel_id,
            enabled=body.enabled,
            autonomy_mode=body.autonomy_mode,
            trigger_mode=body.trigger_mode,
            persona_text=body.persona_text,
            whisper_prompt=body.whisper_prompt,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Channel not found")
        return updated.model_dump(mode="json")

    @app.get("/api/threads")
    async def list_threads() -> list[dict[str, Any]]:
        threads = store.list_threads(limit=50)
        out: list[dict[str, Any]] = []
        for th in threads:
            data = th.model_dump(mode="json")
            data["events"] = [e.model_dump(mode="json") for e in store.list_thread_events(th.id)]
            out.append(data)
        return out

    @app.get("/api/threads/{thread_id}")
    async def get_thread(thread_id: str) -> dict[str, Any]:
        th = store.get_thread(thread_id)
        if th is None:
            raise HTTPException(status_code=404, detail="Thread not found")
        data = th.model_dump(mode="json")
        data["events"] = [e.model_dump(mode="json") for e in store.list_thread_events(thread_id)]
        return data

    @app.post("/api/threads/{thread_id}/steer")
    async def steer_thread(thread_id: str, body: SteerBody) -> dict[str, Any]:
        try:
            updated = await engine.steer_from_owner(thread_id, body.message, approved_go=False)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        data = updated.model_dump(mode="json")
        data["events"] = [e.model_dump(mode="json") for e in store.list_thread_events(thread_id)]
        return data

    @app.post("/api/threads/{thread_id}/approve")
    async def approve_thread(thread_id: str) -> dict[str, Any]:
        try:
            updated = await engine.steer_from_owner(thread_id, "go", approved_go=True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        data = updated.model_dump(mode="json")
        data["events"] = [e.model_dump(mode="json") for e in store.list_thread_events(thread_id)]
        return data

    @app.post("/api/threads/{thread_id}/copied")
    async def copied_thread(thread_id: str) -> dict[str, Any]:
        th = store.get_thread(thread_id)
        if th is None:
            raise HTTPException(status_code=404, detail="Thread not found")
        updated_rows = store.mark_draft_copied(thread_id)
        return {"thread_id": thread_id, "marked": updated_rows}

    @app.post("/api/simulate/run")
    async def simulate_run() -> dict[str, Any]:
        return await engine.run_demo_scenario()

    return app
