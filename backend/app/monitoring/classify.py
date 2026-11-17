"""What kind of failure is this? One place that turns an HTTP status or an exception into a category, a severity, whether it can clear by itself, and what to do about it.

An HTTP status is not one kind of failure: a 401 from the Claude API is an expired key (an administrator must act), the same 401 from our own API is a visitor who is not signed in (nobody must act), a 429 is a
rate limit (wait), a 502 is a gateway that could not reach its backend (look at the backend), a 422 is a mistake in what the caller sent. Each gets its own category and its own advice, and only the failures
that need a person's attention send an email (see `should_email`). Expected client mistakes (400, 404, 409, 422 and a visitor's 401 or 403) are recorded for the dashboard and never emailed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

INFO, WARNING, CRITICAL = "info", "warning", "critical"

#: Groups, for the dashboard's filters and cards.
API, RAG, DATABASE, LLM, EMBEDDING, CRAWLER, WORKER, WEBSITE, CONFIG = "api", "rag", "database", "llm", "embedding", "crawler", "worker", "website_access", "config"
GROUPS = (API, RAG, DATABASE, LLM, EMBEDDING, CRAWLER, WORKER, WEBSITE, CONFIG)


@dataclass(frozen=True)
class Classified:
    category: str
    grouping: str
    severity: str
    temporary: bool
    steps: str
    always_email: bool = False
    title: str = ""


# ---- HTTP status ------------------------------------------------------------------------------------------------------------------------------------------------------

def _http(status: int, service: str | None, upstream: bool) -> Classified:
    """`upstream` is true when the status came from a service WE called (an LLM provider, an embedding API, a partner) rather than from our own API answering a caller."""
    svc = service or "the service"
    if status == 401:
        if upstream:
            return Classified("api_key_invalid", CONFIG, CRITICAL, False, f"The key for {svc} was rejected (expired, revoked or mistyped). Create a new key with the provider, put it in backend/.env, and restart the backend and worker.", True, "API key rejected")
        return Classified("unauthorized", API, INFO, True, "The caller was not signed in or its token expired. No action is needed unless a service key is being rejected: check the Authorization header.", False, "Unauthorized request")
    if status == 403:
        if upstream:
            return Classified("forbidden", CONFIG, CRITICAL, False, f"{svc} refused the request: the key is valid but not allowed this operation (plan, scope, region or IP restriction). Check the key's permissions with the provider.", True, "Request forbidden by a service")
        return Classified("forbidden", API, INFO, True, "The caller is signed in but not allowed this action (usually a non-administrator). No action is needed unless an administrator was refused.", False, "Forbidden request")
    if status == 400:
        return Classified("bad_request", API, WARNING if upstream else INFO, True, "The request was malformed. If a service rejected our request, the payload or the model name may be wrong: check the request the application sent." if upstream else "The caller sent a malformed request. No action is needed.", False, "Bad request")
    if status in (404, 410) and upstream and service in ("embedding_api", "claude_api", "nvidia_api", "llm"):
        return Classified("model_unavailable", CONFIG, CRITICAL, False, f"{svc} no longer serves the configured model or endpoint (HTTP {status}: not found or gone): the provider retired or renamed it. "
                          "Choose a current model in backend/.env (NVIDIA_EMBED_MODEL, NVIDIA_LLM_MODEL or ANTHROPIC_MODEL). A new embedding model needs the same vector size and a re-index of the knowledge base.", True, "Model or endpoint no longer available")
    if status == 410:
        return Classified("gone", API, WARNING if upstream else INFO, True, f"{svc} says the resource is gone (HTTP 410). If a service answered it, the endpoint or model was retired.", False, "Gone")
    if status == 404:
        return Classified("not_found", API, WARNING if upstream else INFO, True, "The address does not exist. If a service returned it, check the configured URL or model name." if upstream else "The caller asked for something that does not exist. No action is needed.", False, "Not found")
    if status == 408:
        return Classified("request_timeout", API, WARNING, True, "The request took too long. It may succeed on a retry; if it keeps happening check the network and the service's load.", False, "Request timeout")
    if status == 409:
        return Classified("conflict", API, INFO, True, "The request conflicted with the current state (for example a scan that is already running). No action is needed.", False, "Conflict")
    if status == 422:
        return Classified("validation_error", API, WARNING if upstream else INFO, True, "The request failed validation. If a service rejected our request, check the fields the application sent." if upstream else "The caller sent a field that failed validation. No action is needed.", False, "Validation error")
    if status == 429:
        return Classified("rate_limited", API, WARNING, True, f"{svc} is limiting the request rate. It clears by itself; if it keeps happening, lower the concurrency or ask the provider for a higher limit.", False, "Rate limited")
    if status == 500:
        return Classified("internal_error", API, CRITICAL, True, f"{svc} failed with an internal error. Open the backend log around the time shown and look for the traceback of this request id.", True, "Internal server error")
    if status == 502:
        return Classified("bad_gateway", API, CRITICAL, True, f"A gateway could not get an answer from {svc}. Check that the backend container is running and healthy (docker compose ps), and its log for a crash or a restart.", True, "Bad gateway")
    if status == 503:
        return Classified("service_unavailable", API, CRITICAL, True, f"{svc} is unavailable or overloaded. Check that it is running and has capacity; it usually clears when it recovers.", True, "Service unavailable")
    if status == 504:
        return Classified("gateway_timeout", API, CRITICAL, True, f"A gateway gave up waiting for {svc}. Check the service's load and the proxy timeouts.", True, "Gateway timeout")
    if status >= 500:
        return Classified("server_error", API, CRITICAL, True, f"{svc} answered HTTP {status}. Check its log.", True, f"Server error {status}")
    if status >= 400:
        return Classified("client_error", API, INFO, True, "The caller made an invalid request. No action is needed.", False, f"Client error {status}")
    return Classified("ok", API, INFO, True, "", False, "")


def classify_status(status: int, *, service: str | None = None, upstream: bool = False) -> Classified:
    return _http(int(status), service, upstream)


# ---- exceptions -------------------------------------------------------------------------------------------------------------------------------------------------------

_KEY_MARKERS = ("invalid x-api-key", "invalid api key", "incorrect api key", "authentication_error", "invalid_api_key", "api key expired", "api key not valid", "unauthorized: invalid", "permission_denied")
_MISSING_CONFIG = re.compile(r"(requires? .{0,60}to be set|is not set|not configured|missing .{0,30}(api[_ ]key|setting|configuration)|no api key)", re.I)
_VECTOR = ("pgvector", "vector_cosine_ops", "different vector dimensions", "operator does not exist: vector", "type \"vector\" does not exist", "hnsw", "ivfflat", "<=>")
_DB_DOWN = ("connection refused", "could not connect", "the database system is starting up", "connection was closed", "server closed the connection", "connection reset", "too many clients",
            "remaining connection slots", "cannot connect now", "terminating connection", "connection is closed", "timeout expired", "queuepool limit", "connection pool")
_BROWSER = ("target closed", "browser has been closed", "browser closed", "playwright", "chromium", "net::err_", "execution context was destroyed", "page crashed", "browsertype.launch", "protocol error")
_EXTRACT = ("pdf", "docx", "ocr", "tesseract", "extraction", "could not extract", "encrypted", "pymupdf", "fitz")
_CHUNK = ("chunk",)


def status_of(exc: BaseException) -> int | None:
    """The HTTP status carried by an exception from an HTTP client or a provider SDK, if it carries one."""
    for attr in ("status_code", "status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int) and 100 <= v <= 599:
            return v
    resp = getattr(exc, "response", None)
    v = getattr(resp, "status_code", None) if resp is not None else None
    if isinstance(v, int) and 100 <= v <= 599:
        return v
    m = re.search(r"\b(?:HTTP|status(?: code)?|error code)[ :]*([45]\d\d)\b", str(exc), re.I)
    return int(m.group(1)) if m else None


def classify_exception(exc: BaseException, *, service: str | None = None, module: str | None = None, upstream: bool = True) -> Classified:
    """Classify a caught exception. `service` names what was being called (claude_api, embedding_api, postgres, vector_db, policy_kb, crawler ...): it decides the group when the exception alone does not."""
    name = type(exc).__name__
    text = f"{name} {exc}".lower()
    mod = (module or "").lower()

    if any(m in text for m in _KEY_MARKERS) or (status_of(exc) == 401 and upstream):
        return _http(401, service, True)
    if name == "EmbeddingFailed":                       # raised by the knowledge base after its retries: the cause (a status, a timeout) is in the message, the kind is an embedding failure
        if re.search(r"\b(404|410)\b", str(exc)):
            return _http(410, "embedding_api", True)
        return Classified("embedding_failure", EMBEDDING, CRITICAL, True, "Embeddings could not be generated, so policy text cannot be indexed or searched semantically. Check the embedding provider's key and availability "
                          "(the cause is in the error message); searches fall back to keywords meanwhile.", True, "Embedding failure")
    st = status_of(exc)
    if st is not None and st >= 400:
        c = _http(st, service, upstream)
        group = {"embedding_api": EMBEDDING, "claude_api": LLM, "nvidia_api": LLM, "llm": LLM, "policy_kb": RAG, "vector_db": DATABASE, "postgres": DATABASE}.get(service or "")
        return Classified(c.category, group or c.grouping, c.severity, c.temporary, c.steps, c.always_email, c.title) if group and c.grouping == API else c
    if any(m in text for m in _VECTOR):
        return Classified("vector_db_error", DATABASE, CRITICAL, False, "The vector search failed. Check that the pgvector extension is installed, that the embedding dimension (1024) matches the column, and that the index exists (python migrate.py --status).", True, "Vector database error")
    if "timeout" in name.lower() or "timed out" in text or "timeouterror" in text or "readtimeout" in text:
        grouping = {"embedding_api": EMBEDDING, "claude_api": LLM, "nvidia_api": LLM, "llm": LLM, "policy_kb": RAG, "postgres": DATABASE, "crawler": CRAWLER, "browser": CRAWLER}.get(service or "", API)
        return Classified("timeout", grouping, WARNING, True, "The call did not answer in time. It may succeed on a retry; if it keeps happening check the service's load and the network.", False, "Timeout")
    if any(m in text for m in _DB_DOWN) or "operationalerror" in text or "interfaceerror" in text or "connectiondoesnotexist" in text or "cannotconnectnow" in text or "invalidcatalogname" in text:
        return Classified("database_unavailable", DATABASE, CRITICAL, True, "PostgreSQL cannot be reached. Check that the db container is running and healthy (docker compose ps db), the connection limit (max_connections), and DATABASE_URL.", True, "PostgreSQL unavailable")
    if "connecterror" in text or "connectionerror" in text or "connection refused" in text or "name or service not known" in text or "nodename nor servname" in text or "all connection attempts failed" in text or "network is unreachable" in text:
        grouping = {"embedding_api": EMBEDDING, "claude_api": LLM, "nvidia_api": LLM, "llm": LLM, "policy_kb": RAG}.get(service or "", API)
        return Classified("connection_failed", grouping, CRITICAL, True, f"{service or 'The service'} could not be reached (connection refused, DNS or no route). Check its address in the configuration, the network and that the service is running.", True, "Service unreachable")
    if _MISSING_CONFIG.search(str(exc)):
        return Classified("config_missing", CONFIG, CRITICAL, False, "A required setting is missing. Set it in backend/.env (the name is in the message) and restart the backend and worker.", True, "Missing configuration")
    if "certificate" in text or "ssl" in text and "verify" in text:
        return Classified("certificate_invalid", WEBSITE if (service or "") in ("website", "crawler") else API, WARNING, False, "A TLS certificate is not valid (expired, self-signed or wrong host). The owner of that site or service must renew it.", False, "Certificate problem")
    if any(m in text for m in _BROWSER) or service in ("browser", "crawler"):
        return Classified("browser_failure", CRAWLER, WARNING, True, "The browser or the page failed while crawling (crashed, closed, or a navigation error). If it repeats for every site, check the container's memory and /dev/shm (shm_size).", False, "Crawler or browser failure")
    if service == "embedding_api" or "embedding" in mod or "embed" in text:
        return Classified("embedding_failure", EMBEDDING, CRITICAL, True, "Embeddings could not be generated, so policy text cannot be indexed or searched semantically. Check the embedding provider's key and availability; searches fall back to keywords meanwhile.", True, "Embedding failure")
    if service == "policy_kb" or "policykb" in mod or mod.startswith("rag"):
        return Classified("rag_failure", RAG, CRITICAL, True, "The policy knowledge base failed. Check the database, the embedding provider and the knowledge base consistency check (GET /api/v1/enterprise/policy-kb/consistency).", True, "Knowledge base failure")
    if any(m in text for m in _EXTRACT) and ("extract" in text or "ocr" in text or "pdf" in text or "docx" in text):
        return Classified("extraction_failure", CRAWLER, WARNING, False, "A document could not be read (corrupt, encrypted, scanned without OCR, or unsupported). The scan continues without it; the document is listed as unreadable.", False, "Document extraction failure")
    if any(m in text for m in _CHUNK):
        return Classified("chunking_failure", RAG, WARNING, True, "Text could not be split into passages. Check the page's stored text.", False, "Chunking failure")
    if service == "worker" or "worker" in mod or "queue" in mod:
        return Classified("worker_failure", WORKER, CRITICAL, True, "A background job failed. Open the worker log (docker compose logs worker) around the time shown.", True, "Background job failed")
    return Classified("unexpected_error", API, CRITICAL, True, "An unexpected error occurred. Open the log of the service named above around the time shown and look for the traceback.", True, "Unexpected error")


def should_email(c: Classified, *, extra_statuses: tuple[int, ...] = (), status: int | None = None) -> bool:
    """Whether a NEW event of this kind emails an administrator: anything critical, an explicit flag (bot block, a rejected key), or a status the operator listed. Expected client mistakes never do."""
    if c.severity == INFO:
        return False
    return c.always_email or c.severity == CRITICAL or (status is not None and status in extra_statuses)
