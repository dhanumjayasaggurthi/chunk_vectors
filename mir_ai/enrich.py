from __future__ import annotations

import threading

from .models import BBox, ElementRef, PageRecord, stable_id

_tls = threading.local()
# PyMuPDF's table finder uses module-level working buffers internally. Serialize
# only table detection to avoid cross-thread corruption while keeping OCR/render
# work parallel and bounded.
_table_find_lock = threading.Lock()


class PDFEnricher:
    def __init__(
        self,
        file_path,
        doc_id,
        ocr_render_dpi=200,
        vision_ocr=None,
        chart_describer=None,
        image_describer=None,
    ):
        self.file_path = file_path
        self.doc_id = doc_id
        self.ocr_render_dpi = ocr_render_dpi
        self.vision_ocr = vision_ocr
        self.chart_describer = chart_describer
        self.image_describer = image_describer

    def _fitz_doc(self):
        key = f"fitz_{id(self)}"
        doc = getattr(_tls, key, None)
        if doc is None:
            import fitz

            doc = fitz.open(self.file_path)
            setattr(_tls, key, doc)
        return doc

    @staticmethod
    def _find_tables(fitz_page):
        """Find tables without constructing a pdfplumber Page object for every PDF page."""
        with _table_find_lock:
            finder = fitz_page.find_tables(
                vertical_strategy="lines",
                horizontal_strategy="lines",
                snap_tolerance=3,
            )
            tables = list(finder.tables)
            if tables:
                return tables
            finder = fitz_page.find_tables(
                vertical_strategy="text",
                horizontal_strategy="text",
                snap_tolerance=3,
                intersection_tolerance=5,
            )
            return list(finder.tables)

    def enrich_page(self, page: PageRecord) -> PageRecord:
        if page.error:
            return page

        fitz_doc = self._fitz_doc()
        idx = page.page_number - 1
        fitz_page = fitz_doc[idx]

        try:
            found = self._find_tables(fitz_page)
            for table_index, table in enumerate(found):
                bb = table.bbox
                rows = table.extract() or []
                clean = [
                    [
                        "" if cell is None else str(cell).replace("\u00ad", "").strip()
                        for cell in (row or [])
                    ]
                    for row in rows
                ]
                if not any(any(cell for cell in row) for row in clean):
                    continue
                bbox = BBox(float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3]))
                page.elements.append(
                    ElementRef(
                        stable_id(
                            "el",
                            self.doc_id,
                            page.page_number,
                            "table",
                            table_index,
                            bbox.to_list(),
                        ),
                        "table",
                        bbox,
                        len(page.elements),
                        self._to_markdown(clean),
                        extraction_status="success",
                        raw={
                            "rows": clean,
                            "row_count": len(clean),
                            "col_count": max((len(row) for row in clean), default=0),
                            "page_height": page.height,
                        },
                    )
                )
        except Exception as exc:
            page.elements.append(
                ElementRef(
                    stable_id("el", self.doc_id, page.page_number, "table_error"),
                    "table",
                    None,
                    len(page.elements),
                    "",
                    extraction_status="error",
                    raw={"error": str(exc)},
                )
            )

        targets = [
            element
            for element in page.elements
            if element.element_type in {"image", "chart"}
            and element.extraction_status == "pending"
        ]
        if page.needs_full_ocr:
            targets = [element for element in targets if element.raw.get("full_page_ocr")]

        if self.vision_ocr:
            for element in targets:
                pix = None
                data = None
                try:
                    import fitz

                    locator = element.source_locator
                    bbox = element.bbox
                    matrix = fitz.Matrix(
                        self.ocr_render_dpi / 72.0, self.ocr_render_dpi / 72.0
                    )
                    if locator.get("kind") == "pdf_full_page":
                        pix = fitz_page.get_pixmap(matrix=matrix, alpha=False)
                    else:
                        pix = fitz_page.get_pixmap(
                            matrix=matrix,
                            clip=fitz.Rect(*bbox.to_list()),
                            alpha=False,
                        )
                    data = pix.tobytes("png")
                    result = self.vision_ocr(data)
                    ocr = str(getattr(result, "text", result or "")).strip()
                    element.confidence = getattr(result, "confidence", None)
                    success = bool(getattr(result, "success", True))
                    nearby = " ".join(
                        e.text
                        for e in page.elements
                        if e.element_type == "text" and e.text
                    )[:1000]

                    if element.element_type == "chart" and self.chart_describer:
                        description = self.chart_describer(data, nearby).strip()
                        parts = []
                        if description:
                            parts.append(f"[CHART DESCRIPTION]\n{description}")
                        if ocr:
                            parts.append(f"[VISIBLE OCR TEXT]\n{ocr}")
                        element.text = "\n\n".join(parts)
                    elif (
                        element.element_type == "image"
                        and not element.raw.get("full_page_ocr")
                        and self.image_describer
                    ):
                        description = self.image_describer(data, nearby).strip()
                        parts = []
                        if description:
                            parts.append(f"[IMAGE DESCRIPTION]\n{description}")
                        if ocr:
                            parts.append(f"[VISIBLE OCR TEXT]\n{ocr}")
                        element.text = "\n\n".join(parts)
                    else:
                        # Full-page scanned images use OCR as the authoritative text
                        # representation; describing the entire page visually would
                        # duplicate content and increase unsupported inference risk.
                        element.text = ocr

                    low_confidence = str(getattr(result, "error", "")) == "low_confidence"
                    if success and low_confidence:
                        element.extraction_status = "low_confidence"
                    elif success and element.text:
                        element.extraction_status = "success"
                    elif success:
                        element.extraction_status = "low_confidence"
                    else:
                        element.extraction_status = "error"
                except Exception as exc:
                    element.extraction_status = "error"
                    element.raw["error"] = str(exc)
                finally:
                    data = None
                    pix = None

        page.elements.sort(
            key=lambda element: (
                element.bbox.y0 if element.bbox else 0,
                element.bbox.x0 if element.bbox else 0,
                element.reading_order,
            )
        )
        for order, element in enumerate(page.elements):
            element.reading_order = order

        if page.needs_full_ocr:
            full_page = next(
                (
                    e
                    for e in page.elements
                    if e.raw.get("full_page_ocr") and e.text
                ),
                None,
            )
            if full_page:
                page.native_text = full_page.text

        page.extraction_status = "enriched"
        return page

    @staticmethod
    def _to_markdown(rows):
        width = max((len(row) for row in rows), default=0)
        if not width:
            return ""
        padded = [row + [""] * (width - len(row)) for row in rows]
        lines = [
            "| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |"
            for row in padded
        ]
        if len(lines) >= 2:
            lines.insert(1, "|" + "|".join(" --- " for _ in range(width)) + "|")
        return "\n".join(lines)
