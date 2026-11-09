"""Plugin Web API helpers for AstrBot's current and earlier backends."""
from __future__ import annotations

try:
    from astrbot.api.web import error_response, json_response, request, stream_response
except ImportError:
    from quart import Response, jsonify
    from quart import request as _quart_request

    class _RequestProxy:
        @property
        def method(self):
            return _quart_request.method

        @property
        def query(self):
            return _quart_request.args

        @property
        def headers(self):
            return _quart_request.headers

        async def json(self, default=None):
            try:
                return await _quart_request.get_json()
            except Exception:
                return default

        async def body(self):
            return await _quart_request.get_data()

    request = _RequestProxy()

    def json_response(data=None, *, status_code=200, headers=None):
        response = jsonify({} if data is None else data)
        response.status_code = status_code
        if headers:
            response.headers.update(headers)
        return response

    def error_response(message, *, status_code=400, data=None, headers=None):
        return json_response(
            {"status": "error", "message": message, "data": data},
            status_code=status_code,
            headers=headers,
        )

    def stream_response(
        content, *, content_type="text/event-stream", status_code=200, headers=None
    ):
        return Response(
            content, content_type=content_type, status=status_code, headers=headers
        )


__all__ = ["error_response", "json_response", "request", "stream_response"]
