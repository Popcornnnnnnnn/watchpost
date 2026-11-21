"""FastAPI server and modern zero-dependency Web Inbox Dashboard for Watchpost."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
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


_DASHBOARD_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>Watchpost — Self-Hosted Field-Ops Inbox</title>
  <style nonce="__CSP_NONCE__">
    :root {
      --bg-light: #f6f7f9;
      --bg-dark: #111317;
      --card-light: #ffffff;
      --card-dark: #1a1d24;
      --border-light: #dfe3ea;
      --border-dark: #2c313d;
      --text-light: #161922;
      --text-dark: #eceff4;
      --muted-light: #5b6475;
      --muted-dark: #9aa3b4;
      --accent-light: #0f62fe;
      --accent-dark: #58a6ff;
      --accent-text-light: #ffffff;
      --accent-text-dark: #0b1220;
      --draft-light: #eef6ff;
      --draft-dark: #18273c;
      --warn-light: #fff4e5;
      --warn-dark: #332512;

      --bg: var(--bg-light);
      --card: var(--card-light);
      --border: var(--border-light);
      --text: var(--text-light);
      --muted: var(--muted-light);
      --accent: var(--accent-light);
      --accent-text: var(--accent-text-light);
      --draft-bg: var(--draft-light);
      --warn-bg: var(--warn-light);

      color-scheme: light dark;
    }

    @media (prefers-color-scheme: dark) {
      :root {
        --bg: var(--bg-dark);
        --card: var(--card-dark);
        --border: var(--border-dark);
        --text: var(--text-dark);
        --muted: var(--muted-dark);
        --accent: var(--accent-dark);
        --accent-text: var(--accent-text-dark);
        --draft-bg: var(--draft-dark);
        --warn-bg: var(--warn-dark);
      }
    }

    @supports (color: light-dark(white, black)) {
      :root {
        --bg: light-dark(var(--bg-light), var(--bg-dark));
        --card: light-dark(var(--card-light), var(--card-dark));
        --border: light-dark(var(--border-light), var(--border-dark));
        --text: light-dark(var(--text-light), var(--text-dark));
        --muted: light-dark(var(--muted-light), var(--muted-dark));
        --accent: light-dark(var(--accent-light), var(--accent-dark));
        --accent-text: light-dark(var(--accent-text-light), var(--accent-text-dark));
        --draft-bg: light-dark(var(--draft-light), var(--draft-dark));
        --warn-bg: light-dark(var(--warn-light), var(--warn-dark));
      }
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }
    header {
      border-bottom: 1px solid var(--border);
      background: var(--card);
      padding: 14px 20px;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .brand h1 { margin: 0; font-size: 1.15rem; }
    .brand p { margin: 2px 0 0; font-size: 0.85rem; color: var(--muted); }
    .toolbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    button, select, input {
      font: inherit;
      border-radius: 8px;
      border: 1px solid var(--border);
      padding: 7px 12px;
      background: var(--card);
      color: var(--text);
      min-block-size: 36px;
    }
    button { cursor: pointer; font-weight: 500; min-inline-size: 40px; }
    button.primary { background: var(--accent); color: var(--accent-text); border-color: transparent; }
    main {
      max-width: 1120px;
      margin: 20px auto;
      padding: 0 16px 40px;
      display: grid;
      grid-template-columns: 320px 1fr;
      gap: 18px;
    }
    @media (max-width: 820px) { main { grid-template-columns: 1fr; } }
    .panel, .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px;
    }
    .panel h2 { margin: 0 0 12px; font-size: 1rem; }
    .metrics {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 8px;
      margin-bottom: 16px;
    }
    .metric {
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 8px 10px;
    }
    .metric span { display: block; font-size: 0.78rem; color: var(--muted); }
    .metric strong { font-size: 1.15rem; }
    .channel-row {
      border-top: 1px solid var(--border);
      padding: 10px 0;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .channel-top { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
    .threads { display: flex; flex-direction: column; gap: 14px; }
    .card-head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; flex-wrap: wrap; }
    .badges { display: flex; gap: 6px; flex-wrap: wrap; font-size: 0.78rem; color: var(--muted); }
    .badge {
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 2px 8px;
    }
    .bubbles { display: flex; flex-direction: column; gap: 8px; margin: 12px 0; }
    .bubble {
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 8px 12px;
      font-size: 0.94rem;
    }
    .draft-box {
      background: var(--draft-bg);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px 12px;
      margin: 10px 0;
    }
    .draft-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 6px; }
    .copy-wrap { display: inline-flex; align-items: center; gap: 6px; }
    .tip { font-size: 0.8rem; color: var(--muted); opacity: 0; transition: opacity 0.15s; }
    button[data-copied] + .tip, button[data-failed] + .tip { opacity: 1; }
    .draft-text { margin: 0; white-space: pre-wrap; font-size: 0.94rem; }
    .attention {
      background: var(--warn-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 8px 12px;
      margin: 8px 0;
      font-size: 0.88rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .steer-form { display: flex; gap: 8px; margin-top: 10px; }
    .steer-form input { flex: 1; min-inline-size: 0; }
    .empty { color: var(--muted); font-size: 0.95rem; }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <h1>Watchpost</h1>
      <p>Self-Hosted Field-Ops &amp; Engineering Copilot (Draft-Only Default)</p>
    </div>
    <div class="toolbar">
      <button type="button" id="btn-demo" class="primary">Run Field-Ops Demo</button>
      <button type="button" id="btn-refresh">Refresh</button>
    </div>
  </header>
  <main>
    <aside class="panel">
      <h2>Health &amp; Metrics</h2>
      <div class="metrics" id="metrics-grid"></div>
      <h2>Monitored Channels</h2>
      <div id="channels-list"></div>
    </aside>
    <section>
      <div id="threads-list" class="threads"></div>
    </section>
  </main>
  <script nonce="__CSP_NONCE__">
    const timers = new WeakMap();
    let lastStatusMessage = '';

    function reportCopy(button, ok) {
      const tip = button.nextElementSibling;
      clearTimeout(timers.get(button));
      button.removeAttribute('data-copied');
      button.removeAttribute('data-failed');
      let msg = ok ? 'Copied' : 'Copy failed';
      if (msg === lastStatusMessage) msg += '\\u00A0';
      lastStatusMessage = msg;
      tip.textContent = '';
      void button.offsetWidth;
      button.setAttribute(ok ? 'data-copied' : 'data-failed', '');
      requestAnimationFrame(() => { tip.textContent = msg; });
      timers.set(button, setTimeout(() => {
        button.removeAttribute('data-copied');
        button.removeAttribute('data-failed');
        tip.textContent = '';
      }, 2000));
    }

    async function handleCopy(button, target) {
      const text = target.innerText.trim();
      if (!text) return;
      try {
        await navigator.clipboard.writeText(text);
        reportCopy(button, true);
        const tid = button.dataset.threadId;
        if (tid) {
          await fetch(`/api/threads/${encodeURIComponent(tid)}/copied`, { method: 'POST' });
          loadAll();
        }
      } catch {
        reportCopy(button, false);
      }
    }

    document.addEventListener('command', (ev) => {
      if (ev.command === '--copy' && ev.target) {
        handleCopy(ev.source, ev.target);
      }
    }, true);

    if (!('CommandEvent' in window)) {
      document.addEventListener('click', (ev) => {
        const btn = ev.target.closest('button[command="--copy"]');
        if (!btn) return;
        const target = document.getElementById(btn.getAttribute('commandfor'));
        if (target) handleCopy(btn, target);
      });
    }

    function el(tag, className, text) {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined && text !== null) node.textContent = String(text);
      return node;
    }

    async function loadAll() {
      const [healthRes, chRes, thRes] = await Promise.all([
        fetch('/api/health'),
        fetch('/api/channels'),
        fetch('/api/threads'),
      ]);
      const health = await healthRes.json();
      const channels = await chRes.json();
      const threads = await thRes.json();

      const metricsGrid = document.getElementById('metrics-grid');
      metricsGrid.replaceChildren();
      for (const [label, val] of [
        ['Channels', health.metrics.channels],
        ['Threads', health.metrics.threads],
        ['Awaiting go', health.metrics.awaiting_go],
        ['Drafts copied', health.metrics.drafts_copied],
      ]) {
        const box = el('div', 'metric');
        box.append(el('span', null, label), el('strong', null, val));
        metricsGrid.append(box);
      }

      const chList = document.getElementById('channels-list');
      chList.replaceChildren();
      if (!channels.length) {
        chList.append(el('p', 'empty', 'No channels configured yet. Click "Run Field-Ops Demo".'));
      }
      for (const ch of channels) {
        const row = el('div', 'channel-row');
        const top = el('div', 'channel-top');
        top.append(el('strong', null, ch.name));
        const toggleBtn = el('button', null, ch.enabled ? 'Enabled' : 'Paused');
        toggleBtn.type = 'button';
        toggleBtn.addEventListener('click', async () => {
          await fetch(`/api/channels/${encodeURIComponent(ch.id)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: !ch.enabled }),
          });
          loadAll();
        });
        top.append(toggleBtn);

        const modeSel = el('select');
        modeSel.setAttribute('aria-label', `Autonomy mode for ${ch.name}`);
        for (const m of ['assistant_only', 'diagnose_only', 'auto_fix']) {
          const opt = el('option', null, m);
          opt.value = m;
          opt.selected = ch.autonomy_mode === m;
          modeSel.append(opt);
        }
        modeSel.addEventListener('change', async () => {
          await fetch(`/api/channels/${encodeURIComponent(ch.id)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ autonomy_mode: modeSel.value }),
          });
          loadAll();
        });
        row.append(top, el('span', 'empty', `${ch.adapter} · ${ch.chat_type}`), modeSel);
        chList.append(row);
      }

      const thList = document.getElementById('threads-list');
      thList.replaceChildren();
      if (!threads.length) {
        const emptyCard = el('div', 'card');
        emptyCard.append(el('p', 'empty', 'Inbox is quiet. Click "Run Field-Ops Demo" above to simulate an overnight field-ops handoff and DM check.'));
        thList.append(emptyCard);
        return;
      }

      for (const th of threads) {
        const card = el('article', 'card');
        const head = el('div', 'card-head');
        head.append(el('strong', null, th.title || th.id));
        const badges = el('div', 'badges');
        badges.append(
          el('span', 'badge', th.triage_mode),
          el('span', 'badge', th.status),
          el('span', 'badge', th.sender_name || 'channel')
        );
        head.append(badges);
        card.append(head);

        const latestReply = [...th.events].reverse().find((e) => e.kind === 'agent_reply' || e.kind === 'error');
        if (latestReply) {
          const bubbles = el('div', 'bubbles');
          for (const b of latestReply.summary_bubbles) {
            bubbles.append(el('div', 'bubble', b));
          }
          card.append(bubbles);

          if (latestReply.suggested_reply) {
            const draftBox = el('div', 'draft-box');
            const dHead = el('div', 'draft-head');
            dHead.append(el('strong', null, 'Suggested Reply Draft'));
            const wrap = el('span', 'copy-wrap');
            const draftId = `draft-${th.id}`;
            const copyBtn = el('button', 'copy-button', 'Copy Draft');
            copyBtn.type = 'button';
            copyBtn.setAttribute('command', '--copy');
            copyBtn.setAttribute('commandfor', draftId);
            copyBtn.setAttribute('aria-label', `Copy suggested reply for ${th.title}`);
            copyBtn.dataset.threadId = th.id;
            const tip = el('span', 'tip');
            tip.setAttribute('role', 'status');
            wrap.append(copyBtn, tip);
            dHead.append(wrap);
            const draftP = el('p', 'draft-text', latestReply.suggested_reply);
            draftP.id = draftId;
            draftBox.append(dHead, draftP);
            card.append(draftBox);
          }

          if (th.pending_go || latestReply.needs_attention) {
            const attn = el('div', 'attention');
            attn.append(el('span', null, latestReply.needs_attention || 'Awaiting approval'));
            if (th.pending_go) {
              const goBtn = el('button', 'primary', 'Approve (go)');
              goBtn.type = 'button';
              goBtn.addEventListener('click', async () => {
                await fetch(`/api/threads/${encodeURIComponent(th.id)}/approve`, { method: 'POST' });
                loadAll();
              });
              attn.append(goBtn);
            }
            card.append(attn);
          }
        }

        const form = el('form', 'steer-form');
        const input = el('input');
        input.type = 'text';
        input.placeholder = 'Steer agent or refine reply...';
        input.setAttribute('aria-label', `Follow-up steer for ${th.title}`);
        const submit = el('button', null, 'Steer');
        submit.type = 'submit';
        form.append(input, submit);
        form.addEventListener('submit', async (ev) => {
          ev.preventDefault();
          const val = input.value.trim();
          if (!val) return;
          input.value = '';
          await fetch(`/api/threads/${encodeURIComponent(th.id)}/steer`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: val }),
          });
          loadAll();
        });
        card.append(form);
        thList.append(card);
      }
    }

    document.getElementById('btn-demo').addEventListener('click', async () => {
      await fetch('/api/simulate/run', { method: 'POST' });
      loadAll();
    });
    document.getElementById('btn-refresh').addEventListener('click', loadAll);
    loadAll();
  </script>
</body>
</html>
"""


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
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        nonce = getattr(request.state, "csp_nonce", "")
        html = _DASHBOARD_TEMPLATE.replace("__CSP_NONCE__", nonce)
        return HTMLResponse(content=html)

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
