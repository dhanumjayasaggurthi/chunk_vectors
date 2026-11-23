"""Validated, optional per-run processing profiles. No third-party configuration dependency."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

_ALLOWED_METHODS = {"label", "regex", "constant"}
_DEFAULTS = {
    "version": 1,
    "chunking": {
        "strategy": "legacy", "min_tokens": 200, "max_tokens": 800,
        "overlap_tokens": 100, "small_section_policy": "merge_forward",
        "preserve_headers": True,
    },
    "metadata": {"document": {}, "chunk": {}, "inherit_document_fields": True},
    "embedding": {"enabled": True, "reuse_existing": True, "batch_size": 16},
    "output": {},
    "existing_chunks": {},
    "extracted_content": {},
}


def load_profile(path: str | None, overrides: list[str] | None = None) -> dict | None:
    """Return None when unused, preserving the production legacy behavior."""
    if path is None and not overrides:
        return None
    data = {}
    if path:
        p = Path(path)
        if p.stat().st_size > 128 * 1024:
            raise ValueError("Profile exceeds 128 KiB")
        content = p.read_text(encoding="utf-8")
        if p.suffix.lower() in (".yml", ".yaml"):
            try:
                import yaml
            except ImportError as exc:
                raise ValueError("Install PyYAML to use YAML intake profiles") from exc
            class UniqueLoader(yaml.SafeLoader):
                pass
            def unique_mapping(loader, node):
                loader.flatten_mapping(node)
                result = {}
                for k, v in node.value:
                    key = loader.construct_object(k, deep=True)
                    if not isinstance(key, str) or key in result:
                        raise ValueError("Invalid or duplicate YAML mapping key")
                    result[key] = loader.construct_object(v, deep=True)
                return result
            UniqueLoader.add_constructor(
                yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)
            data = yaml.load(content, Loader=UniqueLoader)
        else:
            data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("Profile root must be an object")
    result = copy.deepcopy(_DEFAULTS)
    for key, value in data.items():
        if key not in result and key not in {"name", "extraction"}:
            raise ValueError("Unknown profile setting: " + key)
        if isinstance(result.get(key), dict):
            if not isinstance(value, dict):
                raise ValueError(key + " must be an object")
            result[key].update(value)
        else:
            result[key] = value
    for item in overrides or []:
        key, separator, value = item.partition("=")
        if not separator or key not in {
            "chunking.strategy", "chunking.min_tokens", "chunking.max_tokens",
            "chunking.overlap_tokens", "embedding.enabled", "embedding.batch_size",
            "embedding.dimensions", "embedding.deployment", "embedding.model",
            "existing_chunks.schema", "existing_chunks.table",
            "existing_chunks.id_column", "existing_chunks.text_column",
            "existing_chunks.vector_column", "existing_chunks.note_column",
            "embedding.progress_every_batches",
            "embedding.batch_tokens", "embedding.rpm", "embedding.tpm",
            "embedding.max_attempts", "embedding.timeout_seconds",
            "embedding.lease_seconds", "embedding.enqueue_page_size",
            "extracted_content.schema", "extracted_content.table",
            "extracted_content.doc_id_column", "extracted_content.text_column",
            "extracted_content.page_column", "extracted_content.position_column",
            "extracted_content.file_path_column", "extracted_content.file_name_column",
            "extracted_content.element_json_column",
        }:
            raise ValueError("Invalid --set override: " + item)
        section, field = key.split(".", 1)
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
        result[section][field] = parsed
    validate_profile(result)
    canonical = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    result["_fingerprint"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return result


def validate_profile(profile: dict) -> None:
    if type(profile.get("version")) is not int or profile["version"] != 1:
        raise ValueError("Only version 1 profiles are supported")
    allowed_nested = {
        "chunking": {"strategy", "min_tokens", "max_tokens", "overlap_tokens",
                     "small_section_policy", "preserve_headers", "tokenizer"},
        "embedding": {"enabled", "reuse_existing", "batch_size",
                      "dimensions", "deployment", "model", "progress_every_batches",
                      "batch_tokens", "rpm", "tpm", "max_attempts",
                      "timeout_seconds", "lease_seconds", "enqueue_page_size",
                      "input_fields", "input_max_tokens"},
        "metadata": {"document", "chunk", "inherit_document_fields"},
        "existing_chunks": {"schema", "table", "id_column", "text_column",
                            "vector_column", "note_column"},
        "extracted_content": {"schema", "table", "doc_id_column", "text_column",
                              "page_column", "position_column", "file_path_column",
                              "file_name_column", "element_json_column", "width_column",
                              "height_column", "rotation_column", "section_column",
                              "page_end_column", "source_columns"},
        "output": {"schema", "table"},
    }
    if "extraction" in profile:
        allowed_nested["extraction"] = {
            "enabled", "schema", "table",
            "page_batch_size", "ocr_batch_images", "ocr_workers",
            "ocr_requests_rpm", "ocr_images_ipm",
            "ocr_max_batch_bytes", "ocr_max_retries", "ocr_timeout_seconds",
            "ocr_bbox_detail", "ocr_confidence_min",
            "chart_descriptions_enabled", "table_summaries_enabled",
            "max_quota_wait_seconds", "ocr_enabled",
            "max_window_raster_bytes", "max_render_pixels",
            "chart_workers", "capture_rich_text",
            "publish_unreviewed_vector_regions", "approved_image_manifest",
        }
    for section, allowed in allowed_nested.items():
        unexpected = set(profile[section]) - allowed
        if unexpected:
            raise ValueError("Unknown " + section + " options: " + ", ".join(sorted(unexpected)))
    chunking = profile["chunking"]
    if chunking.get("tokenizer", "cl100k_base") not in {"cl100k_base", "o200k_base"}:
        raise ValueError("Unsupported tokenizer")
    if chunking.get("strategy") not in {"legacy", "toc_then_semantic"}:
        raise ValueError("chunking.strategy must be legacy or toc_then_semantic")
    for name in ("min_tokens", "max_tokens", "overlap_tokens"):
        if type(chunking.get(name)) is not int or chunking[name] < 0:
            raise ValueError("Invalid chunking." + name)
    minimum, maximum, overlap = (chunking[k] for k in
                                 ("min_tokens", "max_tokens", "overlap_tokens"))
    if not 16 <= maximum <= 6000 or minimum > maximum or overlap >= maximum // 2:
        raise ValueError("Invalid chunk size or overlap limits")
    if chunking.get("small_section_policy") not in {"merge_forward", "keep"}:
        raise ValueError("Unsupported small_section_policy")
    if type(chunking.get("preserve_headers")) is not bool:
        raise ValueError("preserve_headers must be boolean")
    emb = profile["embedding"]
    if type(emb.get("enabled")) is not bool or type(emb.get("reuse_existing")) is not bool:
        raise ValueError("embedding flags must be boolean")
    if type(emb.get("batch_size")) is not int or not 1 <= emb["batch_size"] <= 2048:
        raise ValueError("embedding.batch_size must be between 1 and 2048")
    bounds = {
        "batch_tokens": (1, 300000), "rpm": (0, 10000000),
        "tpm": (0, 1000000000), "max_attempts": (1, 20),
        "timeout_seconds": (5, 600), "lease_seconds": (40, 3600),
        "enqueue_page_size": (100, 10000),
    }
    for key, (low, high) in bounds.items():
        if key in emb and (type(emb[key]) is not int or not low <= emb[key] <= high):
            raise ValueError(f"Invalid embedding.{key}; expected integer {low}..{high}")
    if emb.get("lease_seconds", 300) <= emb.get("timeout_seconds", 90) + 30:
        raise ValueError("embedding.lease_seconds must exceed timeout_seconds by at least 30s")
    if "dimensions" in emb and (
        type(emb["dimensions"]) is not int or not 1 <= emb["dimensions"] <= 16000
    ):
        raise ValueError("embedding.dimensions must be an integer between 1 and 16000")
    for field in ("deployment", "model"):
        if field in emb and (
            not isinstance(emb[field], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", emb[field])
        ):
            raise ValueError("Invalid embedding." + field)
    if "progress_every_batches" in emb and (
        type(emb["progress_every_batches"]) is not int
        or not 1 <= emb["progress_every_batches"] <= 10000
    ):
        raise ValueError("embedding.progress_every_batches must be a positive integer")
    if "input_fields" in emb:
        fields = emb["input_fields"]
        valid_field = re.compile(
            r"^(chunk_text|section_title|file_name|file_path|"
            r"(?:document|chunk|source)\.[a-z][a-z0-9_]{0,63})$"
        )
        if (not isinstance(fields, list) or not 1 <= len(fields) <= 32
                or not all(isinstance(f, str) and valid_field.fullmatch(f) for f in fields)
                or len(set(fields)) != len(fields)
                or "chunk_text" not in fields):
            raise ValueError("embedding.input_fields must include chunk_text and valid, unique fields")
    if "input_max_tokens" in emb:
        if type(emb["input_max_tokens"]) is not int or not 100 <= emb["input_max_tokens"] <= 8000:
            raise ValueError("embedding.input_max_tokens must be 100..8000")
    output = profile["output"]
    if output:
        if not output.get("schema") or not output.get("table"):
            raise ValueError("output.schema and output.table are required together")
        for value in (output["schema"], output["table"]):
            if not isinstance(value, str) or not re.fullmatch(r"[a-z_][a-z0-9_]{0,49}", value, re.I):
                raise ValueError("Invalid output table identifier")
    source = profile["extracted_content"]
    if "source_columns" in source:
        mapping = source["source_columns"]
        if not isinstance(mapping, dict) or len(mapping) > 50:
            raise ValueError("extracted_content.source_columns must be a mapping of <=50 fields")
        for key, col in mapping.items():
            if (not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key)
                    or not isinstance(col, str)
                    or not re.fullmatch(r"[a-z_][a-z0-9_]*", col, re.I)):
                raise ValueError("Invalid source_columns mapping")
    if "extraction" in profile:
        extract = profile["extraction"]
        if not isinstance(extract, dict):
            raise ValueError("extraction must be a mapping")
        for required in ("schema", "table"):
            value = extract.get(required)
            if not isinstance(value, str) or not re.fullmatch(
                r"[a-z_][a-z0-9_]{0,45}",value,re.I,
            ):
                raise ValueError("extraction." + required + " requires a safe table identifier")
        for field in ("enabled", "ocr_enabled", "capture_rich_text",
                      "publish_unreviewed_vector_regions", "chart_descriptions_enabled",
                      "table_summaries_enabled"):
            if field in extract and type(extract[field]) is not bool:
                raise ValueError("extraction." + field + " must be boolean")
        if "approved_image_manifest" in extract:
            path=extract["approved_image_manifest"]
            if (not isinstance(path,str) or len(path)>512
                    or (path and not path.lower().endswith((".yaml",".yml",".json")))):
                raise ValueError("Invalid extraction.approved_image_manifest")
        limits = {
            "page_batch_size":(1,128), "ocr_batch_images":(1,16),
            "ocr_workers":(1,32), "chart_workers":(1,16),
            "ocr_requests_rpm":(1,100000),
            "ocr_images_ipm":(16,100000),
            "ocr_max_batch_bytes":(100000,8500000),
            "ocr_max_retries":(1,10), "ocr_timeout_seconds":(5,600),
            "max_quota_wait_seconds":(1,3600),
            "max_window_raster_bytes":(10_000_000,1_000_000_000),
            "max_render_pixels":(1_000_000,75_000_000),
        }
        for field,(low,high) in limits.items():
            if field in extract and (
                type(extract[field]) is not int or
                not low <= extract[field] <= high
            ):
                raise ValueError(f"extraction.{field} must be {low}..{high}")
        if extract.get("ocr_bbox_detail","word") not in ("word","block"):
            raise ValueError("extraction.ocr_bbox_detail must be word or block")
        threshold = extract.get("ocr_confidence_min",0.6)
        if type(threshold) not in (float,int) or not 0.0 <= threshold <= 1.0:
            raise ValueError("extraction.ocr_confidence_min must be 0..1")
        if extract.get("ocr_max_batch_bytes",6_000_000) < 1024 * extract.get("ocr_batch_images",8):
            raise ValueError("OCR batch byte budget too small")
    meta = profile["metadata"]
    if type(meta.get("inherit_document_fields")) is not bool:
        raise ValueError("metadata.inherit_document_fields must be boolean")
    for level in ("document", "chunk"):
        fields = meta.get(level, {})
        if not isinstance(fields, dict) or len(fields) > 100:
            raise ValueError("metadata." + level + " must be an object of up to 100 fields")
        for name, rule in fields.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
                raise ValueError("Invalid metadata key: " + name)
            if not isinstance(rule, dict) or rule.get("method") not in _ALLOWED_METHODS:
                raise ValueError("Invalid metadata rule for " + name)
            extra_options = set(rule) - {"method", "label", "pattern", "value", "first_pages",
                                                   "enabled", "required", "type", "allowed_values", "sample_values"}
            if extra_options:
                raise ValueError("Unsupported rule options for " + name)
            if rule["method"] == "regex":
                pattern = rule.get("pattern", "")
                if not isinstance(pattern, str) or not 1 <= len(pattern) <= 300:
                    raise ValueError("Invalid regex for " + name)
                re.compile(pattern)
            if rule["method"] == "label" and not isinstance(rule.get("label"), str):
                raise ValueError("Missing label for " + name)
            if rule["method"] == "constant" and not isinstance(rule.get("value"), (str, int, float, bool)):
                raise ValueError("Missing constant for " + name)
            for flag in ("enabled", "required"):
                if flag in rule and type(rule[flag]) is not bool:
                    raise ValueError(flag + " must be boolean for " + name)
            if rule.get("type","string") not in {
                "string","integer","number","date","boolean","enum"}:
                raise ValueError("Unsupported metadata type for " + name)
            for key in ("sample_values", "allowed_values"):
                if key in rule and (
                    not isinstance(rule[key], list) or len(rule[key]) > 30
                    or any(not isinstance(v, (str, int, float, bool)) for v in rule[key])
                ):
                    raise ValueError("Invalid " + key + " for " + name)
            if rule.get("type") == "enum" and not rule.get("allowed_values"):
                raise ValueError("enum must define allowed_values for " + name)
            if "first_pages" in rule and (
                type(rule["first_pages"]) is not int or not 1 <= rule["first_pages"] <= 100
            ):
                raise ValueError("first_pages must be between 1 and 100")
    for area in ("output", "existing_chunks", "extracted_content"):
        if not isinstance(profile[area], dict):
            raise ValueError(area + " must be an object")
