"""Isolated, durable PostgreSQL visual-review task ledger.

Created figure evidence is a candidate, NOT extracted clinical chart values.
This ledger never publishes pages, approves figures, invokes OCR/LLMs,
modifies extraction profiles or changes native page checkpoints.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from profile_store import _safe_identifier

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_REF = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class LedgerSettings:
    schema: str
    table: str
    max_manifest_bytes: int = 65536
    max_claim_batch: int = 16
    lease_seconds: int = 600
    max_claims: int = 5
    statement_timeout_ms: int = 15000
    lock_timeout_ms: int = 3000

    def validate(self):
        _safe_identifier(self.schema)
        _safe_identifier(self.table)
        if len(self.table) > 42:  # reserve room for PostgreSQL's 63-byte index names
            raise ValueError("Visual table identifier exceeds 42 characters")
        for field, low, high in (
            ("max_manifest_bytes", 1024, 524288),
            ("max_claim_batch", 1, 100),
            ("lease_seconds", 30, 3600),
            ("max_claims", 1, 100),
            ("statement_timeout_ms", 1000, 120000),
            ("lock_timeout_ms", 100, 30000),
        ):
            v = getattr(self, field)
            if type(v) is not int or not low <= v <= high:
                raise ValueError(f"Invalid ledger option {field}")
        return self

    @property
    def relation(self):
        return f"{_safe_identifier(self.schema)}.{_safe_identifier(self.table)}"

    @property
    def queue_index(self):
        return _safe_identifier(self.table + "_queue_idx")


def load_settings(path: str) -> LedgerSettings:
    import yaml

    file = Path(path).resolve(strict=True)
    if file.stat().st_size > 16384:
        raise ValueError("Visual ledger profile too large")
    data = yaml.safe_load(file.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("Visual ledger profile requires version: 1")
    unknown = set(data) - set(LedgerSettings.__dataclass_fields__) - {"version"}
    if unknown:
        raise ValueError("Unknown visual ledger options: " + ", ".join(sorted(unknown)))
    return LedgerSettings(**{k: v for k, v in data.items() if k != "version"}).validate()


def _hex64(text, name):
    if not isinstance(text, str) or not _SHA256.fullmatch(text):
        raise ValueError(f"Invalid {name}: expected lowercase SHA-256")
    return text


def _safe_byte_stream(path: Path, cap: int | None = None):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(part)
            if cap is not None and size > cap:
                raise ValueError("Evidence exceeds configured byte limit")
            digest.update(part)
    return digest.hexdigest(), size


def validate_evidence(manifest_path: Path, pdf_path: Path, extraction_hash: str,
                      max_manifest_bytes: int):
    """Verify source PDF, provenance, exact rendered PNG and immutable names.

    Does not trust the manifest to establish approval or clinical meaning.
    """
    from figure_recovery import ALGORITHM_VERSION

    manifest_path = Path(manifest_path).resolve(strict=True)
    pdf_path = Path(pdf_path).resolve(strict=True)
    if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
        raise ValueError("Expected a local PDF")
    if not isinstance(extraction_hash, str) or not _SHA256.fullmatch(extraction_hash):
        raise ValueError("Invalid current native extraction fingerprint")
    manifest_sha, bytes_count = _safe_byte_stream(manifest_path, max_manifest_bytes)
    raw = manifest_path.read_bytes()
    if len(raw) != bytes_count:
        raise RuntimeError("Evidence manifest changed while reading")
    if hashlib.sha256(raw).hexdigest() != manifest_sha:
        raise RuntimeError("Evidence manifest bytes changed")
    data = json.loads(raw)
    required = {
        "schema_version", "figure_id", "visual_task_id",
        "native_doc_id", "native_extraction_hash", "source_pdf_sha256",
        "page_number", "algorithm", "fragment_count", "source_fragments",
        "composite_sha256", "composite_file", "review_status",
        "interpretation_status", "settings",
    }
    if not isinstance(data, dict) or not required.issubset(data):
        raise ValueError("Incomplete visual evidence manifest")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("Unsupported visual evidence version")
    for key in ("figure_id", "visual_task_id", "native_doc_id", "source_pdf_sha256", "composite_sha256"):
        _hex64(data[key], key)
    if data["native_extraction_hash"] != extraction_hash:
        raise ValueError("Native extraction fingerprint mismatch")
    if data["native_doc_id"] != hashlib.sha256(str(pdf_path).encode("utf-8")).hexdigest():
        raise ValueError("Source document path identity mismatch")
    pdf_digest, _ = _safe_byte_stream(pdf_path)
    if data["source_pdf_sha256"] != pdf_digest:
        raise ValueError("Original source PDF hash mismatch")
    if data["algorithm"] != ALGORITHM_VERSION:
        raise ValueError("Figure recovery algorithm version mismatch")
    if data["review_status"] != "VISUAL_REVIEW_REQUIRED" or data["interpretation_status"] != "NOT_PERFORMED":
        raise ValueError("Only unreviewed, non-interpreted evidence may be registered")
    if type(data["page_number"]) is not int or data["page_number"] < 1:
        raise ValueError("Invalid original page number")
    fragments = data["source_fragments"]
    if (not isinstance(fragments, list) or not 2 <= len(fragments) <= 64
            or data["fragment_count"] != len(fragments)):
        raise ValueError("Invalid figure fragment evidence")
    if any(type(f.get("xref")) is not int or f["xref"] <= 0 or
           not isinstance(f.get("bbox"), list) or len(f["bbox"]) != 4 or
           not _SHA256.fullmatch(str(f.get("original_sha256", ""))) for f in fragments):
        raise ValueError("Invalid fragment provenance")
    if len({f["xref"] for f in fragments}) != len(fragments):
        raise ValueError("Duplicate original image XREF in figure evidence")
    from figure_recovery import FigureSettings, _canonical_bytes, _image_info
    try:
        settings = FigureSettings(**data["settings"]).validate()
    except (TypeError, KeyError, ValueError) as exc:
        raise ValueError("Invalid figure rendering settings") from exc

    # Verify the figure and independently versioned task IDs, preventing
    # arbitrary JSON from being inserted under someone else's identity.
    identity = {
        "source_pdf_sha256": pdf_digest,
        "source_page": data["page_number"],
        "algorithm": data["algorithm"],
        "fragment_sources": [[f["xref"], f["original_sha256"], f["bbox"]] for f in fragments],
    }
    figure_id = hashlib.sha256(_canonical_bytes(identity)).hexdigest()
    if figure_id != data["figure_id"]:
        raise ValueError("Figure identity invalid")
    task_id = hashlib.sha256(_canonical_bytes({
        "figure_id": figure_id,
        "native_extraction_hash": extraction_hash,
        "render_settings": data["settings"],
    })).hexdigest()
    if task_id != data["visual_task_id"]:
        raise ValueError("Visual task identity invalid")

    if manifest_path.name != task_id + ".json" or data["composite_file"] != task_id + ".png":
        raise ValueError("Unsafe or unexpected evidence file name")
    composite_path = manifest_path.with_name(data["composite_file"])
    composite_digest, composite_bytes = _safe_byte_stream(composite_path, settings.max_png_bytes)
    if composite_digest != data["composite_sha256"]:
        raise ValueError("Rendered image checksum mismatch")
    with composite_path.open("rb") as stream:
        if stream.read(8) != b"\x89PNG\r\n\x1a\n":
            raise ValueError("Composite is not a PNG")

    # Only inspect XREFs for the one source page, not the whole PDF.
    import fitz
    with fitz.open(pdf_path) as pdf:
        if pdf.is_encrypted or data["page_number"] > pdf.page_count:
            raise ValueError("Source page invalid or encrypted")
        page = pdf[data["page_number"] - 1]
        infos = {f["xref"]: f for f in _image_info(page, settings)}
        for fragment in fragments:
            source = infos.get(fragment["xref"])
            if not source:
                raise ValueError("Fragment missing from original PDF page")
            if any(abs(a - b) > 0.01 for a, b in zip(source["bbox"], fragment["bbox"])):
                raise ValueError("Source image geometry mismatch")
            source_bytes = pdf.extract_image(fragment["xref"])["image"]
            if hashlib.sha256(source_bytes).hexdigest() != fragment["original_sha256"]:
                raise ValueError("Original image checksum mismatch")

    return {
        "visual_task_id": task_id,
        "doc_id": data["native_doc_id"],
        "source_sha256": pdf_digest,
        "extraction_hash": extraction_hash,
        "page_number": data["page_number"],
        "figure_id": figure_id,
        "manifest_sha256": manifest_sha,
        "composite_sha256": composite_digest,
        "manifest": data,
        "manifest_path": str(manifest_path),
        "composite_bytes": composite_bytes,
    }


def _configure_transaction(cur, settings):
    cur.execute("SELECT set_config('statement_timeout', %s, true)",
                (str(settings.statement_timeout_ms),))
    cur.execute("SELECT set_config('lock_timeout', %s, true)",
                (str(settings.lock_timeout_ms),))


def init(settings: LedgerSettings, *, apply=False):
    settings.validate()
    if not apply:
        return {"writes": False, "destination": settings.relation,
                "queue_index": settings.queue_index}
    from db import get_conn

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            _configure_transaction(cur, settings)
            cur.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (settings.schema,))
            if cur.fetchone() is None:
                raise ValueError("Validation schema does not exist: create it separately")
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {settings.relation} (
                    visual_task_id text PRIMARY KEY,
                    doc_id text NOT NULL,
                    source_sha256 text NOT NULL,
                    extraction_hash text NOT NULL,
                    page_number integer NOT NULL CHECK (page_number >= 1),
                    figure_id text NOT NULL,
                    manifest_sha256 text NOT NULL,
                    composite_sha256 text NOT NULL,
                    manifest jsonb NOT NULL,
                    manifest_path text NOT NULL,
                    status text NOT NULL DEFAULT 'REVIEW_REQUIRED'
                        CHECK (status IN ('REVIEW_REQUIRED','LEASED')),
                    lease_owner text,
                    lease_token text,
                    lease_expires_at timestamptz,
                    claim_attempts integer NOT NULL DEFAULT 0,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now()
                )
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS {settings.queue_index}
                ON {settings.relation} (status, lease_expires_at, created_at)
            """)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"created_or_verified": settings.relation,
            "source_page_tables_unchanged": True}


