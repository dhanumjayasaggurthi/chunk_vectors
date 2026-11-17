"""Conservative IBD (PBRER) and DIBD (DSUR) extraction.

This module never infers dates from report periods, approval dates or a different
birth-date label. A row requires a date and an explicitly associated product, or
a single unambiguous report-cover product.
"""
from __future__ import annotations

import re
from .extract import DATE, norm_date

DATE_AT_START = re.compile(rf"^\s*(?P<date>{DATE})(?:\s*[:\-]\s*|\s+)?(?P<product>.*?)\s*$", re.I)
IBD_LABEL = re.compile(r"\bINTERNATIONAL\s+BIRTH\s+DATE\b(?!\s*\(\s*IBD\s+for\s+inflammatory)", re.I)
DIBD_LABEL = re.compile(r"\b(?:DEVELOPMENT\s+INTERNATIONAL\s+BIRTH\s+DATE|\bDIBD\b)\b", re.I)
DIBD_PAIR = re.compile(
    rf"(?:Development\s+International\s+Birth\s+Date(?:\s*\(\s*DIBD\s*\))?|\bDIBD\b)"
    rf"\s+(?:for|of)\s+(?P<product>[A-Za-z][A-Za-z0-9 /+()\-]{{1,100}}?)"
    rf"\s+(?:is|was|:)\s*(?P<date>{DATE})",
    re.I,
)
COVER_TITLE = re.compile(
    r"^\s*Periodic\s+(?:Benefit[\s-]*Risk\s+Evaluation|Safety\s+Update)\s+Report\s*$",
    re.I,
)
NEXT_HEADING = re.compile(
    r"^\s*(?:PERIOD\s+COVERED|COUNTRY\s+OF|DATE\s+OF\s+FIRST\s+REGISTRATION|"
    r"EUROPEAN\s+UNION\s+REFERENCE|EXECUTIVE\s+SUMMARY|Note\s*:)\b", re.I,
)
SECTION_ONE = re.compile(r"\b(?:EXECUTIVE\s+SUMMARY|SECTION\s*1\b|1[.)]\s*INTRODUCTION)\b", re.I)
SECTION_TWO = re.compile(r"^\s*(?:SECTION\s*2\b|2[.)]\s+[A-Z])", re.I | re.M)


def case_for_artifact(name: str | None) -> str | None:
    s = re.sub(r"[\s\-]+", " ", name or "").casefold()
    if "development safety update report" in s or re.search(r"\bdsur\b", s):
        return "DIBD"
    if ("periodic benefit risk evaluation report" in s
            or re.search(r"\bpbrer\b", s)):
        return "IBD"
    return None


def _clean_product(raw: str) -> str | None:
    product = re.sub(r"\s+", " ", raw).strip(" \t\r\n:;,.()")
    product = re.sub(r"\s+FDC$", "", product, flags=re.I)
    if (not product or len(product) > 120 or not re.search(r"[A-Za-z]", product)
            or re.search(r"\b(?:period covered|international birth date|date of first registration)\b", product, re.I)):
        return None
    # A composite cover title cannot prove which formulation owns one date.
    return product


def _cover_product(pages: list[dict], target_page: int) -> str | None:
    for p in pages:
        if p["page"] > target_page:
            break
        lines = [x.strip() for x in p.get("text", "").splitlines() if x.strip()]
        for i, line in enumerate(lines):
            if COVER_TITLE.fullmatch(line) and i + 1 < len(lines):
                product = _clean_product(lines[i + 1])
                if product and not re.search(r"\s+and\s+|,", product, re.I):
                    return product
    return None


def _pair(product: str, raw_date: str, page: dict, start: int, end: int) -> dict | None:
    date = norm_date(raw_date)
    if not date:
        return None
    text = page["text"]
    return {
        "product": product,
        "date": date,
        "page": page["page"],
        "other_info": text[max(0, start - 200):min(len(text), end + 200)],
        "raw_date": raw_date,
        "method": "LABELED_TEXT",
        "validation": "VERIFIED",
    }


