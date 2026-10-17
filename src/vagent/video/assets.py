"""Reference-image integrity and the local asset library.

D2 needed only the read path: prove that the frozen asset record still matches its
file, decode the published image bounds, and hand the adapter verified bytes for
the data URL. D3 adds the library itself — trusted local import with magic-number
and bounds validation, SHA-256 dedup per project, and a bounded read-only listing
for tools and local clients. Path handling reuses the media module's no-follow
primitives so a symlinked or reparse-point asset directory is rejected exactly
like the media directory.
"""

import hashlib
import os
import stat
from contextlib import ExitStack, contextmanager
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from vagent.errors import AppError
from vagent.video.contracts import IMAGE_MAX_BYTES, ImageInputRule, ImageSize, ShotMediaInput
from vagent.video.media import FileLease, MediaError, _safe_stat, _windows_directory
from vagent.video.storyboards import ASSET_EXTENSIONS, Asset, ImageDimensions, utc_now

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_ALPHA_COLOR_TYPES = {4, 6}
_MIME_BY_SIGNATURE = {
    PNG_SIGNATURE: "image/png",
    b"\xff\xd8\xff": "image/jpeg",
}
# A listing is bounded so one tool result cannot grow with the library.
ASSET_LIST_LIMIT = 50


class ImageFacts:
    """Decoded description of one image file; never a re-encoded copy."""

    __slots__ = ("mime_type", "size", "has_alpha")

    def __init__(self, mime_type: str, size: ImageSize, has_alpha: bool):
        self.mime_type = mime_type
        self.size = size
        self.has_alpha = has_alpha


def _png_facts(data: bytes) -> ImageFacts:
    if len(data) < 33 or data[12:16] != b"IHDR":
        raise AppError("IMAGE_INVALID", "PNG 结构不完整，未使用这张参考图。")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    color_type = data[25]
    alpha = color_type in PNG_ALPHA_COLOR_TYPES
    offset = 8
    while offset + 8 <= len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        chunk = data[offset + 4 : offset + 8]
        if chunk == b"tRNS":
            # A palette or truecolour image may still declare transparency.
            alpha = True
        if chunk in {b"IHDR", b"IDAT", b"IEND"} and length > len(data) - offset - 12:
            raise AppError("IMAGE_INVALID", "PNG 分块长度越界，未使用这张参考图。")
        if chunk == b"IDAT":
            break
        offset += length + 12
    return ImageFacts("image/png", ImageSize(width=width, height=height), alpha)


def _jpeg_facts(data: bytes) -> ImageFacts:
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            raise AppError("IMAGE_INVALID", "JPEG 标记结构不完整，未使用这张参考图。")
        marker = data[offset + 1]
        if marker in {0xD8, 0x01} or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        length = int.from_bytes(data[offset + 2 : offset + 4], "big")
        if length < 2 or offset + 2 + length > len(data):
            raise AppError("IMAGE_INVALID", "JPEG 分块长度越界，未使用这张参考图。")
        if 0xC0 <= marker <= 0xCF and marker not in {0xC4, 0xC8, 0xCC}:
            if length < 7:
                raise AppError("IMAGE_INVALID", "JPEG 帧头不完整，未使用这张参考图。")
            height = int.from_bytes(data[offset + 5 : offset + 7], "big")
            width = int.from_bytes(data[offset + 7 : offset + 9], "big")
            return ImageFacts("image/jpeg", ImageSize(width=width, height=height), False)
        offset += 2 + length
    raise AppError("IMAGE_INVALID", "JPEG 缺少帧头，未使用这张参考图。")


def _webp_facts(data: bytes) -> ImageFacts:
    if len(data) < 16 or data[8:12] != b"WEBP":
        raise AppError("IMAGE_INVALID", "WebP 结构不完整，未使用这张参考图。")
    kind = data[12:16]
    if kind == b"VP8X":
        if len(data) < 30:
            raise AppError("IMAGE_INVALID", "WebP 扩展头不完整，未使用这张参考图。")
        alpha = bool(data[20] & 0x10)
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return ImageFacts("image/webp", ImageSize(width=width, height=height), alpha)
    if kind == b"VP8L":
        if len(data) < 25 or data[20] != 0x2F:
            raise AppError("IMAGE_INVALID", "WebP 无损头不完整，未使用这张参考图。")
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return ImageFacts("image/webp", ImageSize(width=width, height=height), bool((bits >> 28) & 1))
    raise AppError("IMAGE_INVALID", "WebP 只支持 VP8X 或 VP8L 结构，未使用这张参考图。")


