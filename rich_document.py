"""Loss-minimizing, versioned native PDF text runs and safe HTML projections.

No network calls, no extra PDF pass, and no global state. The existing page
extractor invokes native_run() on spans already returned by PyMuPDF. RDR v2
is opt-in; original text and page coordinates remain authoritative.
"""
from __future__ import annotations

import html
import math
import re

RICH_SCHEMA_VERSION = 2
MAX_RUNS_PER_ELEMENT = 100_000
MAX_RUN_TEXT = 2_000_000
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _finite(value, default=0.0):
    if type(value) not in (int, float) or not math.isfinite(value):
        return default
    return round(float(value), 4)


def native_run(span: dict, line_index: int) -> dict:
    """Retain PDF font/style evidence without changing the extracted text."""
    flags = int(span.get("flags", 0) or 0)
    font = str(span.get("font") or "")[:256]
    name = font.casefold()
    box = span.get("bbox", (0, 0, 0, 0))
    if not isinstance(box, (tuple, list)) or len(box) != 4:
        box = (0, 0, 0, 0)
    color = span.get("color", 0)
    if type(color) is not int or not 0 <= color <= 0xFFFFFF:
        color = 0
    return {
        "text": str(span.get("text") or ""),
        "font": font,
        "size_pt": _finite(span.get("size")),
        "flags": flags,
        "bold": bool(flags & 16 or "bold" in name),
        "italic": bool(flags & 2 or "italic" in name or "oblique" in name),
        "color_rgb": f"#{color:06x}",
        "bbox": [_finite(v) for v in box],
        "line": line_index,
    }


def validate_runs(value):
    """Reject malformed, oversized or non-finite stored geometry (fail closed)."""
    if not isinstance(value, list) or len(value) > MAX_RUNS_PER_ELEMENT:
        raise ValueError("Invalid rich text runs")
    normalized = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise ValueError("Invalid rich text run")
        if len(item["text"]) > MAX_RUN_TEXT:
            raise ValueError("Rich text run exceeds limit")
        bbox = item.get("bbox")
        if (not isinstance(bbox, list) or len(bbox) != 4
                or any(type(x) not in (int, float) or not math.isfinite(x)
                       for x in bbox)):
            raise ValueError("Invalid rich text run geometry")
        if not isinstance(item.get("font", ""), str) or len(item.get("font", "")) > 256:
            raise ValueError("Invalid rich text font")
        if type(item.get("flags", 0)) is not int:
            raise ValueError("Invalid rich text font flags")
        if type(item.get("line", 0)) is not int or item.get("line", 0) < 0:
            raise ValueError("Invalid rich text line")
        if any(type(item.get(k, False)) is not bool for k in ("bold", "italic")):
            raise ValueError("Invalid rich text style")
        size = item.get("size_pt", 0)
        if type(size) not in (int, float) or not math.isfinite(size) or size < 0:
            raise ValueError("Invalid rich text font size")
        color = item.get("color_rgb", "#000000")
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise ValueError("Invalid rich text color")
        normalized.append(item)
    return normalized


def html_for_elements(elements) -> str:
    """Safe, semantic HTML projection; not pixel-perfect PDF reconstruction.

    Accepts PageElement objects or the staged JSON dictionaries. Every value
    derived from the source document is HTML-escaped.
    """
    rendered = []
    for element in elements:
        get = element.get if isinstance(element, dict) else lambda k, d=None: getattr(element, k, d)
        if get("type", get("element_type")) not in ("text", None):
            continue
        runs = get("runs") or []
        if runs:
            validate_runs(runs)
            parts = []
            previous_line = None
            for run in runs:
                if previous_line is not None and run["line"] != previous_line:
                    parts.append("\n")
                previous_line = run["line"]
                value = html.escape(_CONTROL.sub("", run["text"]), quote=True)
                if run.get("bold"):
                    value = "<strong>" + value + "</strong>"
                if run.get("italic"):
                    value = "<em>" + value + "</em>"
                parts.append(value)
            body = "".join(parts).replace("\n", "<br>\n")
        else:
            body = html.escape(str(get("text", "")), quote=True).replace("\n", "<br>\n")
        if not body:
            continue
        role = get("semantic_role", "")
        level = get("heading_level", 0)
        if role == "heading" and type(level) is int and 1 <= level <= 6:
            tag = "h" + str(level)
        else:
            tag = "p"
        rendered.append("<" + tag + ">" + body + "</" + tag + ">")
    return "\n".join(rendered)
