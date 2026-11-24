"""Opt-in PDF rendered-page evidence pilot, independent of native extraction.

Only policy-authorized PNG derivatives and metadata are stored, never original
PDF bytes. A complete rendered page is sensitive and may itself be prohibited
by the retention agreement. No cloud calls, database writes, OCR or LLM calls.
This pilot never asserts document completeness or purge authorization.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import multiprocessing
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

SCHEMA = "rendered-page-evidence-v1"
MAX_PAGES = 50
MAX_PIXELS = 40_000_000
MAX_PNG_BYTES = 100_000_000
MAX_NATIVE_BYTES = 8_000_000

# Immutable safety ceilings, not deployment tuning defaults. Runtime profiles
# may reduce these budgets but cannot disable integrity or retention checks.


# Keep this derivative-only module deployable without the optional original-
# source archive module. Both use atomic create-if-absent filesystem semantics.
def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_once(destination, data):
    """Publish without overwriting; replay accepts only identical bytes."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=destination.parent, prefix="." + destination.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if (destination.stat().st_size != len(data)
                    or digest_file(destination) != hashlib.sha256(data).hexdigest()):
                raise RuntimeError("Existing derivative evidence differs: " + destination.name)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _hash_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Expected lowercase SHA-256 identity")
    return value


def _validate_range(first, last, count=None):
    if (type(first) is not int or type(last) is not int
            or first < 1 or last < first or last - first >= MAX_PAGES
            or (count is not None and last > count)):
        raise ValueError("Invalid physical page range (maximum 50 pages per call)")


def _capture_id(identity):
    return _sha(_json_bytes(identity))


def _paths(root, capture_id, number):
    _hash_id(capture_id)
    folder = Path(root) / capture_id[:2] / capture_id
    stem = f"{number:06d}"
    return folder / (stem + ".png"), folder / (stem + ".json")


def _source_state(path):
    s = path.stat()
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)



def _raster_path(root, cid, digest):
    _hash_id(digest)
    _, manifest = _paths(root, cid, 1)
    return manifest.parent / "raster" / digest[:2] / (digest + ".bin")


def _store_raster(root, cid, pdf, xref, settings):
    image = pdf.extract_image(xref)
    if not image or not image.get("image"):
        raise ValueError("RASTER_EXTRACT_UNAVAILABLE")
    width, height, data = image["width"], image["height"], image["image"]
    if (width <= 0 or height <= 0 or width*height > settings["max_raster_pixels"]
            or len(data) > settings["max_raster_bytes"]):
        raise ValueError("RASTER_ASSET_LIMIT")
    digest = _sha(data)
    path = _raster_path(root, cid, digest)
    if path.exists():
        if path.stat().st_size != len(data) or digest_file(path) != digest:
            raise RuntimeError("Existing raster evidence checksum mismatch")
    else:
        _write_once(path, data)
    return {"sha256":digest,"bytes":len(data),"width":width,"height":height,
            "format":image["ext"],"xref":xref,"smask_xref":int(image.get("smask") or 0)}


def _capture_rasters(root, cid, page, pdf, settings):
    """Retain extract_image bytes, not a reconstruction of final compositing."""
    info = page.get_image_info(xrefs=True)
    if len(info) > settings["max_raster_occurrences"]:
        return {"reported_occurrences":len(info),"occurrences":[],
                "status":"OCCURRENCE_LIMIT_RENDER_FALLBACK"}
    records, cache = [], {}
    for obj in info:
        xref = int(obj.get("xref") or 0)
        entry = {"xref":xref,"bbox":list(obj["bbox"]),"transform":list(obj["transform"]),
                 "source_width":obj["width"],"source_height":obj["height"]}
        if xref <= 0:
            entry["status"] = "INLINE_OR_UNKNOWN_RENDER_FALLBACK"
        elif obj["width"]*obj["height"] > settings["max_raster_pixels"]:
            entry["status"] = "PIXEL_LIMIT_RENDER_FALLBACK"
        else:
            if xref not in cache:
                try:
                    asset = _store_raster(root,cid,pdf,xref,settings)
                    mask = _store_raster(root,cid,pdf,asset["smask_xref"],settings) if asset["smask_xref"] else None
                    cache[xref] = {"asset":asset,"mask":mask,
                                   "status":"EXTRACTED_WITH_MASK" if mask else "EXTRACTED"}
                except ValueError as exc:
                    if str(exc) not in ("RASTER_EXTRACT_UNAVAILABLE","RASTER_ASSET_LIMIT"):
                        raise
                    cache[xref] = {"status":str(exc)+"_RENDER_FALLBACK"}
            entry.update(cache[xref])
        records.append(entry)
    return {"reported_occurrences":len(info),"occurrences":records,
            "status":"INVENTORY_WITH_EXPLICIT_FALLBACKS"}


