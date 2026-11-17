"""Records every HTTP error our API answers, and every exception it did not handle, as an error event, and gives each request an id.

It changes NOTHING about any response: no body, no status, no header (the API contract with the website must not move). The request id is kept only inside the server, on the event and in the log.
An exception is recorded and then raised again exactly as before. Expected client mistakes (400, 401, 403, 404, 409, 422) are recorded as information for the dashboard and never emailed (see monitoring.classify).
"""

from __future__ import annotations

import logging
import re
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.monitoring import events
from app.monitoring.context import request_id_var

logger = logging.getLogger(__name__)
_SKIP = ("/health", "/docs", "/openapi.json", "/redoc")
_ID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|\b\d{6,}\b")


def agent_of(path: str) -> str:
    if path.startswith("/api/v1/enterprise"):
        return "enterprise"
    if path.startswith(("/api/v1/consent", "/api/v1/consent-agent")):
        return "five_page"
    return "platform"


def module_of(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None) or _ID.sub(":id", request.url.path)
    return f"{request.method} {path}"[:160]


def service_of(path: str) -> str:
    if "/policy-kb" in path:
        return "policy_kb"
    return "api"


class ErrorMonitorMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = (request.headers.get("x-request-id") or "").strip()[:64] or uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        try:
            skip = request.url.path.startswith(_SKIP)
            try:
                response = await call_next(request)
            except Exception as exc:
                if not skip:
                    await events.record_error(agent=agent_of(request.url.path), module=module_of(request), exc=exc, status=500, service=service_of(request.url.path), upstream=False, request_id=rid)
                logger.exception("Unhandled exception in %s (request %s)", module_of(request), rid)
                raise                      # exactly as before: the response is the application's own, unchanged. Monitoring only records.
            if response.status_code >= 400 and not skip:
                await events.record_error(agent=agent_of(request.url.path), module=module_of(request), status=response.status_code, service=service_of(request.url.path), upstream=False,
                                          message=f"HTTP {response.status_code} from {request.method} {_ID.sub(':id', request.url.path)}", request_id=rid)
            return response
        finally:
            request_id_var.reset(token)
