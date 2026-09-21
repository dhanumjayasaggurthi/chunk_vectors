"""CLI verification for DB -> retrieval fidelity.

Example:
    python retrieval_verifier.py \
      --doc-id <doc_id> \
      --source /path/to/original.pdf \
      --section "II. Requirements" \
      --query "mandatory requirement" \
      --output-dir ./retrieval_results

Checks:
1. DB source blob is byte-identical to the supplied source PDF.
2. DB layout JSON reconstructs exactly the same text/style/geometry manifest.
3. Retrieved section preserves source rendering (pixel similarity threshold).
4. Retrieved literal-text clip still contains the requested text.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import fitz

from layout_preserver import extract_document_layout, find_text_matches
from layout_store import get_source_blob, load_document_layout
from retrieval_service import retrieve_content_pdf, retrieve_section_pdf, retrieve_source_pdf


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _pix_similarity(a: fitz.Pixmap, b: fitz.Pixmap) -> float:
    # Dependency-free mean absolute byte similarity over the overlapping raster.
    aw, ah, an = a.width, a.height, a.n
    bw, bh, bn = b.width, b.height, b.n
    w, h, n = min(aw, bw), min(ah, bh), min(an, bn)
    if w <= 0 or h <= 0 or n <= 0:
        return 0.0
    aa = memoryview(a.samples)
    bb = memoryview(b.samples)
    row_a = aw * an
    row_b = bw * bn
    total = 0
    count = 0
    for y in range(h):
        off_a = y * row_a
        off_b = y * row_b
        for x in range(w):
            pa = off_a + x * an
            pb = off_b + x * bn
            for c in range(n):
                total += abs(int(aa[pa + c]) - int(bb[pb + c]))
                count += 1
    mean_abs = total / max(1, count)
    shape_penalty = (w * h) / max(1, max(aw, bw) * max(ah, bh))
    return max(0.0, 1.0 - mean_abs / 255.0) * shape_penalty


def _section_similarity(source_pdf: Path, retrieved_pdf: Path, fragments) -> float:
    src = fitz.open(source_pdf)
    got = fitz.open(retrieved_pdf)
    scores = []
    try:
        if got.page_count != len(fragments):
            return 0.0
        matrix = fitz.Matrix(1.5, 1.5)
        for i, fragment in enumerate(fragments):
            page = src[fragment.page_number - 1]
            clip = fitz.Rect(fragment.bbox) & page.rect
            a = page.get_pixmap(matrix=matrix, clip=clip, alpha=False)
            b = got[i].get_pixmap(matrix=matrix, alpha=False)
            scores.append(_pix_similarity(a, b))
    finally:
        src.close()
        got.close()
    return sum(scores) / len(scores) if scores else 0.0


def verify(doc_id: str, source: Path, section: str, query: str, output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    stored_layout = load_document_layout(doc_id)
    blob = get_source_blob(doc_id)
    if stored_layout is None or blob is None:
        raise RuntimeError("document fidelity data is missing from PostgreSQL")

    retrieved_source = retrieve_source_pdf(doc_id, output_dir / "retrieved-source.pdf")
    source_bytes_exact = source.read_bytes() == retrieved_source.read_bytes()
    source_sha_exact = _sha(source) == _sha(retrieved_source) == stored_layout.source_sha256

    fresh_layout = extract_document_layout(source)
    manifest_exact = fresh_layout.to_dict() == stored_layout.to_dict()

    section_record = stored_layout.get_section(section)
    retrieved_section = retrieve_section_pdf(doc_id, section, output_dir / "retrieved-section.pdf")
    section_similarity = _section_similarity(source, retrieved_section, section_record.fragments)

    matches = find_text_matches(stored_layout, query)
    content_clip_ok = False
    retrieved_content = None
    if matches:
        retrieved_content = retrieve_content_pdf(
            doc_id, query, output_dir / "retrieved-content.pdf"
        )
        d = fitz.open(retrieved_content)
        try:
            content_clip_ok = query.casefold() in "\n".join(p.get_text() for p in d).casefold()
        finally:
            d.close()

    result = {
        "doc_id": doc_id,
        "source": str(source),
        "source_bytes_exact": source_bytes_exact,
        "source_sha_exact": source_sha_exact,
        "manifest_exact": manifest_exact,
        "page_count": stored_layout.page_count,
        "section": section,
        "section_fragment_count": len(section_record.fragments),
        "section_render_similarity_pct": round(section_similarity * 100, 4),
        "query": query,
        "query_matches": len(matches),
        "content_clip_ok": content_clip_ok,
        "pass": (
            source_bytes_exact
            and source_sha_exact
            and manifest_exact
            and section_similarity >= 0.995
            and content_clip_ok
        ),
        "outputs": {
            "source": str(retrieved_source),
            "section": str(retrieved_section),
            "content": str(retrieved_content) if retrieved_content else None,
        },
    }
    (output_dir / "verification.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc-id", required=True)
    ap.add_argument("--source", required=True, type=Path)
    ap.add_argument("--section", required=True)
    ap.add_argument("--query", required=True)
    ap.add_argument("--output-dir", type=Path, default=Path("retrieval_results"))
    args = ap.parse_args()
    result = verify(args.doc_id, args.source, args.section, args.query, args.output_dir)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["pass"] else 1)


if __name__ == "__main__":
    main()