def _verify_rasters(root, cid, record):
    inventory = record.get("raster_inventory")
    if not record["identity"].get("raster_capture"):
        if inventory is not None:
            raise ValueError("Unexpected raster inventory")
        return
    if not isinstance(inventory,dict) or _sha(_json_bytes(inventory)) != record.get("raster_inventory_sha256"):
        raise ValueError("Raster inventory checksum mismatch")
    entries, count = inventory["occurrences"], inventory["reported_occurrences"]
    if (type(count) is not int or count < 0 or not isinstance(entries,list)
            or len(entries)>1024
            or (inventory["status"]=="OCCURRENCE_LIMIT_RENDER_FALLBACK" and entries)
            or (inventory["status"]!="OCCURRENCE_LIMIT_RENDER_FALLBACK" and len(entries)!=count)):
        raise ValueError("Raster occurrence reconciliation mismatch")
    checked = {}
    for entry in entries:
        if entry["status"] not in ("EXTRACTED","EXTRACTED_WITH_MASK"):
            if "asset" in entry or "mask" in entry:
                raise ValueError("Unexpected fallback raster asset")
            continue
        asset, mask = entry["asset"], entry["mask"]
        if (asset["xref"]!=entry["xref"] or bool(mask)!=(entry["status"]=="EXTRACTED_WITH_MASK")
                or (mask is None and asset["smask_xref"])
                or (mask is not None and mask["xref"]!=asset["smask_xref"])):
            raise ValueError("Raster mask or placement identity mismatch")
        for item in (asset,mask):
            if item is None:
                continue
            digest,size = item["sha256"],item["bytes"]
            path = _raster_path(root,cid,digest)
            if type(size) is not int or not 0<size<=MAX_PNG_BYTES:
                raise ValueError("Invalid raster asset size")
            if digest not in checked:
                if path.stat().st_size!=size or digest_file(path)!=digest:
                    raise ValueError("Raster asset checksum/size mismatch")
                checked[digest]=size
            elif checked[digest]!=size:
                raise ValueError("Inconsistent raster asset size")


def read_raster(root, capture_id, number, occurrence, *, mask=False, settings=None):
    """Return checked asset bytes and placement. Soft masks are separate."""
    settings = _settings({} if settings is None else settings)
    _full_range(number,number)
    record = _verify_page(root,capture_id,number,max_pixels=settings["max_pixels"],
                          max_png_bytes=settings["max_png_bytes"])
    entries = record.get("raster_inventory",{}).get("occurrences",[])
    if type(occurrence) is not int or not 0<=occurrence<len(entries):
        raise ValueError("Invalid retained raster occurrence index")
    entry = entries[occurrence]
    item = entry.get("mask" if mask else "asset")
    if item is None:
        raise ValueError("NO_RETAINED_RASTER_ASSET: use rendered page fallback")
    if item["bytes"]>settings["max_raster_bytes"]:
        raise ValueError("Raster read byte limit exceeded")
    with _raster_path(root,capture_id,item["sha256"]).open("rb") as stream:
        data=stream.read(item["bytes"]+1)
    if len(data)!=item["bytes"] or _sha(data)!=item["sha256"]:
        raise ValueError("Raster asset checksum/size mismatch")
    return data,{"source_pdf_sha256":record["identity"]["source_pdf_sha256"],
                 "capture_id":capture_id,"source_page":number,"occurrence":occurrence,
                 "bbox":entry["bbox"],"transform":entry["transform"],"asset":item,
                 "is_mask":mask,"original_required":False,"writes":False}



