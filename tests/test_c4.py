"""C4 시각화 — 확정 산출물의 mermaid C4 블록 파싱과 단계별 병합(정적 파싱, LLM 없음)."""
from app.services import c4

SPEC = """# 기획서

목적과 범위.

## C4 다이어그램

```mermaid
C4Context
  title 기획 관리 시스템 컨텍스트
  Person(planner, "기획자", "단계를 확정한다")
  Person_Ext(builder, "외주 빌더", "코드를 구현한다")
  System(paas, "GPaaS", "기획·배포 플랫폼")
  System_Ext(gitea, "Gitea", "산출물 리포")
  Rel(planner, paas, "단계 확정", "HTTPS")
  Rel(paas, gitea, "산출물 커밋")
  BiRel(builder, paas, "MCP 연동")
```
"""

ARCHITECTURE = """# 아키텍처 설계

## C4 다이어그램

```mermaid
C4Container
  System_Boundary(paas, "GPaaS") {
    Container(console, "콘솔", "React", "기획 화면")
    Container(api, "API", "FastAPI", "단계 진행·확정")
    ContainerDb(db, "메타 DB", "SQLite", "포인터 저장")
  }
  System_Ext(gitea, "Gitea", "산출물 리포")
  Rel(console, api, "REST", "HTTPS")
  Rel(api, db, "읽기·쓰기")
  Rel(api, gitea, "커밋")
```

```mermaid
C4Component
  Container_Boundary(api, "API") {
    Component(planner, "기획 서비스", "Python", "단계 정의", $link="app/services/planning.py")
    Component(c4svc, "C4 파서", "Python", "블록 파싱", $link="app/services/c4.py")
  }
  Rel(planner, c4svc, "블록 규약 공유")
```
"""

# ③ 솔루션 구성이 같은 레벨을 다시 그린 문서 — 실제 모듈 이름으로 구체화한 그림.
SOLUTION = """# 솔루션 구성

```mermaid
C4Context
  Person(planner, "기획자")
  System(paas, "GPaaS", "내부 솔루션만 사용")
  System_Ext(llm_gw, "LLM 게이트웨이", "중앙 경유")
  Rel(paas, llm_gw, "프록시 경유")
```
"""


def _elements(level: dict) -> dict[str, dict]:
    return {e["alias"]: e for e in level["elements"]}


def test_context_block_reads_people_and_external_systems():
    levels = c4.model_from_stages([("spec", SPEC)])
    assert set(levels) == {"context"}
    level = levels["context"]
    assert level["stage"] == "spec"
    assert level["title"] == "기획 관리 시스템 컨텍스트"

    elements = _elements(level)
    assert elements["planner"]["base"] == "person"
    assert elements["planner"]["label"] == "기획자"
    assert elements["planner"]["description"] == "단계를 확정한다"
    assert elements["planner"]["external"] is False
    # 외부 사용자·외부 시스템은 _Ext로 구분돼야 한다 — 화면에서 내외부를 갈라 그린다.
    assert elements["builder"]["external"] is True
    assert elements["gitea"]["base"] == "system"
    assert elements["gitea"]["external"] is True

    relations = {(r["source"], r["target"]): r for r in level["relations"]}
    assert relations[("planner", "paas")]["label"] == "단계 확정"
    assert relations[("planner", "paas")]["technology"] == "HTTPS"
    assert relations[("builder", "paas")]["bidirectional"] is True


def test_container_and_component_blocks_keep_boundary_nesting():
    levels = c4.model_from_stages([("architecture", ARCHITECTURE)])
    assert set(levels) == {"container", "component"}

    containers = _elements(levels["container"])
    assert containers["paas"]["base"] == "boundary"
    assert containers["paas"]["boundary_type"] == "system"
    # 경계 안의 컨테이너는 부모를 가리켜야 한다(중첩 렌더의 근거).
    assert containers["console"]["parent"] == "paas"
    assert containers["console"]["technology"] == "React"
    assert containers["db"]["shape"] == "db"
    # 경계 밖 선언은 부모가 없다 — `}`로 경계가 닫힌 뒤 나온 것이다.
    assert containers["gitea"]["parent"] is None

    components = _elements(levels["component"])
    assert components["planner"]["parent"] == "api"
    assert components["planner"]["link"] == "app/services/planning.py"


