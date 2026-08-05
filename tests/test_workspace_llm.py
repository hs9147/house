"""LLM diff 추출·리뷰 파싱, 워크스페이스 diff 적용(실제 git), 프로바이더 URL 해석."""
import subprocess
from pathlib import Path

import pytest

from app.services import llm, workspace
from app.services.build import BuildError


def test_resolve_internal_project_url():
    """db 없이 호출하면(조직 조회 불가) 서브패스 조직 자리가 "_"로 안전하게 떨어진다."""
    assert llm.resolve_base_url("project://llm-main") == "http://apps.test/apps/_/llm-main/"
    assert llm.resolve_base_url("https://api.anthropic.com/") == "https://api.anthropic.com"


def test_resolve_internal_project_url_uses_target_organization():
    """db가 주어지면 project:// 대상의 실제 조직으로 서브패스를 구성한다 — 실제
    배포 URL(services/deployer.py)과 정확히 일치해야 한다."""
    from app.db import Base, engine
    from app.models import Organization, Project, ProjectType
    from sqlalchemy.orm import Session as ORMSession

    Base.metadata.create_all(engine)
    with ORMSession(engine) as db:
        org = Organization(name="research")
        db.add(org)
        db.commit()
        db.add(Project(name="llm-main", type=ProjectType.llm,
                        organization_id=org.id, git_url="https://git.example.com/x"))
        db.commit()

        assert llm.resolve_base_url("project://llm-main", db) == "http://apps.test/apps/research/llm-main/"

        db.query(Project).delete()
        db.query(Organization).delete()
        db.commit()


def test_review_parsing(monkeypatch):
    monkeypatch.setattr(
        llm, "_post_chat",
        lambda url, headers, payload: {"choices": [{"message": {"content":
            '```json\n[{"severity": "high", "file": "a.py", "comment": "SQL 인젝션"}]\n```'
        }}]},
    )
    from app.models import LlmProvider, LlmProviderKind

    provider = LlmProvider(name="t", kind=LlmProviderKind.openai,
                           base_url="https://x", model="m")
    findings = llm.review_diff(provider, "--- a/a.py\n+++ b/a.py\n")
    assert findings[0]["severity"] == "high"
    assert llm.max_severity(findings) == "high"
    assert llm.max_severity([]) == "none"


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / "hello.py").write_text('print("hello")\n')
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
        cwd=path, check=True,
    )


DIFF = """--- a/hello.py
+++ b/hello.py
@@ -1 +1 @@
-print("hello")
+print("hello, paas")
"""


def _remote_project(tmp_path, monkeypatch, fresh_settings):
    """실제 원격(bare 리포)과 프로젝트 한 개 — 연속 커밋의 push까지 진짜로 검증한다."""
    from app.config import get_settings
    from app.models import Project, ProjectType

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    _init_repo(seed)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=seed, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "main"], cwd=seed, check=True)

    monkeypatch.setenv("PAAS_WORK_DIR", str(tmp_path / "workspaces"))
    get_settings.cache_clear()
    return Project(name="plan-app", type=ProjectType.python, git_url=str(origin), branch="main")


def _committed_at(origin: Path, ref: str, rel: str) -> str:
    out = subprocess.run(["git", "show", f"{ref}:{rel}"], cwd=origin,
                         capture_output=True, text=True, check=True)
    return out.stdout


def test_consecutive_commits_keep_the_branch_history(tmp_path, monkeypatch, fresh_settings):
    """회귀: 매번 기준 브랜치에서 새로 뻗으면 두 번째 push가 non-fast-forward로 거절된다."""
    project = _remote_project(tmp_path, monkeypatch, fresh_settings)
    branch = "paas/plan-1-abcd1234"

    first = workspace.write_and_commit(
        project, branch, "docs/agent-planning/01-기획서.md", "# 기획서\n", "plan(spec)")
    second = workspace.write_and_commit(
        project, branch, "docs/agent-planning/02-아키텍처설계.md", "# 설계\n", "plan(architecture)")
    assert first != second

    origin = tmp_path / "origin.git"
    # 앞 단계 산출물이 살아 있고, 뒤 단계가 그 위에 쌓였다
    assert _committed_at(origin, branch, "docs/agent-planning/01-기획서.md") == "# 기획서\n"
    assert _committed_at(origin, branch, "docs/agent-planning/02-아키텍처설계.md") == "# 설계\n"
    log = subprocess.run(["git", "log", "--format=%s", branch], cwd=origin,
                         capture_output=True, text=True, check=True)
    assert log.stdout.split("\n")[:2] == ["plan(architecture)", "plan(spec)"]


