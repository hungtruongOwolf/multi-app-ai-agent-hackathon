"""Where runtime state lives (SQLite databases, memory git repos, ShopLab data).

SQLite in WAL mode is unreliable on exFAT/FAT volumes (external SSDs are often formatted that way):
under concurrent writers it fails with "disk I/O error". If the project sits on such a volume, runtime
state defaults to the user's local app data on a proper filesystem instead. IJ_VAR_DIR always wins."""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # incident-judge/
REPO_ROOT = ROOT.parent                          # monorepo root
KNOWLEDGE_DIR = REPO_ROOT / "knowledge"          # the LLM Wiki: architecture, service docs, runbooks
_UNSAFE_FS = {"exfat", "fat", "fat32", "vfat", "msdos"}


def _filesystem_name(path: Path) -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        drive = path.anchor  # e.g. "E:\\"
        buf = ctypes.create_unicode_buffer(64)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(drive), None, 0, None, None, None, buf, len(buf))
        return buf.value.lower() if ok else None
    except Exception:
        return None


@lru_cache(maxsize=1)
def runtime_dir() -> Path:
    env = os.environ.get("IJ_VAR_DIR")
    if env:
        return Path(env)
    fs = _filesystem_name(ROOT)
    if fs in _UNSAFE_FS:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "incident-judge" / "var"
    return ROOT / "var"


def reports_dir() -> Path:
    """Reports are plain files; keep them next to the project so they are easy to find."""
    return ROOT / "var" / "reports"
