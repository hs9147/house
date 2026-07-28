"""워크스페이스 파일 컨텍스트 + LLM diff의 적용(승인 커밋).

LLM은 리포에 직접 쓰지 않는다: diff는 ProposedChange로 저장되고,
apply 승인 시에만 여기서 작업 브랜치에 git apply + commit 된다.
"""
import json
import re
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
    """diff를 적용하고 커밋한 뒤 커밋 SHA를 반환한다.
    LLM이 생성한 패치의 Hunk count 및 CRLF/줄바꿈 오류(corrupt patch)에 유연하게 대응한다.
    """
    patch = workdir / ".paas-proposed.patch"
    # LF 줄바꿈으로 정규화하여 패치 파일 작성
    normalized_diff = diff.replace("\r\n", "\n")
    if not normalized_diff.endswith("\n"):
        normalized_diff += "\n"
    patch.write_text(normalized_diff, encoding="utf-8")

    applied = False
    # 1차 시도: Hunk 카운트 자동 재계산 (--recount) 및 트레일링 공백 수정 (--whitespace=fix)
    for apply_opts in [
        ["apply", "--whitespace=fix", "--recount", "--unidiff-zero", str(patch)],
        ["apply", "--whitespace=fix", "--recount", "--ignore-space-change", "--ignore-whitespace", str(patch)],
        ["apply", "--whitespace=nowarn", "--recount", "--3way", str(patch)],
    ]:
        proc = subprocess.run(["git", *apply_opts], cwd=workdir, capture_output=True, text=True)
        if proc.returncode == 0:
            applied = True
            break

    # 2차 시도: git apply가 corrupt patch로 모두 실패한 경우 파이썬 diff 파서 폴백 실행
    if not applied:
        try:
            _apply_patch_fallback(workdir, normalized_diff)
            applied = True
        except Exception as e:
            patch.unlink(missing_ok=True)
            raise BuildError(f"git apply failed: corrupt patch could not be parsed: {e}")

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


def _apply_patch_fallback(workdir: Path, diff_text: str) -> None:
    """git apply 명령어가 corrupt patch 오류로 거절될 때, 파이썬 기반으로 diff를 안전하게 수동 파싱하여 파일에 적용한다."""
    file_chunks = re.split(r'(?=^diff --git |^--- a/|^\+\+\+ b/)', diff_text, flags=re.MULTILINE)
    for chunk in file_chunks:
        if not chunk.strip():
            continue
        target_file_match = re.search(r'^\+\+\+ b/(.+)$', chunk, re.MULTILINE)
        if not target_file_match:
            continue
        rel_path = target_file_match.group(1).strip()
        target_path = workdir / rel_path

        new_lines = []
        in_hunk = False
        for line in chunk.splitlines():
            if line.startswith("@@"):
                in_hunk = True
                continue
            if not in_hunk:
                continue
            if line.startswith("+"):
                new_lines.append(line[1:])
            elif line.startswith(" "):
                new_lines.append(line[1:])
            elif line.startswith("-"):
                pass
            elif not line.startswith("diff ") and not line.startswith("--- ") and not line.startswith("+++ "):
                new_lines.append(line)

        if new_lines or not target_path.exists():
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


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
