"""Tests for config placeholder expansion, 0600 saving, skill installer, and CLI commands."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

from watchpost.cli import main as cli_main
from watchpost.config import init_config_file, load_config, load_raw_config
from watchpost.daemon import run_doctor


def test_init_config_permissions_env_expansion_and_cli_channels() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cfg_path = root / "config.json"

        saved = init_config_file(cfg_path)
        assert saved == cfg_path
        mode = stat.S_IMODE(os.stat(cfg_path).st_mode)
        assert mode == 0o600

        # Cannot overwrite without --force
        assert cli_main(["init-config", "--config", str(cfg_path)]) == 1
        assert cli_main(["init-config", "--config", str(cfg_path), "--force"]) == 0

        # Add & list & remove channel via CLI while keeping ${ENV_VAR} placeholders intact on disk
        rc_add = cli_main(
            [
                "channels",
                "add",
                "--config",
                str(cfg_path),
                "--adapter",
                "feishu",
                "--chat-id",
                "oc_demo_team",
                "--name",
                "Support Group",
                "--chat-type",
                "group",
                "--trigger",
                "mention_owner",
                "--autonomy",
                "diagnose_only",
                "--persona",
                "Backend rollout checks.",
            ]
        )
        assert rc_add == 0
        raw_cfg = load_raw_config(cfg_path)
        assert raw_cfg.feishu.app_id == "${FEISHU_APP_ID}"
        assert len(raw_cfg.channels) == 1
        cid = raw_cfg.channels[0].id

        resolved = load_config(
            cfg_path,
            env={
                "FEISHU_APP_ID": "cli_test_id",
                "FEISHU_APP_SECRET": "cli_test_secret",
                "TELEGRAM_BOT_TOKEN": "123:abc",
                "TELEGRAM_OWNER_CHAT_ID": "998877",
            },
        )
        assert resolved.feishu.app_id == "cli_test_id"
        assert resolved.telegram.owner_chat_id == "998877"

        assert cli_main(["channels", "list", "--config", str(cfg_path), "--json"]) == 0
        assert cli_main(["channels", "remove", "--config", str(cfg_path), "--id", cid]) == 0
        assert len(load_raw_config(cfg_path).channels) == 0


def test_install_skill_and_doctor() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        skill_dest = root / "skills" / "watchpost"
        sibling_dir = root / "skills" / "other-skill"
        sibling_dir.mkdir(parents=True)
        (sibling_dir / "SKILL.md").write_text("sibling", encoding="utf-8")

        assert cli_main(["install-skill", "--dest", str(skill_dest)]) == 0
        skill_md = skill_dest / "SKILL.md"
        assert skill_md.is_file()
        content = skill_md.read_text(encoding="utf-8")
        assert "name: watchpost" in content
        assert "GPL-3.0" in content

        assert cli_main(["install-skill", "--dest", str(skill_dest), "--uninstall"]) == 0
        assert not skill_md.exists()
        assert (sibling_dir / "SKILL.md").is_file()

        # Doctor on missing vs valid mock config
        missing_report = run_doctor(root / "missing.json")
        assert missing_report["ok"] is False

        valid_cfg = {
            "db_path": str(root / "wp.db"),
            "media_dir": str(root / "media"),
            "workspace_dir": str(root),
            "backend": {"preset": "mock"},
            "feishu": {"enabled": False},
            "telegram": {"enabled": False},
            "imessage": {"enabled": False},
            "channels": [],
        }
        cfg_file = root / "valid.json"
        cfg_file.write_text(json.dumps(valid_cfg), encoding="utf-8")
        assert cli_main(["doctor", "--config", str(cfg_file), "--json"]) == 0
