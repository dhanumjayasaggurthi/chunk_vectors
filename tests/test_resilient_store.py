import os,tempfile
from pathlib import Path
import pytest
from mir_ai.resilient_store import ResilientPostgresStore
from mir_ai.settings import Settings
from mir_ai.models import ChunkRecord


def test_ssl_unexpected_eof_is_transient():
    try:
        import psycopg2;exc=psycopg2.OperationalError('SSL error: unexpected eof while reading')
    except Exception:exc=RuntimeError('SSL error: unexpected eof while reading')
    assert ResilientPostgresStore._is_transient_db_error(exc) is True

pytestmark_integration=pytest.mark.skipif(os.environ.get('MIRAI_PG_INTEGRATION')!='1',reason='requires CI PostgreSQL/pgvector service')

@pytestmark_integration
def test_observability_claim_activation_and_embedding_preservation():
    with tempfile.TemporaryDirectory() as d:
        p=Path(d)/'config.ini';p.write_text('''[POSTGRES]\nhost=localhost\nport=5432\ndatabase=mirai_test\nuser=postgres\npassword=postgres\nschema=mirai_resilience_test\n[PATHS]\nsource_type=nas\ndocs_root=.\n[MIR_AI]\nmetadata_mode=disabled\nrequirements_mode=strict\n''')
        s=Settings.load(p);store=ResilientPostgresStore(s,minconn=1,maxconn=4)
        try:
            store.init_schema();did=store.ensure_document('mirai-object:test','s3://bucket/test.pdf');gid=store.ensure_generation(did,'hash','version','profile')
            assert store.claim_generation(gid,'worker-1',900,5) is True
            assert store.claim_generation(gid,'worker-1',900,5) is True
            assert store.generation_progress(gid)['attempt_count']==1
            chunk=ChunkRecord('chunk-1',did,gid,0,'same text',1,1,['1'],[],['paragraph']);chunk.embedding=[0.0]*3072
            store.insert_chunk_batch([chunk]);chunk2=ChunkRecord('chunk-1',did,gid,0,'same text',1,1,['1'],[],['paragraph'])
            assert store.existing_embedded_chunk_ids(gid,[chunk2])=={'chunk-1'}
            store.insert_chunk_batch([chunk2]);assert store.existing_embedded_chunk_ids(gid,[chunk2])=={'chunk-1'}
            store.activate_generation(gid,'worker-1');store.activate_generation(gid,'worker-1')
            store.create_ingestion_run('run-test','s3','s3://bucket/',1,'profile','disabled',1,0)
            store.register_run_items('run-test',[{'logical_object_id':'test','canonical_path':'s3://bucket/test.pdf','source_url':'s3://bucket/test.pdf','selected_format':'pdf','selection_reason':'PDF_AVAILABLE','status':'SELECTED'}])
            store.update_run_item('run-test','test',status='ACTIVE',stage='COMPLETE',completed=True)
            assert store.finish_ingestion_run('run-test')['ACTIVE']==1
            assert any(x['status']=='SUCCESS' for x in store.get_pipeline_stats()['by_status'])
        finally:
            try:
                with store.conn() as c:
                    with c.cursor() as cur:cur.execute('DROP SCHEMA IF EXISTS mirai_resilience_test CASCADE')
                    c.commit()
            finally:store.close()
