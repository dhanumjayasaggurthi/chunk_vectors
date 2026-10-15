"""Page-chunk processor with atomic checkpoints and restart safety.

The runner is a minimal local reference. Production adapters should implement
the same checkpoint protocol with S3 and durable database task leases.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Callable, Iterable
from .core import Evidence, DocumentResult, Status, validate_fields


def checkpoint_path(directory: Path, identity: str, chunk: int) -> Path:
    if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
        raise ValueError("Use the SHA-256 extraction identity")
    if chunk < 0:
        raise ValueError("Negative chunk")
    return directory / identity / f"{chunk:08d}.json"


def save_checkpoint(path: Path, pages: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        temp_name = tmp.name
        json.dump(pages, tmp, ensure_ascii=False)
        tmp.flush()
        os.fsync(tmp.fileno())
    os.replace(temp_name, path)


def load_checkpoint(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        pages = json.load(fh)
    if not isinstance(pages, list):
        raise ValueError("Invalid checkpoint")
    return pages


def process(
    document_id: str,
    source_version: str,
    identity: str,
    total_pages: int,
    read_pages: Callable[[int, int], list[dict]],
    extract: Callable[[dict[int, str]], dict[str, Evidence]],
    checkpoint_dir: Path,
    chunk_pages: int = 64,
) -> DocumentResult:
    if total_pages <= 0 or chunk_pages <= 0:
        raise ValueError("Invalid page count or chunk size")
    source_pages: dict[int, str] = {}
    for chunk, start in enumerate(range(1, total_pages + 1, chunk_pages)):
        end = min(total_pages, start + chunk_pages - 1)
        path = checkpoint_path(checkpoint_dir, identity, chunk)
        if path.is_file():
            block = load_checkpoint(path)
        else:
            block = read_pages(start, end)
            expected = list(range(start, end + 1))
            if [p.get("page") for p in block] != expected:
                raise ValueError("Page coverage mismatch")
            save_checkpoint(path, block)
        if [p.get("page") for p in block] != list(range(start, end + 1)):
            raise ValueError("Corrupt checkpoint coverage")
        for page in block:
            source_pages[page["page"]] = page["text"]
    candidates = extract(source_pages)
    fields, issues = validate_fields(candidates, source_pages)
    return DocumentResult(
        document_id=document_id,
        source_version=source_version,
        status=Status.REVIEW_REQUIRED if issues else Status.COMPLETED,
        fields=fields,
        issues=issues,
    )
