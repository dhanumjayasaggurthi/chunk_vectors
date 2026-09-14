from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
import configparser
import hashlib
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path, PurePosixPath
from typing import Optional, Iterator

from .bounded import bounded_parallel_map
from .object_selector import (
    SelectionIssue,
    resolve_discovered_sources,
    resolve_preferred_sources,
)
from .pipeline import MIRPipeline, PipelineResult
from .profile import processing_fingerprint
from .resources import ResourceGovernor
from .rimdocs import EmptyRimDocsProvider


@dataclass(frozen=True)
class SourceItem:
    canonical_path: str
    local_path: Optional[str]
    source_url: str
    source_version: str
    size: int
    logical_object_id: Optional[str] = None
    selected_format: Optional[str] = None
    selection_reason: str = ""
    available_formats: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceSelectionResult:
    doc_id: str
    generation_id: str
    pages: int
    chunks: int
    status: str
    logical_object_id: str
    detail: str
    candidates: tuple[str, ...] = ()


def _relative_logical_id(path: Path, root: Path) -> str:
    relative = path.relative_to(root)
    return relative.with_suffix("").as_posix()


def iter_nas(root, max_files=None, recursive=True) -> Iterator[SourceItem]:
    """Yield NAS/local candidates in deterministic order.

    `recursive=true` includes all nested subfolders. `recursive=false` scans
    only files directly under `root`. The iterator remains lazy and does not
    load document bytes.
    """
    root = Path(root).resolve()
    count = 0
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames.sort()
        filenames.sort()
        if not recursive:
            dirnames[:] = []
        for name in filenames:
            if Path(name).suffix.lower() not in {".pdf", ".docx"}:
                continue
            path = Path(dirpath) / name
            try:
                stat = path.stat()
            except OSError:
                continue
            canonical = str(path.resolve()).replace("\\", "/")
            yield SourceItem(
                canonical,
                str(path),
                canonical,
                f"mtime_ns={stat.st_mtime_ns};size={stat.st_size}",
                stat.st_size,
                logical_object_id=_relative_logical_id(path.resolve(), root),
            )
            count += 1
            if max_files and count >= max_files:
                return


