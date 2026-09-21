from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from layout_preserver import (  # noqa: E402
    extract_content_pdf,
    extract_document_layout,
    extract_section_pdf,
    find_text_matches,
    load_document_layout,
)

DATA = ROOT / "testdata"
RESULTS = ROOT / "results" / "retrieval_db"
RESULTS.mkdir(parents=True, exist_ok=True)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS fidelity_documents (
            doc_id TEXT PRIMARY KEY,
            source_sha256 TEXT NOT NULL,
            source_bytes BLOB NOT NULL,
            manifest_json TEXT NOT NULL
        );
        """
    )


def store(conn: sqlite3.Connection, doc_id: str, source_bytes: bytes, manifest: dict) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO fidelity_documents
            (doc_id, source_sha256, source_bytes, manifest_json)
        VALUES (?, ?, ?, ?)
        """,
        (doc_id, sha256(source_bytes), sqlite3.Binary(source_bytes), json.dumps(manifest, ensure_ascii=False)),
    )
    conn.commit()


def load(conn: sqlite3.Connection, doc_id: str):
    row = conn.execute(
        "SELECT source_sha256, source_bytes, manifest_json FROM fidelity_documents WHERE doc_id=?",
        (doc_id,),
    ).fetchone()
    if row is None:
        raise KeyError(doc_id)
    return row[0], bytes(row[1]), json.loads(row[2])


def pix_similarity(a: fitz.Pixmap, b: fitz.Pixmap) -> float:
    aw, ah, an = a.width, a.height, a.n
    bw, bh, bn = b.width, b.height, b.n
    w, h, n = min(aw, bw), min(ah, bh), min(an, bn)
    if w <= 0 or h <= 0 or n <= 0:
        return 0.0
    aa, bb = memoryview(a.samples), memoryview(b.samples)
    total = count = 0
    for y in range(h):
        oa, ob = y * aw * an, y * bw * bn
        for x in range(w):
            pa, pb = oa + x * an, ob + x * bn
            for c in range(n):
                total += abs(int(aa[pa + c]) - int(bb[pb + c]))
                count += 1
    score = max(0.0, 1.0 - (total / max(1, count)) / 255.0)
    shape_penalty = (w * h) / max(1, max(aw, bw) * max(ah, bh))
    return score * shape_penalty


def section_similarity(source_pdf: Path, section_pdf: Path, fragments) -> float:
    src = fitz.open(source_pdf)
    got = fitz.open(section_pdf)
    scores = []
    try:
        if got.page_count != len(fragments):
            return 0.0
        matrix = fitz.Matrix(1.5, 1.5)
        for i, frag in enumerate(fragments):
            page = src[frag.page_number - 1]
            clip = fitz.Rect(frag.bbox) & page.rect
            a = page.get_pixmap(matrix=matrix, clip=clip, alpha=False)
            b = got[i].get_pixmap(matrix=matrix, alpha=False)
            scores.append(pix_similarity(a, b))
    finally:
        src.close()
        got.close()
    return sum(scores) / len(scores) if scores else 0.0


def validate_one(conn: sqlite3.Connection, pdf: Path) -> dict:
    source_bytes = pdf.read_bytes()
    layout = extract_document_layout(pdf)
    doc_id = sha256(pdf.name.encode())
    store(conn, doc_id, source_bytes, layout.to_dict())

    stored_sha, db_bytes, manifest_dict = load(conn, doc_id)
    source_bytes_exact = db_bytes == source_bytes
    source_sha_exact = stored_sha == sha256(source_bytes) == layout.source_sha256
    manifest_json_exact = manifest_dict == layout.to_dict()

    with tempfile.TemporaryDirectory(prefix="retrieval-db-") as td:
        td = Path(td)
        retrieved_source = td / "retrieved-source.pdf"
        retrieved_source.write_bytes(db_bytes)
        manifest_path = td / "manifest.json"
        manifest_path.write_text(json.dumps(manifest_dict, ensure_ascii=False), encoding="utf-8")
        db_layout = load_document_layout(manifest_path)
        manifest_rehydration_exact = db_layout.to_dict() == layout.to_dict()

        section = db_layout.get_section("II. Requirements")
        section_pdf = td / "section.pdf"
        extract_section_pdf(retrieved_source, db_layout, section, section_pdf)
        render_similarity = section_similarity(retrieved_source, section_pdf, section.fragments)

        matches = find_text_matches(db_layout, "mandatory requirement")
        content_ok = False
        if matches:
            content_pdf = td / "content.pdf"
            m = matches[0]
            extract_content_pdf(retrieved_source, m["page_number"], m["bbox"], content_pdf)
            d = fitz.open(content_pdf)
            try:
                content_ok = "mandatory requirement" in "\n".join(p.get_text() for p in d).casefold()
            finally:
                d.close()

    result = {
        "document": pdf.name,
        "source_bytes_exact": source_bytes_exact,
        "source_sha_exact": source_sha_exact,
        "manifest_json_exact": manifest_json_exact,
        "manifest_rehydration_exact": manifest_rehydration_exact,
        "pages": layout.page_count,
        "sections": len(layout.sections),
        "section_render_similarity_pct": round(render_similarity * 100, 4),
        "content_clip_ok": content_ok,
    }
    result["pass"] = all(
        [
            source_bytes_exact,
            source_sha_exact,
            manifest_json_exact,
            manifest_rehydration_exact,
            render_similarity >= 0.995,
            content_ok,
        ]
    )
    return result


def main() -> None:
    if not list(DATA.glob("regulatory_fixture_*.pdf")):
        import generate_regulatory_fixtures
        generate_regulatory_fixtures.main()

    db_path = RESULTS / "retrieval_test.sqlite3"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    init_db(conn)
    try:
        rows = [validate_one(conn, p) for p in sorted(DATA.glob("regulatory_fixture_*.pdf"))]
    finally:
        conn.close()

    summary = {
        "documents": len(rows),
        "passed": sum(1 for r in rows if r["pass"]),
        "failed": sum(1 for r in rows if not r["pass"]),
        "byte_exact": sum(1 for r in rows if r["source_bytes_exact"]),
        "manifest_exact": sum(1 for r in rows if r["manifest_rehydration_exact"]),
        "min_section_render_similarity_pct": min(r["section_render_similarity_pct"] for r in rows),
    }
    (RESULTS / "retrieval_results.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    for r in rows:
        print(r["document"], "PASS" if r["pass"] else "FAIL", r["section_render_similarity_pct"])
    raise SystemExit(0 if summary["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