def _unique(pairs: list[dict]) -> tuple[list[dict], list[str]]:
    clean = []
    seen = set()
    dates_by_product = {}
    issues = []
    for pair in pairs:
        key = pair["product"].casefold()
        existing = dates_by_product.setdefault(key, pair["date"])
        if existing != pair["date"]:
            issues.append("Conflicting birth dates for " + pair["product"])
        identity = (key, pair["date"])
        if identity not in seen:
            clean.append(pair)
            seen.add(identity)
    return clean, issues


def extract_pairs(pages: list[dict], case: str) -> tuple[list[dict], list[str]]:
    """Extract from the designated source area. Page numbers are PDF page numbers."""
    if case not in ("IBD", "DIBD"):
        return [], ["Unsupported document artifact"]
    ordered = sorted(pages, key=lambda p: p["page"])
    if case == "IBD":
        for p in ordered:
            if p["page"] > 25:
                break
            text = p.get("text", "")
            matches = list(IBD_LABEL.finditer(text))
            if not matches:
                continue
            # Stop on the first containing page, but consume all date/product rows.
            label = matches[0]
            after = text[label.end():]
            after = re.sub(r"^\s*(?:\([^)]{0,90}\))?\s*(?::|\bis\b|\bwas\b)?\s*", "", after, count=1, flags=re.I)
            lines = after.splitlines(keepends=True)
            explicit = []
            standalone = []
            position = len(text) - len(after)
            for line in lines[:16]:
                if NEXT_HEADING.match(line) or (len(standalone) + len(explicit) >= 100):
                    break
                m = DATE_AT_START.match(line.strip())
                if m:
                    product = _clean_product(m.group("product"))
                    idx = line.find(m.group("date"))
                    begin = position + max(0, idx)
                    pair = _pair(product or "", m.group("date"), p, begin, begin + len(m.group("date")))
                    if pair:
                        if product:
                            if re.search(r"\s+and\s+|,\s+", product, re.I):
                                return [], ["Date is associated with multiple products; review pairing"]
                            explicit.append(pair)
                        else:
                            standalone.append(pair)
                position += len(line)
            if explicit:
                pairs, issues = _unique(explicit)
                return pairs, issues
            if len(standalone) == 1:
                title = _cover_product(ordered, p["page"])
                if title:
                    standalone[0]["product"] = title
                    return standalone, []
                return [], ["IBD date found but product association is ambiguous"]
            return [], ["IBD label found but no unambiguous product–date pair"]
        return [], ["IBD not found in first 25 pages"]

    in_section_one = False
    for p in ordered:
        text = p.get("text", "")
        if SECTION_ONE.search(text):
            in_section_one = True
        if in_section_one and SECTION_TWO.search(text):
            # Do not use DIBD occurrences from later safety sections.
            in_section_one = False
        if not in_section_one:
            continue
        matches = list(DIBD_PAIR.finditer(text))
        if not matches:
            continue
        pairs = []
        for m in matches:
            product = _clean_product(m.group("product"))
            if not product:
                continue
            value = _pair(product, m.group("date"), p, *m.span("date"))
            if value:
                pairs.append(value)
        if pairs:
            return _unique(pairs)
        return [], ["DIBD label found but product or date could not be validated"]
    return [], ["DIBD not found in Section 1 / executive-summary introduction"]


def result_fields(case: str, pairs: list[dict]) -> dict:
    """Keep the existing first-pair UI contract while retaining every pair."""
    fields = {"use_case": case, "birth_date_pairs": pairs}
    if not pairs:
        return fields
    first = pairs[0]
    proof = {
        "value": first["date"], "display": first["raw_date"],
        "page": first["page"], "excerpt": first["other_info"],
        "method": first["method"], "validation": first["validation"],
    }
    fields["international_birth_date" if case == "IBD" else "development_international_birth_date"] = proof
    fields["product"] = {
        **proof, "value": first["product"], "display": first["product"],
    }
    return fields
