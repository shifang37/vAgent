"""Atomic store writes survive the short Windows handle races that break ``os.replace``."""

import errno
import os
from pathlib import Path

import pytest

from vagent import storage
from vagent.errors import AppError


def patch_replace(monkeypatch, *, failures):
    real = os.replace
    attempts = []

    def flaky(source, destination):
        attempts.append(1)
        if len(attempts) <= failures:
            raise PermissionError(errno.EACCES, "locked by an indexer", str(destination))
        return real(source, destination)

    monkeypatch.setattr(storage.os, "replace", flaky)
    return attempts


def test_transient_replace_lock_is_retried(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    attempts = patch_replace(monkeypatch, failures=3)
    storage.atomic_write_bytes(target, b"new")
    assert target.read_bytes() == b"new"
    assert len(attempts) == 4
    assert not list(tmp_path.glob("*.tmp"))


def test_exhausted_retries_raise_storage_busy_and_keep_the_original(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    attempts = patch_replace(monkeypatch, failures=storage.WRITE_ATTEMPTS)
    monkeypatch.setattr(storage.time, "sleep", lambda _: None)
    with pytest.raises(AppError) as error:
        storage.atomic_write_bytes(target, b"new")
    assert error.value.code == "STORAGE_BUSY"
    assert target.read_bytes() == b"old"
    assert len(attempts) == storage.WRITE_ATTEMPTS
    assert not list(tmp_path.glob("*.tmp"))


def test_non_lock_errors_are_not_retried(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    attempts = []

    def full(source, destination):
        attempts.append(1)
        raise OSError(errno.ENOSPC, "no space left")

    monkeypatch.setattr(storage.os, "replace", full)
    with pytest.raises(OSError) as error:
        storage.atomic_write_bytes(target, b"new")
    assert error.value.errno == errno.ENOSPC
    assert len(attempts) == 1


def test_store_read_retries_a_transient_lock(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_bytes(b"content")
    real = Path.read_bytes
    attempts = []

    def flaky(self):
        attempts.append(1)
        if len(attempts) <= 2:
            raise PermissionError(errno.EACCES, "locked", str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", flaky)
    assert storage.read_bytes_with_retry(target) == b"content"
    assert len(attempts) == 3
