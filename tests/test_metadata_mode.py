from argparse import Namespace, ArgumentParser
from types import SimpleNamespace

import pytest

from mir_ai.batch import BatchRunner, SourceItem
from mir_ai.cli import _batch_rimdocs_provider
from mir_ai.pipeline import MIRPipeline
from mir_ai.rimdocs import EmptyRimDocsProvider


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


def test_required_mode_reports_missing_authoritative_row_before_ingestion():
    runner = BatchRunner.__new__(BatchRunner)
    runner.settings = SimpleNamespace(metadata_mode="required")
    runner.rimdocs = EmptyRimDocsProvider()
    item = SourceItem(
        canonical_path="s3://bucket/path/a.pdf",
        local_path=None,
        source_url="s3://bucket/path/a.pdf",
        source_version="v1",
        size=1,
        logical_object_id="path/a",
    )
    metadata, version, issue = runner._metadata_for_item(item)
    assert metadata is None
    assert version == "none"
    assert issue is not None
    assert issue.status == "RIMDOCS_NOT_FOUND"


def test_optional_mode_allows_missing_authoritative_row():
    runner = BatchRunner.__new__(BatchRunner)
    runner.settings = SimpleNamespace(metadata_mode="optional")
    runner.rimdocs = EmptyRimDocsProvider()
    item = SourceItem(
        canonical_path="/archive/a.pdf",
        local_path="/archive/a.pdf",
        source_url="/archive/a.pdf",
        source_version="v1",
        size=1,
        logical_object_id="a",
    )
    metadata, version, issue = runner._metadata_for_item(item)
    assert metadata is None
    assert version == "none"
    assert issue is None


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
