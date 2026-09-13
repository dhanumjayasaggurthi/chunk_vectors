from __future__ import annotations

import re
from collections.abc import Iterator
from .models import BBox, ElementRef, PageRecord, stable_id


def _clean(text: str) -> str:
    text = (text or "").replace("\u00ad", "").replace("¶", " ")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _looks_like_chart(bbox: BBox, text_elements: list[ElementRef]) -> bool:
    nearby = []
    for el in text_elements:
        if not el.bbox:
            continue
        horizontal = not (el.bbox.x1 < bbox.x0 - 30 or el.bbox.x0 > bbox.x1 + 30)
        vertical = not (el.bbox.y1 < bbox.y0 - 30 or el.bbox.y0 > bbox.y1 + 30)
        if horizontal and vertical:
            nearby.append(el.text)
    text = " ".join(nearby)
    numeric_labels = len(re.findall(r"(?<![A-Za-z])[-+]?\d+(?:[.,]\d+)?%?", text))
    axis_terms = bool(re.search(
        r"\b(mean|dose|day|week|time|concentration|response|percent|%|mg|kg|ml|hr|hours?)\b",
        text,
        re.I,
    ))
    return numeric_labels >= 3 and axis_terms


class PDFPageStream:
    """Lightweight PDF manifest stream.

    `start_page` is 1-based and lets crash recovery seek directly to the next
    uncommitted page instead of reparsing thousands of earlier pages.
    """

    def __init__(self, file_path: str, doc_id: str, scanned_text_threshold: int = 100, start_page: int = 1):
        self.file_path = file_path
        self.doc_id = doc_id
        self.scanned_text_threshold = scanned_text_threshold
        self.start_page = max(1, int(start_page))

    def __iter__(self) -> Iterator[PageRecord]:
        import fitz

        doc = fitz.open(self.file_path)
        try:
            if doc.is_encrypted:
                raise RuntimeError("PDF is password-protected")
            start_index = min(max(0, self.start_page - 1), doc.page_count)
            for idx in range(start_index, doc.page_count):
                page = doc[idx]
                rect = page.rect
                elements = []
                text_parts = []
                headers = []
                footers = []
                order = 0
                try:
                    blocks = page.get_text("blocks")
                except Exception as exc:
                    yield PageRecord(
                        idx + 1,
                        str(idx + 1),
                        rect.width,
                        rect.height,
                        extraction_status="error",
                        error=str(exc),
                    )
                    continue

                for block in blocks:
                    if len(block) < 7 or int(block[6]) != 0:
                        continue
                    x0, y0, x1, y1 = map(float, block[:4])
                    text = _clean(str(block[4]))
                    if not text:
                        continue
                    bbox = BBox(x0, y0, x1, y1)
                    elements.append(ElementRef(
                        stable_id("el", self.doc_id, idx + 1, order, text[:100]),
                        "text",
                        bbox,
                        order,
                        text,
                    ))
                    text_parts.append(text)
                    if y0 <= rect.height * .08:
                        headers.append(text)
                    if y1 >= rect.height * .92:
                        footers.append(text)
                    order += 1

                image_count = 0
                try:
                    for image in page.get_images(full=True):
                        xref = int(image[0])
                        rects = page.get_image_rects(image)
                        for image_rect in rects:
                            if image_rect.width * image_rect.height < 2500:
                                continue
                            bbox = BBox(
                                float(image_rect.x0), float(image_rect.y0),
                                float(image_rect.x1), float(image_rect.y1),
                            )
                            elements.append(ElementRef(
                                stable_id("el", self.doc_id, idx + 1, "img", xref, image_count),
                                "chart" if _looks_like_chart(bbox, elements) else "image",
                                bbox,
                                order,
                                source_locator={
                                    "kind": "pdf_image_region",
                                    "page_index": idx,
                                    "xref": xref,
                                    "bbox": bbox.to_list(),
                                },
                                extraction_status="pending",
                            ))
                            order += 1
                            image_count += 1
                except Exception:
                    # Image enumeration failure must not discard native text.
                    pass

                native = "\n".join(text_parts).strip()
                needs_full_ocr = len(native) < self.scanned_text_threshold and image_count > 0
                if needs_full_ocr:
                    elements.append(ElementRef(
                        stable_id("el", self.doc_id, idx + 1, "fullpage"),
                        "image",
                        BBox(0, 0, float(rect.width), float(rect.height)),
                        order,
                        source_locator={
                            "kind": "pdf_full_page",
                            "page_index": idx,
                            "bbox": [0, 0, float(rect.width), float(rect.height)],
                        },
                        extraction_status="pending",
                        raw={"full_page_ocr": True},
                    ))

                elements.sort(key=lambda e: (
                    e.bbox.y0 if e.bbox else 0,
                    e.bbox.x0 if e.bbox else 0,
                    e.reading_order,
                ))
                for i, element in enumerate(elements):
                    element.reading_order = i

                try:
                    page_label = page.get_label() or str(idx + 1)
                except Exception:
                    page_label = str(idx + 1)

                yield PageRecord(
                    idx + 1,
                    page_label,
                    float(rect.width),
                    float(rect.height),
                    elements,
                    native,
                    " | ".join(headers)[:1000],
                    " | ".join(footers)[:1000],
                    needs_full_ocr,
                )
        finally:
            doc.close()
