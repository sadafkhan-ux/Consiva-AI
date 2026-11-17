"""Records every HTTP error our API answers, and every exception it did not handle, as an error event, and gives each request an id.

It changes nothing about the response except one header, X-Request-ID, so a person who sees an error on screen (or in an email) can find the log line of that request. An exception is recorded and then
raised again exactly as before. Expected client mistakes (400, 401, 403, 404, 409, 422) are recorded as information for the dashboard and never emailed (see monitoring.classify).
"""

from __future__ import annotations

import logging
import re
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

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
                # Answered here, not raised again, so the response can carry the request id: a person who sees "Internal server error" can quote it, and it is the id on the error event.
                return JSONResponse({"detail": f"Internal server error. Reference {rid}."}, status_code=500, headers={"X-Request-ID": rid})
            response.headers["X-Request-ID"] = rid
            if response.status_code >= 400 and not skip:
                await events.record_error(agent=agent_of(request.url.path), module=module_of(request), status=response.status_code, service=service_of(request.url.path), upstream=False,
                                          message=f"HTTP {response.status_code} from {request.method} {_ID.sub(':id', request.url.path)}", request_id=rid)
            return response
        finally:
            request_id_var.reset(token)
