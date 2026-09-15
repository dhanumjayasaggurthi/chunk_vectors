from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import configparser
import os

_MISSING = object()


def _first(cfg: configparser.ConfigParser, sections: tuple[str, ...], key: str, default=_MISSING):
    for section in sections:
        if cfg.has_section(section) and cfg.has_option(section, key):
            return cfg.get(section, key)
    if default is _MISSING:
        raise KeyError(f"Missing config key {key!r} in sections {sections}")
    return default


def _as_int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_float(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _as_bool(v, default: bool) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    config_path: Path
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    db_schema: str
    folder: str

    source_type: str
    docs_root: Path
    source_auto_run: bool
    source_recursive: bool
    source_max_files: int
    source_plan_only: bool
    log_dir: Path
    metadata_mode: str
    rimdocs_jsonl_path: str
    scratch_dir: Path

    page_workers: int
    max_inflight_pages: int
    doc_workers: int
    max_inflight_docs: int
    vision_concurrency: int
    chat_concurrency: int
    embedding_concurrency: int
    lease_seconds: int
    heartbeat_seconds: int
    generation_max_attempts: int
    batch_continue_on_error: bool
    batch_retry_transient_errors: bool
    batch_retry_attempts: int
    batch_retry_base_seconds: float
    batch_exit_nonzero_on_error: bool
    progress_log_every_pages: int

    db_pool_maxconn: int
    db_max_retries: int
    db_retry_base_seconds: float
    db_connect_timeout_s: int
    db_keepalives_idle_s: int
    db_keepalives_interval_s: int
    db_keepalives_count: int

    api_max_retries: int
    api_retry_base_seconds: float
    api_timeout_s: int
    auto_init_db: bool
    preflight_enabled: bool
    preflight_embedding: bool
    preflight_chat: bool
    preflight_vision: bool

    ocr_render_dpi: int
    scanned_text_threshold: int
    embedding_model: str
    embedding_dim: int
    embedding_api_version: str
    embedding_batch_size: int
    chunk_target_min_tokens: int
    chunk_target_max_tokens: int
    chunk_overlap_tokens: int
    table_summary_workers: int
    enable_embeddings: bool
    vector_index_mode: str
    requirements_mode: str

    log_level: str
    log_max_bytes: int
    log_backup_count: int
    log_queue_size: int
    log_console: bool
    log_console_json: bool

    enable_pdf: bool
    enable_docx: bool
    preferred_format: str
    object_list_enabled: bool
    object_list_schema: str
    object_list_table: str
    object_list_id_column: str
    strict_source_selection: bool
    object_match_case_sensitive: bool

    @classmethod
    def load(cls, path: str | Path = "config.ini") -> "Settings":
        path = Path(path)
        cfg = configparser.ConfigParser()
        cfg.read(path)
        pg = ("POSTGRES", "database")
        pipeline = ("MIR_AI", "PIPELINE", "processing")
        paths = ("PATHS", "paths")
        logging_sections = ("MIR_AI", "LOGGING", "logging", "PIPELINE", "processing")
        source_sections = ("MIR_AI", "PATHS", "paths", "PIPELINE", "processing")

        db_schema = str(_first(cfg, pg, "schema", "public")).strip()
        scratch = Path(_first(cfg, pipeline, "scratch_dir", os.environ.get("MIRAI_SCRATCH", ".mirai_scratch")))
        scratch.mkdir(parents=True, exist_ok=True)
        page_workers = _as_int(_first(cfg, pipeline, "page_workers", 4), 4)
        max_inflight = _as_int(_first(cfg, pipeline, "max_inflight_pages", max(4, page_workers * 2)), max(4, page_workers * 2))
        if max_inflight < page_workers:
            max_inflight = page_workers

        return cls(
            config_path=path,
            db_host=_first(cfg, pg, "host"),
            db_port=_as_int(_first(cfg, pg, "port", 5432), 5432),
            db_name=_first(cfg, pg, "database", _first(cfg, pg, "name", "")),
            db_user=_first(cfg, pg, "user"),
            db_password=_first(cfg, pg, "password"),
            db_schema=db_schema,
            folder=_first(cfg, pg, "folder", "mirai"),
            source_type=str(_first(cfg, paths, "source_type", "nas")).strip().lower(),
            docs_root=Path(_first(cfg, paths, "docs_root", ".")),
            source_auto_run=_as_bool(_first(cfg, source_sections, "source_auto_run", "false"), False),
            source_recursive=_as_bool(_first(cfg, source_sections, "source_recursive", "true"), True),
            source_max_files=max(0, _as_int(_first(cfg, source_sections, "source_max_files", 0), 0)),
            source_plan_only=_as_bool(_first(cfg, source_sections, "source_plan_only", "false"), False),
            log_dir=Path(_first(cfg, paths, "log_dir", "logs")),
            metadata_mode=str(_first(cfg, pipeline, "metadata_mode", "required")).strip().lower(),
            rimdocs_jsonl_path=str(_first(cfg, pipeline, "rimdocs_jsonl_path", "")).strip(),
            scratch_dir=scratch,
            page_workers=max(1, page_workers),
            max_inflight_pages=max(1, max_inflight),
            doc_workers=max(1, _as_int(_first(cfg, pipeline, "doc_workers", 2), 2)),
            max_inflight_docs=max(1, _as_int(_first(cfg, pipeline, "max_inflight_docs", 4), 4)),
            vision_concurrency=max(1, _as_int(_first(cfg, pipeline, "vision_concurrency", 16), 16)),
            chat_concurrency=max(1, _as_int(_first(cfg, pipeline, "chat_concurrency", 6), 6)),
            embedding_concurrency=max(1, _as_int(_first(cfg, pipeline, "embedding_concurrency", 8), 8)),
            lease_seconds=max(60, _as_int(_first(cfg, pipeline, "lease_seconds", 900), 900)),
            heartbeat_seconds=max(10, _as_int(_first(cfg, pipeline, "heartbeat_seconds", 60), 60)),
            generation_max_attempts=max(1, _as_int(_first(cfg, pipeline, "generation_max_attempts", _first(cfg, pipeline, "max_doc_retries", 5)), 5)),
            batch_continue_on_error=_as_bool(_first(cfg, pipeline, "batch_continue_on_error", "true"), True),
            batch_retry_transient_errors=_as_bool(_first(cfg, pipeline, "batch_retry_transient_errors", "true"), True),
            batch_retry_attempts=max(1, _as_int(_first(cfg, pipeline, "batch_retry_attempts", 2), 2)),
            batch_retry_base_seconds=max(0.0, _as_float(_first(cfg, pipeline, "batch_retry_base_seconds", 2.0), 2.0)),
            batch_exit_nonzero_on_error=_as_bool(_first(cfg, pipeline, "batch_exit_nonzero_on_error", "true"), True),
            progress_log_every_pages=max(1, _as_int(_first(cfg, pipeline, "progress_log_every_pages", 25), 25)),
            db_pool_maxconn=max(0, _as_int(_first(cfg, pipeline, "db_pool_maxconn", 0), 0)),
            db_max_retries=max(1, _as_int(_first(cfg, pipeline, "db_max_retries", 5), 5)),
            db_retry_base_seconds=max(0.0, _as_float(_first(cfg, pipeline, "db_retry_base_seconds", 1.0), 1.0)),
            db_connect_timeout_s=max(1, _as_int(_first(cfg, pipeline, "db_connect_timeout_s", 15), 15)),
            db_keepalives_idle_s=max(1, _as_int(_first(cfg, pipeline, "db_keepalives_idle_s", 30), 30)),
            db_keepalives_interval_s=max(1, _as_int(_first(cfg, pipeline, "db_keepalives_interval_s", 10), 10)),
            db_keepalives_count=max(1, _as_int(_first(cfg, pipeline, "db_keepalives_count", 5), 5)),
            api_max_retries=max(1, _as_int(_first(cfg, pipeline, "api_max_retries", 5), 5)),
            api_retry_base_seconds=max(0.0, _as_float(_first(cfg, pipeline, "api_retry_base_seconds", _first(cfg, pipeline, "api_retry_delay_s", 1.0)), 1.0)),
            api_timeout_s=max(10, _as_int(_first(cfg, pipeline, "api_timeout_s", 120), 120)),
            auto_init_db=_as_bool(_first(cfg, pipeline, "auto_init_db", "false"), False),
            preflight_enabled=_as_bool(_first(cfg, pipeline, "preflight_enabled", "true"), True),
            preflight_embedding=_as_bool(_first(cfg, pipeline, "preflight_embedding", "true"), True),
            preflight_chat=_as_bool(_first(cfg, pipeline, "preflight_chat", "true"), True),
            preflight_vision=_as_bool(_first(cfg, pipeline, "preflight_vision", "true"), True),
            ocr_render_dpi=max(72, _as_int(_first(cfg, pipeline, "ocr_render_dpi", 200), 200)),
            scanned_text_threshold=max(0, _as_int(_first(cfg, pipeline, "scanned_text_threshold", 100), 100)),
            embedding_model=str(_first(cfg, pipeline, "embedding_model", "text-embedding-3-large")).strip(),
            embedding_dim=_as_int(_first(cfg, pipeline, "embedding_dim", 3072), 3072),
            embedding_api_version=str(_first(cfg, pipeline, "embedding_api_version", "2025-04-01-preview")).strip(),
            embedding_batch_size=max(1, _as_int(_first(cfg, pipeline, "embedding_batch_size", 16), 16)),
            chunk_target_min_tokens=max(1, _as_int(_first(cfg, pipeline, "chunk_target_min_tokens", 1200), 1200)),
            chunk_target_max_tokens=max(1, _as_int(_first(cfg, pipeline, "chunk_target_max_tokens", 1500), 1500)),
            chunk_overlap_tokens=max(0, _as_int(_first(cfg, pipeline, "chunk_overlap_tokens", 100), 100)),
            table_summary_workers=max(1, _as_int(_first(cfg, pipeline, "table_summary_workers", 4), 4)),
            enable_embeddings=_as_bool(_first(cfg, pipeline, "enable_embeddings", "true"), True),
            vector_index_mode=str(_first(cfg, pipeline, "vector_index_mode", "exact")).strip().lower(),
            requirements_mode=str(_first(cfg, pipeline, "requirements_mode", "strict")).strip().lower(),
            log_level=str(_first(cfg, logging_sections, "log_level", "INFO")).upper(),
            log_max_bytes=max(1024 * 1024, _as_int(_first(cfg, logging_sections, "log_max_bytes", 50 * 1024 * 1024), 50 * 1024 * 1024)),
            log_backup_count=max(1, _as_int(_first(cfg, logging_sections, "log_backup_count", 10), 10)),
            log_queue_size=max(100, _as_int(_first(cfg, logging_sections, "log_queue_size", 10000), 10000)),
            log_console=_as_bool(_first(cfg, logging_sections, "log_console", "true"), True),
            log_console_json=_as_bool(_first(cfg, logging_sections, "log_console_json", "false"), False),
            enable_pdf=_as_bool(_first(cfg, pipeline, "enable_pdf", "true"), True),
            enable_docx=_as_bool(_first(cfg, pipeline, "enable_docx", "true"), True),
            preferred_format=str(_first(cfg, pipeline, "preferred_format", "pdf")).strip().lower(),
            object_list_enabled=_as_bool(_first(cfg, pipeline, "object_list_enabled", "false"), False),
            object_list_schema=str(_first(cfg, pipeline, "object_list_schema", db_schema)).strip(),
            object_list_table=str(_first(cfg, pipeline, "object_list_table", "mirai_obj_list")).strip(),
            object_list_id_column=str(_first(cfg, pipeline, "object_list_id_column", "")).strip(),
            strict_source_selection=_as_bool(_first(cfg, pipeline, "strict_source_selection", "true"), True),
            object_match_case_sensitive=_as_bool(_first(cfg, pipeline, "object_match_case_sensitive", "false"), False),
        )

    @property
    def requirement_compliant_embedding_config(self) -> bool:
        return self.embedding_model == "text-embedding-3-large" and self.embedding_dim == 3072 and self.embedding_api_version == "2025-04-01-preview"

    def validate(self) -> None:
        if not self.db_schema or not self.db_schema.replace("_", "").isalnum() or self.db_schema[0].isdigit():
            raise ValueError("POSTGRES schema must be a non-empty safe SQL identifier")
        if self.requirements_mode not in {"strict", "warn"}:
            raise ValueError("requirements_mode must be strict or warn")
        if self.requirements_mode == "strict" and not self.requirement_compliant_embedding_config:
            raise ValueError("MIR-AI strict requirements require embedding_model=text-embedding-3-large, embedding_dim=3072, embedding_api_version=2025-04-01-preview")
        if self.requirements_mode == "warn" and (self.embedding_dim <= 0 or not self.embedding_model or not self.embedding_api_version):
            raise ValueError("embedding model/dimension/api version must be valid")
        if self.chunk_target_min_tokens > self.chunk_target_max_tokens:
            raise ValueError("chunk_target_min_tokens cannot exceed chunk_target_max_tokens")
        if self.vector_index_mode not in {"exact", "halfvec_hnsw"}:
            raise ValueError("vector_index_mode must be exact or halfvec_hnsw")
        if self.source_type not in {"nas", "s3"}:
            raise ValueError("source_type must be nas or s3")
        if self.metadata_mode not in {"required", "optional", "disabled"}:
            raise ValueError("metadata_mode must be required, optional, or disabled")
        if not self.enable_pdf and not self.enable_docx:
            raise ValueError("At least one of enable_pdf or enable_docx must be true")
        if self.preferred_format not in {"pdf", "docx"}:
            raise ValueError("preferred_format must be pdf or docx")
        if self.heartbeat_seconds >= self.lease_seconds:
            raise ValueError("heartbeat_seconds must be shorter than lease_seconds")
        if self.max_inflight_pages < self.page_workers:
            raise ValueError("max_inflight_pages must be >= page_workers")
        if self.max_inflight_docs < self.doc_workers:
            raise ValueError("max_inflight_docs must be >= doc_workers")
        if self.object_list_enabled:
            if not self.object_list_schema:
                raise ValueError("object_list_schema is required when object_list_enabled=true")
            if not self.object_list_table:
                raise ValueError("object_list_table is required when object_list_enabled=true")
            if not self.object_list_id_column:
                raise ValueError("object_list_id_column is required when object_list_enabled=true")
            if not self.strict_source_selection:
                raise ValueError("strict_source_selection must be true when object_list_enabled=true")
