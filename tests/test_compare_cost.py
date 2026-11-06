"""H6 script: the cost comparison reads a true snapshot and never touches the store."""

import sys

import pytest

from scripts import compare_cost
from vagent.errors import AppError
from vagent.storage import FileStore, RunRecordV1, load_snapshot


def running_run() -> dict:
    return RunRecordV1(
        id="run-1",
        session_id="coffee",
        request_id="request-1",
        prompt="offline fixture",
        model="deepseek-flash",
        status="running",
        messages=[],
        model_steps=0,
        tool_calls=0,
        input_tokens=0,
        output_tokens=0,
        answer="",
        created_at="2030-01-01T00:00:00+00:00",
        updated_at="2030-01-01T00:00:00+00:00",
    ).model_dump(by_alias=True)


def seed_running_store(tmp_path):
    home = tmp_path / "store"
    with FileStore.open(home) as store:
        store.ensure_session("coffee")

        def begin(draft):
            draft["runs"]["run-1"] = running_run()
            draft["sessions"]["coffee"]["latestRunId"] = "run-1"

        store.transaction(begin)
    return home


def test_load_snapshot_does_not_recover_or_write(tmp_path):
    home = seed_running_store(tmp_path)
    before = (home / "state.json").read_bytes()
    mtime = (home / "state.json").stat().st_mtime_ns

    state = load_snapshot(home)

    # A read-only snapshot must not turn a running Run into interrupted.
    assert state["runs"]["run-1"]["status"] == "running"
    assert (home / "state.json").read_bytes() == before
    assert (home / "state.json").stat().st_mtime_ns == mtime
    assert not (home / "instance.lock").exists()


def test_compare_cost_never_writes_the_store(tmp_path, monkeypatch, capsys):
    home = tmp_path / "store"
    with FileStore.open(home):
        pass
    before = (home / "state.json").read_bytes()
    mtime = (home / "state.json").stat().st_mtime_ns

    def explode(*_args, **_kwargs):
        raise AssertionError("compare_cost must not write the store")

    monkeypatch.setattr("vagent.storage.atomic_write_json", explode)
    monkeypatch.setattr("vagent.storage.atomic_write_bytes", explode)
    monkeypatch.setattr(sys, "argv", ["compare_cost.py", "--home", str(home)])

    assert compare_cost.main() == 0
    assert "runs=0" in capsys.readouterr().out
    assert (home / "state.json").read_bytes() == before
    assert (home / "state.json").stat().st_mtime_ns == mtime
    assert not (home / "instance.lock").exists()


def test_missing_home_fails_without_creating_it(tmp_path, monkeypatch, capsys):
    home = tmp_path / "ghost"
    monkeypatch.setattr(sys, "argv", ["compare_cost.py", "--home", str(home)])

    assert compare_cost.main() == 1
    assert "STORE_NOT_FOUND" in capsys.readouterr().out
    assert not home.exists()


def test_load_snapshot_rejects_a_corrupt_store_without_writing(tmp_path):
    home = tmp_path / "broken"
    home.mkdir()
    (home / "state.json").write_text('{"schemaVersion": 99}', encoding="utf-8")
    before = (home / "state.json").read_bytes()

    with pytest.raises(AppError) as error:
        load_snapshot(home)

    assert error.value.code == "INVALID_STORE"
    assert (home / "state.json").read_bytes() == before
    assert sorted(path.name for path in home.iterdir()) == ["state.json"]
