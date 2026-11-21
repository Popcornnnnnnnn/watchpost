"""Local Whisper speech-to-text processor and bounded media pruning."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

_NOISE_LINES = {
    "[BLANK_AUDIO]",
    "(BLANK AUDIO)",
    "[SILENCE]",
    "(SILENCE)",
    "[MUSIC]",
    "(MUSIC)",
}


def clean_whisper_output(raw: str) -> str:
    lines: list[str] = []
    for line in (raw or "").splitlines():
        s = line.strip()
        if not s or s.upper() in _NOISE_LINES:
            continue
        lines.append(s)
    return " ".join(lines).strip()


def prune_old_media(media_dir: Path, *, max_age_days: int = 14) -> int:
    """Remove staged media artifacts older than max_age_days."""
    root = Path(media_dir)
    if not root.is_dir() or max_age_days <= 0:
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for item in root.rglob("*"):
        if item.is_file():
            try:
                if item.stat().st_mtime < cutoff:
                    item.unlink()
                    removed += 1
            except OSError:
                continue
    return removed


class LocalWhisperProcessor:
    """Offline voice transcription via ffmpeg + whisper.cpp (`whisper-cli`)."""

    def __init__(
        self,
        *,
        model_path: Path | None = None,
        whisper_bin: str = "",
        ffmpeg_bin: str = "",
    ) -> None:
        self.model_path = Path(model_path) if model_path else None
        self.whisper_bin = whisper_bin or shutil.which("whisper-cli") or "whisper-cli"
        self.ffmpeg_bin = ffmpeg_bin or shutil.which("ffmpeg") or "ffmpeg"

    def transcribe_audio(self, audio_path: Path, *, prompt: str = "", language: str = "zh") -> tuple[str, str]:
        src = Path(audio_path)
        if not src.is_file():
            return "", f"Audio file missing: {src}"
        if self.model_path is None or not self.model_path.is_file():
            return "", "Whisper model not configured"
        wav_path = src.with_suffix(".wav")
        try:
            ff = subprocess.run(
                [self.ffmpeg_bin, "-y", "-i", str(src), "-ar", "16000", "-ac", "1", str(wav_path)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if ff.returncode != 0 or not wav_path.is_file():
                return "", f"ffmpeg failed (rc={ff.returncode})"
            cmd = [self.whisper_bin, "-m", str(self.model_path), "-l", language, "-nt", "-np", "-f", str(wav_path)]
            if prompt.strip():
                cmd.extend(["--prompt", prompt.strip()])
            wh = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if wh.returncode != 0:
                return "", f"whisper-cli failed (rc={wh.returncode})"
            return clean_whisper_output(wh.stdout), ""
        except OSError as exc:
            return "", f"Local ASR binary unavailable: {exc}"
