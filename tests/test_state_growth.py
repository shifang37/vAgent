"""FX-20: bounded state.json growth — event cap, no-write recovery, retention and gc."""

import json
import time

import pytest

from vagent.errors import AppError
from vagent.storage import (
    GC_MARKER_FILE,
    LAST_GOOD_FILE,
    MAX_EVENTS_PER_RUN,
    RETENTION_DAYS,
    FileStore,
    RunRecord,
    append_event,
    collect_garbage,
    dropped_events,
    last_gc_at,
    record_gc_marker,
    restore_last_good,
)


def make_run(run_id="run_1", *, status="completed", updated_at="2026-01-01T00:00:00+00:00", events=None):
    record = RunRecord(
        id=run_id,
        session_id="project",
        request_id=run_id,
        prompt="p",
        model="deepseek-flash",
        status=status,
        messages=[],
        model_steps=1,
        tool_calls=0,
        input_tokens=10,
        output_tokens=5,
        answer="done",
        created_at=updated_at,
        updated_at=updated_at,
        events=events or [],
    )
    return record.model_dump(by_alias=True)


def add_run(store, run):
    def mutate(draft):
        draft["runs"][run["id"]] = run
        draft["sessions"].setdefault(run["sessionId"], {"id": run["sessionId"], "messages": []})

    store.transaction(mutate)


def test_append_event_caps_the_journal_and_keeps_sequence_monotonic():
    run = make_run()
    for index in range(MAX_EVENTS_PER_RUN + 25):
        append_event(run, {"type": "tool.completed", "index": index})
    assert len(run["events"]) == MAX_EVENTS_PER_RUN
    # No new stored field: the first retained sequence still proves 25 were dropped.
    assert dropped_events(run) == 25
    assert "droppedEvents" not in run
    assert run["events"][0]["sequence"] == 26
    assert run["events"][-1]["sequence"] == MAX_EVENTS_PER_RUN + 25
    assert [event["sequence"] for event in run["events"]] == list(range(26, MAX_EVENTS_PER_RUN + 26))


def test_open_does_not_rewrite_a_healthy_state(tmp_path):
    home = tmp_path / "state"
    with FileStore.open(home):
        pass
    path = home / "state.json"
    before = path.stat().st_mtime_ns
    time.sleep(0.02)
    with FileStore.open(home):
        pass
    assert path.stat().st_mtime_ns == before


def test_open_still_writes_a_brand_new_store(tmp_path):
    home = tmp_path / "state"
    with FileStore.open(home):
        pass
    assert json.loads((home / "state.json").read_text(encoding="utf-8"))["schemaVersion"] == 9


def test_stale_lock_from_a_dead_pid_is_reclaimed(tmp_path):
    home = tmp_path / "state"
    home.mkdir()
    dead = 999_999
    (home / "instance.lock").write_text(
        json.dumps({"pid": dead, "createdAt": "2026-01-01T00:00:00+00:00"}), encoding="utf-8"
    )
    with FileStore.open(home) as store:
        assert store.snapshot()["schemaVersion"] == 9
    assert not (home / "instance.lock").exists()


def test_live_lock_is_not_stolen(tmp_path):
    home = tmp_path / "state"
    with FileStore.open(home):
        with pytest.raises(AppError) as error:
            FileStore.open(home)
        assert error.value.code == "STORE_LOCKED"


def _aged_state():
    old = "2026-01-01T00:00:00+00:00"
    recent = "2026-03-20T00:00:00+00:00"
    return {
        "runs": {
            "run_old": make_run("run_old", updated_at=old, events=[{"type": "x"}, {"type": "y"}]),
            "run_recent": make_run("run_recent", updated_at=recent, events=[{"type": "x"}]),
            "run_waiting": make_run("run_waiting", status="waiting_external", updated_at=old),
            "run_resumable": make_run("run_resumable", status="interrupted", updated_at=old),
        },
        "operations": {
            "run_old:1:call": {"fingerprint": "f", "result": {"ok": True, "data": {}}},
            "run_recent:1:call": {"fingerprint": "f", "result": {"ok": True, "data": {}}},
        },
        "waits": {
            "wait_old": {"context": {"runId": "run_old"}},
            "wait_recent": {"context": {"runId": "run_recent"}},
        },
    }


