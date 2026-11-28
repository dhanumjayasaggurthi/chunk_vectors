"""Explicitly reviewed embedded-image assets, keyed by original PDF image bytes.

Do not confuse the source image hash with a rendered PDF crop hash.
Manifest changes are semantic and MUST change the extraction fingerprint.
Unapproved and unknown images continue through normal OCR/review.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

_HEX = re.compile(r"^[0-9a-f]{64}$")
_KINDS = {"logo", "seal", "repeated_header"}
_STATES = {"pending", "approved"}
_MAX_FILE_BYTES = 128 * 1024
_MAX_ASSETS = 1000


def read_approved_image_manifest(path: str | None) -> tuple[dict[str, dict], str]:
    """Load a bounded, strict, auditable approval registry.

    Returns (approved SHA->rule, semantic content digest). Disabled manifests
    produce a stable sentinel. Approval is opt-in per SHA, never by dimensions
    or blanket OCR-disabled settings. Every record is validated even if pending.
    """
    if not path:
        return {}, "disabled"
    filename = Path(path).expanduser().resolve(strict=True)
    if not filename.is_file() or filename.stat().st_size > _MAX_FILE_BYTES:
        raise ValueError("Approved image manifest missing or oversized")
    raw = filename.read_text(encoding="utf-8")
    if filename.suffix.lower() in (".yaml", ".yml"):
        import yaml
        class UniqueLoader(yaml.SafeLoader):
            pass
        def unique_mapping(loader, node):
            loader.flatten_mapping(node)
            result = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node, deep=True)
                if not isinstance(key, str) or key in result:
                    raise ValueError("Duplicate/invalid image manifest mapping key")
                result[key] = loader.construct_object(value_node, deep=True)
            return result
        UniqueLoader.add_constructor(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping
        )
        doc = yaml.load(raw, Loader=UniqueLoader)
    elif filename.suffix.lower() == ".json":
        def unique_pairs(pairs):
            obj = {}
            for key, value in pairs:
                if key in obj:
                    raise ValueError("Duplicate image manifest key")
                obj[key] = value
            return obj
        doc = json.loads(raw, object_pairs_hook=unique_pairs)
    else:
        raise ValueError("Image manifest must be .yml, .yaml or .json")
    if not isinstance(doc, dict) or set(doc) != {"version", "assets"} or doc["version"] != 1:
        raise ValueError("Unsupported image approval manifest schema")
    rules = doc["assets"]
    if not isinstance(rules, list) or len(rules) > _MAX_ASSETS:
        raise ValueError("Image manifest assets must be a bounded list")
    seen = set()
    approved = {}
    normalized = []
    required = {"sha256", "status", "kind", "text", "reviewed_by", "approval_reference"}
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != required:
            raise ValueError("Invalid approved image asset fields")
        digest = rule["sha256"]
        if not isinstance(digest, str) or not _HEX.fullmatch(digest):
            raise ValueError("Invalid SHA-256 in image manifest")
        if digest in seen:
            raise ValueError("Duplicate image SHA-256 in approval manifest")
        seen.add(digest)
        if rule["status"] not in _STATES or rule["kind"] not in _KINDS:
            raise ValueError("Unknown image approval status or kind")
        for key, cap in (("text", 512), ("reviewed_by", 128),
                         ("approval_reference", 256)):
            value = rule[key]
            if not isinstance(value, str) or len(value) > cap:
                raise ValueError("Invalid image approval field: " + key)
        if rule["status"] == "approved":
            if not rule["reviewed_by"].strip() or not rule["approval_reference"].strip() or not rule["text"].strip():
                raise ValueError("Approved image must have reviewer, reference and text")
            approved[digest] = dict(rule)
        normalized.append(rule)
    canonical = json.dumps({"version": 1, "assets": normalized},
                           sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
    semantic_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return approved, semantic_hash


def approved_source_image(doc, xref: int, image_info, rules: dict,
                          cache: dict[int, tuple[str, int]]):
    """Match original embedded-image bytes, not a page rasterization.

    Masked images are deliberately excluded: source RGB bytes alone do not
    identify the composited visual appearance. Never accepts a document
    reference with an invalid or missing source image.
    """
    if not rules or not isinstance(xref, int) or xref <= 0:
        return None
    if len(image_info) > 1 and image_info[1]:
        return None
    if xref not in cache:
        original = doc.extract_image(xref)
        if not isinstance(original, dict) or not isinstance(original.get("image"), bytes):
            raise ValueError("Cannot inspect original image bytes")
        image = original["image"]
        cache[xref] = (hashlib.sha256(image).hexdigest(), len(image))
        if len(cache) > 4096:
            cache.clear()
            cache[xref] = (hashlib.sha256(image).hexdigest(), len(image))
    digest, length = cache[xref]
    rule = rules.get(digest)
    if not rule:
        return None
    return {
        "source_sha256": digest,
        "source_xref": xref,
        "source_bytes": length,
        "kind": rule["kind"],
        "text": rule["text"],
        "reviewed_by": rule["reviewed_by"],
        "approval_reference": rule["approval_reference"],
        "approval_status": "approved",
    }
