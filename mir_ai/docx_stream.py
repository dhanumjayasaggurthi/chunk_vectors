from __future__ import annotations

from collections.abc import Iterator
import re
import threading
import zipfile
import xml.etree.ElementTree as ET

from .models import ElementRef, PageRecord, stable_id

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_C = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"
_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_tls = threading.local()


def _text_from_element(element):
    texts = []
    for node in element.iter():
        if node.tag == _W + "t" and node.text:
            texts.append(node.text)
        elif node.tag == _W + "tab":
            texts.append("\t")
        elif node.tag in {_W + "br", _W + "cr"}:
            texts.append("\n")
    return re.sub(r"[ \t]+", " ", "".join(texts)).strip()


def _table_markdown(table):
    rows = []
    for tr in table.findall(".//" + _W + "tr"):
        row = [
            _text_from_element(tc).replace("\n", " ").strip()
            for tc in tr.findall("./" + _W + "tc")
        ]
        if row:
            rows.append(row)
    if not rows:
        return "", []
    width = max(map(len, rows))
    padded = [row + [""] * (width - len(row)) for row in rows]
    lines = [
        "| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |"
        for row in padded
    ]
    if len(lines) >= 2:
        lines.insert(1, "|" + "|".join(" --- " for _ in range(width)) + "|")
    return "\n".join(lines), rows


def _relationships(zf):
    try:
        root = ET.fromstring(zf.read("word/_rels/document.xml.rels"))
    except KeyError:
        return {}
    relationships = {}
    for relationship in root.findall(_REL_NS + "Relationship"):
        rid = relationship.attrib.get("Id")
        target = relationship.attrib.get("Target", "")
        if not rid or not target or target.startswith("http"):
            continue
        target = target.lstrip("/")
        target = target if target.startswith("word/") else "word/" + target
        parts = []
        for part in target.split("/"):
            if part == ".." and parts:
                parts.pop()
            elif part not in {".", ""}:
                parts.append(part)
        relationships[rid] = "/".join(parts)
    return relationships


def _linked_assets(element, relationships):
    out = []
    for node in element.iter():
        if node.tag == _A + "blip":
            rid = node.attrib.get(_R + "embed")
            if rid and rid in relationships:
                out.append(("image", relationships[rid]))
        elif node.tag == _C + "chart":
            rid = node.attrib.get(_R + "id")
            if rid and rid in relationships:
                out.append(("chart", relationships[rid]))
    seen = set()
    unique = []
    for item in out:
        if item not in seen:
            unique.append(item)
            seen.add(item)
    return unique


