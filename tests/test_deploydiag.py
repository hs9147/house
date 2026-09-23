"""배포 실패 진단 — 원인 하나와 **고칠 것**을 짚는다(LLM 없음, 파일 시스템이 근거).

배포가 실패하면 화면에는 오류 한 줄과 로그 파일만 남는다. 그것으로는 "리포가 잘못됐나 ·
설정이 잘못됐나 · 플랫폼이 잘못됐나"가 갈리지 않아서 사람이 로그를 열어 추측한다.
"""
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import SessionLocal
from app.main import create_app
from app.models import Deployment, DeploymentStatus, Project, ProjectType
from app.services import deploydiag

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"


def _write(root, rel: str, body: str = "x") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_source_in_subdir_is_fixable_by_the_platform(tmp_path):
    """실측 사례(supplier-pool): 루트에 시그니처가 없고 소스가 하위 폴더에 있었다.

    이건 플랫폼이 고칠 수 있다 — 빌드 대상 폴더를 그쪽으로 지정하면 배포 스크립트가 그
    폴더 기준으로 다시 만들어진다.
    """
    _write(tmp_path, "Strategy/peer_core.py")
    _write(tmp_path, "Strategy/requirements.txt", "streamlit\n")
    _write(tmp_path, "docs/agent-planning/01-기획서.md", "# 기획서")

    result = deploydiag.diagnose(tmp_path)
    assert result["cause"] == "source_in_subdir"
    assert result["fix"] == {"kind": "source_subdir", "value": "Strategy",
                             "options": ["Strategy"]}
    assert "Strategy" in result["detail"]


def test_docs_only_repo_cannot_be_fixed_by_the_platform(tmp_path):
    """시그니처가 아예 없으면 플랫폼이 대신 만들 수 없다 — 그렇게 말해야 한다."""
    _write(tmp_path, "docs/README.md", "# 문서만 있다")
    result = deploydiag.diagnose(tmp_path)
    assert result["cause"] == "no_entry_marker"
    assert result["fix"]["kind"] == "repo"
    assert "requirements.txt" in result["fix"]["needs"]


def test_docs_and_sample_data_are_not_proposed_as_source(tmp_path):
    """제안이 엉뚱하면 사람은 그 제안을 믿지 않게 된다 — 소스가 아닌 폴더는 후보가 아니다."""
    _write(tmp_path, "docs/package.json", "{}")          # 문서 폴더
    _write(tmp_path, "node_modules/pkg/package.json", "{}")
    result = deploydiag.diagnose(tmp_path)
    assert result["cause"] == "no_entry_marker"  # 후보가 하나도 없다


def test_streamlit_without_an_entry_file(tmp_path):
    """requirements.txt는 있는데 진입 파일이 없다 — start.cmd가 여기서 실패한다."""
    _write(tmp_path, "requirements.txt", "streamlit==1.40\n")
    result = deploydiag.diagnose(tmp_path, is_streamlit=True)
    assert result["cause"] == "streamlit_entry_missing"
    assert "streamlit_app.py" in result["detail"]

    _write(tmp_path, "app/streamlit_app.py", "import streamlit")
    assert deploydiag.diagnose(tmp_path, is_streamlit=True)["cause"] != "streamlit_entry_missing"


def test_missing_source_subdir_is_named(tmp_path):
    """지정된 폴더가 리포에 없으면 그것이 원인이다 — 다른 원인으로 뭉개지 않는다."""
    _write(tmp_path, "requirements.txt", "fastapi\n")
    result = deploydiag.diagnose(tmp_path, source_subdir="없는폴더")
    assert result["cause"] == "missing_source_dir"
    assert "없는폴더" in result["detail"]


def test_log_tail_distinguishes_npm_and_pip_failures(tmp_path):
    """시그니처가 있으면 실패는 설치·빌드에서 났다 — 로그가 근거다."""
    _write(tmp_path, "package.json", '{"name": "x"}')
    log = tmp_path / "build.log"
    log.write_text("npm error code E404\nnpm error 404 Not Found\n", encoding="utf-8")
    assert deploydiag.diagnose(tmp_path, log_path=str(log))["cause"] == "npm_failed"

    _write(tmp_path, "requirements.txt", "fastapi\n")
    (tmp_path / "package.json").unlink()
    log.write_text("ERROR: Could not find a version that satisfies\n", encoding="utf-8")
    result = deploydiag.diagnose(tmp_path, log_path=str(log))
    assert result["cause"] == "pip_failed"
    assert "사내" in result["detail"]  # 폐쇄망 인덱스를 먼저 의심하게 한다


