"""Retry connection establishment only, never a request that may have reached the model."""

import asyncio

import httpx


class ModelHttpClient(httpx.AsyncClient):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.connection_retries = 0

    async def send(self, request, **kwargs):
        auth = kwargs.get("auth", httpx.USE_CLIENT_DEFAULT)
        if auth is httpx.USE_CLIENT_DEFAULT:
            auth = self.auth
        follow = kwargs.get("follow_redirects", httpx.USE_CLIENT_DEFAULT)
        if follow is httpx.USE_CLIENT_DEFAULT:
            follow = self.follow_redirects
        # Redirects and multi-request authentication flows could have already sent
        # a request before a later connection failed. Never replay those flows.
        eligible = (
            request.url.scheme == "https"
            and request.url.host == "api.deepseek.com"
            and request.url.path == "/chat/completions"
            and request.method == "POST"
            and isinstance(request.stream, httpx.ByteStream)
            and request.extensions.get("vagent_connect_retries", True)
            and auth is None
            and not follow
        )
        for attempt in range(3 if eligible else 1):
            try:
                return await super().send(request, **kwargs)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                # HTTPX raises these before sending HTTP request bytes. Write/read
                # errors, response status errors and stream failures are not retried.
                if not eligible or attempt == 2:
                    raise
                await asyncio.sleep(0.2 * (attempt + 1))
                self.connection_retries += 1
