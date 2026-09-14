from __future__ import annotations

import atexit
import json
import logging
import logging.handlers
import queue
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[-_ ]?key\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(?i)(password\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(?i)(secret(?:_access_key)?\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(?i)(session_token\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(?i)(authorization\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+\-/]+=*"),
]


def _redact(value: str) -> str:
    out = str(value)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(r"\1<redacted>", out)
    return out


EXTRA_FIELDS = (
    "run_id", "doc_id", "generation_id", "logical_object_id", "canonical_path",
    "stage", "page", "pages_total", "chunk_id", "chunks", "attempt", "max_attempts",
    "elapsed_ms", "error_class", "error_category", "error_code", "error_detail",
    "error_location", "resolution_hint", "root_cause_status", "retryable", "service",
    "operation", "status_code", "request_id", "source_type", "selected_format",
    "selection_reason", "selected_count", "issue_count", "requested_limit", "status",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _redact(record.getMessage()),
        }
        for key in EXTRA_FIELDS:
            value = getattr(record, key, None)
            if value is not None and value != "":
                payload[key] = value
        if record.exc_info:
            payload["exception"] = _redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


class HumanFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[36m", "INFO": "\033[32m", "WARNING": "\033[33m",
        "ERROR": "\033[31m", "CRITICAL": "\033[35m", "RESET": "\033[0m",
    }

    def __init__(self, *, color: bool = False):
        super().__init__()
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
        level = record.levelname
        prefix = ""
        reset = ""
        if self.color:
            prefix = self.COLORS.get(level, "")
            reset = self.COLORS["RESET"] if prefix else ""
        context = []
        for key, label in (
            ("run_id", "run"), ("logical_object_id", "object"), ("doc_id", "doc"),
            ("stage", "stage"), ("page", "page"), ("attempt", "attempt"),
            ("service", "service"), ("operation", "op"), ("status_code", "http"),
            ("request_id", "request"), ("error_category", "category"),
        ):
            value = getattr(record, key, None)
            if value not in (None, ""):
                text = str(value)
                if key in {"run_id", "doc_id"} and len(text) > 16:
                    text = text[:16]
                context.append(f"{label}={text}")
        message = _redact(record.getMessage())
        line = f"{prefix}{ts} | {level:<8} | {record.name}"
        if context:
            line += " | " + " ".join(context)
        line += f" | {message}{reset}"
        if record.exc_info:
            line += "\n" + _redact(self.formatException(record.exc_info))
        return line


class BoundedQueueHandler(logging.handlers.QueueHandler):
    """Bound logging memory. Never silently lose ERROR/CRITICAL diagnostics."""

    def __init__(self, q: queue.Queue, emergency_path: Path):
        super().__init__(q)
        self.emergency_path = emergency_path
        self._emergency_lock = threading.Lock()

    def _emergency(self, record: logging.LogRecord, reason: str) -> None:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _redact(record.getMessage()),
            "logging_failure": reason,
        }
        for key in EXTRA_FIELDS:
            value = getattr(record, key, None)
            if value not in (None, ""):
                payload[key] = value
        if record.exc_info:
            try:
                payload["exception"] = _redact(logging.Formatter().formatException(record.exc_info))
            except Exception:
                pass
        text = json.dumps(payload, ensure_ascii=False, default=str)
        try:
            with self._emergency_lock:
                self.emergency_path.parent.mkdir(parents=True, exist_ok=True)
                with self.emergency_path.open("a", encoding="utf-8") as f:
                    f.write(text + "\n")
        except Exception:
            pass
        try:
            sys.stderr.write(text + "\n")
        except Exception:
            pass

    def enqueue(self, record):
        try:
            self.queue.put(record, block=True, timeout=0.25)
        except queue.Full:
            if record.levelno >= logging.ERROR:
                self._emergency(record, "log_queue_full")
            else:
                warning = logging.LogRecord(
                    "mir_ai.logging", logging.WARNING, __file__, 0,
                    "log queue full; non-error record dropped", (), None
                )
                self._emergency(warning, "log_queue_full")


_listener: logging.handlers.QueueListener | None = None


def shutdown_logging() -> None:
    global _listener
    if _listener is not None:
        try:
            _listener.stop()
        finally:
            _listener = None


atexit.register(shutdown_logging)


def configure_logging(
    log_dir: str | Path = "logs",
    level: str = "INFO",
    *,
    max_bytes: int = 50 * 1024 * 1024,
    backup_count: int = 10,
    queue_size: int = 10000,
    console: bool = True,
    console_json: bool = False,
) -> None:
    global _listener
    if _listener is not None:
        return
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    q: queue.Queue = queue.Queue(maxsize=max(100, int(queue_size)))
    handler = BoundedQueueHandler(q, log_dir / "mir_ai_emergency.jsonl")
    root = logging.getLogger("mir_ai")
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    root.handlers[:] = [handler]
    root.propagate = False

    handlers: list[logging.Handler] = []
    json_file = logging.handlers.RotatingFileHandler(
        log_dir / "mir_ai.jsonl", maxBytes=max(1024 * 1024, int(max_bytes)),
        backupCount=max(1, int(backup_count)), encoding="utf-8",
    )
    json_file.setFormatter(JsonFormatter())
    handlers.append(json_file)

    text_file = logging.handlers.RotatingFileHandler(
        log_dir / "mir_ai.log", maxBytes=max(1024 * 1024, int(max_bytes)),
        backupCount=max(1, int(backup_count)), encoding="utf-8",
    )
    text_file.setFormatter(HumanFormatter(color=False))
    handlers.append(text_file)

    if console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(JsonFormatter() if console_json else HumanFormatter(color=sys.stdout.isatty()))
        handlers.append(ch)

    _listener = logging.handlers.QueueListener(q, *handlers, respect_handler_level=True)
    _listener.start()


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name if name.startswith("mir_ai") else f"mir_ai.{name}")