def test_log_tail_is_included_as_evidence(tmp_path):
    _write(tmp_path, "requirements.txt", "fastapi\n")
    log = tmp_path / "build.log"
    log.write_text("마지막 줄이 근거다\n", encoding="utf-8")
    assert "근거" in deploydiag.diagnose(tmp_path, log_path=str(log))["log_tail"]
    # 로그가 없어도 진단은 돌아간다(로그 파일이 지워졌을 수 있다)
    assert deploydiag.diagnose(tmp_path, log_path="없는파일")["log_tail"] == ""


def _project_with_failed_deploy(monkeypatch, tmp_path, name="diag-app") -> int:
    monkeypatch.setenv("PAAS_WORK_DIR", str(tmp_path / "workspaces"))
    get_settings.cache_clear()
    create_app()
    db = SessionLocal()
    try:
        project = Project(name=name, type=ProjectType.streamlit,
                          git_url="https://git.example.com/o/x")
        db.add(project)
        db.commit()
        db.refresh(project)
        db.add(Deployment(project_id=project.id, git_sha="a" * 40, image_tag="",
                          profile="release", status=DeploymentStatus.failed,
                          error="npm run build 실패 (exit 1)"))
        db.commit()
        return project.id
    finally:
        db.close()


def test_diagnose_endpoint_reports_cause_and_fix(monkeypatch, tmp_path, fresh_settings):
    pid = _project_with_failed_deploy(monkeypatch, tmp_path)
    work = tmp_path / "workspaces" / "diag-app"
    _write(work, "Strategy/requirements.txt", "streamlit\n")

    from app.services import build as build_service

    monkeypatch.setattr(build_service, "checkout", lambda p, git_sha=None: (work, "a" * 40))
    from app.api import projects as projects_api

    monkeypatch.setattr(projects_api, "checkout", lambda p, git_sha=None: (work, "a" * 40))

    body = TestClient(create_app()).get(
        f"{API}/projects/{pid}/deploy/diagnose?profile=release", headers=ADMIN).json()
    assert body["cause"] == "source_in_subdir"
    assert body["fix"]["value"] == "Strategy"
    assert body["status"] == "failed"
    assert body["source_subdir"] == ""


def test_diagnose_without_history_is_404(monkeypatch, tmp_path, fresh_settings):
    monkeypatch.setenv("PAAS_WORK_DIR", str(tmp_path / "workspaces"))
    get_settings.cache_clear()
    c = TestClient(create_app())
    pid = c.post(f"{API}/projects", json={
        "name": "no-deploy", "type": "python", "git_url": "https://git.example.com/o/y",
    }, headers=ADMIN).json()["id"]
    assert c.get(f"{API}/projects/{pid}/deploy/diagnose", headers=ADMIN).status_code == 404


def test_applying_the_fix_rejects_a_folder_that_is_not_there(monkeypatch, tmp_path,
                                                             fresh_settings):
    """없는 폴더를 넣으면 다음 배포가 같은 자리에서 또 실패한다 — 그때는 원인이 하나 더 늘어난다."""
    pid = _project_with_failed_deploy(monkeypatch, tmp_path, name="apply-app")
    work = tmp_path / "workspaces" / "apply-app"
    _write(work, "Strategy/requirements.txt", "streamlit\n")
    c = TestClient(create_app())

    bad = c.put(f"{API}/projects/{pid}/source-subdir",
                json={"source_subdir": "없는폴더"}, headers=ADMIN)
    assert bad.status_code == 422

    ok = c.put(f"{API}/projects/{pid}/source-subdir",
               json={"source_subdir": "Strategy"}, headers=ADMIN)
    assert ok.status_code == 200
    assert ok.json()["source_subdir"] == "Strategy"

    # 빈 문자열은 지정 해제다(리포 루트)
    cleared = c.put(f"{API}/projects/{pid}/source-subdir",
                    json={"source_subdir": ""}, headers=ADMIN)
    assert cleared.json()["source_subdir"] is None
