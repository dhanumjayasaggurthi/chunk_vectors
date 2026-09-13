"""
logger.py
=========
Centralised logging for the EPOD ingestion pipeline.

  - Rotating file log  → logs/epod_pipeline.log  (10 MB × 5 files)
  - Console output     → colour-coded by level
  - Per-document child loggers  → easy to filter by file name
  - Machine-readable JSON lines → logs/epod_pipeline.jsonl  (for monitoring)

Usage:
    from logger import get_logger
    log = get_logger("epod.crawler")
    log.info("Starting crawl")
    log.error("Failed", extra={"file": "some.pdf", "error": str(e)})
"""

import json
import logging
import logging.handlers
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from config import LOG_DIR, LOG_FILE, LOG_MAX_BYTES, LOG_BACKUP_COUNT

# ─────────────────────────────────────────────────────────────────────────────
# Ensure log directory exists
# ─────────────────────────────────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_JSONL_FILE = LOG_DIR / "epod_pipeline.jsonl"


# ─────────────────────────────────────────────────────────────────────────────
# Colour codes for console  (disabled on Windows if not supported)
# ─────────────────────────────────────────────────────────────────────────────
_COLOURS = {
    "DEBUG":    "\033[36m",   # cyan
    "INFO":     "\033[32m",   # green
    "WARNING":  "\033[33m",   # yellow
    "ERROR":    "\033[31m",   # red
    "CRITICAL": "\033[35m",   # magenta
    "RESET":    "\033[0m",
}

_USE_COLOUR = sys.platform != "win32" or os.environ.get("FORCE_COLOR")


class _ColourFormatter(logging.Formatter):
    """Console formatter — adds colour + compact timestamp."""

    FMT = "{colour}[{level}]{reset} {time} | {name} | {message}"

    def format(self, record: logging.LogRecord) -> str:
        colour = _COLOURS.get(record.levelname, "") if _USE_COLOUR else ""
        reset  = _COLOURS["RESET"] if _USE_COLOUR else ""
        time   = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        msg    = record.getMessage()

        # Append exception info if present
        if record.exc_info:
            msg += "\n" + "".join(traceback.format_exception(*record.exc_info)).rstrip()

        return self.FMT.format(
            colour=colour,
            level=record.levelname[0],     # D / I / W / E / C
            reset=reset,
            time=time,
            name=record.name,
            message=msg,
        )


class _FileFormatter(logging.Formatter):
    """Plain text formatter for the rotating file log."""

    def format(self, record: logging.LogRecord) -> str:
        ts  = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + "".join(traceback.format_exception(*record.exc_info)).rstrip()
        return f"{ts} | {record.levelname:<8} | {record.name} | {msg}"


class _JsonlHandler(logging.Handler):
    """
    Appends one JSON object per log line to epod_pipeline.jsonl.
    Useful for parsing failures with jq or sending to a monitoring system.
    """

    def __init__(self, path: Path):
        super().__init__()
        self._path = path

    def emit(self, record: logging.LogRecord):
        try:
            entry = {
                "ts":      datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                "level":   record.levelname,
                "logger":  record.name,
                "message": record.getMessage(),
            }
            # Attach any extra fields passed via extra={}
            for key in ("file", "doc_id", "chunk", "page", "error", "elapsed"):
                val = getattr(record, key, None)
                if val is not None:
                    entry[key] = val

            if record.exc_info:
                entry["traceback"] = "".join(
                    traceback.format_exception(*record.exc_info)
                ).rstrip()

            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            self.handleError(record)


# ─────────────────────────────────────────────────────────────────────────────
# Root pipeline logger — built once
# ─────────────────────────────────────────────────────────────────────────────
_initialised = False


def _setup_root_logger():
    global _initialised
    if _initialised:
        return
    _initialised = True

    root = logging.getLogger("epod")
    root.setLevel(logging.DEBUG)          # handlers filter their own levels

    # 1. Rotating plain-text file  (DEBUG+)
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(_FileFormatter())
    root.addHandler(fh)

    # 2. JSONL structured file  (WARNING+  — keeps it small)
    jh = _JsonlHandler(LOG_JSONL_FILE)
    jh.setLevel(logging.WARNING)
    root.addHandler(jh)

    # 3. Console  (INFO+)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(_ColourFormatter())
    root.addHandler(ch)

    root.propagate = False
    root.info(f"Logger initialised → {LOG_FILE}")


