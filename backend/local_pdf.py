"""Local-only, ephemeral evidence inspection from operator-supplied original PDFs.

No corporate service connections. No source PDFs or excerpts persisted in the app.
This is a diagnostic review, not a regulatory approval.
"""
from __future__ import annotations
from pathlib import Path
import fitz
from .birth_dates import extract_pairs

MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_DIBD_PAGES = 150

class LocalPDFError(ValueError):
    pass

def inspect_pdf_bytes(content:bytes, filename:str, case:str, object_id:str="",
                      edms_number:str="", product_code:str="")->dict:
    if case not in ("IBD","DIBD"):
        raise LocalPDFError("Choose IBD (PBRER) or DIBD (DSUR)")
    if not content.startswith(b"%PDF-"):
        raise LocalPDFError("The selected file is not a PDF")
    if len(content)>MAX_UPLOAD_BYTES:
        raise LocalPDFError("PDF exceeds the 100 MB local preview limit")
    try:
        pdf=fitz.open(stream=content,filetype="pdf")
    except Exception as exc:
        raise LocalPDFError("Unable to open this PDF") from exc
    with pdf:
        if pdf.needs_pass:
            raise LocalPDFError("Encrypted PDF: open an authorized decrypted copy")
        if not 0<pdf.page_count<=20000:
            raise LocalPDFError("PDF page count is not supported")
        limit=min(pdf.page_count,25 if case=="IBD" else MAX_DIBD_PAGES)
        pages=[];unreadable=[]
        for i in range(limit):
            p=pdf[i]
            extracted=p.get_text("text",sort=True) or ""
            pages.append({"page":i+1,"text":extracted})
            if not extracted.strip() and (p.get_images() or p.get_drawings()):
                unreadable.append(i+1)
        pairs,issues=extract_pairs(pages,case)
        if unreadable:
            issues.append("OCR needed on image-only pages: "+", ".join(map(str,unreadable[:25])))
        if case=="DIBD" and pdf.page_count>limit and not pairs:
            issues.append(f"Only the first {limit} pages were checked; later Section 1 content was not examined")
        return {
            "file_name":Path(filename or "source.pdf").name[:200],
            "use_case":case,
            "r_object_id":object_id.strip() or None,
            "edms_number":edms_number.strip() or None,
            "product_code":product_code.strip() or None,
            "metadata_verified":False,
            "page_count":pdf.page_count,
            "pages_scanned":limit,
            "status":"EVIDENCE_FOUND" if pairs and not issues else "REVIEW_REQUIRED",
            "pairs":pairs,
            "issues":issues,
            "disclaimer":"Original file processed in this local session; evidence requires human review. No RIMdocs metadata verification or OCR performed.",
        }
