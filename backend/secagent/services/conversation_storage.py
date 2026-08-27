from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from fastapi import UploadFile
from pydantic import TypeAdapter, ValidationError

from secagent.config import Settings
from secagent.conversation_domain import (
    AttachmentMetadataCreate,
    CanonicalUUID,
    SafeClientRelativePath,
)
from secagent.security.files import UnsafeArchive, extract_zip_safely


_READ_CHUNK_BYTES = 1024 * 1024
_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CLOCK$",
    "CONIN$",
    "CONOUT$",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class AttachmentStorageError(ValueError):
    """An attachment cannot be staged, published, or safely removed."""


@dataclass(frozen=True)
class _StagedAttachment:
    original_name: str
    stored_name: str
    relative_path: str | None
    content_type: str
    size_bytes: int
    sha256: str

    def identity(self) -> tuple[str, str | None, str, int, str]:
        return (
            self.original_name,
            self.relative_path,
            self.content_type,
            self.size_bytes,
            self.sha256,
        )


class StagedAttachmentBatch:
    """Opaque capability representing one service-owned temporary batch."""

    __slots__ = ("_batch_id", "_entries", "_owner")

    def __init__(
        self,
        owner: object,
        batch_id: str | None,
        entries: tuple[_StagedAttachment, ...],
    ) -> None:
        self._owner = owner
        self._batch_id = batch_id
        self._entries = entries


