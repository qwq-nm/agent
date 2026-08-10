from secagent.db import make_session_factory


def test_session_factory_creates_sqlite_parent_directory(tmp_path) -> None:
    database_path = tmp_path / "nested" / "secagent.db"

    make_session_factory(f"sqlite:///{database_path.as_posix()}")

    assert database_path.is_file()
