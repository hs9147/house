"""커밋 → 작업 지시 연결 — 리포를 보고 진행 현황을 판정한다(결정론, LLM 없음).

**왜 필요한가.** 예전에는 작업의 진행 현황을 `BuildTask.commit_sha`로만 판정했다. 그 값은
외주 빌더가 MCP(`submit_build_result`)로 보고할 때만 채워지므로, 사람이 직접 커밋해서
기본 브랜치에 push하면 **어떤 작업에도 sha가 없어 판정이 통째로 건너뛰어졌다.** 화면에는
"반영 0건 · 머지 대기 0건"이 찍혀서, 판정할 것이 없었던 것과 판정했는데 반영이 없는 것이
구분되지 않았다 — 동작하는 것처럼 보이면서 아무 일도 일어나지 않았다.

**어떻게 잇는가.** 커밋 메시지의 작업 참조(`task #3`)를 읽는다. 규약 하나면 사람이 커밋해도
빌더가 커밋해도 같은 방식으로 잡히고, git 로그가 원천이라 추정이 없다. 규약은 작업 지시
문서와 MCP 안내에 함께 실어 빌더가 알 수 있게 한다(services/planning.render_tasks_doc).

**추측하지 않는다.** 참조가 없는 커밋은 어느 작업의 것인지 알 수 없다. 커밋 메시지나 변경
파일로 작업을 짐작하면 엉뚱한 작업이 완료로 바뀌고, 그건 조용히 틀리는 쪽이다. 근거를 찾지
못한 작업은 상태를 건드리지 않고 그 수를 돌려준다 — 화면이 "근거를 찾지 못했다"고 말해야
사람이 규약을 쓰거나 직접 확인할 수 있다.
"""
import re

# 커밋 메시지에서 찾는 작업 참조. `task #3`·`Task#3`·`[task #3]` 모두 같은 것으로 읽는다 —
# 사람이 적는 문구를 한 형태로 못 박으면 규약을 지켰는데도 안 잡히는 일이 생긴다.
_REF_RE = re.compile(r"task\s*#\s*(\d{1,9})", re.IGNORECASE)

# 문서·안내에 싣는 규약 문구. 한 곳에서만 정해 화면·문서·MCP가 같은 말을 하게 한다.
COMMIT_CONVENTION = "task #<작업번호>"
CONVENTION_HINT = (
    f"커밋 메시지에 `{COMMIT_CONVENTION}`을 넣으면 그 커밋이 기본 브랜치에 반영될 때 "
    "해당 작업이 자동으로 완료로 바뀝니다 (예: `feat: 로그인 API 추가 (task #3)`)."
)


def refs_in_message(message: str) -> set[int]:
    """커밋 메시지 하나가 가리키는 작업 번호들. 한 커밋이 여러 작업을 끝낼 수 있다."""
    return {int(m.group(1)) for m in _REF_RE.finditer(message or "")}


def commit_by_task(entries: list[tuple[str, str]]) -> dict[int, str]:
    """(sha, 메시지) 목록 → {작업번호: sha}.

    entries는 최신 순으로 들어온다(workspace.log_messages). 같은 작업을 여러 커밋이
    가리키면 **가장 오래된 것**을 남긴다 — 그 작업이 언제 들어왔는지가 판정 기준이고,
    나중 커밋(수정·리팩터링)이 아니라 처음 반영된 커밋이 그 답이다.
    """
    found: dict[int, str] = {}
    for sha, message in entries:
        for task_id in refs_in_message(message):
            found[task_id] = sha  # 최신 순으로 훑으므로 뒤에 오는(=더 오래된) 것이 이긴다
    return found
