"""Refuse a release whose tag and package version disagree.

``.github/workflows/release.yml`` runs this before building, so a wheel can never
be published under a tag that names a different version.
"""

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_package_version() -> str:
    manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(manifest["project"]["version"])


def read_module_version() -> str:
    source = (ROOT / "src" / "vagent" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', source, re.MULTILINE)
    if match is None:
        raise SystemExit("src/vagent/__init__.py 缺少 __version__")
    return match.group(1)


def main(argv: list[str]) -> int:
    tag = argv[0].strip() if argv else ""
    if not tag:
        print("用法: check_release_version.py v0.7.1", file=sys.stderr)
        return 2

    version = read_package_version()
    expected = f"v{version}"
    if tag != expected:
        print(f"tag {tag!r} 与 pyproject 版本 {version!r} 不一致，应为 {expected!r}", file=sys.stderr)
        return 1

    module_version = read_module_version()
    if module_version != version:
        print(
            f"src/vagent/__init__.py 的 {module_version!r} 与 pyproject 的 {version!r} 不一致",
            file=sys.stderr,
        )
        return 1

    notes = ROOT / "docs" / f"RELEASE_{version}.md"
    if not notes.is_file():
        print(f"提示: 缺少 {notes.relative_to(ROOT)}，GitHub Release 将改用自动生成说明")
    print(f"tag 与版本一致: {tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
