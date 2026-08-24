from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile


def test_web_package_excludes_runtime_and_secret_files(tmp_path: Path) -> None:
    source = tmp_path / "web"
    source.mkdir()
    (source / "main.py").write_text("app = object()\n", encoding="utf-8")
    (source / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (source / ".env").write_text("SECRET=value\n", encoding="utf-8")
    (source / ".env.local").write_text("SECRET=value\n", encoding="utf-8")
    (source / "local.db").write_bytes(b"database")
    (source / "server.log").write_text("runtime log\n", encoding="utf-8")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "main.pyc").write_bytes(b"cache")
    (source / "data").mkdir()
    (source / "data" / "my-chat.db").write_bytes(b"stored conversations")
    (source / "uploads").mkdir()
    (source / "uploads" / "sample.pdf").write_bytes(b"stored upload")
    archive_path = tmp_path / "web.zip"

    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "scripts" / "package_web.py"),
            str(source),
            str(archive_path),
        ],
        check=True,
    )

    with ZipFile(archive_path) as archive:
        names = set(archive.namelist())

    assert names == {"main.py", "requirements.txt"}
