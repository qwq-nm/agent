import hashlib
import io
import os
import asyncio
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from secagent.config import Settings
from secagent.services.conversation_storage import (
    AttachmentStorageError,
    ConversationStorageService,
)


def _uuid() -> str:
    return str(uuid4())


def _upload(name: str | None, data: bytes, claimed_type: str = "evil/type") -> UploadFile:
    return UploadFile(
        file=io.BytesIO(data),
        filename=name,
        headers=Headers({"content-type": claimed_type}),
    )


def _zip(entries: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return output.getvalue()


def _service(tmp_path: Path, **overrides) -> ConversationStorageService:
    return ConversationStorageService(
        Settings(
            data_dir=tmp_path / "data",
            upload_max_bytes=overrides.pop("upload_max_bytes", 1024),
            archive_max_files=overrides.pop("archive_max_files", 20),
            archive_max_bytes=overrides.pop("archive_max_bytes", 4096),
            max_attachments_per_message=overrides.pop(
                "max_attachments_per_message", 20
            ),
            max_attachment_total_bytes=overrides.pop(
                "max_attachment_total_bytes", 4096
            ),
            **overrides,
        )
    )


@pytest.mark.asyncio
async def test_stage_publish_multiple_files_hashes_and_uses_server_metadata(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    first = b"hello"
    second = b"{\"ok\":true}"
    batch = await service.stage_many(
        [
            _upload("report.txt", first, "application/x-danger"),
            _upload("evidence.json", second, "text/plain"),
        ],
        ["folder/report.txt", None],
    )
    conversation_id = _uuid()
    message_id = _uuid()
    metadata = service.publish(batch, conversation_id, message_id)

    assert [item.original_name for item in metadata] == [
        "report.txt",
        "evidence.json",
    ]
    assert [item.relative_path for item in metadata] == [
        "folder/report.txt",
        None,
    ]
    assert [item.content_type for item in metadata] == [
        "text/plain",
        "application/json",
    ]
    assert [item.size_bytes for item in metadata] == [len(first), len(second)]
    assert [item.sha256 for item in metadata] == [
        hashlib.sha256(first).hexdigest(),
        hashlib.sha256(second).hexdigest(),
    ]
    for item, expected in zip(metadata, (first, second), strict=True):
        stored = service.data_dir / Path(*item.storage_ref.split("/"))
        assert stored.read_bytes() == expected
        assert stored.suffix == ""
        assert item.original_name not in item.storage_ref
    assert not service.staging_root.exists() or not any(service.staging_root.iterdir())


@pytest.mark.asyncio
async def test_empty_stage_and_publish_create_no_directory(tmp_path: Path) -> None:
    service = _service(tmp_path)
    batch = await service.stage_many([], [])
    assert not service.staging_root.exists()
    assert service.publish(batch, _uuid(), _uuid()) == []
    assert not (service.data_dir / "conversations").exists()
    service.cleanup_staged(batch)


@pytest.mark.asyncio
async def test_count_relative_path_and_size_limits_fail_without_residue(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path,
        upload_max_bytes=4,
        max_attachments_per_message=2,
        max_attachment_total_bytes=6,
    )
    cases = [
        ([_upload("a.txt", b"a")], [], "relative path count"),
        (
            [_upload("a.txt", b"a"), _upload("b.txt", b"b"), _upload("c.txt", b"c")],
            [None, None, None],
            "attachment count",
        ),
        ([_upload("a.txt", b"12345")], [None], "per-file"),
        (
            [_upload("a.txt", b"1234"), _upload("b.txt", b"1234")],
            [None, None],
            "aggregate",
        ),
        ([_upload("a.txt", b"a")], ["../escape"], "relative path"),
    ]
    for uploads, paths, message in cases:
        with pytest.raises((AttachmentStorageError, ValueError), match=message):
            await service.stage_many(uploads, paths)
        assert not service.staging_root.exists() or not any(
            service.staging_root.iterdir()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        None,
        "",
        "../escape.txt",
        "/absolute.txt",
        "C:/drive.txt",
        "folder/file.txt",
        "folder\\file.txt",
        "CON",
        "nul.txt",
        "file.txt:stream",
        "control\x01.txt",
        "trailing.",
        "trailing ",
        "x" * 256,
    ],
)
async def test_unsafe_client_filenames_fail_closed(
    tmp_path: Path, name: str | None
) -> None:
    service = _service(tmp_path)
    with pytest.raises(AttachmentStorageError, match="filename"):
        await service.stage_many([_upload(name, b"data")], [None])
    assert not service.staging_root.exists() or not any(service.staging_root.iterdir())


@pytest.mark.asyncio
async def test_filename_is_normalized_to_nfc_before_metadata(tmp_path: Path) -> None:
    service = _service(tmp_path)
    batch = await service.stage_many([_upload("e\u0301.txt", b"data")], [None])
    metadata = service.publish(batch, _uuid(), _uuid())
    assert metadata[0].original_name == "é.txt"


@pytest.mark.asyncio
async def test_zip_is_preserved_detected_by_name_or_signature_and_isolated(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    first_zip = _zip([("one/a.txt", b"a")])
    second_zip = _zip([("two/b.txt", b"b")])
    batch = await service.stage_many(
        [_upload("named.zip", first_zip), _upload("signature.bin", second_zip)],
        [None, None],
    )
    conversation_id = _uuid()
    message_id = _uuid()
    metadata = service.publish(batch, conversation_id, message_id)
    workspace = service.data_dir / "conversations" / conversation_id / "messages" / message_id
    archive_roots = sorted((workspace / "archives").iterdir())
    assert len(archive_roots) == 2
    assert (archive_roots[0] / "one" / "a.txt").exists() or (
        archive_roots[1] / "one" / "a.txt"
    ).exists()
    assert (archive_roots[0] / "two" / "b.txt").exists() or (
        archive_roots[1] / "two" / "b.txt"
    ).exists()
    assert all((service.data_dir / Path(*item.storage_ref.split("/"))).exists() for item in metadata)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entries",
    [
        [("../escape.txt", b"x")],
        [("CON.txt", b"x")],
        [("file.txt:stream", b"x")],
        [("trailing./file.txt", b"x")],
        [("trailing /file.txt", b"x")],
        [("control\x01.txt", b"x")],
        [("x" * 256 + ".txt", b"x")],
        [("A.txt", b"a"), ("a.txt", b"b")],
        [("é.txt", b"a"), ("e\u0301.txt", b"b")],
    ],
)
async def test_zip_unsafe_aliases_and_normalized_collisions_reject_batch(
    tmp_path: Path, entries: list[tuple[str, bytes]]
) -> None:
    service = _service(tmp_path)
    with pytest.raises((AttachmentStorageError, ValueError)):
        await service.stage_many([_upload("archive.zip", _zip(entries))], [None])
    assert not service.staging_root.exists() or not any(service.staging_root.iterdir())


@pytest.mark.asyncio
async def test_zip_symlink_and_limits_reject_entire_batch(tmp_path: Path) -> None:
    service = _service(tmp_path, archive_max_files=1, archive_max_bytes=3)
    with pytest.raises((AttachmentStorageError, ValueError)):
        await service.stage_many(
            [_upload("archive.zip", _zip([("a", b"a"), ("b", b"b")]))],
            [None],
        )
    with pytest.raises((AttachmentStorageError, ValueError)):
        await service.stage_many(
            [_upload("archive.zip", _zip([("a", b"1234")]))], [None]
        )

    raw = io.BytesIO()
    with ZipFile(raw, "w") as archive:
        info = __import__("zipfile").ZipInfo("link")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        archive.writestr(info, "target")
    with pytest.raises((AttachmentStorageError, ValueError)):
        await service.stage_many([_upload("archive.zip", raw.getvalue())], [None])
    assert not service.staging_root.exists() or not any(service.staging_root.iterdir())


@pytest.mark.asyncio
async def test_cleanup_is_idempotent_confined_and_rejects_untrusted_targets(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    batch = await service.stage_many([_upload("a.txt", b"a")], [None])
    service.cleanup_staged(batch)
    service.cleanup_staged(batch)
    with pytest.raises(AttachmentStorageError):
        service.cleanup_staged(service.data_dir)

    batch = await service.stage_many([_upload("a.txt", b"a")], [None])
    conversation_id = _uuid()
    message_id = _uuid()
    service.publish(batch, conversation_id, message_id)
    service.cleanup_published_message(conversation_id, message_id)
    service.cleanup_published_message(conversation_id, message_id)
    for bad_conversation, bad_message in (
        ("not-a-uuid", message_id),
        (conversation_id, ".."),
        ("", ""),
    ):
        with pytest.raises(AttachmentStorageError):
            service.cleanup_published_message(bad_conversation, bad_message)


@pytest.mark.asyncio
async def test_cleanup_refuses_symlink_target(tmp_path: Path) -> None:
    service = _service(tmp_path)
    conversation_id = _uuid()
    message_id = _uuid()
    target = (
        service.data_dir
        / "conversations"
        / conversation_id
        / "messages"
        / message_id
    )
    target.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        target.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")
    with pytest.raises(AttachmentStorageError, match="symlink|junction"):
        service.cleanup_published_message(conversation_id, message_id)
    assert outside.exists()


@pytest.mark.asyncio
async def test_cleanup_refuses_broken_symlink_target(tmp_path: Path) -> None:
    service = _service(tmp_path)
    conversation_id = _uuid()
    message_id = _uuid()
    target = (
        service.data_dir
        / "conversations"
        / conversation_id
        / "messages"
        / message_id
    )
    target.parent.mkdir(parents=True)
    try:
        target.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")
    with pytest.raises(AttachmentStorageError, match="symlink|junction"):
        service.cleanup_published_message(conversation_id, message_id)


@pytest.mark.asyncio
async def test_cleanup_does_not_trust_mutated_batch_identifier(tmp_path: Path) -> None:
    service = _service(tmp_path)
    batch = await service.stage_many([_upload("a.txt", b"a")], [None])
    sentinel = service.data_dir / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    batch._batch_id = ".."
    with pytest.raises(AttachmentStorageError):
        service.cleanup_staged(batch)
    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_cancelled_partial_staging_cleans_its_workspace(tmp_path: Path) -> None:
    class CancelledUpload(UploadFile):
        async def read(self, size: int = -1) -> bytes:
            raise asyncio.CancelledError

    service = _service(tmp_path)
    upload = CancelledUpload(file=io.BytesIO(b"unused"), filename="cancel.txt")
    with pytest.raises(asyncio.CancelledError):
        await service.stage_many([upload], [None])
    assert not service.staging_root.exists() or not any(service.staging_root.iterdir())
