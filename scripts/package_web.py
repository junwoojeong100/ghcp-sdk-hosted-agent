from __future__ import annotations

import argparse
import os
from collections.abc import Iterator
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

EXCLUDED_DIRECTORIES = {
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "data",
    "uploads",
}
EXCLUDED_FILES = {
    ".DS_Store",
    ".dockerignore",
    "Dockerfile",
    "requirements-dev.txt",
}
EXCLUDED_ENDINGS = (
    ".db",
    ".db-shm",
    ".db-wal",
    ".log",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-shm",
    ".sqlite3-wal",
)


def iter_deployment_files(source: Path, output: Path) -> Iterator[Path]:
    output = output.resolve()
    for root, directory_names, file_names in os.walk(source, followlinks=False):
        root_path = Path(root)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name not in EXCLUDED_DIRECTORIES
            and not (root_path / name).is_symlink()
        )
        for file_name in sorted(file_names):
            path = root_path / file_name
            if path.is_symlink() or path.resolve() == output:
                continue
            if file_name in EXCLUDED_FILES:
                continue
            if file_name == ".env" or file_name.startswith(".env."):
                continue
            if file_name.lower().endswith(EXCLUDED_ENDINGS):
                continue
            yield path


def build_archive(source: Path, output: Path) -> None:
    source = source.resolve()
    output = output.resolve()
    if not source.is_dir():
        raise ValueError(f"Web source directory does not exist: {source}")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in iter_deployment_files(source, output):
            archive.write(path, path.relative_to(source).as_posix())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a web deployment ZIP without local runtime data."
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build_archive(args.source, args.output)


if __name__ == "__main__":
    main()