def register(settings: LedgerSettings, *, manifest_path: Path, pdf_path: Path,
             extraction_hash: str, apply=False):
    settings.validate()
    item = validate_evidence(manifest_path, pdf_path, extraction_hash,
                             settings.max_manifest_bytes)
    if not apply:
        return {"writes": False, "validated": True,
                "visual_task_id": item["visual_task_id"],
                "doc_id": item["doc_id"], "page_number": item["page_number"],
                "status": "REVIEW_REQUIRED",
                "destination": settings.relation}
    from db import get_conn

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            _configure_transaction(cur, settings)
            cur.execute(f"""
                INSERT INTO {settings.relation} (
                    visual_task_id,doc_id,source_sha256,extraction_hash,
                    page_number,figure_id,manifest_sha256,composite_sha256,
                    manifest,manifest_path,status
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,'REVIEW_REQUIRED')
                ON CONFLICT (visual_task_id) DO NOTHING
                RETURNING visual_task_id
            """, (item["visual_task_id"], item["doc_id"], item["source_sha256"],
                  item["extraction_hash"], item["page_number"], item["figure_id"],
                  item["manifest_sha256"], item["composite_sha256"],
                  json.dumps(item["manifest"], ensure_ascii=False, allow_nan=False),
                  item["manifest_path"]))
            inserted = cur.fetchone() is not None
            cur.execute(f"""
                SELECT doc_id,source_sha256,extraction_hash,page_number,
                       figure_id,manifest_sha256,composite_sha256,status
                FROM {settings.relation} WHERE visual_task_id=%s FOR UPDATE
            """, (item["visual_task_id"],))
            row = cur.fetchone()
            if row[:7] != (
                item["doc_id"], item["source_sha256"], item["extraction_hash"],
                item["page_number"], item["figure_id"],
                item["manifest_sha256"], item["composite_sha256"],
            ):
                raise ValueError("VISUAL_EVIDENCE_CONFLICT: task ID already exists with different evidence")
        conn.commit()
        return {"visual_task_id": item["visual_task_id"],
                "page_number": item["page_number"],
                "status": row[7],
                "inserted": inserted,
                "native_pages_modified": 0}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def status(settings: LedgerSettings, *, doc_id: str):
    settings.validate()
    _hex64(doc_id, "doc_id")
    from db import get_conn

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            _configure_transaction(cur, settings)
            cur.execute(f"""
                SELECT page_number,visual_task_id,status,claim_attempts,
                       lease_expires_at,manifest_sha256
                FROM {settings.relation}
                WHERE doc_id=%s ORDER BY page_number,visual_task_id
            """, (doc_id,))
            results = cur.fetchall()
            conn.rollback()
        return [{"page_number": r[0], "visual_task_id": r[1],
                 "status": r[2], "claim_attempts": r[3],
                 "lease_expires_at": str(r[4]) if r[4] else None,
                 "manifest_sha256": r[5]} for r in results]
    finally:
        conn.close()


