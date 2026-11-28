"""No-cloud, source-bytes preservation tests for the 15-day purge boundary."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from source_evidence_archive import capture,digest_file,verify,main


class SourceArchiveTests(unittest.TestCase):
    def test_capture_is_explicit_lossless_idempotent_and_verifies_after_purge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source=root/"intake"/"original.pdf"
            source.parent.mkdir()
            contents=b"%PDF-1.7\n" + b"\0original binary evidence\n"*8192
            source.write_bytes(contents)
            expected=hashlib.sha256(contents).hexdigest()
            storage=root/"authorized-capture"
            with self.assertRaises(PermissionError):
                capture(source,storage)
            self.assertFalse(storage.exists())
            first=capture(source,storage,authorized_retention=True)
            self.assertTrue(first["content_verified"])
            self.assertFalse(first["purge_safe"])
            self.assertFalse(first["durable_storage_verified"])
            self.assertEqual(first["source_sha256"],expected)
            self.assertEqual(first["bytes"],len(contents))
            self.assertEqual(contents,source.read_bytes())
            again=capture(source,storage,authorized_retention=True)
            self.assertEqual(first,again)
            source.unlink()
            recovered=verify(storage,expected)
            self.assertEqual(recovered["source_sha256"],expected)
            self.assertEqual(Path(recovered["archive_path"]).read_bytes(),contents)

    def test_corrupted_archive_is_rejected_and_original_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source=root/"sample.docx"
            source.write_bytes(b"PK\x03\x04" + b"source-data"*200)
            storage=root/"kept"
            entry=capture(source,storage,authorized_retention=True)
            blob=Path(entry["archive_path"])
            blob.write_bytes(b"damaged bytes")
            with self.assertRaisesRegex(ValueError,"byte count|SHA-256"):
                verify(storage,entry["source_sha256"])
            with self.assertRaisesRegex(RuntimeError,"corrupted"):
                capture(source,storage,authorized_retention=True)
            self.assertEqual(digest_file(source),entry["source_sha256"])

    def test_archive_rejects_unknown_format_and_invalid_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source=root/"not_document.txt"
            source.write_text("some text")
            with self.assertRaisesRegex(ValueError,"original PDF or DOCX"):
                capture(source,root/"out",authorized_retention=True)
            with self.assertRaisesRegex(ValueError,"SHA-256"):
                verify(root,"bad")
            self.assertFalse((root/"out").exists())

    def test_cli_verify_read_only_and_capture_fails_without_permission(self):
        from contextlib import redirect_stdout
        from io import StringIO
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source=root/"doc.pdf"
            source.write_bytes(b"%PDF-source")
            storage=root/"evidence"
            with self.assertRaises(SystemExit) as blocked:
                main(["capture","--file",str(source),"--root",str(storage)])
            self.assertNotEqual(blocked.exception.code,0)
            output=StringIO()
            with redirect_stdout(output):
                self.assertEqual(main([
                    "capture","--file",str(source),"--root",str(storage),
                    "--authorized-retention",
                ]),0)
            captured=json.loads(output.getvalue())
            output=StringIO()
            with redirect_stdout(output):
                self.assertEqual(main([
                    "verify","--root",str(storage),
                    "--sha256",captured["source_sha256"],
                ]),0)
            self.assertEqual(json.loads(output.getvalue()),captured)


if __name__=="__main__":
    unittest.main()
