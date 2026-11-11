"""Versioned use-case YAML contract and fail-closed execution tests."""
import copy
import tempfile
import unittest
from pathlib import Path

from ingestion_v2.profile import (
    ProfileError, load_profile, profile_summary, require_approved_for_pilot,
    validate_contract,
)


SAMPLE = Path(__file__).resolve().parents[1] / "profiles" / "document_pipeline_use_case.yml"


class TestConsumerYaml(unittest.TestCase):
    def test_distributed_sample_is_valid_but_draft(self):
        profile = load_profile(SAMPLE)
        self.assertEqual(profile["schema_version"], 1)
        self.assertEqual(profile["chunking"]["max_tokens"], 800)
        self.assertEqual(profile["metadata"]["fields"]["protocol_number"]["label"],
                         "Protocol Number")
        self.assertEqual(profile_summary(profile)["approval"], "draft")
        with self.assertRaisesRegex(ProfileError, "not approved"):
            require_approved_for_pilot(profile)

    def test_approved_pilot_produces_valid_runtime_settings(self):
        profile = copy.deepcopy(load_profile(SAMPLE))
        profile["approval"] = {
            "status": "pilot_approved",
            "business_approver": "Business owner",
            "data_owner_approver": "Data steward",
            "approved_date": "2026-11-10",
        }
        profile["source"]["uri"] = "s3://approved-test-bucket/pilot/"
        profile["use_case"]["business_owner"] = "Business owner"
        validate_contract(profile)
        require_approved_for_pilot(profile)

    def test_attempt_to_enable_unimplemented_ocr_is_rejected(self):
        profile = copy.deepcopy(load_profile(SAMPLE))
        profile["execution"]["gcp_ocr"] = True
        with self.assertRaisesRegex(ProfileError, "not yet implemented"):
            validate_contract(profile)

    def test_future_requirements_do_not_execute_ocr(self):
        profile = load_profile(SAMPLE)
        self.assertTrue(profile["requested_future_features"]["gcp_ocr"])
        self.assertFalse(profile["execution"]["gcp_ocr"])

    def test_unrecognized_top_level_or_runtime_key_rejected(self):
        profile = copy.deepcopy(load_profile(SAMPLE))
        profile["hack_pipeline"] = {"enabled": True}
        with self.assertRaisesRegex(ProfileError, "Unknown setting"):
            validate_contract(profile)
        profile = copy.deepcopy(load_profile(SAMPLE))
        profile["chunking"]["made_up_key"] = 999
        with self.assertRaisesRegex(ProfileError, "Invalid executable policy"):
            validate_contract(profile)

    def test_invalid_boolean_value_rejected(self):
        profile = copy.deepcopy(load_profile(SAMPLE))
        profile["execution"]["embedding"] = "false"
        with self.assertRaisesRegex(ProfileError, "true or false"):
            validate_contract(profile)

    def test_reject_duplicate_yaml_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.yml"
            path.write_text(
                "schema_version: 1\nschema_version: 2\n"
                "profile_id: sample\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ProfileError, "Duplicate YAML key"):
                load_profile(path)

    def test_yaml_typed_empty_null_and_strings(self):
        profile = load_profile(SAMPLE)
        self.assertIsNone(profile["security"]["retention_days"])
        self.assertFalse(profile["security"]["external_ai_processing_approved"])
        self.assertEqual(profile["metadata"]["fields"]["application_number"]["type"],
                         "string")


if __name__ == "__main__":
    unittest.main()
