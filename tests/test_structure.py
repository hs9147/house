"""프로젝트 구조 감지 — 리포 파일 구조에서 배포 단위를 읽는다(결정론, LLM 없음)."""
from app.models import ProjectType
from app.services import structure


def _write(root, rel: str, body: str = "x") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _by_path(result: dict) -> dict[str, dict]:
    return {c["path"]: c for c in result["components"]}


def test_single_component_repo_is_one_entry_at_root(tmp_path):
    """단일 프로젝트도 구조 하나짜리다 — 배포에서 특수 경우가 되지 않게."""
    _write(tmp_path, "requirements.txt", "fastapi\n")
    result = structure.detect(tmp_path)
    assert [(c["path"], c["type"], c["name"]) for c in result["components"]] == [
        (".", "python", "app"),
    ]
    assert structure.representative_type(result) is ProjectType.python


def test_folder_names_do_not_matter(tmp_path):
    """회귀: 예전에는 backend/·frontend/ 두 이름만 잡았다 — api/·web/은 놓쳤다."""
    _write(tmp_path, "api/requirements.txt", "fastapi\n")
    _write(tmp_path, "web/package.json", '{"dependencies": {"react": "18"}}')
    components = _by_path(structure.detect(tmp_path))
    assert components["api"]["type"] == "python"
    assert components["web"]["type"] == "react"
    assert structure.representative_type(structure.detect(tmp_path)) is ProjectType.composite


def test_three_or_more_components_are_all_kept(tmp_path):
    """회귀: composite는 정확히 둘일 때만 인정됐다 — 셋이면 하나도 못 잡았다."""
    _write(tmp_path, "api/pyproject.toml", "[project]\nname='x'\n")
    _write(tmp_path, "web/package.json", '{"dependencies": {"react": "18"}}')
    _write(tmp_path, "worker/requirements.txt", "celery\n")
    components = _by_path(structure.detect(tmp_path))
    assert set(components) == {"api", "web", "worker"}
    assert components["worker"]["type"] == "python"


def test_nested_monorepo_layout_is_found(tmp_path):
    """`apps/web`처럼 한 단계 묶는 배치가 흔하다(MAX_DEPTH=2)."""
    _write(tmp_path, "apps/web/package.json", '{"dependencies": {"react": "18"}}')
    _write(tmp_path, "services/api/requirements.txt", "fastapi\n")
    components = _by_path(structure.detect(tmp_path))
    assert set(components) == {"apps/web", "services/api"}
    # 이름은 경로에서 만든다 — 배지에 그대로 찍힌다
    assert components["apps/web"]["name"] == "apps-web"


def test_component_internals_are_not_split_further(tmp_path):
    """컴포넌트 안쪽의 package.json은 그 컴포넌트의 일부다 — 따로 세지 않는다."""
    _write(tmp_path, "frontend/package.json", '{"dependencies": {"react": "18"}}')
    _write(tmp_path, "frontend/functions/package.json", '{"dependencies": {}}')
    assert set(_by_path(structure.detect(tmp_path))) == {"frontend"}


def test_dependency_and_build_dirs_are_skipped(tmp_path):
    """node_modules를 빼지 않으면 의존성 하나하나가 배포 단위로 잡힌다."""
    _write(tmp_path, "package.json", '{"dependencies": {"react": "18"}}')
    _write(tmp_path, "node_modules/lodash/package.json", '{"name": "lodash"}')
    _write(tmp_path, "dist/index.html", "<html></html>")
    _write(tmp_path, ".venv/x/pyproject.toml", "[project]\n")
    assert set(_by_path(structure.detect(tmp_path))) == {"."}


def test_root_and_subfolders_are_both_kept(tmp_path):
    """루트에도 서브폴더에도 시그니처가 있으면 둘 다 싣는다 — 하나를 골라 버리면
    판단 근거가 사라진다."""
    _write(tmp_path, "requirements.txt", "fastapi\n")
    _write(tmp_path, "frontend/package.json", '{"dependencies": {"react": "18"}}')
    assert set(_by_path(structure.detect(tmp_path))) == {".", "frontend"}


def test_streamlit_beats_plain_python(tmp_path):
    """streamlit 앱에도 requirements.txt가 있다 — 더 구체적인 신호를 먼저 본다.
    (배포 형상이 다르다: streamlit run · 포트 8501)"""
    _write(tmp_path, "requirements.txt", "pandas==2.0\nstreamlit==1.28.0\n")
    assert structure.detect_type(tmp_path) is ProjectType.streamlit


def test_llm_is_separated_from_python(tmp_path):
    """vLLM은 GPU 배정·모델 로딩이 붙는 별개 형상이다."""
    _write(tmp_path, "requirements.txt", "vllm>=0.5.0\ntorch\n")
    assert structure.detect_type(tmp_path) is ProjectType.llm


def test_requirement_names_ignore_versions_and_comments(tmp_path):
    _write(tmp_path, "requirements.txt", "# streamlit 아님\nnot-streamlit==1.0\n")
    assert structure.detect_type(tmp_path) is ProjectType.python


def test_react_is_separated_from_plain_node(tmp_path):
    _write(tmp_path, "package.json", '{"devDependencies": {"react": "18"}}')
    assert structure.detect_type(tmp_path) is ProjectType.react
    _write(tmp_path, "package.json", '{"dependencies": {"express": "4"}}')
    assert structure.detect_type(tmp_path) is ProjectType.node