def _vector_inventory(page, settings):
    """Retain renderer-exposed path, clipping and group geometry, not PDF streams.

    get_cdrawings avoids constructing large numbers of Python geometry objects.
    Limits are applied before serializing; a complete rendered page is still
    required for unsupported paint operations and final visual composition.
    """
    try:
        paths = page.get_cdrawings(extended=True)
    except (RuntimeError, ValueError):
        return {"status":"EXTRACTION_ERROR_RENDER_FALLBACK","path_count":None,
                "item_count":None,"paths":[]}
    count = len(paths)
    if count > settings["max_vector_paths"]:
        return {"status":"PATH_LIMIT_RENDER_FALLBACK","path_count":count,
                "item_count":None,"paths":[]}
    items = sum(len(p.get("items", ())) for p in paths)
    if items > settings["max_vector_items"]:
        return {"status":"ITEM_LIMIT_RENDER_FALLBACK","path_count":count,
                "item_count":items,"paths":[]}
    try:
        payload = _json_bytes(paths)
    except (TypeError, ValueError):
        return {"status":"UNSUPPORTED_GEOMETRY_RENDER_FALLBACK","path_count":count,
                "item_count":items,"paths":[]}
    if len(payload) > settings["max_vector_json_bytes"]:
        return {"status":"BYTE_LIMIT_RENDER_FALLBACK","path_count":count,
                "item_count":items,"paths":[]}
    return {"status":"EXTRACTED_RENDERER_PATHS","path_count":count,
            "item_count":items,"paths":paths}


def _verify_vectors(record):
    inventory = record.get("vector_inventory")
    if not record["identity"].get("vector_capture"):
        if inventory is not None:
            raise ValueError("Unexpected vector inventory")
        return
    if (not isinstance(inventory,dict)
            or _sha(_json_bytes(inventory)) != record.get("vector_inventory_sha256")):
        raise ValueError("Vector inventory checksum mismatch")
    paths = inventory["paths"]
    status = inventory["status"]
    if not isinstance(paths,list):
        raise ValueError("Invalid vector path inventory")
    if status == "EXTRACTED_RENDERER_PATHS":
        identity = record["identity"]
        if (len(paths) != inventory["path_count"]
                or len(paths) > identity["max_vector_paths"]
                or sum(len(p.get("items", ())) for p in paths) != inventory["item_count"]
                or inventory["item_count"] > identity["max_vector_items"]
                or len(_json_bytes(paths)) > identity["max_vector_json_bytes"]):
            raise ValueError("Vector inventory reconciliation mismatch")
    elif (status not in ("EXTRACTION_ERROR_RENDER_FALLBACK","PATH_LIMIT_RENDER_FALLBACK",
                        "ITEM_LIMIT_RENDER_FALLBACK","UNSUPPORTED_GEOMETRY_RENDER_FALLBACK",
                        "BYTE_LIMIT_RENDER_FALLBACK") or paths):
        raise ValueError("Invalid vector fallback inventory")


def read_vectors(root, capture_id, number, *, settings=None):
    """Read checked vector path evidence and explicit fallback state, without source."""
    settings = _settings({} if settings is None else settings)
    _full_range(number,number)
    record = _verify_page(root,capture_id,number,max_pixels=settings["max_pixels"],
                          max_png_bytes=settings["max_png_bytes"])
    if "vector_inventory" not in record:
        raise ValueError("NO_RETAINED_VECTOR_INVENTORY")
    return record["vector_inventory"], {
        "capture_id":capture_id,"source_pdf_sha256":record["identity"]["source_pdf_sha256"],
        "source_page":number,"printed_page_label":record["printed_page_label"],
        "vector_inventory_sha256":record["vector_inventory_sha256"],
        "parent_image_sha256":record["image_sha256"],
        "coordinate_space":"unrotated_pymupdf_points",
        "original_required":False,"writes":False,
        "interpretation_status":"NOT_PERFORMED",
    }


