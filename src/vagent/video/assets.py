"""Reference-image integrity and safe local reading for the asset library.

The asset library itself (upload, import, listing) belongs to D3; D2 only needs
the read path: prove that the frozen asset record still matches its file, decode
the published image bounds, and hand the adapter verified bytes for the data URL.
Path handling reuses the media module's no-follow primitives so a symlinked or
reparse-point asset directory is rejected exactly like the media directory.
"""

import hashlib
import os
import stat
from contextlib import ExitStack
from pathlib import PurePosixPath
from uuid import UUID

from vagent.errors import AppError
from vagent.video.contracts import IMAGE_MAX_BYTES, ImageSize, ShotMediaInput
from vagent.video.media import MediaError, _safe_stat, _windows_directory
from vagent.video.storyboards import ASSET_EXTENSIONS, Asset

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_ALPHA_COLOR_TYPES = {4, 6}
_MIME_BY_SIGNATURE = {
    PNG_SIGNATURE: "image/png",
    b"\xff\xd8\xff": "image/jpeg",
}


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


class AssetFiles:
    """Read-only access to ``assets/<projectId>/<assetId>.<ext>`` inside one data directory."""

    def __init__(self, home):
        self.home = home.resolve()

    def _parent(self, relative):
        parts = PurePosixPath(relative).parts
        if len(parts) != 3 or parts[0] != "assets" or "\\" in relative:
            raise AppError("ASSET_SOURCE_UNSAFE", "素材路径不符合访问规则。")
        name = parts[2]
        stem = name.rsplit(".", 1)[0] if "." in name else name
        try:
            if str(UUID(stem)) != stem:
                raise ValueError("Invalid asset ID")
        except (ValueError, AttributeError):
            raise AppError("ASSET_SOURCE_UNSAFE", "素材路径不符合访问规则。") from None
        stack = ExitStack()
        try:
            path = self.home
            descriptor = None
            if os.name == "nt":
                stack.enter_context(_windows_directory(path))
            else:
                descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                stack.callback(os.close, descriptor)
            for component in parts[:2]:
                path = path / component
                if descriptor is None:
                    try:
                        _safe_stat(path.lstat())
                    except MediaError:
                        raise AppError("ASSET_SOURCE_UNSAFE", "素材目录不是普通目录。") from None
                    stack.enter_context(_windows_directory(path))
                else:
                    try:
                        descriptor = os.open(
                            component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                        )
                    except OSError:
                        raise AppError("ASSET_SOURCE_UNSAFE", "素材目录不是普通目录。") from None
                    stack.callback(os.close, descriptor)
            return stack, path, descriptor, name
        except BaseException:
            stack.close()
            raise

    def read(self, asset: Asset) -> bytes:
        """Read one frozen asset, rejecting links, mismatches and oversized files."""
        stack, path, descriptor, name = self._parent(asset.relative_path)
        try:
            target = path / name if descriptor is None else name
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            try:
                if descriptor is None:
                    _safe_stat(target.lstat())
                handle = os.open(target, flags, dir_fd=descriptor)
                info = os.fstat(handle)
                _safe_stat(info)
                if not stat.S_ISREG(info.st_mode):
                    raise AppError("ASSET_SOURCE_UNSAFE", "素材不是普通文件。")
                if info.st_size != asset.size_bytes or info.st_size > IMAGE_MAX_BYTES:
                    raise AppError("ASSET_SIZE_MISMATCH", "参考图与素材记录的大小不符，未创建任务。")
                with os.fdopen(handle, "rb") as file:
                    data = file.read(asset.size_bytes + 1)
            except FileNotFoundError:
                raise AppError("ASSET_FILE_MISSING", "参考图文件缺失或不可读，未创建任务。") from None
            except PermissionError:
                raise AppError("ASSET_FILE_MISSING", "参考图文件不可读，未创建任务。") from None
            if len(data) != asset.size_bytes:
                raise AppError("ASSET_SIZE_MISMATCH", "参考图与素材记录的大小不符，未创建任务。")
        finally:
            stack.close()
        if hashlib.sha256(data).hexdigest() != asset.sha256:
            raise AppError("ASSET_HASH_MISMATCH", "参考图与素材记录的哈希不符，未创建任务。")
        return data


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
