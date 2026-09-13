import tempfile
from pathlib import Path
from mir_ai.settings import Settings
from mir_ai.profile import processing_fingerprint, validate_runtime


def _settings(tmp: Path, extra: str = "") -> Settings:
    cfg = f'''[database]\nhost=localhost\nport=5432\nname=regulatory\nuser=u\npassword=p\nschema=dev_raw\nfolder=d05\n[paths]\ndocs_root=.\n[processing]\npage_workers=2\n{extra}\n'''
    p = tmp / ("config_" + str(abs(hash(extra))) + ".ini")
    p.write_text(cfg)
    return Settings.load(p)


def test_processing_fingerprint_is_stable_and_changes_for_output_settings():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        a = _settings(root)
        b = _settings(root)
        c = _settings(root, "chunk_target_max_tokens=1400")
        assert processing_fingerprint(a) == processing_fingerprint(b)
        assert processing_fingerprint(a) != processing_fingerprint(c)


def test_heartbeat_must_be_shorter_than_lease():
    with tempfile.TemporaryDirectory() as d:
        s = _settings(Path(d), "lease_seconds=60\nheartbeat_seconds=60")
        try:
            validate_runtime(s)
        except ValueError as exc:
            assert "heartbeat_seconds" in str(exc)
        else:
            raise AssertionError("expected invalid heartbeat/lease configuration")
