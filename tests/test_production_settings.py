import tempfile
from pathlib import Path
from mir_ai.settings import Settings


def test_production_resilience_controls_load_from_config():
    config='''[POSTGRES]\nhost=localhost\nport=5432\ndatabase=regulatory\nuser=u\npassword=p\nschema=dev_raw\n[PATHS]\ndocs_root=.\nsource_type=s3\nlog_dir=custom_logs\n[MIR_AI]\nmetadata_mode=optional\nsource_auto_run=true\nsource_max_files=100\nauto_init_db=false\ngeneration_max_attempts=7\nbatch_continue_on_error=true\nbatch_retry_transient_errors=true\nbatch_retry_attempts=3\nbatch_retry_base_seconds=1.5\nbatch_exit_nonzero_on_error=true\nprogress_log_every_pages=10\ndb_pool_maxconn=20\ndb_max_retries=6\ndb_retry_base_seconds=0.5\ndb_connect_timeout_s=20\ndb_keepalives_idle_s=20\ndb_keepalives_interval_s=5\ndb_keepalives_count=4\napi_max_retries=6\napi_retry_base_seconds=1.5\napi_timeout_s=180\npreflight_enabled=true\npreflight_embedding=true\npreflight_chat=true\npreflight_vision=false\nrequirements_mode=strict\n[LOGGING]\nlog_max_bytes=20971520\nlog_backup_count=7\nlog_queue_size=5000\nlog_console=false\nlog_console_json=false\n'''
    with tempfile.TemporaryDirectory() as d:
        p=Path(d)/'config.ini';p.write_text(config);s=Settings.load(p);s.validate()
    assert s.source_max_files==100 and s.generation_max_attempts==7 and s.batch_retry_attempts==3
    assert s.db_pool_maxconn==20 and s.db_max_retries==6 and s.api_max_retries==6
    assert s.preflight_enabled is True and s.preflight_vision is False and s.log_console is False


def test_requirements_warn_mode_marks_noncompliant_embedding_config():
    config='''[POSTGRES]\nhost=localhost\ndatabase=regulatory\nuser=u\npassword=p\nschema=dev_raw\n[PATHS]\nsource_type=nas\ndocs_root=.\n[MIR_AI]\nrequirements_mode=warn\nembedding_model=text-embedding-3-large\nembedding_dim=3072\nembedding_api_version=2024-02-01\n'''
    with tempfile.TemporaryDirectory() as d:
        p=Path(d)/'config.ini';p.write_text(config);s=Settings.load(p);s.validate()
    assert s.requirement_compliant_embedding_config is False
