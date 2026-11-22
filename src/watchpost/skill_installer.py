"""Install or cleanly uninstall the bundled Watchpost Agent Skill across CLI harnesses."""

from __future__ import annotations

import shutil
from pathlib import Path

SKILL_TARGETS: dict[str, Path] = {
    "claude": Path.home() / ".claude" / "skills" / "watchpost",
    "codex": Path.home() / ".codex" / "skills" / "watchpost",
    "antigravity": Path.home() / ".agents" / "skills" / "watchpost",
}


def get_bundled_skill_path() -> Path:
    path = Path(__file__).resolve().parent / "skills" / "watchpost" / "SKILL.md"
    if not path.is_file():
        raise FileNotFoundError(f"Bundled Watchpost SKILL.md not found at {path}")
    return path


def resolve_skill_dir(target: str = "claude", *, dest: Path | str | None = None) -> Path:
    if dest is not None:
        return Path(dest).expanduser()
    if target not in SKILL_TARGETS:
        choices = ", ".join(sorted(SKILL_TARGETS))
        raise ValueError(f"Unknown harness target {target!r}; choose from: {choices} (or pass --dest)")
    return SKILL_TARGETS[target]


def install_skill(*, target: str = "claude", dest: Path | str | None = None) -> Path:
    """Copy bundled SKILL.md into the target harness skill folder and return its path."""
    src = get_bundled_skill_path()
    target_dir = resolve_skill_dir(target, dest=dest)
    target_dir.mkdir(parents=True, exist_ok=True)
    out = target_dir / "SKILL.md"
    shutil.copyfile(src, out)
    return out


def uninstall_skill(*, target: str = "claude", dest: Path | str | None = None) -> bool:
    """Remove installed SKILL.md (and empty Watchpost folder) without touching sibling skills."""
    target_dir = resolve_skill_dir(target, dest=dest)
    skill_file = target_dir / "SKILL.md"
    if not skill_file.exists():
        return False
    skill_file.unlink()
    try:
        if target_dir.is_dir() and not any(target_dir.iterdir()):
            target_dir.rmdir()
    except OSError:
        pass
    return True
