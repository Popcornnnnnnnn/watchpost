"""Clean 2-column local Web Simulator UI for interactive Watchpost testing (`watchpost sandbox-ui`)."""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from watchpost import __version__
from watchpost.adapters.local_sandbox import LocalSandboxAdapter, default_sandbox_channels
from watchpost.backends.cli_agent import CLIAgentBackend
from watchpost.backends.mock import MockAgentBackend
from watchpost.engine import WatchpostEngine
from watchpost.models import AutonomyMode, ChannelConfig, ChatType, EngineConfig, OwnerAction, ThreadRecord, TriggerMode
from watchpost.notifiers import classify_owner_command, format_start_notice
from watchpost.ports import AgentBackend
from watchpost.store import WatchpostStore

_DATA_URL_RE = re.compile(r"^data:image/(png|jpeg|jpg|webp|gif);base64,([A-Za-z0-9+/=\r\n]+)$", re.IGNORECASE)
_SAFE_IMG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_UPLOAD_BYTES = 8 * 1024 * 1024


class ColleagueSendBody(BaseModel):
    mode: Literal["dm", "group"] = "dm"
    sender_name: str = "Alex"
    text: str = ""
    mentions_owner: bool = False
    reply_to_id: str = ""
    immediate: bool = False
    image_data_url: str = ""
    image_name: str = ""


class ColleagueRecallBody(BaseModel):
    mode: Literal["dm", "group"] = "dm"
    message_id: str


class FlushBody(BaseModel):
    wait: bool = True


class SettingsBody(BaseModel):
    mode: Literal["dm", "group"] | None = None
    autonomy_mode: AutonomyMode | None = None
    sender_name: str | None = None


class PhoneSendBody(BaseModel):
    text: str
    reply_token: str = ""
    wait: bool = True


