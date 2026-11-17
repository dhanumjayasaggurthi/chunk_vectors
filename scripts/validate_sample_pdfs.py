"""Generate and validate representative synthetic PBRER/DSUR PDFs.

Run from repository root:
    python scripts/validate_sample_pdfs.py
Inspect demo_output/results.csv, manifest.json, preview.html and pdfs/*.pdf.

This checks PDF -> PyMuPDF -> rule extractor -> business CSV. It does not
demonstrate corporate RIMdocs access, OCR quality or regulated accuracy.
"""
from __future__ import annotations
import csv
import html
import json
from pathlib import Path
import sys

import fitz

from backend.demo_documents import SAMPLES, pdf_bytes
from backend.birth_dates import extract_pairs, result_fields
from backend.birth_date_export import COLUMNS, rows_for_document, safe_csv


def run(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "pdfs").mkdir(exist_ok=True)
    records = []
    overview = []
    for sample_id, fixture in SAMPLES.items():
        raw = pdf_bytes(sample_id)
        (output / "pdfs" / f"{sample_id}.pdf").write_bytes(raw)
        with fitz.open(stream=raw,filetype="pdf") as pdf:
            pages=[{"page":i+1,"text":pdf[i].get_text("text",sort=True) or ""}
                   for i in range(pdf.page_count)]
        pairs, issues = extract_pairs(pages,fixture["case"])
        got = [(p["product"],p["date"]) for p in pairs]
        expected=fixture["expected"]
        if got != expected or issues:
            raise AssertionError(
                f"{sample_id} failed:\n expected={expected}\n got={got}\n issues={issues}"
            )
        document = {
            "r_object_id":sample_id,
            "edms_number":fixture["edms_number"],
            "product_code":fixture["product_code"],
            "fields":result_fields(fixture["case"],pairs),
        }
        sample_rows = list(rows_for_document(document))
        records.extend(sample_rows)
        overview.append({
            "id":sample_id, "document":fixture["name"],
            "case":fixture["case"], "pages":len(pages),
            "pairs":len(pairs), "status":"PASS",
            "pairs_verified":got,
            "note":"Synthetic example only; original RIMdocs PDF not supplied",
        })
    with (output/"results.csv").open("w",encoding="utf-8",newline="") as f:
        writer = csv.DictWriter(f,fieldnames=COLUMNS)
        writer.writeheader()
        for row in records:
            writer.writerow(safe_csv(row))
    (output/"manifest.json").write_text(json.dumps({
        "fixture_type":"SYNTHETIC_SLIDE_RECONSTRUCTION",
        "documents":len(overview),
        "pairs":len(records),
        "results":overview,
    },indent=2),encoding="utf-8")
    header = "".join(f"<th>{html.escape(s)}</th>" for s in COLUMNS)
    data = "".join(
        "<tr>" + "".join(
            f"<td>{html.escape(str(row.get(col) or ''))}</td>" for col in COLUMNS
        ) + "</tr>"
        for row in records
    )
    (output/"preview.html").write_text(
        '<!doctype html><html lang="en"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>EvidenceFlow PDF validation preview</title>'
        '<style>body{font-family:system-ui,sans-serif;margin:32px;'
        'color:#222}h1{font-size:26px}p{max-width:820px}'
        'table{border-collapse:collapse;width:100%;font-size:13px}'
        'th{background:#c72413;color:white;text-align:left}th,td{padding:9px;'
        'border:1px solid #ddd;vertical-align:top}tr:nth-child(even){background:#faf6f6}'
        '.note{padding:12px;background:#fff6e8;border-left:4px solid #e6a23a}'
        '</style></head><body><h1>EvidenceFlow sample PDF extraction</h1>'
        '<p class="note"><strong>Synthetic test data:</strong> These PDFs were '
        'constructed from slide examples. They are not source RIMdocs documents '
        'and do not validate production accuracy.</p>'
        f'<p><strong>{len(overview)} PDFs passed · {len(records)} product-date pairs</strong></p>'
        f'<table><thead><tr>{header}</tr></thead><tbody>{data}</tbody></table>'
        '<p>See the accompanying pdfs/ folder for each generated PDF.</p>'
        '</body></html>',encoding="utf-8"
    )
    print(f"VALIDATION PASSED: {len(overview)} synthetic PDFs; {len(records)} product-date pairs")
    for record in overview:
        print(f"  {record['id']}: {record['case']}, {record['pairs']} pair(s), source page {SAMPLES[record['id']]['evidence_page']}")
    print("Outputs:",output)


if __name__ == "__main__":
    run(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("demo_output"))
