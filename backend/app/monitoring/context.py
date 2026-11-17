"""The request (correlation) id of the request being handled, so an error event can name it and a person can find the matching log line."""

from contextvars import ContextVar

request_id_var: ContextVar[str] = ContextVar("request_id", default="")