def get_logger(name: str) -> logging.Logger:
    """
    Return a child logger under the 'epod' namespace.

    Examples:
        get_logger("epod.crawler")
        get_logger("epod.pdf_processor")
        get_logger("epod.chunker")
    """
    _setup_root_logger()
    return logging.getLogger(name)


# ─────────────────────────────────────────────────────────────────────────────
# Document-level context helper
# ─────────────────────────────────────────────────────────────────────────────

class DocLogger:
    """
    Thin wrapper that pre-fills file/doc_id on every log call.
    Avoids repeating extra={} everywhere in the pipeline.

    Usage:
        dlog = DocLogger("some_file.pdf", "abc123")
        dlog.info("Processing page 5")
        dlog.error("OCR failed", exc_info=True)
    """

    def __init__(self, file_name: str, doc_id: str, base_logger: str = "epod.doc"):
        _setup_root_logger()
        self._log   = logging.getLogger(base_logger)
        self._extra = {"file": file_name, "doc_id": doc_id[:12]}

    def _x(self, extra: dict = None) -> dict:
        if extra:
            return {**self._extra, **extra}
        return self._extra

    def debug(self, msg, **kw):
        self._log.debug(msg, extra=self._x(kw.pop("extra", None)), **kw)

    def info(self, msg, **kw):
        self._log.info(msg, extra=self._x(kw.pop("extra", None)), **kw)

    def warning(self, msg, **kw):
        self._log.warning(msg, extra=self._x(kw.pop("extra", None)), **kw)

    def error(self, msg, **kw):
        self._log.error(msg, extra=self._x(kw.pop("extra", None)), **kw)

    def critical(self, msg, **kw):
        self._log.critical(msg, extra=self._x(kw.pop("extra", None)), **kw)


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline run summary  (printed + logged at end of main.py)
# ─────────────────────────────────────────────────────────────────────────────

class RunSummary:
    """
    Accumulates per-run counts and prints a clean summary table at the end.

    Usage:
        summary = RunSummary()
        summary.add("SUCCESS", "file.pdf")
        summary.add("SKIPPED", "other.pdf")
        summary.add("ERROR",   "bad.pdf", error="timeout")
        summary.print()
    """

    def __init__(self):
        self._rows: list[dict] = []
        self.started_at = datetime.now()

    def add(self, status: str, file_name: str, error: str = None,
            chunks: int = 0, pages: int = 0, elapsed: float = 0.0):
        self._rows.append({
            "status":    status,
            "file_name": file_name,
            "error":     error,
            "chunks":    chunks,
            "pages":     pages,
            "elapsed":   round(elapsed, 1),
        })

    def counts(self) -> dict:
        from collections import Counter
        return dict(Counter(r["status"] for r in self._rows))

    def print(self):
        log = get_logger("epod.summary")
        elapsed = (datetime.now() - self.started_at).total_seconds()
        counts  = self.counts()
        total   = len(self._rows)

        sep = "─" * 68
        log.info(sep)
        log.info("  EPOD PIPELINE RUN SUMMARY")
        log.info(sep)
        log.info(f"  Total files processed : {total}")
        for status, count in sorted(counts.items()):
            pct = count * 100 / total if total else 0
            log.info(f"  {status:<12}            : {count:>5}  ({pct:.1f}%)")
        log.info(f"  Total elapsed         : {elapsed:.1f}s")
        log.info(sep)

        # List errors with messages
        errors = [r for r in self._rows if r["status"] == "ERROR"]
        if errors:
            log.warning(f"  ⚠  {len(errors)} file(s) failed:")
            for e in errors:
                log.warning(f"     • {e['file_name']}: {e['error']}")
            log.warning(sep)

        # Write full summary to JSONL
        summary_entry = {
            "ts":      datetime.now(tz=timezone.utc).isoformat(),
            "level":   "INFO",
            "logger":  "epod.summary",
            "message": "run_complete",
            "counts":  counts,
            "elapsed": elapsed,
            "errors":  [{"file": e["file_name"], "error": e["error"]} for e in errors],
        }
        try:
            with open(LOG_JSONL_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(summary_entry, ensure_ascii=False) + "\n")
        except Exception:
            pass
