"""등록된 레포 검토 → 공통 제약사항·단계 프롬프트 제안(정적 검사, LLM 없음).

제안에는 **근거**가 붙어야 한다. 근거 없는 제약은 지켜야 할 이유를 설명할 수 없고, 그러면
아무도 지키지 않는다.
"""
import subprocess

from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import SessionLocal
from app.main import create_app
from app.models import PlanConstraint, Project, ProjectType
from app.services import planadvice

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"


def _repo(root, name: str, files: dict[str, str], structure: dict | None = None) -> None:
    """워킹카피를 **실제 git 리포로** 만든다.

    검토가 보는 것은 `git ls-files`다(workspace.file_tree) — "커밋돼 있는가"가 판단의
    근거이므로 일부러 그렇게 두었다. 작업 디렉터리에만 있고 커밋되지 않은 파일로 제약을
    제안하면, 커밋한 사람이 없는데 규칙이 생긴다.
    """
    work = root / name
    for rel, body in files.items():
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    # .gitignore가 없어도 -f로 강제 추가한다 — .env·.pyc가 커밋된 상황을 재현하는 것이 목적이다.
    for args in (["add", "-A", "-f"],
                 ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"]):
        subprocess.run(["git", *args], cwd=work, check=True, capture_output=True)
    db = SessionLocal()
    try:
        db.add(Project(name=name, type=ProjectType.python,
                       git_url=f"https://git.example.com/o/{name}", structure=structure))
        db.commit()
    finally:
        db.close()


def _survey(monkeypatch, tmp_path) -> dict:
    monkeypatch.setenv("PAAS_WORK_DIR", str(tmp_path))
    get_settings.cache_clear()
    create_app()
    db = SessionLocal()
    try:
        return planadvice.survey(db)
    finally:
        db.close()


def test_committed_secret_becomes_a_constraint_with_evidence(monkeypatch, tmp_path,
                                                             fresh_settings):
    """실측 사례: supplier-pool의 Strategy/.env가 사내 Gitea에 커밋돼 있었다."""
    monkeypatch.setenv("PAAS_WORK_DIR", str(tmp_path))
    get_settings.cache_clear()
    create_app()
    _repo(tmp_path, "leaky", {"requirements.txt": "x\n", "Strategy/.env": "KEY=secret"})

    result = _survey(monkeypatch, tmp_path)
    secret = next(p for p in result["proposals"] if "비밀값" in p["text"])
    assert secret["kind"] == "common_constraint"
    assert "leaky: Strategy/.env" in secret["evidence"]
    assert "커밋" in secret["reason"]
    # 값 자체는 싣지 않는다 — 제안을 보여 주는 화면에 비밀이 다시 새면 안 된다
    assert "secret" not in str(secret)


def test_local_absolute_paths_become_a_constraint(monkeypatch, tmp_path, fresh_settings):
    """배포 서버에는 D:\\gp 같은 경로가 없다 — 이 환경에서 실제로 반복된 실수다."""
    monkeypatch.setenv("PAAS_WORK_DIR", str(tmp_path))
    get_settings.cache_clear()
    create_app()
    _repo(tmp_path, "pathy", {
        "requirements.txt": "x\n",
        "core.py": 'MASTER = "D:/gp/자료/업종마스터.xlsx"\n',
    })

    proposals = _survey(monkeypatch, tmp_path)["proposals"]
    local = next(p for p in proposals if "로컬 절대 경로" in p["text"])
    assert "pathy: core.py" in local["evidence"]


def test_generated_artifacts_become_a_gitignore_constraint(monkeypatch, tmp_path,
                                                           fresh_settings):
    monkeypatch.setenv("PAAS_WORK_DIR", str(tmp_path))
    get_settings.cache_clear()
    create_app()
    _repo(tmp_path, "junky", {
        "requirements.txt": "x\n",
        "__pycache__/mod.cpython-314.pyc": "binary",
    })

    proposals = _survey(monkeypatch, tmp_path)["proposals"]
    junk = next(p for p in proposals if ".gitignore" in p["text"])
    assert "junky: __pycache__/" in junk["evidence"]


def test_generated_dirs_are_not_scanned_for_paths(monkeypatch, tmp_path, fresh_settings):
    """생성물 안의 코드는 우리 코드가 아니다 — 거기서 찾은 경로로 제약을 만들면 안 된다."""
    monkeypatch.setenv("PAAS_WORK_DIR", str(tmp_path))
    get_settings.cache_clear()
    create_app()
    _repo(tmp_path, "vendored", {
        "requirements.txt": "x\n",
        "node_modules/pkg/index.js": 'const p = "C:/Users/someone/x"\n',
    })

    proposals = _survey(monkeypatch, tmp_path)["proposals"]
    assert not any("로컬 절대 경로" in p["text"] for p in proposals)


def test_missing_entry_point_proposes_a_prompt_change(monkeypatch, tmp_path, fresh_settings):
    """플랫폼이 고칠 수 없는 것은 제약이 아니라 **문서에 적게** 하는 쪽이 맞다."""
    monkeypatch.setenv("PAAS_WORK_DIR", str(tmp_path))
    get_settings.cache_clear()
    create_app()
    _repo(tmp_path, "docsonly", {"docs/README.md": "# 문서만"})

    proposals = _survey(monkeypatch, tmp_path)["proposals"]
    entry = next(p for p in proposals if p["kind"] == "stage_prompt"
                 and "실행 진입점" in p["text"])
    assert entry["stage"] == "principles"
    assert "docsonly" in entry["evidence"]


def test_type_distribution_proposes_a_solution_prompt_hint(monkeypatch, tmp_path,
                                                           fresh_settings):
    monkeypatch.setenv("PAAS_WORK_DIR", str(tmp_path))
    get_settings.cache_clear()
    create_app()
    _repo(tmp_path, "s1", {"requirements.txt": "streamlit\n"},
          structure={"components": [{"name": "app", "type": "streamlit"}]})
    _repo(tmp_path, "s2", {"requirements.txt": "streamlit\n"},
          structure={"components": [{"name": "app", "type": "streamlit"}]})

    result = _survey(monkeypatch, tmp_path)
    assert result["type_counts"] == {"streamlit": 2}
    hint = next(p for p in result["proposals"] if p["stage"] == "solution")
    assert "streamlit" in hint["text"]


def test_already_registered_constraints_are_not_proposed_again(monkeypatch, tmp_path,
                                                               fresh_settings):
    """같은 말을 두 번 싣는 것은 제약을 읽는 쪽에 비용만 늘린다."""
    monkeypatch.setenv("PAAS_WORK_DIR", str(tmp_path))
    get_settings.cache_clear()
    create_app()
    _repo(tmp_path, "dup", {"requirements.txt": "x\n", ".env": "K=1"})

    first = _survey(monkeypatch, tmp_path)["proposals"]
    secret = next(p for p in first if "비밀값" in p["text"])

    db = SessionLocal()
    try:
        db.add(PlanConstraint(text=secret["text"]))
        db.commit()
    finally:
        db.close()

    again = _survey(monkeypatch, tmp_path)["proposals"]
    assert not any("비밀값" in p["text"] for p in again)


def test_projects_without_a_working_copy_are_reported_as_skipped(monkeypatch, tmp_path,
                                                                 fresh_settings):
    """"제안이 없다"가 "문제가 없다"인지 "보지 못했다"인지 구분돼야 한다."""
    monkeypatch.setenv("PAAS_WORK_DIR", str(tmp_path))
    get_settings.cache_clear()
    create_app()
    db = SessionLocal()
    try:
        db.add(Project(name="never-cloned", type=ProjectType.python,
                       git_url="https://git.example.com/o/z"))
        db.commit()
    finally:
        db.close()

    result = _survey(monkeypatch, tmp_path)
    assert result["skipped"] == ["never-cloned"]
    assert result["inspected"] == []


def test_advice_endpoint_is_admin_only(monkeypatch, tmp_path, fresh_settings):
    monkeypatch.setenv("PAAS_WORK_DIR", str(tmp_path))
    get_settings.cache_clear()
    c = TestClient(create_app())
    assert c.get(f"{API}/plan/advice", headers=ADMIN).status_code == 200
    assert c.get(f"{API}/plan/advice").status_code in (401, 403)
