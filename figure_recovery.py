"""Offline figure-fragment reconstruction with immutable, review-pending evidence.

This is an independent visual-sidecar producer. It neither changes the native
extraction fingerprint nor writes/approves PostgreSQL page checkpoints.
The source PDF and original image XREFs remain authoritative.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path


ALGORITHM_VERSION = "adjacent-horizontal-strips-v2-caption-aware"


@dataclass(frozen=True)
class FigureSettings:
    min_fragments: int = 2
    max_fragments: int = 32
    max_page_images: int = 256
    max_vertical_gap_pt: float = 1.0
    alignment_tolerance_pt: float = 1.5
    pixel_scale_tolerance: float = 0.06
    dpi: int = 200
    margin_horizontal_pt: float = 12.0
    margin_above_pt: float = 25.0
    margin_below_pt: float = 12.0
    caption_search_pt: float = 40.0
    max_render_pixels: int = 25_000_000
    max_png_bytes: int = 32_000_000

    def validate(self):
        for name, lower, upper in (
            ("min_fragments", 2, 64),
            ("max_fragments", 2, 64),
            ("max_page_images", 2, 1024),
            ("dpi", 72, 400),
            ("max_render_pixels", 100_000, 75_000_000),
            ("max_png_bytes", 100_000, 100_000_000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"Invalid figure setting {name}")
        if self.min_fragments > self.max_fragments or self.max_fragments > self.max_page_images:
            raise ValueError("Invalid figure fragment/page limits")
        for name, low, high in (
            ("max_vertical_gap_pt", 0, 10),
            ("alignment_tolerance_pt", 0, 10),
            ("pixel_scale_tolerance", 0, 0.15),
            ("margin_horizontal_pt", 0, 100),
            ("margin_above_pt", 0, 100),
            ("margin_below_pt", 0, 100),
            ("caption_search_pt", 0, 150),
        ):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"Invalid figure setting {name}")
        return self


def _canonical_bytes(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _image_info(page, settings: FigureSettings):
    raw = page.get_image_info(xrefs=True)
    if len(raw) > settings.max_page_images:
        raise ValueError("PAGE_IMAGE_COUNT_LIMIT: cannot silently discard source images")
    result = []
    for info in raw:
        xref = info.get("xref", 0)
        bbox = info.get("bbox", ())
        width, height = info.get("width", 0), info.get("height", 0)
        if not isinstance(xref, int) or xref <= 0:
            # Image without an original XREF cannot meet this source-provenance
            # contract; another visual extraction adapter must handle it.
            continue
        if len(bbox) != 4 or not all(math.isfinite(float(n)) for n in bbox):
            raise ValueError("Invalid source image geometry")
        b = [float(n) for n in bbox]
        if b[0] >= b[2] or b[1] >= b[3]:
            raise ValueError("Invalid source image bounding box")
        if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
            raise ValueError("Invalid source image dimensions")
        result.append({
            "xref": xref, "bbox": b,
            "pixel_width": width, "pixel_height": height,
        })
    return sorted(result, key=lambda v: (v["bbox"][1], v["bbox"][0], v["xref"]))


def _adjacent(a, b, settings: FigureSettings) -> bool:
    first, second = a["bbox"], b["bbox"]
    gap = second[1] - first[3]
    if not -settings.max_vertical_gap_pt <= gap <= settings.max_vertical_gap_pt:
        return False
    if (abs(first[0] - second[0]) > settings.alignment_tolerance_pt
            or abs(first[2] - second[2]) > settings.alignment_tolerance_pt):
        return False
    # A strip-sliced chart has consistent pixel density along BOTH axes.
    for axis in ("x", "y"):
        if axis == "x":
            d1 = a["pixel_width"] / (first[2] - first[0])
            d2 = b["pixel_width"] / (second[2] - second[0])
        else:
            d1 = a["pixel_height"] / (first[3] - first[1])
            d2 = b["pixel_height"] / (second[3] - second[1])
        if abs(d1 - d2) / max(d1, d2) > settings.pixel_scale_tolerance:
            return False
    return True


def find_figure_groups(page, settings: FigureSettings | None = None):
    """Return *candidates*, not automatically approved or classified charts.

    Requires full-width alignment, adjacent vertical edges, consistent pixel
    density and bounded group length. No pairwise O(n^2) geometry search.
    """
    settings = (settings or FigureSettings()).validate()
    source = _image_info(page, settings)
    groups = []
    current = []

    def flush():
        if len(current) >= settings.min_fragments:
            groups.append(list(current))

    for info in source:
        if not current:
            current = [info]
        elif (len(current) < settings.max_fragments
              and _adjacent(current[-1], info, settings)):
            current.append(info)
        else:
            flush()
            current = [info]
    flush()
    return groups


def _write_immutable(path: Path, data: bytes) -> None:
    """Exclusive create; retry is idempotent only with identical content."""
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if _hash_file(path) != hashlib.sha256(data).hexdigest():
            raise RuntimeError(f"Existing evidence differs: {path.name}")


_CAPTION_START = re.compile(r"^\s*(?:figure|fig\.?)\s*\d+[A-Za-z]?(?::|\.|\s)", re.I)


def _caption_lines(page):
    """Bounded native-text caption candidates; never invent caption text."""
    lines = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            content = "".join(str(span.get("text") or "") for span in line.get("spans", []))
            if not _CAPTION_START.match(content):
                continue
            bbox = line.get("bbox", ())
            if len(bbox) != 4 or not all(math.isfinite(float(x)) for x in bbox):
                continue
            lines.append({"bbox": [float(x) for x in bbox],
                          "text_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()})
    return lines


def _nearby_caption(lines, bbox, max_distance):
    eligible = []
    for line in lines:
        b = line["bbox"]
        distance = bbox[1] - b[3]
        if (-4 <= distance <= max_distance
            and min(bbox[2], b[2]) >= max(bbox[0], b[0])):
            eligible.append((abs(distance), line))
    return min(eligible, key=lambda pair: pair[0])[1] if eligible else None


def prepare_page(
    pdf_path: Path, page_number: int, output_dir: Path,
    settings: FigureSettings | None = None, native_extraction_hash: str = "",
):
    """Reconstruct candidates once and emit independently versioned evidence.

    Never performs OCR, clinical chart interpretation or PostgreSQL writes.
    """
    import fitz

    settings = (settings or FigureSettings()).validate()
    pdf_path = pdf_path.resolve(strict=True)
    if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
        raise ValueError("Expected existing local PDF")
    source_sha256 = _hash_file(pdf_path)
    doc_id = hashlib.sha256(str(pdf_path).encode("utf-8")).hexdigest()
    if not isinstance(native_extraction_hash, str) or len(native_extraction_hash) > 128:
        raise ValueError("Invalid native extraction hash")

    records = []
    with fitz.open(pdf_path) as pdf:
        if pdf.is_encrypted or not 1 <= page_number <= pdf.page_count:
            raise ValueError("Encrypted PDF or invalid source page")
        page = pdf[page_number - 1]
        groups = find_figure_groups(page, settings)
        if not groups:
            return []
        caption_lines = _caption_lines(page) if settings.caption_search_pt else []

        output_dir.mkdir(parents=True, exist_ok=True)

        for group in groups:
            fragments = []
            for fragment in group:
                original = pdf.extract_image(fragment["xref"])
                source_bytes = original.get("image") if isinstance(original, dict) else None
                if not isinstance(source_bytes, bytes) or not source_bytes:
                    raise ValueError("Original source image bytes not available")
                fragments.append({
                    **fragment,
                    "original_sha256": hashlib.sha256(source_bytes).hexdigest(),
                    "original_bytes": len(source_bytes),
                })

            bounds = [
                min(f["bbox"][0] for f in fragments),
                min(f["bbox"][1] for f in fragments),
                max(f["bbox"][2] for f in fragments),
                max(f["bbox"][3] for f in fragments),
            ]
            caption = _nearby_caption(caption_lines, bounds, settings.caption_search_pt)
            clip = fitz.Rect(
                min(bounds[0], caption["bbox"][0] if caption else bounds[0]) - settings.margin_horizontal_pt,
                min(bounds[1] - settings.margin_above_pt,
                    caption["bbox"][1] - 5 if caption else bounds[1]),
                max(bounds[2], caption["bbox"][2] if caption else bounds[2]) + settings.margin_horizontal_pt,
                bounds[3] + settings.margin_below_pt,
            ) & page.rect
            scale = settings.dpi / 72
            expected_pixels = math.ceil(clip.width * scale) * math.ceil(clip.height * scale)
            if expected_pixels > settings.max_render_pixels:
                raise ValueError("FIGURE_RENDER_PIXEL_BUDGET_EXCEEDED")

            # The PDF renderer composes masks/overlays correctly; do not try
            # to concatenate raw image rows or infer any numerical chart data.
            png = page.get_pixmap(
                matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False
            ).tobytes("png")
            if len(png) > settings.max_png_bytes:
                raise ValueError("FIGURE_PNG_BYTE_BUDGET_EXCEEDED")

            identity = {
                "source_pdf_sha256": source_sha256,
                "source_page": page_number,
                "algorithm": ALGORITHM_VERSION,
                "fragment_sources": [
                    [f["xref"], f["original_sha256"], f["bbox"]] for f in fragments
                ],
            }
            figure_id = hashlib.sha256(_canonical_bytes(identity)).hexdigest()
            visual_task_id = hashlib.sha256(_canonical_bytes({
                "figure_id": figure_id,
                "native_extraction_hash": native_extraction_hash,
                "render_settings": asdict(settings),
            })).hexdigest()
            png_sha = hashlib.sha256(png).hexdigest()
            data = {
                "schema_version": 1,
                "figure_id": figure_id,
                "visual_task_id": visual_task_id,
                "source_pdf_sha256": source_sha256,
                "native_doc_id": doc_id,
                "native_extraction_hash": native_extraction_hash,
                "page_number": page_number,
                "algorithm": ALGORITHM_VERSION,
                "settings": asdict(settings),
                "region_bbox": bounds,
                "caption_bbox": caption["bbox"] if caption else None,
                "caption_native_text_sha256": caption["text_sha256"] if caption else None,
                "render_clip_bbox": list(clip),
                "render_dpi": settings.dpi,
                "source_fragments": fragments,
                "fragment_count": len(fragments),
                "composite_sha256": png_sha,
                "composite_file": visual_task_id + ".png",
                "review_status": "VISUAL_REVIEW_REQUIRED",
                "interpretation_status": "NOT_PERFORMED",
            }
            manifest = _canonical_bytes(data) + b"\n"
            image_path = output_dir / data["composite_file"]
            manifest_path = output_dir / (visual_task_id + ".json")
            _write_immutable(image_path, png)
            _write_immutable(manifest_path, manifest)
            records.append({
                "figure_id": figure_id,
                "visual_task_id": visual_task_id,
                "fragments": len(fragments),
                "png": str(image_path.resolve()),
                "manifest": str(manifest_path.resolve()),
                "png_sha256": png_sha,
                "review_status": data["review_status"],
            })
    return records


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inspect", "prepare"))
    parser.add_argument("--file", required=True)
    parser.add_argument("--page", type=int, required=True)
    parser.add_argument("--profile", help="Native stage profile; used only to tie visual evidence to its fingerprint")
    parser.add_argument("--output-dir", default="benchmark_artifacts/visual_figures")
    parser.add_argument("--min-fragments", type=int, default=2)
    parser.add_argument("--max-fragments", type=int, default=32)
    parser.add_argument("--max-page-images", type=int, default=256)
    parser.add_argument("--max-vertical-gap-pt", type=float, default=1.0)
    parser.add_argument("--alignment-tolerance-pt", type=float, default=1.5)
    parser.add_argument("--pixel-scale-tolerance", type=float, default=0.06)
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--max-render-pixels", type=int, default=25_000_000)
    parser.add_argument("--max-png-bytes", type=int, default=32_000_000)
    args = parser.parse_args(argv)

    settings = FigureSettings(
        min_fragments=args.min_fragments,
        max_fragments=args.max_fragments,
        max_page_images=args.max_page_images,
        max_vertical_gap_pt=args.max_vertical_gap_pt,
        alignment_tolerance_pt=args.alignment_tolerance_pt,
        pixel_scale_tolerance=args.pixel_scale_tolerance,
        dpi=args.dpi,
        max_render_pixels=args.max_render_pixels,
        max_png_bytes=args.max_png_bytes,
    ).validate()

    extraction_hash = ""
    if args.profile:
        from processing_profile import load_profile
        from page_stage import extraction_fingerprint
        profile = load_profile(args.profile)
        extraction_hash = extraction_fingerprint(profile)

    if args.command == "inspect":
        import fitz
        with fitz.open(args.file) as pdf:
            if pdf.is_encrypted or not 1 <= args.page <= pdf.page_count:
                raise ValueError("Encrypted PDF or invalid page")
            groups = find_figure_groups(pdf[args.page - 1], settings)
            answer = {
                "page": args.page,
                "candidate_groups": len(groups),
                "fragments_per_group": [len(g) for g in groups],
                "xrefs": [[f["xref"] for f in g] for g in groups],
                "writes": False,
                "quality_policy": "REVIEW_REQUIRED",
            }
    else:
        results = prepare_page(
            Path(args.file), args.page, Path(args.output_dir),
            settings=settings, native_extraction_hash=extraction_hash,
        )
        answer = {
            "page": args.page,
            "figures_prepared": len(results),
            "figures": results,
            "database_writes": False,
            "native_extraction_fingerprint_changed": False,
        }
    print(json.dumps(answer, indent=2))


if __name__ == "__main__":
    main()
