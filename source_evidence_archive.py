"""Optional, source-faithful PDF/DOCX byte preservation for a purge window.

This is a filesystem integrity primitive, NOT a retention-policy approval,
a durable object-store integration, or proof that legal purge rules permit
keeping the source. Nothing runs automatically. It performs no cloud calls
or PostgreSQL writes. Caller MUST explicitly authorize retaining originals.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path


SUFFIXES={".pdf",".docx"}
MANIFEST_SCHEMA="source-evidence-sha256-v1"
CHUNK_BYTES=8*1024*1024


def digest_file(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as fh:
        for data in iter(lambda:fh.read(CHUNK_BYTES),b""):
            h.update(data)
    return h.hexdigest()


def _validate_hash(source_hash):
    if (not isinstance(source_hash,str) or len(source_hash)!=64
            or any(c not in "0123456789abcdef" for c in source_hash)):
        raise ValueError("Expected lowercase 64-character source byte SHA-256")


def _location(root,source_hash,suffix):
    _validate_hash(source_hash)
    if suffix not in SUFFIXES:
        raise ValueError("Only original PDF/DOCX content can be archived")
    base=Path(root)
    return (
        base/"sha256"/source_hash[:2]/(source_hash+suffix),
        base/"manifests"/source_hash[:2]/(source_hash+".json"),
    )


def _write_once(destination,data):
    """Publish atomically without silently replacing another writer's bytes.

    Hardlinks are deliberately required: they are same-filesystem and
    create-if-absent atomic. Storage without that guarantee needs a proper
    object-store conditional-put adapter, not an unsafe overwrite fallback.
    """
    destination.parent.mkdir(parents=True,exist_ok=True)
    descriptor,temp=tempfile.mkstemp(
        dir=destination.parent,prefix="."+destination.name+".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor,"wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        try:
            os.link(temp,destination)
        except FileExistsError:
            if destination.read_bytes()!=data:
                raise RuntimeError("Existing evidence manifest differs")
    finally:
        Path(temp).unlink(missing_ok=True)


def verify(root,source_hash,*,suffix=None):
    """Read-only full byte verification by checksum, including after purge."""
    _validate_hash(source_hash)
    root=Path(root).resolve(strict=True)
    manifest_path=root/"manifests"/source_hash[:2]/(source_hash+".json")
    manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
    if (not isinstance(manifest,dict)
            or manifest.get("schema")!=MANIFEST_SCHEMA
            or manifest.get("source_sha256")!=source_hash
            or manifest.get("suffix") not in SUFFIXES
            or type(manifest.get("bytes")) is not int
            or manifest["bytes"]<=0):
        raise ValueError("Archive manifest failed source integrity validation")
    extension=manifest["suffix"]
    if suffix is not None and suffix!=extension:
        raise ValueError("Expected source extension differs from archive manifest")
    blob,expected_manifest=_location(root,source_hash,extension)
    if manifest_path!=expected_manifest:
        raise ValueError("Unexpected manifest storage path")
    if blob.stat().st_size!=manifest["bytes"]:
        raise ValueError("Archived source byte count mismatch")
    if digest_file(blob)!=source_hash:
        raise ValueError("Archived source SHA-256 mismatch")
    return {
        "source_sha256":source_hash,"bytes":manifest["bytes"],
        "suffix":extension,"archive_path":str(blob),
        "content_verified":True,
        "durable_storage_verified":False,
        "purge_safe":False,
        "note":"Filesystem checksum verified; retention permission, backup,"
               " durable storage, source completeness and downstream evidence"
               " readiness require independent gates",
    }


def capture(source,root,*,authorized_retention=False):
    """Copy unchanged source bytes only with explicit authorization.

    Source remains unchanged; no source-deletion or self-enabling behavior.
    """
    if authorized_retention is not True:
        raise PermissionError(
            "A source copy is forbidden without --authorized-retention"
        )
    source=Path(source).resolve(strict=True)
    if not source.is_file() or source.suffix.lower() not in SUFFIXES:
        raise ValueError("Expected an existing original PDF or DOCX source")
    state=source.stat()
    if state.st_size<=0:
        raise ValueError("Cannot archive an empty source")
    source_hash=digest_file(source)
    blob,manifest=_location(root,source_hash,source.suffix.lower())
    blob.parent.mkdir(parents=True,exist_ok=True)
    if blob.exists():
        if blob.stat().st_size!=state.st_size or digest_file(blob)!=source_hash:
            raise RuntimeError("Existing source archive is corrupted")
    else:
        descriptor,temp=tempfile.mkstemp(
            dir=blob.parent,prefix="."+blob.name+".",suffix=".tmp",
        )
        try:
            copy_hash=hashlib.sha256()
            copied=0
            with os.fdopen(descriptor,"wb") as writer,source.open("rb") as reader:
                for data in iter(lambda:reader.read(CHUNK_BYTES),b""):
                    writer.write(data)
                    copy_hash.update(data)
                    copied+=len(data)
                writer.flush()
                os.fsync(writer.fileno())
            now=source.stat()
            if ((state.st_size,state.st_mtime_ns)!=(now.st_size,now.st_mtime_ns)
                    or copied!=state.st_size or copy_hash.hexdigest()!=source_hash
                    or digest_file(source)!=source_hash):
                raise RuntimeError("Original source changed during archive copy")
            try:
                os.link(temp,blob)
            except FileExistsError:
                if blob.stat().st_size!=copied or digest_file(blob)!=source_hash:
                    raise RuntimeError("Concurrent archive source differs")
        finally:
            Path(temp).unlink(missing_ok=True)
    final_state=source.stat()
    if ((final_state.st_size,final_state.st_mtime_ns)
            !=(state.st_size,state.st_mtime_ns)
            or digest_file(source)!=source_hash):
        raise RuntimeError("Original source changed during archive verification")
    record={
        "schema":MANIFEST_SCHEMA,
        "source_sha256":source_hash,
        "suffix":source.suffix.lower(),
        "bytes":state.st_size,
    }
    payload=(json.dumps(record,sort_keys=True,separators=(",",":"))+"\n").encode("utf-8")
    _write_once(manifest,payload)
    return verify(root,source_hash,suffix=source.suffix.lower())


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("capture","verify"))
    parser.add_argument("--root",required=True,
                        help="Explicit authorized storage root (not temporary storage)")
    parser.add_argument("--file",help="Original local source, only for capture")
    parser.add_argument("--sha256",help="Original source byte hash, for verify")
    parser.add_argument("--authorized-retention",action="store_true",
                        help="Explicit confirmation that source byte retention is permitted")
    args=parser.parse_args(argv)
    if args.action=="capture":
        if not args.file or args.sha256:
            parser.error("capture requires --file and no --sha256")
        if not args.authorized_retention:
            parser.error("capture requires explicit --authorized-retention")
        result=capture(args.file,args.root,authorized_retention=True)
    else:
        if not args.sha256 or args.file or args.authorized_retention:
            parser.error("verify requires --sha256 and cannot copy source")
        result=verify(args.root,args.sha256)
    print(json.dumps(result,indent=2))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
