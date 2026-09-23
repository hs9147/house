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


# --- LLM 분석(결정론 판정 위에 얹히는 별도 경로) ---

def test_facts_carry_the_script_that_actually_runs_and_no_env_values(tmp_path):
    """LLM에 싣는 사실에는 **실제로 실행되는** start.cmd가 들어가야 한다.

    템플릿을 읽고 추측하면, 저장된 스크립트로 도는 프로젝트를 틀리게 진단한다. 그리고
    환경변수 값은 절대 싣지 않는다 — 비밀이 모델·화면·로그로 새면 되돌릴 수 없다.
    """
    from app.models import BuildProfile
    from app.services import build as build_module

    _write(tmp_path, "requirements.txt", "fastapi\n")
    project = Project(name="diag-llm", type=ProjectType.python,
                      git_url="https://git.example.com/x",
                      start_scripts={
                          build_module.script_key(BuildProfile.release): "@echo off\nSAVED-ONE",
                      })
    result = deploydiag.diagnose(tmp_path, error="boom")
    facts = deploydiag.facts_for_llm(
        tmp_path, project,
        result=result,
        script=build_module.start_script_for(project, "", BuildProfile.release),
        detected_summary="app=python",
        error="boom",
        files=["requirements.txt", "app/main.py"],
    )
    assert "SAVED-ONE" in facts
    assert "app/main.py" in facts
    assert "boom" in facts
    assert result["cause"] in facts  # 결정론 판정을 먼저 알려 준다(대체가 아니라 덧붙임)
    # 프롬프트에 비밀이 실릴 자리가 없다 — env 값을 넘기는 인자 자체가 없다.
    assert "SECRET" not in facts


def test_explain_asks_for_korean_three_sections_and_returns_text(monkeypatch):
    """돌려주는 것은 글이다 — 저장하지도, 적용하지도 않는다."""
    seen = {}

    def fake_chat(provider, messages, db):
        seen["system"] = messages[0]["content"]
        seen["user"] = messages[1]["content"]
        return "  원인: 진입 파일이 없습니다\n근거: start.cmd exit /b 1\n고칠 것: main.py 추가  "

    from app.services import llm as llm_service
    monkeypatch.setattr(llm_service, "chat_completion", fake_chat)
    out = deploydiag.explain(object(), Project(name="x", git_url="g"), object(),
                             facts="facts here")
    assert out.startswith("원인:")
    assert "facts here" == seen["user"]
    assert "원인:" in seen["system"] and "근거:" in seen["system"] and "고칠 것:" in seen["system"]


def test_explain_endpoint_needs_a_provider_and_returns_the_analysis(monkeypatch, tmp_path,
                                                                    fresh_settings):
    """어느 모델로 읽을지는 사람이 고른다 — 없는 프로바이더는 404다."""
    from app.models import LlmProvider
    from app.services import build as build_service
    from app.services import llm as llm_service

    pid = _project_with_failed_deploy(monkeypatch, tmp_path, name="explain-app")
    work = tmp_path / "workspaces" / "explain-app"
    _write(work, "requirements.txt", "fastapi\n")
    monkeypatch.setattr(build_service, "checkout", lambda p, git_sha=None: (work, "a" * 40))
    from app.api import projects as projects_api
    monkeypatch.setattr(projects_api, "checkout", lambda p, git_sha=None: (work, "a" * 40))
    monkeypatch.setattr(projects_api.workspace, "code_workdir", lambda p: work)
    monkeypatch.setattr(projects_api.workspace, "file_tree",
                        lambda w, limit=200: ["requirements.txt"])
    monkeypatch.setattr(llm_service, "chat_completion",
                        lambda *a, **kw: "원인: pip 인덱스에 닿지 못했습니다")

    c = TestClient(create_app())
    url = f"{API}/projects/{pid}/deploy/diagnose/explain?profile=release"
    assert c.post(url, json={"provider_id": 9999}, headers=ADMIN).status_code == 404

    db = SessionLocal()
    try:
        provider = LlmProvider(name="p-diag", kind="openai", base_url="http://x", model="m")
        db.add(provider)
        db.commit()
        provider_id = provider.id
    finally:
        db.close()

    res = c.post(url, json={"provider_id": provider_id}, headers=ADMIN)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["analysis"].startswith("원인:")
    # 결정론 판정도 함께 온다 — LLM 분석이 그것을 대체하지 않는다.
    assert body["cause"]
    # 무엇을 보고 썼는지 사람이 확인할 수 있어야 한다.
    assert "start.cmd that actually runs" in body["facts"]