def _verify_page(root, capture_id, number, *, max_pixels=MAX_PIXELS, max_png_bytes=MAX_PNG_BYTES):
    png, manifest = _paths(root, capture_id, number)
    record = json.loads(manifest.read_text(encoding="utf-8"))
    identity = record["identity"]
    if (identity.get("schema") != SCHEMA
            or _capture_id(identity) != capture_id
            or record.get("capture_id") != capture_id
            or record.get("source_page") != number
            or record.get("source_page_count", 0) < number):
        raise ValueError("Page evidence manifest identity mismatch")
    _hash_id(identity["source_pdf_sha256"])
    if record.get("representation") == "native_text_only_candidate":
        if (identity.get("selection_mode") != "native_text_or_render_v1"
                or record.get("image_file") is not None
                or record.get("image_bytes") != 0 or png.exists()
                or record.get("native_reconciliation_required") is not True):
            raise ValueError("Invalid native-only evidence manifest")
        native = record["native_page"]
        payload = _json_bytes(native)
        if (len(payload) > MAX_NATIVE_BYTES or not isinstance(native, dict)
                or not native.get("blocks") or _sha(payload) != record.get("native_page_sha256")):
            raise ValueError("Native-only evidence checksum/structure mismatch")
        _verify_rasters(root, capture_id, record)
        _verify_vectors(record)
        return record
    if record.get("image_file") != png.name or record.get("representation") not in (None, "rendered_page"):
        raise ValueError("Invalid rendered evidence manifest")
    _hash_id(record["image_sha256"])
    size = record["image_bytes"]
    if (type(size) is not int or not 0 < size <= max_png_bytes
            or png.stat().st_size != size
            or digest_file(png) != record["image_sha256"]):
        raise ValueError("Page evidence image checksum/size mismatch")
    # Decode the actual stored image, not only the manifest's claimed dimensions.
    import fitz
    pix = fitz.Pixmap(str(png))
    if (pix.width != record["pixel_width"] or pix.height != record["pixel_height"]
            or pix.width * pix.height > max_pixels):
        raise ValueError("Page evidence image geometry mismatch")
    _verify_rasters(root, capture_id, record)
    _verify_vectors(record)
    return record



def read_region(root, capture_id, number, bbox=None, *, settings=None):
    """Return (PNG bytes, provenance) for a verified retained page or region.

    bbox is an optional unrotated PyMuPDF page rectangle in points, NOT raw PDF
    bottom-left coordinates. No resampling, source access or persistent writes.
    The caller supplies a region; this is not automatic figure segmentation.
    """
    import fitz
    import math
    settings = _settings({} if settings is None else settings)
    _full_range(number, number)
    record = _verify_page(root, capture_id, number,
                          max_pixels=settings["max_pixels"],
                          max_png_bytes=settings["max_png_bytes"])
    if record.get("representation") == "native_text_only_candidate":
        raise ValueError("NO_RETAINED_PIXELS: native-only page")
    png, _ = _paths(root, capture_id, number)
    # Verify the exact byte buffer that is returned/decoded; no trust in a
    # previous path lookup if the filesystem changes between reads.
    with png.open("rb") as stream:
        data = stream.read(settings["max_png_bytes"] + 1)
    if len(data) != record["image_bytes"] or _sha(data) != record["image_sha256"]:
        raise ValueError("Page evidence image checksum/size mismatch")
    dpi = record["identity"]["dpi"]
    width, height = record["pixel_width"], record["pixel_height"]
    requested = None
    if bbox is None:
        pixel_box = [0, 0, width, height]
        output = data
    else:
        if (not isinstance(bbox, (list, tuple)) or len(bbox) != 4
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in bbox)):
            raise ValueError("Expected four finite page coordinates")
        requested = list(bbox)
        box = fitz.Rect(bbox)
        if box.is_empty or box.is_infinite:
            raise ValueError("Invalid region rectangle")
        rotation = fitz.Matrix(*record["rotation_matrix"])
        page_rect = fitz.Rect(record["page_rect"])
        rotated = box * rotation
        if not page_rect.contains(rotated):
            raise ValueError("Region outside retained page; refusing to truncate")
        scale = fitz.Matrix(dpi / 72, dpi / 72)
        origin = (page_rect * scale).irect
        area = (rotated * scale).irect
        x0, y0, x1, y1 = (area.x0-origin.x0, area.y0-origin.y0,
                          area.x1-origin.x0, area.y1-origin.y0)
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            raise ValueError("Region outside retained pixel bounds")
        pixel_box = [x0, y0, x1, y1]
        w, h = x1-x0, y1-y0
        if w*h > settings["max_pixels"]:
            raise ValueError("Region pixel limit exceeded")
        pix = fitz.Pixmap(data)
        if pix.n != 3 or pix.alpha:
            raise ValueError("Expected retained RGB image")
        samples = pix.samples_mv
        stride = pix.stride
        cropped = b"".join(samples[y*stride+x0*3:y*stride+x1*3]
                           for y in range(y0,y1))
        result = fitz.Pixmap(fitz.csRGB, w, h, cropped, False)
        result.set_dpi(dpi, dpi)
        output = result.tobytes("png")
    if len(output) > settings["max_png_bytes"]:
        raise ValueError("Region PNG byte limit exceeded")
    return output, {
        "capture_id": capture_id, "source_pdf_sha256": record["identity"]["source_pdf_sha256"],
        "source_page": number, "printed_page_label": record["printed_page_label"],
        "parent_image_sha256": record["image_sha256"], "region_sha256": _sha(output),
        "requested_bbox_pt": requested, "pixel_bbox": pixel_box, "dpi": dpi,
        "coordinate_space": "unrotated_pymupdf_points",
        "quality_policy": "SOURCE_ONLY_UNINTERPRETED",
        "original_required": False, "writes": False,
    }


