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


def _verify_page(root, capture_id, number, *, max_pixels=MAX_PIXELS, max_png_bytes=MAX_PNG_BYTES):
    png, manifest = _paths(root, capture_id, number)
    record = json.loads(manifest.read_text(encoding="utf-8"))
    identity = record["identity"]
    if (identity.get("schema") != SCHEMA
            or _capture_id(identity) != capture_id
            or record.get("capture_id") != capture_id
            or record.get("source_page") != number
            or record.get("image_file") != png.name
            or record.get("source_page_count", 0) < number):
        raise ValueError("Page evidence manifest identity mismatch")
    _hash_id(identity["source_pdf_sha256"])
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
    return record


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
    total = 0
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
    return _report(capture_id, first, last, source_count, total, started)


# Operational settings are reloadable between invocations; output identity
# changes only when evidence semantics change, not when concurrency changes.
_DEFAULT_SETTINGS = {
    "dpi": 300, "workers": 1, "batch_pages": 16, "max_in_flight": 2,
    "max_pixels": MAX_PIXELS, "max_png_bytes": MAX_PNG_BYTES,
}
_LIMITS = {
    "dpi": (150, 400), "workers": (1, 32), "batch_pages": (1, MAX_PAGES),
    "max_in_flight": (1, 128), "max_pixels": (100_000, MAX_PIXELS),
    "max_png_bytes": (1024, MAX_PNG_BYTES),
}


def _settings(value):
    if not isinstance(value, dict) or set(value) - set(_DEFAULT_SETTINGS):
        raise ValueError("Unknown visual evidence profile setting")
    result = {**_DEFAULT_SETTINGS, **value}
    for key, (low, high) in _LIMITS.items():
        v = result[key]
        if type(v) is not int or not low <= v <= high:
            raise ValueError(f"Invalid visual evidence setting {key}: expected {low}..{high}")
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


def _capture_batch(source, root, first, last, identity, state, source_count, settings):
    import fitz
    total = rendered = skipped = 0
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
                skipped += 1
                continue
            # An orphan PNG from a crash before manifest publication is replayed
            # and accepted only if its bytes agree with the new rendering.
            page = pdf[number - 1]
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
            if _source_state(source) != state:
                raise RuntimeError("Source changed during page rendering")
            _write_once(png, data)
            _write_once(manifest, _json_bytes(record))
            del pix, data
            total += _verify_page(root, cid, number, max_pixels=max_pixels, max_png_bytes=max_png_bytes)["image_bytes"]
            rendered += 1
    if _source_state(source) != state:
        raise RuntimeError("Source changed during page batch")
    return total, rendered, skipped


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
    total = rendered = skipped = 0
    for size, fresh, prior in _bounded_batches(args_iter, settings):
        total += size
        rendered += fresh
        skipped += prior
    if rendered + skipped != last-first+1:
        raise RuntimeError("Incomplete page batch reconciliation")
    if _source_state(source) != state or digest_file(source) != source_hash:
        raise RuntimeError("Source changed during evidence capture; batch not accepted")
    result = _report(cid, first, last, source_count, total, started)
    result.update(source_pdf_sha256=source_hash, rendered_pages=rendered,
                  skipped_verified_pages=skipped, derivative_writes=rendered > 0,
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