def test_gc_dry_run_reports_without_mutating():
    state = _aged_state()
    before = json.dumps(state, sort_keys=True)
    report = collect_garbage(state, now="2026-04-01T00:00:00+00:00", days=RETENTION_DAYS, apply=False)
    assert json.dumps(state, sort_keys=True) == before
    assert report["runs"] == 1
    assert report["events"] == 2
    assert report["operations"] == 1
    assert report["waits"] == 1


def test_gc_apply_keeps_recent_and_active_runs():
    state = _aged_state()
    report = collect_garbage(state, now="2026-04-01T00:00:00+00:00", days=RETENTION_DAYS, apply=True)
    assert state["runs"]["run_old"]["events"] == []
    assert state["runs"]["run_old"]["answer"] == "done"  # the verdict and counters stay
    assert state["runs"]["run_recent"]["events"] == [{"type": "x"}]
    assert state["runs"]["run_waiting"]["events"] == []
    assert "run_old:1:call" not in state["operations"]
    assert "run_recent:1:call" in state["operations"]
    assert "wait_old" not in state["waits"]
    assert "wait_recent" in state["waits"]
    assert "lastGcAt" not in state  # the v9 envelope keeps its exact shape
    assert report["at"] == "2026-04-01T00:00:00+00:00"
    assert report["freedBytes"] >= 0


def test_gc_marker_is_a_sidecar(tmp_path):
    home = tmp_path / "state"
    with FileStore.open(home) as store:
        add_run(store, make_run("run_old", updated_at="2026-01-01T00:00:00+00:00"))
        report = store.transaction(
            lambda draft: collect_garbage(draft, now="2026-04-01T00:00:00+00:00", days=30)
        )
        record_gc_marker(store.home, report)
    assert last_gc_at(home) == "2026-04-01T00:00:00+00:00"
    assert (home / GC_MARKER_FILE).exists()
    assert "lastGcAt" not in json.loads((home / "state.json").read_text(encoding="utf-8"))


def test_gc_rejects_a_non_positive_retention_window():
    with pytest.raises(AppError) as error:
        collect_garbage(_aged_state(), days=0)
    assert error.value.code == "INVALID_ARGUMENTS"


def test_gc_runs_through_the_store_transaction(store):
    add_run(store, make_run("run_old", updated_at="2026-01-01T00:00:00+00:00"))
    report = store.transaction(lambda draft: collect_garbage(draft, now="2026-04-01T00:00:00+00:00", days=30))
    assert report["runs"] == 1
    saved = store.snapshot()
    assert "lastGcAt" not in saved
    assert saved["runs"]["run_old"]["events"] == []


def test_last_good_snapshot_is_written_every_n_commits(store):
    for index in range(20):
        add_run(store, make_run(f"run_{index:02d}"))
    snapshot = store.home / LAST_GOOD_FILE
    assert snapshot.exists()
    saved = json.loads(snapshot.read_text(encoding="utf-8"))
    # The snapshot lags by at most one snapshot window, never by the whole history.
    assert set(saved["runs"]) <= set(store.snapshot()["runs"])
    assert len(saved["runs"]) >= 19


def test_explicit_last_good_snapshot_and_restore(tmp_path):
    home = tmp_path / "state"
    with FileStore.open(home) as store:
        add_run(store, make_run("run_keep"))
        store.snapshot_last_good()
    (home / "state.json").write_text("{broken", encoding="utf-8")
    result = restore_last_good(home)
    assert result["schemaVersion"] == 9
    restored = json.loads((home / "state.json").read_text(encoding="utf-8"))
    assert "run_keep" in restored["runs"]
    assert list(home.glob("state-invalid-*.json"))


def test_restore_without_a_snapshot_is_a_clear_error(tmp_path):
    home = tmp_path / "state"
    home.mkdir()
    with pytest.raises(AppError) as error:
        restore_last_good(home)
    assert error.value.code == "NOT_FOUND"
