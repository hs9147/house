"""배포 전 점검 — 걸기 **전에** 알 수 있는 것을 먼저 말한다.

항목마다 실측 사례가 있다. supplier-pool은 의존성 선언이 없어 앱이 기동 직후 죽었고(화면에는
nssm의 SERVICE_PAUSED만 남았다), 소스가 `Strategy/`에 있는데 빌드 대상 폴더가 비어 있어
설치가 리포 루트에서 돌았고, `Strategy/.env`가 커밋돼 있었다.
"""
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import SessionLocal
from app.main import create_app
from app.models import BuildProfile, Project, ProjectType
from app.services import build as build_module
from app.services import deploycheck

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"


def _write(root: Path, rel: str, body: str = "x") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def _git_repo(root: Path) -> None:
    """점검은 **커밋된** 파일을 본다(git ls-files) — 작업 트리에만 있는 것은 배포에 없다."""
    quiet = {"cwd": root, "capture_output": True}
    subprocess.run(["git", "init", "-q"], **quiet, check=True)
    subprocess.run(["git", "add", "-A", "-f"], **quiet, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "init", "--allow-empty"], **quiet, check=True)


def _items(result: dict) -> dict:
    return {i["key"]: i for i in result["items"]}


def test_missing_dependency_declaration_is_a_failure(tmp_path):
    """실측(supplier-pool): requirements.txt가 없어 플랫폼이 아무것도 설치하지 않았다."""
    _write(tmp_path, "app.py", "print(1)")
    _git_repo(tmp_path)
    project = Project(name="nodeps", type=ProjectType.python, git_url="g")

    items = _items(deploycheck.run(tmp_path, project))
    assert items["dependencies"]["status"] == "fail"
    assert "requirements.txt" in items["dependencies"]["detail"]
    # 실행 방법은 app.py로 정해진다 — 두 항목이 서로 다른 것을 본다.
    assert items["entry"]["status"] == "ok"


def test_source_in_subdir_without_source_subdir_is_a_failure(tmp_path):
    """소스가 하위 폴더인데 빌드 대상이 루트면 설치·기동이 빈 폴더에서 돈다."""
    _write(tmp_path, "Strategy/requirements.txt", "streamlit\n")
    _write(tmp_path, "Strategy/app.py", "print(1)")
    _git_repo(tmp_path)
    project = Project(name="subdir", type=ProjectType.streamlit, git_url="g")

    items = _items(deploycheck.run(tmp_path, project))
    assert items["source_subdir"]["status"] == "fail"
    assert "Strategy" in items["source_subdir"]["fix"]

    project.source_subdir = "Strategy"
    fixed = _items(deploycheck.run(tmp_path, project))
    assert fixed["source_subdir"]["status"] == "ok"
    assert fixed["dependencies"]["status"] == "ok"
    assert fixed["streamlit_entry"]["status"] == "ok"  # Strategy/app.py가 진입 파일이다


def test_saved_script_is_revalidated_against_current_rules(tmp_path):
    """저장 시점 이후로 규칙이 늘어난다(ASCII 전용은 나중에 생겼다) — 지금 기준으로 본다."""
    _write(tmp_path, "requirements.txt", "streamlit\n")
    _write(tmp_path, "app.py", "print(1)")
    _git_repo(tmp_path)
    project = Project(name="oldscript", type=ProjectType.python, git_url="g",
                      start_scripts={
                          build_module.script_key(BuildProfile.release):
                              "@echo off\nREM 한글 주석\napp --port 8000\n",
                      })
    items = _items(deploycheck.run(tmp_path, project))
    assert items["start_script"]["status"] == "fail"
    assert "ASCII" in items["start_script"]["detail"] or "%PORT%" in items["start_script"]["detail"]

    # 다른 프로필의 스크립트는 이 프로필 점검에 끌려 들어오지 않는다.
    dev = _items(deploycheck.run(tmp_path, project, BuildProfile.development))
    assert dev["start_script"]["status"] == "ok"


def test_committed_secrets_and_install_artifacts_are_reported(tmp_path):
    """배포 실패는 아니지만 배포보다 급한 문제다(실측: Strategy/.env가 커밋돼 있었다)."""
    _write(tmp_path, "requirements.txt", "streamlit\n")
    _write(tmp_path, "app.py", "print(1)")
    _write(tmp_path, "Strategy/.env", "OPENAI_API_KEY=x")
    _write(tmp_path, "node_modules/left-pad/index.js", "module.exports=1")
    _git_repo(tmp_path)
    project = Project(name="leaky", type=ProjectType.python, git_url="g")

    items = _items(deploycheck.run(tmp_path, project))
    assert items["committed_secrets"]["status"] == "fail"
    assert ".env" in items["committed_secrets"]["detail"]
    assert items["install_artifacts"]["status"] == "warn"
    assert "node_modules/" in items["install_artifacts"]["detail"]


