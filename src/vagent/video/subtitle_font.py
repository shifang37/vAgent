"""The bundled OFL subtitle font: one pinned file, verified by SHA-256.

E1 ships a subset of Noto Sans SC, instanced at weight 400 and renamed to
``VAgent Subtitle SC`` because the OFL forbids a modified version from using the
reserved Noto family name. ``scripts/build_subtitle_font.py`` rebuilds it from a
local source font; the repository only carries the subset, its licence and its
manifest. The renderer refuses to burn subtitles when the file is missing or its
digest changed (``FONT_MISSING``) instead of silently using a system font.
"""

import hashlib
import os
import stat
from pathlib import Path

from pydantic import Field

from vagent.contracts import Contract, Fingerprint
from vagent.errors import AppError

FONT_DIR = Path(__file__).with_name("fonts")
FONT_FILE = "VAgentSubtitleSC-400.ttf"
FONT_NAME = "VAgent Subtitle SC"
FONT_SHA256 = "3cb33efa90a2c6ee51fdf9fda02cb071e812260d09a42a9c23774ab92d5606cb"
FONT_SIZE_BYTES = 1_213_160
LICENSE_FILE = "LICENSE-OFL.txt"
MANIFEST_FILE = "manifest.json"
MESSAGES = {"FONT_MISSING": "包内字幕字体缺失或校验失败，请重新安装完整程序。"}


class FontRef(Contract):
    """A font identity frozen into a subtitle track and a render record."""

    name: str = Field(min_length=1, max_length=80)
    sha256: Fingerprint


def font_ref() -> FontRef:
    return FontRef(name=FONT_NAME, sha256=FONT_SHA256)


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def bundled_font_path() -> Path:
    """Locate and hash the packaged font; only a regular file inside ``fonts``."""
    path = FONT_DIR / FONT_FILE
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise AppError("FONT_MISSING", MESSAGES["FONT_MISSING"]) from None
    if resolved.parent != FONT_DIR.resolve():
        raise AppError("FONT_MISSING", MESSAGES["FONT_MISSING"])
    info = resolved.stat()
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise AppError("FONT_MISSING", MESSAGES["FONT_MISSING"])
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if info.st_size != FONT_SIZE_BYTES or digest.hexdigest() != FONT_SHA256:
        raise AppError("FONT_MISSING", MESSAGES["FONT_MISSING"])
    return resolved


def verify_font_ref(reference: FontRef) -> None:
    """A frozen record may only cite the one bundled font identity."""
    if reference != font_ref():
        raise AppError("FONT_MISSING", MESSAGES["FONT_MISSING"])