class ConversationStorageService:
    def __init__(self, settings: Settings) -> None:
        self.data_dir = settings.data_dir.resolve()
        self.upload_max_bytes = settings.upload_max_bytes
        self.archive_max_files = settings.archive_max_files
        self.archive_max_bytes = settings.archive_max_bytes
        self.max_attachments_per_message = settings.max_attachments_per_message
        self.max_attachment_total_bytes = settings.max_attachment_total_bytes
        self._owner = object()
        self._issued_batches: dict[StagedAttachmentBatch, str | None] = {}

    @property
    def staging_root(self) -> Path:
        return self.data_dir / ".conversation-staging"

    async def stage_many(
        self,
        uploads: Sequence[UploadFile],
        relative_paths: Sequence[str | None],
    ) -> StagedAttachmentBatch:
        upload_items = list(uploads)
        path_items = list(relative_paths)
        if len(upload_items) > self.max_attachments_per_message:
            raise AttachmentStorageError("attachment count exceeds configured limit")
        if len(path_items) != len(upload_items):
            raise AttachmentStorageError("relative path count must equal upload count")

        validated_paths = [self._validate_relative_path(item) for item in path_items]
        normalized_names = [
            self._normalize_filename(upload.filename) for upload in upload_items
        ]
        if not upload_items:
            return self._issue_batch(None, ())

        self._refuse_links(self.staging_root)
        batch_id = uuid4().hex
        batch_root = self.staging_root / batch_id
        uploads_root = batch_root / "uploads"
        entries: list[_StagedAttachment] = []
        aggregate_size = 0
        try:
            uploads_root.mkdir(parents=True, exist_ok=False)
            for index, (upload, original_name, relative_path) in enumerate(
                zip(upload_items, normalized_names, validated_paths, strict=True)
            ):
                stored_name = uuid4().hex
                destination = uploads_root / stored_name
                digest = hashlib.sha256()
                size = 0
                signature = bytearray()
                with destination.open("xb") as output:
                    while chunk := await upload.read(_READ_CHUNK_BYTES):
                        if not isinstance(chunk, bytes):
                            raise AttachmentStorageError(
                                "upload stream must yield bytes"
                            )
                        if len(signature) < 4:
                            signature.extend(chunk[: 4 - len(signature)])
                        size += len(chunk)
                        aggregate_size += len(chunk)
                        if size > self.upload_max_bytes:
                            raise AttachmentStorageError(
                                "per-file upload limit exceeded"
                            )
                        if aggregate_size > self.max_attachment_total_bytes:
                            raise AttachmentStorageError(
                                "aggregate attachment limit exceeded"
                            )
                        digest.update(chunk)
                        output.write(chunk)

                if original_name.casefold().endswith(".zip") or bytes(
                    signature
                ).startswith(_ZIP_SIGNATURES):
                    archive_root = batch_root / "archives" / f"{index:04d}-{uuid4().hex}"
                    self._inspect_archive(destination)
                    extract_zip_safely(
                        destination,
                        archive_root,
                        max_files=self.archive_max_files,
                        max_bytes=self.archive_max_bytes,
                    )

                content_type = (
                    mimetypes.guess_type(original_name, strict=False)[0]
                    or "application/octet-stream"
                )
                entries.append(
                    _StagedAttachment(
                        original_name=original_name,
                        stored_name=stored_name,
                        relative_path=relative_path,
                        content_type=content_type,
                        size_bytes=size,
                        sha256=digest.hexdigest(),
                    )
                )
            return self._issue_batch(batch_id, tuple(entries))
        except BaseException:
            if batch_root.exists() and not self._is_link_or_junction(batch_root):
                shutil.rmtree(batch_root)
            raise

    def publish(
        self,
        batch: StagedAttachmentBatch,
        conversation_id: str,
        message_id: str,
    ) -> list[AttachmentMetadataCreate]:
        self._require_owned_batch(batch)
        canonical_conversation = self._canonical_uuid(conversation_id)
        canonical_message = self._canonical_uuid(message_id)
        if batch._batch_id is None:
            return []

        source = self.staging_root / batch._batch_id
        target = self._published_target(canonical_conversation, canonical_message)
        moved = False
        try:
            self._require_exact_staging_target(source, batch._batch_id)
            self._refuse_links(source)
            self._refuse_links(target)
            if not source.is_dir():
                raise AttachmentStorageError("staged batch no longer exists")
            if target.exists():
                raise AttachmentStorageError("published message workspace exists")
            target.parent.mkdir(parents=True, exist_ok=True)
            source.replace(target)
            moved = True
            metadata = [
                AttachmentMetadataCreate(
                    original_name=item.original_name,
                    storage_ref=str(
                        PurePosixPath(
                            "conversations",
                            canonical_conversation,
                            "messages",
                            canonical_message,
                            "uploads",
                            item.stored_name,
                        )
                    ),
                    relative_path=item.relative_path,
                    content_type=item.content_type,
                    size_bytes=item.size_bytes,
                    sha256=item.sha256,
                )
                for item in batch._entries
            ]
            batch._batch_id = None
            self._issued_batches[batch] = None
            return metadata
        except Exception:
            if moved and target.exists() and not self._is_link_or_junction(target):
                shutil.rmtree(target)
            elif source.exists() and not self._is_link_or_junction(source):
                shutil.rmtree(source)
            batch._batch_id = None
            self._issued_batches[batch] = None
            raise

    def attachment_identities(
        self, batch: StagedAttachmentBatch
    ) -> list[tuple[str, str | None, str, int, str]]:
        self._require_owned_batch(batch)
        return [item.identity() for item in batch._entries]

    def cleanup_staged(self, batch: StagedAttachmentBatch) -> None:
        self._require_owned_batch(batch)
        if batch._batch_id is None:
            return
        target = self.staging_root / batch._batch_id
        self._require_exact_staging_target(target, batch._batch_id)
        self._refuse_links(target)
        if target.exists():
            shutil.rmtree(target)
        batch._batch_id = None
        self._issued_batches[batch] = None

    def cleanup_published_message(
        self, conversation_id: str, message_id: str
    ) -> None:
        canonical_conversation = self._canonical_uuid(conversation_id)
        canonical_message = self._canonical_uuid(message_id)
        target = self._published_target(canonical_conversation, canonical_message)
        relative = target.relative_to(self.data_dir)
        if relative.parts != (
            "conversations",
            canonical_conversation,
            "messages",
            canonical_message,
        ):
            raise AttachmentStorageError("invalid published cleanup target")
        self._refuse_links(target)
        if target.exists():
            shutil.rmtree(target)

    def _inspect_archive(self, archive_path: Path) -> None:
        try:
            with ZipFile(archive_path) as archive:
                seen: set[str] = set()
                for item in archive.infolist():
                    if stat.S_ISLNK(item.external_attr >> 16):
                        raise AttachmentStorageError(
                            "archive symbolic link detected"
                        )
                    logical_name = item.filename[:-1] if item.is_dir() else item.filename
                    if not logical_name:
                        raise AttachmentStorageError("archive entry path is empty")
                    normalized = unicodedata.normalize("NFC", logical_name)
                    try:
                        safe_path = TypeAdapter(SafeClientRelativePath).validate_python(
                            normalized
                        )
                    except ValidationError as exc:
                        raise AttachmentStorageError(
                            "archive entry path is unsafe"
                        ) from exc
                    if any(len(segment) > 255 for segment in safe_path.split("/")):
                        raise AttachmentStorageError(
                            "archive entry path segment exceeds 255 characters"
                        )
                    collision_key = safe_path.casefold()
                    if collision_key in seen:
                        raise AttachmentStorageError(
                            "archive contains a normalized path collision"
                        )
                    seen.add(collision_key)
        except BadZipFile as exc:
            raise AttachmentStorageError("invalid ZIP archive") from exc

    @staticmethod
    def _normalize_filename(value: str | None) -> str:
        if not isinstance(value, str) or not value:
            raise AttachmentStorageError("filename is required")
        name = unicodedata.normalize("NFC", value)
        if len(name) > 255:
            raise AttachmentStorageError("filename exceeds 255 characters")
        if name in {".", ".."} or "/" in name or "\\" in name:
            raise AttachmentStorageError("filename must be one safe path segment")
        if name.endswith((".", " ")):
            raise AttachmentStorageError("filename has a trailing dot or space")
        if ":" in name or any(unicodedata.category(char).startswith("C") for char in name):
            raise AttachmentStorageError("filename contains an unsafe character")
        if name.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            raise AttachmentStorageError("filename uses a Windows device alias")
        if Path(name).is_absolute() or (len(name) >= 2 and name[1] == ":"):
            raise AttachmentStorageError("filename must not be absolute")
        return name

    @staticmethod
    def _validate_relative_path(value: str | None) -> str | None:
        try:
            return TypeAdapter(SafeClientRelativePath | None).validate_python(value)
        except ValidationError as exc:
            raise AttachmentStorageError("relative path is unsafe") from exc

    @staticmethod
    def _canonical_uuid(value: str) -> str:
        try:
            return TypeAdapter(CanonicalUUID).validate_python(value)
        except ValidationError as exc:
            raise AttachmentStorageError("cleanup identifiers must be canonical UUIDs") from exc

    def _published_target(self, conversation_id: str, message_id: str) -> Path:
        return (
            self.data_dir
            / "conversations"
            / conversation_id
            / "messages"
            / message_id
        )

    def _require_owned_batch(self, batch: object) -> None:
        if (
            not isinstance(batch, StagedAttachmentBatch)
            or batch._owner is not self._owner
            or batch not in self._issued_batches
            or batch._batch_id != self._issued_batches[batch]
        ):
            raise AttachmentStorageError("staged batch was not issued by this service")

    def _issue_batch(
        self,
        batch_id: str | None,
        entries: tuple[_StagedAttachment, ...],
    ) -> StagedAttachmentBatch:
        batch = StagedAttachmentBatch(self._owner, batch_id, entries)
        self._issued_batches[batch] = batch_id
        return batch

    def _require_exact_staging_target(self, target: Path, batch_id: str) -> None:
        try:
            relative = target.relative_to(self.data_dir)
        except ValueError as exc:  # pragma: no cover - construction is internal
            raise AttachmentStorageError("staged cleanup target escapes data root") from exc
        if relative.parts != (".conversation-staging", batch_id):
            raise AttachmentStorageError("invalid staged cleanup target")

    def _refuse_links(self, target: Path) -> None:
        try:
            relative = target.relative_to(self.data_dir)
        except ValueError as exc:
            raise AttachmentStorageError("filesystem target escapes data root") from exc
        current = self.data_dir
        for part in relative.parts:
            current = current / part
            if self._is_link_or_junction(current):
                raise AttachmentStorageError(
                    "filesystem target contains a symlink or junction"
                )

    @staticmethod
    def _is_link_or_junction(path: Path) -> bool:
        is_junction = getattr(os.path, "isjunction", lambda _path: False)
        return path.is_symlink() or bool(is_junction(path))
