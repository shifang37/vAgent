"""Local media integrity, safe file access and transactional delivery/retry services."""

import asyncio
import errno
import hashlib
import os
import stat
from contextlib import ExitStack, contextmanager
from datetime import datetime
from functools import lru_cache
from pathlib import PurePosixPath
from uuid import UUID

from pydantic import Field

from vagent.config import assert_id
from vagent.contracts import Contract, Identifier
from vagent.errors import AppError
from vagent.video.contracts import (
    SOURCE_POLICY_VERSION,
    JobErrorV2,
    JobResultV2,
    JobV2,
    MediaAsset,
    PreparedMedia,
    parse_job,
)
from vagent.video.mp4 import inspect_mp4

MESSAGES = {
    "DOWNLOAD_NETWORK": "媒体传输中断，将在剩余额度内重试原下载。",
    "DOWNLOAD_TIMEOUT": "媒体下载超时，将在剩余额度内重试原下载。",
    "DOWNLOAD_INCOMPLETE": "媒体长度与响应不符，将从头重试原下载。",
    "MEDIA_HTTP_ERROR": "媒体地址暂时无法访问；请检查网络或稍后重试原下载。",
    "MEDIA_RESULT_UNAVAILABLE": "原媒体结果无法取回；保留原任务，不会重新生成。",
    "MEDIA_URL_EXPIRED": "原媒体签名已过期，当前供应商不支持刷新结果地址。",
    "MEDIA_SOURCE_UNAPPROVED": "媒体来源尚未获准；需核实来源并更新服务端策略后重试。",
    "MEDIA_SOURCE_UNSAFE": "媒体地址或本地路径不符合访问规则，请检查来源或媒体目录。",
    "MEDIA_TOO_LARGE": "媒体超过本地大小上限，下载已停止。",
    "MEDIA_INVALID_TYPE": "媒体响应类型或压缩方式不受支持，下载已停止。",
    "MEDIA_INVALID_MP4": "媒体不是完整的自包含 H.264 MP4，或使用了尚未支持的结构。",
    "MEDIA_SPEC_MISMATCH": "实测媒体规格与原请求不符，未交付此文件。",
    "MEDIA_DISK_FULL": "本地磁盘空间不足，请释放空间后重试原下载。",
    "MEDIA_PERMISSION_DENIED": "无法访问本地媒体目录，请修复权限后重试原下载。",
    "MEDIA_IO_ERROR": "本地媒体读写失败，请检查磁盘后重试原下载。",
    "MEDIA_FILE_CONFLICT": "目标文件已存在且缺少提交凭据，请备份并移走冲突文件后重试。",
    "MEDIA_HASH_MISMATCH": "媒体与原完整性记录不符；请备份并移走冲突文件后重试，原记录已保留。",
}


class MediaError(AppError):
    def __init__(self, code, *, retryable=False, retry_after_at=None):
        super().__init__(code, MESSAGES[code])
        self.retryable, self.retry_after_at = retryable, retry_after_at

    def record(self):
        return JobErrorV2(stage="download", code=self.code, message=str(self))


def disk_error(error: OSError) -> MediaError:
    code = (
        "MEDIA_DISK_FULL"
        if error.errno in {errno.ENOSPC, errno.EDQUOT}
        else "MEDIA_PERMISSION_DENIED"
        if error.errno in {errno.EACCES, errno.EPERM}
        else "MEDIA_SOURCE_UNSAFE"
        if error.errno in {errno.ELOOP, errno.ENOTDIR}
        else "MEDIA_IO_ERROR"
    )
    return MediaError(code)