class S3Source:
    """S3 source using the project's existing config.ini credential pattern.

    Credential precedence is intentionally compatible with the current server
    architecture:
      1. complete access_key_id + secret_access_key (+ optional session_token)
      2. configured AWS profile
      3. normal boto3 credential chain (IAM role/environment/etc.)

    Incomplete static keys are ignored when a profile is configured, which
    permits the existing profile-based server configuration to remain intact.
    """

    def __init__(self, config_path="config.ini"):
        cfg = configparser.ConfigParser()
        cfg.read(config_path)
        section = "S3" if cfg.has_section("S3") else "s3"
        if not cfg.has_section(section):
            raise KeyError("Missing [S3] or [s3] configuration")
        c = cfg[section]
        self.bucket = c.get("bucket", "").strip()
        self.prefix = c.get("prefix", "").strip()
        self.region = c.get("region", "us-east-1").strip() or "us-east-1"
        self.profile = c.get("profile", "").strip()
        self.endpoint_url = c.get("endpoint_url", "").strip()
        self.temp_dir = c.get("temp_dir", "").strip() or None
        self.access_key_id = c.get("access_key_id", "").strip()
        self.secret_access_key = c.get("secret_access_key", "").strip()
        self.session_token = c.get("session_token", "").strip()
        self.scratch_reserve_bytes = (
            max(0, c.getint("scratch_reserve_mb", fallback=1024)) * 1024 * 1024
        )
        self._scratch_lock = threading.Lock()
        self._reserved_bytes = 0
        self._reservations: dict[str, int] = {}

        if not self.bucket:
            raise ValueError("S3 bucket is required")

        import boto3
        from botocore.config import Config as BotoConfig

        session_kwargs = {"region_name": self.region}
        if self.access_key_id and self.secret_access_key:
            session_kwargs.update(
                {
                    "aws_access_key_id": self.access_key_id,
                    "aws_secret_access_key": self.secret_access_key,
                }
            )
            if self.session_token:
                session_kwargs["aws_session_token"] = self.session_token
        elif self.profile:
            session_kwargs["profile_name"] = self.profile
        elif self.access_key_id or self.secret_access_key or self.session_token:
            raise ValueError(
                "Incomplete static S3 credentials: provide both access_key_id and "
                "secret_access_key, configure profile, or use the default AWS credential chain"
            )

        session = boto3.Session(**session_kwargs)
        kwargs = {
            "config": BotoConfig(retries={"max_attempts": 10, "mode": "adaptive"})
        }
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url
        self.client = session.client("s3", **kwargs)

    def _relative_key(self, key: str) -> str:
        if not self.prefix:
            return key.lstrip("/")
        if not key.startswith(self.prefix):
            return key.lstrip("/")
        return key[len(self.prefix):].lstrip("/")

    def items(self, max_files=None, recursive=True):
        count = 0
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                suffix = Path(key).suffix.lower()
                if suffix not in {".pdf", ".docx"}:
                    continue
                relative_key = self._relative_key(key)
                if not recursive and "/" in relative_key:
                    continue
                etag = str(obj.get("ETag", "")).strip('"')
                modified = obj.get("LastModified")
                modified_text = (
                    modified.astimezone(timezone.utc).isoformat() if modified else ""
                )
                uri = f"s3://{self.bucket}/{key}"
                size = int(obj.get("Size", 0))
                logical_id = str(PurePosixPath(relative_key).with_suffix(""))
                yield SourceItem(
                    uri,
                    None,
                    uri,
                    f"etag={etag};last_modified={modified_text};size={size}",
                    size,
                    logical_object_id=logical_id,
                )
                count += 1
                if max_files and count >= max_files:
                    return

    def _scratch_path(self) -> Path:
        path = Path(self.temp_dir or tempfile.gettempdir())
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _reserve(self, size: int) -> None:
        scratch = self._scratch_path()
        with self._scratch_lock:
            free = shutil.disk_usage(scratch).free - self._reserved_bytes
            required = size + self.scratch_reserve_bytes
            if free < required:
                raise RuntimeError(
                    f"Insufficient S3 scratch space: need {required} bytes including reserve, "
                    f"available after reservations={max(0, free)} bytes"
                )
            self._reserved_bytes += size

    def download(self, item):
        self._reserve(item.size)
        rest = item.canonical_path[len("s3://") :]
        bucket, _, key = rest.partition("/")
        fd = None
        path = None
        try:
            fd, path = tempfile.mkstemp(
                suffix=Path(key).suffix,
                dir=str(self._scratch_path()),
            )
            os.close(fd)
            fd = None
            self.client.download_file(bucket, key, path)
            actual_size = os.path.getsize(path)
            if item.size and actual_size != item.size:
                raise RuntimeError(
                    f"S3 download size mismatch for {item.canonical_path}: "
                    f"expected {item.size}, got {actual_size}"
                )
            with self._scratch_lock:
                self._reservations[path] = item.size
            return path
        except Exception:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            with self._scratch_lock:
                self._reserved_bytes = max(0, self._reserved_bytes - item.size)
            raise

    def cleanup(self, path):
        try:
            os.unlink(path)
        except OSError:
            pass
        with self._scratch_lock:
            size = self._reservations.pop(path, 0)
            self._reserved_bytes = max(0, self._reserved_bytes - size)


