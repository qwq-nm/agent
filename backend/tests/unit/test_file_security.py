import zipfile

import pytest

from secagent.security.files import UnsafeArchive, extract_zip_safely


def test_zip_path_traversal_is_rejected(tmp_path) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../escape.py", "print('bad')")
    with pytest.raises(UnsafeArchive):
        extract_zip_safely(
            archive,
            tmp_path / "out",
            max_files=20,
            max_bytes=1024,
        )


def test_safe_zip_is_extracted_under_destination(tmp_path) -> None:
    archive = tmp_path / "safe.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("src/app.py", "print('not executed')")
    files = extract_zip_safely(
        archive,
        tmp_path / "out",
        max_files=20,
        max_bytes=4096,
    )
    assert files == [tmp_path / "out" / "src" / "app.py"]
