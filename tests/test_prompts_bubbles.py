"""Tests for Watchpost prompts, Markdown stripping, and CJK/EN bubble splitting."""

from __future__ import annotations

from watchpost.bubbles import extract_sections, format_completion_bubbles, split_into_bubbles, strip_markdown
from watchpost.models import AutonomyMode, ChannelConfig, ChatType, NormalizedMessage
from watchpost.prompts import build_new_thread_prompt, build_owner_steer_prompt


def test_markdown_strip_and_bilingual_bubble_splitting() -> None:
    raw = "### Status\n**Fixed** `timeout.rs` in [notes.md](file:///tmp/notes.md).\n* See [PR](https://example.com/1)"
    clean = strip_markdown(raw)
    assert "**" not in clean and "`" not in clean and "###" not in clean and "file://" not in clean
    assert "- See PR (https://example.com/1)" in clean

    long_cn = (
        "刚才查了2号工位日志，现在卡在机械臂握手等待这一步。"
        "视觉传感器三个轴都显示到位了，但控制器里的就绪标志一直没置位。"
        "刚才触发过一次重试，紧接着又超出了十秒阈值导致流程暂停。"
    )
    cn_bubbles = split_into_bubbles(long_cn, max_bubbles=4, soft_bubble_len=80)
    assert 2 <= len(cn_bubbles) <= 4
    assert all(len(b) <= 85 for b in cn_bubbles)
    assert "。 " not in "".join(cn_bubbles)

    sec = extract_sections("总结：\n已定位问题。\n\n建议回复：\n先试下重启。\n\n需要注意：\n无")
    assert sec["summary"] == "已定位问题。"
    assert sec["suggested_reply"] == "先试下重启。"
    assert sec["attention"] == "无"

    bubbles, draft = format_completion_bubbles(
        "Alex",
        "总结：\n已定位 **超时** 问题。\n\n建议回复：\n我刚调整了超时参数，等空闲确认。\n\n需要注意：\n无",
        language="zh",
    )
    assert len(bubbles) == 2
    assert bubbles[0].startswith("【Alex】\n已定位 超时 问题。")
    assert "下面这条可直接复制回复：" in bubbles[0]
    assert bubbles[1] == "我刚调整了超时参数，等空闲确认。"
    assert draft == "我刚调整了超时参数，等空闲确认。"


def test_prompts_enforce_autonomy_gates_and_untrusted_boundary() -> None:
    msg = NormalizedMessage(
        message_id="m1",
        sender_id="u1",
        sender_name="Alex",
        create_time="10:00:00",
        clean_text="Bay 2 failed again",
    )
    for mode, expected in (
        (AutonomyMode.assistant_only, "Channel Autonomy Gate (`assistant_only`)"),
        (AutonomyMode.diagnose_only, 'WAIT FOR `"go"` APPROVAL'),
        (AutonomyMode.auto_fix, "Channel Autonomy Gate (`auto_fix`)"),
    ):
        ch = ChannelConfig(
            id="c1",
            adapter="feishu",
            external_chat_id="x1",
            name="Alex",
            chat_type=ChatType.p2p,
            autonomy_mode=mode,
        )
        prompt = build_new_thread_prompt(ch, "Ops persona", [], [msg], language="zh")
        assert "Intent-First 4-Mode Triage" in prompt
        assert "UNTRUSTED EXTERNAL CHAT INPUT" in prompt
        assert "STRICTLY FORBIDDEN (Draft-Only)" in prompt
        assert "总结：" in prompt and "建议回复：" in prompt
        assert expected in prompt

    go_prompt = build_owner_steer_prompt("go ahead", approved_go=True, language="en")
    assert "APPROVED execution" in go_prompt
    assert "Summary:" in go_prompt
