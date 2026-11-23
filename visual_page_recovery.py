"""Read-only verification gate for one-page recovery from a visual task.

No OCR, artificial chart values, ledger mutation, page checkpoint write,
automatic approval or full-document publication occurs in this module.

Run this against the original PDF, native extraction profile and isolated
visual ledger before permitting any future recovery writer to operate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def plan_recovery(*, pdf_path: str, native_profile_path: str,
                  visual_profile_path: str, page_number: int,
                  expected_visual_task_id: str | None = None,
                  connection_factory=None):
    from processing_profile import load_profile
    from page_stage import names, extraction_fingerprint, source_hash
    from visual_task_ledger import load_settings, validate_evidence

    import fitz

    profile = load_profile(native_profile_path)
    ledger = load_settings(visual_profile_path)
    options = profile["extraction"]
    if options["schema"] != ledger.schema:
        raise ValueError("Visual task schema must match native validation schema")
    if options["ocr_enabled"] is not False:
        raise ValueError("Recovery audit requires separate disabled OCR workload")
    if profile["embedding"]["enabled"] is not False:
        raise ValueError("Recovery audit requires disabled embeddings")
    n = names(profile)
    fingerprint = extraction_fingerprint(profile)
    path = Path(pdf_path).resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise ValueError("Expected an original PDF")
    if type(page_number) is not int or page_number <= 0:
        raise ValueError("Invalid requested page")
    before_stat = path.stat()
    with fitz.open(path) as pdf:
        if pdf.is_encrypted or page_number > pdf.page_count:
            raise ValueError("Source page invalid or encrypted")
        page_count = pdf.page_count

    doc_id = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
    source_sha = source_hash(str(path))
    if expected_visual_task_id and (
        type(expected_visual_task_id) is not str
        or len(expected_visual_task_id) != 64
        or not all(ch in "0123456789abcdef" for ch in expected_visual_task_id)
    ):
        raise ValueError("Invalid expected visual task ID")

    if connection_factory is None:
        from db import get_conn
        connection_factory = get_conn

    conn = connection_factory()
    try:
        # Read-only snapshot: task, checkpoints and source-version status must
        # all come from one consistent database state.
        conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('statement_timeout', %s, true)",
                        (str(ledger.statement_timeout_ms),))
            cur.execute(f"""
                SELECT page_number,visual_task_id,status,source_sha256,
                       extraction_hash,manifest_sha256,manifest_path
                FROM {ledger.relation}
                WHERE doc_id=%s
                ORDER BY page_number,visual_task_id
            """, (doc_id,))
            tasks = cur.fetchall()

            cur.execute(f"""
                SELECT page_number
                FROM {n["work"]}
                WHERE doc_id=%s AND source_hash=%s AND extraction_hash=%s
                ORDER BY page_number
            """, (doc_id,source_sha,fingerprint))
            checkpoints = [int(row[0]) for row in cur.fetchall()]
            cur.execute(f"""
                SELECT count(*)
                FROM {n["published"]}
                WHERE doc_id=%s
            """,(doc_id,))
            published_count = int(cur.fetchone()[0])
            cur.execute(f"""
                SELECT source_hash,extraction_hash,page_count
                FROM {n["docs"]}
                WHERE doc_id=%s
            """,(doc_id,))
            published_state = cur.fetchone()
        conn.rollback()
    finally:
        conn.close()

    existing = set(checkpoints)
    missing = [p for p in range(1,page_count+1) if p not in existing]
    if len(checkpoints) != len(existing):
        raise RuntimeError("Duplicate native checkpoints")
    if any(p < 1 or p > page_count for p in existing):
        raise RuntimeError("Out-of-range native checkpoints")
    task_matches = [
        row for row in tasks if row[0]==page_number
    ]
    other_tasks = [row for row in tasks if row[0]!=page_number]
    target_task = task_matches[0] if len(task_matches)==1 else None

    checks = {
        "exactly_one_missing_page": missing==[page_number],
        "no_prior_visible_publication": published_count==0 and published_state is None,
        "exactly_one_matching_visual_task": target_task is not None,
        "no_other_unresolved_visual_task": not other_tasks,
        "task_requires_review": bool(target_task and target_task[2]=="REVIEW_REQUIRED"),
        "task_source_matches_original": bool(target_task and target_task[3]==source_sha),
        "task_native_version_matches": bool(target_task and target_task[4]==fingerprint),
        "task_identity_matches_expected": bool(target_task and (
            not expected_visual_task_id or target_task[1]==expected_visual_task_id
        )),
    }
    manifest_check = {
        "validated": False,
        "page_matches": False,
        "task_matches": False,
        "digest_matches": False,
    }
    if (target_task is not None and checks["task_source_matches_original"]
            and checks["task_native_version_matches"]):
        # The DB path is evidence, not an instruction. Revalidate all on-disk
        # evidence against original PDF before making a recovery decision.
        evidence = validate_evidence(
            Path(target_task[6]),path,fingerprint,ledger.max_manifest_bytes
        )
        manifest_check = {
            "validated": True,
            "page_matches": evidence["page_number"]==page_number,
            "task_matches": evidence["visual_task_id"]==target_task[1],
            "digest_matches": evidence["manifest_sha256"]==target_task[5],
        }
    checks.update({
        "original_evidence_validated": manifest_check["validated"],
        "figure_page_matches": manifest_check["page_matches"],
        "figure_task_id_matches": manifest_check["task_matches"],
        "evidence_digest_matches": manifest_check["digest_matches"],
    })

    after_stat = path.stat()
    if (before_stat.st_size!=after_stat.st_size
            or before_stat.st_mtime_ns!=after_stat.st_mtime_ns
            or source_hash(str(path))!=source_sha):
        raise RuntimeError("Original PDF changed during recovery audit")

    consistent = all(checks.values())
    return {
        "native_doc_id": doc_id,
        "source_sha256": source_sha,
        "native_extraction_hash": fingerprint,
        "page_count": page_count,
        "requested_page": page_number,
        "checkpoint_count": len(checkpoints),
        "missing_checkpoint_pages": missing[:25],
        "published_page_rows": published_count,
        "visual_tasks_found": len(tasks),
        "visual_task_id": target_task[1] if target_task else None,
        "visual_status": target_task[2] if target_task else None,
        "checks": checks,
        "consistency": "PASS" if consistent else "REVIEW_REQUIRED",
        "figure_interpretation": "NOT_VERIFIED",
        "page_publication": "BLOCKED_PENDING_EXPLICIT_REVIEW",
        "database_writes": False,
        "external_api_calls": False,
        "note": "Consistency PASS does NOT authorize page recovery or chart approval.",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("inspect",))
    parser.add_argument("--file",required=True)
    parser.add_argument("--native-profile",required=True)
    parser.add_argument("--ledger-profile",required=True)
    parser.add_argument("--page",type=int,required=True)
    parser.add_argument("--visual-task-id")
    args=parser.parse_args(argv)
    result=plan_recovery(
        pdf_path=args.file,
        native_profile_path=args.native_profile,
        visual_profile_path=args.ledger_profile,
        page_number=args.page,
        expected_visual_task_id=args.visual_task_id,
    )
    print(json.dumps(result,indent=2))
    return 0 if result["consistency"]=="PASS" else 2


if __name__=="__main__":
    raise SystemExit(main())
