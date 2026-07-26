"""zip/폴더 업로드로 프로젝트를 등록할 때 쓰는 안전한 스테이징 + 최초 push.

방어 대상:
  - zip bomb: 스트리밍 중 실제 압축 해제 바이트 수로 총량을 강제한다(zip 헤더의
    선언값은 위조 가능하므로 신뢰하지 않는다) + 파일별 압축비 사전 점검 +
    엔트리 수 상한.
  - zip slip: 절대경로·상위 디렉토리(..) 탈출 경로, 심볼릭 링크 엔트리 거부.
  - 대용량 업로드: 원본 업로드 바이트 자체도 스트리밍 중 상한을 넘으면 즉시 중단
    (전체를 다 받은 뒤 검사하지 않는다).

git init·최초 커밋·push는 이 모듈에서 1회만 수행한다. 이후 코드 수정은 전부
기존 LLM 채팅/diff 승인 플로우(workspace.py apply_diff)로만 이뤄지며, 이 모듈은
그 경로를 우회하지 않는다.
"""
import io
import stat
import subprocess
import zipfile
from pathlib import Path, PurePosixPath

from fastapi import UploadFile

from ..config import get_settings
from .git_auth import auth_args

CHUNK = 1024 * 1024


class UploadRejected(ValueError):
    """업로드 내용이 안전 기준을 위반함 — 422로 매핑."""


class UploadError(RuntimeError):
    """스테이징 이후 git 초기화/push 실패 — 502로 매핑."""


def _safe_relpath(name: str) -> PurePosixPath:
    posix = PurePosixPath(name.replace("\\", "/"))
    if posix.is_absolute() or not posix.parts or ".." in posix.parts:
        raise UploadRejected(f"허용되지 않는 경로입니다: {name}")
    return posix


async def read_capped(file: UploadFile, max_bytes: int) -> bytes:
    """업로드 바이트를 스트리밍으로 읽으며 상한 초과 시 즉시 중단한다."""
    total = 0
    chunks: list[bytes] = []
    while True:
        chunk = await file.read(CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise UploadRejected(f"업로드 용량이 최대치({max_bytes // (1024 * 1024)}MB)를 초과했습니다")
        chunks.append(chunk)
    return b"".join(chunks)


def stage_zip(data: bytes, dest: Path) -> None:
    """검증된 zip 바이트를 dest(빈 디렉토리)에 안전하게 풀어놓는다."""
    settings = get_settings()
    max_uncompressed = settings.upload_max_uncompressed_mb * 1024 * 1024

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise UploadRejected(f"올바른 zip 파일이 아닙니다: {e}") from e

    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > settings.upload_max_files:
        raise UploadRejected(f"파일 수가 너무 많습니다 (최대 {settings.upload_max_files}개)")

    plans: list[tuple[zipfile.ZipInfo, Path]] = []
    declared_total = 0
    for info in infos:
        mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode):
            raise UploadRejected(f"심볼릭 링크 엔트리는 허용되지 않습니다: {info.filename}")
        rel = _safe_relpath(info.filename)
        declared_total += info.file_size
        if declared_total > max_uncompressed:
            raise UploadRejected("압축 해제 시 총 용량 상한을 초과합니다")
        if info.compress_size > 0:
            ratio = info.file_size / info.compress_size
            if ratio > settings.upload_max_compression_ratio:
                raise UploadRejected(f"의심스러운 압축비가 감지되었습니다: {info.filename}")
        plans.append((info, dest / rel))

    dest.mkdir(parents=True, exist_ok=True)
    budget = [max_uncompressed]  # 헤더 선언값이 아닌 실제 압축 해제 바이트로 강제
    for info, target in plans:
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                budget[0] -= len(chunk)
                if budget[0] < 0:
                    raise UploadRejected("압축 해제 시 총 용량 상한을 초과합니다")
                out.write(chunk)


async def stage_folder(files: list[UploadFile], dest: Path) -> None:
    """webkitdirectory로 올라온 다중 파일을 dest에 안전하게 저장한다."""
    settings = get_settings()
    if len(files) > settings.upload_max_files:
        raise UploadRejected(f"파일 수가 너무 많습니다 (최대 {settings.upload_max_files}개)")

    max_total = settings.upload_max_uncompressed_mb * 1024 * 1024
    dest.mkdir(parents=True, exist_ok=True)
    budget = [max_total]
    for file in files:
        rel = _safe_relpath(file.filename or "")
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as out:
            while True:
                chunk = await file.read(CHUNK)
                if not chunk:
                    break
                budget[0] -= len(chunk)
                if budget[0] < 0:
                    raise UploadRejected("업로드 총 용량이 상한을 초과했습니다")
                out.write(chunk)


def init_repo_and_push(workdir: Path, git_url: str, branch: str) -> str:
    """스테이징된 디렉토리를 새 git 리포로 초기화하고 최초 커밋을 원격에 push한다."""
    _git(workdir, "init", "-q", "-b", branch)
    _git(workdir, "-c", "user.name=paas-bot", "-c", "user.email=paas-bot@localhost",
         "add", "-A")
    _git(workdir, "-c", "user.name=paas-bot", "-c", "user.email=paas-bot@localhost",
         "commit", "-q", "-m", "initial upload")
    _git(workdir, "remote", "add", "origin", git_url)
    _git(workdir, "push", "-q", "-u", "origin", branch, git_url=git_url)
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workdir, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _git(workdir: Path, *args: str, git_url: str | None = None) -> None:
    auth = auth_args(git_url) if git_url else []
    proc = subprocess.run(["git", *auth, *args], cwd=workdir, capture_output=True, text=True)
    if proc.returncode != 0:
        raise UploadError(f"git {args[0]} failed: {(proc.stderr or proc.stdout).strip()[:500]}")
