# Visual retrieval quality and text-only persistence audit

Current release b965877, 2026-11-28. Quality is a P0 requirement, not an optional sacrifice for throughput. Read the [source handshake](SOURCE_EVIDENCE_RETENTION.md) and [living state](PIPELINE_PROJECT_STATE.md) first. This is an engineering design and bounded read-only audit; it is not a claim that selective rendering, full chart fidelity or automated disposal already works.

## 1. Visual content must add retrieval value

Source-owner text-only storage does NOT imply text-only extraction. Detect visual content even when no text, legend or caption is present. Preserve source language and page/region association; distinguish original labels, OCR transcription, source-grounded inferences and unverified model interpretations. Keep useful textual visual descriptions in searchable chunk_text and the native/source-language field, not solely hidden JSON metadata. Include provenance, limitations, evidence kind and source page/figure references. Never invent numeric measurements, identities, causation or outcomes; do not treat OCR confidence or a successful model response as proof.

Scientific examples supplied by the operator show multiple line plots, a multi-panel SEM micrograph, and elemental depth profiles. Screenshots are not stored here. Existing public glass-durability fixture has relevant chart layouts (not hash-matched to server copy). Qualification must include figures with and without text, captions and legends, full scans, unlabeled micrographs, multi-panel images, vector-only plots, dual axes, log axes, multi-page tables, interactive forms and multilingual content. Include text-heavy negative controls.

| Content | Required textual output / checks |
| --- | --- |
| Line, scatter, bar, distribution, dual/log-axis plots | Title/caption, axis variables/units/scales, legend and series mapping when readable, treatment/conditions, direction, relative changes, exceptions, uncertainty. Preserve printed numbers via native/OCR with source reference. Exact inferred plotted values need separate digitization/verification. |
| Micrographs and text-free photos | Visual type, panels, visible features, relative morphology or spatial relationships, caption context, readable scale bar; no unsupported material identity, mechanism, diagnosis or exact measurement. |
| Diagram and flowchart | Nodes/labels, directed edges, relationships and sequences where visible; unknown labels and connections flagged. |
| Scanned or vector tables and forms | Preserve cells, headers, values, order, field names/values and ambiguous regions; avoid inventing logical rows or unchecked fields. |

Illustrative (not provider output): a multi-series chart may show initial growth and subsequent stabilization in some series, with other series remaining lower; describe the actual variable, conditions and association only when grounded. A micrograph can describe visible surface features and panel differences without asserting a chemical mechanism. Generic descriptions such as "A line chart is visible" alone are insufficient for a quality pass when richer information is visible.

## 2. Existing v3 behavior and gaps

Reviewed production.py finish_captured, deferred_ocr.py retrieval_results/visual_plan, visual_enrichment.py, reuse_materialize.py append_enrichment_rows and main legacy extraction. V3 has bounded constrained recovery after selected output violations, with validated candidate targets, explicit limitations and unverified provenance. Its vocabulary is intentionally limited, not a final scientific-summary contract. Current ordinary validator prohibits numeric characters in observation prose, including legitimate scientific identifiers. Do not strip digits or simply remove guard. Future structured source label references should be separated from qualitative model claims and verified against actual evidence.

Visual selection is driven by deferred OCR jobs: chart/vector_region or VISUAL_PENDING results. Native image heuristics and OCR outcomes alone do not certify recall of text-free visuals, especially small/uncaptioned or misclassified images. Audit false negatives against manually annotated pages, not just selected-page coverage. Current visual chunks concatenate observations at page scope and retain target/uncertainty in provenance; stronger figure/panel-level searchable context and numeric label grounding are needed. Preserve native/OCR facts and translations. The live pilot has four visual-model chunks, not demonstrated figure-level coverage or quality. Never weaken v3 validation just to force publication.

Proposed quality qualification: manually annotated expected figures/regions and key facts per public document; measure selection recall/false positives, required text/table/form coverage, label and unit accuracy, figure/series association correctness, unsupported claim rate, usable description completeness, language fidelity, and retrieval Recall@k/MRR on human-authored questions. Separate constrained recovery from ordinary responses. Record failed/abstained/ambiguous cases, provider cost/latency and actual source provenance. Require review and agreed thresholds before claiming production semantic quality; no threshold or score invented now. Exact numerical plot digitization remains a separate unqualified capability. Full provider calls only on approved public or authorized samples.

## 3. Persistence routes — code review findings, not measured contents

| Route | Existing persistence / dependency | Required lifecycle work |
| --- | --- | --- |
| visual_evidence.py _paths/_capture_open_page | capture-ID page PNG and JSON, source identity/geometry, optional raster inventory, vector drawings and figure hints | Stage temporarily; retain only permitted textual provenance; remove all reconstructable graphics after eligibility. |
| visual_evidence.py _raster_path/_store_raster | Original image/mask bytes in raster/<prefix>/<hash>.bin | No permanent source image bytes; include in disposal, audit orphan assets. |
| page_stage.py page serialization / extracted_source.py | Conditional vector_preview with base64 PNG in native published/work JSONB; reader decodes it | Audit actual occurrences; prevent future durable binary payloads without losing textual content or invalidating checkpoints. Filesystem cleanup alone insufficient. |
| deferred_ocr.py | OCR job source refs, bounds, text, regions, leases | Preserve allowed textual output and audit refs; check any reconstructable payloads and active readers/retries. |
| visual_enrichment.py _cache_path | Per-capture JSON responses, identities and review flags | Persist allowed textual results/provenance independent of temporary image cache, avoid regeneration after successful completion. |
| DOCX evidence | Retained XML-derived story/chart token manifests, media .bin assets | Independent text-only conversion/QA; no archive or reconstructable substitutes. |
| reuse_materialize.py / embeddings | Native text, English retrieval text, metadata, vectors, status/failure tables | Preserve allowed outputs; audit nested metadata and embedding/runtime/log fields. |
| receipts, logs, backups/snapshots, provider telemetry | Operational details; possible sensitive paths or source fragments | Apply approved output field/TTL and access policy; no private source copies. |