class DOCXPageStream:
    """Bounded-memory DOCX XML stream.

    DOCX does not provide reliable rendered pages/BBOX coordinates. Explicit Word
    page breaks are honored when available. To prevent a 12k-page-equivalent DOCX
    with no explicit breaks from accumulating the whole document in RAM, content is
    additionally checkpointed into bounded logical segments. These segment numbers
    are internal provenance only and are never represented as rendered page numbers.
    """

    def __init__(self, file_path, doc_id, max_elements_per_segment=200):
        self.file_path = file_path
        self.doc_id = doc_id
        self.max_elements_per_segment = max(1, int(max_elements_per_segment))

    def __iter__(self) -> Iterator[PageRecord]:
        page_no = 1
        order = 0
        current = []
        current_text = []

        def add_element(element_type, text="", source_locator=None, status="native", raw=None):
            nonlocal order
            element = ElementRef(
                stable_id(
                    "el",
                    self.doc_id,
                    page_no,
                    order,
                    element_type,
                    text[:100] if text else (source_locator or {}).get("zip_path", ""),
                ),
                element_type,
                None,
                order,
                text,
                source_locator=source_locator or {},
                extraction_status=status,
                raw={
                    "docx_logical_page": True,
                    "page_provenance": "logical_segment",
                    **(raw or {}),
                },
            )
            current.append(element)
            if text:
                current_text.append(text)
            order += 1

        def flush():
            nonlocal page_no, order, current, current_text
            if not current and page_no != 1:
                return None
            record = PageRecord(
                page_no,
                f"logical-{page_no}",
                0,
                0,
                current,
                "\n".join(current_text),
            )
            page_no += 1
            order = 0
            current = []
            current_text = []
            return record

        with zipfile.ZipFile(self.file_path) as zf:
            relationships = _relationships(zf)
            with zf.open("word/document.xml") as xml:
                context = ET.iterparse(xml, events=("start", "end"))
                table_depth = 0
                stack = []
                for event, element in context:
                    if event == "start":
                        stack.append(element)
                        if element.tag == _W + "tbl":
                            table_depth += 1
                        continue

                    explicit_break = False
                    processed_top_level = False
                    if element.tag == _W + "p" and table_depth == 0:
                        text = _text_from_element(element)
                        if text:
                            add_element("text", text=text)
                        for asset_type, zip_path in _linked_assets(element, relationships):
                            add_element(
                                asset_type,
                                source_locator={
                                    "kind": "docx_media"
                                    if asset_type == "image"
                                    else "docx_chart_xml",
                                    "zip_path": zip_path,
                                },
                                status="pending",
                            )
                        explicit_break = any(
                            node.tag == _W + "br" and node.attrib.get(_W + "type") == "page"
                            for node in element.iter()
                        )
                        processed_top_level = True
                        if len(stack) >= 2:
                            try:
                                stack[-2].remove(element)
                            except ValueError:
                                pass
                        element.clear()

                    elif element.tag == _W + "tbl":
                        if table_depth == 1:
                            text, rows = _table_markdown(element)
                            if text:
                                add_element(
                                    "table",
                                    text=text,
                                    status="success",
                                    raw={
                                        "rows": rows,
                                        "row_count": len(rows),
                                        "col_count": max((len(row) for row in rows), default=0),
                                    },
                                )
                            for asset_type, zip_path in _linked_assets(element, relationships):
                                add_element(
                                    asset_type,
                                    source_locator={
                                        "kind": "docx_media"
                                        if asset_type == "image"
                                        else "docx_chart_xml",
                                        "zip_path": zip_path,
                                    },
                                    status="pending",
                                )
                            explicit_break = any(
                                node.tag == _W + "br" and node.attrib.get(_W + "type") == "page"
                                for node in element.iter()
                            )
                            processed_top_level = True
                            if len(stack) >= 2:
                                try:
                                    stack[-2].remove(element)
                                except ValueError:
                                    pass
                            element.clear()
                        table_depth -= 1

                    if stack and stack[-1] is element:
                        stack.pop()

                    if processed_top_level and (
                        explicit_break or len(current) >= self.max_elements_per_segment
                    ):
                        record = flush()
                        if record is not None:
                            yield record

        if current or page_no == 1:
            record = flush()
            if record is not None:
                yield record


class DOCXEnricher:
    def __init__(self, file_path, vision_ocr=None):
        self.file_path = file_path
        self.vision_ocr = vision_ocr

    def _zip(self):
        key = f"docx_zip_{id(self)}"
        zf = getattr(_tls, key, None)
        if zf is None:
            zf = zipfile.ZipFile(self.file_path)
            setattr(_tls, key, zf)
        return zf

    def enrich_page(self, page):
        zf = self._zip()
        for element in page.elements:
            if element.extraction_status != "pending":
                continue
            try:
                locator = element.source_locator
                if locator.get("kind") == "docx_media" and self.vision_ocr:
                    result = self.vision_ocr(zf.read(locator["zip_path"]))
                    element.text = str(getattr(result, "text", result or ""))
                    element.confidence = getattr(result, "confidence", None)
                    success = bool(getattr(result, "success", True))
                    low = str(getattr(result, "error", "")) == "low_confidence"
                    if success and low:
                        element.extraction_status = "low_confidence"
                    elif success and element.text:
                        element.extraction_status = "success"
                    elif success:
                        element.extraction_status = "low_confidence"
                    else:
                        element.extraction_status = "error"
                elif locator.get("kind") == "docx_chart_xml":
                    root = ET.fromstring(zf.read(locator["zip_path"]))
                    labels = [
                        node.text.strip()
                        for node in root.iter()
                        if node.tag == _A + "t" and node.text and node.text.strip()
                    ]
                    values = [
                        node.text.strip()
                        for node in root.iter()
                        if node.tag == _C + "v" and node.text and node.text.strip()
                    ]
                    parts = []
                    if labels:
                        parts.append("Labels: " + " | ".join(labels))
                    if values:
                        parts.append("Values: " + " | ".join(values))
                    element.text = "[DOCX CHART DATA]\n" + "\n".join(parts) if parts else ""
                    element.extraction_status = "success" if element.text else "missing"
            except Exception as exc:
                element.extraction_status = "error"
                element.raw["error"] = str(exc)

        if any(
            e.element_type in {"image", "chart"} and e.text for e in page.elements
        ):
            page.native_text = "\n".join(
                [page.native_text]
                + [
                    e.text
                    for e in page.elements
                    if e.element_type in {"image", "chart"} and e.text
                ]
            ).strip()
        page.extraction_status = "enriched"
        return page
