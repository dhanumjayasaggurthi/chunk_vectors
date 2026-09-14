from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional, Protocol


class RimDocsProvider(Protocol):
    def get(self, canonical_path: str) -> Optional[dict[str, Any]]: ...
    def get_with_version(self, canonical_path: str) -> tuple[Optional[dict[str, Any]], str]: ...


class EmptyRimDocsProvider:
    """No provider configured. Return None so metadata can be skipped by policy."""

    def get(self, canonical_path):
        return None

    def get_with_version(self, canonical_path):
        return None, "none"


class JSONLRimDocsProvider:
    def __init__(self, path, cache_dir=".mirai_scratch"):
        self.path = Path(path)
        self._lock = threading.RLock()
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        st = self.path.stat()
        fingerprint = hashlib.sha256(
            f"{self.path.resolve()}:{st.st_size}:{st.st_mtime_ns}".encode()
        ).hexdigest()[:20]
        self.db_path = cache_dir / f"rimdocs-{fingerprint}.sqlite3"
        create = not self.db_path.exists()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS metadata(canonical_path TEXT PRIMARY KEY,payload TEXT NOT NULL)"
        )
        if create:
            rows = []
            with self.path.open("r", encoding="utf-8") as f:
                for line_number, line in enumerate(f, 1):
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    canonical = row.get("canonical_path")
                    metadata = row.get("metadata")
                    if not isinstance(canonical, str) or not isinstance(metadata, dict):
                        raise ValueError(
                            f"Invalid RimDocs JSONL row {line_number}: expected canonical_path + metadata object"
                        )
                    rows.append((canonical, json.dumps(metadata, ensure_ascii=False, sort_keys=True)))
                    if len(rows) >= 1000:
                        self.conn.executemany(
                            "INSERT OR REPLACE INTO metadata VALUES (?,?)", rows
                        )
                        self.conn.commit()
                        rows = []
                if rows:
                    self.conn.executemany(
                        "INSERT OR REPLACE INTO metadata VALUES (?,?)", rows
                    )
                    self.conn.commit()

    def get_with_version(self, canonical_path):
        """Return authoritative metadata plus a stable per-document content version.

        Missing rows return ``(None, "none")``. An explicitly present empty metadata
        object returns ``({}, <hash>)`` so required mode can distinguish "row exists
        but no fields are populated" from "no authoritative row exists".
        """
        with self._lock:
            row = self.conn.execute(
                "SELECT payload FROM metadata WHERE canonical_path=?", (canonical_path,)
            ).fetchone()
        if not row:
            return None, "none"
        payload = row[0]
        version = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return json.loads(payload), version

    def get(self, canonical_path):
        metadata, _ = self.get_with_version(canonical_path)
        return metadata

    def close(self):
        with self._lock:
            self.conn.close()