class BatchRunner:
    def __init__(self, settings, store, rimdocs=None):
        self.settings = settings
        self.store = store
        self.rimdocs = rimdocs or EmptyRimDocsProvider()
        self.governor = ResourceGovernor(
            settings.vision_concurrency,
            settings.chat_concurrency,
            settings.embedding_concurrency,
        )
        self.profile = processing_fingerprint(settings)

    def _pipeline(self):
        return MIRPipeline(self.settings, store=self.store, governor=self.governor)

    @staticmethod
    def _safe_ident(value: str) -> str:
        value = str(value or "").strip()
        if not value or not value.replace("_", "").isalnum() or value[0].isdigit():
            raise ValueError(f"Unsafe SQL identifier: {value!r}")
        return value

    def _load_object_ids(self, max_files=None) -> list[str]:
        schema = self._safe_ident(self.settings.object_list_schema)
        table = self._safe_ident(self.settings.object_list_table)
        column = self._safe_ident(self.settings.object_list_id_column)
        sql = (
            f"SELECT DISTINCT {column}::text FROM {schema}.{table} "
            f"WHERE {column} IS NOT NULL AND btrim({column}::text)<>'' "
            f"ORDER BY {column}::text"
        )
        params = []
        if max_files:
            sql += " LIMIT %s"
            params.append(int(max_files))

        rows: list[str] = []
        with self.store.conn() as conn:
            with conn.cursor(name="mirai_object_list_cursor") as cur:
                cur.itersize = 1000
                cur.execute(sql, params)
                rows.extend(str(row[0]).strip() for row in cur)
            conn.rollback()
        return rows

    def _resolve_items(self, items, max_files=None):
        if not self.settings.object_list_enabled:
            outcome = resolve_discovered_sources(
                items,
                enable_pdf=self.settings.enable_pdf,
                enable_docx=self.settings.enable_docx,
                preferred_format=self.settings.preferred_format,
                strict=self.settings.strict_source_selection,
                case_sensitive=self.settings.object_match_case_sensitive,
                max_items=max_files,
            )
            return list(outcome.selected), outcome.issues

        object_ids = self._load_object_ids(max_files)
        outcome = resolve_preferred_sources(
            items,
            object_ids,
            enable_pdf=self.settings.enable_pdf,
            enable_docx=self.settings.enable_docx,
            preferred_format=self.settings.preferred_format,
            strict=self.settings.strict_source_selection,
            case_sensitive=self.settings.object_match_case_sensitive,
        )
        return list(outcome.selected), outcome.issues

    def _document_key(self, item: SourceItem) -> str:
        if not item.logical_object_id:
            return item.canonical_path
        logical = item.logical_object_id.strip().replace("\\", "/")
        if not self.settings.object_match_case_sensitive:
            logical = logical.casefold()
        return f"mirai-object:{logical}"

    def _metadata_for_item(self, item: SourceItem):
        if self.settings.metadata_mode == "disabled":
            return None, "disabled", None

        if hasattr(self.rimdocs, "get_with_version"):
            metadata, version = self.rimdocs.get_with_version(item.canonical_path)
        else:
            metadata = self.rimdocs.get(item.canonical_path)
            if metadata is None:
                version = "none"
            else:
                payload = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                version = hashlib.sha256(payload.encode("utf-8")).hexdigest()

        if self.settings.metadata_mode == "required" and metadata is None:
            object_id = item.logical_object_id or item.canonical_path
            issue = SelectionIssue(
                object_id,
                "RIMDOCS_NOT_FOUND",
                "metadata_mode=required but no authoritative RimDocs row matched this source",
                (item.canonical_path,),
            )
            return None, version, issue

        return metadata, version, None

    def _effective_source_version(self, item: SourceItem, metadata_version: str = "") -> str:
        base = item.source_version
        if item.logical_object_id:
            base = (
                f"{base};format={item.selected_format or ''};"
                f"source={item.canonical_path}"
            )
        return (
            f"{base};metadata_mode={self.settings.metadata_mode};"
            f"metadata_version={metadata_version or 'none'}"
        )

    def _skip_if_unchanged(self, item, metadata_version=""):
        state = self.store.active_source_state(self._document_key(item))
        if not state:
            return None
        if (
            state.get("source_version") == self._effective_source_version(item, metadata_version)
            and state.get("processing_fingerprint") == self.profile
        ):
            return PipelineResult(
                self.store.doc_id(self._document_key(item)),
                state.get("generation_id") or "",
                int(state.get("pages_total") or 0),
                int(state.get("chunk_count") or 0),
                "SKIPPED_UNCHANGED",
            )
        return None

    def _issue_result(self, issue: SelectionIssue) -> SourceSelectionResult:
        key = issue.object_id.strip().replace("\\", "/")
        if not self.settings.object_match_case_sensitive:
            key = key.casefold()
        return SourceSelectionResult(
            self.store.doc_id(f"mirai-object:{key}"),
            "",
            0,
            0,
            issue.status,
            issue.object_id,
            issue.detail,
            issue.candidates,
        )

    def run_nas(self, root, max_files=None):
        selected, issues = self._resolve_items(
            iter_nas(root, recursive=self.settings.source_recursive),
            max_files,
        )
        for issue in issues:
            yield self._issue_result(issue)

        def work(item):
            metadata, metadata_version, metadata_issue = self._metadata_for_item(item)
            if metadata_issue:
                return self._issue_result(metadata_issue)
            skipped = self._skip_if_unchanged(item, metadata_version)
            if skipped:
                return skipped
            return self._pipeline().process_file(
                item.local_path,
                canonical_path=self._document_key(item),
                source_url=item.source_url,
                source_version=self._effective_source_version(item, metadata_version),
                rimdocs_metadata=metadata,
            )

        yield from bounded_parallel_map(
            work,
            selected,
            self.settings.doc_workers,
            self.settings.max_inflight_docs,
        )

    def run_s3(self, source, max_files=None):
        selected, issues = self._resolve_items(
            source.items(recursive=self.settings.source_recursive),
            max_files,
        )
        for issue in issues:
            yield self._issue_result(issue)

        def work(item):
            metadata, metadata_version, metadata_issue = self._metadata_for_item(item)
            if metadata_issue:
                return self._issue_result(metadata_issue)
            skipped = self._skip_if_unchanged(item, metadata_version)
            if skipped:
                return skipped
            local = source.download(item)
            try:
                return self._pipeline().process_file(
                    local,
                    canonical_path=self._document_key(item),
                    source_url=item.source_url,
                    source_version=self._effective_source_version(item, metadata_version),
                    rimdocs_metadata=metadata,
                )
            finally:
                source.cleanup(local)

        yield from bounded_parallel_map(
            work,
            selected,
            self.settings.doc_workers,
            self.settings.max_inflight_docs,
        )
