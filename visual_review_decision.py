"""Auditable, append-only human acceptance of source-figure visual fidelity.

Acceptance is ONLY for an exact rendered figure's fidelity to the original
source PDF. It cannot approve clinical/numerical interpretation, publish page
checkpoints, or change the existing figure extraction status.

The review decision lives in its own configurable PostgreSQL table; the old
ledger constraint remains unchanged and backward compatible.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from profile_store import _safe_identifier
from visual_task_ledger import validate_evidence

HEX64 = re.compile(r"^[0-9a-f]{64}$")
REVIEWER = re.compile(r"^[A-Za-z0-9._@-]{2,80}$")
ATTESTATION = "SOURCE_FIGURE_VISUALLY_MATCHES_ORIGINAL_NO_NUMERIC_INTERPRETATION"


@dataclass(frozen=True)
class ReviewSettings:
    schema: str
    ledger_table: str
    review_table: str
    statement_timeout_ms: int = 15000
    lock_timeout_ms: int = 3000
    max_manifest_bytes: int = 65536

    def validate(self):
        for item in (self.schema,self.ledger_table,self.review_table):
            _safe_identifier(item)
            if len(item)>50:
                raise ValueError("Review SQL name too long")
        if self.review_table == self.ledger_table:
            raise ValueError("Review events must use a separate table")
        if type(self.statement_timeout_ms) is not int or not 1000 <= self.statement_timeout_ms <= 120000:
            raise ValueError("Invalid statement timeout")
        if type(self.lock_timeout_ms) is not int or not 100 <= self.lock_timeout_ms <= 30000:
            raise ValueError("Invalid lock timeout")
        if type(self.max_manifest_bytes) is not int or not 1024 <= self.max_manifest_bytes <= 524288:
            raise ValueError("Invalid review manifest size limit")
        return self

    @property
    def ledger_relation(self):
        return _safe_identifier(self.schema)+"."+_safe_identifier(self.ledger_table)

    @property
    def review_relation(self):
        return _safe_identifier(self.schema)+"."+_safe_identifier(self.review_table)


def load_settings(path):
    import yaml
    p=Path(path).resolve(strict=True)
    if p.stat().st_size>8192:
        raise ValueError("Review profile exceeds size limit")
    raw=yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw,dict) or raw.get("version")!=1:
        raise ValueError("Visual review profile requires version 1")
    unknown=set(raw)-set(ReviewSettings.__dataclass_fields__)-{"version"}
    if unknown:
        raise ValueError("Unknown reviewer settings: "+str(sorted(unknown)))
    return ReviewSettings(**{k:v for k,v in raw.items() if k!="version"}).validate()


def _set_limits(cur,settings):
    cur.execute("SELECT set_config('statement_timeout',%s,true)",
                (str(settings.statement_timeout_ms),))
    cur.execute("SELECT set_config('lock_timeout',%s,true)",
                (str(settings.lock_timeout_ms),))


def init(settings, *, apply=False):
    settings.validate()
    if not apply:
        return {"writes":False,"destination":settings.review_relation,
                "source_page_tables_unchanged":True}
    from db import get_conn
    conn=get_conn()
    try:
        with conn.cursor() as cur:
            _set_limits(cur,settings)
            cur.execute("SELECT to_regclass(%s)", (settings.ledger_relation,))
            if cur.fetchone()[0] is None:
                raise ValueError("Visual task ledger must exist first")
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {settings.review_relation} (
                    visual_task_id text PRIMARY KEY REFERENCES
                        {settings.ledger_relation}(visual_task_id),
                    doc_id text NOT NULL,
                    page_number integer NOT NULL CHECK (page_number>=1),
                    source_sha256 text NOT NULL,
                    native_extraction_hash text NOT NULL,
                    manifest_sha256 text NOT NULL,
                    composite_sha256 text NOT NULL,
                    reviewer_id text NOT NULL,
                    decision text NOT NULL DEFAULT 'VISUAL_FIDELITY_ACCEPTED'
                        CHECK (decision='VISUAL_FIDELITY_ACCEPTED'),
                    attestation_code text NOT NULL,
                    review_note text NOT NULL,
                    reviewed_at timestamptz NOT NULL DEFAULT now()
                )
            """)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"created_or_verified":settings.review_relation,
            "native_pages_modified":0}


