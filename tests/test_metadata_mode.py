from argparse import Namespace, ArgumentParser
from types import SimpleNamespace

import pytest

from mir_ai.cli import _batch_rimdocs_provider
from mir_ai.pipeline import MIRPipeline


def test_pipeline_metadata_mode_disabled_skips_even_with_metadata():
    pipe = MIRPipeline.__new__(MIRPipeline)
    pipe.settings = SimpleNamespace(metadata_mode="disabled")
    assert pipe._metadata_should_run({"Study ID": "S1"}) is False


def test_pipeline_metadata_mode_optional_runs_only_when_authoritative_input_is_supplied():
    pipe = MIRPipeline.__new__(MIRPipeline)
    pipe.settings = SimpleNamespace(metadata_mode="optional")
    assert pipe._metadata_should_run(None) is False
    # An explicit empty authoritative row still means the source was supplied;
    # all requested fields are legitimately missing and may be extracted.
    assert pipe._metadata_should_run({}) is True
    assert pipe._metadata_should_run({"Study ID": "S1"}) is True


def test_pipeline_metadata_mode_required_runs_metadata_stage():
    pipe = MIRPipeline.__new__(MIRPipeline)
    pipe.settings = SimpleNamespace(metadata_mode="required")
    assert pipe._metadata_should_run({}) is True


def test_cli_optional_metadata_mode_does_not_require_jsonl(tmp_path):
    settings = SimpleNamespace(
        metadata_mode="optional",
        rimdocs_jsonl_path="",
        scratch_dir=tmp_path,
    )
    args = Namespace(rimdocs_jsonl=None)
    parser = ArgumentParser()
    assert _batch_rimdocs_provider(settings, args, parser) is None


def test_cli_disabled_metadata_mode_ignores_missing_jsonl(tmp_path):
    settings = SimpleNamespace(
        metadata_mode="disabled",
        rimdocs_jsonl_path="",
        scratch_dir=tmp_path,
    )
    args = Namespace(rimdocs_jsonl=None)
    parser = ArgumentParser()
    assert _batch_rimdocs_provider(settings, args, parser) is None


def test_cli_required_metadata_mode_fails_closed_without_jsonl(tmp_path):
    settings = SimpleNamespace(
        metadata_mode="required",
        rimdocs_jsonl_path="",
        scratch_dir=tmp_path,
    )
    args = Namespace(rimdocs_jsonl=None)
    parser = ArgumentParser()
    with pytest.raises(SystemExit):
        _batch_rimdocs_provider(settings, args, parser)