def _report(capture_id, first, last, source_count, total, started):
    return {
        "capture_id": capture_id, "source_pages": source_count,
        "first_page": first, "last_page": last,
        "verified_pages": last - first + 1, "image_bytes": total,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "local_content_verified": True, "durable_storage_verified": False,
        "original_required_for_verification": False,
        "original_copied": False, "database_writes": False,
        "cloud_calls": False, "purge_safe": False,
        "limitations": ["Rendered pixels only; not original raster/vector objects",
                        "Native text/tables, visual detail and production durability require separate gates"],
    }


def verify(root, capture_id, first, last, *, settings=None):
    """Verify a bounded range without opening or requiring the source PDF."""
    started = time.monotonic()
    if settings is None:
        _validate_range(first, last)
        settings = _settings({})
    else:
        settings = _settings(settings)
        _full_range(first, last)
    total = native_only = 0
    source_count = None
    for number in range(first, last + 1):
        row = _verify_page(root, capture_id, number, max_pixels=settings['max_pixels'],
                           max_png_bytes=settings['max_png_bytes'])
        if source_count is None:
            source_count = row["source_page_count"]
        elif source_count != row["source_page_count"]:
            raise ValueError("Inconsistent source page count")
        _full_range(first, last, source_count)
        total += row["image_bytes"]
        native_only += row.get("representation") == "native_text_only_candidate"
    result = _report(capture_id, first, last, source_count, total, started)
    result.update(native_text_only_pages=native_only, verified_image_pages=last-first+1-native_only,
                  native_reconciliation_required=bool(native_only))
    return result


# Operational settings are reloadable between invocations; output identity
# changes only when evidence semantics change, not when concurrency changes.
_DEFAULT_SETTINGS = {
    "dpi": 300, "workers": 1, "batch_pages": 16, "max_in_flight": 2,
    "max_pixels": MAX_PIXELS, "max_png_bytes": MAX_PNG_BYTES,
    "selection_mode": "all_pages", "min_native_chars": 100,
    "max_native_json_bytes": MAX_NATIVE_BYTES,
    "capture_rasters": False, "max_raster_occurrences": 256,
    "max_raster_pixels": MAX_PIXELS, "max_raster_bytes": MAX_PNG_BYTES,
    "capture_vectors": False, "max_vector_paths": 20000,
    "max_vector_items": 100000, "max_vector_json_bytes": 8000000,
}
_LIMITS = {
    "dpi": (150, 400), "workers": (1, 32), "batch_pages": (1, MAX_PAGES),
    "max_in_flight": (1, 128), "max_pixels": (100_000, MAX_PIXELS),
    "max_png_bytes": (1024, MAX_PNG_BYTES),
    "min_native_chars": (1, 1_000_000), "max_native_json_bytes": (1024, MAX_NATIVE_BYTES),
    "max_raster_occurrences": (1, 1024),
    "max_raster_pixels": (1, MAX_PIXELS), "max_raster_bytes": (1024, MAX_PNG_BYTES),
    "max_vector_paths": (1, 50000), "max_vector_items": (1, 500000),
    "max_vector_json_bytes": (1024, 16000000),
}


