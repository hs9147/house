"""워크스페이스 파일 컨텍스트 + 기획 산출물 커밋.

플랫폼이 리포에 쓰는 경로는 하나다: 확정된 기획 산출물을 작업 브랜치에 커밋하는
write_and_commit. 구현 코드는 외부 빌더가 직접 커밋한다(플랫폼에 diff 적용 경로 없음).
"""
import json
import subprocess
from pathlib import Path

from ..config import get_settings
from ..models import Project
from .build import BuildError, checkout
from .git_auth import auth_args

MAX_CONTEXT_FILE_BYTES = 40_000
CONTEXT_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".md", ".html", ".css",
    ".yml", ".yaml", ".toml", ".txt", ".sql", ".sh",
}
MAX_VIEW_FILE_BYTES = 300_000


def workdir_for(project: Project) -> Path:
    return get_settings().work_dir / project.name


def ensure_branch(project: Project, branch: str) -> Path:
    """기준 브랜치를 최신화한 뒤 작업 브랜치로 전환한다.

    이미 있는 작업 브랜치는 **이어서 쓴다**. 매번 `checkout -B`로 기준 브랜치 끝에
    새로 만들면 앞서 그 브랜치에 올린 커밋이 로컬에서 사라지고, 다음 push가
    non-fast-forward로 거절된다(연속 확정이 깨지던 원인).
    """
    workdir, _ = checkout(project)
    if branch == project.branch:
        return workdir
    remote = subprocess.run(
        ["git", *auth_args(project.git_url), "fetch", "origin", branch],
        cwd=workdir, capture_output=True, text=True,
    )
    if remote.returncode == 0:
        _git(workdir, "checkout", "-B", branch, "FETCH_HEAD")  # 원격 상태에 맞춰 이어간다
    elif _branch_exists(workdir, branch):
        _git(workdir, "checkout", branch)
    else:
        _git(workdir, "checkout", "-B", branch)  # 첫 커밋 — 기준 브랜치에서 뻗는다
    return workdir


def _branch_exists(workdir: Path, branch: str) -> bool:
    out = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=workdir, capture_output=True, text=True,
    )
    return out.returncode == 0


def file_tree(workdir: Path, limit: int = 200) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=workdir, capture_output=True, text=True
    )
    return out.stdout.splitlines()[:limit]


def read_context_files(workdir: Path, paths: list[str]) -> dict[str, str]:
    """LLM 컨텍스트로 주입할 파일 내용. 경로 탈출·바이너리·과대 파일 차단."""
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


def read_file_at_ref(workdir: Path, ref: str, rel: str) -> str | None:
    """특정 브랜치(ref)에 커밋된 파일 내용. 없으면 None.

    확정 산출물은 세션 브랜치에 커밋되므로, 워킹카피가 지금 어떤 브랜치에 있든
    (다른 세션이 체크아웃해 갔더라도) 커밋된 본문을 그대로 읽는다.
    """
    out = subprocess.run(
        ["git", "show", f"{ref}:{rel}"], cwd=workdir, capture_output=True, text=True
    )
    if out.returncode != 0:
        return None
    return out.stdout[:MAX_CONTEXT_FILE_BYTES]


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


