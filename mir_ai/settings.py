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
    scratch_dir: Path
    source_type: str
    docs_root: Path
    page_workers: int
    max_inflight_pages: int
    doc_workers: int
    max_inflight_docs: int
    vision_concurrency: int
    chat_concurrency: int
    embedding_concurrency: int
    lease_seconds: int
    heartbeat_seconds: int
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
    log_level: str
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

        db_schema = str(_first(cfg, pg, "schema", "public")).strip()
        scratch = Path(
            _first(
                cfg,
                pipeline,
                "scratch_dir",
                os.environ.get("MIRAI_SCRATCH", ".mirai_scratch"),
            )
        )
        scratch.mkdir(parents=True, exist_ok=True)
        page_workers = _as_int(_first(cfg, pipeline, "page_workers", 4), 4)
        max_inflight = _as_int(
            _first(cfg, pipeline, "max_inflight_pages", max(4, page_workers * 2)),
            max(4, page_workers * 2),
        )
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
            scratch_dir=scratch,
            source_type=str(_first(cfg, paths, "source_type", "nas")).lower(),
            docs_root=Path(_first(cfg, paths, "docs_root", ".")),
            page_workers=max(1, page_workers),
            max_inflight_pages=max(1, max_inflight),
            doc_workers=max(1, _as_int(_first(cfg, pipeline, "doc_workers", 2), 2)),
            max_inflight_docs=max(
                1, _as_int(_first(cfg, pipeline, "max_inflight_docs", 4), 4)
            ),
            vision_concurrency=max(
                1, _as_int(_first(cfg, pipeline, "vision_concurrency", 16), 16)
            ),
            chat_concurrency=max(
                1, _as_int(_first(cfg, pipeline, "chat_concurrency", 6), 6)
            ),
            embedding_concurrency=max(
                1, _as_int(_first(cfg, pipeline, "embedding_concurrency", 8), 8)
            ),
            lease_seconds=max(
                60, _as_int(_first(cfg, pipeline, "lease_seconds", 900), 900)
            ),
            heartbeat_seconds=max(
                10, _as_int(_first(cfg, pipeline, "heartbeat_seconds", 60), 60)
            ),
            ocr_render_dpi=max(
                72, _as_int(_first(cfg, pipeline, "ocr_render_dpi", 200), 200)
            ),
            scanned_text_threshold=max(
                0, _as_int(_first(cfg, pipeline, "scanned_text_threshold", 100), 100)
            ),
            embedding_model=str(
                _first(cfg, pipeline, "embedding_model", "text-embedding-3-large")
            ),
            embedding_dim=_as_int(
                _first(cfg, pipeline, "embedding_dim", 3072), 3072
            ),
            embedding_api_version=str(
                _first(
                    cfg,
                    pipeline,
                    "embedding_api_version",
                    "2025-04-01-preview",
                )
            ),
            embedding_batch_size=max(
                1, _as_int(_first(cfg, pipeline, "embedding_batch_size", 16), 16)
            ),
            chunk_target_min_tokens=max(
                1,
                _as_int(
                    _first(cfg, pipeline, "chunk_target_min_tokens", 1200),
                    1200,
                ),
            ),
            chunk_target_max_tokens=max(
                1,
                _as_int(
                    _first(cfg, pipeline, "chunk_target_max_tokens", 1500),
                    1500,
                ),
            ),
            chunk_overlap_tokens=max(
                0,
                _as_int(_first(cfg, pipeline, "chunk_overlap_tokens", 100), 100),
            ),
            table_summary_workers=max(
                1, _as_int(_first(cfg, pipeline, "table_summary_workers", 4), 4)
            ),
            enable_embeddings=_as_bool(
                _first(cfg, pipeline, "enable_embeddings", "true"), True
            ),
            vector_index_mode=str(
                _first(cfg, pipeline, "vector_index_mode", "exact")
            ).lower(),
            log_level=str(_first(cfg, pipeline, "log_level", "INFO")).upper(),
            enable_pdf=_as_bool(_first(cfg, pipeline, "enable_pdf", "true"), True),
            enable_docx=_as_bool(_first(cfg, pipeline, "enable_docx", "true"), True),
            preferred_format=str(
                _first(cfg, pipeline, "preferred_format", "pdf")
            ).strip().lower(),
            object_list_enabled=_as_bool(
                _first(cfg, pipeline, "object_list_enabled", "false"), False
            ),
            object_list_schema=str(
                _first(cfg, pipeline, "object_list_schema", db_schema)
            ).strip(),
            object_list_table=str(
                _first(cfg, pipeline, "object_list_table", "mirai_obj_list")
            ).strip(),
            object_list_id_column=str(
                _first(cfg, pipeline, "object_list_id_column", "")
            ).strip(),
            strict_source_selection=_as_bool(
                _first(cfg, pipeline, "strict_source_selection", "true"), True
            ),
            object_match_case_sensitive=_as_bool(
                _first(cfg, pipeline, "object_match_case_sensitive", "false"), False
            ),
        )

    def validate(self) -> None:
        if self.embedding_model != "text-embedding-3-large":
            raise ValueError(
                "MIR-AI requirement requires embedding_model=text-embedding-3-large"
            )
        if self.embedding_dim != 3072:
            raise ValueError("MIR-AI requirement requires embedding_dim=3072")
        if self.embedding_api_version != "2025-04-01-preview":
            raise ValueError(
                "MIR-AI requirement requires embedding_api_version=2025-04-01-preview"
            )
        if self.chunk_target_min_tokens > self.chunk_target_max_tokens:
            raise ValueError(
                "chunk_target_min_tokens cannot exceed chunk_target_max_tokens"
            )
        if self.vector_index_mode not in {"exact", "halfvec_hnsw"}:
            raise ValueError("vector_index_mode must be exact or halfvec_hnsw")
        if not self.enable_pdf and not self.enable_docx:
            raise ValueError("At least one of enable_pdf or enable_docx must be true")
        if self.preferred_format not in {"pdf", "docx"}:
            raise ValueError("preferred_format must be pdf or docx")
        if self.object_list_enabled:
            if not self.object_list_schema:
                raise ValueError(
                    "object_list_schema is required when object_list_enabled=true"
                )
            if not self.object_list_table:
                raise ValueError(
                    "object_list_table is required when object_list_enabled=true"
                )
            if not self.object_list_id_column:
                raise ValueError(
                    "object_list_id_column is required when object_list_enabled=true"
                )
            if not self.strict_source_selection:
                raise ValueError(
                    "strict_source_selection must be true when object_list_enabled=true"
                )
