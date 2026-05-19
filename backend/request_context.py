"""
Raw ASGI middleware that stamps every HTTP request with a UUID request_id
and propagates X-Correlation-ID from the client.

Raw ASGI (not BaseHTTPMiddleware) is intentional: BaseHTTPMiddleware buffers
the entire response body before sending it, which breaks SSE streaming on
/api/query/stream. Raw ASGI wraps the send callable so we can inject the
X-Request-ID response header without touching the body at all.
"""

import uuid
from typing import Callable

from logging_config import bind_request_context


class RequestContextMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return

        # Parse X-Correlation-ID from the raw ASGI headers list
        # (list of [bytes, bytes] pairs).
        headers = dict(scope.get('headers') or [])
        correlation_id = (
            headers.get(b'x-correlation-id', b'').decode('utf-8', errors='ignore') or None
        )
        request_id = str(uuid.uuid4())
        bind_request_context(request_id, correlation_id)

        # Wrap `send` so we can inject X-Request-ID on the response start
        # message without buffering or touching the response body.
        async def send_with_request_id(message) -> None:
            if message['type'] == 'http.response.start':
                headers_list = list(message.get('headers') or [])
                headers_list.append(
                    (b'x-request-id', request_id.encode('utf-8'))
                )
                message = {**message, 'headers': headers_list}
            await send(message)

        await self.app(scope, receive, send_with_request_id)
