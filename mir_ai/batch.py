from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
import configparser
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Optional, Iterator

from .bounded import bounded_parallel_map
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


def iter_nas(root, max_files=None) -> Iterator[SourceItem]:
    count = 0
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames.sort()
        filenames.sort()
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
            )
            count += 1
            if max_files and count >= max_files:
                return


class S3Source:
    def __init__(self, config_path="config.ini"):
        cfg = configparser.ConfigParser()
        cfg.read(config_path)
        section = "S3" if cfg.has_section("S3") else "s3"
        if not cfg.has_section(section):
            raise KeyError("Missing [S3] configuration")
        c = cfg[section]
        self.bucket = c.get("bucket", "")
        self.prefix = c.get("prefix", "")
        self.region = c.get("region", "us-east-1")
        self.profile = c.get("profile", "")
        self.endpoint_url = c.get("endpoint_url", "")
        self.temp_dir = c.get("temp_dir", "") or None
        self.scratch_reserve_bytes = max(0, c.getint("scratch_reserve_mb", fallback=1024)) * 1024 * 1024
        self._scratch_lock = threading.Lock()
        self._reserved_bytes = 0
        self._reservations: dict[str, int] = {}

        import boto3
        from botocore.config import Config as BotoConfig

        session = boto3.Session(
            profile_name=self.profile or None,
            region_name=self.region,
        )
        kwargs = {"config": BotoConfig(retries={"max_attempts": 10, "mode": "adaptive"})}
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url
        self.client = session.client("s3", **kwargs)

    def items(self, max_files=None):
        count = 0
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if Path(key).suffix.lower() not in {".pdf", ".docx"}:
                    continue
                etag = str(obj.get("ETag", "")).strip('"')
                modified = obj.get("LastModified")
                modified_text = (
                    modified.astimezone(timezone.utc).isoformat() if modified else ""
                )
                uri = f"s3://{self.bucket}/{key}"
                size = int(obj.get("Size", 0))
                yield SourceItem(
                    uri,
                    None,
                    uri,
                    f"etag={etag};last_modified={modified_text};size={size}",
                    size,
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
        rest = item.canonical_path[len("s3://"):]
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

    def _skip_if_unchanged(self, item):
        state = self.store.active_source_state(item.canonical_path)
        if not state:
            return None
        if (
            state.get("source_version") == item.source_version
            and state.get("processing_fingerprint") == self.profile
        ):
            return PipelineResult(
                self.store.doc_id(item.canonical_path),
                state.get("generation_id") or "",
                int(state.get("pages_total") or 0),
                int(state.get("chunk_count") or 0),
                "SKIPPED_UNCHANGED",
            )
        return None

    def run_nas(self, root, max_files=None):
        def work(item):
            skipped = self._skip_if_unchanged(item)
            if skipped:
                return skipped
            return self._pipeline().process_file(
                item.local_path,
                canonical_path=item.canonical_path,
                source_url=item.source_url,
                source_version=item.source_version,
                rimdocs_metadata=self.rimdocs.get(item.canonical_path),
            )

        yield from bounded_parallel_map(
            work,
            iter_nas(root, max_files),
            self.settings.doc_workers,
            self.settings.max_inflight_docs,
        )

    def run_s3(self, source, max_files=None):
        def work(item):
            skipped = self._skip_if_unchanged(item)
            if skipped:
                return skipped
            local = source.download(item)
            try:
                return self._pipeline().process_file(
                    local,
                    canonical_path=item.canonical_path,
                    source_url=item.source_url,
                    source_version=item.source_version,
                    rimdocs_metadata=self.rimdocs.get(item.canonical_path),
                )
            finally:
                source.cleanup(local)

        yield from bounded_parallel_map(
            work,
            source.items(max_files),
            self.settings.doc_workers,
            self.settings.max_inflight_docs,
        )
