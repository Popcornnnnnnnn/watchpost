"""Headless FastAPI local JSON control API for the Watchpost daemon (no Web UI)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from watchpost import __version__
from watchpost.adapters.simulator import SimulatorAdapter
from watchpost.backends.mock import MockAgentBackend
from watchpost.config import load_raw_config, save_config
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


class QuietControlBody(BaseModel):
    action: Literal["mute", "unmute", "toggle"]
    minutes: int = 120


def create_app(
    db_path: Path | str,
    *,
    backend: AgentBackend | None = None,
    config: EngineConfig | None = None,
    engine: WatchpostEngine | None = None,
    reload_fn: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    config_path: Path | str | None = None,
) -> FastAPI:
    if engine is not None:
        active_engine = engine
        store = engine.store
        chosen_backend = engine.backend
    else:
        store = WatchpostStore(db_path)
        chosen_backend = backend or MockAgentBackend(language=(config.language if config else "zh"))
        active_engine = WatchpostEngine(store, chosen_backend, config=config)
        active_engine.register_adapter(SimulatorAdapter())

    app = FastAPI(title="Watchpost", version=__version__)
    app.state.watchpost_store = store
    app.state.watchpost_engine = active_engine
    app.state.watchpost_config_path = Path(config_path).expanduser() if config_path else None

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
                "/api/reload",
                "/api/quiet",
                "/api/brief",
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
            "quiet_hours": active_engine.get_quiet_status(),
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

        cfg_file: Path | None = app.state.watchpost_config_path
        if cfg_file is not None and cfg_file.is_file():
            raw_cfg = load_raw_config(cfg_file)
            changed = False
            new_channels = []
            for ch in raw_cfg.channels:
                if ch.id == channel_id:
                    patch_updates: dict[str, Any] = {}
                    if body.enabled is not None:
                        patch_updates["enabled"] = body.enabled
                    if body.autonomy_mode is not None:
                        patch_updates["autonomy_mode"] = body.autonomy_mode
                    if body.trigger_mode is not None:
                        patch_updates["trigger_mode"] = body.trigger_mode
                    if body.whisper_prompt is not None:
                        patch_updates["whisper_prompt"] = body.whisper_prompt
                    if body.persona_text is not None:
                        patch_updates["persona_text"] = body.persona_text
                    if patch_updates:
                        ch = ch.model_copy(update=patch_updates)
                        changed = True
                new_channels.append(ch)
            if changed:
                save_config(raw_cfg.model_copy(update={"channels": new_channels}), cfg_file)

        return updated.model_dump(mode="json")

    @app.post("/api/reload")
    async def reload_config_endpoint() -> dict[str, Any]:
        if reload_fn is None:
            raise HTTPException(status_code=501, detail="Hot reload is only available in daemon mode")
        try:
            return await reload_fn()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Reload failed: {exc}") from exc

    @app.get("/api/quiet")
    async def get_quiet() -> dict[str, Any]:
        return active_engine.get_quiet_status()

    @app.post("/api/quiet")
    async def control_quiet(body: QuietControlBody) -> dict[str, Any]:
        cur = active_engine.get_quiet_status()
        act = body.action
        if act == "toggle":
            act = "unmute" if cur["active"] else "mute"
        if act == "mute":
            return active_engine.set_quiet_mute(minutes=body.minutes)
        st = active_engine.clear_quiet_mute()
        if int(st.get("queued_count") or 0) > 0:
            await active_engine.send_briefing()
            st = active_engine.get_quiet_status()
        return st

    @app.post("/api/brief")
    async def send_brief_endpoint() -> dict[str, Any]:
        return await active_engine.send_briefing()

    @app.get("/api/threads")
    async def list_threads() -> list[dict[str, Any]]:
        threads = store.list_threads(limit=50)
        out: list[dict[str, Any]] = []
        for th in threads:
            data = th.model_dump(mode="json")
            data["events"] = [e.model_dump(mode="json") for e in store.list_thread_events(th.id)]
            out.append(data)
        return out

    @app.post("/api/threads/dismiss-all")
    async def dismiss_all_threads() -> dict[str, Any]:
        cleared = store.dismiss_all_pending_go()
        return {"cleared": cleared, "awaiting_go": store.get_metrics()["awaiting_go"]}

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
            updated = await active_engine.steer_from_owner(thread_id, body.message, approved_go=False)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        data = updated.model_dump(mode="json")
        data["events"] = [e.model_dump(mode="json") for e in store.list_thread_events(thread_id)]
        return data

    @app.post("/api/threads/{thread_id}/approve")
    async def approve_thread(thread_id: str) -> dict[str, Any]:
        try:
            updated = await active_engine.steer_from_owner(thread_id, "go", approved_go=True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        data = updated.model_dump(mode="json")
        data["events"] = [e.model_dump(mode="json") for e in store.list_thread_events(thread_id)]
        return data

    @app.post("/api/threads/{thread_id}/dismiss")
    async def dismiss_thread(thread_id: str) -> dict[str, Any]:
        try:
            updated = active_engine.dismiss_go(thread_id)
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
        return await active_engine.run_demo_scenario()

    return app
