import hashlib
import json
import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile

from secagent.security.files import extract_zip_safely


class UploadTooLarge(ValueError):
    pass


class StorageService:
    def __init__(
        self,
        data_dir: Path,
        *,
        upload_max_bytes: int,
        archive_max_files: int,
        archive_max_bytes: int,
    ) -> None:
        self.data_dir = data_dir
        self.upload_max_bytes = upload_max_bytes
        self.archive_max_files = archive_max_files
        self.archive_max_bytes = archive_max_bytes

    def workspace(self, task_id: str) -> Path:
        path = self.data_dir / "tasks" / task_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def remove_workspace(self, task_id: str) -> None:
        root = (self.data_dir / "tasks").resolve()
        target = (root / task_id).resolve()
        if root not in target.parents:
            raise ValueError("task workspace escapes storage root")
        if target.exists():
            shutil.rmtree(target)

    async def save_upload(self, task_id: str, upload: UploadFile) -> dict:
        workspace = self.workspace(task_id)
        uploads = workspace / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        original_name = Path(upload.filename or "upload.bin").name
        suffix = Path(original_name).suffix.lower()
        destination = uploads / f"{uuid4().hex}{suffix}"
        digest = hashlib.sha256()
        size = 0
        with destination.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > self.upload_max_bytes:
                    output.close()
                    destination.unlink(missing_ok=True)
                    raise UploadTooLarge("upload exceeds configured size limit")
                digest.update(chunk)
                output.write(chunk)

        extracted = []
        if suffix == ".zip":
            extracted = extract_zip_safely(
                destination,
                workspace / "extracted",
                max_files=self.archive_max_files,
                max_bytes=self.archive_max_bytes,
            )
        metadata = {
            "original_name": original_name,
            "stored_name": destination.name,
            "sha256": digest.hexdigest(),
            "size": size,
            "extracted": [str(path.relative_to(workspace)) for path in extracted],
        }
        (workspace / "upload.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return metadata
