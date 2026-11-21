"""Safe, versioned YAML use-case contract reader.

YAML captures consumer requirements and supported native-pilot settings.
Requested future features remain design-only, never executable toggles.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

MAX_PROFILE_BYTES = 256 * 1024
SECTIONS = frozenset({
    "schema_version", "profile_id", "approval", "use_case", "source",
    "content_requirements", "metadata", "search_requirements", "acceptance",
    "security", "extraction", "chunking", "embedding_request",
    "output_request", "requested_future_features", "execution",
})
EXECUTION_FLAGS = frozenset({
    "native_pdf", "gcp_ocr", "ai_metadata", "postgres_write", "embedding",
})
REQUESTED_FEATURES = frozenset({
    "gcp_ocr", "cross_page_layout_stitching", "table_and_figure_structure",
    "ai_metadata_fallback", "distributed_multi_server_workers",
    "postgres_publication", "embeddings",
})
PLANNED = (
    "gcp_ocr", "layout_stitching", "structured_tables_and_figures",
    "ai_metadata", "distributed_leases", "postgres_publication",
    "embeddings", "retrieval_index",
)


class ProfileError(ValueError):
    """Invalid profile or unsupported execution feature."""


def _object(value, path):
    if not isinstance(value, dict):
        raise ProfileError(f"{path} must be a mapping")
    return value


def _keys(value, allowed, path):
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ProfileError(f"Unknown setting(s) in {path}: {', '.join(map(str,unknown))}")


def _required_text(value, path, approved=False):
    if not isinstance(value, str) or not value.strip():
        raise ProfileError(f"{path} must be nonempty text")
    if approved and ("REPLACE" in value.upper() or value.strip().lower() in ("tbd", "unknown")):
        raise ProfileError(f"{path} still contains placeholder content")


def _bool(value, path):
    if type(value) is not bool:
        raise ProfileError(f"{path} must be true or false")


def _read_profile(path):
    file = Path(path)
    if file.suffix.lower() not in (".yml", ".yaml", ".json"):
        raise ProfileError("Profile must be .yaml, .yml or .json")
    if file.stat().st_size > MAX_PROFILE_BYTES:
        raise ProfileError("Profile exceeds the 256 KiB size limit")
    text = file.read_text(encoding="utf-8")
    if file.suffix.lower() == ".json":
        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ProfileError(f"Duplicate JSON key: {key}")
                result[key] = value
            return result
        data = json.loads(text, object_pairs_hook=unique_pairs)
    else:
        try:
            import yaml
        except ImportError as exc:
            raise ProfileError("Install PyYAML>=6.0.2 to use YAML profiles") from exc

        class UniqueSafeLoader(yaml.SafeLoader):
            pass

        def unique_mapping(loader, node):
            loader.flatten_mapping(node)
            result = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node, deep=True)
                if not isinstance(key, str):
                    raise ProfileError("YAML mapping keys must be strings")
                if key in result:
                    raise ProfileError(f"Duplicate YAML key: {key}")
                result[key] = loader.construct_object(value_node, deep=True)
            return result

        UniqueSafeLoader.add_constructor(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping
        )
        data = yaml.load(text, Loader=UniqueSafeLoader)
    return _object(data, "profile")


def validate_contract(profile):
    """Validate requirements and fail closed on unsupported execution stages."""
    _keys(profile, SECTIONS, "profile")
    if type(profile.get("schema_version")) is not int or profile["schema_version"] != 1:
        raise ProfileError("Unsupported schema_version; expected 1")
    name = profile.get("profile_id")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{2,79}", name):
        raise ProfileError("profile_id must be 3-80 lowercase letters, digits, _ or -")

    approval = _object(profile.get("approval"), "approval")
    _keys(approval, ("status", "business_approver", "data_owner_approver", "approved_date"), "approval")
    if approval.get("status") not in ("draft", "pilot_approved"):
        raise ProfileError("approval.status must be draft or pilot_approved")
    approved = approval["status"] == "pilot_approved"
    if approved:
        for key in ("business_approver", "data_owner_approver"):
            _required_text(approval.get(key), f"approval.{key}", approved=True)
        date = approval.get("approved_date")
        if not isinstance(date, str):
            raise ProfileError("approval.approved_date must be a quoted ISO date")
        try:
            dt.date.fromisoformat(date)
        except ValueError as exc:
            raise ProfileError("approval.approved_date must be YYYY-MM-DD") from exc

    use_case = _object(profile.get("use_case"), "use_case")
    _keys(use_case, ("name", "business_unit", "business_owner", "technical_contact",
                     "purpose", "outputs", "priority", "desired_pilot_date"), "use_case")
    for key in ("name", "business_owner", "purpose"):
        _required_text(use_case.get(key), f"use_case.{key}", approved=approved)

    source = _object(profile.get("source"), "source")
    _keys(source, ("type", "uri", "document_id_rule", "formats", "languages",
                   "classification", "expected_volume", "sample_documents"), "source")
    _required_text(source.get("uri"), "source.uri", approved=approved)
    if source.get("type") not in ("s3", "nas", "database", "sharepoint", "other"):
        raise ProfileError("Unsupported source.type")
    if source.get("classification") not in ("internal", "confidential", "regulated"):
        raise ProfileError("Invalid source.classification")
    if source.get("document_id_rule") != "source_uri":
        raise ProfileError("Only document_id_rule=source_uri is defined in contract v1")
    volume = _object(source.get("expected_volume"), "source.expected_volume")
    _keys(volume, ("documents_total", "new_documents_per_day", "typical_pages",
                   "maximum_pages"), "source.expected_volume")
    for key, value in volume.items():
        if type(value) is not int or value < 0:
            raise ProfileError(f"source.expected_volume.{key} must be nonnegative integer")

    content = _object(profile.get("content_requirements"), "content_requirements")
    _keys(content, ("preserve", "cross_page_paragraphs", "cross_page_tables",
                    "missing_or_ambiguous_content", "exclude_from_search"),
          "content_requirements")
    for key in ("cross_page_paragraphs", "cross_page_tables"):
        if content.get(key) not in ("required", "preferred", "not_required"):
            raise ProfileError(f"Invalid content_requirements.{key}")

    metadata = _object(profile.get("metadata"), "metadata")
    from .metadata import validate_metadata_policy
    try:
        validate_metadata_policy(metadata)
    except (TypeError, ValueError) as exc:
        raise ProfileError(f"Invalid metadata: {exc}") from exc

    search = _object(profile.get("search_requirements"), "search_requirements")
    _keys(search, ("example_questions", "results_should_include", "access_model"),
          "search_requirements")
    questions = search.get("example_questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= 20:
        raise ProfileError("Provide 1-20 representative example questions")
    for item in questions:
        q = _object(item, "example question")
        _keys(q, ("question", "expected_evidence", "filters"), "example question")
        _required_text(q.get("question"), "example question")
        _required_text(q.get("expected_evidence"), "expected_evidence")
        if not isinstance(q.get("filters", []), list):
            raise ProfileError("example question filters must be a list")

    acceptance = _object(profile.get("acceptance"), "acceptance")
    _keys(acceptance, ("required_field_accuracy_target", "evidence_citation_required",
                       "no_silent_partial_extraction",
                       "max_turnaround_hours_for_25000_pages", "review_owner"), "acceptance")
    quality = acceptance.get("required_field_accuracy_target")
    if type(quality) not in (int, float) or not 0 <= quality <= 1:
        raise ProfileError("required_field_accuracy_target must be between 0 and 1")
    for key in ("evidence_citation_required", "no_silent_partial_extraction"):
        _bool(acceptance.get(key), f"acceptance.{key}")

    security = _object(profile.get("security"), "security")
    _keys(security, ("data_residency", "external_ai_processing_approved",
                     "retention_days", "security_policy_reference"), "security")
    _bool(security.get("external_ai_processing_approved"),
          "security.external_ai_processing_approved")

    embedding = _object(profile.get("embedding_request"), "embedding_request")
    _keys(embedding, ("desired", "model_family", "dimensions", "text_fields",
                      "filter_only_fields", "retrieval_strategy",
                      "embedding_model_change"), "embedding_request")
    _bool(embedding.get("desired"), "embedding_request.desired")
    if type(embedding.get("dimensions")) is not int or not 1 <= embedding["dimensions"] <= 4096:
        raise ProfileError("embedding_request.dimensions must be an integer in 1..4096")
    if embedding.get("retrieval_strategy") not in ("vector", "keyword", "hybrid"):
        raise ProfileError("Invalid embedding_request.retrieval_strategy")
    fields = _object(metadata.get("fields"), "metadata.fields")
    text_fields = embedding.get("text_fields")
    if not isinstance(text_fields, list):
        raise ProfileError("embedding_request.text_fields must be a list")
    permitted = {"chunk_text", "section_title", "source_document_id", "file_name", "page"} | set(fields)
    for field in text_fields:
        if field not in permitted:
            raise ProfileError(f"Unknown embedding text field: {field}")

    output = _object(profile.get("output_request"), "output_request")
    _keys(output, ("storage", "target_schema", "target_table",
                   "preserve_previous_version", "source_traceability"),
          "output_request")
    _bool(output.get("preserve_previous_version"),
          "output_request.preserve_previous_version")

    future = _object(profile.get("requested_future_features"), "requested_future_features")
    _keys(future, REQUESTED_FEATURES, "requested_future_features")
    for key, value in future.items():
        _bool(value, f"requested_future_features.{key}")

    execution = _object(profile.get("execution"), "execution")
    _keys(execution, EXECUTION_FLAGS | {"mode"}, "execution")
    if execution.get("mode") != "native_pdf_pilot":
        raise ProfileError("Only native_pdf_pilot execution is currently supported")
    for key in EXECUTION_FLAGS:
        _bool(execution.get(key), f"execution.{key}")
    if execution["native_pdf"] is not True:
        raise ProfileError("execution.native_pdf must be true")
    unsupported = sorted(key for key in EXECUTION_FLAGS - {"native_pdf"} if execution[key])
    if unsupported:
        raise ProfileError("Stage(s) not yet implemented: " + ", ".join(unsupported))

    from .plan import ExtractionPolicy
    from .chunking import ChunkPolicy
    try:
        ExtractionPolicy(**_object(profile.get("extraction"), "extraction"))
        ChunkPolicy(**_object(profile.get("chunking"), "chunking"))
    except (TypeError, ValueError) as exc:
        raise ProfileError(f"Invalid executable policy: {exc}") from exc
    return profile


def load_profile(path):
    """Read a versioned YAML/JSON contract or the existing legacy pilot JSON."""
    profile = _read_profile(path)
    if "schema_version" in profile:
        return validate_contract(profile)
    if Path(path).suffix.lower() != ".json":
        raise ProfileError("YAML profiles require schema_version: 1")
    for key in ("extraction", "chunking"):
        if key not in profile:
            raise ProfileError(f"Legacy JSON profile missing {key}")
    # Preserve existing JSON pilot compatibility, with validated policies.
    from .plan import ExtractionPolicy
    from .chunking import ChunkPolicy
    from .metadata import validate_metadata_policy
    try:
        ExtractionPolicy(**_object(profile["extraction"], "extraction"))
        ChunkPolicy(**_object(profile["chunking"], "chunking"))
        validate_metadata_policy(profile.get("metadata", {"enabled": False}))
    except (TypeError, ValueError) as exc:
        raise ProfileError(f"Invalid legacy JSON pilot: {exc}") from exc
    return profile


def require_approved_for_pilot(profile):
    if "schema_version" in profile and profile["approval"]["status"] != "pilot_approved":
        raise ProfileError(
            "YAML pilot is not approved. Set approval.status=pilot_approved "
            "with both owners and ISO date after formal approval."
        )


def profile_summary(profile):
    return {
        "profile_id": profile.get("profile_id", profile.get("name", "legacy_json_pilot")),
        "schema_version": profile.get("schema_version", "legacy"),
        "approval": profile.get("approval", {}).get("status", "legacy_pilot"),
        "executable_now": ["native_pdf", "chunking", "regex_or_label_metadata"],
        "design_only_not_executed": list(PLANNED),
        "metadata_fields": list(profile.get("metadata", {}).get("fields", {})),
        "production_ingestion_modified": False,
    }