def inspect_image(data: bytes) -> ImageFacts:
    """Identify a supported image from its own bytes; reject anything else."""
    for signature, mime_type in _MIME_BY_SIGNATURE.items():
        if data.startswith(signature):
            return _png_facts(data) if mime_type == "image/png" else _jpeg_facts(data)
    if len(data) >= 12 and data.startswith(b"RIFF"):
        return _webp_facts(data)
    raise AppError("IMAGE_FORMAT_UNSUPPORTED", "参考图必须是 PNG、JPEG 或 WebP，未创建任务。")


def check_image_bounds(rule, facts: ImageFacts, size_bytes: int) -> None:
    """Apply the published capability bounds before anything is sent upstream."""
    if facts.mime_type not in rule.mime_types:
        raise AppError("IMAGE_FORMAT_UNSUPPORTED", "参考图格式不在能力表允许范围内，未创建任务。")
    if facts.has_alpha and not rule.allows_alpha:
        raise AppError("IMAGE_ALPHA_UNSUPPORTED", "PNG 透明通道不受支持，请改用不透明图片后重试。")
    if size_bytes > rule.max_bytes:
        raise AppError("IMAGE_TOO_LARGE", "参考图超过单图大小上限，未创建任务。")
    width, height = facts.size.width, facts.size.height
    if not (rule.min_side <= width <= rule.max_side and rule.min_side <= height <= rule.max_side):
        raise AppError("IMAGE_SIDE_OUT_OF_RANGE", "参考图单边像素不在允许范围内，未创建任务。")
    if (
        width * rule.min_aspect > height * rule.max_aspect
        or height * rule.min_aspect > width * rule.max_aspect
    ):
        raise AppError("IMAGE_ASPECT_OUT_OF_RANGE", "参考图宽高比不在允许范围内，未创建任务。")
    if not width or not height:
        raise AppError("IMAGE_INVALID", "参考图尺寸无效，未创建任务。")