def test_clean_repo_passes_everything(tmp_path):
    _write(tmp_path, "requirements.txt", "fastapi\n")
    _write(tmp_path, "main.py", "app = 1")
    _git_repo(tmp_path)
    project = Project(name="clean", type=ProjectType.python, git_url="g")
    result = deploycheck.run(tmp_path, project)
    assert result["summary"]["fail"] == 0 and result["summary"]["warn"] == 0


def test_endpoint_returns_items_without_changing_anything(monkeypatch, tmp_path,
                                                          fresh_settings):
    monkeypatch.setenv("PAAS_WORK_DIR", str(tmp_path / "workspaces"))
    get_settings.cache_clear()
    work = tmp_path / "workspaces" / "checkapi"
    _write(work, "app.py", "print(1)")
    _git_repo(work)

    c = TestClient(create_app())
    pid = c.post(f"{API}/projects", json={
        "name": "checkapi", "type": "python", "git_url": "https://git.example.com/o/x",
    }, headers=ADMIN).json()["id"]
    from app.api import projects as projects_api
    monkeypatch.setattr(projects_api.workspace, "code_workdir", lambda p: work)

    body = c.get(f"{API}/projects/{pid}/deploy/check", headers=ADMIN).json()
    keys = {i["key"] for i in body["items"]}
    assert {"dependencies", "entry", "source_subdir", "start_script"} <= keys
    assert body["run_dir"] == "단일=(리포 루트)"
    # 점검은 프로젝트를 바꾸지 않는다.
    db = SessionLocal()
    try:
        assert db.get(Project, pid).start_scripts is None
    finally:
        db.close()


def test_composite_is_checked_per_component(tmp_path):
    """실측(negowith): 점검이 폴더 하나만 보고 다른 컴포넌트의 문제를 "확인"으로 덮었다.

    복합 배포는 컴포넌트마다 폴더·유닛이 따로다 — 점검도 그렇게 봐야 배포와 같은 것을 본다.
    source_subdir는 복합에서 실행 폴더가 아니므로 기준이 되어서도 안 된다.
    """
    _write(tmp_path, "api/app/main.py", "import pandas\n")   # 선언되지 않은 import
    _write(tmp_path, "api/requirements.txt", "fastapi\n")
    _write(tmp_path, "web/package.json", '{"dependencies": {"react": "18"}}')
    _git_repo(tmp_path)
    project = Project(name="nego-like", type=ProjectType.composite, git_url="g",
                      source_subdir="web",  # 복합에서는 이 값이 실행 폴더가 아니다
                      structure={"components": [
                          {"name": "api", "path": "api", "type": "python"},
                          {"name": "web", "path": "web", "type": "react"},
                      ]})

    result = deploycheck.run(tmp_path, project)
    keys = {i["key"] for i in result["items"]}
    assert "api:dependencies" in keys and "web:dependencies" in keys
    # api 쪽 문제가 web 점검에 묻히지 않는다.
    api_imports = next(i for i in result["items"] if i["key"] == "api:undeclared_imports")
    assert api_imports["status"] == "warn"
    assert "pandas" in api_imports["detail"]
    assert "[api]" in api_imports["title"]


def test_undeclared_imports_ignores_stdlib_and_local_modules(tmp_path):
    """오탐이 나면 아무도 읽지 않는다 — 표준 라이브러리와 같은 리포의 모듈은 빼야 한다."""
    _write(tmp_path, "requirements.txt", "fastapi\npython-dotenv\n")
    _write(tmp_path, "main.py",
           "import os\nimport json\nfrom pathlib import Path\n"
           "import fastapi\nfrom dotenv import load_dotenv\nimport helpers\n")
    _write(tmp_path, "helpers.py", "x = 1\n")
    _git_repo(tmp_path)
    project = Project(name="clean-imports", type=ProjectType.python, git_url="g")

    item = next(i for i in deploycheck.run(tmp_path, project)["items"]
                if i["key"] == "undeclared_imports")
    assert item["status"] == "ok", item