def claim(settings: LedgerSettings, *, worker_id: str, batch_size: int,
          apply=False):
    """Atomic SKIP LOCKED claim. No figure can be auto-approved here."""
    settings.validate()
    if not isinstance(worker_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", worker_id):
        raise ValueError("Invalid bounded worker identifier")
    if type(batch_size) is not int or not 1 <= batch_size <= settings.max_claim_batch:
        raise ValueError("Invalid worker claim batch")
    if not apply:
        return {"writes": False, "claim_limit": batch_size,
                "lease_seconds": settings.lease_seconds,
                "destination": settings.relation}
    from db import get_conn

    token = str(uuid.uuid4())
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            _configure_transaction(cur, settings)
            cur.execute(f"""
                WITH candidate AS (
                    SELECT visual_task_id
                    FROM {settings.relation}
                    WHERE (
                        status='REVIEW_REQUIRED'
                        OR (status='LEASED' AND lease_expires_at < now())
                    ) AND claim_attempts < %s
                    ORDER BY created_at, visual_task_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE {settings.relation} t SET
                    status='LEASED',
                    lease_owner=%s,
                    lease_token=%s,
                    lease_expires_at=now() + make_interval(secs => %s),
                    claim_attempts=t.claim_attempts+1,
                    updated_at=now()
                FROM candidate c WHERE t.visual_task_id=c.visual_task_id
                RETURNING t.visual_task_id,t.doc_id,t.page_number,
                          t.manifest_sha256,t.lease_expires_at
            """, (settings.max_claims, batch_size, worker_id, token,
                  settings.lease_seconds))
            rows = cur.fetchall()
        conn.commit()
        return {"claimed": len(rows), "worker_id": worker_id,
                "lease_token": token if rows else None,
                "tasks": [{"visual_task_id": r[0], "doc_id": r[1],
                           "page_number": r[2], "manifest_sha256": r[3],
                           "lease_expires_at": str(r[4])} for r in rows],
                "note": "Leased for review only; no page publication or auto-approval"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def release(settings: LedgerSettings, *, task_id: str, worker_id: str,
            lease_token: str, apply=False):
    """Return a leased candidate for review without changing extracted data."""
    settings.validate()
    _hex64(task_id, "visual_task_id")
    if not isinstance(worker_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", worker_id):
        raise ValueError("Invalid worker identifier")
    if not isinstance(lease_token, str) or not re.fullmatch(
        r"[a-f0-9]{8}-[a-f0-9-]{27,36}", lease_token
    ):
        raise ValueError("Invalid lease token")
    if not apply:
        return {"writes": False, "task": task_id, "status": "REVIEW_REQUIRED"}
    from db import get_conn
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            _configure_transaction(cur, settings)
            cur.execute(f"""
                UPDATE {settings.relation} SET
                    status='REVIEW_REQUIRED', lease_owner=NULL,
                    lease_token=NULL, lease_expires_at=NULL, updated_at=now()
                WHERE visual_task_id=%s AND status='LEASED'
                  AND lease_owner=%s AND lease_token=%s
                  AND lease_expires_at >= now()
                RETURNING visual_task_id
            """, (task_id, worker_id, lease_token))
            success = cur.fetchone() is not None
            if not success:
                raise ValueError("Lease missing, expired or owned by another worker")
        conn.commit()
        return {"released": True, "status": "REVIEW_REQUIRED",
                "native_pages_modified": 0}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("init", "register", "status", "claim", "release"):
        cmd = sub.add_parser(command)
        cmd.add_argument("--ledger-profile", required=True)
        if command in ("init", "register", "claim", "release"):
            cmd.add_argument("--apply", action="store_true")
        if command == "register":
            cmd.add_argument("--manifest", required=True)
            cmd.add_argument("--file", required=True)
            cmd.add_argument("--native-profile", required=True)
        if command == "status":
            cmd.add_argument("--doc-id", required=True)
        if command == "claim":
            cmd.add_argument("--worker-id", required=True)
            cmd.add_argument("--batch-size", type=int, default=1)
        if command == "release":
            cmd.add_argument("--visual-task-id", required=True)
            cmd.add_argument("--worker-id", required=True)
            cmd.add_argument("--lease-token", required=True)
    args = parser.parse_args(argv)

    cfg = load_settings(args.ledger_profile)
    if args.command == "init":
        result = init(cfg, apply=args.apply)
    elif args.command == "register":
        from processing_profile import load_profile
        from page_stage import extraction_fingerprint
        source_profile = load_profile(args.native_profile)
        result = register(
            cfg, manifest_path=Path(args.manifest),
            pdf_path=Path(args.file),
            extraction_hash=extraction_fingerprint(source_profile),
            apply=args.apply,
        )
    elif args.command == "status":
        result = {"tasks": status(cfg, doc_id=args.doc_id), "writes": False}
    elif args.command == "claim":
        result = claim(cfg, worker_id=args.worker_id,
                       batch_size=args.batch_size, apply=args.apply)
    else:
        result = release(cfg, task_id=args.visual_task_id,
                         worker_id=args.worker_id,
                         lease_token=args.lease_token, apply=args.apply)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
