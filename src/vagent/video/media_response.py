"""Single-range HTTP delivery from a verified, stable local file handle."""

import re

from starlette.responses import Response, StreamingResponse

from vagent.video.media import file_io


class UnsatisfiableRange(ValueError):
    pass


def byte_range(value, size):
    match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", value or "", flags=re.ASCII)
    if match is None or not any(match.groups()):
        return None
    left, right = match.groups()

    def bounded(digits):
        digits = digits.lstrip("0") or "0"
        return int(digits) if len(digits) < 20 else size + 1

    if left:
        start, end = bounded(left), bounded(right) if right else size - 1
        if start >= size or end < start:
            raise UnsatisfiableRange()
        return start, min(end, size - 1)
    suffix = bounded(right)
    if suffix == 0:
        raise UnsatisfiableRange()
    return max(0, size - suffix), size - 1


class LocalMediaResponse(StreamingResponse):
    def __init__(self, lease, start, length, **kwargs):
        self.lease = lease

        async def chunks():
            await file_io(lease.file.seek, start)
            remaining = length
            while remaining:
                chunk = await file_io(lease.file.read, min(65536, remaining))
                if not chunk:
                    raise OSError("Local media changed during delivery")
                remaining -= len(chunk)
                yield chunk

        super().__init__(chunks(), **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.lease.close()


def media_response(asset, lease, *, method, request_headers, download=False):
    etag = f'"{asset.sha256}"'
    headers = {
        "Content-Type": "video/mp4",
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Content-Length": str(asset.size_bytes),
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'{"attachment" if download else "inline"}; filename="{asset.id}.mp4"',
    }
    if method == "HEAD":
        lease.close()
        return Response(headers=headers)
    chosen = None
    if request_headers.get("if-range") in {None, etag}:
        try:
            chosen = byte_range(request_headers.get("range"), asset.size_bytes)
        except UnsatisfiableRange:
            lease.close()
            headers.update({"Content-Range": f"bytes */{asset.size_bytes}", "Content-Length": "0"})
            return Response(status_code=416, headers=headers)
    start, end = chosen or (0, asset.size_bytes - 1)
    length = end - start + 1
    headers["Content-Length"] = str(length)
    if chosen:
        headers["Content-Range"] = f"bytes {start}-{end}/{asset.size_bytes}"
    return LocalMediaResponse(lease, start, length, status_code=206 if chosen else 200, headers=headers)
