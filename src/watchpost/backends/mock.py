"""Deterministic offline MockAgentBackend for tests, CI, and self-contained demos."""

from __future__ import annotations

import uuid
from pathlib import Path

from watchpost.bubbles import extract_sections, split_into_bubbles
from watchpost.models import AgentResult, AutonomyMode, ChannelConfig, TriageMode

_QA_KEYWORDS = ("?", "？", "吗", "更新", "几个", "几号", "多久", "eta", "status", "ready", "rollout", "version")
_BUG_KEYWORDS = ("bug", "error", "stuck", "timeout", "fail", "crash", "503", "异常", "报错", "卡住", "超时", "故障")


def _extract_focus_text(prompt: str) -> str:
    for marker in ("## Trigger Message(s)", "## New Message(s)", "## Follow-up Message(s)", "Owner note:"):
        idx = prompt.find(marker)
        if idx >= 0:
            tail = prompt[idx:]
            next_sec = tail.find("\n## ", len(marker))
            return tail[:next_sec] if next_sec > 0 else tail
    return prompt[-400:]


class MockAgentBackend:
    """Offline mock agent implementing 4-mode intent triage without external LLM calls."""

    name = "mock"

    def __init__(self, *, language: str = "zh") -> None:
        self.language = language
        self.calls: list[dict[str, str]] = []
        self._threads: dict[str, str] = {}

    async def start_thread(
        self,
        title: str,
        prompt: str,
        *,
        cwd: Path,
        model: str = "",
        channel: ChannelConfig | None = None,
    ) -> tuple[str, AgentResult]:
        del cwd, model
        ref = f"mock-{uuid.uuid4().hex[:12]}"
        self._threads[ref] = title
        self.calls.append({"action": "start", "ref": ref, "title": title, "prompt": prompt})
        res = self._synthesize(prompt, channel=channel)
        return ref, res

    async def steer_thread(
        self,
        backend_thread_ref: str,
        title: str,
        message: str,
        *,
        cwd: Path,
        channel: ChannelConfig | None = None,
    ) -> AgentResult:
        del cwd
        self.calls.append({"action": "steer", "ref": backend_thread_ref, "title": title, "prompt": message})
        return self._synthesize(message, channel=channel)

    def _synthesize(self, prompt: str, *, channel: ChannelConfig | None) -> AgentResult:
        mode = channel.autonomy_mode if channel else AutonomyMode.diagnose_only
        is_zh = self.language.lower().startswith("zh")
        focus = _extract_focus_text(prompt).lower()
        approved = "approved execution" in prompt.lower()

        if approved:
            if is_zh:
                raw = (
                    "总结：\n"
                    "已按确认方案完成服务配置核查与参数调整。\n\n"
                    "当前服务运行平稳，没有新的阻塞告警。\n\n"
                    "建议回复：\n"
                    "这边已经修复并验证好了，服务现在已恢复正常，有任何异常随时叫我。\n\n"
                    "需要注意：\n"
                    "无"
                )
            else:
                raw = (
                    "Summary:\n"
                    "Applied the approved fix and verified service metrics look healthy.\n\n"
                    "No active blockers remain.\n\n"
                    "Suggested Reply:\n"
                    "Fix is live and verified now. The API is back to normal.\n\n"
                    "Needs Attention:\n"
                    "None"
                )
            return self._parse_result(raw, TriageMode.mode_4_bug)

        is_qa = any(k in focus for k in _QA_KEYWORDS) and not any(k in focus for k in _BUG_KEYWORDS)
        has_image = "decoded image:" in prompt.lower() or "image" in focus
        if is_qa or mode == AutonomyMode.assistant_only:
            if is_zh:
                raw = (
                    "总结：\n"
                    "这是纯进度确认类问题，无需改代码或重启服务。\n\n"
                    "刚核对了最近发布的版本记录，Node 1 已切到 v2.1 并运行正常。\n\n"
                    "建议回复：\n"
                    "1号节点这边已经更新到 v2.1 了，目前状态正常，可以先按这个版本继续跑。\n\n"
                    "需要注意：\n"
                    "无"
                )
            else:
                raw = (
                    "Summary:\n"
                    "This is a pure rollout status question — no code edits or restarts needed.\n\n"
                    "Checked deployment state: Node 1 is already running v2.1 cleanly.\n\n"
                    "Suggested Reply:\n"
                    "Node 1 is already updated to v2.1 and running fine. Good to proceed.\n\n"
                    "Needs Attention:\n"
                    "None"
                )
            return self._parse_result(raw, TriageMode.mode_1_qa)

        attention_zh = (
            "该通道为只诊断模式，如需执行配置修改或重启服务，请点击 Approve (go)。"
            if mode == AutonomyMode.diagnose_only
            else "无"
        )
        attention_en = (
            "Channel is in diagnose_only mode — click Approve (go) before applying edits."
            if mode == AutonomyMode.diagnose_only
            else "None"
        )
        img_note_zh = "已结合昨晚关联截图定位，" if has_image else "已结合近期上下文日志排查，"
        img_note_en = "Reviewed the linked overnight screenshot; " if has_image else "Reviewed recent logs; "
        if is_zh:
            raw = (
                "总结：\n"
                f"{img_note_zh}本次超时主要卡在连接池闲置回收阈值偏短。\n\n"
                "建议先把服务日志确认清楚，再将 idle_timeout 从 10s 调整为 30s。\n\n"
                "建议回复：\n"
                "收到，昨晚截图和报错我都看了，主要是连接池闲置超时太短触发了 503。\n"
                "我先准备了调整方案，确认发布窗口后就能更新。\n\n"
                "需要注意：\n"
                f"{attention_zh}"
            )
        else:
            raw = (
                "Summary:\n"
                f"{img_note_en}the 503 timeout comes from a 10s connection-pool idle timeout firing early.\n\n"
                "Raising idle_timeout to 30s resolves it without masking real upstream failures.\n\n"
                "Suggested Reply:\n"
                "Got it — checked the overnight screenshot and logs. A 10s pool timeout tripped early; I have a 30s fix ready once you confirm deploy.\n\n"
                "Needs Attention:\n"
                f"{attention_en}"
            )
        return self._parse_result(raw, TriageMode.mode_4_bug)

    def _parse_result(self, raw_text: str, triage: TriageMode) -> AgentResult:
        sec = extract_sections(raw_text)
        summary_bubbles = split_into_bubbles(sec.get("summary", raw_text), max_bubbles=3)
        att = sec.get("attention", "").strip()
        if att.lower() in {"none", "n/a", "-", "无"}:
            att = ""
        return AgentResult(
            raw_text=raw_text,
            summary_bubbles=summary_bubbles,
            suggested_reply=sec.get("suggested_reply", "").strip(),
            needs_attention=att,
            triage_mode=triage,
        )