class AssetFiles:
    """Read and write ``assets/<projectId>/<assetId>.<ext>`` inside one data directory."""

    def __init__(self, home):
        self.home = home.resolve()

    @contextmanager
    def parent(self, relative, *, create=False):
        parts = PurePosixPath(relative).parts
        if len(parts) != 3 or parts[0] != "assets" or "\\" in relative:
            raise AppError("ASSET_SOURCE_UNSAFE", "素材路径不符合访问规则。")
        name = parts[2]
        stem, _, extension = name.rpartition(".")
        if extension not in set(ASSET_EXTENSIONS.values()):
            raise AppError("ASSET_SOURCE_UNSAFE", "素材路径不符合访问规则。")
        try:
            if str(UUID(stem)) != stem:
                raise ValueError("Invalid asset ID")
        except (ValueError, AttributeError):
            raise AppError("ASSET_SOURCE_UNSAFE", "素材路径不符合访问规则。") from None
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
                    stack.enter_context(_windows_directory(path))
                else:
                    descriptor = os.open(
                        component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                    )
                    stack.callback(os.close, descriptor)
            if os.name != "nt" and path.resolve() != self.home / parts[0] / parts[1]:
                raise AppError("ASSET_SOURCE_UNSAFE", "素材目录不是普通目录。")
            yield path, descriptor, name

    @contextmanager
    def _handle(self, relative, *, create=False):
        with self.parent(relative, create=create) as (path, descriptor, name):
            target = path / name if descriptor is None else name
            yield target, descriptor

    def open(self, asset: Asset) -> FileLease:
        """Open one frozen asset and re-prove its recorded size and digest."""
        stack = ExitStack()
        try:
            target, descriptor = stack.enter_context(self._handle(asset.relative_path))
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            try:
                if descriptor is None:
                    _safe_stat(target.lstat())
                handle = os.open(target, flags, dir_fd=descriptor)
            except FileNotFoundError:
                raise AppError("ASSET_FILE_MISSING", "素材文件缺失或不可读。") from None
            except PermissionError:
                raise AppError("ASSET_FILE_MISSING", "素材文件不可读。") from None
            file = stack.enter_context(os.fdopen(handle, "rb"))
            info = os.fstat(file.fileno())
            _safe_stat(info)
            if not stat.S_ISREG(info.st_mode):
                raise AppError("ASSET_SOURCE_UNSAFE", "素材不是普通文件。")
            if info.st_size != asset.size_bytes or info.st_size > IMAGE_MAX_BYTES:
                raise AppError("ASSET_SIZE_MISMATCH", "素材文件与素材记录的大小不符。")
            digest = hashlib.sha256()
            remaining = info.st_size
            while remaining:
                chunk = file.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise AppError("ASSET_SIZE_MISMATCH", "素材文件与素材记录的大小不符。")
                digest.update(chunk)
                remaining -= len(chunk)
            if digest.hexdigest() != asset.sha256:
                raise AppError("ASSET_HASH_MISMATCH", "素材文件与素材记录的哈希不符。")
            file.seek(0)
            return FileLease(stack, file)
        except BaseException:
            stack.close()
            raise

    def read(self, asset: Asset) -> bytes:
        """Read one frozen asset, rejecting links, mismatches and oversized files."""
        with self.open(asset) as file:
            return file.read(asset.size_bytes)

    def write(self, relative: str, data: bytes) -> None:
        """Create one new asset file exclusively; never overwrite existing bytes."""
        with self._handle(relative, create=True) as (target, descriptor):
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_BINARY", 0)
            )
            try:
                handle = os.open(target, flags, mode=0o600, dir_fd=descriptor)
            except FileExistsError:
                raise AppError("ASSET_FILE_CONFLICT", "素材文件已存在，请重试导入。") from None
            except (PermissionError, OSError) as error:
                raise AppError("ASSET_WRITE_FAILED", "素材无法写入本地目录，请检查权限。") from error
            try:
                with os.fdopen(handle, "wb") as file:
                    file.write(data)
                    file.flush()
                    os.fsync(file.fileno())
            except OSError as error:
                raise AppError("ASSET_WRITE_FAILED", "素材无法写入本地目录，请检查磁盘与权限。") from error

    def remove(self, relative: str) -> None:
        """Best-effort cleanup of a file whose record was never committed."""
        try:
            with self._handle(relative) as (target, descriptor):
                info = os.stat(target, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode):
                    os.unlink(target, dir_fd=descriptor)
        except (OSError, AppError, MediaError):
            return

    def rewrite(self, relative: str, data: bytes) -> None:
        """Restore one asset file whose bytes are already proven by its record.

        Only the import path calls this, with bytes whose SHA-256 equals the frozen
        record, so the identity of the asset does not change.
        """
        with self._handle(relative) as (target, descriptor):
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_TRUNC
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_BINARY", 0)
            )
            try:
                handle = os.open(target, flags, mode=0o600, dir_fd=descriptor)
            except (PermissionError, OSError) as error:
                raise AppError("ASSET_WRITE_FAILED", "素材无法写入本地目录，请检查权限。") from error
            try:
                with os.fdopen(handle, "wb") as file:
                    file.write(data)
                    file.flush()
                    os.fsync(file.fileno())
            except OSError as error:
                raise AppError("ASSET_WRITE_FAILED", "素材无法写入本地目录，请检查磁盘与权限。") from error


def verified_reference_data(store_home, asset: Asset, rule) -> tuple[bytes, ImageFacts]:
    """Read, re-verify and bound-check one reference image before submission."""
    if asset.kind != "image" or asset.mime_type not in ASSET_EXTENSIONS:
        raise AppError("ASSET_NOT_IMAGE", "镜头参考图必须是素材库中的图像。")
    data = AssetFiles(store_home).read(asset)
    facts = inspect_image(data)
    if facts.mime_type != asset.mime_type:
        raise AppError("ASSET_MIME_MISMATCH", "参考图内容与素材记录的格式不符，未创建任务。")
    if (facts.size.width, facts.size.height) != (asset.dimensions.width, asset.dimensions.height):
        raise AppError("ASSET_MIME_MISMATCH", "参考图尺寸与素材记录不符，未创建任务。")
    check_image_bounds(rule, facts, asset.size_bytes)
    return data, facts


