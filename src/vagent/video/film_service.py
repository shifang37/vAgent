"""Film assembly orchestration: plan under the lock, run FFmpeg outside it, commit once.

The Store holds a single-writer lock, so the long local operation must not run while
it is held: this service snapshots and freezes the inputs in a short read, verifies
and concatenates outside the lock, then re-acquires it to re-check the storyboard
revision, the input hashes and the produced file before one atomic commit.
"""

import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from vagent.errors import AppError
from vagent.storage import now
from vagent.video.assembler import (
    Engine,
    assemble,
    bundled_engine,
    plan_assembly,
)
from vagent.video.copy_signature import read_copy_signature
from vagent.video.films import (
    Film,
    FilmAssembly,
    FilmEngine,
    FilmInput,
    FilmMedia,
    FilmSegment,
    FilmVersion,
    Rational,
    assembly_fingerprint,
    film_media,
    film_media_path,
    film_view,
    find_film,
    parse_media,
    select_inputs,
    verify_film_media,
)
from vagent.video.media import MediaError, MediaFiles
from vagent.video.storyboards import storyboard_in_state

TOOL_NAME = "film_assemble"


class FilmService:
    def __init__(self, jobs):
        self.jobs = jobs
        self.store = jobs.store
        self.media = jobs.media
        self.files: MediaFiles = jobs.media.files

    # -- paths -----------------------------------------------------------------
    def _work_root(self) -> Path:
        root = self.store.home / "assemble"
        root.mkdir(mode=0o700, exist_ok=True)
        return root

    def _absolute(self, relative: str) -> Path:
        return self.store.home / relative

    def _part_path(self, relative: str) -> Path:
        with self.files.parent(relative, create=True) as (path, _descriptor, name):
            return path / name

    # -- read side -------------------------------------------------------------
    def view(self, project_id: str, storyboard_id: str) -> dict:
        state = self.store.snapshot()
        storyboard_in_state(state, project_id, storyboard_id)
        film = find_film(state, project_id, storyboard_id)
        if film is None:
            return {"storyboardId": storyboard_id, "film": None}
        view = film_view(state, film)
        for entry in view["versions"]:
            media = film_media(state, entry["outputMediaId"])
            if media is None:
                entry["mediaAvailable"], entry["mediaUrl"] = False, None
                continue
            verify_film_media(state, media)
            status, reason = self.media._availability(media)
            entry["mediaAvailable"] = status == "available"
            entry["mediaUrl"] = f"/api/media/{media.id}/content" if status == "available" else None
            entry["mediaAvailability"] = {"status": status, "reason": reason}
        view["latest"] = view["versions"][-1]
        return {"storyboardId": storyboard_id, "film": view}

    # -- write side ------------------------------------------------------------
    async def assemble(
        self,
        *,
        project_id: str,
        storyboard_id: str,
        expected_revision: int,
        operation_key: str,
        args: dict,
        cancelled=None,
    ) -> dict:
        previous = self.store.operation_result(operation_key, TOOL_NAME, args)
        if previous is not None:
            return previous
        engine = bundled_engine()
        state = self.store.snapshot()
        selection = select_inputs(state, project_id, storyboard_id, expected_revision)
        prepared = self._verify_inputs(state, selection["inputs"])
        plan = plan_assembly([item["signature"] for item in prepared])
        fingerprint = self._fingerprint(project_id, selection, plan.compatibility_sha256, engine)
        replay = self._replay_fingerprint(state, project_id, storyboard_id, fingerprint)
        if replay is not None:
            return self.store.operation(operation_key, TOOL_NAME, args, lambda _: replay)
        film = find_film(state, project_id, storyboard_id)
        media_id = str(uuid4())
        final_relative = film_media_path(project_id, media_id)
        part_relative = final_relative + ".part"
        output = self._part_path(part_relative)
        try:
            produced = await assemble(
                engine=engine,
                inputs=[item["path"] for item in prepared],
                work_root=self._work_root(),
                output=output,
                cancelled=cancelled,
            )
            result = self.store.operation(
                operation_key,
                TOOL_NAME,
                args,
                lambda draft: self._commit(
                    draft,
                    project_id=project_id,
                    storyboard_id=storyboard_id,
                    expected_revision=expected_revision,
                    selection=selection,
                    prepared=prepared,
                    plan=plan,
                    produced=produced,
                    engine=engine,
                    film=film,
                    media_id=media_id,
                    final_relative=final_relative,
                    part_relative=part_relative,
                    fingerprint=fingerprint,
                ),
            )
            return result
        except BaseException:
            output.unlink(missing_ok=True)
            raise

    def _verify_inputs(self, state: dict, inputs: tuple[FilmInput, ...]) -> list[dict]:
        """Re-check each indexed file, its hash and its copy signature before the run."""
        prepared = []
        for item in inputs:
            raw = state["media"].get(item.media_id)
            media = parse_media(raw) if raw is not None else None
            if media is None or isinstance(media, FilmMedia):
                raise AppError("FILM_MEDIA_INVALID", f"镜头 {item.shot_id} 的媒体记录不可用。")
            if (media.size_bytes, media.sha256) != (item.size_bytes, item.sha256):
                raise AppError("FILM_MEDIA_INVALID", f"镜头 {item.shot_id} 的媒体记录与分镜引用不一致。")
            try:
                lease = self.files.verified(media.relative_path, media)
            except FileNotFoundError:
                raise AppError("FILM_MEDIA_INVALID", f"镜头 {item.shot_id} 的本地文件缺失。") from None
            except (MediaError, OSError):
                raise AppError("FILM_MEDIA_INVALID", f"镜头 {item.shot_id} 的本地文件损坏。") from None
            try:
                signature = read_copy_signature(lease.file, media.size_bytes)
            except (ValueError, OSError):
                raise AppError(
                    "FILM_INPUT_INCOMPATIBLE", f"镜头 {item.shot_id} 的编码结构不支持流复制拼接。"
                ) from None
            finally:
                lease.close()
            prepared.append(
                {
                    "input": item,
                    "path": self._absolute(media.relative_path),
                    "signature": signature,
                }
            )
        return prepared

    def _fingerprint(
        self, project_id: str, selection: dict, compatibility_sha256: str, engine: Engine
    ) -> str:
        return assembly_fingerprint(
            project_id=project_id,
            storyboard_id=selection["storyboard"].id,
            storyboard_version=selection["storyboardVersion"],
            inputs=selection["inputs"],
            compatibility_sha256=compatibility_sha256,
            engine=FilmEngine(
                package_version=engine.package_version,
                ffmpeg_version=engine.ffmpeg_version,
                binary_sha256=engine.binary_sha256,
            ),
        )

    def _replay_fingerprint(self, state: dict, project_id: str, storyboard_id: str, fingerprint: str):
        film = find_film(state, project_id, storyboard_id)
        if film is None:
            return None
        entry = next(
            (item for item in reversed(film.versions) if item.assembly.fingerprint == fingerprint), None
        )
        if entry is None:
            return None
        media = film_media(state, entry.output_media_id)
        if media is None:
            raise AppError("FILM_MEDIA_INVALID", "历史成片缺少本地媒体记录，请重新拼接。")
        try:
            lease = self.files.verified(media.relative_path, media)
        except (MediaError, OSError):
            raise AppError("FILM_MEDIA_INVALID", "历史成片文件缺失或损坏，请重新拼接。") from None
        lease.close()
        return film_view(state, film)

    def _commit(
        self,
        draft: dict,
        *,
        project_id: str,
        storyboard_id: str,
        expected_revision: int,
        selection: dict,
        prepared: list[dict],
        plan,
        produced,
        engine: Engine,
        film: Film | None,
        media_id: str,
        final_relative: str,
        part_relative: str,
        fingerprint: str,
    ) -> dict:
        record = storyboard_in_state(draft, project_id, storyboard_id)
        if record.revision != expected_revision:
            raise AppError("VERSION_CONFLICT", "分镜在拼接期间发生变化，未提交成片，请重新拼接。")
        for item in prepared:
            reference: FilmInput = item["input"]
            raw = draft["media"].get(reference.media_id)
            if raw is None or (raw.get("sizeBytes"), raw.get("sha256")) != (
                reference.size_bytes,
                reference.sha256,
            ):
                raise AppError("FILM_MEDIA_INVALID", "输入媒体在拼接期间被修改，未提交成片。")
            expected = SimpleNamespace(size_bytes=reference.size_bytes, sha256=reference.sha256)
            try:
                lease = self.files.verified(raw["relativePath"], expected)
            except (MediaError, OSError):
                raise AppError("FILM_MEDIA_INVALID", "输入媒体文件在拼接期间发生变化。") from None
            lease.close()
        part = self._absolute(part_relative)
        try:
            size, digest = self._hash(part)
        except OSError:
            raise AppError("FILM_ASSEMBLY_FAILED", "拼接产物丢失，未提交成片。") from None
        if (size, digest) != (produced.size_bytes, produced.sha256):
            raise AppError("FILM_MEDIA_INVALID", "拼接产物与校验值不一致，未提交成片。")
        try:
            self.files.promote(final_relative)
        except FileExistsError:
            raise AppError("FILM_MEDIA_INVALID", "目标成片文件已存在，请先备份移走。") from None
        timestamp = now()
        film_id = film.id if film is not None else str(uuid4())
        version_number = film.current_version + 1 if film is not None else 1
        segments = tuple(
            FilmSegment(
                shot_id=entry["input"].shot_id,
                shot_version_id=entry["input"].shot_version_id,
                start=Rational.of(item["start"]),
                duration=Rational.of(item["duration"]),
                signature_sha256=entry["signature"].sha256(),
                priming=Rational.of(entry["signature"].priming()),
            )
            for entry, item in zip(prepared, plan.timeline, strict=True)
        )
        assembly = FilmAssembly(
            engine=FilmEngine(
                package_version=engine.package_version,
                ffmpeg_version=engine.ffmpeg_version,
                binary_sha256=engine.binary_sha256,
            ),
            compatibility_sha256=plan.compatibility_sha256,
            timeline=segments,
            fingerprint=fingerprint,
        )
        version = FilmVersion(
            version=version_number,
            storyboard_version=selection["storyboardVersion"],
            storyboard_revision=expected_revision,
            inputs=selection["inputs"],
            output_media_id=media_id,
            assembly=assembly,
            size_bytes=produced.size_bytes,
            sha256=produced.sha256,
            metadata=produced.metadata,
            created_at=timestamp,
        )
        if film is None:
            film = Film(
                id=film_id,
                project_id=project_id,
                storyboard_id=storyboard_id,
                current_version=1,
                versions=(version,),
                revision=1,
                created_at=timestamp,
            )
        else:
            film = film.model_copy(
                update={
                    "current_version": version_number,
                    "versions": (*film.versions, version),
                    "revision": film.revision + 1,
                }
            )
        media = FilmMedia(
            id=media_id,
            project_id=project_id,
            film_id=film_id,
            film_version=version_number,
            relative_path=final_relative,
            size_bytes=produced.size_bytes,
            sha256=produced.sha256,
            metadata=produced.metadata,
            created_at=timestamp,
        )
        draft["films"][film_id] = film.model_dump(mode="json", by_alias=True)
        draft["media"][media_id] = media.model_dump(mode="json", by_alias=True)
        return film_view(draft, film)

    @staticmethod
    def _hash(path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        return size, digest.hexdigest()
