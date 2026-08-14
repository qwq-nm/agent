from pathlib import Path

import pytest

from secagent.config_secrets import read_secret


def test_secret_file_takes_precedence(tmp_path: Path):
    secret_file = tmp_path / "key"
    secret_file.write_text("file-secret\n", encoding="utf-8")
    assert read_secret("env-secret", secret_file) == "file-secret"


def test_missing_secret_file_is_an_error(tmp_path: Path):
    with pytest.raises(ValueError, match="secret file does not exist"):
        read_secret(None, tmp_path / "missing")


def test_blank_secret_is_treated_as_unconfigured():
    assert read_secret("   ", None) is None
