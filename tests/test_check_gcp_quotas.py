"""Test Vision-compatible service-account path selection and safe quota errors.

No real credential keys or network calls are used.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.check_gcp_quotas import (
    QuotaInspectionError, _api_error, credential_file,
    inspect_quotas, resolve_identity,
)


class FakeCredential:
    project_id = "vision-project"
    service_account_email = "vision-svc@vision-project.iam.gserviceaccount.com"


class FakeResponse:
    def __init__(self, code, payload):
        self.status_code = code
        self.payload = payload

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, creds):
        self.creds = creds
        self.urls = []

    def get(self, url, params=None, timeout=None):
        self.urls.append(url)
        if "vision.googleapis.com" in url:
            return FakeResponse(403, {
                "error": {"status": "PERMISSION_DENIED",
                          "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                       "reason": "IAM_PERMISSION_DENIED"}]}
            })
        return FakeResponse(200, {"quotaInfos": [
            {"quotaId": "test_quota", "dimensionsInfos": []}
        ]})

    def close(self):
        pass


class TestGcpKeyResolution(unittest.TestCase):
    def test_config_path_resolves_from_working_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            key = root / "gcp_key.json"
            key.write_text("{}", encoding="utf8")
            (root / "config.ini").write_text(
                "[GCP]\ngcp_key_path = gcp_key.json\n", encoding="utf8"
            )
            result, source = credential_file(
                cwd=root, repo_root=root, environ={}
            )
            self.assertEqual(result, key.resolve())
            self.assertEqual(source, "config.ini [GCP] gcp_key_path")

    def test_config_absolute_key_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            key = root / "gcp_key.json"
            key.write_text("{}", encoding="utf8")
            (root / "config.ini").write_text(
                "[GCP]\ngcp_key_path = " + key.as_posix() + "\n", encoding="utf8"
            )
            result, _ = credential_file(
                cwd=root, repo_root=root, environ={}
            )
            self.assertEqual(result, key.resolve())

    def test_precedence_command_line_env_and_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for file in ("cli.json", "env.json", "ini.json"):
                (root / file).write_text("{}", encoding="utf8")
            (root / "config.ini").write_text(
                "[GCP]\ngcp_key_path=ini.json\n", encoding="utf8"
            )
            path, source = credential_file(
                key_file="cli.json", cwd=root, repo_root=root,
                environ={"GOOGLE_APPLICATION_CREDENTIALS": "env.json"}
            )
            self.assertEqual(path.name, "cli.json")
            self.assertEqual(source, "--key-file")
            path, source = credential_file(
                cwd=root, repo_root=root,
                environ={"GOOGLE_APPLICATION_CREDENTIALS": "env.json"}
            )
            self.assertEqual(path.name, "env.json")
            self.assertEqual(source, "GOOGLE_APPLICATION_CREDENTIALS")
            path, source = credential_file(
                config_file="config.ini", cwd=root, repo_root=root,
                environ={"GOOGLE_APPLICATION_CREDENTIALS": "env.json"}
            )
            self.assertEqual(path.name, "ini.json")
            self.assertEqual(source, "config.ini [GCP] gcp_key_path")

    def test_missing_configured_key_fails_closed_not_adc_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config.ini").write_text(
                "[GCP]\ngcp_key_path=missing.json\n", encoding="utf8"
            )
            with self.assertRaisesRegex(QuotaInspectionError, "cannot be found"):
                credential_file(cwd=root, repo_root=root, environ={})

    def test_fallback_local_key(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "gcp_key.json").write_text("{}",encoding="utf8")
            path, source = credential_file(cwd=root, repo_root=root, environ={})
            self.assertEqual(path.name,"gcp_key.json")
            self.assertEqual(source,"gcp_key.json")

    def test_loads_service_account_file_not_adc(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/"gcp_key.json").write_text("{}",encoding="utf8")
            with patch(
                "google.oauth2.service_account.Credentials.from_service_account_file",
                return_value=FakeCredential(),
            ) as loader:
                _, info = resolve_identity(
                    cwd=root, repo_root=root, environ={},
                )
            self.assertEqual(info["key_project"], "vision-project")
            self.assertEqual(info["quota_project"], "vision-project")
            self.assertIn("vision-svc@", info["service_account"])
            self.assertEqual(loader.call_count, 1)
            self.assertEqual(loader.call_args.kwargs["scopes"],
                             ("https://www.googleapis.com/auth/cloud-platform",))

    def test_403_does_not_hide_other_service_results(self):
        credentials=FakeCredential()
        session=FakeSession(credentials)
        result, errors=inspect_quotas(
            credentials, "vision-project",
            session_factory=lambda creds: session
        )
        self.assertIn("vision.googleapis.com", errors)
        self.assertIn("HTTP 403",errors["vision.googleapis.com"])
        self.assertIn("IAM_PERMISSION_DENIED",errors["vision.googleapis.com"])
        self.assertEqual(len(result["documentai.googleapis.com"]),1)
        self.assertNotIn("vision.googleapis.com",result)

    def test_error_exposes_code_without_response_body(self):
        response=FakeResponse(
            403,{"error":{"status":"PERMISSION_DENIED",
                           "message":"secret-sensitive-message"}}
        )
        text=_api_error(response)
        self.assertIn("HTTP 403",text)
        self.assertNotIn("secret-sensitive-message",text)


if __name__=="__main__":
    unittest.main()