class SimulatorStateNotifier:
    """In-memory NotifierAdapter capturing start/completion/status bubbles for the 2-column simulator."""

    name = "sim_ui"

    def __init__(self, store: WatchpostStore, *, language: str = "zh") -> None:
        self.store = store
        self.language = language
        self._seq = 0
        self.activity: Literal["idle", "debouncing", "thinking"] = "idle"
        self.last_error: str = ""
        self.phone_entries: list[dict[str, Any]] = []
        self.msg_status: dict[str, str] = {}

    def _next_token(self) -> str:
        self._seq += 1
        return f"p-{self._seq}"

    def _now_hhmmss(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    def mark_messages(self, message_ids: list[str], status: str) -> None:
        for mid in message_ids:
            if mid:
                self.msg_status[mid] = status

    async def send_message(self, text: str) -> str:
        clean = (text or "").strip()
        if not clean:
            return ""
        tok = self._next_token()
        self.phone_entries.append(
            {
                "id": tok,
                "kind": "status",
                "text": clean,
                "thread_id": "",
                "channel_name": "Watchpost",
                "triage_mode": "",
                "status": "completed",
                "pending_go": False,
                "created_time": self._now_hhmmss(),
            }
        )
        return tok

    async def notify_start(
        self,
        channel: ChannelConfig,
        thread: ThreadRecord,
        preview: str,
        *,
        is_steer: bool = False,
        attachments: list[str] | None = None,
    ) -> list[str]:
        del attachments
        self.activity = "thinking"
        self.mark_messages(list(thread.message_ids), "thinking")
        tok = self._next_token()
        self.phone_entries.append(
            {
                "id": tok,
                "kind": "start",
                "text": format_start_notice(
                    channel.name,
                    preview,
                    adapter=channel.adapter,
                    is_steer=is_steer,
                    language=self.language,
                ),
                "thread_id": "" if thread.id == "pending" else thread.id,
                "channel_name": channel.name,
                "triage_mode": thread.triage_mode.value,
                "status": "active",
                "pending_go": False,
                "created_time": self._now_hhmmss(),
            }
        )
        return [tok]

    async def notify_completion(
        self,
        channel: ChannelConfig,
        thread: ThreadRecord,
        bubbles: list[str],
    ) -> list[str]:
        events = self.store.list_thread_events(thread.id)
        latest_draft = ""
        for ev in reversed(events):
            if ev.kind in {"agent_reply", "error"}:
                latest_draft = ev.suggested_reply.strip()
                break

        self.mark_messages(list(thread.message_ids), "triaged")
        self.activity = "idle"
        tokens: list[str] = []
        now_s = self._now_hhmmss()
        for idx, bubble in enumerate(bubbles):
            clean = (bubble or "").strip()
            if not clean:
                continue
            tok = self._next_token()
            is_draft = bool(latest_draft and clean == latest_draft and idx == len(bubbles) - 1)
            self.phone_entries.append(
                {
                    "id": tok,
                    "kind": "draft" if is_draft else "bubble",
                    "text": clean,
                    "thread_id": thread.id,
                    "channel_name": channel.name,
                    "triage_mode": thread.triage_mode.value,
                    "status": thread.status.value,
                    "pending_go": thread.pending_go,
                    "created_time": now_s,
                }
            )
            tokens.append(tok)
        return tokens

    async def poll_owner_actions(self) -> list[OwnerAction]:
        return []


_SANDBOX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>Watchpost Dev Simulator — Side-by-Side</title>
  <style nonce="__CSP_NONCE__">
    :root {
      color-scheme: light dark;

      --bg-light: #f2f4f8;
      --bg-dark: #0f1218;
      --panel-light: #ffffff;
      --panel-dark: #171b24;
      --sub-light: #f7f9fc;
      --sub-dark: #1e2430;
      --border-light: #dce2ec;
      --border-dark: #2c3342;
      --text-light: #141821;
      --text-dark: #eef2f8;
      --muted-light: #5f697a;
      --muted-dark: #97a3b6;
      --accent-light: #0f62fe;
      --accent-dark: #58a6ff;
      --accent-text-light: #ffffff;
      --accent-text-dark: #071324;
      --phone-bg-light: #e7eef8;
      --phone-bg-dark: #131c2b;
      --draft-bg-light: #e9f7ef;
      --draft-bg-dark: #142c22;
      --draft-border-light: #9cd6b5;
      --draft-border-dark: #2d6a4f;
      --warn-bg-light: #fff6e5;
      --warn-bg-dark: #33260f;

      --bg: var(--bg-light);
      --panel: var(--panel-light);
      --sub: var(--sub-light);
      --border: var(--border-light);
      --text: var(--text-light);
      --muted: var(--muted-light);
      --accent: var(--accent-light);
      --accent-text: var(--accent-text-light);
      --phone-bg: var(--phone-bg-light);
      --draft-bg: var(--draft-bg-light);
      --draft-border: var(--draft-border-light);
      --warn-bg: var(--warn-bg-light);
    }

    @media (prefers-color-scheme: dark) {
      :root {
        --bg: var(--bg-dark);
        --panel: var(--panel-dark);
        --sub: var(--sub-dark);
        --border: var(--border-dark);
        --text: var(--text-dark);
        --muted: var(--muted-dark);
        --accent: var(--accent-dark);
        --accent-text: var(--accent-text-dark);
        --phone-bg: var(--phone-bg-dark);
        --draft-bg: var(--draft-bg-dark);
        --draft-border: var(--draft-border-dark);
        --warn-bg: var(--warn-bg-dark);
      }
    }

    @supports (color: light-dark(white, black)) {
      :root {
        --bg: light-dark(var(--bg-light), var(--bg-dark));
        --panel: light-dark(var(--panel-light), var(--panel-dark));
        --sub: light-dark(var(--sub-light), var(--sub-dark));
        --border: light-dark(var(--border-light), var(--border-dark));
        --text: light-dark(var(--text-light), var(--text-dark));
        --muted: light-dark(var(--muted-light), var(--muted-dark));
        --accent: light-dark(var(--accent-light), var(--accent-dark));
        --accent-text: light-dark(var(--accent-text-light), var(--accent-text-dark));
        --phone-bg: light-dark(var(--phone-bg-light), var(--phone-bg-dark));
        --draft-bg: light-dark(var(--draft-bg-light), var(--draft-bg-dark));
        --draft-border: light-dark(var(--draft-border-light), var(--draft-border-dark));
        --warn-bg: light-dark(var(--warn-bg-light), var(--warn-bg-dark));
      }
    }

    * { box-sizing: border-box; }
    html, body { height: 100%; margin: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Segoe UI", system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      display: flex;
      flex-direction: column;
      line-height: 1.45;
    }

    .visually-hidden {
      position: absolute;
      width: 1px;
      height: 1px;
      overflow: hidden;
      clip-path: inset(50%);
      white-space: nowrap;
    }

    header {
      padding: 10px 18px;
      background: var(--panel);
      border-bottom: 1px solid var(--border);
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }

    .brand { display: flex; flex-direction: column; gap: 2px; }
    .brand h1 { font-size: 1rem; margin: 0; }
    .brand .meta { font-size: 0.78rem; color: var(--muted); word-break: break-all; }

    .controls { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: var(--sub);
      font-size: 0.8rem;
      font-weight: 600;
    }
    .pill[data-state="thinking"], .pill[data-state="debouncing"] {
      background: var(--warn-bg);
    }

    select, input[type="text"], button {
      font: inherit;
      color: var(--text);
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 6px 10px;
      font-size: 0.85rem;
    }
    button { cursor: pointer; }
    button.primary {
      background: var(--accent);
      color: var(--accent-text);
      border-color: var(--accent);
      font-weight: 600;
    }
    button.small { padding: 3px 8px; font-size: 0.76rem; }

    main {
      flex: 1;
      min-height: 0;
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
      padding: 14px 18px;
    }
    @media (max-width: 860px) {
      main { grid-template-columns: 1fr; }
    }

    .pane {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 12px;
      display: flex;
      flex-direction: column;
      min-height: 0;
      overflow: hidden;
    }

    .pane-head {
      padding: 10px 14px;
      background: var(--sub);
      border-bottom: 1px solid var(--border);
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }
    .pane-head h2 { margin: 0; font-size: 0.95rem; }

    .feed {
      flex: 1;
      overflow-y: auto;
      padding: 12px 14px;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }

    .empty {
      color: var(--muted);
      font-size: 0.86rem;
      text-align: center;
      margin: auto 12px;
    }

    .msg-card {
      border: 1px solid var(--border);
      background: var(--sub);
      border-radius: 10px;
      padding: 8px 11px;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .msg-card.recalled { opacity: 0.6; }
    .msg-top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      font-size: 0.76rem;
      color: var(--muted);
    }
    .msg-sender { font-weight: 600; color: var(--text); }
    .msg-body { white-space: pre-wrap; word-break: break-word; font-size: 0.9rem; }
    .msg-img {
      max-width: 220px;
      max-height: 150px;
      border-radius: 8px;
      border: 1px solid var(--border);
      margin-top: 4px;
      object-fit: contain;
    }
    .badge {
      padding: 1px 6px;
      border-radius: 999px;
      border: 1px solid var(--border);
      font-size: 0.72rem;
    }

    .phone-bubble {
      max-width: 92%;
      border-radius: 12px;
      padding: 9px 12px;
      border: 1px solid var(--border);
      background: var(--phone-bg);
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .phone-bubble.start {
      background: var(--sub);
      color: var(--muted);
      font-size: 0.84rem;
    }
    .phone-bubble.owner {
      align-self: flex-end;
      background: var(--accent);
      color: var(--accent-text);
      border-color: var(--accent);
    }
    .phone-bubble.owner .msg-top,
    .phone-bubble.owner .msg-sender { color: var(--accent-text); opacity: 0.9; }
    .phone-bubble.draft {
      background: var(--draft-bg);
      border-color: var(--draft-border);
    }
    .phone-bubble.selected {
      outline: 2px solid var(--accent);
    }

    .draft-actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
      margin-top: 2px;
    }

    .copy { position: relative; display: inline-flex; align-items: center; gap: 6px; }
    .copy-button {
      min-inline-size: 24px;
      min-block-size: 24px;
      padding: 4px 10px;
      font-size: 0.78rem;
      font-weight: 600;
    }
    .tip {
      font-size: 0.76rem;
      color: var(--muted);
      opacity: 0;
      transition: opacity 0.15s ease;
    }
    .copy-button[data-copied] + .tip,
    .copy-button[data-failed] + .tip { opacity: 1; }
    @media (prefers-reduced-motion: reduce) {
      .tip { transition: none; }
    }

    .composer {
      border-top: 1px solid var(--border);
      padding: 10px 12px;
      background: var(--sub);
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .composer-row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
      font-size: 0.8rem;
    }
    .composer-input-row {
      display: flex;
      align-items: flex-end;
      gap: 8px;
    }

    textarea.chat-input {
      font: inherit;
      color: var(--text);
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 8px 10px;
      width: 100%;
      height: auto;
      field-sizing: content;
      min-block-size: 2.5lh;
      max-block-size: 7lh;
      resize: vertical;
    }

    .quick-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; font-size: 0.78rem; }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <h1>Watchpost Dev Simulator</h1>
      <div class="meta" id="header-meta">Loading...</div>
    </div>
    <div class="controls">
      <span class="pill" id="activity-pill" data-state="idle">Idle</span>
      <label class="visually-hidden" for="mode-select">Channel mode</label>
      <select id="mode-select" aria-label="Channel mode">
        <option value="dm">DM (All msgs trigger)</option>
        <option value="group">Group (@Owner only)</option>
      </select>
      <label class="visually-hidden" for="autonomy-select">Autonomy gate</label>
      <select id="autonomy-select" aria-label="Autonomy gate">
        <option value="diagnose_only">diagnose_only (wait for go)</option>
        <option value="assistant_only">assistant_only (Q&amp;A + draft)</option>
        <option value="auto_fix">auto_fix</option>
      </select>
      <button type="button" id="flush-btn" class="small">Analyze Now</button>
    </div>
  </header>

  <main>
    <section class="pane" aria-label="Outside Colleague Chat">
      <div class="pane-head">
        <h2 id="left-title">Outside Colleague</h2>
        <div class="controls">
          <label class="visually-hidden" for="sender-input">Sender name</label>
          <input type="text" id="sender-input" value="Alex" size="10" aria-label="Sender name">
          <label><input type="checkbox" id="at-owner-chk"> @Owner</label>
        </div>
      </div>

      <div class="feed" id="colleague-feed"></div>

      <form class="composer" id="colleague-form">
        <div class="composer-row">
          <label for="img-input">Attach image:</label>
          <input type="file" id="img-input" accept="image/png,image/jpeg,image/webp,image/gif">
          <button type="button" id="clear-img-btn" class="small" hidden>Clear</button>
          <span id="reply-target-label" class="muted"></span>
        </div>
        <div class="composer-input-row">
          <label for="colleague-input" class="visually-hidden">Colleague message</label>
          <textarea id="colleague-input" class="chat-input" placeholder="Send as colleague (Enter to send, Shift+Enter for newline; Chinese IME safe)..."></textarea>
          <button type="submit" class="primary">Send</button>
        </div>
      </form>
    </section>

    <section class="pane" aria-label="Your Phone Inbox">
      <div class="pane-head">
        <h2>Your Phone (Watchpost)</h2>
        <div class="quick-bar">
          <button type="button" id="btn-status" class="small">/status</button>
          <button type="button" id="btn-list" class="small">/list</button>
          <button type="button" id="btn-go" class="small primary" hidden>Approve "go"</button>
        </div>
      </div>

      <div class="feed" id="phone-feed"></div>

      <form class="composer" id="phone-form">
        <div class="composer-row">
          <span id="phone-reply-target" class="muted">Replying to latest active thread</span>
          <button type="button" id="clear-phone-reply" class="small" hidden>Clear target</button>
        </div>
        <div class="composer-input-row">
          <label for="phone-input" class="visually-hidden">Reply from phone</label>
          <textarea id="phone-input" class="chat-input" placeholder="Reply from phone: go, /status, /list, or follow-up steer..."></textarea>
          <button type="submit" class="primary">Reply</button>
        </div>
      </form>
    </section>
  </main>

  <script nonce="__CSP_NONCE__">
    const STATE_SIGNATURES = { left: '', right: '', header: '' };
    const PENDING_IMAGE = { dataUrl: '', name: '' };
    let SELECTED_REPLY_ID = '';
    let PHONE_REPLY_TOKEN = '';
    let lastVoiceOverMsg = '';
    const copyTimers = new WeakMap();

    async function copyTargetText(button, target) {
      const text = target.matches('input, textarea') ? target.value : target.innerText.trim();
      if (!text) return;
      try {
        await navigator.clipboard.writeText(text);
        reportCopy(button, true);
      } catch {
        reportCopy(button, false);
      }
    }

    function reportCopy(button, ok) {
      const tip = button.nextElementSibling;
      clearTimeout(copyTimers.get(button));
      button.removeAttribute('data-copied');
      button.removeAttribute('data-failed');
      let message = ok ? 'Copied' : 'Copy failed';
      if (message === lastVoiceOverMsg) message += '\\u00A0';
      lastVoiceOverMsg = message;
      tip.textContent = '';
      void button.offsetWidth;
      button.setAttribute(ok ? 'data-copied' : 'data-failed', '');
      requestAnimationFrame(() => { tip.textContent = message; });
      copyTimers.set(button, setTimeout(() => {
        button.removeAttribute('data-copied');
        button.removeAttribute('data-failed');
        tip.textContent = '';
      }, 2000));
    }

    if ('CommandEvent' in window) {
      document.addEventListener('command', (event) => {
        if (event.command && event.command.startsWith('--copy')) {
          copyTargetText(event.source, event.target);
        }
      }, true);
    } else {
      document.addEventListener('click', (event) => {
        const button = event.target.closest('button[command^="--copy"]');
        if (!button) return;
        const target = document.getElementById(button.getAttribute('commandfor'));
        if (target) copyTargetText(button, target);
      });
    }

    function bindImeSafeSubmit(textareaId, formId) {
      const textarea = document.getElementById(textareaId);
      const form = document.getElementById(formId);
      textarea.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && !event.shiftKey) {
          event.preventDefault();
          if (event.isComposing || event.keyCode === 229) return;
          form.requestSubmit();
        }
      });
    }

    bindImeSafeSubmit('colleague-input', 'colleague-form');
    bindImeSafeSubmit('phone-input', 'phone-form');

    document.getElementById('img-input').addEventListener('change', (ev) => {
      const file = ev.target.files && ev.target.files[0];
      if (!file) {
        PENDING_IMAGE.dataUrl = '';
        PENDING_IMAGE.name = '';
        document.getElementById('clear-img-btn').hidden = true;
        return;
      }
      const reader = new FileReader();
      reader.onload = () => {
        PENDING_IMAGE.dataUrl = String(reader.result || '');
        PENDING_IMAGE.name = file.name;
        document.getElementById('clear-img-btn').hidden = false;
      };
      reader.readAsDataURL(file);
    });

    document.getElementById('clear-img-btn').addEventListener('click', () => {
      PENDING_IMAGE.dataUrl = '';
      PENDING_IMAGE.name = '';
      document.getElementById('img-input').value = '';
      document.getElementById('clear-img-btn').hidden = true;
    });

    document.getElementById('clear-phone-reply').addEventListener('click', () => {
      PHONE_REPLY_TOKEN = '';
      updatePhoneReplyBanner();
    });

    function updatePhoneReplyBanner() {
      const el = document.getElementById('phone-reply-target');
      const clr = document.getElementById('clear-phone-reply');
      if (PHONE_REPLY_TOKEN) {
        el.textContent = 'Swipe-replying to bubble ' + PHONE_REPLY_TOKEN;
        clr.hidden = false;
      } else {
        el.textContent = 'Replying to latest active thread';
        clr.hidden = true;
      }
      document.querySelectorAll('.phone-bubble').forEach((node) => {
        node.classList.toggle('selected', node.dataset.token === PHONE_REPLY_TOKEN && Boolean(PHONE_REPLY_TOKEN));
      });
    }

    async function apiJson(url, payload) {
      const opts = payload === undefined
        ? { method: 'GET' }
        : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) };
      const res = await fetch(url, opts);
      return await res.json();
    }

    document.getElementById('mode-select').addEventListener('change', async (ev) => {
      await apiJson('/api/settings', { mode: ev.target.value });
      await refreshState();
    });

    document.getElementById('autonomy-select').addEventListener('change', async (ev) => {
      await apiJson('/api/settings', { autonomy_mode: ev.target.value });
      await refreshState();
    });

    document.getElementById('flush-btn').addEventListener('click', async () => {
      await apiJson('/api/colleague/flush', { wait: false });
      await refreshState();
    });

    document.getElementById('colleague-form').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const input = document.getElementById('colleague-input');
      const text = input.value;
      if (!text.trim() && !PENDING_IMAGE.dataUrl) return;
      const body = {
        mode: document.getElementById('mode-select').value,
        sender_name: document.getElementById('sender-input').value.trim() || 'Alex',
        text,
        mentions_owner: document.getElementById('at-owner-chk').checked,
        reply_to_id: SELECTED_REPLY_ID,
        immediate: false,
        image_data_url: PENDING_IMAGE.dataUrl,
        image_name: PENDING_IMAGE.name,
      };
      input.value = '';
      SELECTED_REPLY_ID = '';
      document.getElementById('reply-target-label').textContent = '';
      PENDING_IMAGE.dataUrl = '';
      PENDING_IMAGE.name = '';
      document.getElementById('img-input').value = '';
      document.getElementById('clear-img-btn').hidden = true;
      await apiJson('/api/colleague/send', body);
      await refreshState();
    });

    async function sendPhoneText(text) {
      const clean = (text || '').trim();
      if (!clean) return;
      await apiJson('/api/phone/send', { text: clean, reply_token: PHONE_REPLY_TOKEN, wait: false });
      PHONE_REPLY_TOKEN = '';
      updatePhoneReplyBanner();
      await refreshState();
    }

    document.getElementById('phone-form').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const input = document.getElementById('phone-input');
      const text = input.value;
      if (!text.trim()) return;
      input.value = '';
      await sendPhoneText(text);
    });

    document.getElementById('btn-status').addEventListener('click', () => sendPhoneText('/status'));
    document.getElementById('btn-list').addEventListener('click', () => sendPhoneText('/list'));
    document.getElementById('btn-go').addEventListener('click', () => sendPhoneText('go'));

    function renderColleagueFeed(state) {
      const sig = JSON.stringify(state.messages);
      if (sig === STATE_SIGNATURES.left) return;
      STATE_SIGNATURES.left = sig;

      const feed = document.getElementById('colleague-feed');
      feed.replaceChildren();
      if (!state.messages.length) {
        const empty = document.createElement('div');
        empty.className = 'empty';
        empty.textContent = 'No messages yet. Type below to message as an outside colleague.';
        feed.append(empty);
        return;
      }

      for (const m of state.messages) {
        const card = document.createElement('div');
        card.className = 'msg-card' + (m.deleted ? ' recalled' : '');

        const top = document.createElement('div');
        top.className = 'msg-top';

        const leftMeta = document.createElement('span');
        const senderSpan = document.createElement('span');
        senderSpan.className = 'msg-sender';
        senderSpan.textContent = m.sender_name + ' ';
        const infoSpan = document.createElement('span');
        infoSpan.textContent = '(' + m.message_id + ' · ' + m.create_time + ')' + (m.mentions_owner ? ' [@Owner]' : '') + (m.reply_to_id ? ' (reply->' + m.reply_to_id + ')' : '');
        leftMeta.append(senderSpan, infoSpan);

        const rightMeta = document.createElement('span');
        if (m.status_label) {
          const badge = document.createElement('span');
          badge.className = 'badge';
          badge.textContent = m.status_label;
          rightMeta.append(badge);
        }
        if (!m.deleted) {
          const replyBtn = document.createElement('button');
          replyBtn.type = 'button';
          replyBtn.className = 'small';
          replyBtn.textContent = 'Quote';
          replyBtn.addEventListener('click', () => {
            SELECTED_REPLY_ID = m.message_id;
            document.getElementById('reply-target-label').textContent = 'Quoting ' + m.message_id;
            document.getElementById('colleague-input').focus();
          });

          const recallBtn = document.createElement('button');
          recallBtn.type = 'button';
          recallBtn.className = 'small';
          recallBtn.textContent = 'Recall';
          recallBtn.addEventListener('click', async () => {
            await apiJson('/api/colleague/recall', { mode: state.active_mode, message_id: m.message_id });
            await refreshState();
          });
          rightMeta.append(' ', replyBtn, ' ', recallBtn);
        }

        top.append(leftMeta, rightMeta);
        card.append(top);

        const body = document.createElement('div');
        body.className = 'msg-body';
        body.textContent = m.clean_text || '';
        card.append(body);

        if (m.image_url) {
          const img = document.createElement('img');
          img.className = 'msg-img';
          img.src = m.image_url;
          img.alt = 'Attached image';
          card.append(img);
        }
        feed.append(card);
      }
      feed.scrollTop = feed.scrollHeight;
    }

    function renderPhoneFeed(state) {
      const sig = JSON.stringify(state.phone_bubbles);
      if (sig === STATE_SIGNATURES.right) return;
      STATE_SIGNATURES.right = sig;

      const feed = document.getElementById('phone-feed');
      feed.replaceChildren();
      if (!state.phone_bubbles.length) {
        const empty = document.createElement('div');
        empty.className = 'empty';
        empty.textContent = 'Phone inbox is quiet. Watchpost pings here only when real action or triage is needed.';
        feed.append(empty);
        return;
      }

      for (const item of state.phone_bubbles) {
        const bubble = document.createElement('div');
        bubble.className = 'phone-bubble ' + item.kind;
        bubble.dataset.token = item.id;
        if (PHONE_REPLY_TOKEN && item.id === PHONE_REPLY_TOKEN) {
          bubble.classList.add('selected');
        }

        const top = document.createElement('div');
        top.className = 'msg-top';
        const title = document.createElement('span');
        title.className = 'msg-sender';
        title.textContent = (item.kind === 'owner' ? 'You' : item.channel_name) + ' · ' + item.id;
        const meta = document.createElement('span');
        meta.textContent = [item.triage_mode, item.created_time].filter(Boolean).join(' · ');
        top.append(title, meta);

        const body = document.createElement('div');
        body.className = 'msg-body';
        const textId = 'bubble-text-' + item.id;
        body.id = textId;
        body.textContent = item.text;

        bubble.append(top, body);

        if (item.kind === 'draft') {
          const actions = document.createElement('div');
          actions.className = 'draft-actions';

          const copyWrap = document.createElement('span');
          copyWrap.className = 'copy';

          const copyBtn = document.createElement('button');
          copyBtn.type = 'button';
          copyBtn.className = 'copy-button';
          copyBtn.setAttribute('command', '--copy');
          copyBtn.setAttribute('commandfor', textId);
          copyBtn.setAttribute('aria-label', 'Copy suggested reply draft ' + item.id);
          copyBtn.textContent = 'Copy Draft';

          const tip = document.createElement('span');
          tip.className = 'tip';
          tip.setAttribute('role', 'status');

          copyWrap.append(copyBtn, tip);
          actions.append(copyWrap);
          bubble.append(actions);
        }

        if (item.thread_id && item.kind !== 'owner') {
          bubble.addEventListener('click', (ev) => {
            if (ev.target.closest('button')) return;
            PHONE_REPLY_TOKEN = item.id;
            updatePhoneReplyBanner();
          });
        }
        feed.append(bubble);
      }
      feed.scrollTop = feed.scrollHeight;
    }

    async function refreshState() {
      try {
        const state = await apiJson('/api/state');
        const hSig = JSON.stringify([state.backend, state.cwd, state.active_mode, state.autonomy_mode, state.activity, state.pending_debounce, state.has_pending_go]);
        if (hSig !== STATE_SIGNATURES.header) {
          STATE_SIGNATURES.header = hSig;
          document.getElementById('header-meta').textContent =
            'backend=' + state.backend + ' | cwd=' + state.cwd + ' | debounce=' + state.debounce_s + 's';
          document.getElementById('mode-select').value = state.active_mode;
          document.getElementById('autonomy-select').value = state.autonomy_mode;
          document.getElementById('left-title').textContent =
            state.active_mode === 'group' ? 'Outside Group (@Owner required)' : 'Outside Colleague (DM)';
          document.getElementById('at-owner-chk').disabled = state.active_mode !== 'group';

          const pill = document.getElementById('activity-pill');
          pill.dataset.state = state.activity;
          if (state.activity === 'thinking') {
            pill.textContent = 'Agent thinking...';
          } else if (state.activity === 'debouncing' || state.pending_debounce > 0) {
            pill.dataset.state = 'debouncing';
            pill.textContent = 'Debouncing (' + state.debounce_s + 's)...';
          } else {
            pill.textContent = 'Idle';
          }
          document.getElementById('btn-go').hidden = !state.has_pending_go;
        }
        renderColleagueFeed(state);
        renderPhoneFeed(state);
      } catch {
        // Ignore transient poll hiccup while shutting down
      }
    }

    refreshState();
    setInterval(refreshState, 900);
  </script>