The native visual profile enables all_pages, capture_rasters, capture_vectors and capture_figures. The production resolver rejects native_text_or_render, even though a prototype exists in visual_evidence.py. Do not change the profile to selective mode on a populated destination. The next implementation must qualify selection recall and keep source-version identities explicit. Capture IDs depend on source bytes/settings, not path, so multiple logical documents can share a capture. Disposal must account for all owners and active readers, not just one completed receipt. Current native replay backfills missing evidence even for published documents.

## 4. Bounded read-only audit (after the current coordinator is idle)

First collect live process, status and disk outputs using README. Do not change config, delete files or start another coordinator while running. If staging is not approved or expiry/disk floor is approaching, coordinate controlled stop and approved disposal. For the six completed documents, use their original exact receipt, not a filename guess. The following inspects only bounded receipt members and counts a known conditional preview payload; no text, full document IDs or credentials printed. It does not prove absence of all binary data, source freshness, semantic quality or cleanup eligibility. No DDL, writes, provider calls or deletions. Run on the isolated pilot only; statement timeout protects the database. No need to repeatedly scan payloads during an active run.

~~~bash
RECEIPT="receipts/ACTUAL_SIX_DOCUMENT_RECEIPT.jsonl"
python - "$RECEIPT" <<'PY'
import json, re, sys
from pathlib import Path
from production import resolve_configuration
from page_stage import names, extraction_fingerprint
from reuse_materialize import identifiers
from db import get_conn

d=resolve_configuration()
receipt=Path(sys.argv[1]).resolve(strict=True)
assert receipt.is_relative_to(d["receipts"].resolve(strict=True))
assert receipt.stat().st_size <= 2*1024*1024, "Bounded pilot receipts only"
rows=[json.loads(line) for line in receipt.read_text(encoding="utf-8").splitlines()]
assert 1 <= len(rows) <= 16
assert all(r["status"]=="LOCAL_CAPTURE_VERIFIED" and r["format"]=="pdf"
           and r.get("local_content_verified") is True for r in rows)
ids=[r["doc_id"] for r in rows]
assert len(set(ids))==len(ids)
assert all(re.fullmatch(r"[0-9a-f]{64}",x) for x in ids)
n=names(d["capture"])
q=identifiers(d["retrieval"])
conn=get_conn()
try:
    with conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cur.execute("SELECT set_config('statement_timeout','15000',true)")
        cur.execute(f"""SELECT count(*), COALESCE(sum(page_count),0)
            FROM {n['docs']} WHERE doc_id=ANY(%s) AND extraction_hash=%s""",
            (ids,extraction_fingerprint(d["capture"])))
        print("Current native docs/pages:",cur.fetchone())
        for label,table in (("published",n["published"]),("work",n["work"])):
            cur.execute(f"""SELECT count(*) FILTER (WHERE item ? 'vector_preview'),
                COALESCE(sum(length(item->'vector_preview'->>'base64')),0)
                FROM {table} p CROSS JOIN LATERAL jsonb_array_elements(p.elements) item
                WHERE p.doc_id=ANY(%s)""",(ids,))
            print(label,"vector preview elements/base64 characters:",cur.fetchone())
        cur.execute(f"""SELECT COALESCE(chunk_metadata->>'source_kind','unspecified_native'),
            count(*), count(chunk_vector)
            FROM {q['table']} WHERE doc_id=ANY(%s)
            GROUP BY 1 ORDER BY 1""",(ids,))
        print("Source kind / chunks / vectors:",cur.fetchall())
        cur.execute(f"""SELECT count(*) FROM {q['table']} WHERE doc_id=ANY(%s)
            AND chunk_metadata->>'source_kind'='visual_model'
            AND chunk_metadata->'source_provenance'->>'corrective_response'='true'""",(ids,))
        print("Corrective visual chunks:",cur.fetchone()[0])
finally:
    conn.rollback()
    conn.close()
print("Read-only audit: no deletion authorization or semantic certification.")
PY
~~~

For visual quality, inspect selected source pages and corresponding native_text/chunk_text and provenance in the configured retrieval table locally (never post confidential text or image bytes to Git). Review both normal and corrective descriptions, compare with independently annotated source, verify retrieval query results after embedding. The operator can share aggregate counts/errors only.

## 5. Implementation sequence

1. Finish observing current folder run; reconcile actual results and retention authorization. Perform bounded audit and source QA, no blind deletion.
2. Implement text-only lifecycle using existing manifests, approved capacity/TTL, source/capture ownership, reader fencing, completed-document early skip, durable textual publication, explicit dry-run/apply, crash/orphan/retry handling and continuous disk admission. Test all persistence routes including DB previews, no source deletion. Avoid new tables/services unless justified.
3. Qualify selective on-demand rendering against manually annotated visual recall and text/table fidelity. Preserve required image interpretation before releasing pixels; separate verified labels/units from AI insights. Richer structured descriptions and retrieval evaluation must pass before production rollout; never rely on generic recovery vocabulary alone.
4. Qualify bounded large documents, >4000-page original, sustained 100/1000-doc workloads and resource/progress metrics. Keep all quality/retention/throughput results distinct.

No automatic cleanup, complete visual selection recall, exact plot digitization, continuous disk guard or new production progress command implemented by this documentation iteration.
