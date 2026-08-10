import stat
from pathlib import Path, PurePosixPath
from zipfile import ZipFile


class UnsafeArchive(ValueError):
    pass


def extract_zip_safely(
    archive: Path,
    destination: Path,
    *,
    max_files: int,
    max_bytes: int,
) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    with ZipFile(archive) as source:
        members = [item for item in source.infolist() if not item.is_dir()]
        if len(members) > max_files:
            raise UnsafeArchive("archive exceeds file-count limit")
        if sum(item.file_size for item in members) > max_bytes:
            raise UnsafeArchive("archive exceeds expanded-size limit")

        outputs: list[Path] = []
        root = destination.resolve()
        for item in members:
            if stat.S_ISLNK(item.external_attr >> 16):
                raise UnsafeArchive("archive symbolic link detected")
            logical = PurePosixPath(item.filename)
            if (
                logical.is_absolute()
                or ".." in logical.parts
                or (logical.parts and logical.parts[0].endswith(":"))
            ):
                raise UnsafeArchive("archive path traversal detected")
            target = (destination / Path(*logical.parts)).resolve()
            if root not in target.parents:
                raise UnsafeArchive("archive target escapes workspace")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(item) as reader, target.open("wb") as writer:
                remaining = max_bytes + 1
                while chunk := reader.read(min(1024 * 1024, remaining)):
                    writer.write(chunk)
                    remaining -= len(chunk)
                    if remaining <= 0:
                        raise UnsafeArchive("archive member exceeds extraction limit")
            outputs.append(target)
        return outputs