def asset_reference_loader(store, rule):
    """Bind one data directory and one published image rule to a loader.

    The adapter calls this while building a body, so the bytes are proven at the
    moment of submission rather than only when the Job was registered.
    """

    def load(item: ShotMediaInput) -> str:
        raw = store.snapshot()["assets"].get(item.asset_id)
        if raw is None:
            raise AppError("ASSET_NOT_FOUND", "参考图不在素材库中，未提交。")
        asset = Asset.model_validate(raw)
        if (asset.sha256, asset.size_bytes, asset.mime_type) != (
            item.sha256,
            item.size_bytes,
            item.mime_type,
        ):
            raise AppError("ASSET_MISMATCH", "素材记录与镜头请求不符，未提交。")
        data, facts = verified_reference_data(store.home, asset, rule)
        return data_url(facts.mime_type, data)

    return load


def data_url(mime_type: str, data: bytes) -> str:
    """The documented image-only delivery form; audio and video stay URL-only."""
    import base64

    return f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"


def asset_metadata(asset: Asset) -> dict:
    """The stored identity of one asset; never a filesystem path or private URL."""
    return {
        "assetId": asset.id,
        "projectId": asset.project_id,
        "kind": asset.kind,
        "mimeType": asset.mime_type,
        "sizeBytes": asset.size_bytes,
        "sha256": asset.sha256,
        "dimensions": {"width": asset.dimensions.width, "height": asset.dimensions.height},
        "createdAt": asset.created_at,
        "source": asset.source,
        "assetFormatVersion": asset.asset_format_version,
    }


def asset_public_metadata(asset: Asset) -> dict:
    """The bounded tool view: no private path and no integrity fingerprint."""
    return {key: value for key, value in asset_metadata(asset).items() if key != "sha256"}


