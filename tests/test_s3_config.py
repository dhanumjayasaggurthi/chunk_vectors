import configparser

import boto3

from mir_ai.batch import S3Source


class _FakeClient:
    pass


class _FakeSession:
    calls = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeSession.calls.append(("session", kwargs))

    def client(self, service_name, **kwargs):
        _FakeSession.calls.append(("client", service_name, kwargs))
        return _FakeClient()


def _write_config(path, values):
    cfg = configparser.ConfigParser()
    cfg["s3"] = {
        "bucket": "test-bucket",
        "prefix": "landing/",
        "region": "us-east-1",
        "profile": values.get("profile", ""),
        "access_key_id": values.get("access_key_id", ""),
        "secret_access_key": values.get("secret_access_key", ""),
        "session_token": values.get("session_token", ""),
        "temp_dir": str(path.parent / "scratch"),
    }
    with path.open("w", encoding="utf-8") as handle:
        cfg.write(handle)


def test_existing_profile_pattern_is_preserved(monkeypatch, tmp_path):
    _FakeSession.calls = []
    monkeypatch.setattr(boto3, "Session", _FakeSession)
    config = tmp_path / "config.ini"
    _write_config(
        config,
        {
            "profile": "rimdocs_prod",
            "secret_access_key": "legacy-unused-value",
        },
    )

    S3Source(str(config))

    session_call = _FakeSession.calls[0][1]
    assert session_call["profile_name"] == "rimdocs_prod"
    assert session_call["region_name"] == "us-east-1"
    assert "aws_access_key_id" not in session_call


def test_complete_static_keys_are_supported_without_architecture_change(monkeypatch, tmp_path):
    _FakeSession.calls = []
    monkeypatch.setattr(boto3, "Session", _FakeSession)
    config = tmp_path / "config.ini"
    _write_config(
        config,
        {
            "profile": "rimdocs_prod",
            "access_key_id": "AKIA_TEST",
            "secret_access_key": "SECRET_TEST",
            "session_token": "TOKEN_TEST",
        },
    )

    S3Source(str(config))

    session_call = _FakeSession.calls[0][1]
    assert session_call["aws_access_key_id"] == "AKIA_TEST"
    assert session_call["aws_secret_access_key"] == "SECRET_TEST"
    assert session_call["aws_session_token"] == "TOKEN_TEST"
    assert "profile_name" not in session_call
