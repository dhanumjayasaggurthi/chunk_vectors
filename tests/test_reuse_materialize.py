"""Tests for main-based ingestion reuse; no calls to Vision or Azure."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from contextlib import closing

from processing_profile import load_profile
from reuse_materialize import compose_embedding_text, identifiers


REPO=Path(__file__).resolve().parents[1]
SAMPLE=REPO/"profiles"/"intake_reuse_immbot_example.yml"



def retained_docx(root):
    import zipfile
    from docx_evidence import capture,W
    src=root/"source.docx"
    cfg=json.loads((REPO/"profiles/docx_evidence_example.json").read_text())
    with zipfile.ZipFile(src,"w",zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml","<Types/>")
        z.writestr("word/document.xml",f'<w:document xmlns:w="{W}"><w:body>'
                   '<w:p><w:r><w:t>剂量 20 mg. '+("Clinical source text. "*250)+'</w:t></w:r></w:p>'
                   '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>First cell</w:t></w:r></w:p></w:tc>'
                   '<w:tc><w:p><w:r><w:t>Second cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
                   '<w:p><w:del><w:r><w:delText>Deleted claim</w:delText></w:r></w:del></w:p>'
                   '</w:body></w:document>')
    out=root/"retained"
    cap=capture(src,out,policy_id="test",authorized_derivatives=True,config=cfg)
    src.unlink()
    return out,cap["capture_id"],cfg


class TestConsumerIntake(unittest.TestCase):
    def test_unique_metadata_pairs_refuse_conflicting_applications(self):
        from metadata_extractor import attach_metadata
        from pdf_processor import PageContent,PageElement,BBox,ElementType
        def page(number,text):
            return PageContent(page_number=number,width=612,height=792,
                elements=[PageElement(ElementType.TEXT,BBox(20,30,500,200),text=text)])
        # Deployment YAML deliberately gets identifiers from config.ini in
        # production.resolve_configuration; supply equivalent test overrides.
        p=load_profile(str(REPO/"profiles/deployment_policy.example.yml"),[
            'extraction.schema="intake_ci"','extraction.table="cv_test_pages"',
            'extracted_content.schema="intake_ci"','extracted_content.table="cv_test_pages"',
            'output.schema="intake_ci"','output.table="cv_test_chunks"',
            'output.text_contract="english_v1"',
        ])
        rules=p["metadata"]
        document=SimpleNamespace(pages=[page(1,"Protocol Number: ABC-2026-001\nBLA 12345"),
            page(2,"BLA 12345"),page(4001,"NDA 98765")])
        result=attach_metadata(document,[],rules)
        self.assertEqual(result["application_type"],"BLA")
        self.assertEqual(result["application_number"],"12345")
        self.assertEqual(result["protocol_number"],"ABC-2026-001")
        self.assertEqual(document.metadata_evidence["application_number"]["page"],1)
        document.pages.insert(1,page(3,"NDA 98765"))
        result=attach_metadata(document,[],rules)
        self.assertNotIn("application_number",result)
        self.assertNotIn("application_type",result)
        document.pages=[page(1,"Asset ABC-2026-001; compound gMG")]
        self.assertNotIn("protocol_number",attach_metadata(document,[],rules))
        self.assertNotIn("indication_code",attach_metadata(document,[],rules))
        rules["document"]["indication_code"]["enabled"]=True
        self.assertNotIn("indication_code",attach_metadata(document,[],rules))
        document.pages=[page(1,"Indication Code: gMG")]
        self.assertEqual(attach_metadata(document,[],rules)["indication_code"],"gMG")
        rules["document"]["indication_code"]["enabled"]=False
        self.assertNotIn("indication_code",attach_metadata(document,[],rules))

    def test_docx_preview_logical_spans_and_review_exclusion(self):
        from reuse_materialize import prepare_docx_document
        from docx_evidence import iter_logical_blocks
        with tempfile.TemporaryDirectory() as tmp:
            root,cid,cfg=retained_docx(Path(tmp))
            profile=load_profile(str(SAMPLE))
            args=dict(config=cfg,doc_id="docx:test",file_name="source.docx")
            rows,report=prepare_docx_document(root,cid,profile,**args)
            self.assertEqual(report["quality"]["structural_errors"],0)
            self.assertEqual(report["withheld_review_blocks"],1)
            self.assertIn("document.protocol_number",report["omitted_optional_pdf_metadata_rules"])
            self.assertEqual(rows,prepare_docx_document(root,cid,profile,**args)[0])
            self.assertTrue(all(row[8:10]==(None,None) for row in rows))
            blocks={b["block_id"]:"".join(s["text"] for s in b["segments"])
                    for b in iter_logical_blocks(root,cid,config=cfg)}
            for row in rows:
                meta=json.loads(row[13])
                self.assertEqual(meta["source_format"],"docx")
                parts=[blocks[s["block_id"]][s["char_start"]:s["char_end"]]
                       for s in meta["logical_spans"]]
                self.assertEqual("\n".join(parts),row[6])
                self.assertNotIn("Deleted claim",row[6])
                self.assertFalse("First cell" in row[6] and "Second cell" in row[6])
            with self.assertRaisesRegex(ValueError,"namespaced"):
                prepare_docx_document(root,cid,profile,**{**args,"doc_id":"pdf-id"})
            profile["metadata"]["document"]["protocol_number"]["required"]=True
            with self.assertRaisesRegex(ValueError,"Required PDF metadata"):
                prepare_docx_document(root,cid,profile,**args)

    def test_yaml_input_fields_and_existing_source_mapping(self):
        profile=load_profile(str(SAMPLE))
        self.assertEqual(profile["chunking"]["max_tokens"],800)
        self.assertEqual(profile["chunking"]["min_tokens"],100)
        self.assertEqual(profile["chunking"]["overlap_tokens"],80)
        self.assertIn("document.protocol_number",profile["embedding"]["input_fields"])
        self.assertEqual(profile["extracted_content"]["position_column"],"chunk_index")
        self.assertNotEqual(identifiers(profile)["source"],identifiers(profile)["table"])

    def test_input_composition_is_ordered_and_preserves_original(self):
        chunk=SimpleNamespace(chunk_text="Results were clinically relevant.",
                              section_title="3. Results",file_name="study.pdf",
                              file_path="nas://study.pdf",
                              document_metadata={"protocol_number":"ABC-123"},
                              chunk_metadata={"classification":"sensitive"})
        original=chunk.chunk_text
        result=compose_embedding_text(
            chunk,["document.protocol_number","source.product",
                   "section_title","chunk_text"],
            {"product":"Compound X"},max_tokens=1000)
        self.assertIn("ABC-123",result)
        self.assertIn("Compound X",result)
        self.assertIn("3. Results",result)
        self.assertTrue(result.endswith(original))
        self.assertEqual(chunk.chunk_text,original)

    def test_input_does_not_fabricate_missing_metadata_or_truncate(self):
        chunk=SimpleNamespace(chunk_text="Clinical text",section_title="",
                              file_name="sample.pdf",file_path="x",
                              document_metadata={},chunk_metadata={})
        result=compose_embedding_text(
            chunk,["document.protocol_number","chunk_text"],{},max_tokens=100)
        self.assertEqual(result,"Clinical text")
        with self.assertRaisesRegex(ValueError,"exceeds"):
            compose_embedding_text(chunk,["chunk_text"],{},max_tokens=1)

    def test_reject_same_source_output(self):
        profile=load_profile(str(SAMPLE))
        profile["output"]["schema"]=profile["extracted_content"]["schema"]
        profile["output"]["table"]=profile["extracted_content"]["table"]
        with self.assertRaisesRegex(ValueError,"different tables"):
            identifiers(profile)

    def test_ambiguous_extracted_row_order_fails_closed(self):
        from extracted_source import load_extracted_document
        from unittest.mock import patch
        class FakeCursor:
            def __init__(self):
                self.calls = 0
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def execute(self, *args):
                pass
            def fetchmany(self, size):
                self.calls += 1
                return [("first paragraph", 0), ("second paragraph", 0)] if self.calls == 1 else []
        class FakeConnection:
            def cursor(self, *, name=None):
                self.cursor_name=name
                return FakeCursor()
            def rollback(self):
                pass
            def close(self):
                pass
        profile = {"extracted_content": {
            "schema": "palm_paw", "table": "existing_chunks",
            "doc_id_column": "doc_id", "text_column": "chunk_text",
            "position_column": "chunk_index",
        }}
        with patch("extracted_source.get_conn", return_value=FakeConnection()):
            with self.assertRaisesRegex(ValueError, "Ambiguous extracted row order"):
                load_extracted_document(profile, "doc_1")

    def test_server_cursor_fingerprint_matches_full_validating_load(self):
        from extracted_source import fingerprint_extracted_document,load_extracted_document
        from unittest.mock import patch
        elements=lambda text:[{"type":"text","bbox":[0,0,100,20],"text":text,
                               "confidence":1.0,"reading_order":0}]
        source=[
            ("Alpha",1,1,"nas://fixed.pdf","fixed.pdf","Section A",elements("Alpha"),612,792,0),
            ("Beta",2,2,"nas://fixed.pdf","fixed.pdf","Section A",elements("Beta"),612,792,0),
        ]
        cursors=[]
        class FakeCursor:
            def __init__(self):
                self.offset=0
                self.itersize=None
                cursors.append(self)
            def __enter__(self): return self
            def __exit__(self,*args): return False
            def execute(self,*args): pass
            def fetchmany(self,size):
                batch=source[self.offset:self.offset+size]
                self.offset+=len(batch)
                return batch
        class FakeConnection:
            def cursor(self,*,name=None):
                self.cursor_name=name
                return FakeCursor()
            def rollback(self): pass
            def close(self): pass
        profile={"extracted_content":{
            "schema":"palm_paw","table":"pages","doc_id_column":"doc_id",
            "text_column":"chunk_text","page_column":"page_number",
            "position_column":"page_number","page_end_column":"page_end",
            "file_path_column":"file_path","file_name_column":"file_name",
            "section_column":"section_title","element_json_column":"elements",
            "width_column":"page_width","height_column":"page_height",
            "rotation_column":"page_rotation","source_columns":{"source_file_name":"file_name"},
        }}
        with patch("extracted_source.get_conn",side_effect=lambda:FakeConnection()):
            struct,full=load_extracted_document(profile,"fixed-doc",batch_size=1)
            bounded=fingerprint_extracted_document(profile,"fixed-doc",batch_size=1)
        self.assertEqual(full,bounded)
        self.assertEqual([p.raw_text for p in struct.pages],["Alpha","Beta"])
        self.assertEqual(struct.source_metadata,{"source_file_name":"fixed.pdf"})
        self.assertEqual([cursor.itersize for cursor in cursors],[1,1])

    def test_duplicate_yaml_keys_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"bad.yml"
            path.write_text("version: 1\nversion: 1\n",encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"duplicate"):
                load_profile(str(path))


@unittest.skipUnless(
    os.getenv("INGESTION_REUSE_TEST_DB")=="1" and os.getenv("PGDATABASE")=="embedding_test",
    "Requires explicitly authorized disposable embedding_test PostgreSQL",
)
class TestPostgresRechunkIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg2
        cls.connect=staticmethod(lambda: psycopg2.connect(
            host=os.environ["PGHOST"],port=os.environ.get("PGPORT","5432"),
            dbname=os.environ["PGDATABASE"],user=os.environ["PGUSER"],
            password=os.environ["PGPASSWORD"]))
        conn=cls.connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cur.execute("CREATE SCHEMA IF NOT EXISTS intake_ci")
                for table in ("out_chunks_v1_failures","out_chunks_v1_status",
                              "out_chunks_v1","source_chunks_docs","source_chunks"):
                    cur.execute('DROP TABLE IF EXISTS intake_ci."' + table + '" CASCADE')
                cur.execute("""
                    CREATE TABLE intake_ci.source_chunks(
                      doc_id text NOT NULL, chunk_index integer NOT NULL,
                      chunk_text text NOT NULL, page_start integer,
                      page_end integer, section_title text, file_path text,
                      file_name text,
                      PRIMARY KEY(doc_id,chunk_index))
                """)
                cur.execute("""CREATE TABLE intake_ci.source_chunks_docs(
                    doc_id text PRIMARY KEY, source_hash text NOT NULL,
                    extraction_hash text NOT NULL, completed_at timestamptz NOT NULL DEFAULT now())""")
                text="Protocol Number: ABC-123\n1 STUDY DESIGN\n" + "Study information. "*80
                cur.execute("""
                    INSERT INTO intake_ci.source_chunks VALUES
                    ('doc_ok',0,%s,1,1,'Study Design','nas://doc_ok.pdf','doc_ok.pdf'),
                    ('doc_ok',1,%s,2,3,'Study Design','nas://doc_ok.pdf','doc_ok.pdf'),
                    ('doc_bad',0,%s,1,1,'Overview','nas://doc_bad.pdf','first.pdf'),
                    ('doc_bad',1,%s,2,2,'Overview','nas://doc_bad.pdf','other.pdf')
                """,(text,"Continuation of study information. "*90,
                     text,"Additional information. "*90))
        conn.close()
        p=load_profile(str(SAMPLE))
        p["extracted_content"]["schema"]="intake_ci"
        p["extracted_content"]["table"]="source_chunks"
        p["output"]["schema"]="intake_ci"
        p["output"]["table"]="out_chunks_v1"
        # Recompute stable profile fingerprint using the identical
        # canonicalization used by processing_profile.load_profile.
        p.pop("_fingerprint")
        p["_fingerprint"]=hashlib.sha256(
            json.dumps(p,sort_keys=True,separators=(",",":"),ensure_ascii=False
                      ).encode("utf-8")
        ).hexdigest()
        cls.profile=p

    @classmethod
    def tearDownClass(cls):
        conn=cls.connect()
        try:
            with conn:
                with conn.cursor() as cur:
                    for name in ("out_chunks_v1_failures","out_chunks_v1_status",
                                 "out_chunks_v1","source_chunks_docs","source_chunks"):
                        cur.execute('DROP TABLE IF EXISTS intake_ci."'+name+'" CASCADE')
            conn.close()
        except Exception:
            conn.rollback()
            conn.close()
            raise

    def test_atomic_skip_failure_recovery_and_embedding_readiness(self):
        from reuse_materialize import init_output, run,run_one,verify_output
        p=self.profile
        self.assertEqual(init_output(p,apply=False)["status"],"NO_WRITES")
        init_output(p,apply=True)
        result=run(p,max_docs=10,workers=2)
        conn_debug=self.connect()
        with conn_debug:
            with conn_debug.cursor() as cur:
                cur.execute("SELECT doc_id,error_message FROM intake_ci.out_chunks_v1_failures ORDER BY id")
                failures_debug=cur.fetchall()
        conn_debug.close()
        self.assertEqual(result["SUCCESS"],1, (result,failures_debug))
        self.assertEqual(result["FAILED"],1, (result,failures_debug))
        # A current document uses the bounded digest replay and never rebuilds
        # its complete page/element graph.
        from unittest.mock import patch
        with patch("extracted_source.load_extracted_document",
                   side_effect=AssertionError("current replay rebuilt the document")):
            self.assertEqual(run_one("doc_ok",p,verify_output(p),source_batch_rows=1)["status"],
                             "SKIPPED")
        conn=self.connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT COUNT(*),bool_and(chunk_vector IS NULL)
                    FROM intake_ci.out_chunks_v1""")
                count, no_vectors=cur.fetchone()
                self.assertGreater(count,0)
                self.assertTrue(no_vectors)
                cur.execute("""SELECT chunk_text,source_chunk_text,document_metadata,
                    metadata_evidence,page_end FROM intake_ci.out_chunks_v1
                    WHERE doc_id='doc_ok' ORDER BY chunk_index LIMIT 1""")
                composed,original,meta,evidence,page_end=cur.fetchone()
                self.assertIn("ABC-123",composed)
                self.assertNotIn("document / protocol",original)
                self.assertEqual(meta["protocol_number"],"ABC-123")
                self.assertEqual(evidence["protocol_number"]["page"],1)
                cur.execute("""SELECT COUNT(*) FROM intake_ci.out_chunks_v1_failures
                    WHERE doc_id='doc_bad'""")
                self.assertEqual(cur.fetchone()[0],1)
                # Repair the one poisoned document without touching successful rows.
                cur.execute("""UPDATE intake_ci.source_chunks
                    SET file_name='first.pdf' WHERE doc_id='doc_bad'""")
        conn.close()
        # Verify rollback and reader visibility after a batch actually inserts.
        from extracted_source import load_extracted_document
        from psycopg2.extras import execute_values
        from reuse_materialize import iter_prepared_document_rows, prepare_document, save_document_stream
        struct,source_hash=load_extracted_document(p,"doc_ok",batch_size=1)
        prepared=prepare_document(struct,p)
        names = verify_output(p)

        def published():
            with closing(self.connect()) as conn, conn.cursor() as cur:
                cur.execute(f"SELECT chunk_id,chunk_text FROM {names['table']} WHERE doc_id='doc_ok' ORDER BY chunk_index")
                output = cur.fetchall()
                cur.execute(f"SELECT source_hash,chunk_count FROM {names['status']} WHERE doc_id='doc_ok'")
                return output, cur.fetchone()

        before = published()
        inserted = []

        def observed_insert(cur, sql, batch, **kwargs):
            execute_values(cur, sql, batch, **kwargs)
            inserted.append(len(batch))

        def interrupted():
            yield prepared[0][:7] + ("uncommitted replacement",) + prepared[0][8:]
            self.assertEqual(inserted, [1])
            self.assertEqual(published(), before)
            raise RuntimeError("injected publication failure")

        with patch("psycopg2.extras.execute_values", side_effect=observed_insert):
            with self.assertRaisesRegex(RuntimeError,"injected publication failure"):
                save_document_stream("doc_ok",source_hash,p,names,interrupted(),batch_size=1)
        self.assertEqual(published(), before)

        # Replace the native revision during a pinned materialization while
        # keeping the original PDF hash unchanged. The prior output survives.
        pinned = "a" * 64
        captured_profile = {**p, "extraction": {
            "enabled": True, "schema": "intake_ci", "table": "source_chunks",
        }}
        with closing(self.connect()) as conn, conn, conn.cursor() as cur:
            cur.execute("INSERT INTO intake_ci.source_chunks_docs VALUES ('doc_ok',%s,%s,now())",
                        (pinned,"b" * 64))
            cur.execute("SELECT chunk_text FROM intake_ci.source_chunks WHERE doc_id='doc_ok' AND chunk_index=1")
            original = cur.fetchone()[0]
            cur.execute("UPDATE intake_ci.source_chunks SET chunk_text=%s WHERE doc_id='doc_ok' AND chunk_index=1",
                        (original + " An updated paragraph.",))

        def republish_during_write(*args, **kwargs):
            rows = iter_prepared_document_rows(*args, **kwargs)
            yield next(rows)
            with closing(self.connect()) as conn, conn, conn.cursor() as cur:
                cur.execute("UPDATE intake_ci.source_chunks_docs SET extraction_hash=%s WHERE doc_id='doc_ok'",
                            ("c" * 64,))
            yield from rows

        with patch("reuse_materialize.iter_prepared_document_rows", side_effect=republish_during_write):
            failed = run_one("doc_ok",captured_profile,names,expected_source_hash=pinned,
                             source_batch_rows=1,write_batch_rows=1)
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(published(), before)
        with closing(self.connect()) as conn, conn, conn.cursor() as cur:
            cur.execute(f"SELECT error_message FROM {names['failures']} WHERE doc_id='doc_ok'")
            self.assertIn("Native version changed during materialization", cur.fetchone()[0])
            cur.execute("UPDATE intake_ci.source_chunks SET chunk_text=%s WHERE doc_id='doc_ok' AND chunk_index=1",
                        (original,))
        result2=run(p,max_docs=10,workers=2)
        self.assertEqual(result2["SUCCESS"],1)
        self.assertEqual(result2["SKIPPED"],1)
        self.assertEqual(result2["FAILED"],0)
        conn=self.connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT COUNT(*) FROM intake_ci.out_chunks_v1
                    WHERE doc_id='doc_bad'""")
                self.assertGreater(cur.fetchone()[0],0)
                cur.execute("""SELECT max(page_end) FROM intake_ci.out_chunks_v1
                    WHERE doc_id='doc_ok'""")
                self.assertEqual(cur.fetchone()[0],3)
        conn.close()
        # Use the SAME initialized output table, profile, and transaction;
        # preserve physical PDF citations and publish NULL DOCX page values.
        from reuse_materialize import run_docx
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root,cid,cfg=retained_docx(Path(tmp))
            kw=dict(config=cfg,doc_id="docx:ci",file_name="source.docx")
            with self.assertRaises(PermissionError):
                run_docx(root,cid,p,**kw)
            first=run_docx(root,cid,p,allow_logical_locators=True,**kw)
            self.assertEqual(first["status"],"SUCCESS")
            with patch("reuse_materialize.save_document",side_effect=AssertionError("unexpected rewrite")):
                self.assertEqual(run_docx(root,cid,p,allow_logical_locators=True,**kw)["status"],"SKIPPED")
            conn=self.connect()
            try:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute("""SELECT count(*),bool_and(page_start IS NULL AND page_end IS NULL),
                                       bool_and(chunk_metadata->>'source_format'='docx')
                                       FROM intake_ci.out_chunks_v1 WHERE doc_id='docx:ci'""")
                        self.assertEqual(cur.fetchone(),(first["prepared_chunks"],True,True))
                        cur.execute("SELECT max(page_end) FROM intake_ci.out_chunks_v1 WHERE doc_id='doc_ok'")
                        self.assertEqual(cur.fetchone()[0],3)
                        cur.execute("""DELETE FROM intake_ci.out_chunks_v1 WHERE doc_id='docx:ci'
                                       AND chunk_index=0""")
                with self.assertRaisesRegex(ValueError,"reconciliation"):
                    run_docx(root,cid,p,allow_logical_locators=True,**kw)
            finally:
                conn.close()


if __name__=="__main__":
    unittest.main()
