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
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Profile root must be an object")
    result = copy.deepcopy(_DEFAULTS)
    for key, value in data.items():
        if key not in result and key not in {"name"}:
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
            "existing_chunks.vector_column",
            "extracted_content.schema", "extracted_content.table",
            "extracted_content.doc_id_column", "extracted_content.text_column",
            "extracted_content.page_column", "extracted_content.position_column",
            "extracted_content.file_path_column", "extracted_content.file_name_column",
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
                      "dimensions", "deployment", "model"},
        "metadata": {"document", "chunk", "inherit_document_fields"},
        "existing_chunks": {"schema", "table", "id_column", "text_column", "vector_column"},
        "extracted_content": {"schema", "table", "doc_id_column", "text_column",
                              "page_column", "position_column", "file_path_column", "file_name_column"},
        "output": set(),
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
    if type(emb.get("batch_size")) is not int or not 1 <= emb["batch_size"] <= 128:
        raise ValueError("embedding.batch_size must be between 1 and 128")
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
            extra_options = set(rule) - {"method", "label", "pattern", "value", "first_pages"}
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
            if "first_pages" in rule and (
                type(rule["first_pages"]) is not int or not 1 <= rule["first_pages"] <= 20
            ):
                raise ValueError("first_pages must be between 1 and 20")
    for area in ("output", "existing_chunks", "extracted_content"):
        if not isinstance(profile[area], dict):
            raise ValueError(area + " must be an object")
