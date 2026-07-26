"""워크스페이스 파일 컨텍스트 + LLM diff의 적용(승인 커밋).

LLM은 리포에 직접 쓰지 않는다: diff는 ProposedChange로 저장되고,
apply 승인 시에만 여기서 작업 브랜치에 git apply + commit 된다.
"""
import subprocess
from pathlib import Path

from ..config import get_settings
from ..models import Project
from .build import BuildError, checkout

MAX_CONTEXT_FILE_BYTES = 40_000
CONTEXT_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".md", ".html", ".css",
    ".yml", ".yaml", ".toml", ".txt", ".sql", ".sh",
}
MAX_VIEW_FILE_BYTES = 300_000


def workdir_for(project: Project) -> Path:
    return get_settings().work_dir / project.name


def ensure_branch(project: Project, branch: str) -> Path:
    """기준 브랜치를 최신화한 뒤 작업 브랜치로 전환(없으면 생성)한다."""
    workdir, _ = checkout(project)
    if branch != project.branch:
        _git(workdir, "checkout", "-B", branch)
    return workdir


def file_tree(workdir: Path, limit: int = 200) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=workdir, capture_output=True, text=True
    )
    return out.stdout.splitlines()[:limit]


def read_context_files(workdir: Path, paths: list[str]) -> dict[str, str]:
    """채팅 컨텍스트로 주입할 파일 내용. 경로 탈출·바이너리·과대 파일 차단."""
    result: dict[str, str] = {}
    root = workdir.resolve()
    for rel in paths:
        p = (root / rel).resolve()
        if not p.is_relative_to(root) or not p.is_file():
            continue
        if p.suffix.lower() not in CONTEXT_EXTENSIONS:
            continue
        if p.stat().st_size > MAX_CONTEXT_FILE_BYTES:
            continue
        result[rel] = p.read_text(encoding="utf-8", errors="replace")
    return result


def read_file(workdir: Path, rel: str) -> str:
    """코드 확인 화면용 단일 파일 조회(읽기 전용). 경로 탈출·과대 파일을 차단한다."""
    root = workdir.resolve()
    p = (root / rel).resolve()
    if not p.is_relative_to(root) or not p.is_file():
        raise FileNotFoundError(rel)
    size = p.stat().st_size
    if size > MAX_VIEW_FILE_BYTES:
        raise ValueError(f"파일이 너무 큽니다 ({size} bytes, 최대 {MAX_VIEW_FILE_BYTES})")
    return p.read_text(encoding="utf-8", errors="replace")


def apply_diff(workdir: Path, diff: str, message: str) -> str:
    """diff를 적용하고 커밋한 뒤 커밋 SHA를 반환한다."""
    patch = workdir / ".paas-proposed.patch"
    patch.write_text(diff if diff.endswith("\n") else diff + "\n", encoding="utf-8")
    try:
        _git(workdir, "apply", "--whitespace=nowarn", str(patch))
    finally:
        patch.unlink(missing_ok=True)
    _git(workdir, "add", "-A")
    _git(
        workdir,
        "-c", "user.name=paas-bot",
        "-c", "user.email=paas-bot@localhost",
        "commit", "-m", message,
    )
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workdir, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def diff_between(workdir: Path, base_ref: str, head_ref: str = "HEAD") -> str:
    out = subprocess.run(
        ["git", "diff", f"{base_ref}..{head_ref}"], cwd=workdir, capture_output=True, text=True
    )
    if out.returncode != 0:
        raise BuildError(f"git diff failed: {out.stderr.strip()[:300]}")
    return out.stdout


def _git(workdir: Path, *args: str) -> None:
    proc = subprocess.run(["git", *args], cwd=workdir, capture_output=True, text=True)
    if proc.returncode != 0:
        raise BuildError(f"git {args[0]} failed: {(proc.stderr or proc.stdout).strip()[:500]}")
