"""zip/폴더 업로드 안전 스테이징 — zip bomb/zip slip 방어, 최초 git push."""
import io
import os
import stat
import subprocess
import zipfile

import pytest
from fastapi import UploadFile

from app.config import get_settings
from app.services import upload


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_stage_zip_extracts_normal_files(tmp_path):
    data = _zip_bytes({"README.md": b"hello", "src/app.py": b"print(1)\n"})
    dest = tmp_path / "out"
    upload.stage_zip(data, dest)
    assert (dest / "README.md").read_bytes() == b"hello"
    assert (dest / "src" / "app.py").read_bytes() == b"print(1)\n"


def test_stage_zip_rejects_parent_traversal(tmp_path):
    data = _zip_bytes({"../evil.txt": b"x"})
    with pytest.raises(upload.UploadRejected):
        upload.stage_zip(data, tmp_path / "out")


def test_stage_zip_rejects_absolute_path(tmp_path):
    data = _zip_bytes({"/etc/passwd": b"x"})
    with pytest.raises(upload.UploadRejected):
        upload.stage_zip(data, tmp_path / "out")


def test_stage_zip_rejects_symlink_entry(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zi = zipfile.ZipInfo("link")
        zi.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(zi, "/etc/passwd")
    with pytest.raises(upload.UploadRejected):
        upload.stage_zip(buf.getvalue(), tmp_path / "out")


def test_stage_zip_rejects_high_compression_ratio(tmp_path, fresh_settings, monkeypatch):
    monkeypatch.setenv("PAAS_UPLOAD_MAX_COMPRESSION_RATIO", "10")
    get_settings.cache_clear()
    data = _zip_bytes({"zeros.bin": b"\x00" * (2 * 1024 * 1024)})
    with pytest.raises(upload.UploadRejected):
        upload.stage_zip(data, tmp_path / "out")


def test_stage_zip_rejects_total_size_over_cap(tmp_path, fresh_settings, monkeypatch):
    monkeypatch.setenv("PAAS_UPLOAD_MAX_UNCOMPRESSED_MB", "1")
    get_settings.cache_clear()
    data = _zip_bytes({"random.bin": os.urandom(2 * 1024 * 1024)})
    with pytest.raises(upload.UploadRejected):
        upload.stage_zip(data, tmp_path / "out")


def test_stage_zip_rejects_too_many_entries(tmp_path, fresh_settings, monkeypatch):
    monkeypatch.setenv("PAAS_UPLOAD_MAX_FILES", "2")
    get_settings.cache_clear()
    data = _zip_bytes({f"f{i}.txt": b"x" for i in range(3)})
    with pytest.raises(upload.UploadRejected):
        upload.stage_zip(data, tmp_path / "out")


@pytest.mark.anyio
async def test_stage_folder_writes_relative_paths(tmp_path):
    files = [
        UploadFile(filename="app/main.py", file=io.BytesIO(b"print(1)\n")),
        UploadFile(filename="README.md", file=io.BytesIO(b"hi")),
    ]
    dest = tmp_path / "out"
    await upload.stage_folder(files, dest)
    assert (dest / "app" / "main.py").read_bytes() == b"print(1)\n"
    assert (dest / "README.md").read_bytes() == b"hi"


@pytest.mark.anyio
async def test_stage_folder_rejects_traversal(tmp_path):
    files = [UploadFile(filename="../evil.txt", file=io.BytesIO(b"x"))]
    with pytest.raises(upload.UploadRejected):
        await upload.stage_folder(files, tmp_path / "out")


@pytest.mark.anyio
async def test_read_capped_rejects_oversize():
    big = UploadFile(filename="big.zip", file=io.BytesIO(b"x" * 1000))
    with pytest.raises(upload.UploadRejected):
        await upload.read_capped(big, max_bytes=100)


@pytest.mark.anyio
async def test_read_capped_allows_within_limit():
    small = UploadFile(filename="small.zip", file=io.BytesIO(b"x" * 50))
    data = await upload.read_capped(small, max_bytes=100)
    assert data == b"x" * 50


def test_init_repo_and_push_creates_initial_commit(tmp_path):
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "hello.txt").write_text("hi\n")

    sha = upload.init_repo_and_push(workdir, f"file://{bare}", "main")
    assert len(sha) == 40

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--branch", "main", str(bare), str(clone)], check=True)
    assert (clone / "hello.txt").read_text() == "hi\n"
