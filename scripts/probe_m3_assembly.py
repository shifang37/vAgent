"""E0 experiment for the committed synthetic clip; not a production assembler.

Runs two local stream-copy variants and preserves the initial DTS warning.
The padded duration is specific to this fixture, not a general timeline rule.
Requires an explicitly supplied local FFmpeg executable; never downloads tools.
"""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from vagent.video.mp4 import inspect_mp4

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "m1c" / "sample-720p.mp4"
FIXTURE_SHA256 = "b179cca0ebb91e2b49a1e212df743037a8e292a3b754ee040c2124b31a3b6a54"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    executable = args.ffmpeg.resolve(strict=True)
    if hashlib.sha256(FIXTURE.read_bytes()).hexdigest() != FIXTURE_SHA256:
        parser.error("The committed fixture hash changed; review the probe first.")
    destination = args.output_dir.resolve()
    destination.mkdir(parents=True, exist_ok=False)

    def run(arguments):
        return subprocess.run(
            [str(executable), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )

    version = run(["-version"])
    license_result = run(["-L"])
    if version.returncode or license_result.returncode:
        raise RuntimeError("FFmpeg version/license inspection failed.")
    (destination / "version.txt").write_text(version.stdout, encoding="utf-8")
    (destination / "license.txt").write_text(license_result.stdout, encoding="utf-8")
    # The experiment accepts no user-supplied input paths or arbitrary concat text.
    fixture_path = FIXTURE.resolve().as_posix()
    if "'" in fixture_path or "\n" in fixture_path:
        raise ValueError("This development probe requires a simple repository path.")
    reports = []
    for name, duration in (("default", None), ("fixture-padded", "5.021333333")):
        listing = destination / f"{name}.txt"
        segment = f"file '{fixture_path}'\n"
        if duration:
            segment += f"duration {duration}\n"
        listing.write_text(segment * 2, encoding="utf-8")
        output = destination / f"{name}.mp4"
        result = run(
            [
                "-hide_banner",
                "-nostdin",
                "-n",
                "-protocol_whitelist",
                "file",
                "-f",
                "concat",
                "-safe",
                "0",
                "-auto_convert",
                "0",
                "-i",
                str(listing),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(output),
            ]
        )
        (destination / f"{name}.stderr.txt").write_text(result.stderr, encoding="utf-8")
        report = {
            "variant": name,
            "fixtureDurationOverride": duration,
            "returnCode": result.returncode,
            "nonMonotonicDtsWarning": "non-monotonic dts" in result.stderr.lower(),
        }
        if not result.returncode:
            with output.open("rb") as file:
                metadata = inspect_mp4(file, output.stat().st_size)
            report.update(
                sizeBytes=output.stat().st_size,
                sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
                metadata=metadata.model_dump(mode="json", by_alias=True),
            )
        reports.append(report)
    report = {
        "kind": "synthetic-e0-assembly-probe-only",
        "ffmpegVersion": version.stdout.splitlines()[0],
        "binarySha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "fixtureSha256": FIXTURE_SHA256,
        "inputCopies": 2,
        "submitCount": 0,
        "browserDecodeVerified": False,
        "variants": reports,
    }
    (destination / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if reports[1]["returnCode"] or reports[1]["nonMonotonicDtsWarning"]:
        raise SystemExit("The fixture-specific padded experiment did not pass.")


if __name__ == "__main__":
    main()