def write_and_commit(project: Project, branch: str, rel_path: str, content: str, message: str) -> str:
    """작업 브랜치에 파일 하나를 쓰고 커밋한 뒤 원격(Gitea)에 push, 커밋 SHA를 반환한다.

    에이전트 기획의 단계 산출물 확정 경로 — diff가 아니라 완성된 문서를 리포에 남긴다.
    경로 탈출을 막고, 인증(git_auth)은 push에서만 주입한다(git_url은 로그·원격 인자로만).
    """
    workdir = ensure_branch(project, branch)
    root = workdir.resolve()
    target = (root / rel_path).resolve()
    if not target.is_relative_to(root):
        raise BuildError(f"경로가 워크스페이스를 벗어납니다: {rel_path}")
    target.parent.mkdir(parents=True, exist_ok=True)
    normalized = content.replace("\r\n", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    target.write_text(normalized, encoding="utf-8")

    _git(workdir, "add", "--", rel_path)
    # 같은 내용을 다시 확정하면 바뀐 게 없다 — git commit은 이걸 오류로 내지만
    # 사용자에겐 "이미 그 상태"라 실패가 아니다. 커밋을 건너뛰고 현재 커밋을 돌려준다.
    staged = subprocess.run(
        ["git", "status", "--porcelain"], cwd=workdir, capture_output=True, text=True
    )
    if staged.stdout.strip():
        _git(
            workdir,
            "-c", "user.name=paas-bot",
            "-c", "user.email=paas-bot@localhost",
            "commit", "-m", message,
        )
    _git(workdir, *auth_args(project.git_url), "push", "-u", "origin", branch)
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workdir, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def delete_branch(project: Project, branch: str) -> bool:
    """작업 브랜치를 로컬·원격에서 지운다. 원격 삭제 성공 여부를 반환한다.

    기본 브랜치는 절대 지우지 않는다 — 세션 정리가 프로젝트를 망가뜨리면 안 된다.
    이미 없는 브랜치나 원격 거절은 실패로 보지 않고 조용히 넘어간다(정리는 베스트 에포트).
    """
    if not branch or branch == project.branch:
        return False
    workdir = workdir_for(project)
    if not workdir.exists():
        return False
    # 지우려는 브랜치에 체크아웃돼 있으면 삭제할 수 없다 — 기본 브랜치로 먼저 옮긴다.
    subprocess.run(["git", "checkout", project.branch], cwd=workdir, capture_output=True, text=True)
    subprocess.run(["git", "branch", "-D", branch], cwd=workdir, capture_output=True, text=True)
    pushed = subprocess.run(
        ["git", *auth_args(project.git_url), "push", "origin", "--delete", branch],
        cwd=workdir, capture_output=True, text=True,
    )
    return pushed.returncode == 0


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


def detect_project_stack_and_deps(workdir: Path) -> dict:
    """프로젝트 워크스페이스의 package.json, requirements.txt, pyproject.toml, go.mod 등을 감지하여
    언어 스택, 프레임워크 및 주요 라이브러리 의존성 명세를 반환한다."""
    stack = {"language": "unknown", "framework": "unknown", "dependencies": []}
    if not workdir.exists():
        return stack

    # 1. Node.js / TypeScript / JavaScript
    pkg_json = workdir / "package.json"
    if pkg_json.is_file():
        stack["language"] = "TypeScript/JavaScript"
        try:
            content = json.loads(pkg_json.read_text(encoding="utf-8", errors="ignore"))
            deps = {**content.get("dependencies", {}), **content.get("devDependencies", {})}
            stack["dependencies"] = list(deps.keys())[:30]
            if "next" in deps:
                stack["framework"] = "Next.js"
            elif "express" in deps:
                stack["framework"] = "Express"
            elif "vite" in deps:
                stack["framework"] = "Vite / React"
            elif "react" in deps:
                stack["framework"] = "React"
            elif "vue" in deps:
                stack["framework"] = "Vue"
            elif "nest" in deps or "@nestjs/core" in deps:
                stack["framework"] = "NestJS"
        except Exception:
            pass
        return stack

    # 2. Python
    req_txt = workdir / "requirements.txt"
    pyproject = workdir / "pyproject.toml"
    if req_txt.is_file() or pyproject.is_file() or any(workdir.glob("*.py")):
        stack["language"] = "Python"
        deps = []
        if req_txt.is_file():
            for line in req_txt.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    pkg = line.split("==")[0].split(">=")[0].split("<=")[0].strip()
                    if pkg:
                        deps.append(pkg)
        stack["dependencies"] = deps[:30]
        dep_str = " ".join(deps).lower()
        if "fastapi" in dep_str:
            stack["framework"] = "FastAPI"
        elif "django" in dep_str:
            stack["framework"] = "Django"
        elif "flask" in dep_str:
            stack["framework"] = "Flask"
        return stack

    # 3. Go
    go_mod = workdir / "go.mod"
    if go_mod.is_file():
        stack["language"] = "Go"
        stack["framework"] = "Go Standard / Gin"
        return stack

    return stack
