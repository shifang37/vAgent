"""M7 E3 CLI: `vagent timeline|audio|film render --timeline` stay local and keyless."""

import asyncio
import json
import os
import subprocess
import sys
import wave

from timeline_support import PROJECT, clips_for, renderable_episode

from vagent.storage import FileStore

CHILD_ENVIRONMENT = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "APPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "LANG",
    "LC_ALL",
)


def command(directory, home, *args):
    wanted = {name.upper() for name in CHILD_ENVIRONMENT}
    env = {key: value for key, value in os.environ.items() if key.upper() in wanted}
    env.update(
        VAGENT_HOME=str(home),
        VAGENT_VIDEO_MODE="off",
        VAGENT_DASHSCOPE_KEY="",
        VAGENT_DASHSCOPE_WORKSPACE_ID="",
        VAGENT_DEEPSEEK_KEY="",
        DEEPSEEK_API_KEY="",
        VAGENT_MCP_CONFIG=str(directory / "missing-mcp.json"),
        PYTHONIOENCODING="utf-8",
    )
    return subprocess.run(
        [sys.executable, "-m", "vagent", *args],
        cwd=directory,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def wav_file(path, seconds=1.0):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(b"\x00\x00" * int(48000 * seconds))


def seed(directory, home):
    with FileStore.open(home) as store:
        store.ensure_session(PROJECT)
        fixture = asyncio.run(renderable_episode(store))
        clips = clips_for(store.snapshot(), fixture["boards"])
        (directory / "clips.json").write_text(json.dumps(clips, ensure_ascii=False), encoding="utf-8")
        return {
            "episodeId": fixture["episode"]["episodeId"],
            "trackId": fixture["trackId"],
            "clips": clips,
            "jobs": len(store.snapshot()["jobs"]),
        }


def test_cli_timeline_sources_save_show_and_render(tmp_path):
    home = tmp_path / "state"
    fixture = seed(tmp_path, home)
    sources = command(tmp_path, home, "timeline", "sources", "-s", PROJECT, "--episode", fixture["episodeId"])
    assert sources.returncode == 0, sources.stderr
    editor = json.loads(sources.stdout)
    assert editor["timeline"] is None and len(editor["shots"]) == len(fixture["clips"])
    assert editor["subtitleTracks"][0]["trackId"] == fixture["trackId"]

    created = command(
        tmp_path,
        home,
        "timeline",
        "save",
        "-s",
        PROJECT,
        "--episode",
        fixture["episodeId"],
        "--clips",
        str(tmp_path / "clips.json"),
        "--subtitle-track",
        fixture["trackId"],
        "--subtitle-version",
        "1",
        "--request-id",
        "cli-timeline-save",
    )
    assert created.returncode == 0, created.stderr
    saved = json.loads(created.stdout)
    assert saved["currentVersion"] == 1 and saved["replayed"] is False

    shown = command(tmp_path, home, "timeline", "show", "-s", PROJECT, "--episode", fixture["episodeId"])
    assert shown.returncode == 0, shown.stderr
    view = json.loads(shown.stdout)
    assert view["timelineId"] == saved["timelineId"]
    assert view["latest"]["clipCount"] == len(fixture["clips"])

    # A stale expected revision is refused with the frozen code; the content must
    # differ from the saved version, otherwise the identical content replays.
    changed_clips = [dict(item) for item in fixture["clips"]]
    changed_clips[0]["inMs"] = 1000
    (tmp_path / "clips2.json").write_text(json.dumps(changed_clips, ensure_ascii=False), encoding="utf-8")
    stale = command(
        tmp_path,
        home,
        "timeline",
        "save",
        "-s",
        PROJECT,
        "--episode",
        fixture["episodeId"],
        "--timeline",
        saved["timelineId"],
        "--clips",
        str(tmp_path / "clips2.json"),
        "--subtitle-track",
        fixture["trackId"],
        "--subtitle-version",
        "1",
        "--expected-revision",
        "9",
        "--request-id",
        "cli-timeline-stale",
    )
    assert stale.returncode != 0
    assert "TIMELINE_VERSION_CONFLICT" in stale.stderr or "TIMELINE_VERSION_CONFLICT" in stale.stdout

    audio_path = tmp_path / "rain.wav"
    wav_file(audio_path)
    imported = command(
        tmp_path,
        home,
        "audio",
        "import",
        str(audio_path),
        "-s",
        PROJECT,
        "--kind",
        "bgm",
        "--title",
        "雨声",
        "--source-url",
        "https://example.test/rain.wav",
        "--license",
        "CC0",
        "--request-id",
        "cli-audio-import",
    )
    assert imported.returncode == 0, imported.stderr
    audio = json.loads(imported.stdout)
    assert audio["kind"] == "bgm" and audio["replayed"] is False
    listing = command(tmp_path, home, "audio", "list", "-s", PROJECT)
    assert listing.returncode == 0, listing.stderr
    assert [item["audioId"] for item in json.loads(listing.stdout)["audio"]] == [audio["audioId"]]

    rendered = command(
        tmp_path,
        home,
        "film",
        "render",
        "-s",
        PROJECT,
        "--timeline",
        saved["timelineId"],
        "--confirm",
        "--request-id",
        "cli-timeline-render",
    )
    assert rendered.returncode == 0, rendered.stderr
    film = json.loads(rendered.stdout)
    assert film["scope"] == "timeline" and film["timelineId"] == saved["timelineId"]
    assert film["film"]["latest"]["assemblyVersion"] == "ffmpeg-render-timeline-v1"

    with FileStore.open(home) as store:
        state = store.snapshot()
        assert len(state["jobs"]) == fixture["jobs"]
        assert len(state["timelines"]) == 1
        assert len(state["audio"]) >= 1


def test_cli_timeline_save_requires_clips_and_an_episode(tmp_path):
    home = tmp_path / "state"
    fixture = seed(tmp_path, home)
    missing = command(tmp_path, home, "timeline", "save", "-s", PROJECT, "--episode", fixture["episodeId"])
    assert missing.returncode != 0
    unknown = command(tmp_path, home, "timeline", "show", "-s", PROJECT, "--episode", "not-a-uuid")
    assert unknown.returncode != 0