def test_reconfirming_identical_content_is_not_an_error(tmp_path, monkeypatch, fresh_settings):
    """같은 내용을 다시 확정하면 '바뀐 게 없다'는 실패가 아니라 현재 커밋 그대로다."""
    project = _remote_project(tmp_path, monkeypatch, fresh_settings)
    branch = "paas/plan-2-beef0001"
    path = "docs/agent-planning/01-기획서.md"

    sha = workspace.write_and_commit(project, branch, path, "# 기획서\n", "plan(spec)")
    again = workspace.write_and_commit(project, branch, path, "# 기획서\n", "plan(spec) 재확정")
    assert again == sha  # 새 커밋을 만들지 않는다


def test_context_files_guardrails(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "big.py").write_text("x" * 50_000)
    (repo / "bin.dat").write_text("data")
    files = workspace.read_context_files(repo, ["hello.py", "big.py", "bin.dat", "../escape"])
    assert list(files) == ["hello.py"]


def test_read_file_returns_content(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    assert workspace.read_file(repo, "hello.py") == 'print("hello")\n'


def test_read_file_rejects_path_escape(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    with pytest.raises(FileNotFoundError):
        workspace.read_file(repo, "../escape")


def test_read_file_rejects_missing_file(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    with pytest.raises(FileNotFoundError):
        workspace.read_file(repo, "nope.py")


def test_read_file_rejects_oversized_file(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "huge.py").write_text("x" * (workspace.MAX_VIEW_FILE_BYTES + 1))
    with pytest.raises(ValueError):
        workspace.read_file(repo, "huge.py")


def test_korean_paths_survive_a_non_utf8_locale(tmp_path, monkeypatch, fresh_settings):
    """회귀: POSIX/C 로케일에서 한글 산출물 경로·문서가 깨져 요청이 통째로 실패했다.

    git 출력 디코딩은 로케일이 아니라 UTF-8로 고정해야 하고, 파일시스템 인코딩으로
    표현할 수 없는 경로는 원인을 알려주며 멈춰야 한다. 인코딩은 인터프리터 기동 시
    결정되므로 자식 프로세스를 띄워 검증한다.
    """
    import json as _json
    import os
    import sys

    project = _remote_project(tmp_path, monkeypatch, fresh_settings)
    script = tmp_path / "run.py"
    script.write_text(
        "import json, sys\n"
        f"sys.path.insert(0, {str(Path.cwd())!r})\n"
        "from app.models import Project, ProjectType\n"
        "from app.services import workspace\n"
        f"p = Project(name='plan-app', type=ProjectType.python, git_url={project.git_url!r},"
        " branch='main')\n"
        "out = {'fs': sys.getfilesystemencoding()}\n"
        "try:\n"
        "    sha = workspace.write_and_commit(\n"
        "        p, 'paas/plan-1-abcd1234', 'docs/agent-planning/01-기획서.md',\n"
        "        '# 기획서\\n한글 본문\\n', 'plan(spec): 기획서 확정')\n"
        "    out['committed'] = bool(sha)\n"
        "    body = workspace.read_file_at_ref(\n"
        "        workspace.workdir_for(p), 'paas/plan-1-abcd1234',\n"
        "        'docs/agent-planning/01-기획서.md')\n"
        "    out['read'] = body\n"
        "except Exception as e:\n"
        "    out['error'] = f'{type(e).__name__}: {e}'\n"
        "print(json.dumps(out, ensure_ascii=True))\n",
        encoding="utf-8",
    )
    env = {
        **os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "1",
        "PAAS_WORK_DIR": str(tmp_path / "workspaces"),
        "PYTHONIOENCODING": "utf-8",
    }
    proc = subprocess.run([sys.executable, str(script)], capture_output=True,
                          text=True, encoding="utf-8", env=env)
    assert proc.returncode == 0, proc.stderr[-2000:]
    result = _json.loads(proc.stdout.strip().splitlines()[-1])
    assert "error" not in result, result["error"]
    assert result["committed"] is True
    assert result["read"] == "# 기획서\n한글 본문\n"  # 로케일과 무관하게 UTF-8로 읽는다

    # UTF-8 모드마저 꺼져 파일시스템 인코딩이 ascii면, 무엇을 고쳐야 하는지 알려주며 멈춘다
    ascii_env = {**env, "PYTHONUTF8": "0", "PAAS_WORK_DIR": str(tmp_path / "ws-ascii")}
    proc = subprocess.run([sys.executable, str(script)], capture_output=True,
                          text=True, encoding="utf-8", env=ascii_env)
    result = _json.loads(proc.stdout.strip().splitlines()[-1])
    if result["fs"] == "utf-8":
        return  # 이 플랫폼은 로케일과 무관하게 utf-8(Windows 등) — 검증할 실패 경로가 없다
    assert "PYTHONUTF8=1" in result["error"], result
