from pathlib import Path


def read_secret(value: str | None, file_path: Path | None) -> str | None:
    if file_path is not None:
        if not file_path.is_file():
            raise ValueError(f"secret file does not exist: {file_path}")
        value = file_path.read_text(encoding="utf-8")
    normalized = (value or "").strip()
    return normalized or None