def test_later_stage_overrides_same_level():
    """③ 솔루션 구성이 다시 그린 그림이 ②의 그림을 대체한다(구체화)."""
    levels = c4.model_from_stages([
        ("spec", SPEC), ("architecture", ARCHITECTURE), ("solution", SOLUTION),
    ])
    assert levels["context"]["stage"] == "solution"
    assert "llm_gw" in _elements(levels["context"])
    # 솔루션 문서가 다시 그리지 않은 레벨은 앞 단계 그림이 그대로 남는다.
    assert levels["container"]["stage"] == "architecture"
    assert levels["component"]["stage"] == "architecture"


def test_unparsable_lines_do_not_lose_the_diagram():
    """스타일 지시·오타 한 줄 때문에 그림 전체를 잃지 않는다."""
    levels = c4.model_from_stages([("spec", """
```mermaid
C4Context
  %% 주석
  UpdateElementStyle(paas, $fontColor="white")
  Deployment_Node(node, "서버")
  Person(planner, "기획자")
  System(paas, "GPaaS")
  Rel(planner, paas, "쓴다")
  이건 선언이 아니다
```
""")])
    elements = _elements(levels["context"])
    assert set(elements) == {"planner", "paas"}
    assert len(levels["context"]["relations"]) == 1


def test_document_without_c4_block_yields_no_levels():
    assert c4.model_from_stages([("spec", "# 기획서\n\n블록 없음.\n")]) == {}


def test_component_paths_prefer_link_over_name_match():
    tree = [
        "app/services/planning.py", "app/services/c4.py", "app/api/planning.py",
        "console/src/pages/AgentPlanning.tsx",
    ]
    components = _elements(c4.model_from_stages([("architecture", ARCHITECTURE)])["component"])
    # $link이 있으면 그 경로가 원천이다 — 이름이 같은 다른 파일(app/api/planning.py)을 끌어오지 않는다.
    assert c4.component_paths(components["planner"], tree) == ["app/services/planning.py"]


def test_component_paths_fall_back_to_name_when_link_missing():
    """`$link` 관례 이전에 확정된 문서에서도 code 레벨이 비지 않는다."""
    element = {"alias": "planning", "label": "기획 서비스", "link": ""}
    tree = ["app/services/planning.py", "app/api/planning.py", "app/services/c4.py"]
    assert c4.component_paths(element, tree) == [
        "app/services/planning.py", "app/api/planning.py",
    ]


def test_component_paths_with_directory_link():
    element = {"alias": "console", "label": "콘솔", "link": "console/src/pages"}
    tree = ["console/src/pages/AgentPlanning.tsx", "console/src/lib/api.ts", "console/src/pages"]
    assert c4.component_paths(element, tree) == [
        "console/src/pages/AgentPlanning.tsx", "console/src/pages",
    ]


def test_same_blocks_ignores_prose_and_whitespace():
    """라벨이 말하는 것은 **그림**이다 — 산문을 고쳤다고 그림이 미확정이 되면 안 된다."""
    edited = SPEC.replace("목적과 범위.", "목적과 범위를 더 자세히 적었다.")
    assert c4.same_blocks(SPEC, edited)
    # 여백·마지막 개행 차이도 같은 그림이다(편집기·커밋 사이에서 흔히 생긴다)
    assert c4.same_blocks(SPEC, SPEC + "\n\n")


def test_same_blocks_detects_a_changed_diagram():
    changed = SPEC.replace('Person(planner, "기획자"', 'Person(planner, "기획 담당자"')
    assert not c4.same_blocks(SPEC, changed)
    # 블록이 아예 없어진 것도 다른 그림이다(잘린 문서가 확정되면 이렇게 된다)
    assert not c4.same_blocks(SPEC, "# 기획서\n\n블록 없음\n")


def test_model_marks_untouched_confirmed_stage_as_confirmed():
    """확정된 단계를 열어 보기만 해도 "초안(미확정)"으로 보이던 문제의 판정 지점.

    편집기에는 확정본이 들어 있으므로, 내용이 같으면 그 단계를 draft로 넘기지 않는다
    (api/planning.get_plan_c4_model이 same_blocks로 가린다).
    """
    as_draft = c4.model_from_stages([("spec", SPEC)], draft_stage="spec")
    assert as_draft["context"]["confirmed"] is False  # 초안으로 넘기면 미확정

    as_confirmed = c4.model_from_stages([("spec", SPEC)], draft_stage=None)
    assert as_confirmed["context"]["confirmed"] is True
    assert as_confirmed["context"]["stage"] == "spec"  # 어느 단계에서 왔는지는 그대로
