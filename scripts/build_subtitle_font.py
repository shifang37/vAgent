"""Build the bundled OFL subtitle font subset from a local Noto Sans SC source.

The repository ships the **subset**, its OFL licence and a manifest; the 17 MB
source font is never committed. Run this only when the subset has to be rebuilt
(for example after widening the character coverage):

    .\\.venv\\Scripts\\python.exe scripts/build_subtitle_font.py ^
        --source C:\\Windows\\Fonts\\NotoSansSC-VF.ttf

The build is deterministic for one source file: the variable font is instanced at
weight 400, the family is renamed away from the reserved Noto name (OFL requires
that for modified versions), and the glyph set is a fixed list (ASCII, the GB2312
level-1 hanzi and the CJK punctuation the subtitle style uses). No network access
and no provider or model call happens here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from fontTools.subset import Options, Subsetter
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "src" / "vagent" / "video" / "fonts"
FONT_FILE = "VAgentSubtitleSC-400.ttf"
FAMILY = "VAgent Subtitle SC"
PUNCTUATION = "，。！？；：、“”‘’（）《》〈〉【】…—·～　￥%°"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fonttools_version() -> str:
    from fontTools import version

    return version


def charset() -> str:
    """ASCII, GB2312 level-1 hanzi and the punctuation the style profile uses."""
    chars = {chr(code) for code in range(0x20, 0x7F)}
    for high in range(0xB0, 0xD8):
        for low in range(0xA1, 0xFF):
            try:
                chars.add(bytes([high, low]).decode("gb2312"))
            except UnicodeDecodeError:
                continue
    chars.update(PUNCTUATION)
    return "".join(sorted(chars))


def build(source: Path, output: Path, *, weight: int) -> dict:
    raw = source.read_bytes()
    font = TTFont(source)
    axes = [axis.axisTag for axis in font["fvar"].axes] if "fvar" in font else []
    if "wght" in axes:
        font = instantiateVariableFont(font, {"wght": weight}, updateFontNames=False)
    names = {
        1: FAMILY,
        2: "Regular",
        4: FAMILY,
        6: "VAgentSubtitleSC-Regular",
        16: FAMILY,
        17: "Regular",
    }
    for record in list(font["name"].names):
        if record.nameID in names:
            font["name"].setName(
                names[record.nameID], record.nameID, record.platformID, record.platEncID, record.langID
            )
    options = Options()
    options.layout_features = ["*"]
    options.name_IDs = ["*"]
    options.name_legacy = True
    options.name_languages = ["*"]
    options.notdef_outline = True
    options.recommended_glyphs = True
    subsetter = Subsetter(options=options)
    subsetter.populate(text=charset())
    subsetter.subset(font)
    output.mkdir(parents=True, exist_ok=True)
    target = output / FONT_FILE
    font.save(target)
    subset = target.read_bytes()
    cmap = TTFont(target, lazy=True).getBestCmap()
    manifest = {
        "font": FONT_FILE,
        "family": FAMILY,
        "weight": weight,
        "purpose": "M6 E1 subtitle burn-in; shipped as a Mod Version under the SIL Open Font License 1.1",
        "license": "OFL-1.1",
        "licenseFile": "LICENSE-OFL.txt",
        "source": {
            "file": source.name,
            "sha256": sha256_bytes(raw),
            "sizeBytes": len(raw),
            "family": "Noto Sans SC",
            "version": TTFont(source, lazy=True)["name"].getDebugName(5),
            "variableAxes": axes,
            "upstream": "https://github.com/notofonts/noto-cjk",
        },
        "subset": {
            "sha256": sha256_bytes(subset),
            "sizeBytes": len(subset),
            "glyphCount": len(TTFont(target, lazy=True).getGlyphOrder()),
            "charset": "ASCII 0x20-0x7E + GB2312 level-1 hanzi + CJK subtitle punctuation",
            "characterCount": len(charset()),
            "covers": all(ord(char) in cmap for char in charset()),
        },
        "builder": "scripts/build_subtitle_font.py",
        "fontTools": _fonttools_version(),
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Local Noto Sans SC variable font")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--weight", type=int, default=400)
    args = parser.parse_args()
    manifest = build(args.source, args.out, weight=args.weight)
    print(json.dumps(manifest["subset"], ensure_ascii=False, indent=2))
    print(manifest["subset"]["sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