async def file_io(function, *args, cancel_cleanup=None):
    """Cancellation waits for the outstanding file operation before closing its handle."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        try:
            result = await task
            if cancel_cleanup:
                cancel_cleanup(result)
        except Exception:
            pass
        raise cancelled


class FileLease:
    def __init__(self, stack, file):
        self.stack, self.file = stack, file

    def close(self):
        self.stack.close()

    def __enter__(self):
        return self.file

    def __exit__(self, *_):
        self.close()


def _safe_stat(info):
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise MediaError("MEDIA_SOURCE_UNSAFE")


@lru_cache(maxsize=1)
def _windows_functions():
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes, close.restype = [wintypes.HANDLE], wintypes.BOOL
    return create, close


@contextmanager
def _windows_directory(path):
    # Holding directories without FILE_SHARE_DELETE prevents rename/junction swaps
    # throughout file access. OPEN_REPARSE_POINT prevents following a late junction.
    import ctypes

    create, close = _windows_functions()
    handle = create(str(path), 0x80, 3, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        _safe_stat(path.lstat())
        yield
    finally:
        close(handle)


class MediaFiles:
    def __init__(self, home):
        self.home = home.resolve()

    @contextmanager
    def parent(self, relative, *, create=False):
        parts = PurePosixPath(relative).parts
        if len(parts) != 3 or parts[0] != "media" or "\\" in relative:
            raise MediaError("MEDIA_SOURCE_UNSAFE")
        try:
            assert_id(parts[1])
            name = parts[2].removesuffix(".part")
            if not name.endswith(".mp4") or str(UUID(name[:-4])) != name[:-4]:
                raise ValueError("Invalid media ID")
        except (ValueError, AppError):
            raise MediaError("MEDIA_SOURCE_UNSAFE") from None
        with ExitStack() as stack:
            path = self.home
            descriptor = None
            if os.name == "nt":
                stack.enter_context(_windows_directory(path))
            else:
                descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                stack.callback(os.close, descriptor)
            for component in parts[:2]:
                path = path / component
                if create:
                    try:
                        if descriptor is None:
                            path.mkdir(mode=0o700)
                        else:
                            os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                if descriptor is None:
                    _safe_stat(path.lstat())
                    stack.enter_context(_windows_directory(path))
                else:
                    descriptor = os.open(
                        component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                    )
                    stack.callback(os.close, descriptor)
            # Windows ancestors are already held without delete sharing and have
            # been checked for reparse points. Re-resolving them adds no protection.
            if os.name != "nt" and path.resolve() != self.home / parts[0] / parts[1]:
                raise MediaError("MEDIA_SOURCE_UNSAFE")
            yield path, descriptor, parts[2]

    def open(self, relative, *, writing=False):
        stack = ExitStack()
        try:
            path, descriptor, name = stack.enter_context(self.parent(relative, create=writing))
            target = path / name if descriptor is None else name
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL if writing else os.O_RDONLY
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            if descriptor is None and not writing:
                _safe_stat(target.lstat())
            fd = os.open(target, flags, mode=0o600, dir_fd=descriptor)
            file = stack.enter_context(os.fdopen(fd, "w+b" if writing else "rb"))
            info = os.fstat(file.fileno())
            _safe_stat(info)
            if not stat.S_ISREG(info.st_mode):
                raise MediaError("MEDIA_SOURCE_UNSAFE")
            if descriptor is None:
                # A file itself may have been swapped between lstat and open.
                _safe_stat(target.lstat())
                if not os.path.samestat(info, target.stat()):
                    raise MediaError("MEDIA_SOURCE_UNSAFE")
            return FileLease(stack, file)
        except BaseException:
            stack.close()
            raise

    def remove_part(self, relative):
        if not relative.endswith(".mp4.part"):
            raise MediaError("MEDIA_SOURCE_UNSAFE")
        try:
            with self.parent(relative) as (path, descriptor, name):
                target = path / name if descriptor is None else name
                info = os.stat(target, dir_fd=descriptor, follow_symlinks=False)
                _safe_stat(info)
                if not stat.S_ISREG(info.st_mode):
                    raise MediaError("MEDIA_SOURCE_UNSAFE")
                os.unlink(target, dir_fd=descriptor)
        except FileNotFoundError:
            pass

    def promote(self, relative):
        with self.parent(relative) as (path, descriptor, name):
            if descriptor is None:
                # Windows rename is atomic and refuses to overwrite an existing file.
                os.rename(path / (name + ".part"), path / name)
            else:
                # Atomic no-clobber publication, with a recoverable extra part link if
                # killed before unlink. Unlike POSIX rename, this cannot overwrite evidence.
                os.link(
                    name + ".part", name, src_dir_fd=descriptor, dst_dir_fd=descriptor, follow_symlinks=False
                )
                os.unlink(name + ".part", dir_fd=descriptor)
                os.fsync(descriptor)

    @staticmethod
    def digest(file):
        before = os.fstat(file.fileno())
        file.seek(0)
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            chunk = file.read(min(1024 * 1024, remaining))
            if not chunk:
                raise MediaError("MEDIA_HASH_MISMATCH")
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(file.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise MediaError("MEDIA_HASH_MISMATCH")
        file.seek(0)
        return before.st_size, digest.hexdigest()

    def verified(self, relative, expected):
        lease = self.open(relative)
        try:
            if os.fstat(lease.file.fileno()).st_size != expected.size_bytes:
                raise MediaError("MEDIA_HASH_MISMATCH")
            if self.digest(lease.file) != (expected.size_bytes, expected.sha256):
                raise MediaError("MEDIA_HASH_MISMATCH")
            return lease
        except BaseException:
            lease.close()
            raise

    def prepare(self, file, job, timestamp):
        file.flush()
        os.fsync(file.fileno())
        size = os.fstat(file.fileno()).st_size
        if size > job.download_policy.max_bytes:
            raise MediaError("MEDIA_TOO_LARGE")
        try:
            metadata = inspect_mp4(file, size)
        except (ValueError, OverflowError):
            raise MediaError("MEDIA_INVALID_MP4") from None
        if (metadata.width, metadata.height) != (1280, 720) or abs(
            metadata.duration_seconds - job.request.spec.duration_seconds
        ) > 0.1000001:
            raise MediaError("MEDIA_SPEC_MISMATCH")
        actual_size, digest = self.digest(file)
        prepared = PreparedMedia(
            size_bytes=actual_size, sha256=digest, metadata=metadata, validated_at=timestamp
        )
        if job.download.repair and (actual_size, digest, metadata) != (
            job.download.prepared.size_bytes,
            job.download.prepared.sha256,
            job.download.prepared.metadata,
        ):
            raise MediaError("MEDIA_HASH_MISMATCH")
        return prepared


class DownloadRetry(Contract):
    client_request_id: Identifier
    expected_revision: int = Field(ge=0)


def changed_download(job, timestamp, **changes):
    from vagent.video.jobs import changed_job

    download = {**job.download.model_dump(mode="json", by_alias=True), **changes.pop("download", {})}
    return changed_job(job, timestamp, download=download, **changes)


def failed_download(job, error, timestamp, *, failure_at=None):
    from vagent.video.jobs import after_seconds

    record = job.download
    retry = error.retryable and record.window_attempts < job.download_policy.max_attempts_per_window
    next_at = None
    if retry:
        delay = job.download_policy.retry_delays_seconds[max(0, record.window_attempts - 1)]
        next_at = after_seconds(failure_at or timestamp, delay)
        if error.retry_after_at:
            next_at = max(
                datetime.fromisoformat(next_at), datetime.fromisoformat(error.retry_after_at)
            ).isoformat()
    status = job.status if record.repair else "downloading" if retry else "download_failed"
    saved_error = error.record().model_dump(mode="json", by_alias=True)
    return changed_download(
        job,
        timestamp,
        status=status,
        error=None if record.repair or retry else saved_error,
        download={
            "phase": "pending" if retry else "failed",
            "nextAttemptAt": next_at,
            "startedAt": None,
            "deadlineAt": None,
            "error": saved_error,
        },
    )


class MediaService:
    def __init__(self, jobs):
        self.jobs, self.store = jobs, jobs.store
        self.files = MediaFiles(self.store.home)

    def asset(self, media_id, *, project_id=None):
        state = self.store.snapshot()
        raw = state["media"].get(media_id)
        if raw is None or (project_id is not None and raw["projectId"] != project_id):
            raise AppError("MEDIA_NOT_FOUND", "当前项目中没有这个媒体。")
        asset = MediaAsset.model_validate(raw)
        job = parse_job(state["jobs"][asset.job_id])
        if (
            job.context.project_id != asset.project_id
            or job.result.media_refs[0].media_id != asset.id
            or job.request.source_refs != asset.source_refs
        ):
            raise AppError("MEDIA_CONFLICT", "媒体来源与原任务不一致。")
        return asset

    def _availability(self, asset):
        try:
            with self.files.verified(asset.relative_path, asset):
                return "available", None
        except FileNotFoundError:
            return "unavailable", "missing"
        except MediaError as error:
            return "unavailable", error.code.removeprefix("MEDIA_").lower()
        except OSError as error:
            return "unavailable", disk_error(error).code.removeprefix("MEDIA_").lower()

    def refresh(self, job):
        if not isinstance(job, JobV2) or job.status != "succeeded":
            return job
        asset = self.asset(job.result.media_refs[0].media_id, project_id=job.context.project_id)
        for _ in range(3):
            status, reason = self._availability(asset)
            latest = self.jobs.get(job.id)
            if latest.revision != job.revision:
                job = latest
                continue
            if (status, reason) == (job.media_availability.status, job.media_availability.reason):
                return job
            try:
                return self.jobs.update(
                    job,
                    mediaAvailability={
                        "status": status,
                        "reason": reason,
                        "checkedAt": self.jobs.timestamp(),
                    },
                )
            except AppError as error:
                if error.code != "JOB_REVISION_CONFLICT":
                    raise
                job = self.jobs.get(job.id)
        return job

    def refresh_all(self):
        for job in self.jobs.list():
            self.refresh(job)

    def view(self, media_id, *, project_id=None):
        asset = self.asset(media_id, project_id=project_id)
        job = self.refresh(self.jobs.get(asset.job_id))
        return {
            "mediaId": asset.id,
            **asset.model_dump(mode="json", by_alias=True, exclude={"id", "relative_path"}),
            "availability": job.media_availability.model_dump(mode="json", by_alias=True),
            "mediaAvailable": job.media_availability.status == "available",
            "contentUrl": f"/api/media/{asset.id}/content"
            if job.media_availability.status == "available"
            else None,
        }

    def open_content(self, media_id):
        asset = self.asset(media_id)
        try:
            lease = self.files.verified(asset.relative_path, asset)
        except (OSError, MediaError):
            self.refresh(self.jobs.get(asset.job_id))
            raise AppError(
                "JOB_MEDIA_UNAVAILABLE", "本地媒体文件缺失或损坏，请查看原任务并重试下载。"
            ) from None
        job = self.jobs.get(asset.job_id)
        try:
            if job.media_availability.status != "available":
                self.jobs.update(
                    job,
                    mediaAvailability={
                        "status": "available",
                        "reason": None,
                        "checkedAt": self.jobs.timestamp(),
                    },
                )
            return asset, lease
        except BaseException:
            lease.close()
            raise

    def retry_download(self, job_id, request: DownloadRetry, *, project_id):
        from vagent.video.views import job_view

        job = self.jobs.get(job_id, project_id=project_id)
        key = f"download-retry:{job_id}:{request.client_request_id}"
        args = request.model_dump(mode="json", by_alias=True)
        replay = self.store.operation_result(key, "retry_download", args)
        if replay is not None:
            if not replay["ok"]:
                raise AppError(replay["error"]["code"], replay["error"]["message"])
            return replay["data"]
        self.refresh(job)

        def retry(draft):
            raw = draft["jobs"].get(job_id)
            if raw is None or raw["context"]["projectId"] != project_id:
                raise AppError("JOB_NOT_FOUND", "当前项目中没有这个视频任务。")
            job = parse_job(raw)
            if job.revision != request.expected_revision:
                raise AppError("JOB_REVISION_CONFLICT", "Job 版本已变化，请重新读取。")
            if not isinstance(job, JobV2) or job.download is None:
                raise AppError("JOB_DOWNLOAD_RETRY_UNAVAILABLE", "这个任务没有可恢复的媒体下载。")
            if job.download.phase in {"pending", "writing", "prepared"}:
                return job_view(job)
            if job.status != "download_failed" and not (
                job.status == "succeeded" and job.media_availability.status == "unavailable"
            ):
                raise AppError("JOB_DOWNLOAD_RETRY_UNAVAILABLE", "仅下载失败或本地媒体不可用时可恢复下载。")
            updated = changed_download(
                job,
                self.jobs.timestamp(),
                status="succeeded" if job.status == "succeeded" else "downloading",
                error=None,
                download={
                    "phase": "pending",
                    "generation": job.download.generation + 1,
                    "windowAttempts": 0,
                    "nextAttemptAt": self.jobs.timestamp(),
                    "startedAt": None,
                    "deadlineAt": None,
                    "error": None,
                    "repair": job.status == "succeeded",
                    "sourcePolicyVersion": SOURCE_POLICY_VERSION,
                },
            )
            draft["jobs"][job_id] = updated.model_dump(mode="json", by_alias=True)
            return job_view(updated)

        result = self.store.operation(key, "retry_download", args, retry)
        if not result["ok"]:
            raise AppError(result["error"]["code"], result["error"]["message"])
        self.jobs.publish(self.jobs.get(job_id, project_id=project_id))
        return result["data"]

    def commit(self, job):
        from vagent.video.jobs import changed_job

        prepared, download = job.download.prepared, job.download
        timestamp = self.jobs.timestamp()
        asset = MediaAsset(
            id=download.media_id,
            project_id=job.context.project_id,
            job_id=job.id,
            source_refs=job.request.source_refs,
            relative_path=download.relative_path,
            size_bytes=prepared.size_bytes,
            sha256=prepared.sha256,
            metadata=prepared.metadata,
            created_at=timestamp,
        )
        result = job.result or JobResultV2(
            request_fingerprint=job.request_fingerprint,
            spec=job.request.spec,
            source_refs=job.request.source_refs,
            media_refs=[{"mediaId": asset.id}],
            summary="单镜头视频已完整保存到本地，可播放和下载。",
        )
        updated = changed_job(
            job,
            timestamp,
            status="succeeded",
            result=result.model_dump(mode="json", by_alias=True),
            error=None,
            download={
                **download.model_dump(mode="json", by_alias=True),
                "phase": "committed",
                "nextAttemptAt": None,
                "startedAt": None,
                "deadlineAt": None,
                "error": None,
            },
            mediaAvailability={"status": "available", "reason": None, "checkedAt": timestamp},
        )

        def save(draft):
            if draft["jobs"][job.id]["revision"] != job.revision:
                raise AppError("JOB_REVISION_CONFLICT", "Job 版本已变化，请重新读取。")
            draft["media"].setdefault(asset.id, asset.model_dump(mode="json", by_alias=True))
            draft["jobs"][job.id] = updated.model_dump(mode="json", by_alias=True)

        self.store.transaction(save)
        self.jobs.publish(updated)
        return updated
