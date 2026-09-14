from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import traceback
from typing import Any


@dataclass(frozen=True)
class ErrorDiagnostic:
    error_class: str
    error_category: str
    error_code: str
    observed_error: str
    retryable: bool
    service: str
    operation: str
    status_code: int | None
    request_id: str
    error_location: str
    resolution_hint: str
    root_cause_status: str = "unconfirmed"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _deepest(exc: BaseException) -> BaseException:
    seen: set[int] = set()
    cur: BaseException = exc
    while id(cur) not in seen:
        seen.add(id(cur))
        nxt = cur.__cause__ or cur.__context__
        if not isinstance(nxt, BaseException):
            break
        cur = nxt
    return cur


def _location(exc: BaseException) -> str:
    tb = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    if not tb:
        return ""
    frame = tb[-1]
    return f"{frame.filename}:{frame.lineno} in {frame.name}"


def _safe_text(value: Any, limit: int = 3000) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]


def classify_exception(exc: BaseException, *, service: str = "", operation: str = "") -> ErrorDiagnostic:
    """Classify only what the exception supports; do not invent a root cause.

    `root_cause_status` deliberately remains `unconfirmed` unless a dependency
    returns an explicit machine-readable reason. `resolution_hint` is a
    verification/remediation step, not a claim about the unseen root cause.
    """
    top = exc
    cause = _deepest(exc)
    observed = _safe_text(top)
    location = _location(top) or _location(cause)
    error_class = type(cause).__name__
    category = "UNCLASSIFIED"
    code = ""
    retryable = False
    status_code = getattr(top, "status_code", None)
    request_id = str(getattr(top, "request_id", "") or "")[:200]
    svc = service or str(getattr(top, "service", "") or "")
    op = operation or str(getattr(top, "operation", "") or "")
    resolution = "Inspect the captured exception, component, operation, and traceback before retrying."
    root_status = "unconfirmed"

    # Pipeline/domain exceptions may carry explicit observed diagnostics. Honor
    # those fields before generic classification; they are produced from actual
    # extractor/dependency results rather than inferred root causes.
    custom_category = str(getattr(top, "error_category", "") or "")
    if custom_category:
        return ErrorDiagnostic(
            error_class=type(top).__name__,
            error_category=custom_category,
            error_code=str(getattr(top, "error_code", "") or ""),
            observed_error=_safe_text(getattr(top, "observed_error", None) or top),
            retryable=bool(getattr(top, "retryable", False)),
            service=service or str(getattr(top, "service", "") or ""),
            operation=operation or str(getattr(top, "operation", "") or ""),
            status_code=getattr(top, "status_code", None),
            request_id=str(getattr(top, "request_id", "") or "")[:200],
            error_location=_location(top),
            resolution_hint=str(getattr(top, "resolution_hint", "") or "Inspect the captured extraction error and source evidence before retrying."),
            root_cause_status=str(getattr(top, "root_cause_status", "unconfirmed") or "unconfirmed"),
        )

    try:
        from .azure_gateway import AzureRequestError
    except Exception:  # pragma: no cover - import safety during bootstrap
        AzureRequestError = ()  # type: ignore[assignment]

    if AzureRequestError and isinstance(top, AzureRequestError):
        category = "EXTERNAL_HTTP"
        retryable = bool(top.retryable)
        status_code = int(top.status_code)
        request_id = top.request_id
        svc = top.service
        op = top.operation
        summary = top.error_summary
        observed = _safe_text(summary)
        try:
            parsed = json.loads(summary.split("; input_items=", 1)[0])
            if isinstance(parsed, dict):
                code = _safe_text(parsed.get("code") or parsed.get("type"), 200)
                if parsed.get("message"):
                    observed = _safe_text(parsed.get("message"))
                if code:
                    root_status = "dependency_reported"
        except Exception:
            pass
        if status_code == 400:
            resolution = (
                "Permanent HTTP 400. Verify the configured endpoint/deployment, API version, and request contract "
                "against the enterprise gateway response/code. Do not retry unchanged payloads automatically."
            )
        elif status_code == 401:
            resolution = "Verify the configured API credential and enterprise gateway authorization for this deployment."
        elif status_code == 403:
            resolution = "Verify deployment access policy/authorization using the returned request ID; do not assume a credential issue without gateway evidence."
        elif status_code == 404:
            resolution = "Verify endpoint route, deployment name, and API version against the enterprise gateway configuration."
        elif status_code == 429:
            resolution = "Dependency throttled the request. Respect Retry-After/backoff and verify configured concurrency/quota."
        elif status_code and status_code >= 500:
            resolution = "Dependency returned a server error. Retry with bounded backoff; use request ID to investigate persistent failures."
        return ErrorDiagnostic(error_class, category, code, observed, retryable, svc, op, status_code, request_id, location, resolution, root_status)

    # PostgreSQL/psycopg2. Import lazily so unit tests can run without it.
    try:
        import psycopg2
        pg_operational = isinstance(cause, (psycopg2.OperationalError, psycopg2.InterfaceError))
        pg_error = isinstance(cause, psycopg2.Error)
    except Exception:
        pg_operational = False
        pg_error = False

    lower = f"{type(cause).__name__}: {cause}".casefold()
    if pg_operational or any(marker in lower for marker in (
        "ssl error: unexpected eof", "server closed the connection unexpectedly",
        "connection not open", "connection already closed", "terminating connection",
        "could not connect to server", "connection timed out", "connection reset by peer",
    )):
        category = "DB_CONNECTION_LOST"
        retryable = True
        svc = svc or "postgres"
        resolution = (
            "A PostgreSQL transport/session failure was observed. The failed pooled connection should be discarded; "
            "retry the idempotent operation with backoff. If repeated, verify DB/network/TLS/load-balancer stability and server logs."
        )
    elif pg_error:
        category = "DB_ERROR"
        svc = svc or "postgres"
        code = str(getattr(cause, "pgcode", "") or "")
        resolution = "PostgreSQL returned a database error. Use SQLSTATE/error text and server logs to correct the statement/schema/data issue before retrying."
    else:
        try:
            import requests
            if isinstance(cause, (requests.Timeout, requests.ConnectionError)):
                category = "HTTP_TRANSPORT"
                retryable = True
                resolution = "A network transport timeout/connection failure was observed. Retry with bounded backoff and investigate persistent network/dependency failures."
        except Exception:
            pass

    # Botocore errors expose machine-readable service error codes.
    try:
        from botocore.exceptions import ClientError, EndpointConnectionError, ConnectionClosedError, ReadTimeoutError
        if isinstance(cause, ClientError):
            category = "S3_CLIENT_ERROR"
            svc = svc or "s3"
            response = cause.response or {}
            error = response.get("Error", {}) if isinstance(response, dict) else {}
            meta = response.get("ResponseMetadata", {}) if isinstance(response, dict) else {}
            code = _safe_text(error.get("Code"), 200)
            observed = _safe_text(error.get("Message") or cause)
            request_id = _safe_text(meta.get("RequestId"), 200)
            retryable = code in {"RequestTimeout", "SlowDown", "InternalError", "ServiceUnavailable"}
            root_status = "dependency_reported" if code else "unconfirmed"
            resolution = (
                "Use the S3 error code/request ID to verify permissions, object existence, region/endpoint, or service health. "
                "Only transient S3 codes are retried automatically."
            )
        elif isinstance(cause, (EndpointConnectionError, ConnectionClosedError, ReadTimeoutError)):
            category = "S3_TRANSPORT"
            svc = svc or "s3"
            retryable = True
            resolution = "S3 transport failed. Retry with bounded SDK backoff; investigate persistent endpoint/network/TLS failures."
    except Exception:
        pass

    if category == "UNCLASSIFIED":
        # Known local/document problems are permanent until input/config changes.
        if isinstance(cause, FileNotFoundError):
            category = "SOURCE_NOT_FOUND"
            resolution = "Verify the resolved NAS/S3/local path and source-selection plan; the source must exist before retrying."
        elif isinstance(cause, PermissionError):
            category = "SOURCE_PERMISSION"
            resolution = "Verify runtime identity permissions for the reported source/path."
        elif isinstance(cause, ValueError):
            category = "VALIDATION_ERROR"
            resolution = "Correct the reported input/configuration validation error before retrying."
        elif isinstance(cause, KeyError):
            category = "CONFIGURATION_ERROR"
            resolution = "Add/correct the explicitly reported configuration key/section before retrying."

    return ErrorDiagnostic(error_class, category, code, observed, retryable, svc, op, status_code, request_id, location, resolution, root_status)


def traceback_text(exc: BaseException, limit: int = 12000) -> str:
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return text[-limit:]