def record(settings, *, manifest_path, pdf_path, extraction_hash,
           reviewer_id, attestation_code, review_note, lease_owner,
           lease_token, apply=False):
    settings.validate()
    if not isinstance(reviewer_id,str) or not REVIEWER.fullmatch(reviewer_id):
        raise ValueError("Reviewer ID must be a nonempty bounded internal identifier")
    if attestation_code!=ATTESTATION:
        raise ValueError("Explicit visual-fidelity-only attestation required")
    if not isinstance(review_note,str) or not 8<=len(review_note)<=500 or "\x00" in review_note:
        raise ValueError("Review note must contain 8-500 characters")
    if not isinstance(lease_owner,str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}",lease_owner):
        raise ValueError("Invalid lease owner")
    if not isinstance(lease_token,str) or not re.fullmatch(r"[a-f0-9-]{36}",lease_token):
        raise ValueError("Invalid lease token")

    evidence=validate_evidence(
        Path(manifest_path),Path(pdf_path),extraction_hash,
        settings.max_manifest_bytes,
    )
    task_id=evidence["visual_task_id"]
    response={
        "visual_task_id":task_id,"source_page":evidence["page_number"],
        "decision":"VISUAL_FIDELITY_ACCEPTED",
        "numeric_interpretation":"NOT_PERFORMED",
        "source_pdf_sha256":evidence["source_sha256"],
        "manifest_sha256":evidence["manifest_sha256"],
        "composite_sha256":evidence["composite_sha256"],
        "destination":settings.review_relation,
        "writes":bool(apply),
    }
    if not apply:
        return response
    from db import get_conn
    conn=get_conn()
    try:
        with conn.cursor() as cur:
            _set_limits(cur,settings)
            cur.execute(f"""
                SELECT doc_id,page_number,source_sha256,extraction_hash,
                       manifest_sha256,composite_sha256,status,
                       lease_owner,lease_token,lease_expires_at
                FROM {settings.ledger_relation}
                WHERE visual_task_id=%s FOR UPDATE
            """,(task_id,))
            row=cur.fetchone()
            expected=(
                evidence["doc_id"],evidence["page_number"],
                evidence["source_sha256"],evidence["extraction_hash"],
                evidence["manifest_sha256"],evidence["composite_sha256"],
            )
            if row is None or tuple(row[:6])!=expected:
                raise ValueError("Ledger evidence does not match source and figure")
            cur.execute(f"""
                SELECT reviewer_id,attestation_code,review_note,
                       source_sha256,manifest_sha256,composite_sha256
                FROM {settings.review_relation}
                WHERE visual_task_id=%s FOR UPDATE
            """,(task_id,))
            existing=cur.fetchone()
            expected_review=(
                reviewer_id,attestation_code,review_note,
                evidence["source_sha256"],evidence["manifest_sha256"],
                evidence["composite_sha256"],
            )
            if existing:
                if tuple(existing)!=expected_review:
                    raise ValueError("Immutable review already exists with different evidence or reviewer")
                conn.rollback()
                return {**response,"inserted":False,"native_pages_modified":0}
            if (row[6]!="LEASED" or row[7]!=lease_owner
                    or row[8]!=lease_token or row[9] is None):
                raise ValueError("A current matching figure-review lease is required")
            cur.execute("SELECT now() < %s",(row[9],))
            if not cur.fetchone()[0]:
                raise ValueError("Figure-review lease expired")
            cur.execute(f"""
                INSERT INTO {settings.review_relation} (
                    visual_task_id,doc_id,page_number,source_sha256,
                    native_extraction_hash,manifest_sha256,composite_sha256,
                    reviewer_id,attestation_code,review_note
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,(
                task_id,evidence["doc_id"],evidence["page_number"],
                evidence["source_sha256"],evidence["extraction_hash"],
                evidence["manifest_sha256"],evidence["composite_sha256"],
                reviewer_id,attestation_code,review_note,
            ))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {**response,"inserted":True,"native_pages_modified":0}


def inspect(settings, *, doc_id):
    settings.validate()
    if not isinstance(doc_id,str) or not HEX64.fullmatch(doc_id):
        raise ValueError("Invalid document identifier")
    from db import get_conn
    conn=get_conn()
    try:
        conn.set_session(readonly=True,isolation_level="READ COMMITTED")
        with conn.cursor() as cur:
            _set_limits(cur,settings)
            cur.execute(f"""
                SELECT l.visual_task_id,l.page_number,l.status,
                       r.decision,r.reviewer_id,r.reviewed_at,
                       r.manifest_sha256,r.composite_sha256
                FROM {settings.ledger_relation} l
                LEFT JOIN {settings.review_relation} r
                  ON r.visual_task_id=l.visual_task_id
                WHERE l.doc_id=%s
                ORDER BY l.page_number,l.visual_task_id
            """,(doc_id,))
            rows=cur.fetchall()
        conn.rollback()
    finally:
        conn.close()
    return {"tasks":[{
        "visual_task_id":r[0],"page_number":r[1],"ledger_status":r[2],
        "visual_fidelity_decision":r[3],"reviewer_id":r[4],
        "reviewed_at":r[5].isoformat() if r[5] else None,
        "manifest_sha256":r[6],"composite_sha256":r[7],
        "numeric_interpretation":"NOT_VERIFIED",
    } for r in rows],"writes":False}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="command",required=True)
    for command in ("init","record","status"):
        cmd=sub.add_parser(command)
        cmd.add_argument("--review-profile",required=True)
        if command in ("init","record"):
            cmd.add_argument("--apply",action="store_true")
        if command=="record":
            for opt in ("manifest","file","native-profile","reviewer-id",
                        "lease-owner","lease-token","review-note"):
                cmd.add_argument("--"+opt,required=True)
            cmd.add_argument("--attestation-code",required=True)
        if command=="status":
            cmd.add_argument("--doc-id",required=True)
    args=parser.parse_args(argv)
    cfg=load_settings(args.review_profile)
    if args.command=="init":
        result=init(cfg,apply=args.apply)
    elif args.command=="record":
        from page_stage import extraction_fingerprint
        from processing_profile import load_profile
        fingerprint=extraction_fingerprint(load_profile(args.native_profile))
        result=record(
            cfg,manifest_path=args.manifest,pdf_path=args.file,
            extraction_hash=fingerprint,reviewer_id=args.reviewer_id,
            attestation_code=args.attestation_code,
            review_note=args.review_note,lease_owner=args.lease_owner,
            lease_token=args.lease_token,apply=args.apply,
        )
    else:
        result=inspect(cfg,doc_id=args.doc_id)
    print(json.dumps(result,indent=2))


if __name__=="__main__":
    main()