</body>
</html>
"""


def build_sandbox_ui_backend(backend_name: str, *, language: str = "zh") -> AgentBackend:
    if backend_name == "mock":
        return MockAgentBackend(language=language)
    return CLIAgentBackend(backend_name, timeout_s=180.0)


def create_sandbox_ui_app(
    sandbox_dir: Path | str,
    *,
    backend: AgentBackend | None = None,
    backend_name: str = "mock",
    workspace_dir: Path | str = ".",
    autonomy: AutonomyMode = AutonomyMode.diagnose_only,
    debounce_s: float = 5.0,
    language: str = "zh",
) -> FastAPI:
    """Build an isolated 2-column Watchpost dev-simulator FastAPI app."""
    sb_dir = Path(sandbox_dir).expanduser()
    sb_dir.mkdir(parents=True, exist_ok=True)
    uploads_dir = sb_dir / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    ws_dir = Path(workspace_dir).expanduser()
    store = WatchpostStore(sb_dir / "sandbox.db")
    adapter = LocalSandboxAdapter(sb_dir)
    notifier = SimulatorStateNotifier(store, language=language)
    chosen_backend = backend or build_sandbox_ui_backend(backend_name, language=language)
    engine = WatchpostEngine(
        store,
        chosen_backend,
        config=EngineConfig(
            workspace_dir=ws_dir,
            language=language,
            debounce_s=float(debounce_s),
            max_debounce_s=max(float(debounce_s) * 3.0, float(debounce_s)),
        ),
        adapters={"sandbox": adapter},
        notifiers=[notifier],
        media_dir=sb_dir / "media",
    )

    for ch in default_sandbox_channels(autonomy=autonomy):
        if store.get_channel(ch.id) is None:
            store.upsert_channel(ch)

    ui_state: dict[str, Any] = {"active_mode": "dm", "sender_name": "Alex"}
    run_lock = asyncio.Lock()
    background_tasks: set[asyncio.Task[Any]] = set()

    def _spawn(coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return task

    def _finalize_messages(msgs_or_ids: list[Any]) -> None:
        triaged_ids: set[str] = set()
        for th in store.list_threads(limit=50):
            triaged_ids.update(th.message_ids)
        for item in msgs_or_ids:
            mid = item if isinstance(item, str) else getattr(item, "message_id", "")
            if not mid:
                continue
            if mid in triaged_ids:
                notifier.msg_status[mid] = "triaged"
            elif notifier.msg_status.get(mid, "") in {"debouncing", "thinking", ""}:
                notifier.msg_status[mid] = "filtered_small_talk"
        notifier.activity = "idle"

    orig_debouncer_cb = engine.debouncer._on_flush

    async def _tracked_debouncer_cb(key: str, items: list[Any]) -> None:
        async with run_lock:
            try:
                await orig_debouncer_cb(key, items)
            except Exception as exc:
                notifier.last_error = str(exc)
            finally:
                _finalize_messages(items)

    engine.debouncer._on_flush = _tracked_debouncer_cb

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        yield
        engine.debouncer.cancel_all()
        for task in list(background_tasks):
            task.cancel()

    app = FastAPI(title="Watchpost Dev Simulator", version=__version__, lifespan=_lifespan)
    app.state.sandbox_engine = engine
    app.state.sandbox_notifier = notifier
    app.state.sandbox_adapter = adapter

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
            "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    def _channel_id(mode: str) -> str:
        return "sandbox_group" if mode == "group" else "sandbox_dm"

    def _status_label(m: Any) -> str:
        if m.deleted:
            return "recalled"
        raw = notifier.msg_status.get(m.message_id, "")
        labels = {
            "debouncing": "debouncing...",
            "thinking": "thinking...",
            "triaged": "sent to agent",
            "filtered_small_talk": "ignored (small talk)",
            "saved_group_context": "context only (no @owner)",
        }
        return labels.get(raw, raw)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        nonce = getattr(request.state, "csp_nonce", "")
        return HTMLResponse(_SANDBOX_HTML.replace("__CSP_NONCE__", nonce))

    @app.get("/api/media/{filename}")
    async def get_uploaded_media(filename: str) -> FileResponse:
        safe = Path(filename).name
        target = (uploads_dir / safe).resolve()
        root_resolved = uploads_dir.resolve()
        if not target.is_file() or not target.is_relative_to(root_resolved):
            raise HTTPException(status_code=404, detail="Media not found")
        return FileResponse(target)

    @app.get("/api/state")
    async def get_state() -> dict[str, Any]:
        mode = ui_state["active_mode"]
        cid = _channel_id(mode)
        ch = store.get_channel(cid)
        msgs = adapter.list_messages(cid, limit=100)
        threads = store.list_threads(limit=30)
        pending_count = engine.debouncer.pending_count()
        activity = notifier.activity
        if activity == "idle" and pending_count > 0:
            activity = "debouncing"

        serialized_msgs: list[dict[str, Any]] = []
        for m in msgs:
            img_url = ""
            for ref in m.media_refs:
                lp = str(ref.extra.get("local_path") or "")
                if lp and Path(lp).resolve().is_relative_to(uploads_dir.resolve()):
                    img_url = f"/api/media/{Path(lp).name}"
                    break
            serialized_msgs.append(
                {
                    "message_id": m.message_id,
                    "sender_name": m.sender_name,
                    "create_time": m.create_time,
                    "clean_text": m.clean_text,
                    "reply_to_id": m.reply_to_id,
                    "mentions_owner": m.mentions_owner,
                    "deleted": m.deleted,
                    "status_label": _status_label(m),
                    "image_url": img_url,
                }
            )

        return {
            "backend": chosen_backend.name,
            "cwd": str(ws_dir),
            "active_mode": mode,
            "autonomy_mode": ch.autonomy_mode.value if ch else autonomy.value,
            "debounce_s": engine.config.debounce_s,
            "activity": activity,
            "pending_debounce": pending_count,
            "has_pending_go": any(t.pending_go for t in threads),
            "messages": serialized_msgs,
            "phone_bubbles": notifier.phone_entries[-100:],
            "threads": [t.model_dump(mode="json") for t in threads],
        }

    @app.post("/api/settings")
    async def update_settings(body: SettingsBody) -> dict[str, Any]:
        if body.mode is not None:
            ui_state["active_mode"] = body.mode
        if body.sender_name is not None and body.sender_name.strip():
            ui_state["sender_name"] = body.sender_name.strip()
        cid = _channel_id(ui_state["active_mode"])
        if body.autonomy_mode is not None:
            store.update_channel_settings(cid, autonomy_mode=body.autonomy_mode)
        return {"ok": True, "active_mode": ui_state["active_mode"]}

    async def _ingest_colleague_now(cid: str, msg_id: str) -> None:
        ch = store.get_channel(cid)
        if ch is None:
            return
        async with run_lock:
            try:
                fetched = await adapter.fetch_recent(ch)
                await engine.ingest_messages(cid, fetched, debounced=False)
            finally:
                _finalize_messages([msg_id])

    @app.post("/api/colleague/send")
    async def colleague_send(body: ColleagueSendBody) -> dict[str, Any]:
        ui_state["active_mode"] = body.mode
        cid = _channel_id(body.mode)
        ch = store.get_channel(cid)
        if ch is None:
            raise HTTPException(status_code=404, detail="Channel missing")

        saved_img_path: Path | None = None
        if body.image_data_url.strip():
            m = _DATA_URL_RE.match(body.image_data_url.strip())
            if not m:
                raise HTTPException(status_code=400, detail="Unsupported image data URL")
            ext = ".jpg" if m.group(1).lower() in {"jpeg", "jpg"} else f".{m.group(1).lower()}"
            raw = base64.b64decode(m.group(2), validate=True)
            if len(raw) > _MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="Image exceeds 8 MB limit")
            stem = _SAFE_IMG_RE.sub("_", Path(body.image_name or "upload").stem).strip("._") or "upload"
            saved_img_path = uploads_dir / f"{secrets.token_hex(6)}_{stem}{ext}"
            saved_img_path.write_bytes(raw)

        mentions_owner = bool(body.mentions_owner or re.search(r"(?:^|\s)@(?:me|owner|lead)\b", body.text, re.I))
        msg = adapter.append_message(
            cid,
            sender_name=body.sender_name or ui_state["sender_name"],
            text=body.text,
            reply_to_id=body.reply_to_id,
            mentions_owner=mentions_owner,
            image_path=saved_img_path,
        )

        if ch.chat_type == ChatType.group and ch.trigger_mode == TriggerMode.mention_owner and not msg.mentions_owner:
            notifier.msg_status[msg.message_id] = "saved_group_context"
            fetched = await adapter.fetch_recent(ch)
            await engine.ingest_messages(cid, fetched, debounced=False)
            return {"message_id": msg.message_id, "queued": "context_only"}

        use_debounce = (not body.immediate) and engine.config.debounce_s > 0
        if use_debounce:
            notifier.msg_status[msg.message_id] = "debouncing"
            notifier.activity = "debouncing"
            fetched = await adapter.fetch_recent(ch)
            await engine.ingest_messages(cid, fetched, debounced=True)
            return {"message_id": msg.message_id, "queued": "debouncing"}

        await _ingest_colleague_now(cid, msg.message_id)
        return {"message_id": msg.message_id, "queued": "processed"}

    @app.post("/api/colleague/flush")
    async def colleague_flush(body: FlushBody | None = None) -> dict[str, Any]:
        should_wait = True if body is None else body.wait

        async def _do_flush() -> int:
            return await engine.flush_all()

        if should_wait:
            count = await _do_flush()
            return {"flushed": count}
        _spawn(_do_flush())
        return {"flushed": "scheduled"}

    @app.post("/api/colleague/recall")
    async def colleague_recall(body: ColleagueRecallBody) -> dict[str, Any]:
        cid = _channel_id(body.mode)
        rec = adapter.recall_message(cid, body.message_id)
        notifier.msg_status[body.message_id] = "recalled"
        notifier.msg_status[rec.message_id] = "recalled"
        ch = store.get_channel(cid)
        if ch is not None:
            await engine.ingest_messages(cid, await adapter.fetch_recent(ch), debounced=False)
        return {"recalled": body.message_id, "event_id": rec.message_id}

    @app.post("/api/phone/send")
    async def phone_send(body: PhoneSendBody) -> dict[str, Any]:
        clean = body.text.strip()
        if not clean:
            raise HTTPException(status_code=400, detail="Empty phone message")
        action = classify_owner_command(clean, notifier=notifier.name, reply_token=body.reply_token.strip())
        if action is None:
            raise HTTPException(status_code=400, detail="Invalid command")

        tok = notifier._next_token()
        notifier.phone_entries.append(
            {
                "id": tok,
                "kind": "owner",
                "text": clean,
                "thread_id": "",
                "channel_name": "You",
                "triage_mode": "",
                "status": "sent",
                "pending_go": False,
                "created_time": notifier._now_hhmmss(),
            }
        )

        async def _dispatch() -> None:
            async with run_lock:
                try:
                    if action.kind in {"steer", "approve_go"}:
                        notifier.activity = "thinking"
                    await engine.handle_owner_action(action)
                finally:
                    notifier.activity = "idle"

        if body.wait:
            await _dispatch()
            return {"ok": True, "bubble_id": tok, "kind": action.kind}
        _spawn(_dispatch())
        return {"ok": True, "bubble_id": tok, "kind": action.kind, "scheduled": True}

    return app