def _settings(value):
    if not isinstance(value, dict) or set(value) - set(_DEFAULT_SETTINGS):
        raise ValueError("Unknown visual evidence profile setting")
    result = {**_DEFAULT_SETTINGS, **value}
    for key, (low, high) in _LIMITS.items():
        v = result[key]
        if type(v) is not int or not low <= v <= high:
            raise ValueError(f"Invalid visual evidence setting {key}: expected {low}..{high}")
    if type(result["capture_rasters"]) is not bool:
        raise ValueError("capture_rasters must be boolean")
    if type(result["capture_vectors"]) is not bool:
        raise ValueError("capture_vectors must be boolean")
    if (result["capture_rasters"] or result["capture_vectors"]) and result["selection_mode"] != "all_pages":
        raise ValueError("Raster/vector pilot requires all_pages to retain compositing fallback")
    if result["selection_mode"] not in ("all_pages", "native_text_or_render"):
        raise ValueError("Unsupported visual evidence selection_mode")
    if result["max_in_flight"] < result["workers"]:
        raise ValueError("max_in_flight must be at least workers")
    return result


def load_visual_profile(path):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or set(value) != {"version", "visual_evidence"}:
        raise ValueError("Expected versioned visual evidence profile")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("Unsupported visual evidence profile version")
    return _settings(value["visual_evidence"])


def _full_range(first, last, count=None):
    if (type(first) is not int or type(last) is not int or first < 1
            or last < first or (count is not None and last > count)):
        raise ValueError("Invalid physical page range")


def _native_only_payload(page, settings):
    """Conservative candidate, not a proof of semantic or table completeness.

    Non-text paint operations (including invisible OCR text, paths, shadings,
    images, inline images), annotations, links, widgets, Type3 glyphs, rotated
    layouts or missing/unreadable native text force a rendered page fallback.
    Native text-only candidates retain text/style/geometry, not just a hash.
    """
    import fitz
    if page.rotation or page.first_annot or page.first_widget or page.first_link:
        return None
    operations = page.get_bboxlog()
    if not operations or any(op[0] != "fill-text" for op in operations):
        return None
    if any(len(font) < 3 or font[2] == "Type3" for font in page.get_fonts()):
        return None
    native = page.get_text("dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)
    blocks = native.get("blocks", [])
    if not blocks or any(block.get("type") != 0 for block in blocks):
        return None
    count = 0
    for block in blocks:
        for line in block.get("lines", []):
            if tuple(line.get("dir", ())) != (1.0, 0.0) or line.get("wmode") != 0:
                return None
            for span in line.get("spans", []):
                value = span.get("text", "")
                if ("\ufffd" in value or any(ord(c) < 32 and c not in "\t\n\r" for c in value)
                        or span.get("alpha", 255) != 255):
                    return None
                count += len(value.strip())
    if count < settings["min_native_chars"]:
        return None
    try:
        payload = _json_bytes(native)
    except (TypeError, ValueError):
        return None
    if len(payload) > settings["max_native_json_bytes"]:
        return None
    return native, _sha(payload)


def _capture_batch(source, root, first, last, identity, state, source_count, settings):
    import fitz
    total = rendered = skipped = native_new = native_total = 0
    dpi = identity['dpi']
    cid = _capture_id(identity)
    max_pixels, max_png_bytes = settings['max_pixels'], settings['max_png_bytes']
    with fitz.open(source) as pdf:
        if pdf.is_encrypted:
            raise ValueError("Encrypted source PDF")
        if pdf.page_count != source_count or _source_state(source) != state:
            raise RuntimeError('Source version changed before page batch')
        _validate_range(first, last, source_count)
        for number in range(first, last + 1):
            if _source_state(source) != state:
                raise RuntimeError("Source changed during evidence capture")
            png, manifest = _paths(root, cid, number)
            # A published manifest is a commit marker, never a mere existence
            # check. Verify actual stored bytes before skipping a completed page.
            if manifest.exists():
                record = _verify_page(root, cid, number, max_pixels=max_pixels, max_png_bytes=max_png_bytes)
                if record["source_page_count"] != source_count:
                    raise ValueError("Existing derivative source page count mismatch")
                total += record["image_bytes"]
                native_total += record.get("representation") == "native_text_only_candidate"
                skipped += 1
                continue
            # An orphan PNG from a crash before manifest publication is replayed
            # and accepted only if its bytes agree with the new rendering.
            page = pdf[number - 1]
            if settings["selection_mode"] == "native_text_or_render":
                # Any detector exception must keep the conservative render fallback.
                try:
                    candidate = _native_only_payload(page, settings)
                except Exception:
                    candidate = None
                if candidate is not None:
                    if png.exists():
                        raise ValueError("Unexpected orphan PNG for native-only candidate")
                    native, native_hash = candidate
                    record = {
                        "identity": identity, "capture_id": cid,
                        "source_page": number, "source_page_count": source_count,
                        "printed_page_label": page.get_label(), "page_rotation": page.rotation,
                        "page_rect": list(page.rect), "page_cropbox": list(page.cropbox),
                        "page_mediabox": list(page.mediabox),
                        "rotation_matrix": list(page.rotation_matrix),
                        "representation": "native_text_only_candidate",
                        "native_page": native, "native_page_sha256": native_hash,
                        "native_reconciliation_required": True,
                        "image_file": None, "image_bytes": 0,
                        "interpretation_status": "NOT_PERFORMED",
                    }
                    if _source_state(source) != state:
                        raise RuntimeError("Source changed during native evidence capture")
                    _write_once(manifest, _json_bytes(record))
                    _verify_page(root, cid, number, max_pixels=max_pixels, max_png_bytes=max_png_bytes)
                    native_new += 1
                    native_total += 1
                    continue
            predicted = (page.rect * fitz.Matrix(dpi / 72, dpi / 72)).irect
            if (predicted.width <= 0 or predicted.height <= 0
                    or predicted.width * predicted.height > max_pixels):
                raise ValueError(f"PAGE_RENDER_PIXEL_LIMIT: physical page {number}")
            pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB,
                                  alpha=False, annots=True)
            if pix.width * pix.height > max_pixels:
                raise ValueError(f"PAGE_RENDER_PIXEL_LIMIT: physical page {number}")
            data = pix.tobytes("png")
            if not 0 < len(data) <= max_png_bytes:
                raise ValueError(f"PAGE_RENDER_BYTE_LIMIT: physical page {number}")
            record = {
                "identity": identity, "capture_id": cid,
                "source_page": number, "source_page_count": pdf.page_count,
                "printed_page_label": page.get_label(), "page_rotation": page.rotation,
                "page_rect": list(page.rect), "page_cropbox": list(page.cropbox),
                "page_mediabox": list(page.mediabox),
                "rotation_matrix": list(page.rotation_matrix),
                "pixel_width": pix.width, "pixel_height": pix.height,
                "image_file": png.name, "image_bytes": len(data),
                "image_sha256": _sha(data),
                "interpretation_status": "NOT_PERFORMED",
            }
            del pix
            if settings["capture_rasters"]:
                inventory = _capture_rasters(root, cid, page, pdf, settings)
                record.update(raster_inventory=inventory,
                              raster_inventory_sha256=_sha(_json_bytes(inventory)))
            if settings["capture_vectors"]:
                vectors = _vector_inventory(page, settings)
                record.update(vector_inventory=vectors,
                              vector_inventory_sha256=_sha(_json_bytes(vectors)))
            if _source_state(source) != state:
                raise RuntimeError("Source changed during page rendering")
            _write_once(png, data)
            _write_once(manifest, _json_bytes(record))
            del data
            total += _verify_page(root, cid, number, max_pixels=max_pixels, max_png_bytes=max_png_bytes)["image_bytes"]
            rendered += 1
    if _source_state(source) != state:
        raise RuntimeError("Source changed during page batch")
    return total, rendered, skipped, native_new, native_total