class AssetService:
    """The M2 asset library: import, per-project dedup, listing and verified delivery.

    Import is a trusted local action (CLI path, Web upload from the loopback UI or
    an already-imported record), never an Agent tool: the model can only list what
    the library already holds. Nothing here contacts a provider.
    """

    def __init__(self, store):
        self.store, self.files = store, AssetFiles(store.home)

    def asset(self, asset_id: str, *, project_id: str | None = None) -> Asset:
        raw = self.store.snapshot()["assets"].get(asset_id)
        if raw is None or (project_id is not None and raw["projectId"] != project_id):
            raise AppError("ASSET_NOT_FOUND", "当前数据目录中没有这个素材。")
        return Asset.model_validate(raw)

    def metadata(self, asset: Asset) -> dict:
        return asset_metadata(asset)

    def list(self, *, project_id: str | None = None, limit: int | None = None) -> list[dict]:
        """Newest-first metadata; never a filesystem path or a private URL."""
        items = [
            Asset.model_validate(raw)
            for raw in self.store.snapshot()["assets"].values()
            if project_id is None or raw["projectId"] == project_id
        ]
        items.sort(key=lambda asset: (asset.created_at, asset.id), reverse=True)
        if limit is not None:
            items = items[:limit]
        return [self.metadata(asset) for asset in items]

    def availability(self, asset: Asset) -> tuple[str, str | None]:
        try:
            with self.files.open(asset):
                return "available", None
        except AppError as error:
            reason = error.code.removeprefix("ASSET_").lower()
            return "unavailable", "missing" if reason == "file_missing" else reason

    def view(self, asset_id: str, *, project_id: str | None = None) -> dict:
        asset = self.asset(asset_id, project_id=project_id)
        status, reason = self.availability(asset)
        return {
            **self.metadata(asset),
            "availability": {"status": status, "reason": reason},
            "available": status == "available",
            "contentUrl": f"/api/assets/{asset.id}/content" if status == "available" else None,
        }

    def open_content(self, asset_id: str, *, project_id: str | None = None):
        """Open verified content for ranged local delivery, or explain why not."""
        asset = self.asset(asset_id, project_id=project_id)
        try:
            return asset, self.files.open(asset)
        except AppError as error:
            raise AppError(
                "ASSET_UNAVAILABLE", f"本地素材不可用（{error.code}）；记录保留，可重新导入同一张图片。"
            ) from None

    def import_bytes(self, data: bytes, *, project_id: str, filename: str | None = None) -> dict:
        """Validate one image and add it to the library; identical bytes are reused."""
        if not isinstance(data, bytes) or not data:
            raise AppError("ASSET_EMPTY", "没有读到素材内容，请重新选择文件。")
        if len(data) > IMAGE_MAX_BYTES:
            raise AppError("IMAGE_TOO_LARGE", "参考图超过单图大小上限，未导入素材。")
        facts = inspect_image(data)
        check_image_bounds(ImageInputRule(), facts, len(data))
        digest = hashlib.sha256(data).hexdigest()
        if project_id not in self.store.snapshot()["projects"]:
            raise AppError("NOT_FOUND", "当前数据目录中没有这个项目，未导入素材。")
        written: list[str] = []

        def import_asset(draft):
            for raw in draft["assets"].values():
                if raw["projectId"] == project_id and raw["sha256"] == digest:
                    existing = Asset.model_validate(raw)
                    repaired = self._repair(existing, data)
                    return {**self._imported(existing, deduplicated=True), "repaired": repaired}
            asset_id = str(uuid4())
            relative = f"assets/{project_id}/{asset_id}.{ASSET_EXTENSIONS[facts.mime_type]}"
            self.files.write(relative, data)
            written.append(relative)
            asset = Asset(
                id=asset_id,
                project_id=project_id,
                relative_path=relative,
                mime_type=facts.mime_type,
                size_bytes=len(data),
                sha256=digest,
                dimensions=ImageDimensions(width=facts.size.width, height=facts.size.height),
                created_at=utc_now(),
            )
            draft["assets"][asset_id] = asset.model_dump(mode="json", by_alias=True)
            return self._imported(asset, deduplicated=False)

        try:
            return self.store.transaction(import_asset)
        except BaseException:
            for relative in written:
                self.files.remove(relative)
            raise

    def import_path(self, path, *, project_id: str, filename: str | None = None) -> dict:
        """Import one local file chosen by a trusted local caller."""
        target = os.fspath(path)
        try:
            info = os.stat(target)
        except OSError:
            raise AppError("ASSET_IMPORT_UNREADABLE", "无法读取这个文件，请检查路径与权限。") from None
        if not stat.S_ISREG(info.st_mode):
            raise AppError("ASSET_IMPORT_UNREADABLE", "只能导入普通文件。")
        if info.st_size > IMAGE_MAX_BYTES:
            raise AppError("IMAGE_TOO_LARGE", "参考图超过单图大小上限，未导入素材。")
        try:
            with open(target, "rb") as handle:
                data = handle.read(IMAGE_MAX_BYTES + 1)
        except OSError:
            raise AppError("ASSET_IMPORT_UNREADABLE", "无法读取这个文件，请检查路径与权限。") from None
        return self.import_bytes(data, project_id=project_id, filename=filename)

    def _repair(self, asset: Asset, data: bytes) -> bool:
        """Re-importing identical bytes restores a missing or damaged library file."""
        try:
            with self.files.open(asset):
                return False
        except AppError:
            pass
        self.files.rewrite(asset.relative_path, data)
        return True

    @staticmethod
    def _imported(asset: Asset, *, deduplicated: bool) -> dict:
        return {
            "assetId": asset.id,
            "projectId": asset.project_id,
            "kind": asset.kind,
            "mimeType": asset.mime_type,
            "sizeBytes": asset.size_bytes,
            "sha256": asset.sha256,
            "dimensions": {"width": asset.dimensions.width, "height": asset.dimensions.height},
            "createdAt": asset.created_at,
            "deduplicated": deduplicated,
            "repaired": False,
        }
