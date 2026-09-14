from pathlib import Path

import pytest

from mir_ai.settings import Settings


BASE = """
[POSTGRES]
host = localhost
port = 5432
database = test
user = test
password = test
schema = mirai_uat

[PATHS]
source_type = s3
docs_root = .

[MIR_AI]
embedding_model = text-embedding-3-large
embedding_dim = 3072
embedding_api_version = 2025-04-01-preview
"""


def test_object_list_requires_id_column_when_enabled(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text(
        BASE
        + """
object_list_enabled = true
object_list_schema = regulatory
object_list_table = mirai_obj_list
""",
        encoding="utf-8",
    )
    settings = Settings.load(path)
    with pytest.raises(ValueError, match="object_list_id_column"):
        settings.validate()


def test_pdf_and_docx_switches_are_configurable(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text(
        BASE
        + """
enable_pdf = false
enable_docx = true
preferred_format = pdf
""",
        encoding="utf-8",
    )
    settings = Settings.load(path)
    settings.validate()
    assert settings.enable_pdf is False
    assert settings.enable_docx is True
    assert settings.preferred_format == "pdf"


def test_object_list_disallows_non_strict_duplicate_guessing(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text(
        BASE
        + """
object_list_enabled = true
object_list_schema = regulatory
object_list_table = mirai_obj_list
object_list_id_column = file_name
strict_source_selection = false
""",
        encoding="utf-8",
    )
    settings = Settings.load(path)
    with pytest.raises(ValueError, match="strict_source_selection"):
        settings.validate()