def _bounded_batches(args_iter, settings):
    """Bound outstanding tasks, keep PNG data out of IPC, and propagate failure."""
    if settings["workers"] == 1:
        for args in args_iter:
            yield _capture_batch(*args)
        return
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=settings["workers"], mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        pending = set()
        iterator = iter(args_iter)
        try:
            while True:
                while len(pending) < settings["max_in_flight"]:
                    args = next(iterator, None)
                    if args is None:
                        break
                    pending.add(pool.submit(_capture_batch, *args))
                if not pending:
                    break
                done, pending = concurrent.futures.wait(
                    pending, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    yield future.result()
        finally:
            for future in pending:
                future.cancel()


def capture(source, root, first, last, *, dpi=None, policy_id="",
            authorized_page_derivatives=False, settings=None):
    """One source hash before/after; independently opened bounded page batches."""
    if authorized_page_derivatives is not True:
        raise PermissionError("Explicit approval for full-page PNG derivatives is required")
    if not isinstance(policy_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", policy_id):
        raise ValueError("Explicit retention policy identifier required")
    if settings is None:
        _validate_range(first, last)
        settings = _settings({} if dpi is None else {"dpi": dpi})
    else:
        if dpi is not None:
            raise ValueError("Set DPI in the profile, not both profile and CLI")
        settings = _settings(settings)
        _full_range(first, last)
    source = Path(source).resolve(strict=True)
    root = Path(root).resolve()
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise ValueError("Expected an existing local PDF")
    import fitz
    started = time.monotonic()
    state = _source_state(source)
    source_hash = digest_file(source)
    if _source_state(source) != state:
        raise RuntimeError("Source changed during hashing")
    identity = {
        "schema": SCHEMA, "source_pdf_sha256": source_hash,
        "dpi": settings["dpi"], "policy_id": policy_id, "renderer": str(fitz.VersionBind),
        "colorspace": "RGB", "alpha": False, "annotations": True,
    }
    if settings["selection_mode"] == "native_text_or_render":
        identity.update(selection_mode="native_text_or_render_v1",
                        min_native_chars=settings["min_native_chars"],
                        max_native_json_bytes=settings["max_native_json_bytes"])
    if settings["capture_rasters"]:
        identity.update(raster_capture="xref_extracted_bytes_v1",
                        max_raster_occurrences=settings["max_raster_occurrences"],
                        max_raster_pixels=settings["max_raster_pixels"],
                        max_raster_bytes=settings["max_raster_bytes"])
    if settings["capture_vectors"]:
        identity.update(vector_capture="extended_cdrawings_v1",
                        max_vector_paths=settings["max_vector_paths"],
                        max_vector_items=settings["max_vector_items"],
                        max_vector_json_bytes=settings["max_vector_json_bytes"])
    cid = _capture_id(identity)
    with fitz.open(source) as pdf:
        if pdf.is_encrypted:
            raise ValueError("Encrypted source PDF")
        source_count = pdf.page_count
    _full_range(first, last, source_count)
    batch = settings["batch_pages"]
    args_iter = (
        (source, root, start, min(last, start+batch-1), identity, state, source_count, settings)
        for start in range(first,last+1,batch)
    )
    total = rendered = skipped = native_new = native_total = 0
    for size, fresh, prior, native_fresh, native_all in _bounded_batches(args_iter, settings):
        total += size
        rendered += fresh
        skipped += prior
        native_new += native_fresh
        native_total += native_all
    if rendered + skipped + native_new != last-first+1:
        raise RuntimeError("Incomplete page batch reconciliation")
    if _source_state(source) != state or digest_file(source) != source_hash:
        raise RuntimeError("Source changed during evidence capture; batch not accepted")
    result = _report(cid, first, last, source_count, total, started)
    result.update(source_pdf_sha256=source_hash, rendered_pages=rendered,
                  skipped_verified_pages=skipped, derivative_writes=rendered + native_new > 0,
                  native_text_only_pages=native_total, new_native_text_only_pages=native_new,
                  verified_image_pages=last-first+1-native_total,
                  native_reconciliation_required=bool(native_total),
                  workers=settings["workers"], batch_pages=batch,
                  max_in_flight=settings["max_in_flight"])
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("capture", "verify"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--file")
    parser.add_argument("--capture-id")
    parser.add_argument("--page", type=int, required=True)
    parser.add_argument("--end-page", type=int)
    parser.add_argument("--dpi", type=int)
    parser.add_argument("--profile", help="Versioned JSON visual evidence runtime profile")
    parser.add_argument("--policy-id", default="")
    parser.add_argument("--authorized-page-derivatives", action="store_true")
    args = parser.parse_args(argv)
    last = args.page if args.end_page is None else args.end_page
    settings = load_visual_profile(args.profile) if args.profile else None
    if args.action == "capture":
        if not args.file or args.capture_id or not args.authorized_page_derivatives:
            parser.error("capture requires --file, --policy-id and explicit --authorized-page-derivatives")
        result = capture(args.file, args.root, args.page, last, dpi=args.dpi,
                         policy_id=args.policy_id,
                         authorized_page_derivatives=args.authorized_page_derivatives, settings=settings)
    else:
        if not args.capture_id or args.file or args.authorized_page_derivatives:
            parser.error("verify requires --capture-id and does not access the source")
        if args.dpi is not None:
            parser.error("verify does not accept --dpi")
        result = verify(args.root, args.capture_id, args.page, last, settings=settings)
        result["derivative_writes"] = False
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