def test_unparsable_package_json_still_counts_as_node(tmp_path):
    """깨진 매니페스트로 react 여부는 알 수 없지만, node 프로젝트인 것은 사실이다."""
    _write(tmp_path, "package.json", "{ not json")
    assert structure.detect_type(tmp_path) is ProjectType.node


def test_unknown_folder_is_reported_as_none_not_guessed(tmp_path):
    """추측성 기본값을 넣으면 엉뚱한 Dockerfile로 빌드된다 — 판정 불가를 드러낸다."""
    _write(tmp_path, "go.mod", "module x\n")
    assert structure.detect_type(tmp_path) is None
    assert structure.detect(tmp_path)["components"] == []


def test_missing_workdir_is_empty_not_an_error(tmp_path):
    assert structure.detect(tmp_path / "nope")["components"] == []


def test_representative_type_maps_to_the_existing_column(tmp_path):
    assert structure.representative_type(None) is None
    assert structure.representative_type({"components": []}) is None
    assert structure.representative_type(
        {"components": [{"type": "react"}]}) is ProjectType.react
    assert structure.representative_type(
        {"components": [{"type": "python"}, {"type": "react"}]}) is ProjectType.composite
    # 판정 불가 컴포넌트 하나만 있으면 대표 타입도 없다
    assert structure.representative_type({"components": [{"type": None}]}) is None


def test_same_components_ignores_metadata(tmp_path):
    """감지 시각·커밋이 달라도 배포에 영향이 없으면 같은 구조다."""
    a = {"detected_at": "1", "components": [{"path": "api", "type": "python"}]}
    b = {"detected_at": "2", "components": [{"path": "api", "type": "python"}]}
    assert structure.same_components(a, b)
    c = {"components": [{"path": "api", "type": "node"}]}
    assert not structure.same_components(a, c)
    # 순서가 달라도 같다
    two = {"components": [{"path": "a", "type": "python"}, {"path": "b", "type": "react"}]}
    two_rev = {"components": [{"path": "b", "type": "react"}, {"path": "a", "type": "python"}]}
    assert structure.same_components(two, two_rev)


def test_summary_reads_in_one_line():
    assert structure.summary({"components": [
        {"name": "backend", "type": "python"}, {"name": "frontend", "type": None},
    ]}) == "backend=python, frontend=판정불가"


# --- 저장·갱신 (services/structure.refresh) ---


class _FakeProject:
    def __init__(self, name="p", structure=None, ptype=ProjectType.python):
        self.name = name
        self.structure = structure
        self.type = ptype


class _FakeDb:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


def test_refresh_saves_and_aligns_the_representative_type(tmp_path, monkeypatch):
    """구조만 갱신하면 type 컬럼과 어긋난다 — 화면·필터·빌드가 그 값을 쓴다."""
    from app import audit

    recorded: list[tuple] = []
    monkeypatch.setattr(audit, "record",
                        lambda db, actor, action, target, detail=None: recorded.append(
                            (action, target, detail)))
    _write(tmp_path, "api/requirements.txt", "fastapi\n")
    _write(tmp_path, "web/package.json", '{"dependencies": {"react": "18"}}')

    project = _FakeProject()
    db = _FakeDb()
    detected, changed = structure.refresh(db, project, tmp_path, actor="tester")
    assert changed is True
    assert project.type is ProjectType.composite  # 대표 타입도 맞춰진다
    assert {c["path"] for c in project.structure["components"]} == {"api", "web"}
    assert detected["source"] == "repo"
    assert recorded and recorded[0][0] == "project.structure.detect"
    assert "api=python" in recorded[0][2]["after"]


def test_refresh_is_quiet_when_nothing_changed(tmp_path, monkeypatch):
    """배포마다 같은 감사 줄이 쌓이면 정작 구조가 바뀐 순간을 찾을 수 없다."""
    from app import audit

    recorded: list[tuple] = []
    monkeypatch.setattr(audit, "record",
                        lambda db, actor, action, target, detail=None: recorded.append((action,)))
    _write(tmp_path, "requirements.txt", "fastapi\n")
    project = _FakeProject()
    db = _FakeDb()
    structure.refresh(db, project, tmp_path, actor="t")
    assert len(recorded) == 1
    # 같은 리포를 다시 감지 — 기록도 커밋도 늘지 않는다
    commits = db.commits
    _detected, changed = structure.refresh(db, project, tmp_path, actor="t")
    assert changed is False
    assert len(recorded) == 1
    assert db.commits == commits


def test_refresh_never_wipes_a_saved_structure_on_empty_detection(tmp_path):
    """체크아웃이 실패해 빈 디렉터리를 봤을 때 확정해 둔 구조를 날리면 안 된다."""
    saved = {"components": [{"name": "api", "path": "api", "type": "python"}]}
    project = _FakeProject(structure=saved)
    kept, changed = structure.refresh(_FakeDb(), project, tmp_path / "nope", actor="t")
    assert changed is False
    assert project.structure == saved
    assert kept == saved


def test_refresh_keeps_the_chosen_type_when_detection_is_unclear(tmp_path):
    """판정 불가 컴포넌트만 있으면 대표 타입을 덮어쓰지 않는다(사용자가 고른 값 유지)."""
    _write(tmp_path, "go.mod", "module x\n")  # 시그니처가 아니다 → 컴포넌트 없음
    project = _FakeProject(ptype=ProjectType.node)
    _kept, changed = structure.refresh(_FakeDb(), project, tmp_path, actor="t")
    assert changed is False
    assert project.type is ProjectType.node
