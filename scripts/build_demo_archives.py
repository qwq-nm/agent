from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


def build_source_demo() -> Path:
    root = Path(__file__).resolve().parents[1]
    source = root / "demo_cases" / "source_audit" / "vulnerable_app"
    target = root / "demo_cases" / "source_audit" / "vulnerable_app.zip"
    with ZipFile(target, "w") as archive:
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            info = ZipInfo(
                path.relative_to(source.parent).as_posix(),
                date_time=(2026, 1, 1, 0, 0, 0),
            )
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    return target


if __name__ == "__main__":
    print(build_source_demo())
