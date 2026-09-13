from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Optional, Protocol


class RimDocsProvider(Protocol):
    def get(self, canonical_path: str) -> Optional[dict[str, Any]]: ...


class EmptyRimDocsProvider:
    """No provider configured. Return None so existing authoritative data is preserved."""

    def get(self, canonical_path):
        return None


class JSONLRimDocsProvider:
    def __init__(self, path, cache_dir=".mirai_scratch"):
        self.path = Path(path)
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
                    rows.append((canonical, json.dumps(metadata, ensure_ascii=False)))
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

    def get(self, canonical_path):
        row = self.conn.execute(
            "SELECT payload FROM metadata WHERE canonical_path=?", (canonical_path,)
        ).fetchone()
        # A configured authoritative provider with no row for this document means
        # "no authoritative metadata available for this document", not "provider absent".
        # Keep that distinct from EmptyRimDocsProvider.get(), which deliberately returns
        # None so previously persisted authoritative values are preserved.
        return json.loads(row[0]) if row else {}

    def close(self):
        self.conn.close()
