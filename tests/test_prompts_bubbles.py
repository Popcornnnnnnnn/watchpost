"""Tests for Watchpost prompts, Markdown stripping, and CJK/EN bubble splitting."""

from __future__ import annotations

from watchpost.bubbles import extract_sections, format_completion_bubbles, split_into_bubbles, strip_markdown
from watchpost.models import AutonomyMode, ChannelConfig, ChatType, NormalizedMessage
from watchpost.prompts import build_new_thread_prompt, build_owner_steer_prompt, build_personal_direct_prompt


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

    inline_sec = extract_sections("总结：当前目录正常。\n\n需要注意：无")
    assert inline_sec["summary"] == "当前目录正常。"
    assert inline_sec["attention"] == "无"
    assert extract_sections("Summary of changes looks good\n\n需要注意的事情很多") == {}

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

    personal_inline, inline_draft = format_completion_bubbles(
        "Direct",
        "总结：当前目录正常。\n\n需要注意：无",
        language="zh",
        include_header=False,
    )
    assert personal_inline == ["当前目录正常。"]
    assert inline_draft == ""

    personal_plain, plain_draft = format_completion_bubbles(
        "Direct",
        "目前正常，可以直接改代码。",
        language="zh",
        include_header=False,
    )
    assert personal_plain == ["目前正常，可以直接改代码。"]
    assert plain_draft == ""


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
        assert "EXPLICIT LANGUAGE REQUEST" in prompt and "简体中文" in prompt
        assert "总结：" in prompt and "建议回复：" in prompt
        assert expected in prompt
        if mode == AutonomyMode.auto_fix:
            assert "Local code & verification allowed" in prompt
            assert 'WAIT FOR `"go"` BEFORE DEPLOY' in prompt

    go_prompt = build_owner_steer_prompt("go ahead", approved_go=True, language="en")
    assert "APPROVED execution" in go_prompt
    assert "Summary:" in go_prompt

    go_prompt_zh = build_owner_steer_prompt("go ahead", approved_go=True, language="zh")
    assert "EXPLICIT LANGUAGE REQUEST" in go_prompt_zh
    assert "简体中文" in go_prompt_zh


def test_personal_direct_prompt_enforces_chinese_override_and_english_fallback() -> None:
    zh_new = build_personal_direct_prompt(
        "看看这张图",
        notifier="imessage",
        workspace_dir="/tmp/ws",
        is_steer=False,
        language="zh",
    )
    assert "EXPLICIT LANGUAGE REQUEST" in zh_new
    assert "简体中文" in zh_new
    assert "禁止使用任何 Markdown" in zh_new
    assert "不要附带对外回复草稿" in zh_new
    assert "严禁输出" in zh_new and "总结：" in zh_new and "需要注意：" in zh_new
    assert "当前工作区：/tmp/ws" in zh_new

    zh_steer = build_personal_direct_prompt(
        "换个角度查一下",
        notifier="imessage",
        workspace_dir="/tmp/ws",
        is_steer=True,
        language="zh",
    )
    assert "EXPLICIT LANGUAGE REQUEST" in zh_steer
    assert "简体中文" in zh_steer
    assert "禁止使用任何 Markdown" in zh_steer
    assert "不要附带对外回复草稿" in zh_steer
    assert "严禁输出" in zh_steer and "总结：" in zh_steer and "需要注意：" in zh_steer
    assert "对话跟进" in zh_steer
    assert "个人直聊" not in zh_new and "个人直聊" not in zh_steer
    assert "当前工作区" not in zh_steer

    en_new = build_personal_direct_prompt(
        "check this photo",
        notifier="imessage",
        workspace_dir="/tmp/ws",
        is_steer=False,
        language="en",
    )
    assert "EXPLICIT LANGUAGE REQUEST" not in en_new
    assert "简体中文" not in en_new
    assert "NEVER output robotic template labels" in en_new
    assert "Summary:" in en_new and "Needs Attention:" in en_new
    assert "Do NOT use any Markdown formatting symbols" in en_new
    assert "Workspace: /tmp/ws" in en_new
